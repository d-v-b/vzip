"""The NIfTI profile (profiles/nifti.md, §7)."""

from __future__ import annotations

import base64
import math
import struct

from vzip.virtualize.common import (
    Output, Reader, Rejected, array_json, image_ome, json_number, json_text, root_json, transpose_codec,
)

MAX_SAFE = 2**53 - 1
BLOCK_BYTES = 1 << 17  # the most bytes of a row block (conventions/nifti/README.md §4.1)

# Header length, magic (§1.2) and the offset and struct format of each field (conventions/nifti/README.md §2).
LAYOUTS = {
    1: {
        "length": 348, "dim": (40, "8h"), "datatype": (70, "h"), "bitpix": (72, "h"), "pixdim": (76, "8f"),
        "vox_offset": (108, "f"), "scl_slope": (112, "f"), "scl_inter": (116, "f"), "xyzt_units": (123, "B"),
        "cal_max": (124, "f"), "cal_min": (128, "f"), "qform_code": (252, "h"), "sform_code": (254, "h"),
        "quatern": (256, "3f"), "qoffset": (268, "3f"), "srow": (280, "12f"),
    },
    2: {
        "length": 540, "dim": (16, "8q"), "datatype": (12, "h"), "bitpix": (14, "h"), "pixdim": (104, "8d"),
        "vox_offset": (168, "q"), "scl_slope": (176, "d"), "scl_inter": (184, "d"), "xyzt_units": (500, "i"),
        "cal_max": (192, "d"), "cal_min": (200, "d"), "qform_code": (344, "i"), "sform_code": (348, "i"),
        "quatern": (352, "3d"), "qoffset": (376, "3d"), "srow": (400, "12d"),
    },
}

# Every header field, in the standard's order, with its offset and kind: the
# source metadata of conventions/nifti/README.md §5. A kind is a struct format,
# or "s<N>" for a character field of N bytes.
HEADER_FIELDS = {
    1: [
        ("sizeof_hdr", 0, "i"), ("data_type", 4, "s10"), ("db_name", 14, "s18"), ("extents", 32, "i"),
        ("session_error", 36, "h"), ("regular", 38, "s1"), ("dim_info", 39, "B"), ("dim", 40, "8h"),
        ("intent_p1", 56, "f"), ("intent_p2", 60, "f"), ("intent_p3", 64, "f"), ("intent_code", 68, "h"),
        ("datatype", 70, "h"), ("bitpix", 72, "h"), ("slice_start", 74, "h"), ("pixdim", 76, "8f"),
        ("vox_offset", 108, "f"), ("scl_slope", 112, "f"), ("scl_inter", 116, "f"), ("slice_end", 120, "h"),
        ("slice_code", 122, "B"), ("xyzt_units", 123, "B"), ("cal_max", 124, "f"), ("cal_min", 128, "f"),
        ("slice_duration", 132, "f"), ("toffset", 136, "f"), ("glmax", 140, "i"), ("glmin", 144, "i"),
        ("descrip", 148, "s80"), ("aux_file", 228, "s24"), ("qform_code", 252, "h"), ("sform_code", 254, "h"),
        ("quatern_b", 256, "f"), ("quatern_c", 260, "f"), ("quatern_d", 264, "f"), ("qoffset_x", 268, "f"),
        ("qoffset_y", 272, "f"), ("qoffset_z", 276, "f"), ("srow_x", 280, "4f"), ("srow_y", 296, "4f"),
        ("srow_z", 312, "4f"), ("intent_name", 328, "s16"), ("magic", 344, "s4"),
    ],
    2: [
        ("sizeof_hdr", 0, "i"), ("magic", 4, "s8"), ("datatype", 12, "h"), ("bitpix", 14, "h"), ("dim", 16, "8q"),
        ("intent_p1", 80, "d"), ("intent_p2", 88, "d"), ("intent_p3", 96, "d"), ("pixdim", 104, "8d"),
        ("vox_offset", 168, "q"), ("scl_slope", 176, "d"), ("scl_inter", 184, "d"), ("cal_max", 192, "d"),
        ("cal_min", 200, "d"), ("slice_duration", 208, "d"), ("toffset", 216, "d"), ("slice_start", 224, "q"),
        ("slice_end", 232, "q"), ("descrip", 240, "s80"), ("aux_file", 320, "s24"), ("qform_code", 344, "i"),
        ("sform_code", 348, "i"), ("quatern_b", 352, "d"), ("quatern_c", 360, "d"), ("quatern_d", 368, "d"),
        ("qoffset_x", 376, "d"), ("qoffset_y", 384, "d"), ("qoffset_z", 392, "d"), ("srow_x", 400, "4d"),
        ("srow_y", 432, "4d"), ("srow_z", 464, "4d"), ("slice_code", 496, "i"), ("xyzt_units", 500, "i"),
        ("intent_code", 504, "i"), ("intent_name", 508, "s16"), ("dim_info", 524, "B"),
        ("unused_str", 525, "s15"),
    ],
}
MAX_EXTENSION_BYTES = 1 << 24  # the most extension data recorded (conventions/nifti/README.md §5)


def header_json(version: int, order: str, header: bytes) -> dict:
    """Every header field, translated (conventions/nifti/README.md §5)."""
    out = {}
    for name, offset, kind in HEADER_FIELDS[version]:
        if kind[0] == "s":
            out[name] = json_text(header[offset:offset + int(kind[1:])])
            continue
        values = struct.unpack_from(order + kind, header, offset)
        out[name] = [json_number(v) for v in values] if kind[0].isdigit() else json_number(values[0])
    return out


def extensions_json(read: Reader, order: str, start: int, vox: int) -> tuple[list[dict], bool]:
    """The extensions from `start` up to the voxel data, and whether the chain was
    cut short (conventions/nifti/README.md §5)."""
    out, q, total = [], start, 0
    while q + 8 <= vox:
        esize, ecode = struct.unpack(order + "ii", read(q, 8))
        if esize < 8 or q + esize > vox or total + esize - 8 > MAX_EXTENSION_BYTES:
            return out, True
        out.append({"ecode": ecode, "edata": base64.b64encode(read(q + 8, esize - 8)).decode()})
        total += esize - 8
        q += esize
    return out, False


# datatype: (Zarr data type, bitpix, samples per voxel) (conventions/nifti/README.md §3).
DATATYPES = {
    2: ("uint8", 8, 1), 4: ("int16", 16, 1), 8: ("int32", 32, 1), 16: ("float32", 32, 1),
    64: ("float64", 64, 1), 256: ("int8", 8, 1), 512: ("uint16", 16, 1), 768: ("uint32", 32, 1),
    1024: ("int64", 64, 1), 1280: ("uint64", 64, 1), 128: ("uint8", 24, 3), 2304: ("uint8", 32, 4),
}
UNSUPPORTED = {1: "BINARY", 32: "COMPLEX64", 1536: "FLOAT128", 1792: "COMPLEX128", 2048: "COMPLEX256"}
SPACE_UNITS = {1: "meter", 2: "millimeter", 3: "micrometer"}
TIME_UNITS = {8: "second", 16: "millisecond", 24: "microsecond"}
COLORS = [("R", "FF0000"), ("G", "00FF00"), ("B", "0000FF"), ("A", "FFFFFF")]


def detect(head: bytes) -> int | None:
    """The NIfTI version that the file's first bytes select (§1.2), or None."""
    if len(head) < 12:
        return None
    sizes = struct.unpack("<i", head[:4]) + struct.unpack(">i", head[:4])
    if 348 in sizes and len(head) >= 348 and head[344:348] == b"n+1\0":
        return 1
    if 540 in sizes and head[4:12] == b"n+2\0\r\n\x1a\n":
        return 2
    return None


def _valid(v: float) -> bool:
    return math.isfinite(v) and v > 0


def virtualize_nifti(url: str, read: Reader, size: int) -> Output:
    version = detect(read(0, min(552, size)))
    if version is None:
        raise Rejected("not a NIfTI-1 or NIfTI-2 single file")
    # §7.1
    layout = LAYOUTS[version]
    length = layout["length"]
    if size < length:
        raise Rejected(f"file too short for a {length}-byte NIfTI-{version} header")
    header = read(0, length)
    order = "<" if struct.unpack("<i", header[:4])[0] == length else ">"

    def field(name: str):
        offset, fmt = layout[name]
        values = struct.unpack_from(order + fmt, header, offset)
        return values if fmt[0].isdigit() else values[0]

    dim = field("dim")
    n = dim[0]
    if not 1 <= n <= 7:
        raise Rejected(f"dim[0] = {n} is not from 1 to 7")
    sizes = [1] * 8
    for i in range(1, n + 1):
        if not 1 <= dim[i] <= MAX_SAFE:
            raise Rejected(f"dim[{i}] = {dim[i]} is not from 1 to 2^53 - 1")
        sizes[i] = dim[i]
    if sizes[6] != 1 or sizes[7] != 1:
        raise Rejected("dimensions 6 and 7 must have size 1")
    code, bitpix = field("datatype"), field("bitpix")
    if code in UNSUPPORTED:
        raise Rejected(f"unsupported NIfTI datatype {code} ({UNSUPPORTED[code]})")
    if code not in DATATYPES:
        raise Rejected(f"unknown NIfTI datatype {code}")
    data_type, bits, samples = DATATYPES[code]
    if bitpix != bits:
        raise Rejected(f"bitpix {bitpix} does not match datatype {code}")
    color = samples > 1
    if color and n >= 5:
        raise Rejected("color data with a fifth dimension")
    vox = field("vox_offset")
    if version == 1:
        if not (math.isfinite(vox) and vox.is_integer()):
            raise Rejected(f"vox_offset {vox} is not an integer")
        vox = int(vox)
    if not length + 4 <= vox <= MAX_SAFE:
        raise Rejected(f"vox_offset {vox} is not from {length + 4} to 2^53 - 1")
    X, Y, Z, T, C = sizes[1:6]
    b = bits // 8
    total = X * Y * Z * T * C * b
    if total > MAX_SAFE or vox + total > size:
        raise Rejected(f"the {total}-byte voxel data at {vox} is outside the {size}-byte file")

    # Row blocks (conventions/nifti/README.md §4.1) and their ranges (profiles/nifti.md §7.2)
    row = X * b
    h = next((d for d in range(min(Y, BLOCK_BYTES // row), 0, -1) if Y % d == 0), 1)

    # conventions/nifti/README.md §4.2
    scaling = None
    if not color:
        slope, inter = field("scl_slope"), field("scl_inter")
        if math.isfinite(slope) and slope != 0:
            scaling = (slope, inter if math.isfinite(inter) else 0.0)
    nontrivial = scaling is not None and scaling != (1, 0)

    # conventions/nifti/README.md §4.3
    units_code = field("xyzt_units")
    space, time = SPACE_UNITS.get(units_code & 7), TIME_UNITS.get(units_code & 56)
    pixdim = field("pixdim")
    affine, diagonal, offset = None, None, None
    if field("sform_code") > 0:
        affine = "sform"
        s = field("srow")
        if (all(math.isfinite(v) for v in s) and all(s[k] == 0 for k in (1, 2, 4, 6, 8, 9))
                and s[0] > 0 and s[5] > 0 and s[10] > 0):
            diagonal, offset = (s[0], s[5], s[10]), (s[3], s[7], s[11])
    elif field("qform_code") > 0:
        affine = "qform"
        q, o = field("quatern"), field("qoffset")
        if (all(v == 0 for v in q) and not pixdim[0] < 0 and all(_valid(v) for v in pixdim[1:4])
                and all(math.isfinite(v) for v in o)):
            diagonal, offset = tuple(pixdim[1:4]), o
    scale = {"t": pixdim[4] if _valid(pixdim[4]) else 1, "c": 1}
    units = {"t": time if _valid(pixdim[4]) else None}
    for k, a in enumerate("xyz"):
        v = diagonal[k] if diagonal else pixdim[k + 1]
        scale[a], units[a] = (v, space) if _valid(v) else (1, None)

    # conventions/nifti/README.md §4.4
    channels = None
    if color:
        channels = [{"label": label, "color": c, "active": True,
                     "window": {"min": 0, "max": 255, "start": 0, "end": 255}} for label, c in COLORS[:samples]]
    else:
        lo, hi = field("cal_min"), field("cal_max")
        if math.isfinite(lo) and math.isfinite(hi) and hi > lo:
            if scaling:
                s, i = scaling
                lo, hi = sorted([(lo - i) / s, (hi - i) / s])
                if not (math.isfinite(lo) and math.isfinite(hi)):
                    raise Rejected("the display window is not finite")
            channels = [{"label": f"C{k}", "color": "FFFFFF", "active": True,
                         "window": {"min": lo, "max": hi, "start": lo, "end": hi}} for k in range(C)]

    # conventions/nifti/README.md §4.1
    axes = (["t"] if n >= 4 else []) + (["c"] if n >= 5 or color else []) + (["z"] if n >= 3 else []) + ["y", "x"]
    shape = {"t": T, "c": samples if color else C, "z": Z, "y": Y, "x": X}
    chunk_shape = {"t": 1, "c": samples, "z": 1, "y": h, "x": X}
    translation = [offset["xyz".index(a)] if a in "xyz" else 0 for a in axes] if diagonal else None
    ome = image_ome(axes, units, [[scale[a] for a in axes]], None, [translation] if translation else None)
    if channels is not None:
        ome["omero"] = {"channels": channels}
    # The source metadata (conventions/nifti/README.md §5).
    endian = "little" if order == "<" else "big"
    meta = {"nifti_version": version, "byte_order": endian, "header": header_json(version, order, header)}
    extended = read(length, 1) != b"\0"
    if extended:
        meta["extensions"], cut = extensions_json(read, order, length + 4, vox)
        if cut:
            meta["extensions_truncated"] = True
    if nontrivial:
        meta["scaling"] = {"slope": scaling[0], "inter": scaling[1]}
    root = root_json(ome, "nifti", url, meta)
    codecs = ([transpose_codec(axes)] if color else []) + [
        {"name": "bytes", "configuration": {"endian": endian}} if bits // samples > 8 else {"name": "bytes"}]
    out = Output(url)
    out.json("zarr.json", root)
    out.json("0/zarr.json", array_json([shape[a] for a in axes], data_type, [chunk_shape[a] for a in axes],
                                       codecs, axes))
    slab = Y * row
    for k in range(C):
        for t in range(T):
            for z in range(Z):
                start = vox + ((k * T + t) * Z + z) * slab
                for j in range(Y // h):
                    coords = {"t": t, "c": k, "z": z, "y": j, "x": 0}
                    out.refs["0/c/" + "/".join(str(coords[a]) for a in axes)] = [(start + j * h * row, h * row)]
    out.summary = {
        "version": version, "byteOrder": endian, "sizes": {a: shape[a] for a in axes}, "dataType": data_type,
        "color": color, "rowBlock": h, "chunks": len(out.refs),
        "scaling": {"slope": scaling[0], "inter": scaling[1]} if nontrivial else None,
        "affine": affine, "translation": translation is not None,
        "extensions": extended,
    }
    return out
