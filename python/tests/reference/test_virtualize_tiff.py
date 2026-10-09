"""The Python TIFF virtualizer (spec/virtualize/tiff/profile.md) on the synthetic fixtures.

Equivalence with the browser virtualizer is checked by
conformance/virtualize/compare.py, and pixel correctness by
js/test/tiff/verify.py.
"""

import sys
from pathlib import Path

# the frozen reference twins (conformance/virtualize/reference/vzip_reference)
sys.path.insert(0, str(Path(__file__).parents[3] / "conformance" / "virtualize" / "reference"))

import json
import struct
from pathlib import Path

import pytest
import tifffile

from vzip_reference.tiff.tags import LAYOUT, json_size

from vzip.virtualize import Rejected
from vzip_reference import virtualize

FIXTURES = Path(__file__).parents[3] / "fixtures" / "tiff"


def test_virtualizes_the_synthetic_files():
    # (summary members, whether the ImageDescription is OME-XML)
    cases = {
        "rgb_planar_jpeg2000_bigtiff_be.ome.tif": (
            {"axes": ["c", "y", "x"], "levels": [[3, 96, 128], [3, 48, 64], [3, 24, 32]]}, True),
        "tczyx_uint16_deflate.ome.tif": ({"axes": ["t", "c", "z", "y", "x"]}, True),
        "svs_like_int16.tif": ({"axes": ["y", "x"], "levels": [[96, 128], [48, 64]]}, False),
        "edge_ome_invalid_utf8.tif": ({}, False),
    }
    for name, (expected, ome_xml) in cases.items():
        fmt, out = virtualize(str(FIXTURES / name), url=f"https://data.test/{name}")
        assert fmt == "tiff", name
        assert {**out.summary, **expected} == out.summary, name
        assert out.url == f"https://data.test/{name}"
        prop = json.loads(out.bytes_entries["zarr.json"])["attributes"]["vzip_virtualized"]
        node = source(out, "")
        # Every IFD's tags but the layout and pointer ones, by number, with tifffile's
        # types and counts, each IFD on its own group of vzip_source (spec/virtualize/tiff.md §5).
        with tifffile.TiffFile(FIXTURES / name) as tf:
            assert node == {"ifd_count": len(tf.pages)}, name
            for i, page in enumerate(tf.pages):
                ifd = source(out, f"ifds/{i}")
                assert {k: (v["type"], v["count"]) for k, v in ifd["tags"].items()} == {
                    str(t.code): (int(t.dtype), t.count) for t in page.tags
                    if t.code not in LAYOUT and str(t.code) not in ifd.get("pointers", {})}, name
            assert prop["tiff"] == {"byte_order": "little" if tf.byteorder == "<" else "big",
                                    "bigtiff": tf.is_bigtiff}, name
            if ome_xml:
                assert source(out, "ifds/0")["tags"]["270"]["value"] == tf.pages[0].description, name


def source(out, path: str) -> dict:
    """The source metadata of a node of vzip_source."""
    doc = json.loads(out.bytes_entries["/".join(["vzip_source", *filter(None, [path]), "zarr.json"])])
    return doc["attributes"].get("vzip_virtualized", {}).get("tiff", {})


def test_records_every_ifd_in_its_own_group():
    """spec/virtualize/tiff.md §5: pointer tags, nested SubIFDs, a shared EXIF IFD,
    duplicate tags, an unknown field type, a JPEG interchange stream, contiguous strips."""
    _, out = virtualize(str(FIXTURES / "edge_pointers.tif"), url="https://data.test/p.tif")
    ifd0 = source(out, "ifds/0")
    assert ifd0["pointers"] == {
        "330": {"type": 4, "count": 1, "ifds": ["ifds/0/subifds/0"]},
        "400": {"type": 13, "count": 1, "ifds": ["ifds/0/ifd_400"]},
        "34665": {"type": 4, "count": 1, "ifds": ["ifds/0/exif"]},
        "34853": {"type": 4, "count": 1, "ifds": [None]},  # outside the file
        "65000": {"type": 13, "count": 2, "ifds": ["ifds/0/ifd_400", "ifds/0/ifd_65000/1"]},
    }
    assert ifd0["tags"]["305"]["value"] == "first"
    assert ifd0["tags"]["65100"] == {"type": 99, "count": 4, "field": "AQIDBA=="}
    assert ifd0["duplicates"] == [{"tag": 305, "type": 2, "count": 7, "value": "second"},
                                  {"tag": 305, "type": 3, "count": 70}]
    assert "vzip_source/ifds/0/duplicates/1/zarr.json" in out.bytes_entries
    assert source(out, "ifds/0/subifds/0")["pointers"]["330"]["ifds"] == ["ifds/0/subifds/0/subifds/0"]
    assert source(out, "ifds/0/subifds/0/subifds/0")["tags"]["256"]["value"] == [16]
    assert source(out, "ifds/0/exif")["pointers"]["40965"]["ifds"] == ["ifds/0/exif/interoperability"]
    assert source(out, "ifds/0/exif/interoperability")["tags"] == {"1": {"type": 2, "count": 4, "value": "R98"}}
    assert source(out, "ifds/0/ifd_65000/1")["tags"]["305"]["value"] == "other"
    assert source(out, "ifds/0/ifd_65000") == {}  # a plain group holding the targets of a count above 1
    ifd1 = source(out, "ifds/1")
    assert ifd1["pointers"] == {"34665": {"type": 4, "count": 1, "ifds": ["ifds/0/exif"]}}  # shared
    assert not {"273", "279", "513", "514"} & set(ifd1["tags"])
    data = (FIXTURES / "edge_pointers.tif").read_bytes()
    (start, n), = out.refs["vzip_source/ifds/1/jpeg_interchange/c/0"]
    assert data[start:start + n].startswith(b"\xff\xd8") and data[start + n - 2:start + n] == b"\xff\xd9"
    # Three contiguous strips are one range of the data chunk (conventions §7).
    (start, n), = out.refs["vzip_source/ifds/1/data/data/c/0"]
    assert (n, data[start:start + n]) == (640, bytes([7]) * 640)

    # A pointer tag of 71 values: an int32 array of record numbers (IFDs 0 and 1 are 0 and
    # 1, GlobalParametersIFD 2, the new targets from 3), -1 for an IFD of no entries, one
    # inside another's extent and one outside the file; the IFDs it refers to have their number.
    _, out = virtualize(str(FIXTURES / "edge_pointer_list.tif"), url="https://data.test/p.tif")
    ifd0 = source(out, "ifds/0")
    assert ifd0["pointers"] == {"400": {"type": 4, "count": 1, "ifds": ["ifds/0/ifd_400"]},
                                "50001": {"type": 13, "count": 71}}
    numbers = struct.unpack("<71i", out.bytes_entries["vzip_source/ifds/0/50001/c/0"])
    assert numbers == (*range(3, 69), -1, -1, 3, -1, 1)
    assert json.loads(out.bytes_entries["vzip_source/ifds/0/50001/zarr.json"])["data_type"] == "int32"
    assert source(out, "ifds/0/ifd_50001/0") == {"tags": {"305": {"type": 2, "count": 3, "value": "t0"}}, "record": 3}
    assert source(out, "ifds/1")["record"] == 1 and "record" not in source(out, "ifds/0/ifd_400")
    assert "vzip_source/ifds/0/ifd_50001/66/zarr.json" not in out.bytes_entries  # no entries: not recorded
    assert source(out, "ifds/0/ifd_400")["tags"]["305"]["value"] == "gp"
    # Old-style JPEG tables are layout: families of 64-byte quantization tables and of
    # Huffman tables (16 counts and their sum of values); a table outside the file is absent.
    data = (FIXTURES / "edge_pointer_list.tif").read_bytes()
    assert not {"519", "520", "521"} & set(source(out, "ifds/1")["tags"])
    for name, shape, n in (("jpeg_q_tables", [2, 64], 128), ("jpeg_dc_tables", [1, 28], 28),
                           ("jpeg_ac_tables", [1, 22], 22)):
        assert json.loads(out.bytes_entries[f"vzip_source/ifds/1/{name}/zarr.json"])["shape"] == shape, name
        (start, length), = out.refs[f"vzip_source/ifds/1/{name}/c/0/0"]
        assert length == n, name
    assert data[start:start + 4] == bytes([0, 2, 1, 3])
    # At most 10000 IFDs are recorded through pointer tags.
    _, out = virtualize(str(FIXTURES / "edge_pointer_limit.tif"), url="https://data.test/p.tif")
    numbers = struct.unpack("<10001i", out.bytes_entries["vzip_source/ifds/0/50001/c/0"])
    assert numbers == (*range(1, 10001), -1)


def test_no_document_grows_with_the_ifds(tmp_path):
    """One group per IFD: vzip_source's own document is the same for 2 and 200 IFDs."""
    import numpy as np

    sizes = []
    # 1500 planes with stage positions: an OME-XML of more than 2^16 bytes, an array.
    path = tmp_path / "positions.ome.tif"
    tifffile.imwrite(path, np.zeros((1500, 16, 16), np.uint8), ome=True, tile=(16, 16),
                     metadata={"axes": "ZYX", "Plane": {"PositionZ": [float(i) for i in range(1500)]}})
    _, out = virtualize(str(path), url="https://data.test/n.ome.tif")
    assert "value" not in source(out, "ifds/0")["tags"]["270"] and "vzip_source/ifds/0/270/zarr.json" in out.bytes_entries
    for n in (2, 200):
        path = tmp_path / f"n{n}.ome.tif"
        tifffile.imwrite(path, np.zeros((n, 16, 16), np.uint8), ome=True, tile=(16, 16), metadata={"axes": "ZYX"})
        _, out = virtualize(str(path), url="https://data.test/n.ome.tif")
        assert source(out, "") == {"ifd_count": n}
        sizes.append(len(out.bytes_entries["vzip_source/zarr.json"]))
        assert max(len(v) for k, v in out.bytes_entries.items() if k.startswith("vzip_source/ifds/")) < 2000
    assert sizes[0] == sizes[1] - 2  # "ifd_count": 2 vs 200


@pytest.mark.parametrize("name,message", [
    ("unsupported_lzw.tif", "compression 5"),
    ("unsupported_predictor.tif", "predictor 2"),
    ("unsupported_strips.tif", "strips"),
    ("edge_reject_fill_order.tif", "FillOrder 2"),
    ("edge_reject_ycbcr_subsampled.tif", r"YCbCr with subsampling \[2, 2\]"),
    ("edge_reject_ycbcr_default.tif", r"YCbCr with subsampling \[2, 2\]"),
    ("edge_reject_tiffdata_steps.tif", "cover more than 1008 planes"),
    ("edge_reject_unused_value_outside.tif", "tag 324 is outside the file"),
])
def test_rejects(name, message):
    with pytest.raises(Rejected, match=message):
        virtualize(str(FIXTURES / name), url="https://data.test/x")


def test_jpeg_prefix_is_a_data_source():
    # spec/virtualize/tiff/profile.md §3.3: every JPEG tile is its first 2 bytes (SOI), its IFD's prefix P,
    # a data source (one per distinct P, in order of first use), then the rest of the tile:
    # every byte of the tile is referenced. Without a prefix (one sample, no JPEGTables),
    # the tile is one range.
    _, out = virtualize(str(FIXTURES / "jpeg_gray.tif"), url="https://data.test/x.tif")
    assert not out.data and all(len(r) == 1 for k, r in out.refs.items() if not k.startswith("vzip_source/"))
    for name in ("jpeg_aperio_rgb.tif", "jpeg_ycbcr.tif"):
        _, out = virtualize(str(FIXTURES / name), url="https://data.test/x.tif")
        data = list(out.data)
        assert data and not any(d.startswith(b"\xff\xd8") for d in data), name
        used = set()
        for key, ranges in out.refs.items():
            if key.startswith("vzip_source/"):
                continue
            soi, prefix, rest = ranges
            assert len(prefix) == 3 and prefix[1:] == (0, len(data[prefix[0] - 1])), key
            assert soi[1] == 2 and rest[0] == soi[0] + 2, key
            used.add(prefix[0])
        assert used == set(range(1, len(data) + 1)), name


def test_resolution_tags_give_a_pixel_size_only_when_it_is_microscopic(tmp_path):
    """An explicit ResolutionUnit and a pixel under 25.4 µm (spec/virtualize/tiff.md §4.4)."""
    import numpy as np

    cases = [
        ({"resolution": (72, 72), "resolutionunit": 2}, None),  # a document's 72 dpi
        ({"resolution": (40000, 40000), "resolutionunit": 3}, 0.25),  # 40000 pixels per cm
        ({"resolution": (40000, 40000), "resolutionunit": 1}, None),  # no absolute unit
        ({"resolution": (101600, 101600)}, None),  # the unit left to its default: not used
    ]
    for i, (kwargs, size) in enumerate(cases):
        path = tmp_path / f"r{i}.tif"
        tifffile.imwrite(path, np.ones((32, 32), np.uint8), tile=(16, 16), metadata=None, **kwargs)
        if "resolutionunit" not in kwargs:  # tifffile always writes ResolutionUnit: retag it as private
            data = path.read_bytes()
            entry = b"\x28\x01\x03\x00\x01\x00\x00\x00\x02\x00"  # 296, SHORT, 1, 2
            assert entry in data
            path.write_bytes(data.replace(entry, b"\xff\xff" + entry[2:], 1))
        _, out = virtualize(str(path), url="https://data.test/r.tif")
        axes = json.loads(out.bytes_entries["zarr.json"])["attributes"]["ome"]["multiscales"][0]
        scale = axes["datasets"][0]["coordinateTransformations"][0]["scale"]
        units = [a.get("unit") for a in axes["axes"]]
        assert (scale[-1], units[-1]) == ((size, "micrometer") if size else (1, None)), kwargs


def test_bounds_the_source_metadata_and_shares_tables():
    """spec/virtualize/tiff.md §5: the budget of values as JSON, shared tables kept once,
    a tiled IFD's strips, the offsets tried through pointer tags; spec/virtualize/tiff/profile.md §3.1:
    the values of an IFD that is not used are not read."""
    # A value's size is its compact JSON's, numbers as ECMAScript writes them (from node).
    sizes = [([1e21, 1.5e-7, -0.0, 0.1, 123.456, 1e-6], 37),
             ([5e-324, 1.7976931348623157e308, -2.5e-7, 1e20, 123456789012345680000.0, 0.000123,
               3.4028234663852886e38, 0.10000000149011612], 136),
             ("a\x01\"\\\n\x7f\u2028é", 21), ({"latin1": "\xff"}, 15), ([None, "ifds/0"], 15),
             ([[1, 2], [3, 4]], 13), (["NaN", "18446744073709551615"], 30)]
    assert [json_size(v) for v, _ in sizes] == [n for _, n in sizes]

    # Two IFDs with the same strip, JPEGQTables and pointer entries: the second names the
    # first's arrays. IFD 0, a tiled image, keeps its strips; IFD 3 is not used, so its
    # LONG8 TileOffsets above 2^53 - 1 are not read (its member at offset 1 is kept).
    _, out = virtualize(str(FIXTURES / "edge_shared_tables.tif"), url="https://data.test/t.tif")
    assert source(out, "ifds/2")["same_as"] == {
        "50001": "ifds/1/50001", "jpeg_q_tables": "ifds/1/jpeg_q_tables", "data": "ifds/1/data"}
    assert "same_as" not in source(out, "ifds/1") and "same_as" not in source(out, "ifds/0")
    assert [k for k in [*out.bytes_entries, *out.refs] if k.startswith("vzip_source/ifds/2/")] == [
        "vzip_source/ifds/2/zarr.json"]
    (start, n), = out.refs["vzip_source/ifds/0/strips/c/0/0"]
    assert n == 64 and (FIXTURES / "edge_shared_tables.tif").read_bytes()[start:start + n] == bytes([9]) * 64
    assert out.refs["vzip_source/ifds/3/data/c/1/0"] == [(1, 1)]

    # IFD 1 holds 2^16 bytes of values as JSON exactly: two 30000-byte texts, a short
    # one, one of 900 escapes and a DOUBLE of 37 bytes; the third text, a later DOUBLE,
    # EXIF's ifds and the duplicate are arrays. IFD 2's texts spend the total budget, so
    # IFD 3's short text is an array.
    _, out = virtualize(str(FIXTURES / "edge_budget.tif"), url="https://data.test/b.tif")
    ifd1 = source(out, "ifds/1")
    assert {t for t, v in ifd1["tags"].items() if "value" in v} == {"1000", "1001", "1003", "1004", "1005", "1006"}
    assert ifd1["tags"]["1005"]["value"] == [1e21, 1.5e-7, 0.0, 0.1, 123.456, 1e-6]
    assert ifd1["pointers"] == {"34665": {"type": 4, "count": 1}} and ifd1["duplicates"][0]["count"] == 30000
    for array in ("1002", "1007", "34665", "duplicates/0"):
        assert f"vzip_source/ifds/1/{array}/zarr.json" in out.bytes_entries, array
    assert struct.unpack("<i", out.bytes_entries["vzip_source/ifds/1/34665/c/0"]) == (4,)  # ifds/1/exif, after the 4 main-chain IFDs
    assert source(out, "ifds/1/exif")["record"] == 4
    assert not any("value" in v for v in source(out, "ifds/2")["tags"].values())
    assert source(out, "ifds/3") == {"tags": {"305": {"type": 2, "count": 12}}}

    # Offsets tried: 100 values of one offset that leads to no IFD are one try; with 9998
    # others, the valid IFD after them is the 10000th try, and nothing after it is tried.
    _, out = virtualize(str(FIXTURES / "edge_pointer_tries.tif"), url="https://data.test/p.tif")
    numbers = struct.unpack("<10101i", out.bytes_entries["vzip_source/ifds/0/50001/c/0"])
    assert numbers == (-1,) * 10098 + (1, -1, -1)
