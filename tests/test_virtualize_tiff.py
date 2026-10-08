"""The Python TIFF virtualizer (profiles/tiff.md) on the synthetic fixtures.

Equivalence with the browser virtualizer is checked by
conformance/virtualize/compare.py, and pixel correctness by
web/test/tiff/verify.py.
"""

import json
from pathlib import Path

import pytest
import tifffile

from vzip.virtualize import Rejected, virtualize

FIXTURES = Path(__file__).parents[1] / "web" / "test" / "fixtures" / "tiff"


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
        # Every IFD's tags, by number, with tifffile's types and counts (conventions/tiff/README.md §5).
        with tifffile.TiffFile(FIXTURES / name) as tf:
            ifds = prop["tiff"]["ifds"]
            assert len(ifds) == len(tf.pages), name
            for ifd, page in zip(ifds, tf.pages):
                assert {k: (v["type"], v["count"]) for k, v in ifd["tags"].items()} == {
                    str(t.code): (int(t.dtype), t.count) for t in page.tags}, name
            assert prop["tiff"]["byte_order"] == ("little" if tf.byteorder == "<" else "big"), name
            assert prop["tiff"]["bigtiff"] == tf.is_bigtiff, name
            if ome_xml:
                assert ifds[0]["tags"]["270"]["value"] == tf.pages[0].description, name
        assert not [k for k in out.bytes_entries if not k.endswith("zarr.json")], name


@pytest.mark.parametrize("name,message", [
    ("unsupported_lzw.tif", "compression 5"),
    ("unsupported_predictor.tif", "predictor 2"),
    ("unsupported_strips.tif", "strips"),
])
def test_rejects(name, message):
    with pytest.raises(Rejected, match=message):
        virtualize(str(FIXTURES / name), url="https://data.test/x")


def test_jpeg_prefix_is_a_data_source():
    # profiles/tiff.md §3.3: every JPEG tile starts with its IFD's prefix P, a data source
    # (one per distinct P, in order of first use), then the tile without SOI.
    for name in ("jpeg_aperio_rgb.tif", "jpeg_gray.tif", "jpeg_ycbcr.tif"):
        _, out = virtualize(str(FIXTURES / name), url="https://data.test/x.tif")
        data = list(out.data)
        assert data and all(d.startswith(b"\xff\xd8") for d in data), name
        used = set()
        for key, ranges in out.refs.items():
            prefix, tile = ranges
            assert len(prefix) == 3 and prefix[1:] == (0, len(data[prefix[0] - 1])), key
            assert len(tile) == 2, key
            used.add(prefix[0])
        assert used == set(range(1, len(data) + 1)), name
