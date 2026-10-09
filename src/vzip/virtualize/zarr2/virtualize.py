"""The Zarr v2 profile (profiles/zarr2.md, §10)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from vzip.virtualize.common import Rejected
from vzip.virtualize.n5.virtualize import blosc_codec
from vzip.virtualize.store import (
    GROUP_IMPLICIT,
    Store,
    StoreOutput,
    as_int,
    canonical_index,
    classify,
    doc_key,
    find_chunks,
    grid,
    is_number,
    join,
    num,
    source_metadata,
)

MAX_SAFE = 2**53 - 1
_DTYPE = re.compile(r"^([<>|])([biuf])([1248])\Z")
TYPES = {("b", 1): "bool", ("i", 1): "int8", ("u", 1): "uint8", ("i", 2): "int16", ("u", 2): "uint16",
         ("f", 2): "float16", ("i", 4): "int32", ("u", 4): "uint32", ("f", 4): "float32",
         ("i", 8): "int64", ("u", 8): "uint64", ("f", 8): "float64"}
FLOAT_MAX = {2: 65504.0, 4: 3.4028234663852886e38, 8: float("inf")}
SHUFFLES = {0: "noshuffle", 1: "shuffle", 2: "bitshuffle", -1: "auto"}


def data_type(dtype) -> tuple[str, int, str]:
    """(Zarr data type, size b, byte order character) of a .zarray dtype (conventions/zarr2/README.md §3)."""
    m = _DTYPE.match(dtype) if isinstance(dtype, str) else None
    if m is None or (m.group(2), int(m.group(3))) not in TYPES:
        raise Rejected(f"dtype {str(dtype)[:60]} is not supported")
    order, kind, b = m.group(1), m.group(2), int(m.group(3))
    if b > 1 and order == "|":
        raise Rejected(f"dtype {dtype} has no byte order")
    return TYPES[(kind, b)], b, order


# The members of .zarray that §3 reads; the others are kept in M (conventions/zarr2/README.md §4).
ZARRAY_READ = ("zarr_format", "shape", "chunks", "dtype", "order", "compressor", "filters", "fill_value",
               "dimension_separator")
NEG_ZERO = {2: "0x8000", 4: "0x80000000", 8: "0x8000000000000000"}
# The levels a compressor may have (zlib's -1, its default, is level 6, which the Zarr v3
# codecs take), and the levels numcodecs uses when the member is absent.
LEVELS = {"zlib": (-1, 9, 1), "gzip": (-1, 9, 1), "zstd": (-131072, 22, 0)}


def fill_value(v, dt: str, b: int):
    if v is None:
        return False if dt == "bool" else 0
    if dt == "bool":
        if isinstance(v, bool):
            return v
    elif dt.startswith(("int", "uint")):
        bits = 8 * b
        lo, hi = (0, 2**bits - 1) if dt.startswith("u") else (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1)
        i = as_int(v, lo, hi)
        if i is not None:
            return i
    else:
        if v in ("NaN", "Infinity", "-Infinity"):
            return v
        if is_number(v) and abs(num(v)) <= FLOAT_MAX[b]:
            v = num(v)
            if isinstance(v, float) and v == 0 and math.copysign(1, v) < 0:
                return NEG_ZERO[b]  # -0.0, which JSON writers do not all keep
            return v
    raise Rejected(f"fill_value {str(v)[:40]} is not a {dt}")


def level(cid: str, c: dict) -> int:
    """The level of a zlib, gzip or zstd compressor (conventions/zarr2/README.md §3.1)."""
    lo, hi, default = LEVELS[cid]
    if "level" not in c:
        return default
    v = as_int(c["level"], lo, hi)
    if v is None:
        raise Rejected(f"{cid} level {str(c['level'])[:20]} is not an integer from {lo} to {hi}")
    return 6 if v == -1 and cid != "zstd" else v


def compressor(c, b: int) -> tuple[dict | None, dict]:
    """The compressor's codec, and its members the codec does not carry."""
    if c is None:
        return None, {}
    if not isinstance(c, dict) or not isinstance(c.get("id"), str):
        raise Rejected("compressor is not null or an object with a string id")
    cid = c["id"]
    if cid in ("zlib", "gzip"):
        codec, carried = {"name": cid, "configuration": {"level": level(cid, c)}}, ("id", "level")
    elif cid == "zstd":
        checksum = c.get("checksum", False)
        if not isinstance(checksum, bool):
            raise Rejected(f"zstd checksum {str(checksum)[:20]} is not a boolean")
        codec = {"name": "zstd", "configuration": {"level": level(cid, c), "checksum": checksum}}
        carried = ("id", "level", "checksum")
    elif cid == "blosc":
        codec, carried = blosc_codec(c, b, SHUFFLES), ("id", "cname", "clevel", "shuffle", "blocksize")
        if as_int(c.get("shuffle")) == -1:
            carried = tuple(k for k in carried if k != "shuffle")  # -1 (automatic) is resolved: keep it
    else:
        raise Rejected(f"compressor {cid!r} is not supported")
    return codec, {k: v for k, v in c.items() if k not in carried}


def array(path: str, z, attrs: dict) -> tuple[dict, str, dict]:
    """An array's zarr.json, its dimension separator, and the members of its
    .zarray that the zarr.json does not reproduce (conventions/zarr2/README.md §3, §4)."""
    if not isinstance(z, dict):
        raise Rejected(f"{path}/.zarray is not a JSON object")
    if as_int(z.get("zarr_format")) != 2:
        raise Rejected(f"{path}/.zarray: zarr_format is not 2")
    shape, chunks = z.get("shape"), z.get("chunks")
    if not isinstance(shape, list) or len(shape) > 32 or any(as_int(s, 0) is None for s in shape):
        raise Rejected(f"{path}: shape {str(shape)[:80]} is not up to 32 sizes")
    n = len(shape)
    if not isinstance(chunks, list) or len(chunks) != n or any(as_int(c, 1) is None for c in chunks):
        raise Rejected(f"{path}: chunks {str(chunks)[:80]} is not {n} chunk sizes")
    dt, b, byteorder = data_type(z.get("dtype"))
    order = z.get("order")
    if order not in ("C", "F"):
        raise Rejected(f"{path}: order {str(order)[:20]!r} is not C or F")
    filters = z.get("filters")
    if filters is not None and filters != []:
        raise Rejected(f"{path}: filters are not supported")
    sep = z.get("dimension_separator")
    if sep is None:
        sep = "."
    if sep not in (".", "/"):
        raise Rejected(f"{path}: dimension_separator {str(sep)[:20]!r} is not . or /")
    fill = fill_value(z.get("fill_value"), dt, b)
    codecs = []
    if order == "F" and n >= 2:
        codecs.append({"name": "transpose", "configuration": {"order": list(range(n - 1, -1, -1))}})
    codecs.append({"name": "bytes"} if b == 1 else
                  {"name": "bytes", "configuration": {"endian": "little" if byteorder == "<" else "big"}})
    c, extra = compressor(z.get("compressor"), b)
    if c is not None:
        codecs.append(c)
    meta = {k: v for k, v in z.items() if k not in ZARRAY_READ}
    fv = z.get("fill_value")
    if fv is None:
        meta["fill_value"] = None
    elif dt.startswith("float") and is_number(fv) and isinstance(fv, int) and not -MAX_SAFE <= fv <= MAX_SAFE:
        meta["fill_value"] = fv  # an integer literal beyond 2^53 - 1: F is its binary64 value, so keep the digits
    if extra:
        meta["compressor"] = extra
    return {
        "zarr_format": 3,
        "node_type": "array",
        "shape": [as_int(s) for s in shape],
        "data_type": dt,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [as_int(c) for c in chunks]}},
        "chunk_key_encoding": {"name": "v2", "configuration": {"separator": sep}},
        "fill_value": fill,
        "codecs": codecs,
        "attributes": attrs,
    }, sep, meta


@dataclass
class Hierarchy:
    """A Zarr v2 hierarchy read by conventions/zarr2/README.md §2–§3: each array's zarr.json and
    separator, each explicit group's attributes, the implicit groups, each node's
    metadata M (§4), and the node documents read."""

    arrays: dict[str, dict] = field(default_factory=dict)
    seps: dict[str, str] = field(default_factory=dict)
    groups: dict[str, dict] = field(default_factory=dict)
    implicit: set[str] = field(default_factory=set)
    meta: dict[str, dict] = field(default_factory=dict)
    documents: set[str] = field(default_factory=set)


def _nodes(objects: dict[str, int]) -> tuple[dict[str, str], set[str]]:
    candidates: dict[str, str] = {}
    for key in objects:
        head, _, name = key.rpartition("/")
        if name in (".zarray", ".zgroup"):
            kind = "array" if name == ".zarray" else "group"
            if candidates.get(head, kind) != kind:
                raise Rejected(f"{head or '/'} has both .zarray and .zgroup")
            candidates[head] = kind
    return classify(candidates)


def prefetch(store: Store) -> None:
    """Starts reading, concurrently, exactly the documents read_hierarchy reads
    and in its order: for each node in path order, its .zarray or .zgroup,
    then its .zattrs if listed."""
    try:
        nodes, _ = _nodes(store.objects)
    except Rejected:
        return  # read_hierarchy rejects before it reads a document
    keys = []
    for path in sorted(nodes):
        keys.append(join(path, ".zarray" if nodes[path] == "array" else ".zgroup"))
        if join(path, ".zattrs") in store.objects:
            keys.append(join(path, ".zattrs"))
    store.prefetch(keys)


def read_hierarchy(store: Store) -> Hierarchy:
    """The nodes of a Zarr v2 store (conventions/zarr2/README.md §2), each document read and checked."""
    objects = store.objects
    nodes, implicit = _nodes(objects)
    prefetch(store)

    h = Hierarchy(implicit=implicit)

    def attributes(path: str) -> dict:
        key = join(path, ".zattrs")
        if key not in objects:
            return {}
        h.documents.add(key)
        a = store.document(key)
        if not isinstance(a, dict):
            raise Rejected(f"{key} is not a JSON object")
        return a

    for path in sorted(nodes):
        if nodes[path] == "array":
            h.documents.add(join(path, ".zarray"))
            h.arrays[path], h.seps[path], h.meta[path] = array(
                path or "/", store.document(join(path, ".zarray")), attributes(path))
        else:
            h.documents.add(join(path, ".zgroup"))
            g = store.document(join(path, ".zgroup"))
            if not isinstance(g, dict) or as_int(g.get("zarr_format")) != 2:
                raise Rejected(f"{join(path, '.zgroup')}: zarr_format is not 2")
            h.groups[path] = attributes(path)
            h.meta[path] = {k: v for k, v in g.items() if k != "zarr_format"}
    return h


def chunk_objects(store: Store, h: Hierarchy) -> list[tuple[str, int]]:
    """Every chunk object of every array (conventions/zarr2/README.md §3), sizes 0 included, in key order."""
    tests = {}
    for path, a in h.arrays.items():
        g = grid(a["shape"], a["chunk_grid"]["configuration"]["chunk_shape"])
        sep = h.seps[path]

        def is_chunk(rest: str, g=g, sep=sep) -> bool:
            if not g:
                return rest == "0"
            if sep == "." and "/" in rest:
                return False
            parts = rest.split(sep)
            return len(parts) == len(g) and all(canonical_index(s, n) for s, n in zip(parts, g))

        tests[path] = is_chunk
    return find_chunks(store.objects, tests)


def hierarchy_output(store: Store, h: Hierarchy, groups: dict[str, dict], arrays: dict[str, dict], profile: str,
                     extra: list[tuple[str, int]] = (), omes: dict[str, dict] | None = None,
                     unversioned: dict[str, list[str]] | None = None,
                     ) -> tuple[StoreOutput, list[tuple[str, int]], int]:
    """The output of a hierarchy: a zarr.json per node (explicit groups with the
    attributes `groups` gives, arrays as `arrays` gives, their `attributes` the
    attributes A to keep), an entry per nonempty chunk object, the whole objects
    `extra` under their own keys, and every other object under
    `vzip_source/objects/` (§1.4, conventions/zarr2/README.md §5). Each node's
    source metadata is A and its metadata M, under `profile`'s convention, and
    the groups `omes` names get the member `ome` it gives (conventions §2), and the
    members `unversioned` gives (conventions/ome-zarr/README.md §8). Returns the output,
    every chunk object, and the number of other objects."""
    unversioned = unversioned or {}
    out = StoreOutput(store.url)
    for path in h.implicit:
        out.docs[doc_key(path)] = dict(GROUP_IMPLICIT, attributes={})
    for path, attrs in groups.items():
        out.docs[doc_key(path)] = {"zarr_format": 3, "node_type": "group",
                                   "attributes": source_metadata(attrs, h.meta[path], unversioned.get(path))}
    for path, doc in arrays.items():
        out.docs[doc_key(path)] = {**doc, "attributes": source_metadata(doc["attributes"], h.meta[path])}
    out.declare(profile, omes)
    chunks = chunk_objects(store, h)
    out.chunks = sorted([(k, n) for k, n in chunks if n > 0] + [(k, n) for k, n in extra if n > 0])
    # An empty object in `extra` is not in the hierarchy: its key is listed with the other empty ones.
    # An empty chunk object has no entry either: its key is listed with the empty ones (§5).
    used = h.documents | {k for k, n in chunks if n > 0} | {k for k, n in extra if n > 0}
    others = out.keep_objects(store.objects, used, [*h.arrays, *h.groups, *h.implicit], store.ignored)
    out.check_keys()
    return out, chunks, others


def virtualize_zarr2(store: Store) -> StoreOutput:
    h = read_hierarchy(store)
    out, chunks, others = hierarchy_output(store, h, h.groups, h.arrays, "zarr2")
    nonempty = sum(1 for _, n in chunks if n > 0)
    out.summary = {
        "groups": len(h.groups) + len(h.implicit), "arrays": len(h.arrays), "chunks": nonempty,
        "emptyChunks": len(chunks) - nonempty, "objects": len(store.objects), "otherObjects": others,
        "listingRequests": store.requests,
    }
    return out
