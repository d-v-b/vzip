"""The OME-Zarr profile (profiles/ome-zarr.md, §11): an OME-Zarr 0.4 hierarchy
on Zarr v2 storage as OME-Zarr 0.5 on Zarr v3, each chunk referenced in place.
"""

from __future__ import annotations

import re

from vzip.virtualize.common import Rejected
from vzip.virtualize.store import Store, StoreOutput, as_int, canonical_index, is_number, join, parent_of
from vzip.virtualize.zarr2.virtualize import hierarchy_output, read_hierarchy

OME_KEYS = ("multiscales", "omero", "labels", "image-label", "plate", "well", "bioformats2raw.layout")
VERSIONED = ("omero", "image-label", "plate", "well")  # whose own `version` is dropped (§11.4)
LABEL_TYPES = {"int8", "uint8", "int16", "uint16", "int32", "uint32", "int64", "uint64"}
_KINDS = re.compile(r"^t?o?s{2,3}$")
_HEX6 = re.compile(r"^[0-9A-Fa-f]{6}$")
_ALNUM = re.compile(r"^[A-Za-z0-9]+$")


def rel_path(p) -> bool:
    """A relative path (§11.2): one or more segments separated by `/`, none empty, `.` or `..`."""
    return isinstance(p, str) and p != "" and all(s not in ("", ".", "..") for s in p.split("/"))


def is_int(v, lo: int = -(2**53 - 1)) -> bool:
    return as_int(v, lo) is not None


def _multiscales_04(a) -> bool:
    ms = a.get("multiscales")
    return isinstance(ms, list) and any(isinstance(m, dict) and m.get("version") == "0.4" for m in ms)


def _version_04(a, key: str) -> bool:
    v = a.get(key)
    return isinstance(v, dict) and v.get("version") == "0.4"


def declares_04(store: Store) -> bool:
    """Does the root of this Zarr v2 store declare OME-NGFF 0.4 content (§1.4)?"""
    objects = store.objects

    def attrs(path: str):
        if join(path, ".zgroup") not in objects or join(path, ".zattrs") not in objects:
            return None
        a = store.document(join(path, ".zattrs"))
        return a if isinstance(a, dict) else None

    if ".zarray" in objects:
        return False
    a = attrs("")
    if a is None:
        return False
    if _multiscales_04(a) or _version_04(a, "plate") or _version_04(a, "well"):
        return True
    if "bioformats2raw.layout" not in a:
        return False
    q = None
    if "plate" in a:
        plate = a["plate"]
        wells = plate.get("wells") if isinstance(plate, dict) else None
        if isinstance(wells, list) and wells and isinstance(wells[0], dict) and rel_path(wells[0].get("path")):
            w = wells[0]["path"]
            wa = attrs(w)
            well = wa.get("well") if wa is not None else None
            images = well.get("images") if isinstance(well, dict) else None
            if (isinstance(images, list) and images and isinstance(images[0], dict)
                    and rel_path(images[0].get("path"))):
                q = join(w, images[0]["path"])
    else:
        q = "0"
        oa = attrs("OME")
        series = oa.get("series") if oa is not None else None
        if isinstance(series, list) and series and rel_path(series[0]):
            q = series[0]
    if q is None:
        return False
    qa = attrs(q)
    return qa is not None and _multiscales_04(qa)


def resolve(base: str, rel: str) -> str | None:
    """`rel` resolved against the group path `base` (§11.3, image-label `source`), or None above the root."""
    segs = base.split("/") if base else []
    for s in rel.split("/"):
        if s in ("", "."):
            continue
        if s == "..":
            if not segs:
                return None
            segs.pop()
        else:
            segs.append(s)
    return "/".join(segs)


def _transforms(where: str, ts, n: int) -> list:
    """The scale of a valid list of coordinate transformations (§11.3, rule I5)."""
    if not isinstance(ts, list) or len(ts) not in (1, 2) or not all(isinstance(t, dict) for t in ts):
        raise Rejected(f"{where}: coordinateTransformations is not one or two transformation objects")
    s = ts[0]
    if s.get("type") != "scale":
        raise Rejected(f"{where}: the first transformation is not a scale")
    if not (isinstance(s.get("scale"), list) and len(s["scale"]) == n and all(is_number(x) for x in s["scale"])):
        raise Rejected(f"{where}: the scale is not {n} numbers")
    if len(ts) == 2:
        t = ts[1]
        if t.get("type") != "translation":
            raise Rejected(f"{where}: the second transformation is not a translation")
        tr = t.get("translation")
        if not (isinstance(tr, list) and len(tr) == n and all(is_number(x) for x in tr)):
            raise Rejected(f"{where}: the translation is not {n} numbers")
    return s["scale"]


def _version(where: str, obj: dict) -> None:
    if "version" in obj and obj["version"] != "0.4":
        raise Rejected(f"{where}: version {str(obj['version'])[:20]!r} is not 0.4")


def virtualize_ome_zarr(store: Store) -> StoreOutput:
    h = read_hierarchy(store)  # §11.1: every rule of §10.1–§10.3
    attrs = h.groups
    arrays = h.arrays
    for path in sorted(attrs):
        if "ome" in attrs[path]:
            raise Rejected(f"{path or '/'}: the group already has an `ome` attribute")
    collections = sorted(p for p in attrs if "bioformats2raw.layout" in attrs[p])
    series_groups = {join(c, "OME") for c in collections if "series" in attrs.get(join(c, "OME"), {})}
    ome_groups = sorted(p for p in attrs if p in series_groups or any(k in attrs[p] for k in OME_KEYS))
    images = {p for p in attrs if "multiscales" in attrs[p]}

    # §11.3 Images.
    names: dict[str, list[str]] = {}  # level array -> axis names
    multiscales: dict[str, list[dict]] = {}
    for g in sorted(images):
        ms = attrs[g]["multiscales"]
        where = f"{g or '/'}: multiscales"
        if not isinstance(ms, list) or not ms or not all(isinstance(m, dict) for m in ms):
            raise Rejected(f"{where} is not a nonempty array of objects")
        multiscales[g] = ms
        for i, m in enumerate(ms):
            w = f"{where}[{i}]"
            _version(w, m)
            axes = m.get("axes")
            if not isinstance(axes, list) or not 2 <= len(axes) <= 5 or not all(isinstance(a, dict) for a in axes):
                raise Rejected(f"{w}: axes is not 2 to 5 axis objects")
            if not all(isinstance(a.get("name"), str) for a in axes):
                raise Rejected(f"{w}: an axis name is not a string")
            axis_names = [a["name"] for a in axes]
            if len(set(axis_names)) != len(axis_names):
                raise Rejected(f"{w}: axis names {axis_names} are not distinct")
            kinds = ""
            for a in axes:
                t = a.get("type")
                if t is not None and not isinstance(t, str):
                    raise Rejected(f"{w}: axis type {str(t)[:20]} is not a string")
                kinds += "s" if t == "space" else "t" if t == "time" else "o"
            if not _KINDS.match(kinds):
                raise Rejected(f"{w}: axis types {[a.get('type') for a in axes]} are not [time], [channel or "
                               f"custom], then 2 or 3 space")
            n = len(axes)
            datasets = m.get("datasets")
            if not isinstance(datasets, list) or not datasets or not all(isinstance(d, dict) for d in datasets):
                raise Rejected(f"{w}: datasets is not a nonempty array of objects")
            prev = None
            for j, d in enumerate(datasets):
                wd = f"{w}.datasets[{j}]"
                p = d.get("path")
                if not rel_path(p):
                    raise Rejected(f"{wd}: path {str(p)[:40]!r} is not a relative path")
                target = join(g, p)
                if target not in arrays:
                    raise Rejected(f"{wd}: {target} is not an array")
                if len(arrays[target]["shape"]) != n:
                    raise Rejected(f"{wd}: {target} has {len(arrays[target]['shape'])} dimensions, not {n}")
                scale = _transforms(wd, d.get("coordinateTransformations"), n)
                if prev is not None and any(b < a for a, b in zip(prev, scale)):
                    raise Rejected(f"{wd}: the scale {scale} is smaller than the previous level's {prev}")
                prev = scale
                if names.setdefault(target, axis_names) != axis_names:
                    raise Rejected(f"{target} is a level of images with axes {names[target]} and {axis_names}")
            if "coordinateTransformations" in m:
                _transforms(w, m["coordinateTransformations"], n)

    # §11.3 omero.
    for g in ome_groups:
        if "omero" not in attrs[g]:
            continue
        o = attrs[g]["omero"]
        w = f"{g or '/'}: omero"
        channels = o.get("channels") if isinstance(o, dict) else None
        if not isinstance(channels, list) or not all(isinstance(c, dict) for c in channels):
            raise Rejected(f"{w}: channels is not an array of objects")
        for c in channels:
            if not (isinstance(c.get("color"), str) and _HEX6.match(c["color"])):
                raise Rejected(f"{w}: channel color {str(c.get('color'))[:20]!r} is not 6 hexadecimal digits")
            win = c.get("window")
            if not isinstance(win, dict) or not all(is_number(win.get(k)) for k in ("min", "max", "start", "end")):
                raise Rejected(f"{w}: a channel window does not have numbers min, max, start and end")

    # §11.3 Labels.
    label_images: set[str] = set()
    for g in ome_groups:
        if "labels" in attrs[g]:
            ls = attrs[g]["labels"]
            if not isinstance(ls, list):
                raise Rejected(f"{g or '/'}: labels is not an array")
            for x in ls:
                if not rel_path(x) or join(g, x) not in images:
                    raise Rejected(f"{g or '/'}: label {str(x)[:40]!r} is not the path of an image")
                label_images.add(join(g, x))
        if "image-label" in attrs[g]:
            il = attrs[g]["image-label"]
            w = f"{g or '/'}: image-label"
            if not isinstance(il, dict):
                raise Rejected(f"{w} is not an object")
            _version(w, il)
            if g not in images:
                raise Rejected(f"{w}: the group has no multiscales")
            label_images.add(g)
            for key in ("colors", "properties"):
                if key not in il:
                    continue
                items = il[key]
                if not isinstance(items, list) or not all(isinstance(c, dict) and is_int(c.get("label-value"))
                                                          for c in items):
                    raise Rejected(f"{w}: {key} is not an array of objects with an integer label-value")
            if "colors" in il:
                values = [as_int(c["label-value"]) for c in il["colors"]]
                if len(set(values)) != len(values):
                    raise Rejected(f"{w}: colors label-values are not unique")
                for c in il["colors"]:
                    if "rgba" in c and not (isinstance(c["rgba"], list) and len(c["rgba"]) == 4
                                            and all(as_int(x, 0, 255) is not None for x in c["rgba"])):
                        raise Rejected(f"{w}: rgba {str(c['rgba'])[:40]} is not four integers from 0 to 255")
            if "source" in il:
                src = il["source"]
                if not isinstance(src, dict) or ("image" in src and not isinstance(src["image"], str)):
                    raise Rejected(f"{w}: source is not an object with a string image")
    for x in sorted(label_images):
        il = attrs[x].get("image-label")
        rel = il.get("source", {}).get("image") if isinstance(il, dict) else None
        source = resolve(x, rel if rel is not None else "../../")
        if rel is not None and source not in images:
            raise Rejected(f"{x}: the label's source image {rel!r} is not an image")
        if source in images:
            n = len(multiscales[source][0]["datasets"])
            for m in multiscales[x]:
                if len(m["datasets"]) != n:
                    raise Rejected(f"{x}: the label image has {len(m['datasets'])} levels, its image "
                                   f"{source or '/'} {n}")
        for m in multiscales[x]:
            for d in m["datasets"]:
                level = join(x, d["path"])
                if arrays[level]["data_type"] not in LABEL_TYPES:
                    raise Rejected(f"{level}: label data type {arrays[level]['data_type']} is not an integer type")

    # §11.3 Plates and wells.
    wells = {p for p in attrs if "well" in attrs[p]}
    well_images: dict[str, list[dict]] = {}
    for g in sorted(wells):
        wl = attrs[g]["well"]
        w = f"{g or '/'}: well"
        if not isinstance(wl, dict):
            raise Rejected(f"{w} is not an object")
        _version(w, wl)
        ims = wl.get("images")
        if not isinstance(ims, list) or not all(isinstance(i, dict) for i in ims):
            raise Rejected(f"{w}: images is not an array of objects")
        paths = [i.get("path") for i in ims]
        for i in ims:
            p = i.get("path")
            if not (isinstance(p, str) and _ALNUM.match(p)):
                raise Rejected(f"{w}: image path {str(p)[:40]!r} is not alphanumeric")
            if join(g, p) not in images:
                raise Rejected(f"{w}: {join(g, p)} is not an image")
            if "acquisition" in i and not is_int(i["acquisition"]):
                raise Rejected(f"{w}: acquisition {str(i['acquisition'])[:20]} is not an integer")
        if len(set(paths)) != len(paths):
            raise Rejected(f"{w}: image paths are not unique")
        well_images[g] = ims
    plates = sorted(p for p in attrs if "plate" in attrs[p])
    for g in plates:
        pl = attrs[g]["plate"]
        w = f"{g or '/'}: plate"
        if not isinstance(pl, dict):
            raise Rejected(f"{w} is not an object")
        _version(w, pl)
        named = {}
        for key in ("columns", "rows"):
            items = pl.get(key)
            if not isinstance(items, list) or not all(isinstance(c, dict) for c in items):
                raise Rejected(f"{w}: {key} is not an array of objects")
            ns = [c.get("name") for c in items]
            if not all(isinstance(x, str) and _ALNUM.match(x) for x in ns):
                raise Rejected(f"{w}: a {key[:-1]} name is not alphanumeric")
            if len(set(ns)) != len(ns):
                raise Rejected(f"{w}: {key[:-1]} names are not unique")
            named[key] = ns
        if "field_count" in pl and as_int(pl["field_count"], 1) is None:
            raise Rejected(f"{w}: field_count {str(pl['field_count'])[:20]} is not a positive integer")
        if "name" in pl and not isinstance(pl["name"], str):
            raise Rejected(f"{w}: name is not a string")
        ids = None
        if "acquisitions" in pl:
            acq = pl["acquisitions"]
            if not isinstance(acq, list) or not all(isinstance(a, dict) for a in acq):
                raise Rejected(f"{w}: acquisitions is not an array of objects")
            for a in acq:
                if as_int(a.get("id"), 0) is None:
                    raise Rejected(f"{w}: acquisition id {str(a.get('id'))[:20]} is not an integer from 0")
                if "maximumfieldcount" in a and as_int(a["maximumfieldcount"], 1) is None:
                    raise Rejected(f"{w}: maximumfieldcount is not a positive integer")
                for k in ("name", "description"):
                    if k in a and not isinstance(a[k], str):
                        raise Rejected(f"{w}: acquisition {k} is not a string")
                for k in ("starttime", "endtime"):
                    if k in a and not is_int(a[k]):
                        raise Rejected(f"{w}: acquisition {k} is not an integer")
            ids = [as_int(a["id"]) for a in acq]
            if len(set(ids)) != len(ids):
                raise Rejected(f"{w}: acquisition ids are not unique")
        ws = pl.get("wells")
        if not isinstance(ws, list) or not all(isinstance(x, dict) for x in ws):
            raise Rejected(f"{w}: wells is not an array of objects")
        for x in ws:
            r, c, p = as_int(x.get("rowIndex"), 0), as_int(x.get("columnIndex"), 0), x.get("path")
            if r is None or c is None or r >= len(named["rows"]) or c >= len(named["columns"]):
                raise Rejected(f"{w}: well rowIndex or columnIndex is not an index of rows or columns")
            if p != f"{named['rows'][r]}/{named['columns'][c]}":
                raise Rejected(f"{w}: well path {str(p)[:40]!r} is not row {named['rows'][r]}/column "
                               f"{named['columns'][c]}")
            if join(g, p) not in wells:
                raise Rejected(f"{w}: {join(g, p)} is not a well")
            if ids is not None:
                for i in well_images[join(g, p)]:
                    if "acquisition" in i:
                        if as_int(i["acquisition"]) not in ids:
                            raise Rejected(f"{join(g, p)}: acquisition {i['acquisition']} is not one of the plate's")
                    elif len(ids) > 1:
                        raise Rejected(f"{join(g, p)}: an image has no acquisition, and the plate has several")

    # §11.3 Collections (bioformats2raw.layout).
    for g in collections:
        a = attrs[g]
        if as_int(a["bioformats2raw.layout"]) != 3:
            raise Rejected(f"{g or '/'}: bioformats2raw.layout is not 3")
        ome = join(g, "OME")
        if ome in series_groups:
            series = attrs[ome]["series"]
            if not isinstance(series, list) or not all(rel_path(s) and join(g, s) in images for s in series):
                raise Rejected(f"{ome}: series is not an array of image paths")
        elif "plate" not in a:
            numbered = sorted(int(p.rpartition("/")[2]) for p in images
                              if parent_of(p) == g and p != g and canonical_index(p.rpartition("/")[2], 2**53))
            if numbered != list(range(len(numbered))) or not numbered:
                raise Rejected(f"{g or '/'}: the images are not numbered consecutively from 0")

    # §11.4, §11.5 Output.
    groups = {}
    for path, a in attrs.items():
        if path not in ome_groups:
            groups[path] = a
            continue
        keys = OME_KEYS + (("series",) if path in series_groups else ())
        ome = {"version": "0.5"}
        for k in keys:
            if k not in a:
                continue
            v = a[k]
            if k == "multiscales":
                v = [{x: y for x, y in m.items() if x != "version"} for m in v]
            elif k in VERSIONED:
                v = {x: y for x, y in v.items() if x != "version"}
            ome[k] = v
        out = {k: v for k, v in a.items() if k not in keys}
        out["ome"] = ome
        groups[path] = out
    docs = {}
    for path, doc in arrays.items():
        if path in names:
            doc = {**{k: v for k, v in doc.items() if k != "attributes"}, "dimension_names": names[path],
                   "attributes": doc["attributes"]}
        docs[path] = doc
    xml = [(k, store.objects[k]) for k in (join(join(c, "OME"), "METADATA.ome.xml") for c in collections)
           if k in store.objects]
    out, chunks = hierarchy_output(store, h, groups, docs, xml)
    nonempty = sum(1 for _, n in chunks if n > 0)
    out.summary = {
        "groups": len(attrs) + len(h.implicit), "arrays": len(arrays), "chunks": nonempty,
        "emptyChunks": len(chunks) - nonempty, "objects": len(store.objects), "images": len(images),
        "labels": len(label_images), "plates": len(plates), "wells": len(wells),
        "fields": sum(len(v) for v in well_images.values()), "omeXml": sum(1 for _, n in xml if n > 0),
        "listingRequests": store.requests,
    }
    return out
