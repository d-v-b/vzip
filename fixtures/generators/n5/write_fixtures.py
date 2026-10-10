"""Writes synthetic N5 containers to fixtures/n5/<name>/, covering the
rules of the N5 profile (spec/virtualize.md §9): every data type, each supported
compression (raw, gzip, zlib, zstd, blosc), truncated and padded edge
blocks, missing and empty blocks, explicit and implicit groups, a dataset at
the root, nodes inside datasets, COSEM and n5-viewer multiscales, and the
inputs the profile rejects (`n5_reject_*`, one per rejection rule).

Most datasets are one block (an edge block, truncated or padded, where the
block size is larger than the dimensions), so that each container is a
handful of objects; only the datasets that test the block grid have two or
three blocks. Each rejection is the smallest container that breaks its rule:
one attributes.json at the root, with no block.

Blocks are written by hand here (header, column-major big-endian elements,
compression), as Java N5 writes them; js/test/n5/verify.py reads them back
with its own block reader.

Usage: uv run python fixtures/generators/n5/write_fixtures.py
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


def pinned_gzip(data: bytes, level: int = 9) -> bytes:
    """gzip.compress with modification time 0 and the header's OS byte pinned to 255
    ("unknown"): zlib writes its build's OS code there (3 on Linux, 19 on macOS), so
    unpinned fixtures differ between platforms."""
    out = bytearray(gzip.compress(data, compresslevel=level, mtime=0))
    out[9] = 255
    return bytes(out)

OUT = Path(__file__).parents[2] / "n5"
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
        return pinned_gzip(data)
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
            legacy: bool = False, write_blocks: bool = True) -> None:
    """Writes `data` (indexed in N5 dimension order) as the dataset at `path`."""
    d = root / path if path else root
    attrs = {"dimensions": list(data.shape), "blockSize": block, "dataType": dtype or str(data.dtype)}
    if legacy:
        attrs["compressionType"] = compression["type"]
    else:
        attrs["compression"] = compression
    attrs.update(extra or {})
    write_json(d / "attributes.json", attrs)
    if not write_blocks:
        return
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

    # Compressions, data types, truncated (Java N5) and padded edge blocks. raw_u16 is two
    # blocks along its second dimension (a truncated edge block), gzip_i32_padded two along
    # its first (a padded edge block); the others are one block, truncated where the block
    # size is larger than the dimensions.
    d = store("n5_compressions", {"n5": "4.0.0", "note": "compressions"})
    dataset(d, "raw_u16", arr((10, 7), "u2"), [10, 4], RAW)
    dataset(d, "gzip_i32_padded", arr((9, 5, 3), "i4"), [5, 6, 3], GZIP, padded=True)
    dataset(d, "zlib_f64", arr((6, 6), "f8"), [6, 6], {"type": "gzip", "useZlib": True, "level": 6})
    dataset(d, "zstd_f32", arr((5, 9), "f4"), [6, 9], {"type": "zstd", "level": 3})
    dataset(d, "blosc_u8", arr((8, 6, 5), "u1"), [8, 6, 5],
            {"type": "blosc", "cname": "lz4", "clevel": 5, "shuffle": 1, "blocksize": 0, "nthreads": 1})
    dataset(d, "blosc_i16_bitshuffle", arr((7, 7), "i2"), [8, 8],
            {"type": "blosc", "cname": "zstd", "clevel": 3, "shuffle": 2, "blocksize": 0})
    dataset(d, "blosc_u64_noshuffle_noblocksize", arr((5, 3), "u8"), [5, 3],
            {"type": "blosc", "cname": "zlib", "clevel": 1, "shuffle": 0})
    dataset(d, "legacy_gzip_i8", arr((6, 4), "i1"), [6, 4], GZIP, legacy=True)

    d = store("n5_dtypes", {"n5": "2.5.0"})
    for dt in ("u1", "i1", "u2", "i2", "u4", "i4", "u8", "i8", "f4", "f8"):
        name = {"u": "uint", "i": "int", "f": "float"}[dt[0]] + str(8 * int(dt[1]))
        dataset(d, name, arr((5, 4), dt), [5, 4], RAW, dtype=name)

    # A dataset at the root, with user attributes kept.
    d = OUT / "n5_root_dataset"
    dataset(d, "", arr((11, 3, 2), "u2"), [11, 3, 2], GZIP, extra={"n5": "4.0.0", "resolution": [1.5, 2, 3],
                                                                   "name": "root"})

    # Implicit groups, a directory that is no node, missing and empty blocks,
    # objects under a dataset that are not chunk keys, and a node inside a dataset.
    # a/b/sparse is three blocks along its first dimension: present, missing, and empty.
    d = store("n5_hierarchy", {"n5": "4.0.0", "description": "groups"})
    dataset(d, "a/b/sparse", arr((8, 8), "u2"), [3, 8], RAW, skip={(1, 0)})
    put(d / "a/b/sparse/2/0", b"")  # an empty block: no entry
    put(d / "a/b/sparse/9/9", b"x")  # outside the grid
    put(d / "a/b/sparse/01/0", b"x")  # leading zero: not a chunk key
    write_json(d / "a/b/sparse/0/attributes.json", {"dimensions": [1], "blockSize": [1], "dataType": "uint8",
                                                    "compression": RAW})  # inside a dataset: not a node
    write_json(d / "c/attributes.json", {"kind": "explicit group", "n5": "x"})
    dataset(d, "c/d/e", arr((3,), "f4"), [4], RAW)
    put(d / "docs/README.txt", b"not part of the container\n")
    put(d / "c/notes.txt", b"a stray object in a group\n")
    write_json(d / "f g/attributes.json", {"spaces": "in a name"})  # percent-encoded URLs
    dataset(d, "f g/h é", arr((4, 2), "u1"), [4, 2], RAW)

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
    pyramid(d, "em/fibsem-uint8", 3, base, [12, 10, 8], GZIP,
            extra=lambda i: {"transform": datasets[i]["transform"],
                             "pixelResolution": {"dimensions": [4.0, 4.0, 5.24], "unit": "nm"}})

    # COSEM, transforms only on the arrays, order F, translate absent, units as names.
    d = store("n5_cosem_array_transforms", {"n5": "2.0.0"})
    write_json(d / "img/attributes.json", {"multiscales": [{"datasets": [{"path": "s0"}, {"path": "s1"}]}]})
    for i in range(2):
        t = {"axes": ["x", "y", "z"], "scale": [1.0 * 2**i, 1.0 * 2**i, 3.0 * 2**i], "units": ["micrometer"] * 3,
             "order": "F"}
        dataset(d, f"img/s{i}", base[:: 2**i, :: 2**i, :: 2**i], [12, 10, 8], RAW, extra={"transform": t})

    # n5-viewer: scales and pixelResolution on the group.
    d = store("n5_viewer_scales", {"n5": "2.1.0"})
    write_json(d / "setup0/timepoint0/attributes.json", {
        "scales": [[1, 1, 1], [2, 2, 1], [4, 4, 2]],
        "pixelResolution": {"dimensions": [0.25, 0.25, 1.5], "unit": "um"}})
    for i, f in enumerate([[1, 1, 1], [2, 2, 1], [4, 4, 2]]):
        dataset(d, f"setup0/timepoint0/s{i}", base[:: f[0], :: f[1], :: f[2]], [12, 10, 8], GZIP,
                extra={"downsamplingFactors": f} if i else None)

    # n5-viewer: no scales; downsamplingFactors on the levels, pixelResolution (an array) on s0.
    d = store("n5_viewer_downsampling", {"n5": "2.1.0"})
    write_json(d / "g/attributes.json", {"axes": ["x", "y"], "note": "2-D"})
    plane = arr((13, 9), "u2")
    for i in range(3):
        extra = {"downsamplingFactors": [2**i, 2**i]} if i else {"pixelResolution": [0.5, 0.75]}
        dataset(d, f"g/s{i}", plane[:: 2**i, :: 2**i], [13, 9], RAW, extra=extra)

    # Not recognized: malformed multiscales (the hierarchy stays plain), and a group with `ome`.
    d = store("n5_multiscales_unrecognized", {"n5": "2.0.0"})
    write_json(d / "bad_scale/attributes.json", {"multiscales": [{"datasets": [
        {"path": "s0", "transform": {"axes": ["z", "y", "x"], "scale": [0, 1, 1]}}]}]})
    dataset(d, "bad_scale/s0", base, [12, 10, 8], RAW)
    write_json(d / "bad_axes/attributes.json", {"scales": [[1, 1, 1]], "axes": ["x", "x", "y"]})
    dataset(d, "bad_axes/s0", base, [12, 10, 8], RAW)
    write_json(d / "has_ome/attributes.json", {"ome": {"version": "0.5", "custom": True}, "scales": [[1, 1, 1]]})
    dataset(d, "has_ome/s0", base, [12, 10, 8], RAW)
    write_json(d / "four_d/attributes.json", {"scales": [[1, 1, 1, 1]]})
    dataset(d, "four_d/s0", arr((3, 3, 3, 2), "u1"), [3, 3, 3, 2], RAW)
    write_json(d / "dotdot/attributes.json", {"multiscales": [{"datasets": [{"path": "../bad_scale/s0"}]}]})

    # A 4-D image whose axes put time first: valid for OME-NGFF.
    d = store("n5_viewer_time_first", {"n5": "2.0.0"})
    write_json(d / "t/attributes.json", {"scales": [[1, 1, 1, 1], [1, 2, 2, 2]], "axes": ["t", "z", "y", "x"],
                                         "pixelResolution": {"dimensions": [1, 2, 0.5, 0.5], "unit": "nm"}})
    vol = arr((2, 4, 6, 6), "f4")
    dataset(d, "t/s0", vol, [2, 4, 6, 6], {"type": "zstd"})
    dataset(d, "t/s1", vol[:, ::2, ::2, ::2], [2, 4, 6, 6], {"type": "zstd"})

    # Two recognized groups sharing levels with different axis names (spec/virtualize/n5.md §4): the
    # group whose path comes first (COSEM `a`, axes x, y, z) keeps its image; `a/b`
    # (n5-viewer, axes c, y, x) is left plain, since an array has one set of
    # dimension names.
    d = store("n5_shared_levels", {"n5": "2.0.0"})
    write_json(d / "a/attributes.json", {"multiscales": [{"datasets": [
        {"path": "b/s0", "transform": cosem_transform([4.0, 4.0, 4.0], [0.0, 0.0, 0.0])}]}]})
    write_json(d / "a/b/attributes.json", {"scales": [[1, 1, 1]], "axes": ["c", "y", "x"]})
    dataset(d, "a/b/s0", arr((3, 4, 5), "u1"), [3, 4, 5], RAW)

    # A varlength-mode block next to a default one: the store is accepted; reading that chunk fails.
    d = store("n5_edge_varlength_block", {"n5": "4.0.0"})
    dataset(d, "v", arr((4, 4), "u1"), [2, 4], RAW)
    put(d / "v/1/0", block_bytes(arr((2, 4), "u1"), RAW, mode=1))

    # Rejections, one per rule: a dataset at the root (or a root attributes.json that is not
    # valid), with no block.
    def reject(name: str, attrs, *, raw: bytes | None = None):
        doc = json.dumps({"n5": "4.0.0", **attrs}).encode() if raw is None else raw
        put(store(f"n5_reject_{name}") / "attributes.json", doc)

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
    dataset(d, "__vz__/x", arr((2, 2), "u1"), [2, 2], RAW, write_blocks=False)
    d = store("n5_reject_no_root_attributes")
    dataset(d, "x", arr((2, 2), "u1"), [2, 2], RAW, write_blocks=False)

    # Source metadata (spec/virtualize/n5.md §5): the attributes, and the layout members the
    # Zarr v3 metadata does not reproduce (the version `n5`, a compression's members its codec
    # does not carry, a compressionType next to a compression); the compression levels, which
    # the codecs carry; integers beyond 2^53 kept exact. Written after the stores above, so
    # that the random data of those does not change.
    d = store("n5_source_metadata", {"n5": "4.0.0", "id": 18446744073709551615, "neg": -9007199254740993})
    dataset(d, "gzip_9", arr((4,), "u1"), [4], {"type": "gzip", "level": 9})
    dataset(d, "gzip_no_level", arr((4,), "u1"), [4], {"type": "gzip"})
    dataset(d, "zlib_default", arr((4,), "u1"), [4], {"type": "gzip", "useZlib": True, "level": -1})
    dataset(d, "zstd_extra", arr((4,), "u1"), [4], {"type": "zstd", "level": -5, "nbWorkers": 2})
    dataset(d, "both", arr((4,), "u1"), [4], RAW, extra={"compressionType": "gzip", "n5": "4.0.0"})
    dataset(d, "layout_only", arr((4,), "u1"), [4], RAW)
    write_json(d / "version_only/attributes.json", {"n5": "4.0.0"})
    dataset(d, "version_only/x", arr((4,), "u1"), [4], RAW)

    # Objects that are neither node documents nor blocks (spec/virtualize/n5.md §6): each is
    # kept whole under vzip_source/objects/, a dataset nested in a dataset included; an empty
    # object is not.
    d = store("n5_objects", {"n5": "4.0.0"})
    dataset(d, "a", arr((4,), "u1"), [4], RAW)
    dataset(d, "a/labels", arr((4,), "u1"), [4], RAW, extra={"meaning": "kept as objects"})
    put(d / "README", b"about this container\n")
    put(d / "g/notes.txt", b"a stray object\n")
    write_json(d / "g/attributes.json", {"kind": "group"})
    put(d / "g/empty", b"")

    reject("gzip_level", {**ok, "compression": {"type": "gzip", "level": 10}})
    reject("zstd_level", {**ok, "compression": {"type": "zstd", "level": 2.5}})
    d = store("n5_reject_objects_collision", {"n5": "4.0.0"})  # a node where the other objects go
    write_json(d / "vzip_source/attributes.json", {"mine": True})
    put(d / "README", b"x")

    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"{sum(1 for _ in OUT.iterdir())} stores, {total} bytes")


def exact_objects() -> None:
    """Objects named zarr.json, escaped with `~` under vzip_source/objects/, and empty
    objects, whose keys are vzip_source's source metadata (spec/virtualize/n5.md §6)."""
    d = store("n5_node_names", {"n5": "4.0.0"})
    put(d / "zarr.json", b'{"zarr_format": 3, "node_type": "group"}')
    put(d / "g/zarr.json~", b"one tilde")
    dataset(d, "a", np.arange(4, dtype="uint16"), [4], RAW)
    put(d / "a/zarr.json", b'{"zarr_format": 3, "node_type": "group"}')
    put(d / "e", b"")




def empty_chunks() -> None:
    """A root dataset with an empty block object, whose key is listed in
    vzip_source/empty.json (spec/virtualize/n5.md §3.1, §6)."""
    d = store("n5_root_dataset_empty_block")
    dataset(d, "", np.arange(4, dtype="uint16"), [2], RAW, extra={"n5": "4.0.0"})
    put(d / "1", b"")


def root_objects() -> None:
    """A dataset at the root with another object beside its block (a README), which the
    root keeps without a vzip_source group (spec/virtualize/n5.md §6). Written byte for
    byte as the first, hand-made copy was: compact JSON with a newline, no RNG."""
    d = store("n5_root_dataset_objects")
    put(d / "attributes.json",
        b'{"dimensions":[4],"blockSize":[4],"dataType":"uint8","compression":{"type":"raw"}}\n')
    put(d / "0", block_bytes(np.array([1, 2, 3, 4], dtype="uint8"), RAW))
    put(d / "README", b"notes\n")


if __name__ == "__main__":
    main()
    exact_objects()
    empty_chunks()
    root_objects()
