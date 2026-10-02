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
