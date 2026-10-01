"""vzip reader (spec §3, §4, §7, §8)."""

from __future__ import annotations

import os
import stat
import struct
import zlib
from dataclasses import dataclass

from . import proto
from .errors import ArchiveError, BodyError, EntryError, PayloadError, RequestError, ResolutionError, VzipError
from .fetch import is_strong_etag, read_url_source
from .uri import file_base_uri

U64_MAX = proto.U64_MAX

PREFIX = b"__vz__/"
SOURCES_KEY = b"__vz__/sources"
INDEX_KEY = b"__vz__/index"
FORMAT_KEYS = (SOURCES_KEY, INDEX_KEY)

SIG_EOCD = 0x06054B50
SIG_Z64_EOCD = 0x06064B50
SIG_Z64_LOC = 0x07064B50
SIG_CDR = 0x02014B50

EXTRA_RANGE = 0x7A76
EXTRA_CONCAT = 0x7A77
EXTRA_ZIP64 = 0x0001

# Resource limit (spec §10): the largest value window or inflated body a single
# request may materialise.  Exceeding it is a request error.
MAX_REQUEST_BYTES = 1 << 30


def is_hidden(key: bytes) -> bool:
    return key.startswith(PREFIX)


# ------------------------------------------------------------------ requests


@dataclass(frozen=True)
class Request:
    kind: str = "whole"  # whole | range | offset | suffix
    a: int = 0
    b: int = 0

    def check(self) -> None:
        if self.kind == "range" and self.a > self.b:
            raise RequestError(f"range start {self.a} > end {self.b}")
        if any(v < 0 for v in (self.a, self.b)):
            raise RequestError("negative request bound")

    def window(self, n: int) -> tuple[int, int]:
        if self.kind == "whole":
            return 0, n
        if self.kind == "range":
            return min(self.a, n), min(self.b, n)
        if self.kind == "offset":
            return min(self.a, n), n
        if self.kind == "suffix":
            return max(n - self.a, 0), n
        raise AssertionError(self.kind)


WHOLE = Request()


# ------------------------------------------------------------------ central directory records


class CDParseError(Exception):
    pass


@dataclass
class Entry:
    """What a lookup finds for a key."""
    name: bytes
    kind: str | None = None  # 'bytes' | 'reference'
    method: int = 0
    csize: int = 0
    usize: int | None = 0  # None: not known (format entries located through the comment)
    body_offset: int = 0
    payload_id: int = 0
    payload: bytes = b""
    entry_error: str | None = None
    is_format: bool = False


def parse_records(buf: bytes) -> list[tuple[bytes, bytes]]:
    """Split a byte string into central directory records.

    Returns (fixed_header_and_fields, name) tuples; raises CDParseError unless buf is
    exactly a sequence of whole records with correct signatures.
    """
    out = []
    pos = 0
    end = len(buf)
    while pos < end:
        if end - pos < 46:
            raise CDParseError(f"truncated central directory record at {pos}")
        sig, = struct.unpack_from("<I", buf, pos)
        if sig != SIG_CDR:
            raise CDParseError(f"bad central directory signature at {pos}")
        nlen, xlen, clen = struct.unpack_from("<HHH", buf, pos + 28)
        rec_end = pos + 46 + nlen + xlen + clen
        if rec_end > end:
            raise CDParseError(f"central directory record at {pos} overruns the directory")
        out.append(bytes(buf[pos:rec_end]))
        pos = rec_end
    return out


def analyse_record(rec: bytes) -> Entry:
    (_sig, _made, _need, flags, method, _t, _d, _crc, csize, usize, nlen, xlen, _clen,
     _disk, _iattr, _eattr, loff) = struct.unpack_from("<IHHHHHHIIIHHHHHII", rec, 0)
    name = rec[46:46 + nlen]
    extra = rec[46 + nlen:46 + nlen + xlen]
    e = Entry(name=name, method=method, csize=csize, usize=usize)

    blocks = []
    pos = 0
    ok = True
    while pos < len(extra):
        if len(extra) - pos < 4:
            ok = False
            break
        hid, dsz = struct.unpack_from("<HH", extra, pos)
        if pos + 4 + dsz > len(extra):
            ok = False
            break
        blocks.append((hid, extra[pos + 4:pos + 4 + dsz]))
        pos += 4 + dsz
    if not ok:
        e.entry_error = "extra field does not parse"
        return e
    refs = [(h, d) for h, d in blocks if h in (EXTRA_RANGE, EXTRA_CONCAT)]
    if len(refs) > 1:
        e.entry_error = "more than one reference block"
        return e
    if refs:
        e.kind = "reference"
        e.payload_id, e.payload = refs[0]
    else:
        e.kind = "bytes"
    if method not in (0, 8):
        e.entry_error = f"unsupported compression method {method}"
        return e
    if flags & 1:
        e.entry_error = "entry is encrypted"
        return e
    if e.kind == "reference" and method == 8:
        e.entry_error = "reference entry uses method 8"
        return e

    # ZIP64 extended information: values for the all-ones fields, in APPNOTE order.
    needed = [f for f, v in (("usize", usize), ("csize", csize), ("loff", loff)) if v == 0xFFFFFFFF]
    if needed:
        z = [d for h, d in blocks if h == EXTRA_ZIP64]
        if len(z) != 1:
            e.entry_error = ("missing ZIP64 extra block" if not z else "more than one ZIP64 extra block")
            return e
        if len(z[0]) < 8 * len(needed):
            e.entry_error = "ZIP64 extra block too short"
            return e
        vals = {}
        for i, f in enumerate(needed):
            vals[f] = struct.unpack_from("<Q", z[0], 8 * i)[0]
        usize = vals.get("usize", usize)
        csize = vals.get("csize", csize)
        loff = vals.get("loff", loff)
        e.usize, e.csize = usize, csize
    e.body_offset = loff + 30 + nlen
    return e


def record_name(rec: bytes) -> bytes:
    nlen, = struct.unpack_from("<H", rec, 28)
    return rec[46:46 + nlen]


def valid_key_name(name: bytes) -> bool:
    if not name:
        return False
    try:
        name.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    return True


# ------------------------------------------------------------------ DEFLATE


class InflateError(Exception):
    pass


def inflate_clean(body: bytes, expect: int | None = None) -> bytes:
    """Inflate a raw DEFLATE body that must be exactly one complete stream (spec §8.1)."""
    limit = MAX_REQUEST_BYTES
    if expect is not None and expect > limit:
        raise RequestError(f"entry inflates to {expect} bytes, above the {limit}-byte limit")
    d = zlib.decompressobj(-15)
    try:
        out = d.decompress(body, limit + 1)
    except zlib.error as e:
        raise InflateError(f"invalid DEFLATE data: {e}") from None
    if len(out) > limit:
        raise RequestError(f"entry inflates to more than {limit} bytes")
    if not d.eof:
        raise InflateError("DEFLATE stream is incomplete")
    if d.unused_data or d.unconsumed_tail:
        raise InflateError("bytes follow the end of the DEFLATE stream")
    if expect is not None and len(out) != expect:
        raise InflateError(f"inflated to {len(out)} bytes, record says {expect}")
    return out


# ------------------------------------------------------------------ the archive


class Archive:
    def __init__(self, path, base_uri: str | None = None):
        self.path = path
        try:
            self._fd = os.open(path, os.O_RDONLY)
        except OSError as e:
            raise ArchiveError(f"cannot open {path!r}: {e}") from None
        try:
            st = os.fstat(self._fd)
            if not stat.S_ISREG(st.st_mode):
                raise ArchiveError(f"{path!r} is not a regular file")
            self.size = st.st_size
            self.base_uri = base_uri if base_uri is not None else file_base_uri(path)
            self._open()
        except BaseException:
            os.close(self._fd)
            self._fd = -1
            raise
        self._value_cache: dict[bytes, bytes] = {}

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _read(self, off: int, n: int) -> bytes:
        if off < 0 or n < 0 or off + n > self.size:
            raise ValueError("read outside file")
        out = bytearray()
        while len(out) < n:
            chunk = os.pread(self._fd, n - len(out), off + len(out))
            if not chunk:
                raise OSError("unexpected end of file")
            out += chunk
        return bytes(out)

    # ---------------------------------------------------------------- open (§8.1)

    def _find_eocd(self) -> tuple[int, bytes]:
        size = self.size
        if size >= 60:
            rec = self._read(size - 60, 60)
            sig, = struct.unpack_from("<I", rec, 0)
            clen, = struct.unpack_from("<H", rec, 20)
            if sig == SIG_EOCD and clen == 38:
                return size - 60, rec
        if size >= 44:
            rec = self._read(size - 44, 44)
            sig, = struct.unpack_from("<I", rec, 0)
            clen, = struct.unpack_from("<H", rec, 20)
            if sig == SIG_EOCD and clen == 22:
                return size - 44, rec
        raise ArchiveError("not a vzip archive: no end of central directory record with a vzip comment")

    def _open(self) -> None:
        try:
            self._open_inner()
        except ArchiveError:
            raise
        except OSError as e:
            raise ArchiveError(f"I/O error: {e}") from None

    def _open_inner(self) -> None:
        eocd_off, eocd = self._find_eocd()
        comment = eocd[22:]
        if not comment.startswith(b"vzip/"):
            raise ArchiveError("not a vzip archive: comment does not start with 'vzip/'")
        if comment[:6] != b"vzip/0":
            raise ArchiveError(f"unsupported vzip format version {comment[5:6]!r}")
        (_sig, _disk, _cddisk, n_disk, n_total, cd_size, cd_off, _clen) = struct.unpack_from(
            "<IHHHHIIH", eocd, 0)
        if n_disk == 0xFFFF or n_total == 0xFFFF or cd_size == 0xFFFFFFFF or cd_off == 0xFFFFFFFF:
            if eocd_off < 20:
                raise ArchiveError("zip64 locator missing")
            loc = self._read(eocd_off - 20, 20)
            lsig, _ldisk, z64_off, _ndisks = struct.unpack("<IIQI", loc)
            if lsig != SIG_Z64_LOC:
                raise ArchiveError("zip64 end of central directory locator missing")
            if z64_off > self.size or self.size - z64_off < 56:
                raise ArchiveError("zip64 end of central directory record lies outside the file")
            z = self._read(z64_off, 56)
            zsig, zsize = struct.unpack_from("<IQ", z, 0)
            if zsig != SIG_Z64_EOCD:
                raise ArchiveError("bad zip64 end of central directory signature")
            if zsize != 44:
                raise ArchiveError(f"zip64 end of central directory size field is {zsize}, not 44")
            n_disk, n_total, cd_size, cd_off = struct.unpack_from("<QQQQ", z, 24)
        if cd_off > self.size or cd_size > self.size - cd_off:
            raise ArchiveError("central directory lies outside the file")
        self.cd_off, self.cd_size = cd_off, cd_size

        so, ss = struct.unpack_from("<QQ", comment, 6)
        self.sources_loc = (so, ss)
        self.paged = len(comment) == 38
        self.index_loc = struct.unpack_from("<QQ", comment, 22) if self.paged else None

        # format entries
        sources_raw = self._inflate_format(so, ss, "__vz__/sources")
        try:
            self.sources = proto.decode_source_table(sources_raw)
        except proto.Malformed as e:
            raise ArchiveError(f"source table is malformed: {e}") from None
        for i, s in enumerate(self.sources):
            if s.kind is None:
                raise ArchiveError(f"source {i} has no kind")
            if s.kind == "url" and s.value == "":
                raise ArchiveError(f"source {i} has an empty url")
            if s.kind != "url" and s.has_pins():
                raise ArchiveError(f"source {i} is a {s.kind} source with pins")
            if s.etag is not None and not is_strong_etag(s.etag):
                raise ArchiveError(f"source {i} has an etag pin that is not a strong entity tag")

        self.records: dict[bytes, bytes] | None = None
        self.pages: list[proto.Page] = []
        self.pinned: dict[bytes, proto.Pinned] = {}
        self._page_cache: dict[int, object] = {}
        if self.paged:
            io, isz = self.index_loc
            index_raw = self._inflate_format(io, isz, "__vz__/index")
            self._load_index(index_raw)
        else:
            try:
                recs = parse_records(self._read(cd_off, cd_size))
            except CDParseError as e:
                raise ArchiveError(f"central directory does not parse: {e}") from None
            self.records = {}
            for rec in recs:
                name = record_name(rec)
                if name == INDEX_KEY:
                    raise ArchiveError("archive without a page index has an __vz__/index entry")
                if not valid_key_name(name):
                    continue
                self.records.setdefault(name, rec)

    def _inflate_format(self, off: int, size: int, what: str) -> bytes:
        if off > self.size or size > self.size - off:
            raise ArchiveError(f"{what} body lies outside the file")
        try:
            return inflate_clean(self._read(off, size))
        except (InflateError, RequestError) as e:
            raise ArchiveError(f"{what} does not inflate cleanly: {e}") from None

    def _load_index(self, raw: bytes) -> None:
        try:
            idx = proto.decode_cd_index(raw)
        except proto.Malformed as e:
            raise ArchiveError(f"page index is malformed: {e}") from None
        expect = 0
        prev = None
        for i, p in enumerate(idx.pages):
            fk = p.first_key.encode("utf-8")
            if p.length == 0:
                raise ArchiveError(f"page index is malformed: page {i} has length 0")
            if p.offset != expect:
                raise ArchiveError(f"page index is malformed: page {i} is not contiguous")
            if p.offset + p.length > self.cd_size:
                raise ArchiveError(f"page index is malformed: page {i} lies outside the central directory")
            if not fk:
                raise ArchiveError(f"page index is malformed: page {i} has an empty first_key")
            if prev is not None and not fk > prev:
                raise ArchiveError("page index is malformed: first_key values do not strictly increase")
            prev = fk
            expect = p.offset + p.length
        for p in idx.pinned:
            k = p.key.encode("utf-8")
            if not k:
                raise ArchiveError("page index is malformed: empty pinned key")
            if k in self.pinned:
                raise ArchiveError(f"page index is malformed: pinned key {p.key!r} listed twice")
            if k in FORMAT_KEYS:
                raise ArchiveError(f"page index is malformed: pinned key {p.key!r} is a format entry")
            if p.method not in (0, 8):
                raise ArchiveError(f"page index is malformed: pinned method {p.method}")
            if p.data_offset > self.size or p.csize > self.size - p.data_offset:
                raise ArchiveError(f"page index is malformed: pinned body of {p.key!r} lies outside the file")
            self.pinned[k] = p
        self.pages = idx.pages
        self._page_keys = [p.first_key.encode("utf-8") for p in idx.pages]

    # ---------------------------------------------------------------- lookup (§7.2)

    def _page_records(self, i: int) -> dict[bytes, bytes]:
        cached = self._page_cache.get(i)
        if cached is None:
            p = self.pages[i]
            try:
                recs = parse_records(self._read(self.cd_off + p.offset, p.length))
                cached = {}
                for rec in recs:
                    name = record_name(rec)
                    if valid_key_name(name):
                        cached.setdefault(name, rec)
            except CDParseError as e:
                cached = EntryError(f"page {i} cannot be parsed: {e}")
            self._page_cache[i] = cached
        if isinstance(cached, EntryError):
            raise EntryError(str(cached))
        return cached

    def _page_for(self, k: bytes) -> int | None:
        lo, hi = 0, len(self._page_keys)
        while lo < hi:  # last page with first_key <= k
            mid = (lo + hi) // 2
            if self._page_keys[mid] <= k:
                lo = mid + 1
            else:
                hi = mid
        return lo - 1 if lo > 0 else None

    def _lookup(self, k: bytes) -> Entry | None:
        """Find the entry for key k (any key, hidden or not). Raises EntryError for page errors.

        The returned Entry may carry an entry_error; callers decide when to raise it.
        """
        if k == SOURCES_KEY:
            so, ss = self.sources_loc
            return Entry(name=k, kind="bytes", method=8, csize=ss, usize=None, body_offset=so, is_format=True)
        if k == INDEX_KEY:
            if not self.paged:
                return None
            io, isz = self.index_loc
            return Entry(name=k, kind="bytes", method=8, csize=isz, usize=None, body_offset=io, is_format=True)
        if self.paged:
            p = self.pinned.get(k)
            if p is not None:
                return Entry(name=k, kind="bytes", method=p.method, csize=p.csize, usize=p.size,
                             body_offset=p.data_offset)
            i = self._page_for(k)
            if i is None:
                return None
            rec = self._page_records(i).get(k)
        else:
            rec = self.records.get(k)
        if rec is None:
            return None
        return analyse_record(rec)

    def _lookup_checked(self, k: bytes) -> Entry | None:
        e = self._lookup(k)
        if e is not None and e.entry_error:
            raise EntryError(f"{k.decode('utf-8', 'replace')!r}: {e.entry_error}")
        return e

    # ---------------------------------------------------------------- bodies

    def _check_body_bounds(self, e: Entry) -> None:
        if e.body_offset > self.size or e.csize > self.size - e.body_offset:
            raise BodyError("entry body lies outside the file")

    def _body_value(self, e: Entry, window_fn) -> bytes:
        """Return a window of the entry's (inflated) body. window_fn(n) -> (a, b)."""
        self._check_body_bounds(e)
        if e.method == 8:
            cached = self._value_cache.get(e.name) if not e.is_format else None
            if cached is None:
                try:
                    cached = inflate_clean(self._read(e.body_offset, e.csize), e.usize)
                except InflateError as ex:
                    raise BodyError(str(ex)) from None
                if not e.is_format:
                    self._value_cache[e.name] = cached
            a, b = window_fn(len(cached))
            return cached[a:b]
        if e.usize is not None and e.csize != e.usize:
            raise BodyError(f"STORED entry has compressed size {e.csize} != uncompressed size {e.usize}")
        a, b = window_fn(e.csize)
        if b - a > MAX_REQUEST_BYTES:
            raise RequestError("request exceeds the reader's memory limit")
        return self._read(e.body_offset + a, b - a)

    # ---------------------------------------------------------------- operations (§8.2)

    def classify(self, key: str | bytes) -> str:
        k = _kb(key)
        if is_hidden(k):
            return "missing"
        e = self._lookup_checked(k)
        return "missing" if e is None else e.kind

    def get(self, key: str | bytes, request: Request = WHOLE) -> bytes | None:
        request.check()
        k = _kb(key)
        if is_hidden(k):
            return None
        e = self._lookup_checked(k)
        if e is None:
            return None
        if e.kind == "bytes":
            return self._body_value(e, request.window)
        return self._get_reference(e, request)

    def raw(self, key: str | bytes) -> bytes | None:
        k = _kb(key)
        e = self._lookup_checked(k)
        if e is None:
            return None
        return self._body_value(e, lambda n: (0, n))

    def list(self, prefix: str | bytes = "") -> list[str]:
        p = _kb(prefix)
        out: set[bytes] = set()
        if not self.paged:
            out = {k for k in self.records if k.startswith(p) and not is_hidden(k)}
        else:
            out = {k for k in self.pinned if k.startswith(p) and not is_hidden(k)}
            upper = _prefix_upper(p)
            n = len(self.pages)
            for i in range(n):
                lo = self._page_keys[i]
                hi = self._page_keys[i + 1] if i + 1 < n else None
                # does [lo, hi) contain a key starting with p, i.e. intersect [p, upper)?
                start = max(lo, p)
                stop_candidates = [x for x in (hi, upper) if x is not None]
                if stop_candidates and not start < min(stop_candidates):
                    continue
                recs = self._page_records(i)
                for k in recs:
                    if not k.startswith(p) or is_hidden(k):
                        continue
                    if k < lo or (hi is not None and k >= hi):
                        continue  # lookup would not find it in this page
                    if k in FORMAT_KEYS:
                        continue
                    out.add(k)
        return [k.decode("utf-8") for k in sorted(out)]

    # ---------------------------------------------------------------- references (§8.3)

    def _decode_payload(self, e: Entry) -> list[proto.Range]:
        try:
            if e.payload_id == EXTRA_RANGE:
                parts = [proto.decode_range(e.payload)]
            else:
                parts = proto.decode_concat(e.payload)
        except proto.Malformed as ex:
            raise PayloadError(f"reference payload is malformed: {ex}") from None
        total = 0
        nsrc = len(self.sources)
        for i, r in enumerate(parts):
            if r.data is not None:
                if r.source or r.offset or r.length:
                    raise PayloadError(f"part {i}: literal range with non-zero source/offset/length")
            else:
                if r.source >= nsrc:
                    raise PayloadError(f"part {i}: source {r.source} out of range ({nsrc} sources)")
                if r.offset + r.length > U64_MAX:
                    raise PayloadError(f"part {i}: offset + length exceeds 2^64-1")
            total += r.size
        if total > U64_MAX:
            raise PayloadError("reference size exceeds 2^64-1")
        return parts

    def _get_reference(self, e: Entry, request: Request) -> bytes:
        parts = self._decode_payload(e)
        n = sum(r.size for r in parts)
        a, b = request.window(n)
        if b - a > MAX_REQUEST_BYTES:
            raise RequestError("request exceeds the reader's memory limit")
        out = bytearray()
        pos = 0
        for r in parts:
            rs, re_ = pos, pos + r.size
            pos = re_
            lo, hi = max(rs, a), min(re_, b)
            if lo >= hi:
                continue
            i, j = lo - rs, hi - rs
            if r.data is not None:
                out += r.data[i:j]
            else:
                out += self._read_source(r.source, r.offset + i, r.offset + j)
        return bytes(out)

    def _read_source(self, idx: int, start: int, end: int) -> bytes:
        s = self.sources[idx]
        if s.kind == "data":
            if end > len(s.value):
                raise ResolutionError(f"data source {idx} has {len(s.value)} bytes, range needs {end}")
            return s.value[start:end]
        if s.kind == "url":
            return read_url_source(self.base_uri, s, start, end)
        # key source
        k = s.value.encode("utf-8")
        if k in FORMAT_KEYS:
            raise ResolutionError(f"key source {idx} names a format entry")
        try:
            e = self._lookup_checked(k)
            if e is None:
                raise ResolutionError(f"key source {idx} names missing key {s.value!r}")
            if e.kind != "bytes":
                raise ResolutionError(f"key source {idx} names reference entry {s.value!r}")
            short = []

            def win(n):
                if end > n:
                    short.append(n)
                    return 0, 0
                return start, end
            data = self._body_value(e, win)
            if short:
                raise ResolutionError(f"key source {idx} value has {short[0]} bytes, range needs {end}")
            return data
        except (EntryError, BodyError) as ex:
            raise ResolutionError(f"key source {idx} ({s.value!r}): {ex}") from None


def _kb(key) -> bytes:
    if isinstance(key, bytes):
        return key
    return key.encode("utf-8", errors="surrogatepass")


def _prefix_upper(p: bytes) -> bytes | None:
    """Smallest byte string greater than every string with prefix p (None: unbounded)."""
    b = bytearray(p)
    while b and b[-1] == 0xFF:
        b.pop()
    if not b:
        return None
    b[-1] += 1
    return bytes(b)


def open_archive(path, base_uri: str | None = None) -> Archive:
    return Archive(path, base_uri)
