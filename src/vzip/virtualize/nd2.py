"""The ND2 profile of VIRTUALIZE.md (§4)."""

from __future__ import annotations

import math
import re
import struct

from vzip.virtualize.common import Output, Reader, Rejected, array_json, group_json, image_ome, transpose_codec
from vzip.virtualize.lv import LVList, Scalar, decode_lv

CHUNK_MAGIC = 0x0ABECEDA
FILE_SIGNATURE = b"ND2 FILE SIGNATURE CHUNK NAME01!"
MAP_SIGNATURE = b"ND2 CHUNK MAP SIGNATURE 0000001!"
FILEMAP_NAME = b"ND2 FILEMAP SIGNATURE NAME 0001!"
FRAME = re.compile(rb"ImageDataSeq\|(0|[1-9][0-9]*)!")
MAX_PAYLOAD = 65519
MAX_SAFE = 2**53 - 1
REQUIRED = object()


def is_nd2(head: bytes) -> bool:
    return len(head) >= 4 and struct.unpack("<I", head[:4])[0] == CHUNK_MAGIC


def _header(read: Reader, offset: int) -> tuple[int, int, bytes]:
    if offset > MAX_SAFE:
        raise Rejected(f"chunk offset {offset} is too large")
    magic, name_length, data_length = struct.unpack("<IIQ", read(offset, 16))
    if magic != CHUNK_MAGIC:
        raise Rejected(f"no ND2 chunk at {offset}")
    return name_length, data_length, read(offset + 16, name_length).split(b"\0", 1)[0]


# ---- typed member access (§4.2)

def _missing(what: str, default):
    if default is REQUIRED:
        raise Rejected(f"missing {what}")
    return default


def number(value, what: str, default=REQUIRED):
    if value is None:
        return _missing(what, default)
    if not isinstance(value, Scalar) or value.type not in (2, 3, 4, 5, 6):
        raise Rejected(f"{what} is not a number")
    v = float(value.value)  # every number is used as binary64 (§4.2)
    if not math.isfinite(v):
        raise Rejected(f"{what} is not finite")
    return v


def integer(value, what: str, default=REQUIRED):
    if value is None:
        return _missing(what, default)
    v = number(value, what)
    if v != int(v) or not 0 <= v <= MAX_SAFE:
        raise Rejected(f"{what} = {v} is not an integer from 0 to 2^53 - 1")
    return int(v)


def color(value, what: str, default=REQUIRED):
    if value is None:
        return _missing(what, default)
    v = number(value, what)
    if v != int(v) or not -(2**31) <= v <= 2**32 - 1:
        raise Rejected(f"{what} = {v} is not a color")
    return int(v) % 2**32


def flag(value, what: str, default=REQUIRED) -> bool:
    if value is None:
        return _missing(what, default)
    if not isinstance(value, Scalar) or value.type not in (1, 2, 3, 4, 5):
        raise Rejected(f"{what} is not a flag")
    return bool(value.value)


def string(value, what: str, default=REQUIRED) -> str:
    if value is None:
        return _missing(what, default)
    if not isinstance(value, Scalar) or value.type != 8:
        raise Rejected(f"{what} is not a string")
    return value.value


def obj(value, what: str, default=REQUIRED):
    if value is None:
        return _missing(what, default)
    if not isinstance(value, dict):
        raise Rejected(f"{what} is not an object")
    return value


def lst(value, what: str, default=REQUIRED):
    if value is None:
        return _missing(what, default)
    if not isinstance(value, LVList):
        raise Rejected(f"{what} is not a list")
    return value


def members(value, what: str) -> list:
    """The members of an object or a list (default: none)."""
    if value is None:
        return []
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, LVList):
        return list(value)
    raise Rejected(f"{what} is not an object or a list")


def _valid(items: list, flags, what: str) -> list:
    if flags is None:
        return items
    f = [flag(x, f"{what} entry") for x in lst(flags, what)]
    return [m for i, m in enumerate(items) if i < len(f) and f[i]]


# ---- experiment (§4.3)

def _node_loop(node: dict):
    """A node's loop as (kind, count, period or step), "spectral", or None (skipped)."""
    typ = integer(node.get("eType"), "eType")
    if typ not in (1, 2, 4, 6, 8):
        raise Rejected(f"unsupported experiment loop type {typ}")
    pars = obj(node.get("uLoopPars"), "uLoopPars", None)
    item_valid = lst(node.get("pItemValid"), "pItemValid", None)
    if pars is None:
        return None
    if typ == 1:
        count = integer(pars.get("uiCount"), "uiCount", 0)
        loop = ("t", count, number(pars.get("dPeriod"), "dPeriod", 0))
    elif typ == 8:
        periods = [obj(p, "pPeriod member") for p in members(pars.get("pPeriod"), "pPeriod")]
        valid = _valid(periods, pars.get("pPeriodValid"), "pPeriodValid")
        count = sum(integer(p.get("uiCount"), "uiCount") for p in valid)
        periods_ms = [number(p.get("dPeriod"), "dPeriod", 0) for p in valid]
        loop = ("t", count, periods_ms[0] if periods_ms else 0)
    elif typ == 2:
        count = len(_valid(members(pars.get("Points"), "Points"), item_valid, "pItemValid"))
        loop = ("p", count, None)
    elif typ == 4:
        count = integer(pars.get("uiCount"), "uiCount", 0)
        step = abs(number(pars.get("dZStep"), "dZStep", 0))
        high = number(pars.get("dZHigh"), "dZHigh", 0)
        low = number(pars.get("dZLow"), "dZLow", 0)
        if step == 0 and count > 1:
            step = abs(high - low) / (count - 1)
        loop = ("z", count, step)
    else:
        count = integer(pars.get("uiCount"), "uiCount", None)
        if count is None:
            planes = obj(pars.get("pPlanes"), "pPlanes", None)
            count = integer(planes.get("uiCount"), "pPlanes/uiCount", 0) if planes is not None else 0
        loop = "spectral"
    return loop if count else None


def flatten_experiment(root) -> list[dict]:
    loops: list[dict] = []

    def visit(node: dict, depth: int) -> None:
        loop = _node_loop(node)
        if loop is None:
            return
        child_depth = depth + 1
        if loop == "spectral":
            child_depth = depth
        else:
            kind, count, scale = loop
            item = {"kind": kind, "count": count, "scale": scale, "depth": depth}
            if not loops or loops[-1]["depth"] < depth:
                loops.append(item)
            elif loops[-1]["depth"] == depth and loops[-1]["kind"] == kind and loops[-1]["count"] < count:
                loops[-1] = item
        for child in members(node.get("ppNextLevelEx"), "ppNextLevelEx"):
            visit(obj(child, "experiment node"), child_depth)

    if root is not None:
        visit(obj(root, "SLxExperiment"), 0)
    kinds = [l["kind"] for l in loops]
    if len(set(kinds)) != len(kinds):
        raise Rejected(f"repeated loop kinds {kinds}")
    return loops


# ---- reference payloads (§1.2)

def _varint_size(v: int) -> int:
    return max(1, (v.bit_length() + 6) // 7)


def _range_size(offset: int, length: int) -> int:
    return (1 + _varint_size(offset) if offset else 0) + (1 + _varint_size(length) if length else 0)


def payload_size(ranges: list[tuple[int, int]]) -> int:
    if len(ranges) == 1:
        return _range_size(*ranges[0])
    return sum(1 + _varint_size(r) + r for r in (_range_size(o, n) for o, n in ranges))


def _check_range(offset: int, length: int, size: int) -> None:
    if offset + length > size:
        raise Rejected(f"range [{offset}, {offset + length}) outside the {size}-byte file")


def virtualize_nd2(url: str, read: Reader, size: int) -> Output:
    # §4.1
    name_length, data_length, name = _header(read, 0)
    if name != FILE_SIGNATURE or name_length != 32 or data_length != 64:
        raise Rejected("not an ND2 file (bad signature chunk)")
    version = re.match(rb"Ver([0-9]+)\.", read(48, 64))
    if version is None or int(version[1]) < 3:
        raise Rejected(f"unsupported ND2 version {read(48, 8)!r}")
    if size < 40:
        raise Rejected("file too short for an ND2 chunk map")
    tail = read(size - 40, 40)
    if tail[:32] != MAP_SIGNATURE:
        raise Rejected("no ND2 chunk map signature")
    map_offset = struct.unpack("<Q", tail[32:])[0]
    n, d, mname = _header(read, map_offset)
    if mname != FILEMAP_NAME:
        raise Rejected("bad ND2 chunk map chunk")
    data = read(map_offset + 16 + n, d)
    chunks: dict[bytes, int] = {}
    pos = 0
    while True:
        end = data.find(b"!", pos)
        if end < 0:
            raise Rejected("unterminated ND2 chunk map")
        cname = data[pos : end + 1]
        if cname == MAP_SIGNATURE:
            break
        if end + 17 > len(data):
            raise Rejected("truncated ND2 chunk map record")
        chunks[cname] = struct.unpack("<Q", data[end + 1 : end + 9])[0]
        pos = end + 17

    def chunk(cname: bytes):
        if cname not in chunks:
            return None
        n, d, _ = _header(read, chunks[cname])
        return decode_lv(read(chunks[cname] + 16 + n, d))

    # §4.3 attributes
    attributes = chunk(b"ImageAttributesLV!")
    if attributes is None:
        raise Rejected("no ImageAttributesLV! chunk")
    attrs = obj(attributes.get("SLxImageAttributes"), "SLxImageAttributes")
    width, height = integer(attrs.get("uiWidth"), "uiWidth"), integer(attrs.get("uiHeight"), "uiHeight")
    width_bytes = integer(attrs.get("uiWidthBytes"), "uiWidthBytes")
    comp = integer(attrs.get("uiComp"), "uiComp")
    bpc = integer(attrs.get("uiBpcInMemory"), "uiBpcInMemory")
    significant = number(attrs.get("uiBpcSignificant"), "uiBpcSignificant")
    compression = integer(attrs.get("eCompression"), "eCompression", 2)
    tile_width = integer(attrs.get("uiTileWidth"), "uiTileWidth", 0)
    tile_height = integer(attrs.get("uiTileHeight"), "uiTileHeight", 0)
    if min(width, height, comp) < 1:
        raise Rejected("image width, height and components must be at least 1")
    data_type = {8: "uint8", 16: "uint16", 32: "float32"}.get(bpc)
    if data_type is None:
        raise Rejected(f"unsupported bits per component {bpc}")
    if compression == 1:
        raise Rejected("lossy ND2 compression is not supported")
    if compression not in (0, 2):
        raise Rejected(f"unknown ND2 compression {compression}")
    if (tile_width > 0 and tile_width != width) or (tile_height > 0 and tile_height != height):
        raise Rejected("tiled ND2 frames are not supported")
    compressed = compression == 0
    row_bytes = width * comp * bpc // 8
    if width_bytes < row_bytes:
        raise Rejected("uiWidthBytes is less than a row")
    if compressed and width_bytes != row_bytes:
        raise Rejected("compressed frames with padded rows are not supported")

    # §4.3 experiment
    exp = chunk(b"ImageMetadataLV!")
    loops = flatten_experiment(exp.get("SLxExperiment") if exp is not None else None)

    # §4.3 picture metadata
    seq = chunk(b"ImageMetadataSeqLV|0!")
    picture = (obj(seq.get("SLxPictureMetadata"), "SLxPictureMetadata", None) if seq is not None else None) or {}
    bcal = flag(picture.get("bCalibrated"), "bCalibrated", False)
    cal = number(picture.get("dCalibration"), "dCalibration", None)
    aspect = number(picture.get("dAspect"), "dAspect", 1)
    calibrated = bcal and cal is not None and cal > 0
    if not aspect > 0:
        aspect = 1
    pp = obj(picture.get("sPicturePlanes"), "sPicturePlanes", None) or {}
    plane_count = integer(pp.get("uiCount"), "uiCount", 0)
    new = obj(pp.get("sPlaneNew"), "sPlaneNew", None) or {}
    planes = {}
    for key, value in new.items():
        m = re.fullmatch(r"a(0|[1-9][0-9]*)", key)
        if m and int(m[1]) < plane_count:
            p = obj(value, key)
            planes[int(m[1])] = (string(p.get("sDescription"), "sDescription", ""),
                                 color(p.get("uiColor"), "uiColor", 0xFFFFFF),
                                 integer(p.get("uiCompCount"), "uiCompCount", 1))

    # §4.5
    labels, colors = [], []
    if (plane_count >= 1 and len(planes) == plane_count
            and all(k in (1, 3) for _, _, k in planes.values())
            and sum(k for _, _, k in planes.values()) == comp):
        for i in range(plane_count):
            desc, abgr, k = planes[i]
            if k == 3:
                labels += [f"{desc} R", f"{desc} G", f"{desc} B"]
                colors += ["FF0000", "00FF00", "0000FF"]
            else:
                labels.append(desc)
                colors.append(f"{abgr & 255:02X}{(abgr >> 8) & 255:02X}{(abgr >> 16) & 255:02X}")
    else:
        labels = [f"C{k}" for k in range(comp)]
        colors = ["FFFFFF"] * comp

    # §4.4
    total = 1
    for l in loops:
        total *= l["count"]
    offset = {}
    for cname, o in chunks.items():
        m = FRAME.fullmatch(cname)
        if m and int(m[1]) < total:
            offset[int(m[1])] = o
    present = sorted(offset)
    h = height
    if compressed:
        starts = {}
        for f in present:
            n, d, _ = _header(read, offset[f])
            if d <= 8:
                raise Rejected(f"compressed frame {f} has no data")
            starts[f] = (offset[f] + 16 + n + 8, d - 8)

        def frame_chunks(f):
            return [[starts[f]]]
    else:
        name_len = 0
        if present:
            first, last = _header(read, offset[present[0]]), _header(read, offset[present[-1]])
            if first[0] != last[0]:
                raise Rejected("frame chunk headers differ in name length")
            if min(first[1], last[1]) < 8 + height * width_bytes:
                raise Rejected("frame chunk too short for its pixels")
            name_len = first[0]

        def start(f):
            return offset[f] + 16 + name_len + 8

        if width_bytes != row_bytes and present:
            # The block with the largest payload is the last block of the
            # frame that starts last: varint sizes grow with offsets.
            far = max(start(f) for f in present)
            h = next(d for d in range(height, 0, -1) if height % d == 0 and payload_size(
                [(far + r * width_bytes, row_bytes) for r in range(height - d, height)]) <= MAX_PAYLOAD)

        def frame_chunks(f):
            if width_bytes == row_bytes:
                return [[(start(f), height * row_bytes)]]
            rows = [(start(f) + r * width_bytes, row_bytes) for r in range(height)]
            return [rows[j : j + h] for j in range(0, height, h)]

    # §4.6
    def loop(kind):
        return next((l for l in loops if l["kind"] == kind), None)

    t, z, p = loop("t"), loop("z"), loop("p")
    axes = (["t"] if t else []) + (["c"] if comp > 1 else []) + (["z"] if z else []) + ["y", "x"]
    shape = {"t": t["count"] if t else 1, "c": comp, "z": z["count"] if z else 1, "y": height, "x": width}
    chunk_shape = {"t": 1, "c": comp, "z": 1, "y": h, "x": width}
    period = t["scale"] if t and t["scale"] > 0 else None
    step = z["scale"] if z and z["scale"] > 0 else None
    scale = {"t": period / 1000 if period else 1, "c": 1, "z": step if step else 1,
             "y": cal * aspect if calibrated else 1, "x": cal if calibrated else 1}
    for v in scale.values():
        if not math.isfinite(v):
            raise Rejected("a scale is not finite")
    units = {"t": "second" if period else None, "z": "micrometer" if step else None,
             "y": "micrometer" if calibrated else None, "x": "micrometer" if calibrated else None}
    codecs = ([transpose_codec(axes)] if comp > 1 else []) + [
        {"name": "bytes", "configuration": {"endian": "little"}} if bpc > 8 else {"name": "bytes"}]
    if compressed:
        codecs.append({"name": "zlib", "configuration": {"level": 1}})
    b = int(significant) if significant == int(significant) and 1 <= significant <= bpc else bpc
    window = {} if data_type == "float32" else {"window": {"min": 0, "max": 2**b - 1, "start": 0, "end": 2**b - 1}}
    positions = p["count"] if p else 1
    out = Output(url)
    out.json("zarr.json", group_json({"version": "0.5", "bioformats2raw.layout": 3}))
    out.json("OME/zarr.json", group_json({"version": "0.5", "series": [str(i) for i in range(positions)]}))
    for pi in range(positions):
        ome = image_ome(axes, units, [[scale[a] for a in axes]], f"position {pi}")
        ome["omero"] = {"channels": [{"label": label, "color": c, "active": True, **window}
                                     for label, c in zip(labels, colors)]}
        out.json(f"{pi}/zarr.json", group_json(ome))
        out.json(f"{pi}/0/zarr.json", array_json([shape[a] for a in axes], data_type,
                                                  [chunk_shape[a] for a in axes], codecs, axes))
    for f in present:
        coords, rest = {}, f
        for l in reversed(loops):
            coords[l["kind"]] = rest % l["count"]
            rest //= l["count"]
        for j, ranges in enumerate(frame_chunks(f)):
            for o, n in ranges:
                _check_range(o, n, size)
            if payload_size(ranges) > MAX_PAYLOAD:
                raise Rejected(f"frame {f}'s reference payload exceeds {MAX_PAYLOAD} bytes")
            index = [coords.get(a, 0) if a in ("t", "z") else j if a == "y" else 0 for a in axes]
            out.refs[f"{coords.get('p', 0)}/0/c/" + "/".join(map(str, index))] = ranges
    out.summary = {"sizes": {**{l["kind"]: l["count"] for l in loops}, "c": comp, "y": height, "x": width},
                   "dataType": data_type, "compressed": compressed, "paddedRows": width_bytes != row_bytes,
                   "rowBlock": h, "positions": positions, "frames": total, "missing": total - len(present),
                   "channels": labels}
    return out
