"""The CZI profile (spec/virtualize/czi.md, spec/virtualize.md §13)."""

from __future__ import annotations

from vzip_reference.revision import REVISION
from vzip.virtualize.common import (
    SOURCE_NODE, Output, Reader, Rejected, array_json, declare, emit_plans, group_json, image_ome, metadata_group,
    root_json, transpose_codec,
)
from vzip_reference.czi.coding import (
    JPEG, JPEGXR, PIXEL_TYPES, UNCOMPRESSED, ZSTD1, bounded_head, codec_chain, coded_size, sample_letters,
)
from vzip_reference.czi.layout import SERIES_LETTERS, Level, Placed, classify, layer, plane_axes, row_band
from vzip_reference.czi.segments import (
    ATTACH, ATTDIR, DIRECTORY, FILE, METADATA, SUBBLOCK, guid, read_attachments, read_directory, read_file_header,
    read_metadata_segment, read_subblocks, walk,
)
from vzip_reference.czi.source import (
    attachment_plans, directory_plans, metadata_plans, node_metadata, segment_plans, subblock_plans, tail_plan,
)
from vzip_reference.czi.xml import MAX_XML, XmlValues, read_xml_values

MAGIC = b"ZISRAWFILE" + bytes(6)
MAX_IMAGES = 1 << 16  # series with an image (spec/virtualize.md §13.3)
MAX_LEVELS = 64  # levels per image
MAX_EXTENT = 1 << 31  # each dimension of an array's shape
MAX_OMERO = 64  # an image's channel indexes, for omero
MAX_NAME = 256  # bytes of a name in UTF-8
FILL = {"complex64": [0.0, 0.0]}
TILES = "tiles"


def is_czi(head: bytes) -> bool:
    return head[:16] == MAGIC


def _name(v: str | None) -> str | None:
    return v if v and len(v.encode("utf-8")) <= MAX_NAME else None


def _extent(v: int) -> int:
    if v > MAX_EXTENT:
        raise Rejected(f"an array dimension of {v}, more than 2^31")
    return v


def _dimensions(series: tuple) -> dict:
    return {letter: k[1] for letter, k in zip(SERIES_LETTERS, series) if len(k) == 2}


def _array_doc(shape, data_type, lengths, codecs, axes) -> dict:
    """An array's zarr.json, with the rectilinear chunk grid when a length is not one integer."""
    regular = all(isinstance(v, int) for v in lengths)
    doc = array_json(shape, data_type, lengths if regular else [], codecs, axes)
    if not regular:
        doc["chunk_grid"] = {"name": "rectilinear", "configuration": {"kind": "inline", "chunk_shapes": list(lengths)}}
    doc["fill_value"] = FILL.get(data_type, 0)
    return doc


def _codecs(form: tuple[int, int, bool], axes: list[str]) -> list[dict]:
    p = PIXEL_TYPES[form[0]][2]
    return ([transpose_codec(axes)] if p > 1 else []) + codec_chain(*form)


def _chunk_ranges(s: Placed, data: dict, band: int | None, q: int) -> list[tuple[int, int]]:
    """The ranges of a subblock's chunks along y, in order: one, or one per band of `band` rows."""
    start, n = data[s.index]
    comp = s.form[1]
    if comp == UNCOMPRESSED:
        if band is None or band >= s.h:
            return [(start, s.w * s.h * q)]
        row = s.w * q
        return [(start + k * band * row, band * row) for k in range(s.h // band)]
    if comp == ZSTD1:
        return [(start + s.header, n - s.header)]
    return [(start, n)]


def _omero(values: XmlValues, form: tuple[int, int, bool], lo_c: int, channels: int) -> dict | None:
    pixel_type, compression, _ = form
    _, data_type, p, _ = PIXEL_TYPES[pixel_type]
    if channels * p > MAX_OMERO:
        return None
    letters = sample_letters(pixel_type, compression)
    colors = {"B": "0000FF", "G": "00FF00", "R": "FF0000", "A": "FFFFFF"}
    type_bits = {"uint8": 8, "uint16": 16}.get(data_type)
    out = []
    for c in range(lo_c, lo_c + channels):
        info = values.info[c] if 0 <= c < len(values.info) else None
        display = values.display[c] if 0 <= c < len(values.display) else None
        label = _name(info.name if info else None) or _name(display.name if display else None) or f"C{c}"
        color = (display.color if display else None) or (info.color if info else None) or "FFFFFF"
        window = {}
        if type_bits is not None:
            bits = next((b for b in (info.bits if info else None, values.bits)
                         if b is not None and 1 <= b <= type_bits), type_bits)
            top, full = 2**bits - 1, 2**type_bits - 1
            low = display.low if display else None
            high = display.high if display else None
            window = {"window": {"min": 0, "max": top, "start": low * full if low is not None else 0,
                                 "end": high * full if high is not None else top}}
        for letter in letters:
            out.append({"label": f"{label} {letter}" if letter else label,
                        "color": colors[letter] if letter else color, "active": True, **window})
    return {"channels": out}


def virtualize_czi(url: str, read: Reader, size: int) -> Output:
    fh = read_file_header(read, size)
    d = read_directory(read, size, fh.directory)
    s = read_subblocks(read, size, d)
    meta = read_metadata_segment(read, size, fh.metadata) if fh.metadata else None
    att_allocated, atts = read_attachments(read, size, fh.attachments) if fh.attachments else (0, [])
    values = read_xml_values(read(meta.offset + 288, meta.xml)) if meta and 0 < meta.xml <= MAX_XML else XmlValues()

    # Placement (spec/virtualize/czi.md §3.2).
    placed: list[Placed] = []
    unplaced, trailing = [], []
    data: dict[int, tuple[int, int]] = {}
    for i in range(d.count):
        if not s.agrees[i]:
            unplaced.append(i)
            continue
        pt, comp, n = d.pixel_type[i], d.compression[i], s.data[i]
        x, y = d.dim(i, "X"), d.dim(i, "Y")
        start = d.file_position[i] + 32 + s.length[i] + s.metadata[i]
        coded = coded_size(pt, comp, x[2], y[2], n, bounded_head(read, start, n))
        if coded is None:
            unplaced.append(i)
            continue
        series = tuple((1, v[0]) if (v := d.dim(i, letter)) else (0,) for letter in SERIES_LETTERS)
        plane = tuple(v[0] if (v := d.dim(i, letter)) else 0 for letter in "TCZ")
        placed.append(Placed(i, series, plane, x[0], y[0], x[1], y[1], x[2], y[2], coded[0], coded[1],
                             (pt, comp, coded[2]), coded[3], layer(x[1], y[1], x[2], y[2])))
        data[i] = (start, n)
        pixels = x[2] * y[2] * PIXEL_TYPES[pt][3]
        if comp == UNCOMPRESSED and n > pixels:
            trailing.append((i, start + pixels, n - pixels))

    # Series, layers, levels and tiles (§3.3–§3.5).
    by_series: dict[tuple, list[Placed]] = {}
    for p in placed:
        by_series.setdefault(p.series, []).append(p)
    images: list[tuple[tuple, list[Level]]] = []
    tiles: list[Placed] = []
    for key in sorted(by_series):
        layers: dict[tuple, list[Placed]] = {}
        for p in by_series[key]:
            if p.layer is not None and p.conforming:
                layers.setdefault(p.layer, []).append(p)
            else:
                tiles.append(p)
        levels = []
        for lay in sorted(layers):
            level = classify(layers[lay])
            if level is None:
                tiles += layers[lay]
            else:
                levels.append(level)
        levels.sort(key=lambda lv: (lv.factor, lv.layer))
        if levels:
            kept = [lv for lv in levels if lv.form[0] == levels[0].form[0]]
            for lv in levels:
                if lv.form[0] != levels[0].form[0]:
                    tiles += [c[0] for c in lv.cells]
            if len(kept) > MAX_LEVELS:
                raise Rejected(f"an image of {len(kept)} levels, more than {MAX_LEVELS}")
            images.append((key, kept))
    if len(images) > MAX_IMAGES:
        raise Rejected(f"{len(images)} series with an image, more than {MAX_IMAGES}")

    out = Output(url)
    out.lazy = (SOURCE_NODE + "/", TILES + "/")
    root_s = {"version": [fh.major, fh.minor], "primary_file_guid": guid(fh.primary_file_guid),
              "file_guid": guid(fh.file_guid), "file_part": fh.file_part, "update_pending": fh.update_pending}
    root_ome = {"ome": {"version": "0.5", "bioformats2raw.layout": 3}} if images else {}
    out.json("zarr.json", {"zarr_format": 3, "node_type": "group", "attributes": declare(root_ome, "czi", url, root_s, REVISION)})
    if images:
        out.json("OME/zarr.json", group_json({"version": "0.5", "series": [str(k) for k in range(len(images))]}))
    levels_summary = []
    for k, (key, levels) in enumerate(images):
        _emit_image(out, k, key, levels, values, data)
        levels_summary.append([[lv.factor, lv.grid[0], lv.grid[1]] for lv in levels])

    # Tiles, one array per tile position and copy (§4.4).
    groups: dict[tuple, list[Placed]] = {}
    copies: dict[tuple, int] = {}
    for p in sorted(tiles, key=lambda t: t.index):
        g = (p.series, p.x, p.y, p.wl, p.hl, p.w, p.h, p.cw, p.ch, p.form)
        copy = copies.get((g, p.plane), 0)
        copies[(g, p.plane)] = copy + 1
        groups.setdefault((g, copy), []).append(p)
    if groups:
        metadata_group(out, TILES)
    for n, ((g, copy), members) in enumerate(groups.items()):
        _emit_tile(out, f"{TILES}/{n}", members, copy, data)

    # The source metadata node (§5).
    known = {0: (FILE, fh.allocated, 0), fh.directory: (DIRECTORY, d.allocated, 0)}
    for i in range(d.count):
        known[d.file_position[i]] = (SUBBLOCK, s.allocated[i], 0)
    if meta is not None:
        known[meta.offset] = (METADATA, meta.allocated, 0)
    if fh.attachments:
        known[fh.attachments] = (ATTDIR, att_allocated, 0)
    for a in atts:
        if a.a1:
            known[a.offset] = (ATTACH, a.allocated, 0)
    segments, tail = walk(read, size, known)
    others = [seg for seg in segments if seg[0] not in known]
    listed, att_plans = attachment_plans(read, atts)
    node, index_plans = node_metadata(listed)
    plans = (directory_plans(d) + subblock_plans(read, d, s, unplaced, trailing) + metadata_plans(meta)
             + att_plans + index_plans + segment_plans(read, others) + tail_plan(tail, size))
    group_plans = [p for p in plans if p.data_type == "group"]
    plans = [p for p in plans if p.data_type != "group"]
    if node or plans:
        emit_plans(out, plans, declare({}, "czi", None, node))
        for g in group_plans:
            metadata_group(out, f"{SOURCE_NODE}/{g.path}", g.attributes)
    out.summary = {"subblocks": d.count, "images": levels_summary, "tiles": len(groups), "unplaced": len(unplaced),
                   "segments": len(others), "attachments": len(atts), "tail": size - tail}
    return out


def _emit_image(out: Output, k: int, key: tuple, levels: list[Level], values: XmlValues, data: dict) -> None:
    pixel_type = levels[0].form[0]
    _, data_type, p, q = PIXEL_TYPES[pixel_type]
    axes, lo, extent = plane_axes([c[0].plane for lv in levels for c in lv.cells], p)
    dims = _dimensions(key)
    name = _name(values.scenes.get(dims["S"])) if "S" in dims else None
    um = 1 / 1e-6  # metres in micrometres (conventions §5)
    px = values.px * um if values.px is not None else None
    py = values.py * um if values.py is not None else None
    units = {"x": "micrometer" if px is not None else None, "y": "micrometer" if py is not None else None,
             "z": "micrometer" if values.pz is not None else None, "t": "second" if values.inc is not None else None}
    scales, translations = [], []
    for lv in levels:
        f = lv.factor
        scale = {"t": values.inc if values.inc is not None else 1, "c": 1,
                 "z": values.pz * um if values.pz is not None else 1,
                 "y": py * f if py is not None else f, "x": px * f if px is not None else f}
        shift = {"x": (lv.origin[0] + (f - 1) / 2) * (px if px is not None else 1),
                 "y": (lv.origin[1] + (f - 1) / 2) * (py if py is not None else 1)}
        scales.append([scale[a] for a in axes])
        translations.append([shift.get(a, 0) for a in axes])
    ome = image_ome(axes, units, scales, name, translations)
    omero = _omero(values, levels[0].form, lo["c"], extent["c"])
    if omero is not None:
        ome["omero"] = omero
    out.json(f"{k}/zarr.json", {"zarr_format": 3, "node_type": "group",
                                "attributes": declare({"ome": ome}, "czi", None, {"dimensions": dims} if dims else None)})
    for di, lv in enumerate(levels):
        (w, h), (w2, h2), (m, r) = lv.tile, lv.edge, lv.grid
        band = row_band(h, h2, w, q) if lv.form[1] == UNCOMPRESSED else h
        banded = band < h
        size = {"t": _extent(extent["t"]), "c": _extent(extent["c"] * p), "z": _extent(extent["z"]),
                "y": _extent((r - 1) * h + h2), "x": _extent((m - 1) * w + w2)}
        y_len = band if banded or h2 == h else ([[h, r - 1], h2] if r > 1 else [h2])
        x_len = w if w2 == w else ([[w, m - 1], w2] if m > 1 else [w2])
        lengths = {"t": 1, "c": p, "z": 1, "y": y_len, "x": x_len}
        path = f"{k}/{di}"
        out.json(f"{path}/zarr.json", _array_doc([size[a] for a in axes], data_type, [lengths[a] for a in axes],
                                                 _codecs(lv.form, axes), axes))
        per_tile = h // band
        for sb, col, row in lv.cells:
            index = {"t": sb.plane[0] - lo["t"], "c": sb.plane[1] - lo["c"], "z": sb.plane[2] - lo["z"]}
            for bk, rng in enumerate(_chunk_ranges(sb, data, band if banded else None, q)):
                index["y"], index["x"] = row * per_tile + bk, col
                out.refs[f"{path}/c/" + "/".join(str(index[a]) for a in axes)] = [rng]


def _emit_tile(out: Output, path: str, members: list[Placed], copy: int, data: dict) -> None:
    first = members[0]
    _, data_type, p, q = PIXEL_TYPES[first.form[0]]
    axes, lo, extent = plane_axes([m.plane for m in members], p)
    band = row_band(first.ch, first.ch, first.cw, q) if first.form[1] == UNCOMPRESSED else first.ch
    size = {"t": _extent(extent["t"]), "c": _extent(extent["c"] * p), "z": _extent(extent["z"]),
            "y": first.ch, "x": first.cw}
    lengths = {"t": 1, "c": p, "z": 1, "y": band, "x": first.cw}
    dims = _dimensions(first.series)
    own = {**({"dimensions": dims} if dims else {}), "x": first.x, "y": first.y, "size": [first.wl, first.hl],
           "stored_size": [first.w, first.h], "planes": {"t": lo["t"], "c": lo["c"], "z": lo["z"]}, "copy": copy}
    doc = _array_doc([size[a] for a in axes], data_type, [lengths[a] for a in axes], _codecs(first.form, axes), axes)
    doc["attributes"] = declare({}, "czi", None, own)
    out.json(f"{path}/zarr.json", doc)
    for sb in members:
        index = {"t": sb.plane[0] - lo["t"], "c": sb.plane[1] - lo["c"], "z": sb.plane[2] - lo["z"], "x": 0}
        for bk, rng in enumerate(_chunk_ranges(sb, data, band if band < first.ch else None, q)):
            index["y"] = bk
            out.refs[f"{path}/c/" + "/".join(str(index[a]) for a in axes)] = [rng]
