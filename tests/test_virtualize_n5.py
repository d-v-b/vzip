"""The Python N5 virtualizer (profiles/n5.md) on the synthetic stores.

Equivalence with the browser virtualizer is checked by
conformance/virtualize/compare.py, and pixel correctness against an
independent block reader and zarr-n5 by web/test/n5/verify.py.
"""

from pathlib import Path

import pytest

from vzip.virtualize import Rejected, virtualize

FIXTURES = Path(__file__).parents[1] / "web" / "test" / "fixtures" / "n5"
URL = "https://data.test/n5/{}/"

T = lambda n: {"name": "transpose", "configuration": {"order": list(range(n - 1, -1, -1))}}  # noqa: E731
BIG = {"name": "bytes", "configuration": {"endian": "big"}}
ONE = {"name": "bytes"}


def n5(*inner):
    return [{"name": "n5_default", "configuration": {"codecs": list(inner)}}]


# store: (summary members, {array path: (shape, chunk shape, data type, codecs, dimension names)},
#         {group path: ome multiscale (without datasets) or None}, (first chunk key, size) or None)
CASES = {
    "n5_compressions": (
        {"arrays": 8, "groups": 1, "images": []},
        {
            "raw_u16": ([10, 7], [4, 3], "uint16", n5(T(2), BIG), None),
            "gzip_i32_padded": ([9, 5, 3], [4, 2, 2], "int32",
                                n5(T(3), BIG, {"name": "gzip", "configuration": {"level": 1}}), None),
            "zlib_f64": ([6, 6], [4, 4], "float64", n5(T(2), BIG, {"name": "zlib", "configuration": {"level": 1}}), None),
            "zstd_f32": ([5, 9], [3, 4], "float32",
                         n5(T(2), BIG, {"name": "zstd", "configuration": {"level": 0, "checksum": False}}), None),
            "blosc_u8": ([8, 6, 5], [4, 4, 4], "uint8", n5(T(3), ONE, {"name": "blosc", "configuration": {
                "cname": "lz4", "clevel": 5, "shuffle": "shuffle", "typesize": 1, "blocksize": 0}}), None),
            "blosc_i16_bitshuffle": ([7, 7], [4, 4], "int16", n5(T(2), BIG, {"name": "blosc", "configuration": {
                "cname": "zstd", "clevel": 3, "shuffle": "bitshuffle", "typesize": 2, "blocksize": 0}}), None),
            "blosc_u64_noshuffle_noblocksize": ([5, 3], [2, 2], "uint64", n5(T(2), BIG, {
                "name": "blosc", "configuration": {"cname": "zlib", "clevel": 1, "shuffle": "noshuffle",
                                                   "typesize": 8, "blocksize": 0}}), None),
            "legacy_gzip_i8": ([6, 4], [3, 3], "int8", n5(T(2), ONE, {"name": "gzip", "configuration": {"level": 1}}),
                               None),
        },
        {"": None}, ("blosc_i16_bitshuffle/0/0", None)),
    "n5_root_dataset": ({"arrays": 1, "groups": 0}, {"": ([11, 3, 2], [5, 2, 2], "uint16", None, None)}, {},
                        ("0/0/0", None)),
    "n5_hierarchy": (
        {"arrays": 3, "groups": 6, "chunks": 9, "emptyChunks": 1},
        {"a/b/sparse": ([8, 8], [3, 3], "uint16", None, None), "c/d/e": ([3], [2], "float32", None, None),
         "f g/h é": ([4, 2], [2, 2], "uint8", None, None)},
        {"": None, "a": None, "a/b": None, "c": None, "c/d": None, "f g": None}, ("a/b/sparse/0/0", 30)),
    "n5_cosem": (
        {"images": [{"path": "em/fibsem-uint8", "convention": "cosem"}]},
        {"em/fibsem-uint8/s1": ([6, 5, 4], [4, 4, 4], "uint8", None, ["x", "y", "z"])},
        {"em/fibsem-uint8": {"name": "em/fibsem-uint8", "axes": [
            {"name": "x", "type": "space", "unit": "nanometer"}, {"name": "y", "type": "space", "unit": "nanometer"},
            {"name": "z", "type": "space", "unit": "nanometer"}]}}, None),
    "n5_cosem_array_transforms": (
        {"images": [{"path": "img", "convention": "cosem"}]}, {"img/s0": ([12, 10, 8], [5, 5, 5], "uint8", None,
                                                                           ["x", "y", "z"])},
        {"img": {"axes": [{"name": a, "type": "space", "unit": "micrometer"} for a in "xyz"]}}, None),
    "n5_viewer_scales": (
        {"images": [{"path": "setup0/timepoint0", "convention": "n5-viewer"}]}, {},
        {"setup0/timepoint0": {"axes": [{"name": a, "type": "space", "unit": "micrometer"} for a in "xyz"]}}, None),
    "n5_viewer_downsampling": (
        {"images": [{"path": "g", "convention": "n5-viewer"}]}, {"g/s2": ([4, 3], [4, 4], "uint16", None, ["x", "y"])},
        {"g": {"axes": [{"name": "x", "type": "space"}, {"name": "y", "type": "space"}]}}, None),
    "n5_viewer_time_first": (
        {"images": [{"path": "t", "convention": "n5-viewer"}]}, {"t/s1": ([2, 2, 3, 3], [1, 4, 4, 4], "float32", None,
                                                                           ["t", "z", "y", "x"])},
        {"t": {"axes": [{"name": "t", "type": "time"}] + [{"name": a, "type": "space", "unit": "nanometer"}
                                                           for a in "zyx"]}}, None),
    "n5_multiscales_unrecognized": (
        {"images": [], "arrays": 4}, {"bad_scale/s0": ([12, 10, 8], [6, 6, 6], "uint8", None, None)},
        {"bad_scale": None, "bad_axes": None, "four_d": None, "dotdot": None}, None),
    "n5_edge_varlength_block": ({"chunks": 4}, {"v": ([4, 4], [2, 2], "uint8", n5(T(2), ONE), None)}, {}, None),
}

# The scales and translations of the COSEM and n5-viewer images, level by level.
TRANSFORMS = {
    "n5_cosem": ("em/fibsem-uint8", [[4.0, 4.0, 5.24], [8.0, 8.0, 10.48], [16.0, 16.0, 20.96]],
                 [[0.0, 0.0, 0.0], [2.0, 2.0, 2.62], [6.0, 6.0, 7.86]]),
    "n5_cosem_array_transforms": ("img", [[1.0, 1.0, 3.0], [2.0, 2.0, 6.0]], [[0, 0, 0], [0, 0, 0]]),
    "n5_viewer_scales": ("setup0/timepoint0", [[0.25, 0.25, 1.5], [0.5, 0.5, 1.5], [1.0, 1.0, 3.0]], None),
    "n5_viewer_downsampling": ("g", [[0.5, 0.75], [1.0, 1.5], [2.0, 3.0]], None),
    "n5_viewer_time_first": ("t", [[1.0, 2.0, 0.5, 0.5], [1.0, 4.0, 1.0, 1.0]], None),
}


def key(path: str) -> str:
    return f"{path}/zarr.json" if path else "zarr.json"


def test_virtualizes_the_synthetic_stores():
    for name, (summary, arrays, groups, chunk) in CASES.items():
        fmt, out = virtualize(str(FIXTURES / name), url=URL.format(name))
        assert fmt == "n5", name
        assert {**out.summary, **summary} == out.summary, name
        for path, (shape, chunks, dtype, codecs, names) in arrays.items():
            doc = out.docs[key(path)]
            assert (doc["shape"], doc["chunk_grid"]["configuration"]["chunk_shape"], doc["data_type"]) == \
                (shape, chunks, dtype), (name, path)
            assert doc["chunk_key_encoding"] == {"name": "v2", "configuration": {"separator": "/"}}
            assert doc["fill_value"] == 0
            if codecs is not None:
                assert doc["codecs"] == codecs, (name, path)
            assert doc.get("dimension_names") == names, (name, path)
            assert not {"dimensions", "blockSize", "dataType", "compression", "n5"} & set(doc["attributes"])
        for path, ms in groups.items():
            doc = out.docs[key(path)]
            assert doc["node_type"] == "group" and "n5" not in doc["attributes"], (name, path)
            if ms is None:
                assert "ome" not in doc["attributes"], (name, path)
            else:
                got = dict(doc["attributes"]["ome"]["multiscales"][0])
                assert doc["attributes"]["ome"]["version"] == "0.5"
                del got["datasets"]
                assert got == ms, (name, path)
        # One source per chunk entry, in key order, each the whole object.
        keys = [k for k, _ in out.chunks]
        assert keys == sorted(keys) and all(n > 0 for _, n in out.chunks), name
        assert out.sources == [URL.format(name) + k.replace(" ", "%20").replace("é", "%C3%A9") for k in keys]
        assert out.refs() == {k: [(i, 0, n)] for i, (k, n) in enumerate(out.chunks)}
        if chunk is not None:
            assert chunk[0] in keys, name
            if chunk[1] is not None:
                assert dict(out.chunks)[chunk[0]] == chunk[1], name
    for name, (group, scales, translations) in TRANSFORMS.items():
        _, out = virtualize(str(FIXTURES / name), url=URL.format(name))
        datasets = out.docs[key(group)]["attributes"]["ome"]["multiscales"][0]["datasets"]
        flat = lambda xs: [v for x in xs for v in x]  # noqa: E731
        assert flat(d["coordinateTransformations"][0]["scale"] for d in datasets) == pytest.approx(flat(scales)), name
        if translations is None:
            assert all(len(d["coordinateTransformations"]) == 1 for d in datasets), name
        else:
            got = flat(d["coordinateTransformations"][1]["translation"] for d in datasets)
            assert got == pytest.approx(flat(translations)), name
    # Every N5 attribute that is not an array key is kept; nodes inside datasets and stray objects are not.
    _, out = virtualize(str(FIXTURES / "n5_hierarchy"), url=URL.format("n5_hierarchy"))
    assert out.docs["zarr.json"]["attributes"] == {"description": "groups"}
    assert out.docs["c/zarr.json"]["attributes"] == {"kind": "explicit group"}
    assert out.docs["a/zarr.json"] == {"zarr_format": 3, "node_type": "group", "attributes": {}}
    assert "a/b/sparse/0/zarr.json" not in out.docs and "docs/zarr.json" not in out.docs
    assert [k for k, _ in out.chunks if k.startswith("a/b/sparse/")] == [
        "a/b/sparse/0/0", "a/b/sparse/0/2", "a/b/sparse/1/1", "a/b/sparse/1/2", "a/b/sparse/2/0"]
    _, out = virtualize(str(FIXTURES / "n5_root_dataset"), url=URL.format("n5_root_dataset"))
    assert out.docs["zarr.json"]["attributes"] == {"resolution": [1.5, 2, 3], "name": "root"}
    _, out = virtualize(str(FIXTURES / "n5_multiscales_unrecognized"), url=URL.format("x"))
    assert out.docs["has_ome/zarr.json"]["attributes"]["ome"] == {"version": "0.5", "custom": True}


@pytest.mark.parametrize("name,message", [
    ("n5_reject_datatype_string", "dataType 'string'"),
    ("n5_reject_datatype_object", "dataType 'object'"),
    ("n5_reject_compression_lz4", "compression 'lz4'"),
    ("n5_reject_compression_xz", "compression 'xz'"),
    ("n5_reject_compression_bzip2", "compression 'bzip2'"),
    ("n5_reject_compression_jpeg", "compression 'jpeg'"),
    ("n5_reject_compression_no_type", "string type"),
    ("n5_reject_no_compression", "no compression"),
    ("n5_reject_gzip_usezlib_string", "useZlib"),
    ("n5_reject_blosc_cname", "cname 'lz5'"),
    ("n5_reject_blosc_clevel", "clevel 10"),
    ("n5_reject_blosc_shuffle_missing", "shuffle None"),
    ("n5_reject_dimensions_empty", "dimensions \\[\\]"),
    ("n5_reject_dimensions_negative", "dimensions \\[4, -1\\]"),
    ("n5_reject_dimensions_fraction", "dimensions \\[4, 2.5\\]"),
    ("n5_reject_dimensions_huge", "dimensions"),
    ("n5_reject_blocksize_mismatch", "blockSize \\[2\\]"),
    ("n5_reject_blocksize_zero", "blockSize \\[2, 0\\]"),
    ("n5_reject_blocksize_huge", "blockSize"),
    ("n5_reject_attributes_not_object", "not a JSON object"),
    ("n5_reject_json_nan", "literal NaN"),
    ("n5_reject_json_bom", "byte order mark"),
    ("n5_reject_json_not_utf8", "not UTF-8"),
    ("n5_reject_json_trailing_comma", "invalid JSON"),
    ("n5_reject_json_overflow", "not finite"),
    ("n5_reject_json_too_deep", "nests more than 256"),
    ("n5_reject_json_empty_object", "invalid JSON"),
    ("n5_reject_reserved_key", "__vz__"),
    ("n5_reject_no_root_attributes", "not an N5 or Zarr v2 store"),
])
def test_rejects(name, message):
    with pytest.raises(Rejected, match=message):
        virtualize(str(FIXTURES / name), url=URL.format(name))
