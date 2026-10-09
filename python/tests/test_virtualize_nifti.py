"""The Python NIfTI virtualizer (spec/virtualize/nifti/profile.md) on the synthetic fixtures.

Equivalence with the browser virtualizer is checked by
conformance/virtualize/compare.py, and pixel correctness against nibabel by
js/test/nifti/verify.py.
"""

import json
import struct
from pathlib import Path

import pytest

from vzip.virtualize import Rejected, virtualize

FIXTURES = Path(__file__).parents[2] / "fixtures" / "nifti"

MM = "millimeter"
# fixture: (summary members, [(axis, unit)], scale, translation, `nifti` attribute, first omero window,
#           (key, range) of one chunk)
CASES = {
    "nifti_n1_le_int16_3d_sform": (
        {"version": 1, "byteOrder": "little", "dataType": "int16", "affine": "sform"},
        [("z", MM), ("y", MM), ("x", MM)], [4, 3, 2], [7, 20.25, -10.5], None, None,
        ("0/c/0/0/0", [(352, 120)])),
    "nifti_n1_be_int16_4d_scaled": (
        {"byteOrder": "big", "sizes": {"t": 5, "z": 2, "y": 3, "x": 4}, "scaling": {"slope": 0.5, "inter": -20}},
        [("t", "second"), ("z", MM), ("y", MM), ("x", MM)], [2.5, 3, 1.5, 1.5], None,
        {"slope": 0.5, "inter": -20}, {"min": -20, "max": 60, "start": -20, "end": 60},
        ("0/c/0/0/0/0", [(352, 240)])),
    "nifti_n1_le_qform_identity": (
        {"affine": "qform", "dataType": "uint16"}, [("z", "micrometer"), ("y", "micrometer"), ("x", "micrometer")],
        [2, 0.5, 0.25], [0.125, -50.5, 100], None, None, None),
    "nifti_n1_be_qform_oblique": ({"affine": "qform", "translation": False}, None, [1.2999999523162842, 1.2000000476837158, 1.100000023841858], None, None, None, None),
    "nifti_n1_le_qfac_negative": ({"affine": "qform", "translation": False}, None, [1, 1, 1], None, None, None, None),
    "nifti_n2_le_float32_4d": (
        {"version": 2, "dataType": "float32"}, [("t", "millisecond"), ("z", MM), ("y", MM), ("x", MM)],
        [750, 4, 3, 2], [0, 7, 20.25, -10.5], None, {"min": 0, "max": 200, "start": 0, "end": 200}, None),
    "nifti_n2_be_float64_5d": (
        {"sizes": {"t": 2, "c": 3, "z": 2, "y": 2, "x": 3}, "byteOrder": "big"},
        [("t", "microsecond"), ("c", None), ("z", "meter"), ("y", "meter"), ("x", "meter")],
        [0.5, 1, 0.002, 0.001, 0.001], None, None, {"min": -5, "max": 5, "start": -5, "end": 5},
        # One chunk across c and t, which the file stores c first (transposed).
        ("0/c/0/0/0/0/0", [(544, 576)])),
    "nifti_n1_be_rgb24_4d": (
        {"color": True, "scaling": None, "sizes": {"t": 2, "c": 3, "z": 2, "y": 3, "x": 2}}, None, None, None, None,
        {"min": 0, "max": 255, "start": 0, "end": 255}, ("0/c/0/0/0/0/0", [(352, 72)])),
    "nifti_n2_be_rgba32": ({"color": True, "dataType": "uint8"}, None, None, None, None,
                           {"min": 0, "max": 255, "start": 0, "end": 255}, None),
    "nifti_n1_le_rgba32_2d": ({"extensions": True, "sizes": {"c": 4, "y": 2, "x": 3}}, None, None, None, None,
                              {"min": 0, "max": 255, "start": 0, "end": 255}, ("0/c/0/0/0", [(400, 24)])),
    "nifti_nibabel_n2_vector_ext": ({"version": 2, "extensions": True, "sizes": {"t": 2, "c": 3, "z": 3, "y": 4, "x": 5}},
                                    None, [250, 1, 2, 2, 2], [0, 0, 0, 0, 0], None, None, None),
    "nifti_nibabel_rgb24": ({"color": True, "sizes": {"c": 3, "z": 2, "y": 3, "x": 4}}, None, None, [0, 0, 0, 0], None,
                            {"min": 0, "max": 255, "start": 0, "end": 255}, None),
    "nifti_dtype_uint64": ({"dataType": "uint64"}, None, None, None, None, None, None),
    "nifti_dtype_float32_be": ({"dataType": "float32", "byteOrder": "big"}, None, None, None, None, None, None),
    "nifti_dims_1d": ({"sizes": {"y": 1, "x": 7}}, None, None, None, None, None, None),
    "nifti_dims_7d_ones": ({"sizes": {"t": 2, "c": 2, "z": 2, "y": 2, "x": 3}}, None, None, None, None, None, None),
    "nifti_dims_unread_beyond_n": ({"sizes": {"z": 2, "y": 2, "x": 3}}, None, None, None, None, None, None),
    "nifti_edge_pixdim_invalid": ({}, [("t", None), ("z", None), ("y", None), ("x", None)], [1, 1, 1, 1], None, None, None, None),
    "nifti_edge_units_unknown": ({}, [("t", None), ("z", None), ("y", None), ("x", None)], [2, 2, 2, 2], None, None, None, None),
    "nifti_edge_n2_units_negative": ({}, [("t", None), ("z", None), ("y", None), ("x", None)], None, None, None, None, None),
    "nifti_edge_slope_zero": ({"scaling": None}, None, None, None, None, {"min": 1, "max": 2, "start": 1, "end": 2}, None),
    "nifti_edge_slope_nan": ({"scaling": None}, None, None, None, None, None, None),
    "nifti_edge_inter_inf": ({}, None, None, None, {"slope": 2, "inter": 0},
                             {"min": -2, "max": 4, "start": -2, "end": 4}, None),
    "nifti_edge_slope_negative": ({}, None, None, None, {"slope": -2, "inter": 1},
                                  {"min": -3.5, "max": 2.5, "start": -3.5, "end": 2.5}, None),
    "nifti_edge_slope_trivial": ({"scaling": None}, None, None, None, None, None, None),
    "nifti_edge_cal_reversed": ({}, None, None, None, None, None, None),
    "nifti_edge_sform_set_not_representable": ({"affine": "sform", "translation": False}, None, None, None, None, None, None),
    "nifti_edge_sform_nan": ({"affine": "sform", "translation": False}, None, None, None, None, None, None),
    "nifti_edge_sform_code_negative": ({"affine": "qform"}, None, [1, 1, 1], [3.5, 2.5, 1.5], None, None, None),
    "nifti_edge_qform_2d": ({}, [("y", MM), ("x", MM)], [1, 1], [2, 1], None, None, None),
    "nifti_edge_vox_offset_padded": ({}, None, None, None, None, None, ("0/c/0/0/0", [(480, 24)])),
    # Chunks of at most 2^17 bytes, cut as conventions §7 cuts contiguous values.
    "nifti_rowblock_split": ({"chunkShape": [1, 350, 200], "chunks": 2}, None, None, None, None, None,
                             ("0/c/0/1/0", [(352 + 70000, 70000)])),
    "nifti_block_rows_edge_rgba": ({"chunkShape": [4, 110, 150], "chunks": 2}, None, None, None, None,
                                   {"min": 0, "max": 255, "start": 0, "end": 255},
                                   ("0/c/0/1/0", [(352 + 66000, 66000)])),
    "nifti_block_x_split": ({"chunkShape": [1, 10000], "chunks": 2, "byteOrder": "big"}, None, None, None,
                            None, None, ("0/c/0/1", [(352 + 80000, 80000)])),
    "nifti_block_x_split_rgb24": ({"chunkShape": [3, 1, 1, 22500], "chunks": 4}, None, None, None, None,
                                  {"min": 0, "max": 255, "start": 0, "end": 255},
                                  ("0/c/0/1/0/1", [(544 + 135000 + 67500, 67500)])),
    "nifti_chunk_time_series": ({"chunkShape": [20000, 1, 1, 1], "chunks": 2}, None, None, None, None, None,
                                ("0/c/1/0/0/0", [(544 + 80000, 80000)])),
    # Over 64 channels: no display windows, though cal_min and cal_max are set.
    "nifti_chunk_many_channels": ({"chunkShape": [3, 100, 1, 2, 2], "chunks": 1}, None, None, None, None, None,
                                  ("0/c/0/0/0/0/0", [(544, 2400)])),
    # The edge chunk's one byte of padding is a literal.
    "nifti_chunk_edge_literal": ({"chunkShape": [1, 65537], "chunks": 2}, None, None, None, None, None,
                                 ("0/c/0/1", [(544 + 65537, 65536), b"\0"])),
    "nifti_header_latin1": ({}, None, None, None, None, None, None),
    "nifti_edge_n2_window_overflow": ({"version": 2}, None, None, None, {"slope": 1, "inter": -1.7e308}, None, None),
}


def test_virtualizes_the_synthetic_files():
    for name, (summary, axes, scale, translation, scaling, window, chunk) in CASES.items():
        fmt, out = virtualize(str(FIXTURES / f"{name}.nii"), url=f"https://data.test/{name}.nii")
        assert fmt == "nifti", name
        assert out.url == f"https://data.test/{name}.nii"
        assert {**out.summary, **summary} == out.summary, name
        attributes = json.loads(out.bytes_entries["zarr.json"])["attributes"]
        ms = attributes["ome"]["multiscales"][0]
        transforms = ms["datasets"][0]["coordinateTransformations"]
        if axes is not None:
            assert [(a["name"], a.get("unit")) for a in ms["axes"]] == axes, name
        if scale is not None:
            assert transforms[0]["scale"][-len(scale):] == scale, name
        assert (transforms[1]["translation"] if len(transforms) > 1 else None) == translation, name
        assert set(attributes) == {"ome", "zarr_conventions", "vzip_virtualized"}, name
        meta = attributes["vzip_virtualized"]["nifti"]  # spec/virtualize/nifti.md §5
        assert meta.get("scaling") == scaling, name
        assert meta["header"]["magic"] == f"n+{meta['nifti_version']}", name
        omero = attributes["ome"].get("omero")
        assert (omero["channels"][0]["window"] if omero else None) == window, name
        if chunk is not None:
            assert out.refs[chunk[0]] == chunk[1], name
        assert out.data == {}, name  # no data sources (spec/virtualize/nifti/profile.md §7.2)
    # The transpose codec puts the axes in the file's order: dimension 5 first, color samples last.
    for name, order in {"nifti_n2_be_float64_5d": [1, 0, 2, 3, 4], "nifti_n1_be_rgb24_4d": [0, 2, 3, 4, 1],
                        "nifti_n1_le_int16_3d_sform": None}.items():
        _, out = virtualize(str(FIXTURES / f"{name}.nii"), url="https://data.test/x.nii")
        codecs = json.loads(out.bytes_entries["0/zarr.json"])["codecs"]
        transpose = [c["configuration"]["order"] for c in codecs if c["name"] == "transpose"]
        assert transpose == ([order] if order else []), name


@pytest.mark.parametrize("name,message", [
    ("nifti_reject_dim0", "dim\\[0\\] = 0"),
    ("nifti_reject_dim0_8", "dim\\[0\\] = 8"),
    ("nifti_reject_dim_zero", "dim\\[2\\] = 0"),
    ("nifti_reject_dim_negative", "dim\\[2\\] = -2"),
    ("nifti_reject_n2_dim_huge", "dim\\[1\\] = 9007199254740992"),
    ("nifti_reject_dim6", "dimensions 6 and 7"),
    ("nifti_reject_dim7", "dimensions 6 and 7"),
    ("nifti_reject_complex64", "COMPLEX64"),
    ("nifti_reject_float128", "FLOAT128"),
    ("nifti_reject_binary", "BINARY"),
    ("nifti_reject_datatype_unknown", "unknown NIfTI datatype 3"),
    ("nifti_reject_bitpix", "bitpix 8"),
    ("nifti_reject_rgb_5d", "color data with a fifth dimension"),
    ("nifti_reject_vox_offset_fraction", "not an integer"),
    ("nifti_reject_vox_offset_nan", "not an integer"),
    ("nifti_reject_vox_offset_zero", "vox_offset 0 is not from 352"),
    ("nifti_reject_vox_offset_in_extender", "vox_offset 348 is not from 352"),
    ("nifti_reject_n2_vox_offset_low", "vox_offset 540 is not from 544"),
    ("nifti_reject_truncated", "outside the 375-byte file"),
    ("nifti_reject_n2_size_overflow", "voxel data"),
    ("nifti_reject_n2_short_header", "too short"),
    ("nifti_reject_pair_magic", "not a TIFF, NDPI, ND2, DICOM"),
])
def test_rejects(name, message):
    with pytest.raises(Rejected, match=message):
        virtualize(str(FIXTURES / f"{name}.nii"), url="https://data.test/x.nii")


def test_extensions_and_trailing_bytes_are_kept():
    """spec/virtualize/nifti.md §5: text extensions as text, binary ones as arrays,
    the bytes the chain does not hold and the bytes after the voxels as bytes."""
    _, out = virtualize(str(FIXTURES / "nifti_reconstruction.nii"), url="https://data.test/r.nii")
    meta = json.loads(out.bytes_entries["zarr.json"])["attributes"]["vzip_virtualized"]["nifti"]
    assert meta["extensions"] == [{"ecode": 6, "text": "hello"}, {"ecode": 2, "data": "vzip_source/extensions/1"}]
    assert (meta["extensions_truncated"], meta["unparsed"], meta["trailing"]) == (
        True, "vzip_source/unparsed", "vzip_source/trailing")
    shapes = {k: json.loads(v)["shape"] for k, v in out.bytes_entries.items()
              if k.startswith("vzip_source/") and k.endswith("/zarr.json") and json.loads(v)["node_type"] == "array"}
    assert shapes == {"vzip_source/extensions/1/zarr.json": [104], "vzip_source/unparsed/zarr.json": [16],
                      "vzip_source/trailing/zarr.json": [8]}


def test_the_bytes_between_the_header_and_the_voxels():
    """spec/virtualize/nifti.md §5: header_rest, extender, extensions (as a list, or over the
    root's budget as ecodes, texts and a family), extensions_truncated and unparsed, for each kind
    of region."""
    rest = {"descrip": "aGlkZGVuIGFmdGVyIHRoZSBOVUwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "aux_file": "AABtb3JlAAAAAAAAAAAAAAAAAAA=", "intent_name": "eAAAAAAAAAAAAAAAAAAA"}
    abc = [{"ecode": 6, "text": "abcdefg"}]
    compact = {"ecode": "vzip_source/extensions/ecode", "esize": "vzip_source/extensions/esize",
               "data": "vzip_source/extensions/data"}
    texts = {"1": "z" * 9000, **{str(i + 2): f"comment {i}" for i in range(373)}}
    # fixture: (the source metadata but nifti_version, byte_order and header,
    #           the arrays: their shape and their first chunk, or the start of its ranges)
    cases = {
        "nifti_header_rest": ({"header_rest": rest}, {}),
        "nifti_region_no_extender": ({"unparsed": "vzip_source/unparsed"}, {"unparsed": ([44], [(352, 44)])}),
        "nifti_region_extender_reserved": ({"extender": "AQcICQ==", "extensions": [{"ecode": 6, "text": "hello"}]},
                                           {}),
        "nifti_region_chain_tail": ({"extensions": abc, "extensions_truncated": True,
                                     "unparsed": "vzip_source/unparsed"}, {"unparsed": ([4], [(368, 4)])}),
        "nifti_region_chain_zero_pad": ({"extensions": abc}, {}),
        # 400 extensions of 24 bytes: the family is offsets and data whatever the lengths.
        "nifti_region_extensions_moved": ({"extensions": compact}, {
            "extensions/ecode": ([400], b"".join(struct.pack("<i", 40) for _ in range(400))),
            "extensions/esize": ([400], struct.pack("<400i", *[24 + 8] * 400)),
            "extensions/data/offsets": ([401], struct.pack("<401q", *range(0, 401 * 24, 24))),
            "extensions/data/data": ([9600], [(360, 24), (392, 24)])}),
        # A text over the limit of a text value, and one at it, but over the budget.
        "nifti_region_text_large": ({"extensions": compact}, {
            "extensions/ecode": ([2], struct.pack("<2i", 6, 6)),
            "extensions/esize": ([2], struct.pack("<2i", 70016, 65544)),
            "extensions/data/data": ([135544], [(360, 70008), (70376, 65536)])}),
        # The long comment and the first 373 short ones fit in the budget, and the others' data is
        # the family's.
        "nifti_region_extensions_compact": ({"extensions": {**compact, "text": texts}}, {
            "extensions/ecode": ([902], struct.pack("<902i", 2, *[6] * 901)),
            "extensions/esize": ([902], struct.pack("<902i", 112, 9008, *[32] * 900)),
            "extensions/data/data": ([104 + 527 * 24], [(360, 104), (9472 + 373 * 32 + 8, 24)])}),
        # Text extensions whose esize is not the fewest NULs': kept with the text, or in the esizes.
        "nifti_region_text_esize": ({"extensions": [
            {"ecode": 6, "text": "abc", "esize": 32}, {"ecode": 6, "text": "hello", "esize": 13},
            {"ecode": 4, "text": "<x/>"}, {"ecode": 6, "text": "12345678"}]}, {}),
        "nifti_region_extensions_compact_esize": (
            {"extensions": {**compact, "text": {str(i): f"note {i}" for i in range(700)}}},
            {"extensions/esize": ([700], struct.pack("<700i", *[48] * 700))}),
        # Over the budget once built: every empty comment is on the root, and the family has no data.
        "nifti_region_extensions_built_over": ({"extensions": {**compact, "text": dict.fromkeys(map(str, range(760)), "")}},
                                               {"extensions/ecode": ([760], struct.pack("<760i", *[6] * 760)),
                                                "extensions/data/offsets": ([761], bytes(8 * 761))}),
        # Over the budget by the count alone: the first 1736 empty comments fit on the root.
        "nifti_region_extensions_count_over": (
            {"extensions": {**compact, "text": dict.fromkeys(map(str, range(1736)), "")}},
            {"extensions/esize": ([2500], struct.pack("<2500i", *[16] * 2500)),
             "extensions/data/offsets": ([2501], struct.pack("<2501q", *[0] * 1737, *range(8, 8 * 765, 8))),
             "extensions/data/data": ([6112], [(352 + 16 * 1736 + 8, 8), (352 + 16 * 1737 + 8, 8)])}),
        # A family cut in 2 chunks, members inlined as text or of no bytes.
        "nifti_region_extensions_family_chunks": (
            {"extensions": {**compact, "text": {str(i): f"note {i}" for i in range(500, 24000, 1000)}}},
            {"extensions/data/data": ([1265167], [(360, 9), (377, 9)])}),
        "nifti_header_latin1": ({}, {}),
        "nifti_nibabel_n1_uint8": ({}, {}),
    }
    for name, (want, arrays) in cases.items():
        _, out = virtualize(str(FIXTURES / f"{name}.nii"), url=f"https://data.test/{name}.nii")
        meta = json.loads(out.bytes_entries["zarr.json"])["attributes"]["vzip_virtualized"]["nifti"]
        got = {k: v for k, v in meta.items() if k not in ("nifti_version", "byte_order", "header", "affine")}
        assert got == want, name
        if isinstance(got.get("extensions"), dict):
            size = len(json.dumps(got["extensions"], separators=(",", ":"), ensure_ascii=False).encode())
            assert size <= 16384, name
        node = json.loads(out.bytes_entries.get("vzip_source/zarr.json", b'{"attributes": {}}'))["attributes"]
        assert "vzip_virtualized" not in node, name
        for path, (shape, first) in arrays.items():
            assert json.loads(out.bytes_entries[f"vzip_source/{path}/zarr.json"])["shape"] == shape, name
            key = f"vzip_source/{path}/c/0"
            if isinstance(first, bytes):
                assert out.bytes_entries[key] == first, name
            else:
                assert out.refs[key][:len(first)] == first, name
    _, out = virtualize(str(FIXTURES / "nifti_header_latin1.nii"), url="https://data.test/l.nii")
    meta = json.loads(out.bytes_entries["zarr.json"])["attributes"]["vzip_virtualized"]["nifti"]
    assert meta["header"]["descrip"] == {"latin1": "caf\u00e9"}
    # The second chunk of the family holds over a payload's worth of ranges: its bytes are copied,
    # the end of the large extension's data, the short ones' and a padding NUL.
    _, out = virtualize(str(FIXTURES / "nifti_region_extensions_family_chunks.nii"), url="https://data.test/f.nii")
    assert "vzip_source/extensions/data/data/c/1" not in out.refs
    second = out.bytes_entries["vzip_source/extensions/data/data/c/1"]
    large = bytes(range(256)) * 4100 + bytes(8)
    tail = large[632584 - 1996 * 9:]
    assert len(second) == 632584 and second[:len(tail)] == tail and second[-1:] == b"\0"
    assert second[len(tail):len(tail) + 9] == bytes([2001 % 251]) * 9


def test_float_header_fields_keep_their_bits():
    """spec/virtualize/nifti.md §5: a negative zero, or a NaN other than the canonical one,
    is {"bits": hex}; the canonical NaN is "NaN", in either byte order and width."""
    cases = {
        "nifti_header_float_bits": {
            "intent_p1": {"bits": "80000000"}, "scl_slope": {"bits": "7fa00000"}, "cal_min": "NaN",
            "slice_duration": {"bits": "ffc00000"}, "toffset": {"bits": "7fc00001"},
            "pixdim": [1.0, 1.0, 1.0, 1.0, 1.0, {"bits": "80000000"}, 1.0, 1.0],
            "srow_x": [0.0, {"bits": "80000000"}, 0.0, 0.0], "cal_max": 0.0},
        "nifti_header_n2_float_bits": {
            "intent_p1": {"bits": "8000000000000000"}, "cal_min": "NaN",
            "slice_duration": {"bits": "fff8000000000000"}, "toffset": {"bits": "7ff8000000000001"},
            "pixdim": [1.0, {"bits": "7ff4000000000000"}, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0], "scl_slope": 1.0},
    }
    for name, want in cases.items():
        _, out = virtualize(str(FIXTURES / f"{name}.nii"), url=f"https://data.test/{name}.nii")
        header = json.loads(out.bytes_entries["zarr.json"])["attributes"]["vzip_virtualized"]["nifti"]["header"]
        assert {k: header[k] for k in want} == want, name
