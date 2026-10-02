"""The TIFF profile of VIRTUALIZE.md (§3)."""

from __future__ import annotations

import html
import math
import re
import struct

from vzip.virtualize.common import (
    UNITS, Output, Reader, Rejected, array_json, group_json, image_ome, transpose_codec,
)

TAGS = {254, 256, 257, 258, 259, 270, 277, 284, 317, 322, 323, 324, 325, 330, 339}
SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}
FORMATS = {1: "B", 6: "b", 7: "B", 3: "H", 8: "h", 4: "I", 13: "I", 9: "i", 16: "Q", 18: "Q", 17: "q",
           11: "f", 12: "d"}
JPEG2000 = {33003, 33004, 33005, 34712}


class Ifd:
    def __init__(self, offset: int, tags: dict) -> None:
        self.offset = offset
        self.tags = tags
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
    if head[:2] not in (b"II", b"MM"):
        raise Rejected("not a TIFF file")
    e = "<" if head[:2] == b"II" else ">"
    magic = struct.unpack(e + "H", head[2:4])[0]
    if magic == 42:
        big, first = False, struct.unpack(e + "I", head[4:8])[0]
    elif magic == 43:
        if struct.unpack(e + "H", head[4:6])[0] != 8:
            raise Rejected("invalid BigTIFF header")
        big, first = True, struct.unpack(e + "Q", head[8:16])[0]
    else:
        raise Rejected("not a TIFF file")
    count_size, entry_size, field_size = (8, 20, 8) if big else (2, 12, 4)
    seen: set[int] = set()

    def values(data: bytes, typ: int, n: int):
        if typ == 2:
            end = data.find(b"\0")
            return (data if end < 0 else data[:end]).decode("utf-8", "replace")
        if typ in (5, 10):
            f = e + ("I" if typ == 5 else "i") * (2 * n)
            v = struct.unpack(f, data[: 8 * n])
            return [v[2 * i] / v[2 * i + 1] for i in range(n)]
        return list(struct.unpack(e + FORMATS[typ] * n, data[: SIZES[typ] * n]))

    def read_ifd(offset: int):
        if offset in seen:
            raise Rejected(f"IFD cycle at {offset}")
        seen.add(offset)
        count = struct.unpack(e + ("Q" if big else "H"), read(offset, count_size))[0]
        if count > 1 << 16:
            raise Rejected(f"IFD with {count} entries")
        body = read(offset + count_size, count * entry_size + field_size)
        tags = {}
        for i in range(count):
            at = i * entry_size
            tag, typ = struct.unpack(e + "HH", body[at : at + 4])
            if tag not in TAGS:
                continue
            if typ not in SIZES:
                raise Rejected(f"tag {tag} has unknown type {typ}")
            n = struct.unpack(e + ("Q" if big else "I"), body[at + 4 : at + 4 + (8 if big else 4)])[0]
            vat = at + 4 + (8 if big else 4)
            if n * SIZES[typ] <= field_size:
                tags[tag] = values(body[vat : vat + n * SIZES[typ]], typ, n)
            else:
                where = struct.unpack(e + ("Q" if big else "I"), body[vat : vat + field_size])[0]
                tags[tag] = values(read(where, n * SIZES[typ]), typ, n)
        nxt = struct.unpack(e + ("Q" if big else "I"), body[count * entry_size : count * entry_size + field_size])[0]
        return Ifd(offset, tags), nxt

    ifds = []
    offset = first
    while offset:
        if len(ifds) >= 100000:
            raise Rejected("too many IFDs")
        ifd, offset = read_ifd(offset)
        ifds.append(ifd)
    for ifd in ifds:
        subs = ifd.tags.get(330)
        if isinstance(subs, list):
            ifd.sub = [read_ifd(o)[0] for o in subs]
    return e == "<", ifds


def _attrs(tag: str) -> dict:
    return {m[1]: html.unescape(m[2]) for m in re.finditer(r'([\w:]+)\s*=\s*"([^"]*)"', tag)}


def parse_ome(xml: str):
    """(image name, Pixels attributes, TiffData attributes) of the first image (§3.2)."""
    image = re.search(r"<(?:\w+:)?Image\b[^>]*>", xml)
    start = re.search(r"<(?:\w+:)?Pixels\b", xml)
    if start is None:
        return None
    end = xml.find("Pixels>", start.start())
    body = xml[start.start() : None if end < 0 else end]
    tag = re.match(r"<(?:\w+:)?Pixels\b[^>]*>", body).group(0)
    tiff_data = []
    for m in re.finditer(r"<(?:\w+:)?TiffData\b[^>]*?(/>|>[\s\S]*?</(?:\w+:)?TiffData>)", body):
        attrs = _attrs(re.match(r"<[^>]*>", m.group(0)).group(0))
        uuid = re.search(r"<(?:\w+:)?UUID\b[^>]*>", m.group(0))
        if uuid and "FileName" in _attrs(uuid.group(0)):
            attrs["FileName"] = _attrs(uuid.group(0))["FileName"]
        tiff_data.append(attrs)
    name = _attrs(image.group(0)).get("Name") if image else None
    return name, _attrs(tag), tiff_data


def fmt(ifd: Ifd):
    bits = ifd.nums(258)
    if any(b != bits[0] for b in bits):
        raise Rejected("samples of different sizes")
    spp = ifd.num(277, 1)
    return (bits[0], spp, ifd.num(339, 1), ifd.num(284, 1) if spp > 1 else 1, ifd.num(259, 1), ifd.num(317, 1))


def tiled(ifd: Ifd) -> bool:
    return 322 in ifd.tags and 324 in ifd.tags


def level(ifds: list[Ifd]) -> dict:
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
    xml = desc if isinstance(desc, str) and "<OME" in desc else None
    ome = parse_ome(xml) if xml is not None else None
    bits, spp, sample_format, planar, compression, predictor = fmt(ifd0)
    if compression not in JPEG2000 and predictor != 1:
        raise Rejected(f"unsupported predictor {predictor}")

    # §3.3 planes, in (t, c, z) order, as main-chain IFD indices.
    px = ome[1] if ome else {}
    size_z, size_t = int(px.get("SizeZ", 1)), int(px.get("SizeT", 1))
    size_c = int(px.get("SizeC", spp))
    if spp > 1 and size_c != spp:
        if size_c == 1:
            size_c = spp
        else:
            raise Rejected(f"SizeC {size_c} with {spp} samples per pixel is not supported")
    plane_c = 1 if spp > 1 else size_c

    def plane(t, c, z):
        return (t * plane_c + c) * size_z + z

    plane_ifd = [-1] * (size_t * plane_c * size_z)
    if ome is None:
        plane_ifd[0] = 0
    else:
        order = px.get("DimensionOrder", "XYZCT")[2:]
        sizes = {"Z": size_z, "C": plane_c, "T": size_t}
        entries = ome[2] or [{}]
        for td in entries:
            if "FileName" in td:
                raise Rejected("multi-file OME-TIFF is not supported")
            pos = {"Z": int(td.get("FirstZ", 0)), "C": int(td.get("FirstC", 0)), "T": int(td.get("FirstT", 0))}
            ifd = int(td.get("IFD", 0))
            default = len(plane_ifd) if len(entries) == 1 and "IFD" not in td else 1
            count = int(td.get("PlaneCount", default))
            while count > 0:
                count -= 1
                plane_ifd[plane(pos["T"], pos["C"], pos["Z"])] = ifd
                ifd += 1
                for d in order:
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
        for k in range(-1, len(ifd0.sub)):
            levels.append(level([p if k < 0 else p.sub[k] for p in planes]))
    else:
        levels.append(level(planes))
        if ome is None:
            for ifd in ifds[1:]:
                prev = levels[-1]
                if tiled(ifd) and fmt(ifd) == fmt(ifd0) and ifd.num(256) < prev["w"] and ifd.num(257) < prev["h"]:
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
        if compression in (8, 32946):
            codecs.append({"name": "zlib", "configuration": {"level": 1}})
        elif compression == 50000:
            codecs.append({"name": "zstd", "configuration": {"level": 0, "checksum": False}})
        elif compression != 1:
            raise Rejected(f"unsupported compression {compression}")
    if contig:
        codecs.insert(0, transpose_codec(axes))

    # §3.6 output.
    def physical(d):
        v = px.get(f"PhysicalSize{d}")
        return None if v is None else float(v)

    units = {}
    for d, a in (("Z", "z"), ("Y", "y"), ("X", "x")):
        if physical(d) is not None:
            unit = UNITS.get(px.get(f"PhysicalSize{d}Unit", "µm"))
            if unit:
                units[a] = unit
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
                    if len(offsets) != samples * per or len(counts) != len(offsets):
                        raise Rejected(f"IFD at {ifd.offset} has {len(offsets)} tiles, expected {samples * per}")
                    for s in range(samples):
                        for j in range(per):
                            k = s * per + j
                            if counts[k] == 0:
                                continue
                            coords = []
                            if size_t > 1:
                                coords.append(t)
                            if size_c > 1:
                                coords.append((0 if contig else s) if spp > 1 else c)
                            if size_z > 1:
                                coords.append(z)
                            coords += [j // across, j % across]
                            out.refs[f"{li}/c/" + "/".join(map(str, coords))] = [(offsets[k], counts[k])]
        sc = {"t": 1.0, "c": 1.0,
              "z": physical("Z") if physical("Z") is not None else 1.0,
              "y": (physical("Y") if physical("Y") is not None else 1.0) * (base["h"] / lv["h"]),
              "x": (physical("X") if physical("X") is not None else 1.0) * (base["w"] / lv["w"])}
        scales.append([sc[a] for a in axes])
    out.json("zarr.json", group_json(image_ome(axes, units, scales, ome[0] if ome else None)))
    if xml is not None:
        out.bytes_entries["OME/METADATA.ome.xml"] = xml.encode()
    out.summary = {"axes": axes, "levels": [[{"t": size_t, "c": size_c, "z": size_z, "y": lv["h"], "x": lv["w"]}[a] for a in axes] for lv in levels],
                   "references": len(out.refs)}
    return out
