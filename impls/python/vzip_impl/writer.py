"""vzip writer (spec §9) and the harness JSON description parser."""

from __future__ import annotations

import os
import re
import struct
import zlib
from dataclasses import dataclass, field

from . import proto
from .fetch import is_strong_etag
from .uri import is_uri_reference

U64_MAX = proto.U64_MAX
I64_MIN, I64_MAX = -(1 << 63), (1 << 63) - 1
SOURCES_KEY = b"__vz__/sources"
INDEX_KEY = b"__vz__/index"
FORMAT_KEYS = (SOURCES_KEY, INDEX_KEY)
MAX_PAYLOAD = 65519
DOS_DATE = (0 << 9) | (1 << 5) | 1  # 1980-01-01
DOS_TIME = 0
FLAG_UTF8 = 0x0800


class WriteInputError(Exception):
    """The writer's input is invalid (spec §9.1); no archive is produced."""


@dataclass
class WEntry:
    key: str
    data: bytes | None = None  # bytes entry
    ranges: list | None = None  # reference entry: list[proto.Range]
    compress: bool = False
    pinned: bool = False


@dataclass
class _Out:
    key: bytes
    method: int
    crc: int
    csize: int
    usize: int
    body: bytes
    extra_ref: bytes = b""  # complete extra block for the reference payload, if any
    offset: int = 0

    @property
    def body_offset(self) -> int:
        return self.offset + 30 + len(self.key)


def _deflate(data: bytes) -> bytes:
    c = zlib.compressobj(6, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


def _key_bytes(key) -> bytes:
    if not isinstance(key, str):
        raise WriteInputError(f"key {key!r} is not a string")
    try:
        k = key.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise WriteInputError(f"key {key!r} is not valid UTF-8") from None
    if not k:
        raise WriteInputError("empty key")
    if len(k) > 65535:
        raise WriteInputError("key longer than 65535 bytes")
    return k


def build_archive(entries: list[WEntry], sources: list[proto.Source], page_size: int | None,
                  mirror: bool = True) -> bytes:
    """Validate the input (spec §9.1) and return the archive bytes."""
    # ---- validation
    nsrc = len(sources)
    keys: dict[bytes, WEntry] = {}
    for e in entries:
        k = _key_bytes(e.key)
        if k in FORMAT_KEYS:
            raise WriteInputError(f"key {e.key!r} is reserved for a format entry")
        if k in keys:
            raise WriteInputError(f"duplicate key {e.key!r}")
        keys[k] = e
        if (e.data is None) == (e.ranges is None):
            raise WriteInputError(f"entry {e.key!r} must have exactly one of bytes and ranges")
        if e.pinned:
            if page_size is None:
                raise WriteInputError(f"entry {e.key!r} is pinned but there is no page index")
            if e.data is None:
                raise WriteInputError(f"pinned entry {e.key!r} is not a bytes entry")
        if e.ranges is not None and e.compress:
            raise WriteInputError(f"reference entry {e.key!r} cannot be compressed")
    for i, s in enumerate(sources):
        if s.kind not in ("url", "key", "data"):
            raise WriteInputError(f"source {i} has no kind")
        if s.kind == "url":
            if not s.value:
                raise WriteInputError(f"source {i} has an empty url")
            if not is_uri_reference(s.value):
                raise WriteInputError(f"source {i} url {s.value!r} is not an RFC 3986 URI-reference")
        else:
            if s.has_pins():
                raise WriteInputError(f"source {i} is a {s.kind} source and cannot carry pins")
        if s.kind == "key":
            try:
                k = s.value.encode("utf-8")
            except UnicodeEncodeError:
                raise WriteInputError(f"source {i} key is not valid UTF-8") from None
            if k in FORMAT_KEYS:
                raise WriteInputError(f"source {i} names a format entry")
            target = keys.get(k)
            if target is None:
                raise WriteInputError(f"source {i} names absent key {s.value!r}")
            if target.data is None:
                raise WriteInputError(f"source {i} names reference entry {s.value!r}")
        if s.etag is not None and not is_strong_etag(s.etag):
            raise WriteInputError(f"source {i} etag {s.etag!r} is not a strong entity tag")
        if s.size is not None and not 0 <= s.size <= U64_MAX:
            raise WriteInputError(f"source {i} size pin out of range")
        if s.modified_not_after is not None and not I64_MIN <= s.modified_not_after <= I64_MAX:
            raise WriteInputError(f"source {i} modified_not_after out of range")
    if page_size is not None and page_size < 1:
        raise WriteInputError("page_size must be at least 1")

    outs: list[_Out] = []
    pinned_flags: dict[bytes, bool] = {}
    for e in entries:
        k = e.key.encode("utf-8")
        pinned_flags[k] = e.pinned
        if e.data is not None:
            if len(e.data) >= 0xFFFFFFFF:
                raise WriteInputError(f"bytes entry {e.key!r} is 4 GiB or larger")
            if e.compress:
                body = _deflate(e.data)
                method = 8
            else:
                body = e.data
                method = 0
            if len(body) >= 0xFFFFFFFF:
                raise WriteInputError(f"bytes entry {e.key!r} compresses to 4 GiB or more")
            outs.append(_Out(k, method, zlib.crc32(e.data), len(body), len(e.data), body))
        else:
            total = 0
            for j, r in enumerate(e.ranges):
                if r.data is None:
                    if r.source >= nsrc:
                        raise WriteInputError(f"entry {e.key!r} range {j}: source {r.source} out of range")
                    if r.source > proto.U32_MAX:
                        raise WriteInputError(f"entry {e.key!r} range {j}: source index too large")
                    if r.offset < 0 or r.length < 0 or r.offset + r.length > U64_MAX:
                        raise WriteInputError(f"entry {e.key!r} range {j}: offset + length exceeds 2^64-1")
                total += r.size
            if total > U64_MAX:
                raise WriteInputError(f"entry {e.key!r}: total size exceeds 2^64-1")
            if len(e.ranges) == 1:
                hid, payload = 0x7A76, proto.encode_range(e.ranges[0])
            else:
                hid, payload = 0x7A77, proto.encode_concat(e.ranges)
            if len(payload) > MAX_PAYLOAD:
                raise WriteInputError(f"entry {e.key!r}: reference payload of {len(payload)} bytes exceeds 65519")
            body = payload if mirror else b""
            outs.append(_Out(k, 0, zlib.crc32(body), len(body), len(body), body,
                             extra_ref=struct.pack("<HH", hid, len(payload)) + payload))

    # ---- layout
    regular = [o for o in outs if not pinned_flags[o.key]]
    pinned = [o for o in outs if pinned_flags[o.key]]
    src_raw = proto.encode_source_table(sources)
    src_body = _deflate(src_raw)
    src_out = _Out(SOURCES_KEY, 8, zlib.crc32(src_raw), len(src_body), len(src_raw), src_body)

    buf = bytearray()

    def emit(o: _Out) -> None:
        o.offset = len(buf)
        need = 45 if o.offset >= 0xFFFFFFFF else 20
        buf.extend(struct.pack("<IHHHHHIIIHH", 0x04034B50, need, FLAG_UTF8, o.method, DOS_TIME, DOS_DATE,
                               o.crc, o.csize, o.usize, len(o.key), 0))
        buf.extend(o.key)
        buf.extend(o.body)

    for o in regular:
        emit(o)
    emit(src_out)
    for o in pinned:
        emit(o)

    def cd_record(o: _Out) -> bytes:
        extra = b""
        if o.offset >= 0xFFFFFFFF:
            extra += struct.pack("<HHQ", 0x0001, 8, o.offset)
            loff, need = 0xFFFFFFFF, 45
        else:
            loff, need = o.offset, 20
        extra += o.extra_ref
        if len(extra) > 65535:
            raise WriteInputError(f"extra field of {o.key!r} too long")
        return (struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, need, FLAG_UTF8, o.method, DOS_TIME,
                            DOS_DATE, o.crc, o.csize, o.usize, len(o.key), len(extra), 0, 0, 0, 0, loff)
                + o.key + extra)

    body_sorted = sorted(outs, key=lambda o: o.key)
    body_recs = [cd_record(o) for o in body_sorted]

    idx_out = None
    if page_size is not None:
        idx = proto.CdIndex()
        pos = 0
        cur_start = 0
        cur_first = None
        cur_len = 0
        for o, rec in zip(body_sorted, body_recs):
            if cur_first is None:
                cur_first, cur_start, cur_len = o.key, pos, 0
            cur_len += len(rec)
            pos += len(rec)
            if cur_len >= page_size:
                idx.pages.append(proto.Page(cur_first.decode("utf-8"), cur_start, cur_len))
                cur_first = None
        if cur_first is not None:
            idx.pages.append(proto.Page(cur_first.decode("utf-8"), cur_start, cur_len))
        for o in pinned:
            idx.pinned.append(proto.Pinned(o.key.decode("utf-8"), o.body_offset, o.usize, o.csize, o.method))
        idx_raw = proto.encode_cd_index(idx)
        idx_body = _deflate(idx_raw)
        idx_out = _Out(INDEX_KEY, 8, zlib.crc32(idx_raw), len(idx_body), len(idx_raw), idx_body)
        emit(idx_out)

    cd_off = len(buf)
    for rec in body_recs:
        buf.extend(rec)
    buf.extend(cd_record(src_out))
    if idx_out is not None:
        buf.extend(cd_record(idx_out))
    cd_size = len(buf) - cd_off
    n = len(outs) + 1 + (1 if idx_out is not None else 0)

    if n >= 0xFFFF or cd_size >= 0xFFFFFFFF or cd_off >= 0xFFFFFFFF:
        z64_off = len(buf)
        buf.extend(struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0, n, n, cd_size, cd_off))
        buf.extend(struct.pack("<IIQI", 0x07064B50, 0, z64_off, 1))
    comment = b"vzip/0" + struct.pack("<QQ", src_out.body_offset, src_out.csize)
    if idx_out is not None:
        comment += struct.pack("<QQ", idx_out.body_offset, idx_out.csize)
    n16 = min(n, 0xFFFF)
    buf.extend(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, n16, n16, min(cd_size, 0xFFFFFFFF),
                           min(cd_off, 0xFFFFFFFF), len(comment)))
    buf.extend(comment)
    return bytes(buf)


def write_archive(path, entries, sources, page_size=None, mirror=True) -> None:
    """Build and write an archive; on invalid input raise WriteInputError and create no file."""
    data = build_archive(entries, sources, page_size, mirror)
    with open(path, "xb") as f:
        f.write(data)


# ------------------------------------------------------------------ harness description

_HEX_RE = re.compile(r"(?:[0-9a-f]{2})*")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _get(obj: dict, name: str, default, check, what: str):
    if name not in obj:
        return default
    v = obj[name]
    if v is None:
        raise WriteInputError(f"{what}.{name} is null")
    if not check(v):
        raise WriteInputError(f"{what}.{name} has the wrong type: {v!r}")
    return v


def _hex(v, what: str) -> bytes:
    if not isinstance(v, str) or not _HEX_RE.fullmatch(v):
        raise WriteInputError(f"{what} is not a lowercase even-length hex string")
    return bytes.fromhex(v)


def _nonneg_int(v) -> bool:
    return _is_int(v) and v >= 0


def parse_description(desc) -> tuple[list[WEntry], list[proto.Source], int | None, bool]:
    if not isinstance(desc, dict):
        raise WriteInputError("description is not a JSON object")
    if "page_size" in desc and desc["page_size"] is not None:
        ps = desc["page_size"]
        if not _is_int(ps) or ps < 1:
            raise WriteInputError("page_size must be null or an integer >= 1")
    else:
        ps = None
    mirror = _get(desc, "mirror", True, lambda v: isinstance(v, bool), "description")
    srcs_j = _get(desc, "sources", [], lambda v: isinstance(v, list), "description")
    ents_j = _get(desc, "entries", [], lambda v: isinstance(v, list), "description")

    sources = []
    for i, sj in enumerate(srcs_j):
        what = f"sources[{i}]"
        if not isinstance(sj, dict):
            raise WriteInputError(f"{what} is not an object")
        kinds = [k for k in ("url", "key", "data") if k in sj]
        if len(kinds) != 1:
            raise WriteInputError(f"{what} must have exactly one of url, key, data")
        kind = kinds[0]
        v = sj[kind]
        if v is None:
            raise WriteInputError(f"{what}.{kind} is null")
        if kind == "data":
            value = _hex(v, f"{what}.data")
        else:
            if not isinstance(v, str):
                raise WriteInputError(f"{what}.{kind} is not a string")
            value = v
        s = proto.Source(kind=kind, value=value)
        s.size = _get(sj, "size", None, _nonneg_int, what)
        s.etag = _get(sj, "etag", None, lambda x: isinstance(x, str), what)
        s.modified_not_after = _get(sj, "modified_not_after", None, _is_int, what)
        sources.append(s)

    entries = []
    for i, ej in enumerate(ents_j):
        what = f"entries[{i}]"
        if not isinstance(ej, dict):
            raise WriteInputError(f"{what} is not an object")
        if "key" not in ej or not isinstance(ej["key"], str):
            raise WriteInputError(f"{what}.key is missing or not a string")
        has_b, has_r = "bytes" in ej, "ranges" in ej
        if has_b == has_r:
            raise WriteInputError(f"{what} must have exactly one of bytes and ranges")
        compress = _get(ej, "compress", False, lambda v: isinstance(v, bool), what)
        pinned = _get(ej, "pinned", False, lambda v: isinstance(v, bool), what)
        e = WEntry(key=ej["key"], compress=compress, pinned=pinned)
        if has_b:
            if ej["bytes"] is None:
                raise WriteInputError(f"{what}.bytes is null")
            e.data = _hex(ej["bytes"], f"{what}.bytes")
        else:
            rj = ej["ranges"]
            if not isinstance(rj, list):
                raise WriteInputError(f"{what}.ranges is not a list")
            e.ranges = []
            for j, r in enumerate(rj):
                rw = f"{what}.ranges[{j}]"
                if not isinstance(r, dict):
                    raise WriteInputError(f"{rw} is not an object")
                if "data" in r:
                    if any(k in r for k in ("source", "offset", "length")):
                        raise WriteInputError(f"{rw} mixes a literal and a source range")
                    if r["data"] is None:
                        raise WriteInputError(f"{rw}.data is null")
                    e.ranges.append(proto.Range(data=_hex(r["data"], f"{rw}.data")))
                else:
                    e.ranges.append(proto.Range(
                        source=_get(r, "source", 0, _nonneg_int, rw),
                        offset=_get(r, "offset", 0, _nonneg_int, rw),
                        length=_get(r, "length", 0, _nonneg_int, rw)))
        entries.append(e)
    return entries, sources, ps, mirror
