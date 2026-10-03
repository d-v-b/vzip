"""The Zarr v2 profile (profiles/zarr2.md, §10)."""

from __future__ import annotations

import re

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
    """(Zarr data type, size b, byte order character) of a .zarray dtype (§10.2)."""
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
    """An array's zarr.json and its dimension separator (§10.2)."""
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


def level_path(p) -> bool:
    return isinstance(p, str) and p != "" and all(s not in ("", ".", "..") for s in p.split("/"))


def ome_04(attrs: dict) -> dict | None:
    """The group attributes with 0.4 multiscales moved into `ome` (§10.4), or None."""
    ms = attrs.get("multiscales")
    if "ome" in attrs or not isinstance(ms, list) or not ms:
        return None
    if not all(isinstance(m, dict) and m.get("version") == "0.4" for m in ms):
        return None
    ome = {"version": "0.5", "multiscales": [{k: v for k, v in m.items() if k != "version"} for m in ms]}
    if "omero" in attrs:
        ome["omero"] = attrs["omero"]
    out = {k: v for k, v in attrs.items() if k not in ("multiscales", "omero")}
    out["ome"] = ome
    return out


def virtualize_zarr2(store: Store) -> StoreOutput:
    objects = store.objects
    candidates: dict[str, str] = {}
    for key in objects:
        head, _, name = key.rpartition("/")
        if name in (".zarray", ".zgroup"):
            kind = "array" if name == ".zarray" else "group"
            if candidates.get(head, kind) != kind:
                raise Rejected(f"{head or '/'} has both .zarray and .zgroup")
            candidates[head] = kind
    nodes, implicit = classify(candidates)

    def attributes(path: str) -> dict:
        key = join(path, ".zattrs")
        if key not in objects:
            return {}
        a = store.document(key)
        if not isinstance(a, dict):
            raise Rejected(f"{key} is not a JSON object")
        return a

    out = StoreOutput(store.url)
    arrays: dict[str, dict] = {}
    seps: dict[str, str] = {}
    groups: dict[str, dict] = {}
    for path in sorted(nodes):
        if nodes[path] == "array":
            arrays[path], seps[path] = array(path or "/", store.document(join(path, ".zarray")), attributes(path))
        else:
            g = store.document(join(path, ".zgroup"))
            if not isinstance(g, dict) or as_int(g.get("zarr_format")) != 2:
                raise Rejected(f"{join(path, '.zgroup')}: zarr_format is not 2")
            groups[path] = {"zarr_format": 3, "node_type": "group", "attributes": attributes(path)}

    images = []
    named: dict[str, list[str]] = {}
    for path in sorted(groups):
        converted = ome_04(groups[path]["attributes"])
        if converted is None:
            continue
        groups[path]["attributes"] = converted
        images.append(path)
        for m in converted["ome"]["multiscales"]:
            axes = m.get("axes")
            if not isinstance(axes, list) or not all(isinstance(a, dict) and isinstance(a.get("name"), str)
                                                     for a in axes):
                continue
            names = [a["name"] for a in axes]
            if len(set(names)) != len(names):
                continue
            datasets = m.get("datasets")
            for d in datasets if isinstance(datasets, list) else []:
                if not isinstance(d, dict) or not level_path(d.get("path")):
                    continue
                target = join(path, d["path"])
                if target in arrays and len(arrays[target]["shape"]) == len(names):
                    named.setdefault(target, names)
    for target, names in named.items():
        doc = arrays[target]
        arrays[target] = {**{k: v for k, v in doc.items() if k != "attributes"}, "dimension_names": names,
                          "attributes": doc["attributes"]}

    for path in implicit:
        out.docs[doc_key(path)] = dict(GROUP_IMPLICIT, attributes={})
    for path, doc in groups.items():
        out.docs[doc_key(path)] = doc
    for path, doc in arrays.items():
        out.docs[doc_key(path)] = doc

    tests = {}
    for path, a in arrays.items():
        g = grid(a["shape"], a["chunk_grid"]["configuration"]["chunk_shape"])
        sep = seps[path]

        def is_chunk(rest: str, g=g, sep=sep) -> bool:
            if not g:
                return rest == "0"
            if sep == "." and "/" in rest:
                return False
            parts = rest.split(sep)
            return len(parts) == len(g) and all(canonical_index(s, n) for s, n in zip(parts, g))

        tests[path] = is_chunk
    chunks = find_chunks(objects, tests)
    out.chunks = [(k, n) for k, n in chunks if n > 0]
    out.check_keys()
    out.summary = {
        "groups": len(groups) + len(implicit), "arrays": len(arrays), "chunks": len(out.chunks),
        "emptyChunks": len(chunks) - len(out.chunks), "objects": len(objects), "images": images,
        "listingRequests": store.requests,
    }
    return out
