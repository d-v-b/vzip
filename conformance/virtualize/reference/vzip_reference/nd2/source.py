"""An ND2 file's metadata chunks as JSON, and its numeric streams as arrays:
the source metadata of the ND2 convention (spec/virtualize/nd2.md §5)."""

from __future__ import annotations

import json
import math
import re
import struct
from dataclasses import dataclass, field

from vzip.virtualize.common import (
    MAX_CHUNK, MAX_PADDING, MAX_SAFE, MAX_TOTAL_PADDING, SMALL_PADDING, Plan, Reader, Rejected, blob_chunks, declare,
    decode_text, family_plans, grid_chunks, text_json,
)
from vzip_reference.nd2.lv import LVBytes, Scalar, decode_lv
from vzip_reference.tiff.virtualize import ATTRS, DECIMAL, REFS, SCAN

CHUNK_MAGIC = 0x0ABECEDA
MAX_DATA_BYTES = 1 << 26  # the most chunk data, inflated, read per file
MAX_ROOT_JSON = 1 << 14  # a decoded chunk with more JSON than this is on vzip_source
MAX_ROOT_TOTAL = 1 << 16  # the most JSON of the root's chunks
MAX_NODE_TOTAL = 1 << 16  # the most JSON of vzip_source's source metadata
NODE_RESERVE = 128  # of it, what its chunks leave for its braces, keys, and other and empty as paths
LISTS = {"other": "other/names", "empty": "other/empty"}  # where other and empty go past the budget
MAX_COPIED = 1 << 16  # the chunk limit of an array of copied values (spec/conventions.md §7)
MAX_FAMILY = 1 << 24  # a family member's index is less than this
MAX_STAMPS = 1 << 20  # the frame times' written chunks hold at most this many bytes, or 512 per placed frame
MAX_TAG_JSON = 1 << 14  # the most JSON of a declared stream's tag (spec/virtualize/nd2.md §5.2)
DOCUMENTS = ("zarr.json", ".zarray", ".zgroup")  # names a path segment cannot have
TAGS = ("utf16", "int", "float")  # the names of the tags (spec/virtualize/nd2.md §5.1)
# Streams that every ND2 writer uses without declaring them: one value per frame.
STREAMS = {"AcqTimesCache": "float64", "AcqTimes2Cache": "float64", "X": "float64", "Y": "float64",
           "Z": "float64", "Z1": "float64", "Z2": "float64", "AcqFramesCache": "int32"}
DECLARED = {2: "int32", 3: "float64"}  # CustomTagDescription Type -> data type
SIZES = {"int32": 4, "float64": 8}
INTS = {f"lx_{s}int{b}" for s in ("", "u") for b in (8, 16, 32, 64)}
INDEXED = re.compile(rb"\|([0-9]+)!\Z")
INDEX = re.compile(r"0|[1-9][0-9]*")  # a name that is an array index: JavaScript would order it first
FRAME = re.compile(rb"ImageDataSeq\|(0|[1-9][0-9]*)!")
FAMILY = re.compile(rb"CustomDataSeq\|(.+)\|(0|[1-9][0-9]*)!", re.S)
DECLARATIONS = b"CustomDataVar|CustomDataV2_0!"
OTHER = "other"  # the reserved path of the chunks that have no path of their own


def small_int(digits: bytes) -> int | None:
    """Decimal digits as an integer, or None when there are more than 20 (at least 10^20)."""
    return int(digits) if len(digits) <= 20 else None


# ---- JSON (spec/virtualize/nd2.md §5.1)

def _pair_like(j) -> bool:
    return isinstance(j, list) and len(j) == 2 and isinstance(j[0], str)


def pairs_or_object(records: list):
    """Named values as an object, or as [name, value] pairs when a name repeats,
    a name is an array index (0, 1, ..., without leading zeros), or the object
    would read as a tag (one member named utf16, int or float)."""
    names = [n for n, _ in records]
    if (len(set(names)) < len(names) or (len(names) == 1 and names[0] in TAGS)
            or any(INDEX.fullmatch(n) for n in names)):
        return [[n, v] for n, v in records]
    return dict(records)


def number_json(v):
    """A number as JSON: itself, or a tag when JSON cannot hold it exactly
    (spec/virtualize/nd2.md §5.1)."""
    if isinstance(v, float):
        if math.isnan(v):
            return {"float": "NaN"}
        if math.isinf(v):
            return {"float": "Infinity" if v > 0 else "-Infinity"}
        if v == 0 and math.copysign(1, v) < 0:
            return {"float": "-0"}  # JSON.stringify writes -0 as 0
        return v
    return v if abs(v) <= MAX_SAFE else {"int": str(v)}


def lv_json(v):
    """An LV value as JSON: objects (or pairs), lists, and scalars by their type."""
    if isinstance(v, Scalar):
        if v.type == 8:
            return v[2] if len(v) > 2 else v.value
        return v.value if v.type == 1 else number_json(v.value)
    if isinstance(v, LVBytes):
        return list(v)
    if isinstance(v, list):
        items = [lv_json(x) for x in v]
        # A list that would read as pairs is written as pairs with empty names.
        return [["", j] for j in items] if items and all(map(_pair_like, items)) else items
    records = getattr(v, "records", None)
    return pairs_or_object([(k, lv_json(x)) for k, x in (records if records is not None else v.items())])


def _js_number(x: float) -> str:
    """ECMAScript Number::toString of a finite binary64 (as JSON.stringify writes it)."""
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    mantissa, _, exp = repr(abs(x)).partition("e")
    whole, _, frac = mantissa.partition(".")
    digits, n = whole + frac, len(whole) + int(exp or 0)
    stripped = digits.lstrip("0")
    n -= len(digits) - len(stripped)
    digits = stripped.rstrip("0")
    k = len(digits)
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * -n + digits
    e = n - 1
    return sign + digits[0] + ("." + digits[1:] if k > 1 else "") + ("e+" if e >= 0 else "e-") + str(abs(e))


def _js_string_size(s: str) -> int:
    size = 2
    for c in s:
        o = ord(c)
        if c in '"\\\b\f\n\r\t':
            size += 2
        elif o < 0x20 or 0xD800 <= o <= 0xDFFF:
            size += 6
        else:
            size += 1 if o < 0x80 else 2 if o < 0x800 else 3 if o < 0x10000 else 4
    return size


def json_size(v) -> int:
    """The length in UTF-8 bytes of `v` as ECMAScript's JSON.stringify writes it,
    without whitespace (spec/virtualize/nd2.md §5.1)."""
    if v is None or v is True:
        return 4
    if v is False:
        return 5
    if isinstance(v, int):
        return len(str(v))
    if isinstance(v, float):
        return len(_js_number(v))
    if isinstance(v, str):
        return _js_string_size(v)
    if isinstance(v, list):
        return 2 + sum(json_size(x) for x in v) + max(0, len(v) - 1)
    return 2 + sum(_js_string_size(k) + 1 + json_size(x) for k, x in v.items()) + max(0, len(v) - 1)


# ---- XML variants (spec/virtualize/nd2.md §5.1)

def _decode(v: str) -> str:
    """Character references as spec/virtualize/tiff.md §3 reads them, without parsing long digit strings."""
    def ref(m):
        if m[3]:
            return {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}[m[3]]
        digits = (m[1] or m[2]).lstrip("0")
        c = int(digits or "0", 16 if m[1] else 10) if len(digits) <= 8 else 0x110000
        if c == 0 or 0xD800 <= c <= 0xDFFF or c > 0x10FFFF:
            return m[0]
        return chr(c)
    return REFS.sub(ref, v)


def _tags(xml: str):
    """The tags of `xml`, as (closing, name, attributes, self-closing) (spec/virtualize/tiff.md §3)."""
    for m in SCAN.finditer(xml):
        if m["tag"] is not None:
            attrs: dict = {}
            for a in ATTRS.finditer(m["attrs"]):
                attrs.setdefault(a[1], _decode(a[2] if a[2] is not None else a[3]))  # first wins
            yield m["close"] == "/", m["name"], attrs, m["self"] == "/"


def _integer(value: str):
    sign = "-" if value[0] == "-" else ""
    digits = value.lstrip("+-").lstrip("0") or "0"
    return number_json(int(sign + digits)) if len(digits) <= 20 else {"int": sign + digits}


def _scalar(runtype: str | None, value: str):
    if runtype in INTS and re.fullmatch(r"[+-]?[0-9]+", value):
        return _integer(value)
    if runtype in ("double", "float") and DECIMAL.fullmatch(value):
        return number_json(float(value))
    if runtype == "bool" and value in ("true", "false"):
        return value == "true"
    return value


NO_VALUE = object()
MAX_XML_DEPTH = 100  # the variant element is at depth 0


def variant_json(data: bytes):
    """An XML variant document (<variant> of elements with runtype and value) as
    JSON, or None if it is not one."""
    try:
        xml = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    stack: list[list] = []  # [name, value attribute or NO_VALUE, children]
    root = None
    for closing, name, attrs, self_closing in _tags(xml):
        if root is not None:
            return None  # content after the document element
        if closing:
            if not stack or stack[-1][0] != name:
                return None
            _, scalar, children = stack.pop()
        else:
            if len(stack) > MAX_XML_DEPTH:
                return None  # nested too deep
            scalar = _scalar(attrs.get("runtype"), attrs["value"]) if "value" in attrs else NO_VALUE
            children = []
            if not self_closing:
                stack.append([name, scalar, children])
                continue
        value = scalar if scalar is not NO_VALUE else pairs_or_object(children)
        if stack:
            stack[-1][2].append((name, value))  # (the children of an element with a value are not kept)
        elif name == "variant" and scalar is NO_VALUE:
            root = value
        else:
            return None
    return root if not stack else None


def as_object(v) -> dict | None:
    """A JSON object, or pairs read as an object (first position, last value); None otherwise."""
    if isinstance(v, dict):
        return v
    if isinstance(v, list) and v and all(map(_pair_like, v)):
        return dict((n, x) for n, x in v)
    return None


def declared_streams(doc) -> dict:
    """ID -> (data type, CustomTagDescription member, its index among the
    members) of the streams CustomDataV2_0 declares (§5.2)."""
    out: dict = {}
    doc = as_object(doc)
    tags = as_object(doc.get("CustomTagDescription_v1.0")) if doc is not None else None
    for i, tag in enumerate((tags or {}).values()):
        t = as_object(tag)
        if t is None:
            continue
        sid, typ = t.get("ID"), t.get("Type")
        if isinstance(sid, str) and type(typ) in (int, float) and typ in DECLARED and sid not in out:
            out[sid] = (DECLARED[typ], tag, i)
    return out


# ---- paths (spec/virtualize/nd2.md §5.3)

def _segment(s: str) -> bool:
    return (s.strip(".") != "" and not s.startswith("__") and "/" not in s and "\0" not in s
            and s not in DOCUMENTS)


def node_path(name: str) -> str | None:
    """A chunk name's path under vzip_source: `A|B!` is `A/B` (None if not a valid path)."""
    parts = (name[:-1] if name.endswith("!") else name).split("|")
    return "/".join(parts) if all(map(_segment, parts)) else None


class Paths:
    """The paths taken under vzip_source, each a leaf. A path is free when it is
    valid, not taken, and neither an ancestor nor a descendant of one taken.
    `other` is taken from the start."""

    def __init__(self) -> None:
        self.leaves = {OTHER}
        self.ancestors: set[str] = set()

    def free(self, path: str | None) -> bool:
        if path is None or path in self.leaves or path in self.ancestors:
            return False
        parts = path.split("/")
        return not any("/".join(parts[:k]) in self.leaves for k in range(1, len(parts)))

    def take(self, path: str) -> None:
        self.leaves.add(path)
        parts = path.split("/")
        self.ancestors.update("/".join(parts[:k]) for k in range(1, len(parts)))


# ---- the chunks

@dataclass
class Source:
    root: dict = field(default_factory=dict)  # name -> JSON tree, at the root
    node: dict = field(default_factory=dict)  # vzip_source's source metadata
    arrays: list[Plan] = field(default_factory=list)


@dataclass
class Frames:
    """What the image takes from the frames (spec/virtualize.md §5.3)."""
    count: int  # N
    placed: dict[int, int]  # each placed frame's chunk offset
    stamps: dict[int, int]  # each placed frame's timestamp offset
    name_length: int | None = None  # uncompressed: the name length of every frame
    pixels: int | None = None  # uncompressed: the bytes of pixels after the timestamp


def _frame_scaled(name: bytes) -> bool:
    """Chunks that grow with the frames: the events, and per-frame metadata after frame 0."""
    if name in (b"ImageEventsLV!", b"CustomData|ExperimentEventsV1_0!"):
        return True
    m = INDEXED.search(name)
    return bool(m) and name.startswith(b"ImageMetadataSeqLV|") and m[1].strip(b"0") != b""  # n >= 1


def grid_index(loops: list[dict], f: int) -> int:
    """Frame f's index in the frame grid, row-major over the loops, with a
    flipped z index when the z loop flips (spec/virtualize/nd2.md §4.3).
    (The flip is its own inverse: the frame at grid index g is grid_index(g).)"""
    coords, rest = [], f
    for l in reversed(loops):
        c = rest % l["count"]
        coords.append(l["count"] - 1 - c if l.get("flip") else c)
        rest //= l["count"]
    g = 0
    for l, c in zip(loops, reversed(coords)):
        g = g * l["count"] + c
    return g


def grid_cut(shape: list[int], item: int, limit: int = MAX_CHUNK) -> tuple[int, int]:
    """The cut axis `a` and the chunk length `c` along it that spec/conventions.md §7
    gives contiguous values of `shape` with the chunk limit `limit` (as
    common.grid_chunks cuts them), without listing the chunks: of the counts
    with the same `c` (and so the same padding), only the first is tried."""
    a, slab, total = 0, item, item
    for v in shape[1:]:
        slab *= v
    for v in shape:
        total *= v
    while slab > limit:
        a += 1
        slab //= shape[a]
    while True:
        n = shape[a]
        outer = math.prod(shape[:a])  # the edge chunks
        k = -(-n // (limit // slab))
        best = None  # (padding, c): the first count padding at most SMALL_PADDING, else the least
        q = k
        while q <= min(2 * k, n):
            c = -(-n // q)
            pad = (-(-n // c) * c - n) * slab
            if best is None or pad < best[0]:
                best = (pad, c)
            if pad <= SMALL_PADDING or c == 1:
                break
            q = -(-n // (c - 1))  # the first count with a smaller c
        if best[0] <= MAX_PADDING and best[0] * outer <= max(MAX_TOTAL_PADDING, total // 64):
            return a, best[1]
        if a == len(shape) - 1:
            return a, -(-n // k)
        a += 1
        slab //= shape[a]


def grid_chunk(shape: list[int], a: int, c: int, g: int) -> tuple[tuple, int, int]:
    """The chunk of grid index `g` under the cut (a, c): its coords, the element's
    index within it, and the elements of the chunk that lie within the array."""
    inner = 1
    for v in shape[a + 1:]:
        inner *= v
    n = shape[a]
    outer, within = divmod(g, n * inner)
    i, rest = divmod(within, inner)
    coords = []
    for v in reversed(shape[:a]):
        outer, x = divmod(outer, v)
        coords.append(x)
    q = i // c
    return (*reversed(coords), q, *(0,) * (len(shape) - a - 1)), (i - q * c) * inner + rest, min(c, n - q * c) * inner


def _lv_named(name: bytes) -> bool:
    """A chunk whose name says it holds LV data (spec/virtualize/nd2.md §5.1)."""
    return name.endswith(b"LV!") or b"LV|" in name


def source_metadata(read: Reader, size: int, chunks: dict[bytes, int], loops: list[dict], frames: Frames) -> Source:
    """The chunk map's chunks (spec/virtualize/nd2.md §5), in map order, sharing one budget."""
    out = Source()
    n_frames = frames.count
    grid = [l["count"] for l in loops] or [1]
    dims = [l["kind"] for l in loops] or ["frame"]
    flipped = any(l.get("flip") for l in loops)
    position = {name: i for i, name in enumerate(chunks)}  # map order

    def data_of(offset: int) -> tuple[int, int] | None:
        """A chunk's (data offset, length), if it takes part."""
        if offset + 16 > size:
            return None
        magic, n, d = struct.unpack("<IIQ", read(offset, 16))
        start = offset + 16 + n
        return (start, d) if magic == CHUNK_MAGIC and start + d <= size else None

    used = 0

    def decode(name: bytes, start: int, d: int):
        """A chunk decoded within the budget, or None. Every attempt is charged."""
        nonlocal used
        if not d or used + d > MAX_DATA_BYTES:
            return None
        data = read(start, d)
        inflated: list[int] = []
        if name.startswith(b"CustomDataVar|"):
            value = variant_json(data)
        else:
            try:
                # A CustomData chunk holds LV data only by guess: it decodes only when its JSON keeps every byte.
                # LV data of more records and array bytes than vzip_source's budget has JSON too large for it.
                value = lv_json(decode_lv(data, MAX_DATA_BYTES - used - d, inflated, exact=not _lv_named(name),
                                          room=MAX_NODE_TOTAL))
            except Rejected:
                value = None
        used += d + sum(inflated)
        return value

    # The declarations first (§5.2): they say which chunks are streams.
    decoded: dict[bytes, object] = {}
    at = data_of(chunks[DECLARATIONS]) if DECLARATIONS in chunks else None
    if at is not None:
        decoded[DECLARATIONS] = decode(DECLARATIONS, *at)
    declared = declared_streams(decoded.get(DECLARATIONS))
    stream_ids = list(STREAMS) + [i for i in declared if i not in STREAMS]
    stream_names = {f"CustomData|{sid}!".encode("utf-8"): sid for sid in stream_ids}

    empty: list[bytes] = []
    other: list[tuple[bytes, int, int]] = []  # (chunk name, data offset, length)
    beyond: list[tuple[int, int, int, bytes]] = []  # frames f >= N: (f - N, offset, length, chunk name)
    families: dict[bytes, list] = {}  # CustomDataSeq|<name>|<i>! -> [(i, offset, length, chunk name)]
    kept: list = []  # chunks kept as bytes at their path, and families
    streams: dict[str, tuple[bytes, int, int]] = {}
    texts: set[str] = set()
    chunk_json: dict[bytes, object] = {}
    chunk_at: dict[bytes, tuple[int, int]] = {}  # a decoded chunk's data offset and length
    for name, offset in chunks.items():
        frame = FRAME.fullmatch(name)
        f = small_int(frame[1]) if frame else None
        if f is not None and f < n_frames:
            continue  # a placed frame: its pixels are the image; its timestamp and trailing bytes below
        at = data_of(offset)
        if at is None:
            continue
        start, d = at
        is_frame = name.startswith(b"ImageDataSeq|")
        text = decode_text(name)
        if d == 0:
            empty.append(name)
        elif is_frame:
            if f is not None and f - n_frames < MAX_FAMILY:
                beyond.append((f - n_frames, start, d, name))
            else:
                other.append((name, start, d))
        elif text in texts:
            other.append((name, start, d))  # a name that reads like an earlier one
        elif m := FAMILY.fullmatch(name):
            i = small_int(m[2])
            if i is not None and i < MAX_FAMILY:
                if m[1] not in families:
                    families[m[1]] = []
                    kept.append(("family", m[1]))
                families[m[1]].append((i, start, d, name))
            else:
                other.append((name, start, d))
        elif name in stream_names:
            streams[stream_names[name]] = (name, start, d)
        else:
            value = None
            if _lv_named(name) or name.startswith((b"CustomDataVar|", b"CustomData|")):
                value = decoded[name] if name in decoded else decode(name, start, d)
            if value is not None:
                chunk_json[name] = value
                chunk_at[name] = (start, d)
            else:
                kept.append(("bytes", name, start, d))
        if not is_frame:
            texts.add(text)

    # Decoded chunks: on vzip_source when they grow with the frames or are large;
    # then, while the root's chunks have more than 64 KiB of JSON, the largest
    # of them (the first in map order of equal sizes) moves too.
    sizes = {name: json_size(value) for name, value in chunk_json.items()}
    on_node = {name for name in chunk_json if _frame_scaled(name) or sizes[name] > MAX_ROOT_JSON}
    members = {name: _js_string_size(decode_text(name)) + 1 + sizes[name] for name in chunk_json if name not in on_node}
    total, count = sum(members.values()), len(members)
    for name in sorted(members, key=lambda n: (-members[n], position[n])):
        if 2 + total + max(0, count - 1) <= MAX_ROOT_TOTAL:
            break
        on_node.add(name)
        total, count = total - members[name], count - 1
    # vzip_source's chunks, in map order, each while they fit in 64 KiB of
    # JSON; a chunk that does not is kept as bytes.
    room = MAX_NODE_TOTAL - NODE_RESERVE - 2
    for name, value in chunk_json.items():
        if name not in on_node:
            out.root[decode_text(name)] = value
            continue
        cost = _js_string_size(decode_text(name)) + 1 + sizes[name] + (1 if "chunks" in out.node else 0)
        if cost <= room:
            room -= cost
            out.node.setdefault("chunks", {})[decode_text(name)] = value
        else:
            kept.append(("bytes", name, *chunk_at[name]))

    paths = Paths()

    def family(path: str, members: list[tuple[int, int, int, bytes]]) -> None:
        """A family (conventions §7) of at most 16 members per member and 1024 more:
        a member whose index is not less than that goes to other."""
        cap = 16 * len(members) + 1024
        other.extend((name, o, d) for i, o, d, name in members if i >= cap)
        members = [(i, o, d) for i, o, d, _ in members if i < cap]
        if members:
            paths.take(path)
            out.arrays.extend(family_plans(path, members, read))

    # The frames' timestamps: the binary64 at the start of each frame, copied,
    # since they are scattered. The array is cut as contiguous values would be
    # (conventions §7), and only the chunks that hold a placed frame are
    # written, NaN where a frame is missing (and zero past the array's edge).
    # The chunk limit halves from 64 KiB while those chunks would hold more
    # than max(1 MiB, 512 bytes per placed frame).
    if frames.stamps:
        paths.take("ImageDataSeq")
        bound = max(MAX_STAMPS, 512 * len(frames.stamps))
        indexes = [grid_index(loops, f) for f in frames.stamps]
        limit = MAX_COPIED
        while True:
            a, c = grid_cut(grid, 8, limit)
            chunk_shape = [1] * a + [c] + grid[a + 1:]
            elements = math.prod(chunk_shape)
            keys: set = set()
            for g in indexes:
                keys.add(grid_chunk(grid, a, c, g)[0])
                if 8 * elements * len(keys) > bound:
                    break
            if 8 * elements * len(keys) <= bound:
                break
            limit //= 2  # at 8 bytes, a chunk per placed frame: 8 bytes each
        nan = struct.pack("<d", float("nan"))
        stamps: dict = {}
        for at, g in zip(frames.stamps.values(), indexes):
            key, i, m = grid_chunk(grid, a, c, g)
            if key not in stamps:
                stamps[key] = bytearray(nan * m + bytes(8 * (elements - m)))
            stamps[key][8 * i:8 * i + 8] = read(at, 8)
        out.arrays.append(Plan("ImageDataSeq", "float64", grid, chunk_shape, dims,
                               {key: bytes(b) for key, b in stamps.items()}, "NaN"))
    # The frames the image does not place, whole, and the bytes after a placed frame's pixels.
    if beyond:
        family("ImageDataSeq.beyond", beyond)
    if frames.pixels is not None:
        trailing = []
        for f in sorted(frames.placed):
            o = frames.placed[f]
            n = frames.name_length
            d = struct.unpack("<Q", read(o + 8, 8))[0]  # the profile has checked the header
            if o + 16 + n + d > size or d <= 8 + frames.pixels:
                continue
            start, length = o + 16 + n + 8 + frames.pixels, d - 8 - frames.pixels
            if f < MAX_FAMILY:
                trailing.append((f, start, length, b"ImageDataSeq|%d!" % f))
            else:
                other.append((b"ImageDataSeq|%d!" % f, start, length))
        if trailing:
            family("ImageDataSeq.trailing", trailing)

    # Streams: one value per frame, typed (the undeclared ones, then those that
    # CustomDataV2_0 declares), shaped to the frame grid and cut as contiguous values.
    for sid in stream_ids:
        if sid not in streams:
            continue
        name, start, d = streams[sid]
        data_type = STREAMS[sid] if sid in STREAMS else declared[sid][0]
        k = SIZES[data_type]
        path = node_path(decode_text(name))
        rest = d - k * n_frames
        if rest < 0 or not paths.free(path) or (rest > 0 and not paths.free(f"{path}.rest")):
            kept.append(("bytes", name, start, d))
            continue
        paths.take(path)
        if flipped:  # in the grid's order: copies, padded with zero bytes
            values = read(start, k * n_frames)
            a, c = grid_cut(grid, k, MAX_COPIED)
            chunk_shape = [1] * a + [c] + grid[a + 1:]
            elements = math.prod(chunk_shape)
            copies: dict = {}
            for g in range(n_frames):
                key, i, _ = grid_chunk(grid, a, c, g)
                if key not in copies:
                    copies[key] = bytearray(k * elements)
                f = grid_index(loops, g)
                copies[key][k * i:k * (i + 1)] = values[k * f:k * (f + 1)]
            cs = {key: bytes(b) for key, b in copies.items()}
        else:
            chunk_shape, cs = grid_chunks(start, grid, k, read)
        attrs = None
        if sid in declared:  # its member, or past 16 KiB of JSON its index among the members
            _, tag, index = declared[sid]
            attrs = declare({}, "nd2", None, {"tag": tag} if json_size(tag) <= MAX_TAG_JSON else {"tag_index": index})
        out.arrays.append(Plan(path, data_type, grid, chunk_shape, dims, cs, 0, attrs))
        if rest > 0:  # the bytes after the first N values
            paths.take(f"{path}.rest")
            chunk_size, cs = blob_chunks(start + k * n_frames, rest)
            out.arrays.append(Plan(f"{path}.rest", "uint8", [rest], [chunk_size], ["byte"], cs))

    # Every other chunk, as its bytes, at its path, in map order (a family at its first member's place).
    def map_position(item) -> int:
        return position[item[1]] if item[0] == "bytes" else position[families[item[1]][0][3]]

    for item in sorted(kept, key=map_position):
        if item[0] == "bytes":
            _, name, start, d = item
            path = node_path(decode_text(name))
            if paths.free(path):
                paths.take(path)
                chunk_size, cs = blob_chunks(start, d)
                out.arrays.append(Plan(path, "uint8", [d], [chunk_size], ["byte"], cs))
            else:
                other.append((name, start, d))
        else:
            members = families[item[1]]
            path = node_path(f"CustomDataSeq|{decode_text(item[1])}!")
            if paths.free(path):
                family(path, members)
            else:
                other += [(name, o, d) for _, o, d, name in members]

    # The chunks without a path of their own, numbered in map order.
    other.sort(key=lambda x: position[x[0]])
    for i, (_, start, d) in enumerate(other):
        chunk_size, cs = blob_chunks(start, d)
        out.arrays.append(Plan(f"{OTHER}/{i}", "uint8", [d], [chunk_size], ["byte"], cs))
    # The names in other and empty, in S while it stays within 64 KiB, else as
    # an array of their JSON text (copied), at other/names and other/empty.
    for key, names in (("other", [name for name, _, _ in other]), ("empty", empty)):
        if not names:
            continue
        value = [text_json(name) for name in names]
        if json_size({**out.node, key: value}) <= MAX_NODE_TOTAL:
            out.node[key] = value
        else:
            out.node[key] = LISTS[key]
            out.arrays.append(copied_bytes(LISTS[key], json.dumps(value, ensure_ascii=False,
                                                                  separators=(",", ":")).encode("utf-8")))
    return out


def copied_bytes(path: str, data: bytes) -> Plan:
    """Bytes held in memory as a 1-D uint8 array (conventions §7), each chunk copied."""
    k = -(-len(data) // MAX_CHUNK)
    size = -(-len(data) // k)
    chunks = {(i,): data[i * size:(i + 1) * size].ljust(size, b"\0") for i in range(k)}
    return Plan(path, "uint8", [len(data)], [size], ["byte"], chunks)
