"""Writes synthetic Zarr v2 hierarchies to web/test/fixtures/zarr2/<name>/,
covering the rules of the Zarr v2 profile (profiles/zarr2.md §10): C and F
order, both dimension separators, every supported data type and byte order,
each compressor (none, zlib, gzip, zstd, blosc with each shuffle), fill
values (numbers, "NaN", "Infinity", "-Infinity", null), 0-d arrays, missing
and empty chunks, implicit groups, nodes inside arrays, stale consolidated
metadata, OME-NGFF attributes below a root that does not declare
OME-NGFF 0.4 (copied unchanged), and the inputs the profile rejects
(`zarr2_reject_*`, one per rejection rule).

Documents and chunks are written here as zarr-python 2 writes them (each
chunk the full chunk shape, padded with the fill value, in the array's
order, then compressed with numcodecs); web/test/zarr2/verify.py reads them
back with zarr-python's own Zarr v2 reader.

Usage: uv run python web/test/zarr2/write_fixtures.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numcodecs
import numpy as np

OUT = Path(__file__).parents[1] / "fixtures" / "zarr2"
RNG = np.random.default_rng(2)


def put(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_json(path: Path, doc) -> None:
    put(path, json.dumps(doc, indent=2, allow_nan=True).encode())


def codec(compressor: dict | None):
    if compressor is None:
        return None
    c = dict(compressor)
    return numcodecs.get_codec(c)


def array(root: Path, path: str, data: np.ndarray, chunks: list[int], *, compressor=None, order="C", sep=None,
          fill=0, skip=frozenset(), attrs=None, dtype: str | None = None, zarray_extra=None) -> None:
    d = root / path if path else root
    doc = {"zarr_format": 2, "shape": list(data.shape), "chunks": chunks, "dtype": dtype or data.dtype.str,
           "compressor": compressor, "fill_value": fill, "order": order, "filters": None}
    if sep is not None:
        doc["dimension_separator"] = sep
    doc.update(zarray_extra or {})
    write_json(d / ".zarray", doc)
    if attrs is not None:
        write_json(d / ".zattrs", attrs)
    c = codec(compressor)
    if data.ndim == 0:
        put(d / "0", c.encode(data.tobytes()) if c else data.tobytes())
        return
    grid = [-(-s // n) for s, n in zip(data.shape, chunks)]
    padding = 0 if fill is None or isinstance(fill, str) else fill
    if isinstance(fill, str):
        padding = float(fill.replace("Infinity", "inf"))
    for idx in np.ndindex(*grid):
        if idx in skip:
            continue
        sl = tuple(slice(i * n, min((i + 1) * n, s)) for i, n, s in zip(idx, chunks, data.shape))
        block = np.full(chunks, padding, dtype=data.dtype)
        block[tuple(slice(0, e.stop - e.start) for e in sl)] = data[sl]
        raw = block.tobytes(order=order)
        key = (sep or ".").join(map(str, idx))
        put(d / key, c.encode(raw) if c else raw)


def group(root: Path, path: str, attrs=None) -> None:
    d = root / path if path else root
    write_json(d / ".zgroup", {"zarr_format": 2})
    if attrs is not None:
        write_json(d / ".zattrs", attrs)


def arr(shape, dtype) -> np.ndarray:
    dt = np.dtype(dtype)
    if dt.kind == "f":
        return (RNG.standard_normal(shape) * 100).astype(dt)
    if dt.kind == "b":
        return RNG.integers(0, 2, size=shape).astype(dt)
    info = np.iinfo(dt)
    return RNG.integers(info.min, info.max, size=shape, endpoint=True, dtype=dt.newbyteorder("=")).astype(dt)


def store(name: str) -> Path:
    d = OUT / name
    d.mkdir(parents=True)
    return d


ZLIB = {"id": "zlib", "level": 1}
GZIP = {"id": "gzip", "level": 5}
ZSTD = {"id": "zstd", "level": 1, "checksum": False}


def blosc(cname="lz4", clevel=5, shuffle=1, blocksize=0):
    return {"id": "blosc", "cname": cname, "clevel": clevel, "shuffle": shuffle, "blocksize": blocksize}


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    # Compressors, orders and separators.
    d = store("zarr2_compressors")
    group(d, "", {"title": "compressors"})
    array(d, "raw_c", arr((7, 5), "<i2"), [3, 2])
    array(d, "zlib_f", arr((6, 5, 4), "<u2"), [4, 2, 3], compressor=ZLIB, order="F")
    array(d, "gzip_nested", arr((9, 4), "<f4"), [4, 3], compressor=GZIP, sep="/")
    array(d, "zstd_f_nested", arr((5, 5, 3), "<f8"), [2, 4, 2], compressor=ZSTD, order="F", sep="/")
    array(d, "blosc_shuffle", arr((10, 6), "<i4"), [4, 4], compressor=blosc())
    array(d, "blosc_bitshuffle", arr((6, 6), "<u8"), [4, 4], compressor=blosc("zstd", 3, 2))
    array(d, "blosc_noshuffle", arr((6, 3), "<i8"), [4, 2], compressor=blosc("zlib", 1, 0, 256))
    array(d, "blosc_autoshuffle_u1", arr((9, 9), "|u1"), [4, 4], compressor=blosc("blosclz", 9, -1))
    array(d, "blosc_autoshuffle_i2", arr((5, 9), "<i2"), [4, 4], compressor=blosc("lz4hc", 2, -1))
    array(d, "f_order_1d", arr((11,), "<u4"), [4], compressor=ZLIB, order="F")

    # Data types and byte orders.
    d = store("zarr2_dtypes")
    group(d, "")
    for dt in ("|b1", "|i1", "|u1", ">i2", "<u2", ">u4", "<i4", ">i8", "<u8", "<f2", ">f2", ">f4",
               "<f4", ">f8"):
        name = dt.replace("<", "le_").replace(">", "be_").replace("|", "na_")
        array(d, name, arr((5, 3), dt), [2, 2], dtype=dt, fill=False if dt.endswith("b1") else 0)

    # Fill values, missing and empty chunks.
    d = store("zarr2_fill_values")
    group(d, "")
    sparse = {(0, 1), (1, 0)}
    array(d, "nan", arr((6, 6), "<f4"), [3, 3], fill="NaN", skip=sparse)
    array(d, "inf", arr((6, 6), "<f8"), [3, 3], fill="Infinity", skip=sparse, compressor=ZLIB)
    array(d, "neg_inf", arr((6, 6), ">f4"), [3, 3], fill="-Infinity", skip=sparse)
    array(d, "null_int", arr((6, 6), "<i2"), [3, 3], fill=None, skip=sparse)
    array(d, "null_bool", arr((6, 6), "|b1"), [3, 3], fill=None, skip=sparse)
    array(d, "true_bool", arr((6, 6), "|b1"), [3, 3], fill=True, skip=sparse)
    array(d, "neg_i1", arr((6, 6), "|i1"), [3, 3], fill=-5, skip=sparse)
    array(d, "float_fill", arr((6, 6), "<f8"), [3, 3], fill=0.5, skip=sparse)
    array(d, "float_as_int_fill", arr((6, 6), "<u2"), [3, 3], fill=7.0, skip=sparse)
    array(d, "f2_max", arr((4, 4), "<f2"), [3, 3], fill=65504, skip={(1, 1)})
    put(d / "nan/1.1", b"")  # an empty chunk: no entry

    # 0-d arrays, a root array, extra objects.
    d = store("zarr2_scalar_root")
    array(d, "", np.array(42.5, dtype="<f8"), [], compressor=ZLIB, attrs={"units": "K"})

    d = store("zarr2_hierarchy")
    group(d, "", {"root": True})
    array(d, "a/b/zero_d", np.array(7, dtype="<i4"), [])  # a/ and a/b/ are implicit groups
    array(d, "a/c", arr((4, 4), "<u2"), [2, 2], sep="/", attrs={"_ARRAY_DIMENSIONS": ["y", "x"]})
    group(d, "a/c/inside")  # inside an array: not a node
    put(d / "a/c/0/9", b"x")  # outside the grid
    put(d / "a/c/00/1", b"x")  # leading zero
    put(d / "a/c/0.0", b"x")  # the other separator
    group(d, "g", {"empty": {}})
    array(d, "g/h", arr((3,), "<f4"), [2])
    put(d / "g/h/0.0", b"x")  # too many indices for a 1-d array
    put(d / "notes/README.md", b"not part of the hierarchy\n")
    write_json(d / "orphan/.zattrs", {"no": "node"})  # .zattrs without .zarray or .zgroup
    group(d, "sp ace/é")
    array(d, "sp ace/é/x y", arr((2, 2), "|u1"), [1, 2])
    array(d, "empty_shape", np.zeros((0, 3), "<i2"), [2, 2])
    # Stale consolidated metadata: never read.
    write_json(d / ".zmetadata", {"zarr_consolidated_format": 1, "metadata": {
        ".zgroup": {"zarr_format": 2}, "ghost/.zarray": {"shape": [1]}}})

    # OME-NGFF 0.4 attributes below a root that does not declare OME-NGFF 0.4
    # (VIRTUALIZE.md §1.4): the store is read by §10, and every attribute is
    # copied unchanged (OME-Zarr 0.4 stores are read by profiles/ome-zarr.md).
    d = store("zarr2_ome_attrs")
    img = arr((2, 8, 10), "<u2")
    axes = [{"name": "c", "type": "channel"}, {"name": "y", "type": "space", "unit": "micrometer"},
            {"name": "x", "type": "space", "unit": "micrometer"}]
    group(d, "", {"note": "a plain group"})
    group(d, "0", {
        "multiscales": [{"version": "0.4", "name": "image", "axes": axes, "datasets": [
            {"path": str(i), "coordinateTransformations": [{"type": "scale", "scale": [1, 0.5 * 2**i, 0.5 * 2**i]}]}
            for i in range(2)], "type": "mean", "metadata": {"method": "stride"}}],
        "omero": {"channels": [{"label": "a", "color": "FF0000", "window": {"start": 0, "end": 1000, "min": 0,
                                                                            "max": 65535}, "active": True}] * 2,
                  "rdefs": {"model": "color"}},
        "other": "kept"})
    for i in range(2):
        array(d, f"0/{i}", img[:, :: 2**i, :: 2**i], [1, 4, 4], compressor=blosc(), sep="/")
    group(d, "0/labels", {"labels": ["cells"]})
    group(d, "0/labels/cells", {"multiscales": [{"version": "0.4", "axes": axes, "datasets": [
        {"path": "0", "coordinateTransformations": [{"type": "scale", "scale": [1, 0.5, 0.5]}]}]}],
        "image-label": {"version": "0.4", "colors": [{"label-value": 1, "rgba": [255, 0, 0, 255]}]}})
    array(d, "0/labels/cells/0", arr((2, 8, 10), "<u4"), [1, 8, 8], compressor=ZSTD, sep="/")
    # Other versions, a group that already has `ome`, and mixed versions.
    group(d, "v03", {"multiscales": [{"version": "0.3", "axes": ["y", "x"], "datasets": [{"path": "0"}]}]})
    array(d, "v03/0", arr((3, 3), "|u1"), [3, 3])
    group(d, "has_ome", {"ome": {"version": "0.5"}, "multiscales": [{"version": "0.4", "datasets": []}]})
    group(d, "mixed", {"multiscales": [{"version": "0.4"}, {"version": "0.5"}]})
    # Axes that are not valid OME-NGFF (duplicates).
    group(d, "dup_axes", {"multiscales": [{"version": "0.4", "axes": [{"name": "x"}, {"name": "x"}],
                                           "datasets": [{"path": "0"}]}]})
    array(d, "dup_axes/0", arr((3, 3), "|u1"), [3, 3])

    # Rejections, one per rule.
    def reject(name: str, **kw):
        d = store(f"zarr2_reject_{name}")
        group(d, "")
        doc = {"zarr_format": 2, "shape": [4, 4], "chunks": [2, 2], "dtype": "<u2", "compressor": None,
               "fill_value": 0, "order": "C", "filters": None}
        raw = kw.pop("raw", None)
        doc.update(kw)
        if raw is not None:
            put(d / "x/.zarray", raw)
        else:
            write_json(d / "x/.zarray", {k: v for k, v in doc.items() if v != "DROP"})
        put(d / "x/0.0", bytes(8))

    reject("filters", filters=[{"id": "delta", "dtype": "<u2"}])
    reject("compressor_lz4", compressor={"id": "lz4", "acceleration": 1})
    reject("compressor_bz2", compressor={"id": "bz2", "level": 1})
    reject("compressor_no_id", compressor={"level": 1})
    reject("blosc_cname", compressor=blosc("snappy2"))
    reject("blosc_shuffle", compressor=blosc(shuffle=3))
    reject("blosc_clevel", compressor={"id": "blosc", "cname": "lz4", "shuffle": 1})
    reject("dtype_unicode", dtype="<U4")
    reject("dtype_bytes", dtype="|S8")
    reject("dtype_object", dtype="|O")
    reject("dtype_complex", dtype="<c8")
    reject("dtype_datetime", dtype="<M8[ns]")
    reject("dtype_structured", dtype=[["a", "<u2"], ["b", "<f4"]])
    reject("dtype_no_byteorder", dtype="|u2")
    reject("order", order="A")
    reject("order_missing", order="DROP")
    reject("fill_out_of_range", dtype="|u1", fill_value=300)
    reject("fill_bool_int", dtype="|b1", fill_value=1)
    reject("fill_fraction_int", fill_value=0.5)
    reject("fill_string_int", fill_value="NaN")
    reject("fill_uint64_max", dtype="<u8", fill_value=18446744073709551615)
    reject("fill_f4_overflow", dtype="<f4", fill_value=1e39)
    reject("fill_base64", dtype="<f8", fill_value="AAAAAAAA+H8=")
    reject("separator", dimension_separator="-")
    reject("zarr_format_3", zarr_format=3)
    reject("shape_chunks_mismatch", chunks=[2])
    reject("chunks_zero", chunks=[2, 0])
    reject("shape_negative", shape=[4, -4])
    reject("json_nan_literal", raw=b'{"zarr_format": 2, "shape": [4, 4], "chunks": [2, 2], "dtype": "<f4", '
                                   b'"compressor": null, "fill_value": NaN, "order": "C", "filters": null}')
    reject("json_duplicate_last_wins", raw=b'{"zarr_format": 2, "shape": [4, 4], "chunks": [2, 2], "dtype": "<u2", '
                                           b'"compressor": null, "fill_value": 0, "order": "C", "filters": null, '
                                           b'"order": "K"}')
    d = store("zarr2_reject_zarray_and_zgroup")
    group(d, "")
    array(d, "x", arr((2, 2), "|u1"), [2, 2])
    group(d, "x")
    d = store("zarr2_reject_zattrs_not_object")
    group(d, "")
    array(d, "x", arr((2, 2), "|u1"), [2, 2], attrs=["not", "an", "object"])
    d = store("zarr2_reject_zgroup_format")
    write_json(d / ".zgroup", {"zarr_format": "2"})
    d = store("zarr2_reject_no_root_metadata")
    array(d, "x", arr((2, 2), "|u1"), [2, 2])

    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"{sum(1 for _ in OUT.iterdir())} stores, {total} bytes")


if __name__ == "__main__":
    main()
