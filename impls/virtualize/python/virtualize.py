"""Virtualize a TIFF or ND2 file as OME-Zarr references (VIRTUALIZE.md, revision 2).

Usage: virtualize <url> <out.json>

Standard library only. Exit 0 on success, 3 on a rejected input, 1 on failure.
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

MAX_SAFE = (1 << 53) - 1
MAX_PAYLOAD = 65519


class Reject(Exception):
    pass


class Fail(Exception):
    pass


def reject(msg):
    raise Reject(msg)


def checkf(x, what="number"):
    if math.isinf(x) or math.isnan(x):
        reject(f"{what} is not finite")
    return x


# --------------------------------------------------------------------------
# HTTP range reader


class Source:
    BLOCK = 1 << 16

    def __init__(self, url):
        self.url = url
        u = urllib.parse.urlsplit(url)
        if u.scheme != "http":
            raise Fail(f"unsupported URL scheme: {u.scheme}")
        self.host = u.hostname
        self.port = u.port or 80
        self.path = u.path + (("?" + u.query) if u.query else "")
        self.conn = None
        self.cache = {}
        st, hdrs, _ = self._request("HEAD", {})
        if st != 200:
            raise Fail(f"HEAD returned {st}")
        cl = hdrs.get("content-length")
        if cl is None or not cl.isdigit():
            raise Fail("no Content-Length")
        self.size = int(cl)

    def _request(self, method, headers):
        last = None
        for _ in range(3):
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPConnection(self.host, self.port, timeout=300)
                self.conn.request(method, self.path, headers=headers)
                r = self.conn.getresponse()
                body = r.read()
                return r.status, {k.lower(): v for k, v in r.getheaders()}, body
            except (OSError, http.client.HTTPException) as e:
                last = e
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.conn = None
        raise Fail(f"network error: {last}")

    def _block(self, i):
        b = self.cache.get(i)
        if b is None:
            start = i * self.BLOCK
            end = min(self.size, start + self.BLOCK)
            st, _, body = self._request("GET", {"Range": f"bytes={start}-{end - 1}"})
            if st != 206 or len(body) != end - start:
                raise Fail(f"GET range returned {st} with {len(body)} bytes")
            b = body
            self.cache[i] = b
        return b

    def read(self, off, n):
        if off < 0 or n < 0 or off > MAX_SAFE or n > MAX_SAFE:
            reject(f"read at {off} of {n} bytes out of range")
        if off + n > self.size:
            reject(f"read at {off} of {n} bytes outside the file (size {self.size})")
        if n == 0:
            return b""
        out = []
        pos = off
        end = off + n
        while pos < end:
            bi = pos // self.BLOCK
            blk = self._block(bi)
            s = pos - bi * self.BLOCK
            e = min(len(blk), end - bi * self.BLOCK)
            out.append(blk[s:e])
            pos = bi * self.BLOCK + e
        return b"".join(out)


# --------------------------------------------------------------------------
# Output helpers


def varint_len(v):
    n = 1
    while v >= 0x80:
        v >>= 7
        n += 1
    return n


def range_enc_len(off, length):
    n = 0
    if off:
        n += 1 + varint_len(off)
    if length:
        n += 1 + varint_len(length)
    return n


def payload_len(ranges):
    if len(ranges) == 1:
        return range_enc_len(ranges[0][1], ranges[0][2])
    t = 0
    for _, o, l in ranges:
        p = range_enc_len(o, l)
        t += 1 + varint_len(p) + p
    return t


class Output:
    def __init__(self, src):
        self.src = src
        self.entries = {}

    def ref(self, key, ranges):
        for _, o, l in ranges:
            if o < 0 or l < 0 or o > MAX_SAFE or l > MAX_SAFE or o + l > self.src.size:
                reject(f"range ({o}, {l}) of {key} outside the file")
        if payload_len(ranges) > MAX_PAYLOAD:
            reject(f"reference payload of {key} exceeds {MAX_PAYLOAD} bytes")
        self.entries[key] = {"ranges": [list(r) for r in ranges]}

    def json(self, key, value):
        self.entries[key] = {"json": value}

    def raw(self, key, data):
        self.entries[key] = {"base64": base64.b64encode(data).decode("ascii")}

    def dump(self, path):
        doc = {"sources": [self.src.url], "entries": self.entries}
        with open(path, "w") as f:
            json.dump(doc, f, allow_nan=False, sort_keys=True)


def num(x):
    """Write integral floats as integers (equal as binary64 either way)."""
    if isinstance(x, float) and x.is_integer() and abs(x) <= MAX_SAFE:
        return int(x)
    return x


def array_meta(shape, dtype, chunks, codecs, dims):
    return {
        "zarr_format": 3,
        "node_type": "array",
        "shape": shape,
        "data_type": dtype,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": chunks}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": 0,
        "codecs": codecs,
        "dimension_names": dims,
        "attributes": {},
    }


AXIS_TYPE = {"t": "time", "c": "channel", "z": "space", "y": "space", "x": "space"}


def image_meta(name, axes, units, scales_per_level, omero=None):
    ax = []
    for a in axes:
        d = {"name": a, "type": AXIS_TYPE[a]}
        if units.get(a):
            d["unit"] = units[a]
        ax.append(d)
    ms = {}
    if name is not None:
        ms["name"] = name
    ms["axes"] = ax
    ms["datasets"] = [
        {"path": str(i), "coordinateTransformations": [{"type": "scale", "scale": [num(v) for v in sc]}]}
        for i, sc in enumerate(scales_per_level)
    ]
    m = {"version": "0.5", "multiscales": [ms]}
    if omero is not None:
        m["omero"] = omero
    return {"zarr_format": 3, "node_type": "group", "attributes": {"ome": m}}


def transpose_codec(axes):
    order = [i for i, a in enumerate(axes) if a != "c"] + [axes.index("c")]
    return {"name": "transpose", "configuration": {"order": order}}


def bytes_codec(itemsize, endian):
    if itemsize == 1:
        return {"name": "bytes"}
    return {"name": "bytes", "configuration": {"endian": endian}}


UNITS = {
    "µm": "micrometer", "μm": "micrometer", "um": "micrometer",
    "nm": "nanometer", "mm": "millimeter", "cm": "centimeter", "m": "meter",
    "Å": "angstrom", "Å": "angstrom", "pm": "picometer",
    "in": "inch", "ft": "foot", "s": "second", "ms": "millisecond",
    "min": "minute", "h": "hour",
}


# --------------------------------------------------------------------------
# TIFF

TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8,
             11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}
INT_TYPES = {1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i", 13: "I", 16: "Q", 17: "q", 18: "Q"}


class IFD:
    def __init__(self, tiff, offset, entries, sub_offsets=None):
        self.tiff = tiff
        self.offset = offset
        self.entries = entries  # tag -> (type, count, value_field_bytes) (first occurrence)
        self._cache = {}

    def has(self, tag):
        return tag in self.entries

    def raw(self, tag):
        """(type, bytes) of the tag's value."""
        typ, count, field = self.entries[tag]
        if typ not in TYPE_SIZE:
            reject(f"tag {tag} has unknown field type {typ}")
        nbytes = TYPE_SIZE[typ] * count
        t = self.tiff
        if nbytes <= t.inline:
            data = field[:nbytes]
        else:
            off = struct.unpack(t.bo + ("Q" if t.big else "I"), field)[0]
            data = t.src.read(off, nbytes)
        return typ, count, data

    def ints(self, tag):
        if tag in self._cache:
            return self._cache[tag]
        typ, count, data = self.raw(tag)
        if typ not in INT_TYPES:
            reject(f"tag {tag} has non-integer field type {typ}")
        vals = list(struct.unpack(self.tiff.bo + INT_TYPES[typ] * count, data))
        for v in vals:
            if v < 0 or v > MAX_SAFE:
                reject(f"tag {tag} value {v} out of range")
        self._cache[tag] = vals
        return vals

    def int1(self, tag, default=None):
        if tag not in self.entries:
            if default is None:
                reject(f"IFD at {self.offset}: required tag {tag} missing")
            return default
        v = self.ints(tag)
        if not v:
            reject(f"IFD at {self.offset}: tag {tag} has no values")
        return v[0]

    def all_equal(self, tag, default):
        if tag not in self.entries:
            return default
        v = self.ints(tag)
        if not v:
            reject(f"IFD at {self.offset}: tag {tag} has no values")
        if any(x != v[0] for x in v):
            reject(f"IFD at {self.offset}: tag {tag} values differ")
        return v[0]

    def spp(self):
        return self.int1(277, 1)

    def tiled(self):
        return self.has(322) and self.has(324)

    def format(self, check_equal=True):
        """(bps, spp, sf, pc, comp, pred)."""
        if check_equal:
            bps = self.all_equal(258, None)
            if bps is None:
                reject(f"IFD at {self.offset}: BitsPerSample missing")
            sf = self.all_equal(339, 1)
        else:
            bps = self.int1(258)
            sf = self.int1(339, 1)
        spp = self.spp()
        if spp > 1:
            pc = self.int1(284, 1)
            if pc not in (1, 2):
                reject(f"IFD at {self.offset}: PlanarConfiguration {pc}")
        else:
            pc = 1
        comp = self.int1(259, 1)
        pred = self.int1(317, 1)
        return (bps, spp, sf, pc, comp, pred)

    def subifds(self):
        if 330 not in self.entries:
            return []
        return self.ints(330)


class Tiff:
    def __init__(self, src):
        self.src = src
        h = src.read(0, 8)
        self.bo = "<" if h[:2] == b"II" else ">"
        magic = struct.unpack(self.bo + "H", h[2:4])[0]
        if magic == 42:
            self.big = False
            self.inline = 4
            first = struct.unpack(self.bo + "I", h[4:8])[0]
        else:
            self.big = True
            self.inline = 8
            osz, res = struct.unpack(self.bo + "HH", h[4:8])
            if osz != 8 or res != 0:
                reject("BigTIFF header: bad offset size or reserved word")
            first = struct.unpack(self.bo + "Q", src.read(8, 8))[0]
        self.seen = set()
        self.nread = 0
        self.main = []
        off = first
        while off != 0:
            ifd, nxt = self.read_ifd(off)
            self.main.append(ifd)
            off = nxt
        if not self.main:
            reject("no IFDs")
        # SubIFDs of every main-chain IFD
        self.sub = []
        for ifd in self.main:
            subs = []
            for so in ifd.subifds():
                s, _ = self.read_ifd(so)
                subs.append(s)
            self.sub.append(subs)

    def read_ifd(self, off):
        if off in self.seen:
            reject(f"IFD cycle at offset {off}")
        self.seen.add(off)
        self.nread += 1
        if self.nread > 100000:
            reject("more than 100000 IFDs")
        if off > MAX_SAFE:
            reject("IFD offset out of range")
        if self.big:
            n = struct.unpack(self.bo + "Q", self.src.read(off, 8))[0]
            esz, base = 20, off + 8
            if n > MAX_SAFE // 20:
                reject("IFD entry count out of range")
        else:
            n = struct.unpack(self.bo + "H", self.src.read(off, 2))[0]
            esz, base = 12, off + 2
        osz = 8 if self.big else 4
        data = self.src.read(base, n * esz + osz)
        entries = {}
        for i in range(n):
            e = data[i * esz:(i + 1) * esz]
            if self.big:
                tag, typ, count = struct.unpack(self.bo + "HHQ", e[:12])
                field = e[12:20]
            else:
                tag, typ, count = struct.unpack(self.bo + "HHI", e[:8])
                field = e[8:12]
            if tag not in entries:
                entries[tag] = (typ, count, field)
        nxt = struct.unpack(self.bo + ("Q" if self.big else "I"), data[n * esz:])[0]
        return IFD(self, off, entries), nxt


# ---- OME-XML tag scanner

XML_WS = " \t\r\n"
NAME_END = re.compile(r"[ \t\r\n/>]")


def decode_entities(s):
    def rep(m):
        body = m.group(1)
        named = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}
        if body in named:
            return named[body]
        try:
            if body.startswith("#x"):
                cp = int(body[2:], 16)
            elif body.startswith("#"):
                cp = int(body[1:], 10)
            else:
                return m.group(0)
        except ValueError:
            return m.group(0)
        if cp == 0 or cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
            return m.group(0)
        return chr(cp)

    return re.sub(r"&(#x[0-9A-Fa-f]+|#[0-9]+|[A-Za-z_][A-Za-z0-9._-]*);", rep, s)


def local(name):
    return name.rpartition(":")[2]


ATTR_RE = re.compile(r"""([^\s=/>"']+)\s*=\s*("([^"]*)"|'([^']*)')""")


class Tag:
    __slots__ = ("kind", "name", "attrs", "selfclose", "start", "end")


def scan_tags(x):
    """Yield Tag objects ('start' or 'end') in document order."""
    i = 0
    n = len(x)
    tags = []
    while True:
        i = x.find("<", i)
        if i < 0:
            break
        if x.startswith("<!--", i):
            j = x.find("-->", i + 4)
            i = n if j < 0 else j + 3
            continue
        if x.startswith("<![CDATA[", i):
            j = x.find("]]>", i + 9)
            i = n if j < 0 else j + 3
            continue
        if x.startswith("<?", i):
            j = x.find("?>", i + 2)
            i = n if j < 0 else j + 2
            continue
        if x.startswith("<!", i):
            j = x.find(">", i + 2)
            i = n if j < 0 else j + 1
            continue
        if x.startswith("</", i):
            m = NAME_END.search(x, i + 2)
            nend = n if m is None else m.start()
            j = x.find(">", i + 2)
            t = Tag()
            t.kind = "end"
            t.name = local(x[i + 2:nend])
            t.attrs = {}
            t.selfclose = False
            t.start = i
            t.end = n if j < 0 else j + 1
            tags.append(t)
            i = t.end
            continue
        # start tag: find the closing '>' outside quotes
        j = i + 1
        q = None
        while j < n:
            ch = x[j]
            if q:
                if ch == q:
                    q = None
            elif ch in "\"'":
                q = ch
            elif ch == ">":
                break
            j += 1
        body = x[i + 1:j]
        m = NAME_END.search(body)
        nend = len(body) if m is None else m.start()
        t = Tag()
        t.kind = "start"
        t.name = local(body[:nend])
        t.selfclose = body.endswith("/")
        attrs = {}
        for am in ATTR_RE.finditer(body, nend):
            k = am.group(1)
            v = am.group(3) if am.group(3) is not None else am.group(4)
            if k not in attrs:
                attrs[k] = decode_entities(v)
        t.attrs = attrs
        t.start = i
        t.end = min(n, j + 1)
        tags.append(t)
        i = t.end
    return tags


OME_START = re.compile(r"<([^\s<>/:!?=\"']+:)?OME[ \t\r\n/>]")
INT_RE = re.compile(r"^[ \t\r\n]*([0-9]+)[ \t\r\n]*$")
FLOAT_RE = re.compile(r"^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$")


def ome_int(attrs, name, minimum, default):
    if name not in attrs:
        return default
    m = INT_RE.match(attrs[name])
    if not m:
        reject(f"OME-XML {name}={attrs[name]!r} is not an integer")
    v = int(m.group(1))
    if v < minimum:
        reject(f"OME-XML {name}={v} below {minimum}")
    return v


def ome_float(attrs, name):
    v = attrs.get(name)
    if v is None or not FLOAT_RE.match(v):
        return None
    f = float(v)
    if math.isinf(f) or math.isnan(f) or f <= 0:
        return None
    return f


def parse_ome(x):
    tags = scan_tags(x)
    ome = {"image_name": None, "pixels": None, "tiffdata": []}
    for t in tags:
        if t.kind == "start" and t.name == "Image":
            ome["image_name"] = t.attrs.get("Name")
            break
    pi = None
    for k, t in enumerate(tags):
        if t.kind == "start" and t.name == "Pixels":
            pi = k
            break
    if pi is None:
        return ome
    ome["pixels"] = tags[pi].attrs
    if tags[pi].selfclose:
        return ome
    pend = len(tags)
    for k in range(pi + 1, len(tags)):
        if tags[k].kind == "end" and tags[k].name == "Pixels":
            pend = k
            break
    k = pi + 1
    while k < pend:
        t = tags[k]
        if t.kind == "start" and t.name == "TiffData":
            td = {"attrs": t.attrs, "uuid": None}
            if not t.selfclose:
                tend = pend
                for k2 in range(k + 1, pend):
                    if tags[k2].kind == "end" and tags[k2].name == "TiffData":
                        tend = k2
                        break
                for k2 in range(k + 1, tend):
                    u = tags[k2]
                    if u.kind == "start" and u.name == "UUID":
                        if u.selfclose:
                            text = ""
                        else:
                            nxt = x.find("<", u.end)
                            text = x[u.end:(len(x) if nxt < 0 else nxt)]
                        td["uuid"] = (u.attrs.get("FileName"), decode_entities(text).strip(XML_WS))
                        break
            ome["tiffdata"].append(td)
        k += 1
    return ome


def virtualize_tiff(src, out):
    tiff = Tiff(src)
    ifd0 = tiff.main[0]
    spp = ifd0.spp()
    fmt0 = ifd0.format()

    # ---- OME-XML
    D = None
    ome = None
    if ifd0.has(270):
        typ, count, data = ifd0.raw(270)
        if typ == 2:
            d = data.split(b"\x00", 1)[0]
            try:
                text = d.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None and OME_START.search(text):
                D = d
                ome = parse_ome(text)

    # ---- planes
    if ome is None:
        SizeZ = SizeT = 1
        SizeC = spp
        Cp = 1
        planes = {(0, 0, 0): 0}
        image_name = None
        PX = PY = PZ = None
        units = {}
    else:
        px = ome["pixels"] if ome["pixels"] is not None else {}
        SizeZ = ome_int(px, "SizeZ", 1, 1)
        SizeT = ome_int(px, "SizeT", 1, 1)
        SizeC = ome_int(px, "SizeC", 1, spp)
        if spp > 1:
            if SizeC == 1:
                SizeC = spp
            elif SizeC != spp:
                reject(f"SizeC {SizeC} != SamplesPerPixel {spp}")
        Cp = 1 if spp > 1 else SizeC
        order = px.get("DimensionOrder", "XYZCT")
        if order[:2] != "XY" or sorted(order[2:]) != ["C", "T", "Z"] or len(order) != 5:
            reject(f"DimensionOrder {order!r}")
        P = SizeZ * Cp * SizeT
        tds = ome["tiffdata"] or [{"attrs": {}, "uuid": None}]
        files = set()
        for td in tds:
            if td["uuid"] is not None:
                fn, text = td["uuid"]
                files.add(("f", fn) if fn is not None else ("u", text))
        if len(files) > 1:
            reject("multi-file dataset")
        sizes = {"Z": SizeZ, "C": Cp, "T": SizeT}
        letters = order[2:]
        parsed = []
        bound = 0
        for td in tds:
            a = td["attrs"]
            fz = ome_int(a, "FirstZ", 0, 0)
            fc = ome_int(a, "FirstC", 0, 0)
            ft = ome_int(a, "FirstT", 0, 0)
            if fz >= SizeZ or fc >= Cp or ft >= SizeT:
                reject("TiffData First* out of range")
            ifd = ome_int(a, "IFD", 0, 0)
            if "PlaneCount" in a:
                pc = ome_int(a, "PlaneCount", 1, 1)
            elif len(tds) == 1 and "IFD" not in a:
                pc = P
            else:
                pc = 1
            parsed.append((fz, fc, ft, ifd, pc))
            bound += min(pc, P)
        if bound < P:
            reject("not every plane is mapped")
        mapping = {}
        for fz, fc, ft, ifd, pc in parsed:
            pos = {"Z": fz, "C": fc, "T": ft}
            lin = 0
            mul = 1
            for L in letters:
                lin += pos[L] * mul
                mul *= sizes[L]
            for i in range(min(pc, P - lin)):
                li = lin + i
                p = {}
                for L in letters:
                    p[L] = li % sizes[L]
                    li //= sizes[L]
                mapping[(p["Z"], p["C"], p["T"])] = ifd + i
        if len(mapping) != P:
            reject("not every plane is mapped")
        for k, v in mapping.items():
            if v >= len(tiff.main):
                reject(f"plane {k} mapped to missing IFD {v}")
        planes = mapping
        image_name = ome["image_name"]
        PX = ome_float(px, "PhysicalSizeX")
        PY = ome_float(px, "PhysicalSizeY")
        PZ = ome_float(px, "PhysicalSizeZ")
        units = {}
        for ax, val in (("x", PX), ("y", PY), ("z", PZ)):
            if val is not None:
                units[ax] = UNITS.get(px.get(f"PhysicalSize{ax.upper()}Unit", "µm"))

    # ---- levels: list of {plane: IFD}
    plane_keys = sorted(planes)
    levels = [{k: tiff.main[planes[k]] for k in plane_keys}]
    main_index = {id(ifd): i for i, ifd in enumerate(tiff.main)}
    subs0 = tiff.sub[0]
    if subs0:
        s = len(subs0)
        for k in range(1, s + 1):
            lv = {}
            for pk in plane_keys:
                subs = tiff.sub[planes[pk]]
                if len(subs) < s:
                    reject("plane IFD has fewer SubIFDs than IFD 0")
                lv[pk] = subs[k - 1]
            levels.append(lv)
    elif ome is None:
        last = ifd0
        lw, ll = ifd0.int1(256), ifd0.int1(257)
        for ifd in tiff.main[1:]:
            if not (ifd.tiled() and ifd.has(258)):
                continue
            if not (ifd.has(256) and ifd.has(257)):
                continue
            if ifd.format(check_equal=False) != fmt0:
                continue
            w, l = ifd.int1(256), ifd.int1(257)
            if w < lw and l < ll:
                levels.append({(0, 0, 0): ifd})
                lw, ll = w, l

    # ---- validate levels
    lvinfo = []
    for lv in levels:
        geo = None
        for pk in plane_keys:
            ifd = lv[pk]
            if not ifd.tiled():
                reject(f"IFD at {ifd.offset} is not tiled")
            f = ifd.format()
            if f != fmt0:
                reject(f"IFD at {ifd.offset} format differs from IFD 0")
            W, H = ifd.int1(256), ifd.int1(257)
            TW, TL = ifd.int1(322), ifd.int1(323)
            if not ifd.has(325):
                reject("TileByteCounts missing")
            if W < 1 or H < 1 or TW < 1 or TL < 1:
                reject("zero image or tile size")
            g = (W, H, TW, TL)
            if geo is None:
                geo = g
            elif g != geo:
                reject("planes of a level differ in size or tiling")
        lvinfo.append(geo)

    bps, _, sf, pcfg, comp, pred = fmt0
    if sf not in (1, 2, 3):
        reject(f"SampleFormat {sf}")
    if bps not in (8, 16, 32, 64) or (sf == 3 and bps not in (32, 64)):
        reject(f"BitsPerSample {bps} for SampleFormat {sf}")
    dtype = {1: "uint", 2: "int", 3: "float"}[sf] + str(bps)
    interleaved = spp > 1 and pcfg == 1
    endian = "little" if tiff.bo == "<" else "big"

    # ---- axes
    nchan = SizeC
    axes = []
    if SizeT > 1:
        axes.append("t")
    if nchan > 1:
        axes.append("c")
    if SizeZ > 1:
        axes.append("z")
    axes += ["y", "x"]

    codecs = []
    if interleaved:
        codecs.append(transpose_codec(axes))
    if comp in (33003, 33004, 33005, 34712):
        codecs.append({"name": "imagecodecs_jpeg2k"})
    elif pred != 1:
        reject(f"Predictor {pred} with Compression {comp}")
    elif comp == 1:
        codecs.append(bytes_codec(bps // 8, endian))
    elif comp in (8, 32946):
        codecs.append(bytes_codec(bps // 8, endian))
        codecs.append({"name": "zlib", "configuration": {"level": 1}})
    elif comp == 50000:
        codecs.append(bytes_codec(bps // 8, endian))
        codecs.append({"name": "zstd", "configuration": {"level": 0, "checksum": False}})
    else:
        reject(f"Compression {comp}")

    W0, H0 = lvinfo[0][0], lvinfo[0][1]
    scales = []
    for L, (W, H, TW, TL) in enumerate(lvinfo):
        shape, chunks, sc = [], [], []
        for a in axes:
            if a == "t":
                shape.append(SizeT); chunks.append(1); sc.append(1)
            elif a == "c":
                shape.append(nchan); chunks.append(spp if interleaved else 1); sc.append(1)
            elif a == "z":
                shape.append(SizeZ); chunks.append(1); sc.append(PZ if PZ is not None else 1)
        y = checkf((PY if PY is not None else 1) * checkf(H0 / H))
        x = checkf((PX if PX is not None else 1) * checkf(W0 / W))
        shape += [H, W]
        chunks += [TL, TW]
        sc += [y, x]
        scales.append(sc)
        out.json(f"{L}/zarr.json", array_meta(shape, dtype, chunks, codecs, axes))

        nsp = spp if (spp > 1 and pcfg == 2) else 1
        across = -(-W // TW)
        down = -(-H // TL)
        T = across * down
        for (z, c, t) in plane_keys:
            ifd = levels[L][(z, c, t)]
            offs = ifd.ints(324)
            cnts = ifd.ints(325)
            if len(offs) != T * nsp or len(cnts) != T * nsp:
                reject(f"IFD at {ifd.offset}: tile count {len(offs)}/{len(cnts)} != {T * nsp}")
            for s in range(nsp):
                for j in range(T):
                    k = s * T + j
                    n = cnts[k]
                    if n <= 0:
                        continue
                    coords = []
                    if "t" in axes:
                        coords.append(t)
                    if "c" in axes:
                        coords.append(s if nsp > 1 else (0 if interleaved else c))
                    if "z" in axes:
                        coords.append(z)
                    coords += [j // across, j % across]
                    out.ref(f"{L}/c/" + "/".join(map(str, coords)), [(0, offs[k], n)])

    name = image_name if image_name else None
    out.json("zarr.json", image_meta(name, axes, units, scales))
    if D is not None:
        out.raw("OME/METADATA.ome.xml", D)


# --------------------------------------------------------------------------
# ND2

ND2_MAGIC = 0x0ABECEDA


class LV:
    """A decoded lite-variant value: typ is the LV type (or 'byte'), val the Python value.
    Levels have typ 11 and val either an LVObj or a list of LV."""
    __slots__ = ("typ", "val")

    def __init__(self, typ, val):
        self.typ = typ
        self.val = val


class LVObj:
    def __init__(self):
        self.members = {}  # insertion order = first appearance; value = last


def utf16(units_bytes):
    units = struct.unpack("<%dH" % (len(units_bytes) // 2), units_bytes)
    out = []
    i = 0
    while i < len(units):
        u = units[i]
        if 0xD800 <= u <= 0xDBFF and i + 1 < len(units) and 0xDC00 <= units[i + 1] <= 0xDFFF:
            out.append(chr(0x10000 + ((u - 0xD800) << 10) + (units[i + 1] - 0xDC00)))
            i += 2
            continue
        if 0xD800 <= u <= 0xDFFF:
            out.append("�")
        else:
            out.append(chr(u))
        i += 1
    return "".join(out)


class LVParser:
    def __init__(self, data):
        self.d = data
        self.n = len(data)

    def need(self, pos, k):
        if pos + k > self.n:
            reject("LV structure truncated")

    def record(self, pos):
        start = pos
        self.need(pos, 2)
        typ, k = self.d[pos], self.d[pos + 1]
        pos += 2
        self.need(pos, 2 * k)
        units = self.d[pos:pos + 2 * k]
        pos += 2 * k
        uu = struct.unpack("<%dH" % k, units)
        if 0 in uu:
            name = utf16(units[:2 * uu.index(0)])
        else:
            name = utf16(units)
        d = self.d
        if typ == 1:
            self.need(pos, 1)
            return name, LV(1, d[pos]), pos + 1
        if typ in (2, 3):
            self.need(pos, 4)
            return name, LV(typ, struct.unpack_from("<i" if typ == 2 else "<I", d, pos)[0]), pos + 4
        if typ in (4, 5, 7):
            self.need(pos, 8)
            return name, LV(typ, struct.unpack_from("<q" if typ == 4 else "<Q", d, pos)[0]), pos + 8
        if typ == 6:
            self.need(pos, 8)
            return name, LV(6, struct.unpack_from("<d", d, pos)[0]), pos + 8
        if typ == 8:
            j = pos
            while True:
                self.need(j, 2)
                if d[j] == 0 and d[j + 1] == 0:
                    break
                j += 2
            return name, LV(8, utf16(d[pos:j])), j + 2
        if typ == 9:
            self.need(pos, 8)
            b = struct.unpack_from("<Q", d, pos)[0]
            pos += 8
            self.need(pos, b)
            return name, LV(9, [LV("byte", x) for x in d[pos:pos + b]]), pos + b
        if typ == 11:
            self.need(pos, 12)
            c, L = struct.unpack_from("<IQ", d, pos)
            pos += 12
            recs = []
            for _ in range(c):
                rn, rv, pos = self.record(pos)
                recs.append((rn, rv))
            if pos != start + L:
                reject(f"LV level length mismatch ({pos - start} != {L})")
            self.need(pos, 8 * c)
            pos += 8 * c
            return name, LV(11, build_level(recs)), pos
        reject(f"LV record type {typ}")

    def all(self):
        pos = 0
        recs = []
        while pos < self.n:
            rn, rv, pos = self.record(pos)
            recs.append((rn, rv))
        return recs


def build_level(recs, top=False):
    if not top and recs and all(n == "" for n, _ in recs):
        return [v for _, v in recs]
    o = LVObj()
    for n, v in recs:
        o.members[n] = v
    return o


def decode_lv(data):
    if data and data[0] == 76:
        if len(data) < 12:
            reject("compressed LV record truncated")
        dobj = zlib.decompressobj()
        try:
            inner = dobj.decompress(data[12:])
            inner += dobj.flush()
        except zlib.error as e:
            reject(f"bad zlib stream in LV: {e}")
        if not dobj.eof or dobj.unused_data:
            reject("zlib stream does not end at the end of the chunk")
        if inner and inner[0] == 76:
            reject("nested compressed LV")
        data = inner
    return LV(11, build_level(LVParser(data).all(), top=True))


MISSING = object()


def get(v, path):
    """Follow a /-separated path from an LV; return MISSING if a member is absent."""
    for part in path.split("/"):
        if v is MISSING:
            return MISSING
        if v.typ != 11:
            reject(f"LV path {path}: {part} looked up in a non-level value")
        if isinstance(v.val, LVObj):
            v = v.val.members.get(part, MISSING)
        else:
            return MISSING
    return v


def members(v):
    if v is MISSING:
        return []
    if v.typ == 9:
        return v.val
    if v.typ != 11:
        reject("members of a non-level value")
    if isinstance(v.val, LVObj):
        return list(v.val.members.values())
    return list(v.val)


def as_num(v, path, default=MISSING):
    if v is MISSING:
        if default is MISSING:
            reject(f"required LV member {path} missing")
        return default
    if v.typ not in (2, 3, 4, 5, 6, "byte"):
        reject(f"LV member {path} has type {v.typ}, not a number")
    return v.val


def as_int(v, path, default=MISSING):
    x = as_num(v, path, default)
    if isinstance(x, float):
        if not x.is_integer():
            reject(f"LV member {path} = {x} is not an integer")
        x = int(x)
    return x


def as_flag(v, path, default=MISSING):
    if v is MISSING:
        if default is MISSING:
            reject(f"required LV member {path} missing")
        return default
    if v.typ not in (1, 2, 3, 4, 5, "byte"):
        reject(f"LV member {path} has type {v.typ}, not a flag")
    return v.val != 0


def as_str(v, path, default):
    if v is MISSING:
        return default
    if v.typ != 8:
        reject(f"LV member {path} is not a string")
    return v.val


class ND2:
    def __init__(self, src):
        self.src = src

    def header(self, o):
        h = self.src.read(o, 16)
        magic, n, d = struct.unpack("<IIQ", h)
        if magic != ND2_MAGIC:
            reject(f"no chunk magic at {o}")
        if d > MAX_SAFE:
            reject("chunk data length out of range")
        return n, d

    def chunk(self, o):
        n, d = self.header(o)
        name = self.src.read(o + 16, n)
        data = self.src.read(o + 16 + n, d)
        return name, data


def virtualize_nd2(src, out):
    f = ND2(src)
    n, d = f.header(0)
    name = src.read(16, n)
    if n != 32 or d != 64 or name != b"ND2 FILE SIGNATURE CHUNK NAME01!":
        reject("bad signature chunk")
    data = src.read(48, 64)
    m = re.match(rb"Ver([0-9]+)\.", data)
    if not m or int(m.group(1)) < 3:
        reject("unsupported ND2 version")
    if src.size < 40:
        reject("file too short for chunk map")
    tail = src.read(src.size - 40, 40)
    if tail[:32] != b"ND2 CHUNK MAP SIGNATURE 0000001!":
        reject("no chunk map signature")
    mo = struct.unpack("<Q", tail[32:])[0]
    if mo > MAX_SAFE:
        reject("chunk map offset out of range")
    mname, mdata = f.chunk(mo)
    if mname.rstrip(b"\x00") != b"ND2 FILEMAP SIGNATURE NAME 0001!":
        reject("bad chunk map name")
    cmap = {}
    pos = 0
    while True:
        j = mdata.find(b"!", pos)
        if j < 0:
            reject("chunk map truncated (no terminating record)")
        rname = mdata[pos:j + 1]
        if rname == b"ND2 CHUNK MAP SIGNATURE 0000001!":
            break
        if j + 17 > len(mdata):
            reject("chunk map record truncated")
        off, _size = struct.unpack_from("<QQ", mdata, j + 1)
        cmap[rname] = off
        pos = j + 17

    def lv_chunk(cname):
        o = cmap.get(cname)
        if o is None:
            return None
        if o > MAX_SAFE:
            reject("chunk offset out of range")
        _, cdata = f.chunk(o)
        return decode_lv(cdata)

    # ---- attributes
    attrs_lv = lv_chunk(b"ImageAttributesLV!")
    if attrs_lv is None:
        reject("no ImageAttributesLV! chunk")
    A = get(attrs_lv, "SLxImageAttributes")
    if A is MISSING:
        reject("no SLxImageAttributes")
    P = "SLxImageAttributes/"
    W = as_int(get(A, "uiWidth"), P + "uiWidth")
    H = as_int(get(A, "uiHeight"), P + "uiHeight")
    WB = as_int(get(A, "uiWidthBytes"), P + "uiWidthBytes")
    comp = as_int(get(A, "uiComp"), P + "uiComp")
    bpc = as_int(get(A, "uiBpcInMemory"), P + "uiBpcInMemory")
    sig = as_int(get(A, "uiBpcSignificant"), P + "uiBpcSignificant")
    ecomp = as_int(get(A, "eCompression"), P + "eCompression", 2)
    tw = as_int(get(A, "uiTileWidth"), P + "uiTileWidth", 0)
    th = as_int(get(A, "uiTileHeight"), P + "uiTileHeight", 0)
    if W < 1 or H < 1 or comp < 1:
        reject("uiWidth, uiHeight and uiComp must be at least 1")
    dtype = {8: "uint8", 16: "uint16", 32: "float32"}.get(bpc)
    if dtype is None:
        reject(f"uiBpcInMemory {bpc}")
    if ecomp not in (0, 2):
        reject(f"eCompression {ecomp}")
    compressed = ecomp == 0
    if (tw > 0 and tw != W) or (th > 0 and th != H):
        reject("tiled ND2")

    # ---- experiment
    loops = []  # dicts: kind, etype, depth, count, period, step
    exp_lv = lv_chunk(b"ImageMetadataLV!")
    if exp_lv is not None:
        root = get(exp_lv, "SLxExperiment")
        if root is MISSING:
            reject("no SLxExperiment")

        def valid_members(node, lp, listname, validpath_node):
            items = members(get(lp, listname))
            if validpath_node[0] == "lp":
                vv = get(lp, validpath_node[1])
            else:
                vv = get(node, validpath_node[1])
            if vv is MISSING:
                return items
            flags = members(vv)
            res = []
            for i, it in enumerate(items):
                if i < len(flags) and as_flag(flags[i], validpath_node[1]):
                    res.append(it)
            return res

        def visit(node, depth):
            et = as_int(get(node, "eType"), "eType")
            if et not in (1, 2, 4, 6, 8):
                reject(f"eType {et}")
            lp = get(node, "uLoopPars")
            children = members(get(node, "ppNextLevelEx"))
            if lp is MISSING:
                return
            loop = {"etype": et, "depth": depth, "period": 0.0, "step": 0.0}
            if et == 1:
                loop["kind"] = "t"
                loop["count"] = as_int(get(lp, "uiCount"), "uiCount", 0)
                loop["period"] = as_num(get(lp, "dPeriod"), "dPeriod", 0)
            elif et == 8:
                loop["kind"] = "t"
                vp = valid_members(node, lp, "pPeriod", ("lp", "pPeriodValid"))
                loop["count"] = sum(as_int(get(p, "uiCount"), "pPeriod/uiCount") for p in vp)
                loop["period"] = as_num(get(vp[0], "dPeriod"), "dPeriod", 0) if vp else 0
            elif et == 2:
                loop["kind"] = "p"
                loop["count"] = len(valid_members(node, lp, "Points", ("node", "pItemValid")))
            elif et == 4:
                loop["kind"] = "z"
                cnt = as_int(get(lp, "uiCount"), "uiCount", 0)
                loop["count"] = cnt
                step = abs(as_num(get(lp, "dZStep"), "dZStep", 0))
                if step == 0 and cnt > 1:
                    hi = as_num(get(lp, "dZHigh"), "dZHigh", 0)
                    lo = as_num(get(lp, "dZLow"), "dZLow", 0)
                    step = checkf(checkf(abs(checkf(float(hi) - float(lo)))) / float(cnt - 1))
                loop["step"] = step
            else:  # 6 spectral
                c = get(lp, "uiCount")
                if c is not MISSING:
                    cnt = as_int(c, "uiCount")
                else:
                    cnt = as_int(get(lp, "pPlanes/uiCount"), "pPlanes/uiCount", 0)
                loop["count"] = cnt
            if loop["count"] < 0:
                reject(f"negative loop count {loop['count']}")
            if loop["count"] == 0:
                return
            if et == 6:
                for ch in children:
                    visit(ch, depth)
                return
            if not loops or loops[-1]["depth"] < depth:
                loops.append(loop)
            elif (loops[-1]["depth"] == depth and loops[-1]["etype"] == et
                  and loops[-1]["count"] < loop["count"]):
                loops[-1] = loop
            for ch in children:
                visit(ch, depth + 1)

        visit(root, 0)
        kinds = [l["kind"] for l in loops]
        if len(set(kinds)) != len(kinds):
            reject("two loops of the same kind")

    # ---- picture metadata
    pic_lv = lv_chunk(b"ImageMetadataSeqLV|0!")
    PM = get(pic_lv, "SLxPictureMetadata") if pic_lv is not None else MISSING
    planes = None
    calibrated = False
    cal = 1.0
    aspect = 1.0
    if PM is not MISSING:
        np_ = as_int(get(PM, "sPicturePlanes/uiCount"), "sPicturePlanes/uiCount", 0)
        planes = []
        for i in range(max(np_, 0)):
            planes.append(get(PM, f"sPicturePlanes/sPlaneNew/a{i}"))
        bc = as_flag(get(PM, "bCalibrated"), "bCalibrated", False)
        dc = get(PM, "dCalibration")
        if bc and dc is not MISSING:
            cv = as_num(dc, "dCalibration")
            if cv > 0:
                calibrated = True
                cal = float(cv)
        da = as_num(get(PM, "dAspect"), "dAspect", 1)
        aspect = float(da) if da > 0 else 1.0

    # ---- channels
    channels = None
    if planes and all(p is not MISSING for p in planes):
        ccs = [as_int(get(p, "uiCompCount"), "uiCompCount", 1) for p in planes]
        if all(c in (1, 3) for c in ccs) and sum(ccs) == comp:
            channels = []
            for p, cc in zip(planes, ccs):
                desc = as_str(get(p, "sDescription"), "sDescription", "")
                if cc == 1:
                    col = as_int(get(p, "uiColor"), "uiColor", 0xFFFFFF)
                    rgb = "%02X%02X%02X" % (col & 0xFF, (col >> 8) & 0xFF, (col >> 16) & 0xFF)
                    channels.append((desc, rgb))
                else:
                    channels += [(desc + " R", "FF0000"), (desc + " G", "00FF00"), (desc + " B", "0000FF")]
    if channels is None:
        channels = [(f"C{k}", "FFFFFF") for k in range(comp)]

    # ---- frames
    N = 1
    for l in loops:
        N *= l["count"]
    R = W * comp * bpc // 8
    if WB < R:
        reject("uiWidthBytes < uiWidth * uiComp * bytes")
    fre = re.compile(rb"ImageDataSeq\|(0|[1-9][0-9]*)!")
    frames = {}
    for nm, o in cmap.items():
        mm = fre.fullmatch(nm)
        if mm:
            fi = int(mm.group(1))
            if fi < N:
                frames[fi] = o
    for o in frames.values():
        if o > MAX_SAFE:
            reject("frame offset out of range")
    franges = {}
    if frames:
        if not compressed:
            lo, hi = min(frames), max(frames)
            n1, d1 = f.header(frames[lo])
            n2, d2 = f.header(frames[hi])
            if n1 != n2:
                reject("frame name lengths differ")
            if d1 < 8 + H * WB or d2 < 8 + H * WB:
                reject("frame data too short")
            for fi, o in frames.items():
                start = o + 16 + n1 + 8
                if WB == R:
                    franges[fi] = [(0, start, H * R)]
                else:
                    franges[fi] = [(0, start + r * WB, R) for r in range(H)]
        else:
            if WB != R:
                reject("compressed frames with row padding")
            for fi, o in frames.items():
                nn, dd = f.header(o)
                if dd <= 8:
                    reject("compressed frame without data")
                franges[fi] = [(0, o + 16 + nn + 8, dd - 8)]
    elif compressed and WB != R:
        reject("compressed frames with row padding")

    # ---- output
    kinds = [l["kind"] for l in loops]
    tl = loops[kinds.index("t")] if "t" in kinds else None
    zl = loops[kinds.index("z")] if "z" in kinds else None
    pl = loops[kinds.index("p")] if "p" in kinds else None
    npos = pl["count"] if pl else 1
    axes, shape, chunks, scale, units = [], [], [], [], {}
    if tl:
        axes.append("t"); shape.append(tl["count"]); chunks.append(1)
        per = float(tl["period"])
        if per > 0:
            scale.append(checkf(per / 1000)); units["t"] = "second"
        else:
            scale.append(1)
    if comp > 1:
        axes.append("c"); shape.append(comp); chunks.append(comp); scale.append(1)
    if zl:
        axes.append("z"); shape.append(zl["count"]); chunks.append(1)
        st = float(zl["step"])
        if st > 0:
            scale.append(st); units["z"] = "micrometer"
        else:
            scale.append(1)
    axes += ["y", "x"]
    shape += [H, W]
    chunks += [H, W]
    if calibrated:
        scale += [checkf(cal * aspect), cal]
        units["y"] = units["x"] = "micrometer"
    else:
        scale += [1, 1]
    codecs = []
    if comp > 1:
        codecs.append(transpose_codec(axes))
    codecs.append(bytes_codec(bpc // 8, "little"))
    if compressed:
        codecs.append({"name": "zlib", "configuration": {"level": 1}})

    if bpc == 32:
        window = None
    else:
        b = sig if 1 <= sig <= bpc else bpc
        V = (1 << b) - 1
        window = {"min": 0, "max": V, "start": 0, "end": V}
    omero_ch = []
    for label, color in channels:
        ch = {"label": label, "color": color, "active": True}
        if window is not None:
            ch["window"] = dict(window)
        omero_ch.append(ch)

    out.json("zarr.json", {"zarr_format": 3, "node_type": "group",
                           "attributes": {"ome": {"version": "0.5", "bioformats2raw.layout": 3}}})
    out.json("OME/zarr.json", {"zarr_format": 3, "node_type": "group",
                               "attributes": {"ome": {"version": "0.5", "series": [str(p) for p in range(npos)]}}})
    for p in range(npos):
        out.json(f"{p}/zarr.json", image_meta(f"position {p}", axes, units, [scale],
                                              omero={"channels": omero_ch}))
        out.json(f"{p}/0/zarr.json", array_meta(shape, dtype, chunks, codecs, axes))

    counts = [l["count"] for l in loops]
    for fi in sorted(franges):
        coord = {}
        rem = fi
        for k in range(len(loops) - 1, -1, -1):
            coord[kinds[k]] = rem % counts[k]
            rem //= counts[k]
        p = coord.get("p", 0)
        cs = []
        if tl:
            cs.append(coord["t"])
        if comp > 1:
            cs.append(0)
        if zl:
            cs.append(coord["z"])
        cs += [0, 0]
        out.ref(f"{p}/0/c/" + "/".join(map(str, cs)), franges[fi])


# --------------------------------------------------------------------------


def main(argv):
    if len(argv) != 3:
        print("usage: virtualize <url> <out.json>", file=sys.stderr)
        return 2
    url, path = argv[1], argv[2]
    try:
        src = Source(url)
        out = Output(src)
        head = src.read(0, min(4, src.size))
        if head in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
            virtualize_tiff(src, out)
        elif head == b"\xda\xce\xbe\x0a":
            virtualize_nd2(src, out)
        else:
            reject("not a TIFF or ND2 (version 3+) file")
    except Reject as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 3
    except Fail as e:
        print(f"failed: {e}", file=sys.stderr)
        return 1
    out.dump(path)
    print(f"{len(out.entries)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
