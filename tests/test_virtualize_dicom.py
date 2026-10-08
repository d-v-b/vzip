"""The Python DICOM virtualizer (profiles/dicom.md) on the synthetic fixtures.

Equivalence with the browser virtualizer is checked by
conformance/virtualize/compare.py, and pixel correctness against pydicom by
web/test/dicom/verify.py.
"""

import json
from pathlib import Path

import pytest

from vzip.virtualize import Rejected, virtualize

FIXTURES = Path(__file__).parents[1] / "web" / "test" / "fixtures" / "dicom"


def _ome(out) -> dict:
    return json.loads(out.bytes_entries["zarr.json"])["attributes"]["ome"]


def _codecs(out) -> list:
    return [c["name"] for c in json.loads(out.bytes_entries["0/zarr.json"])["codecs"]]


CASES = [
    ("dicom_implicit_mono16.dcm",
     {"axes": ["y", "x"], "shape": [12, 10], "dataType": "uint16", "references": 1},
     ["bytes"], [0.5, 0.25], {"min": 0, "max": 4095, "start": 864, "end": 1264}),
    ("dicom_tiff_preamble.dcm",
     {"axes": ["y", "x"], "shape": [12, 10], "dataType": "uint16", "references": 1},
     ["bytes"], [0.5, 0.25], {"min": 0, "max": 4095, "start": 864, "end": 1264}),
    ("dicom_explicit_signed16_frames.dcm",
     {"axes": ["z", "y", "x"], "shape": [4, 6, 9], "dataType": "int16", "photometric": "MONOCHROME1"},
     ["bytes"], [2.5, 1, 1], {"min": -32768, "max": 32767, "start": -32768, "end": 32767}),
    ("dicom_bigendian_mono16.dcm",
     {"shape": [2, 5, 7], "transferSyntax": "1.2.840.10008.1.2.2"}, ["bytes"], [1, 0.1, 0.1], None),
    ("dicom_explicit_sequences_mono8.dcm", {"shape": [5, 3]}, ["bytes"], [0.2, 0.3], None),
    ("dicom_rgb_interleaved.dcm",
     {"axes": ["c", "z", "y", "x"], "shape": [3, 2, 5, 6], "references": 2}, ["transpose", "bytes"], None, None),
    ("dicom_rgb_planar.dcm", {"axes": ["c", "z", "y", "x"], "references": 6}, ["bytes"], None, None),
    ("dicom_jpeg_ybr422_nobot.dcm",
     {"shape": [3, 2, 16, 24], "photometric": "YBR_FULL_422"}, ["transpose", "imagecodecs_jpeg"], None, None),
    ("dicom_jpeg_mono_bot.dcm", {"shape": [3, 16, 16]}, ["imagecodecs_jpeg"], None, None),
    ("dicom_j2k_mono16_eot.dcm", {"shape": [3, 9, 7], "dataType": "uint16"}, ["imagecodecs_jpeg2k"], None,
     {"min": 0, "max": 4095, "start": 0, "end": 4095}),
    ("dicom_j2k_signed16.dcm", {"shape": [7, 6], "dataType": "int16"}, ["imagecodecs_jpeg2k"], None, None),
    ("dicom_wsi_tiled_full_jpeg.dcm",
     {"axes": ["c", "y", "x"], "shape": [3, 30, 40], "wholeSlide": True, "references": 6},
     ["transpose", "imagecodecs_jpeg"], [1, 0.00025, 0.0005], None),
    ("dicom_wsi_tiled_full_native.dcm", {"shape": [9, 13], "wholeSlide": True, "references": 4}, ["bytes"], None, None),
]


def test_virtualizes_the_synthetic_files():
    # Each case: the file, part of its summary, its codecs, and its scale and
    # first window where given.
    for name, summary, codecs, scale, window in CASES:
        fmt, out = virtualize(str(FIXTURES / name), url=f"https://data.test/{name}")
        assert fmt == "dicom", name
        assert out.url == f"https://data.test/{name}"
        assert {**out.summary, **summary} == out.summary, name
        assert _codecs(out) == codecs, name
        ome = _ome(out)
        if scale is not None:
            assert ome["multiscales"][0]["datasets"][0]["coordinateTransformations"][0]["scale"] == scale, name
        if window is not None:
            assert ome["omero"]["channels"][0]["window"] == window, name


@pytest.mark.parametrize("name,message", [
    ("dicom_reject_rle.dcm", r"transfer syntax 1\.2\.840\.10008\.1\.2\.5"),
    ("dicom_reject_jpegls.dcm", "transfer syntax"),
    ("dicom_reject_palette.dcm", "PALETTE COLOR"),
    ("dicom_reject_native_ybr.dcm", "YBR_FULL"),
    ("dicom_reject_tiled_sparse.dcm", "TILED_FULL"),
    ("dicom_reject_focal_planes.dcm", "focal planes"),
    ("dicom_reject_wsi_frames.dcm", "3 frames for 2 by 2 tiles"),
    ("dicom_reject_fragments_without_offsets.dcm", "without an offset table"),
    ("dicom_reject_bot_offset.dcm", "not a fragment's"),
    ("dicom_reject_eot_with_bot.dcm", "Extended Offset Table with a Basic"),
    ("dicom_reject_short_pixel_data.dcm", "less than 3 frames"),
    ("dicom_reject_high_bit.dcm", "High Bit"),
    ("dicom_reject_bits_allocated.dcm", "Bits Allocated 12"),
    ("dicom_reject_bigendian_ow8.dcm", "OW in big endian"),
    ("dicom_reject_zero_frames.dcm", "Number of Frames 0"),
    ("dicom_reject_no_pixel_data.dcm", "no Pixel Data"),
    ("dicom_reject_misplaced_delimiter.dcm", "expected an item"),
    ("dicom_reject_truncated_sequence.dcm", "runs past its container"),
    ("dicom_reject_unknown_vr.dcm", "unknown VR"),
    ("dicom_reject_rows_vr.dcm", "VR SS, not US"),
    ("dicom_reject_depth.dcm", "nested more than 64"),
])
def test_rejects(name, message):
    with pytest.raises(Rejected, match=message):
        virtualize(str(FIXTURES / name), url="https://data.test/x")


def test_value_json():
    """Each VR's translation (conventions/dicom/README.md §5)."""
    import struct

    from vzip.virtualize.dicom.source import value_json

    cases = [
        ("CS", b"ORIGINAL\\PRIMARY ", True, {"vr": "CS", "Value": ["ORIGINAL", "PRIMARY"]}),
        ("UI", b"1.2.3\0", True, {"vr": "UI", "Value": ["1.2.3"]}),
        ("LO", b" a\\\\b ", True, {"vr": "LO", "Value": ["a", None, "b"]}),
        ("LT", b" two\\lines ", True, {"vr": "LT", "Value": [" two\\lines"]}),
        ("PN", b"Doe^Jane==ja^ne", True, {"vr": "PN", "Value": [{"Alphabetic": "Doe^Jane", "Phonetic": "ja^ne"}]}),
        ("DS", b"0.50\\1e999\\x ", True, {"vr": "DS", "Value": [0.5, "1e999", "x"]}),
        ("IS", b"+12\\-9007199254740993", True, {"vr": "IS", "Value": [12, "-9007199254740993"]}),
        ("SH", b"caf\xe9", True, {"vr": "SH", "Value": ["café"]}),  # not UTF-8: ISO 8859-1
        ("AT", struct.pack("<4H", 0x0028, 0x0010, 0x7FE0, 0x0010), True, {"vr": "AT", "Value": ["00280010", "7FE00010"]}),
        ("US", struct.pack(">2H", 1, 65535) + b"\x01", False, {"vr": "US", "Value": [1, 65535]}),
        ("FD", struct.pack("<2d", 1.5, float("nan")), True, {"vr": "FD", "Value": [1.5, "NaN"]}),
        ("UV", struct.pack("<Q", 2**64 - 1), True, {"vr": "UV", "Value": ["18446744073709551615"]}),
        ("OB", b"\x00\x01", True, {"vr": "OB", "InlineBinary": "AAE="}),
        ("ST", b"", True, {"vr": "ST"}),
    ]
    for vr, data, little, expected in cases:
        assert value_json(vr, data, little) == expected, vr


def test_a_malformed_sequence_that_the_profile_does_not_walk_has_no_value():
    import struct

    from vzip.virtualize.dicom.dataset import EXPLICIT_LE
    from vzip.virtualize.dicom.source import Translator

    def el(group, element, vr, value):
        if vr in ("SQ", "OB", "UN", "UT"):
            return struct.pack("<HH2sHI", group, element, vr.encode(), 0, len(value)) + value
        return struct.pack("<HH2sH", group, element, vr.encode(), len(value)) + value

    bad_items = struct.pack("<HHI", 0x0008, 0x0016, 4) + b"oops"  # not an item tag
    good = el(0x0010, 0x0010, "PN", b"Doe^Jane")
    blob = el(0x0008, 0x1140, "SQ", bad_items) + good
    t = Translator(lambda o, n: blob[o:o + n], len(blob))
    out, _ = t.dataset(0, len(blob), True, EXPLICIT_LE, 0)
    assert out == {"00081140": {"vr": "SQ"}, "00100010": {"vr": "PN", "Value": [{"Alphabetic": "Doe^Jane"}]}}
    assert t.used == len(b"Doe^Jane")  # the failed sequence's values used none of the budget
