"""The Python NDPI virtualizer (profiles/ndpi.md): its JPEG header parser and
the data sources that hold the header.

Whole files are checked by conformance/virtualize/compare.py and
web/test/ndpi/verify.py.
"""

import struct
from pathlib import Path

import pytest

from vzip.virtualize import Rejected, ndpi, virtualize


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


def test_ndpi_header_pieces_are_data_sources():
    # §4: the header up to SOF0 and after it are ranges of data sources, one
    # per distinct byte string in order of first use, shared by every chunk
    # and level; the bytes are the strip's own.
    path = Path(__file__).parents[1] / "web" / "test" / "fixtures" / "ndpi" / "ndpi_levels.ndpi"
    _, out = virtualize(str(path), url="https://data.test/x.ndpi")
    data = list(out.data)
    assert [out.data[d] for d in data] == list(range(1, len(data) + 1))
    file = path.read_bytes()
    used = set()
    for key, ranges in out.refs.items():
        if len(ranges) == 1:  # a level without McuStarts: its whole strip
            continue
        before, sof, after = ranges[:3]
        assert len(before) == 3 and len(after) == 3 and isinstance(sof, bytes), key
        assert before[1:] == (0, len(data[before[0] - 1])) and after[1:] == (0, len(data[after[0] - 1])), key
        head, tail = data[before[0] - 1], data[after[0] - 1]
        # In the file, the strip's SOF0 (of the literal's length) separates them.
        p = file.find(head + b"\xff\xc0" + sof[2:4])
        assert head.startswith(b"\xff\xd8") and p >= 0, key
        assert file[p + len(head) + len(sof) :].startswith(tail), key
        used |= {before[0], after[0]}
    assert used == set(range(1, len(data) + 1))
