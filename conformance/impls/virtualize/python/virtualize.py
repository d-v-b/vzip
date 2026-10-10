"""virtualize <url> <out.json>: spec/virtualize.md (revision 4), standard library only.

Exit status: 0 on success, 3 when the specification rejects the input, other
values when reading fails (or on an internal error).
"""

from __future__ import annotations

import base64
import http.client
import json
import math
import re
import struct
import sys
import urllib.parse
import zlib

MAX = 2**53 - 1
MAX_PAYLOAD = 65519
WS = " \t\r\n"


class Reject(Exception):
    pass


class ReadError(Exception):
    pass


def need(cond, msg):
    if not cond:
        raise Reject(msg)


# --------------------------------------------------------------------------
# Remote source with a block cache


class Source:
    BLOCK = 1 << 16

    def __init__(self, url: str) -> None:
        u = urllib.parse.urlsplit(url)
        if u.scheme != "http":
            raise ReadError(f"unsupported URL scheme: {u.scheme}")
        self.host = u.hostname
        self.port = u.port or 80
        self.path = (u.path or "/") + (("?" + u.query) if u.query else "")
        self.conn = None
        self.cache: dict[int, bytes] = {}
        st, hdrs, _ = self._request("HEAD", {})
        if st != 200:
            raise ReadError(f"HEAD status {st}")
        cl = hdrs.get("content-length")
        if cl is None or not cl.isdigit():
            raise ReadError("HEAD without Content-Length")
        self.size = int(cl)

    def _request(self, method, headers):
        for attempt in range(3):
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPConnection(self.host, self.port, timeout=600)
                self.conn.request(method, self.path, headers=headers)
                r = self.conn.getresponse()
                body = r.read()
                return r.status, {k.lower(): v for k, v in r.getheaders()}, body
            except (OSError, http.client.HTTPException) as e:
                self.conn = None
                if attempt == 2:
                    raise ReadError(str(e)) from e

    def _fetch(self, first: int, last: int) -> None:
        start = first * self.BLOCK
        end = min(self.size, (last + 1) * self.BLOCK)
        st, _, body = self._request("GET", {"Range": f"bytes={start}-{end - 1}"})
        if st != 206 or len(body) != end - start:
            raise ReadError(f"GET status {st}, {len(body)} bytes")
        for b in range(first, last + 1):
            self.cache[b] = body[(b - first) * self.BLOCK : (b - first + 1) * self.BLOCK]

    def read(self, off: int, n: int) -> bytes:
        if off < 0 or n < 0 or off > MAX or n > MAX or off + n > self.size:
            raise Reject(f"read outside the file ({off}+{n} > {self.size})")
        if n == 0:
            return b""
        first, last = off // self.BLOCK, (off + n - 1) // self.BLOCK
        b = first
        while b <= last:
            if b in self.cache:
                b += 1
                continue
            e = b
            while e + 1 <= last and e + 1 not in self.cache:
                e += 1
            self._fetch(b, e)
            b = e + 1
        out = b"".join(self.cache[b] for b in range(first, last + 1))
        s = off - first * self.BLOCK
        return out[s : s + n]


# --------------------------------------------------------------------------
# Output helpers


def varint_len(v: int) -> int:
    n = 1
    while v >= 0x80:
        v >>= 7
        n += 1
    return n


def range_msg_len(o: int, n: int) -> int:
    ln = 0
    if o > 0:
        ln += 1 + varint_len(o)
    if n > 0:
        ln += 1 + varint_len(n)
    return ln


def payload_len(ranges) -> int:
    if len(ranges) == 1:
        return range_msg_len(ranges[0][1], ranges[0][2])
    tot = 0
    for _, o, n in ranges:
        r = range_msg_len(o, n)
        tot += 1 + varint_len(r) + r
    return tot


class Output:
    def __init__(self, url: str, size: int) -> None:
        self.url = url
        self.size = size
        self.entries: dict[str, dict] = {}

    def json(self, key, value):
        self.entries[key] = {"json": value}

    def bytes(self, key, data: bytes):
        self.entries[key] = {"base64": base64.b64encode(data).decode("ascii")}

    def ref(self, key, ranges):
        for _, o, n in ranges:
            need(o >= 0 and n >= 0 and o + n <= self.size, f"{key}: range ({o}, {n}) outside the file")
        need(payload_len(ranges) <= MAX_PAYLOAD, f"{key}: payload too large")
        self.entries[key] = {"ranges": [[0, o, n] for _, o, n in ranges]}

    def dump(self) -> str:
        return json.dumps({"sources": [self.url], "entries": self.entries}, allow_nan=False)


def finite(x: float, what: str) -> float:
    need(math.isfinite(x), f"{what} is not finite")
    return x


def array_json(shape, dtype, chunk_shape, codecs, dims):
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


def transpose_codec(dims):
    order = [i for i, d in enumerate(dims) if d != "c"] + [dims.index("c")]
    return {"name": "transpose", "configuration": {"order": order}}


def bytes_codec(itemsize, endian):
    if itemsize == 1:
        return {"name": "bytes"}
    return {"name": "bytes", "configuration": {"endian": endian}}


AXIS_TYPE = {"t": "time", "c": "channel", "z": "space", "y": "space", "x": "space"}


def axis_json(name, unit):
    a = {"name": name, "type": AXIS_TYPE[name]}
    if unit is not None:
        a["unit"] = unit
    return a


# --------------------------------------------------------------------------
# TIFF profile (§3)

TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}
INT_FMT = {1: "B", 3: "H", 4: "I", 13: "I", 16: "Q", 18: "Q", 6: "b", 8: "h", 9: "i", 17: "q"}
UNSIGNED_TYPES = {1, 3, 4, 13, 16, 18}
DESC_TYPES = set(range(1, 13)) | {13, 16, 17, 18}
SCALAR_TAGS = {256, 257, 259, 277, 284, 317, 322, 323}
ARRAY_TAGS = {258, 324, 325, 330, 339}
TEXT_TAG = 270
TABLE_TAGS = SCALAR_TAGS | ARRAY_TAGS | {TEXT_TAG}
MAX_IFDS = 100000


class IFD:
    def __init__(self, offset):
        self.offset = offset
        self.tags: dict[int, tuple] = {}  # tag -> (type, count, values or None, data offset)
        self.next = 0
        self.subs: list[IFD] = []

    def has(self, tag):
        return tag in self.tags

    def scalar(self, tag, default=None):
        if tag not in self.tags:
            return default
        return self.tags[tag][2][0]

    def array(self, tag):
        return self.tags[tag][2] if tag in self.tags else None


class Tiff:
    def __init__(self, src: Source, out: Output) -> None:
        self.src = src
        self.out = out
        h = src.read(0, 4)
        self.bo = "<" if h[:2] == b"II" else ">"
        magic = struct.unpack(self.bo + "H", h[2:4])[0]
        self.big = magic == 43
        if self.big:
            osz, res, first = struct.unpack(self.bo + "HHQ", src.read(4, 12))
            need(osz == 8, "BigTIFF offset size is not 8")
            need(res == 0, "BigTIFF reserved word is not 0")
        else:
            first = struct.unpack(self.bo + "I", src.read(4, 4))[0]
        need(first <= MAX, "IFD offset above 2^53-1")
        self.seen: set[int] = set()
        self.nread = 0
        self.main: list[IFD] = []
        off = first
        while off != 0:
            ifd = self.read_ifd(off, True)
            self.main.append(ifd)
            off = ifd.next
        need(self.main, "no IFDs")
        for ifd in self.main:
            for o in ifd.array(330) or []:
                ifd.subs.append(self.read_ifd(o, False))

    def read_ifd(self, off: int, main: bool) -> IFD:
        need(off >= (16 if self.big else 8), f"IFD offset {off} too small")
        need(off <= MAX, "IFD offset above 2^53-1")
        need(off not in self.seen, f"IFD offset {off} read twice")
        self.seen.add(off)
        self.nread += 1
        need(self.nread <= MAX_IFDS, "more than 100000 IFDs")
        bo = self.bo
        if self.big:
            count = struct.unpack(bo + "Q", self.src.read(off, 8))[0]
            esz, base, inl = 20, off + 8, 8
        else:
            count = struct.unpack(bo + "H", self.src.read(off, 2))[0]
            esz, base, inl = 12, off + 2, 4
        need(count <= MAX, "IFD entry count too large")
        raw = self.src.read(base, count * esz)
        ifd = IFD(off)
        if main:
            # A SubIFD's next-IFD offset is ignored (§3.1), so it is not read.
            nxt = self.src.read(base + count * esz, inl)
            ifd.next = struct.unpack(bo + ("Q" if self.big else "I"), nxt)[0]
            need(ifd.next <= MAX, "next IFD offset above 2^53-1")
        for i in range(count):
            e = raw[i * esz : (i + 1) * esz]
            if self.big:
                tag, typ, cnt = struct.unpack(bo + "HHQ", e[:12])
                field = e[12:20]
            else:
                tag, typ, cnt = struct.unpack(bo + "HHI", e[:8])
                field = e[8:12]
            if tag not in TABLE_TAGS or tag in ifd.tags:
                continue
            if tag == TEXT_TAG:
                need(typ in DESC_TYPES, f"ImageDescription has field type {typ}")
            else:
                need(typ in UNSIGNED_TYPES, f"tag {tag} has field type {typ}")
            need(cnt <= MAX, f"tag {tag} count above 2^53-1")
            nbytes = cnt * TYPE_SIZE[typ]
            if nbytes <= inl:
                data_off = base + i * esz + (12 if self.big else 8)
                data = field[:nbytes]
            else:
                data_off = struct.unpack(bo + ("Q" if self.big else "I"), field)[0]
                need(data_off <= MAX and nbytes <= MAX, f"tag {tag} value offset above 2^53-1")
                need(data_off + nbytes <= self.src.size, f"tag {tag} value outside the file")
                data = None
            values = None
            if typ in INT_FMT:
                if data is None:
                    data = self.src.read(data_off, nbytes)
                values = list(struct.unpack(f"{bo}{cnt}{INT_FMT[typ]}", data))
                if typ in (16, 17, 18):
                    for v in values:
                        need(abs(v) <= MAX, f"tag {tag} value above 2^53-1")
            if tag in SCALAR_TAGS:
                need(cnt >= 1, f"scalar tag {tag} has no value")
            ifd.tags[tag] = (typ, cnt, values, data_off)
        return ifd

    # ---- §3.1 derived values

    def fmt(self, ifd: IFD):
        bps = ifd.array(258)
        need(bps is not None and len(bps) >= 1, "BitsPerSample missing or empty")
        need(all(b == bps[0] for b in bps) and bps[0] >= 1, "BitsPerSample values differ or are 0")
        spp = ifd.scalar(277, 1)
        need(spp >= 1, "SamplesPerPixel is 0")
        sf = ifd.array(339)
        if sf is None:
            sfv = 1
        else:
            need(len(sf) >= 1, "SampleFormat has no values")
            need(all(v == sf[0] for v in sf), "SampleFormat values differ")
            sfv = sf[0]
        if spp == 1:
            planar = 1
        else:
            planar = ifd.scalar(284, 1)
            need(planar in (1, 2), f"PlanarConfiguration {planar}")
        return (bps[0], spp, sfv, planar, ifd.scalar(259, 1), ifd.scalar(317, 1))

    @staticmethod
    def tiled(ifd: IFD) -> bool:
        return ifd.has(322) and ifd.has(324)

    def check_size(self, ifd: IFD):
        need(ifd.has(256) and ifd.has(257), "ImageWidth or ImageLength missing")
        need(ifd.scalar(256) >= 1 and ifd.scalar(257) >= 1, "ImageWidth or ImageLength is 0")
        if self.tiled(ifd):
            need(all(ifd.has(t) for t in (322, 323, 324, 325)), "tile tags missing")
            need(ifd.scalar(322) >= 1 and ifd.scalar(323) >= 1, "tile size is 0")

    def description(self, ifd: IFD):
        t = ifd.tags.get(TEXT_TAG)
        if t is None or t[0] != 2:
            return None
        _, cnt, _, data_off = t
        data = self.src.read(data_off, cnt)  # inline values point into the IFD entry
        z = data.find(b"\0")
        return data if z < 0 else data[:z]

    # ---- driver

    def run(self):
        ifd0 = self.main[0]
        fmt0 = self.fmt(ifd0)
        bits, spp, sf, planar, comp, pred = fmt0
        D = self.description(ifd0)
        ome = None
        if D is not None:
            try:
                X = D.decode("utf-8")
            except UnicodeDecodeError:
                X = None
            if X is not None:
                ome = parse_ome(X)
                if ome is None:
                    D = None
            else:
                D = None

        # §3.3 planes
        if ome is None:
            SZ, SC, ST = 1, spp, 1
            Cp = 1
            plane_ifd = {(0, 0, 0): 0}
            positions = [(0, 0, 0)]
        else:
            px = ome["pixels"]
            SZ = px.get("SizeZ", 1)
            ST = px.get("SizeT", 1)
            SC = px.get("SizeC", spp)
            if spp > 1:
                if SC == 1:
                    SC = spp
                need(SC == spp, f"SizeC {SC} differs from SamplesPerPixel {spp}")
                Cp = 1
            else:
                Cp = SC
            total = SZ * Cp * ST
            need(total <= 100000, "more than 100000 planes")
            order = px.get("DimensionOrder", "XYZCT")[2:]
            sizes = {"Z": SZ, "C": Cp, "T": ST}
            tds = ome["tiffdata"] or [{}]
            files = set()
            for td in ome["tiffdata"]:
                if td.get("_file") is not None:
                    files.add(td["_file"])
            need(len(files) <= 1, "multi-file dataset")
            mapping: dict[int, int] = {}
            for td in tds:
                fz, fc, ft = td.get("FirstZ", 0), td.get("FirstC", 0), td.get("FirstT", 0)
                need(fz < SZ and fc < Cp and ft < ST, "TiffData First* out of range")
                first = {"Z": fz, "C": fc, "T": ft}
                lin = 0
                mul = 1
                for L in order:
                    lin += first[L] * mul
                    mul *= sizes[L]
                ifdi = td.get("IFD", 0)
                if "PlaneCount" in td:
                    pc = td["PlaneCount"]
                elif len(tds) == 1 and "IFD" not in td:
                    pc = total
                else:
                    pc = 1
                for i in range(min(pc, total - lin)):
                    mapping[lin + i] = ifdi + i
            positions = []
            plane_ifd = {}
            for lin in range(total):
                need(lin in mapping and mapping[lin] < len(self.main), f"plane {lin} not mapped to an IFD")
                rem = lin
                pos = {}
                for L in order:
                    pos[L] = rem % sizes[L]
                    rem //= sizes[L]
                key = (pos["Z"], pos["C"], pos["T"])
                plane_ifd[key] = mapping[lin]
            positions = sorted(plane_ifd)

        # §3.4 levels: list of {pos: IFD}
        levels: list[dict] = [{p: self.main[plane_ifd[p]] for p in positions}]
        if ifd0.array(330):
            s = len(ifd0.array(330))
            for p in positions:
                need(len(levels[0][p].subs) >= s, "plane IFD has fewer SubIFDs than IFD 0")
            for k in range(1, s + 1):
                levels.append({p: levels[0][p].subs[k - 1] for p in positions})
        elif ome is None:
            last = ifd0
            for ifd in self.main[1:]:
                if not (self.tiled(ifd) and ifd.has(258)):
                    continue
                f = self.fmt(ifd)
                self.check_size(ifd)
                if f == fmt0 and ifd.scalar(256) < last.scalar(256) and ifd.scalar(257) < last.scalar(257):
                    levels.append({(0, 0, 0): ifd})
                    last = ifd

        geo = []
        for lv in levels:
            g = None
            for p in positions:
                ifd = lv[p]
                f = self.fmt(ifd)
                self.check_size(ifd)
                need(self.tiled(ifd), "image is not tiled")
                need(f == fmt0, "image format differs from IFD 0")
                gi = (ifd.scalar(256), ifd.scalar(257), ifd.scalar(322), ifd.scalar(323))
                need(g is None or g == gi, "planes of a level differ")
                g = gi
            geo.append(g)

        # §3.5 data type and codecs
        need(sf in (1, 2, 3), f"SampleFormat {sf}")
        need(bits in ((32, 64) if sf == 3 else (8, 16, 32, 64)), f"BitsPerSample {bits}")
        dtype = {1: "uint", 2: "int", 3: "float"}[sf] + str(bits)
        endian = "little" if self.bo == "<" else "big"
        interleaved = spp > 1 and planar == 1
        if comp in (33003, 33004, 33005, 34712):
            a2b, compressor = {"name": "imagecodecs_jpeg2k"}, None
        else:
            need(pred == 1, f"Predictor {pred}")
            if comp == 1:
                compressor = None
            elif comp in (8, 32946):
                compressor = {"name": "zlib", "configuration": {"level": 1}}
            elif comp == 50000:
                compressor = {"name": "zstd", "configuration": {"level": 0, "checksum": False}}
            else:
                raise Reject(f"Compression {comp}")
            a2b = bytes_codec(bits // 8, endian)

        # §3.6 output
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

        px = ome["pixels"] if ome else {}
        PX = px.get("PhysicalSizeX")
        PY = px.get("PhysicalSizeY")
        PZ = px.get("PhysicalSizeZ")
        units = {}
        if ome:
            for ax, P in (("x", PX), ("y", PY), ("z", PZ)):
                if P is not None:
                    units[ax] = UNITS.get(px.get(f"PhysicalSize{ax.upper()}Unit", "µm"))
        W0, H0 = geo[0][0], geo[0][1]
        datasets = []
        for L, (W, H, TW, TL) in enumerate(geo):
            sc = {
                "t": 1,
                "c": 1,
                "z": PZ if PZ is not None else 1,
                "y": finite((PY if PY is not None else 1) * (H0 / H), "scale"),
                "x": finite((PX if PX is not None else 1) * (W0 / W), "scale"),
            }
            datasets.append(
                {"path": str(L), "coordinateTransformations": [{"type": "scale", "scale": [sc[d] for d in dims]}]}
            )
        ms = {}
        if ome and ome.get("name"):
            ms["name"] = ome["name"]
        ms["axes"] = [axis_json(d, units.get(d)) for d in dims]
        ms["datasets"] = datasets
        self.out.json("zarr.json", {"zarr_format": 3, "node_type": "group", "attributes": {"ome": {"version": "0.5", "multiscales": [ms]}}})

        nsp = spp if (spp > 1 and planar == 2) else 1
        for L, (W, H, TW, TL) in enumerate(geo):
            full = {"t": ST, "c": nchan, "z": SZ, "y": H, "x": W}
            chunk = {"t": 1, "c": spp if interleaved else 1, "z": 1, "y": TL, "x": TW}
            self.out.json(f"{L}/zarr.json", array_json([full[d] for d in dims], dtype, [chunk[d] for d in dims], codecs, dims))
            across = -(-W // TW)
            T = -(-H // TL) * across
            for p in positions:
                ifd = levels[L][p]
                offs, cnts = ifd.array(324), ifd.array(325)
                need(len(offs) == T * nsp and len(cnts) == T * nsp, "TileOffsets/TileByteCounts count")
                z, c, t = p
                for k in range(T * nsp):
                    n = cnts[k]
                    if n == 0:
                        continue
                    s, j = divmod(k, T)
                    cc = c if spp == 1 else (s if planar == 2 else 0)
                    coord = {"t": t, "c": cc, "z": z, "y": j // across, "x": j % across}
                    key = f"{L}/c/" + "/".join(str(coord[d]) for d in dims)
                    self.out.ref(key, [(0, offs[k], n)])
        if ome is not None:
            self.out.bytes("OME/METADATA.ome.xml", D)


UNITS = {
    "\u00b5m": "micrometer",
    "\u03bcm": "micrometer",
    "um": "micrometer",
    "nm": "nanometer",
    "mm": "millimeter",
    "cm": "centimeter",
    "m": "meter",
    "pm": "picometer",
    "in": "inch",
    "ft": "foot",
    "s": "second",
    "ms": "millisecond",
    "min": "minute",
    "h": "hour",
    "\u00c5": "angstrom",
    "\u212b": "angstrom",
}

# ---- §3.2 tag scan

_NAME = r"[A-Za-z0-9_.\-]+"
_WSC = r"[ \t\r\n]"
_ATTR = _WSC + r"+([^ \t\r\n=/>\"'<]+)" + _WSC + r"*=" + _WSC + r"*(?:\"([^\"]*)\"|'([^']*)')"
_ATTR_NC = _WSC + r"+[^ \t\r\n=/>\"'<]+" + _WSC + r"*=" + _WSC + r"*(?:\"[^\"]*\"|'[^']*')"
TAG_RE = re.compile(r"<(/?)(?:" + _NAME + r":)?(" + _NAME + r")((?:" + _ATTR_NC + r")*)" + _WSC + r"*(/?)>")
ATTR_RE = re.compile(_ATTR)
REF_RE = re.compile(r"&(lt|gt|amp|quot|apos);|&#([0-9]+);|&#x([0-9A-Fa-f]+);")
ENT = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}
INT_RE = re.compile(r"[ \t\r\n]*([0-9]+)[ \t\r\n]*")
FLOAT_RE = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")
DIM_ORDERS = {"XY" + a + b + c for a, b, c in ("ZCT", "ZTC", "CZT", "CTZ", "TZC", "TCZ")}


def decode_refs(s: str) -> str:
    def rep(m):
        if m.group(1):
            return ENT[m.group(1)]
        v = int(m.group(2)) if m.group(2) is not None else int(m.group(3), 16)
        if v == 0 or 0xD800 <= v <= 0xDFFF or v > 0x10FFFF:
            return m.group(0)
        return chr(v)

    return REF_RE.sub(rep, s)


class Tag:
    __slots__ = ("start", "end", "is_end", "name", "attrs", "self_closing")


def scan_tags(X: str):
    tags = []
    skips = []
    n = len(X)
    i = 0
    while True:
        j = X.find("<", i)
        if j < 0:
            break
        end_marker = None
        for start_m, end_m in (("<!--", "-->"), ("<![CDATA[", "]]>"), ("<?", "?>"), ("<!", ">")):
            if X.startswith(start_m, j):
                end_marker = (start_m, end_m)
                break
        if end_marker:
            e = X.find(end_marker[1], j + len(end_marker[0]))
            e = n if e < 0 else e + len(end_marker[1])
            skips.append((j, e))
            i = e
            continue
        m = TAG_RE.match(X, j)
        if m is None:
            i = j + 1
            continue
        t = Tag()
        t.start, t.end = j, m.end()
        t.is_end = m.group(1) == "/"
        t.name = m.group(2)
        t.self_closing = m.group(4) == "/"
        attrs = {}
        for a in ATTR_RE.finditer(m.group(3)):
            if a.group(1) not in attrs:
                attrs[a.group(1)] = a.group(2) if a.group(2) is not None else a.group(3)
        t.attrs = attrs
        tags.append(t)
        i = m.end()
    return tags, skips


def int_attr(v: str, name: str, min1: bool) -> int:
    m = INT_RE.fullmatch(v)
    need(m is not None, f"{name}={v!r} is not an integer")
    x = int(m.group(1))
    need(x <= MAX, f"{name} above 2^53-1")
    need(not min1 or x >= 1, f"{name} is 0")
    return x


def parse_ome(X: str):
    tags, skips = scan_tags(X)
    if not any(not t.is_end and t.name == "OME" for t in tags):
        return None
    res = {"name": None, "pixels": {}, "tiffdata": []}
    for t in tags:
        if not t.is_end and t.name == "Image":
            nm = t.attrs.get("Name")
            res["name"] = decode_refs(nm) if nm is not None else None
            break
    pi = next((i for i, t in enumerate(tags) if not t.is_end and t.name == "Pixels"), None)
    if pi is None:
        return res
    pt = tags[pi]
    a = {k: decode_refs(v) for k, v in pt.attrs.items()}
    px = res["pixels"]
    for k in ("SizeZ", "SizeC", "SizeT"):
        if k in a:
            px[k] = int_attr(a[k], k, True)
    if "DimensionOrder" in a:
        need(a["DimensionOrder"] in DIM_ORDERS, f"DimensionOrder {a['DimensionOrder']!r}")
        px["DimensionOrder"] = a["DimensionOrder"]
    for ax in "XYZ":
        v = a.get(f"PhysicalSize{ax}")
        if v is not None and FLOAT_RE.fullmatch(v):
            f = float(v)
            if math.isfinite(f) and f > 0:
                px[f"PhysicalSize{ax}"] = f
        u = a.get(f"PhysicalSize{ax}Unit")
        if u is not None:
            px[f"PhysicalSize{ax}Unit"] = u
    if pt.self_closing:
        return res
    # TiffData tags up to the first Pixels end tag
    end = len(tags)
    for i in range(pi + 1, len(tags)):
        if tags[i].is_end and tags[i].name == "Pixels":
            end = i
            break
    for i in range(pi + 1, end):
        t = tags[i]
        if t.is_end or t.name != "TiffData":
            continue
        ta = {k: decode_refs(v) for k, v in t.attrs.items()}
        td = {}
        for k in ("IFD", "FirstZ", "FirstC", "FirstT", "PlaneCount"):
            if k in ta:
                td[k] = int_attr(ta[k], k, k == "PlaneCount")
        td["_file"] = None
        if not t.self_closing:
            for k in range(i + 1, len(tags)):
                u = tags[k]
                if (u.is_end and u.name in ("TiffData", "Pixels")) or (not u.is_end and u.name == "TiffData"):
                    break
                if not u.is_end and u.name == "UUID":
                    if "FileName" in u.attrs:
                        td["_file"] = decode_refs(u.attrs["FileName"])
                    elif u.self_closing:
                        td["_file"] = ""
                    else:
                        stop = tags[k + 1].start if k + 1 < len(tags) else len(X)
                        td["_file"] = uuid_text(X, u.end, stop, skips)
                    break
        res["tiffdata"].append(td)
    return res


def uuid_text(X, a, b, skips):
    parts = []
    pos = a
    for s, e in skips:
        if e <= a or s >= b:
            continue
        if s > pos:
            parts.append(X[pos:s])
        pos = max(pos, e)
    if pos < b:
        parts.append(X[pos:b])
    return decode_refs("".join(parts)).strip(WS)


# --------------------------------------------------------------------------
# ND2 profile (§4)

CHUNK_MAGIC = 0x0ABECEDA
SIG_NAME = b"ND2 FILE SIGNATURE CHUNK NAME01!"
MAP_SIG = b"ND2 CHUNK MAP SIGNATURE 0000001!"
FILEMAP_NAME = b"ND2 FILEMAP SIGNATURE NAME 0001!"


class LVObj:
    __slots__ = ("m",)

    def __init__(self, m):
        self.m = m


class LVList:
    __slots__ = ("items",)

    def __init__(self, items):
        self.items = items


class Scalar:
    __slots__ = ("t", "v")

    def __init__(self, t, v):
        self.t = t
        self.v = v


def utf16(units_bytes: bytes) -> str:
    units = struct.unpack(f"<{len(units_bytes) // 2}H", units_bytes)
    out = []
    i = 0
    n = len(units)
    while i < n:
        u = units[i]
        if 0xD800 <= u <= 0xDBFF and i + 1 < n and 0xDC00 <= units[i + 1] <= 0xDFFF:
            out.append(chr(0x10000 + ((u - 0xD800) << 10) + (units[i + 1] - 0xDC00)))
            i += 2
            continue
        out.append("�" if 0xD800 <= u <= 0xDFFF else chr(u))
        i += 1
    return "".join(out)


MAX_DEPTH = 100


class LVParser:
    def __init__(self, data: bytes):
        self.d = data
        self.n = len(data)

    def take(self, pos, k):
        need(pos + k <= self.n, "LV record runs past the end of the data")
        return self.d[pos : pos + k]

    def record(self, pos, depth):
        need(depth <= MAX_DEPTH, "LV levels nested more than 100 deep")
        start = pos
        typ, k = self.take(pos, 2)
        pos += 2
        nb = self.take(pos, 2 * k)
        pos += 2 * k
        z = 0
        while z < k and nb[2 * z : 2 * z + 2] != b"\0\0":
            z += 1
        name = utf16(nb[: 2 * z])
        if typ == 1:
            v = Scalar(1, self.take(pos, 1)[0])
            pos += 1
        elif typ in (2, 3):
            v = Scalar(typ, struct.unpack("<i" if typ == 2 else "<I", self.take(pos, 4))[0])
            pos += 4
        elif typ in (4, 5, 7):
            v = Scalar(typ, struct.unpack("<q" if typ == 4 else "<Q", self.take(pos, 8))[0])
            pos += 8
        elif typ == 6:
            v = Scalar(6, struct.unpack("<d", self.take(pos, 8))[0])
            pos += 8
        elif typ == 8:
            e = pos
            while True:
                need(e + 2 <= self.n, "LV string without terminator")
                if self.d[e] == 0 and self.d[e + 1] == 0:
                    break
                e += 2
            v = Scalar(8, utf16(self.d[pos:e]))
            pos = e + 2
        elif typ == 9:
            b = struct.unpack("<Q", self.take(pos, 8))[0]
            pos += 8
            v = LVList([Scalar(3, x) for x in self.take(pos, b)])
            pos += b
        elif typ == 11:
            c, L = struct.unpack("<IQ", self.take(pos, 12))
            pos += 12
            recs = []
            for _ in range(c):
                pos, r = self.record(pos, depth + 1)
                recs.append(r)
            need(pos == start + L, "LV level length mismatch")
            self.take(pos, 8 * c)
            pos += 8 * c
            v = make_level(recs)
        else:
            raise Reject(f"LV record type {typ}")
        return pos, (name, v)

    def top(self):
        pos = 0
        recs = []
        while pos < self.n:
            pos, r = self.record(pos, 0)
            recs.append(r)
        m = {}
        for name, v in recs:
            m[name] = v
        return LVObj(m)


def make_level(recs):
    if recs and all(name == "" for name, _ in recs):
        return LVList([v for _, v in recs])
    m = {}
    for name, v in recs:
        m[name] = v
    return LVObj(m)


def parse_lv_chunk(data: bytes) -> LVObj:
    if data[:1] == b"\x4c":
        need(len(data) >= 12, "compressed LV record too short")
        d = zlib.decompressobj()
        try:
            inner = d.decompress(data[12:])
        except zlib.error as e:
            raise Reject(f"bad zlib stream in LV chunk: {e}")
        need(d.eof and not d.unused_data, "zlib stream does not end at the end of the chunk")
        need(inner[:1] != b"\x4c", "nested compressed LV record")
        return LVParser(inner).top()
    return LVParser(data).top()


MISSING = object()
REQUIRED = object()


def get(obj, path, default=REQUIRED):
    cur = obj
    for step in path.split("/"):
        need(isinstance(cur, LVObj), f"path step {step!r} from a non-object")
        if step not in cur.m:
            need(default is not REQUIRED, f"required member {path} missing")
            return default
        cur = cur.m[step]
    return cur


def as_number(v, what):
    need(isinstance(v, Scalar) and 2 <= v.t <= 6, f"{what} is not a number")
    x = float(v.v)
    need(math.isfinite(x), f"{what} is not finite")
    return x


def as_integer(v, what):
    x = as_number(v, what)
    need(x == int(x) and 0 <= x <= MAX, f"{what} is not an integer in range")
    return int(x)


def as_color(v, what):
    x = as_number(v, what)
    need(x == int(x) and -(2**31) <= x <= 2**32 - 1, f"{what} is not a color")
    return int(x) % 2**32


def as_flag(v, what):
    need(isinstance(v, Scalar) and 1 <= v.t <= 5, f"{what} is not a flag")
    return v.v != 0


def as_string(v, what):
    need(isinstance(v, Scalar) and v.t == 8, f"{what} is not a string")
    return v.v


def as_object(v, what):
    need(isinstance(v, LVObj), f"{what} is not an object")
    return v


def as_list(v, what):
    need(isinstance(v, LVList), f"{what} is not a list")
    return v


def members(v):
    return list(v.m.values()) if isinstance(v, LVObj) else list(v.items)


def opt(obj, path, conv, default):
    v = get(obj, path, MISSING)
    if v is MISSING:
        return default
    return conv(v, path)


def as_obj_or_list(v, what):
    need(isinstance(v, (LVObj, LVList)), f"{what} is not an object or a list")
    return v


def validity(lst, what):
    """Return a function i -> valid, given an optional validity list."""
    if lst is MISSING:
        return lambda i: True
    flags = [as_flag(x, what) for x in members(as_list(lst, what))]
    return lambda i: i < len(flags) and flags[i]


class Node:
    pass


def check_node(obj) -> Node:
    node = Node()
    as_object(obj, "experiment node")
    node.etype = as_integer(get(obj, "eType"), "eType")
    need(node.etype in (1, 2, 4, 6, 8), f"eType {node.etype}")
    pars = get(obj, "uLoopPars", MISSING)
    item_valid = validity(get(obj, "pItemValid", MISSING), "pItemValid")
    nxt = get(obj, "ppNextLevelEx", MISSING)
    node.children = []
    if nxt is not MISSING:
        for ch in members(as_obj_or_list(nxt, "ppNextLevelEx")):
            node.children.append(check_node(as_object(ch, "experiment child")))
    node.has_pars = pars is not MISSING
    node.count = 0
    node.value = 0.0
    et = node.etype
    node.kind = {1: "time", 8: "time", 2: "position", 4: "z", 6: "spectral"}[et]
    if not node.has_pars:
        return node
    as_object(pars, "uLoopPars")
    if et == 1:
        node.count = opt(pars, "uiCount", as_integer, 0)
        node.value = opt(pars, "dPeriod", as_number, 0.0)
    elif et == 8:
        pp = get(pars, "pPeriod", MISSING)
        valid = validity(get(pars, "pPeriodValid", MISSING), "pPeriodValid")
        total = 0
        period = None
        if pp is not MISSING:
            for i, p in enumerate(members(as_obj_or_list(pp, "pPeriod"))):
                as_object(p, "pPeriod member")
                if not valid(i):
                    continue
                total += as_integer(get(p, "uiCount"), "pPeriod/uiCount")
                d = opt(p, "dPeriod", as_number, 0.0)
                if period is None:
                    period = d
        need(total <= MAX, "time loop count above 2^53-1")
        node.count = total
        node.value = period if period is not None else 0.0
    elif et == 2:
        pts = get(pars, "Points", MISSING)
        if pts is not MISSING:
            node.count = sum(1 for i, _ in enumerate(members(as_obj_or_list(pts, "Points"))) if item_valid(i))
    elif et == 4:
        node.count = opt(pars, "uiCount", as_integer, 0)
        st = opt(pars, "dZStep", as_number, 0.0)
        lo = opt(pars, "dZLow", as_number, 0.0)
        hi = opt(pars, "dZHigh", as_number, 0.0)
        step = abs(st)
        if step == 0 and node.count > 1:
            step = finite(abs(hi - lo), "z range") / (node.count - 1)
        node.value = step
    elif et == 6:
        c = get(pars, "uiCount", MISSING)
        if c is not MISSING:
            node.count = as_integer(c, "uiCount")
        else:
            node.count = opt(pars, "pPlanes/uiCount", as_integer, 0)
    return node


def flatten(root: Node):
    loops = []  # [kind, depth, count, value]

    def visit(node, depth):
        if not node.has_pars or node.count == 0:
            return
        if node.kind == "spectral":
            for ch in node.children:
                visit(ch, depth)
            return
        loop = [node.kind, depth, node.count, node.value]
        if not loops or loops[-1][1] < depth:
            loops.append(loop)
        elif loops[-1][1] == depth and loops[-1][0] == node.kind and loops[-1][2] < node.count:
            loops[-1] = loop
        for ch in node.children:
            visit(ch, depth + 1)

    visit(root, 0)
    kinds = [l[0] for l in loops]
    need(len(set(kinds)) == len(kinds), "two loops of the same kind")
    return loops


class ND2:
    def __init__(self, src: Source, out: Output):
        self.src = src
        self.out = out

    def header(self, o):
        need(o <= MAX, "chunk offset above 2^53-1")
        magic, n, d = struct.unpack("<IIQ", self.src.read(o, 16))
        need(magic == CHUNK_MAGIC, f"no chunk magic at {o}")
        need(d <= MAX, "chunk data length above 2^53-1")
        return n, d

    def chunk_data(self, o):
        n, d = self.header(o)
        return self.src.read(o + 16 + n, d)

    def run(self):
        src = self.src
        n, d = self.header(0)
        need(n == 32 and d == 64, "signature chunk sizes")
        need(src.read(16, 32) == SIG_NAME, "signature chunk name")
        sig = src.read(48, 64)
        m = re.match(rb"Ver([0-9]+)\.", sig)
        need(m is not None, "signature version")
        need(int(m.group(1)) >= 3, "ND2 version below 3")

        need(src.size >= 40, "file shorter than 40 bytes")
        tail = src.read(src.size - 40, 40)
        need(tail[:32] == MAP_SIG, "chunk map signature")
        mo = struct.unpack("<Q", tail[32:])[0]
        n, d = self.header(mo)
        name = src.read(mo + 16, n)
        z = name.find(b"\0")
        need((name if z < 0 else name[:z]) == FILEMAP_NAME, "chunk map name")
        data = src.read(mo + 16 + n, d)
        cmap: dict[bytes, int] = {}
        pos = 0
        while True:
            e = data.find(b"!", pos)
            need(e >= 0, "chunk map without its last record")
            nm = data[pos : e + 1]
            if nm == MAP_SIG:
                break
            need(e + 17 <= len(data), "chunk map record runs past the data")
            cmap[nm] = struct.unpack("<Q", data[e + 1 : e + 9])[0]
            pos = e + 17
        self.cmap = cmap

        # §4.3 attributes
        need(b"ImageAttributesLV!" in cmap, "no ImageAttributesLV! chunk")
        attrs_top = parse_lv_chunk(self.chunk_data(cmap[b"ImageAttributesLV!"]))
        A = as_object(get(attrs_top, "SLxImageAttributes"), "SLxImageAttributes")
        W = as_integer(get(A, "uiWidth"), "uiWidth")
        H = as_integer(get(A, "uiHeight"), "uiHeight")
        WB = as_integer(get(A, "uiWidthBytes"), "uiWidthBytes")
        comp = as_integer(get(A, "uiComp"), "uiComp")
        bpc = as_integer(get(A, "uiBpcInMemory"), "uiBpcInMemory")
        need(W >= 1 and H >= 1 and comp >= 1, "uiWidth/uiHeight/uiComp is 0")
        sig_bits = as_number(get(A, "uiBpcSignificant"), "uiBpcSignificant")
        ecomp = opt(A, "eCompression", as_integer, 2)
        tw = opt(A, "uiTileWidth", as_integer, 0)
        th = opt(A, "uiTileHeight", as_integer, 0)
        need(bpc in (8, 16, 32), f"uiBpcInMemory {bpc}")
        dtype = {8: "uint8", 16: "uint16", 32: "float32"}[bpc]
        need(ecomp in (0, 2), f"eCompression {ecomp}")
        compressed = ecomp == 0
        need(not (tw > 0 and tw != W) and not (th > 0 and th != H), "tiled ND2")

        # experiment
        loops = []
        if b"ImageMetadataLV!" in cmap:
            top = parse_lv_chunk(self.chunk_data(cmap[b"ImageMetadataLV!"]))
            exp = get(top, "SLxExperiment", MISSING)
            if exp is not MISSING:
                loops = flatten(check_node(as_object(exp, "SLxExperiment")))

        # picture metadata
        calibrated = False
        dcal = None
        aspect = 1.0
        planes = None  # (uiCount, {i: (desc, color, compcount)})
        if b"ImageMetadataSeqLV|0!" in cmap:
            top = parse_lv_chunk(self.chunk_data(cmap[b"ImageMetadataSeqLV|0!"]))
            pm = get(top, "SLxPictureMetadata", MISSING)
            if pm is not MISSING:
                as_object(pm, "SLxPictureMetadata")
                bcal = opt(pm, "bCalibrated", as_flag, False)
                dcal = opt(pm, "dCalibration", as_number, None)
                aspect = opt(pm, "dAspect", as_number, 1.0)
                sp = get(pm, "sPicturePlanes", MISSING)
                if sp is not MISSING:
                    as_object(sp, "sPicturePlanes")
                    cnt = opt(sp, "uiCount", as_integer, 0)
                    pn = get(sp, "sPlaneNew", MISSING)
                    pl = {}
                    if pn is not MISSING:
                        as_object(pn, "sPlaneNew")
                        for k, v in pn.m.items():
                            mm = re.fullmatch(r"a(0|[1-9][0-9]*)", k)
                            if not mm or int(mm.group(1)) >= cnt:
                                continue
                            as_object(v, k)
                            pl[int(mm.group(1))] = (
                                opt(v, "sDescription", as_string, ""),
                                opt(v, "uiColor", as_color, 0xFFFFFF),
                                opt(v, "uiCompCount", as_integer, 1),
                            )
                    planes = (cnt, pl)
                calibrated = bcal and dcal is not None and dcal > 0
                if not aspect > 0:
                    aspect = 1.0

        # §4.4 frames
        counts = [l[2] for l in loops]
        N = 1
        for c in counts:
            N *= c
        need(N <= MAX, "frame count above 2^53-1")
        R = W * comp * bpc // 8
        need(WB >= R, "uiWidthBytes below the row size")
        frames = {}
        for nm, o in cmap.items():
            mm = re.fullmatch(rb"ImageDataSeq\|(0|[1-9][0-9]*)!", nm)
            if mm:
                f = int(mm.group(1))
                if f < N:
                    frames[f] = o
        present = sorted(frames)
        franges: dict[int, list] = {}
        h = H
        if not compressed:
            if present:
                lo, hi = present[0], present[-1]
                n_lo, d_lo = self.header(frames[lo])
                n_hi, d_hi = self.header(frames[hi])
                need(n_lo == n_hi, "frame header name lengths differ")
                need(d_lo >= 8 + H * WB and d_hi >= 8 + H * WB, "frame data too short")
                nn = n_lo
                if WB == R:
                    for f in present:
                        start = frames[f] + 16 + nn + 8
                        franges[f] = [[(0, start, H * R)]]
                else:
                    starts = {f: frames[f] + 16 + nn + 8 for f in present}
                    fmax = max(present, key=lambda f: starts[f])
                    smax = starts[fmax]
                    best = 1
                    # every row costs at least 6 payload bytes, so larger blocks never fit
                    for cand in range(min(H, MAX_PAYLOAD // 6), 0, -1):
                        if H % cand:
                            continue
                        rows = [(0, smax + r * WB, R) for r in range(H - cand, H)]
                        if payload_len(rows) <= MAX_PAYLOAD:
                            best = cand
                            break
                    h = best
                    for f in present:
                        s = starts[f]
                        franges[f] = [
                            [(0, s + r * WB, R) for r in range(j * h, j * h + h)] for j in range(H // h)
                        ]
        else:
            need(WB == R, "compressed frames with padded rows")
            for f in present:
                o = frames[f]
                nn, dd = self.header(o)
                need(dd > 8, "compressed frame without data")
                franges[f] = [[(0, o + 16 + nn + 8, dd - 8)]]

        # §4.5 channels
        channels = None
        if planes is not None and planes[0] >= 1:
            cnt, pl = planes
            if all(i in pl for i in range(cnt)) and all(pl[i][2] in (1, 3) for i in range(cnt)) and sum(
                pl[i][2] for i in range(cnt)
            ) == comp:
                channels = []
                for i in range(cnt):
                    desc, color, cc = pl[i]
                    if cc == 1:
                        r, g, b = color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF
                        channels.append((desc, f"{r:02X}{g:02X}{b:02X}"))
                    else:
                        channels += [(desc + " R", "FF0000"), (desc + " G", "00FF00"), (desc + " B", "0000FF")]
        if channels is None:
            channels = [(f"C{k}", "FFFFFF") for k in range(comp)]

        # §4.6 output
        kinds = [l[0] for l in loops]
        loop_of = {l[0]: l for l in loops}
        dims = []
        if "time" in loop_of:
            dims.append("t")
        if comp > 1:
            dims.append("c")
        if "z" in loop_of:
            dims.append("z")
        dims += ["y", "x"]
        shape = {"y": H, "x": W, "c": comp}
        if "time" in loop_of:
            shape["t"] = loop_of["time"][2]
        if "z" in loop_of:
            shape["z"] = loop_of["z"][2]
        chunk = {"t": 1, "z": 1, "c": comp, "y": h, "x": W}
        codecs = []
        if "c" in dims:
            codecs.append(transpose_codec(dims))
        codecs.append(bytes_codec(bpc // 8, "little"))
        if compressed:
            codecs.append({"name": "zlib", "configuration": {"level": 1}})
        units = {}
        scale = {"t": 1, "c": 1, "z": 1, "y": 1, "x": 1}
        if calibrated:
            units["x"] = units["y"] = "micrometer"
            scale["x"] = dcal
            scale["y"] = finite(dcal * aspect, "y scale")
        if "z" in loop_of and loop_of["z"][3] > 0:
            units["z"] = "micrometer"
            scale["z"] = loop_of["z"][3]
        if "time" in loop_of and loop_of["time"][3] > 0:
            units["t"] = "second"
            scale["t"] = loop_of["time"][3] / 1000
        b = bpc
        if sig_bits == int(sig_bits) and 1 <= sig_bits <= bpc:
            b = int(sig_bits)
        chan_json = []
        for label, color in channels:
            cj = {"label": label, "color": color, "active": True}
            if dtype != "float32":
                V = 2**b - 1
                cj["window"] = {"min": 0, "max": V, "start": 0, "end": V}
            chan_json.append(cj)

        npos = loop_of["position"][2] if "position" in loop_of else 1
        out = self.out
        out.json("zarr.json", {"zarr_format": 3, "node_type": "group", "attributes": {"ome": {"version": "0.5", "bioformats2raw.layout": 3}}})
        out.json(
            "OME/zarr.json",
            {"zarr_format": 3, "node_type": "group", "attributes": {"ome": {"version": "0.5", "series": [str(p) for p in range(npos)]}}},
        )
        arr = array_json([shape[d] for d in dims], dtype, [chunk[d] for d in dims], codecs, dims)
        for p in range(npos):
            M = {
                "version": "0.5",
                "multiscales": [
                    {
                        "name": f"position {p}",
                        "axes": [axis_json(d, units.get(d)) for d in dims],
                        "datasets": [
                            {"path": "0", "coordinateTransformations": [{"type": "scale", "scale": [scale[d] for d in dims]}]}
                        ],
                    }
                ],
                "omero": {"channels": chan_json},
            }
            out.json(f"{p}/zarr.json", {"zarr_format": 3, "node_type": "group", "attributes": {"ome": M}})
            out.json(f"{p}/0/zarr.json", arr)
        for f in present:
            rem = f
            coord = {}
            for k, cnt in zip(reversed(kinds), reversed(counts)):
                coord[k] = rem % cnt
                rem //= cnt
            p = coord.get("position", 0)
            pre = []
            if "t" in dims:
                pre.append(coord["time"])
            if "c" in dims:
                pre.append(0)
            if "z" in dims:
                pre.append(coord["z"])
            for j, rng in enumerate(franges[f]):
                key = f"{p}/0/c/" + "/".join(str(x) for x in pre + [j, 0])
                out.ref(key, rng)


# --------------------------------------------------------------------------


def main(argv):
    if len(argv) != 3:
        print("usage: virtualize <url> <out.json>", file=sys.stderr)
        return 2
    url, path = argv[1], argv[2]
    try:
        src = Source(url)
        out = Output(url, src.size)
        head = src.read(0, min(src.size, 12)) if src.size else b""
        if head[:4] in (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+"):
            Tiff(src, out).run()
        elif head[:4] == b"\xda\xce\xbe\x0a":
            ND2(src, out).run()
        else:
            raise Reject("unrecognized file type")
        text = out.dump()
    except Reject as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 3
    except ReadError as e:
        print(f"read failed: {e}", file=sys.stderr)
        return 1
    with open(path, "w") as fh:
        fh.write(text)
    nrefs = sum(1 for v in out.entries.values() if "ranges" in v)
    print(f"ok: {len(out.entries)} entries, {nrefs} references")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
