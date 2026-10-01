"""vzip writer (spec section 9)."""

import struct
import zlib
from dataclasses import dataclass, field
from typing import Optional

from . import pb, uri
from .errors import WriteError
from .reader import (EXT_CONCAT, EXT_RANGE, FORMAT_KEYS, INDEX_KEY, MAGIC,
                     SIG_CDR, SIG_EOCD, SIG_LOCAL, SIG_Z64_EOCD, SIG_Z64_LOC,
                     SOURCES_KEY, is_strong_etag)

MAX_PAYLOAD = 65519
DOS_TIME = 0
DOS_DATE = (0 << 9) | (1 << 5) | 1  # 1980-01-01
FLAG_UTF8 = 0x0800


@dataclass
class InEntry:
    key: bytes
    data: Optional[bytes] = None        # bytes entry
    ranges: Optional[list] = None       # reference entry: list of pb.Range
    compress: bool = False
    pinned: bool = False


@dataclass
class _Placed:
    key: bytes
    method: int
    crc: int
    csize: int
    usize: int
    body: bytes
    extra_blocks: list = field(default_factory=list)  # [(id, data)]
    offset: int = 0


def deflate(data: bytes) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


def _check_key(k):
    if not isinstance(k, bytes) or not k:
        raise WriteError("key is empty")
    try:
        k.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise WriteError(f"key {k!r} is not valid UTF-8") from None


def build_archive(entries, sources, page_size=None, mirror=True) -> bytes:
    """entries: list of InEntry. sources: list of pb.Source (value as bytes)."""
    # ---------------------------------------------------------- validation
    seen = {}
    for e in entries:
        _check_key(e.key)
        if e.key in FORMAT_KEYS:
            raise WriteError(f"key {e.key!r} is reserved for a format entry")
        if e.key in seen:
            raise WriteError(f"duplicate key {e.key!r}")
        seen[e.key] = e
        if e.pinned:
            if page_size is None:
                raise WriteError("pinned entry without a page index")
            if e.data is None:
                raise WriteError(f"pinned entry {e.key!r} is not a bytes entry")
        if e.data is None and e.compress:
            raise WriteError("reference entries cannot be compressed")
    for i, s in enumerate(sources):
        if s.kind == "url":
            if not s.value:
                raise WriteError(f"source {i}: empty url")
            try:
                text = s.value.decode("utf-8")
            except UnicodeDecodeError:
                raise WriteError(f"source {i}: url is not valid UTF-8") from None
            if not uri.is_uri_reference(text):
                raise WriteError(f"source {i}: url {text!r} is not an RFC 3986 URI-reference")
        else:
            if s.has_pins():
                raise WriteError(f"source {i}: pins are only allowed on url sources")
        if s.kind == "key":
            tgt = seen.get(s.value)
            if s.value in FORMAT_KEYS:
                raise WriteError(f"source {i}: key source names a format entry")
            if tgt is None:
                raise WriteError(f"source {i}: key source names an absent key")
            if tgt.data is None:
                raise WriteError(f"source {i}: key source names a reference entry")
        if s.etag is not None and not is_strong_etag(s.etag):
            raise WriteError(f"source {i}: etag pin is not a strong entity tag")
        if s.size is not None and not 0 <= s.size <= pb.U64MAX:
            raise WriteError(f"source {i}: size pin out of range")
        if s.modified_not_after is not None and not -(1 << 63) <= s.modified_not_after < (1 << 63):
            raise WriteError(f"source {i}: modified_not_after out of range")

    # ---------------------------------------------------------- encode entries
    placed = {}
    for e in entries:
        if e.data is not None:
            if len(e.data) >= 0xFFFFFFFF:
                raise WriteError(f"entry {e.key!r} is too large")
            body = deflate(e.data) if e.compress else e.data
            if len(body) >= 0xFFFFFFFF:
                raise WriteError(f"entry {e.key!r} is too large when compressed")
            placed[e.key] = _Placed(e.key, 8 if e.compress else 0, zlib.crc32(e.data),
                                    len(body), len(e.data), body)
        else:
            for r in e.ranges:
                if r.data is None:
                    if not 0 <= r.source < len(sources):
                        raise WriteError(f"entry {e.key!r}: source index {r.source} out of range")
                    if r.source > pb.U32MAX:
                        raise WriteError("source index exceeds uint32")
                    if r.offset < 0 or r.length < 0 or r.offset + r.length > pb.U64MAX:
                        raise WriteError(f"entry {e.key!r}: offset + length out of range")
            if sum(r.size for r in e.ranges) > pb.U64MAX:
                raise WriteError(f"entry {e.key!r}: total size exceeds 2^64-1")
            if len(e.ranges) == 1:
                pid, payload = EXT_RANGE, pb.encode_range(e.ranges[0])
            else:
                pid, payload = EXT_CONCAT, pb.encode_concat(e.ranges)
            if len(payload) > MAX_PAYLOAD:
                raise WriteError(f"entry {e.key!r}: reference payload exceeds {MAX_PAYLOAD} bytes")
            body = payload if mirror else b""
            placed[e.key] = _Placed(e.key, 0, zlib.crc32(body), len(body), len(body), body,
                                    [(pid, payload)])

    src_raw = pb.encode_source_table(sources)
    src_body = deflate(src_raw)
    if len(src_body) >= 0xFFFFFFFF or len(src_raw) >= 0xFFFFFFFF:
        raise WriteError("source table too large")
    src = _Placed(SOURCES_KEY, 8, zlib.crc32(src_raw), len(src_body), len(src_raw), src_body)

    # ---------------------------------------------------------- layout
    out = bytearray()

    def emit(p: _Placed):
        p.offset = len(out)
        name = p.key
        out.extend(struct.pack("<IHHHHHIIIHH", SIG_LOCAL, 20, FLAG_UTF8, p.method, DOS_TIME,
                               DOS_DATE, p.crc, p.csize, p.usize, len(name), 0))
        out.extend(name)
        out.extend(p.body)

    pinned_keys = [e.key for e in entries if e.pinned]
    for e in entries:
        if not e.pinned:
            emit(placed[e.key])
    emit(src)
    for k in pinned_keys:
        emit(placed[k])

    def cd_record(p: _Placed) -> bytes:
        blocks = []
        off = p.offset
        if off >= 0xFFFFFFFF:
            blocks.append((0x0001, struct.pack("<Q", off)))
            off = 0xFFFFFFFF
        blocks.extend(p.extra_blocks)
        extra = b"".join(struct.pack("<HH", hid, len(d)) + d for hid, d in blocks)
        if len(extra) > 0xFFFF:
            raise WriteError("extra field too large")
        vneed = 45 if off == 0xFFFFFFFF else 20
        return (struct.pack("<IHHHHHHIIIHHHHHII", SIG_CDR, 20, vneed, FLAG_UTF8, p.method,
                            DOS_TIME, DOS_DATE, p.crc, p.csize, p.usize, len(p.key),
                            len(extra), 0, 0, 0, 0, off) + p.key + extra)

    body_keys = sorted(placed)  # UTF-8 byte order
    body_recs = [cd_record(placed[k]) for k in body_keys]

    idx = None
    if page_size is not None:
        cdi = pb.CdIndex()
        pos = 0
        cur_start = None
        cur_len = 0
        for k, rec in zip(body_keys, body_recs):
            if cur_start is None:
                cur_start, cur_len = (pos, 0)
                cdi.pages.append(pb.Page(k, pos, 0))
            cur_len += len(rec)
            pos += len(rec)
            cdi.pages[-1].length = cur_len
            if cur_len >= page_size:
                cur_start = None
        for k in pinned_keys:
            p = placed[k]
            cdi.pinned.append(pb.Pinned(k, p.offset + 30 + len(k), p.usize, p.csize, p.method))
        idx_raw = pb.encode_cdindex(cdi)
        idx_body = deflate(idx_raw)
        if len(idx_body) >= 0xFFFFFFFF or len(idx_raw) >= 0xFFFFFFFF:
            raise WriteError("page index too large")
        idx = _Placed(INDEX_KEY, 8, zlib.crc32(idx_raw), len(idx_body), len(idx_raw), idx_body)
        emit(idx)

    cd_off = len(out)
    for rec in body_recs:
        out.extend(rec)
    out.extend(cd_record(src))
    if idx is not None:
        out.extend(cd_record(idx))
    cd_size = len(out) - cd_off
    n = len(placed) + 1 + (1 if idx is not None else 0)

    if n >= 0xFFFF or cd_size >= 0xFFFFFFFF or cd_off >= 0xFFFFFFFF:
        z_off = len(out)
        out.extend(struct.pack("<IQHHIIQQQQ", SIG_Z64_EOCD, 44, 45, 45, 0, 0, n, n, cd_size, cd_off))
        out.extend(struct.pack("<IIQI", SIG_Z64_LOC, 0, z_off, 1))

    comment = MAGIC + struct.pack("<QQ", src.offset + 30 + len(SOURCES_KEY), src.csize)
    if idx is not None:
        comment += struct.pack("<QQ", idx.offset + 30 + len(INDEX_KEY), idx.csize)
    out.extend(struct.pack("<IHHHHIIH", SIG_EOCD, 0, 0,
                           min(n, 0xFFFF), min(n, 0xFFFF),
                           min(cd_size, 0xFFFFFFFF), min(cd_off, 0xFFFFFFFF), len(comment)))
    out.extend(comment)
    return bytes(out)
