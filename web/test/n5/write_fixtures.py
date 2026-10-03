"""Writes synthetic N5 containers to web/test/fixtures/n5/<name>/, covering the
rules of the N5 profile (profiles/n5.md §9): every data type, each supported
compression (raw, gzip, zlib, zstd, blosc), truncated and padded edge
blocks, missing and empty blocks, explicit and implicit groups, a dataset at
the root, nodes inside datasets, COSEM and n5-viewer multiscales, and the
inputs the profile rejects (`n5_reject_*`, one per rejection rule).

Blocks are written by hand here (header, column-major big-endian elements,
compression), as Java N5 writes them; web/test/n5/verify.py reads them back
with its own block reader.

Usage: uv run python web/test/n5/write_fixtures.py
"""

from __future__ import annotations

import gzip
import json
import shutil
import struct
import zlib
from pathlib import Path

import numcodecs
import numpy as np

OUT = Path(__file__).parents[1] / "fixtures" / "n5"
RNG = np.random.default_rng(5)


def write_json(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1))


def compress(data: bytes, compression: dict, itemsize: int) -> bytes:
    t = compression["type"]
    if t == "raw":
        return data
    if t == "gzip":
        if compression.get("useZlib"):
            return zlib.compress(data, 6)
        return gzip.compress(data, mtime=0)
    if t == "zstd":
        return numcodecs.Zstd(level=3).encode(data)
    if t == "blosc":
        return _blosc(data, compression, itemsize)
    raise ValueError(t)


def _blosc(data: bytes, c: dict, itemsize: int) -> bytes:
    codec = numcodecs.Blosc(cname=c["cname"], clevel=c["clevel"], shuffle=c["shuffle"],
                            blocksize=c.get("blocksize", 0))
    # numcodecs takes the type size from the array's dtype.
    return codec.encode(np.frombuffer(data, dtype=f"u{itemsize}" if itemsize > 1 else "u1"))


def block_bytes(block: np.ndarray, compression: dict, mode: int = 0) -> bytes:
    """An N5 block: header (mode, ndim, size per dimension), then the elements,
    first dimension fastest, big-endian, compressed."""
    header = struct.pack(f">HH{block.ndim}I", mode, block.ndim, *block.shape)
    if mode == 1:
        header += struct.pack(">I", block.size)
    body = np.asfortranarray(block).astype(block.dtype.newbyteorder(">")).tobytes(order="F")
    return header + compress(body, compression, block.dtype.itemsize)


def dataset(root: Path, path: str, data: np.ndarray, block: list[int], compression: dict, *,
            dtype: str | None = None, padded: bool = False, skip: set = frozenset(), extra: dict | None = None,
            legacy: bool = False) -> None:
    """Writes `data` (indexed in N5 dimension order) as the dataset at `path`."""
    d = root / path if path else root
    attrs = {"dimensions": list(data.shape), "blockSize": block, "dataType": dtype or str(data.dtype)}
    if legacy:
        attrs["compressionType"] = compression["type"]
    else:
        attrs["compression"] = compression
    attrs.update(extra or {})
    write_json(d / "attributes.json", attrs)
    grid = [-(-s // b) for s, b in zip(data.shape, block)]
    for idx in np.ndindex(*grid):
        if idx in skip:
            continue
        sl = tuple(slice(i * b, min((i + 1) * b, s)) for i, b, s in zip(idx, block, data.shape))
        part = data[sl]
        if padded and part.shape != tuple(block):
            full = np.zeros(block, dtype=data.dtype)
            full[tuple(slice(0, n) for n in part.shape)] = part
            part = full
        f = d.joinpath(*map(str, idx))
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(block_bytes(part, compression))


def put(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def arr(shape, dtype) -> np.ndarray:
    dt = np.dtype(dtype)
    if dt.kind == "f":
        return (RNG.standard_normal(shape) * 100).astype(dt)
    info = np.iinfo(dt)
    return RNG.integers(info.min, info.max, size=shape, endpoint=True, dtype=dt)


RAW = {"type": "raw"}
GZIP = {"type": "gzip", "level": -1, "useZlib": False}


def store(name: str, root_attrs: dict | None = None) -> Path:
    d = OUT / name
    d.mkdir(parents=True)
    if root_attrs is not None:
        write_json(d / "attributes.json", root_attrs)
    return d


def cosem_transform(scale, translate, order="C"):
    t = {"axes": ["z", "y", "x"], "scale": scale[::-1], "translate": translate[::-1], "units": ["nm"] * 3}
    if order != "C":
        t = {"axes": ["x", "y", "z"], "scale": scale, "translate": translate, "units": ["nm"] * 3, "order": "F"}
    return t


def pyramid(root: Path, group: str, levels: int, base: np.ndarray, block, compression, extra=None):
    """Levels s0..s(levels-1), each half the previous (by striding)."""
    for i in range(levels):
        f = 2**i
        dataset(root, f"{group}/s{i}" if group else f"s{i}", base[::f, ::f, ::f], block, compression,
                extra=(extra(i) if extra else None))


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    # Compressions, data types, truncated (Java N5) and padded edge blocks.
    d = store("n5_compressions", {"n5": "4.0.0", "note": "compressions"})
    dataset(d, "raw_u16", arr((10, 7), "u2"), [4, 3], RAW)
    dataset(d, "gzip_i32_padded", arr((9, 5, 3), "i4"), [4, 2, 2], GZIP, padded=True)
    dataset(d, "zlib_f64", arr((6, 6), "f8"), [4, 4], {"type": "gzip", "useZlib": True, "level": 6})
    dataset(d, "zstd_f32", arr((5, 9), "f4"), [3, 4], {"type": "zstd", "level": 3})
    dataset(d, "blosc_u8", arr((8, 6, 5), "u1"), [4, 4, 4],
            {"type": "blosc", "cname": "lz4", "clevel": 5, "shuffle": 1, "blocksize": 0, "nthreads": 1})
    dataset(d, "blosc_i16_bitshuffle", arr((7, 7), "i2"), [4, 4],
            {"type": "blosc", "cname": "zstd", "clevel": 3, "shuffle": 2, "blocksize": 0})
    dataset(d, "blosc_u64_noshuffle_noblocksize", arr((5, 3), "u8"), [2, 2],
            {"type": "blosc", "cname": "zlib", "clevel": 1, "shuffle": 0})
    dataset(d, "legacy_gzip_i8", arr((6, 4), "i1"), [3, 3], GZIP, legacy=True)

    d = store("n5_dtypes", {"n5": "2.5.0"})
    for dt in ("u1", "i1", "u2", "i2", "u4", "i4", "u8", "i8", "f4", "f8"):
        name = {"u": "uint", "i": "int", "f": "float"}[dt[0]] + str(8 * int(dt[1]))
        dataset(d, name, arr((5, 4), dt), [3, 3], RAW, dtype=name)

    # A dataset at the root, with user attributes kept.
    d = OUT / "n5_root_dataset"
    dataset(d, "", arr((11, 3, 2), "u2"), [5, 2, 2], GZIP, extra={"n5": "4.0.0", "resolution": [1.5, 2, 3],
                                                                   "name": "root"})

    # Implicit groups, a directory that is no node, missing and empty blocks,
    # objects under a dataset that are not chunk keys, and a node inside a dataset.
    d = store("n5_hierarchy", {"n5": "4.0.0", "description": "groups"})
    dataset(d, "a/b/sparse", arr((8, 8), "u2"), [3, 3], RAW, skip={(0, 1), (2, 2), (1, 0)})
    put(d / "a/b/sparse/2/1", b"")  # an empty block: no entry
    put(d / "a/b/sparse/9/9", b"x")  # outside the grid
    put(d / "a/b/sparse/01/1", b"x")  # leading zero: not a chunk key
    write_json(d / "a/b/sparse/0/attributes.json", {"dimensions": [1], "blockSize": [1], "dataType": "uint8",
                                                    "compression": RAW})  # inside a dataset: not a node
    write_json(d / "c/attributes.json", {"kind": "explicit group", "n5": "x"})
    dataset(d, "c/d/e", arr((3,), "f4"), [2], RAW)
    put(d / "docs/README.txt", b"not part of the container\n")
    put(d / "c/notes.txt", b"a stray object in a group\n")
    write_json(d / "f g/attributes.json", {"spaces": "in a name"})  # percent-encoded URLs
    dataset(d, "f g/h é", arr((4, 2), "u1"), [2, 2], RAW)

    # COSEM, transforms on the group's multiscales (as on OpenOrganelle).
    d = store("n5_cosem", {"n5": "2.0.0"})
    base = arr((12, 10, 8), "u1")
    datasets = [{"path": f"s{i}", "transform": cosem_transform([4.0 * 2**i, 4.0 * 2**i, 5.24 * 2**i],
                                                                [2.0 * (2**i - 1), 2.0 * (2**i - 1), 2.62 * (2**i - 1)])}
                for i in range(3)]
    write_json(d / "em/fibsem-uint8/attributes.json", {
        "axes": ["x", "y", "z"], "units": ["nm"] * 3, "n5": "2.0.0",
        "multiscales": [{"datasets": datasets, "name": "em/fibsem-uint8"}],
        "pixelResolution": {"dimensions": [4.0, 4.0, 5.24], "unit": "nm"},
        "scales": [[1, 1, 1], [2, 2, 2], [4, 4, 4]]})
    pyramid(d, "em/fibsem-uint8", 3, base, [4, 4, 4], GZIP,
            extra=lambda i: {"transform": datasets[i]["transform"],
                             "pixelResolution": {"dimensions": [4.0, 4.0, 5.24], "unit": "nm"}})

    # COSEM, transforms only on the arrays, order F, translate absent, units as names.
    d = store("n5_cosem_array_transforms", {"n5": "2.0.0"})
    write_json(d / "img/attributes.json", {"multiscales": [{"datasets": [{"path": "s0"}, {"path": "s1"}]}]})
    for i in range(2):
        t = {"axes": ["x", "y", "z"], "scale": [1.0 * 2**i, 1.0 * 2**i, 3.0 * 2**i], "units": ["micrometer"] * 3,
             "order": "F"}
        dataset(d, f"img/s{i}", base[:: 2**i, :: 2**i, :: 2**i], [5, 5, 5], RAW, extra={"transform": t})

    # n5-viewer: scales and pixelResolution on the group.
    d = store("n5_viewer_scales", {"n5": "2.1.0"})
    write_json(d / "setup0/timepoint0/attributes.json", {
        "scales": [[1, 1, 1], [2, 2, 1], [4, 4, 2]],
        "pixelResolution": {"dimensions": [0.25, 0.25, 1.5], "unit": "um"}})
    for i, f in enumerate([[1, 1, 1], [2, 2, 1], [4, 4, 2]]):
        dataset(d, f"setup0/timepoint0/s{i}", base[:: f[0], :: f[1], :: f[2]], [4, 4, 4], GZIP,
                extra={"downsamplingFactors": f} if i else None)

    # n5-viewer: no scales; downsamplingFactors on the levels, pixelResolution (an array) on s0.
    d = store("n5_viewer_downsampling", {"n5": "2.1.0"})
    write_json(d / "g/attributes.json", {"axes": ["x", "y"], "note": "2-D"})
    plane = arr((13, 9), "u2")
    for i in range(3):
        extra = {"downsamplingFactors": [2**i, 2**i]} if i else {"pixelResolution": [0.5, 0.75]}
        dataset(d, f"g/s{i}", plane[:: 2**i, :: 2**i], [4, 4], RAW, extra=extra)

    # Not recognized: malformed multiscales (the hierarchy stays plain), and a group with `ome`.
    d = store("n5_multiscales_unrecognized", {"n5": "2.0.0"})
    write_json(d / "bad_scale/attributes.json", {"multiscales": [{"datasets": [
        {"path": "s0", "transform": {"axes": ["z", "y", "x"], "scale": [0, 1, 1]}}]}]})
    dataset(d, "bad_scale/s0", base, [6, 6, 6], RAW)
    write_json(d / "bad_axes/attributes.json", {"scales": [[1, 1, 1]], "axes": ["x", "x", "y"]})
    dataset(d, "bad_axes/s0", base, [6, 6, 6], RAW)
    write_json(d / "has_ome/attributes.json", {"ome": {"version": "0.5", "custom": True}, "scales": [[1, 1, 1]]})
    dataset(d, "has_ome/s0", base, [6, 6, 6], RAW)
    write_json(d / "four_d/attributes.json", {"scales": [[1, 1, 1, 1]]})
    dataset(d, "four_d/s0", arr((3, 3, 3, 2), "u1"), [2, 2, 2, 2], RAW)
    write_json(d / "dotdot/attributes.json", {"multiscales": [{"datasets": [{"path": "../bad_scale/s0"}]}]})

    # A 4-D image whose axes put time first: valid for OME-NGFF.
    d = store("n5_viewer_time_first", {"n5": "2.0.0"})
    write_json(d / "t/attributes.json", {"scales": [[1, 1, 1, 1], [1, 2, 2, 2]], "axes": ["t", "z", "y", "x"],
                                         "pixelResolution": {"dimensions": [1, 2, 0.5, 0.5], "unit": "nm"}})
    vol = arr((2, 4, 6, 6), "f4")
    dataset(d, "t/s0", vol, [1, 4, 4, 4], {"type": "zstd"})
    dataset(d, "t/s1", vol[:, ::2, ::2, ::2], [1, 4, 4, 4], {"type": "zstd"})

    # A varlength-mode block: the store is accepted; reading that chunk fails.
    d = store("n5_edge_varlength_block", {"n5": "4.0.0"})
    dataset(d, "v", arr((4, 4), "u1"), [2, 2], RAW)
    put(d / "v/1/1", block_bytes(arr((2, 2), "u1"), RAW, mode=1))

    # Rejections, one per rule.
    def reject(name: str, attrs, *, root=True, raw: bytes | None = None):
        d = store(f"n5_reject_{name}", {"n5": "4.0.0"} if root else None)
        f = d / "x/attributes.json"
        f.parent.mkdir(parents=True)
        if raw is not None:
            f.write_bytes(raw)
        else:
            f.write_text(json.dumps(attrs))
        put(d / "x/0/0", block_bytes(np.zeros((2, 2), "u1"), RAW))

    ok = {"dimensions": [4, 4], "blockSize": [2, 2], "dataType": "uint8", "compression": RAW}
    reject("datatype_string", {**ok, "dataType": "string"})
    reject("datatype_object", {**ok, "dataType": "object"})
    reject("compression_lz4", {**ok, "compression": {"type": "lz4", "blockSize": 65536}})
    reject("compression_xz", {**ok, "compression": {"type": "xz", "preset": 6}})
    reject("compression_bzip2", {**ok, "compression": {"type": "bzip2", "blockSize": 9}})
    reject("compression_jpeg", {**ok, "compression": {"type": "jpeg", "quality": 90}})
    reject("compression_no_type", {**ok, "compression": {"level": 1}})
    reject("no_compression", {k: v for k, v in ok.items() if k != "compression"})
    reject("gzip_usezlib_string", {**ok, "compression": {"type": "gzip", "useZlib": "true"}})
    reject("blosc_cname", {**ok, "compression": {"type": "blosc", "cname": "lz5", "clevel": 5, "shuffle": 1}})
    reject("blosc_clevel", {**ok, "compression": {"type": "blosc", "cname": "lz4", "clevel": 10, "shuffle": 1}})
    reject("blosc_shuffle_missing", {**ok, "compression": {"type": "blosc", "cname": "lz4", "clevel": 5}})
    reject("dimensions_empty", {**ok, "dimensions": [], "blockSize": []})
    reject("dimensions_negative", {**ok, "dimensions": [4, -1]})
    reject("dimensions_fraction", {**ok, "dimensions": [4, 2.5]})
    reject("dimensions_huge", {**ok, "dimensions": [9007199254740992, 4]})
    reject("blocksize_mismatch", {**ok, "blockSize": [2]})
    reject("blocksize_zero", {**ok, "blockSize": [2, 0]})
    reject("blocksize_huge", {**ok, "blockSize": [2, 2**31]})
    reject("attributes_not_object", None, raw=b"[1, 2]")
    reject("json_nan", None, raw=b'{"dimensions": [4, 4], "blockSize": [2, 2], "dataType": "uint8", '
                                 b'"compression": {"type": "raw"}, "offset": NaN}')
    reject("json_bom", None, raw=b"\xef\xbb\xbf" + json.dumps(ok).encode())
    reject("json_not_utf8", None, raw=json.dumps({**ok, "name": "é"}, ensure_ascii=False).encode().replace(b"\xc3\xa9", b"\xe9"))
    reject("json_trailing_comma", None, raw=b'{"dimensions": [4, 4], "blockSize": [2, 2],}')
    reject("json_overflow", None, raw=json.dumps(ok).encode()[:-1] + b', "big": 1e400}')
    reject("json_too_deep", None, raw=json.dumps(ok).encode()[:-1] + b', "deep": ' + b"[" * 300 + b"]" * 300 + b"}")
    reject("json_empty_object", None, raw=b"")
    d = store("n5_reject_reserved_key", {"n5": "4.0.0"})
    dataset(d, "__vz__/x", arr((2, 2), "u1"), [2, 2], RAW)
    d = store("n5_reject_no_root_attributes")
    dataset(d, "x", arr((2, 2), "u1"), [2, 2], RAW)

    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"{sum(1 for _ in OUT.iterdir())} stores, {total} bytes")


if __name__ == "__main__":
    main()
