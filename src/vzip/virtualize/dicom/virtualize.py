"""The DICOM profile (profiles/dicom.md, §6)."""

from __future__ import annotations

import math
import re
import struct

from vzip.virtualize.common import (
    MAX_PAYLOAD, Output, Reader, Rejected, array_json, root_json, image_ome, payload_size, transpose_codec,
)
from vzip.virtualize.dicom.source import source_json
from vzip.virtualize.dicom.dataset import (
    EXPLICIT_LE, IMPLICIT_LE, ITEM, PIXEL_DATA, UNDEFINED, Dataset, Element, Encoding, Walker, tag_name,
)

MAX_SAFE = 2**53 - 1
# Transfer syntaxes (conventions/dicom/README.md §2.1): the dataset's encoding, and the codec of
# encapsulated frames (None for native pixel data).
SYNTAXES: dict[bytes, tuple[Encoding, str | None]] = {
    b"1.2.840.10008.1.2": (IMPLICIT_LE, None),
    b"1.2.840.10008.1.2.1": (EXPLICIT_LE, None),
    b"1.2.840.10008.1.2.2": (Encoding(True, False), None),
    b"1.2.840.10008.1.2.4.50": (EXPLICIT_LE, "jpeg"),
    b"1.2.840.10008.1.2.4.90": (EXPLICIT_LE, "jpeg2k"),
    b"1.2.840.10008.1.2.4.91": (EXPLICIT_LE, "jpeg2k"),
}
WHOLE_SLIDE = b"1.2.840.10008.5.1.4.1.1.77.1.6"
# Photometric interpretations by pixel data and samples per pixel (conventions/dicom/README.md §3).
PHOTOMETRIC = {
    (None, 1): {b"MONOCHROME1", b"MONOCHROME2"}, (None, 3): {b"RGB"},
    ("jpeg", 1): {b"MONOCHROME1", b"MONOCHROME2"}, ("jpeg", 3): {b"RGB", b"YBR_FULL", b"YBR_FULL_422"},
    ("jpeg2k", 1): {b"MONOCHROME1", b"MONOCHROME2"}, ("jpeg2k", 3): {b"RGB", b"YBR_ICT", b"YBR_RCT"},
}
# SOI and the Adobe APP14 marker, without its last byte, the color transform (profiles/dicom.md §6.5).
ADOBE = bytes.fromhex("FFD8FFEE000E41646F6265006400000000")
DECIMAL = re.compile(rb"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")
INTEGER = re.compile(rb"([+-]?)([0-9]+)")


def is_dicom(head: bytes) -> bool:
    """The DICOM row of VIRTUALIZE.md §1.2."""
    return head[128:132] == b"DICM"


# ---- attribute values (conventions/dicom/README.md §2.2)

class Values:
    """Reads the attributes of a dataset by their kind (conventions/dicom/README.md §2.2)."""

    def __init__(self, read: Reader, ds: Dataset | None) -> None:
        self.read = read
        self.ds = ds or {}

    def _element(self, tag: int, vrs: tuple[str, ...]) -> Element | None:
        el = self.ds.get(tag)
        if el is None or el.length == 0:
            return None
        if el.vr is not None and el.vr not in vrs:
            raise Rejected(f"{tag_name(tag)} has VR {el.vr}, not {' or '.join(vrs)}")
        if el.length == UNDEFINED:
            raise Rejected(f"{tag_name(tag)} has an undefined length")
        return el

    def integer(self, tag: int, vr: str) -> int | None:
        el = self._element(tag, (vr,))
        if el is None:
            return None
        n = 2 if vr == "US" else 4
        if el.length % n:
            raise Rejected(f"{tag_name(tag)} has a length that is not a multiple of {n}")
        return struct.unpack(("<" if el.little else ">") + ("H" if n == 2 else "I"), self.read(el.value, n))[0]

    def integers64(self, tag: int) -> list[int] | None:
        el = self._element(tag, ("OV",))
        if el is None:
            return None
        if el.length % 8:
            raise Rejected(f"{tag_name(tag)} has a length that is not a multiple of 8")
        values = struct.unpack(("<" if el.little else ">") + f"{el.length // 8}Q", self.read(el.value, el.length))
        if any(v > MAX_SAFE for v in values):
            raise Rejected(f"{tag_name(tag)} has a value above 2^53 - 1")
        return list(values)

    def strings(self, tag: int, vr: str) -> list[bytes] | None:
        el = self._element(tag, (vr,))
        if el is None:
            return None
        return [v.strip(b" \0") for v in self.read(el.value, el.length).split(b"\\")]

    def string(self, tag: int, vr: str) -> bytes | None:
        values = self.strings(tag, vr)
        return None if values is None else values[0]

    def integer_string(self, tag: int) -> int | float | None:
        value = self.string(tag, "IS")
        if value is None:
            return None
        match = INTEGER.fullmatch(value)
        if match is None:
            raise Rejected(f"{tag_name(tag)} is not an integer string: {value!r}")
        # Without leading zeros; more than 16 digits is above 2^53 - 1 anyway.
        digits = match[2].lstrip(b"0") or b"0"
        magnitude = math.inf if len(digits) > 16 else int(digits)
        return -magnitude if match[1] == b"-" else magnitude

    def decimals(self, tag: int) -> list[float | None] | None:
        """The string values as numbers, None for an invalid one."""
        values = self.strings(tag, "DS")
        if values is None:
            return None
        out = []
        for v in values:
            x = float(v) if DECIMAL.fullmatch(v) else None
            out.append(x if x is not None and math.isfinite(x) else None)
        return out


def _need(value, what: str):
    if value is None:
        raise Rejected(f"missing {what}")
    return value


def _check_range(offset: int, length: int, size: int) -> None:
    if offset + length > size:
        raise Rejected(f"range [{offset}, {offset + length}) outside the {size}-byte file")


def virtualize_dicom(url: str, read: Reader, size: int) -> Output:
    # §6.2
    if size < 132 or read(128, 4) != b"DICM":
        raise Rejected("not a DICOM file")
    walker = Walker(read, size)
    meta, start = walker.meta()
    dataset_start = start
    syntax = _need(Values(read, meta).string(0x00020010, "UI"), "Transfer Syntax UID")
    if syntax not in SYNTAXES:
        raise Rejected(f"unsupported transfer syntax {syntax.decode('latin-1')}")
    encoding, codec = SYNTAXES[syntax]

    # §6.3
    top, _ = walker.dataset(start, size, False, encoding, 0, top=True)
    pixel = top[PIXEL_DATA]

    # conventions/dicom/README.md §2.2: the Pixel Measures item of the Shared Functional Groups, if any.
    measures = None
    shared = top.get(0x52009229)
    if shared is not None and shared.items:
        pm = shared.items[0].get(0x00289110)
        if pm is not None and pm.items:
            measures = pm.items[0]
    v, m = Values(read, top), Values(read, measures)
    sop_class = v.string(0x00080016, "UI")
    organization = v.string(0x00209311, "CS")
    spp = v.integer(0x00280002, "US")
    photometric = v.string(0x00280004, "CS")
    planar = v.integer(0x00280006, "US")
    frames = v.integer_string(0x00280008)
    rows, columns = v.integer(0x00280010, "US"), v.integer(0x00280011, "US")
    bits_allocated, bits_stored = v.integer(0x00280100, "US"), v.integer(0x00280101, "US")
    high_bit, representation = v.integer(0x00280102, "US"), v.integer(0x00280103, "US")
    center, width = v.decimals(0x00281050), v.decimals(0x00281051)
    intercept, slope = v.decimals(0x00281052), v.decimals(0x00281053)
    total_columns, total_rows = v.integer(0x00480006, "UL"), v.integer(0x00480007, "UL")
    optical_paths, focal_planes = v.integer(0x00480302, "UL"), v.integer(0x00480303, "UL")
    eot, eot_lengths = v.integers64(0x7FE00001), v.integers64(0x7FE00002)
    if pixel.vr is not None and pixel.vr not in ("OB", "OW"):
        raise Rejected(f"Pixel Data has VR {pixel.vr}")
    # (FG) attributes: the Pixel Measures item where present there, else the top level.
    fg = {}
    for tag in (0x00280030, 0x00180088):
        in_item, at_top = m.decimals(tag), v.decimals(tag)
        fg[tag] = in_item if in_item is not None else at_top

    # conventions/dicom/README.md §3
    spp = _need(spp, "Samples per Pixel")
    photometric = _need(photometric, "Photometric Interpretation")
    rows, columns = _need(rows, "Rows"), _need(columns, "Columns")
    bits_allocated, bits_stored = _need(bits_allocated, "Bits Allocated"), _need(bits_stored, "Bits Stored")
    representation = _need(representation, "Pixel Representation")
    if rows < 1 or columns < 1:
        raise Rejected("Rows and Columns must be at least 1")
    if spp not in (1, 3):
        raise Rejected(f"unsupported Samples per Pixel {spp}")
    if spp == 3 and planar not in (0, 1):
        raise Rejected(f"Planar Configuration {planar} with 3 samples per pixel")
    n = 1 if frames is None else frames
    if not 1 <= n <= MAX_SAFE:
        raise Rejected(f"Number of Frames {n} is not from 1 to 2^53 - 1")
    if not 1 <= bits_stored <= bits_allocated:
        raise Rejected(f"Bits Stored {bits_stored} is not from 1 to Bits Allocated {bits_allocated}")
    if high_bit is not None and high_bit != bits_stored - 1:
        raise Rejected(f"High Bit {high_bit} is not Bits Stored - 1")
    if representation not in (0, 1):
        raise Rejected(f"unsupported Pixel Representation {representation}")
    if bits_allocated not in {None: (8, 16, 32), "jpeg": (8,), "jpeg2k": (8, 16)}[codec]:
        raise Rejected(f"unsupported Bits Allocated {bits_allocated}")
    if codec == "jpeg" and (bits_stored != 8 or representation != 0):
        raise Rejected("JPEG Baseline needs 8 unsigned bits stored")
    if photometric not in PHOTOMETRIC[codec, spp]:
        raise Rejected(f"unsupported Photometric Interpretation {photometric.decode('latin-1')!r} "
                       f"with {spp} samples per pixel")
    data_type = f"{'u' if representation == 0 else ''}int{bits_allocated}"
    whole_slide = sop_class == WHOLE_SLIDE
    if whole_slide:
        if organization != b"TILED_FULL":
            raise Rejected("a whole-slide image whose Dimension Organization Type is not TILED_FULL")
        width_px, height_px = _need(total_columns, "Total Pixel Matrix Columns"), _need(total_rows, "Total Pixel Matrix Rows")
        if width_px < 1 or height_px < 1:
            raise Rejected("the total pixel matrix must be at least 1 by 1")
        if (optical_paths if optical_paths is not None else 1) != 1:
            raise Rejected(f"{optical_paths} optical paths")
        if (focal_planes if focal_planes is not None else 1) != 1:
            raise Rejected(f"{focal_planes} focal planes")
        across, down = -(-width_px // columns), -(-height_px // rows)
        if n != across * down:
            raise Rejected(f"{n} frames for {down} by {across} tiles")
    else:
        width_px, height_px, across = columns, rows, 1

    # profiles/dicom.md §6.5: each frame's ranges, and the samples' when planar.
    planar_native = codec is None and spp == 3 and planar == 1
    frame_size = rows * columns * spp * (bits_allocated // 8)
    frame_ranges: list[list[tuple[int, int] | bytes]] = []
    if codec is None:
        if pixel.length == UNDEFINED:
            raise Rejected("native Pixel Data with an undefined length")
        if pixel.length < n * frame_size:
            raise Rejected(f"Pixel Data has {pixel.length} bytes, less than {n} frames of {frame_size}")
        if not encoding.little and bits_allocated == 8 and pixel.vr == "OW":
            raise Rejected("8-bit Pixel Data of VR OW in big endian")
        frame_ranges = [[(pixel.value + f * frame_size, frame_size)] for f in range(n)]
    else:
        if pixel.length != UNDEFINED:
            raise Rejected("encapsulated Pixel Data with a defined length")
        if pixel.value + 8 > size:
            raise Rejected("no Basic Offset Table")
        group, number, bot_length = struct.unpack("<HHI", read(pixel.value, 8))
        if (group << 16 | number) != ITEM or bot_length == UNDEFINED or bot_length % 4:
            raise Rejected("the Basic Offset Table is not an item of a multiple of 4 bytes")
        q = pixel.value + 8 + bot_length
        if eot is not None or eot_lengths is not None:
            if eot is None or eot_lengths is None:
                raise Rejected("an Extended Offset Table without its lengths, or lengths without the table")
            if len(eot) != n or len(eot_lengths) != n:
                raise Rejected("the Extended Offset Table does not have one value per frame")
            if bot_length != 0:
                raise Rejected("an Extended Offset Table with a Basic Offset Table")
            fragments = [[(q + eot[f] + 8, eot_lengths[f])] for f in range(n)]
        else:
            items, _ = walker.fragments(pixel.value, size, True)
            items = items[1:]
            if not items:
                raise Rejected("encapsulated Pixel Data without fragments")
            bot = struct.unpack(f"<{bot_length // 4}I", read(pixel.value + 8, bot_length))
            data = [(o, length) for _, o, length in items]
            if bot:
                position = {p - q: i for i, (p, _, _) in enumerate(items)}
                if len(bot) != n:
                    raise Rejected(f"the Basic Offset Table has {len(bot)} offsets for {n} frames")
                if bot[0] != 0 or any(a >= b for a, b in zip(bot, bot[1:])):
                    raise Rejected("the Basic Offset Table does not start at 0 and increase")
                if any(o not in position for o in bot):
                    raise Rejected("a Basic Offset Table offset is not a fragment's")
                bounds = [position[o] for o in bot] + [len(items)]
                fragments = [data[bounds[f]:bounds[f + 1]] for f in range(n)]
            elif len(items) == n:
                fragments = [[d] for d in data]
            elif n == 1:
                fragments = [data]
            else:
                raise Rejected(f"{len(items)} fragments for {n} frames without an offset table")
        for f, frags in enumerate(fragments):
            ranges: list[tuple[int, int] | bytes] = [r for r in frags if r[1] > 0]
            if not ranges:
                raise Rejected(f"frame {f} has no data")
            if codec == "jpeg" and spp == 3:
                o, length = ranges[0]
                if length <= 2:
                    raise Rejected(f"frame {f}'s first fragment is too short for a JPEG stream")
                ranges = [ADOBE + bytes([0 if photometric == b"RGB" else 1]), (o + 2, length - 2)] + ranges[1:]
            frame_ranges.append(ranges)

    # conventions/dicom/README.md §4; profiles/dicom.md §6.6: the chunk references
    axes = (["c"] if spp == 3 else []) + (["z"] if not whole_slide and n > 1 else []) + ["y", "x"]
    shape = {"c": 3, "z": n, "y": height_px, "x": width_px}
    chunk_shape = {"c": 1 if planar_native else 3, "z": 1, "y": rows, "x": columns}
    codecs = [transpose_codec(axes)] if spp == 3 and not planar_native else []
    if codec is None:
        codecs.append({"name": "bytes", "configuration": {"endian": "little" if encoding.little else "big"}}
                      if bits_allocated > 8 else {"name": "bytes"})
    else:
        codecs.append({"name": f"imagecodecs_{codec}"})
    spacing = fg[0x00280030]
    usable = spacing is not None and len(spacing) == 2 and all(s is not None and s > 0 for s in spacing)
    between = fg[0x00180088]
    step = between[0] if between is not None and between[0] is not None and between[0] > 0 else None
    scale = {"c": 1, "z": step or 1, "y": spacing[0] if usable else 1, "x": spacing[1] if usable else 1}
    units = {"z": "millimeter" if step else None, "y": "millimeter" if usable else None,
             "x": "millimeter" if usable else None}
    ome = image_ome(axes, units, [[scale[a] for a in axes]], None)

    if representation == 0:
        low, high = 0, 2**bits_stored - 1
    else:
        low, high = -(2 ** (bits_stored - 1)), 2 ** (bits_stored - 1) - 1
    start, end = low, high
    if spp == 1 and center is not None and width is not None and center[0] is not None and width[0] is not None:
        c, w = center[0], width[0]
        k = 0.0 if intercept is None else intercept[0]
        s = 1.0 if slope is None else slope[0]
        if w >= 1 and k is not None and s is not None and s != 0:
            start, end = (c - w / 2 - k) / s, (c + w / 2 - k) / s
            if s < 0:
                start, end = end, start
            if not (math.isfinite(start) and math.isfinite(end)):
                raise Rejected("the VOI window is not finite")
    window = {"min": low, "max": high, "start": start, "end": end}
    if spp == 1:
        channels = [{"label": "gray", "color": "FFFFFF", "active": True, "window": window,
                     **({"inverted": True} if photometric == b"MONOCHROME1" else {})}]
    else:
        channels = [{"label": label, "color": color, "active": True, "window": window}
                    for label, color in (("R", "FF0000"), ("G", "00FF00"), ("B", "0000FF"))]
    ome["omero"] = {"channels": channels}

    out = Output(url)
    # The source metadata (conventions/dicom/README.md §5).
    out.json("zarr.json", root_json(ome, "dicom", url, source_json(read, size, dataset_start, encoding)))
    out.json("0/zarr.json", array_json([shape[a] for a in axes], data_type, [chunk_shape[a] for a in axes],
                                       codecs, axes))
    for f, ranges in enumerate(frame_ranges):
        row, col = (f // across, f % across) if whole_slide else (0, 0)
        samples = range(3) if planar_native else [0]
        for s in samples:
            if planar_native:
                (o, length), = ranges
                part: list[tuple[int, int] | bytes] = [(o + s * (length // 3), length // 3)]
            else:
                part = ranges
            for r in part:
                if not isinstance(r, bytes):
                    _check_range(r[0], r[1], size)
            if payload_size(part) > MAX_PAYLOAD:
                raise Rejected(f"frame {f}'s reference payload exceeds {MAX_PAYLOAD} bytes")
            coords = {"c": s, "z": f, "y": row, "x": col}
            out.refs["0/c/" + "/".join(str(coords[a]) for a in axes)] = part
    out.summary = {"axes": axes, "shape": [shape[a] for a in axes], "dataType": data_type,
                   "transferSyntax": syntax.decode("latin-1"), "photometric": photometric.decode("latin-1"),
                   "frames": n, "wholeSlide": whole_slide, "references": len(out.refs)}
    return out
