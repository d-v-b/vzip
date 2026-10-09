"""Writes synthetic OME-Zarr 0.4 hierarchies (on Zarr v2) to
fixtures/ome-zarr/<name>/, covering the OME-Zarr profile
(spec/virtualize/ome-zarr/profile.md §11): 2-D, 3-D and 5-D images with and without
translations, omero metadata, custom axes, labels (image-label colors,
properties and source; a label image with an extra level), a plate with wells, fields and acquisitions, a well
at the root, bioformats2raw collections (numbered images with a series and
OME-XML, and a plate), F order and both separators, missing and empty
chunks; and the inputs the profile rejects (`ome_zarr_reject_*`, one per
rule of spec/virtualize/ome-zarr.md §4).

Each array is one chunk, except level 0 of ome_zarr_image_2d (three chunks:
present, empty and missing), so that each store is a handful of objects.
Each rejection is the smallest store that breaks its rule and is otherwise
valid: one level where the rule does not need two, a plate of one well and
one field, and arrays with no chunk.

Arrays and chunks are written by fixtures/generators/zarr2/write_fixtures.py's helpers,
as zarr-python 2 writes them; documents are written here by hand.
js/test/ome-zarr/verify.py checks every accepted store with ome-zarr-models
(0.4) and zarr-python's Zarr v2 reader.

Usage: uv run python fixtures/generators/ome-zarr/write_fixtures.py
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
OUT = HERE.parents[1] / "ome-zarr"
_spec = importlib.util.spec_from_file_location("zarr2_fixtures", HERE.parent / "zarr2" / "write_fixtures.py")
z2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(z2)
array, group, put, write_json = z2.array, z2.group, z2.put, z2.write_json
RNG = np.random.default_rng(11)
ZLIB = {"id": "zlib", "level": 1}

SPACE = {"type": "space", "unit": "micrometer"}
AX = {
    "t": {"name": "t", "type": "time", "unit": "second"},
    "c": {"name": "c", "type": "channel"},
    "z": {"name": "z", **SPACE},
    "y": {"name": "y", **SPACE},
    "x": {"name": "x", **SPACE},
}


def data(shape, dtype="|u1") -> np.ndarray:
    dt = np.dtype(dtype)
    if dt.kind == "f":
        return RNG.standard_normal(shape).astype(dt)
    hi = min(np.iinfo(dt).max, 200)
    return RNG.integers(0, hi, size=shape, endpoint=True, dtype=dt.newbyteorder("=")).astype(dt)


def multiscale(axes: str, levels: int, *, base=None, translation=None, name="image", **extra) -> dict:
    """A 0.4 multiscale over `axes` (a string of AX keys) with `levels` levels 0, 1, ...
    downsampled by 2 along y and x."""
    n = len(axes)
    base = base or [1.0] * n
    datasets = []
    for i in range(levels):
        scale = [b * (2**i if a in "yx" else 1) for a, b in zip(axes, base)]
        ct = [{"type": "scale", "scale": scale}]
        if translation is not None:
            ct.append({"type": "translation", "translation": translation})
        datasets.append({"path": str(i), "coordinateTransformations": ct})
    return {"version": "0.4", "name": name, "axes": [AX[a] for a in axes], "datasets": datasets, **extra}


def image(root: Path, path: str, axes: str, shape, levels=2, *, dtype="|u1", chunks=None, attrs=None, ms=None,
          order="C", sep="/", compressor=None, write_chunks=True, **kw) -> None:
    """An image group with its levels (shape halved along y and x per level), each level one
    chunk unless `chunks` is given (or none, if not `write_chunks`)."""
    m = ms or multiscale(axes, levels, **kw)
    group(root, path, {"multiscales": [m], **(attrs or {})})
    full = data(shape, dtype)
    for i in range(levels):
        sl = tuple(slice(None, None, 2**i) if a in "yx" else slice(None) for a in axes)
        level = np.ascontiguousarray(full[sl])
        array(root, f"{path}/{i}" if path else str(i), level, chunks or list(level.shape), order=order, sep=sep,
              compressor=compressor, write_chunks=write_chunks)


def store(name: str) -> Path:
    d = OUT / name
    d.mkdir(parents=True)
    return d


def omero(n: int) -> dict:
    return {"id": 1, "name": "synthetic", "version": "0.4",
            "channels": [{"active": True, "coefficient": 1.0, "color": ["FF0000", "00FF00", "0000FF"][i % 3],
                          "family": "linear", "inverted": False, "label": f"ch{i}",
                          "window": {"start": 0, "end": 200, "min": 0, "max": 255}} for i in range(n)],
            "rdefs": {"defaultT": 0, "defaultZ": 0, "model": "color"}}


def labelled(d: Path, *, image_label=None, image_levels=2, label_levels=2, label_dtype="<u2", listed=("cells",),
             write_chunks=True) -> None:
    """An image `""` (c, y, x) with a labels group and the label image labels/cells."""
    image(d, "", "cyx", (2, 8, 10), image_levels, attrs={"omero": omero(2)}, write_chunks=write_chunks)
    group(d, "labels", {"labels": list(listed)})
    il = image_label if image_label is not None else {
        "version": "0.4", "colors": [{"label-value": 1, "rgba": [255, 0, 0, 255]},
                                     {"label-value": 2, "rgba": [0, 255, 0, 128]}],
        "properties": [{"label-value": 1, "class": "nucleus", "area (pixels)": 12}, {"label-value": 2}],
        "source": {"image": "../../"}}
    image(d, "labels/cells", "cyx", (1, 8, 10), label_levels, dtype=label_dtype, attrs={"image-label": il},
          name="cells", write_chunks=write_chunks)


def plate_doc(**over) -> dict:
    p = {"version": "0.4", "name": "plate", "field_count": 2,
         "acquisitions": [{"id": 0, "name": "first", "maximumfieldcount": 2, "starttime": 1343731272000},
                          {"id": 1, "name": "second", "maximumfieldcount": 1}],
         "rows": [{"name": "A"}, {"name": "B"}], "columns": [{"name": "1"}, {"name": "2"}],
         "wells": [{"path": "A/1", "rowIndex": 0, "columnIndex": 0}, {"path": "B/2", "rowIndex": 1, "columnIndex": 1}]}
    p.update(over)
    return p


WELLS = {"A/1": [{"path": "0", "acquisition": 0}, {"path": "1", "acquisition": 1}],
         "B/2": [{"path": "0", "acquisition": 0}]}
# The plate of the rejections: one well (A/1) of one field.
WELL_A1 = {"path": "A/1", "rowIndex": 0, "columnIndex": 0}
WELLS_SMALL = {"A/1": [{"path": "0", "acquisition": 0}]}


def plate(d: Path, *, doc=None, wells=None, field_order="C", small=False, root=None) -> None:
    group(d, "", root or {"plate": doc or plate_doc(), "_creator": {"name": "synthetic"}})
    if not small:
        group(d, "A")  # an explicit row group; row B is implicit
    for w, ims in (wells or WELLS).items():
        group(d, w, {"well": {"version": "0.4", "images": ims}})
        for im in ims:
            if small:  # one level, with no chunk
                image(d, f"{w}/{im['path']}", "yx", (2, 2), 1, write_chunks=False)
            else:
                image(d, f"{w}/{im['path']}", "cyx", (2, 6, 6), 1, attrs={"omero": omero(2)}, order=field_order)


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    # ------------------------------------------------------------ accepted

    # 2-D, "." separator, level 0 three chunks along y (present, empty, and a missing
    # partial edge chunk), a stray object.
    d = store("ome_zarr_image_2d")
    m = multiscale("yx", 2)
    group(d, "", {"multiscales": [m], "_creator": {"name": "synthetic", "version": "1"}})
    img = data((8, 10))
    array(d, "0", img, [3, 10], sep=".", skip={(2, 0)}, attrs={"_ARRAY_DIMENSIONS": ["y", "x"]})
    array(d, "1", img[::2, ::2], [4, 5], sep=".")
    put(d / "0/1.0", b"")  # an empty chunk object: no entry
    put(d / "notes.txt", b"not part of the hierarchy\n")

    # 3-D with translations (per level and for the multiscale), F order, zlib.
    d = store("ome_zarr_image_3d_translation")
    m = multiscale("zyx", 3, base=[2.0, 0.5, 0.5], translation=[10.0, -3.5, 4.25],
                   coordinateTransformations=[{"type": "scale", "scale": [1.0, 1.0, 1.0]},
                                              {"type": "translation", "translation": [0.0, 100.0, 0.0]}])
    image(d, "", "zyx", (3, 9, 11), 3, ms=m, order="F", compressor=ZLIB, dtype="<u2")

    # 5-D with omero, extra multiscale members, big-endian floats, another group left alone.
    d = store("ome_zarr_image_5d_omero")
    m = multiscale("tczyx", 2, base=[0.5, 1.0, 1.5, 0.25, 0.25], type="gaussian",
                   metadata={"method": "skimage.transform.pyramid_gaussian", "version": "0.16.1"})
    image(d, "", "tczyx", (2, 3, 2, 6, 8), 2, ms=m, attrs={"omero": omero(3), "custom": [1, 2]}, dtype=">f4",
          sep=".")
    group(d, "extra", {"multiscales_like": True})  # not an OME group: copied unchanged
    array(d, "extra/table", data((3, 2), "<i2"), [3, 2])

    # A custom axis type, a null type, an axis without a unit.
    d = store("ome_zarr_custom_axes")
    axes = [{"name": "angle", "type": "angle", "unit": "degree"}, {"name": "y", "type": "space"},
            {"name": "x", "type": "space"}]
    m = {"version": "0.4", "axes": axes, "datasets": [
        {"path": "s0", "coordinateTransformations": [{"type": "scale", "scale": [15.0, 1.0, 1.0]}]}]}
    group(d, "", {"multiscales": [m]})
    array(d, "s0", data((4, 5, 6)), [4, 5, 6])
    # a second image whose channel-like axis has a null type, nested under the first
    m2 = {"axes": [{"name": "k", "type": None}, {"name": "y", "type": "space"}, {"name": "x", "type": "space"}],
          "datasets": [{"path": "0", "coordinateTransformations": [{"type": "scale", "scale": [1, 1, 1]}]}]}
    group(d, "nested", {"multiscales": [m2]})  # no version: read as 0.4
    array(d, "nested/0", data((2, 3, 3)), [2, 3, 3])

    # Labels.
    d = store("ome_zarr_labels")
    labelled(d)
    group(d, "labels/unlisted")  # an intermediate group, not listed: not an OME group

    # A label image with one more level than its image (as omero-zarr writes them): the extra
    # level is dropped from the 0.5 multiscales and stays a plain array (spec/virtualize/ome-zarr.md §4, L7).
    d = store("ome_zarr_labels_extra_level")
    labelled(d, label_levels=3)

    # A plate: two rows and two columns, two wells, three fields (two in A/1, of two acquisitions),
    # an explicit and an implicit row, F-order fields of one level.
    d = store("ome_zarr_plate")
    plate(d, field_order="F")

    # A well at the root.
    d = store("ome_zarr_well_root")
    group(d, "", {"well": {"version": "0.4", "images": [{"path": "0"}, {"path": "f1"}]}})
    image(d, "0", "yx", (5, 5), 1)
    image(d, "f1", "yx", (5, 5), 1)

    # bioformats2raw: numbered images, an OME group with series, OME-XML.
    d = store("ome_zarr_bioformats2raw")
    group(d, "", {"bioformats2raw.layout": 3})
    image(d, "0", "cyx", (2, 6, 6), 2, attrs={"omero": omero(2)})
    m = multiscale("zyx", 1)
    del m["version"]  # 0.4 SHOULD, not MUST
    image(d, "1", "zyx", (2, 4, 4), 1, ms=m, sep=".")
    group(d, "OME", {"series": ["0", "1"]})
    put(d / "OME/METADATA.ome.xml", b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"><Image ID="Image:0"/>'
        b'<Image ID="Image:1"/></OME>\n')

    # bioformats2raw with a plate (no plate or well version: found through the first field).
    d = store("ome_zarr_bioformats2raw_plate")
    p = plate_doc(acquisitions=[{"id": 0}], wells=[{"path": "A/1", "rowIndex": 0, "columnIndex": 0}])
    del p["version"]
    group(d, "", {"bioformats2raw.layout": 3, "plate": p})
    group(d, "A/1", {"well": {"images": [{"path": "0", "acquisition": 0}]}})
    image(d, "A/1/0", "tczyx", (1, 2, 1, 4, 4), 2)
    put(d / "OME/METADATA.ome.xml", b'<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"/>\n')

    # ------------------------------------------------------------ rejected, one per rule of spec/virtualize/ome-zarr.md §4

    def reject(name: str, attrs_root=None, *, ms=None, shape=(4, 6), axes="yx", levels=1, setup=None):
        """A store whose root is an image of `levels` levels, with multiscale `ms` (default valid)
        and no chunks."""
        d = store(f"ome_zarr_reject_{name}")
        m = ms if ms is not None else multiscale(axes, levels)
        group(d, "", {"multiscales": [m], **(attrs_root or {})})
        full = data(shape)
        for i in range(levels):
            sl = tuple(slice(None, None, 2**i) for _ in shape)
            array(d, str(i), np.ascontiguousarray(full[sl]), [2] * len(shape), write_chunks=False)
        if setup:
            setup(d)
        return d

    def ms_with(**over):
        m = multiscale("yx", 1)
        m.update(over)
        return m

    def ms_datasets(fn, levels=1):
        """The multiscale with `fn` applied to its datasets (to the last level's)."""
        m = multiscale("yx", levels)
        fn(m["datasets"])
        return m

    reject("has_ome", {"ome": {"version": "0.5"}})
    reject("version", ms=ms_with(version="0.4"),
           setup=lambda d: (group(d, "old", {"multiscales": [{"version": "0.3", "axes": ["y", "x"],
                                                             "datasets": [{"path": "0"}]}]}),
                            array(d, "old/0", data((2, 2)), [2, 2], write_chunks=False)))
    reject("multiscales_empty", setup=lambda d: group(d, "g", {"multiscales": []}))
    reject("axes_count", ms=ms_with(axes=[AX["x"]]))
    reject("axes_name", ms=ms_with(axes=[AX["y"], {"name": 0, "type": "space"}]))
    reject("axes_duplicate", ms=ms_with(axes=[AX["x"], AX["x"]]))
    reject("axes_type", ms=ms_with(axes=[AX["y"], {"name": "x", "type": 3}]))
    reject("axes_order", axes="ycx", shape=(4, 2, 6))
    scale4 = [{"path": "0", "coordinateTransformations": [{"type": "scale", "scale": [1, 1, 1, 1]}]}]
    reject("axes_two_others", shape=(2, 2, 4, 6),
           ms=ms_with(axes=[AX["c"], {"name": "p", "type": "phase"}, AX["y"], AX["x"]], datasets=scale4))
    reject("axes_four_space", shape=(2, 2, 4, 6),
           ms=ms_with(axes=[{"name": n, "type": "space"} for n in "wzyx"], datasets=scale4))
    reject("datasets_empty", ms=ms_with(datasets=[]))
    reject("datasets_path", ms=ms_datasets(lambda ds: ds[-1].update(path="../1")))
    reject("datasets_missing", ms=ms_datasets(lambda ds: ds[-1].update(path="9")))
    reject("datasets_rank", ms=ms_datasets(lambda ds: ds[-1].update(path="r")),
           setup=lambda d: array(d, "r", data((2, 3, 3)), [2, 3, 3], write_chunks=False))
    reject("transforms_missing", ms=ms_datasets(lambda ds: ds[-1].pop("coordinateTransformations")))
    reject("transforms_order", ms=ms_datasets(lambda ds: ds[-1].update(coordinateTransformations=[
        {"type": "translation", "translation": [0, 0]}, {"type": "scale", "scale": [2, 2]}])))
    reject("transforms_path", ms=ms_datasets(lambda ds: ds[-1].update(coordinateTransformations=[
        {"type": "scale", "path": "scales/1"}])))
    reject("transforms_identity", ms=ms_datasets(lambda ds: ds[-1].update(coordinateTransformations=[
        {"type": "identity"}])))
    reject("scale_length", ms=ms_datasets(lambda ds: ds[-1].update(coordinateTransformations=[
        {"type": "scale", "scale": [2, 2, 2]}])))
    reject("translation_length", ms=ms_datasets(lambda ds: ds[-1].update(coordinateTransformations=[
        {"type": "scale", "scale": [2, 2]}, {"type": "translation", "translation": [1]}])))
    reject("multiscale_transforms", ms=ms_with(coordinateTransformations=[{"type": "scale", "scale": [1]}]))
    reject("scale_order", levels=2, ms=ms_datasets(lambda ds: ds[-1].update(coordinateTransformations=[
        {"type": "scale", "scale": [0.5, 2]}]), levels=2))

    reject("level_axes_conflict", setup=lambda d: (
        group(d, "p", {"multiscales": [{"version": "0.4", "axes": [AX["y"], AX["x"]], "datasets": [
            {"path": "q/lvl", "coordinateTransformations": [{"type": "scale", "scale": [1, 1]}]}]}]}),
        group(d, "p/q", {"multiscales": [{"version": "0.4", "axes": [{"name": "a", "type": "space"},
                                                                     {"name": "b", "type": "space"}],
                                          "datasets": [{"path": "lvl", "coordinateTransformations": [
                                              {"type": "scale", "scale": [1, 1]}]}]}]}),
        array(d, "p/q/lvl", data((2, 2)), [2, 2], write_chunks=False)))
    reject("omero_color", {"omero": {**omero(1), "channels": [{**omero(1)["channels"][0], "color": "red"}]}})
    reject("omero_window", {"omero": {**omero(1), "channels": [{**omero(1)["channels"][0],
                                                                 "window": {"start": 0, "min": 0, "max": 1}}]}})
    reject("omero_channels", {"omero": {"rdefs": {"model": "color"}}})

    def label_reject(name, image_levels=1, label_levels=1, **kw):
        d = store(f"ome_zarr_reject_{name}")
        labelled(d, image_levels=image_levels, label_levels=label_levels, write_chunks=False, **kw)
        return d

    d = label_reject("labels_not_image", listed=("cells", "missing"))
    group(d, "labels/missing")
    il = {"colors": [{"label-value": 1, "rgba": [1, 2, 3, 4]}]}
    label_reject("image_label_levels", image_label=il, image_levels=2)  # fewer levels than the image
    label_reject("image_label_dtype", image_label=il, label_dtype="<f4")
    label_reject("image_label_color_duplicate",
                 image_label={"colors": [{"label-value": 1, "rgba": [1, 2, 3, 4]}, {"label-value": 1.0}]})
    label_reject("image_label_rgba", image_label={"colors": [{"label-value": 1, "rgba": [1, 2, 3, 300]}]})
    label_reject("image_label_value", image_label={"properties": [{"label-value": "one"}]})
    label_reject("image_label_source", image_label={**il, "source": {"image": "../../elsewhere"}})
    label_reject("image_label_version", image_label={**il, "version": "0.3"})
    d = store("ome_zarr_reject_image_label_no_multiscales")
    image(d, "", "yx", (4, 4), 1, write_chunks=False)
    group(d, "labels", {"labels": []})
    group(d, "labels/cells", {"image-label": il})

    def plate_reject(name, fields=None, root=None, **over):
        """A plate of one well (A/1) of one field (or `fields`), with the plate document's members `over`."""
        d = store(f"ome_zarr_reject_{name}")
        plate(d, doc=plate_doc(**{"wells": [WELL_A1], **over}), wells=fields or WELLS_SMALL, small=True, root=root)
        return d

    def well_reject(name, images):
        """The one-well plate, with well A/1's images replaced by `images`."""
        d = plate_reject(name)
        group(d, "A/1", {"well": {"version": "0.4", "images": images}})

    plate_reject("plate_column_name", columns=[{"name": "1"}, {"name": "2-b"}])
    plate_reject("plate_row_duplicate", rows=[{"name": "A"}, {"name": "B"}, {"name": "A"}])
    plate_reject("plate_well_path", wells=[WELL_A1, {"path": "B/2", "rowIndex": 0, "columnIndex": 1}])
    plate_reject("plate_well_index", wells=[WELL_A1, {"path": "B/2", "rowIndex": 1, "columnIndex": 2}])
    plate_reject("plate_well_missing", wells=[WELL_A1, {"path": "A/2", "rowIndex": 0, "columnIndex": 1}])
    plate_reject("plate_acquisition_id", acquisitions=[{"id": 0}, {"id": -1}])
    plate_reject("plate_field_count", field_count=0)
    # a plate of version 0.3 under bioformats2raw.layout, found through its first field's 0.4 multiscales
    plate_reject("plate_version", root={"bioformats2raw.layout": 3,
                                        "plate": plate_doc(version="0.3", wells=[WELL_A1])})
    plate_reject("well_image_path", fields={"A/1": [{"path": "f-0", "acquisition": 0}]})
    well_reject("well_image_missing", [{"path": "0", "acquisition": 0}, {"path": "7", "acquisition": 0}])
    well_reject("well_image_duplicate", [{"path": "0", "acquisition": 0}] * 2)
    well_reject("well_acquisition", [{"path": "0", "acquisition": 5}])
    well_reject("well_acquisition_missing", [{"path": "0"}])

    d = store("ome_zarr_reject_bioformats2raw_layout")
    group(d, "", {"bioformats2raw.layout": 2})
    image(d, "0", "yx", (4, 4), 1, write_chunks=False)
    d = store("ome_zarr_reject_bioformats2raw_numbering")
    group(d, "", {"bioformats2raw.layout": 3})
    image(d, "0", "yx", (4, 4), 1, write_chunks=False)
    image(d, "2", "yx", (4, 4), 1, write_chunks=False)
    d = store("ome_zarr_reject_bioformats2raw_series")
    group(d, "", {"bioformats2raw.layout": 3})
    image(d, "0", "yx", (4, 4), 1, write_chunks=False)
    group(d, "OME", {"series": ["0", "1"]})

    # ------------------------------------------------------------ source metadata and other objects

    # The attributes A of an OME group keep only what the inverse of spec/virtualize/ome-zarr.md §5
    # does not give back from `ome` (§8): an omero without a version, and a non-OME member; an
    # OME group with only OME members of version 0.4 has no attributes there.
    # The Zarr v2 metadata M (a .zgroup member, a null fill value) is kept as in the Zarr v2
    # convention, and so are the objects that are not documents or chunks: OME-XML outside a
    # bioformats2raw collection, a README. Written after the stores above, so that their
    # random data does not change.
    d = store("ome_zarr_source_metadata")
    write_json(d / ".zgroup", {"zarr_format": 2, "creator": "a writer"})
    write_json(d / ".zattrs", {"well": {"version": "0.4", "images": [{"path": "0"}, {"path": "1"}]}})
    o = omero(1)
    del o["version"]
    image(d, "0", "yx", (4, 4), 1, attrs={"omero": o, "note": {"big": 18446744073709551615}})
    image(d, "1", "yx", (4, 4), 1, attrs={"omero": omero(1)})
    write_json(d / "1/0/.zarray", {**json.loads((d / "1/0/.zarray").read_text()), "fill_value": None})
    put(d / "OME/METADATA.ome.xml", b"<OME/>\n")
    put(d / "README.md", b"# a well\n")

    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"{sum(1 for _ in OUT.iterdir())} stores, {total} bytes")


def exact_members() -> None:
    """An empty OME-XML object, which is an empty object of the Zarr v2 convention, and a
    plate whose wells and images have no version: unversioned members, given back by name
    (spec/virtualize/ome-zarr.md §5, §7, §8). Fixed data, no RNG."""
    d = store("ome_zarr_empty_xml")
    group(d, "", {"bioformats2raw.layout": 3})
    group(d, "OME", {"series": ["0"]})
    put(d / "OME/METADATA.ome.xml", b"")
    ms = {k: v for k, v in multiscale("yx", 1).items() if k != "version"}
    group(d, "0", {"multiscales": [{**ms, "version": "0.4"}]})
    array(d, "0/0", np.arange(4, dtype="|u1").reshape(2, 2), [2, 2], sep="/")
    d = store("ome_zarr_plate_unversioned")
    rows, cols = ["A", "B"], ["1", "2", "3"]
    wells = [{"path": f"{r}/{c}", "rowIndex": i, "columnIndex": j} for i, r in enumerate(rows) for j, c in enumerate(cols)]
    group(d, "", {"plate": {"version": "0.4", "rows": [{"name": r} for r in rows],
                            "columns": [{"name": c} for c in cols], "wells": wells, "field_count": 1}})
    for w in wells:
        group(d, w["path"], {"well": {"images": [{"path": "0"}]}})
        group(d, f"{w['path']}/0", {"multiscales": [ms], "omero": {k: v for k, v in omero(1).items() if k != "version"}})
        array(d, f"{w['path']}/0/0", np.arange(4, dtype="|u1").reshape(2, 2), [2, 2], sep="/", write_chunks=False)




if __name__ == "__main__":
    main()
    exact_members()
