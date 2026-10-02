"""ND2 profile (VIRTUALIZE.md §4), including the lite-variant decoder (§4.2)."""

from __future__ import annotations

import os
import struct
import sys
import zlib

from vz_common import Reject, Source, array_json, build_codecs, group_json, image_ome

CHUNK_MAGIC = 0x0ABECEDA
SIG_NAME = b"ND2 FILE SIGNATURE CHUNK NAME01!"
MAP_SIG = b"ND2 CHUNK MAP SIGNATURE 0000001!"
FILEMAP_NAME = b"ND2 FILEMAP SIGNATURE NAME 0001!"
_DEBUG = bool(os.environ.get("VZ_DEBUG_LV"))
JP2_SIG = b"\x00\x00\x00\x0cjP  \r\n\x87\n"


# ---------------------------------------------------------------- LV (§4.2)


class _Level:
    """Parsed records of a level: list of (name, value)."""

    __slots__ = ("records",)

    def __init__(self, records):
        self.records = records


def _materialize(records):
    if all(name == "" for name, _ in records):
        return [v for _, v in records]
    d = {}
    for name, v in records:
        d[name] = v  # a repeated name keeps its last value
    return d


def _parse_records(buf: bytes, pos: int, end: int, count: int | None, top: bool):
    """Parse records from pos. If count is None, parse until end. Returns (records, pos)."""
    recs = []
    n = 0
    while (pos < end) if count is None else (n < count):
        if pos + 2 > end:
            raise Reject("LV record header runs past the end")
        typ = buf[pos]
        k = buf[pos + 1]
        if typ == 76:
            if not (top and n == 0):
                raise Reject("LV compressed record not at the start of a chunk's data")
            start = pos + 2 + 10
            if start > end:
                raise Reject("LV compressed record truncated")
            try:
                d = zlib.decompressobj()
                inner = d.decompress(buf[start:end])
                if not d.eof:
                    raise Reject("LV compressed stream is truncated")
            except zlib.error as e:
                raise Reject(f"LV compressed stream: {e}")
            recs, _ = _parse_records(inner, 0, len(inner), None, True)
            return recs, end
        rec_start = pos
        nb = 2 * k
        if pos + 2 + nb > end:
            raise Reject("LV name runs past the end")
        name = buf[pos + 2:pos + 2 + nb].decode("utf-16-le", errors="strict")
        nul = name.find("\0")
        if nul >= 0:
            name = name[:nul]
        pos += 2 + nb

        def need(m):
            if pos + m > end:
                raise Reject("LV value runs past the end")

        if typ == 1:
            need(1); v = buf[pos] != 0; pos += 1
        elif typ == 2:
            need(4); v = struct.unpack_from("<i", buf, pos)[0]; pos += 4
        elif typ == 3:
            need(4); v = struct.unpack_from("<I", buf, pos)[0]; pos += 4
        elif typ == 4:
            need(8); v = struct.unpack_from("<q", buf, pos)[0]; pos += 8
        elif typ == 5:
            need(8); v = struct.unpack_from("<Q", buf, pos)[0]; pos += 8
        elif typ == 6:
            need(8); v = struct.unpack_from("<d", buf, pos)[0]; pos += 8
        elif typ == 7:
            need(8); v = struct.unpack_from("<Q", buf, pos)[0]; pos += 8
        elif typ == 8:
            q = pos
            while True:
                if q + 2 > end:
                    raise Reject("LV string not terminated")
                if buf[q] == 0 and buf[q + 1] == 0:
                    break
                q += 2
            v = buf[pos:q].decode("utf-16-le", errors="surrogatepass")
            pos = q + 2  # the terminating NUL unit is consumed
        elif typ == 9:
            need(8)
            b = struct.unpack_from("<Q", buf, pos)[0]
            pos += 8
            need(b)
            v = list(buf[pos:pos + b])
            pos += b
        elif typ == 11:
            need(12)
            c, L = struct.unpack_from("<IQ", buf, pos)
            pos += 12
            sub, seq_end = _parse_records(buf, pos, end, c, False)
            v = _materialize(sub)
            if _DEBUG and seq_end != rec_start + L:
                print(f'LV: level {name!r} records end at {seq_end - rec_start}, L={L}', file=sys.stderr)
            pos = rec_start + L  # L counts from the start of this record
            if pos > end:
                raise Reject("LV level length runs past the end")
            pos += 8 * c
            if pos > end:
                raise Reject("LV level index runs past the end")
        else:
            raise Reject(f"LV record type {typ}")
        recs.append((name, v))
        n += 1
    return recs, pos


def parse_lv(data: bytes):
    recs, _ = _parse_records(data, 0, len(data), None, True)
    return _materialize(recs)


def members(v):
    if isinstance(v, dict):
        return list(v.values())
    if isinstance(v, list):
        return list(v)
    return []


def get(v, path, default=None):
    for p in path.split("/"):
        if isinstance(v, dict) and p in v:
            v = v[p]
        else:
            return default
    return v


# ---------------------------------------------------------------- file structure (§4.1)


class ND2:
    def __init__(self, src: Source) -> None:
        self.src = src
        h = src.read(0, 16)
        magic, n, d = struct.unpack("<IIQ", h)
        if magic != CHUNK_MAGIC:
            raise Reject("not an ND2 chunk file")
        if n != 32 or d != 64:
            raise Reject("bad signature chunk lengths")
        name = src.read(16, n)
        if name != SIG_NAME:
            raise Reject("bad signature chunk name")
        data = src.read(16 + n, d)
        if not data.startswith(b"Ver"):
            raise Reject("signature data does not start with Ver")
        i = 3
        while i < len(data) and data[i:i + 1].isdigit():
            i += 1
        if i == 3 or data[i:i + 1] != b".":
            raise Reject("bad version string")
        major = int(data[3:i])
        if major < 3:
            raise Reject(f"ND2 version {major} is not supported")
        # chunk map
        if src.size < 40:
            raise Reject("file too small for a chunk map")
        tail = src.read(src.size - 40, 40)
        if tail[:32] != MAP_SIG:
            raise Reject("no chunk map signature at the end of the file")
        (m,) = struct.unpack("<Q", tail[32:])
        mn, md, mdata_off = self.header(m)
        if src.read(m + 16, mn).rstrip(b"\0") != FILEMAP_NAME:
            raise Reject("chunk map does not point at the file map chunk")
        mdata = src.read(mdata_off, md)
        self.map: dict[bytes, int] = {}
        pos = 0
        while True:
            e = mdata.find(b"!", pos)
            if e < 0 or e + 17 > len(mdata):
                raise Reject("chunk map not terminated")
            nm = mdata[pos:e + 1]
            off, _size = struct.unpack_from("<QQ", mdata, e + 1)
            pos = e + 17
            if nm == MAP_SIG:
                break
            self.map[nm] = off

    def header(self, o: int):
        """(name length n, data length d, data offset) of the chunk at o."""
        magic, n, d = struct.unpack("<IIQ", self.src.read(o, 16))
        if magic != CHUNK_MAGIC:
            raise Reject(f"no chunk magic at offset {o}")
        return n, d, o + 16 + n

    def chunk(self, name: bytes):
        if name not in self.map:
            return None
        n, d, do = self.header(self.map[name])
        return self.src.read(do, d)


# ---------------------------------------------------------------- metadata (§4.3)

KIND = {1: "time", 8: "time", 2: "position", 4: "z"}


def _req(d, k, what):
    if not isinstance(d, dict) or k not in d:
        raise Reject(f"{what}: missing {k}")
    return d[k]


def loops_from_experiment(exp):
    """Flatten SLxExperiment into [(kind, count, depth, eType, param)]."""
    out = []

    def visit(node, depth):
        if not isinstance(node, dict):
            raise Reject("experiment node is not an object")
        et = node.get("eType")
        if et not in (1, 2, 4, 6, 8):
            raise Reject(f"experiment loop eType {et!r}")
        lp = node.get("uLoopPars")
        children = members(node.get("ppNextLevelEx"))
        if lp is None:
            return
        param = None
        if et == 1:
            count = _req(lp, "uiCount", "time loop")
            param = lp.get("dPeriod")
        elif et == 8:
            periods = members(_req(lp, "pPeriod", "NE time loop"))
            valid = lp.get("pPeriodValid")
            count = 0
            for i, p in enumerate(periods):
                ok = True if valid is None else (i < len(members(valid)) and members(valid)[i] != 0)
                if ok:
                    count += _req(p, "uiCount", "NE time period")
                    if param is None:
                        param = p.get("dPeriod")
        elif et == 2:
            pts = members(_req(lp, "Points", "position loop"))
            valid = node.get("pItemValid")
            if valid is None:
                count = len(pts)
            else:
                vm = members(valid)
                count = sum(1 for i in range(len(pts)) if i < len(vm) and vm[i] != 0)
        elif et == 4:
            count = _req(lp, "uiCount", "z loop")
            step = abs(lp.get("dZStep", 0.0) or 0.0)
            if step == 0 and count > 1 and "dZHigh" in lp and "dZLow" in lp:
                step = abs(lp["dZHigh"] - lp["dZLow"]) / (count - 1)
            param = step
        else:  # 6 spectral
            if "uiCount" in lp:
                count = lp["uiCount"]
            elif get(lp, "pPlanes/uiCount") is not None:
                count = get(lp, "pPlanes/uiCount")
            else:
                count = 0
        if count == 0:
            return
        if et == 6:
            for ch in children:
                visit(ch, depth)
            return
        loop = (KIND[et], count, depth, et, param)
        if not out or out[-1][2] < depth:
            out.append(loop)
        elif out[-1][2] == depth and out[-1][3] == et and out[-1][1] < count:
            out[-1] = loop
        for ch in children:
            visit(ch, depth + 1)

    if exp is not None:
        visit(exp, 0)
    kinds = [l[0] for l in out]
    if len(set(kinds)) != len(kinds):
        raise Reject(f"two loops of the same kind: {kinds}")
    return out


def color_hex(c: int) -> str:
    r, g, b = c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF
    return f"{r:02X}{g:02X}{b:02X}"


# ---------------------------------------------------------------- virtualize


def virtualize_nd2(src: Source) -> dict:
    f = ND2(src)

    raw = f.chunk(b"ImageAttributesLV!")
    if raw is None:
        raise Reject("no ImageAttributesLV! chunk")
    attrs = parse_lv(raw)
    ia = get(attrs, "SLxImageAttributes")
    if not isinstance(ia, dict):
        raise Reject("no SLxImageAttributes")
    W = _req(ia, "uiWidth", "attributes")
    H = _req(ia, "uiHeight", "attributes")
    WB = _req(ia, "uiWidthBytes", "attributes")
    comp = _req(ia, "uiComp", "attributes")
    bpc = _req(ia, "uiBpcInMemory", "attributes")
    sig = _req(ia, "uiBpcSignificant", "attributes")
    ecomp = ia.get("eCompression", 2)
    tw = ia.get("uiTileWidth", 0)
    th = ia.get("uiTileHeight", 0)
    dtype = {8: "uint8", 16: "uint16", 32: "float32"}.get(bpc)
    if dtype is None:
        raise Reject(f"uiBpcInMemory {bpc}")
    if ecomp == 2:
        compressed = False
    elif ecomp == 0:
        compressed = True
    else:
        raise Reject(f"eCompression {ecomp}")
    if (tw > 0 and tw != W) or (th > 0 and th != H):
        raise Reject(f"tiled ND2 ({tw}x{th} tiles of {W}x{H})")
    if comp < 1 or W < 1 or H < 1:
        raise Reject("empty image")

    raw = f.chunk(b"ImageMetadataLV!")
    exp = get(parse_lv(raw), "SLxExperiment") if raw is not None else None
    loops = loops_from_experiment(exp)

    raw = f.chunk(b"ImageMetadataSeqLV|0!")
    pm = get(parse_lv(raw), "SLxPictureMetadata") if raw is not None else None
    planes = []
    cal = None
    aspect = 1.0
    if isinstance(pm, dict):
        pcount = get(pm, "sPicturePlanes/uiCount", 0) or 0
        pn = get(pm, "sPicturePlanes/sPlaneNew")
        ok = True
        for i in range(pcount):
            p = get(pn, f"a{i}") if isinstance(pn, dict) else None
            if not isinstance(p, dict):
                ok = False
                break
            planes.append(p)
        if not ok:
            planes = None
        if pm.get("bCalibrated") and "dCalibration" in pm:
            cal = pm["dCalibration"]
        aspect = pm.get("dAspect", 1.0)

    # channels (§4.5)
    chans = None
    if planes:
        cc = [p.get("uiCompCount", 1) for p in planes]
        if sum(cc) == comp and all(x in (1, 3) for x in cc):
            chans = []
            for p, n in zip(planes, cc):
                desc = p.get("sDescription", "")
                if n == 1:
                    chans.append((desc, color_hex(p.get("uiColor", 0xFFFFFF))))
                else:
                    for s, col in zip("RGB", ("FF0000", "00FF00", "0000FF")):
                        chans.append((f"{desc} {s}", col))
    if chans is None:
        chans = [(f"C{k}", "FFFFFF") for k in range(comp)]

    # frames (§4.4)
    N = 1
    for l in loops:
        N *= l[1]
    R = W * comp * bpc // 8
    if compressed and WB != R:
        raise Reject("compressed ND2 with padded rows")
    frames = []
    for fi in range(N):
        o = f.map.get(f"ImageDataSeq|{fi}!".encode())
        if o is not None:
            frames.append((fi, o))
    ranges_of = {}
    if frames:
        if compressed:
            for fi, o in frames:
                n, d, do = f.header(o)
                ranges_of[fi] = [[0, do + 8, d - 8]]
        else:
            n0, _, _ = f.header(frames[0][1])
            n1, _, _ = f.header(frames[-1][1])
            if n0 != n1:
                raise Reject("first and last frame headers differ in name length")
            for fi, o in frames:
                start = o + 16 + n0 + 8
                if WB == R:
                    ranges_of[fi] = [[0, start, H * R]]
                else:
                    ranges_of[fi] = [[0, start + r * WB, R] for r in range(H)]

    # output (§4.6)
    tloop = next((l for l in loops if l[0] == "time"), None)
    zloop = next((l for l in loops if l[0] == "z"), None)
    ploop = next((l for l in loops if l[0] == "position"), None)
    npos = ploop[1] if ploop else 1
    axes, shape, chunk, scale = [], [], [], []
    units = {}
    if tloop:
        axes.append("t"); shape.append(tloop[1]); chunk.append(1)
        period = tloop[4]
        if period is not None and period > 0:
            units["t"] = "second"; scale.append(period / 1000)
        else:
            scale.append(1.0)
    if comp > 1:
        axes.append("c"); shape.append(comp); chunk.append(comp); scale.append(1.0)
    if zloop:
        axes.append("z"); shape.append(zloop[1]); chunk.append(1)
        if zloop[4] is not None and zloop[4] > 0:
            units["z"] = "micrometer"; scale.append(zloop[4])
        else:
            scale.append(1.0)
    axes += ["y", "x"]
    shape += [H, W]
    chunk += [H, W]
    if cal is not None:
        units["y"] = units["x"] = "micrometer"
        scale += [cal * aspect, cal]
    else:
        scale += [1.0, 1.0]
    codecs = build_codecs(axes, bpc // 8, "little", comp > 1, False, "zlib" if compressed else None)
    channels = []
    for label, color in chans:
        ch = {"label": label, "color": color, "active": True}
        if dtype != "float32":
            V = 2 ** sig - 1
            ch["window"] = {"min": 0, "max": V, "start": 0, "end": V}
        channels.append(ch)

    entries: dict = {}
    entries["zarr.json"] = {"json": group_json({"ome": {"version": "0.5", "bioformats2raw.layout": 3}})}
    entries["OME/zarr.json"] = {"json": group_json(
        {"ome": {"version": "0.5", "series": [str(p) for p in range(npos)]}})}
    for p in range(npos):
        m = image_ome(f"position {p}", axes, units, [scale], omero={"channels": channels})
        entries[f"{p}/zarr.json"] = {"json": group_json({"ome": m})}
        entries[f"{p}/0/zarr.json"] = {"json": array_json(shape, dtype, chunk, codecs, axes)}
    for fi, rngs in ranges_of.items():
        coord = {}
        r = fi
        for l in reversed(loops):
            coord[l[0]] = r % l[1]
            r //= l[1]
        p = coord.get("position", 0)
        cs = []
        if tloop:
            cs.append(coord["time"])
        if comp > 1:
            cs.append(0)
        if zloop:
            cs.append(coord["z"])
        cs += [0, 0]
        entries[f"{p}/0/c/" + "/".join(map(str, cs))] = {"ranges": rngs}
    return entries
