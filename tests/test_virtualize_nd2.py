"""The Python ND2 virtualizer (profiles/nd2.md) on the synthetic fixtures.

Equivalence with the browser virtualizer is checked by
conformance/virtualize/compare.py, and pixel correctness by
web/test/nd2/verify.py.
"""

from pathlib import Path

import pytest

from vzip.virtualize import Rejected, virtualize

FIXTURES = Path(__file__).parents[1] / "web" / "test" / "fixtures" / "nd2"


def test_virtualizes_the_synthetic_files():
    cases = {
        "nd2_tz_uint16.nd2": {"sizes": {"t": 3, "z": 4, "c": 2, "y": 6, "x": 5}, "channels": ["DAPI", "GFP"]},
        "nd2_padded_rgb.nd2": {"paddedRows": True, "channels": ["Brightfield R", "Brightfield G", "Brightfield B"]},
        "nd2_compressed_positions.nd2": {"sizes": {"t": 3, "p": 3, "c": 1, "y": 5, "x": 7}, "compressed": True, "missing": 1},
        "nd2_float_uncalibrated.nd2": {"dataType": "float32", "channels": ["C0"]},
    }
    for name, expected in cases.items():
        fmt, out = virtualize(str(FIXTURES / name), url=f"https://data.test/{name}")
        assert fmt == "nd2", name
        assert {**out.summary, **expected} == out.summary, name
        assert out.url == f"https://data.test/{name}"


def test_padded_rows_become_one_range_per_row():
    _, out = virtualize(str(FIXTURES / "nd2_padded_rgb.nd2"), url="https://data.test/x.nd2")
    rows = out.refs["0/0/c/0/1/0/0"]
    assert len(rows) == 4 and rows[0][1] == 39 and rows[1][0] - rows[0][0] == 40


@pytest.mark.parametrize("name,message", [
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
