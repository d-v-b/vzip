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


def _patch(name: str, f) -> None:
    path = OUT / f"{name}.dcm"
    before = path.read_bytes()
    after = f(before)
    assert after != before, name
    path.write_bytes(after)


if __name__ == "__main__":
    main()
