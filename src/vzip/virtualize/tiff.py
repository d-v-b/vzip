"""The TIFF profile of VIRTUALIZE.md (§3)."""

from __future__ import annotations

import math
import re
import struct

from vzip.virtualize.common import (
    LENGTHS, MAX_PAYLOAD, UNITS, Output, Reader, Rejected, array_json, centred, group_json, image_ome, payload_size,
    transpose_codec,
)

TAGS = {256, 257, 258, 259, 262, 270, 277, 282, 283, 284, 296, 317, 322, 323, 324, 325, 330, 339, 347}
RATIONAL_TAGS = {282, 283}
SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}
FORMATS = {1: "B", 6: "b", 7: "B", 3: "H", 8: "h", 4: "I", 13: "I", 9: "i", 16: "Q", 18: "Q", 17: "q",
           11: "f", 12: "d"}
JPEG2000 = {33003, 33004, 33005, 34712}
MAX_IFDS = 100000
MAX_PLANES = 100000
MAX_SAFE = 2**53 - 1
INTEGER_TYPES = {1, 3, 4, 13, 16, 18}
SCALARS = {256, 257, 259, 262, 277, 282, 283, 284, 296, 317, 322, 323}
JPEG = 7
# The Adobe APP14 marker, with its colour transform byte last (§3.6).
ADOBE = bytes.fromhex("FFEE000E41646F626500640000000000")[:-1]


class Ifd:
    def __init__(self, offset: int, tags: dict, types: dict) -> None:
        self.offset = offset
        self.tags = tags
        self.types = types
        self.sub: list[Ifd] = []

    def num(self, tag: int, default: int | None = None) -> int:
        v = self.tags.get(tag)
        if isinstance(v, list) and v:
            return v[0]
        if default is None:
            raise Rejected(f"IFD at {self.offset} has no tag {tag}")
        return default

    def nums(self, tag: int) -> list:
        v = self.tags.get(tag)
        if not isinstance(v, list):
            raise Rejected(f"IFD at {self.offset} has no tag {tag}")
        return v


def read_tiff(read: Reader, size: int):
    """(little_endian, main-chain IFDs with their SubIFDs) (§3.1)."""
    head = read(0, min(16, size))
    if len(head) < 8:
        raise Rejected("file too short for a TIFF header")
    if head[:2] not in (b"II", b"MM"):
        raise Rejected("not a TIFF file")
    e = "<" if head[:2] == b"II" else ">"
    magic = struct.unpack(e + "H", head[2:4])[0]
    if magic == 42:
        big, first = False, struct.unpack(e + "I", head[4:8])[0]
    elif magic == 43:
        if len(head) < 16 or struct.unpack(e + "HH", head[4:8]) != (8, 0):
            raise Rejected("invalid BigTIFF header")
        big, first = True, struct.unpack(e + "Q", head[8:16])[0]
    else:
        raise Rejected("not a TIFF file")
    count_size, entry_size, field_size = (8, 20, 8) if big else (2, 12, 4)
    seen: set[int] = set()

    def values(data: bytes, typ: int, n: int):
        if typ in (5, 10):
            v = struct.unpack(e + ("I" if typ == 5 else "i") * (2 * n), data[: 8 * n])
            return [v[2 * i] / v[2 * i + 1] if v[2 * i + 1] else 0.0 for i in range(n)]
        if typ == 2:
            return data
        out = list(struct.unpack(e + FORMATS[typ] * n, data[: SIZES[typ] * n]))
        if typ in (16, 17, 18) and any(abs(v) > MAX_SAFE for v in out):
            raise Rejected("a tag value is more than 2^53 - 1")
        return out

    def read_ifd(offset: int):
        if offset < (16 if big else 8):
            raise Rejected(f"IFD offset {offset} is inside the header")
        if offset in seen:
            raise Rejected(f"IFD offset {offset} read twice")
        if len(seen) >= MAX_IFDS:
            raise Rejected("too many IFDs")
        seen.add(offset)
        count = struct.unpack(e + ("Q" if big else "H"), read(offset, count_size))[0]
        body = read(offset + count_size, count * entry_size + field_size)
        tags, types = {}, {}
        for i in range(count):
            at = i * entry_size
            tag, typ = struct.unpack(e + "HH", body[at : at + 4])
            if tag not in TAGS or tag in tags:
                continue  # unused, or a duplicate (the first is used)
            allowed = SIZES if tag == 270 else (1, 7) if tag == 347 else (5,) if tag in RATIONAL_TAGS else INTEGER_TYPES
            if typ not in allowed:
                raise Rejected(f"tag {tag} has field type {typ}")
            n = struct.unpack(e + ("Q" if big else "I"), body[at + 4 : at + 4 + (8 if big else 4)])[0]
            if tag in SCALARS and n == 0:
                raise Rejected(f"tag {tag} has no value")
            vat = at + 4 + (8 if big else 4)
            if n * SIZES[typ] <= field_size:
                tags[tag] = values(body[vat : vat + n * SIZES[typ]], typ, n)
            else:
                where = struct.unpack(e + ("Q" if big else "I"), body[vat : vat + field_size])[0]
                tags[tag] = values(read(where, n * SIZES[typ]), typ, n)
            if tag == 347:
                tags[tag] = bytes(tags[tag])  # JPEGTables: its bytes
            elif tag in RATIONAL_TAGS:  # (numerator, denominator) pairs
                raw = (body[vat : vat + 8 * n] if 8 * n <= field_size
                       else read(struct.unpack(e + ("Q" if big else "I"), body[vat : vat + field_size])[0], 8 * n))
                v = struct.unpack(e + "I" * (2 * n), raw)
                tags[tag] = [(v[2 * i], v[2 * i + 1]) for i in range(n)]
            types[tag] = typ
        nxt = struct.unpack(e + ("Q" if big else "I"), body[count * entry_size : count * entry_size + field_size])[0]
        return Ifd(offset, tags, types), nxt

    ifds = []
    offset = first
    while offset:
        if offset > MAX_SAFE:
            raise Rejected("IFD offset too large")
        ifd, offset = read_ifd(offset)
        ifds.append(ifd)
    for ifd in ifds:
        subs = ifd.tags.get(330)
        if isinstance(subs, list):
            ifd.sub = [read_ifd(o)[0] for o in subs]
    return e == "<", ifds


# ---- OME-XML (§3.2)

WS = "[ \t\r\n]"
NAME = "[A-Za-z0-9_.-]+"
ATTR = rf"""([^ \t\r\n=/>"'<]+){WS}*={WS}*(?:"([^"]*)"|'([^']*)')"""
SKIP = r"<!--.*?(?:-->|\Z)|<!\[CDATA\[.*?(?:\]\]>|\Z)|<\?.*?(?:\?>|\Z)|<!.*?(?:>|\Z)"
TAG = rf"""<(?P<close>/?)(?:{NAME}:)?(?P<name>{NAME})(?P<attrs>(?:{WS}+[^ \t\r\n=/>"'<]+{WS}*={WS}*(?:"[^"]*"|'[^']*'))*){WS}*(?P<self>/?)>"""
SCAN = re.compile(rf"(?P<skip>{SKIP})|(?P<tag>{TAG})|<", re.S)
ATTRS = re.compile(ATTR)
REFS = re.compile(r"&(?:#x([0-9a-fA-F]+)|#([0-9]+)|(lt|gt|amp|quot|apos));")
DECIMAL = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")


def _decode(v: str) -> str:
    def ref(m):
        if m[3]:
            return {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}[m[3]]
        c = int(m[1], 16) if m[1] else int(m[2])
        if c == 0 or 0xD800 <= c <= 0xDFFF or c > 0x10FFFF:
            return m[0]
        return chr(c)
    return REFS.sub(ref, v)


def scan(xml: str):
    """The tags of `xml` in order, as (start, end, closing, local name,
    attributes, self-closing), and the spans of skipped sections (§3.2)."""
    tags, skipped = [], []
    for m in SCAN.finditer(xml):
        if m["skip"] is not None:
            skipped.append(m.span())
        elif m["tag"] is not None:
            attrs: dict = {}
            for a in ATTRS.finditer(m["attrs"]):
                attrs.setdefault(a[1], _decode(a[2] if a[2] is not None else a[3]))  # first wins
            tags.append((m.start(), m.end(), m["close"] == "/", m["name"], attrs, m["self"] == "/"))
    return tags, skipped


def is_ome(xml: str) -> bool:
    return any(not closing and name == "OME" for _, _, closing, name, _, _ in scan(xml)[0])


def parse_ome(xml: str):
    """(image name, Pixels attributes, TiffData list, Plane attributes or None)
    of the first image (§3.2)."""
    tags, skipped = scan(xml)
    image = next((t for t in tags if not t[2] and t[3] == "Image"), None)
    name = image[4].get("Name") if image else None
    pi = next((i for i, t in enumerate(tags) if not t[2] and t[3] == "Pixels"), None)
    if pi is None:
        return name, {}, [], None
    pixels = tags[pi]
    inside = []
    if not pixels[5]:
        for t in tags[pi + 1 :]:
            if t[2] and t[3] == "Pixels":
                break
            inside.append(t)

    def text(start: int, end: int) -> str:
        pieces, pos = [], start
        for a, b in skipped:
            if b <= start or a >= end:
                continue
            pieces.append(xml[pos:a])
            pos = b
        pieces.append(xml[pos:end])
        return _decode("".join(pieces)).strip(" \t\r\n")

    tiff_data = []
    for i, t in enumerate(inside):
        if t[2] or t[3] != "TiffData":
            continue
        td = {"attrs": t[4], "uuid": None}
        if not t[5]:
            for j in range(i + 1, len(inside)):
                u = inside[j]
                if u[3] == "TiffData":
                    break  # its end tag, or the next TiffData
                if not u[2] and u[3] == "UUID":
                    file_name = u[4].get("FileName")
                    if file_name is not None:
                        td["uuid"] = file_name
                    elif u[5]:
                        td["uuid"] = ""
                    else:
                        td["uuid"] = text(u[1], tags[tags.index(u) + 1][0] if tags.index(u) + 1 < len(tags) else len(xml))
                    break
        tiff_data.append(td)
    plane = next((t[4] for t in inside if not t[2] and t[3] == "Plane"
                  and all(_int(t[4], k, 0) == 0 for k in ("TheZ", "TheC", "TheT"))), None)
    return name, pixels[4], tiff_data, plane


def _int(attrs: dict, key: str, default: int, minimum: int = 0) -> int:
    v = attrs.get(key)
    if v is None:
        return default
    s = v.strip(" \t\r\n")
    if not re.fullmatch(r"[0-9]+", s) or int(s) > MAX_SAFE:
        raise Rejected(f"{key}={v!r} is not an integer")
    if int(s) < minimum:
        raise Rejected(f"{key}={v!r} is less than {minimum}")
    return int(s)


def _physical(attrs: dict, d: str):
    return _decimal(attrs.get(f"PhysicalSize{d}"), positive=True)


def _decimal(v: str | None, positive: bool = False):
    """A decimal value (§3.2), or None."""
    if v is None or not DECIMAL.fullmatch(v):
        return None
    x = float(v)
    return x if math.isfinite(x) and (x > 0 or not positive) else None


def aperio_fields(description: bytes) -> dict | None:
    """The `name = value` fields of an Aperio ImageDescription (§3.6), or None."""
    if not description.startswith(b"Aperio"):
        return None
    try:
        text = description.decode("utf-8")
    except UnicodeDecodeError:
        return None
    fields = {}
    for part in text.split("|"):
        if "=" in part:
            name, value = part.split("=", 1)
            fields.setdefault(name.strip(" \t\r\n"), value.strip(" \t\r\n"))
    return fields


# ---- profile

def jpeg_prefix(ifd: Ifd, spp: int, photometric) -> bytes:
    """The literal start of each JPEG tile's stream (§3.6): SOI, the Adobe
    colour marker for 3 samples, and the IFD's tables."""
    out = b"\xff\xd8"
    if spp == 3:
        out += ADOBE + bytes([0 if photometric == 2 else 1])
    tables = ifd.tags.get(347)
    if tables is not None:
        if len(tables) < 4 or tables[:2] != b"\xff\xd8" or tables[-2:] != b"\xff\xd9":
            raise Rejected(f"the IFD at {ifd.offset} has malformed JPEGTables")
        out += tables[2:-2]
    return out


def fmt(ifd: Ifd):
    bits = ifd.nums(258)
    if not bits or any(b != bits[0] for b in bits) or bits[0] < 1:
        raise Rejected("BitsPerSample values are missing, differ or are 0")
    formats = ifd.tags.get(339, [1])
    if not formats or any(f != formats[0] for f in formats):
        raise Rejected("SampleFormat values are missing or differ")
    spp = ifd.num(277, 1)
    if spp < 1:
        raise Rejected("SamplesPerPixel is 0")
    planar = ifd.num(284, 1) if spp > 1 else 1
    if planar not in (1, 2):
        raise Rejected(f"PlanarConfiguration {planar}")
    compression = ifd.num(259, 1)
    photometric = ifd.tags.get(262, [None])[0] if compression == JPEG else None
    return (bits[0], spp, formats[0], planar, compression, ifd.num(317, 1), photometric)


def tiled(ifd: Ifd) -> bool:
    return 322 in ifd.tags and 324 in ifd.tags


def check_size(ifd: Ifd) -> None:
    """The size checks of §3.1, for planes, levels and level-scan candidates."""
    if ifd.num(256) < 1 or ifd.num(257) < 1:
        raise Rejected(f"the image at {ifd.offset} is empty")
    if tiled(ifd):
        ifd.nums(325)
        if ifd.num(322) < 1 or ifd.num(323) < 1:
            raise Rejected(f"the image at {ifd.offset} has an empty tile size")


def level(ifds: list[Ifd]) -> dict:
    for i in ifds:
        check_size(i)
        fmt(i)
    for i in ifds:
        if not tiled(i):
            raise Rejected(f"only tiled TIFFs are supported; the image at {i.offset} is stored in strips")
    f0 = ifds[0]
    lv = {"w": f0.num(256), "h": f0.num(257), "tw": f0.num(322), "th": f0.num(323), "ifds": ifds}
    for i in ifds:
        if (i.num(256), i.num(257), i.num(322), i.num(323)) != (lv["w"], lv["h"], lv["tw"], lv["th"]) \
                or fmt(i) != fmt(f0):
            raise Rejected("planes of one pyramid level differ in size, tiling or format")
    return lv


def virtualize_tiff(url: str, read: Reader, size: int) -> Output:
    little, ifds = read_tiff(read, size)
    if not ifds:
        raise Rejected("no images")
    ifd0 = ifds[0]
    desc = ifd0.tags.get(270)
    raw = None
    xml = None
    if ifd0.types.get(270) == 2 and isinstance(desc, bytes):
        raw = desc.split(b"\0", 1)[0]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is not None and is_ome(text):
            xml = text
    ome = parse_ome(xml) if xml is not None else None
    bits, spp, sample_format, planar, compression, predictor, photometric = fmt(ifd0)
    if compression not in JPEG2000 and predictor != 1:
        raise Rejected(f"unsupported predictor {predictor}")

    # §3.2, §3.3 planes, in (t, c, z) order, as main-chain IFD indices.
    px = ome[1] if ome else {}
    size_z = _int(px, "SizeZ", 1, 1)
    size_t = _int(px, "SizeT", 1, 1)
    size_c = _int(px, "SizeC", spp, 1)
    order = px.get("DimensionOrder", "XYZCT")
    if ome and (len(order) != 5 or order[:2] != "XY" or sorted(order[2:]) != ["C", "T", "Z"]):
        raise Rejected(f"DimensionOrder {order!r}")
    if spp > 1 and size_c != spp:
        if size_c == 1:
            size_c = spp
        else:
            raise Rejected(f"SizeC {size_c} with {spp} samples per pixel is not supported")
    plane_c = 1 if spp > 1 else size_c

    def plane(t, c, z):
        return (t * plane_c + c) * size_z + z

    if size_t * plane_c * size_z > MAX_PLANES:
        raise Rejected(f"{size_t * plane_c * size_z} planes is more than {MAX_PLANES}")
    plane_ifd = [-1] * (size_t * plane_c * size_z)
    if ome is None:
        plane_ifd[0] = 0
    else:
        sizes = {"Z": size_z, "C": plane_c, "T": size_t}
        entries = ome[2] or [{"attrs": {}, "uuid": None}]
        if len({td["uuid"] for td in entries if td["uuid"] is not None}) > 1:
            raise Rejected("multi-file OME-TIFF is not supported")
        for td in entries:
            a = td["attrs"]
            pos = {"Z": _int(a, "FirstZ", 0), "C": _int(a, "FirstC", 0), "T": _int(a, "FirstT", 0)}
            if any(pos[d] >= sizes[d] for d in "ZCT"):
                raise Rejected("TiffData starts outside the planes")
            ifd = _int(a, "IFD", 0)
            default = len(plane_ifd) if len(entries) == 1 and "IFD" not in a else 1
            count = _int(a, "PlaneCount", default, 1)
            while count > 0:
                count -= 1
                plane_ifd[plane(pos["T"], pos["C"], pos["Z"])] = ifd
                ifd += 1
                for d in order[2:]:
                    pos[d] += 1
                    if pos[d] < sizes[d]:
                        break
                    pos[d] = 0
                else:
                    break
    if any(i < 0 or i >= len(ifds) for i in plane_ifd):
        raise Rejected("OME-XML planes do not match the TIFF's images")
    planes = [ifds[i] for i in plane_ifd]

    # §3.4 levels.
    levels = []
    if ifd0.sub:
        s = len(ifd0.sub)
        for p in planes:
            if len(p.sub) < s:
                raise Rejected(f"the IFD at {p.offset} has fewer SubIFDs than IFD 0")
        for k in range(-1, s):
            levels.append(level([p if k < 0 else p.sub[k] for p in planes]))
    else:
        levels.append(level(planes))
        if ome is None:
            for ifd in ifds[1:]:
                prev = levels[-1]
                if not (tiled(ifd) and 258 in ifd.tags):
                    continue
                check_size(ifd)
                if fmt(ifd) == fmt(ifd0) and ifd.num(256) < prev["w"] and ifd.num(257) < prev["h"]:
                    levels.append(level([ifd]))
    for lv in levels:
        for i in lv["ifds"]:
            if fmt(i) != fmt(ifd0):
                raise Rejected("pyramid levels differ in sample format or compression")

    # §3.5 data type and codecs.
    kind = {1: "uint", 2: "int", 3: "float"}.get(sample_format)
    if kind is None or bits not in (8, 16, 32, 64) or (kind == "float" and bits < 32):
        raise Rejected(f"unsupported sample type: {bits}-bit, SampleFormat {sample_format}")
    data_type = f"{kind}{bits}"
    contig = spp > 1 and planar == 1
    axes = (["t"] if size_t > 1 else []) + (["c"] if size_c > 1 else []) + (["z"] if size_z > 1 else []) + ["y", "x"]
    if compression in JPEG2000:
        codecs = [{"name": "imagecodecs_jpeg2k"}]
    else:
        codecs = [{"name": "bytes", "configuration": {"endian": "little" if little else "big"}}
                  if bits > 8 else {"name": "bytes"}]
        if compression == JPEG:
            if bits != 8 or sample_format != 1 or not (
                    spp == 1 or (spp == 3 and planar == 1 and photometric in (2, 6))):
                raise Rejected(f"unsupported JPEG: {bits}-bit, {spp} samples, planar {planar}, "
                               f"photometric {photometric}")
            codecs = [{"name": "imagecodecs_jpeg"}]
        elif compression in (8, 32946):
            codecs.append({"name": "zlib", "configuration": {"level": 1}})
        elif compression == 50000:
            codecs.append({"name": "zstd", "configuration": {"level": 0, "checksum": False}})
        elif compression != 1:
            raise Rejected(f"unsupported compression {compression}")
    if contig:
        codecs.insert(0, transpose_codec(axes))

    # §3.6 pixel size and position.
    units, sizes, centre, corner = {}, {}, None, None
    if ome is not None:
        for d, a in (("Z", "z"), ("Y", "y"), ("X", "x")):
            if _physical(px, d) is not None:
                sizes[a] = _physical(px, d)
                unit = UNITS.get(px.get(f"PhysicalSize{d}Unit", "µm"))
                if unit:
                    units[a] = unit
        stage = ome[3] or {}  # the OME Plane's attributes
        pos = {a: _decimal(stage.get(f"Position{d}")) for d, a in (("X", "x"), ("Y", "y"))}
        pos_units = {a: UNITS.get(stage.get(f"Position{d}Unit", "")) for d, a in (("X", "x"), ("Y", "y"))}
        if all(pos[a] is not None and pos_units[a] in LENGTHS and units.get(a) in LENGTHS for a in "xy"):
            centre = {a: pos[a] * (LENGTHS[pos_units[a]] / LENGTHS[units[a]]) for a in "xy"}
    else:
        fields = aperio_fields(raw) if raw is not None else None
        mpp = _decimal(fields.get("MPP"), positive=True) if fields else None
        if mpp is not None:
            sizes = {"x": mpp, "y": mpp}
            units = {"x": "micrometer", "y": "micrometer"}
            left, top = _decimal(fields.get("Left")), _decimal(fields.get("Top"))
            if left is not None and top is not None:
                corner = {"x": left * 1000, "y": top * 1000}
        else:
            per_unit = {2: 25400.0, 3: 10000.0}.get(ifd0.num(296, 2))
            for tag, a in ((282, "x"), (283, "y")):
                r = ifd0.tags.get(tag)
                if per_unit is not None and r and r[0][0] > 0 and r[0][1] > 0:
                    sizes[a] = per_unit / (r[0][0] / r[0][1])
                    units[a] = "micrometer"
    out = Output(url)
    base = levels[0]
    scales = []
    for li, lv in enumerate(levels):
        shape = {"t": size_t, "c": size_c, "z": size_z, "y": lv["h"], "x": lv["w"]}
        chunk = {"t": 1, "c": spp if contig else 1, "z": 1, "y": lv["th"], "x": lv["tw"]}
        out.json(f"{li}/zarr.json", array_json([shape[a] for a in axes], data_type,
                                                [chunk[a] for a in axes], codecs, axes))
        across, down = math.ceil(lv["w"] / lv["tw"]), math.ceil(lv["h"] / lv["th"])
        per = across * down
        samples = spp if spp > 1 and not contig else 1
        for t in range(size_t):
            for c in range(plane_c):
                for z in range(size_z):
                    ifd = lv["ifds"][plane(t, c, z)]
                    offsets, counts = ifd.nums(324), ifd.nums(325)
                    prefix = jpeg_prefix(ifd, spp, photometric) if compression == JPEG else None
                    if len(offsets) != samples * per or len(counts) != samples * per:
                        raise Rejected(f"IFD at {ifd.offset} has {len(offsets)} tiles, expected {samples * per}")
                    for s in range(samples):
                        for j in range(per):
                            k = s * per + j
                            if counts[k] == 0:
                                continue
                            if offsets[k] + counts[k] > size:
                                raise Rejected(f"tile {k} of the IFD at {ifd.offset} is outside the file")
                            coords = []
                            if size_t > 1:
                                coords.append(t)
                            if size_c > 1:
                                coords.append((0 if contig else s) if spp > 1 else c)
                            if size_z > 1:
                                coords.append(z)
                            coords += [j // across, j % across]
                            if prefix is None:
                                ranges = [(offsets[k], counts[k])]
                            elif counts[k] > 2:
                                ranges = [prefix, (offsets[k] + 2, counts[k] - 2)]
                            else:
                                raise Rejected(f"JPEG tile {k} of the IFD at {ifd.offset} is too short")
                            if payload_size(ranges) > MAX_PAYLOAD:
                                raise Rejected(f"tile {k}'s reference payload exceeds {MAX_PAYLOAD} bytes")
                            out.refs[f"{li}/c/" + "/".join(map(str, coords))] = ranges
        pxs, pys, pzs = (sizes.get(a, 1) for a in "xyz")
        sc = {"t": 1, "c": 1, "z": pzs, "y": pys * (base["h"] / lv["h"]), "x": pxs * (base["w"] / lv["w"])}
        scales.append([sc[a] for a in axes])
    translation = None
    if "x" in units and "y" in units:
        if centre is not None:
            corner = centred(centre["x"], centre["y"], base["w"], base["h"], sizes.get("x", 1), sizes.get("y", 1))
        if corner is not None:
            translation = [corner.get(a, 0) for a in axes]
    name = ome[0] if ome else None
    out.json("zarr.json", group_json(image_ome(axes, units, scales, name or None,
                                               [translation] * len(levels) if translation else None)))
    if xml is not None:
        out.bytes_entries["OME/METADATA.ome.xml"] = raw
    out.summary = {"axes": axes, "levels": [[{"t": size_t, "c": size_c, "z": size_z, "y": lv["h"], "x": lv["w"]}[a] for a in axes] for lv in levels],
                   "references": len(out.refs)}
    return out
