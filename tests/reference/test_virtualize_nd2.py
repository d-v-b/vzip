"""The Python ND2 virtualizer (profiles/nd2.md) on the synthetic fixtures.

Equivalence with the browser virtualizer is checked by
conformance/virtualize/compare.py, and pixel correctness by
web/test/nd2/verify.py.
"""

import sys
from pathlib import Path

# the frozen reference twins (conformance/virtualize/reference/vzip_reference)
sys.path.insert(0, str(Path(__file__).parents[2] / "conformance" / "virtualize" / "reference"))

import json
import math
import struct
import zlib
from pathlib import Path

import pytest

from vzip.virtualize import Rejected
from vzip_reference import virtualize

FIXTURES = Path(__file__).parents[2] / "web" / "test" / "fixtures" / "nd2"


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


def test_frame_headers_are_prefetched_with_a_padded_name():
    """Real files pad frame names to 4072 bytes: the prefetch covers the header and a 4 KiB name."""
    from vzip.virtualize.common import file_reader
    from vzip_reference.nd2 import virtualize_nd2

    for name, frames in (("nd2_tz_uint16.nd2", 12), ("nd2_compressed_positions.nd2", 8)):
        read, size = file_reader(str(FIXTURES / name))
        asked = []

        def reader(offset, length):
            return read(offset, length)

        reader.prefetch = asked.extend
        url = f"https://data.test/{name}"
        assert virtualize_nd2(url, reader, size) == virtualize_nd2(url, read, size), name
        assert len(asked) == frames and all(n == 16 + 4096 for _, n in asked), name


def test_padded_rows_become_one_range_per_row():
    _, out = virtualize(str(FIXTURES / "nd2_padded_rgb.nd2"), url="https://data.test/x.nd2")
    rows = out.refs["0/0/c/0/1/0/0"]
    assert len(rows) == 4 and rows[0][1] == 39 and rows[1][0] - rows[0][0] == 40
    assert rows == [(rows[0][0] + 40 * r, 39) for r in range(4)] and rows[-1] == rows[3] and rows[1:3] == list(rows)[1:3]


def test_more_than_2_16_positions_are_rejected():
    """profiles/nd2.md §5.3: at most 2^16 positions."""
    with pytest.raises(Rejected, match="65537 positions of 1 components"):
        virtualize(str(FIXTURES / "nd2_reject_positions.nd2"), url="https://data.test/x")


def test_more_than_2_20_positions_x_components_are_rejected():
    """profiles/nd2.md §5.3: at most 2^20 positions x components."""
    with pytest.raises(Rejected, match="1025 positions of 1024 components"):
        virtualize(str(FIXTURES / "nd2_reject_position_channels.nd2"), url="https://data.test/x")


def test_the_profile_chunks_share_a_record_budget():
    """profiles/nd2.md §5.1: two chunks of 600000 records each, together over 2^20."""
    with pytest.raises(Rejected, match="more than 1048576 LV records"):
        virtualize(str(FIXTURES / "nd2_reject_profile_records.nd2"), url="https://data.test/x")


@pytest.mark.parametrize("name,message", [
    ("nd2_reject_lossy.nd2", "lossy"),
    ("nd2_reject_tiled.nd2", "tiled"),
    ("nd2_reject_loop_type.nd2", "loop type 7"),
    ("nd2_reject_version2.nd2", "Ver2.0"),
    ("nd2_reject_header_lengths.nd2", "name length"),
    ("nd2_reject_frame_name_length.nd2", "frame 1's chunk header differs in name length"),
    ("nd2_reject_frame_magic.nd2", "no ND2 chunk"),
    ("nd2_reject_frame_short.nd2", "frame 1's chunk is too short for its pixels"),
    ("nd2_reject_legacy.nd2", "not a TIFF, NDPI, ND2, DICOM"),
    ("nd2_reject_inflate_limit.nd2", "inflates to more than 67108864 bytes"),
])
def test_rejects(name, message):
    with pytest.raises(Rejected, match=message):
        virtualize(str(FIXTURES / name), url="https://data.test/x")


def test_variant_json():
    """An XML variant document as JSON (conventions/nd2/README.md §5)."""
    from vzip_reference.nd2.source import variant_json

    doc = (b'<?xml version="1.0"?><variant version="1.0"><a runtype="CLxListVariant">'
           b'<i runtype="lx_int32" value="-7"/><u runtype="lx_uint64" value="18446744073709551615"/>'
           b'<d runtype="double" value="2.5"/><b runtype="bool" value="true"/><s runtype="CLxStringW" value="x &amp; y"/>'
           b'<bad runtype="lx_int32" value="7.5"/><e runtype="CLxListVariant"></e></a></variant>')
    assert variant_json(doc) == {"a": {"i": -7, "u": {"int": "18446744073709551615"}, "d": 2.5, "b": True, "s": "x & y",
                                       "bad": "7.5", "e": {}}}
    # Repeated children are pairs, in order.
    assert variant_json(b'<variant><n runtype="lx_int32" value="1"/><n runtype="lx_int32" value="2"/>'
                        b'<m runtype="CLxStringW" value="x"/></variant>') == [["n", 1], ["n", 2], ["m", "x"]]
    for bad in (b"<variant><a></b></variant>", b"<other/>", b"<variant/><variant/>", b"\xff<variant/>",
                b'<variant value="1"/>'):
        assert variant_json(bad) is None, bad


def test_every_chunk_is_in_the_hierarchy():
    """The streams fixture: decoded chunks at the root and on vzip_source, streams
    shaped to the (flipped) frame grid, opaque chunks as bytes."""
    _, out = virtualize(str(FIXTURES / "nd2_streams.nd2"), url="https://data.test/s.nd2")
    root = json.loads(out.bytes_entries["zarr.json"])["attributes"]["vzip_virtualized"]["nd2"]
    assert list(root["chunks"]) == ["ImageAttributesLV!", "ImageMetadataLV!", "ImageMetadataSeqLV|0!",
                                    "CustomDataVar|CustomDataV2_0!", "CustomDataVar|AppInfo_V1_0!"]
    assert root["chunks"]["CustomDataVar|AppInfo_V1_0!"] == {"no_name": {
        "Version": "5.42 & more", "Gain": 1.5, "Live": True, "Count": 7, "Odd": "x7"}}
    node = json.loads(out.bytes_entries["vzip_source/zarr.json"])["attributes"]["vzip_virtualized"]["nd2"]
    assert list(node["chunks"]) == ["ImageMetadataSeqLV|1!", "ImageEventsLV!"]
    arrays = {k[len("vzip_source/"):-len("/zarr.json")]: json.loads(v) for k, v in out.bytes_entries.items()
              if k.startswith("vzip_source/") and k.endswith("/zarr.json") and json.loads(v)["node_type"] == "array"}
    assert {k: (a["data_type"], a["shape"]) for k, a in arrays.items()} == {
        "CustomData/AcqTimesCache": ("float64", [2, 3]), "CustomData/X": ("float64", [2, 3]),
        "CustomData/PFS_OFFSET": ("int32", [2, 3]), "ImageDataSeq": ("float64", [2, 3]),
        "CustomData/Y": ("uint8", [8]), "CustomData/Blob": ("uint8", [37]), "CustomDataSeq/STREAM_0": ("uint8", [1, 16])}
    # The z loop flips, so streams are copies in the flipped order: frame f = t * 3 + z at [t, 2 - z].
    times = struct.unpack("<6d", out.bytes_entries["vzip_source/CustomData/AcqTimesCache/c/0/0"])
    assert times == (200.0, 100.0, 0.0, 500.0, 400.0, 300.0)
    assert arrays["CustomData/PFS_OFFSET"]["attributes"]["vzip_virtualized"]["nd2"]["tag"]["Desc"] == "PFS offset"
    assert "vzip_source/CustomData/Blob/c/0" in out.refs  # opaque bytes stay where they are


def test_a_multi_phase_time_loop_has_a_period_only_when_its_phases_agree():
    """conventions/nd2/README.md §3, eType 8."""
    from vzip_reference.nd2.lv import LVList, Scalar
    from vzip_reference.nd2.virtualize import flatten_experiment

    def phase(n, ms):
        return {"uiCount": Scalar((3, n)), "dPeriod": Scalar((6, ms))}

    for periods, period in (([1000.0, 1000.0], 1000.0), ([1000.0, 4000.0], 0)):
        root = {"eType": Scalar((3, 8)), "uLoopPars": {"pPeriod": LVList([phase(2, p) for p in periods])}}
        assert flatten_experiment(root) == [{"kind": "t", "count": 4, "scale": period, "depth": 0}], periods


def _source(name: str):
    """The root's and vzip_source's source metadata, and the arrays of vzip_source, of a fixture."""
    _, out = virtualize(str(FIXTURES / name), url=f"https://data.test/{name}")
    root = json.loads(out.bytes_entries["zarr.json"])["attributes"]["vzip_virtualized"]["nd2"]
    node = json.loads(out.bytes_entries["vzip_source/zarr.json"])["attributes"].get("vzip_virtualized", {}).get("nd2", {})
    arrays = {k[len("vzip_source/"):-len("/zarr.json")]: json.loads(v) for k, v in out.bytes_entries.items()
              if k.startswith("vzip_source/") and k.endswith("/zarr.json") and json.loads(v)["node_type"] == "array"}
    return out, root, node, arrays


def _bytes(out, path: str) -> bytes:
    """A 1-D uint8 array's bytes, from the file or the archive (conventions/README.md §7)."""
    data = (FIXTURES / out.url.rsplit("/", 1)[-1]).read_bytes()
    shape = json.loads(out.bytes_entries[f"vzip_source/{path}/zarr.json"])["shape"][0]
    got = b""
    for i in range(len([k for k in {**out.refs, **out.bytes_entries} if k.startswith(f"vzip_source/{path}/c/")])):
        key = f"vzip_source/{path}/c/{i}"
        got += out.bytes_entries[key] if key in out.bytes_entries else b"".join(
            data[r[0]:r[0] + r[1]] if isinstance(r, tuple) else r for r in out.refs[key])
    return got[:shape]


def test_source_metadata_keeps_every_value():
    """Repeated names as pairs, unpaired surrogates, declarations read first, and a
    large chunk on vzip_source (conventions/nd2/README.md §5.1-§5.3)."""
    out, root, node, arrays = _source("nd2_source_values.nd2")
    chunks = root["chunks"]
    assert chunks["ImageTextInfoLV!"] == {"SLxImageTextInfo": [
        ["a", 1], ["a", 2], ["b", "x"], ["s", {"utf16": "QQAA3A=="}], ["\0ANg=", 5],
        ["pairs", [["", ["k", 1]], ["", ["l", 2]]]]]}
    control = chunks["CustomDataVar|NDControlV1_0!"]["NDControl"]
    assert control["LoopSize"] == [["no_name", 57], ["no_name", 0]]
    assert control["Ref"] == "A&#" + "9" * 5000 + ";"
    assert control["Long"] == {"int": "-" + "7" * 30}
    # The declared stream is an array, not decoded; the tag whose Type is an element declares nothing.
    assert "CustomData|S!" not in chunks and arrays["CustomData/S"]["data_type"] == "int32"
    assert "CustomData/T" not in arrays
    assert list(node["chunks"]) == ["ImageCalibrationLV|0!"]
    # An XML element at depth 101 keeps its document from decoding; at depth 100 it does not.
    assert "CustomDataVar|DeepEnoughV1_0!" in chunks and "CustomDataVar|DeepV1_0!" not in chunks
    assert arrays["CustomDataVar/DeepV1_0"]["data_type"] == "uint8"
    trailing = out.bytes_entries["vzip_source/ImageDataSeq.trailing/offsets/c/0"]
    assert struct.unpack("<4q", trailing) == (0, 0, 5, 8)
    assert _bytes(out, "ImageDataSeq.trailing/data") == b"TRAILEND"


def test_source_metadata_keeps_every_chunk():
    """Frames the image does not place, a stream's rest, collisions, invalid and
    reserved paths, names that read alike, and empty chunks (conventions/nd2/README.md §5.3, §5.4)."""
    out, _, node, arrays = _source("nd2_source_chunks.nd2")
    other = [(b"ImageDataSeq|01!", b"\0frame"), (b"ImageDataSeq!", b"\0data"), (b"CustomData|X|1!", b"\0q"),
             (b"CustomData|Foo|Bar!", b"\0xyz"), (b"CustomData|a/b!", b"\0slash"), (b"CustomData|...!", b"\0dots"),
             (b"CustomData|__x!", b"\0reserved"), (b"other!", b"\0other"), (b"CustomData|\xc3\xa9!", b"\0utf8"),
             (b"CustomDataSeq|F|16777216!", b"\0far"), (b"CustomDataSeq|F!", b"\0blob")]
    assert node == {"other": [n.decode() for n, _ in other], "empty": ["ImageDataSeq|x!", "CustomData|Empty!"]}
    for i, (_, data) in enumerate(other):
        assert _bytes(out, f"other/{i}") == data, i
    assert _bytes(out, "CustomData/é") == b"\0latin1"  # the first of the names that read alike
    assert arrays["ImageDataSeq.beyond"]["shape"] == [1, 32]
    assert arrays["CustomData/AcqTimesCache"]["shape"] == [2]
    assert _bytes(out, "CustomData/AcqTimesCache.rest") == struct.pack("<d", 3.0)
    assert {"CustomDataSeq/F/offsets", "CustomDataSeq/F/data", "CustomDataSeq/a\nb", "CustomData/Foo"} <= set(arrays)


def test_a_chunk_over_16_kib_of_json_is_on_vzip_source():
    """The size is that of JSON.stringify's text, in UTF-8 (conventions/nd2/README.md §5.1)."""
    from vzip_reference.nd2.source import json_size

    cases = {0.1: 3, 1e-7: 4, 1e21: 5, 123456789.125: 13, 5e-324: 6, -2.5e-300: 9, 2.0: 1, 1e16: 17,
             1.5e-6: 9, 1e-6: 8, 123e20: 8, -0.0: 1, 1e-5: 7, 123.456e-10: 10, 1.7976931348623157e308: 23}
    assert {v: json_size(v) for v in cases} == cases
    assert json_size('é\n\t"\\\x01\x7f 😀') == 26
    assert json_size({"a": [1, 2.5, True, None, {"\0x": "y"}]}) == 39
    _, root, node, _ = _source("nd2_source_json_size_tagged.nd2")
    assert "AtLimitLV!" in root["chunks"] and list(node["chunks"]) == ["OverLimitLV!"]


def test_values_json_cannot_hold_are_tags():
    """Integers beyond 2^53 - 1 and non-finite binary64s are tags, and an object
    that would read as a tag is pairs; integer-like member names keep their order
    (conventions/nd2/README.md §5.1, §5.2)."""
    out, root, node, arrays = _source("nd2_source_tags.nd2")
    chunks = root["chunks"]
    assert chunks["ImageTextInfoLV!"] == {"SLxTags": {
        "big": {"int": "18446744073709551615"}, "neg": {"int": "-9007199254740992"}, "safe": 2**53 - 1,
        "nan": {"float": "NaN"}, "inf": {"float": "Infinity"}, "ninf": {"float": "-Infinity"}, "text": "NaN",
        "one": [["utf16", "x"]], "i": [["int", "5"]], "fl": [["float", 1.0]], "two": {"int": 1, "float": 2.0},
        "list": [1.0, {"float": "Infinity"}]}}
    assert chunks["ImageTagLV!"] == [["int", 5]]
    assert chunks["CustomDataVar|TagsV1_0!"] == {"no_name": {
        "Big": {"int": "18446744073709551615"}, "Inf": {"float": "Infinity"}, "one": [["float", 1.0]]}}
    # The first declaration of S, under Tag_b, is before the one under "5".
    assert arrays["CustomData/S"]["data_type"] == "int32"
    # ImageMetadataSeqLV|00! is picture metadata of frame 0, at the root.
    assert "ImageMetadataSeqLV|00!" in chunks and list(node["chunks"]) == ["ImageMetadataSeqLV|01!"]


def test_custom_data_decodes_when_its_json_keeps_every_byte():
    """conventions/nd2/README.md §5.1, the lossless test: compression and offset
    tables (in any order) are layout, and k = 0 is the canonical empty name; a
    chunk named as LV decodes whatever."""
    _, root, _, _ = _source("nd2_source_tags.nd2")
    good = {"a": True, "b": 2.5, "n": {"float": "NaN"}, "l": [1, 2], "t": {"z": 1, "B": 2, "a": 3}}
    assert root["chunks"]["CustomData|Good!"] == root["chunks"]["CustomData|Zipped!"] == good
    assert root["chunks"]["CustomData|Unsorted!"] == {"t": {"z": 1, "B": 2, "a": 3}}
    assert root["chunks"]["CustomData|Ties!"] == {"l": [1, 2]}
    assert root["chunks"]["CustomData|Unnamed!"] == {"": 1}
    assert root["chunks"]["ImageLossyLV!"] == {"a": True}


@pytest.mark.parametrize("name,data", [
    ("BoolByte", b"\x01\x02a\0\0\0\x05"),
    ("EmptyNul", None),
    ("AfterNul", b"\x03\x03a\0\0\0b\0\x01\0\0\0"),
    ("Skipped", None),
    ("BadTable", None),
    ("DupTable", None),
    ("NaNBits", b"\x06\x02x\0\0\0\x01\0\0\0\0\0\xf8\x7f"),
    ("ZippedLossy", None),
])
def test_lossy_custom_data_is_bytes(name, data):
    out, root, _, arrays = _source("nd2_source_tags.nd2")
    assert f"CustomData|{name}!" not in root["chunks"] and arrays[f"CustomData/{name}"]["data_type"] == "uint8"
    if data is not None:
        assert _bytes(out, f"CustomData/{name}") == data


def test_sparse_frames_and_families_are_bounded():
    """The frame times are cut as contiguous values and only chunks with a placed
    frame are written; a family keeps indices below 16 per member and 1024 more
    (conventions/nd2/README.md §5.3, §5.4)."""
    out, _, node, arrays = _source("nd2_source_sparse.nd2")
    times = arrays["ImageDataSeq"]
    # Copied values: chunks of at most 64 KiB, cut as contiguous values with that limit.
    assert (times["shape"], times["chunk_grid"]["configuration"]["chunk_shape"]) == ([3_000_000], [8043])
    assert [k for k in out.bytes_entries if k.startswith("vzip_source/ImageDataSeq/c/")] == ["vzip_source/ImageDataSeq/c/0"]
    chunk = out.bytes_entries["vzip_source/ImageDataSeq/c/0"]
    # Frames 0, 1 and 2000 have their timestamps (1.5); frame 2 is missing (NaN).
    values = struct.unpack_from("<2d", chunk) + struct.unpack_from("<d", chunk, 8 * 2000) + struct.unpack_from("<d", chunk, 16)
    assert len(chunk) == 8 * 8043 and values[:3] == (1.5, 1.5, 1.5) and math.isnan(values[3])
    assert node["other"] == ["ImageDataSeq|2000!", "ImageDataSeq|3003000!", "CustomDataSeq|x|5000!"]
    assert _bytes(out, "other/0") == b"T2000"
    assert arrays["ImageDataSeq.beyond"]["shape"][0] == 2 and arrays["CustomDataSeq/x"]["shape"] == [1, 1]
    assert arrays["ImageDataSeq.trailing"]["shape"] == [1, 2]


def test_the_root_keeps_at_most_64_kib_of_chunks():
    """conventions/nd2/README.md §5.1: the largest move first, of equal sizes the first in map order."""
    from vzip_reference.nd2.source import json_size

    _, root, node, _ = _source("nd2_source_root_budget.nd2")
    assert list(node["chunks"]) == ["BLV!"]
    assert list(root["chunks"]) == ["ImageAttributesLV!", "ALV!", "CLV!", "DLV!", "ELV!"]
    assert json_size(root["chunks"]) <= 65536 < json_size({**root["chunks"], **node["chunks"]})


def test_the_node_keeps_at_most_64_kib_of_chunks():
    """conventions/nd2/README.md §5.1: vzip_source's chunks in map order while they
    fit (a later, smaller one too); byte arrays as JSON arrays; names that are
    array indices make pairs; -0 is a tag, guessed, named and in XML."""
    from vzip_reference.nd2.source import json_size

    out, root, node, arrays = _source("nd2_source_node_budget.nd2")
    assert list(node["chunks"]) == ["ImageMetadataSeqLV|1!", "ImageMetadataSeqLV|2!", "ImageMetadataSeqLV|4!"]
    assert json_size(node["chunks"]) <= 65536
    assert root["chunks"]["BytesLV!"] == {"b": list(range(100))}
    assert root["chunks"]["CustomData|Index!"] == [["b", 1.0], ["1", 2.0], ["a", [["z", 1], ["0", 2]]]]
    assert root["chunks"]["IndexLV!"] == {"L": [["x", 1], ["01", 2], ["10", 3]]}
    assert root["chunks"]["CustomData|NegZero!"] == {"a": {"float": "-0"}, "b": 1.0}
    assert root["chunks"]["NegZeroLV!"] == {"a": {"float": "-0"}}
    assert root["chunks"]["CustomDataVar|NegZeroV1_0!"] == {"z": {"float": "-0"}, "one": 1.0}


def test_a_chunk_that_does_not_fit_the_node_is_bytes():
    """A chunk over what is left of vzip_source's 64 KiB, or whose JSON is larger
    than that (a byte array of 70000 bytes, or of 8 MiB, compressed), is kept as
    bytes (conventions/nd2/README.md §5.1)."""
    out, root, node, arrays = _source("nd2_source_node_budget.nd2")
    for path in ("ImageMetadataSeqLV/3", "BigBytesLV", "HugeBytesLV"):
        assert arrays[path]["data_type"] == "uint8", path
    assert not {"ImageMetadataSeqLV|3!", "BigBytesLV!", "HugeBytesLV!"} & {*root["chunks"], *node["chunks"]}
    assert len(_bytes(out, "BigBytesLV")) == 70000 + 2 + 2 * 2 + 8


def test_other_and_empty_past_the_node_budget_are_arrays():
    """conventions/nd2/README.md §5.4: each list over what is left of vzip_source's
    64 KiB is the path of an array holding its JSON text."""
    from vzip_reference.nd2.source import json_size

    out, _, node, arrays = _source("nd2_source_names_spill.nd2")
    assert node == {"other": "other/names", "empty": "other/empty"}
    assert json.loads(_bytes(out, "other/names")) == [f"CustomData|a/{i}!" for i in range(4000)]
    assert json.loads(_bytes(out, "other/empty")) == [f"Empty|{i}!" for i in range(6000)]
    assert json_size([f"Empty|{i}!" for i in range(6000)]) > 65536
    assert _bytes(out, "other/3999") == b"\0"


def test_a_failed_decode_is_charged():
    """Chunks that inflate and are not lossless spend the budget as if they
    decoded; the one that inflates past what is left spends the rest
    (conventions/nd2/README.md §5.1)."""
    out, root, _, arrays = _source("nd2_source_charged.nd2")
    assert list(root["chunks"]) == ["ImageAttributesLV!"]
    assert arrays["CustomData/Ok"]["data_type"] == "uint8"


def test_an_invalid_stream_is_charged_the_most_it_could_inflate_to():
    """1032 bytes per byte of the chunk, capped at what is left (conventions/nd2/README.md §5.1)."""
    out, root, _, arrays = _source("nd2_source_charged_invalid.nd2")
    assert list(root["chunks"]) == ["ImageAttributesLV!"]
    assert arrays["CustomData/Ok"]["data_type"] == "uint8"


@pytest.mark.parametrize("name,chunk_shape,keys,value", [
    # dense: the cut at 64 KiB
    ("nd2_streams.nd2", [2, 3], ["0/0"], None),
    # a placed frame per time point of a [40, 2^21] grid: 40 chunks of 64 KiB are
    # more than 1 MiB, of 32 KiB too; 40 of 16 KiB are not
    ("nd2_source_frame_times.nd2", [1, 2048], [f"{t}/0" for t in range(40)], ("7/0", 0, 7.0)),
    # 2000 frames 8192 apart: at most max(1 MiB, 512 x 2000) bytes, chunks of 64 values
    ("nd2_source_frame_times_sparse.nd2", [64], [str(128 * k) for k in range(2000)], ("128", 0, 1.0)),
])
def test_frame_times_are_bounded_copies(name, chunk_shape, keys, value):
    """conventions/nd2/README.md §5.3: the chunk limit halves from 64 KiB until the
    chunks written hold at most max(2^20, 512 x the placed frames) bytes."""
    out, _, _, arrays = _source(name)
    assert arrays["ImageDataSeq"]["chunk_grid"]["configuration"]["chunk_shape"] == chunk_shape
    stamps = {k[len("vzip_source/ImageDataSeq/c/"):]: v for k, v in out.bytes_entries.items()
              if k.startswith("vzip_source/ImageDataSeq/c/")}
    assert sorted(stamps) == sorted(keys)
    assert all(len(v) == 8 * math.prod(chunk_shape) for v in stamps.values())
    assert sum(map(len, stamps.values())) <= max(1 << 20, 512 * len(keys) * math.prod(chunk_shape))
    if value is not None:
        key, i, t = value
        assert struct.unpack_from("<d", stamps[key], 8 * i) == (t,)


def test_a_declared_tag_over_16_kib_is_its_index():
    """conventions/nd2/README.md §5.2: a member of at most 16384 bytes of JSON is
    kept; a larger one is its index among CustomTagDescription_v1.0's members."""
    _, _, _, arrays = _source("nd2_source_tag_index.nd2")
    tag = lambda path: arrays[path]["attributes"]["vzip_virtualized"]["nd2"]
    assert tag("CustomData/Small") == {"tag": {"ID": "Small", "Type": 3, "Desc": "small"}}
    assert tag("CustomData/Big") == {"tag_index": 2}


def test_path_segments_a_store_cannot_hold_go_to_other():
    """conventions/nd2/README.md §5.4: zarr.json, .zarray, .zgroup and U+0000."""
    out, _, node, arrays = _source("nd2_source_document_names.nd2")
    assert node["other"] == ["zarr.json!", "Foo|zarr.json!", "Foo|.zarray!", ".zgroup|x!", "Foo\0Baz!"]
    assert _bytes(out, "other/4") == b"\x01\x02\x03"
    assert _bytes(out, "Foo/Bar") == b"Foo|Bar!" and _bytes(out, "zarr.json~") == b"zarr.json~!"


def test_grid_cut_is_the_cut_of_contiguous_values():
    """source.grid_cut gives the chunk shape of common.grid_chunks, with each chunk
    limit (conventions/README.md §7), on chosen and on random shapes."""
    import random

    from vzip.virtualize.common import grid_chunks
    from vzip_reference.nd2.source import grid_chunk, grid_cut

    cases = [([1], 8), ([5, 7], 4), ([3_000_000], 8), ([2_097_153], 8), ([4096, 1024], 8), ([3, 5_000_000], 4),
             ([1000, 3, 7000], 8), ([2**21 + 1, 3], 8), ([65537, 257], 8), ([4195, 1000], 8), ([40, 2**21], 8)]
    rng = random.Random(3)
    while len(cases) < 600:
        shape = [rng.choice([1, 2, 3, 7, rng.randint(1, 60), rng.randint(1, 5000), rng.randint(1, 300_000)])
                 for _ in range(rng.randint(1, 3))]
        if math.prod(shape) <= 2**28:
            cases.append((shape, rng.choice([1, 4, 8])))
    for shape, item in cases:
        for limit in (2**24, 2**16, 2**12):
            a, c = grid_cut(shape, item, limit)
            if math.prod(shape[:a]) > 2**16:
                continue  # too many chunks for grid_chunks to list
            expected, chunks = grid_chunks(0, shape, item, lambda o, n: bytes(n), limit=limit)
            assert [1] * a + [c] + shape[a + 1:] == expected, (shape, item, limit)
            # The last grid index is in the last chunk, at its last element within the array.
            key, i, m = grid_chunk(shape, a, c, math.prod(shape) - 1)
            assert key == max(chunks) and i == m - 1, shape


def _level(name: bytes, records: list[bytes], table: bytes | None = None) -> bytes:
    """An LV level record (name: its UTF-16LE units with the NUL, or b"" for k = 0)."""
    head = bytes([11, len(name) // 2]) + name
    length = len(head) + 12 + sum(map(len, records))
    return head + struct.pack("<IQ", len(records), length) + b"".join(records) + (
        table if table is not None else bytes(8 * len(records)))


def test_lv_exact_decoding():
    """decode_lv with `exact`: lossless data decodes as without it, with zero or
    name-ordered offset tables, k = 0 empty names, and compression."""
    import zlib

    from vzip_reference.nd2.lv import decode_lv
    from vzip_reference.nd2.source import lv_json

    b_rec, a_rec = b"\x03\x02b\0\0\0\x01\0\0\0", b"\x03\x02a\0\0\0\x02\0\0\0"
    head = 2 + 4 + 12  # level "t"
    data = (b"\x01\x02a\0\0\0\x01" + b"\x06\x02n\0\0\0" + struct.pack("<Q", 0x7FF8000000000000)
            + _level(b"l\0\0\0", [b"\x03\x00\x07\0\0\0"])
            + _level(b"t\0\0\0", [b_rec, a_rec], struct.pack("<2Q", head + len(b_rec), head))
            + _level(b"u\0\0\0", [b_rec, a_rec], struct.pack("<2Q", head, head + len(b_rec))))
    expected = {"a": True, "n": {"float": "NaN"}, "l": [7], "t": {"b": 1, "a": 2}, "u": {"b": 1, "a": 2}}
    assert lv_json(decode_lv(data, exact=True)) == lv_json(decode_lv(data)) == expected
    zipped = b"\x4c\x00" + bytes(10) + zlib.compress(data)
    assert lv_json(decode_lv(zipped, exact=True)) == expected
    # A byte array is one value, a JSON array of its bytes; the inflated size is reported.
    inflated: list[int] = []
    assert lv_json(decode_lv(b"\x4c\x00" + bytes(10) + zlib.compress(b"\x09\x02b\0\0\0" + struct.pack("<Q", 3) + b"\1\2\3"),
                             100, inflated, room=5)) == {"b": [1, 2, 3]}
    assert inflated == [17]


@pytest.mark.parametrize("data,limit", [
    (b"\x4c\x00" + bytes(10) + zlib.compress(bytes(1000))[:-3], 10**6),  # truncated
    (b"\x4c\x00" + bytes(10) + zlib.compress(bytes(1000)) + b"x", 10**6),  # bytes after the stream
    (b"\x4c\x00" + bytes(10) + zlib.compress(bytes(1000)), 999),  # past the limit
])
def test_a_stream_that_does_not_inflate_is_charged_the_most_it_could(data, limit):
    """min(limit, 1032 x the data's length) (conventions/nd2/README.md §5.1, the budget)."""
    from vzip_reference.nd2.lv import decode_lv

    inflated: list[int] = []
    with pytest.raises(Rejected):
        decode_lv(data, limit, inflated)
    assert inflated == [min(limit, 1032 * len(data))]


def test_lv_data_past_its_room_is_too_large():
    """1 per record and 1 per byte of a byte array (each at least a byte of JSON)."""
    from vzip_reference.nd2.lv import TooLarge, decode_lv

    data = b"\x09\x02b\0\0\0" + struct.pack("<Q", 3) + b"\1\2\3"
    decode_lv(data, room=4)
    with pytest.raises(TooLarge):
        decode_lv(data, room=3)


@pytest.mark.parametrize("data,message", [
    (b"\x01\x02a\0\0\0\x02", "bool"),
    (b"\x03\x01\0\0\x01\0\0\0", "k = 0 when empty"),
    (b"\x03\x02a\0b\0\x01\0\0\0", "one NUL"),
    (b"\x03\x03a\0\0\0b\0\x01\0\0\0", "one NUL"),
    (_level(b"", [b"\x03\x00\x07\0\0\0"], b"\x01" + bytes(7)), "offset table"),
    (_level(b"", [b"\x03\x02b\0\0\0\x01\0\0\0", b"\x03\x02a\0\0\0\x02\0\0\0"],
            struct.pack("<2Q", 14, 25)), "offset table"),  # a wrong offset
    (_level(b"", [b"\x03\x00\x01\0\0\0", b"\x03\x00\x02\0\0\0"], struct.pack("<2Q", 14, 14)),
     "offset table"),  # one record twice
    (b"\x06\x02n\0\0\0" + struct.pack("<Q", 0xFFF8000000000000), "NaN"),
    (b"\x4c\x00" + bytes(10) + __import__("zlib").compress(b"\x01\x02a\0\0\0\x02"), "bool"),
])
def test_lv_exact_decoding_rejects(data, message):
    from vzip_reference.nd2.lv import Lossy, decode_lv

    decode_lv(data)  # it decodes, with a loss
    with pytest.raises(Lossy, match=message):
        decode_lv(data, exact=True)
