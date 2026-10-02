"""TIFF profile (VIRTUALIZE.md §3)."""

from __future__ import annotations

import re
import struct

from vz_common import UNITS, Reject, Source, array_json, build_codecs, group_json, image_ome

# type -> (size, struct code); codes are for one element
TYPES = {
    1: (1, "B"), 2: (1, "B"), 3: (2, "H"), 4: (4, "I"), 5: (8, "II"), 6: (1, "b"),
    7: (1, "B"), 8: (2, "h"), 9: (4, "i"), 10: (8, "ii"), 11: (4, "f"), 12: (8, "d"),
    13: (4, "I"), 16: (8, "Q"), 17: (8, "q"), 18: (8, "Q"),
}

MAX_IFDS = 100000

TAG_WIDTH, TAG_LENGTH, TAG_BPS, TAG_COMPRESSION = 256, 257, 258, 259
TAG_DESCRIPTION, TAG_SPP, TAG_PLANAR, TAG_PREDICTOR = 270, 277, 284, 317
TAG_TILE_W, TAG_TILE_L, TAG_TILE_OFF, TAG_TILE_BC = 322, 323, 324, 325
TAG_SUBIFDS, TAG_SAMPLEFORMAT = 330, 339

JPEG2K = {33003, 33004, 33005, 34712}


class Tiff:
    def __init__(self, src: Source, head: bytes) -> None:
        self.src = src
        if head[:2] == b"II":
            self.bo = "<"
        elif head[:2] == b"MM":
            self.bo = ">"
        else:
            raise Reject("not a TIFF")
        magic = struct.unpack(self.bo + "H", head[2:4])[0]
        if magic == 42:
            self.big = False
            self.first = struct.unpack(self.bo + "I", head[4:8])[0]
        elif magic == 43:
            self.big = True
            osz, zero = struct.unpack(self.bo + "HH", head[4:8])
            if osz != 8 or zero != 0:
                raise Reject("BigTIFF with offset size other than 8")
            self.first = struct.unpack(self.bo + "Q", src.read(8, 8))[0]
        else:
            raise Reject(f"TIFF magic {magic}")
        self.nifds = 0
        self.seen: set[int] = set()

    # ------------------------------------------------------------ IFDs

    def read_ifd(self, off: int):
        """Return ({tag: (type, count, value_bytes_offset or inline bytes)}, next offset)."""
        if off in self.seen:
            raise Reject(f"IFD cycle at offset {off}")
        self.seen.add(off)
        self.nifds += 1
        if self.nifds > MAX_IFDS:
            raise Reject("more than 100000 IFDs")
        bo = self.bo
        if self.big:
            (n,) = struct.unpack(bo + "Q", self.src.read(off, 8))
            esz, base, inl = 20, off + 8, 8
        else:
            (n,) = struct.unpack(bo + "H", self.src.read(off, 2))
            esz, base, inl = 12, off + 2, 4
        raw = self.src.read(base, n * esz + inl)
        tags = {}
        for i in range(n):
            e = raw[i * esz:(i + 1) * esz]
            if self.big:
                tag, typ, cnt = struct.unpack(bo + "HHQ", e[:12])
                vfield = e[12:20]
            else:
                tag, typ, cnt = struct.unpack(bo + "HHI", e[:8])
                vfield = e[8:12]
            tags[tag] = (typ, cnt, vfield)
        nxt_raw = raw[n * esz:]
        (nxt,) = struct.unpack(bo + ("Q" if self.big else "I"), nxt_raw)
        return tags, nxt

    def value_bytes(self, entry) -> bytes:
        typ, cnt, vfield = entry
        if typ not in TYPES:
            raise Reject(f"unknown TIFF field type {typ}")
        size = TYPES[typ][0] * cnt
        inl = 8 if self.big else 4
        if size <= inl:
            return vfield[:size]
        (off,) = struct.unpack(self.bo + ("Q" if self.big else "I"), vfield)
        return self.src.read(off, size)

    def values(self, tags, tag, default=None):
        if tag not in tags:
            return default
        entry = tags[tag]
        typ, cnt, _ = entry
        b = self.value_bytes(entry)
        if typ == 2:
            return b
        code = TYPES[typ][1]
        vals = struct.unpack(self.bo + code * cnt, b)
        if typ in (5, 10):
            vals = [vals[i] / vals[i + 1] if vals[i + 1] else float("nan") for i in range(0, len(vals), 2)]
        return list(vals)

    def scalar(self, tags, tag, default=None):
        v = self.values(tags, tag)
        if v is None:
            return default
        if len(v) == 0:
            raise Reject(f"tag {tag} has no values")
        return v[0]


class Image:
    """The tags of one IFD that §3 uses."""

    def __init__(self, t: Tiff, tags, nxt) -> None:
        self.next = nxt
        self.tags = tags
        self.t = t
        self.width = t.scalar(tags, TAG_WIDTH)
        self.length = t.scalar(tags, TAG_LENGTH)
        bps = t.values(tags, TAG_BPS)
        self.spp = t.scalar(tags, TAG_SPP, 1)
        if bps is not None and len(set(bps)) > 1:
            raise Reject(f"BitsPerSample values differ: {bps}")
        self.bps = bps[0] if bps else None
        sf = t.values(tags, TAG_SAMPLEFORMAT)
        if sf is not None and len(set(sf)) > 1:
            raise Reject(f"SampleFormat values differ: {sf}")
        self.sampleformat = sf[0] if sf else 1
        self.planar = t.scalar(tags, TAG_PLANAR, 1) if self.spp > 1 else 1
        self.compression = t.scalar(tags, TAG_COMPRESSION, 1)
        self.predictor = t.scalar(tags, TAG_PREDICTOR, 1)
        self.tiled = TAG_TILE_W in tags and TAG_TILE_OFF in tags
        self.subifd_offsets = t.values(tags, TAG_SUBIFDS) or []
        self.subifds: list[Image] = []

    @property
    def format(self):
        return (self.bps, self.spp, self.sampleformat, self.planar, self.compression, self.predictor)

    def tile_info(self):
        t = self.t
        tw = t.scalar(self.tags, TAG_TILE_W)
        tl = t.scalar(self.tags, TAG_TILE_L)
        if tw is None or tl is None:
            raise Reject("tiled image without TileWidth/TileLength")
        if TAG_TILE_BC not in self.tags:
            raise Reject("tiled image without TileByteCounts")
        return tw, tl

    def tiles(self):
        offs = self.t.values(self.tags, TAG_TILE_OFF)
        cnts = self.t.values(self.tags, TAG_TILE_BC)
        return offs, cnts

    def description(self):
        if TAG_DESCRIPTION not in self.tags:
            return None
        return self.t.value_bytes(self.tags[TAG_DESCRIPTION])


# ---------------------------------------------------------------- OME-XML (§3.2)

_TOKEN = re.compile(
    r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>|<![^>]*>"
    r"|<(/?)([^\s/>!?]+)((?:\s+[^\s=/>]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))*)\s*(/?)>",
    re.S,
)
_ATTR = re.compile(r"([^\s=/>]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_ENT = re.compile(r"&(lt|gt|amp|quot|apos|#[0-9]+|#x[0-9A-Fa-f]+);")
_ENTS = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}


def _unescape(s: str) -> str:
    def rep(m):
        e = m.group(1)
        if e[0] == "#":
            return chr(int(e[2:], 16) if e[1] in "xX" else int(e[1:]))
        return _ENTS[e]

    return _ENT.sub(rep, s)


def _local(name: str) -> str:
    return name.rsplit(":", 1)[-1]


def parse_ome(x: str):
    """Return (image name or None, Pixels attributes or None, [TiffData dicts])."""
    image_name = None
    seen_image = False
    pixels = None
    pixels_done = False
    depth = 0  # element depth
    pixels_depth = None
    tiffdatas = []
    cur_td = None
    td_depth = None
    for m in _TOKEN.finditer(x):
        if m.group(2) is None:
            continue  # comment, CDATA, PI, declaration
        closing, qname, attrs, selfclose = m.group(1), m.group(2), m.group(3), m.group(4)
        name = _local(qname)
        if closing:
            depth -= 1
            if pixels_depth is not None and depth == pixels_depth:
                pixels_depth = None
                pixels_done = True
            if td_depth is not None and depth == td_depth:
                td_depth = None
                cur_td = None
            continue
        a = {k: _unescape(v1 if v1 is not None and v2 is None else (v2 if v2 is not None else v1))
             for k, v1, v2 in ((mm.group(1), mm.group(2), mm.group(3)) for mm in _ATTR.finditer(attrs or ""))}
        if name == "Image" and not seen_image:
            seen_image = True
            image_name = a.get("Name")
        if name == "Pixels" and pixels is None:
            pixels = a
            if not selfclose:
                pixels_depth = depth
        elif name == "TiffData" and pixels_depth is not None and not pixels_done:
            td = dict(a)
            td["_uuid_filename"] = None
            tiffdatas.append(td)
            if not selfclose:
                cur_td = td
                td_depth = depth
        elif name == "UUID" and cur_td is not None:
            if "FileName" in a:
                cur_td["_uuid_filename"] = a["FileName"]
        if not selfclose:
            depth += 1
    return image_name, pixels, tiffdatas


def _int_attr(d, k, default):
    if k not in d:
        return default
    try:
        return int(d[k].strip())
    except ValueError:
        raise Reject(f"attribute {k}={d[k]!r} is not an integer")


_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def _float_attr(d, k):
    """A decimal attribute, correctly rounded to binary64 (§1.3)."""
    if k not in d:
        return None
    v = d[k].strip()
    if not _DECIMAL.fullmatch(v):
        raise Reject(f"attribute {k}={d[k]!r} is not a decimal number")
    x = float(v)
    if x != x or x in (float("inf"), float("-inf")):
        raise Reject(f"attribute {k}={d[k]!r} overflows binary64")
    return x


# ---------------------------------------------------------------- virtualize


def virtualize_tiff(src: Source, head: bytes) -> dict:
    t = Tiff(src, head)
    # main chain
    main: list[Image] = []
    off = t.first
    while off != 0:
        tags, nxt = t.read_ifd(off)
        main.append(Image(t, tags, nxt))
        off = nxt
    if not main:
        raise Reject("no IFDs")
    for im in main:
        for so in im.subifd_offsets:
            tags, nxt = t.read_ifd(so)
            im.subifds.append(Image(t, tags, nxt))

    first = main[0]
    spp = first.spp
    if first.width is None or first.length is None or first.bps is None:
        raise Reject("first IFD lacks ImageWidth/ImageLength/BitsPerSample")

    desc = first.description()
    xml = None
    if desc is not None:
        raw = desc.split(b"\0", 1)[0]
        if b"<OME" in raw:
            try:
                xml = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise Reject("OME-XML is not valid UTF-8")

    image_name = None
    psize = {"x": None, "y": None, "z": None}
    punit = {"x": "µm", "y": "µm", "z": "µm"}
    if xml is None:
        size_z = size_t = 1
        size_c = spp
        cp = 1
        # one plane: the first IFD at (z, c, t) = (0, 0, 0)
        plane_ifd = {(0, 0, 0): 0}
    else:
        image_name, pix, tds = parse_ome(xml)
        if pix is None:
            pix = {}
        size_z = _int_attr(pix, "SizeZ", 1)
        size_t = _int_attr(pix, "SizeT", 1)
        size_c = _int_attr(pix, "SizeC", spp)
        if spp > 1:
            if size_c == 1:
                size_c = spp
            elif size_c != spp:
                raise Reject(f"SizeC {size_c} differs from SamplesPerPixel {spp}")
            cp = 1
        else:
            cp = size_c
        if size_z < 1 or size_t < 1 or size_c < 1:
            raise Reject("SizeZ/SizeC/SizeT must be positive")
        order = pix.get("DimensionOrder", "XYZCT")
        if len(order) != 5 or order[:2] != "XY" or sorted(order[2:]) != ["C", "T", "Z"]:
            raise Reject(f"bad DimensionOrder {order!r}")
        for k in "XYZ":
            psize[k.lower()] = _float_attr(pix, f"PhysicalSize{k}")
            if f"PhysicalSize{k}Unit" in pix:
                punit[k.lower()] = pix[f"PhysicalSize{k}Unit"]
        sizes = {"Z": size_z, "C": cp, "T": size_t}
        dims = order[2:]
        nplanes = size_z * size_t * cp
        if not tds:
            tds = [{"_uuid_filename": None}]
        plane_ifd = {}
        for td in tds:
            if td["_uuid_filename"] is not None:
                raise Reject("TiffData refers to another file (UUID FileName)")
            ifd0 = _int_attr(td, "IFD", 0)
            if len(tds) == 1 and "IFD" not in td:
                pc_default = nplanes
            else:
                pc_default = 1
            pc = _int_attr(td, "PlaneCount", pc_default)
            pos = {"Z": _int_attr(td, "FirstZ", 0), "C": _int_attr(td, "FirstC", 0),
                   "T": _int_attr(td, "FirstT", 0)}
            for d in "ZCT":
                if not 0 <= pos[d] < sizes[d]:
                    raise Reject(f"TiffData First{d}={pos[d]} out of range")
            if ifd0 < 0 or pc < 0:
                raise Reject("negative TiffData IFD or PlaneCount")
            lin = pos[dims[0]] + sizes[dims[0]] * (pos[dims[1]] + sizes[dims[1]] * pos[dims[2]])
            for i in range(pc):
                li = lin + i
                if li >= nplanes:
                    break  # steps past the last position are ignored
                p = {}
                r = li
                for d in dims:
                    p[d] = r % sizes[d]
                    r //= sizes[d]
                plane_ifd[(p["Z"], p["C"], p["T"])] = ifd0 + i
    # every plane must map to an existing main-chain IFD
    planes = {}
    for tt in range(size_t):
        for c in range(cp):
            for z in range(size_z):
                k = (z, c, tt)
                if k not in plane_ifd:
                    raise Reject(f"plane z={z} c={c} t={tt} is not mapped to an IFD")
                idx = plane_ifd[k]
                if idx >= len(main):
                    raise Reject(f"plane z={z} c={c} t={tt} maps to missing IFD {idx}")
                planes[k] = main[idx]

    # pyramid levels (§3.4): each level maps plane key -> Image
    levels = [planes]
    if first.subifds:
        nsub = len(first.subifds)
        for k in range(1, nsub + 1):
            lv = {}
            for key, im in planes.items():
                if len(im.subifds) < k:
                    raise Reject(f"plane {key} has no SubIFD {k}")
                lv[key] = im.subifds[k - 1]
            levels.append(lv)
    elif xml is None:
        prev = first
        for im in main[1:]:
            if im.tiled and im.format == first.format and im.width is not None and im.length is not None \
                    and im.width < prev.width and im.length < prev.length:
                levels.append({(0, 0, 0): im})
                prev = im

    fmt = first.format
    bps, _, sf, planar, comp, pred = fmt
    level_info = []
    for lv in levels:
        ref = None
        for im in lv.values():
            if not im.tiled:
                raise Reject("image is not tiled")
            if im.format != fmt:
                raise Reject(f"image format {im.format} differs from the first IFD's {fmt}")
            geom = (im.width, im.length) + im.tile_info()
            if ref is None:
                ref = geom
            elif geom != ref:
                raise Reject("planes of a level differ in size or tile size")
        level_info.append(ref)

    # data type and codecs (§3.5)
    kind = {1: "uint", 2: "int", 3: "float"}.get(sf)
    if kind is None:
        raise Reject(f"SampleFormat {sf}")
    if bps not in (8, 16, 32, 64) or (kind == "float" and bps not in (32, 64)):
        raise Reject(f"BitsPerSample {bps} with SampleFormat {sf}")
    dtype = f"{kind}{bps}"
    jpeg2k = False
    compressor = None
    if comp in JPEG2K:
        jpeg2k = True
    elif pred != 1:
        raise Reject(f"Predictor {pred}")
    elif comp == 1:
        pass
    elif comp in (8, 32946):
        compressor = "zlib"
    elif comp == 50000:
        compressor = "zstd"
    else:
        raise Reject(f"Compression {comp}")
    interleaved = spp > 1 and planar == 1
    nsplanes = spp if (spp > 1 and planar != 1) else 1

    # axes (§3.6)
    nchan = size_c if xml is not None else spp
    axes = []
    if size_t > 1:
        axes.append("t")
    if nchan > 1:
        axes.append("c")
    if size_z > 1:
        axes.append("z")
    axes += ["y", "x"]
    units = {}
    for a in "zyx":
        if psize[a] is not None:
            units[a] = UNITS.get(punit[a])
    codecs = build_codecs(axes, bps // 8, "little" if t.bo == "<" else "big", interleaved, jpeg2k, compressor)

    entries: dict = {}
    W0, H0 = level_info[0][0], level_info[0][1]
    scales = []
    for L, (lv, (W, H, TW, TL)) in enumerate(zip(levels, level_info)):
        shape, chunk = [], []
        sc = []
        for a in axes:
            if a == "t":
                shape.append(size_t); chunk.append(1); sc.append(1.0)
            elif a == "c":
                shape.append(nchan); chunk.append(spp if interleaved else 1); sc.append(1.0)
            elif a == "z":
                shape.append(size_z); chunk.append(1)
                sc.append(psize["z"] if psize["z"] is not None else 1.0)
        shape += [H, W]
        chunk += [TL, TW]
        py = psize["y"] if psize["y"] is not None else 1.0
        px = psize["x"] if psize["x"] is not None else 1.0
        sc += [py * (H0 / H), px * (W0 / W)]
        scales.append(sc)
        entries[f"{L}/zarr.json"] = {"json": array_json(shape, dtype, chunk, codecs, axes)}
        across = -(-W // TW)
        down = -(-H // TL)
        T = across * down
        for (z, c, tt), im in lv.items():
            offs, cnts = im.tiles()
            if len(offs) != T * nsplanes or len(cnts) != T * nsplanes:
                raise Reject(f"tile count {len(offs)}/{len(cnts)} is not {T}x{nsplanes}")
            for k in range(T * nsplanes):
                n = cnts[k]
                if n <= 0:
                    continue
                s, j = divmod(k, T)
                row, col = divmod(j, across)
                coords = []
                if "t" in axes:
                    coords.append(tt)
                if "c" in axes:
                    coords.append(0 if interleaved else (s if nsplanes > 1 else c))
                if "z" in axes:
                    coords.append(z)
                coords += [row, col]
                key = f"{L}/c/" + "/".join(str(v) for v in coords)
                entries[key] = {"ranges": [[0, offs[k], n]]}
    entries["zarr.json"] = {"json": group_json({"ome": image_ome(image_name, axes, units, scales)})}
    if xml is not None:
        entries["OME/METADATA.ome.xml"] = {"bytes": xml.encode("utf-8")}
    return entries
