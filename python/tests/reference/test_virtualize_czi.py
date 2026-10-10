"""The Python CZI virtualizer (spec/virtualize/czi.md) on the synthetic fixtures.

Equivalence with the browser virtualizer is checked by
conformance/virtualize/compare.py, and pixels and reconstruction against
czifile and libCZI by js/test/czi/verify.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

# the frozen reference twins (conformance/virtualize/reference/vzip_reference)
sys.path.insert(0, str(Path(__file__).parents[3] / "conformance" / "virtualize" / "reference"))


import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from vzip.virtualize import Rejected
from vzip_reference import virtualize
from vzip_reference.czi.coding import jpeg_frame, jpegxr_size, zstd1_header, zstd_content_size
from vzip_reference.czi.layout import layer, row_band
from vzip_reference.czi.segments import guid

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "fixtures" / "czi"
WIC = bytes.fromhex("24C3DD6F034EFE4BB1853D77768DC9")


def run(name: str):
    return virtualize(str(FIXTURES / f"{name}.czi"), url=f"https://data.test/{name}.czi")[1]


def doc(out, key: str) -> dict:
    return json.loads(out.bytes_entries[key])


def czi_attrs(d: dict) -> dict:
    return d["attributes"]["vzip_virtualized"]["czi"]


# (levels per image as [factor, columns, rows], tile arrays, unplaced, unreferenced segments, attachments, tail)
EXPECTED = {
    "czi_attachments": ([[[1, 1, 1]]], 0, 0, 0, 12, 0),
    "czi_bgr24_raw": ([[[1, 1, 1]]], 0, 0, 0, 0, 0),
    "czi_bgr48_raw": ([[[1, 1, 1]]], 0, 0, 0, 0, 0),
    "czi_bgra32_raw": ([[[1, 1, 1]]], 0, 0, 0, 0, 0),
    "czi_clipped_raw": ([[[1, 3, 2]]], 0, 0, 0, 0, 0),
    "czi_deleted": ([[[1, 1, 1]]], 0, 0, 4, 0, 30),
    "czi_empty_directory": ([], 0, 0, 0, 0, 0),
    "czi_entry_mismatch": ([[[1, 1, 1]]], 0, 1, 0, 0, 0),
    "czi_gray16_tczyx": ([[[1, 1, 1]]], 0, 0, 0, 3, 0),
    "czi_gray8_single": ([[[1, 1, 1]]], 0, 0, 0, 0, 0),
    "czi_irregular": ([], 5, 0, 0, 0, 0),
    "czi_jpeg_grid": ([[[1, 2, 2]], [[1, 2, 1]]], 0, 0, 0, 0, 0),
    "czi_jxr": ([[[1, 1, 1]]] * 8, 0, 0, 0, 0, 0),
    "czi_jxr_pyramid": ([[[2, 2, 2], [4, 1, 1]]], 4, 0, 0, 0, 0),
    "czi_line_scan": ([[[1, 1, 1]]], 0, 0, 0, 0, 0),
    "czi_many_attachments": ([[[1, 1, 1]]], 0, 0, 0, 700, 0),
    "czi_metadata_attachment": ([[[1, 1, 1]]], 0, 0, 0, 0, 0),
    "czi_mixed_types": ([], 2, 0, 0, 0, 0),
    "czi_mosaic_overlap": ([], 3, 0, 0, 0, 0),
    "czi_multiscene": ([[[1, 1, 1]]] * 5, 0, 0, 0, 0, 0),
    "czi_pyramid_edge_bug": ([[[1, 1, 1]]], 1, 0, 0, 0, 0),
    "czi_regular_holes": ([[[1, 3, 2]]], 0, 0, 0, 0, 0),
    "czi_spare_bytes": ([[[1, 1, 1], [2, 1, 1]]], 0, 0, 0, 0, 0),
    "czi_subblock_attachments": ([[[1, 1, 1]]], 0, 0, 0, 0, 0),
    "czi_subblock_masks": ([[[1, 1, 1]]], 0, 0, 0, 0, 0),
    "czi_subsampled": ([[[2, 1, 1]]], 0, 0, 0, 0, 0),
    "czi_trailing": ([[[1, 1, 1]]], 0, 0, 0, 0, 0),
    "czi_types": ([[[1, 1, 1]]] * 6, 0, 0, 0, 0, 0),
    "czi_unplaced": ([[[1, 1, 1]]], 0, 6, 0, 0, 0),
    "czi_xml_latin1": ([[[1, 1, 1]]], 0, 0, 0, 0, 0),
    "czi_xml_odd": ([[[1, 1, 1]]], 0, 0, 0, 0, 0),
    "czi_zstd": ([[[1, 1, 1]]] * 6, 0, 0, 0, 0, 0),
}


def check_gray16_tczyx(out):
    ome = doc(out, "0/zarr.json")["attributes"]["ome"]
    ms = ome["multiscales"][0]
    assert [a["name"] for a in ms["axes"]] == ["t", "c", "z", "y", "x"]
    assert ms["datasets"][0]["coordinateTransformations"][0]["scale"] == [0.5, 1, 0.5, 1.2 * (1 / 1e-6) * 1e-7 / 1.2 * 1.2, 0.1 * (1 / 1e-6) * 1e-6]
    channels = ome["omero"]["channels"]
    assert [c["label"] for c in channels] == ["DAPI", "disp1", "disp2"]
    assert [c["color"] for c in channels] == ["FF0000", "00FF00", "FFFFFF"]
    assert channels[0]["window"] == {"min": 0, "max": 2**14 - 1, "start": 0.01 * 65535, "end": 0.25 * 65535}
    assert channels[1]["window"] == {"min": 0, "max": 2**12 - 1, "start": 0, "end": 0.5 * 65535}
    assert channels[2]["window"]["end"] == 2**12 - 1
    a = doc(out, "0/0/zarr.json")
    assert a["shape"] == [2, 3, 4, 9, 11] and a["chunk_grid"]["configuration"]["chunk_shape"] == [1, 1, 1, 9, 11]
    assert "0/0/c/1/2/3/0/0" in out.refs
    assert doc(out, "vzip_source/attachments/0/zarr.json")["shape"] == [2]
    assert "vzip_source/attachments/1/time/zarr.json" in out.bytes_entries
    assert "vzip_source/subblocks/metadata/offsets/zarr.json" in out.bytes_entries or \
        "vzip_source/subblocks/metadata/zarr.json" in out.bytes_entries


def check_jxr_pyramid(out):
    a = doc(out, "0/0/zarr.json")
    assert a["codecs"] == [{"name": "imagecodecs_jpegxr"}]
    assert a["shape"] == [13, 19]
    assert a["chunk_grid"] == {"name": "rectilinear", "configuration": {"kind": "inline",
                                                                          "chunk_shapes": [[[8, 1], 5], [[10, 1], 9]]}}
    ms = doc(out, "0/zarr.json")["attributes"]["ome"]["multiscales"][0]
    assert ms["name"] == "ScanRegion0"
    px = 3.444225755520869e-07 * (1 / 1e-6)
    t = ms["datasets"][0]["coordinateTransformations"]
    assert t[0]["scale"] == [px * 2, px * 2] and t[1]["translation"] == [(50 + 0.5) * px, (-100 + 0.5) * px]
    assert doc(out, "0/1/zarr.json")["chunk_grid"]["name"] == "regular"
    tile = czi_attrs(doc(out, "tiles/0/zarr.json"))
    assert tile == {"dimensions": {"S": 0}, "x": -100, "y": 50, "size": [20, 16], "stored_size": [20, 16],
                    "planes": {"t": 0, "c": 0, "z": 0}, "copy": 0}


def check_zstd(out):
    assert doc(out, "2/0/zarr.json")["codecs"] == [
        {"name": "bytes", "configuration": {"endian": "little"}},
        {"name": "numcodecs.shuffle", "configuration": {"elementsize": 2}},
        {"name": "zstd", "configuration": {"level": 0, "checksum": False}}]
    assert doc(out, "1/0/zarr.json")["codecs"][1:] == [{"name": "zstd", "configuration": {"level": 0, "checksum": False}}]
    assert doc(out, "3/0/zarr.json")["codecs"][0]["name"] == "transpose"
    assert doc(out, "0/zarr.json")["attributes"]["vzip_virtualized"]["czi"] == {"dimensions": {"S": 0}}


def check_irregular(out):
    tiles = [czi_attrs(doc(out, f"tiles/{n}/zarr.json")) for n in range(5)]
    assert [(t["x"], t["stored_size"], t["copy"]) for t in tiles] == [
        (0, [10, 10], 0), (3, [10, 10], 0), (3, [10, 10], 1), (3, [8, 10], 0), (3, [10, 10], 0)]
    assert doc(out, "tiles/1/zarr.json")["shape"] == [2, 10, 10]  # channels 0 and 1 share the position
    assert doc(out, "tiles/4/zarr.json")["codecs"][-1]["name"] == "zstd"
    assert "tiles/1/c/1/0/0" in out.refs


def check_types(out):
    assert doc(out, "3/0/zarr.json")["data_type"] == "complex64"
    assert doc(out, "3/0/zarr.json")["fill_value"] == [0.0, 0.0]
    names = [doc(out, f"{k}/zarr.json")["attributes"]["ome"]["multiscales"][0].get("name") for k in range(6)]
    assert names == ["float", "int32", "double", None, None, None]
    assert "omero" not in doc(out, "0/zarr.json")["attributes"]["ome"] or \
        "window" not in doc(out, "0/zarr.json")["attributes"]["ome"]["omero"]["channels"][0]


def check_bgr24(out):
    labels = [c["label"] for c in doc(out, "0/zarr.json")["attributes"]["ome"]["omero"]["channels"]]
    assert labels == ["Brightfield B", "Brightfield G", "Brightfield R"]
    assert doc(out, "0/0/zarr.json")["codecs"][0] == {"name": "transpose", "configuration": {"order": [1, 2, 0]}}


def check_jpeg(out):
    labels = [c["label"] for c in doc(out, "0/zarr.json")["attributes"]["ome"]["omero"]["channels"]]
    assert labels == ["C0 R", "C0 G", "C0 B"]


def check_deleted(out):
    ids = out.bytes_entries["vzip_source/segments/id/c/0/0"]
    assert ids[:16] == b"DELETED".ljust(16, b"\0") and ids[48:64] == b"UNKNOWN_ID".ljust(16, b"\0")
    assert "vzip_source/tail/c/0" in out.refs


def check_entry_mismatch(out):
    assert "vzip_source/subblocks/entry/c/1/0" in out.refs
    assert "vzip_source/subblocks/data/c/1/0" in out.refs


def check_unplaced(out):
    assert doc(out, "vzip_source/subblocks/data/offsets/zarr.json")["shape"] == [8]
    assert doc(out, "0/zarr.json")["attributes"]["vzip_virtualized"]["czi"] == {"dimensions": {"S": 6}}


def check_attachments(out):
    s = czi_attrs(doc(out, "vzip_source/zarr.json"))["attachments"]
    assert [a.get("form") for a in s] == ["focus_positions", "bytes", "bytes", "bytes", None, "bytes", "empty",
                                          "bytes", "bytes", "bytes", "event_list", "time_stamps"]
    assert s[4] == {"entry": s[4]["entry"]} and s[5]["segment_entry"]
    assert s[9]["name"] == "Caf\xe9" and s[9]["content_guid"] == "03020100-0504-0706-0809-0a0b0c0d0e0f"
    assert czi_attrs(doc(out, "vzip_source/attachments/0/zarr.json")) == {"size": 12}
    assert "vzip_source/attachments/6/zarr.json" not in out.bytes_entries


def check_many(out):
    assert czi_attrs(doc(out, "vzip_source/zarr.json")) == {"attachments": "attachments/index"}
    text = b"".join(v for k, v in sorted(out.bytes_entries.items()) if k.startswith("vzip_source/attachments/index/c/"))
    assert len(json.loads(text.rstrip(b"\0"))) == 700


def check_trailing(out):
    assert "vzip_source/subblocks/trailing/c/0/0" in out.refs


def check_clipped(out):
    a = doc(out, "0/0/zarr.json")
    assert a["shape"] == [2, 10, 21]
    assert a["chunk_grid"]["configuration"]["chunk_shapes"] == [1, [[6, 1], 4], [[8, 2], 5]]
    assert out.refs["0/0/c/1/1/2"][0][1] == 5 * 4


def check_xml_odd(out):
    ome = doc(out, "0/zarr.json")["attributes"]["ome"]
    assert ome["multiscales"][0]["datasets"][0]["coordinateTransformations"][0]["scale"] == [1, 1, 2e-6 * (1 / 1e-6)]
    assert [c["label"] for c in ome["omero"]["channels"]] == ["a & b", "second"]
    assert ome["multiscales"][0]["axes"][2] == {"name": "x", "type": "space", "unit": "micrometer"}


def check_empty(out):
    assert "ome" not in doc(out, "zarr.json")["attributes"] and "OME/zarr.json" not in out.bytes_entries


def check_root(out):
    root = czi_attrs(doc(out, "zarr.json"))
    assert root == {"version": [1, 0], "primary_file_guid": "9314a6f8-3e05-4ec9-bae0-bc7e96d18997",
                    "file_guid": "9314a6f8-3e05-4ec9-bae0-bc7e96d18997", "file_part": 0, "update_pending": 0}


CHECKS = {"czi_gray16_tczyx": check_gray16_tczyx, "czi_jxr_pyramid": check_jxr_pyramid, "czi_zstd": check_zstd,
          "czi_irregular": check_irregular, "czi_types": check_types, "czi_bgr24_raw": check_bgr24,
          "czi_jpeg_grid": check_jpeg, "czi_deleted": check_deleted, "czi_entry_mismatch": check_entry_mismatch,
          "czi_unplaced": check_unplaced, "czi_attachments": check_attachments, "czi_many_attachments": check_many,
          "czi_trailing": check_trailing, "czi_clipped_raw": check_clipped, "czi_xml_odd": check_xml_odd,
          "czi_empty_directory": check_empty, "czi_gray8_single": check_root}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_virtualizes_the_synthetic_files(name):
    out = run(name)
    images, tiles, unplaced, segments, attachments, tail = EXPECTED[name]
    s = out.summary
    assert (s["images"], s["tiles"], s["unplaced"], s["segments"], s["attachments"], s["tail"]) == (
        images, tiles, unplaced, segments, attachments, tail)
    if name in CHECKS:
        CHECKS[name](out)


REJECTIONS = {
    "major_2": "Major MUST be 1",
    "file_part": "FilePart 1",
    "entry_file_part": "directory entry 0 is in FilePart 1",
    "attachment_file_part": "attachment entry 0 is in FilePart 1",
    "no_directory": "subblock directory at 0 is not a ZISRAWDIRECTORY",
    "directory_id": "is not a ZISRAWDIRECTORY",
    "entry_count_negative": "EntryCount -1",
    "entry_count_over_limit": "EntryCount 2097153",
    "directory_overrun": "entry 1 does not end within",
    "directory_used": "entry 0 does not end within",
    "de_schema": "schema b'DE', not DV",
    "unknown_schema": "schema b'XX', not DV",
    "dimension_letter": "has a dimension b'Q",
    "dimension_lowercase": "has a dimension b'z",
    "dimension_padding": "has a dimension b'Z\\\\x00\\\\x00\\\\x01'",
    "duplicate_dimension": "the dimension C twice",
    "missing_x": "lacks the X or Y",
    "zero_size": "X Size and StoredSize must be at least 1",
    "plane_size_not_1": "C Size and StoredSize must be 1",
    "dimension_count_13": "DimensionCount 13",
    "subblock_id": "is not a ZISRAWSUBBLOCK",
    "subblock_outside_file": "subblock 0's parts",
    "subblock_copy_schema": "copy of its entry has schema",
    "subblock_copy_count": "copy of its entry has DimensionCount 41",
    "negative_sizes": "negative MetadataSize",
    "pixel_type_unknown": "PixelType 5",
    "compression_lzw": "Compression 2",
    "compression_chunked": "Compression 7",
    "compression_raw_camera": "Compression 100",
    "jpeg_gray16": "Compression 1 with PixelType 1",
    "metadata_id": "is not a ZISRAWMETADATA",
    "metadata_outside_file": "metadata segment's parts",
    "metadata_negative": "negative XmlSize",
    "attdir_id": "is not a ZISRAWATTDIR",
    "attdir_count_over_limit": "EntryCount 65537",
    "attachment_id": "is not a ZISRAWATTACH",
    "attachment_outside_file": "attachment 0's data",
    "extent": "more than 2\\^31",
    "segment_negative": "AllocatedSize -1",
}


@pytest.mark.parametrize("case", sorted(REJECTIONS))
def test_rejects(case):
    with pytest.raises(Rejected, match=REJECTIONS[case]):
        run(f"czi_reject_{case}")


def test_every_reject_fixture_has_a_test():
    assert {p.stem[len("czi_reject_"):] for p in FIXTURES.glob("czi_reject_*.czi")} == set(REJECTIONS)


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    d = tmp_path_factory.mktemp("czi_big")
    subprocess.run([sys.executable, str(ROOT / "fixtures" / "generators" / "czi" / "write_fixtures.py"), "--big", str(d)],
                   check=True)
    return d


def test_large_uncompressed_tiles_are_cut_in_row_bands(big):
    out = virtualize(str(big / "czi_big_bands.czi"), url="https://data.test/big.czi")[1]
    a = doc(out, "0/0/zarr.json")
    assert a["shape"] == [3072, 4100] and a["chunk_grid"] == {"name": "regular",
                                                               "configuration": {"chunk_shape": [1024, 4100]}}
    assert sorted(k for k in out.refs if k.startswith("0/0/c/")) == ["0/0/c/0/0", "0/0/c/1/0", "0/0/c/2/0"]
    (start, n), = out.refs["0/0/c/1/0"]
    assert n == 1024 * 4100 * 2 and start == out.refs["0/0/c/0/0"][0][0] + n
    t = doc(out, "tiles/0/zarr.json")
    assert t["shape"] == [2050, 4100] and t["chunk_grid"]["configuration"]["chunk_shape"] == [1025, 4100]
    assert len([k for k in out.refs if k.startswith("tiles/0/c/")]) == 2


def test_more_than_2_16_series_with_an_image_are_rejected(big):
    with pytest.raises(Rejected, match="65537 series with an image"):
        virtualize(str(big / "czi_reject_too_many_series.czi"), url="https://data.test/x.czi")


# ---- units

def test_layer():
    cases = {(4, 4, 4, 4): (1, 0), (8, 4, 4, 2): (2, 1), (21, 4, 10, 2): (2, 1), (22, 4, 10, 2): None,
             (4, 8, 3, 4): (2, 1), (4, 9, 2, 4): None, (16, 8, 4, 2): (2, 2), (30, 30, 10, 10): (3, 1), (2048, 1, 2, 1): (2, 10),
             (3, 3, 4, 4): None, (6561, 1, 3, 1): (3, 7), (999, 1, 1, 1): None}
    for args, want in cases.items():
        assert layer(*args) == want, args


def test_row_band():
    assert row_band(2048, 1024, 4100, 2) == 1024
    assert row_band(2050, 2050, 4100, 2) == 1025
    assert row_band(100, 100, 100, 1) == 100
    assert row_band(4099, 4099, 1 << 22, 4) == 1  # a prime height: one row per band


def head_of(b: bytes):
    return lambda o, n: b[o:o + n] if 0 <= o and o + n <= len(b) else None


def test_codec_headers():
    zstd = bytes.fromhex("28B52FFD")
    assert zstd1_header(head_of(b"\x01")) == (1, False)
    assert zstd1_header(head_of(b"\x03\x01\x01")) == (3, True)
    assert zstd1_header(head_of(b"\x03\x01\x00")) == (3, False)
    for bad in (b"", b"\x02", b"\x03\x02\x01", b"\x03\x01"):
        assert zstd1_header(head_of(bad)) is None, bad
    assert zstd_content_size(head_of(zstd + b"\x20" + b"\x30"), 0) == 0x30  # single segment, 1 byte
    assert zstd_content_size(head_of(zstd + b"\x40\x00" + b"\x10\x00"), 0) == 0x10 + 256  # 2 bytes, +256
    assert zstd_content_size(head_of(zstd + b"\x80\x00" + struct.pack("<I", 70000)), 0) == 70000
    assert zstd_content_size(head_of(zstd + b"\xe0" + struct.pack("<Q", 2**40)), 0) == 2**40
    for bad in (zstd + b"\x00\x00", zstd + b"\x28\x30", zstd + b"\x21\x30", b"\x00" * 6, zstd + b"\x80\x00\x01"):
        assert zstd_content_size(head_of(bad), 0) is None, bad
    sof = b"\xff\xd8\xff\xdb\x00\x04\x00\x00\xff\xff\xc0\x00\x0b\x08\x00\x09\x00\x10\x03"
    assert jpeg_frame(head_of(sof), 3) == (16, 9)
    for bad in (sof.replace(b"\xc0", b"\xc3"), sof[:-1] + b"\x01", b"\xff\xd8\xff\xda\x00\x02",
                b"\xff\xd8\x00", b"\xff\xd8\xff\xc4\x00\x01", sof.replace(b"\x0b\x08", b"\x0b\x0c")):
        assert jpeg_frame(head_of(bad), 3) is None, bad
    ifd = struct.pack("<H", 3) + struct.pack("<HHII", 0xBC01, 1, 16, 46) + struct.pack("<HHII", 0xBC80, 3, 1, 12) \
        + struct.pack("<HHII", 0xBC81, 4, 1, 7)
    jxr = b"\x49\x49\xbc\x01" + struct.pack("<I", 8) + ifd + WIC + b"\x0b"
    assert jpegxr_size(head_of(jxr), 1) == (12, 7)
    assert jpegxr_size(head_of(jxr), 0) is None  # 16bppGray is not Gray8's
    assert jpegxr_size(head_of(jxr[:-1] + b"\x08"), 0) == (12, 7)
    assert jpegxr_size(head_of(jxr.replace(struct.pack("<HHII", 0xBC80, 3, 1, 12), struct.pack("<HHII", 0xBC80, 3, 2, 12))), 1) is None
    assert jpegxr_size(head_of(b"II*\0" + jxr[4:]), 1) is None


def test_guid_text():
    assert guid(bytes(range(16))) == "03020100-0504-0706-0809-0a0b0c0d0e0f"
