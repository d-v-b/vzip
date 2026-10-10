"""The Python OME-Zarr virtualizer (spec/virtualize/ome-zarr.md) on the synthetic stores.

Equivalence with the browser virtualizer is checked by
conformance/virtualize/compare.py, and the outputs against ome-zarr-models and
zarr-python by js/test/ome-zarr/verify.py.
"""

import json
from pathlib import Path

import pytest

from vzip.virtualize import Rejected, virtualize
from vzip.virtualize.common import declare
from vzip.virtualize.store import DirStore
from vzip.virtualize.zarr2 import virtualize_zarr2

FIXTURES = Path(__file__).parents[2] / "fixtures" / "ome-zarr"
URL = "https://data.test/ome-zarr/{}/"
OME_MEMBERS = {"multiscales", "omero", "labels", "image-label", "plate", "well", "bioformats2raw.layout", "series"}
IMAGE = {"multiscales"}

# store: (summary members, {group path: (names of the attributes A keeps as source metadata, members of
#         `ome` besides `version`)}, {array path: dimension_names, or None for none})
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
        {"": (set(), IMAGE), "nested": (set(), IMAGE)},  # nested has no version: unversioned
        {"s0": ["angle", "y", "x"], "nested/0": ["k", "y", "x"]}),
    "ome_zarr_labels": (
        {"images": 2, "labels": 1, "droppedLabelLevels": 0, "groups": 4},
        {"": (set(), {"multiscales", "omero"}), "labels": (set(), {"labels"}),
         "labels/cells": (set(), {"multiscales", "image-label"}), "labels/unlisted": (set(), None)},
        {"0": ["c", "y", "x"], "labels/cells/1": ["c", "y", "x"]}),
    "ome_zarr_labels_extra_level": (
        {"labels": 1, "droppedLabelLevels": 1, "arrays": 5},
        {"labels/cells": ({"multiscales"}, {"multiscales", "image-label"})},  # a dropped dataset
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
    "ome_zarr_source_metadata": (
        {"wells": 1, "fields": 2, "otherObjects": 2},
        {"": (set(), {"well"}), "0": ({"note"}, {"multiscales", "omero"}),
         "1": (set(), {"multiscales", "omero"})},
        {"1/0": ["y", "x"]}),
}

# (store, group path): the OME members without a version, which `ome` gives back as they are (§5, §8).
UNVERSIONED = {
    ("ome_zarr_custom_axes", "nested"): ["multiscales"], ("ome_zarr_bioformats2raw", "1"): ["multiscales"],
    ("ome_zarr_bioformats2raw_plate", ""): ["plate"], ("ome_zarr_bioformats2raw_plate", "A/1"): ["well"],
    ("ome_zarr_source_metadata", "0"): ["omero"],
}

VERSIONED = ("omero", "image-label", "plate", "well")


def inverse(ome: dict, a: dict, unversioned: list[str]) -> dict:
    """The .zattrs of an OME group, from its `ome`, its attributes A and its unversioned
    members U (spec/virtualize/ome-zarr.md §5, §8)."""
    out = {}
    for k, v in ome.items():
        if k in unversioned:
            pass
        elif k == "multiscales":
            v = [{"version": "0.4", **m} for m in v]
        elif k in VERSIONED:
            v = {"version": "0.4", **v}
        if k != "version":
            out[k] = v
    return {**out, **a}


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
            # The attributes A that `ome` does not give back are copied under the convention, which
            # the root and any node with source metadata declare (conventions §2).
            s = attrs.get("vzip_virtualized", {}).get("ome-zarr", {})
            assert set(s.get("attributes", {})) == outer, (name, path)
            declared = {"zarr_conventions", "vzip_virtualized"} if path == "" or s else set()
            if ome is None:
                assert "ome" not in attrs and set(attrs) == declared, (name, path)
                continue
            assert set(attrs) == declared | {"ome"}, (name, path)
            assert attrs["ome"]["version"] == "0.5" and set(attrs["ome"]) == ome | {"version"}, (name, path)
            for m in attrs["ome"].get("multiscales", []):
                assert "version" not in m, (name, path)
            for k in ("omero", "image-label", "plate", "well"):
                assert "version" not in attrs["ome"].get(k, {}), (name, path, k)
        for path, names in arrays.items():
            assert out.docs[key(path)].get("dimension_names") == names, (name, path)
        # Every .zattrs is its `ome`, by the inverse of §5, and its attributes A (§8).
        for z in (FIXTURES / name).rglob(".zgroup"):
            path = z.parent.relative_to(FIXTURES / name).as_posix().removeprefix(".")
            attrs = out.docs[key(path)]["attributes"]
            s = attrs.get("vzip_virtualized", {}).get("ome-zarr", {})
            assert s.get("unversioned", []) == UNVERSIONED.get((name, path), []), (name, path)
            zattrs = z.parent / ".zattrs"
            assert inverse(attrs.get("ome", {}), s.get("attributes", {}), s.get("unversioned", [])) == (
                json.loads(zattrs.read_text()) if zattrs.exists() else {}), (name, path)
        # The chunks and other objects are §10's, referenced in place, except the OME-XML of a
        # collection, which keeps its own key (§7).
        plain = virtualize_zarr2(DirStore(str(FIXTURES / name), url))
        xml = [(k, n) for k, n in out.chunks if not k.startswith("vzip_source/") and k.endswith("OME/METADATA.ome.xml")]
        assert out.chunks == sorted([c for c in plain.chunks if c[0].removeprefix("vzip_source/objects/")
                                     not in dict(xml)] + xml), name
        assert len(xml) == summary.get("omeXml", 0), name
        # Only attributes and dimension names differ from §10's documents.
        source = {"vzip_source/zarr.json"}
        assert set(out.docs) - source == set(plain.docs) - source, name
        assert ("vzip_source/zarr.json" in out.docs) == any(k.startswith("vzip_source/") for k, _ in out.chunks)
        for k, doc in plain.docs.items():
            if k not in out.docs:
                continue
            strip = {"attributes", "dimension_names"}
            assert {a: b for a, b in out.docs[k].items() if a not in strip} == \
                {a: b for a, b in doc.items() if a not in strip}, (name, k)
    # Level 0's empty chunk (1.0) and missing one (2.0) have no entry; the stray object is kept whole.
    _, out = virtualize(str(FIXTURES / "ome_zarr_image_2d"), url=URL.format("i"))
    assert [k for k, _ in out.chunks] == ["0/0.0", "1/0.0", "vzip_source/objects/notes.txt"]
    # The empty chunk's key is listed with the empty objects (the Zarr v2 convention §5).
    assert out.docs["vzip_source/zarr.json"]["attributes"]["vzip_virtualized"]["ome-zarr"] == {"empty": ["0/1.0"]}
    # The OME members, moved under `ome` without their own `version`, are otherwise unchanged.
    _, out = virtualize(str(FIXTURES / "ome_zarr_labels"), url=URL.format("l"))
    ome = out.docs["labels/cells/zarr.json"]["attributes"]["ome"]
    assert ome["image-label"] == {
        "colors": [{"label-value": 1, "rgba": [255, 0, 0, 255]}, {"label-value": 2, "rgba": [0, 255, 0, 128]}],
        "properties": [{"label-value": 1, "class": "nucleus", "area (pixels)": 12}, {"label-value": 2}],
        "source": {"image": "../../"}}
    attrs = out.docs["labels/zarr.json"]["attributes"]
    assert attrs == {"ome": {"version": "0.5", "labels": ["cells"]}}  # all given back: no source metadata
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
    assert out.docs["zarr.json"]["attributes"] == declare({"ome": {"version": "0.5", "bioformats2raw.layout": 3}},
                                                          "ome-zarr", URL.format("b"), {})
    assert out.docs["OME/zarr.json"]["attributes"]["ome"] == {"version": "0.5", "series": ["0", "1"]}
    assert out.sources[-1] == "https://data.test/ome-zarr/b/OME/METADATA.ome.xml"
    # The Zarr v2 metadata M, the attributes with integers kept exact, and the other objects:
    # OME-XML outside a collection and a README (spec/virtualize/ome-zarr.md §7, §8).
    url = URL.format("s")
    _, out = virtualize(str(FIXTURES / "ome_zarr_source_metadata"), url=url)
    s = lambda p: out.docs[key(p)]["attributes"].get("vzip_virtualized", {}).get("ome-zarr")  # noqa: E731
    assert s("")["metadata"] == {"creator": "a writer"}
    assert s("0")["attributes"]["note"] == {"big": 18446744073709551615}
    assert s("1") is None and s("1/0") == {"metadata": {"fill_value": None}}
    assert [k for k, _ in out.chunks if k.startswith("vzip_source/")] == [
        "vzip_source/objects/OME/METADATA.ome.xml", "vzip_source/objects/README.md"]
    assert out.sources[-1] == url + "README.md"


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
