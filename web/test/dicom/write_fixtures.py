"""Writes synthetic DICOM files to web/test/fixtures/dicom/, covering the
rules of the DICOM profile (profiles/dicom.md §6): the native transfer
syntaxes in both byte orders, signed and 8/16/32-bit data, RGB planar and
interleaved, multi-frame images, JPEG and JPEG 2000 frames with and without
offset tables and split into fragments, whole-slide images, sequences of
undefined length before the pixel data, and the inputs the profile rejects
(`dicom_reject_*`).

The files are written with pydicom; JPEG and JPEG 2000 frames are encoded
with imagecodecs. web/test/dicom/verify.py checks the virtualized pixels
against pydicom (and imagecodecs, for the encapsulated frames).

Usage: uv run python web/test/dicom/write_fixtures.py
"""

from __future__ import annotations

import struct
from pathlib import Path

import imagecodecs
import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import encapsulate, encapsulate_extended
from pydicom.sequence import Sequence
from pydicom.uid import (
    UID, ExplicitVRBigEndian, ExplicitVRLittleEndian, ImplicitVRLittleEndian, JPEG2000, JPEG2000Lossless,
    JPEGBaseline8Bit, JPEGLSLossless, RLELossless, generate_uid,
)

OUT = Path(__file__).parents[1] / "fixtures" / "dicom"
SECONDARY_CAPTURE = UID("1.2.840.10008.5.1.4.1.1.7")
MULTIFRAME_GRAYSCALE = UID("1.2.840.10008.5.1.4.1.1.7.3")
CT = UID("1.2.840.10008.5.1.4.1.1.2")
WHOLE_SLIDE = UID("1.2.840.10008.5.1.4.1.1.77.1.6")
RNG = np.random.default_rng(6)


def image(sop_class: UID, rows: int, columns: int, spp: int = 1, photometric: str = "MONOCHROME2",
          bits: int = 8, stored: int | None = None, signed: bool = False, frames: int | None = None,
          planar: int | None = None) -> Dataset:
    ds = Dataset()
    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = generate_uid(prefix="1.2.826.0.1.3680043.8.498.", entropy_srcs=[str(rows), str(RNG.random())])
    ds.Modality = "OT"
    ds.SamplesPerPixel = spp
    ds.PhotometricInterpretation = photometric
    if spp == 3:
        ds.PlanarConfiguration = 0 if planar is None else planar
    if frames is not None:
        ds.NumberOfFrames = frames
    ds.Rows, ds.Columns = rows, columns
    ds.BitsAllocated = bits
    ds.BitsStored = bits if stored is None else stored
    ds.HighBit = ds.BitsStored - 1
    ds.PixelRepresentation = 1 if signed else 0
    return ds


def save(name: str, ds: Dataset, syntax: UID) -> None:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = ds.SOPClassUID
    meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    meta.TransferSyntaxUID = syntax
    ds.file_meta = meta
    ds.preamble = b"\0" * 128
    ds.save_as(OUT / f"{name}.dcm", enforce_file_format=True)


def native(ds: Dataset, pixels: np.ndarray, big: bool = False, vr: str = "OW") -> Dataset:
    """Stores `pixels` ([frames,] rows, columns[, samples]) as native pixel data."""
    data = pixels
    if ds.SamplesPerPixel == 3 and ds.PlanarConfiguration == 1:
        data = np.moveaxis(pixels, -1, -3)  # samples before rows
    data = data.astype(data.dtype.newbyteorder(">" if big else "<"))
    raw = data.tobytes()
    ds.PixelData = raw + (b"\0" if len(raw) % 2 else b"")
    ds["PixelData"].VR = vr
    return ds


def ramp(shape: tuple[int, ...], dtype) -> np.ndarray:
    info = np.iinfo(dtype)
    return RNG.integers(max(info.min, -30000), min(info.max, 60000), size=shape, endpoint=True).astype(dtype)


def smooth(frames: int, rows: int, columns: int, spp: int) -> np.ndarray:
    """Smooth uint8 pictures, which JPEG keeps well."""
    y, x = np.mgrid[0:rows, 0:columns]
    out = np.empty((frames, rows, columns, spp), np.uint8)
    for f in range(frames):
        for s in range(spp):
            out[f, :, :, s] = (128 + 100 * np.sin((x + 3 * f + 7 * s) / 5) * np.cos((y - 2 * s) / 7)).astype(np.uint8)
    return out if spp == 3 else out[..., 0]


def jpeg(pixels: np.ndarray, photometric: str) -> bytes:
    if photometric == "RGB":
        # Stored as RGB, then made silent about it (no APP14, component ids
        # 1, 2, 3): a decoder that guesses would take it for YCbCr, so the
        # profile's Adobe marker decides (profiles/dicom.md §6.5).
        return _strip_adobe(imagecodecs.jpeg8_encode(pixels, level=95, colorspace="RGB", outcolorspace="RGB"))
    if photometric == "YBR_FULL_422":
        return imagecodecs.jpeg8_encode(pixels, level=95, subsampling="422")
    if photometric == "YBR_FULL":
        return imagecodecs.jpeg8_encode(pixels, level=95, subsampling="444")
    return imagecodecs.jpeg8_encode(pixels, level=95)


def _strip_adobe(stream: bytes) -> bytes:
    out, pos = bytearray(stream[:2]), 2
    while pos < len(stream):
        marker = stream[pos + 1]
        length = struct.unpack(">H", stream[pos + 2 : pos + 4])[0]
        segment = bytearray(stream[pos : pos + 2 + length])
        if marker == 0xC0:  # SOF0: component ids R, G, B -> 1, 2, 3
            for k in range(3):
                segment[10 + 3 * k] = k + 1
        if marker == 0xDA:  # SOS: the same
            for k in range(segment[4]):
                segment[5 + 2 * k] = k + 1
        if marker != 0xEE:
            out += segment
        pos += 2 + length
        if marker == 0xDA:
            out += stream[pos:]
            break
    return bytes(out)


def j2k(pixels: np.ndarray, mct: bool = True) -> bytes:
    return imagecodecs.jpeg2k_encode(pixels, level=0, codecformat="J2K", reversible=True, mct=mct)


def sequences(ds: Dataset, implicit: bool = False) -> None:
    """Nested sequences of undefined length (and one of defined length, and
    an empty one) before the pixel data."""
    inner = Dataset()
    inner.CodeValue = "121327"
    inner.CodingSchemeDesignator = "DCM"
    inner.CodeMeaning = "Full fidelity image"
    item = Dataset()
    item.ReferencedSOPClassUID = SECONDARY_CAPTURE
    item.ReferencedSOPInstanceUID = generate_uid(entropy_srcs=["ref"])
    item.PurposeOfReferenceCodeSequence = Sequence([inner])
    item.PurposeOfReferenceCodeSequence.is_undefined_length = True
    inner.is_undefined_length_sequence_item = True
    item.is_undefined_length_sequence_item = True
    ds.SourceImageSequence = Sequence([item, Dataset()])
    ds["SourceImageSequence"].is_undefined_length = True
    ds.ReferencedImageSequence = Sequence([])
    ds["ReferencedImageSequence"].is_undefined_length = True
    ds.ReferencedStudySequence = Sequence([Dataset()])  # defined length, skipped


def shared_measures(ds: Dataset, spacing: list[str], between: str | None = None) -> None:
    pm = Dataset()
    pm.PixelSpacing = spacing
    if between is not None:
        pm.SpacingBetweenSlices = between
    fg = Dataset()
    fg.PixelMeasuresSequence = Sequence([pm])
    ds.SharedFunctionalGroupsSequence = Sequence([fg])


def whole_slide(ds: Dataset, width: int, height: int, organization: str = "TILED_FULL") -> None:
    ds.ImageType = ["ORIGINAL", "PRIMARY", "VOLUME", "NONE"]
    ds.DimensionOrganizationType = organization
    ds.TotalPixelMatrixColumns, ds.TotalPixelMatrixRows = width, height
    ds.TotalPixelMatrixFocalPlanes = 1
    ds.NumberOfOpticalPaths = 1
    shared_measures(ds, ["0.00025", "0.0005"])


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.dcm"):
        old.unlink()

    # ---- native
    ds = image(CT, 12, 10, bits=16, stored=12)
    ds.PixelSpacing = ["0.5", "0.25"]
    ds.WindowCenter, ds.WindowWidth = "40", "400"
    ds.RescaleIntercept, ds.RescaleSlope = "-1024", "1"
    save("dicom_implicit_mono16", native(ds, ramp((12, 10), np.uint16) & 0x0FFF), ImplicitVRLittleEndian)
    # The same file with a TIFF header in its preamble, as some toolkits write
    # it: §1.2 tests for DICM first, so it is still DICOM.
    (OUT / "dicom_tiff_preamble.dcm").write_bytes(
        b"II*\0" + (OUT / "dicom_implicit_mono16.dcm").read_bytes()[4:])

    ds = image(MULTIFRAME_GRAYSCALE, 6, 9, photometric="MONOCHROME1", bits=16, signed=True, frames=4)
    ds.SpacingBetweenSlices = "2.5"
    save("dicom_explicit_signed16_frames", native(ds, ramp((4, 6, 9), np.int16)), ExplicitVRLittleEndian)

    ds = image(MULTIFRAME_GRAYSCALE, 5, 7, bits=16, frames=2)
    ds.PixelSpacing = ["0.1", "0.1"]
    save("dicom_bigendian_mono16", native(ds, ramp((2, 5, 7), np.uint16), big=True), ExplicitVRBigEndian)

    ds = image(MULTIFRAME_GRAYSCALE, 3, 5, bits=8, frames=3)
    save("dicom_bigendian_mono8_ob", native(ds, ramp((3, 3, 5), np.uint8), big=True, vr="OB"), ExplicitVRBigEndian)

    ds = image(SECONDARY_CAPTURE, 4, 6, bits=32, signed=True)
    save("dicom_explicit_int32", native(ds, ramp((4, 6), np.int32)), ExplicitVRLittleEndian)

    ds = image(SECONDARY_CAPTURE, 5, 3, bits=8)
    sequences(ds)
    shared_measures(ds, ["0.2", "0.3"])
    ds.PixelSpacing = ["9", "9"]  # the shared functional groups' come first
    save("dicom_explicit_sequences_mono8", native(ds, ramp((5, 3), np.uint8), vr="OB"), ExplicitVRLittleEndian)

    ds = image(SECONDARY_CAPTURE, 4, 5, bits=16, stored=10)
    sequences(ds)
    save("dicom_implicit_sequences_mono16", native(ds, ramp((4, 5), np.uint16) & 0x3FF), ImplicitVRLittleEndian)

    ds = image(MULTIFRAME_GRAYSCALE, 5, 6, spp=3, photometric="RGB", frames=2, planar=0)
    save("dicom_rgb_interleaved", native(ds, ramp((2, 5, 6, 3), np.uint8), vr="OB"), ExplicitVRLittleEndian)

    ds = image(MULTIFRAME_GRAYSCALE, 5, 6, spp=3, photometric="RGB", frames=2, planar=1)
    save("dicom_rgb_planar", native(ds, ramp((2, 5, 6, 3), np.uint8), vr="OB"), ExplicitVRLittleEndian)

    ds = image(SECONDARY_CAPTURE, 3, 3, spp=3, photometric="RGB", bits=16, planar=1)
    save("dicom_rgb_planar16_implicit", native(ds, ramp((3, 3, 3), np.uint16)), ImplicitVRLittleEndian)

    # ---- JPEG Baseline
    pixels = smooth(3, 16, 16, 1)
    ds = image(MULTIFRAME_GRAYSCALE, 16, 16, frames=3)
    ds.PixelData = encapsulate([jpeg(p, "MONOCHROME2") for p in pixels], has_bot=True)
    save("dicom_jpeg_mono_bot", ds, JPEGBaseline8Bit)

    pixels = smooth(2, 16, 24, 3)
    ds = image(MULTIFRAME_GRAYSCALE, 16, 24, spp=3, photometric="YBR_FULL_422", frames=2)
    ds.PixelData = encapsulate([jpeg(p, "YBR_FULL_422") for p in pixels], has_bot=False)
    save("dicom_jpeg_ybr422_nobot", ds, JPEGBaseline8Bit)

    pixels = smooth(2, 16, 16, 3)
    ds = image(MULTIFRAME_GRAYSCALE, 16, 16, spp=3, photometric="RGB", frames=2)
    ds.PixelData = encapsulate([jpeg(p, "RGB") for p in pixels], fragments_per_frame=3, has_bot=True)
    save("dicom_jpeg_rgb_fragments_bot", ds, JPEGBaseline8Bit)

    pixels = smooth(3, 8, 16, 3)
    ds = image(MULTIFRAME_GRAYSCALE, 8, 16, spp=3, photometric="YBR_FULL", frames=3)
    ds.PixelData, ds.ExtendedOffsetTable, ds.ExtendedOffsetTableLengths = encapsulate_extended(
        [jpeg(p, "YBR_FULL") for p in pixels])
    save("dicom_jpeg_ybr_eot", ds, JPEGBaseline8Bit)

    pixels = smooth(1, 16, 16, 1)
    ds = image(SECONDARY_CAPTURE, 16, 16, photometric="MONOCHROME1")
    ds.PixelData = encapsulate([jpeg(pixels[0], "MONOCHROME2")], fragments_per_frame=4, has_bot=False)
    save("dicom_jpeg_single_frame_fragments", ds, JPEGBaseline8Bit)

    # ---- JPEG 2000
    pixels = ramp((3, 9, 7), np.uint16) & 0x0FFF
    ds = image(MULTIFRAME_GRAYSCALE, 9, 7, bits=16, stored=12, frames=3)
    ds.PixelData, ds.ExtendedOffsetTable, ds.ExtendedOffsetTableLengths = encapsulate_extended(
        [j2k(p) for p in pixels])
    save("dicom_j2k_mono16_eot", ds, JPEG2000Lossless)

    pixels = ramp((2, 8, 8, 3), np.uint8)
    ds = image(MULTIFRAME_GRAYSCALE, 8, 8, spp=3, photometric="YBR_RCT", frames=2)
    ds.PixelData = encapsulate([j2k(p) for p in pixels], fragments_per_frame=2, has_bot=True)
    save("dicom_j2k_rct_fragments_bot", ds, JPEG2000Lossless)

    pixels = ramp((2, 6, 5, 3), np.uint8)
    ds = image(MULTIFRAME_GRAYSCALE, 6, 5, spp=3, photometric="RGB", frames=2)
    ds.PixelData = encapsulate([j2k(p, mct=False) for p in pixels], has_bot=False)
    save("dicom_j2k_rgb_nobot", ds, JPEG2000)

    pixels = ramp((7, 6), np.int16)
    ds = image(SECONDARY_CAPTURE, 7, 6, bits=16, signed=True)
    ds.PixelData = encapsulate([j2k(pixels)], has_bot=True)
    sequences(ds)
    save("dicom_j2k_signed16", ds, JPEG2000Lossless)

    # ---- whole-slide images: 40 x 30 pixels in 16 x 16 tiles, 3 across and 2 down
    tiles = smooth(6, 16, 16, 3)
    ds = image(WHOLE_SLIDE, 16, 16, spp=3, photometric="YBR_FULL_422", frames=6)
    whole_slide(ds, 40, 30)
    ds.PixelData = encapsulate([jpeg(t, "YBR_FULL_422") for t in tiles], has_bot=True)
    save("dicom_wsi_tiled_full_jpeg", ds, JPEGBaseline8Bit)

    ds = image(WHOLE_SLIDE, 8, 8, frames=4)
    whole_slide(ds, 13, 9)
    save("dicom_wsi_tiled_full_native", native(ds, ramp((4, 8, 8), np.uint8), vr="OB"), ExplicitVRLittleEndian)

    rejects()


def rejects() -> None:
    def mono(frames: int | None = None, bits: int = 8) -> Dataset:
        ds = image(MULTIFRAME_GRAYSCALE if frames else SECONDARY_CAPTURE, 4, 4, bits=bits, frames=frames)
        return ds

    def pixels(ds: Dataset, n: int = 1) -> Dataset:
        return native(ds, ramp((n, 4, 4), np.uint8 if ds.BitsAllocated == 8 else np.uint16), vr="OB")

    for name, syntax in (("dicom_reject_rle", RLELossless), ("dicom_reject_jpegls", JPEGLSLossless)):
        ds = mono()
        ds.PixelData = encapsulate([bytes(16)])
        save(name, ds, syntax)

    ds = image(SECONDARY_CAPTURE, 4, 4, photometric="PALETTE COLOR")
    save("dicom_reject_palette", pixels(ds), ExplicitVRLittleEndian)

    ds = image(SECONDARY_CAPTURE, 4, 4, spp=3, photometric="YBR_FULL")
    save("dicom_reject_native_ybr", native(ds, ramp((4, 4, 3), np.uint8), vr="OB"), ExplicitVRLittleEndian)

    ds = image(WHOLE_SLIDE, 4, 4, frames=4)
    whole_slide(ds, 8, 8, organization="TILED_SPARSE")
    save("dicom_reject_tiled_sparse", pixels(ds, 4), ExplicitVRLittleEndian)

    ds = image(WHOLE_SLIDE, 4, 4, frames=8)
    whole_slide(ds, 8, 8)
    ds.TotalPixelMatrixFocalPlanes = 2
    save("dicom_reject_focal_planes", pixels(ds, 8), ExplicitVRLittleEndian)

    ds = image(WHOLE_SLIDE, 4, 4, frames=3)
    whole_slide(ds, 8, 8)
    save("dicom_reject_wsi_frames", pixels(ds, 3), ExplicitVRLittleEndian)

    tiles = smooth(2, 8, 8, 1)
    ds = image(MULTIFRAME_GRAYSCALE, 8, 8, frames=2)
    ds.PixelData = encapsulate([jpeg(t, "MONOCHROME2") for t in tiles], fragments_per_frame=2, has_bot=False)
    save("dicom_reject_fragments_without_offsets", ds, JPEGBaseline8Bit)

    ds = image(MULTIFRAME_GRAYSCALE, 8, 8, frames=2)
    data = bytearray(encapsulate([jpeg(t, "MONOCHROME2") for t in tiles], fragments_per_frame=2, has_bot=True))
    data[12:16] = struct.pack("<I", struct.unpack("<I", data[12:16])[0] + 2)  # not a fragment's position
    ds.PixelData = bytes(data)
    save("dicom_reject_bot_offset", ds, JPEGBaseline8Bit)

    ds = image(MULTIFRAME_GRAYSCALE, 8, 8, frames=2)
    eot = encapsulate_extended([jpeg(t, "MONOCHROME2") for t in tiles])
    ds.PixelData = encapsulate([jpeg(t, "MONOCHROME2") for t in tiles], has_bot=True)
    ds.ExtendedOffsetTable, ds.ExtendedOffsetTableLengths = eot[1], eot[2]
    save("dicom_reject_eot_with_bot", ds, JPEGBaseline8Bit)

    ds = mono(frames=3)
    save("dicom_reject_short_pixel_data", pixels(ds, 2), ExplicitVRLittleEndian)

    ds = mono()
    ds.HighBit = 3
    save("dicom_reject_high_bit", pixels(ds), ExplicitVRLittleEndian)

    ds = image(SECONDARY_CAPTURE, 4, 4, bits=16)
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = 12, 12, 11
    save("dicom_reject_bits_allocated", native(ds, ramp((4, 4), np.uint16)), ExplicitVRLittleEndian)

    ds = mono(frames=2)
    save("dicom_reject_bigendian_ow8", native(ds, ramp((2, 4, 4), np.uint8), big=True, vr="OW"),
         ExplicitVRBigEndian)

    ds = mono()
    ds.NumberOfFrames = 0
    save("dicom_reject_zero_frames", pixels(ds), ExplicitVRLittleEndian)

    ds = mono()
    sequences(ds)
    save("dicom_reject_no_pixel_data", ds, ExplicitVRLittleEndian)

    ds = mono()
    sequences(ds)
    save("dicom_reject_misplaced_delimiter", pixels(ds), ExplicitVRLittleEndian)
    _patch(  # the sequence delimiter of SourceImageSequence becomes an item delimiter
        "dicom_reject_misplaced_delimiter",
        lambda b: b.replace(b"\xfe\xff\xdd\xe0\x00\x00\x00\x00", b"\xfe\xff\x0d\xe0\x00\x00\x00\x00", 1))

    ds = mono()
    sequences(ds)
    save("dicom_reject_truncated_sequence", pixels(ds), ExplicitVRLittleEndian)
    _patch("dicom_reject_truncated_sequence", lambda b: b[: b.index(bytes.fromhex("08001221") + b"SQ") + 40])

    ds = mono()
    ds.Rows = 4
    save("dicom_reject_unknown_vr", pixels(ds), ExplicitVRLittleEndian)
    _patch("dicom_reject_unknown_vr", lambda b: b.replace(b"\x28\x00\x10\x00US", b"\x28\x00\x10\x00ZZ", 1))

    ds = mono()
    save("dicom_reject_rows_vr", pixels(ds), ExplicitVRLittleEndian)
    _patch("dicom_reject_rows_vr", lambda b: b.replace(b"\x28\x00\x10\x00US", b"\x28\x00\x10\x00SS", 1))

    ds = mono()
    nested = Dataset()
    for _ in range(65):
        outer = Dataset()
        outer.ReferencedImageSequence = Sequence([nested])
        outer["ReferencedImageSequence"].is_undefined_length = True
        nested = outer
    ds.ReferencedImageSequence = nested.ReferencedImageSequence
    ds["ReferencedImageSequence"].is_undefined_length = True
    save("dicom_reject_depth", pixels(ds), ExplicitVRLittleEndian)


def later() -> None:
    """Fixtures added after the others, last so that the seeded UIDs of the
    others do not change."""
    # Cine frames: the Frame Increment Pointer names Frame Time, so the frames
    # are along t, 33.3 ms apart (conventions/dicom/README.md §4).
    ds = image(MULTIFRAME_GRAYSCALE, 4, 6, bits=8, frames=5)
    ds.FrameIncrementPointer = 0x00181063
    ds.FrameTime = "33.3"
    save("dicom_cine_frame_time", native(ds, ramp((5, 4, 6), np.uint8)), ExplicitVRLittleEndian)

    # A Frame Increment Pointer whose length is not a multiple of 4.
    ds = image(MULTIFRAME_GRAYSCALE, 4, 4, frames=2)
    ds.FrameIncrementPointer = 0x00181063
    save("dicom_reject_increment_length", native(ds, ramp((2, 4, 4), np.uint8), vr="OB"), ExplicitVRLittleEndian)
    _patch("dicom_reject_increment_length", lambda b: b.replace(
        b"\x28\x00\x09\x00AT\x04\x00\x18\x00\x63\x10", b"\x28\x00\x09\x00AT\x02\x00\x18\x00", 1))


def reconstruction() -> None:
    """Values the source metadata keeps as arrays, on vzip_source, and elements
    after Pixel Data (conventions/dicom/README.md §5)."""
    from pydicom.dataelem import DataElement
    from pydicom.sequence import Sequence

    ds = image(MULTIFRAME_GRAYSCALE, 4, 4, bits=8, frames=2)
    ds.add_new(0x00091010, "OB", bytes(range(200)))  # a large private binary value: an array
    ds.add_new(0x00090010, "LO", "VZIP TEST")
    ds.add_new(0x00283006, "US", list(range(100)))  # LUT Data: 100 values, an array
    frames = Sequence()
    for z in range(2):
        item = Dataset()
        pos = Dataset()
        pos.ImagePositionPatient = ["0", "0", str(z)]
        item.PlanePositionSequence = Sequence([pos])
        frames.append(item)
    ds.PerFrameFunctionalGroupsSequence = frames  # on vzip_source
    icon = Dataset()
    icon.Rows, icon.Columns, icon.BitsAllocated, icon.BitsStored = 8, 8, 8, 8
    icon.add_new(0x7FE00010, "OB", bytes(64 + 8 * 8 - 64))  # 64 bytes: kept as InlineBinary
    big_icon = Dataset()
    big_icon.add_new(0x7FE00010, "OB", bytes(range(100)))  # 100 bytes: an array
    ds.IconImageSequence = Sequence([icon, big_icon])
    ds = native(ds, ramp((2, 4, 4), np.uint8), vr="OB")
    ds.add_new(0x7FE10010, "LO", "VZIP AFTER")  # after Pixel Data: still metadata
    ds.add_new(0x7FE11001, "OB", bytes(range(90)))
    ds.add_new(0xFFFCFFFC, "OB", bytes(16))  # padding: not recorded
    save("dicom_reconstruction", ds, ExplicitVRLittleEndian)


def _patch(name: str, f) -> None:
    path = OUT / f"{name}.dcm"
    before = path.read_bytes()
    after = f(before)
    assert after != before, name
    path.write_bytes(after)


def review() -> None:
    """What else the source metadata keeps (conventions/dicom/README.md §5):
    duplicate elements, values under a multibyte character set, large values,
    per-frame values, a preamble, and the pixel data that no frame holds."""
    from pydicom.filebase import DicomBytesIO
    from pydicom.filewriter import write_dataset, write_file_meta_info

    def encode(ds: Dataset) -> bytes:
        fp = DicomBytesIO()
        fp.is_little_endian, fp.is_implicit_VR = True, False
        write_dataset(fp, ds)
        return fp.getvalue()

    def element(tag: int, vr: str, value: bytes, length: int | None = None) -> bytes:
        """An element in explicit VR little endian, of any length and order."""
        n = len(value) if length is None else length
        if vr in ("OB", "OD", "OF", "OL", "OV", "OW", "SQ", "SV", "UC", "UN", "UR", "UT", "UV"):
            return struct.pack("<HH2sHI", tag >> 16, tag & 0xFFFF, vr.encode(), 0, n) + value
        return struct.pack("<HH2sH", tag >> 16, tag & 0xFFFF, vr.encode(), n) + value

    def item(body: bytes, undefined: bool = False) -> bytes:
        if undefined:
            return struct.pack("<HHI", 0xFFFE, 0xE000, 0xFFFFFFFF) + body + struct.pack("<HHI", 0xFFFE, 0xE00D, 0)
        return struct.pack("<HHI", 0xFFFE, 0xE000, len(body)) + body

    def raw(name: str, ds: Dataset, parts: list[bytes], preamble: bytes = bytes(128)) -> None:
        """A file of `parts`, as they are: pydicom does not write duplicates."""
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = ds.SOPClassUID
        meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        fp = DicomBytesIO()
        fp.is_little_endian, fp.is_implicit_VR = True, False
        write_file_meta_info(fp, meta, enforce_standard=True)
        (OUT / f"{name}.dcm").write_bytes(preamble + b"DICM" + fp.getvalue() + b"".join(parts))

    def pixels(ds: Dataset, n: int = 1) -> bytes:
        px = Dataset()
        px.PixelData = ramp((n, ds.Rows, ds.Columns), np.uint8).tobytes()
        px["PixelData"].VR = "OB"
        return encode(px)

    # Duplicates: a later element with a tag already in its dataset, at the top
    # level (a sequence among them) and in an item. After Pixel Data, a sequence
    # of undefined length, then a duplicate, which ends the elements: the rest
    # is trailing bytes.
    ds = image(SECONDARY_CAPTURE, 4, 4)
    ds.PatientName = "First^Name"
    ref = Dataset()
    ref.ReferencedSOPInstanceUID = "1.2.3"
    ds.ReferencedImageSequence = Sequence([ref])
    dup = Dataset()
    dup.PatientName = "Second^Name"
    refs = [Dataset(), Dataset()]
    refs[0].ReferencedSOPInstanceUID, refs[1].ReferencedSOPInstanceUID = "4.5.6", "7.8"
    dup.ReferencedImageSequence = Sequence(refs)
    code = element(0x00080100, "SH", b"A1") + element(0x00080102, "SH", b"DCM ") + element(0x00080100, "SH", b"B2")
    after = (element(0x7FE10010, "LO", b"VZIP AFTER")
             + element(0x7FE11010, "SQ", item(element(0x00080100, "SH", b"C3"), undefined=True)
                       + struct.pack("<HHI", 0xFFFE, 0xE0DD, 0), length=0xFFFFFFFF)
             + element(0x00100010, "PN", b"Third^Name")
             + element(0x7FE10011, "LO", b"TRAILING"))
    raw("dicom_duplicates", ds, [encode(ds), encode(dup), element(0x00082112, "SQ", item(code)), pixels(ds), after])

    # Multibyte character sets: GBK, whose second bytes include 5C, inherited by
    # one item and overridden by another; and ISO 2022 IR 87, where 3D and 5C
    # occur inside the escaped characters.
    ds = image(SECONDARY_CAPTURE, 4, 4)
    ds.SpecificCharacterSet = "GBK"
    ds.InstitutionName = "\u4e57 Hospital"  # GBK 81 5C
    ds.PatientName = "\u4e57^\u592a\u90ce"
    inherits, utf8 = Dataset(), Dataset()
    inherits.CodeMeaning = "\u4e57"
    utf8.SpecificCharacterSet = "ISO_IR 192"
    utf8.CodeMeaning = "\u00dcn\u00efcode"
    ds.ConceptNameCodeSequence = Sequence([inherits, utf8])
    save("dicom_charset_gbk", native(ds, ramp((4, 4), np.uint8), vr="OB"), ExplicitVRLittleEndian)

    ds = image(SECONDARY_CAPTURE, 4, 4)
    ds.SpecificCharacterSet = ["", "ISO 2022 IR 87"]
    ds.PatientName = "Yamada^Tarou=\u5c71\u7530^\u592a\u90ce=\u3084\u307e\u3060^\u305f\u308d\u3046"
    ds.StudyDescription = "\u4fd1X"  # JIS 50 5C
    save("dicom_charset_jis", native(ds, ramp((4, 4), np.uint8), vr="OB"), ExplicitVRLittleEndian)

    # Large values, and values that are not what their VR says.
    ds = image(SECONDARY_CAPTURE, 4, 4)
    ds.SpatialResolution = "9007199254740993"  # not a binary64 value: text
    ds.SliceThickness = "0.1000000000000000055511151231257827"
    ds.add_new(0x30060050, "DS", [str(x / 4) for x in range(100)])  # 100 values: an array of their text
    ds.add_new(0x00081160, "IS", list(range(70)))
    ds.add_new(0x0040A160, "UT", "x" * 70000)  # over 64 KiB: an array
    series = Sequence()
    for i in range(300):
        d = Dataset()
        d.SeriesInstanceUID = f"1.2.826.0.1.3680043.8.498.{i}"
        series.append(d)
    ds.ReferencedSeriesSequence = series  # over 16 KiB as JSON: on vzip_source
    odd = (element(0x00090010, "LO", b"VZIP")
           + element(0x00091001, "US", b"\x01\x02\x03")  # not whole values: as stored
           + element(0x00091002, "AT", b"\x28\x00\x10\x00\x28\x00")
           + element(0x00091003, "FL", bytes(i % 251 for i in range(70 * 4 + 2))))  # and large: an array of bytes
    raw("dicom_large_values", ds, [encode(ds), odd, pixels(ds)], preamble=b"VZIP PREAMBLE".ljust(128, b"."))

    # Per-frame values, gathered across the frames: of one length (an array of
    # bytes, and one of values) or not (a family).
    ds = image(MULTIFRAME_GRAYSCALE, 2, 3, frames=4)
    frames = Sequence()
    for f in range(4):
        fg = Dataset()
        fg.add_new(0x00090010, "LO", "VZIP")
        fg.add_new(0x00091001, "OB", bytes([f]) * 100)
        fg.add_new(0x00091002, "US", list(range(f, f + 70)))
        fg.add_new(0x00091003, "OB", bytes(range(80 + 2 * f)))
        content = Dataset()
        content.FrameAcquisitionNumber = f
        fg.FrameContentSequence = Sequence([content])
        frames.append(fg)
    ds.PerFrameFunctionalGroupsSequence = frames
    save("dicom_per_frame_values", native(ds, ramp((4, 2, 3), np.uint8), vr="OB"), ExplicitVRLittleEndian)

    # Native pixel data longer than its frames.
    ds = image(MULTIFRAME_GRAYSCALE, 4, 4, frames=2)
    native(ds, ramp((2, 4, 4), np.uint8), vr="OB")
    ds.PixelData += bytes(range(1, 7))
    save("dicom_pixel_extra", ds, ExplicitVRLittleEndian)

    # An Extended Offset Table that skips fragments: one before the first frame,
    # one between the first and the second.
    tiles = smooth(3, 8, 8, 1)
    ds = image(MULTIFRAME_GRAYSCALE, 8, 8, frames=3)

    def fragment(b: bytes) -> bytes:
        return item(b + (b"\0" if len(b) % 2 else b""))

    body = fragment(b"unreferenced")
    offsets, lengths = [], []
    for k, t in enumerate(tiles):
        if k == 1:
            body += fragment(b"skipped fragment")
        f = fragment(jpeg(t, "MONOCHROME2"))
        offsets.append(len(body))
        lengths.append(len(f) - 8)
        body += f
    ds.PixelData = item(b"") + body
    ds["PixelData"].is_undefined_length = True
    ds.ExtendedOffsetTable = struct.pack("<3Q", *offsets)
    ds.ExtendedOffsetTableLengths = struct.pack("<3Q", *lengths)
    save("dicom_eot_unreferenced", ds, JPEGBaseline8Bit)


def review2() -> None:
    """More of what the source metadata keeps (conventions/dicom/README.md §5):
    per-frame values whose paths nest, numbers too long to convert, a root
    kept small, and Extended Offset Table frames without their item headers;
    and a JPEG frame that is not a JPEG stream, which the profile rejects."""
    from pydicom.filebase import DicomBytesIO
    from pydicom.filewriter import write_dataset

    def encode(ds: Dataset) -> bytes:
        fp = DicomBytesIO()
        fp.is_little_endian, fp.is_implicit_VR = True, False
        write_dataset(fp, ds)
        return fp.getvalue()

    def element(tag: int, vr: str, value: bytes) -> bytes:
        if vr in ("OB", "OD", "OF", "OL", "OV", "OW", "SQ", "SV", "UC", "UN", "UR", "UT", "UV"):
            return struct.pack("<HH2sHI", tag >> 16, tag & 0xFFFF, vr.encode(), 0, len(value)) + value
        return struct.pack("<HH2sH", tag >> 16, tag & 0xFFFF, vr.encode(), len(value)) + value

    def item(body: bytes) -> bytes:
        return struct.pack("<HHI", 0xFFFE, 0xE000, len(body)) + body

    def raw(name: str, ds: Dataset, parts: list[bytes], syntax: UID = ExplicitVRLittleEndian,
            meta_extra: bytes = b"") -> None:
        """A file of `parts`, as they are, after a File Meta Information that
        ends with `meta_extra`."""
        meta = (element(0x00020001, "OB", b"\0\1")
                + element(0x00020002, "UI", ds.SOPClassUID.encode() + b"\0" * (len(ds.SOPClassUID) % 2))
                + element(0x00020003, "UI", ds.SOPInstanceUID.encode() + b"\0" * (len(ds.SOPInstanceUID) % 2))
                + element(0x00020010, "UI", syntax.encode() + b"\0" * (len(syntax) % 2))
                + meta_extra)
        group = element(0x00020000, "UL", struct.pack("<I", len(meta)))
        (OUT / f"{name}.dcm").write_bytes(bytes(128) + b"DICM" + group + meta + b"".join(parts))

    def native_pixels(ds: Dataset, n: int = 1) -> bytes:
        return element(0x7FE00010, "OB", ramp((n, ds.Rows, ds.Columns), np.uint8).tobytes())

    def around(ds: Dataset, middle: bytes, at: int) -> bytes:
        """The dataset, with `middle` before its first tag from `at`."""
        lower, upper = Dataset(), Dataset()
        for el in ds:
            (lower if el.tag < at else upper).add(el)
        return encode(lower) + middle + encode(upper)

    # Per-frame values whose paths within their items nest: (0020,9111) is a
    # sequence that breaks a rule (its bytes) in item 0, and holds a value
    # that is an array in item 1; (0009,1001) is a value in item 0, and a
    # sequence that holds one in item 1.
    ds = image(MULTIFRAME_GRAYSCALE, 2, 2, frames=2)
    broken = element(0x00209111, "SQ", element(0x00100010, "PN", b"AB"))  # an element where an item is expected
    holds = element(0x00209111, "SQ", item(element(0x00091001, "OB", bytes(range(100)))))
    first = element(0x00090010, "LO", b"VZIP") + element(0x00091001, "OB", bytes(100)) + broken
    second = (element(0x00090010, "LO", b"VZIP")
              + element(0x00091001, "SQ", item(element(0x00091002, "OB", bytes(range(1, 101))))) + holds)
    raw("dicom_per_frame_nested", ds, [encode(ds), element(0x52009230, "SQ", item(first) + item(second)),
                                        native_pixels(ds, 2)])

    # Numbers too long to convert: an IS value of 5000 digits, and DS values
    # whose exponents have 5001 digits, of which the first are leading zeros
    # (10), or not (out of range: text); and with digits all zero (0).
    ds = image(SECONDARY_CAPTURE, 2, 2)
    long_is = element(0x00200012, "IS", b"-" + b"9" * 4999)
    long_ds = element(0x00201041, "DS", b"1e" + b"0" * 5000 + b"1\\1e-" + b"1" * 5001 + b"\\0e" + b"9" * 5000 + b" ")
    raw("dicom_long_numbers", ds, [around(ds, long_is + long_ds, 0x00280000), native_pixels(ds)])

    # A root kept small: a File Meta Information duplicate of over 16 KiB, and
    # private text values of under 16 KiB each that make the dataset too large
    # for the root, of which the largest move to vzip_source, the first by tag
    # of two of equal size.
    ds = image(SECONDARY_CAPTURE, 2, 2)
    duplicates = b"".join(element(0x00020013, "SH", b"VZIP%04d" % i + b"." * 992) for i in range(20))
    private = element(0x00110010, "LO", b"VZIP")
    for k, n in enumerate((14000, 15002, 12000, 15002, 9000)):
        private += element(0x00111000 + k, "LT", bytes(65 + (i + k) % 26 for i in range(n)))
    raw("dicom_root_budget", ds, [around(ds, private, 0x00280000), native_pixels(ds)],
        meta_extra=element(0x00020013, "SH", b"VZIP") + duplicates)

    # An Extended Offset Table whose second frame's item is longer than the
    # frame (two bytes of padding after it): the 8 bytes before each frame's
    # data are kept with the bytes between the frames.
    tiles = smooth(3, 8, 8, 1)
    ds = image(MULTIFRAME_GRAYSCALE, 8, 8, frames=3)
    body, offsets, lengths = b"", [], []
    for k, t in enumerate(tiles):
        f = jpeg(t, "MONOCHROME2")
        f += b"\0" * (len(f) % 2)
        offsets.append(len(body))
        lengths.append(len(f))
        body += item(f + (b"\0\0" if k == 1 else b""))
    ds.PixelData = item(b"") + body
    ds["PixelData"].is_undefined_length = True
    ds.ExtendedOffsetTable = struct.pack("<3Q", *offsets)
    ds.ExtendedOffsetTableLengths = struct.pack("<3Q", *lengths)
    save("dicom_eot_item_headers", ds, JPEGBaseline8Bit)

    # An RGB JPEG frame that does not start with SOI (FF D8): its color
    # transform cannot be made explicit.
    ds = image(SECONDARY_CAPTURE, 8, 8, spp=3, photometric="RGB")
    ds.PixelData = encapsulate([jpeg(smooth(1, 8, 8, 3)[0], "RGB")[2:]])
    ds["PixelData"].is_undefined_length = True
    save("dicom_reject_jpeg_soi", ds, JPEGBaseline8Bit)


def review3() -> None:
    """Round three (conventions/dicom/README.md §5, profiles/dicom.md §6.5):
    native pixel data that runs past the file and overlapping Extended Offset
    Table frames, which the profile rejects; group lengths and offset tables
    that are not layout; explicit UN values in big endian; per-frame values
    as columns; and a frame of several fragments, one of them empty."""
    from pydicom.filebase import DicomBytesIO
    from pydicom.filewriter import write_dataset

    def encode(ds: Dataset, little: bool = True) -> bytes:
        fp = DicomBytesIO()
        fp.is_little_endian, fp.is_implicit_VR = little, False
        write_dataset(fp, ds)
        return fp.getvalue()

    def element(tag: int, vr: str, value: bytes, length: int | None = None, little: bool = True) -> bytes:
        n = len(value) if length is None else length
        o = "<" if little else ">"
        if vr in ("OB", "OD", "OF", "OL", "OV", "OW", "SQ", "SV", "UC", "UN", "UR", "UT", "UV"):
            return struct.pack(o + "HH2sHI", tag >> 16, tag & 0xFFFF, vr.encode(), 0, n) + value
        return struct.pack(o + "HH2sH", tag >> 16, tag & 0xFFFF, vr.encode(), n) + value

    def item(body: bytes) -> bytes:
        return struct.pack("<HHI", 0xFFFE, 0xE000, len(body)) + body

    def text(b: bytes) -> bytes:
        return b + b" " * (len(b) % 2)

    def raw(name: str, ds: Dataset, parts: list[bytes], syntax: UID = ExplicitVRLittleEndian) -> None:
        meta = (element(0x00020001, "OB", b"\0\1")
                + element(0x00020002, "UI", ds.SOPClassUID.encode() + b"\0" * (len(ds.SOPClassUID) % 2))
                + element(0x00020003, "UI", ds.SOPInstanceUID.encode() + b"\0" * (len(ds.SOPInstanceUID) % 2))
                + element(0x00020010, "UI", syntax.encode() + b"\0" * (len(syntax) % 2)))
        group = element(0x00020000, "UL", struct.pack("<I", len(meta)))  # its group's length: layout
        (OUT / f"{name}.dcm").write_bytes(bytes(128) + b"DICM" + group + meta + b"".join(parts))

    def around(ds: Dataset, middle: bytes, at: int, little: bool = True) -> bytes:
        lower, upper = Dataset(), Dataset()
        for el in ds:
            (lower if el.tag < at else upper).add(el)
        return encode(lower, little) + middle + encode(upper, little)

    # Native Pixel Data whose length runs past the end of the file, with
    # frames that lie within it.
    ds = image(SECONDARY_CAPTURE, 2, 2)
    raw("dicom_reject_pixel_past_eof", ds, [encode(ds), element(0x7FE00010, "OB", bytes(4), length=104)])

    # Extended Offset Table frames whose items overlap: both at offset 0.
    ds = image(MULTIFRAME_GRAYSCALE, 8, 8, frames=2)
    f = jpeg(smooth(1, 8, 8, 1)[0], "MONOCHROME2")
    f += b"\0" * (len(f) % 2)
    ds.PixelData = item(b"") + item(f)
    ds["PixelData"].is_undefined_length = True
    ds.ExtendedOffsetTable = struct.pack("<2Q", 0, 0)
    ds.ExtendedOffsetTableLengths = struct.pack("<2Q", len(f), len(f))
    save("dicom_reject_eot_overlap", ds, JPEGBaseline8Bit)

    # Group lengths: of group 0008 (its run's length: layout), of a private
    # group, one that differs from its run's length, two of one tag (all three
    # kept), and one in an item (layout); an offset table in an item and
    # after native Pixel Data (kept); and a sequence that breaks a rule, of
    # under 64 bytes (its bytes, inline).
    ds = image(SECONDARY_CAPTURE, 2, 2)
    run8 = element(0x00080016, "UI", ds.SOPClassUID.encode() + b"\0" * (len(ds.SOPClassUID) % 2))
    in_item = element(0x00080000, "UL", struct.pack("<I", 8 + 6)) + element(0x00081150, "UI", b"1.2.3\0")
    in_item += element(0x7FE00001, "OV", struct.pack("<Q", 7))
    run8 += element(0x00081140, "SQ", item(in_item))
    lower = (element(0x00080000, "UL", struct.pack("<I", len(run8))) + run8
             + element(0x00090000, "UL", struct.pack("<I", 12)) + element(0x00090010, "LO", b"VZIP")
             + element(0x00100000, "UL", struct.pack("<I", 99)) + element(0x00100010, "PN", b"Doe^Jane")
             + element(0x00180000, "UL", struct.pack("<I", 12)) + element(0x00180050, "DS", b"0.5 ")
             + element(0x00180000, "UL", struct.pack("<I", 0))
             + element(0x00209222, "SQ", element(0x00100010, "PN", b"AB")))  # an element where an item is expected
    after = (element(0x7FE00001, "OV", struct.pack("<Q", 123456789))
             + element(0x7FE00002, "OV", struct.pack("<Q", 42)) + element(0x7FE10010, "LO", b"KEEP"))
    upper = Dataset()
    for el in ds:
        if el.tag >= 0x00280000:
            upper.add(el)
    raw("dicom_layout_tags", ds, [lower, encode(upper), element(0x7FE00010, "OB", ramp((1, 2, 2), np.uint8).tobytes()),
                                  after])

    # An Extended Offset Table, with a group length of group 7FE0 that runs to
    # the end of Pixel Data (layout), and a second table of each tag, which is
    # kept, as a duplicate.
    tiles = smooth(2, 8, 8, 1)
    ds = image(MULTIFRAME_GRAYSCALE, 8, 8, frames=2)
    body, offsets, lengths = b"", [], []
    for t in tiles:
        f = jpeg(t, "MONOCHROME2")
        f += b"\0" * (len(f) % 2)
        offsets.append(len(body))
        lengths.append(len(f))
        body += item(f)
    pixel = (struct.pack("<HH2sHI", 0x7FE0, 0x0010, b"OB", 0, 0xFFFFFFFF) + item(b"") + body
             + struct.pack("<HHI", 0xFFFE, 0xE0DD, 0))
    tables = (element(0x7FE00001, "OV", struct.pack("<2Q", *offsets))
              + element(0x7FE00002, "OV", struct.pack("<2Q", *lengths)))
    group = tables + tables + pixel
    raw("dicom_eot_duplicate", ds, [encode(ds), element(0x7FE00000, "UL", struct.pack("<I", len(group))), group],
        JPEGBaseline8Bit)

    # Explicit VR big endian, with UN elements whose values are in little
    # endian (PS3.5 §6.2.2): Acquisition Matrix (US), Diffusion b-value (FD),
    # and 70 values of Real World Value LUT Data (FD): an array.
    ds = image(SECONDARY_CAPTURE, 2, 3, bits=16)
    un = (element(0x00181310, "UN", struct.pack("<4H", 0, 256, 256, 0), little=False)
          + element(0x00189087, "UN", struct.pack("<d", 1000.0), little=False)
          + element(0x00409212, "UN", struct.pack("<70d", *[k / 4 for k in range(70)]), little=False))
    pixels = ramp((2, 3), np.uint16).astype(">u2").tobytes()
    raw("dicom_un_bigendian", ds, [around(ds, un, 0x00280000, False), element(0x7FE00010, "OW", pixels, little=False)],
        ExplicitVRBigEndian)

    # Per-frame values as columns: positions (DS, every value a number: float64),
    # window centers (DS, one not a number, of the same length: their text),
    # window widths (DS, one not a number, of different lengths: a family), a frame number (IS:
    # int64), a stack position (UL, absent in one item), a datetime (DT: its
    # text), a private creator in one item only, and group lengths, layout
    # but in item 0 (a column of one row).
    n = 5
    ds = image(MULTIFRAME_GRAYSCALE, 2, 2, frames=n)
    items = b""
    for f in range(n):
        content = (element(0x00189074, "DT", b"20240101120000.%06d " % f)
                   + (element(0x00209057, "UL", struct.pack("<I", f + 1)) if f != 3 else b"")
                   + element(0x00209128, "IS", text(b"%d" % (10 * f))))
        position = element(0x00200032, "DS", text(b"-125.5\\130.25\\%d.5" % f))
        voi = (element(0x00281050, "DS", text(b"40.00000000000000000%d" % (f == 2)))  # 40.000000000000000001: no binary64
               + element(0x00281051, "DS", text(b"400" + b"0" * f) if f != 4 else text(b"0.10000000000000001")))
        group = element(0x00289145, "SQ", item(voi))
        length = element(0x00280000, "UL", struct.pack("<I", len(group) if f else 1))  # layout but in item 0
        body = (element(0x00189114, "SQ", b"") + element(0x00209111, "SQ", item(content))
                + element(0x00209113, "SQ", item(position)) + length + group)
        if f == 1:
            body = element(0x00090010, "LO", b"VZIP") + body
        items += item(body)
    raw("dicom_per_frame_columns", ds, [encode(ds), element(0x52009230, "SQ", items),
                                        element(0x7FE00010, "OB", ramp((n, 2, 2), np.uint8).tobytes())])

    # A Basic Offset Table, and a frame of three fragments, the first empty.
    tiles = smooth(2, 8, 8, 1)
    ds = image(MULTIFRAME_GRAYSCALE, 8, 8, frames=2)
    streams = []
    for t in tiles:
        f = jpeg(t, "MONOCHROME2")
        streams.append(f + b"\0" * (len(f) % 2))
    second = [b"", streams[1][:40], streams[1][40:]]
    body = item(streams[0]) + b"".join(item(x) for x in second)
    ds.PixelData = item(struct.pack("<2I", 0, 8 + len(streams[0]))) + body
    ds["PixelData"].is_undefined_length = True
    save("dicom_jpeg_empty_fragment", ds, JPEGBaseline8Bit)


def review4() -> None:
    """Round four (conventions/dicom/README.md §5, Gathered sequences): any
    sequence of more than 64 items gathered, at any depth, of defined and
    undefined length; sparse columns; the bounds past which a sequence keeps
    its bytes; and explicit UN values gathered in explicit VR big endian."""

    def element(tag: int, vr: str, value: bytes, little: bool = True, length: int | None = None) -> bytes:
        n = len(value) if length is None else length
        o = "<" if little else ">"
        if vr in ("OB", "OD", "OF", "OL", "OV", "OW", "SQ", "SV", "UC", "UN", "UR", "UT", "UV"):
            return struct.pack(o + "HH2sHI", tag >> 16, tag & 0xFFFF, vr.encode(), 0, n) + value
        return struct.pack(o + "HH2sH", tag >> 16, tag & 0xFFFF, vr.encode(), n) + value

    def item(body: bytes, little: bool = True, undefined: bool = False) -> bytes:
        o = "<" if little else ">"
        if undefined:
            return struct.pack(o + "HHI", 0xFFFE, 0xE000, 0xFFFFFFFF) + body + struct.pack(o + "HHI", 0xFFFE, 0xE00D, 0)
        return struct.pack(o + "HHI", 0xFFFE, 0xE000, len(body)) + body

    def undefined_sequence(tag: int, items: bytes) -> bytes:
        return element(tag, "SQ", items, length=0xFFFFFFFF) + struct.pack("<HHI", 0xFFFE, 0xE0DD, 0)

    def uid(b: bytes) -> bytes:
        return b + b"\0" * (len(b) % 2)

    def raw(name: str, ds: Dataset, before: bytes, after: bytes, frames: int, syntax: UID = ExplicitVRLittleEndian,
            little: bool = True) -> None:
        """A file of `ds`'s elements, `before` them, then `after` and the pixel data."""
        meta = (element(0x00020001, "OB", b"\0\1")
                + element(0x00020002, "UI", uid(ds.SOPClassUID.encode()))
                + element(0x00020003, "UI", uid(ds.SOPInstanceUID.encode()))
                + element(0x00020010, "UI", uid(syntax.encode())))
        group = element(0x00020000, "UL", struct.pack("<I", len(meta)))
        from pydicom.filebase import DicomBytesIO
        from pydicom.filewriter import write_dataset
        fp = DicomBytesIO()
        fp.is_little_endian, fp.is_implicit_VR = little, False
        write_dataset(fp, ds)
        pixels = element(0x7FE00010, "OB", ramp((frames, ds.Rows, ds.Columns), np.uint8).tobytes(), little)
        (OUT / f"{name}.dcm").write_bytes(bytes(128) + b"DICM" + group + meta + before + fp.getvalue() + after + pixels)

    # Gathered sequences: Referenced Series > Referenced Instance Sequence of
    # 100 items (one value of one length, one of different lengths), whose
    # item 0 holds a sequence of 66 items (in a gathered item: not gathered
    # itself); a Source Image Sequence of undefined length of 70 items, of
    # undefined length, a private value in 10 of them (sparse); a Content
    # Sequence of 64 items (not gathered); and per-frame items, 80, with a
    # value in each and one in 3 of them (sparse).
    n = 80
    ds = image(MULTIFRAME_GRAYSCALE, 2, 2, frames=n)
    purposes = element(0x0040A170, "SQ", b"".join(item(element(0x00080100, "SH", b"P%03d " % j)) for j in range(66)))
    instances = b"".join(item(element(0x00081150, "UI", uid(b"1.2.840.10008.5.1.4.1.1.2"))
                              + element(0x00081155, "UI", uid(b"1.2.3.%d" % (7 * i)))
                              + (purposes if i == 0 else b"")) for i in range(100))
    series = element(0x00081115, "SQ", item(element(0x0008114A, "SQ", instances)
                                             + element(0x0020000E, "UI", uid(b"1.2.3.9"))))
    sources = b"".join(item(element(0x00081150, "UI", uid(b"1.2.840.10008.5.1.4.1.1.4"))
                            + element(0x00081155, "UI", uid(b"1.2.4.%d" % (100 + i)))
                            + (element(0x00090010, "LO", b"VZIP") + element(0x00091001, "OB", bytes([i, 1]))
                               if i % 7 == 0 else b""), undefined=True) for i in range(70))
    before = series + undefined_sequence(0x00082112, sources)
    content = element(0x0040A730, "SQ", b"".join(item(element(0x0040A160, "UT", b"text %02d " % j)) for j in range(64)))
    frames = b"".join(item(element(0x00209111, "SQ", item(element(0x00209057, "UL", struct.pack("<I", f + 1))))
                           + (element(0x00290010, "LO", b"VZIP") + element(0x00291001, "FD", struct.pack("<d", f / 4))
                              if f in (3, 40, 79) else b"")) for f in range(n))
    raw("dicom_gathered_sequences", ds, before, content + element(0x52009230, "SQ", frames), n)

    # Past the bounds, a sequence keeps its bytes: per-frame items with 1025
    # distinct paths; a Referenced Image Sequence of undefined length of 65
    # items, whose 64 paths of values of two lengths each are families that
    # cost more than 16 times its bytes; and a Related Series Sequence of 65
    # items whose structures are over 64 KiB (3000 elements without values).
    ds = image(MULTIFRAME_GRAYSCALE, 2, 2, frames=1)
    many = element(0x00090010, "LO", b"VZIP") + b"".join(element(0x00091000 + j, "LO", b"AB") for j in range(1025))
    ragged = b"".join(item((element(0x00091000 + i - 1, "LO", b"ABCD") if i else b"")
                           + (element(0x00091000 + i, "LO", b"AB") if i < 64 else b"")) for i in range(65))
    empty = item(b"".join(element(0x00091000 + j, "LO", b"") for j in range(3000))) + item(b"") * 64
    before = undefined_sequence(0x00081140, ragged) + element(0x00081250, "SQ", empty)
    raw("dicom_gathered_bounds", ds, before, element(0x52009230, "SQ", item(many)), 1)

    # Explicit VR big endian: a per-frame value that is UL in item 0 and an
    # explicit UN (in little endian) in item 1, so gathered as bytes; and a
    # private explicit UN value, inline.
    ds = image(MULTIFRAME_GRAYSCALE, 2, 2, frames=2)
    b = lambda tag, vr, value: element(tag, vr, value, little=False)  # noqa: E731
    first = b(0x00209111, "SQ", item(b(0x00209057, "UL", struct.pack(">I", 1)), little=False))
    second = b(0x00209111, "SQ", item(b(0x00209057, "UN", struct.pack("<I", 1)), little=False))
    private = b(0x00090010, "LO", b"VZIP") + b(0x00091001, "UN", struct.pack("<2H", 1, 2))
    raw("dicom_un_bigendian_gathered", ds, private, b(0x52009230, "SQ", item(first, False) + item(second, False)), 2,
        ExplicitVRBigEndian, little=False)


if __name__ == "__main__":
    main()
    later()
    reconstruction()
    review()
    review2()
    review3()
    review4()
