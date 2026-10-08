"""The Zarr v2 profile (profiles/zarr2.md, §10)."""

from __future__ import annotations

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
)

MAX_SAFE = 2**53 - 1
_DTYPE = re.compile(r"^([<>|])([biuf])([1248])$")
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
        if is_number(v) and abs(v) <= FLOAT_MAX[b]:
            return v
    raise Rejected(f"fill_value {str(v)[:40]} is not a {dt}")


def compressor(c, b: int) -> dict | None:
    if c is None:
        return None
    if not isinstance(c, dict) or not isinstance(c.get("id"), str):
        raise Rejected("compressor is not null or an object with a string id")
    cid = c["id"]
    if cid == "zlib":
        return {"name": "zlib", "configuration": {"level": 1}}
    if cid == "gzip":
        return {"name": "gzip", "configuration": {"level": 1}}
    if cid == "zstd":
        return {"name": "zstd", "configuration": {"level": 0, "checksum": False}}
    if cid == "blosc":
        return blosc_codec(c, b, SHUFFLES)
    raise Rejected(f"compressor {cid!r} is not supported")


def array(path: str, z, attrs: dict) -> tuple[dict, str]:
    """An array's zarr.json and its dimension separator (conventions/zarr2/README.md §3)."""
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
    c = compressor(z.get("compressor"), b)
    if c is not None:
        codecs.append(c)
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
    }, sep


@dataclass
class Hierarchy:
    """A Zarr v2 hierarchy read by conventions/zarr2/README.md §2–§3: each array's zarr.json and
    separator, each explicit group's attributes, and the implicit groups."""

    arrays: dict[str, dict] = field(default_factory=dict)
    seps: dict[str, str] = field(default_factory=dict)
    groups: dict[str, dict] = field(default_factory=dict)
    implicit: set[str] = field(default_factory=set)


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

    def attributes(path: str) -> dict:
        key = join(path, ".zattrs")
        if key not in objects:
            return {}
        a = store.document(key)
        if not isinstance(a, dict):
            raise Rejected(f"{key} is not a JSON object")
        return a

    h = Hierarchy(implicit=implicit)
    for path in sorted(nodes):
        if nodes[path] == "array":
            h.arrays[path], h.seps[path] = array(path or "/", store.document(join(path, ".zarray")), attributes(path))
        else:
            g = store.document(join(path, ".zgroup"))
            if not isinstance(g, dict) or as_int(g.get("zarr_format")) != 2:
                raise Rejected(f"{join(path, '.zgroup')}: zarr_format is not 2")
            h.groups[path] = attributes(path)
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
                     ) -> tuple[StoreOutput, list[tuple[str, int]]]:
    """The output of a hierarchy: a zarr.json per node (explicit groups with the
    copied attributes `groups` gives, arrays as `arrays` gives, their
    `attributes` the copied ones) and an entry per nonempty chunk object, plus
    the whole objects `extra` (§1.4). Returns the output and every chunk
    object. The copied attributes go under `profile`'s convention, and the
    groups `omes` names get the member `ome` it gives (conventions §2)."""
    out = StoreOutput(store.url)
    for path in h.implicit:
        out.docs[doc_key(path)] = dict(GROUP_IMPLICIT, attributes={})
    for path, attrs in groups.items():
        out.docs[doc_key(path)] = {"zarr_format": 3, "node_type": "group", "attributes": attrs}
    for path, doc in arrays.items():
        out.docs[doc_key(path)] = doc
    out.declare(profile, omes)
    chunks = chunk_objects(store, h)
    out.chunks = sorted([(k, n) for k, n in chunks if n > 0] + [(k, n) for k, n in extra if n > 0])
    out.check_keys()
    return out, chunks


def virtualize_zarr2(store: Store) -> StoreOutput:
    h = read_hierarchy(store)
    out, chunks = hierarchy_output(store, h, h.groups, h.arrays, "zarr2")
    out.summary = {
        "groups": len(h.groups) + len(h.implicit), "arrays": len(h.arrays), "chunks": len(out.chunks),
        "emptyChunks": len(chunks) - len(out.chunks), "objects": len(store.objects),
        "listingRequests": store.requests,
    }
    return out
