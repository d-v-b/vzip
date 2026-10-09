"""The NIfTI profile (spec/virtualize.md, §7)."""

from __future__ import annotations

import json
import math
import struct
import sys
from array import array

import numpy as np

from vzip.virtualize.common import (
    MAX_PAYLOAD, RAGGED_CHUNK, SOURCE_NODE, Output, Plan, Reader, Rejected, array_json, declare, emit_plans,
    grid_chunks, image_ome, json_base64, json_number, json_text, payload_size, root_json, row_chunks, text_json,
)

MAX_SAFE = 2**53 - 1
CHUNK_BYTES = 1 << 17  # the most bytes of a chunk of the image (spec/virtualize/nifti.md §4.1)
MAX_CHANNELS = 64  # the most channels given display windows (spec/virtualize/nifti.md §4.4)

# Header length, magic (§1.2) and the offset and struct format of each field (spec/virtualize/nifti.md §2).
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
# source metadata of spec/virtualize/nifti.md §5. A kind is a struct format,
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


# The bits of the one NaN a float field is written back as from "NaN" (spec/virtualize/nifti.md §5).
CANONICAL_NAN = {"f": 0x7FC00000, "d": 0x7FF8000000000000}


def float_json(order: str, kind: str, raw: bytes):
    """A float field's value as source metadata (spec/virtualize/nifti.md §5): a number,
    or {"bits": hex} for a negative zero or a NaN other than the canonical one, whose
    bits JSON numbers and "NaN" do not keep."""
    v = struct.unpack(order + kind, raw)[0]
    bits = int.from_bytes(raw, "little" if order == "<" else "big")
    if (v == 0 and math.copysign(1, v) < 0) or (math.isnan(v) and bits != CANONICAL_NAN[kind]):
        return {"bits": f"{bits:0{2 * len(raw)}x}"}
    return json_number(v)


def header_json(version: int, order: str, header: bytes) -> tuple[dict, dict]:
    """Every header field, translated, and the bytes after the first NUL of each
    character field but `magic` where they are not all NUL (spec/virtualize/nifti.md §5)."""
    out, rest = {}, {}
    for name, offset, kind in HEADER_FIELDS[version]:
        if kind[0] == "s":
            raw = header[offset:offset + int(kind[1:])]
            out[name] = json_text(raw)
            tail = raw.partition(b"\0")[2]
            if name != "magic" and tail.strip(b"\0"):
                rest[name] = json_base64(tail)
            continue
        if kind[-1] in "fd":
            size = struct.calcsize(kind[-1])
            values = [float_json(order, kind[-1], header[at:at + size])
                      for at in range(offset, offset + struct.calcsize(kind), size)]
        else:
            values = [json_number(v) for v in struct.unpack_from(order + kind, header, offset)]
        out[name] = values if kind[0].isdigit() else values[0]
    return out, rest


TEXT_CODES = {4, 6, 8, 32, 44}  # AFNI and XCEDE XML, comment, CIFTI XML, MRS JSON: text
INLINE = 64  # the most bytes of binary extension data kept as JSON
MAX_TEXT = 1 << 16  # the longest text extension kept as JSON
EXTENSIONS_BUDGET = 1 << 14  # the most bytes of the extensions' JSON on the root
LIST_MOST = EXTENSIONS_BUDGET // 12  # the most extensions of an array E in the budget: each is >= 12 bytes
SCAN = 1 << 20  # the bytes read at a time to check that a run is all zero
EXTENDERS = (bytes(4), b"\1\0\0\0")  # the extenders that are not recorded


ECODES = f"{SOURCE_NODE}/extensions/ecode"
ESIZES = f"{SOURCE_NODE}/extensions/esize"
EDATA = f"{SOURCE_NODE}/extensions/data"
ECODE_CHUNK = 1 << 22  # the most values of a chunk of the ecodes (spec/virtualize/nifti.md §5)


def extensions_json(read: Reader, order: str, start: int, vox: int) -> tuple[tuple[object, list], int]:
    """The extension chain from `start` up to the voxel data (spec/virtualize/nifti.md §5):
    (its JSON, its arrays, where it ends). The chain is kept as two packed columns, so that
    memory per extension is 8 bytes; the JSON is built per extension only while it may fit."""
    esizes, ecodes, q = array("i"), array("i"), start
    fmt = order + "ii"
    while q + 8 <= vox:  # read the chain a block at a time
        block, p = read(q, min(SCAN, vox - q)), 0
        while p + 8 <= len(block):
            esize, ecode = struct.unpack_from(fmt, block, p)
            if esize < 8 or q + p + esize > vox:
                return extensions_value(read, order, start, q + p, esizes, ecodes), q + p
            esizes.append(esize)
            ecodes.append(ecode)
            p += esize
        q += p
    return extensions_value(read, order, start, q, esizes, ecodes), q


def extension_text(ecode: int, data: bytes | None):
    """The text value of an extension's data, or None when it is not a text extension (§5)."""
    if ecode not in TEXT_CODES or data is None:
        return None
    head, _, pad = data.partition(b"\0")
    return None if pad.strip(b"\0") else text_json(head)


def extensions_value(read: Reader, order: str, start: int, end: int, esizes: array, ecodes: array
                     ) -> tuple[object, list]:
    """The value of `extensions` and its arrays, for the chain from `start` to `end` (§5)."""
    count = len(esizes)
    if count <= LIST_MOST:  # the array E may fit: build it while its running size does
        out, plans, size, at = [], [], 1, start + 8  # size: the JSON of "[" and of each entry and its separator
        for i in range(count):
            n, ecode = esizes[i] - 8, ecodes[i]
            entry: dict = {"ecode": ecode}
            data = read(at, n) if n <= MAX_TEXT else None
            text = extension_text(ecode, data)
            if text is not None:
                entry["text"] = text
                if n + 8 != text_esize(len(data.partition(b"\0")[0])):  # padded otherwise than with the fewest NULs
                    entry["esize"] = n + 8
            elif n <= INLINE:
                entry["edata"] = json_base64(data)
            else:
                path = f"extensions/{i}"
                rows, chunks = row_chunks(at, n, 1)
                plans.append(Plan(path, "uint8", [n], [rows], ["byte"], chunks))
                entry["data"] = f"{SOURCE_NODE}/{path}"
            out.append(entry)
            size += json_size(entry) + 1
            if size > EXTENSIONS_BUDGET:
                break
            at += n + 8
        else:
            return out, plans
    # Over the budget: the ecodes, the text extensions that fit, and the others' data as a family.
    compact: dict = {"ecode": ECODES, "esize": ESIZES, "data": EDATA}
    size, inline = json_size(compact), {}
    sizes = np.frombuffer(esizes, dtype=np.int32)
    # The extensions that may be text, and where their data is.
    candidates = np.flatnonzero(np.isin(np.frombuffer(ecodes, dtype=np.int32), sorted(TEXT_CODES))
                                & (sizes <= MAX_TEXT + 8))
    places = start + 8 + np.cumsum(sizes, dtype=np.int64)[candidates] - sizes[candidates]
    for j in range(len(candidates)):
        i = int(candidates[j])
        least = len(str(i)) + 2 + 1 + 2 + (1 if inline else len(',"text":{}'))  # "i":"" with its separator
        if size + least > EXTENSIONS_BUDGET:
            break  # no later text fits: their indexes are no shorter
        text = extension_text(ecodes[i], read(int(places[j]), esizes[i] - 8))
        if text is not None:
            more = least - 2 + json_size(text)
            if size + more <= EXTENSIONS_BUDGET:
                inline[i] = text
                size += more
    del sizes, candidates, places
    if inline:
        compact["text"] = {str(i): text for i, text in inline.items()}
    k = -(-count // ECODE_CHUNK)
    c = -(-count // k)
    plans = []
    for name, column in (("ecode", ecodes), ("esize", esizes)):
        if (order == "<") != (sys.byteorder == "little"):
            column = array("i", column)
            column.byteswap()
        values = column.tobytes() + bytes(4 * (k * c - count))
        plans.append(Plan(f"extensions/{name}", "int32", [count], [c], ["index"],
                          {(j,): values[j * c * 4:(j + 1) * c * 4] for j in range(k)},
                          endian="little" if order == "<" else "big"))
    return compact, plans + data_family("extensions/data", Window(read, end), start, esizes, inline)


def data_family(path: str, read: Window, start: int, esizes: array, inline: dict) -> list[Plan]:
    """The extensions' data as a family of byte values in its second form (conventions §7),
    member i absent when it is in `inline`: the rule of common.family_plans, from the packed
    esizes, so that memory is 8 bytes per member and one chunk at a time."""
    count = len(esizes)
    ends = np.frombuffer(esizes, dtype=np.int32).astype(np.int64)
    ends -= 8
    ends[sorted(inline)] = 0
    np.cumsum(ends, out=ends)  # where each member ends in the data
    starts = array("q", [0])  # the members' starts in the data, then its length
    starts.frombytes(memoryview(ends).cast("B"))
    del ends
    total = starts[count]
    packed = starts.tobytes() if sys.byteorder == "little" else np.array(starts, dtype="<i8").tobytes()
    # The offsets are copied, and cut as contiguous values (spec/conventions.md §7).
    shape, cut = grid_chunks(0, [count + 1], 8, lambda o, n: packed[o : o + n])
    offsets = {k: v if isinstance(v, bytes) else b"".join(packed[r[0] : r[0] + r[1]] if isinstance(r, tuple) else r
                                                          for r in v) for k, v in cut.items()}
    plans = [Plan(f"{path}/offsets", "int64", [count + 1], shape, ["index"], offsets)]
    if not total:
        return plans
    size_c = -(-total // -(-total // RAGGED_CHUNK))  # balanced: k = ceil(total / 2^20) chunks
    data_chunks = {}
    first, source = 0, start  # the first member that may reach the chunk, and where its extension is
    for c in range(-(-total // size_c)):
        lo, hi = c * size_c, min(total, (c + 1) * size_c)
        while first < count and (first in inline or starts[first + 1] <= lo):
            source += esizes[first]
            first += 1
        ranges: list = []
        fixed = 0  # the payload of the ranges but the last, as payload_size counts them
        copy, filled = None, 0  # the chunk's bytes, once its ranges are over the payload, and how many
        o = source
        for i in range(first, count):
            at = starts[i]
            if at >= hi:
                break
            if i not in inline and starts[i + 1] > lo:
                begin, n = o + 8 + max(lo, at) - at, min(hi, starts[i + 1]) - max(lo, at)
                if copy is not None:
                    copy[filled:filled + n] = read(begin, n)
                    filled += n
                elif ranges and ranges[-1][0] + ranges[-1][1] == begin:  # adjacent in the source: one range
                    ranges[-1] = (ranges[-1][0], ranges[-1][1] + n)
                else:
                    if ranges:
                        fixed += _term(ranges[-1])
                    ranges.append((begin, n))
                if copy is None and len(ranges) > 1 and fixed + _term(ranges[-1]) > MAX_PAYLOAD:
                    copy = bytearray(size_c)  # it stays over: copy it, the padding zero
                    for r in ranges:
                        copy[filled:filled + r[1]] = read(*r)
                        filled += r[1]
            o += esizes[i]
        pad = [bytes(size_c - (hi - lo))] if hi - lo < size_c else []
        if copy is not None:
            data_chunks[(c,)] = bytes(copy)
            continue
        ranges += pad
        if payload_size(ranges) > MAX_PAYLOAD:
            ranges = b"".join(read(r[0], r[1]) if isinstance(r, tuple) else r for r in ranges)
        data_chunks[(c,)] = ranges
    plans.append(Plan(f"{path}/data", "uint8", [total], [size_c], ["byte"], data_chunks))
    return plans


class Window:
    """Reads of the chain, in increasing order, through a window of SCAN bytes, so that
    the data of many short extensions is one read (it ends at the chain's `end`)."""

    def __init__(self, read: Reader, end: int):
        self.read_source, self.end, self.start, self.data = read, end, 0, b""

    def __call__(self, offset: int, length: int) -> bytes:
        if not (self.start <= offset and offset + length <= self.start + len(self.data)):
            self.start, self.data = offset, self.read_source(offset, max(length, min(SCAN, self.end - offset)))
        return self.data[offset - self.start:offset - self.start + length]


def _term(r) -> int:
    """A range's bytes in a reference of several ranges (common.payload_size)."""
    return payload_size([r, r]) // 2


def text_esize(length: int) -> int:
    """The esize of a text extension of `length` bytes padded with the fewest NULs: the
    least multiple of 16 that is at least length + 8 (spec/virtualize/nifti.md §5)."""
    return -(-(length + 8) // 16) * 16


def json_size(value) -> int:
    """The bytes of `value`'s compact JSON, in UTF-8, non-ASCII characters unescaped."""
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def all_zero(read: Reader, start: int, end: int) -> bool:
    while start < end:
        n = min(SCAN, end - start)
        if read(start, n).count(0) != n:
            return False
        start += n
    return True


# datatype: (Zarr data type, bitpix, samples per voxel) (spec/virtualize/nifti.md §3).
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

    # spec/virtualize/nifti.md §4.2
    scaling = None
    if not color:
        slope, inter = field("scl_slope"), field("scl_inter")
        if math.isfinite(slope) and slope != 0:
            scaling = (slope, inter if math.isfinite(inter) else 0.0)
    nontrivial = scaling is not None and scaling != (1, 0)

    # spec/virtualize/nifti.md §4.3
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

    # spec/virtualize/nifti.md §4.4
    channels = None
    if color:
        channels = [{"label": label, "color": c, "active": True,
                     "window": {"min": 0, "max": 255, "start": 0, "end": 255}} for label, c in COLORS[:samples]]
    else:
        lo, hi = field("cal_min"), field("cal_max")
        if math.isfinite(lo) and math.isfinite(hi) and hi > lo and C <= MAX_CHANNELS:
            if scaling:
                s, i = scaling
                lo, hi = sorted([(lo - i) / s, (hi - i) / s])
            if math.isfinite(lo) and math.isfinite(hi):  # else the scaled window overflowed: none
                channels = [{"label": f"C{k}", "color": "FFFFFF", "active": True,
                             "window": {"min": lo, "max": hi, "start": lo, "end": hi}} for k in range(C)]

    # spec/virtualize/nifti.md §4.1
    axes = (["t"] if n >= 4 else []) + (["c"] if n >= 5 or color else []) + (["z"] if n >= 3 else []) + ["y", "x"]
    shape = {"t": T, "c": samples if color else C, "z": Z, "y": Y, "x": X}
    # The voxels in file order: dimension 5 outermost, a color type's samples innermost.
    stored = (["c"] if n >= 5 else []) + [a for a in axes if a != "c"] + (["c"] if color else [])
    grid, voxel_chunks = grid_chunks(vox, [shape[a] for a in stored], b // samples, read, limit=CHUNK_BYTES)
    chunk_shape = {a: grid[stored.index(a)] for a in axes}
    translation = [offset["xyz".index(a)] if a in "xyz" else 0 for a in axes] if diagonal else None
    ome = image_ome(axes, units, [[scale[a] for a in axes]], None, [translation] if translation else None)
    if channels is not None:
        ome["omero"] = {"channels": channels}
    # The source metadata (spec/virtualize/nifti.md §5).
    endian = "little" if order == "<" else "big"
    header_meta, header_rest = header_json(version, order, header)
    meta = {"nifti_version": version, "byte_order": endian, "header": header_meta}
    if header_rest:
        meta["header_rest"] = header_rest
    extender = read(length, 4)
    if extender not in EXTENDERS:
        meta["extender"] = json_base64(extender)
    extended = extender[0] != 0
    plans, end = [], length + 4
    if extended:
        (meta["extensions"], plans), end = extensions_json(read, order, end, vox)
    if end < vox and not all_zero(read, end, vox):  # bytes the chain does not hold, and not padding
        if extended:
            meta["extensions_truncated"] = True
        rows, chunks = row_chunks(end, vox - end, 1)
        plans.append(Plan("unparsed", "uint8", [vox - end], [rows], ["byte"], chunks))
        meta["unparsed"] = f"{SOURCE_NODE}/unparsed"
    if vox + total < size:  # after the voxel data
        rows, chunks = row_chunks(vox + total, size - vox - total, 1)
        plans.append(Plan("trailing", "uint8", [size - vox - total], [rows], ["byte"], chunks))
        meta["trailing"] = f"{SOURCE_NODE}/trailing"
    if nontrivial:
        meta["scaling"] = {"slope": scaling[0], "inter": scaling[1]}
    if affine is not None:
        meta["affine"] = {"form": affine, "applied": diagonal is not None}
    root = root_json(ome, "nifti", url, meta)
    transpose = {"name": "transpose", "configuration": {"order": [axes.index(a) for a in stored]}}
    codecs = ([transpose] if stored != axes else []) + [
        {"name": "bytes", "configuration": {"endian": endian}} if bits // samples > 8 else {"name": "bytes"}]
    out = Output(url)
    out.json("zarr.json", root)
    if plans:
        emit_plans(out, plans, declare({}, "nifti", None))
    out.json("0/zarr.json", array_json([shape[a] for a in axes], data_type, [chunk_shape[a] for a in axes],
                                       codecs, axes))
    for coords, parts in voxel_chunks.items():  # spec/virtualize.md §7.2
        key = "0/c/" + "/".join(str(coords[stored.index(a)]) for a in axes)
        if isinstance(parts, bytes):
            out.bytes_entries[key] = parts
        else:
            out.refs[key] = parts
    out.summary = {
        "version": version, "byteOrder": endian, "sizes": {a: shape[a] for a in axes}, "dataType": data_type,
        "color": color,
        "chunkShape": [chunk_shape[a] for a in axes], "chunks": len(voxel_chunks),
        "scaling": {"slope": scaling[0], "inter": scaling[1]} if nontrivial else None,
        "affine": affine, "translation": translation is not None,
        "extensions": extended,
    }
    return out
