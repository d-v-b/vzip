"""The Python NIfTI virtualizer (profiles/nifti.md) on the synthetic fixtures.

Equivalence with the browser virtualizer is checked by
conformance/virtualize/compare.py, and pixel correctness against nibabel by
web/test/nifti/verify.py.
"""

import json
from pathlib import Path

import pytest

from vzip.virtualize import Rejected, virtualize

FIXTURES = Path(__file__).parents[1] / "web" / "test" / "fixtures" / "nifti"

MM = "millimeter"
# fixture: (summary members, [(axis, unit)], scale, translation, `nifti` attribute, first omero window,
#           (key, range) of one chunk)
CASES = {
    "nifti_n1_le_int16_3d_sform": (
        {"version": 1, "byteOrder": "little", "dataType": "int16", "affine": "sform"},
        [("z", MM), ("y", MM), ("x", MM)], [4, 3, 2], [7, 20.25, -10.5], None, None,
        ("0/c/2/0/0", [(352 + 2 * 40, 40)])),
    "nifti_n1_be_int16_4d_scaled": (
        {"byteOrder": "big", "sizes": {"t": 5, "z": 2, "y": 3, "x": 4}, "scaling": {"slope": 0.5, "inter": -20}},
        [("t", "second"), ("z", MM), ("y", MM), ("x", MM)], [2.5, 3, 1.5, 1.5], None,
        {"slope": 0.5, "inter": -20}, {"min": -20, "max": 60, "start": -20, "end": 60},
        ("0/c/4/1/0/0", [(352 + 9 * 24, 24)])),
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
        # k = 2, t = 1, z = 0: slab (2 * 2 + 1) * 2 = 10 of 6 voxels of 8 bytes, after 544 bytes.
        ("0/c/1/2/0/0/0", [(544 + 10 * 48, 48)])),
    "nifti_n1_be_rgb24_4d": (
        {"color": True, "scaling": None, "sizes": {"t": 2, "c": 3, "z": 2, "y": 3, "x": 2}}, None, None, None, None,
        {"min": 0, "max": 255, "start": 0, "end": 255}, ("0/c/1/0/1/0/0", [(352 + 3 * 18, 18)])),
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
    "nifti_edge_vox_offset_padded": ({}, None, None, None, None, None, ("0/c/0/0/0", [(480, 12)])),
    "nifti_rowblock_split": ({"rowBlock": 350, "chunks": 2}, None, None, None, None, None,
                             ("0/c/0/1/0", [(352 + 350 * 200, 350 * 200)])),
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
        meta = attributes["vzip_virtualized"]["nifti"]  # conventions/nifti/README.md §5
        assert meta.get("scaling") == scaling, name
        assert meta["header"]["magic"] == f"n+{meta['nifti_version']}", name
        omero = attributes["ome"].get("omero")
        assert (omero["channels"][0]["window"] if omero else None) == window, name
        if chunk is not None:
            assert out.refs[chunk[0]] == chunk[1], name


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
