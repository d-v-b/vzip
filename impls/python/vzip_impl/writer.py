"""vzip writer (spec §9)."""

import struct
import zlib

from . import proto, uri
from .errors import WriteError
from .reader import FORMAT_KEYS, INDEX_KEY, SOURCES_KEY, is_strong_etag

U64_MAX = (1 << 64) - 1
I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1
MAX_PAYLOAD = 65519
DOS_TIME = 0
DOS_DATE = (0 << 9) | (1 << 5) | 1  # 1980-01-01
FLAGS = 0x0800  # UTF-8 names


class Entry:
    """key: str. Either data (bytes) + compress, or ranges (list of dicts:
    {'data': bytes} or {'source': int, 'offset': int, 'length': int})."""

    def __init__(self, key, data=None, ranges=None, compress=False, pinned=False):
        self.key = key
        self.data = data
        self.ranges = ranges
        self.compress = compress
        self.pinned = pinned


class Source:
    """kind in {'url','key','data'}; value str/str/bytes; optional pins."""

    def __init__(self, kind, value, size=None, etag=None, modified_not_after=None):
        self.kind = kind
        self.value = value
        self.size = size
        self.etag = etag
        self.modified_not_after = modified_not_after

    def as_dict(self):
        return {self.kind: self.value, "size": self.size, "etag": self.etag,
                "modified_not_after": self.modified_not_after}


def _deflate(data):
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


def validate(entries, sources, page_size):
    """Raise WriteError if the input must be rejected (spec §9.1).

    Returns {key_bytes: payload info} for reference entries."""
    if page_size is not None and (not isinstance(page_size, int) or page_size < 1):
        raise WriteError("page_size must be null or an integer >= 1")
    keys = {}
    for e in entries:
        try:
            kb = e.key.encode("utf-8")
        except UnicodeEncodeError:
            raise WriteError(f"key {e.key!r} is not valid UTF-8") from None
        if not kb:
            raise WriteError("empty key")
        if len(kb) > 65535:
            raise WriteError("key longer than 65535 bytes")
        if kb in FORMAT_KEYS:
            raise WriteError(f"key {e.key!r} is reserved for a format entry")
        if kb in keys:
            raise WriteError(f"duplicate key {e.key!r}")
        keys[kb] = e
        if (e.data is None) == (e.ranges is None):
            raise WriteError(f"entry {e.key!r} must have exactly one of bytes and ranges")
        if e.ranges is not None and e.compress:
            raise WriteError(f"reference entry {e.key!r} cannot be compressed")
        if e.pinned:
            if page_size is None:
                raise WriteError(f"entry {e.key!r} is pinned but there is no page index")
            if e.data is None:
                raise WriteError(f"pinned entry {e.key!r} is not a bytes entry")
        if e.data is not None and len(e.data) >= 0xFFFFFFFF:
            raise WriteError(f"entry {e.key!r} is too large")
    for i, s in enumerate(sources):
        if s.kind == "url":
            if s.value == "":
                raise WriteError(f"source {i}: empty url")
            if not uri.is_uri_reference(s.value):
                raise WriteError(f"source {i}: url {s.value!r} is not an RFC 3986 URI-reference")
        else:
            if s.size is not None or s.etag is not None or s.modified_not_after is not None:
                raise WriteError(f"source {i}: pins are only allowed on url sources")
        if s.kind == "key":
            try:
                kb = s.value.encode("utf-8")
            except UnicodeEncodeError:
                raise WriteError(f"source {i}: key is not valid UTF-8") from None
            if kb in FORMAT_KEYS:
                raise WriteError(f"source {i}: names a format entry")
            tgt = keys.get(kb)
            if tgt is None:
                raise WriteError(f"source {i}: key {s.value!r} is absent")
            if tgt.data is None:
                raise WriteError(f"source {i}: key {s.value!r} is a reference entry")
        if s.etag is not None and not is_strong_etag(s.etag):
            raise WriteError(f"source {i}: etag {s.etag!r} is not a strong entity tag")
        if s.size is not None and not 0 <= s.size <= U64_MAX:
            raise WriteError(f"source {i}: size pin out of range")
        if s.modified_not_after is not None and \
                not I64_MIN <= s.modified_not_after <= I64_MAX:
            raise WriteError(f"source {i}: modified_not_after out of range")
    payloads = {}
    for e in entries:
        if e.ranges is None:
            continue
        total = 0
        for r in e.ranges:
            if "data" in r:
                total += len(r["data"])
            else:
                src, off, ln = r["source"], r["offset"], r["length"]
                if src < 0 or off < 0 or ln < 0:
                    raise WriteError(f"entry {e.key!r}: negative range field")
                if src >= len(sources):
                    raise WriteError(f"entry {e.key!r}: source {src} out of bounds")
                if off + ln > U64_MAX:
                    raise WriteError(f"entry {e.key!r}: offset + length exceeds 2^64-1")
                total += ln
        if total > U64_MAX:
            raise WriteError(f"entry {e.key!r}: total size exceeds 2^64-1")
        if len(e.ranges) == 1:
            hid, payload = 0x7A76, proto.encode_range(e.ranges[0])
        else:
            hid, payload = 0x7A77, proto.encode_concat(e.ranges)
        if len(payload) > MAX_PAYLOAD:
            raise WriteError(f"entry {e.key!r}: reference payload is {len(payload)} bytes "
                             f"(> {MAX_PAYLOAD})")
        payloads[e.key.encode("utf-8")] = (hid, payload)
    return payloads


class _Out:
    def __init__(self, f):
        self.f = f
        self.pos = 0

    def write(self, b):
        self.f.write(b)
        self.pos += len(b)


def _local_header(name, method, crc, csize, usize):
    return struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, FLAGS, method, DOS_TIME, DOS_DATE,
                       crc, csize, usize, len(name), 0) + name


def _cd_record(name, method, crc, csize, usize, lho, extra_blocks):
    extra = b""
    need_z64 = lho >= 0xFFFFFFFF
    if need_z64:
        extra += struct.pack("<HHQ", 0x0001, 8, lho)
    for hid, data in extra_blocks:
        extra += struct.pack("<HH", hid, len(data)) + data
    return struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 45 if need_z64 else 20, FLAGS,
                       method, DOS_TIME, DOS_DATE, crc, csize, usize, len(name), len(extra),
                       0, 0, 0, 0, min(lho, 0xFFFFFFFF)) + name + extra


def write_archive(f, entries, sources, page_size=None, mirror=True):
    """Write a vzip archive to binary file object f. Validates first."""
    payloads = validate(entries, sources, page_size)
    out = _Out(f)
    records = {}  # key bytes -> cd record bytes (body records)
    pinned_info = []

    def put(name, method, body, usize, crc, extra_blocks=()):
        lho = out.pos
        out.write(_local_header(name, method, crc, len(body), usize))
        body_off = out.pos
        out.write(body)
        rec = _cd_record(name, method, crc, len(body), usize, lho, list(extra_blocks))
        return body_off, rec

    def put_entry(e):
        kb = e.key.encode("utf-8")
        if e.data is not None:
            crc = zlib.crc32(e.data)
            if e.compress:
                body, method = _deflate(e.data), 8
            else:
                body, method = e.data, 0
            if len(body) >= 0xFFFFFFFF:
                raise WriteError(f"entry {e.key!r}: compressed size too large")
            body_off, rec = put(kb, method, body, len(e.data), crc)
            if e.pinned:
                pinned_info.append((kb, body_off, len(e.data), len(body), method))
        else:
            hid, payload = payloads[kb]
            body = payload if mirror else b""
            _, rec = put(kb, 0, body, len(body), zlib.crc32(body), [(hid, payload)])
        records[kb] = rec

    for e in entries:
        if not e.pinned:
            put_entry(e)
    src_raw = proto.encode_source_table([s.as_dict() for s in sources])
    src_body = _deflate(src_raw)
    s_off, s_rec = put(SOURCES_KEY, 8, src_body, len(src_raw), zlib.crc32(src_raw))
    for e in entries:
        if e.pinned:
            put_entry(e)
    format_recs = [s_rec]
    body_names = sorted(records)
    comment = b"vzip/0" + struct.pack("<QQ", s_off, len(src_body))
    if page_size is not None:
        pages = []
        cur_off = 0
        page_start = 0
        page_first = None
        for name in body_names:
            rl = len(records[name])
            if page_first is not None and cur_off - page_start + rl > page_size:
                pages.append((page_first, page_start, cur_off - page_start))
                page_first = None
            if page_first is None:
                page_first, page_start = name, cur_off
            cur_off += rl
        if page_first is not None:
            pages.append((page_first, page_start, cur_off - page_start))
        pinned_info.sort()
        idx_raw = proto.encode_cd_index(pages, pinned_info)
        idx_body = _deflate(idx_raw)
        i_off, i_rec = put(INDEX_KEY, 8, idx_body, len(idx_raw), zlib.crc32(idx_raw))
        format_recs.append(i_rec)
        comment += struct.pack("<QQ", i_off, len(idx_body))
    cd_off = out.pos
    for name in body_names:
        out.write(records[name])
    for rec in format_recs:
        out.write(rec)
    cd_size = out.pos - cd_off
    n = len(body_names) + len(format_recs)
    if n >= 0xFFFF or cd_size >= 0xFFFFFFFF or cd_off >= 0xFFFFFFFF:
        z64_off = out.pos
        out.write(struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0, n, n, cd_size,
                              cd_off))
        out.write(struct.pack("<IIQI", 0x07064B50, 0, z64_off, 1))
    out.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0,
                          min(n, 0xFFFF), min(n, 0xFFFF),
                          min(cd_size, 0xFFFFFFFF), min(cd_off, 0xFFFFFFFF), len(comment)))
    out.write(comment)
