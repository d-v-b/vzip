"""Shared parts of the virtualizers (spec/virtualize.md §1, §2)."""

from __future__ import annotations

import base64
import os
import re
import struct
import json
import http.client
import threading
import urllib.error
import urllib.parse
import urllib.request
import math
from dataclasses import dataclass, field
from typing import Callable

from vzip.archive import VZipWriter
from vzip.pb import Range, Source


class Rejected(Exception):
    """The input is not accepted by the profile (spec/virtualize.md §1.2)."""


def same(a, b) -> bool:
    """JSON values equal as spec/virtualize.md §1.1 compares them: a number written as an integer
    (a Python int) is that integer exactly (§1.6 copies one with every digit), any other
    number (a float) its binary64 value, and two numbers are equal when their values are, so
    an integer equals only a float that is exactly that integer. Booleans are not numbers."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b  # Python compares an int and a float by their exact values
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


# A range of an output: (offset, length) of source 0, (source, offset, length)
# of any source, or literal bytes.
Part = tuple[int, int] | tuple[int, int, int] | bytes


@dataclass
class Output:
    """A virtualizer's output: the url source 0, the data sources after it,
    and the entries (§1.1, §1.2)."""

    url: str
    bytes_entries: dict[str, bytes] = field(default_factory=dict)
    refs: dict[str, list[Part]] = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    # The data sources 1, 2, ... in order of first use (§1.2), and their indexes.
    data: dict[bytes, int] = field(default_factory=dict)
    # The documents under these prefixes are read only when asked for, like chunks:
    # those of the source metadata node, and of nodes a profile has one of per tile.
    lazy: tuple[str, ...] = ("vzip_source/",)
    # Source 0's pins (§1.2), and the CRC-32C of a url source's bytes, when the
    # ranges are to carry checksums (spec/archive.md §5.2); set by vzip.virtualize.virtualize.
    size: int | None = None
    etag: str | None = None
    checksum: Callable[[str, int, int], int] | None = None

    def json(self, key: str, value) -> None:
        # UTF-8, not \u escapes: the documents are the size the budgets count, as JSON.stringify writes them.
        self.bytes_entries[key] = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()

    def shared(self, value: bytes) -> tuple[int, int, int]:
        """A range of all of `value`, as a data source (§1.2): the source is
        added to the table the first time `value` is used."""
        value = bytes(value)
        i = self.data.setdefault(value, len(self.data) + 1)
        return (i, 0, len(value))

    def write(self, out_path: str) -> None:
        with open(out_path, "wb") as fh:
            w = VZipWriter(fh, page_size=1 << 16, checksum=self.checksum)
            src = w.source(Source(url=self.url, size=self.size, etag=self.etag))
            for value, i in self.data.items():
                if w.blob(value) != i:
                    raise AssertionError("data sources out of order")

            def rng(r: Part) -> Range:
                if isinstance(r, bytes):
                    return Range(data=r)
                if len(r) == 3:
                    return Range(source=r[0], offset=r[1], length=r[2])
                return Range(source=src, offset=r[0], length=r[1])

            for key in sorted(self.refs):
                w.add_ranges(key, [rng(r) for r in self.refs[key]])
            for key in sorted(self.bytes_entries):
                # The hierarchy's documents are read when the archive opens; those of
                # the source metadata node (and the lazy prefixes), like chunks, only when asked for.
                late = key.endswith("zarr.json") and not key.startswith(self.lazy)
                w.add_bytes(key, self.bytes_entries[key], compress=not late, late=late)
            w.close()


MAX_PAYLOAD = 65519
SOURCE_NODE = "vzip_source"  # the source metadata node (spec/conventions.md §2)


@dataclass
class Plan:
    """An array of the source metadata node (spec/conventions.md §7)."""
    path: str  # under vzip_source
    data_type: str
    shape: list[int]
    chunk_shape: list[int]
    dims: list[str]
    chunks: dict  # coords -> ranges of the source, or bytes
    fill: object = 0
    attributes: dict | None = None
    endian: str = "little"
    compressor: dict | None = None  # a codec after `bytes`


def emit_plans(out: "Output", plans: list[Plan], node_attributes: dict | None) -> None:
    """The source metadata node, its groups and its arrays."""
    metadata_group(out, SOURCE_NODE, node_attributes)
    groups = sorted({"/".join(a.path.split("/")[:k]) for a in plans for k in range(1, a.path.count("/") + 1)})
    for g in groups:
        metadata_group(out, f"{SOURCE_NODE}/{g}")
    for a in plans:
        metadata_array(out, f"{SOURCE_NODE}/{a.path}", a.data_type, a.shape, a.chunk_shape, a.dims, a.chunks,
                       a.fill, a.attributes, a.endian, a.compressor)


def metadata_array(out: "Output", path: str, data_type: str, shape: list[int], chunk_shape: list[int],
                   dims: list[str], chunks: dict, fill=0, attributes: dict | None = None,
                   endian: str = "little", compressor: dict | None = None) -> None:
    """An array of the source metadata node, each chunk the ranges of the source
    that hold it, or its bytes when copied; `dims` None gives no dimension names."""
    codecs = [{"name": "bytes", "configuration": {"endian": endian}}] if data_type not in (
        "uint8", "int8") else [{"name": "bytes"}]
    if compressor is not None:
        codecs.append(compressor)
    doc = array_json(shape, data_type, chunk_shape, codecs, dims or [])
    if dims is None:
        del doc["dimension_names"]
    doc["fill_value"] = fill
    if attributes:
        doc["attributes"] = attributes
    out.json(f"{path}/zarr.json", doc)
    for coords, c in chunks.items():
        key = "/".join([f"{path}/c", *map(str, coords)])
        if isinstance(c, (bytes, bytearray)):
            out.bytes_entries[key] = bytes(c)
        else:
            out.refs[key] = list(c)


def metadata_group(out: "Output", path: str, attributes: dict | None = None) -> None:
    out.json(f"{path}/zarr.json", {"zarr_format": 3, "node_type": "group", "attributes": attributes or {}})


MAX_CHUNK = 1 << 24  # the largest chunk of a metadata array of bytes


MAX_PADDING = (1 << 16) - (1 << 10)  # the most zero bytes an edge chunk's reference holds
SMALL_PADDING = 1 << 10  # edge padding small enough to take the fewest chunks
MAX_TOTAL_PADDING = 1 << 20  # the most zero bytes all edge chunks of a cut hold


def grid_chunks(offset: int, shape: list[int], item: int, read: "Reader | None" = None,
                deepen: bool = True, limit: int = MAX_CHUNK) -> tuple[list[int], dict]:
    """The C-order array of `shape`, of `item`-byte elements contiguous at
    `offset`, cut as spec/conventions.md §7 cuts contiguous values: its chunk
    shape, and its chunks (coords -> the ranges that hold it, or its bytes).
    The cut axis `a` is the first whose index holds at most 2^24 bytes (`s`);
    along it, k = ceil(n / floor(2^24 / s)) chunks of c = ceil(n / k) indices,
    the grid's ceil(n / c), the last padded with zero bytes, or copied (with
    `read`) when the padding does not fit in a payload. Chunks hold at most
    `limit` bytes (2^24 by default). When the edge padding of k chunks would
    exceed MAX_PADDING bytes, k + 1, ..., 2k chunks are tried in turn (each
    balanced), and, with `deepen`, failing those the cut moves to the next axis
    (the last axis's first choice is kept whatever its padding)."""
    shape = list(shape)
    if item > limit:
        raise ValueError(f"an element of more than {limit} bytes")
    if 0 in shape:
        return [max(1, v) for v in shape], {}
    if not shape:
        return [], {(): [(offset, item)]}
    a, slab = 0, item
    for v in shape[1:]:
        slab *= v
    while slab > limit:
        a += 1
        slab //= shape[a]
    while True:
        n = shape[a]
        k = -(-n // (limit // slab))
        c = -(-n // k)
        best = None  # (padding bytes, c): the first count padding at most SMALL_PADDING, else the least
        for q in range(k, min(2 * k, n) + 1):
            cq = -(-n // q)
            pad = (-(-n // cq) * cq - n) * slab
            if best is None or pad < best[0]:
                best = (pad, cq)
            if pad <= SMALL_PADDING:
                break
        outer = 1
        for v in shape[:a]:
            outer *= v
        total = item
        for v in shape:
            total *= v
        # All edge chunks' padding: at most 1 MiB, or 1/64 of the values when they are larger.
        fits = best[0] <= MAX_PADDING and best[0] * outer <= max(MAX_TOTAL_PADDING, total // 64)
        if fits:
            c = best[1]
        if fits or not deepen or a == len(shape) - 1:
            break
        a += 1
        slab //= shape[a]
    tail = (0,) * (len(shape) - a - 1)
    chunks: dict = {}
    outer: list[tuple] = [()]
    for v in shape[:a]:
        outer = [(*o, i) for o in outer for i in range(v)]
    for o in outer:
        base = 0
        for i, v in zip(o, shape[:a]):
            base = base * v + i
        for q in range(-(-n // c)):
            m = min(c, n - q * c)
            parts: list = [(offset + (base * n + q * c) * slab, m * slab)]
            if m < c:
                parts.append(bytes((c - m) * slab))
                if payload_size(parts) > MAX_PAYLOAD:
                    if read is None:
                        raise ValueError("an edge chunk whose padding does not fit in a payload")
                    parts = read(parts[0][0], parts[0][1]) + parts[1]
            chunks[(*o, q, *tail)] = parts
    return [1] * a + [c, *shape[a + 1 :]], chunks


def row_chunks(offset: int, rows: int, row_bytes: int, row_shape: tuple = (), read: "Reader | None" = None
               ) -> tuple[int, dict]:
    """`rows` values of `row_bytes` bytes each (at most 2^24), contiguous at
    `offset`, as the chunks of an array along its first axis: grid_chunks of
    the bytes [rows, row_bytes], each chunk's coords followed by `row_shape`."""
    if row_bytes > MAX_CHUNK:
        raise ValueError("a row of more than 2^24 bytes")
    shape, chunks = grid_chunks(offset, [rows, row_bytes], 1, read, deepen=False)
    return shape[0], {(k[0], *row_shape): v for k, v in chunks.items()}


def family_plans(path: str, members: list[tuple[int, int, int]], read: "Reader",
                 count: int | None = None, ragged: bool = False) -> list[Plan]:
    """A family of byte values (index, offset, length), as spec/conventions.md §7 says,
    of `count` members (by default, one more than the largest index); `ragged`
    asks for the offsets-and-data form whatever the lengths."""
    if count is None:
        count = max(i for i, _, _ in members) + 1
    lengths = {d for _, _, d in members}
    if len(lengths) == 1 and lengths != {0} and not ragged:
        (length,) = lengths
        ordered = sorted(members)
        if len(ordered) == count and all(i == j and o == ordered[0][1] + j * length
                                         for j, (i, o, _) in enumerate(ordered)):
            # Adjacent in the source, in index order: contiguous values.
            shape, chunks = grid_chunks(ordered[0][1], [count, length], 1, read)
            return [Plan(path, "uint8", [count, length], shape, ["index", "byte"], chunks)]
        if length <= MAX_CHUNK:
            return [Plan(path, "uint8", [count, length], [1, length], ["index", "byte"],
                         {(i, 0): [(o, d)] for i, o, d in members})]
    by_index = {}
    for i, o, d in members:
        by_index.setdefault(i, (o, d))
    starts, pieces, total = [], [], 0
    for i in range(count):
        starts.append(total)
        if i in by_index:
            pieces.append((total, *by_index[i]))
            total += by_index[i][1]
    starts.append(total)
    packed = struct.pack(f"<{count + 1}q", *starts)
    # The offsets are copied, and cut as contiguous values (spec/conventions.md §7).
    shape, cut = grid_chunks(0, [count + 1], 8, lambda o, n: packed[o : o + n])
    offsets = {k: v if isinstance(v, bytes) else b"".join(packed[r[0] : r[0] + r[1]] if isinstance(r, tuple) else r
                                                          for r in v) for k, v in cut.items()}
    plans = [Plan(f"{path}/offsets", "int64", [count + 1], shape, ["index"], offsets)]
    if total:
        size_c = -(-total // -(-total // RAGGED_CHUNK))  # balanced: k = ceil(total / 2^20) chunks
        data_chunks = {}
        first = 0  # the first piece that may reach the chunk: one sweep over the pieces
        for c in range(-(-total // size_c)):
            lo, hi = c * size_c, min(total, (c + 1) * size_c)
            while first < len(pieces) and pieces[first][0] + pieces[first][2] <= lo:
                first += 1
            ranges: list = []
            for at, o, d in pieces[first:]:
                if at >= hi:
                    break
                if at + d > lo:
                    start, n = o + max(lo, at) - at, min(hi, at + d) - max(lo, at)
                    if ranges and ranges[-1][0] + ranges[-1][1] == start:  # adjacent in the source: one range
                        ranges[-1] = (ranges[-1][0], ranges[-1][1] + n)
                    else:
                        ranges.append((start, n))
            ranges += [bytes(size_c - (hi - lo))] if hi - lo < size_c else []
            if payload_size(ranges) > MAX_PAYLOAD:
                ranges = b"".join(read(r[0], r[1]) if isinstance(r, tuple) else r for r in ranges)
            data_chunks[(c,)] = ranges
        plans.append(Plan(f"{path}/data", "uint8", [total], [size_c], ["byte"], data_chunks))
    return plans


RAGGED_CHUNK = 1 << 20


def blob_chunks(offset: int, length: int) -> tuple[int, dict]:
    """A run of the source's bytes (length at least 1) as a 1-D uint8 array:
    its chunk size and chunks. The k chunks are of equal size ceil(length / k),
    k = ceil(length / 2^24), so that the last one, which Zarr stores whole,
    is padded with fewer than k zero bytes."""
    k = -(-length // MAX_CHUNK)
    size = -(-length // k)
    chunks = {}
    for i in range(k):
        n = min(size, length - i * size)
        chunks[(i,)] = [(offset + i * size, n)] + ([bytes(size - n)] if n < size else [])
    return size, chunks


MAX_SAFE = 2**53 - 1


def json_number(v):
    """A number as source metadata (spec/conventions.md §6)."""
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if math.isinf(v):
            return "Infinity" if v > 0 else "-Infinity"
        return v
    return v if abs(v) <= MAX_SAFE else str(v)


def decode_text(b: bytes) -> str:
    """Bytes as text: UTF-8 if valid, else ISO 8859-1 (spec/conventions.md §6)."""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1")


def text_json(b: bytes):
    """A text value (spec/conventions.md §6): UTF-8 if valid, else
    {"latin1": ...}, so that the bytes can always be recovered."""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return {"latin1": b.decode("latin-1")}


def json_text(b: bytes):
    """A fixed-size character field: its bytes up to the first NUL, as a text value."""
    return text_json(b.split(b"\0", 1)[0])


def json_base64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _varint_size(v: int) -> int:
    return max(1, (v.bit_length() + 6) // 7)


def _range_size(r: Part) -> int:
    if isinstance(r, bytes):
        return 1 + _varint_size(len(r)) + len(r)
    source, offset, length = r if len(r) == 3 else (0, *r)
    return sum(1 + _varint_size(v) for v in (source, offset, length) if v)


def payload_size(ranges: list[Part]) -> int:
    """The encoded size of a reference to `ranges` (§1.2)."""
    if len(ranges) == 1:
        return _range_size(ranges[0])
    return sum(1 + _varint_size(n) + n for n in map(_range_size, ranges))


UNITS = {
    "µm": "micrometer", "μm": "micrometer", "um": "micrometer", "nm": "nanometer",
    "mm": "millimeter", "cm": "centimeter", "m": "meter", "\u00c5": "angstrom", "\u212b": "angstrom",
    "pm": "picometer",
    "in": "inch", "ft": "foot", "s": "second", "ms": "millisecond", "min": "minute", "h": "hour",
}

TYPES = {"t": "time", "c": "channel", "z": "space", "y": "space", "x": "space"}

# Sizes of the length units in metres (conventions §5).
LENGTHS = {
    "micrometer": 1e-6, "nanometer": 1e-9, "millimeter": 1e-3, "centimeter": 1e-2, "meter": 1.0,
    "angstrom": 1e-10, "picometer": 1e-12, "inch": 0.0254, "foot": 0.3048,
}


def centred(cx: float, cy: float, w0: int, h0: int, sx: float, sy: float) -> dict:
    """The translation of an image whose centre is at (cx, cy) (conventions §5)."""
    return {"x": cx - w0 * sx / 2, "y": cy - h0 * sy / 2}


def array_json(shape, data_type: str, chunk_shape, codecs: list, axes: list[str]) -> dict:
    """An array's zarr.json (conventions §3)."""
    return {
        "zarr_format": 3,
        "node_type": "array",
        "shape": list(shape),
        "data_type": data_type,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": list(chunk_shape)}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": 0,
        "codecs": codecs,
        "dimension_names": list(axes),
        "attributes": {},
    }


def transpose_codec(axes: list[str]) -> dict:
    """The transpose codec for frames that hold the channel axis last (conventions §3)."""
    stored = [a for a in axes if a != "c"] + ["c"]
    return {"name": "transpose", "configuration": {"order": [axes.index(a) for a in stored]}}


def group_json(ome: dict) -> dict:
    return {"zarr_format": 3, "node_type": "group", "attributes": {"ome": ome}}


# The virtualization conventions (conventions §2): one per profile, each with its fixed
# UUID, its current version and its name in the description.
CONVENTION_KEY = "vzip_virtualized"
# The revision of spec/virtualize.md this implementation follows. Until the release, every
# convention is at version 0 and the root property records it (conventions §1, §2).
REVISION = 24
PROFILES = {
    "tiff": ("48e9ac4e-1156-4a62-955e-20467d9c2700", 0, "TIFF"),
    "ndpi": ("6cac71ef-dbb2-4acd-b60c-00389aa4238a", 0, "NDPI"),
    "nd2": ("59612f14-e314-4207-ba00-8f422ba71490", 0, "ND2"),
    "dicom": ("acf17198-e5a5-48d3-8187-22ec4bb40ea5", 0, "DICOM"),
    "nifti": ("06e5809d-4d54-4b72-afd0-6bf61a7b4c85", 0, "NIfTI"),
    "ims": ("5067a535-8261-4b25-a93c-1985ed333bde", 0, "IMS"),
    "n5": ("ad5d4c39-c69e-48f7-a3ef-4cc8c607d416", 0, "N5"),
    "zarr2": ("8e792619-d671-4687-ab51-752885dd3ee6", 0, "Zarr v2"),
    "ome-zarr": ("b74ea302-65bb-49ae-b81f-f9bb52cd4eed", 0, "OME-Zarr"),
    "safe": ("ef81346c-19e8-42ad-93b0-a279ccaf44c1", 0, "Sentinel-2 SAFE"),
    "czi": ("7a0733c7-d4be-4482-a64f-6904d9354ea5", 0, "CZI"),
}
UUIDS = frozenset(uuid for uuid, _, _ in PROFILES.values())


def convention(profile: str) -> dict:
    """The Convention Metadata Object of a profile's convention (conventions §2)."""
    uuid, version, title = PROFILES[profile]
    # Version 0 has no tag: its URLs name the development branch (conventions §1).
    ref, blob = ("heads/main", "main") if version == 0 else (f"tags/virtualize-{profile}-v{version}",) * 2
    return {
        "uuid": uuid,
        "schema_url": f"https://raw.githubusercontent.com/d-v-b/vzip/refs/{ref}/spec/virtualize/{profile}/schema.json",
        "spec_url": f"https://github.com/d-v-b/vzip/blob/{blob}/spec/virtualize/{profile}.md",
        "name": CONVENTION_KEY,
        "description": f"The Zarr layout of a {title} source virtualized by vzip, and the source's metadata",
    }


def root_property(profile: str, url: str, revision: int = REVISION) -> dict:
    """The root's property without its source metadata (conventions §2): the profile, its
    convention's version, the revision while that version is 0 (`revision`: the one the
    implementation follows; the frozen reference records its own), and the source URL."""
    version = PROFILES[profile][1]
    return {"profile": profile, "version": version, **({"revision": revision} if version == 0 else {}),
            "source": {"url": url}}


def declare(attributes: dict, profile: str, url: str | None, own: dict | None = None,
            revision: int = REVISION) -> dict:
    """A node's attributes (conventions §2): `attributes`, the members the target formats
    define (such as `ome`), and the profile's convention when the node is the
    root (`url`, the source URL, is given) or has source-specific metadata
    (`own` is a nonempty object): its metadata object in `zarr_conventions`,
    and the property `vzip_virtualized`, which holds `own` as its member
    named after the profile."""
    value: dict = {}
    if url is not None:
        value = root_property(profile, url, revision)
    if own:
        value[profile] = own
    if not value:
        return dict(attributes)
    return {**attributes, "zarr_conventions": [convention(profile)], CONVENTION_KEY: value}


def root_json(ome: dict, profile: str, url: str, own: dict | None = None, revision: int = REVISION) -> dict:
    """The root image group of a file profile (conventions §4, conventions §2), with the
    source-specific metadata `own`."""
    return {"zarr_format": 3, "node_type": "group",
            "attributes": declare({"ome": ome}, profile, url, own, revision)}


def image_ome(axes: list[str], units: dict, scales: list[list[float]], name: str | None,
              translations: list[list[float]] | None = None) -> dict:
    for t in translations or []:
        if not all(math.isfinite(v) for v in t):
            raise Rejected("a translation is not finite")
    """The OME-NGFF 0.5 object of an image (conventions §4), with a translation per
    level after its scale when `translations` is given."""
    ms = {}
    if name is not None:
        ms["name"] = name
    ms["axes"] = [{"name": a, "type": TYPES[a], **({"unit": units[a]} if units.get(a) else {})} for a in axes]
    ms["datasets"] = [
        {"path": str(i), "coordinateTransformations": [{"type": "scale", "scale": s}] + (
            [{"type": "translation", "translation": translations[i]}] if translations else [])}
        for i, s in enumerate(scales)
    ]
    return {"version": "0.5", "multiscales": [ms]}


Reader = Callable[[int, int], bytes]


def _retry(f):
    """Calls f, retrying transient network errors with backoff."""
    import time
    import urllib.error

    for attempt in range(5):
        try:
            return f()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 4:
                raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == 4:
                raise
        time.sleep(2**attempt)


def http_reader(url: str, block: int = 1 << 16) -> tuple[Reader, int]:
    """A cached range reader for an http(s) URL, and the object's size."""

    etags = EtagLog()

    def head():
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA}), timeout=60) as r:
                etags.saw(r.headers.get("ETag"))
                return int(r.headers["Content-Length"])
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                raise
        # The server refuses HEAD: take the size from a one-byte range request.
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            total = (r.headers.get("Content-Range") or "").rpartition("/")[2]
            if r.status != 206 or not total.isdigit():
                raise Rejected(f"{url}: cannot determine the size")
            return int(total)

    size = _retry(head)
    from collections import OrderedDict
    from concurrent.futures import ThreadPoolExecutor
    import bisect

    cache: "OrderedDict[int, bytes]" = OrderedDict()  # recently used blocks, at most MAX_BLOCKS
    exact: dict[int, bytes] = {}  # prefetched ranges, by start
    starts: list[int] = []
    # The reader is shared by threads (a profile's parallel reads, prefetch's pool): the
    # cache, the prefetched ranges and the counts change only under this lock. Fetches
    # run outside it, so two threads may fetch the same block once each.
    lock = threading.Lock()

    requests = [0, 0]  # requests, bytes

    local = threading.local()
    parts = urllib.parse.urlsplit(url)
    target = parts.path + (f"?{parts.query}" if parts.query else "")

    def get_range(start: int, end: int) -> bytes:
        with lock:
            requests[0] += 1
            requests[1] += end - start
        headers = {"Range": f"bytes={start}-{end - 1}", "User-Agent": UA}

        def get():
            # A kept-alive connection per thread: a new TLS connection per range
            # costs a round trip or more each, which dominates many small reads.
            conn = getattr(local, "conn", None)
            if conn is not None:
                try:
                    conn.request("GET", target, headers=headers)
                    r = conn.getresponse()
                    if r.status == 206:
                        etags.saw(r.getheader("ETag"))
                        return r.read()
                    r.read()
                except (OSError, http.client.HTTPException):
                    pass
                conn.close()
                local.conn = None
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
                etags.saw(r.headers.get("ETag"))
                data = r.read()
                final = urllib.parse.urlsplit(r.geturl())
                if final == parts and r.status == 206:  # no redirect: keep a connection for the next read
                    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
                    local.conn = cls(parts.netloc, timeout=120)
                return data

        data = _retry(get)
        if len(data) != end - start:
            raise OSError(f"{url}: short read at {start}")
        return data

    def prefetch(ranges) -> None:
        """Fetch many small (offset, length) ranges at once, in parallel: scattered
        reads, such as one per frame header, then cost one round trip each in
        parallel rather than one whole block each in turn."""
        with lock:
            have = {(o, o + len(exact[o])) for o in exact}
        todo = sorted({(o, min(size, o + n)) for o, n in ranges if 0 <= o < size and n > 0} - have)
        with ThreadPoolExecutor(32) as pool:
            got = list(zip(todo, pool.map(lambda r: get_range(*r), todo)))
        with lock:
            for (o, _), data in got:
                exact[o] = data
            starts[:] = sorted(exact)

    def blocks(first: int, last: int) -> list[bytes]:
        """Blocks `first` to `last`: each run of consecutive uncached blocks in one request."""
        got: dict[int, bytes] = {}
        with lock:
            for i in range(first, last + 1):
                if i in cache:
                    cache.move_to_end(i)
                    got[i] = cache[i]
        missing = [i for i in range(first, last + 1) if i not in got]
        k = 0
        while k < len(missing):
            j = k
            while j + 1 < len(missing) and missing[j + 1] == missing[j] + 1:
                j += 1
            a, b = missing[k], missing[j]
            data = get_range(a * block, min(size, (b + 1) * block))
            for i in range(a, b + 1):
                got[i] = data[(i - a) * block : (i - a + 1) * block]
            k = j + 1
        with lock:
            for i in missing:
                cache[i] = got[i]
                cache.move_to_end(i)
            while len(cache) > MAX_BLOCKS:
                cache.popitem(last=False)
        return [got[i] for i in range(first, last + 1)]

    def read(offset: int, length: int) -> bytes:
        if offset < 0 or offset + length > size:
            raise Rejected(f"read of [{offset}, {offset + length}) outside the {size}-byte file")
        with lock:
            j = bisect.bisect_right(starts, offset) - 1
            hit = exact[starts[j]] if j >= 0 and offset + length <= starts[j] + len(exact[starts[j]]) else None
            if hit is not None:
                return hit[offset - starts[j] : offset - starts[j] + length]
        if length == 0:
            return b""
        first = offset // block
        data = b"".join(blocks(first, (offset + length - 1) // block))
        return data[offset - first * block : offset - first * block + length]

    def uncached(offset: int, length: int) -> bytes:
        """A range read of its own, past the cache (thread-safe): for many small
        scattered reads, such as a codestream's tile-part headers."""
        if offset < 0 or offset + length > size:
            raise Rejected(f"read of [{offset}, {offset + length}) outside the {size}-byte file")
        return get_range(offset, offset + length) if length else b""

    read.prefetch = prefetch  # type: ignore[attr-defined]
    read.uncached = uncached  # type: ignore[attr-defined]
    read.requests = requests  # type: ignore[attr-defined]
    read.etag = etags.pin  # type: ignore[attr-defined]
    return read, size


class EtagLog:
    """The ETag of every response for one object, for its `etag` pin (spec/virtualize.md §1.2)."""

    def __init__(self) -> None:
        self.seen: set[str | None] = set()
        self._lock = threading.Lock()

    def saw(self, etag: str | None) -> None:
        with self._lock:
            self.seen.add(etag)
            strong = {e for e in self.seen if e is not None and not e.startswith("W/")}
        if len(strong) > 1:  # the object changed while it was read: a failure, not a rejection
            raise OSError(f"the object changed while it was read (ETags {sorted(strong)})")

    def pin(self) -> str | None:
        """The object's strong ETag, if every response gave the same one, else None."""
        if len(self.seen) != 1:
            return None
        (etag,) = self.seen
        return etag if etag is not None and STRONG_ETAG.match(etag) else None


STRONG_ETAG = re.compile(r'^"[\x21\x23-\x7e]*"$')


# Some servers (Mendeley Data) refuse urllib's default User-Agent; the store reads send this one.
UA = "vzip-virtualize"
MAX_BLOCKS = 2048  # cached blocks of an http reader (128 MiB at 64 KiB)


def file_reader(path: str) -> tuple[Reader, int]:
    import mmap

    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        # Mapped, not read: a file larger than memory is read only where it is used.
        data = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) if size else b""

    def read(offset: int, length: int) -> bytes:
        if offset < 0 or offset + length > len(data):
            raise Rejected(f"read of [{offset}, {offset + length}) outside the {len(data)}-byte file")
        return bytes(data[offset : offset + length])

    read.uncached = read  # type: ignore[attr-defined]
    return read, len(data)
