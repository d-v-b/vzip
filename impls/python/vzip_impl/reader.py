"""vzip reader (spec §3, §4, §7, §8)."""

import os
import struct
import zlib

from . import proto, uri
from .errors import (ArchiveError, BodyError, EntryError, PayloadError, RequestError,
                     ResolutionError)
from .fetch import Pins, read_url

PREFIX = b"__vz__/"
SOURCES_KEY = b"__vz__/sources"
INDEX_KEY = b"__vz__/index"
FORMAT_KEYS = (SOURCES_KEY, INDEX_KEY)
EOCD_SIG = 0x06054B50
CDR_SIG = 0x02014B50
Z64_EOCD_SIG = 0x06064B50
Z64_LOC_SIG = 0x07064B50
REF_IDS = (0x7A76, 0x7A77)
U64_MAX = (1 << 64) - 1

# Resource limits (spec §10). A request whose window, or a DEFLATE body that
# must be inflated for it, exceeds this many bytes fails with a request error.
MAX_REQUEST_BYTES = 1 << 30
# Format entries larger than this when inflated make opening fail.
MAX_FORMAT_ENTRY_BYTES = 256 << 20


def _inflate_clean(data, limit):
    """Inflate a raw DEFLATE body; return bytes or raise ValueError.

    The body must be one complete stream ending exactly at its end.
    `limit`: maximum output size; output beyond it raises OverflowError.
    """
    d = zlib.decompressobj(-15)
    try:
        out = d.decompress(data, limit + 1)
    except zlib.error as e:
        raise ValueError(f"invalid DEFLATE data: {e}") from None
    if len(out) > limit:
        raise OverflowError("inflated size exceeds limit")
    if not d.eof:
        raise ValueError("DEFLATE stream is incomplete")
    if d.unused_data or d.unconsumed_tail:
        raise ValueError("bytes follow the end of the DEFLATE stream")
    return out


class FileBlob:
    def __init__(self, path):
        try:
            self.f = open(path, "rb")
            st = os.fstat(self.f.fileno())
        except OSError as e:
            raise ArchiveError(f"cannot open archive: {e}") from None
        self.size = st.st_size

    def read(self, off, n):
        if off < 0 or off + n > self.size:
            raise ValueError("read outside the file")
        try:
            data = os.pread(self.f.fileno(), n, off)
        except OSError as e:
            raise ValueError(f"read error: {e}") from None
        if len(data) != n:
            raise ValueError("short read")
        return data

    def close(self):
        self.f.close()


class Record:
    """A central directory record, analysed lazily for entry errors."""

    __slots__ = ("name", "flags", "method", "crc", "csize", "usize", "lho", "extra",
                 "_analysed", "error", "kind", "ref_id", "payload", "body_offset")

    def __init__(self, name, flags, method, crc, csize, usize, lho, extra):
        self.name = name
        self.flags = flags
        self.method = method
        self.crc = crc
        self.csize = csize
        self.usize = usize
        self.lho = lho
        self.extra = extra
        self._analysed = False
        self.error = None

    def analyse(self):
        if self._analysed:
            return
        self._analysed = True
        blocks = []
        ex = self.extra
        pos = 0
        while pos < len(ex):
            if pos + 4 > len(ex):
                self.error = "extra field does not parse"
                return
            hid, n = struct.unpack_from("<HH", ex, pos)
            if pos + 4 + n > len(ex):
                self.error = "extra field does not parse"
                return
            blocks.append((hid, ex[pos + 4:pos + 4 + n]))
            pos += 4 + n
        refs = [b for b in blocks if b[0] in REF_IDS]
        if len(refs) > 1:
            self.error = "more than one reference block"
            return
        if self.method not in (0, 8):
            self.error = f"unsupported compression method {self.method}"
            return
        if self.flags & 1:
            self.error = "entry is encrypted"
            return
        if refs:
            if self.method != 0:
                self.error = "reference entry uses DEFLATE"
                return
            self.kind = "reference"
            self.ref_id, self.payload = refs[0]
        else:
            self.kind = "bytes"
            self.ref_id = self.payload = None
        lho = self.lho
        if lho == 0xFFFFFFFF:
            z = [b for b in blocks if b[0] == 0x0001]
            if not z:
                self.error = "local header offset 0xFFFFFFFF without a ZIP64 extra block"
                return
            if len(z) > 1:
                self.error = "more than one ZIP64 extra block"
                return
            data = z[0][1]
            vals = []
            need = [self.usize == 0xFFFFFFFF, self.csize == 0xFFFFFFFF, True]
            if len(data) < 8 * sum(need):
                self.error = "ZIP64 extra block too short"
                return
            p = 0
            for i, nd in enumerate(need):
                if nd:
                    vals.append((i, struct.unpack_from("<Q", data, p)[0]))
                    p += 8
            for i, v in vals:
                if i == 0:
                    self.usize = v
                elif i == 1:
                    self.csize = v
                else:
                    lho = v
        self.body_offset = lho + 30 + len(self.name)


class PinnedEntry:
    """A `bytes` entry described by the page index (spec §7.1)."""

    def __init__(self, name, body_offset, usize, csize, method):
        self.name = name
        self.body_offset = body_offset
        self.usize = usize
        self.csize = csize
        self.method = method
        self.kind = "bytes"
        self.error = None

    def analyse(self):
        pass


def _parse_records(buf, strict_names=False):
    """Parse a sequence of central directory records that exactly fills buf.

    Returns a list of (name_bytes, Record). Records whose name is empty or
    not valid UTF-8 are dropped.  Raises ValueError if it cannot be parsed.
    """
    out = []
    pos = 0
    n = len(buf)
    while pos < n:
        if pos + 46 > n:
            raise ValueError("truncated central directory record")
        (sig, _vm, _vn, flags, method, _t, _d, crc, csize, usize, nlen, xlen, clen,
         _disk, _ia, _ea, lho) = struct.unpack_from("<IHHHHHHIIIHHHHHII", buf, pos)
        if sig != CDR_SIG:
            raise ValueError(f"bad central directory signature at +{pos}")
        end = pos + 46 + nlen + xlen + clen
        if end > n:
            raise ValueError("central directory record overruns its container")
        name = bytes(buf[pos + 46:pos + 46 + nlen])
        extra = bytes(buf[pos + 46 + nlen:pos + 46 + nlen + xlen])
        pos = end
        if not name:
            continue
        try:
            name.decode("utf-8")
        except UnicodeDecodeError:
            continue
        out.append((name, Record(name, flags, method, crc, csize, usize, lho, extra)))
    return out


def _utf8(key):
    if isinstance(key, bytes):
        return key
    return key.encode("utf-8", "surrogatepass")


def _prefix_upper(p):
    """Smallest byte string greater than every string that starts with p, or None."""
    b = bytearray(p)
    while b and b[-1] == 0xFF:
        b.pop()
    if not b:
        return None
    b[-1] += 1
    return bytes(b)


class Archive:
    def __init__(self, path=None, base_uri=None, allowed_url_prefixes=None):
        """Open the archive at local `path`.

        allowed_url_prefixes: optional list of resolved-URL string prefixes that
        sources may name; default None means unrestricted (spec §10).
        """
        self.blob = FileBlob(path)
        self.base_uri = base_uri or uri.base_uri_for_path(path)
        self.base = uri.parse(self.base_uri)
        self.allowed = allowed_url_prefixes
        try:
            self._open()
        except (ValueError, struct.error) as e:
            self.blob.close()
            raise ArchiveError(str(e)) from None
        except BaseException:
            self.blob.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------- open

    def _open(self):
        blob = self.blob
        size = blob.size
        eocd = None
        if size >= 60:
            tail = blob.read(size - 60, 60)
            if struct.unpack_from("<I", tail, 0)[0] == EOCD_SIG and \
                    struct.unpack_from("<H", tail, 20)[0] == 38:
                eocd = size - 60
        if eocd is None and size >= 44:
            tail = blob.read(size - 44, 44)
            if struct.unpack_from("<I", tail, 0)[0] == EOCD_SIG and \
                    struct.unpack_from("<H", tail, 20)[0] == 22:
                eocd = size - 44
        if eocd is None:
            raise ArchiveError("not a vzip archive: no end of central directory record "
                               "with a vzip comment")
        rec = blob.read(eocd, size - eocd)
        comment = rec[22:]
        if not comment.startswith(b"vzip/"):
            raise ArchiveError("not a vzip archive: comment does not start with vzip/")
        if comment[5:6] != b"0":
            raise ArchiveError(f"unsupported vzip version {comment[5:6]!r}")
        (_d1, _d2, n_disk, n_total, cd_size, cd_off) = struct.unpack_from("<HHHHII", rec, 4)
        if n_disk == 0xFFFF or n_total == 0xFFFF or cd_size == 0xFFFFFFFF or \
                cd_off == 0xFFFFFFFF:
            if eocd < 20:
                raise ArchiveError("zip64 locator missing")
            loc = blob.read(eocd - 20, 20)
            sig, _disk, z64_off, _ndisks = struct.unpack("<IIQI", loc)
            if sig != Z64_LOC_SIG:
                raise ArchiveError("zip64 end of central directory locator missing")
            if z64_off + 56 > size:
                raise ArchiveError("zip64 end of central directory record outside the file")
            z = blob.read(z64_off, 56)
            zsig, zsize = struct.unpack_from("<IQ", z, 0)
            if zsig != Z64_EOCD_SIG:
                raise ArchiveError("bad zip64 end of central directory signature")
            if zsize != 44:
                raise ArchiveError(f"zip64 end of central directory size field is {zsize}")
            n_disk, n_total, cd_size, cd_off = struct.unpack_from("<QQQQ", z, 24)
        if cd_off + cd_size > size:
            raise ArchiveError("central directory lies outside the file")
        self.cd_off, self.cd_size = cd_off, cd_size

        # format entries
        self.paged = len(comment) == 38
        s_off, s_size = struct.unpack_from("<QQ", comment, 6)
        self.sources_loc = (s_off, s_size)
        sources_raw = self._read_format_entry(s_off, s_size, "__vz__/sources")
        self.format_raw = {SOURCES_KEY: sources_raw}
        self._load_sources(sources_raw)
        self.pinned = {}
        self.pages = []
        self.page_cache = {}
        self.records = None
        if self.paged:
            i_off, i_size = struct.unpack_from("<QQ", comment, 22)
            index_raw = self._read_format_entry(i_off, i_size, "__vz__/index")
            self.format_raw[INDEX_KEY] = index_raw
            self._load_index(index_raw)
        else:
            try:
                recs = _parse_records(blob.read(cd_off, cd_size))
            except ValueError as e:
                raise ArchiveError(f"central directory does not parse: {e}") from None
            self.records = {}
            for name, r in recs:
                if name == INDEX_KEY:
                    raise ArchiveError("__vz__/index entry in an archive without a page index")
                self.records.setdefault(name, r)  # duplicates: first wins (unspecified)

    def _read_format_entry(self, off, csize, what):
        if off + csize > self.blob.size:
            raise ArchiveError(f"{what} body lies outside the file")
        try:
            return _inflate_clean(self.blob.read(off, csize), MAX_FORMAT_ENTRY_BYTES)
        except OverflowError:
            raise ArchiveError(f"{what} exceeds the reader's size limit") from None
        except ValueError as e:
            raise ArchiveError(f"{what} does not inflate cleanly: {e}") from None

    def _load_sources(self, raw):
        try:
            table = proto.decode(raw, proto.SOURCE_TABLE)
        except proto.ProtoError as e:
            raise ArchiveError(f"source table is malformed: {e}") from None
        self.sources = []
        for i, s in enumerate(table["sources"]):
            kinds = [k for k in ("url", "key", "data") if k in s]
            if not kinds:
                raise ArchiveError(f"source {i} has no kind")
            kind = kinds[0]
            pins = Pins(s.get("size"), s.get("etag"), s.get("modified_not_after"))
            has_pin = any(k in s for k in ("size", "etag", "modified_not_after"))
            if kind == "url" and s["url"] == "":
                raise ArchiveError(f"source {i} has an empty url")
            if kind != "url" and has_pin:
                raise ArchiveError(f"source {i}: pin on a {kind} source")
            if pins.etag is not None and not is_strong_etag(pins.etag):
                raise ArchiveError(f"source {i}: etag pin is not a strong entity tag")
            self.sources.append((kind, s[kind], pins))

    def _load_index(self, raw):
        try:
            idx = proto.decode(raw, proto.CD_INDEX)
        except proto.ProtoError as e:
            raise ArchiveError(f"page index is malformed: {e}") from None
        expect = 0
        prev = None
        for i, p in enumerate(idx["pages"]):
            fk = p.get("first_key", "").encode("utf-8")
            off, ln = p.get("offset", 0), p.get("length", 0)
            if ln == 0:
                raise ArchiveError(f"page {i} has length 0")
            if off + ln > self.cd_size:
                raise ArchiveError(f"page {i} lies outside the central directory")
            if off != expect:
                raise ArchiveError(f"page {i} is not contiguous")
            if not fk:
                raise ArchiveError(f"page {i} has an empty first_key")
            if prev is not None and not fk > prev:
                raise ArchiveError(f"page {i}: first_key values do not strictly increase")
            prev = fk
            expect = off + ln
            self.pages.append((fk, off, ln))
        for p in idx["pinned"]:
            k = p.get("key", "").encode("utf-8")
            if not k:
                raise ArchiveError("pinned key is empty")
            if k in self.pinned:
                raise ArchiveError(f"pinned key {k!r} listed twice")
            if k in FORMAT_KEYS:
                raise ArchiveError("pinned key is a format entry")
            method = p.get("method", 0)
            if method not in (0, 8):
                raise ArchiveError(f"pinned method {method}")
            d_off, csize = p.get("data_offset", 0), p.get("csize", 0)
            if d_off + csize > self.blob.size:
                raise ArchiveError(f"pinned body of {k!r} lies outside the file")
            self.pinned[k] = PinnedEntry(k, d_off, p.get("size", 0), csize, method)

    # ------------------------------------------------------------ lookup

    def _page_records(self, i):
        """Records of page i as a dict; raise EntryError if it cannot be parsed."""
        if i in self.page_cache:
            res = self.page_cache[i]
        else:
            _fk, off, ln = self.pages[i]
            try:
                recs = _parse_records(self.blob.read(self.cd_off + off, ln))
                d = {}
                for name, r in recs:
                    d.setdefault(name, r)
                res = d
            except (ValueError, struct.error) as e:
                res = str(e)
            self.page_cache[i] = res
        if isinstance(res, str):
            raise EntryError(f"page {i} cannot be parsed: {res}")
        return res

    def _page_for(self, k):
        lo, hi = 0, len(self.pages)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.pages[mid][0] <= k:
                lo = mid + 1
            else:
                hi = mid
        return lo - 1

    def _lookup(self, k):
        """Return the Record / PinnedEntry for key bytes k, or None. Format
        entries are handled by callers. Raises EntryError."""
        if not self.paged:
            r = self.records.get(k)
        elif k in self.pinned:
            r = self.pinned[k]
        else:
            i = self._page_for(k)
            if i < 0:
                return None
            r = self._page_records(i).get(k)
        if r is None:
            return None
        r.analyse()
        if r.error:
            raise EntryError(r.error)
        return r

    # ------------------------------------------------------------ bodies

    def _check_body_bounds(self, r):
        if r.body_offset + r.csize > self.blob.size:
            raise BodyError("entry body lies outside the file")

    def _body_value(self, r, start=None, end=None):
        """Value of a bytes entry (or raw body). Window [start, end) of the value
        (None = whole). Raises BodyError."""
        self._check_body_bounds(r)
        if r.method == 8:
            if r.usize > MAX_REQUEST_BYTES:
                raise RequestError("entry exceeds the reader's inflate limit")
            data = self.blob.read(r.body_offset, r.csize)
            try:
                out = _inflate_clean(data, r.usize)
            except OverflowError:
                raise BodyError("DEFLATE body inflates to more than its uncompressed size") \
                    from None
            except ValueError as e:
                raise BodyError(str(e)) from None
            if len(out) != r.usize:
                raise BodyError("DEFLATE body inflates to the wrong size")
            if start is None:
                return out
            return out[start:end]
        if r.csize != r.usize:
            raise BodyError("STORED entry's compressed and uncompressed sizes differ")
        if start is None:
            start, end = 0, r.usize
        start = min(start, r.usize)
        end = min(end, r.usize)
        if end - start > MAX_REQUEST_BYTES:
            raise RequestError("request exceeds the reader's size limit")
        return self.blob.read(r.body_offset + start, end - start) if end > start else b""

    # ------------------------------------------------------------ operations

    def classify(self, key):
        k = _utf8(key)
        if k.startswith(PREFIX):
            return "missing"
        r = self._lookup(k)
        return "missing" if r is None else r.kind

    def raw(self, key):
        k = _utf8(key)
        if k in self.format_raw:
            return self.format_raw[k]
        r = self._lookup(k)
        if r is None:
            return None
        return self._body_value(r)

    def get(self, key, request=("whole",)):
        k = _utf8(key)
        _check_request(request)
        if k.startswith(PREFIX):
            return None
        r = self._lookup(k)
        if r is None:
            return None
        if r.kind == "bytes":
            self._check_body_bounds(r)
            if r.method == 0:
                n = r.usize
                if r.csize != r.usize:
                    raise BodyError("STORED entry's compressed and uncompressed sizes differ")
                a, b = _window(request, n)
                return self._body_value(r, a, b)
            n = r.usize
            a, b = _window(request, n)
            return self._body_value(r, a, b)
        return self._resolve(r, request)

    def list(self, prefix=""):
        p = _utf8(prefix)
        found = set()
        if not self.paged:
            for name in self.records:
                if name.startswith(p) and not name.startswith(PREFIX):
                    found.add(name)
        else:
            for name in self.pinned:
                if name.startswith(p) and not name.startswith(PREFIX):
                    found.add(name)
            up = _prefix_upper(p)
            for i, (lo, _off, _ln) in enumerate(self.pages):
                hi = self.pages[i + 1][0] if i + 1 < len(self.pages) else None
                hit = (lo <= p and (hi is None or p < hi)) or \
                      (lo.startswith(p))
                if up is not None and lo >= up:
                    hit = False
                if not hit:
                    continue
                for name in self._page_records(i):
                    if name < lo or (hi is not None and name >= hi):
                        continue  # lookup would not find it in this page
                    if name.startswith(p) and not name.startswith(PREFIX):
                        found.add(name)
        return sorted(found)

    # ------------------------------------------------------------ references

    def _decode_payload(self, r):
        try:
            if r.ref_id == 0x7A76:
                parts = [proto.decode(r.payload, proto.RANGE)]
            else:
                parts = proto.decode(r.payload, proto.CONCAT)["parts"]
        except proto.ProtoError as e:
            raise PayloadError(f"malformed reference payload: {e}") from None
        out = []
        total = 0
        for i, p in enumerate(parts):
            src, off, ln = p.get("source", 0), p.get("offset", 0), p.get("length", 0)
            if "data" in p:
                if src or off or ln:
                    raise PayloadError(f"range {i}: literal range with source/offset/length")
                out.append(("lit", p["data"], len(p["data"])))
                total += len(p["data"])
            else:
                if src >= len(self.sources):
                    raise PayloadError(f"range {i}: source {src} out of bounds")
                if off + ln > U64_MAX:
                    raise PayloadError(f"range {i}: offset + length exceeds 2^64-1")
                out.append(("src", (src, off), ln))
                total += ln
        if total > U64_MAX:
            raise PayloadError("reference size exceeds 2^64-1")
        return out, total

    def _resolve(self, r, request):
        parts, n = self._decode_payload(r)
        a, b = _window(request, n)
        if b - a > MAX_REQUEST_BYTES:
            raise RequestError("request exceeds the reader's size limit")
        out = []
        pos = 0
        for kind, val, size in parts:
            lo, hi = max(a, pos), min(b, pos + size)
            if lo < hi:
                i, j = lo - pos, hi - pos
                if kind == "lit":
                    out.append(val[i:j])
                else:
                    src, off = val
                    out.append(self._read_source(src, off + i, off + j))
            pos += size
        return b"".join(out)

    def _read_source(self, idx, start, end):
        kind, val, pins = self.sources[idx]
        if kind == "data":
            if len(val) < end:
                raise ResolutionError(f"data source {idx} is {len(val)} bytes, need {end}")
            return val[start:end]
        if kind == "key":
            k = val.encode("utf-8")
            if k in FORMAT_KEYS:
                raise ResolutionError(f"source {idx} names a format entry")
            try:
                r = self._lookup(k)
            except EntryError as e:
                raise ResolutionError(f"source key {val!r} has an entry error: {e}") from None
            if r is None:
                raise ResolutionError(f"source key {val!r} is missing")
            if r.kind != "bytes":
                raise ResolutionError(f"source key {val!r} is a reference")
            try:
                self._check_body_bounds(r)
                if r.method == 0 and r.csize != r.usize:
                    raise BodyError("STORED sizes differ")
                if r.usize < end:
                    raise ResolutionError(f"source key {val!r} is {r.usize} bytes, need {end}")
                return self._body_value(r, start, end)
            except BodyError as e:
                raise ResolutionError(f"source key {val!r} has a body error: {e}") from None
        # url
        try:
            ref = uri.parse(val)
        except uri.URIError as e:
            raise ResolutionError(f"source {idx} url is not a valid URI reference: {e}") \
                from None
        target = uri.resolve(self.base, ref)
        if self.allowed is not None:
            s = str(target)
            if not any(s.startswith(p) for p in self.allowed):
                raise ResolutionError(f"URL {s} is not in an allowed prefix")
        return read_url(target, start, end, pins)

    def close(self):
        self.blob.close()


def is_strong_etag(s):
    if len(s) < 2 or s[0] != '"' or s[-1] != '"':
        return False
    return all(0x21 <= ord(c) <= 0x7E and c != '"' for c in s[1:-1])


def _check_request(req):
    if req[0] == "range":
        _, s, e = req
        if s < 0 or e < 0:
            raise RequestError("negative range bound")
        if s > e:
            raise RequestError("range start > end")
    elif req[0] in ("offset", "suffix"):
        if req[1] < 0:
            raise RequestError("negative request value")


def _window(req, n):
    t = req[0]
    if t == "whole":
        return 0, n
    if t == "range":
        return min(req[1], n), min(req[2], n)
    if t == "offset":
        return min(req[1], n), n
    if t == "suffix":
        return max(n - req[1], 0), n
    raise ValueError(t)
