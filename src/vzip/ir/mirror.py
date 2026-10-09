"""The source mirror (ARCHITECTURE.md §3.4): one projection, for every format,
that writes an IR under `vzip_source`. It never reads the source.

- `vzip_source/ir/...`, the record: the element table as arrays (below), from
  which a reader rebuilds the source with no format code;
- `vzip_source/bytes`: the source's bytes in order, referenced in place
  (conventions §7 bytes), which the leaves' extents index;
- `vzip_source/tree/...`, a view: one group per struct that holds structs,
  whose attributes are its values as JSON. The view need not be lossless.

The table (n elements, m extents):

| array | type | holds |
|---|---|---|
| `ir/kind` | uint8 [n] | 0 struct, 1 value, 2 data, 3 derived, 4 alias, 5 gap |
| `ir/parent` | int64 [n] | the parent's id, −1 for the root |
| `ir/target` | int64 [n] | an alias's target, else −1 |
| `ir/extent_index` | int64 [n + 1] | element i's extents are rows [x[i], x[i+1]) of `ir/extents` |
| `ir/extents` | int64 [m, 2] | (offset, length) |
| `ir/name`, `ir/type` | families (offsets, data) | UTF-8 name and type |
| `ir/form`, `ir/forms` | int32 [n], family | an element's form (−1: none): the JSON of its geometry, codec, transform and recipe, the recipe relative to its first extent |
| `ir/shared` | family | the shared data sources recipes name |

The view's JSON (one vocabulary): values are JSON where JSON can hold them; anything
else is an object with the one tag `$vz`: `{"$vz": "int", "v": digits}`,
`{"$vz": "float", "v": "NaN"}`, `{"$vz": "bytes", "b64": ...}` (at most 64 bytes),
`{"$vz": "array", "path": p}` (a large value, as an array at `p`, relative to
`vzip_source`), `{"$vz": "alias", "path": p}`, `{"$vz": "group", "path": p}`, and the one
spill form `{"$vz": "element", "id": i}` (past a budget: read element i in the
table). The one escape rule: an object key that starts with `$` gets another `$`
in front; a path segment is percent-encoded outside `[A-Za-z0-9._~-]`.
"""

from __future__ import annotations

import base64
import json
import math
import struct
import zlib
import urllib.parse
import zipfile

import numpy as np

from vzip.ir.model import ALIAS, DATA, DERIVED, IR, KINDS, LEAVES, STRUCT, VALUE
from vzip.ir.types import parse as parse_type
from vzip.virtualize.common import (
    MAX_SAFE, SOURCE_NODE, Output, blob_chunks, declare, grid_chunks, metadata_array, metadata_group,
)

DOC = 1 << 16  # the most bytes of a view document
SMALL = 1 << 12  # a value of more JSON is an array
TABLE_CHUNK = 1 << 20  # chunks of the copied table arrays
MAX_GROUPS = 1 << 12  # groups of the view


def tag_json(v):
    """A decoded value as JSON, in the `$vz` vocabulary."""
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    if isinstance(v, int):
        return v if abs(v) <= MAX_SAFE else {"$vz": "int", "v": str(v)}
    if isinstance(v, float):
        return v if math.isfinite(v) else {"$vz": "float", "v": "NaN" if math.isnan(v) else (
            "Infinity" if v > 0 else "-Infinity")}
    if isinstance(v, (bytes, bytearray)):
        return {"$vz": "bytes", "b64": base64.b64encode(v).decode()}
    if isinstance(v, (list, tuple)):
        return [tag_json(x) for x in v]
    if isinstance(v, dict):
        return {escape_key(k): tag_json(x) for k, x in v.items()}
    raise TypeError(type(v))


def escape_key(k: str) -> str:
    return "$" + k if k.startswith("$") else k


def escape_path(p: str) -> str:
    return "/".join(urllib.parse.quote(s, safe="") or "%" for s in p.split("/")) if p else ""


def _size(v) -> int:
    return len(json.dumps(v, separators=(",", ":"), ensure_ascii=False).encode())


def _copied(out: Output, path: str, data_type: str, shape: list[int], item: int, data: bytes,
            dims: list[str]) -> None:
    """An array of values held in memory: copied, cut as contiguous values (conventions §7)."""
    chunk_shape, chunks = grid_chunks(0, shape, item, lambda o, n: data[o:o + n], limit=TABLE_CHUNK)
    resolved = {k: v if isinstance(v, bytes) else b"".join(
        data[r[0]:r[0] + r[1]] if isinstance(r, tuple) else r for r in v) for k, v in chunks.items()}
    metadata_array(out, f"{SOURCE_NODE}/{path}", data_type, shape, chunk_shape, dims, resolved)


def _family(out: Output, path: str, members: list[bytes]) -> None:
    starts = [0]
    for m in members:
        starts.append(starts[-1] + len(m))
    metadata_group(out, f"{SOURCE_NODE}/{path}")
    _copied(out, f"{path}/offsets", "int64", [len(starts)], 8, struct.pack(f"<{len(starts)}q", *starts), ["index"])
    data = b"".join(members)
    if data:
        _copied(out, f"{path}/data", "uint8", [len(data)], 1, data, ["byte"])


def form(ir: IR, i: int):
    """An element's geometry, codec, transform and recipe, its recipe relative to its first
    extent (an offset from its start; a length that is not positive counts from its end), so
    that the tiles of one image share one form."""
    info = ir.info.get(i)
    if info is None:
        return None
    ext = ir.extents(i)
    o, n = ext[0] if ext else (0, 0)
    rel = []
    for p in info.get("recipe", []):
        if p[0] == "src":
            rel.append(["src", p[1] - o, p[2] - n if p[1] + p[2] == o + n else p[2]])
        else:
            rel.append(list(p))
    return json.dumps(tag_json({**{k: v for k, v in info.items() if k != "recipe"}, "recipe": rel}),
                      separators=(",", ":")).encode()


def mirror(ir: IR, out: Output, profile: str) -> None:
    n = len(ir)
    node = {"ir": {"version": 0, "size": ir.size, "elements": n}}
    metadata_group(out, SOURCE_NODE, declare({}, profile, None, node))
    metadata_group(out, f"{SOURCE_NODE}/ir")
    for name, col, dt, item in (("kind", ir.kind, "uint8", 1), ("parent", ir.parent, "int64", 8),
                                ("target", ir.target, "int64", 8), ("extent_index", ir.ext_index, "int64", 8)):
        _copied(out, f"ir/{name}", dt, [len(col)], item, col.tobytes(), ["index"])
    m = len(ir.ext) // 2
    _copied(out, "ir/extents", "int64", [m, 2], 8, ir.ext.tobytes(), ["index", "part"])
    _family(out, "ir/name", [s.encode() for s in ir.name])
    _family(out, "ir/type", [(t or "").encode() for t in ir.type])
    forms: dict[bytes, int] = {}
    ids = [-1 if f is None else forms.setdefault(f, len(forms)) for f in (form(ir, i) for i in range(n))]
    _copied(out, "ir/form", "int32", [n], 4, struct.pack(f"<{n}i", *ids), ["index"])
    _family(out, "ir/forms", list(forms))
    if ir.shared:
        _family(out, "ir/shared", ir.shared)
    if ir.size:
        chunk, chunks = blob_chunks(0, ir.size)
        metadata_array(out, f"{SOURCE_NODE}/bytes", "uint8", [ir.size], [chunk], ["byte"], chunks)
    _tree(ir, out, profile)


def _record_like(ir: IR, i: int) -> bool:
    kids = ir.children(i)
    return len(kids) <= 64 and all(ir.kind[k] != STRUCT for k in kids)


def _tree(ir: IR, out: Output, profile: str) -> None:
    """The view: a group per struct that holds structs, within the budget."""
    groups: set[str] = set()
    todo = [1, 0]  # the groups queued so far, then the queue
    while len(todo) > 1:
        s = todo.pop(1)
        path = f"tree/{escape_path(ir.path(s))}".rstrip("/")
        left = [DOC]
        doc = _doc(ir, out, s, path, left, todo)
        cost = DOC - left[0]
        if cost > ir.budget.left("documents"):
            break  # the rest of the view is past the budget: the table has it
        ir.budget.charge("documents", cost)
        metadata_group(out, f"{SOURCE_NODE}/{path}", declare({}, profile, None, doc))
        groups.add(path)
    have = {k[len(SOURCE_NODE) + 1:-len("/zarr.json")] for k in out.bytes_entries if k.startswith(SOURCE_NODE + "/")}
    for p in sorted(have):
        parts = p.split("/")
        for k in range(1, len(parts)):
            q = "/".join(parts[:k])
            if q not in have:
                have.add(q)
                metadata_group(out, f"{SOURCE_NODE}/{q}")


def _doc(ir: IR, out: Output, s: int, path: str, left: list[int], todo: list[int]) -> dict:
    doc: dict = {}
    for c in ir.children(s):
        if left[0] < 1 << 10:  # full: the rest of the children are in the table
            doc["$vz"] = "partial"
            break
        k, name = ir.kind[c], ir.name[c]
        key = escape_key(name)
        if k == STRUCT:
            if _record_like(ir, c):
                v = _doc(ir, out, c, f"{path}/{escape_path(name)}", left, todo)
            elif todo[0] < MAX_GROUPS:
                todo[0] += 1
                todo.append(c)
                v = {"$vz": "group", "path": f"tree/{escape_path(ir.path(c))}"}
            else:
                v = {"$vz": "element", "id": c}
        elif k == ALIAS and ir.was.get(c) is None:
            v = {"$vz": "alias", "path": ir.path(ir.target[c])}
        elif k == VALUE or (k == ALIAS and ir.was.get(c) == VALUE):
            if k == ALIAS:  # the bytes another element claims: named, and kept only when small
                v = {"$vz": "alias", "path": ir.path(ir.target[c])}
                if _small(ir, c):
                    v["value"] = _value(ir, out, c, "")
            else:
                v = _value(ir, out, c, f"{path}/{escape_path(name)}")
        else:
            continue  # data, derived and gaps are in the table only
        size = _size(v) + _size(key) + 2
        if size > left[0]:
            v = {"$vz": "element", "id": c}
            size = _size(v) + _size(key) + 2
        left[0] -= size
        doc[key] = v
    return doc


def _value(ir: IR, out: Output, i: int, path: str):
    v = ir.values.get(i, _MISSING)
    ext = ir.extents(i)
    if _small(ir, i):
        j = tag_json(v)
        if _size(j) <= SMALL:
            return j
    if v is _MISSING or len(ext) != 1 or ir.budget.left("documents") < (1 << 12):
        return {"$vz": "element", "id": i}  # not decoded, or past the budget: the table holds it
    ir.budget.charge("documents", 1 << 10)  # what an array's documents cost
    t = parse_type(ir.type[i])
    o, n = ext[0]
    try:
        if not (t.kind == "num" and t.dims):
            raise ValueError
        shape, chunks = grid_chunks(o, list(t.dims), t.size // _count(t.dims), None)
        metadata_array(out, f"{SOURCE_NODE}/{path}", t.dtype, list(t.dims), shape,
                       ["value"] + ["part"] * (len(t.dims) - 1), chunks, endian=t.endian)
    except ValueError:  # not numbers, or an edge chunk that needs copying: the bytes
        chunk, chunks = blob_chunks(o, n)
        metadata_array(out, f"{SOURCE_NODE}/{path}", "uint8", [n], [chunk], ["byte"], chunks)
    return {"$vz": "array", "path": path}


def _small(ir: IR, i: int) -> bool:
    """A decoded value of at most 1 KiB (64 bytes of opaque bytes): JSON, when its JSON is small too."""
    v = ir.values.get(i, _MISSING)
    n = sum(m for _, m in ir.extents(i))
    return v is not _MISSING and n <= (64 if isinstance(v, bytes) else 1024)


def _count(dims) -> int:
    k = 1
    for d in dims:
        k *= d
    return k


_MISSING = object()


# ---- reading an archive back, with no format code

class Archive:
    """A vzip archive's entries, references resolved: `read_source(offset, length)`
    reads the archive's source 0 (the file the archive describes)."""

    def __init__(self, path: str, read_source) -> None:
        from vzip.pb import Concat, Range, decode_source_table

        self.z = zipfile.ZipFile(path)
        self.sources = decode_source_table(self.z.read("__vz__/sources"))
        self.read_source = read_source
        self.refs = {}
        for info in self.z.infolist():
            ex, p = info.extra, 0
            while p < len(ex):
                hid, k = struct.unpack_from("<HH", ex, p)
                if hid == 0x7A76:
                    self.refs[info.filename] = [Range.decode(ex[p + 4:p + 4 + k])]
                elif hid == 0x7A77:
                    self.refs[info.filename] = list(Concat.decode(ex[p + 4:p + 4 + k]).parts)
                p += 4 + k

    def get(self, key: str) -> bytes | None:
        if key in self.refs:
            out = bytearray()
            for r in self.refs[key]:
                if r.data is not None:
                    out += r.data
                elif r.source == 0:
                    out += self.read_source(r.offset, r.length)
                else:
                    out += self.sources[r.source].data[r.offset:r.offset + r.length]
            return bytes(out)
        try:
            return self.z.read(key)
        except KeyError:
            return None

    def json(self, key: str) -> dict:
        return json.loads(self.get(key))

    def array(self, path: str) -> np.ndarray:
        """A 1-D or 2-D array whose chunks are cut along its first axis."""
        meta = self.json(f"{path}/zarr.json")
        shape, chunk = meta["shape"], meta["chunk_grid"]["configuration"]["chunk_shape"]
        dt = np.dtype(meta["data_type"]).newbyteorder("<")
        flat = np.zeros(shape, dt)
        if 0 in shape:
            return flat
        deflated = any(c["name"] == "zlib" for c in meta["codecs"])
        for q in range(-(-shape[0] // chunk[0])):
            b = self.get(f"{path}/c/" + "/".join([str(q)] + ["0"] * (len(shape) - 1)))
            if b is None:
                continue
            if deflated:
                b = zlib.decompress(b)
            block = np.frombuffer(b, dt).reshape([chunk[0], *shape[1:]])
            m = min(chunk[0], shape[0] - q * chunk[0])
            flat[q * chunk[0]:q * chunk[0] + m] = block[:m]
        return flat

    def family(self, path: str) -> list[bytes]:
        starts = self.array(f"{path}/offsets")
        data = self.array(f"{path}/data").tobytes() if starts[-1] else b""
        return [data[starts[i]:starts[i + 1]] for i in range(len(starts) - 1)]


def load(archive: Archive) -> IR:
    """The IR's table, as the mirror stored it (no decoded values)."""
    from array import array

    base = f"{SOURCE_NODE}/ir"
    node = archive.json(f"{SOURCE_NODE}/zarr.json")["attributes"]
    profile = next(k for k in node["vzip_virtualized"])
    size = node["vzip_virtualized"][profile]["ir"]["size"]
    ir = IR.__new__(IR)
    ir.size = size
    ir.kind = array("B", archive.array(f"{base}/kind").astype("u1").tobytes())
    ir.parent = array("q", archive.array(f"{base}/parent").astype("<i8").tobytes())
    ir.target = array("q", archive.array(f"{base}/target").astype("<i8").tobytes())
    ir.ext_index = array("q", archive.array(f"{base}/extent_index").astype("<i8").tobytes())
    ir.ext = array("q", archive.array(f"{base}/extents").astype("<i8").tobytes())
    ir.name = [b.decode() for b in archive.family(f"{base}/name")]
    ir.type = [b.decode() or None for b in archive.family(f"{base}/type")]
    from vzip.ir.model import Values

    ir.info, ir.values, ir.was, ir.kids, ir.by_name = {}, Values(ir.type), {}, {}, {}
    ir.finished = True
    return ir


def rebuild_from_archive(path: str, read_source, write) -> int:
    """The source, from the archive alone: the IR's table says where each leaf is,
    and `vzip_source/bytes` holds the bytes. No format code is involved."""
    from vzip.ir.check import check, leaves_in_order

    a = Archive(path, read_source)
    ir = load(a)
    check(ir)
    meta = a.json(f"{SOURCE_NODE}/bytes/zarr.json") if ir.size else None
    chunk = meta["chunk_grid"]["configuration"]["chunk_shape"][0] if meta else 1
    cache: dict[int, bytes] = {}

    def at(o: int, n: int) -> bytes:
        out = bytearray()
        while n:
            q = o // chunk
            if q not in cache:
                cache.clear()
                cache[q] = a.get(f"{SOURCE_NODE}/bytes/c/{q}")
            take = min(n, (q + 1) * chunk - o)
            out += cache[q][o - q * chunk:o - q * chunk + take]
            o, n = o + take, n - take
        return bytes(out)

    total = 0
    for o, n in leaves_in_order(ir):
        write(at(o, n))
        total += n
    return total


__all__ = ["Archive", "load", "mirror", "rebuild_from_archive", "tag_json", "KINDS", "LEAVES", "DATA", "DERIVED"]
