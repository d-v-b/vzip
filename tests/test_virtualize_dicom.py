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
    ("dicom_cine_frame_time.dcm",  # Frame Increment Pointer: Frame Time, so the frames are along t
     {"axes": ["t", "y", "x"], "shape": [5, 4, 6]}, ["bytes"], [33.3 / 1000, 1, 1], None),
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
    ("dicom_reject_increment_length.dcm", "multiple of 4"),
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
    ("dicom_reject_jpeg_soi.dcm", r"does not start with a JPEG SOI marker \(FF D8\)"),
    ("dicom_reject_pixel_past_eof.dcm", "104 bytes run past the end of the 514-byte file"),
    ("dicom_reject_eot_overlap.dcm", "Extended Offset Table's frames overlap"),
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
        # A number only when it is the text's decimal value exactly.
        ("DS", b"9007199254740993\\9007199254740992\\-0\\1.10E+2\\.1\\0.1000000000000000055511151231257827",
         True, {"vr": "DS", "Value": ["9007199254740993", 9007199254740992, 0, 110, 0.1,
                                      "0.1000000000000000055511151231257827"]}),
        ("IS", b"+12\\-9007199254740993", True, {"vr": "IS", "Value": [12, "-9007199254740993"]}),
        # Too long for Python's int(): by its digits without sign and leading zeros.
        ("IS", b"-00042\\" + b"0" * 5000 + b"12345678901234567\\+" + b"0" * 5000 + b"\\-" + b"9" * 5000, True,
         {"vr": "IS", "Value": [-42, "12345678901234567", 0, "-" + "9" * 5000]}),
        # An exponent of over 5 digits, without its sign and leading zeros: out of range unless the digits are 0.
        ("DS", b"1e" + b"0" * 5000 + b"1\\-2.5E+" + b"0" * 4999 + b"1\\1e-111111\\0.0e999999999\\1e99999", True,
         {"vr": "DS", "Value": [10, -25, "1e-111111", 0, "1e99999"]}),
        ("SH", b"caf\xe9", True, {"vr": "SH", "Value": [{"latin1": "café"}]}),  # not UTF-8: ISO 8859-1, tagged
        ("AT", struct.pack("<4H", 0x0028, 0x0010, 0x7FE0, 0x0010), True, {"vr": "AT", "Value": ["00280010", "7FE00010"]}),
        ("US", struct.pack(">2H", 1, 65535), False, {"vr": "US", "Value": [1, 65535]}),
        ("US", b"\x00\x01\x02", False, {"vr": "US", "InlineBinary": "AAEC"}),  # not whole values: as stored
        ("AT", b"\x28\x00\x10\x00\x28\x00", True, {"vr": "AT", "InlineBinary": "KAAQACgA"}),
        ("FD", struct.pack("<2d", 1.5, float("nan")), True, {"vr": "FD", "Value": [1.5, "NaN"]}),
        ("UV", struct.pack("<Q", 2**64 - 1), True, {"vr": "UV", "Value": ["18446744073709551615"]}),
        ("OB", b"\x00\x01", True, {"vr": "OB", "InlineBinary": "AAE="}),
        ("ST", b"", True, {"vr": "ST"}),
    ]
    for vr, data, little, expected in cases:
        assert value_json(vr, data, little) == expected, vr
    # Under a multibyte character set, the values of LO, PN, SH and UC are their
    # bytes, unsplit; the others are translated as before.
    gbk = "\u4e57^x".encode("gbk")  # 81 5C 5E 78
    assert value_json("PN", gbk, True, True) == {"vr": "PN", "InlineBinary": "gVxeeA=="}
    assert value_json("CS", b"A\\B", True, True) == {"vr": "CS", "Value": ["A", "B"]}


def test_json_size():
    """The size of an attribute as JSON.stringify writes it, which decides
    whether it stays on the root (conventions/dicom/README.md §5)."""
    from vzip.virtualize.dicom.source import json_size

    numbers = [1e21, 1e-7, 0.1, 9.0, -0.0, 123456789012345680000.0, 1.5e-6, -2.5e-300, 5e-324, 2**53]
    # As JSON.stringify writes them: 1e+21, 1e-7, 0.1, 9, 0, 123456789012345680000, 0.0000015, -2.5e-300, 5e-324,
    # 9007199254740992.
    assert json_size(numbers) == 2 + 9 + 5 + 4 + 3 + 1 + 1 + 21 + 9 + 9 + 6 + 16
    assert json_size({"a\u00e9\n\u0001": ["x\"", None, True]}) == len('{"a\u00e9\\n\\u0001":["x\\"",null,true]}'.encode())


def test_a_malformed_sequence_that_the_profile_does_not_walk_keeps_its_bytes():
    import struct

    from vzip.virtualize.dicom.dataset import EXPLICIT_LE
    from vzip.virtualize.dicom.source import Translator

    def el(group, element, vr, value):
        if vr in ("SQ", "OB", "UN", "UT"):
            return struct.pack("<HH2sHI", group, element, vr.encode(), 0, len(value)) + value
        return struct.pack("<HH2sH", group, element, vr.encode(), len(value)) + value

    bad_items = struct.pack("<HHI", 0x0008, 0x0016, 4) + b"oops"  # not an item tag
    good = el(0x0010, 0x0010, "PN", b"Doe^Jane")
    # Its bytes by the binary rule: inline up to 64 bytes, else an array.
    blob = el(0x0008, 0x1115, "SQ", bad_items) + el(0x0008, 0x1140, "SQ", bad_items + bytes(60)) + good
    t = Translator(lambda o, n: blob[o:o + n], len(blob))
    out, _ = t.dataset(0, len(blob), True, EXPLICIT_LE, 0, "dataset")
    assert out == {"00081115": {"vr": "SQ", "InlineBinary": "CAAWAAQAAABvb3Bz"},
                   "00081140": {"vr": "SQ", "BulkDataURI": "vzip_source/dataset/00081140"},
                   "00100010": {"vr": "PN", "Value": [{"Alphabetic": "Doe^Jane"}]}}
    (plan,) = t.plans()  # its bytes, where the file holds them
    assert (plan.path, plan.data_type, plan.shape, plan.dims) == ("dataset/00081140", "uint8", [len(bad_items) + 60],
                                                                  ["byte"])


def _bounds(tag: str) -> tuple[dict, list]:
    """In dicom_gathered_bounds.dcm, the attribute of tag `tag` of the
    dataset, and the references of the array of its bytes."""
    _, out = virtualize(str(FIXTURES / "dicom_gathered_bounds.dcm"), url="https://data.test/x.dcm")
    root, node, docs = _source("dicom_gathered_bounds.dcm")
    attribute = {**root["dataset"], **node.get("dataset", {})}[tag]
    assert docs[f"vzip_source/dataset/{tag}/zarr.json"]["shape"] == [sum(n for _, n in out.refs[
        f"vzip_source/dataset/{tag}/c/0"])]
    assert not [k for k in docs if k.startswith(f"vzip_source/dataset/{tag}/items/")]  # nothing gathered
    return attribute, out.refs[f"vzip_source/dataset/{tag}/c/0"]


def test_a_gathered_sequence_of_over_1024_paths_keeps_its_bytes():
    attribute, refs = _bounds("52009230")  # one item, of 1025 private values
    assert attribute == {"vr": "SQ", "BulkDataURI": "vzip_source/dataset/52009230"}
    assert sum(n for _, n in refs) == 8 + 12 + 1025 * 10


def test_a_gathered_sequence_whose_structures_are_over_64_kib_keeps_its_bytes():
    attribute, refs = _bounds("00081250")  # 65 items, 3000 elements without a value in the first
    assert attribute == {"vr": "SQ", "BulkDataURI": "vzip_source/dataset/00081250"}
    assert sum(n for _, n in refs) == 65 * 8 + 3000 * 8


def test_a_gathered_sequence_whose_arrays_cost_over_16_times_its_bytes_keeps_its_bytes():
    attribute, refs = _bounds("00081140")  # undefined length, 65 items, 64 families of two values
    assert attribute == {"vr": "SQ", "BulkDataURI": "vzip_source/dataset/00081140"}
    assert sum(n for _, n in refs) == 65 * 8 + 64 * (10 + 12)  # without its sequence delimiter


def test_dictionary_vr():
    """The VR of an element whose file does not state it (conventions/dicom/README.md §5)."""
    from vzip.virtualize.dicom.source import dictionary_vr

    cases = {
        0x00280010: "US",  # Rows
        0x00081140: "SQ",  # Referenced Image Sequence
        0x60000010: "US",  # Overlay Rows, by the mask 60xx0010
        0x60020010: "US",
        0x00290010: "LO",  # a private creator
        0x00291010: None,  # a private element
        0x00280106: None,  # Smallest Image Pixel Value: US or SS, ambiguous
        0x7FE00010: None,  # Pixel Data: OB or OW
        0x0FF00100: None,  # not in the dictionary
    }
    for tag, vr in cases.items():
        assert dictionary_vr(tag) == vr, f"{tag:08X}"


def test_elements_without_a_stated_vr_take_the_dictionarys():
    import struct

    from vzip.virtualize.dicom.dataset import EXPLICIT_LE, IMPLICIT_LE
    from vzip.virtualize.dicom.source import Translator

    def implicit(group, element, value):
        return struct.pack("<HHI", group, element, len(value)) + value

    item = implicit(0xFFFE, 0xE000, implicit(0x0008, 0x1150, b"1.2\0"))
    blob = (implicit(0x0008, 0x1140, item)  # SQ by the dictionary, of defined length
            + implicit(0x0010, 0x0010, b"Doe^Jane")
            + implicit(0x0028, 0x0010, struct.pack("<H", 512))
            + implicit(0x0028, 0x0106, struct.pack("<H", 7))  # ambiguous: UN
            + implicit(0x0040, 0xA730, b"not items")  # SQ by the dictionary, but not a sequence: UN
            + implicit(0x0018, 0x1310, b"\x01\x02\x03"))  # US by the dictionary, but not whole values
    out, _ = Translator(lambda o, n: blob[o:o + n], len(blob)).dataset(0, len(blob), True, IMPLICIT_LE, 0, "dataset")
    assert out == {
        "00081140": {"vr": "SQ", "Value": [{"00081150": {"vr": "UI", "Value": ["1.2"]}}]},
        "00100010": {"vr": "PN", "Value": [{"Alphabetic": "Doe^Jane"}]},
        "00280010": {"vr": "US", "Value": [512]},
        "00280106": {"vr": "UN", "InlineBinary": "BwA="},
        "0040A730": {"vr": "UN", "InlineBinary": "bm90IGl0ZW1z"},
        "00181310": {"vr": "US", "InlineBinary": "AQID"},
    }
    # An explicit UN element is typed the same way.
    un = struct.pack("<HH2sHI", 0x0028, 0x0010, b"UN", 0, 2) + struct.pack("<H", 64)
    out, _ = Translator(lambda o, n: un[o:o + n], len(un)).dataset(0, len(un), True, EXPLICIT_LE, 0, "dataset")
    assert out == {"00280010": {"vr": "US", "Value": [64]}}


def test_large_values_are_arrays_and_nothing_after_pixel_data_is_lost():
    """conventions/dicom/README.md §5: arrays named by BulkDataURI, per-frame groups on
    vzip_source, elements after Pixel Data, and no layout or padding."""
    _, out = virtualize(str(FIXTURES / "dicom_reconstruction.dcm"), url="https://data.test/r.dcm")
    dataset = json.loads(out.bytes_entries["zarr.json"])["attributes"]["vzip_virtualized"]["dicom"]["dataset"]
    node = json.loads(out.bytes_entries["vzip_source/zarr.json"])["attributes"]["vzip_virtualized"]["dicom"]
    bulk = {k: v["BulkDataURI"] for k, v in dataset.items() if "BulkDataURI" in v}
    assert bulk == {"00091010": "vzip_source/dataset/00091010", "00283006": "vzip_source/dataset/00283006",
                    "7FE11001": "vzip_source/dataset/7FE11001"}
    icons = dataset["00880200"]["Value"]
    assert "InlineBinary" in icons[0]["7FE00010"]  # 64 bytes
    assert icons[1]["7FE00010"] == {"vr": "OB", "BulkDataURI": "vzip_source/dataset/00880200/1/7FE00010"}
    assert dataset["7FE10010"] == {"vr": "LO", "Value": ["VZIP AFTER"]}  # after Pixel Data
    assert "FFFCFFFC" not in dataset and "52009230" not in dataset
    assert list(node["dataset"]) == ["52009230"]
    lut = json.loads(out.bytes_entries["vzip_source/dataset/00283006/zarr.json"])
    assert (lut["data_type"], lut["shape"]) == ("uint16", [100])


def _source(name: str):
    _, out = virtualize(str(FIXTURES / name), url=f"https://data.test/{name}")
    docs = {k: json.loads(v) for k, v in out.bytes_entries.items() if k.endswith("zarr.json")}
    root = docs["zarr.json"]["attributes"]["vzip_virtualized"]["dicom"]
    node = docs.get("vzip_source/zarr.json", {}).get("attributes", {}).get("vzip_virtualized", {}).get("dicom", {})
    return root, node, docs


def _items(name: str, sequence: str = "dataset/52009230") -> tuple[list[dict], list[int]]:
    """The distinct structures of the items of a file's gathered sequence at
    path `sequence`, and each item's."""
    import struct

    _, out = virtualize(str(FIXTURES / name), url=f"https://data.test/{name}")
    root, node, _ = _source(name)
    first, *names = sequence.split("/")
    attribute = {**root[first], **node.get(first, {})}
    for k in names:
        attribute = attribute[k] if len(k) == 8 else attribute["Value"][int(k)]  # a tag, or an item's index
    path = f"vzip_source/{sequence}/items/structure"
    doc = json.loads(out.bytes_entries[f"{path}/zarr.json"])
    assert (doc["data_type"], doc["dimension_names"]) == ("int32", ["index"])
    (n,) = doc["shape"]
    chunk = b"".join(out.bytes_entries[f"{path}/c/{q}"] for q in range(-(-n // doc["chunk_grid"]["configuration"]
                                                                                     ["chunk_shape"][0])))
    return attribute["Structures"], list(struct.unpack(f"<{n}i", chunk[:4 * n]))


def test_the_source_metadata_keeps_what_the_file_holds():
    """conventions/dicom/README.md §5: duplicates, values under a multibyte
    character set, the preamble, large values as arrays (or on vzip_source),
    per-frame values gathered by frame, and the bytes of the pixel data that no
    frame holds. web/test/dicom/verify.py checks the values against pydicom."""
    root, _, docs = _source("dicom_duplicates.dcm")
    dataset = root["dataset"]
    assert dataset["00100010"]["Value"] == [{"Alphabetic": "First^Name"}]
    assert dataset["duplicates"] == [  # in file order, with all their items
        {"00081140": {"vr": "SQ", "Value": [{"00081155": {"vr": "UI", "Value": ["4.5.6"]}},
                                            {"00081155": {"vr": "UI", "Value": ["7.8"]}}]}},
        {"00100010": {"vr": "PN", "Value": [{"Alphabetic": "Second^Name"}]}}]
    assert dataset["00082112"]["Value"][0]["duplicates"] == [{"00080100": {"vr": "SH", "Value": ["B2"]}}]
    # After Pixel Data, a sequence of undefined length is read; a duplicate ends the elements.
    assert dataset["7FE11010"] == {"vr": "SQ", "Value": [{"00080100": {"vr": "SH", "Value": ["C3"]}}]}
    assert root["trailing"] == "vzip_source/trailing"
    assert docs["vzip_source/trailing/zarr.json"]["dimension_names"] == ["byte"]

    root, _, _ = _source("dicom_charset_gbk.dcm")
    dataset = root["dataset"]
    assert dataset["00080080"] == {"vr": "LO", "InlineBinary": "gVwgSG9zcGl0YWwg"}  # 81 5C: unsplit
    inherits, utf8 = dataset["0040A043"]["Value"]
    assert inherits["00080104"] == {"vr": "LO", "InlineBinary": "gVw="}
    assert utf8["00080104"] == {"vr": "LO", "Value": ["\u00dcn\u00efcode"]}  # its own character set
    root, _, _ = _source("dicom_charset_jis.dcm")
    assert set(root["dataset"]["00100010"]) == {"vr", "InlineBinary"}

    root, node, docs = _source("dicom_large_values.dcm")
    dataset = root["dataset"]
    assert root["preamble"].startswith("VlpJUCBQUkVBTUJMRS4u")  # "VZIP PREAMBLE.."
    bulk = {k: v["BulkDataURI"] for k, v in dataset.items() if "BulkDataURI" in v}
    assert bulk == {k: f"vzip_source/dataset/{k}" for k in ("00081160", "00091003", "0040A160", "30060050")}
    for k, (data_type, shape) in {"00081160": ("uint8", [200]), "30060050": ("uint8", [510]),
                                  "0040A160": ("uint8", [70000]), "00091003": ("uint8", [282])}.items():
        doc = docs[f"vzip_source/dataset/{k}/zarr.json"]
        assert (doc["data_type"], doc["shape"], doc["dimension_names"]) == (data_type, shape, ["byte"]), k
    assert dataset["00091001"] == {"vr": "US", "InlineBinary": "AQID"}
    assert dataset["00181050"]["Value"] == ["9007199254740993"]
    # 300 items: gathered, so small enough for the root.
    assert set(dataset["00081115"]) == {"vr", "Structures"} and "00081115" not in node.get("dataset", {})
    assert len(_items("dicom_large_values.dcm", "dataset/00081115")[1]) == 300

    # Per-frame values: every value gathered by its path within the item,
    # the items keeping only their structure.
    _, node, docs = _source("dicom_per_frame_values.dcm")
    structures, frames = _items("dicom_per_frame_values.dcm")  # items that differ only in their values
    assert frames == [0, 0, 0, 0] and len(structures) == 1 and set(node["dataset"]["52009230"]) == {"vr", "Structures"}
    base = "vzip_source/dataset/52009230/items"
    assert structures[0]["00091002"] == {"vr": "US", "Gathered": True}
    for k, (data_type, shape, dims) in {"00091001/value": ("uint8", [4, 100], ["index", "byte"]),
                                        "00091002/value": ("uint16", [4, 70], ["index", "value"]),
                                        "00091003/value/offsets": ("int64", [5], ["index"]),
                                        "00090010/value": ("uint8", [4, 4], ["index", "byte"])}.items():
        doc = docs[f"{base}/{k}/zarr.json"]
        assert (doc["data_type"], doc["shape"], doc["dimension_names"]) == (data_type, shape, dims), k
    # Columns of typed values, of text, and families; a value absent in an
    # item; chunks of several rows.
    _, node, docs = _source("dicom_per_frame_columns.dcm")
    structures, frames = _items("dicom_per_frame_columns.dcm")
    assert frames == [0, 1, 2, 3, 2]  # a group length kept in item 0, a private creator in 1, no UL in 3
    second = structures[frames[1]]
    assert second["00209113"]["Value"][0]["00200032"] == {"vr": "DS", "Gathered": True}
    assert second["00189114"] == {"vr": "SQ", "Value": []} and "00280000" not in second
    assert structures[frames[0]]["00280000"] == {"vr": "UL", "Gathered": True}
    assert "00209057" not in structures[frames[3]]["00209111"]["Value"][0]
    for k, (data_type, shape, chunks) in {
            "00209113/0/00200032": ("float64", [5, 3], [5, 3]),  # DS, every value a number
            "00209111/0/00209128": ("int64", [5, 1], [5, 1]),  # IS
            "00209111/0/00209057": ("uint32", [5, 1], [5, 1]),  # UL, absent in item 3
            "00209111/0/00189074": ("uint8", [5, 22], [5, 22]),  # DT: its text
            "00289145/0/00281050": ("uint8", [5, 22], [5, 22]),  # DS, one not a binary64 value: its text
            "00090010": ("uint8", [5, 4], [1, 4]),  # in item 1 only: sparse, a chunk per row
            "00280000": ("uint32", [5, 1], [1, 1])}.items():  # a group length, layout but in item 0: sparse
        doc = docs[f"{base}/{k}/value/zarr.json"]
        assert (doc["data_type"], doc["shape"], doc["chunk_grid"]["configuration"]["chunk_shape"]) == (
            data_type, shape, chunks), k
    assert docs[f"{base}/00289145/0/00281051/value/offsets/zarr.json"]["shape"] == [6]  # of different lengths
    # Paths that nest across the items (a value in one, a sequence that holds
    # one in the other): each array is the leaf `value` of its path's group.
    _, node, docs = _source("dicom_per_frame_nested.dcm")
    structures, frames = _items("dicom_per_frame_nested.dcm")
    first, second = (structures[k] for k in frames)
    assert first["00209111"] == {"vr": "SQ", "Gathered": True}  # breaks a rule: its bytes
    assert second["00209111"]["Value"][0]["00091001"] == {"vr": "OB", "Gathered": True}
    assert f"{base}/00209111/value/zarr.json" in docs and f"{base}/00209111/0/00091001/value/zarr.json" in docs
    assert f"{base}/00091001/value/zarr.json" in docs and f"{base}/00091001/0/00091002/value/zarr.json" in docs
    arrays = [k[:-len("/zarr.json")] for k, d in docs.items() if d["node_type"] == "array"]
    assert not [(a, b) for a in arrays for b in arrays if b.startswith(a + "/")]  # no array has children

    # Any sequence of more than 64 items is gathered, at any depth: of
    # defined length, nested in an item of a sequence that is not gathered; of
    # undefined length, with items of undefined length; and the sequences in
    # its items are not. One of 64 items is not.
    root, node, docs = _source("dicom_gathered_sequences.dcm")
    dataset = root["dataset"]
    structures, each = _items("dicom_gathered_sequences.dcm", "dataset/00081115/0/0008114A")
    assert len(each) == 100 and each[0] == 0 and set(each[1:]) == {1} and len(structures) == 2
    assert structures[1] == {"00081150": {"vr": "UI", "Gathered": True}, "00081155": {"vr": "UI", "Gathered": True}}
    assert len(structures[0]["0040A170"]["Value"]) == 66  # in a gathered item: its values gathered with the item's
    base = "vzip_source/dataset/00081115/0/0008114A/items"
    for k, (data_type, shape, chunks) in {
            "00081150/value": ("uint8", [100, 26], [100, 26]),  # in every item
            "0040A170/65/00080100/value": ("uint8", [100, 5], [1, 5]),  # in item 0 only: sparse
            "00081155/value/offsets": ("int64", [101], [101])}.items():  # of different lengths: a family
        doc = docs[f"{base}/{k}/zarr.json"]
        assert (doc["data_type"], doc["shape"], doc["chunk_grid"]["configuration"]["chunk_shape"]) == (
            data_type, shape, chunks), k
    structures, each = _items("dicom_gathered_sequences.dcm", "dataset/00082112")
    assert len(each) == 70 and len(structures) == 2  # with the private value (in every 7th item), and without
    assert docs["vzip_source/dataset/00082112/items/00091001/value/zarr.json"]["chunk_grid"]["configuration"][
        "chunk_shape"] == [1, 2]
    assert len(dataset["0040A730"]["Value"]) == 64  # not gathered
    structures, each = _items("dicom_gathered_sequences.dcm")
    assert len(each) == 80 and len(structures) == 2
    for k, (data_type, chunks) in {"00209111/0/00209057": ("uint32", [80, 1]),
                                   "00291001": ("float64", [1, 1])}.items():  # in 3 of 80 items: sparse
        doc = docs[f"vzip_source/dataset/52009230/items/{k}/value/zarr.json"]
        assert (doc["data_type"], doc["chunk_grid"]["configuration"]["chunk_shape"]) == (data_type, chunks), k
    _, out = virtualize(str(FIXTURES / "dicom_gathered_sequences.dcm"), url="https://data.test/x.dcm")
    sparse = sorted(k for k in out.refs if k.startswith("vzip_source/dataset/52009230/items/00291001/value/c/"))
    assert sparse == [f"vzip_source/dataset/52009230/items/00291001/value/c/{f}/0" for f in (3, 40, 79)]
    # An explicit UN value in big endian keeps its bytes in little endian, and says so.
    root, node, docs = _source("dicom_un_bigendian_gathered.dcm")
    assert root["dataset"]["00091001"] == {"vr": "UN", "InlineBinary": "AQACAA==", "LittleEndian": True}
    first, second = _items("dicom_un_bigendian_gathered.dcm")[0]
    assert first["00209111"]["Value"][0]["00209057"] == {"vr": "UL", "Gathered": True}
    assert second["00209111"]["Value"][0]["00209057"] == {"vr": "UL", "Gathered": True, "LittleEndian": True}
    doc = docs["vzip_source/dataset/52009230/items/00209111/0/00209057/value/zarr.json"]
    assert (doc["data_type"], doc["shape"]) == ("uint8", [2, 4])  # bytes as stored: in both byte orders

    # Group lengths that are not their run's length, of a private group, or
    # of a tag that occurs twice, are kept; offset tables the profile did not
    # read the frames from are kept, and so is a later table of a tag it did.
    root, _, _ = _source("dicom_layout_tags.dcm")
    dataset = root["dataset"]
    assert "00020000" not in root["meta"] and "00080000" not in dataset
    assert "00080000" not in dataset["00081140"]["Value"][0]
    assert dataset["00081140"]["Value"][0]["7FE00001"] == {"vr": "OV", "InlineBinary": "BwAAAAAAAAA="}
    assert dataset["00090000"]["Value"] == [12] and dataset["00100000"]["Value"] == [99]
    assert dataset["00180000"]["Value"] == [12] and dataset["duplicates"] == [{"00180000": {"vr": "UL", "Value": [0]}}]
    assert dataset["7FE00001"] == {"vr": "OV", "InlineBinary": "Fc1bBwAAAAA="}  # after Pixel Data
    assert dataset["00209222"] == {"vr": "SQ", "InlineBinary": "EAAQAFBOAgBBQg=="}  # breaks a rule: inline
    root, _, _ = _source("dicom_eot_duplicate.dcm")
    dataset = root["dataset"]
    assert "7FE00000" not in dataset and "7FE00001" not in dataset and "7FE00002" not in dataset
    assert [list(d) for d in dataset["duplicates"]] == [["7FE00001"], ["7FE00002"]]
    # UN values in explicit VR big endian are in little endian.
    root, _, docs = _source("dicom_un_bigendian.dcm")
    assert root["dataset"]["00181310"] == {"vr": "US", "Value": [0, 256, 256, 0]}
    assert root["dataset"]["00189087"] == {"vr": "FD", "Value": [1000]}
    assert docs["vzip_source/dataset/00409212/zarr.json"]["codecs"][0]["configuration"]["endian"] == "little"
    # The fragments of frames of several, and whether the Basic Offset Table is empty.
    for name, fragments in (("dicom_jpeg_empty_fragment.dcm", [4, 2]), ("dicom_jpeg_single_frame_fragments.dcm", None),
                            ("dicom_j2k_rgb_nobot.dcm", None)):
        root, _, docs = _source(name)
        assert root.get("pixel_offset_table", False) == (name == "dicom_jpeg_empty_fragment.dcm"), name
        if fragments is not None:
            assert docs["vzip_source/pixel_fragments/zarr.json"]["shape"] == fragments, name
        assert ("pixel_fragments" in root) == (name != "dicom_j2k_rgb_nobot.dcm"), name

    root, _, docs = _source("dicom_pixel_extra.dcm")
    assert docs["vzip_source/pixel_extra/zarr.json"]["shape"] == [6]
    root, _, docs = _source("dicom_eot_unreferenced.dcm")
    assert root["pixel_unreferenced"] == "vzip_source/pixel_unreferenced"
    assert "pixel_unreferenced_headers" not in root
    assert docs["vzip_source/pixel_unreferenced/offsets/zarr.json"]["shape"] == [4]  # 3 frames
    # A frame whose 8 bytes before its data are not its item header: every
    # frame's are kept, with the bytes between the frames.
    root, _, docs = _source("dicom_eot_item_headers.dcm")
    assert root["pixel_unreferenced_headers"] is True
    _, out = virtualize(str(FIXTURES / "dicom_eot_item_headers.dcm"), url="https://data.test/x.dcm")
    assert [n for _, n in out.refs["vzip_source/pixel_unreferenced/data/c/0"]] == [8, 8, 10]  # 2 bytes of padding

    # Numbers too long to convert.
    root, _, _ = _source("dicom_long_numbers.dcm")
    assert root["dataset"]["00200012"]["Value"] == ["-" + "9" * 4999]
    assert root["dataset"]["00201041"]["Value"] == [10, "1e-" + "1" * 5001, 0]

    # The root is kept small: a member of meta over 16 KiB, then, while meta
    # and dataset are over 64 KiB together, the largest left, the first by tag.
    root, node, _ = _source("dicom_root_budget.dcm")
    assert list(node) == ["meta", "dataset"]
    assert list(node["meta"]) == ["duplicates"] and len(node["meta"]["duplicates"]) == 20
    assert list(node["dataset"]) == ["00111001"]  # of 15002 bytes, as 00111003 is
    assert "00111003" in root["dataset"] and "00020013" in root["meta"]
    assert len(json.dumps([root["meta"], root["dataset"]], separators=(",", ":"))) - 3 <= 1 << 16
    # Nothing else keeps these.
    root, node, _ = _source("dicom_explicit_signed16_frames.dcm")
    assert set(root) == {"meta", "dataset"} and node == {}
