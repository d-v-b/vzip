"""The Python NDPI virtualizer (profiles/ndpi.md): its JPEG header parser.

Whole files are checked by conformance/virtualize/compare.py and
web/test/ndpi/verify.py.
"""

import struct

import pytest

from vzip.virtualize import Rejected, ndpi


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
