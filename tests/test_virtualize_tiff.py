"""The Python TIFF virtualizer (profiles/tiff.md) on the synthetic fixtures.

Equivalence with the browser virtualizer is checked by
conformance/virtualize/compare.py, and pixel correctness by
web/test/tiff/verify.py.
"""

from pathlib import Path

import pytest

from vzip.virtualize import Rejected, virtualize

FIXTURES = Path(__file__).parents[1] / "web" / "test" / "fixtures" / "tiff"


def test_virtualizes_the_synthetic_files():
    cases = {
        "rgb_planar_jpeg2000_bigtiff_be.ome.tif": {"axes": ["c", "y", "x"], "levels": [[3, 96, 128], [3, 48, 64], [3, 24, 32]]},
        "tczyx_uint16_deflate.ome.tif": {"axes": ["t", "c", "z", "y", "x"]},
        "svs_like_int16.tif": {"axes": ["y", "x"], "levels": [[96, 128], [48, 64]]},
    }
    for name, expected in cases.items():
        fmt, out = virtualize(str(FIXTURES / name), url=f"https://data.test/{name}")
        assert fmt == "tiff", name
        assert {**out.summary, **expected} == out.summary, name
        assert out.url == f"https://data.test/{name}"


@pytest.mark.parametrize("name,message", [
    ("unsupported_lzw.tif", "compression 5"),
    ("unsupported_predictor.tif", "predictor 2"),
    ("unsupported_strips.tif", "strips"),
])
def test_rejects(name, message):
    with pytest.raises(Rejected, match=message):
        virtualize(str(FIXTURES / name), url="https://data.test/x")
