"""The ND2 profile of VIRTUALIZE.md (§4)."""

from __future__ import annotations

import re
import struct

from vzip.pb import Concat, Range
from vzip.virtualize.common import Output, Reader, Rejected, array_json, group_json, image_ome, transpose_codec
from vzip.virtualize.lv import LVList, decode_lv

CHUNK_MAGIC = 0x0ABECEDA
FILE_SIGNATURE = b"ND2 FILE SIGNATURE CHUNK NAME01!"
MAP_SIGNATURE = b"ND2 CHUNK MAP SIGNATURE 0000001!"
FILEMAP_NAME = b"ND2 FILEMAP SIGNATURE NAME 0001!"
MAX_PAYLOAD = 65519
REQUIRED = object()


def is_nd2(head: bytes) -> bool:
    return len(head) >= 4 and struct.unpack("<I", head[:4])[0] == CHUNK_MAGIC


def _header(read: Reader, offset: int) -> tuple[int, int, bytes]:
    magic, name_length, data_length = struct.unpack("<IIQ", read(offset, 16))
    if magic != CHUNK_MAGIC:
        raise Rejected(f"no ND2 chunk at {offset}")
    return name_length, data_length, read(offset + 16, name_length).rstrip(b"\0")


def _at(value, path: str):
    for part in path.split("/"):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, LVList) and part.isdigit():
            value = value[int(part)] if int(part) < len(value) else None
        else:
            return None
    return value


def _members(value) -> list:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return list(value.values())
    return []


def _number(value, what: str, default=REQUIRED):
    """A member used as a number (§4.2): types 2-6."""
    if value is None:
        if default is REQUIRED:
            raise Rejected(f"missing {what}")
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Rejected(f"{what} is not a number")
    return value


def _flag(value, what: str, default=False) -> bool:
    """A member used as a flag (§4.2): types 1-5, true if nonzero."""
    if value is None:
        return default
    if isinstance(value, float) or not isinstance(value, (bool, int)):
        raise Rejected(f"{what} is not a flag")
    return bool(value)


def _valid(members: list, flags) -> list:
    if flags is None:
        return members
    f = _members(flags)
    return [m for i, m in enumerate(members) if i < len(f) and _flag(f[i], "validity entry")]


def _node_loop(node):
    """A node's loop (kind, eType, count, period, step), "spectral", or None (§4.3)."""
    typ = _number(_at(node, "eType"), "eType")
    if typ not in (1, 2, 4, 6, 8):
        raise Rejected(f"unsupported experiment loop type {typ}")
    pars = _at(node, "uLoopPars")
    if pars is None:
        return None
    if typ == 1:
        count = _number(_at(pars, "uiCount"), "uiCount", 0)
        return ("t", typ, count, _number(_at(pars, "dPeriod"), "dPeriod", 0), None) if count else None
    if typ == 8:
        periods = _valid(_members(_at(pars, "pPeriod")), _at(pars, "pPeriodValid"))
        count = sum(_number(_at(p, "uiCount"), "uiCount", 0) for p in periods)
        period = _number(_at(periods[0], "dPeriod"), "dPeriod", 0) if periods else 0
        return ("t", typ, count, period, None) if count else None
    if typ == 2:
        count = len(_valid(_members(_at(pars, "Points")), _at(node, "pItemValid")))
        return ("p", typ, count, None, None) if count else None
    if typ == 4:
        count = _number(_at(pars, "uiCount"), "uiCount", 0)
        step = abs(_number(_at(pars, "dZStep"), "dZStep", 0))
        if step == 0 and count > 1:
            high = _number(_at(pars, "dZHigh"), "dZHigh", 0)
            low = _number(_at(pars, "dZLow"), "dZLow", 0)
            step = abs(high - low) / (count - 1)
        return ("z", typ, count, None, step) if count else None
    count = _at(pars, "uiCount")
    if count is None:
        count = _at(pars, "pPlanes/uiCount")
    return "spectral" if _number(count, "uiCount", 0) else None


def flatten_experiment(root) -> list[dict]:
    loops: list[dict] = []

    def visit(node, depth: int) -> None:
        loop = _node_loop(node)
        if loop is None:
            return
        child_depth = depth + 1
        if loop == "spectral":
            child_depth = depth
        else:
            kind, typ, count, period, step = loop
            item = {"kind": kind, "type": typ, "count": count, "period": period, "step": step, "depth": depth}
            if not loops or loops[-1]["depth"] < depth:
                loops.append(item)
            elif loops[-1]["depth"] == depth and loops[-1]["type"] == typ and loops[-1]["count"] < count:
                loops[-1] = item
        for child in _members(_at(node, "ppNextLevelEx")):
            visit(child, child_depth)

    if root is not None:
        visit(root, 0)
    kinds = [l["kind"] for l in loops]
    if len(set(kinds)) != len(kinds):
        raise Rejected(f"repeated loop kinds {kinds}")
    return loops


def _check_range(offset: int, length: int, size: int) -> None:
    if offset < 0 or length < 0 or offset + length > size:
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

    # §4.3
    attrs = _at(chunk(b"ImageAttributesLV!"), "SLxImageAttributes")
    if not isinstance(attrs, dict):
        raise Rejected("no ImageAttributesLV! chunk")
    width, height = _number(attrs.get("uiWidth"), "uiWidth"), _number(attrs.get("uiHeight"), "uiHeight")
    width_bytes, comp = _number(attrs.get("uiWidthBytes"), "uiWidthBytes"), _number(attrs.get("uiComp"), "uiComp")
    bpc = _number(attrs.get("uiBpcInMemory"), "uiBpcInMemory")
    significant = _number(attrs.get("uiBpcSignificant"), "uiBpcSignificant")
    compression = _number(attrs.get("eCompression"), "eCompression", 2)
    if min(width, height, comp) < 1:
        raise Rejected("image width, height and components must be at least 1")
    data_type = {8: "uint8", 16: "uint16", 32: "float32"}.get(bpc)
    if data_type is None:
        raise Rejected(f"unsupported bits per component {bpc}")
    if compression == 1:
        raise Rejected("lossy ND2 compression is not supported")
    if compression not in (0, 2):
        raise Rejected(f"unknown ND2 compression {compression}")
    for key, full in (("uiTileWidth", width), ("uiTileHeight", height)):
        tile = _number(attrs.get(key), key, 0)
        if tile > 0 and tile != full:
            raise Rejected("tiled ND2 frames are not supported")
    compressed = compression == 0
    row_bytes = width * comp * bpc // 8
    if width_bytes < row_bytes:
        raise Rejected("uiWidthBytes is less than a row")
    if compressed and width_bytes != row_bytes:
        raise Rejected("compressed frames with padded rows are not supported")
    exp = chunk(b"ImageMetadataLV!")
    loops = flatten_experiment(_at(exp, "SLxExperiment") if exp is not None else None)
    picture = _at(chunk(b"ImageMetadataSeqLV|0!"), "SLxPictureMetadata")

    # §4.5
    plane_count = _number(_at(picture, "sPicturePlanes/uiCount"), "uiCount", 0)
    planes = [_at(picture, f"sPicturePlanes/sPlaneNew/a{i}") for i in range(int(plane_count))]
    labels, colors = [], []
    counts = [_number(_at(p, "uiCompCount"), "uiCompCount", 1) for p in planes if p is not None]
    if planes and all(p is not None for p in planes) and all(k in (1, 3) for k in counts) and sum(counts) == comp:
        for p, k in zip(planes, counts):
            desc = _at(p, "sDescription")
            name = "" if desc is None else desc
            if not isinstance(name, str):
                raise Rejected("sDescription is not a string")
            if k == 3:
                labels += [f"{name} R", f"{name} G", f"{name} B"]
                colors += ["FF0000", "00FF00", "0000FF"]
            else:
                abgr = int(_number(_at(p, "uiColor"), "uiColor", 0xFFFFFF))
                labels.append(name)
                colors.append(f"{abgr & 255:02X}{(abgr >> 8) & 255:02X}{(abgr >> 16) & 255:02X}")
    else:
        labels = [f"C{k}" for k in range(comp)]
        colors = ["FFFFFF"] * comp

    # §4.4
    total = 1
    for l in loops:
        total *= l["count"]
    present = [f for f in range(total) if f"ImageDataSeq|{f}!".encode() in chunks]
    offset = {f: chunks[f"ImageDataSeq|{f}!".encode()] for f in present}
    if compressed:
        def frame_ranges(f):
            n, d, _ = _header(read, offset[f])
            if d <= 8:
                raise Rejected(f"compressed frame {f} has no data")
            return [(offset[f] + 16 + n + 8, d - 8)]
    else:
        name_len = 0
        if present:
            first, last = _header(read, offset[present[0]]), _header(read, offset[present[-1]])
            if first[0] != last[0]:
                raise Rejected("frame chunk headers differ in name length")
            if min(first[1], last[1]) < 8 + height * width_bytes:
                raise Rejected("frame chunk too short for its pixels")
            name_len = first[0]

        def frame_ranges(f):
            start = offset[f] + 16 + name_len + 8
            if width_bytes == row_bytes:
                return [(start, height * row_bytes)]
            return [(start + r * width_bytes, row_bytes) for r in range(height)]

    # §4.6
    def loop(kind):
        return next((l for l in loops if l["kind"] == kind), None)

    t, z, p = loop("t"), loop("z"), loop("p")
    axes = (["t"] if t else []) + (["c"] if comp > 1 else []) + (["z"] if z else []) + ["y", "x"]
    shape = {"t": t["count"] if t else 1, "c": comp, "z": z["count"] if z else 1, "y": height, "x": width}
    chunk_shape = {"t": 1, "c": comp, "z": 1, "y": height, "x": width}
    cal = _number(_at(picture, "dCalibration"), "dCalibration", 0)
    calibrated = _flag(_at(picture, "bCalibrated"), "bCalibrated") and cal > 0
    aspect = _number(_at(picture, "dAspect"), "dAspect", 1)
    if not aspect > 0:
        aspect = 1
    period = t["period"] if t and t["period"] > 0 else None
    step = z["step"] if z and z["step"] > 0 else None
    scale = {"t": period / 1000 if period else 1, "c": 1, "z": step if step else 1,
             "y": cal * aspect if calibrated else 1, "x": cal if calibrated else 1}
    for v in scale.values():
        if v != v or v in (float("inf"), float("-inf")):
            raise Rejected("a scale is not finite")
    units = {"t": "second" if period else None, "z": "micrometer" if step else None,
             "y": "micrometer" if calibrated else None, "x": "micrometer" if calibrated else None}
    codecs = ([transpose_codec(axes)] if comp > 1 else []) + [
        {"name": "bytes", "configuration": {"endian": "little"}} if bpc > 8 else {"name": "bytes"}]
    if compressed:
        codecs.append({"name": "zlib", "configuration": {"level": 1}})
    b = significant if 1 <= significant <= bpc else bpc
    window = {} if data_type == "float32" else {"window": {"min": 0, "max": 2**b - 1, "start": 0, "end": 2**b - 1}}
    positions = p["count"] if p else 1
    out = Output(url)
    out.json("zarr.json", group_json({"version": "0.5", "bioformats2raw.layout": 3}))
    out.json("OME/zarr.json", group_json({"version": "0.5", "series": [str(i) for i in range(positions)]}))
    for pi in range(positions):
        ome = image_ome(axes, units, [[scale[a] for a in axes]], f"position {pi}")
        ome["omero"] = {"channels": [{"label": label, "color": color, "active": True, **window}
                                     for label, color in zip(labels, colors)]}
        out.json(f"{pi}/zarr.json", group_json(ome))
        out.json(f"{pi}/0/zarr.json", array_json([shape[a] for a in axes], data_type,
                                                  [chunk_shape[a] for a in axes], codecs, axes))
    for f in present:
        coords, rest = {}, f
        for l in reversed(loops):
            coords[l["kind"]] = rest % l["count"]
            rest //= l["count"]
        index = [coords.get(a, 0) if a in ("t", "z") else 0 for a in axes]
        ranges = frame_ranges(f)
        for o, n in ranges:
            _check_range(o, n, size)
        if len(ranges) > 1 and len(Concat(tuple(Range(0, o, n) for o, n in ranges)).encode()) > MAX_PAYLOAD:
            raise Rejected(f"frame {f}'s reference payload exceeds {MAX_PAYLOAD} bytes")
        out.refs[f"{coords.get('p', 0)}/0/c/" + "/".join(map(str, index))] = ranges
    out.summary = {"sizes": {**{l["kind"]: l["count"] for l in loops}, "c": comp, "y": height, "x": width},
                   "dataType": data_type, "compressed": compressed, "paddedRows": width_bytes != row_bytes,
                   "positions": positions, "frames": total, "missing": total - len(present), "channels": labels}
    return out
