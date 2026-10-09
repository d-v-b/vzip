"""The Python IMS virtualizer (spec/virtualize/ims/profile.md) on the synthetic fixtures.

Equivalence with the browser virtualizer is checked by
conformance/virtualize/compare.py, and pixel correctness by
js/test/ims/verify.py.
"""

import json
import struct
from pathlib import Path

import pytest

import numpy as np

from vzip.virtualize import Rejected, virtualize
from vzip.virtualize.ims.source import canonical

FIXTURES = Path(__file__).parents[2] / "fixtures" / "ims"


def paths(out) -> list[str]:
    """The paths of the object table (spec/virtualize/ims.md §5.7) of a virtualized file."""
    e = out.bytes_entries
    shape = json.loads(e["vzip_source/objects/parent/zarr.json"])["shape"]
    parents = struct.unpack(f"<{shape[0]}i", e["vzip_source/objects/parent/c/0"])
    table: list[str] = []
    for parent, name in zip(parents, family(out, "objects/name"), strict=True):
        table.append("/" if parent < 0 else table[parent].rstrip("/") + "/" + name.decode())
    return table


def family(out, path: str, source: bytes = b"") -> list[bytes]:
    """The members of a family of byte values (conventions §7) under vzip_source; chunks
    that reference the file read `source`, its bytes."""
    e = out.bytes_entries

    def chunk(key: str) -> bytes:
        if key in e:
            return e[key]
        return b"".join(p if isinstance(p, bytes) else source[p[0] : p[0] + p[1]] for p in out.refs[key])

    doc = json.loads(e[f"vzip_source/{path}/zarr.json"])
    if doc["node_type"] == "array":
        return [chunk(f"vzip_source/{path}/c/{i}/0") for i in range(doc["shape"][0])]
    raw = e[f"vzip_source/{path}/offsets/c/0"]
    starts = struct.unpack(f"<{len(raw) // 8}q", raw)
    keys = sorted((k for k in [*e, *out.refs] if k.startswith(f"vzip_source/{path}/data/c/")),
                  key=lambda k: int(k.rsplit("/", 1)[1]))
    data = b"".join(chunk(k) for k in keys)
    return [data[a:b] for a, b in zip(starts, starts[1:])]


def test_virtualizes_the_synthetic_files():
    # name: (summary subset, axes with units, level 0 scale, translation or None, channel 0 of omero)
    cases = {
        "ims_earliest_uint16_deflate.ims": (
            {"levels": 2, "sizes": {"t": 2, "c": 2, "z": 5, "y": 12, "x": 14}, "dataType": "uint16",
             "compressed": [True, True]},
            [("t", "second"), ("c", None), ("z", "micrometer"), ("y", "micrometer"), ("x", "micrometer")],
            [2.5, 1, 0.5, (-0.5999999999999996 - -3.0) / 12, (13.3 - 10.5) / 14], [0, 0, 2.0, -3.0, 10.5],
            {"label": "DAPI", "color": "0033FF", "active": True,
             "window": {"min": 0, "max": 65535, "start": 100, "end": 2500}}),
        "ims_latest_uint8.ims": (
            {"levels": 3, "sizes": {"t": 1, "c": 3, "z": 4, "y": 9, "x": 11}, "dataType": "uint8",
             "channels": ["red", "Channel 1", "far red"]},
            [("c", None), ("z", "micrometer"), ("y", "micrometer"), ("x", "micrometer")],
            [1, 2.0, 0.5, 0.5], [0, 0.0, 0.0, 0.0],
            {"label": "red", "color": "FF0000", "active": True, "window": {"min": 0, "max": 255, "start": 0, "end": 255}}),
        "ims_latest_float32_be.ims": (
            {"sizes": {"t": 3, "c": 1, "z": 1, "y": 7, "x": 9}, "dataType": "float32", "compressed": [True]},
            [("t", "second"), ("y", "micrometer"), ("x", "micrometer")],
            [0.125, 1.5, 1.5], [0, 4.0, -5.0],
            {"label": "intensity", "color": "FFFFFF", "active": True,
             "window": {"min": -1.5, "max": 80.25, "start": -1.5, "end": 80.25}}),
        "ims_latest_dense_links.ims": (
            {"sizes": {"t": 50, "c": 1, "z": 1, "y": 2, "x": 3}, "chunks": 50},
            [("t", None), ("y", "micrometer"), ("x", "micrometer")],
            [1, 0.25, 0.25], [0, 0.0, 0.0],
            {"label": "Channel 0", "color": "FFFFFF", "active": True,
             "window": {"min": 0, "max": 65535, "start": 0, "end": 65535}}),
        "ims_latest_huge_attribute.ims": (
            {"levels": 2, "sizes": {"t": 1, "c": 2, "z": 2, "y": 6, "x": 6}, "channels": ["Kanal α", "Kanal β"]},
            [("c", None), ("z", "nanometer"), ("y", "nanometer"), ("x", "nanometer")],
            [1, 1.0, 0.25, 0.25], [0, 0.0, 0.0, 0.0],
            {"label": "Kanal α", "color": "FFFFFF", "active": True, "window": {"min": 0, "max": 255, "start": 0, "end": 255}}),
        "ims_latest_paged.ims": (
            {"sizes": {"t": 1, "c": 1, "z": 1, "y": 80, "x": 63}, "chunkShape": [1, 1, 2], "chunks": 64},
            [("y", None), ("x", None)], [1, 1], None,
            {"label": "Channel 0", "color": "FFFFFF", "active": True, "window": {"min": 0, "max": 255, "start": 0, "end": 255}}),
        "ims_latest_soft_links.ims": (
            {"sizes": {"t": 1, "c": 2, "z": 1, "y": 5, "x": 6}, "channels": ["first", "second"]},
            [("c", None), ("y", "micrometer"), ("x", "micrometer")], [1, 0.25, 0.25], [0, 0.0, 0.0],
            {"label": "first", "color": "FFFFFF", "active": True, "window": {"min": 0, "max": 255, "start": 0, "end": 255}}),
        "ims_small_k.ims": (
            {"sizes": {"t": 1, "c": 5, "z": 3, "y": 5, "x": 6}, "chunkShape": [1, 2, 2], "chunks": 5 * 27 - 2},
            [("c", None), ("z", "micrometer"), ("y", "micrometer"), ("x", "micrometer")],
            [1, 1.0, 0.25, 0.25], [0, 0.0, 0.0, 0.0],
            {"label": "Channel 0", "color": "FFFFFF", "active": True,
             "window": {"min": 0, "max": 65535, "start": 0, "end": 65535}}),
        "ims_2d_no_metadata.ims": (
            {"sizes": {"t": 1, "c": 1, "z": 1, "y": 5, "x": 6}, "dataType": "int16"},
            [("y", None), ("x", None)], [1, 1], None,
            {"label": "Channel 0", "color": "FFFFFF", "active": True,
             "window": {"min": -32768, "max": 32767, "start": -32768, "end": 32767}}),
        # One z plane in chunks of 4: the z axis stays, so chunks decode whole.
        "ims_2d_deep_chunks.ims": (
            {"sizes": {"t": 1, "c": 1, "z": 1, "y": 5, "x": 6}, "chunkShape": [4, 8, 8], "chunks": 1},
            [("z", None), ("y", None), ("x", None)], [1, 1, 1], None,
            {"label": "Channel 0", "color": "FFFFFF", "active": True, "window": {"min": 0, "max": 255, "start": 0, "end": 255}}),
        # Chunks indexed by an extensible array (one unlimited dimension) and by a
        # version 2 B-tree (several), with a chunk never written.
        "ims_latest_extensible.ims": (
            {"sizes": {"t": 1, "c": 1, "z": 2, "y": 6, "x": 7}, "dataType": "uint16", "chunks": 3},
            [("z", None), ("y", None), ("x", None)], [1, 1, 1], None,
            {"label": "Channel 0", "color": "FFFFFF", "active": True,
             "window": {"min": 0, "max": 65535, "start": 0, "end": 65535}}),
        "ims_latest_btree2.ims": (
            {"sizes": {"t": 1, "c": 1, "z": 2, "y": 6, "x": 7}, "chunkShape": [1, 2, 2], "compressed": [True],
             "chunks": 2 * 3 * 4 - 1},
            [("z", None), ("y", None), ("x", None)], [1, 1, 1], None,
            {"label": "Channel 0", "color": "FFFFFF", "active": True, "window": {"min": 0, "max": 255, "start": 0, "end": 255}}),
    }
    for name, (summary, axes, scale, translation, channel) in cases.items():
        fmt, out = virtualize(str(FIXTURES / name), url=f"https://data.test/{name}")
        assert fmt == "ims", name
        assert out.url == f"https://data.test/{name}"
        assert {**out.summary, **summary} == out.summary, name
        ome = json.loads(out.bytes_entries["zarr.json"])["attributes"]["ome"]
        ms = ome["multiscales"][0]
        assert [(a["name"], a.get("unit")) for a in ms["axes"]] == axes, name
        transforms = ms["datasets"][0]["coordinateTransformations"]
        assert transforms[0]["scale"] == scale, name
        assert (transforms[1]["translation"] if len(transforms) > 1 else None) == translation, name
        assert ome["omero"]["channels"][0] == channel, name
        image = {k: v for k, v in out.refs.items() if not k.startswith("vzip_source/")}
        assert len(image) == out.summary["chunks"]
        assert all(len(ranges) == 1 for ranges in image.values())


@pytest.mark.parametrize("name,message", [
    ("ims_reject_not_imaris.ims", "not an Imaris file"),
    ("ims_reject_shuffle.ims", r"filters \[2, 1\]"),
    ("ims_reject_fletcher32.ims", r"filters \[3\]"),
    ("ims_reject_fill_value.ims", "fill value"),
    ("ims_reject_float64.ims", "class 1, 8 bytes"),
    ("ims_reject_offset_size.ims", "offsets and lengths of 4"),
    ("ims_reject_size_mismatch.ims", "differ in size"),
    ("ims_reject_chunk_mismatch.ims", "differ in size, chunk shape"),
    ("ims_reject_dtype_mismatch.ims", "different data types"),
    ("ims_reject_image_larger.ims", "larger than its dataset"),
    ("ims_reject_imagesize_text.ims", "ImageSizeX of level 0"),
    ("ims_reject_imagesize_vlen.ims", "ImageSizeX is not a string"),
    ("ims_reject_missing_channel.ims", "no Channel 1"),
    ("ims_reject_contiguous.ims", "not chunked"),
    ("ims_reject_rank.ims", "not 3-dimensional"),
    ("ims_reject_relative_soft_link.ims", "relative path"),
    ("ims_reject_z_levels.ims", "more than one z plane"),
    ("ims_reject_raw_edges_image.ims", "a chunk's filter mask is not 0"),
])
def test_rejects(name, message):
    with pytest.raises(Rejected, match=message):
        virtualize(str(FIXTURES / name), url="https://data.test/x")


INT32 = [0x10, 0x08, 0, 0, 4, 0, 0, 0, 0, 0, 32, 0]
I32 = {"class": "integer", "size": 4, "order": "little", "signed": True}


def test_describe_type():
    """Each datatype class's description (spec/virtualize/ims.md §5.2); js/test/ims/source.test.ts
    has the same cases."""
    from vzip.virtualize.ims.source import describe_type

    cases = [
        ([0x10, 0, 0, 0, 2, 0, 0, 0, 0, 0, 16, 0], {"class": "integer", "size": 2, "order": "little", "signed": False}),
        ([0x10, 0x09, 0, 0, 4, 0, 0, 0, 0, 0, 32, 0], {"class": "integer", "size": 4, "order": "big", "signed": True}),
        ([0x10, 0, 0, 0, 2, 0, 0, 0, 4, 0, 12, 0],
         {"class": "integer", "size": 2, "order": "little", "signed": False, "offset": 4, "precision": 12,
          "padding": [0, 0]}),
        ([0x11, 0x20, 0x1F, 0, 4, 0, 0, 0, 0, 0, 32, 0, 23, 8, 0, 23, 127, 0, 0, 0],
         {"class": "float", "size": 4, "order": "little"}),
        ([0x13, 0x11, 0, 0, 5, 0, 0, 0], {"class": "string", "size": 5, "padding": "null-padded", "charset": "utf-8"}),
        ([0x15, 0x03, 0, 0, 3, 0, 0, 0, 0x61, 0x62, 0], {"class": "opaque", "size": 3, "tag": "ab"}),
        ([0x36, 0x02, 0, 0, 8, 0, 0, 0, 0x69, 0, 0, *INT32, 0x6A, 0, 4, *INT32],
         {"class": "compound", "size": 8, "members": [{"name": "i", "offset": 0, "type": I32},
                                                      {"name": "j", "offset": 4, "type": I32}]}),
        ([0x3A, 0, 0, 0, 24, 0, 0, 0, 2, 2, 0, 0, 0, 3, 0, 0, 0, *INT32],
         {"class": "array", "size": 24, "shape": [2, 3], "base": I32}),
        ([0x19, 0x01, 0, 0, 16, 0, 0, 0, 0x10, 0, 0, 0, 1, 0, 0, 0, 0, 0, 8, 0],
         {"class": "variable-length", "size": 16,
          "base": {"class": "integer", "size": 1, "order": "little", "signed": False},
          "string": True, "padding": "null-terminated", "charset": "ascii"}),
    ]
    for data, expected in cases:
        assert describe_type(bytes(data))[0] == expected, data


def test_describe_type_rejects_an_unknown_version():
    from vzip.virtualize.ims.source import describe_type

    with pytest.raises(Rejected, match="unsupported datatype message version 6"):
        describe_type(bytes([0x60, 0, 0, 0, 1, 0, 0, 0]))


def test_describe_type_rejects_an_unknown_class():
    from vzip.virtualize.ims.source import describe_type

    with pytest.raises(Rejected, match="unknown datatype class 11"):
        describe_type(bytes([0x1B, 0, 0, 0, 1, 0, 0, 0]))


def test_describe_type_rejects_a_truncated_datatype():
    from vzip.virtualize.ims.source import describe_type

    with pytest.raises(Rejected, match="truncated HDF5 structure"):
        describe_type(bytes([0x10, 0, 0, 0, 2, 0, 0, 0, 0]))


def test_source_node_holds_the_hdf5_tree():
    """Groups, datasets, links and named datatypes of the HDF5 file under vzip_source/hdf5
    (spec/virtualize/ims.md §5); js/test/ims/verify.py checks every value against h5py."""
    for libver in ("earliest", "latest"):
        _, out = virtualize(str(FIXTURES / f"ims_{libver}_reconstruction.ims"), url="https://data.test/x")
        doc = lambda p: json.loads(out.bytes_entries[f"vzip_source/hdf5/{p}zarr.json"])  # noqa: E731
        assert "ims" not in json.loads(out.bytes_entries["zarr.json"])["attributes"]["vzip_virtualized"]
        channel = doc("DataSet/ResolutionLevel 0/TimePoint 0/Channel 1/")["attributes"]["vzip_virtualized"]["ims"]
        assert channel["images"] == {"Data": {"level": 0, "t": 0, "c": 1, "shape": [2, 8, 8]}}
        extra = doc("Extra/")["attributes"]["vzip_virtualized"]["ims"]
        assert extra["links"] == {"contiguous": {"hard": paths(out).index("/Extra/alias")},
                                  "soft": {"soft": "/Extra/contiguous"},
                                  "external": {"external": {"file": "other.h5", "path": "/data"}}}
        assert "unsupported" not in extra
        assert extra["datatypes"]["typedef"]["datatype"]["class"] == "compound"
        assert extra["attributes"]["long"] == {"datatype": {"class": "float", "size": 4, "order": "little"},
                                               "array": "attributes/0"}
        assert extra["attributes"]["scalar"] == {"datatype": {"class": "float", "size": 8, "order": "little"},
                                                 "value": 2.5}
        arrays = {k[len("vzip_source/hdf5/Extra/"):-len("/zarr.json")]: json.loads(v)
                  for k, v in out.bytes_entries.items()
                  if k.startswith("vzip_source/hdf5/Extra/") and k.endswith("/zarr.json")
                  and k != "vzip_source/hdf5/Extra/zarr.json"}
        kinds = {k: (d["node_type"], d.get("data_type"), d.get("shape")) for k, d in arrays.items()}
        assert kinds == {
            "alias": ("array", "float64", [2, 3, 4]), "compact": ("array", "int8", [5]),
            "deflate": ("array", "uint16", [10, 30]), "empty": ("array", "uint16", [0, 4]),
            "enum": ("array", "uint8", [3]), "half": ("array", "float16", [5]),
            "names": ("group", None, None), "names/offsets": ("array", "int64", [4]),
            "names/data": ("array", "uint8", [6]), "nested": ("group", None, None),
            "nested/deeper": ("group", None, None), "opaque": ("array", "uint8", [3]),
            "points": ("array", "float32", [2, 2, 3]), "ragged": ("group", None, None),
            "ragged/offsets": ("array", "int64", [3]), "ragged/data": ("array", "uint8", [28]),
            "scalar": ("array", "uint32", []), "table": ("array", "uint8", [4, 16]),
            "unallocated": ("array", "int32", [3]),
        }, libver
        assert arrays["unallocated"]["fill_value"] == -1 and arrays["deflate"]["fill_value"] == 9
        assert arrays["deflate"]["codecs"][-1] == {"name": "zlib", "configuration": {"level": 1}}










def test_source_node_keeps_what_the_review_found():
    """Datatypes and shapes of attributes, named datatypes, references, external
    storage and links, null dataspaces, fill values, names and the attribute
    budget (spec/virtualize/ims.md §5); js/test/ims/verify.py rebuilds them all."""
    for libver in ("earliest", "latest"):
        _, out = virtualize(str(FIXTURES / f"ims_{libver}_review.ims"), url="https://data.test/x")
        doc = lambda p: json.loads(out.bytes_entries[f"vzip_source/hdf5/{p}zarr.json"])  # noqa: E731
        source = lambda p: doc(p)["attributes"]["vzip_virtualized"]["ims"]  # noqa: E731
        x = source("X/")
        a = x["attributes"]
        i16 = {"class": "integer", "size": 2, "order": "little", "signed": True}
        assert a["imaris"] == "2.5 micrometer"
        assert a["imaris_nul"]["value"] == ["a", "", "b"]
        assert a["s1_trail"]["value"] == ["a", "b", "", ""] and a["s1_2d"]["shape"] == [2, 2]
        assert "data" in a["s3_nul"]
        assert a["u8s"] == {"datatype": {"class": "integer", "size": 1, "order": "little", "signed": False}, "value": 3}
        assert a["u64"]["value"] == ["18446744073709551615", "9007199254740992", 3]
        assert a["bits"]["datatype"]["class"] == "bitfield"
        assert a["null"] == {"datatype": {"class": "float", "size": 4, "order": "little"}, "shape": None}
        table = paths(out)
        assert a["refs"]["value"] == [table.index("/DataSet"), table.index("/X"), None]
        assert a["typed"] == {"named": table.index("/X/zT"), "value": [0, 1]}
        assert a["n\u00c3A"]["name"] == {"latin1": "n\u00c3A"} and a["n\u00e9"]["value"] == 2
        assert [c["name"] for c in x["attribute_collisions"]] == ["n\u00c3A", {"latin1": "n\u00e9"}]
        assert source("")["attributes"]["forward"]["value"] == table.index("/Z")
        assert x["names"] == {"lat\u00e9n": {"latin1": "lat\u00e9n"}}
        assert x["collisions"] == [{"latin1": "caf\u00e9"}]
        assert x["links"]["elsewhere"] == {"external": {"file": "other.h5", "path": "/data"}}
        assert x["datatypes"]["zT"] == {"datatype": i16, "attributes": {"note": {
            "datatype": {"class": "integer", "size": 4, "order": "little", "signed": True}, "value": 5}}}
        assert "zarr.json type" in x["datatypes"]
        u = x["unsupported"]
        assert set(u) == {"__reserved", "external", "zarr.json"}
        assert u["external"]["external"] == [{"file": "external.bin", "offset": 0, "size": 16}]
        assert u["external"]["attributes"]["kept"]["value"] == 1 and u["zarr.json"]["attributes"]["kept"]["value"] == 1
        assert source("X/c/")["unsupported"]["zarr.json"]["shape"] == [3]
        assert source("X/a_typed/") == {"named": table.index("/X/zT")}
        assert source("X/null/") == {"datatype": {"class": "float", "size": 4, "order": "little"}, "shape": None}
        # References are indexes into the object table, which lists each object once, in the walk's order.
        assert doc("X/refs/")["data_type"] == "int32" and doc("X/refs/")["shape"] == [4]
        refs = struct.unpack("<4i", out.bytes_entries["vzip_source/hdf5/X/refs/c/0"])
        table = paths(out)
        assert [table[i] if i >= 0 else None for i in refs] == ["/DataSet", "/X", "/Z", None]
        assert source("X/shuffled/") == {"datatype": {"class": "integer", "size": 4, "order": "little", "signed": True},
                                         "attributes": {"kept": {"datatype": {"class": "variable-length", "size": 16,
                                         "base": {"class": "integer", "size": 1, "order": "little", "signed": False},
                                         "string": True, "padding": "null-terminated", "charset": "utf-8"},
                                                                 "value": "yes"}}}
        assert doc("X/cvlen/")["node_type"] == "group" and source("X/regions/")["shape"] == [1]
        for name in ("allempty", "noelements"):
            assert doc(f"X/{name}/")["node_type"] == "group" and source(f"X/{name}/")["shape"] in ([2], [0])
        strfill, negzero = doc("X/strfill/"), doc("X/negzero/")
        assert strfill["fill_value"] == 0 and source("X/strfill/")["fill"] == "Ti9BAA=="
        assert negzero["fill_value"] == 0 and source("X/negzero/")["fill"] == "gAAAAAAAAAA="
        assert doc("X/nanfill/")["fill_value"] == "NaN" and doc("X/f32fill/")["fill_value"] == 0.10000000149011612
        # The protocol texts over the document's budget are arrays; the small attribute after them is not.
        custom = out.bytes_entries["vzip_source/hdf5/DataSetInfo/CustomData/zarr.json"]
        held = source("DataSetInfo/CustomData/")["attributes"]
        assert [k for k, v in held.items() if isinstance(v, dict) and "array" in v] == [
            f"Protocol {i:02d}" for i in range(8, 12)]
        assert held["Small"]["value"] == 1 and len(custom) < (1 << 16) + 4096


def test_source_node_keeps_integer_fill_values_beyond_2_53():
    _, out = virtualize(str(FIXTURES / "exact" / "ims_big_fill.ims"), url="https://data.test/x")
    fills = {n: json.loads(out.bytes_entries[f"vzip_source/hdf5/{n}/zarr.json"])["fill_value"]
             for n in ("bigfill", "u64fill", "negfill")}
    assert fills == {"bigfill": 2**60 + 1, "u64fill": 2**64 - 1, "negfill": -2**63}
    assert b'"fill_value":18446744073709551615' in out.bytes_entries["vzip_source/hdf5/u64fill/zarr.json"]


@pytest.mark.parametrize("max_objects,per_image,overflow", [
    (100000, 5, {}),
    (5, 5, {}),  # 5 + 5 x 12 = 65: every object
    (4, 5, {"Thumbnail": 1}),  # the last object met, Thumbnail/Data, is beyond
    # A cap that does not grow with the image: its tree uses up the walk before DataSetInfo's members.
    (8, 4, {"DataSet/ResolutionLevel 1/TimePoint 2/Channel 0": 1, "DataSet/ResolutionLevel 1/TimePoint 2/Channel 1": 3,
            "DataSetInfo": 4, "Thumbnail": 1}),
])
def test_source_node_counts_the_members_beyond_the_object_limit(monkeypatch, max_objects, per_image, overflow):
    """The walk meets at most MAX_OBJECTS + PER_IMAGE x (Data datasets) objects (spec/virtualize/ims.md
    §5.1), so that the image's tree leaves DataSetInfo and Thumbnail room; the members beyond are counted.
    The time-lapse has 12 Data datasets (2 levels, 3 time points, 2 channels), each with Histogram and
    Histogram1024: 57 objects of the image's tree and 8 others, the root included."""
    from vzip.virtualize.ims import source

    monkeypatch.setattr(source, "MAX_OBJECTS", max_objects)
    monkeypatch.setattr(source, "PER_IMAGE", per_image)
    _, out = virtualize(str(FIXTURES / "ims_latest_timelapse.ims"), url="https://data.test/x")
    found = {}
    for k, v in out.bytes_entries.items():
        if k.startswith("vzip_source/hdf5") and k.endswith("zarr.json"):
            s = json.loads(v)["attributes"].get("vzip_virtualized", {}).get("ims", {})
            assert "unsupported" not in s
            if "overflow" in s:
                found[k[len("vzip_source/hdf5/") : -len("zarr.json")].rstrip("/")] = s["overflow"]
    assert found == overflow


def test_source_node_keeps_what_the_second_review_found():
    """Documents over their budget, shared datatypes, references at any depth, compound
    datasets with variable-length members, filters, region references, virtual datasets
    and NaNs (spec/virtualize/ims.md §5); js/test/ims/verify.py rebuilds them all."""
    for libver in ("earliest", "latest"):
        _, out = virtualize(str(FIXTURES / f"ims_{libver}_review2.ims"), url="https://data.test/x")
        e = out.bytes_entries
        doc = lambda p: json.loads(e[f"vzip_source/hdf5/{p}zarr.json"])  # noqa: E731
        source = lambda p: doc(p)["attributes"]["vzip_virtualized"]["ims"]  # noqa: E731
        table = paths(out)
        deep = "/Deep" + "".join(f"/{'n' * 200}{i}" for i in range(4))
        # A shared datatype is described once, in its entry of datatypes; its users name it.
        assert source("")["datatypes"]["T"]["datatype"]["class"] == "compound"
        shared = source("Shared/")["attributes"]
        assert all(set(v) == {"named", "data", "shape"} and v["named"] == table.index("/T") for v in shared.values())
        assert len(shared) == 120
        # References: indexes into the object table; more than 64 of them are an array.
        refs = source("Refs/")["attributes"]
        assert refs["few"]["value"] == [table.index(deep), None]
        assert refs["many"] == {"datatype": {"class": "reference", "size": 8, "kind": 0}, "array": "attributes/1"}
        assert set(struct.unpack("<300i", e["vzip_source/attributes/1/c/0"])) == {table.index(deep)}
        assert doc("Refs/refs/")["data_type"] == "int32" and len(e["vzip_source/hdf5/Refs/refs/c/0"]) == 600
        # Hard links are indexes into the object table, so long paths do not repeat.
        assert source("Hard/") == {"links": {f"l{i:03d}": {"hard": table.index(deep)} for i in range(100)}}
        # Entries over the budget are spilled, in order, to a family of JSON texts.
        many = source("Many/")
        assert len(e["vzip_source/hdf5/Many/zarr.json"]) < (1 << 16) + 4096 and many["spilled"] == "spilled/0"
        spilled = [json.loads(m) for m in family(out, "spilled/0")]
        held = {**many["attributes"], **{k: v for m in spilled for k, v in m["attributes"].items()}}
        assert list(held) == [f"{'k' * 250}{i:03d}" for i in range(300)] and len(many["attributes"]) + len(spilled) == 300
        # Each held as JSON, but for the one whose entry first did not fit, which is in the array form.
        i8 = {"class": "integer", "size": 1, "order": "little", "signed": True}
        assert [i for i, v in enumerate(held.values()) if v != {"datatype": i8, "value": i % 100}] == [len(many["attributes"])]
        assert spilled[0]["attributes"][f"{'k' * 250}{len(many['attributes']):03d}"] == {"datatype": i8, "array": "attributes/0", "shape": []}
        # A string datatype of no bytes holds empty texts.
        if libver == "earliest":
            assert source("Empty/")["attributes"]["zz"] == {
                "datatype": {"class": "string", "size": 0, "padding": "null-padded", "charset": "ascii"},
                "value": ["", "", ""]}
        # Dimension scales and references inside other datatypes.
        index = table.index
        assert source("X/withdim/")["attributes"]["DIMENSION_LIST"]["value"] == [[index("/X/scale")]]
        assert source("X/scale/")["attributes"]["REFERENCE_LIST"]["value"] == [
            {"dataset": index("/X/withdim"), "dimension": 0}]
        a = source("X/")["attributes"]
        assert a["nested"]["value"] == [{"a": {"i": 1, "r": index("/X")}, "b": [index("/X"), index("/X/scale")]}]
        assert a["region"]["value"] == {"object": index("/X/target"), "selection": {
            "select": "hyperslab", "rank": 2, **({"start": [0, 5], "stride": [1, 1], "count": [1, 1], "block": [2, 1]}
                                                 if libver == "latest" else {"blocks": [[[0, 5], [1, 5]]]})}}
        assert a["points"]["value"]["selection"]["points"] == [[0, 1], [5, 9], [2, 2]]
        # NaNs: only the canonical one, and no negative zero, as JSON numbers.
        assert a["nan"]["value"] == ["NaN", 1.5] and "data" in a["nan_payload"] and "data" in a["negzero"]
        # A compound with variable-length and reference members: its bytes, and a column per such member.
        assert doc("X/cvlen/")["node_type"] == "group" and doc("X/cvlen/data/")["shape"] == [3, 44]
        assert family(out, "hdf5/X/cvlen/1", (FIXTURES / f"ims_{libver}_review2.ims").read_bytes()) == [b"x", b"yy", b""]
        assert doc("X/cvlen/3/")["data_type"] == "int32"
        assert family(out, "hdf5/X/regions")[1] == b'{"object":%d,"selection":{"select":"all"}}' % index("/X/target")
        # Filters: Fletcher32's checksum left out of each chunk, shuffled chunks decoded and copied.
        assert [r[0][1] for k, r in sorted(out.refs.items()) if k.startswith("vzip_source/hdf5/X/fletcher/c/")] == [120] * 4
        assert doc("X/fletcher_deflate/")["codecs"][-1]["name"] == "zlib"
        assert struct.unpack(">30d", e["vzip_source/hdf5/X/shuffle_all/c/0"]) == tuple(range(30))
        u = source("X/")["unsupported"]
        assert u["scaleoffset"]["reason"] == "unsupported HDF5 filters [6]"
        assert u["virtual"]["virtual"] == [
            {"file": ".", "dataset": "/X/withdim", "source": {"select": "all"},
             "selection": u["virtual"]["virtual"][0]["selection"]},
            {"file": "other.h5", "dataset": "/data", "source": {"select": "all"},
             "selection": u["virtual"]["virtual"][1]["selection"]}]


def test_source_node_keeps_what_the_third_review_found():
    """Region references as an index and a selection, read once; object references as
    indexes in a spilled document; variable-length data that HDF5 left unshuffled
    (filter mask bit 0); tiny chunks indexed implicitly; attribute data referenced
    where the file holds it; the image datasets' shapes; and the names and channels
    the root lists (spec/virtualize/ims.md §4, §5); js/test/ims/verify.py rebuilds
    them all, and js/test/ims/review.test.ts has the same cases."""
    path = FIXTURES / "ims_latest_review3.ims"
    raw = path.read_bytes()
    _, out = virtualize(str(path), url="https://data.test/x")
    e = out.bytes_entries
    doc = lambda p: json.loads(e[f"vzip_source/hdf5/{p}zarr.json"])  # noqa: E731
    source = lambda p: doc(p)["attributes"]["vzip_virtualized"]["ims"]  # noqa: E731
    table = paths(out)
    d = table.index("/Deep" + "".join(f"/{'n' * 200}{i}" for i in range(4)) + "/d")
    selection = {"select": "hyperslab", "rank": 2, "start": [1, 2], "stride": [1, 2], "count": [1, 3], "block": [2, 1]}
    assert family(out, "hdf5/R/regions") == [canonical({"object": d, "selection": selection}).encode()] * 40
    r = source("R/")
    assert r["attributes"]["region"]["value"]["object"] == d
    assert set(r["unsupported"]) == {"bigchunk", "over"}
    # Object references in a spilled entry: indexes, as in the document.
    spill = source("Spill/")
    spilled = [json.loads(m) for m in family(out, spill["spilled"])]
    held = {k: v for m in [spill, *spilled] for k, v in m.get("attributes", {}).items()}
    assert "zz" not in spill["attributes"] and held["zz"]["value"] == [d, None, table.index("/Deep" + "".join(f"/{'n' * 200}{i}" for i in range(4)))]
    # Shuffled variable-length data: its chunks skip the shuffle, and are decoded without it.
    assert family(out, "hdf5/R/names", raw) == [b"a", b"bb", b"", b"dddd"]
    # Tiny chunks indexed implicitly: contiguous values, a chunk per row, or the chunks themselves.
    grid = {n: (doc(f"R/{n}/")["chunk_grid"]["configuration"]["chunk_shape"],
                sum(k.startswith(f"vzip_source/hdf5/R/{n}/c/") for k in out.refs)) for n in
            ("tiny", "slabs", "rows", "gaps", "blocks")}
    assert grid == {"tiny": ([1000], 1), "slabs": ([6, 4], 1), "rows": ([1, 5], 3), "gaps": ([1, 4], 3),
                    "blocks": ([2, 2], 4)}
    # Attribute data in the array form: referenced where the file holds it.
    arrays = {"header": (r["attributes"]["header"]["array"], np.arange(100, dtype=">i2")),
              "managed": (held["managed"]["array"], np.arange(200, dtype="<f8")),
              "huge": (held["huge"]["array"], np.arange(20000, dtype="<u4"))}
    for name, (array, values) in arrays.items():
        ranges = out.refs[f"vzip_source/{array}/c/0"]
        assert b"".join(raw[o : o + n] for o, n in ranges) == values.tobytes(), name
    channel = source("DataSet/ResolutionLevel 0/TimePoint 0/Channel 0/")
    assert channel["images"]["Data"] == {"level": 0, "t": 0, "c": 0, "shape": [2, 8, 8]}
    # The root: a name or label over 256 bytes is left out; more than 64 channels, no omero.
    roots = {n: json.loads(virtualize(str(FIXTURES / f"ims_latest_{n}.ims"), url="https://data.test/x")[1]
                           .bytes_entries["zarr.json"])["attributes"]["ome"] for n in ("long_names", "many_channels")}
    assert "name" not in roots["long_names"]["multiscales"][0]
    assert [c["label"] for c in roots["long_names"]["omero"]["channels"]] == ["Channel 0", "\u00e9" * 128]
    assert "omero" not in roots["many_channels"] and roots["many_channels"]["multiscales"][0]["name"] == "y" * 256


def array(out, path: str, source: bytes) -> np.ndarray:
    """A numeric array under vzip_source, its chunks copied or referenced in `source` (zlib or raw)."""
    import zlib

    e = out.bytes_entries
    doc = json.loads(e[f"vzip_source/{path}/zarr.json"])
    shape, chunk = doc["shape"], doc["chunk_grid"]["configuration"]["chunk_shape"]
    dtype = np.dtype(doc["data_type"]).newbyteorder("<" if doc["codecs"][0].get("configuration", {}).get("endian") != "big" else ">")
    zipped = any(c["name"] == "zlib" for c in doc["codecs"])
    grid = [-(-n // c) for n, c in zip(shape, chunk)]
    full = np.zeros([g * c for g, c in zip(grid, chunk)], dtype)
    for index in np.ndindex(*grid):
        key = f"vzip_source/{path}/c/" + "/".join(map(str, index))
        if key in e:
            b = e[key]
        else:
            b = b"".join(source[o : o + n] for o, n in out.refs[key])
            b = zlib.decompress(b) if zipped else b
        full[tuple(slice(i * c, (i + 1) * c) for i, c in zip(index, chunk))] = np.frombuffer(b, dtype).reshape(chunk)
    return full[tuple(slice(0, n) for n in shape)]


def test_source_node_copies_partial_edge_chunks_stored_unfiltered():
    """Datasets whose partial edge chunks HDF5 stores unfiltered (H5Pset_chunk_opts): those chunks
    are copied raw and the others decoded, under every chunk index and with shuffle and
    Fletcher32; without a partial chunk, the chunks are referenced (spec/virtualize/ims.md §5.4)."""
    path = FIXTURES / "ims_latest_raw_edges.ims"
    raw = path.read_bytes()
    _, out = virtualize(str(path), url="https://data.test/x")
    group = json.loads(out.bytes_entries["vzip_source/hdf5/E/zarr.json"])
    assert "vzip_virtualized" not in group["attributes"]  # nothing unsupported
    expected = {"fixed": np.arange(10), "extensible": np.arange(10), "btree": np.arange(70).reshape(10, 7),
                "single": np.arange(3), "whole": np.arange(8), "pipeline": np.linspace(0, 1, 70).reshape(10, 7)}
    for name, values in expected.items():
        assert np.array_equal(array(out, f"hdf5/E/{name}", raw), values), name
    copied = {name: f"vzip_source/hdf5/E/{name}/c/0" if name in ("fixed", "extensible", "single", "whole")
              else f"vzip_source/hdf5/E/{name}/c/0/0" for name in expected}
    assert {name: key in out.bytes_entries for name, key in copied.items()} == {
        "fixed": True, "extensible": True, "btree": True, "single": True, "whole": False, "pipeline": True}
    assert family(out, "hdf5/E/names", raw) == [b"a", b"bb", b"", b"dddd", b"e"]


def test_source_node_lists_region_references_over_the_budget_of_selections():
    """400 references to a selection of about 20 KB: the dataset is unsupported, and what
    it read does not count against the references after it (spec/virtualize/ims.md §5.4)."""
    _, out = virtualize(str(FIXTURES / "ims_latest_review3.ims"), url="https://data.test/x")
    r = json.loads(out.bytes_entries["vzip_source/hdf5/R/zarr.json"])["attributes"]["vzip_virtualized"]["ims"]
    assert r["unsupported"]["over"]["reason"] == "over the budget of region selections"
    assert r["attributes"]["region"]["value"]["selection"]["select"] == "hyperslab"


def test_source_node_lists_references_in_a_chunk_too_large_to_decode():
    _, out = virtualize(str(FIXTURES / "ims_latest_review3.ims"), url="https://data.test/x")
    r = json.loads(out.bytes_entries["vzip_source/hdf5/R/zarr.json"])["attributes"]["vzip_virtualized"]["ims"]
    assert r["unsupported"]["bigchunk"]["reason"] == "a chunk of more than 2^24 bytes to decode"


def test_source_node_reads_every_chunk_index():
    """Extensible arrays, version 2 B-trees, implicit and fixed array indexes at maximum
    dimensions (spec/virtualize/ims/profile.md §8.5); js/test/ims/verify.py compares the data with h5py."""
    _, out = virtualize(str(FIXTURES / "ims_latest_indexes.ims"), url="https://data.test/x")
    group = json.loads(out.bytes_entries["vzip_source/hdf5/Indexes/zarr.json"])
    assert "vzip_virtualized" not in group["attributes"]  # nothing unsupported
    chunks = {name: sum(1 for k in [*out.refs, *out.bytes_entries] if k.startswith(f"vzip_source/hdf5/Indexes/{name}/c/"))
              for name in ("ea", "ea_last", "ea_deflate", "bt2", "bt2_deflate", "implicit", "implicit_max", "farray_max")}
    # The implicit index's chunks of 10 elements hold its 95 in row-major order: one array chunk.
    assert chunks == {"ea": 600, "ea_last": 2 * 14, "ea_deflate": 72, "bt2": 15 * 17, "bt2_deflate": 4, "implicit": 1,
                      "implicit_max": 4, "farray_max": 9}


def test_json_text():
    """The JSON text whose size a document's budget counts (spec/virtualize/ims.md §5.6);
    js/test/ims/source.test.ts has the same cases."""
    from vzip.virtualize.ims.source import canonical, size

    cases = [
        (None, "null"), (True, "true"), (0, "0"), (-0.0, "0"), (2.0, "2"), (1.5, "1.5"), (1e-7, "1e-7"),
        (0.000001, "0.000001"), (1e20, "100000000000000000000"), (1e21, "1e+21"), (-1.25e-300, "-1.25e-300"),
        (0.1, "0.1"), (123456.789, "123456.789"), (2**53 + 0.0, "9007199254740992"), (5e-324, "5e-324"),
        (1.2345678901234567e19, "12345678901234567000"), (-(2.0**60), "-1152921504606847000"),
        ("a\"\\\n\x01é", '"a\\"\\\\\\n\\u0001é"'), ([1, "x"], '[1,"x"]'),
        ({"b": 1, "a": {"é": [], "z": None}, "B": 2}, '{"B":2,"a":{"z":null,"é":[]},"b":1}'),
    ]
    for value, text in cases:
        assert canonical(value) == text, value
    assert size({"é": 1}) == 8


def test_parse_selection():
    """Serialized selections of each type and version (spec/virtualize/ims/profile.md §8.9);
    js/test/ims/source.test.ts has the same cases."""
    from vzip.virtualize.ims.hdf5 import parse_selection

    u32 = lambda *v: b"".join(x.to_bytes(4, "little") for x in v)  # noqa: E731
    u64 = lambda *v: b"".join(x.to_bytes(8, "little") for x in v)  # noqa: E731
    cases = [
        (u32(3, 1, 0, 0), {"select": "all"}),
        (u32(0, 1, 0, 0), {"select": "none"}),
        (u32(1, 1, 0, 0, 2, 2, 1, 2, 3, 4), {"select": "points", "rank": 2, "points": [[1, 2], [3, 4]]}),
        (u32(1, 2) + bytes([2]) + u32(1) + bytes([1, 0, 5, 0]), {"select": "points", "rank": 1, "points": [[5]]}),
        (u32(2, 1, 0, 0, 1, 1, 2, 7), {"select": "hyperslab", "rank": 1, "blocks": [[[2], [7]]]}),
        (u32(2, 2) + bytes([1]) + u32(0, 1) + u64(1, 2, 3, 2**64 - 1),
         {"select": "hyperslab", "rank": 1, "start": [1], "stride": [2], "count": [3], "block": ["unlimited"]}),
        (u32(2, 3) + bytes([0, 8]) + u32(1) + u64(1, 2**60, 2**60 + 1),
         {"select": "hyperslab", "rank": 1, "blocks": [[[str(2**60)], [str(2**60 + 1)]]]}),
    ]
    for data, selection in cases:
        assert parse_selection(data + b"rest", 0) == (selection, len(data)), selection


def test_parse_selection_rejects_an_unknown_type():
    from vzip.virtualize.ims.hdf5 import parse_selection

    with pytest.raises(Rejected, match="unknown selection type 4"):
        parse_selection(bytes([4, 0, 0, 0, 1, 0, 0, 0]), 0)


def test_parse_selection_rejects_an_unknown_version():
    from vzip.virtualize.ims.hdf5 import parse_selection

    with pytest.raises(Rejected, match="unsupported selection version 4"):
        parse_selection(bytes([2, 0, 0, 0, 4, 0, 0, 0]), 0)


def test_parse_selection_rejects_an_unknown_encoding_size():
    from vzip.virtualize.ims.hdf5 import parse_selection

    with pytest.raises(Rejected, match="invalid selection encoding size 3"):
        parse_selection(bytes([2, 0, 0, 0, 3, 0, 0, 0, 0, 3, 1, 0, 0, 0]), 0)


def test_parse_selection_rejects_unknown_flags():
    from vzip.virtualize.ims.hdf5 import parse_selection

    with pytest.raises(Rejected, match="unknown selection flags"):
        parse_selection(bytes([2, 0, 0, 0, 3, 0, 0, 0, 2, 8, 1, 0, 0, 0]), 0)


def test_parse_selection_rejects_rank_0():
    """Points and irregular hyperslabs of rank 0, whose count no bytes bound."""
    from vzip.virtualize.ims.hdf5 import parse_selection

    for data in (bytes([1, 0, 0, 0, 1, 0, 0, 0, *[0] * 8, 0, 0, 0, 0, 255, 255, 255, 255]),
                 bytes([2, 0, 0, 0, 3, 0, 0, 0, 0, 8, 0, 0, 0, 0, *[0, 0, 0, 0, 4, 0, 0, 0]])):
        with pytest.raises(Rejected, match="a selection of rank 0"):
            parse_selection(data, 0)


def test_parse_selection_rejects_a_truncated_selection():
    from vzip.virtualize.ims.hdf5 import parse_selection

    with pytest.raises(Rejected, match="truncated selection"):
        parse_selection(bytes([1, 0, 0, 0, 2, 0, 0, 0, 4, 1, 0, 0, 0, 9, 0, 0, 0]), 0)


def _dataset(dims, maxdims, chunk):
    from vzip.virtualize.ims.hdf5 import Dataset, Datatype

    grid = [-(-n // c) for n, c in zip(dims, chunk)]
    return Dataset(dims, maxdims, Datatype(0, 1, 0, b""), chunk, grid, [], "farray", None, None)


def test_max_grid():
    """The chunk counts at the maximum dimensions (spec/virtualize/ims/profile.md §8.5)."""
    from vzip.virtualize.ims.hdf5 import UNDEFINED, _max_grid

    assert _max_grid(_dataset([5, 6], None, [2, 4]), False) == [3, 2]
    assert _max_grid(_dataset([5, 6], [9, 6], [2, 4]), False) == [5, 2]
    assert _max_grid(_dataset([5, 6], [UNDEFINED, 6], [2, 4]), True) == [None, 2]


def test_max_grid_rejects_a_maximum_below_the_size():
    from vzip.virtualize.ims.hdf5 import _max_grid

    with pytest.raises(Rejected, match="maximum dimension"):
        _max_grid(_dataset([5, 6], [4, 6], [2, 4]), False)


def test_max_grid_rejects_an_unlimited_dimension_the_index_does_not_allow():
    from vzip.virtualize.ims.hdf5 import UNDEFINED, _max_grid

    with pytest.raises(Rejected, match="maximum dimension"):
        _max_grid(_dataset([5, 6], [UNDEFINED, 6], [2, 4]), False)
