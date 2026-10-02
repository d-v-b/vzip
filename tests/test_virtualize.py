"""The Python virtualizers (VIRTUALIZE.md) on the synthetic fixtures.

Equivalence with the browser virtualizers is checked by
conformance/virtualize/compare.py, and pixel correctness by
web/test/verify_tiff.py and web/test/verify_nd2.py.
"""

import struct
from pathlib import Path

import pytest

from vzip.virtualize import Rejected, ndpi, virtualize

FIXTURES = Path(__file__).parents[1] / "web" / "test" / "fixtures"


def test_virtualizes_the_synthetic_files():
    cases = {
        "rgb_planar_jpeg2000_bigtiff_be.ome.tif": ("tiff", {"axes": ["c", "y", "x"], "levels": [[3, 96, 128], [3, 48, 64], [3, 24, 32]]}),
        "tczyx_uint16_deflate.ome.tif": ("tiff", {"axes": ["t", "c", "z", "y", "x"]}),
        "svs_like_int16.tif": ("tiff", {"axes": ["y", "x"], "levels": [[96, 128], [48, 64]]}),
        "nd2_tz_uint16.nd2": ("nd2", {"sizes": {"t": 3, "z": 4, "c": 2, "y": 6, "x": 5}, "channels": ["DAPI", "GFP"]}),
        "nd2_padded_rgb.nd2": ("nd2", {"paddedRows": True, "channels": ["Brightfield R", "Brightfield G", "Brightfield B"]}),
        "nd2_compressed_positions.nd2": ("nd2", {"sizes": {"t": 3, "p": 3, "c": 1, "y": 5, "x": 7}, "compressed": True, "missing": 1}),
        "nd2_float_uncalibrated.nd2": ("nd2", {"dataType": "float32", "channels": ["C0"]}),
    }
    for name, (fmt, expected) in cases.items():
        got_fmt, out = virtualize(str(FIXTURES / name), url=f"https://data.test/{name}")
        assert got_fmt == fmt, name
        assert {**out.summary, **expected} == out.summary, name
        assert out.url == f"https://data.test/{name}"


def test_padded_rows_become_one_range_per_row():
    _, out = virtualize(str(FIXTURES / "nd2_padded_rgb.nd2"), url="https://data.test/x.nd2")
    rows = out.refs["0/0/c/0/1/0/0"]
    assert len(rows) == 4 and rows[0][1] == 39 and rows[1][0] - rows[0][0] == 40


@pytest.mark.parametrize("name,message", [
    ("unsupported_lzw.tif", "compression 5"),
    ("unsupported_predictor.tif", "predictor 2"),
    ("unsupported_strips.tif", "strips"),
    ("nd2_reject_lossy.nd2", "lossy"),
    ("nd2_reject_tiled.nd2", "tiled"),
    ("nd2_reject_loop_type.nd2", "loop type 7"),
    ("nd2_reject_version2.nd2", "Ver2.0"),
    ("nd2_reject_header_lengths.nd2", "name length"),
    ("nd2_reject_legacy.nd2", "not a TIFF or ND2"),
])
def test_rejects(name, message):
    with pytest.raises(Rejected, match=message):
        virtualize(str(FIXTURES / name), url="https://data.test/x")


def _jpeg_header(factors: list[int]) -> bytes:
    """SOI, SOF0 for a 16 × 32 image with the given sampling factors, DRI 4, SOS."""
    sof = struct.pack(">BHHB", 8, 16, 32, len(factors)) + b"".join(bytes([k, f, 0]) for k, f in enumerate(factors))
    sos = bytes([len(factors)]) + b"".join(bytes([k, 0]) for k in range(len(factors))) + b"\x00\x3f\x00"
    return (b"\xff\xd8" + b"\xff\xc0" + struct.pack(">H", 2 + len(sof)) + sof + b"\xff\xdd\x00\x04\x00\x04"
            + b"\xff\xda" + struct.pack(">H", 2 + len(sos)) + sos)


def test_ndpi_jpeg_header_mcu_size():
    # (width, height) of the MCU: 8 times the largest horizontal and vertical
    # sampling factors (high and low 4 bits).
    for factors, mcu in (([0x11, 0x11, 0x11], (8, 8)), ([0x21, 0x11, 0x11], (16, 8)), ([0x22, 0x11, 0x11], (16, 16)),
                         ([0x12, 0x11, 0x11], (8, 16))):
        header = _jpeg_header(factors)
        _, _, mw, mh, interval = ndpi.jpeg_header(header)
        assert (mw, mh, interval) == (*mcu, 4)


def test_ndpi_jpeg_header_rejects_progressive():
    header = _jpeg_header([0x11]).replace(b"\xff\xc0", b"\xff\xc2", 1)
    with pytest.raises(Rejected, match="not baseline"):
        ndpi.jpeg_header(header)
