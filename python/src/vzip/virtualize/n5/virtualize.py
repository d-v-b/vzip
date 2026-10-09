"""The N5 profile (spec/virtualize/n5/profile.md, §9)."""

from __future__ import annotations

from vzip.virtualize.common import UNITS, Rejected
from vzip.virtualize.store import (
    GROUP_IMPLICIT,
    PathTrie,
    Store,
    StoreOutput,
    as_int,
    canonical_index,
    classify,
    depth,
    doc_key,
    find_chunks,
    grid,
    is_number,
    join,
    num,
    parent_of,
    source_metadata,
)

DATA_TYPES = {"uint8": 1, "int8": 1, "uint16": 2, "int16": 2, "uint32": 4, "int32": 4, "float32": 4,
              "uint64": 8, "int64": 8, "float64": 8}
BLOSC_NAMES = ("blosclz", "lz4", "lz4hc", "snappy", "zlib", "zstd")
SHUFFLES = {0: "noshuffle", 1: "shuffle", 2: "bitshuffle"}
UNIT_NAMES = set(UNITS.values())
MAX_BLOCK = 2**31 - 1
# The layout members of a dataset's attributes.json: the Zarr array metadata holds them.
LAYOUT_KEYS = ("dimensions", "blockSize", "dataType", "compression", "compressionType", "n5")


def blosc_codec(c: dict, typesize: int, shuffles: dict) -> dict:
    """The Zarr v3 blosc codec of an n5-blosc or numcodecs Blosc configuration (spec/virtualize/n5.md §3, spec/virtualize/zarr2.md §3.1)."""
    cname, clevel, shuffle = c.get("cname"), as_int(c.get("clevel"), 0, 9), as_int(c.get("shuffle"))
    blocksize = as_int(c.get("blocksize", 0), 0, MAX_BLOCK)
    if cname not in BLOSC_NAMES:
        raise Rejected(f"blosc cname {cname!r} is not supported")
    if clevel is None:
        raise Rejected(f"blosc clevel {c.get('clevel')!r} is not an integer from 0 to 9")
    if shuffle not in shuffles:
        raise Rejected(f"blosc shuffle {c.get('shuffle')!r} is not supported")
    if blocksize is None:
        raise Rejected(f"blosc blocksize {c.get('blocksize')!r} is not supported")
    s = shuffles[shuffle]
    if s == "auto":
        s = "bitshuffle" if typesize == 1 else "shuffle"
    return {"name": "blosc", "configuration": {"cname": cname, "clevel": clevel, "shuffle": s,
                                               "typesize": typesize, "blocksize": blocksize}}


# The levels a compression may have (Java's Deflater level -1, its default, is zlib's level 6),
# and the levels Java N5 uses when the member is absent.
LEVELS = {"gzip": (-1, 9, 6), "zstd": (-131072, 22, 3)}


def level(t: str, compression: dict) -> int:
    """The level of a gzip or zstd compression (spec/virtualize/n5.md §3)."""
    lo, hi, default = LEVELS[t]
    if "level" not in compression:
        return default
    v = as_int(compression["level"], lo, hi)
    if v is None:
        raise Rejected(f"{t} level {str(compression['level'])[:20]} is not an integer from {lo} to {hi}")
    return 6 if v == -1 and t == "gzip" else v


def compressor(compression: dict, b: int) -> tuple[dict | None, dict]:
    """The compression's codec, and its members the codec does not carry."""
    t = compression["type"]
    if t == "raw":
        codec, carried = None, ("type",)
    elif t == "gzip":
        z = compression.get("useZlib", False)
        if not isinstance(z, bool):
            raise Rejected(f"gzip useZlib {z!r} is not a boolean")
        codec = {"name": "zlib" if z else "gzip", "configuration": {"level": level(t, compression)}}
        carried = ("type", "useZlib", "level")
    elif t == "zstd":
        codec = {"name": "zstd", "configuration": {"level": level(t, compression), "checksum": False}}
        carried = ("type", "level")
    elif t == "blosc":
        codec, carried = blosc_codec(compression, b, SHUFFLES), ("type", "cname", "clevel", "shuffle", "blocksize")
    else:
        raise Rejected(f"N5 compression {t!r} is not supported")
    return codec, {k: v for k, v in compression.items() if k not in carried}


def dataset(path: str, doc: dict) -> dict:
    """A dataset's zarr.json (spec/virtualize/n5.md §3)."""
    dims, block, dtype = doc.get("dimensions"), doc.get("blockSize"), doc.get("dataType")
    if not isinstance(dims, list) or not 1 <= len(dims) <= 32 or any(as_int(d, 0) is None for d in dims):
        raise Rejected(f"{path}: dimensions {str(dims)[:80]} is not 1 to 32 sizes")
    n = len(dims)
    if not isinstance(block, list) or len(block) != n or any(as_int(x, 1, MAX_BLOCK) is None for x in block):
        raise Rejected(f"{path}: blockSize {str(block)[:80]} is not {n} block sizes")
    if not isinstance(dtype, str) or dtype not in DATA_TYPES:
        raise Rejected(f"{path}: dataType {str(dtype)[:40]!r} is not supported")
    if "compression" in doc:
        compression = doc["compression"]
        if not isinstance(compression, dict) or not isinstance(compression.get("type"), str):
            raise Rejected(f"{path}: compression is not an object with a string type")
    elif isinstance(doc.get("compressionType"), str):
        compression = {"type": doc["compressionType"]}
    else:
        raise Rejected(f"{path}: no compression")
    b = DATA_TYPES[dtype]
    inner = [{"name": "transpose", "configuration": {"order": list(range(n - 1, -1, -1))}},
             {"name": "bytes", "configuration": {"endian": "big"}} if b > 1 else {"name": "bytes"}]
    c, extra = compressor(compression, b)
    if c is not None:
        inner.append(c)
    meta = {}
    if "n5" in doc:
        meta["n5"] = doc["n5"]
    if extra:
        meta["compression"] = extra
    if "compression" in doc and "compressionType" in doc:
        meta["compressionType"] = doc["compressionType"]  # not read: `compression` is
    return {
        "zarr_format": 3,
        "node_type": "array",
        "shape": [as_int(d) for d in dims],
        "data_type": dtype,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [as_int(x) for x in block]}},
        "chunk_key_encoding": {"name": "v2", "configuration": {"separator": "/"}},
        "fill_value": 0,
        "codecs": [{"name": "n5_default", "configuration": {"codecs": inner}}],
        # The source metadata (spec/virtualize/n5.md §5): the document without its layout
        # members, and what of those the zarr.json does not reproduce.
        "attributes": source_metadata({k: v for k, v in doc.items() if k not in LAYOUT_KEYS}, meta),
    }


# ---------------------------------------------------------------- multiscales (spec/virtualize/n5.md §4)

AXIS_TYPES = {"x": "space", "y": "space", "z": "space", "t": "time", "c": "channel"}


def level_path(p) -> bool:
    return isinstance(p, str) and p != "" and all(s not in ("", ".", "..") for s in p.split("/"))


def valid_axes(names: list[str]) -> bool:
    if any(not a for a in names) or len(set(names)) != len(names):
        return False
    kinds = [AXIS_TYPES.get(a, "custom") for a in names]
    i = 0
    if i < len(kinds) and kinds[i] == "time":
        i += 1
    if i < len(kinds) and kinds[i] in ("channel", "custom"):
        i += 1
    rest = kinds[i:]
    return 2 <= len(rest) <= 3 and all(k == "space" for k in rest)


def unit_of(u) -> str | None:
    if not isinstance(u, str):
        return None
    return u if u in UNIT_NAMES else UNITS.get(u)


def ome_multiscale(name, axes: list[str], units: list, paths: list[str], scales, translations) -> dict:
    ms = {}
    if name is not None:
        ms["name"] = name
    ms["axes"] = []
    for a, u in zip(axes, units):
        ax = {"name": a}
        if a in AXIS_TYPES:
            ax["type"] = AXIS_TYPES[a]
        if u is not None:
            ax["unit"] = u
        ms["axes"].append(ax)
    ms["datasets"] = []
    for i, p in enumerate(paths):
        t = [{"type": "scale", "scale": scales[i]}]
        if translations is not None:
            t.append({"type": "translation", "translation": translations[i]})
        ms["datasets"].append({"path": p, "coordinateTransformations": t})
    return {"version": "0.5", "multiscales": [ms]}


def numbers(v, n: int, positive: bool = False) -> list | None:
    if not isinstance(v, list) or len(v) != n or not all(is_number(x) for x in v):
        return None
    v = [num(x) for x in v]
    if positive and not all(x > 0 for x in v):
        return None
    return v


def strings(v, n: int) -> bool:
    return isinstance(v, list) and len(v) == n and all(isinstance(x, str) for x in v)


def cosem(g: str, attrs: dict, ndim: dict[str, int], docs: dict[str, dict]):
    ms = attrs.get("multiscales")
    if not isinstance(ms, list) or not ms or not isinstance(ms[0], dict):
        return None
    m = ms[0]
    ds = m.get("datasets")
    if not isinstance(ds, list) or not ds or not all(isinstance(d, dict) for d in ds):
        return None
    levels = []
    for d in ds:
        p = d.get("path")
        if not level_path(p) or join(g, p) not in ndim:
            return None
        n = ndim[join(g, p)]
        t = d["transform"] if "transform" in d else docs[join(g, p)].get("transform")
        if not isinstance(t, dict):
            return None
        axes, scale = t.get("axes"), numbers(t.get("scale"), n, True)
        translate = numbers(t.get("translate"), n) if "translate" in t else [0] * n
        units = t["units"] if "units" in t else [None] * n
        order = t.get("order", "C")
        if not strings(axes, n) or scale is None or translate is None or order not in ("C", "F"):
            return None
        if "units" in t and not strings(units, n):
            return None
        if order == "C":
            axes, scale, translate, units = axes[::-1], scale[::-1], translate[::-1], units[::-1]
        levels.append((p, axes, scale, translate, units))
    if any(lv[1] != levels[0][1] or lv[4] != levels[0][4] for lv in levels):
        return None
    axes = levels[0][1]
    if not valid_axes(axes):
        return None
    name = m.get("name") if isinstance(m.get("name"), str) else None
    return ome_multiscale(name, axes, [unit_of(u) for u in levels[0][4]], [lv[0] for lv in levels],
                          [lv[2] for lv in levels], [lv[3] for lv in levels]), [join(g, lv[0]) for lv in levels], axes


def n5_viewer(g: str, attrs: dict, ndim: dict[str, int], docs: dict[str, dict]):
    s0 = join(g, "s0")
    if s0 not in ndim:
        return None
    n = ndim[s0]
    if "scales" in attrs:
        scales = attrs["scales"]
        if not isinstance(scales, list) or not scales:
            return None
        factors = [numbers(f, n, True) for f in scales]
        if any(f is None for f in factors):
            return None
        levels = [join(g, f"s{i}") for i in range(len(factors))]
        if any(ndim.get(lv) != n for lv in levels):
            return None
    else:
        if join(g, "s1") not in ndim:
            return None
        levels = []
        while join(g, f"s{len(levels)}") in ndim:
            levels.append(join(g, f"s{len(levels)}"))
        factors = []
        for i, lv in enumerate(levels):
            if ndim[lv] != n:
                return None
            f = docs[lv].get("downsamplingFactors")
            if f is None and i == 0 and "downsamplingFactors" not in docs[lv]:
                factors.append([1] * n)
                continue
            f = numbers(f, n, True)
            if f is None:
                return None
            factors.append(f)
    res_doc = attrs if "pixelResolution" in attrs else docs[s0] if "pixelResolution" in docs[s0] else None
    unit = None
    if res_doc is None:
        r = [1] * n
    else:
        pr = res_doc["pixelResolution"]
        if isinstance(pr, dict):
            r = numbers(pr.get("dimensions"), n, True)
            if "unit" in pr:
                if not isinstance(pr["unit"], str):
                    return None
                unit = pr["unit"]
        else:
            r = numbers(pr, n, True)
        if r is None:
            return None
    if "axes" in attrs:
        axes = attrs["axes"]
        if not strings(axes, n):
            return None
    elif n in (2, 3):
        axes = ["x", "y", "z"][:n]
    else:
        return None
    if not valid_axes(axes):
        return None
    u = unit_of(unit)
    units = [u if AXIS_TYPES.get(a) == "space" else None for a in axes]
    scales = [[float(r[j]) * float(f[j]) for j in range(n)] for f in factors]
    for s in scales:
        if not all(abs(v) != float("inf") for v in s):
            raise Rejected("a scale is not finite")
    # A downsampled voxel's center is (f - 1) / 2 source voxels from the origin.
    translations = [[(float(f[j]) - 1) / 2 * float(r[j]) for j in range(n)] for f in factors]
    paths = [lv[len(g) + 1:] if g else lv for lv in levels]
    return ome_multiscale(None, axes, units, paths, scales, translations), levels, axes


# ---------------------------------------------------------------- the profile


def virtualize_n5(store: Store) -> StoreOutput:
    objects = store.objects
    candidates = {}
    for key in objects:
        if key == "attributes.json":
            candidates[""] = "candidate"
        elif key.endswith("/attributes.json"):
            candidates[key[: -len("/attributes.json")]] = "candidate"
    # spec/virtualize/n5.md §2: classify from the root down; a candidate's kind needs its document.
    docs: dict[str, dict] = {}
    kinds: dict[str, str] = {}
    datasets: set[str] = set()
    trie = PathTrie()  # the datasets, for a lookup linear in the path's length

    def wanted(key: str) -> bool | None:
        # Read ahead exactly the documents the loop below reads: a candidate's,
        # once every candidate above it has been read and none is a dataset.
        p, known = parent_of(key), True  # the candidate
        while p:
            p = parent_of(p)
            if p in candidates:
                d = docs.get(p)
                if isinstance(d, dict) and "dimensions" in d:
                    return False
                known = known and d is not None
        return True if known else None

    order = sorted(candidates, key=lambda p: (depth(p), p))
    store.prefetch([join(p, "attributes.json") for p in order], wanted)
    for path in order:
        if trie.nearest(path.split("/")) is not None:
            continue
        doc = store.document(join(path, "attributes.json"))
        if not isinstance(doc, dict):
            raise Rejected(f"{join(path, 'attributes.json')} is not a JSON object")
        docs[path] = doc
        kinds[path] = "array" if "dimensions" in doc else "group"
        if kinds[path] == "array":
            datasets.add(path)
            trie.add(path, True)
    nodes, implicit = classify(kinds)

    out = StoreOutput(store.url)
    arrays: dict[str, dict] = {}
    for path in sorted(datasets):
        arrays[path] = dataset(path or "/", docs[path])
    ndim = {p: len(a["shape"]) for p, a in arrays.items()}

    groups = {}
    for path in nodes:
        if kinds[path] == "group":
            doc = docs[path]
            groups[path] = {"zarr_format": 3, "node_type": "group", "attributes": source_metadata(
                {k: v for k, v in doc.items() if k != "n5"}, {"n5": doc["n5"]} if "n5" in doc else {})}
    images = []
    omes: dict[str, dict] = {}
    named: dict[str, list[str]] = {}
    for g in sorted(groups):
        attrs = docs[g]
        if "ome" in attrs:
            continue
        found = cosem(g, attrs, ndim, docs)
        convention = "cosem"
        if found is None:
            found, convention = n5_viewer(g, attrs, ndim, docs), "n5-viewer"
        if found is None:
            continue
        ome, levels, axes = found
        if any(named.get(lv, axes) != list(axes) for lv in levels):
            continue  # a level of an earlier image with other axis names (spec/virtualize/n5.md §4)
        omes[g] = ome
        images.append({"path": g, "convention": convention})
        for lv in levels:
            named.setdefault(lv, list(axes))
    for path, names in named.items():
        doc = arrays[path]
        arrays[path] = {**{k: v for k, v in doc.items() if k != "attributes"}, "dimension_names": names,
                        "attributes": doc["attributes"]}

    for path in implicit:
        out.docs[doc_key(path)] = dict(GROUP_IMPLICIT, attributes={})
    for path, doc in groups.items():
        out.docs[doc_key(path)] = doc
    for path, doc in arrays.items():
        out.docs[doc_key(path)] = doc
    out.declare("n5", omes)

    tests = {}
    for path, a in arrays.items():
        g = grid(a["shape"], a["chunk_grid"]["configuration"]["chunk_shape"])

        def is_chunk(rest: str, g=g) -> bool:
            parts = rest.split("/")
            return len(parts) == len(g) and all(canonical_index(s, n) for s, n in zip(parts, g))

        tests[path] = is_chunk
    chunks = find_chunks(objects, tests)
    out.chunks = [(k, n) for k, n in chunks if n > 0]
    # An empty chunk object has no entry: its key is listed with the empty objects (§6).
    used = {join(p, "attributes.json") for p in nodes} | {k for k, n in chunks if n > 0}
    others = out.keep_objects(objects, used, [*nodes, *implicit], store.ignored)
    out.check_keys()
    nonempty = sum(1 for _, n in chunks if n > 0)
    out.summary = {
        "groups": len(groups) + len(implicit), "arrays": len(arrays), "chunks": nonempty,
        "emptyChunks": len(chunks) - nonempty, "objects": len(objects), "otherObjects": others, "images": images,
        "listingRequests": store.requests,
    }
    return out
