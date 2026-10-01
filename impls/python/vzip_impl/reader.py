"""vzip reader (spec section 8)."""

import bisect
import os
import struct
import zlib
from dataclasses import dataclass
from typing import Optional

from . import pb, uri
from .errors import (ArchiveError, BodyError, EntryError, PayloadError,
                     RequestError, ResolutionError)

HIDDEN = b"__vz__/"
SOURCES_KEY = b"__vz__/sources"
INDEX_KEY = b"__vz__/index"
FORMAT_KEYS = (SOURCES_KEY, INDEX_KEY)
MAGIC = b"vzip/1"
EXT_RANGE = 0x7A76
EXT_CONCAT = 0x7A77

SIG_LOCAL = 0x04034B50
SIG_CDR = 0x02014B50
SIG_EOCD = 0x06054B50
SIG_Z64_EOCD = 0x06064B50
SIG_Z64_LOC = 0x07064B50

# Documented resource limit (spec section 10): a single request may return at
# most this many bytes, and a DEFLATE body may inflate to at most this many.
MAX_REQUEST = 1 << 30
MAX_INFLATE = 1 << 32


def is_hidden(key: bytes) -> bool:
    return key.startswith(HIDDEN)


def valid_key(name: bytes) -> bool:
    if not name:
        return False
    try:
        name.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return False
    return True


def inflate_clean(body: bytes, expected_size: Optional[int]):
    """Inflate a raw DEFLATE body; raises ValueError unless it inflates cleanly."""
    d = zlib.decompressobj(-15)
    limit = (expected_size + 1) if expected_size is not None else MAX_INFLATE + 1
    try:
        out = d.decompress(body, limit)
    except zlib.error as e:
        raise ValueError(f"invalid DEFLATE data: {e}") from None
    if len(out) > (expected_size if expected_size is not None else MAX_INFLATE):
        raise ValueError("DEFLATE body inflates to more than the expected size")
    if not d.eof:
        raise ValueError("DEFLATE stream is truncated (no final block end)")
    if d.unused_data or d.unconsumed_tail:
        raise ValueError("bytes follow the end of the DEFLATE stream")
    if expected_size is not None and len(out) != expected_size:
        raise ValueError(f"DEFLATE body inflates to {len(out)} bytes, record says {expected_size}")
    return out


@dataclass
class Record:
    name: bytes
    flags: int
    method: int
    crc: int
    csize: int
    usize: int
    local_offset: int
    extra: bytes


@dataclass
class Entry:
    key: bytes
    kind: str  # 'bytes' | 'reference'
    method: int
    body_offset: int
    csize: int
    usize: int
    payload_id: int = 0
    payload: bytes = b""


def parse_records(buf: bytes):
    """Parse a sequence of central directory records exactly filling buf.

    Raises ValueError on any structural problem."""
    out = []
    pos = 0
    n = len(buf)
    while pos < n:
        if pos + 46 > n:
            raise ValueError(f"truncated central directory record at {pos}")
        (sig, _vm, _vn, flags, method, _t, _d, crc, csize, usize, nlen, xlen, clen,
         _disk, _ia, _ea, loff) = struct.unpack_from("<IHHHHHHIIIHHHHHII", buf, pos)
        if sig != SIG_CDR:
            raise ValueError(f"bad central directory record signature at {pos}")
        end = pos + 46 + nlen + xlen + clen
        if end > n:
            raise ValueError(f"central directory record at {pos} overruns")
        name = buf[pos + 46:pos + 46 + nlen]
        extra = buf[pos + 46 + nlen:pos + 46 + nlen + xlen]
        out.append(Record(name, flags, method, crc, csize, usize, loff, extra))
        pos = end
    return out


def entry_from_record(rec: Record) -> Entry:
    """Classify a record (section 4.1) and check it; raises EntryError."""
    blocks = []
    x = rec.extra
    pos = 0
    while pos < len(x):
        if pos + 4 > len(x):
            raise EntryError("extra field does not parse (truncated block header)")
        hid, size = struct.unpack_from("<HH", x, pos)
        if pos + 4 + size > len(x):
            raise EntryError("extra field does not parse (block overruns)")
        blocks.append((hid, x[pos + 4:pos + 4 + size]))
        pos += 4 + size
    refs = [b for b in blocks if b[0] in (EXT_RANGE, EXT_CONCAT)]
    if len(refs) > 1:
        raise EntryError("entry has more than one reference extra block")
    if rec.method not in (0, 8):
        raise EntryError(f"unsupported compression method {rec.method}")
    if rec.flags & 1:
        raise EntryError("entry is encrypted (general purpose bit 0)")
    kind = "reference" if refs else "bytes"
    if kind == "reference" and rec.method != 0:
        raise EntryError("reference entry uses method 8")
    usize, csize, loff = rec.usize, rec.csize, rec.local_offset
    if 0xFFFFFFFF in (usize, csize, loff):
        z64 = [b for b in blocks if b[0] == 0x0001]
        if not z64:
            if loff == 0xFFFFFFFF:
                raise EntryError("local header offset 0xFFFFFFFF without a ZIP64 extra block")
        else:
            data = z64[0][1]
            p = 0
            vals = {}
            for fld, cur in (("usize", usize), ("csize", csize), ("loff", loff)):
                if cur == 0xFFFFFFFF:
                    if p + 8 > len(data):
                        raise EntryError("ZIP64 extra block too short")
                    vals[fld] = struct.unpack_from("<Q", data, p)[0]
                    p += 8
            usize = vals.get("usize", usize)
            csize = vals.get("csize", csize)
            loff = vals.get("loff", loff)
    body_offset = loff + 30 + len(rec.name)
    e = Entry(rec.name, kind, rec.method, body_offset, csize, usize)
    if refs:
        e.payload_id, e.payload = refs[0][0], refs[0][1]
    return e


class Archive:
    def __init__(self, path):
        self.path = path
        try:
            self.f = open(path, "rb")
            self.file_size = os.fstat(self.f.fileno()).st_size
        except OSError as e:
            raise ArchiveError(f"cannot open archive: {e}") from None
        try:
            self.base_uri = uri.path_to_base_uri(path)
        except OSError as e:
            self.f.close()
            raise ArchiveError(f"cannot determine base URI: {e}") from None
        self._inflated = {}
        try:
            self._open()
        except BaseException:
            self.f.close()
            raise

    def close(self):
        self.f.close()

    # ------------------------------------------------------------ I/O
    def _read(self, off, n):
        self.f.seek(off)
        data = self.f.read(n)
        if len(data) != n:
            raise OSError("short read")
        return data

    # ------------------------------------------------------------ open
    def _open(self):
        size = self.file_size
        eocd_off = None
        clen = None
        if size >= 60:
            tail = self._read(size - 60, 60)
            sig, = struct.unpack_from("<I", tail, 0)
            c, = struct.unpack_from("<H", tail, 20)
            if sig == SIG_EOCD and c == 38:
                eocd_off, clen = size - 60, 38
        if eocd_off is None and size >= 44:
            tail = self._read(size - 44, 44)
            sig, = struct.unpack_from("<I", tail, 0)
            c, = struct.unpack_from("<H", tail, 20)
            if sig == SIG_EOCD and c == 22:
                eocd_off, clen = size - 44, 22
        if eocd_off is None:
            raise ArchiveError("no end of central directory record with a vzip comment")
        eocd = self._read(eocd_off, 22 + clen)
        (_sig, _d1, _d2, n_disk, n_total, cd_size, cd_off, _cl) = struct.unpack_from("<IHHHHIIH", eocd, 0)
        comment = eocd[22:]
        if comment[:6] != MAGIC:
            raise ArchiveError("archive comment does not start with vzip/1")
        if clen == 22:
            s_off, s_size = struct.unpack_from("<QQ", comment, 6)
            i_off = i_size = None
        else:
            s_off, s_size, i_off, i_size = struct.unpack_from("<QQQQ", comment, 6)

        if n_disk == 0xFFFF or n_total == 0xFFFF or cd_size == 0xFFFFFFFF or cd_off == 0xFFFFFFFF:
            loc_off = eocd_off - 20
            if loc_off < 0:
                raise ArchiveError("no room for a zip64 end of central directory locator")
            loc = self._read(loc_off, 20)
            lsig, _disk, z_off, _ndisks = struct.unpack("<IIQI", loc)
            if lsig != SIG_Z64_LOC:
                raise ArchiveError("zip64 locator missing before end of central directory record")
            if z_off + 56 > size:
                raise ArchiveError("zip64 end of central directory record lies outside the file")
            z = self._read(z_off, 56)
            (zsig, zsize, _vm, _vn, _d1, _d2, n_disk, n_total, cd_size, cd_off) = \
                struct.unpack("<IQHHIIQQQQ", z)
            if zsig != SIG_Z64_EOCD:
                raise ArchiveError("bad zip64 end of central directory record signature")
            if zsize != 44:
                raise ArchiveError(f"zip64 end of central directory record size field is {zsize}, not 44")
        if cd_off + cd_size > size:
            raise ArchiveError("central directory lies outside the file")
        self.cd_off, self.cd_size = cd_off, cd_size

        # format entries
        self.sources_raw = self._inflate_format(s_off, s_size, "__vz__/sources")
        try:
            self.sources = pb.decode_source_table(self.sources_raw)
        except pb.Malformed as e:
            raise ArchiveError(f"source table is malformed: {e}") from None
        for i, s in enumerate(self.sources):
            if s.kind is None:
                raise ArchiveError(f"source {i} has no kind")
            if s.kind == "url" and s.value == b"":
                raise ArchiveError(f"source {i} has an empty url")
            if s.kind != "url" and s.has_pins():
                raise ArchiveError(f"source {i} has a pin on a {s.kind} source")
            if s.etag is not None and not is_strong_etag(s.etag):
                raise ArchiveError(f"source {i} has an invalid etag pin {s.etag!r}")

        self.paged = i_off is not None
        self.index_raw = None
        if self.paged:
            self.index_raw = self._inflate_format(i_off, i_size, "__vz__/index")
            self._check_index()
        else:
            try:
                recs = parse_records(self._read(cd_off, cd_size))
            except (ValueError, OSError) as e:
                raise ArchiveError(f"central directory does not parse: {e}") from None
            self.records = {}
            for r in recs:
                if r.name == INDEX_KEY:
                    raise ArchiveError("__vz__/index entry in an archive without a page index")
                if not valid_key(r.name):
                    continue
                self.records.setdefault(r.name, r)

    def _inflate_format(self, off, csize, what):
        if off + csize > self.file_size:
            raise ArchiveError(f"{what} body lies outside the file")
        try:
            return inflate_clean(self._read(off, csize), None)
        except ValueError as e:
            raise ArchiveError(f"{what} does not inflate cleanly: {e}") from None

    def _check_index(self):
        try:
            idx = pb.decode_cdindex(self.index_raw)
        except pb.Malformed as e:
            raise ArchiveError(f"page index does not decode: {e}") from None
        expect = 0
        prev = None
        for i, p in enumerate(idx.pages):
            if p.length == 0:
                raise ArchiveError(f"page {i} has length 0")
            if p.offset + p.length > self.cd_size:
                raise ArchiveError(f"page {i} lies outside the central directory")
            if p.offset != expect:
                raise ArchiveError(f"page {i} is not contiguous")
            expect = p.offset + p.length
            if prev is not None and not prev < p.first_key:
                raise ArchiveError(f"page {i} first_key does not strictly increase")
            prev = p.first_key
        self.pinned = {}
        for p in idx.pinned:
            if p.key in self.pinned:
                raise ArchiveError(f"pinned key {p.key!r} listed twice")
            if p.key in FORMAT_KEYS:
                raise ArchiveError("a format entry is pinned")
            if p.method not in (0, 8):
                raise ArchiveError(f"pinned method {p.method} is not 0 or 8")
            if p.data_offset + p.csize > self.file_size:
                raise ArchiveError("pinned body lies outside the file")
            self.pinned[p.key] = p
        self.pages = idx.pages
        self.page_keys = [p.first_key for p in idx.pages]
        self._page_cache = {}

    # ------------------------------------------------------------ lookup
    def _page_records(self, i):
        if i not in self._page_cache:
            p = self.pages[i]
            try:
                recs = parse_records(self._read(self.cd_off + p.offset, p.length))
            except (ValueError, OSError) as e:
                raise EntryError(f"page {i} cannot be parsed: {e}") from None
            self._page_cache[i] = recs
        return self._page_cache[i]

    def _lookup(self, key: bytes):
        """Return an Entry, 'format', or None (missing). Raises EntryError."""
        if key == SOURCES_KEY or (key == INDEX_KEY and self.paged):
            return "format"
        if not self.paged:
            rec = self.records.get(key)
            return entry_from_record(rec) if rec is not None else None
        p = self.pinned.get(key)
        if p is not None:
            return Entry(key, "bytes", p.method, p.data_offset, p.csize, p.size)
        i = bisect.bisect_right(self.page_keys, key) - 1
        if i < 0:
            return None
        for rec in self._page_records(i):
            if rec.name == key:
                return entry_from_record(rec)
        return None

    def _format_body(self, key):
        return self.sources_raw if key == SOURCES_KEY else self.index_raw

    # ------------------------------------------------------------ bodies
    def _bytes_value(self, e: Entry, a=None, b=None):
        """Read [a, b) of a bytes entry's value (whole if a is None). BodyError."""
        if e.body_offset + e.csize > self.file_size:
            raise BodyError("entry body lies outside the file")
        if e.method == 0:
            if e.csize != e.usize:
                raise BodyError("STORED entry with differing compressed and uncompressed sizes")
            if a is None:
                a, b = 0, e.usize
            if b - a > MAX_REQUEST:
                raise RequestError("request exceeds the reader's limit of %d bytes" % MAX_REQUEST)
            return self._read(e.body_offset + a, b - a)
        k = (e.body_offset, e.csize, e.usize)
        out = self._inflated.get(k)
        if out is None:
            if e.usize > MAX_INFLATE:
                raise RequestError("entry too large to inflate")
            try:
                out = inflate_clean(self._read(e.body_offset, e.csize), e.usize)
            except ValueError as ex:
                raise BodyError(str(ex)) from None
            self._inflated = {k: out}  # keep only the most recent
        if a is None:
            return out
        return out[a:b]

    # ------------------------------------------------------------ operations
    def classify(self, key: bytes):
        if is_hidden(key):
            return "missing"
        e = self._lookup(key)
        if e is None or e == "format":
            return "missing"
        return e.kind

    def raw(self, key: bytes):
        e = self._lookup(key)
        if e is None:
            return None
        if e == "format":
            return self._format_body(key)
        if e.kind == "bytes":
            return self._bytes_value(e)
        # reference: its STORED body
        return self._bytes_value(Entry(e.key, "bytes", 0, e.body_offset, e.csize, e.usize))

    def get(self, key: bytes, request=("whole",)):
        if request[0] == "range" and request[1] > request[2]:
            raise RequestError("range start is greater than end")
        if is_hidden(key):
            return None
        e = self._lookup(key)
        if e is None or e == "format":
            return None
        if e.kind == "bytes":
            if e.body_offset + e.csize > self.file_size:
                raise BodyError("entry body lies outside the file")
            if e.method == 0 and e.csize != e.usize:
                raise BodyError("STORED entry with differing compressed and uncompressed sizes")
            if e.method == 8:
                self._bytes_value(e)  # inflate and check in full
            a, b = window(request, e.usize)
            if b - a > MAX_REQUEST:
                raise RequestError("request exceeds the reader's limit of %d bytes" % MAX_REQUEST)
            return self._bytes_value(e, a, b)
        # reference
        try:
            if e.payload_id == EXT_RANGE:
                parts = [pb.decode_range(e.payload)]
            else:
                parts = pb.decode_concat(e.payload)
            n = pb.validate_ranges(parts, len(self.sources))
        except pb.Malformed as ex:
            raise PayloadError(f"reference payload is malformed: {ex}") from None
        a, b = window(request, n)
        if b - a > MAX_REQUEST:
            raise RequestError("request exceeds the reader's limit of %d bytes" % MAX_REQUEST)
        out = []
        pos = 0
        for r in parts:
            rs, re_ = pos, pos + r.size
            pos = re_
            lo, hi = max(rs, a), min(re_, b)
            if lo >= hi:
                continue
            i, j = lo - rs, hi - rs
            if r.data is not None:
                out.append(r.data[i:j])
            else:
                out.append(self._source_read(r.source, r.offset + i, r.offset + j))
        return b"".join(out)

    def list(self, prefix: bytes):
        keys = set()
        if not self.paged:
            for name in self.records:
                if not is_hidden(name) and name.startswith(prefix):
                    keys.add(name)
            return sorted(keys)
        for name in self.pinned:
            if not is_hidden(name) and name.startswith(prefix):
                keys.add(name)
        upper = prefix_upper(prefix)
        for i, p in enumerate(self.pages):
            lo = p.first_key
            hi = self.pages[i + 1].first_key if i + 1 < len(self.pages) else None
            if upper is not None and not lo < upper:
                continue
            if hi is not None and not hi > prefix:
                continue
            for rec in self._page_records(i):
                name = rec.name
                if not valid_key(name):
                    continue
                if not (lo <= name and (hi is None or name < hi)):
                    continue
                if not is_hidden(name) and name.startswith(prefix):
                    keys.add(name)
        return sorted(keys)

    # ------------------------------------------------------------ sources
    def _source_read(self, idx, start, end):
        s = self.sources[idx]
        if s.kind == "data":
            if len(s.value) < end:
                raise ResolutionError(f"data source {idx} is shorter than {end} bytes")
            return s.value[start:end]
        if s.kind == "key":
            k = s.value
            if k in FORMAT_KEYS:
                raise ResolutionError(f"key source {idx} names a format entry")
            try:
                e = self._lookup(k)
            except EntryError as ex:
                raise ResolutionError(f"key source {idx}: entry error: {ex}") from None
            if e is None or e == "format":
                raise ResolutionError(f"key source {idx} names a missing key")
            if e.kind != "bytes":
                raise ResolutionError(f"key source {idx} names a reference entry")
            try:
                if e.method == 8:
                    v = self._bytes_value(e)
                    n = len(v)
                else:
                    if e.body_offset + e.csize > self.file_size or e.csize != e.usize:
                        self._bytes_value(e, 0, 0)  # raises BodyError
                    n = e.usize
                    v = None
                if n < end:
                    raise ResolutionError(f"key source {idx} is shorter than {end} bytes")
                return v[start:end] if v is not None else self._bytes_value(e, start, end)
            except BodyError as ex:
                raise ResolutionError(f"key source {idx}: body error: {ex}") from None
        return self._url_read(idx, s, start, end)

    def _url_read(self, idx, s, start, end):
        ref = s.value.decode("utf-8")
        if not uri.is_uri_reference(ref):
            raise ResolutionError(f"source {idx} url is not a valid URI reference")
        comps = uri.resolve(self.base_uri, ref)
        scheme = (comps[0] or "").lower()
        if scheme != "file":
            raise ResolutionError(f"source {idx}: unsupported URL scheme {scheme!r}")
        try:
            path = uri.file_uri_to_path(comps)
        except uri.FileUriError as ex:
            raise ResolutionError(f"source {idx}: {ex}") from None
        if s.etag is not None:
            raise ResolutionError(f"source {idx}: etag pin cannot be checked for file: URLs")
        try:
            with open(path, "rb") as fh:
                st = os.fstat(fh.fileno())
                if s.size is not None and st.st_size != s.size:
                    raise ResolutionError(f"source {idx}: size pin fails ({st.st_size} != {s.size})")
                if s.modified_not_after is not None:
                    mtime = st.st_mtime_ns // 1_000_000_000
                    if mtime > s.modified_not_after:
                        raise ResolutionError(f"source {idx}: modified_not_after pin fails")
                if st.st_size < end:
                    raise ResolutionError(f"source {idx} is shorter than {end} bytes")
                fh.seek(start)
                data = fh.read(end - start)
                if len(data) != end - start:
                    raise ResolutionError(f"source {idx}: short read")
                return data
        except OSError as ex:
            raise ResolutionError(f"source {idx}: cannot read {path!r}: {ex}") from None


def is_strong_etag(b: bytes) -> bool:
    if len(b) < 2 or b[0] != 0x22 or b[-1] != 0x22:
        return False
    return all(0x21 <= c <= 0x7E and c != 0x22 for c in b[1:-1])


def prefix_upper(prefix: bytes):
    p = bytearray(prefix)
    while p and p[-1] == 0xFF:
        p.pop()
    if not p:
        return None
    p[-1] += 1
    return bytes(p)


def window(request, n):
    kind = request[0]
    if kind == "whole":
        return 0, n
    if kind == "range":
        return min(request[1], n), min(request[2], n)
    if kind == "offset":
        return min(request[1], n), n
    if kind == "suffix":
        return max(n - request[1], 0), n
    raise ValueError(kind)
