"""The Python NDPI virtualizer (profiles/ndpi.md): its JPEG header parser and
the data sources that hold the header.

Whole files are checked by conformance/virtualize/compare.py and
web/test/ndpi/verify.py.
"""

import base64
import json
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
        p = file.rfind(head + b"\xff\xc0" + sof[2:4], 0, ranges[3][0])  # this level's strip
        assert head.startswith(b"\xff\xd8") and p >= 0, key
        assert file[p + len(head) + len(sof) :].startswith(tail), key
        used |= {before[0], after[0]}
        # The strip's own SOF0, which the literal replaces, is in its IFD's source metadata.
        level = key.split("/")[0]
        group = json.loads(out.bytes_entries[f"vzip_source/ifds/{level}/zarr.json"])["attributes"]
        original = base64.b64decode(group["vzip_virtualized"]["ndpi"]["sof0"])
        assert original[:2] == b"\xff\xc0" and file[p + len(head) : p + len(head) + len(sof)] == original, key
    assert used == set(range(1, len(data) + 1))
    assert "sof0" not in json.loads(out.bytes_entries["vzip_source/ifds/2/zarr.json"])["attributes"][
        "vzip_virtualized"]["ndpi"]  # a level without McuStarts: its strip is referenced whole


@pytest.mark.parametrize("name,message", [
    ("edge_reject_ndpi_wide_interval.ndpi", "65536 pixels wide"),
    ("edge_reject_ndpi_short_strip.ndpi", "3 bytes is shorter than 4"),
])
def test_ndpi_rejects(name, message):
    path = Path(__file__).parents[1] / "web" / "test" / "fixtures" / "ndpi" / name
    with pytest.raises(Rejected, match=message):
        virtualize(str(path), url="https://data.test/x.ndpi")


def test_ndpi_records_every_ifd_in_its_own_group():
    """conventions/ndpi/README.md §5: each IFD on its own group of vzip_source, the
    pointer tags' IFDs read in NDPI's layout, a duplicate tag, an unknown field type
    (its value field and high word), and the macro image's contiguous strips as one range."""
    import json

    path = Path(__file__).parents[1] / "web" / "test" / "fixtures" / "ndpi" / "ndpi_pointers.ndpi"
    _, out = virtualize(str(path), url="https://data.test/x.ndpi")

    def source(p: str) -> dict:
        doc = json.loads(out.bytes_entries["/".join(["vzip_source", *filter(None, [p]), "zarr.json"])])
        return doc["attributes"].get("vzip_virtualized", {}).get("ndpi", {})

    assert source("") == {"ifd_count": 2}
    macro = source("ifds/1")
    assert macro["pointers"] == {"34665": {"type": 4, "count": 1, "ifds": ["ifds/1/exif"]},
                                 "65449": {"type": 13, "count": 1, "ifds": ["ifds/1/ifd_65449"]}}
    assert macro["tags"]["65500"] == {"type": 99, "count": 4, "field": "AQIDBAAAAAA="}
    assert macro["duplicates"] == [{"tag": 65420, "type": 4, "count": 1, "value": [2]}]
    assert source("ifds/1/exif")["tags"]["36867"]["value"] == "2026:01:01 00:00:00"
    assert source("ifds/1/ifd_65449")["tags"]["305"]["value"] == "private"
    (start, n), = out.refs["vzip_source/ifds/1/data/data/c/0"]
    assert path.read_bytes()[start:start + n].startswith(b"\xff\xd8") and n > 2


def test_ndpi_reads_only_the_values_it_uses():
    """profiles/ndpi.md §4: the values of an IFD that is not a level are not read, so the
    macro image's McuStarts (LONG8, above 2^53 - 1) does not reject; its strip is kept."""
    path = Path(__file__).parents[1] / "web" / "test" / "fixtures" / "ndpi" / "ndpi_unused_values.ndpi"
    _, out = virtualize(str(path), url="https://data.test/x.ndpi")
    assert out.summary["levels"] == [[3, 48, 64]]
    assert "vzip_source/ifds/1/data/zarr.json" in out.bytes_entries
