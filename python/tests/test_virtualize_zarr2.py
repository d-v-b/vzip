"""The Python Zarr v2 virtualizer (spec/virtualize/zarr2/profile.md) on the synthetic stores.

Equivalence with the browser virtualizer is checked by
conformance/virtualize/compare.py, and pixel correctness against zarr-python's
Zarr v2 reader by js/test/zarr2/verify.py.
"""

import json
import zipfile
from pathlib import Path

import pytest

from vzip.virtualize import Rejected, virtualize
from vzip.virtualize.common import declare

FIXTURES = Path(__file__).parents[2] / "fixtures" / "zarr2"
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
        "gzip_nested": ("float32", [LE, {"name": "gzip", "configuration": {"level": 5}}], "/", 0),
        "zstd_f_nested": ("float64", [T3, LE, {"name": "zstd", "configuration": {"level": 1, "checksum": False}}],
                          "/", 0),
        "blosc_shuffle": ("int32", [LE, blosc("lz4", 5, "shuffle", 4)], ".", 0),
        "blosc_bitshuffle": ("uint64", [LE, blosc("zstd", 3, "bitshuffle", 8)], ".", 0),
        "blosc_noshuffle": ("int64", [LE, blosc("zlib", 1, "noshuffle", 8, 256)], ".", 0),
        "blosc_autoshuffle_u1": ("uint8", [ONE, blosc("blosclz", 9, "bitshuffle", 1)], ".", 0),
        "blosc_autoshuffle_i2": ("int16", [LE, blosc("lz4hc", 2, "shuffle", 2)], ".", 0),
        "f_order_1d": ("uint32", [LE, ZLIB], ".", 0),
    }, {"arrays": 10, "groups": 1, "chunks": 12}),
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
    }, {"emptyChunks": 1, "chunks": 10}),
    "zarr2_scalar_root": ({"": ("float64", [LE, ZLIB], ".", 0)}, {"arrays": 1, "groups": 0, "chunks": 1}),
    "zarr2_hierarchy": ({
        "a/b/zero_d": ("int32", [LE], ".", 0), "a/c": ("uint16", [LE], "/", 0), "g/h": ("float32", [LE], ".", 0),
        "sp ace/é/x y": ("uint8", [ONE], ".", 0), "empty_shape": ("int16", [LE], ".", 0),
    }, {"arrays": 5, "groups": 6, "chunks": 5}),
    "zarr2_ome_attrs": ({"0/0": ("uint16", [LE, blosc("lz4", 5, "shuffle", 2)], "/", 0)}, {"groups": 8}),
    # The compressors' levels, which the codecs carry, and -0.0 fill values as hex fills.
    "zarr2_source_metadata": ({
        "null_fill": ("float32", [LE], ".", 0),
        "neg_zero_f2": ("float16", [BE], ".", "0x8000"),
        "neg_zero_f4": ("float32", [LE], ".", "0x80000000"),
        "neg_zero_f8": ("float64", [LE], ".", "0x8000000000000000"),
        "big_fill_f8": ("float64", [LE], ".", 18446744073709551616.0),
        "zlib_9": ("uint16", [LE, {"name": "zlib", "configuration": {"level": 9}}], ".", 0),
        "zlib_no_level": ("uint16", [LE, ZLIB], ".", 0),
        "gzip_default": ("uint16", [LE, {"name": "gzip", "configuration": {"level": 6}}], ".", 0),
        "zstd_checksum": ("uint16", [LE, {"name": "zstd", "configuration": {"level": -3, "checksum": True}}], ".", 0),
        "extra_members": ("uint16", [LE, blosc("lz4", 5, "shuffle", 2)], ".", 0),
    }, {"arrays": 10, "otherObjects": 0}),
    "zarr2_objects": ({"a": ("uint16", [LE], ".", 0)}, {"chunks": 3, "otherObjects": 6}),
}


def key(path: str) -> str:
    return f"{path}/zarr.json" if path else "zarr.json"


def test_virtualizes_the_synthetic_stores(tmp_path):
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
    # Chunk keys follow each array's separator and grid (two chunks, with a partial edge chunk,
    # along the first axis in C order and along the second in F order).
    _, out = virtualize(str(FIXTURES / "zarr2_compressors"), url=URL.format("c"))
    assert [k for k, _ in out.chunks if k.startswith(("raw_c/", "zstd_f_nested/"))] == [
        "raw_c/0.0", "raw_c/1.0", "zstd_f_nested/0/0/0", "zstd_f_nested/0/1/0"]
    # A missing chunk and an empty one have no entry.
    _, out = virtualize(str(FIXTURES / "zarr2_fill_values"), url=URL.format("f"))
    assert [k for k, _ in out.chunks if k.startswith(("nan/", "inf/"))] == ["inf/1.0", "nan/1.0"]
    # The empty one's key is listed with the empty objects (§3.2, §5).
    assert out.docs["vzip_source/zarr.json"]["attributes"] == declare({}, "zarr2", None, {"empty": ["nan/0.0"]})
    # Stray objects and nodes inside arrays are kept whole under vzip_source/objects/
    # (spec/virtualize/zarr2.md §5); .zmetadata is not.
    _, out = virtualize(str(FIXTURES / "zarr2_hierarchy"), url=URL.format("h"))
    assert [k for k, _ in out.chunks] == [
        "a/b/zero_d/0", "a/c/0/0", "a/c/1/0", "g/h/0", "sp ace/é/x y/0.0", "vzip_source/objects/.zmetadata", "vzip_source/objects/a/c/0.0",
        "vzip_source/objects/a/c/0/9", "vzip_source/objects/a/c/00/1", "vzip_source/objects/a/c/inside/.zgroup~",
        "vzip_source/objects/g/h/0.0", "vzip_source/objects/notes/README.md", "vzip_source/objects/orphan/.zattrs"]
    assert out.sources[4] == "https://data.test/zarr2/h/sp%20ace/%C3%A9/x%20y/0.0"
    assert out.sources[-1] == "https://data.test/zarr2/h/orphan/.zattrs"
    assert out.docs["a/zarr.json"] == {"zarr_format": 3, "node_type": "group", "attributes": {}}
    assert out.docs["a/c/zarr.json"]["attributes"] == declare({}, "zarr2", None,
                                                              {"attributes": {"_ARRAY_DIMENSIONS": ["y", "x"]}})
    assert not {"a/c/inside/zarr.json", "orphan/zarr.json", "ghost/zarr.json", "notes/zarr.json"} & set(out.docs)
    # Attributes are copied unchanged under the convention (spec/virtualize.md conventions §2), OME-NGFF 0.4
    # ones included: the root does not declare OME-NGFF 0.4 (§1.4), so the store is not read
    # by the OME-Zarr profile.
    _, out = virtualize(str(FIXTURES / "zarr2_ome_attrs"), url=URL.format("o"))
    for path in ("", "0", "0/labels", "0/labels/cells", "v03", "has_ome", "mixed", "dup_axes"):
        zattrs = json.loads((FIXTURES / "zarr2_ome_attrs" / path / ".zattrs").read_text())
        expected = declare({}, "zarr2", URL.format("o") if path == "" else None, {"attributes": zattrs})
        assert out.docs[key(path)]["attributes"] == expected, path
    assert not any("dimension_names" in d for d in out.docs.values())
    # Other conventions, a vzip declaration and a malformed zarr_conventions are all copied as they are.
    _, out = virtualize(str(FIXTURES / "zarr2_conventions"), url=URL.format("c"))
    for path in ("", "x", "v", "bad"):
        zattrs = json.loads((FIXTURES / "zarr2_conventions" / path / ".zattrs").read_text())
        assert out.docs[key(path)]["attributes"] == declare({}, "zarr2", URL.format("c") if path == "" else None,
                                                            {"attributes": zattrs}), path
    # Source metadata (spec/virtualize/zarr2.md §4): the attributes, with integers beyond 2^53
    # kept exact, and the members of .zarray and .zgroup the Zarr v3 metadata does not reproduce.
    url = URL.format("zarr2_source_metadata")
    _, out = virtualize(str(FIXTURES / "zarr2_source_metadata"), url=url)
    assert out.docs["zarr.json"]["attributes"] == declare({}, "zarr2", url, {
        "attributes": {"id": 18446744073709551615, "neg": -9007199254740993, "safe": 9007199254740991},
        "metadata": {"creator": {"name": "a writer"}}})
    assert out.docs["null_fill/zarr.json"]["attributes"] == declare({}, "zarr2", None, {"metadata": {"fill_value": None}})
    assert out.docs["extra_members/zarr.json"]["attributes"] == declare({}, "zarr2", None, {
        "attributes": {"_ARRAY_DIMENSIONS": ["x"]}, "metadata": {"custom": {"x": 1}, "compressor": {"nthreads": 2}}})
    for path in ("neg_zero_f4", "zlib_9", "zstd_checksum"):
        assert out.docs[key(path)]["attributes"] == {}, path
    # A float fill written as an integer beyond 2^53 - 1 is kept as written (§4).
    assert out.docs[key("big_fill_f8")]["attributes"] == declare(
        {}, "zarr2", None, {"metadata": {"fill_value": 18446744073709551616}})
    # The archive holds the integers' every digit, and the hex fill.
    archive = tmp_path / "s.vzip"
    out.write(str(archive))
    with zipfile.ZipFile(archive) as z:
        assert b'"id":18446744073709551615,"neg":-9007199254740993' in z.read("zarr.json")
        assert b'"fill_value":"0x80000000"' in z.read("neg_zero_f4/zarr.json")
    # Every other object is referenced whole, from its own URL, under vzip_source/objects/ (a
    # node document's name escaped with ~); the node documents and the chunks are not; empty
    # objects are listed in vzip_source's source metadata.
    url = URL.format("zarr2_objects")
    _, out = virtualize(str(FIXTURES / "zarr2_objects"), url=url)
    others = [".zmetadata", "OME/METADATA.ome.xml", "README.md", "a/inner/.zgroup~", "a/notes.txt", "sub/.zattrs"]
    assert out.chunks == sorted([("a/0", 4), ("a/1", 4), ("sub/b/0", 2)] + [
        ("vzip_source/objects/" + k, (FIXTURES / "zarr2_objects" / k.rstrip("~")).stat().st_size) for k in others])
    assert out.sources == [url + out.origins.get(k, k) for k, _ in out.chunks]
    assert out.docs["vzip_source/zarr.json"]["attributes"] == declare({}, "zarr2", None, {"empty": ["empty.txt"]})
    assert "zarr2" not in out.docs["zarr.json"]["attributes"]["vzip_virtualized"]
    assert "sub/zarr.json" in out.docs and out.docs["sub/zarr.json"]["attributes"] == {}
    # Escaping is one to one: one ~ more on each last segment that is a node document's name and ~s.
    _, out = virtualize(str(FIXTURES / "zarr2_node_names"), url=URL.format("n"))
    assert {k: out.origins[k] for k, _ in out.chunks if k.startswith("vzip_source/")} == {
        "vzip_source/objects/zarr.json~": "zarr.json", "vzip_source/objects/zarr.json~~": "zarr.json~",
        "vzip_source/objects/g/zarr.json~": "g/zarr.json", "vzip_source/objects/a/zarr.json~": "a/zarr.json",
        "vzip_source/objects/a/inner/.zgroup~": "a/inner/.zgroup", "vzip_source/objects/x/.zarray~~~": "x/.zarray~~",
        "vzip_source/objects/x/zarr.jsonx": "x/zarr.jsonx"}
    assert out.docs["vzip_source/zarr.json"]["attributes"] == declare({}, "zarr2", None, {"empty": ["e", "g/e2"]})
    # A float fill written as an integer beyond 2^53 - 1 keeps its digits in M; F is its binary64 value.
    _, out = virtualize(str(FIXTURES / "zarr2_fill_float_int"), url=URL.format("i"))
    m = {p: out.docs[key(p)]["attributes"].get("vzip_virtualized", {}).get("zarr2") for p in ("h", "k", "s")}
    assert m == {"h": {"metadata": {"fill_value": 9007199254740993}},
                 "k": {"metadata": {"fill_value": -18446744073709551616}}, "s": None}
    assert [out.docs[key(p)]["fill_value"] for p in ("h", "k", "s")] == [2.0**53, -(2.0**64), 2**53 - 1]
    archive = tmp_path / "i.vzip"
    out.write(str(archive))
    with zipfile.ZipFile(archive) as z:
        assert b'"fill_value":9007199254740993' in z.read("h/zarr.json")


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
    ("zarr2_reject_no_root_metadata", "not an N5, Zarr v2 or SAFE store"),
    ("zarr2_reject_zlib_level", "zlib level 10"),
    ("zarr2_reject_gzip_level", "gzip level 1 is not an integer"),
    ("zarr2_reject_zstd_level", "zstd level 23"),
    ("zarr2_reject_zstd_checksum", "zstd checksum 1"),
    ("zarr2_reject_objects_collision", "the node vzip_source is where"),
])
def test_rejects(name, message):
    with pytest.raises(Rejected, match=message):
        virtualize(str(FIXTURES / name), url=URL.format(name))


def test_root_array_keeps_other_objects_without_a_group(tmp_path):
    """An array has no children: under a root array, other objects are plain keys, with no
    vzip_source group (spec/virtualize/zarr2.md §5; spec/virtualize/n5.md §6)."""
    for name in ("zarr2/zarr2_root_array_objects", "n5/n5_root_dataset_objects"):
        _, out = virtualize(str(FIXTURES.parent / name), url=f"https://data.test/{name}/")
        assert out.docs["zarr.json"]["node_type"] == "array", name
        assert "vzip_source/zarr.json" not in out.docs, name
        kept = {k for k, _ in out.chunks if k.startswith("vzip_source/")}
        assert kept & {"vzip_source/objects/README.md", "vzip_source/objects/README"}, name
    # The keys of empty objects are the plain key vzip_source/empty.json, in UTF-8.
    name = "zarr2/zarr2_root_array_empty"
    _, out = virtualize(str(FIXTURES.parent / name), url=f"https://data.test/{name}/")
    assert "vzip_source/zarr.json" not in out.docs
    assert out.docs["vzip_source/empty.json"] == {"empty": ["empty", "\u00e9mpty"]}
    archive = tmp_path / "e.vzip"
    out.write(str(archive))
    with zipfile.ZipFile(archive) as z:
        assert z.read("vzip_source/empty.json") == '{"empty":["empty","\u00e9mpty"]}'.encode()
    # An empty chunk object of a root array is listed there too (spec/virtualize/zarr2.md §3.2,
    # spec/virtualize/n5.md §3.1).
    for name in ("zarr2/zarr2_root_array_empty_chunk", "n5/n5_root_dataset_empty_block"):
        _, out = virtualize(str(FIXTURES.parent / name), url=f"https://data.test/{name}/")
        assert [k for k, _ in out.chunks] == ["0"], name
        assert out.docs["vzip_source/empty.json"] == {"empty": ["1"]}, name
