"""The ND2 profile (profiles/nd2.md, §5)."""

from __future__ import annotations

import math
import re
import struct
from collections.abc import Sequence

from vzip_reference.revision import REVISION
from vzip.virtualize.common import (
    MAX_PAYLOAD, SOURCE_NODE, Output, Reader, Rejected, array_json, declare, group_json, image_ome, json_text,
    emit_plans, payload_size, root_json, transpose_codec,
)
from vzip_reference.nd2.lv import LVBytes, LVList, Scalar, TooLarge, decode_lv
from vzip_reference.nd2.source import Frames, small_int, source_metadata

CHUNK_MAGIC = 0x0ABECEDA
FILE_SIGNATURE = b"ND2 FILE SIGNATURE CHUNK NAME01!"
MAP_SIGNATURE = b"ND2 CHUNK MAP SIGNATURE 0000001!"
FILEMAP_NAME = b"ND2 FILEMAP SIGNATURE NAME 0001!"
FRAME = re.compile(rb"ImageDataSeq\|(0|[1-9][0-9]*)!")
MAX_SAFE = 2**53 - 1
MAX_INFLATE = 1 << 26  # the most a chunk the profile reads may inflate to (profiles/nd2.md §5.1)
MAX_RECORDS = 1 << 20  # the most LV records and byte-array bytes of the chunks the profile reads (§5.1)
MAX_POSITIONS = 1 << 16  # the most positions (profiles/nd2.md §5.3)
MAX_POSITION_CHANNELS = 1 << 20  # the most positions x components (profiles/nd2.md §5.3)
REQUIRED = object()


def is_nd2(head: bytes) -> bool:
    return len(head) >= 4 and struct.unpack("<I", head[:4])[0] == CHUNK_MAGIC


FRAME_HEAD = 16 + 4096  # the bytes prefetched at each frame chunk: its header and a padded name


def _header(read: Reader, offset: int) -> tuple[int, int, bytes]:
    if offset > MAX_SAFE:
        raise Rejected(f"chunk offset {offset} is too large")
    magic, name_length, data_length = struct.unpack("<IIQ", read(offset, 16))
    if magic != CHUNK_MAGIC:
        raise Rejected(f"no ND2 chunk at {offset}")
    if data_length > MAX_SAFE:
        raise Rejected("chunk length too large")
    return name_length, data_length, read(offset + 16, name_length).split(b"\0", 1)[0]


# ---- typed member access (conventions/nd2/README.md §2.2)

def _missing(what: str, default):
    if default is REQUIRED:
        raise Rejected(f"missing {what}")
    return default


def number(value, what: str, default=REQUIRED):
    if value is None:
        return _missing(what, default)
    if not isinstance(value, Scalar) or value.type not in (2, 3, 4, 5, 6):
        raise Rejected(f"{what} is not a number")
    v = float(value.value)  # every number is used as binary64 (conventions/nd2/README.md §2.2)
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


def _byte_list(value: LVBytes) -> LVList:
    """A byte array as a list: each byte a value of type 3."""
    return LVList(Scalar((3, b)) for b in value)


def lst(value, what: str, default=REQUIRED):
    if value is None:
        return _missing(what, default)
    if isinstance(value, LVBytes):
        return _byte_list(value)
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
    if isinstance(value, LVBytes):
        return list(_byte_list(value))
    raise Rejected(f"{what} is not an object or a list")


def _valid(items: list, flags, what: str) -> list:
    if flags is None:
        return items
    f = [flag(x, f"{what} entry") for x in lst(flags, what)]
    return [m for i, m in enumerate(items) if i < len(f) and f[i]]


# ---- experiment (conventions/nd2/README.md §3)

def _node_loop(node: dict):
    """A node's loop as (kind, count, period or step), "spectral", or None (skipped)."""
    typ = integer(node.get("eType"), "eType")
    if typ not in (1, 2, 4, 6, 8):
        raise Rejected(f"unsupported experiment loop type {typ}")
    pars = obj(node.get("uLoopPars"), "uLoopPars", None)
    item_valid = lst(node.get("pItemValid"), "pItemValid", None)
    for x in item_valid or []:
        flag(x, "pItemValid entry")
    if pars is None:
        return None
    if typ == 1:
        count = integer(pars.get("uiCount"), "uiCount", 0)
        loop = ("t", count, number(pars.get("dPeriod"), "dPeriod", 0))
    elif typ == 8:
        periods = [obj(p, "pPeriod member") for p in members(pars.get("pPeriod"), "pPeriod")]
        valid = _valid(periods, pars.get("pPeriodValid"), "pPeriodValid")
        count = sum(integer(p.get("uiCount"), "uiCount") for p in valid)
        if count > MAX_SAFE:
            raise Rejected("the time loop's count is more than 2^53 - 1")
        periods_ms = [number(p.get("dPeriod"), "dPeriod", 0) for p in valid]
        # One period only when every valid phase has it (else no time step).
        loop = ("t", count, periods_ms[0] if periods_ms and all(p == periods_ms[0] for p in periods_ms) else 0)
    elif typ == 2:
        points = [obj(q, "Points member") for q in
                  _valid(members(pars.get("Points"), "Points"), item_valid, "pItemValid")]
        count = len(points)
        # The stage position of each valid point, in µm (None if absent).
        stage = [(number(q.get("dPosX"), "dPosX", None), number(q.get("dPosY"), "dPosY", None)) for q in points]
        loop = ("p", count, stage)
    elif typ == 4:
        count = integer(pars.get("uiCount"), "uiCount", 0)
        step = abs(number(pars.get("dZStep"), "dZStep", 0))
        high = number(pars.get("dZHigh"), "dZHigh", 0)
        low = number(pars.get("dZLow"), "dZLow", 0)
        if step == 0 and count > 1:
            step = abs(high - low) / (count - 1)
            if not math.isfinite(step):
                raise Rejected("the z step is not finite")
        # A negative step: the stage moves down, and the z index is flipped (§4.3).
        loop = ("z", count, step, number(pars.get("dZStep"), "dZStep", 0) < 0)
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
            kind, count, scale, *flip = loop
            item = {"kind": kind, "count": count, "scale": scale, "depth": depth}
            if kind == "z":
                item["flip"] = bool(flip and flip[0])
            if not loops or loops[-1]["depth"] < depth:
                loops.append(item)
            elif loops[-1]["depth"] == depth and loops[-1]["kind"] == kind and loops[-1]["count"] < count:
                loops[-1] = item
        for child in members(node.get("ppNextLevelEx"), "ppNextLevelEx"):
            visit(obj(child, "experiment node"), child_depth)

    def check(node: dict) -> None:
        # Every node is checked, whether or not the flattening visits it.
        _node_loop(node)
        for child in members(node.get("ppNextLevelEx"), "ppNextLevelEx"):
            check(obj(child, "experiment node"))

    if root is not None:
        check(obj(root, "SLxExperiment"))
        visit(root, 0)
    kinds = [l["kind"] for l in loops]
    if len(set(kinds)) != len(kinds):
        raise Rejected(f"repeated loop kinds {kinds}")
    return loops


class Rows(Sequence):
    """The ranges of `count` rows of `length` bytes, `stride` bytes apart from
    `start`: a row block's reference, built only when written."""

    __slots__ = ("start", "stride", "length", "count")

    def __init__(self, start: int, stride: int, length: int, count: int) -> None:
        self.start, self.stride, self.length, self.count = start, stride, length, count

    def __len__(self) -> int:
        return self.count

    def __getitem__(self, i):
        if isinstance(i, slice):
            return [self[j] for j in range(*i.indices(self.count))]
        if not -self.count <= i < self.count:
            raise IndexError(i)
        return (self.start + (i % self.count) * self.stride, self.length)

    def __iter__(self):
        for r in range(self.count):
            yield (self.start + r * self.stride, self.length)

    def __eq__(self, other) -> bool:
        return isinstance(other, Sequence) and list(self) == list(other)

    def __repr__(self) -> str:
        return f"Rows({self.start}, {self.stride}, {self.length}, {self.count})"


def _check_range(offset: int, length: int, size: int) -> None:
    if offset + length > size:
        raise Rejected(f"range [{offset}, {offset + length}) outside the {size}-byte file")


def virtualize_nd2(url: str, read: Reader, size: int) -> Output:
    # §5.1
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
        try:
            return decode_lv(read(chunks[cname] + 16 + n, d), MAX_INFLATE, room=room)
        except TooLarge:
            raise Rejected(f"the profile's chunks hold more than {MAX_RECORDS} LV records and byte-array bytes") from None

    room = [MAX_RECORDS]  # shared by the three chunks (profiles/nd2.md §5.1)

    # conventions/nd2/README.md §3 attributes
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
    if min(width, height, comp) < 1 or comp > 1024:
        raise Rejected("image width and height must be at least 1, and components from 1 to 1024")
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

    # conventions/nd2/README.md §3 experiment
    exp = chunk(b"ImageMetadataLV!")
    loops = flatten_experiment(exp.get("SLxExperiment") if exp is not None else None)
    positions = next((l["count"] for l in loops if l["kind"] == "p"), 1)
    if positions > MAX_POSITIONS or positions * comp > MAX_POSITION_CHANNELS:
        raise Rejected(f"{positions} positions of {comp} components: more than {MAX_POSITIONS} positions, "
                       f"or more than {MAX_POSITION_CHANNELS} positions x components")

    # conventions/nd2/README.md §3 picture metadata
    seq = chunk(b"ImageMetadataSeqLV|0!")
    picture = (obj(seq.get("SLxPictureMetadata"), "SLxPictureMetadata", None) if seq is not None else None) or {}
    bcal = flag(picture.get("bCalibrated"), "bCalibrated", False)
    cal = number(picture.get("dCalibration"), "dCalibration", None)
    aspect = number(picture.get("dAspect"), "dAspect", 1)
    camera = [number(picture.get(f"dStgLgCT{k}"), f"dStgLgCT{k}", default)
              for k, default in (("11", 1.0), ("12", 0.0), ("21", 0.0), ("22", 1.0))]
    # The stage position without a position loop (conventions/nd2/README.md §4.3).
    picture_stage = (number(picture.get("dXPos"), "dXPos", None), number(picture.get("dYPos"), "dYPos", None))
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

    # conventions/nd2/README.md §4.2
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

    # profiles/nd2.md §5.3
    total = 1
    for l in loops:
        total *= l["count"]
    if total > MAX_SAFE:
        raise Rejected("more than 2^53 - 1 frames")
    offset = {}
    for cname, o in chunks.items():
        m = FRAME.fullmatch(cname)
        f = small_int(m[1]) if m else None
        if f is not None and f < total:
            offset[f] = o
    present = sorted(offset)
    # Every placed frame's header is read below: fetch them all at once where the
    # reader can. Each is the 16-byte chunk header and the name, which real files pad
    # so that the pixels start on a 4 KiB boundary (4072 bytes, then 8 bytes of stamp).
    if hasattr(read, "prefetch"):
        read.prefetch([(offset[f], FRAME_HEAD) for f in present])
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
        # Every placed frame's header: the image takes each frame's pixels at
        # the same distance from its chunk's offset.
        name_len = None
        for f in present:
            n, d, _ = _header(read, offset[f])
            if name_len is not None and n != name_len:
                raise Rejected(f"frame {f}'s chunk header differs in name length from frame {present[0]}'s")
            if d < 8 + height * width_bytes:
                raise Rejected(f"frame {f}'s chunk is too short for its pixels")
            name_len = n
        name_len = name_len or 0

        def start(f):
            return offset[f] + 16 + name_len + 8

        if width_bytes != row_bytes and present:
            # The block with the largest payload is the last block of the
            # frame that starts last: varint sizes grow with offsets.
            far = max(start(f) for f in present)
            h = next(d for d in range(height, 0, -1) if height % d == 0 and payload_size(
                Rows(far + (height - d) * width_bytes, width_bytes, row_bytes, d)) <= MAX_PAYLOAD)

        def frame_chunks(f):
            if width_bytes == row_bytes:
                return [[(start(f), height * row_bytes)]]
            return [Rows(start(f) + j * width_bytes, width_bytes, row_bytes, h) for j in range(0, height, h)]

    # conventions/nd2/README.md §4.3
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
    # conventions/nd2/README.md §4.3 stage positions: where each position's image goes.
    translations = None
    m11, m12, m21, m22 = camera
    det = m11 * m22 - m12 * m21
    stages = p["scale"] if p else [picture_stage]
    if calibrated and det != 0 and all(x is not None and y is not None for x, y in stages):
        translations = []
        for sx, sy in stages:
            u = (m22 * sx - m12 * sy) / det
            v = (m11 * sy - m21 * sx) / det
            shift = {"x": u - width * scale["x"] / 2, "y": v - height * scale["y"] / 2}
            if not all(math.isfinite(t) for t in shift.values()):
                raise Rejected("a stage position is not finite")
            translations.append([shift.get(a, 0) for a in axes])
    out = Output(url)
    # The source metadata (conventions/nd2/README.md §5): the small file-level
    # chunks at the root; the rest on vzip_source.
    stamps = {f: (starts[f][0] - 8 if compressed else start(f) - 8) for f in present}
    frames = Frames(total, offset, stamps, None if compressed else name_len,
                    None if compressed else height * width_bytes)
    meta = source_metadata(read, size, chunks, loops, frames)
    root_meta = {"signature": json_text(read(48, 64)), "chunks": meta.root}
    out.json("zarr.json", root_json({"version": "0.5", "bioformats2raw.layout": 3}, "nd2", url, root_meta, REVISION))
    if meta.node or meta.arrays:
        emit_plans(out, meta.arrays, declare({}, "nd2", None, meta.node or None))
    out.json("OME/zarr.json", group_json({"version": "0.5", "series": [str(i) for i in range(positions)]}))
    for pi in range(positions):
        ome = image_ome(axes, units, [[scale[a] for a in axes]], f"position {pi}",
                        [translations[pi]] if translations else None)
        ome["omero"] = {"channels": [{"label": label, "color": c, "active": True, **window}
                                     for label, c in zip(labels, colors)]}
        out.json(f"{pi}/zarr.json", group_json(ome))
        out.json(f"{pi}/0/zarr.json", array_json([shape[a] for a in axes], data_type,
                                                  [chunk_shape[a] for a in axes], codecs, axes))
    for f in present:
        coords, rest = {}, f
        for l in reversed(loops):
            c = rest % l["count"]
            coords[l["kind"]] = l["count"] - 1 - c if l.get("flip") else c  # a negative z step is flipped
            rest //= l["count"]
        for j, ranges in enumerate(frame_chunks(f)):
            last = ranges[-1]  # the ranges ascend
            _check_range(*last, size)
            if payload_size(ranges) > MAX_PAYLOAD:
                raise Rejected(f"frame {f}'s reference payload exceeds {MAX_PAYLOAD} bytes")
            index = [coords.get(a, 0) if a in ("t", "z") else j if a == "y" else 0 for a in axes]
            out.refs[f"{coords.get('p', 0)}/0/c/" + "/".join(map(str, index))] = ranges
    out.summary = {"sizes": {**{l["kind"]: l["count"] for l in loops}, "c": comp, "y": height, "x": width},
                   "dataType": data_type, "compressed": compressed, "paddedRows": width_bytes != row_bytes,
                   "rowBlock": h, "positions": positions, "frames": total, "missing": total - len(present),
                   "channels": labels}
    return out
