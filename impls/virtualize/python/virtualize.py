#!/usr/bin/env python3
"""virtualize <url> <out.json>: VIRTUALIZE.md (profiles version 0, revision 3).

Standard library only. Exit 0 on success, 3 on rejection, 1 on read failure.
"""
import base64
import http.client
import json
import math
import re
import struct
import sys
import urllib.parse
import zlib

MAXI = 2**53 - 1
MAX_PAYLOAD = 65519


class Reject(Exception):
    pass


class Fail(Exception):
    pass


def reject(msg):
    raise Reject(msg)


# ---------------------------------------------------------------------------
# Reading


class Source:
    BLOCK = 1 << 16

    def __init__(self, url):
        u = urllib.parse.urlsplit(url)
        if u.scheme != "http" or not u.hostname:
            raise Fail("only http:// URLs are supported")
        self.host = u.hostname
        self.port = u.port or 80
        self.path = (u.path or "/") + ("?" + u.query if u.query else "")
        self.conn = None
        self.cache = {}
        self.size = self._head()

    def _request(self, method, headers):
        last = None
        for _ in range(3):
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPConnection(self.host, self.port, timeout=120)
                self.conn.request(method, self.path, headers=headers)
                r = self.conn.getresponse()
                body = r.read()
                return r, body
            except (OSError, http.client.HTTPException) as e:
                last = e
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.conn = None
        raise Fail(f"network error: {last}")

    def _head(self):
        r, _ = self._request("HEAD", {})
        if r.status != 200:
            raise Fail(f"HEAD status {r.status}")
        cl = r.getheader("Content-Length")
        if cl is None or not cl.isdigit():
            raise Fail("no Content-Length")
        return int(cl)

    def _get(self, a, n):
        r, body = self._request("GET", {"Range": f"bytes={a}-{a + n - 1}"})
        if r.status != 206 or len(body) != n:
            raise Fail(f"GET {a}+{n}: status {r.status}, {len(body)} bytes")
        return body

    def read(self, off, n):
        if off < 0 or n < 0 or off > MAXI or n > MAXI or off + n > self.size:
            reject(f"read outside the file ({off}+{n} > {self.size})")
        if n == 0:
            return b""
        B = self.BLOCK
        if n > 8 * B:
            return self._get(off, n)
        b0, b1 = off // B, (off + n - 1) // B
        parts = []
        missing = [b for b in range(b0, b1 + 1) if b not in self.cache]
        if missing:
            m0, m1 = missing[0], missing[-1]
            start = m0 * B
            end = min((m1 + 1) * B, self.size)
            data = self._get(start, end - start)
            for b in range(m0, m1 + 1):
                self.cache[b] = data[(b - m0) * B:(b - m0 + 1) * B]
        for b in range(b0, b1 + 1):
            parts.append(self.cache[b])
        buf = b"".join(parts)
        s = off - b0 * B
        return buf[s:s + n]


# ---------------------------------------------------------------------------
# Common output helpers


def varint_len(v):
    n = 1
    while v >= 0x80:
        v >>= 7
        n += 1
    return n


def range_msg_len(o, n):
    k = 0
    if o > 0:
        k += 1 + varint_len(o)
    if n > 0:
        k += 1 + varint_len(n)
    return k


def payload_len(ranges):
    if len(ranges) == 1:
        return range_msg_len(ranges[0][1], ranges[0][2])
    t = 0
    for _, o, n in ranges:
        L = range_msg_len(o, n)
        t += 1 + varint_len(L) + L
    return t


class Out:
    def __init__(self, src):
        self.src = src
        self.entries = {}

    def ref(self, key, ranges):
        for _, o, n in ranges:
            if o < 0 or n < 0 or o + n > self.src.size:
                reject(f"range ({o}, {n}) of {key} lies outside the file")
        if payload_len(ranges) > MAX_PAYLOAD:
            reject(f"payload of {key} exceeds {MAX_PAYLOAD} bytes")
        self.entries[key] = {"ranges": [[0, o, n] for _, o, n in ranges]}

    def json(self, key, value):
        self.entries[key] = {"json": value}

    def raw(self, key, data):
        self.entries[key] = {"base64": base64.b64encode(data).decode("ascii")}


def finite(x):
    if math.isinf(x) or math.isnan(x):
        reject("computed number is infinite or NaN")
    return x


def array_meta(shape, dtype, chunk_shape, codecs, dims):
    return {
        "zarr_format": 3,
        "node_type": "array",
        "shape": shape,
        "data_type": dtype,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": chunk_shape}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": 0,
        "codecs": codecs,
        "dimension_names": dims,
        "attributes": {},
    }


AXIS_TYPE = {"t": "time", "c": "channel", "z": "space", "y": "space", "x": "space"}


def axis_objs(dims, units):
    out = []
    for d in dims:
        a = {"name": d, "type": AXIS_TYPE[d]}
        if units.get(d) is not None:
            a["unit"] = units[d]
        out.append(a)
    return out


def transpose_codec(dims):
    order = [i for i, d in enumerate(dims) if d != "c"] + [dims.index("c")]
    return {"name": "transpose", "configuration": {"order": order}}


UNITS = {
    "µm": "micrometer", "μm": "micrometer", "um": "micrometer",
    "nm": "nanometer", "mm": "millimeter", "cm": "centimeter", "m": "meter",
    "Å": "angstrom", "Å": "angstrom", "pm": "picometer",
    "in": "inch", "ft": "foot", "s": "second", "ms": "millisecond",
    "min": "minute", "h": "hour",
}


# ---------------------------------------------------------------------------
# TIFF profile

TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4,
             12: 8, 13: 4, 16: 8, 17: 8, 18: 8}
UINT_FMT = {1: "B", 3: "H", 4: "I", 13: "I", 16: "Q", 18: "Q"}
USED = {256: "s", 257: "s", 258: "a", 259: "s", 270: "t", 277: "s", 284: "s",
        317: "s", 322: "s", 323: "s", 324: "a", 325: "a", 330: "a", 339: "a"}


class Tag:
    __slots__ = ("type", "count", "inline", "offset")


class IFD:
    def __init__(self, offset):
        self.offset = offset
        self.tags = {}
        self.next = 0


class Tiff:
    def __init__(self, src):
        self.src = src
        h = src.read(0, 4)
        self.e = "<" if h[:2] == b"II" else ">"
        magic = struct.unpack(self.e + "H", h[2:4])[0]
        self.big = magic == 43
        if self.big:
            bs, res, first = struct.unpack(self.e + "HHQ", src.read(4, 12))
            if bs != 8 or res != 0:
                reject("BigTIFF offset size not 8 or reserved word not 0")
            self.minoff = 16
        else:
            first = struct.unpack(self.e + "I", src.read(4, 4))[0]
            self.minoff = 8
        self.first = first
        self.seen = set()
        self.nread = 0

    def u(self, fmt, data):
        return struct.unpack(self.e + fmt, data)

    def check_ifd_offset(self, off):
        if off < self.minoff:
            reject(f"IFD offset {off} below {self.minoff}")
        if off > MAXI:
            reject("IFD offset above 2^53-1")
        if off in self.seen:
            reject(f"IFD offset {off} read twice (cycle or shared SubIFD)")
        self.seen.add(off)
        self.nread += 1
        if self.nread > 100000:
            reject("more than 100000 IFDs")

    def read_ifd(self, off):
        self.check_ifd_offset(off)
        src = self.src
        ifd = IFD(off)
        if self.big:
            n = self.u("Q", src.read(off, 8))[0]
            esz, inl, p = 20, 8, off + 8
        else:
            n = self.u("H", src.read(off, 2))[0]
            esz, inl, p = 12, 4, off + 2
        body = src.read(p, n * esz + inl)
        for i in range(n):
            e = body[i * esz:(i + 1) * esz]
            if self.big:
                tag, typ, cnt = self.u("HHQ", e[:12])
                vf = e[12:20]
            else:
                tag, typ, cnt = self.u("HHI", e[:8])
                vf = e[8:12]
            kind = USED.get(tag)
            if kind is None or tag in ifd.tags:
                continue
            if kind == "t":
                if typ not in TYPE_SIZE:
                    reject(f"tag {tag} has field type {typ}")
            elif typ not in UINT_FMT:
                reject(f"tag {tag} has field type {typ}")
            if kind == "s" and cnt < 1:
                reject(f"scalar tag {tag} has no values")
            t = Tag()
            t.type, t.count = typ, cnt
            nb = cnt * TYPE_SIZE[typ]
            if nb <= inl:
                t.inline, t.offset = vf[:nb], None
            else:
                vo = self.u("Q" if self.big else "I", vf)[0]
                if vo > MAXI or vo + nb > src.size:
                    reject(f"value of tag {tag} lies outside the file")
                t.inline, t.offset = None, vo
            ifd.tags[tag] = t
        ifd.next = self.u("Q" if self.big else "I", body[n * esz:])[0]
        return ifd

    def raw(self, t):
        if t.inline is not None:
            return t.inline
        return self.src.read(t.offset, t.count * TYPE_SIZE[t.type])

    def values(self, ifd, tag):
        t = ifd.tags.get(tag)
        if t is None:
            return None
        f = UINT_FMT[t.type]
        return list(struct.unpack(f"{self.e}{t.count}{f}", self.raw(t)))

    def scalar(self, ifd, tag, default=None):
        t = ifd.tags.get(tag)
        if t is None:
            return default
        sz = TYPE_SIZE[t.type]
        if t.inline is not None:
            b = t.inline[:sz]
        else:
            b = self.src.read(t.offset, sz)
        v = self.u(UINT_FMT[t.type], b)[0]
        if v > MAXI:
            reject(f"tag {tag} value above 2^53-1")
        return v

    def read_all(self):
        main = []
        off = self.first
        while off != 0:
            ifd = self.read_ifd(off)
            main.append(ifd)
            off = ifd.next
        if not main:
            reject("no IFDs")
        for ifd in main:
            subs = self.values(ifd, 330) or []
            ifd.subs = []
            for so in subs:
                ifd.subs.append(self.read_ifd(so))
        self.main = main

    # --- format / size

    def fmt(self, ifd):
        bps = self.values(ifd, 258)
        if not bps:
            reject("BitsPerSample missing or empty")
        if any(b != bps[0] for b in bps) or bps[0] < 1:
            reject("BitsPerSample values differ or are 0")
        spp = self.scalar(ifd, 277, 1)
        if spp < 1:
            reject("SamplesPerPixel 0")
        sf = self.values(ifd, 339)
        if sf is None:
            sfv = 1
        else:
            if not sf:
                reject("SampleFormat with no values")
            if any(v != sf[0] for v in sf):
                reject("SampleFormat values differ")
            sfv = sf[0]
        pc = self.scalar(ifd, 284, 1)
        if spp == 1:
            pc = 1
        elif pc not in (1, 2):
            reject(f"PlanarConfiguration {pc}")
        comp = self.scalar(ifd, 259, 1)
        pred = self.scalar(ifd, 317, 1)
        return (bps[0], spp, sfv, pc, comp, pred)

    def size(self, ifd):
        w = self.scalar(ifd, 256)
        h = self.scalar(ifd, 257)
        if w is None or h is None:
            reject("ImageWidth or ImageLength missing")
        if w < 1 or h < 1:
            reject("ImageWidth or ImageLength 0")
        return w, h

    def tiled(self, ifd):
        return 322 in ifd.tags and 324 in ifd.tags

    def tile_size(self, ifd):
        if not self.tiled(ifd):
            reject("image is not tiled")
        if not all(t in ifd.tags for t in (322, 323, 324, 325)):
            reject("tiled image misses a tile tag")
        tw, tl = self.scalar(ifd, 322), self.scalar(ifd, 323)
        if tw < 1 or tl < 1:
            reject("TileWidth or TileLength 0")
        return tw, tl


# --- OME-XML tag scan

WS = " \t\r\n"
_name = r"[A-Za-z0-9_.\-]+"
_ws = r"[ \t\r\n]"
TAG_RE = re.compile(
    r"<(/?)(?:" + _name + r":)?(" + _name + r")"
    r"((?:" + _ws + r"+[^ \t\r\n=/>\"'<]+" + _ws + r"*=" + _ws + r"*(?:\"[^\"]*\"|'[^']*'))*)"
    + _ws + r"*(/?)>")
ATTR_RE = re.compile(
    _ws + r"+([^ \t\r\n=/>\"'<]+)" + _ws + r"*=" + _ws + r"*(?:\"([^\"]*)\"|'([^']*)')")
REF_RE = re.compile(r"&(lt|gt|amp|quot|apos);|&#([0-9]+);|&#x([0-9A-Fa-f]+);")
ENT = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}


def decode_refs(s):
    def rep(m):
        if m.group(1):
            return ENT[m.group(1)]
        cp = int(m.group(2)) if m.group(2) else int(m.group(3), 16)
        if cp == 0 or 0xD800 <= cp <= 0xDFFF or cp > 0x10FFFF:
            return m.group(0)
        return chr(cp)
    return REF_RE.sub(rep, s)


class XTag:
    __slots__ = ("start", "end", "is_end", "name", "attrs", "selfclosing")


def scan_tags(X):
    tags, skips = [], []
    i, n = 0, len(X)
    while True:
        j = X.find("<", i)
        if j < 0:
            break
        end = None
        for opener, closer in (("<!--", "-->"), ("<![CDATA[", "]]>"), ("<?", "?>"), ("<!", ">")):
            if X.startswith(opener, j):
                e = X.find(closer, j + len(opener))
                end = n if e < 0 else e + len(closer)
                break
        if end is not None:
            skips.append((j, end))
            i = end
            continue
        m = TAG_RE.match(X, j)
        if m:
            t = XTag()
            t.start, t.end = j, m.end()
            t.is_end = m.group(1) == "/"
            t.name = m.group(2)
            t.selfclosing = m.group(4) == "/"
            attrs = {}
            for am in ATTR_RE.finditer(m.group(3)):
                k = am.group(1)
                if k not in attrs:
                    v = am.group(2) if am.group(2) is not None else am.group(3)
                    attrs[k] = decode_refs(v)
            t.attrs = attrs
            tags.append(t)
            i = m.end()
            continue
        i = j + 1
    return tags, skips


INT_RE = re.compile(r"[ \t\r\n]*([0-9]+)[ \t\r\n]*")
FLOAT_RE = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")


def int_attr(attrs, name, minimum=0):
    v = attrs.get(name)
    if v is None:
        return None
    m = INT_RE.fullmatch(v)
    if not m:
        reject(f"attribute {name}={v!r} is not an integer")
    x = int(m.group(1))
    if x > MAXI or x < minimum:
        reject(f"attribute {name}={v!r} out of range")
    return x


def phys_attr(attrs, name):
    v = attrs.get(name)
    if v is None or not FLOAT_RE.fullmatch(v):
        return None
    x = float(v)
    if math.isinf(x) or math.isnan(x) or x <= 0:
        return None
    return x


class OME:
    pass


def parse_ome(X):
    tags, skips = scan_tags(X)
    if not any(t.name == "OME" and not t.is_end for t in tags):
        return None
    o = OME()
    o.image_name = None
    for t in tags:
        if t.name == "Image" and not t.is_end:
            o.image_name = t.attrs.get("Name")
            break
    pi = None
    for idx, t in enumerate(tags):
        if t.name == "Pixels" and not t.is_end:
            pi = idx
            break
    o.pixels = tags[pi].attrs if pi is not None else {}
    o.tiffdata = []  # list of (attrs, uuid identifier or None)
    if pi is not None and not tags[pi].selfclosing:
        stop = len(tags)
        for k in range(pi + 1, len(tags)):
            if tags[k].name == "Pixels" and tags[k].is_end:
                stop = k
                break
        for k in range(pi + 1, stop):
            t = tags[k]
            if t.name != "TiffData" or t.is_end:
                continue
            ident = None
            if not t.selfclosing:
                for q in range(k + 1, len(tags)):
                    u = tags[q]
                    if u.name == "TiffData" or (u.name == "Pixels" and u.is_end):
                        break
                    if u.name == "UUID" and not u.is_end:
                        if "FileName" in u.attrs:
                            ident = u.attrs["FileName"]
                        elif u.selfclosing:
                            ident = ""
                        else:
                            a = u.end
                            b = tags[q + 1].start if q + 1 < len(tags) else len(X)
                            parts, p = [], a
                            for s0, s1 in skips:
                                if s1 <= a or s0 >= b:
                                    continue
                                parts.append(X[p:max(p, s0)])
                                p = max(p, min(s1, b))
                            parts.append(X[p:b])
                            ident = decode_refs("".join(parts)).strip(WS)
                        break
            o.tiffdata.append((t.attrs, ident))
    return o


def tiff_profile(src, url):
    T = Tiff(src)
    T.read_all()
    main = T.main
    ifd0 = main[0]
    fmt0 = T.fmt(ifd0)
    bps, spp, sf, pc, comp, pred = fmt0

    # OME-XML
    D = None
    ome = None
    t270 = ifd0.tags.get(270)
    if t270 is not None and t270.type == 2:
        raw = T.raw(t270)
        nul = raw.find(b"\0")
        cand = raw if nul < 0 else raw[:nul]
        try:
            X = cand.decode("utf-8")
        except UnicodeDecodeError:
            X = None
        if X is not None:
            ome = parse_ome(X)
            if ome is not None:
                D = cand

    # Planes
    if ome is None:
        SZ, SC, ST, Cp = 1, spp, 1, 1
        planes = {(0, 0, 0): 0}
        PX = PY = PZ = None
        units = {}
    else:
        px = ome.pixels
        SZ = int_attr(px, "SizeZ", 1)
        SC = int_attr(px, "SizeC", 1)
        ST = int_attr(px, "SizeT", 1)
        SZ = 1 if SZ is None else SZ
        ST = 1 if ST is None else ST
        SC = spp if SC is None else SC
        order = px.get("DimensionOrder", "XYZCT")
        if not (len(order) == 5 and order[:2] == "XY" and sorted(order[2:]) == ["C", "T", "Z"]):
            reject(f"DimensionOrder {order!r}")
        if spp > 1:
            if SC == 1:
                SC = spp
            elif SC != spp:
                reject(f"SizeC {SC} differs from SamplesPerPixel {spp}")
            Cp = 1
        else:
            Cp = SC
        total = SZ * Cp * ST
        if total > 100000:
            reject(f"{total} planes")
        tds = []
        for attrs, ident in ome.tiffdata:
            td = {
                "IFD": int_attr(attrs, "IFD"),
                "FirstZ": int_attr(attrs, "FirstZ"),
                "FirstC": int_attr(attrs, "FirstC"),
                "FirstT": int_attr(attrs, "FirstT"),
                "PlaneCount": int_attr(attrs, "PlaneCount", 1),
                "ident": ident,
            }
            tds.append(td)
        files = {td["ident"] for td in tds if td["ident"] is not None}
        if len(files) > 1:
            reject("TiffData elements name more than one file")
        if not tds:
            tds = [{"IFD": None, "FirstZ": None, "FirstC": None, "FirstT": None,
                    "PlaneCount": None, "ident": None}]
        sizes = {"Z": SZ, "C": Cp, "T": ST}
        letters = order[2:]
        mapping = {}
        for td in tds:
            fz = td["FirstZ"] or 0
            fc = td["FirstC"] or 0
            ft = td["FirstT"] or 0
            if fz >= SZ or fc >= Cp or ft >= ST:
                reject("TiffData First* out of range")
            ifd_i = td["IFD"] or 0
            pcnt = td["PlaneCount"]
            if pcnt is None:
                pcnt = total if (len(tds) == 1 and td["IFD"] is None) else 1
            first = {"Z": fz, "C": fc, "T": ft}
            # linear index, first letter fastest
            lin, mul = 0, 1
            for L in letters:
                lin += first[L] * mul
                mul *= sizes[L]
            for i in range(min(pcnt, total - lin)):
                p = lin + i
                pos = {}
                for L in letters:
                    pos[L] = p % sizes[L]
                    p //= sizes[L]
                mapping[(pos["Z"], pos["C"], pos["T"])] = ifd_i + i
        planes = {}
        for z in range(SZ):
            for c in range(Cp):
                for t in range(ST):
                    k = mapping.get((z, c, t))
                    if k is None or k >= len(main):
                        reject(f"plane z={z} c={c} t={t} is not mapped to an IFD")
                    planes[(z, c, t)] = k
        PX = phys_attr(px, "PhysicalSizeX")
        PY = phys_attr(px, "PhysicalSizeY")
        PZ = phys_attr(px, "PhysicalSizeZ")
        units = {}
        for ax, P in (("x", PX), ("y", PY), ("z", PZ)):
            if P is not None:
                sym = px.get("PhysicalSize" + ax.upper() + "Unit", "µm")
                units[ax] = UNITS.get(sym)

    # Levels: each level is a dict plane -> IFD
    levels = [{k: main[i] for k, i in planes.items()}]
    s0 = T.values(ifd0, 330) or []
    if s0:
        s = len(s0)
        for k in range(1, s + 1):
            lv = {}
            for key, ifd in levels[0].items():
                if len(ifd.subs) < s:
                    reject("plane IFD has fewer SubIFDs than IFD 0")
                lv[key] = ifd.subs[k - 1]
            levels.append(lv)
    elif ome is None:
        lw, lh = T.size(ifd0)
        for ifd in main[1:]:
            if not (T.tiled(ifd) and 258 in ifd.tags):
                continue
            f = T.fmt(ifd)
            w, h = T.size(ifd)
            T.tile_size(ifd)
            if f == fmt0 and w < lw and h < lh:
                levels.append({(0, 0, 0): ifd})
                lw, lh = w, h

    # Check levels
    linfo = []
    for lv in levels:
        ref = None
        for key, ifd in lv.items():
            f = T.fmt(ifd)
            w, h = T.size(ifd)
            tw, tl = T.tile_size(ifd)
            if f != fmt0:
                reject("level image format differs from IFD 0")
            info = (w, h, tw, tl)
            if ref is None:
                ref = info
            elif info != ref:
                reject("planes of a level differ in size or tile size")
        linfo.append(ref)

    # Data type and codecs
    if sf not in (1, 2, 3):
        reject(f"SampleFormat {sf}")
    if bps not in ((32, 64) if sf == 3 else (8, 16, 32, 64)):
        reject(f"BitsPerSample {bps} for SampleFormat {sf}")
    dtype = {1: "uint", 2: "int", 3: "float"}[sf] + str(bps)
    interleaved = spp > 1 and pc == 1
    planar = spp > 1 and pc == 2
    if comp in (33003, 33004, 33005, 34712):
        a2b, compressor = {"name": "imagecodecs_jpeg2k"}, None
    elif pred != 1:
        reject(f"Predictor {pred}")
    elif comp == 1:
        a2b, compressor = None, None
    elif comp in (8, 32946):
        a2b, compressor = None, {"name": "zlib", "configuration": {"level": 1}}
    elif comp == 50000:
        a2b, compressor = None, {"name": "zstd", "configuration": {"level": 0, "checksum": False}}
    else:
        reject(f"Compression {comp}")
    if a2b is None:
        if bps == 8:
            a2b = {"name": "bytes"}
        else:
            a2b = {"name": "bytes", "configuration": {"endian": "little" if T.e == "<" else "big"}}

    nchan = SC
    dims = []
    if ST > 1:
        dims.append("t")
    if nchan > 1:
        dims.append("c")
    if SZ > 1:
        dims.append("z")
    dims += ["y", "x"]
    codecs = []
    if interleaved:
        codecs.append(transpose_codec(dims))
    codecs.append(a2b)
    if compressor:
        codecs.append(compressor)

    out = Out(src)
    W0, H0 = linfo[0][0], linfo[0][1]
    datasets = []
    nsp = spp if planar else 1
    for L, (lv, (W, H, TW, TL)) in enumerate(zip(levels, linfo)):
        shape, chunks = [], []
        for d in dims:
            if d == "t":
                shape.append(ST); chunks.append(1)
            elif d == "c":
                shape.append(nchan); chunks.append(spp if interleaved else 1)
            elif d == "z":
                shape.append(SZ); chunks.append(1)
        shape += [H, W]
        chunks += [TL, TW]
        out.json(f"{L}/zarr.json", array_meta(shape, dtype, chunks, codecs, dims))
        sy = finite((PY if PY is not None else 1) * finite(H0 / H))
        sx = finite((PX if PX is not None else 1) * finite(W0 / W))
        scale = []
        for d in dims:
            scale.append({"t": 1, "c": 1, "z": PZ if PZ is not None else 1, "y": sy, "x": sx}[d])
        datasets.append({"path": str(L), "coordinateTransformations": [{"type": "scale", "scale": scale}]})
        rows, cols = -(-H // TL), -(-W // TW)
        Tn = rows * cols
        for (z, c, t), ifd in lv.items():
            offs = T.values(ifd, 324)
            cnts = T.values(ifd, 325)
            if len(offs) != Tn * nsp or len(cnts) != Tn * nsp:
                reject("TileOffsets/TileByteCounts count mismatch")
            for sidx in range(nsp):
                for j in range(Tn):
                    k = sidx * Tn + j
                    n = cnts[k]
                    if n == 0:
                        continue
                    o = offs[k]
                    if o > MAXI or n > MAXI:
                        reject("tile offset or length above 2^53-1")
                    coords = []
                    for d in dims:
                        if d == "t":
                            coords.append(t)
                        elif d == "c":
                            coords.append(sidx if planar else (0 if interleaved else c))
                        elif d == "z":
                            coords.append(z)
                    coords += [j // cols, j % cols]
                    out.ref(f"{L}/c/" + "/".join(map(str, coords)), [(0, o, n)])
    ms = {}
    if ome is not None and ome.image_name:
        ms["name"] = ome.image_name
    ms["axes"] = axis_objs(dims, units if ome is not None else {})
    ms["datasets"] = datasets
    out.json("zarr.json", {"zarr_format": 3, "node_type": "group",
                           "attributes": {"ome": {"version": "0.5", "multiscales": [ms]}}})
    if D is not None:
        out.raw("OME/METADATA.ome.xml", D)
    return out


# ---------------------------------------------------------------------------
# ND2 profile

CHUNK_MAGIC = 0x0ABECEDA
MAP_SIG = b"ND2 CHUNK MAP SIGNATURE 0000001!"
FILEMAP_NAME = b"ND2 FILEMAP SIGNATURE NAME 0001!"
FILE_SIG = b"ND2 FILE SIGNATURE CHUNK NAME01!"


def chunk_header(src, o):
    if o > MAXI:
        reject("chunk offset above 2^53-1")
    magic, n, d = struct.unpack("<IIQ", src.read(o, 16))
    if magic != CHUNK_MAGIC:
        reject(f"no chunk magic at {o}")
    if d > MAXI:
        reject("chunk data length above 2^53-1")
    return n, d


def chunk_data(src, o):
    n, d = chunk_header(src, o)
    return src.read(o + 16 + n, d)


# --- LV values: ("s", type, value) | ("o", dict) | ("l", list) | ("b", bytes)


def utf16_units(data, a, b):
    units = struct.unpack(f"<{(b - a) // 2}H", data[a:b])
    out, i = [], 0
    while i < len(units):
        u = units[i]
        if 0xD800 <= u <= 0xDBFF and i + 1 < len(units) and 0xDC00 <= units[i + 1] <= 0xDFFF:
            out.append(chr(0x10000 + ((u - 0xD800) << 10) + (units[i + 1] - 0xDC00)))
            i += 2
            continue
        out.append("�" if 0xD800 <= u <= 0xDFFF else chr(u))
        i += 1
    return "".join(out)


SCALAR_FMT = {2: ("<i", 4), 3: ("<I", 4), 4: ("<q", 8), 5: ("<Q", 8), 6: ("<d", 8), 7: ("<Q", 8)}


def parse_record(data, pos, end):
    if pos + 2 > end:
        reject("LV record truncated")
    typ, k = data[pos], data[pos + 1]
    p = pos + 2
    if p + 2 * k > end:
        reject("LV name truncated")
    nb = data[p:p + 2 * k]
    z = 0
    while z < k and nb[2 * z:2 * z + 2] != b"\0\0":
        z += 1
    name = utf16_units(nb, 0, 2 * z)
    p += 2 * k
    if typ == 1:
        if p + 1 > end:
            reject("LV value truncated")
        return name, ("s", 1, data[p]), p + 1
    if typ in SCALAR_FMT:
        f, sz = SCALAR_FMT[typ]
        if p + sz > end:
            reject("LV value truncated")
        return name, ("s", typ, struct.unpack(f, data[p:p + sz])[0]), p + sz
    if typ == 8:
        q = p
        while True:
            idx = data.find(b"\0\0", q, end)
            if idx < 0:
                reject("LV string without terminator")
            if (idx - p) % 2:
                q = idx + 1
                continue
            break
        return name, ("s", 8, utf16_units(data, p, idx)), idx + 2
    if typ == 9:
        if p + 8 > end:
            reject("LV byte array truncated")
        b = struct.unpack("<Q", data[p:p + 8])[0]
        p += 8
        if p + b > end:
            reject("LV byte array truncated")
        return name, ("b", data[p:p + b]), p + b
    if typ == 11:
        if p + 12 > end:
            reject("LV level truncated")
        c, L = struct.unpack("<IQ", data[p:p + 12])
        p += 12
        recs = []
        for _ in range(c):
            nm, v, p = parse_record(data, p, end)
            recs.append((nm, v))
        if p != pos + L:
            reject("LV level does not end at its length")
        p += 8 * c
        if p > end:
            reject("LV level offset table truncated")
        if recs and all(nm == "" for nm, _ in recs):
            return name, ("l", [v for _, v in recs]), p
        return name, make_object(recs), p
    reject(f"LV record type {typ}")


def make_object(recs):
    d = {}
    for nm, v in recs:
        d[nm] = v
    return ("o", d)


def parse_lv(data):
    if len(data) >= 1 and data[0] == 76:
        if len(data) < 12:
            reject("compressed LV record truncated")
        dec = zlib.decompressobj()
        try:
            inner = dec.decompress(data[12:]) + dec.flush()
        except zlib.error as e:
            reject(f"bad zlib stream in LV: {e}")
        if not dec.eof or dec.unused_data:
            reject("zlib stream does not end exactly at the end of the chunk data")
        data = inner
        if len(data) >= 1 and data[0] == 76:
            reject("nested compressed LV record")
    recs, p = [], 0
    while p < len(data):
        nm, v, p = parse_record(data, p, len(data))
        recs.append((nm, v))
    return make_object(recs)


MISSING = object()


def members(v):
    if v[0] == "o":
        return list(v[1].values())
    if v[0] == "l":
        return v[1]
    if v[0] == "b":
        return [("s", 3, x) for x in v[1]]
    reject("not an object or list")


def conv(v, kind, path):
    if kind in ("number", "integer", "color"):
        if v[0] != "s" or v[1] not in (2, 3, 4, 5, 6):
            reject(f"{path} is not a number")
        x = float(v[2])
        if math.isinf(x) or math.isnan(x):
            reject(f"{path} is not finite")
        if kind == "number":
            return x
        if x != math.floor(x):
            reject(f"{path} is not integral")
        if kind == "integer":
            if x < 0 or x > MAXI:
                reject(f"{path} out of range")
            return int(x)
        if x < -2**31 or x > 2**32 - 1:
            reject(f"{path} out of color range")
        return int(x) % 2**32
    if kind == "flag":
        if v[0] != "s" or v[1] not in (1, 2, 3, 4, 5):
            reject(f"{path} is not a flag")
        return v[2] != 0
    if kind == "string":
        if v[0] != "s" or v[1] != 8:
            reject(f"{path} is not a string")
        return v[2]
    if kind == "object":
        if v[0] != "o":
            reject(f"{path} is not an object")
        return v
    if kind == "list":
        if v[0] not in ("l", "b"):
            reject(f"{path} is not a list")
        return v
    if kind == "objlist":
        if v[0] not in ("o", "l", "b"):
            reject(f"{path} is not an object or list")
        return v
    raise AssertionError(kind)


def get(obj, path, kind, default=MISSING):
    cur = obj
    for step in path.split("/"):
        if cur[0] != "o":
            reject(f"path {path} steps through a non-object")
        if step not in cur[1]:
            if default is MISSING:
                reject(f"required member {path} missing")
            return default
        cur = cur[1][step]
    return conv(cur, kind, path)


def validity(lst):
    if lst is None:
        return None
    return [conv(m, "flag", "validity") for m in members(lst)]


def is_valid(vl, i):
    return vl is None or (i < len(vl) and vl[i])


class Node:
    pass


def node_info(v):
    if v[0] != "o":
        reject("experiment node is not an object")
    nd = Node()
    nd.etype = get(v, "eType", "integer")
    pars = get(v, "uLoopPars", "object", None)
    item_valid = validity(get(v, "pItemValid", "list", None))
    nxt = get(v, "ppNextLevelEx", "objlist", None)
    kids = members(nxt) if nxt is not None else []
    for kd in kids:
        if kd[0] != "o":
            reject("child node is not an object")
    if nd.etype not in (1, 2, 4, 6, 8):
        reject(f"eType {nd.etype}")
    nd.has_pars = pars is not None
    nd.count, nd.param, nd.kind = 0, 0.0, None
    if pars is not None:
        e = nd.etype
        if e == 1:
            nd.kind = "time"
            nd.count = get(pars, "uiCount", "integer", 0)
            nd.param = get(pars, "dPeriod", "number", 0.0)
        elif e == 8:
            nd.kind = "time"
            pp = get(pars, "pPeriod", "objlist", None)
            pms = members(pp) if pp is not None else []
            for m in pms:
                if m[0] != "o":
                    reject("pPeriod member is not an object")
            pv = validity(get(pars, "pPeriodValid", "list", None))
            period = None
            for i, m in enumerate(pms):
                if is_valid(pv, i):
                    nd.count += get(m, "uiCount", "integer")
                    dp = get(m, "dPeriod", "number", 0.0)
                    if period is None:
                        period = dp
            nd.param = period if period is not None else 0.0
        elif e == 2:
            nd.kind = "position"
            pts = get(pars, "Points", "objlist", None)
            pms = members(pts) if pts is not None else []
            nd.count = sum(1 for i in range(len(pms)) if is_valid(item_valid, i))
        elif e == 4:
            nd.kind = "z"
            nd.count = get(pars, "uiCount", "integer", 0)
            st = get(pars, "dZStep", "number", 0.0)
            lo = get(pars, "dZLow", "number", 0.0)
            hi = get(pars, "dZHigh", "number", 0.0)
            step = abs(st)
            if step == 0 and nd.count > 1:
                step = finite(abs(finite(hi - lo)) / (nd.count - 1))
            nd.param = step
        elif e == 6:
            c = get(pars, "uiCount", "integer", None)
            if c is None:
                c = get(pars, "pPlanes/uiCount", "integer", 0)
            nd.count = c
    nd.children = [node_info(k) for k in kids]
    return nd


def flatten(root):
    loops = []

    def visit(nd, depth):
        if not nd.has_pars or nd.count == 0:
            return
        if nd.etype == 6:
            for ch in nd.children:
                visit(ch, depth)
            return
        lp = {"kind": nd.kind, "depth": depth, "count": nd.count, "param": nd.param}
        if not loops or loops[-1]["depth"] < depth:
            loops.append(lp)
        elif loops[-1]["depth"] == depth and loops[-1]["kind"] == nd.kind and loops[-1]["count"] < nd.count:
            loops[-1] = lp
        for ch in nd.children:
            visit(ch, depth + 1)

    visit(root, 0)
    kinds = [l["kind"] for l in loops]
    if len(set(kinds)) != len(kinds):
        reject("two loops of the same kind")
    return loops


FRAME_RE = re.compile(rb"ImageDataSeq\|(0|[1-9][0-9]*)!")


def nd2_profile(src, url):
    size = src.size
    n0, d0 = chunk_header(src, 0)
    if n0 != 32 or d0 != 64 or src.read(16, 32) != FILE_SIG:
        reject("bad signature chunk")
    sig = src.read(48, 64)
    m = re.match(rb"Ver([0-9]+)\.", sig)
    if not m or int(m.group(1)) < 3:
        reject("ND2 version below 3 or unreadable")
    if size < 40:
        reject("file shorter than 40 bytes")
    tail = src.read(size - 40, 40)
    if tail[:32] != MAP_SIG:
        reject("no chunk map signature")
    moff = struct.unpack("<Q", tail[32:])[0]
    mn, md = chunk_header(src, moff)
    mname = src.read(moff + 16, mn)
    if mname.split(b"\0", 1)[0] != FILEMAP_NAME:
        reject("chunk map chunk has the wrong name")
    mdata = src.read(moff + 16 + mn, md)
    cmap = {}
    p = 0
    while True:
        j = mdata.find(b"!", p)
        if j < 0:
            reject("chunk map without terminating record")
        nm = mdata[p:j + 1]
        if nm == MAP_SIG:
            break
        if j + 17 > len(mdata):
            reject("chunk map record truncated")
        cmap[nm] = struct.unpack("<Q", mdata[j + 1:j + 9])[0]
        p = j + 17

    def lv_chunk(name):
        o = cmap.get(name)
        if o is None:
            return None
        return parse_lv(chunk_data(src, o))

    # Attributes
    top = lv_chunk(b"ImageAttributesLV!")
    if top is None:
        reject("no ImageAttributesLV! chunk")
    A = get(top, "SLxImageAttributes", "object")
    W = get(A, "uiWidth", "integer")
    H = get(A, "uiHeight", "integer")
    WB = get(A, "uiWidthBytes", "integer")
    comp = get(A, "uiComp", "integer")
    bpc = get(A, "uiBpcInMemory", "integer")
    sig_bits = get(A, "uiBpcSignificant", "number")
    ecomp = get(A, "eCompression", "integer", 2)
    tw = get(A, "uiTileWidth", "integer", 0)
    th = get(A, "uiTileHeight", "integer", 0)
    if W < 1 or H < 1 or comp < 1:
        reject("uiWidth, uiHeight or uiComp is 0")
    if bpc not in (8, 16, 32):
        reject(f"uiBpcInMemory {bpc}")
    if ecomp not in (0, 2):
        reject(f"eCompression {ecomp}")
    if (tw > 0 and tw != W) or (th > 0 and th != H):
        reject("tiled ND2")
    dtype = {8: "uint8", 16: "uint16", 32: "float32"}[bpc]
    compressed = ecomp == 0

    # Experiment
    loops = []
    top = lv_chunk(b"ImageMetadataLV!")
    if top is not None:
        exp = get(top, "SLxExperiment", "object", None)
        if exp is not None:
            loops = flatten(node_info(exp))

    # Picture metadata
    calibrated, cal, aspect = False, None, 1.0
    plane_list = None  # list of planes (or None entries for missing)
    ucount = 0
    top = lv_chunk(b"ImageMetadataSeqLV|0!")
    if top is not None:
        pm = get(top, "SLxPictureMetadata", "object", None)
        if pm is not None:
            bcal = get(pm, "bCalibrated", "flag", False)
            cal = get(pm, "dCalibration", "number", None)
            asp = get(pm, "dAspect", "number", 1.0)
            calibrated = bcal and cal is not None and cal > 0
            aspect = asp if asp > 0 else 1.0
            sp = get(pm, "sPicturePlanes", "object", None)
            if sp is not None:
                ucount = get(sp, "uiCount", "integer", 0)
                spn = get(sp, "sPlaneNew", "object", None)
                plane_list = {}
                if spn is not None:
                    for key, v in spn[1].items():
                        mm = re.fullmatch(r"a(0|[1-9][0-9]*)", key)
                        if not mm or int(mm.group(1)) >= ucount:
                            continue
                        if v[0] != "o":
                            reject(f"plane {key} is not an object")
                        plane_list[int(mm.group(1))] = (
                            get(v, "sDescription", "string", ""),
                            get(v, "uiColor", "color", 0xFFFFFF),
                            get(v, "uiCompCount", "integer", 1),
                        )

    # Channels
    channels = None
    if ucount >= 1 and plane_list is not None and all(i in plane_list for i in range(ucount)) \
            and all(plane_list[i][2] in (1, 3) for i in range(ucount)) \
            and sum(plane_list[i][2] for i in range(ucount)) == comp:
        channels = []
        for i in range(ucount):
            desc, col, cc = plane_list[i]
            if cc == 1:
                r, g, b = col & 0xFF, (col >> 8) & 0xFF, (col >> 16) & 0xFF
                channels.append((desc, f"{r:02X}{g:02X}{b:02X}"))
            else:
                channels += [(desc + " R", "FF0000"), (desc + " G", "00FF00"), (desc + " B", "0000FF")]
    if channels is None:
        channels = [(f"C{k}", "FFFFFF") for k in range(comp)]

    # Frames
    N = 1
    for lp in loops:
        N *= lp["count"]
    frames = {}
    for nm, o in cmap.items():
        mm = FRAME_RE.fullmatch(nm)
        if mm:
            f = int(mm.group(1))
            if f < N:
                frames[f] = o
    R = W * comp * bpc // 8
    if WB < R:
        reject("uiWidthBytes below the row size")
    frame_ranges = {}  # f -> list of blocks (each a list of ranges)
    h = H
    if compressed:
        if WB != R:
            reject("compressed frames with padded rows")
        for f in sorted(frames):
            o = frames[f]
            n, d = chunk_header(src, o)
            if d <= 8:
                reject(f"compressed frame {f} has no data")
            frame_ranges[f] = [[(0, o + 16 + n + 8, d - 8)]]
    elif frames:
        fl, fh = min(frames), max(frames)
        nl, dl = chunk_header(src, frames[fl])
        nh, dh = chunk_header(src, frames[fh])
        if nl != nh:
            reject("first and last frame name lengths differ")
        need = 8 + H * WB
        if dl < need or dh < need:
            reject("frame data shorter than its pixels")
        starts = {f: o + 16 + nl + 8 for f, o in frames.items()}
        smax = max(starts.values())
        if smax + (H - 1) * WB + R > size:
            reject("frame pixels lie outside the file")
        if WB == R:
            for f, st in starts.items():
                frame_ranges[f] = [[(0, st, H * R)]]
        else:
            h = 1
            divs = set()
            x = 1
            while x * x <= H:
                if H % x == 0:
                    divs.add(x)
                    divs.add(H // x)
                x += 1
            for cand in sorted(divs, reverse=True):
                # every row of a Concat costs at least 6 bytes
                if cand == 1 or cand * 6 > MAX_PAYLOAD:
                    if cand == 1:
                        break
                    continue
                rngs = [(0, smax + r * WB, R) for r in range(H - cand, H)]
                if payload_len(rngs) <= MAX_PAYLOAD:
                    h = cand
                    break
            for f, st in starts.items():
                frame_ranges[f] = [[(0, st + r * WB, R) for r in range(j * h, j * h + h)]
                                   for j in range(H // h)]

    # Output
    out = Out(src)
    kinds = {lp["kind"]: lp for lp in loops}
    npos = kinds["position"]["count"] if "position" in kinds else 1
    out.json("zarr.json", {"zarr_format": 3, "node_type": "group",
                           "attributes": {"ome": {"version": "0.5", "bioformats2raw.layout": 3}}})
    out.json("OME/zarr.json", {"zarr_format": 3, "node_type": "group",
                               "attributes": {"ome": {"version": "0.5",
                                                      "series": [str(p) for p in range(npos)]}}})
    dims, shape, chunks, scale, units = [], [], [], [], {}
    if "time" in kinds:
        dims.append("t"); shape.append(kinds["time"]["count"]); chunks.append(1)
        per = kinds["time"]["param"]
        if per > 0:
            scale.append(finite(per / 1000)); units["t"] = "second"
        else:
            scale.append(1)
    if comp > 1:
        dims.append("c"); shape.append(comp); chunks.append(comp); scale.append(1)
    if "z" in kinds:
        dims.append("z"); shape.append(kinds["z"]["count"]); chunks.append(1)
        st = kinds["z"]["param"]
        if st > 0:
            scale.append(st); units["z"] = "micrometer"
        else:
            scale.append(1)
    dims += ["y", "x"]
    shape += [H, W]
    chunks += [h, W]
    if calibrated:
        scale += [finite(cal * aspect), cal]
        units["y"] = units["x"] = "micrometer"
    else:
        scale += [1, 1]
    codecs = []
    if comp > 1:
        codecs.append(transpose_codec(dims))
    codecs.append({"name": "bytes"} if bpc == 8 else {"name": "bytes", "configuration": {"endian": "little"}})
    if compressed:
        codecs.append({"name": "zlib", "configuration": {"level": 1}})
    if bpc == 32:
        b = None
    elif sig_bits == math.floor(sig_bits) and 1 <= sig_bits <= bpc:
        b = int(sig_bits)
    else:
        b = bpc
    omero_ch = []
    for label, color in channels:
        c = {"label": label, "color": color, "active": True}
        if b is not None:
            V = 2**b - 1
            c["window"] = {"min": 0, "max": V, "start": 0, "end": V}
        omero_ch.append(c)
    meta = array_meta(shape, dtype, chunks, codecs, dims)
    for p in range(npos):
        out.json(f"{p}/zarr.json", {"zarr_format": 3, "node_type": "group", "attributes": {"ome": {
            "version": "0.5",
            "multiscales": [{
                "name": f"position {p}",
                "axes": axis_objs(dims, units),
                "datasets": [{"path": "0", "coordinateTransformations": [{"type": "scale", "scale": scale}]}],
            }],
            "omero": {"channels": omero_ch},
        }}})
        out.json(f"{p}/0/zarr.json", meta)
    for f in sorted(frame_ranges):
        rem = f
        idx = {}
        for lp in reversed(loops):
            idx[lp["kind"]] = rem % lp["count"]
            rem //= lp["count"]
        p = idx.get("position", 0)
        pre = []
        if "t" in dims:
            pre.append(idx["time"])
        if "c" in dims:
            pre.append(0)
        if "z" in dims:
            pre.append(idx["z"])
        for j, rngs in enumerate(frame_ranges[f]):
            out.ref(f"{p}/0/c/" + "/".join(map(str, pre + [j, 0])), rngs)
    return out


# ---------------------------------------------------------------------------


def main(argv):
    if len(argv) != 3:
        print("usage: virtualize <url> <out.json>", file=sys.stderr)
        return 2
    url, outpath = argv[1], argv[2]
    try:
        src = Source(url)
        head = src.read(0, 4) if src.size >= 4 else src.read(0, src.size)
        if head in (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+"):
            out = tiff_profile(src, url)
        elif head == b"\xda\xce\xbe\x0a":
            out = nd2_profile(src, url)
        else:
            reject("not a TIFF or ND2 (v3+) file")
    except Reject as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 3
    except Fail as e:
        print(f"failed: {e}", file=sys.stderr)
        return 1
    doc = {"sources": [url], "entries": out.entries}
    with open(outpath, "w") as fh:
        json.dump(doc, fh, ensure_ascii=False, allow_nan=False)
    print(f"ok: {len(out.entries)} entries")
    return 0


if __name__ == "__main__":
    # LV levels and experiment trees may nest deeply; the specification sets no limit.
    import threading
    sys.setrecursionlimit(1000000)
    threading.stack_size(1 << 29)
    result = []
    th = threading.Thread(target=lambda: result.append(main(sys.argv)))
    th.start()
    th.join()
    sys.exit(result[0] if result else 1)
