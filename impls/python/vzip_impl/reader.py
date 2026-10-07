"""vzip reader (spec §3, §4, §7, §8)."""

import bisect
import os
import re
import stat
import struct
import zlib

from . import pb
from . import uri as urimod
from .errors import VzError

HIDDEN = b"__vz__/"
SOURCES_KEY = b"__vz__/sources"
INDEX_KEY = b"__vz__/index"
FORMAT_KEYS = (SOURCES_KEY, INDEX_KEY)
EOCD_SIG = 0x06054B50
Z64_EOCD_SIG = 0x06064B50
Z64_LOC_SIG = 0x07064B50
CD_SIG = 0x02014B50
ID_RANGE = 0x7A76
ID_CONCAT = 0x7A77
MAX_PAYLOAD = 65519
MAX64 = (1 << 64) - 1

# Documented resource limit (spec §10): a single request (window of a value,
# inflated body, or HTTP response body) may use at most this many bytes.
DEFAULT_LIMIT = 1 << 30

ETAG_RE = re.compile(rb'"[\x21\x23-\x7e]*"')


def _arch(msg):
    return VzError("archive", msg)


def _entry(msg):
    return VzError("entry", msg)


def _body(msg):
    return VzError("body", msg)


def _payload(msg):
    return VzError("payload", msg)


def _res(msg):
    return VzError("resolution", msg)


def inflate_clean(data, expected, limit, errcls):
    """Inflate a raw DEFLATE body that must be exactly one complete stream.

    expected: required uncompressed size, or None.
    """
    cap = expected if expected is not None else limit
    if cap > limit:
        raise VzError("request", "inflated size %d exceeds the resource limit" % cap)
    d = zlib.decompressobj(-15)
    try:
        out = d.decompress(data, cap + 1)
    except zlib.error as e:
        raise VzError(errcls, "DEFLATE body is corrupt: %s" % e)
    if len(out) > cap:
        if expected is None:
            raise VzError("request", "inflated size exceeds the resource limit")
        raise VzError(errcls, "DEFLATE body inflates to more than %d bytes" % expected)
    if not d.eof:
        raise VzError(errcls, "DEFLATE body is truncated (stream does not end)")
    if d.unused_data or d.unconsumed_tail:
        raise VzError(errcls, "DEFLATE body has trailing bytes after the stream")
    if expected is not None and len(out) != expected:
        raise VzError(errcls, "DEFLATE body inflates to %d bytes, expected %d" % (len(out), expected))
    return out


class Rec:
    """A central directory record, structurally parsed."""
    __slots__ = ("name", "flags", "method", "crc", "csize", "usize", "offset", "extra")

    def __init__(self, name, flags, method, crc, csize, usize, offset, extra):
        self.name = name
        self.flags = flags
        self.method = method
        self.crc = crc
        self.csize = csize
        self.usize = usize
        self.offset = offset
        self.extra = extra


def parse_records(buf):
    """Parse a sequence of whole CD records exactly filling buf. Raises ValueError."""
    pos = 0
    n = len(buf)
    recs = []
    while pos < n:
        if n - pos < 46:
            raise ValueError("truncated central directory record at %d" % pos)
        (sig, _vm, _vn, flags, method, _t, _d, crc, csize, usize, nl, el, cl,
         _disk, _ia, _ea, off) = struct.unpack_from("<IHHHHHHIIIHHHHHII", buf, pos)
        if sig != CD_SIG:
            raise ValueError("bad central directory record signature at %d" % pos)
        total = 46 + nl + el + cl
        if pos + total > n:
            raise ValueError("central directory record at %d overruns" % pos)
        name = bytes(buf[pos + 46:pos + 46 + nl])
        extra = bytes(buf[pos + 46 + nl:pos + 46 + nl + el])
        recs.append(Rec(name, flags, method, crc, csize, usize, off, extra))
        pos += total
    return recs


def valid_key_bytes(name):
    if not name:
        return False
    try:
        name.decode("utf-8", "strict")
        return True
    except UnicodeDecodeError:
        return False


class Entry:
    __slots__ = ("kind", "method", "body_off", "csize", "usize", "ref_id", "payload")

    def __init__(self, kind, method, body_off, csize, usize, ref_id=None, payload=None):
        self.kind = kind
        self.method = method
        self.body_off = body_off
        self.csize = csize
        self.usize = usize
        self.ref_id = ref_id
        self.payload = payload


def analyze(rec):
    """Classify a record and compute its body location; raises entry errors (§4.1, §3.2, §8.4)."""
    blocks = []
    ex = rec.extra
    pos = 0
    while pos < len(ex):
        if len(ex) - pos < 4:
            raise _entry("extra field does not parse")
        hid, sz = struct.unpack_from("<HH", ex, pos)
        if pos + 4 + sz > len(ex):
            raise _entry("extra field does not parse")
        blocks.append((hid, ex[pos + 4:pos + 4 + sz]))
        pos += 4 + sz
    refs = [b for b in blocks if b[0] in (ID_RANGE, ID_CONCAT)]
    if len(refs) > 1:
        raise _entry("record has %d reference blocks" % len(refs))
    z64 = [b for b in blocks if b[0] == 0x0001]
    if len(z64) > 1:
        raise _entry("record has more than one ZIP64 extra block")
    # §3.2: an entry is large when both size fields are all ones; its sizes, then an
    # offset of all ones, come from the one ZIP64 block, in that order
    if (rec.csize == 0xFFFFFFFF) != (rec.usize == 0xFFFFFFFF):
        raise _entry("exactly one size field is 0xFFFFFFFF")
    large = rec.csize == 0xFFFFFFFF
    csize, usize, off = rec.csize, rec.usize, rec.offset
    need = (16 if large else 0) + (8 if off == 0xFFFFFFFF else 0)
    if need:
        if not z64 or len(z64[0][1]) < need:
            raise _entry("ZIP64 extra block missing or shorter than %d bytes" % need)
        blk = z64[0][1]
        if large:
            usize, csize = struct.unpack_from("<QQ", blk, 0)
        if off == 0xFFFFFFFF:
            off = struct.unpack_from("<Q", blk, 16 if large else 0)[0]
    if rec.flags & 1:
        raise _entry("entry is encrypted")
    if rec.method not in (0, 8):
        raise _entry("unsupported compression method %d" % rec.method)
    if large and rec.method != 0:
        raise _entry("large entry uses method %d" % rec.method)
    if refs:
        if rec.method != 0:
            raise _entry("reference entry uses method %d" % rec.method)
        if large:
            raise _entry("reference entry is large")
        return Entry("reference", 0, off + 30 + len(rec.name), csize, usize,
                     refs[0][0], refs[0][1])
    return Entry("bytes", rec.method, off + 30 + len(rec.name) + (20 if large else 0), csize, usize)


class Part:
    __slots__ = ("source", "offset", "length", "data", "size")

    def __init__(self, source, offset, length, data):
        self.source = source
        self.offset = offset
        self.length = length
        self.data = data
        self.size = len(data) if data is not None else length


def decode_payload(ref_id, payload, nsources):
    """Decode and check a reference payload (§4.3, §5.2, §5.3). Raises payload errors."""
    if len(payload) > MAX_PAYLOAD:
        raise _payload("reference payload is %d bytes, more than %d" % (len(payload), MAX_PAYLOAD))
    try:
        if ref_id == ID_RANGE:
            ranges = [pb.decode(payload, pb.RANGE)]
        else:
            ranges = pb.decode(payload, pb.CONCAT).get("parts", [])
    except pb.Malformed as e:
        raise _payload("malformed reference payload: %s" % e)
    parts = []
    total = 0
    for r in ranges:
        src, off, ln, data = r.get("source", 0), r.get("offset", 0), r.get("length", 0), r.get("data")
        if data is not None:
            if src or off or ln:
                raise _payload("literal range has non-zero source/offset/length")
        else:
            if src >= nsources:
                raise _payload("range source %d out of bounds (%d sources)" % (src, nsources))
            if off + ln > MAX64:
                raise _payload("range offset + length exceeds 2^64-1")
        p = Part(src, off, ln, data)
        total += p.size
        parts.append(p)
    if total > MAX64:
        raise _payload("reference size exceeds 2^64-1")
    return parts, total


def window(n, req):
    kind = req[0]
    if kind == "whole":
        return 0, n
    if kind == "range":
        return min(req[1], n), min(req[2], n)
    if kind == "offset":
        return min(req[1], n), n
    if kind == "suffix":
        return max(n - req[1], 0), n
    raise ValueError(req)


class Source:
    __slots__ = ("kind", "value", "size", "etag", "mnf")

    def __init__(self, kind, value, size, etag, mnf):
        self.kind = kind
        self.value = value
        self.size = size
        self.etag = etag
        self.mnf = mnf


class Archive:
    def __init__(self, path, base_uri=None, limit=DEFAULT_LIMIT):
        self.limit = limit
        self._fd = None
        try:
            self._fd = os.open(path, os.O_RDONLY)
            st = os.fstat(self._fd)
            if stat.S_ISDIR(st.st_mode):
                raise _arch("archive path is a directory")
            self.file_size = st.st_size
        except OSError as e:
            self.close()
            raise _arch("cannot open archive: %s" % e)
        self.base_uri = base_uri if base_uri is not None else urimod.path_to_file_uri(path)
        self._value_cache = {}
        self._page_cache = {}
        try:
            self._open()
        except VzError as e:
            self.close()
            if e.cls != "archive":
                raise VzError("archive", str(e))
            raise
        except OSError as e:
            self.close()
            raise _arch("I/O error while opening: %s" % e)

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _pread(self, off, n):
        out = bytearray()
        while len(out) < n:
            chunk = os.pread(self._fd, n - len(out), off + len(out))
            if not chunk:
                raise OSError("unexpected end of file")
            out += chunk
        return bytes(out)

    def _within(self, off, size):
        return off + size <= self.file_size

    # ------------------------------------------------------------ open (§8.1)

    def _open(self):
        fs = self.file_size
        eocd = None
        clen = None
        if fs >= 60:
            b = self._pread(fs - 60, 22)
            if struct.unpack_from("<I", b)[0] == EOCD_SIG and struct.unpack_from("<H", b, 20)[0] == 38:
                eocd, clen = fs - 60, 38
        if eocd is None and fs >= 44:
            b = self._pread(fs - 44, 22)
            if struct.unpack_from("<I", b)[0] == EOCD_SIG and struct.unpack_from("<H", b, 20)[0] == 22:
                eocd, clen = fs - 44, 22
        if eocd is None:
            raise _arch("not a vzip archive: no end of central directory record with a vzip comment")
        rec = self._pread(eocd, 22 + clen)
        comment = rec[22:]
        if comment[:5] != b"vzip/":
            raise _arch("not a vzip archive: comment does not start with vzip/")
        if comment[5:6] != b"0":
            raise _arch("unsupported vzip format version %r" % comment[5:6])
        self.sources_offset, self.sources_size = struct.unpack_from("<QQ", comment, 6)
        self.paged = clen == 38
        if self.paged:
            self.index_offset, self.index_size = struct.unpack_from("<QQ", comment, 22)

        # §3.2: the directory's size and offset always come from the zip64 record; the
        # end record's own counts, size and offset are ignored
        if eocd < 20:
            raise _arch("zip64 end of central directory locator missing")
        loc = self._pread(eocd - 20, 20)
        lsig, _ldisk, z64off, _ndisks = struct.unpack("<IIQI", loc)
        if lsig != Z64_LOC_SIG:
            raise _arch("zip64 end of central directory locator missing")
        if not self._within(z64off, 56):
            raise _arch("zip64 end of central directory record outside the file")
        z = self._pread(z64off, 56)
        (zsig, zsize, _vm, _vn, _dk, _cdk, _n1, _n2, cd_size, cd_off) = struct.unpack("<IQHHIIQQQQ", z)
        if zsig != Z64_EOCD_SIG:
            raise _arch("bad zip64 end of central directory record signature")
        if zsize != 44:
            raise _arch("zip64 end of central directory record size is %d, not 44" % zsize)
        if not self._within(cd_off, cd_size):
            raise _arch("central directory lies outside the file")
        self.cd_off, self.cd_size = cd_off, cd_size

        # format entries
        if not self._within(self.sources_offset, self.sources_size):
            raise _arch("__vz__/sources body lies outside the file")
        self.sources_raw = inflate_clean(self._pread(self.sources_offset, self.sources_size),
                                         None, self.limit, "archive")
        self.sources = self._parse_sources(self.sources_raw)
        if self.paged:
            if not self._within(self.index_offset, self.index_size):
                raise _arch("__vz__/index body lies outside the file")
            self.index_raw = inflate_clean(self._pread(self.index_offset, self.index_size),
                                           None, self.limit, "archive")
            self._parse_index(self.index_raw)
        else:
            self.index_raw = None
            try:
                recs = parse_records(self._pread(cd_off, cd_size))
            except ValueError as e:
                raise _arch("central directory does not parse: %s" % e)
            self.records = {}
            for r in recs:
                if r.name == INDEX_KEY:
                    raise _arch("archive without a page index has an __vz__/index entry")
                if valid_key_bytes(r.name):
                    self.records.setdefault(r.name, r)

    def _parse_sources(self, raw):
        try:
            t = pb.decode(raw, pb.SOURCE_TABLE)
        except pb.Malformed as e:
            raise _arch("source table is malformed: %s" % e)
        out = []
        for i, s in enumerate(t.get("sources", [])):
            kind = s.get("kind")
            if kind is None:
                raise _arch("source %d has no kind" % i)
            k, v = kind
            if k in ("url", "key") and v == "":
                raise _arch("source %d has an empty %s" % (i, k))
            size, etag, mnf = s.get("size"), s.get("etag"), s.get("modified_not_after")
            if k != "url" and (size is not None or etag is not None or mnf is not None):
                raise _arch("source %d: pin on a %s source" % (i, k))
            if etag is not None and not ETAG_RE.fullmatch(etag.encode("utf-8")):
                raise _arch("source %d: etag pin is not a strong entity tag" % i)
            out.append(Source(k, v, size, etag, mnf))
        return out

    def _parse_index(self, raw):
        try:
            idx = pb.decode(raw, pb.CD_INDEX)
        except pb.Malformed as e:
            raise _arch("page index is malformed: %s" % e)
        self.pages = []
        expect = 0
        prev = None
        for i, p in enumerate(idx.get("pages", [])):
            fk = p.get("first_key", "").encode("utf-8")
            off, ln = p.get("offset", 0), p.get("length", 0)
            if not fk:
                raise _arch("page index is malformed: page %d has an empty first_key" % i)
            if ln == 0:
                raise _arch("page index is malformed: page %d has length 0" % i)
            if off + ln > self.cd_size:
                raise _arch("page index is malformed: page %d lies outside the central directory" % i)
            if off != expect:
                raise _arch("page index is malformed: pages are not contiguous from offset 0")
            if prev is not None and not prev < fk:
                raise _arch("page index is malformed: first_key values do not strictly increase")
            prev = fk
            expect = off + ln
            self.pages.append((fk, off, ln))
        self.first_keys = [p[0] for p in self.pages]
        self.pinned = {}
        for i, p in enumerate(idx.get("pinned", [])):
            k = p.get("key", "").encode("utf-8")
            if not k:
                raise _arch("page index is malformed: pinned entry %d has an empty key" % i)
            if k in self.pinned:
                raise _arch("page index is malformed: pinned key listed twice")
            if k in FORMAT_KEYS:
                raise _arch("page index is malformed: pinned key is a format entry")
            m = p.get("method", 0)
            if m not in (0, 8):
                raise _arch("page index is malformed: pinned method %d" % m)
            doff, csize = p.get("data_offset", 0), p.get("csize", 0)
            if m != 0 and (p.get("size", 0) >= 0xFFFFFFFF or csize >= 0xFFFFFFFF):
                raise _arch("page index is malformed: pinned large entry is not STORED")
            if not self._within(doff, csize):
                raise _arch("page index is malformed: pinned body lies outside the file")
            self.pinned[k] = Entry("bytes", m, doff, csize, p.get("size", 0))

    # ------------------------------------------------------------ lookup (§7.2)

    def _page(self, i):
        c = self._page_cache.get(i)
        if c is None:
            _fk, off, ln = self.pages[i]
            try:
                recs = parse_records(self._pread(self.cd_off + off, ln))
                d = {}
                for r in recs:
                    d.setdefault(r.name, r)
                c = d
            except (ValueError, OSError) as e:
                c = VzError("entry", "central directory page %d cannot be parsed: %s" % (i, e))
            self._page_cache[i] = c
        if isinstance(c, VzError):
            raise c
        return c

    def _lookup(self, k):
        """Return an Entry, None (missing), or raise an entry error. k must not be a format key."""
        if self.paged:
            e = self.pinned.get(k)
            if e is not None:
                return e
            i = bisect.bisect_right(self.first_keys, k) - 1
            if i < 0:
                return None
            rec = self._page(i).get(k)
        else:
            rec = self.records.get(k)
        if rec is None:
            return None
        return analyze(rec)

    # ------------------------------------------------------------ bodies

    def _check_body(self, e):
        if not self._within(e.body_off, e.csize):
            raise _body("entry body lies outside the file")
        if e.method == 0 and e.csize != e.usize:
            raise _body("STORED entry has compressed size %d != uncompressed size %d" % (e.csize, e.usize))

    def _bytes_value(self, e, a, b):
        """Bytes [a, b) of a bytes entry's value (a <= b <= usize). Raises body/request errors."""
        self._check_body(e)
        if e.method == 8:
            key = (e.body_off, e.csize, e.usize)
            v = self._value_cache.get(key)
            if v is None:
                # check before reading the compressed body, which may be 4 GiB or more
                if e.usize > self.limit or e.csize > self.limit:
                    raise VzError("request", "inflating the entry exceeds the resource limit")
                v = inflate_clean(self._pread(e.body_off, e.csize), e.usize, self.limit, "body")
                if len(self._value_cache) > 64:
                    self._value_cache.clear()
                self._value_cache[key] = v
            return v[a:b]
        if b - a > self.limit:
            raise VzError("request", "request exceeds the resource limit")
        return self._pread(e.body_off + a, b - a)

    # ------------------------------------------------------------ operations (§8.2)

    @staticmethod
    def _key_bytes(key):
        if isinstance(key, bytes):
            return key
        return key.encode("utf-8", "surrogatepass")

    @staticmethod
    def _check_request(req):
        if req[0] == "range" and req[1] > req[2]:
            raise VzError("request", "range start %d > end %d" % (req[1], req[2]))

    def classify(self, key):
        k = self._key_bytes(key)
        if k.startswith(HIDDEN):
            return "missing"
        e = self._lookup(k)
        return "missing" if e is None else e.kind

    def get(self, key, req=("whole",)):
        self._check_request(req)
        k = self._key_bytes(key)
        if k.startswith(HIDDEN):
            return None
        e = self._lookup(k)
        if e is None:
            return None
        if e.kind == "bytes":
            self._check_body(e)
            a, b = window(e.usize, req)
            return self._bytes_value(e, a, b)
        return self._get_reference(e, req)

    def raw(self, key):
        k = self._key_bytes(key)
        if k == SOURCES_KEY:
            return self.sources_raw
        if k == INDEX_KEY:
            return self.index_raw
        e = self._lookup(k)
        if e is None:
            return None
        if e.kind == "reference":
            self._check_body(e)
            if e.usize > self.limit:
                raise VzError("request", "request exceeds the resource limit")
            return self._pread(e.body_off, e.csize)
        return self._bytes_value(e, 0, e.usize)

    def list(self, prefix=""):
        p = self._key_bytes(prefix)
        keys = set()
        if not self.paged:
            for name in self.records:
                if name.startswith(p) and not name.startswith(HIDDEN):
                    keys.add(name)
        else:
            for name in self.pinned:
                if name.startswith(p) and not name.startswith(HIDDEN):
                    keys.add(name)
            n = len(self.pages)
            for i in range(n):
                lo = self.pages[i][0]
                hi = self.pages[i + 1][0] if i + 1 < n else None
                if (hi is None or p < hi) and (lo <= p or lo.startswith(p)):
                    for name in self._page(i):
                        if (valid_key_bytes(name) and name.startswith(p) and not name.startswith(HIDDEN)
                                and lo <= name and (hi is None or name < hi)):
                            keys.add(name)
        return [k.decode("utf-8") for k in sorted(keys)]

    # ------------------------------------------------------------ references (§8.3)

    def _get_reference(self, e, req):
        parts, n = decode_payload(e.ref_id, e.payload, len(self.sources))
        a, b = window(n, req)
        if b - a > self.limit:
            raise VzError("request", "request of %d bytes exceeds the resource limit" % (b - a))
        out = bytearray()
        pos = 0
        for part in parts:
            lo, hi = max(a, pos), min(b, pos + part.size)
            if lo < hi:
                i, j = lo - pos, hi - pos
                if part.data is not None:
                    out += part.data[i:j]
                else:
                    out += self._read_source(part.source, part.offset + i, part.offset + j)
            pos += part.size
        return bytes(out)

    def _read_source(self, idx, start, end):
        src = self.sources[idx]
        if src.kind == "data":
            if end > len(src.value):
                raise _res("data source %d has %d bytes, range needs %d" % (idx, len(src.value), end))
            return src.value[start:end]
        if src.kind == "key":
            kb = src.value.encode("utf-8")
            if kb in FORMAT_KEYS:
                raise _res("key source %d names a format entry" % idx)
            try:
                e = self._lookup(kb)
            except VzError as err:
                raise _res("key source %d: %s" % (idx, err))
            if e is None:
                raise _res("key source %d names a missing key" % idx)
            if e.kind != "bytes":
                raise _res("key source %d names a reference entry" % idx)
            try:
                self._check_body(e)
            except VzError as err:
                raise _res("key source %d: %s" % (idx, err))
            if e.usize < end:
                raise _res("key source %d value has %d bytes, range needs %d" % (idx, e.usize, end))
            try:
                return self._bytes_value(e, start, end)
            except VzError as err:
                if err.cls == "body":
                    raise _res("key source %d: %s" % (idx, err))
                raise
        return self._read_url(idx, src, start, end)

    def _read_url(self, idx, src, start, end):
        if not urimod.is_uri_reference(src.value):
            raise _res("source %d url is not a valid URI reference" % idx)
        t = urimod.resolve(self.base_uri, src.value)
        scheme = (t.scheme or "").lower()
        if scheme == "file":
            return self._read_file(idx, src, t, start, end)
        if scheme in ("http", "https"):
            from . import httpfetch
            return httpfetch.http_read(t, start, end, src.size, src.etag, src.mnf, self.limit)
        raise _res("source %d: unsupported URL scheme %r" % (idx, t.scheme))

    def _read_file(self, idx, src, t, start, end):
        try:
            path = urimod.file_uri_to_path(t)
        except urimod.FileMappingError as e:
            raise _res("source %d: %s" % (idx, e))
        if src.etag is not None:
            raise _res("source %d: etag pin cannot be checked for a file: URL" % idx)
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError as e:
            raise _res("source %d: cannot open %r: %s" % (idx, path, e))
        try:
            st = os.fstat(fd)
            if stat.S_ISDIR(st.st_mode):
                raise _res("source %d: %r is a directory" % (idx, path))
            if src.size is not None and st.st_size != src.size:
                raise _res("source %d: size pin failed (%d != %d)" % (idx, st.st_size, src.size))
            if src.mnf is not None and st.st_mtime_ns // 1_000_000_000 > src.mnf:
                raise _res("source %d: modified_not_after pin failed" % idx)
            if stat.S_ISREG(st.st_mode) and st.st_size < end:
                raise _res("source %d: file has %d bytes, range needs %d" % (idx, st.st_size, end))
            out = bytearray()
            while len(out) < end - start:
                chunk = os.pread(fd, end - start - len(out), start + len(out))
                if not chunk:
                    raise _res("source %d: file is shorter than the range" % idx)
                out += chunk
            return bytes(out)
        except OSError as e:
            raise _res("source %d: read failed: %s" % (idx, e))
        finally:
            os.close(fd)
