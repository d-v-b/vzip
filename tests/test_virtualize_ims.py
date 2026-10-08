"""The Python IMS virtualizer (profiles/ims.md) on the synthetic fixtures.

Equivalence with the browser virtualizer is checked by
conformance/virtualize/compare.py, and pixel correctness by
web/test/ims/verify.py.
"""

import json
from pathlib import Path

import pytest

from vzip.virtualize import Rejected, virtualize

FIXTURES = Path(__file__).parents[1] / "web" / "test" / "fixtures" / "ims"


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
        assert len(out.refs) == out.summary["chunks"]
        assert all(len(ranges) == 1 for ranges in out.refs.values())


@pytest.mark.parametrize("name,message", [
    ("ims_reject_not_imaris.ims", "not an Imaris file"),
    ("ims_reject_shuffle.ims", r"filters \[2, 1\]"),
    ("ims_reject_fletcher32.ims", r"filters \[3\]"),
    ("ims_reject_fill_value.ims", "fill value"),
    ("ims_reject_float64.ims", "class 1, 8 bytes"),
    ("ims_reject_extensible.ims", "chunk index type 4"),
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
])
def test_rejects(name, message):
    with pytest.raises(Rejected, match=message):
        virtualize(str(FIXTURES / name), url="https://data.test/x")


def test_value_json():
    """Each datatype class's translation (conventions/ims/README.md §5)."""
    import struct

    from vzip.virtualize.ims.source import value_json

    cases = [
        ((3, 1, 0, 4, b"5.5\0"), "5.5"),  # Imaris: an array of 1-character strings
        ((3, 1, 0, 3, b"\xb5m\0"), "µm"),  # not UTF-8: ISO 8859-1
        ((3, 4, 0, 2, b"ab\0\0cd\0\0"), ["ab", "cd"]),
        ((0, 2, 8, 2, struct.pack("<2h", -1, 7)), [-1, 7]),  # signed
        ((0, 4, 1, 1, struct.pack(">I", 7)), [7]),  # big-endian
        ((0, 8, 0, 1, struct.pack("<Q", 2**64 - 1)), ["18446744073709551615"]),
        ((1, 8, 0, 2, struct.pack("<2d", 0.5, float("inf"))), [0.5, "Infinity"]),
        ((6, 2, 0, 1, b"\x01\x02"), {"class": 6, "size": 2, "data": "AQI="}),  # a compound: opaque
    ]
    for args, expected in cases:
        assert value_json(*args) == expected, args
