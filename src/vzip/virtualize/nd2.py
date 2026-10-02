"""The ND2 profile of VIRTUALIZE.md (§4).

Chunk and frame layout, the experiment and channel rules are implemented
here; the lite variant (LV) metadata encoding (§4.2) is decoded with the `nd2`
package (https://github.com/tlambert03/nd2), an optional dependency.
"""

from __future__ import annotations

import struct

from vzip.virtualize.common import Output, Reader, Rejected, array_json, group_json, image_ome, transpose_codec

CHUNK_MAGIC = 0x0ABECEDA
FILE_SIGNATURE = b"ND2 FILE SIGNATURE CHUNK NAME01!"
MAP_SIGNATURE = b"ND2 CHUNK MAP SIGNATURE 0000001!"
FILEMAP_NAME = b"ND2 FILEMAP SIGNATURE NAME 0001!"


def is_nd2(head: bytes) -> bool:
    return len(head) >= 4 and struct.unpack("<I", head[:4])[0] == CHUNK_MAGIC


def _decode_lv(data: bytes) -> dict:
    try:
        from nd2._parse._clx_lite import json_from_clx_lite_variant
    except ImportError as e:  # pragma: no cover
        raise ImportError("the ND2 virtualizer needs the `nd2` package (pip install nd2)") from e
    return json_from_clx_lite_variant(data, strip_prefix=False, lists_to_indexed_dicts=False)


def _header(read: Reader, offset: int) -> tuple[int, int, bytes]:
    magic, name_length, data_length = struct.unpack("<IIQ", read(offset, 16))
    if magic != CHUNK_MAGIC:
        raise Rejected(f"no ND2 chunk at {offset}")
    return name_length, data_length, read(offset + 16, name_length).rstrip(b"\0")


def _at(value, path: str):
    for part in path.split("/"):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit():
            value = value[int(part)] if int(part) < len(value) else None
        else:
            return None
    return value


def _members(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _num(value, default=0):
    if isinstance(value, bool):
        return int(value)
    return value if isinstance(value, (int, float)) else default


def _node_loop(node):
    """A node's loop (kind, eType, count, period, step), "spectral", or None (§4.3)."""
    typ = _num(_at(node, "eType"))
    pars = _at(node, "uLoopPars")
    if pars is None:
        return None
    if typ == 1:
        count = _num(_at(pars, "uiCount"))
        return ("t", typ, count, _num(_at(pars, "dPeriod")), None) if count else None
    if typ == 8:
        valid = _members(_at(pars, "pPeriodValid"))
        count, period = 0, None
        for i, p in enumerate(_members(_at(pars, "pPeriod"))):
            if i < len(valid) and _num(valid[i]):
                count += _num(_at(p, "uiCount"))
                if period is None:
                    period = _num(_at(p, "dPeriod"))
        return ("t", typ, count, period, None) if count else None
    if typ == 2:
        points = _members(_at(pars, "Points"))
        valid = _at(node, "pItemValid")
        if valid is not None:
            flags = _members(valid)
            count = sum(1 for i in range(len(points)) if i < len(flags) and _num(flags[i]))
        else:
            count = len(points)
        return ("p", typ, count, None, None) if count else None
    if typ == 4:
        count = _num(_at(pars, "uiCount"))
        step = abs(_num(_at(pars, "dZStep")))
        if step == 0 and count > 1:
            step = abs(_num(_at(pars, "dZHigh")) - _num(_at(pars, "dZLow"))) / (count - 1)
        return ("z", typ, count, None, step) if count else None
    if typ == 6:
        count = _num(_at(pars, "uiCount"), _num(_at(pars, "pPlanes/uiCount")))
        return "spectral" if count else None
    raise Rejected(f"unsupported experiment loop type {typ}")


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


def virtualize_nd2(url: str, read: Reader, size: int) -> Output:
    # §4.1
    name_length, data_length, name = _header(read, 0)
    if name != FILE_SIGNATURE or name_length != 32 or data_length != 64:
        raise Rejected("not an ND2 file (bad signature chunk)")
    version = read(48, 6).decode("ascii", "replace")
    if not version.startswith("Ver") or not version[3].isdigit() or int(version[3]) < 3:
        raise Rejected(f"unsupported ND2 version {version}")
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
        chunks[cname] = struct.unpack("<Q", data[end + 1 : end + 9])[0]
        pos = end + 17

    def chunk(cname: bytes):
        if cname not in chunks:
            return None
        n, d, _ = _header(read, chunks[cname])
        return _decode_lv(read(chunks[cname] + 16 + n, d))

    # §4.3
    attrs = _at(chunk(b"ImageAttributesLV!"), "SLxImageAttributes")
    if attrs is None:
        raise Rejected("no ImageAttributesLV! chunk")
    width, height = _num(attrs.get("uiWidth")), _num(attrs.get("uiHeight"))
    width_bytes, comp = _num(attrs.get("uiWidthBytes")), _num(attrs.get("uiComp"))
    bpc, significant = _num(attrs.get("uiBpcInMemory")), _num(attrs.get("uiBpcSignificant"))
    compression = _num(attrs.get("eCompression"), 2)
    data_type = {8: "uint8", 16: "uint16", 32: "float32"}.get(bpc)
    if data_type is None:
        raise Rejected(f"unsupported bits per component {bpc}")
    if compression == 1:
        raise Rejected("lossy ND2 compression is not supported")
    if compression not in (0, 2):
        raise Rejected(f"unknown ND2 compression {compression}")
    for key, full in (("uiTileWidth", width), ("uiTileHeight", height)):
        tile = _num(attrs.get(key))
        if tile > 0 and tile != full:
            raise Rejected("tiled ND2 frames are not supported")
    compressed = compression == 0
    row_bytes = width * comp * bpc // 8
    if compressed and width_bytes != row_bytes:
        raise Rejected("compressed frames with padded rows are not supported")
    loops = flatten_experiment(_at(chunk(b"ImageMetadataLV!"), "SLxExperiment"))
    picture = _at(chunk(b"ImageMetadataSeqLV|0!"), "SLxPictureMetadata")

    # §4.5
    planes = [(_at(picture, f"sPicturePlanes/sPlaneNew/a{i}") or {})
              for i in range(int(_num(_at(picture, "sPicturePlanes/uiCount"))))]
    comp_counts = [_num(_at(p, "uiCompCount"), 1) for p in planes]
    labels, colors = [], []
    if planes and sum(comp_counts) == comp:
        for p, k in zip(planes, comp_counts):
            name = str(_at(p, "sDescription") or "")
            if k == 3:
                labels += [f"{name} R", f"{name} G", f"{name} B"]
                colors += ["FF0000", "00FF00", "0000FF"]
            else:
                abgr = int(_num(_at(p, "uiColor")))
                labels.append(name)
                colors.append(f"{abgr & 255:02X}{(abgr >> 8) & 255:02X}{(abgr >> 16) & 255:02X}")
    if len(labels) != comp:
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
            return [(offset[f] + 16 + n + 8, d - 8)]
    else:
        name_len = 0
        if present:
            first, last = _header(read, offset[present[0]])[0], _header(read, offset[present[-1]])[0]
            if first != last:
                raise Rejected("frame chunk headers differ in name length")
            name_len = first

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
    cal = _num(_at(picture, "dCalibration"))
    calibrated = _at(picture, "bCalibrated") is True and cal > 0
    period = t["period"] if t and t["period"] and t["period"] > 0 else None
    step = z["step"] if z and z["step"] and z["step"] > 0 else None
    scale = {"t": period / 1000 if period else 1.0, "c": 1.0, "z": step if step else 1.0,
             "y": cal * _num(_at(picture, "dAspect"), 1) if calibrated else 1.0,
             "x": cal if calibrated else 1.0}
    units = {"t": "second" if period else None, "z": "micrometer" if step else None,
             "y": "micrometer" if calibrated else None, "x": "micrometer" if calibrated else None}
    codecs = ([transpose_codec(axes)] if comp > 1 else []) + [
        {"name": "bytes", "configuration": {"endian": "little"}} if bpc > 8 else {"name": "bytes"}]
    if compressed:
        codecs.append({"name": "zlib", "configuration": {"level": 1}})
    window = {} if data_type == "float32" else {
        "window": {"min": 0, "max": 2**significant - 1, "start": 0, "end": 2**significant - 1}}
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
        out.refs[f"{coords.get('p', 0)}/0/c/" + "/".join(map(str, index))] = frame_ranges(f)
    out.summary = {"sizes": {**{l["kind"]: l["count"] for l in loops}, "c": comp, "y": height, "x": width},
                   "dataType": data_type, "compressed": compressed, "paddedRows": width_bytes != row_bytes,
                   "positions": positions, "frames": total, "missing": total - len(present), "channels": labels}
    return out
