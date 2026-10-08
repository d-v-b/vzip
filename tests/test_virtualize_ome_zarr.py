"""The Python OME-Zarr virtualizer (profiles/ome-zarr.md) on the synthetic stores.

Equivalence with the browser virtualizer is checked by
conformance/virtualize/compare.py, and the outputs against ome-zarr-models and
zarr-python by web/test/ome-zarr/verify.py.
"""

from pathlib import Path

import pytest

from vzip.virtualize import Rejected, virtualize
from vzip.virtualize.store import DirStore
from vzip.virtualize.zarr2 import virtualize_zarr2

FIXTURES = Path(__file__).parents[1] / "web" / "test" / "fixtures" / "ome-zarr"
URL = "https://data.test/ome-zarr/{}/"
IMAGE = {"multiscales"}

# store: (summary members, {group path: (attribute names besides `ome`, members of `ome` besides `version`)},
#         {array path: dimension_names, or None for none})
CASES = {
    "ome_zarr_image_2d": (
        {"images": 1, "chunks": 2, "emptyChunks": 1},
        {"": ({"_creator"}, IMAGE)},
        {"0": ["y", "x"], "1": ["y", "x"]}),
    "ome_zarr_image_3d_translation": (
        {"images": 1, "arrays": 3},
        {"": (set(), IMAGE)},
        {"0": ["z", "y", "x"], "2": ["z", "y", "x"]}),
    "ome_zarr_image_5d_omero": (
        {"images": 1, "groups": 2},
        {"": ({"custom"}, {"multiscales", "omero"}), "extra": ({"multiscales_like"}, None)},
        {"0": list("tczyx"), "1": list("tczyx"), "extra/table": None}),
    "ome_zarr_custom_axes": (
        {"images": 2},
        {"": (set(), IMAGE), "nested": (set(), IMAGE)},
        {"s0": ["angle", "y", "x"], "nested/0": ["k", "y", "x"]}),
    "ome_zarr_labels": (
        {"images": 2, "labels": 1, "droppedLabelLevels": 0, "groups": 4},
        {"": (set(), {"multiscales", "omero"}), "labels": (set(), {"labels"}),
         "labels/cells": (set(), {"multiscales", "image-label"}), "labels/unlisted": (set(), None)},
        {"0": ["c", "y", "x"], "labels/cells/1": ["c", "y", "x"]}),
    "ome_zarr_labels_extra_level": (
        {"labels": 1, "droppedLabelLevels": 1, "arrays": 5},
        {"labels/cells": (set(), {"multiscales", "image-label"})},
        {"labels/cells/1": ["c", "y", "x"], "labels/cells/2": None}),
    "ome_zarr_plate": (
        {"plates": 1, "wells": 2, "fields": 3, "images": 3, "groups": 8},
        {"": ({"_creator"}, {"plate"}), "A": (set(), None), "A/1": (set(), {"well"}),
         "B/2/0": (set(), {"multiscales", "omero"})},
        {"A/1/1/0": ["c", "y", "x"]}),
    "ome_zarr_well_root": (
        {"wells": 1, "fields": 2},
        {"": (set(), {"well"}), "f1": (set(), IMAGE)},
        {"f1/0": ["y", "x"]}),
    "ome_zarr_bioformats2raw": (
        {"images": 2, "omeXml": 1},
        {"": (set(), {"bioformats2raw.layout"}), "OME": (set(), {"series"}), "1": (set(), IMAGE)},
        {"1/0": ["z", "y", "x"]}),
    "ome_zarr_bioformats2raw_plate": (
        {"plates": 1, "fields": 1, "omeXml": 1},
        {"": (set(), {"bioformats2raw.layout", "plate"}), "A/1": (set(), {"well"})},
        {"A/1/0/1": list("tczyx")}),
}


def key(path: str) -> str:
    return f"{path}/zarr.json" if path else "zarr.json"


def test_virtualizes_the_synthetic_stores():
    for name, (summary, groups, arrays) in CASES.items():
        url = URL.format(name)
        fmt, out = virtualize(str(FIXTURES / name), url=url)
        assert fmt == "ome-zarr", name
        assert {**out.summary, **summary} == out.summary, name
        for path, (outer, ome) in groups.items():
            attrs = out.docs[key(path)]["attributes"]
            if ome is None:
                assert "ome" not in attrs and set(attrs) == outer, (name, path)
                continue
            assert set(attrs) == outer | {"ome"}, (name, path)
            assert attrs["ome"]["version"] == "0.5" and set(attrs["ome"]) == ome | {"version"}, (name, path)
            for m in attrs["ome"].get("multiscales", []):
                assert "version" not in m, (name, path)
            for k in ("omero", "image-label", "plate", "well"):
                assert "version" not in attrs["ome"].get(k, {}), (name, path, k)
        for path, names in arrays.items():
            assert out.docs[key(path)].get("dimension_names") == names, (name, path)
        # The chunks are §10's, referenced in place; the only other references are OME-XML objects.
        plain = virtualize_zarr2(DirStore(str(FIXTURES / name), url))
        xml = [(k, n) for k, n in out.chunks if k.endswith("OME/METADATA.ome.xml")]
        assert out.chunks == sorted(plain.chunks + xml), name
        assert len(xml) == summary.get("omeXml", 0), name
        # Only attributes and dimension names differ from §10's documents.
        assert set(out.docs) == set(plain.docs), name
        for k, doc in plain.docs.items():
            strip = {"attributes", "dimension_names"}
            assert {a: b for a, b in out.docs[k].items() if a not in strip} == \
                {a: b for a, b in doc.items() if a not in strip}, (name, k)
    # Level 0's empty chunk (1.0) and missing one (2.0) have no entry.
    _, out = virtualize(str(FIXTURES / "ome_zarr_image_2d"), url=URL.format("i"))
    assert [k for k, _ in out.chunks] == ["0/0.0", "1/0.0"]
    # The OME members, moved under `ome` without their own `version`, are otherwise unchanged.
    _, out = virtualize(str(FIXTURES / "ome_zarr_labels"), url=URL.format("l"))
    ome = out.docs["labels/cells/zarr.json"]["attributes"]["ome"]
    assert ome["image-label"] == {
        "colors": [{"label-value": 1, "rgba": [255, 0, 0, 255]}, {"label-value": 2, "rgba": [0, 255, 0, 128]}],
        "properties": [{"label-value": 1, "class": "nucleus", "area (pixels)": 12}, {"label-value": 2}],
        "source": {"image": "../../"}}
    assert out.docs["labels/zarr.json"]["attributes"] == {"ome": {"version": "0.5", "labels": ["cells"]}}
    # L7: a label image's levels past its image's are dropped from its multiscales, transforms unchanged.
    _, out = virtualize(str(FIXTURES / "ome_zarr_labels_extra_level"), url=URL.format("x"))
    m = out.docs["labels/cells/zarr.json"]["attributes"]["ome"]["multiscales"][0]
    assert [d["path"] for d in m["datasets"]] == ["0", "1"]
    assert m["datasets"][1]["coordinateTransformations"] == [{"type": "scale", "scale": [1.0, 2.0, 2.0]}]
    _, out = virtualize(str(FIXTURES / "ome_zarr_image_3d_translation"), url=URL.format("t"))
    m = out.docs["zarr.json"]["attributes"]["ome"]["multiscales"][0]
    assert m["datasets"][2]["coordinateTransformations"] == [
        {"type": "scale", "scale": [2.0, 2.0, 2.0]}, {"type": "translation", "translation": [10.0, -3.5, 4.25]}]
    assert m["coordinateTransformations"][1] == {"type": "translation", "translation": [0.0, 100.0, 0.0]}
    _, out = virtualize(str(FIXTURES / "ome_zarr_bioformats2raw"), url=URL.format("b"))
    assert out.docs["zarr.json"]["attributes"] == {"ome": {"version": "0.5", "bioformats2raw.layout": 3}}
    assert out.docs["OME/zarr.json"]["attributes"] == {"ome": {"version": "0.5", "series": ["0", "1"]}}
    assert out.sources[-1] == "https://data.test/ome-zarr/b/OME/METADATA.ome.xml"


@pytest.mark.parametrize("name,message", [
    ("has_ome", "already has an `ome` attribute"),
    ("version", "old: multiscales\\[0\\]: version '0.3' is not 0.4"),
    ("multiscales_empty", "g: multiscales is not a nonempty array"),
    ("axes_count", "axes is not 2 to 5 axis objects"),
    ("axes_name", "an axis name is not a string"),
    ("axes_duplicate", "are not distinct"),
    ("axes_type", "axis type 3 is not a string"),
    ("axes_order", "axis types \\['space', 'channel', 'space'\\]"),
    ("axes_two_others", "axis types \\['channel', 'phase', 'space', 'space'\\]"),
    ("axes_four_space", "axis types \\['space', 'space', 'space', 'space'\\]"),
    ("datasets_empty", "datasets is not a nonempty array"),
    ("datasets_path", "path '../1' is not a relative path"),
    ("datasets_missing", "9 is not an array"),
    ("datasets_rank", "r has 3 dimensions, not 2"),
    ("transforms_missing", "coordinateTransformations is not one or two"),
    ("transforms_order", "the first transformation is not a scale"),
    ("transforms_identity", "the first transformation is not a scale"),
    ("transforms_path", "the scale is not 2 numbers"),
    ("scale_length", "the scale is not 2 numbers"),
    ("translation_length", "the translation is not 2 numbers"),
    ("multiscale_transforms", "multiscales\\[0\\]: the scale is not 2 numbers"),
    ("scale_order", "is smaller than the previous level's"),
    ("level_axes_conflict", "p/q/lvl is a level of images with axes"),
    ("omero_channels", "omero: channels is not an array"),
    ("omero_color", "color 'red' is not 6 hexadecimal digits"),
    ("omero_window", "window does not have numbers"),
    ("labels_not_image", "label 'missing' is not the path of an image"),
    ("image_label_no_multiscales", "image-label: the group has no multiscales"),
    ("image_label_levels", "the label image has 1 levels, fewer than its image /'s 2"),
    ("image_label_dtype", "label data type float32 is not an integer type"),
    ("image_label_color_duplicate", "label-values are not unique"),
    ("image_label_rgba", "rgba \\[1, 2, 3, 300\\]"),
    ("image_label_value", "properties is not an array of objects with an integer label-value"),
    ("image_label_source", "source image '../../elsewhere' is not an image"),
    ("image_label_version", "image-label: version '0.3' is not 0.4"),
    ("plate_column_name", "a column name is not alphanumeric"),
    ("plate_row_duplicate", "row names are not unique"),
    ("plate_well_path", "well path 'B/2' is not row A/column 2"),
    ("plate_well_index", "rowIndex or columnIndex is not an index"),
    ("plate_well_missing", "A/2 is not a well"),
    ("plate_acquisition_id", "acquisition id -1"),
    ("plate_field_count", "field_count 0 is not a positive integer"),
    ("plate_version", "plate: version '0.3' is not 0.4"),
    ("well_image_path", "image path 'f-0' is not alphanumeric"),
    ("well_image_missing", "A/1/7 is not an image"),
    ("well_image_duplicate", "image paths are not unique"),
    ("well_acquisition", "acquisition 5 is not one of the plate's"),
    ("well_acquisition_missing", "an image has no acquisition, and the plate has several"),
    ("bioformats2raw_layout", "bioformats2raw.layout is not 3"),
    ("bioformats2raw_numbering", "not numbered consecutively from 0"),
    ("bioformats2raw_series", "series is not an array of image paths"),
])
def test_rejects(name, message):
    with pytest.raises(Rejected, match=message):
        virtualize(str(FIXTURES / f"ome_zarr_reject_{name}"), url=URL.format(name))


def test_every_rejection_fixture_is_tested():
    names = {d.name.removeprefix("ome_zarr_reject_") for d in FIXTURES.iterdir() if "_reject_" in d.name}
    params = [m for m in test_rejects.pytestmark if m.name == "parametrize"][0].args[1]
    assert names == {n for n, _ in params}
