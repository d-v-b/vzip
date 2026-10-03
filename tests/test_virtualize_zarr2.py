"""The Python Zarr v2 virtualizer (profiles/zarr2.md) on the synthetic stores.

Equivalence with the browser virtualizer is checked by
conformance/virtualize/compare.py, and pixel correctness against zarr-python's
Zarr v2 reader by web/test/zarr2/verify.py.
"""

import json
from pathlib import Path

import pytest

from vzip.virtualize import Rejected, virtualize

FIXTURES = Path(__file__).parents[1] / "web" / "test" / "fixtures" / "zarr2"
URL = "https://data.test/zarr2/{}/"

LE = {"name": "bytes", "configuration": {"endian": "little"}}
BE = {"name": "bytes", "configuration": {"endian": "big"}}
ONE = {"name": "bytes"}
T2 = {"name": "transpose", "configuration": {"order": [1, 0]}}
T3 = {"name": "transpose", "configuration": {"order": [2, 1, 0]}}
ZLIB = {"name": "zlib", "configuration": {"level": 1}}


def blosc(cname, clevel, shuffle, typesize, blocksize=0):
    return {"name": "blosc", "configuration": {"cname": cname, "clevel": clevel, "shuffle": shuffle,
                                               "typesize": typesize, "blocksize": blocksize}}


# store: {array path: (data type, codecs, separator, fill value)}, summary members
CASES = {
    "zarr2_compressors": ({
        "raw_c": ("int16", [LE], ".", 0),
        "zlib_f": ("uint16", [T3, LE, ZLIB], ".", 0),
        "gzip_nested": ("float32", [LE, {"name": "gzip", "configuration": {"level": 1}}], "/", 0),
        "zstd_f_nested": ("float64", [T3, LE, {"name": "zstd", "configuration": {"level": 0, "checksum": False}}],
                          "/", 0),
        "blosc_shuffle": ("int32", [LE, blosc("lz4", 5, "shuffle", 4)], ".", 0),
        "blosc_bitshuffle": ("uint64", [LE, blosc("zstd", 3, "bitshuffle", 8)], ".", 0),
        "blosc_noshuffle": ("int64", [LE, blosc("zlib", 1, "noshuffle", 8, 256)], ".", 0),
        "blosc_autoshuffle_u1": ("uint8", [ONE, blosc("blosclz", 9, "bitshuffle", 1)], ".", 0),
        "blosc_autoshuffle_i2": ("int16", [LE, blosc("lz4hc", 2, "shuffle", 2)], ".", 0),
        "f_order_1d": ("uint32", [LE, ZLIB], ".", 0),
    }, {"arrays": 10, "groups": 1}),
    "zarr2_dtypes": ({
        "na_b1": ("bool", [ONE], ".", False), "na_i1": ("int8", [ONE], ".", 0), "be_i2": ("int16", [BE], ".", 0),
        "le_u2": ("uint16", [LE], ".", 0), "be_u4": ("uint32", [BE], ".", 0), "be_i8": ("int64", [BE], ".", 0),
        "le_f2": ("float16", [LE], ".", 0), "be_f2": ("float16", [BE], ".", 0), "be_f8": ("float64", [BE], ".", 0),
    }, {"arrays": 14}),
    "zarr2_fill_values": ({
        "nan": ("float32", [LE], ".", "NaN"), "inf": ("float64", [LE, ZLIB], ".", "Infinity"),
        "neg_inf": ("float32", [BE], ".", "-Infinity"), "null_int": ("int16", [LE], ".", 0),
        "null_bool": ("bool", [ONE], ".", False), "true_bool": ("bool", [ONE], ".", True),
        "neg_i1": ("int8", [ONE], ".", -5), "float_fill": ("float64", [LE], ".", 0.5),
        "float_as_int_fill": ("uint16", [LE], ".", 7), "f2_max": ("float16", [LE], ".", 65504),
    }, {"emptyChunks": 1, "chunks": 20}),
    "zarr2_scalar_root": ({"": ("float64", [LE, ZLIB], ".", 0)}, {"arrays": 1, "groups": 0, "chunks": 1}),
    "zarr2_hierarchy": ({
        "a/b/zero_d": ("int32", [LE], ".", 0), "a/c": ("uint16", [LE], "/", 0), "g/h": ("float32", [LE], ".", 0),
        "sp ace/é/x y": ("uint8", [ONE], ".", 0), "empty_shape": ("int16", [LE], ".", 0),
    }, {"arrays": 5, "groups": 6}),
    "zarr2_ome_attrs": ({"0/0": ("uint16", [LE, blosc("lz4", 5, "shuffle", 2)], "/", 0)}, {"groups": 8}),
}


def key(path: str) -> str:
    return f"{path}/zarr.json" if path else "zarr.json"


def test_virtualizes_the_synthetic_stores():
    for name, (arrays, summary) in CASES.items():
        fmt, out = virtualize(str(FIXTURES / name), url=URL.format(name))
        assert fmt == "zarr2", name
        assert {**out.summary, **summary} == out.summary, name
        for path, (dtype, codecs, sep, fill) in arrays.items():
            doc = out.docs[key(path)]
            assert (doc["data_type"], doc["codecs"], doc["chunk_key_encoding"], doc["fill_value"]) == \
                (dtype, codecs, {"name": "v2", "configuration": {"separator": sep}}, fill), (name, path)
        keys = [k for k, _ in out.chunks]
        assert keys == sorted(keys) and all(n > 0 for _, n in out.chunks), name
        assert out.refs() == {k: [(i, 0, n)] for i, (k, n) in enumerate(out.chunks)}
    # Chunk keys follow each array's separator; stray objects, nodes inside arrays and .zmetadata are not used.
    _, out = virtualize(str(FIXTURES / "zarr2_hierarchy"), url=URL.format("h"))
    assert [k for k, _ in out.chunks] == [
        "a/b/zero_d/0", "a/c/0/0", "a/c/0/1", "a/c/1/0", "a/c/1/1", "g/h/0", "g/h/1", "sp ace/é/x y/0.0",
        "sp ace/é/x y/1.0"]
    assert out.sources[-1] == "https://data.test/zarr2/h/sp%20ace/%C3%A9/x%20y/1.0"
    assert out.docs["a/zarr.json"] == {"zarr_format": 3, "node_type": "group", "attributes": {}}
    assert out.docs["a/c/zarr.json"]["attributes"] == {"_ARRAY_DIMENSIONS": ["y", "x"]}
    assert not {"a/c/inside/zarr.json", "orphan/zarr.json", "ghost/zarr.json", "notes/zarr.json"} & set(out.docs)
    # Attributes are copied unchanged, OME-NGFF 0.4 ones included: the root does not declare
    # OME-NGFF 0.4 (VIRTUALIZE.md §1.4), so the store is not read by the OME-Zarr profile.
    _, out = virtualize(str(FIXTURES / "zarr2_ome_attrs"), url=URL.format("o"))
    for path in ("", "0", "0/labels", "0/labels/cells", "v03", "has_ome", "mixed", "dup_axes"):
        zattrs = FIXTURES / "zarr2_ome_attrs" / path / ".zattrs"
        assert out.docs[key(path)]["attributes"] == json.loads(zattrs.read_text()), path
    assert not any("dimension_names" in d for d in out.docs.values())


@pytest.mark.parametrize("name,message", [
    ("zarr2_reject_filters", "filters"),
    ("zarr2_reject_compressor_lz4", "compressor 'lz4'"),
    ("zarr2_reject_compressor_bz2", "compressor 'bz2'"),
    ("zarr2_reject_compressor_no_id", "string id"),
    ("zarr2_reject_blosc_cname", "cname 'snappy2'"),
    ("zarr2_reject_blosc_shuffle", "shuffle 3"),
    ("zarr2_reject_blosc_clevel", "clevel None"),
    ("zarr2_reject_dtype_unicode", "dtype <U4"),
    ("zarr2_reject_dtype_bytes", "dtype \\|S8"),
    ("zarr2_reject_dtype_object", "dtype \\|O"),
    ("zarr2_reject_dtype_complex", "dtype <c8"),
    ("zarr2_reject_dtype_datetime", "dtype <M8"),
    ("zarr2_reject_dtype_structured", "dtype \\[\\[.a"),
    ("zarr2_reject_dtype_no_byteorder", "no byte order"),
    ("zarr2_reject_order", "order 'A'"),
    ("zarr2_reject_order_missing", "order 'None'"),
    ("zarr2_reject_fill_out_of_range", "fill_value 300"),
    ("zarr2_reject_fill_bool_int", "fill_value 1 is not a bool"),
    ("zarr2_reject_fill_fraction_int", "fill_value 0.5"),
    ("zarr2_reject_fill_string_int", "fill_value NaN is not a uint16"),
    ("zarr2_reject_fill_uint64_max", "is not a uint64"),
    ("zarr2_reject_fill_f4_overflow", "is not a float32"),
    ("zarr2_reject_fill_base64", "is not a float64"),
    ("zarr2_reject_separator", "dimension_separator '-'"),
    ("zarr2_reject_zarr_format_3", "zarr_format is not 2"),
    ("zarr2_reject_shape_chunks_mismatch", "chunks \\[2\\]"),
    ("zarr2_reject_chunks_zero", "chunks \\[2, 0\\]"),
    ("zarr2_reject_shape_negative", "shape \\[4, -4\\]"),
    ("zarr2_reject_json_nan_literal", "literal NaN"),
    ("zarr2_reject_json_duplicate_last_wins", "order 'K'"),
    ("zarr2_reject_zarray_and_zgroup", "both .zarray and .zgroup"),
    ("zarr2_reject_zattrs_not_object", "x/.zattrs is not a JSON object"),
    ("zarr2_reject_zgroup_format", ".zgroup: zarr_format is not 2"),
    ("zarr2_reject_no_root_metadata", "not an N5 or Zarr v2 store"),
])
def test_rejects(name, message):
    with pytest.raises(Rejected, match=message):
        virtualize(str(FIXTURES / name), url=URL.format(name))
