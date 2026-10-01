"""Hand-written protobuf wire format encoding/decoding for the vzip schema (spec §5, Appendix A)."""

from __future__ import annotations

from dataclasses import dataclass, field

U64_MAX = (1 << 64) - 1
U32_MAX = (1 << 32) - 1
MAX_FIELD = (1 << 29) - 1

WT_VARINT = 0
WT_I64 = 1
WT_LEN = 2
WT_I32 = 5


class Malformed(Exception):
    """A message violates the decoding rules of spec §5.1."""


# ---------------------------------------------------------------- decoding


def _read_varint(buf: bytes, pos: int, end: int) -> tuple[int, int]:
    result = 0
    shift = 0
    for i in range(10):
        if pos >= end:
            raise Malformed("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            if result > U64_MAX:
                raise Malformed("varint exceeds 2^64-1")
            return result, pos
    raise Malformed("varint longer than 10 bytes")


def iter_fields(buf: bytes, start: int = 0, end: int | None = None):
    """Yield (field_number, wire_type, value) for every field of a message.

    value is an int for VARINT/I64/I32 and a bytes object for LEN.
    Structural problems raise Malformed.
    """
    if end is None:
        end = len(buf)
    pos = start
    while pos < end:
        tag, pos = _read_varint(buf, pos, end)
        fnum = tag >> 3
        wt = tag & 7
        if fnum == 0 or fnum > MAX_FIELD:
            raise Malformed(f"invalid field number {fnum}")
        if wt == WT_VARINT:
            v, pos = _read_varint(buf, pos, end)
        elif wt == WT_I64:
            if pos + 8 > end:
                raise Malformed("truncated I64")
            v = int.from_bytes(buf[pos:pos + 8], "little")
            pos += 8
        elif wt == WT_I32:
            if pos + 4 > end:
                raise Malformed("truncated I32")
            v = int.from_bytes(buf[pos:pos + 4], "little")
            pos += 4
        elif wt == WT_LEN:
            n, pos = _read_varint(buf, pos, end)
            if n > end - pos:
                raise Malformed("LEN field extends past end of message")
            v = bytes(buf[pos:pos + n])
            pos += n
        else:
            raise Malformed(f"invalid wire type {wt}")
        yield fnum, wt, v


def _expect(wt: int, want: int, fnum: int, msg: str) -> None:
    if wt != want:
        raise Malformed(f"{msg} field {fnum} has wire type {wt}, expected {want}")


def _str(v: bytes, what: str) -> str:
    try:
        return v.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise Malformed(f"{what} is not valid UTF-8") from None


def _u32(v: int, what: str) -> int:
    if v > U32_MAX:
        raise Malformed(f"{what} exceeds 2^32-1")
    return v


def _i64(v: int) -> int:
    return v - (1 << 64) if v >= (1 << 63) else v


@dataclass
class Range:
    source: int = 0
    offset: int = 0
    length: int = 0
    data: bytes | None = None

    @property
    def is_literal(self) -> bool:
        return self.data is not None

    @property
    def size(self) -> int:
        return len(self.data) if self.data is not None else self.length


@dataclass
class Source:
    kind: str | None = None  # 'url' | 'key' | 'data'
    value: object = None  # str for url/key, bytes for data
    size: int | None = None
    etag: str | None = None
    modified_not_after: int | None = None

    def has_pins(self) -> bool:
        return self.size is not None or self.etag is not None or self.modified_not_after is not None


@dataclass
class Page:
    first_key: str = ""
    offset: int = 0
    length: int = 0


@dataclass
class Pinned:
    key: str = ""
    data_offset: int = 0
    size: int = 0
    csize: int = 0
    method: int = 0


@dataclass
class CdIndex:
    pages: list = field(default_factory=list)
    pinned: list = field(default_factory=list)


def decode_range(buf: bytes) -> Range:
    r = Range()
    for fnum, wt, v in iter_fields(buf):
        if fnum == 1:
            _expect(wt, WT_VARINT, fnum, "Range")
            r.source = _u32(v, "Range.source")
        elif fnum == 3:
            _expect(wt, WT_VARINT, fnum, "Range")
            r.offset = v
        elif fnum == 4:
            _expect(wt, WT_VARINT, fnum, "Range")
            r.length = v
        elif fnum == 5:
            _expect(wt, WT_LEN, fnum, "Range")
            r.data = v
        # field 2 is reserved: skipped like unknown fields
    return r


def decode_concat(buf: bytes) -> list[Range]:
    parts = []
    for fnum, wt, v in iter_fields(buf):
        if fnum == 1:
            _expect(wt, WT_LEN, fnum, "Concat")
            parts.append(decode_range(v))
    return parts


def decode_source(buf: bytes) -> Source:
    s = Source()
    for fnum, wt, v in iter_fields(buf):
        if fnum == 1:
            _expect(wt, WT_LEN, fnum, "Source")
            s.kind, s.value = "url", _str(v, "Source.url")
        elif fnum == 2:
            _expect(wt, WT_LEN, fnum, "Source")
            s.kind, s.value = "key", _str(v, "Source.key")
        elif fnum == 3:
            _expect(wt, WT_LEN, fnum, "Source")
            s.kind, s.value = "data", v
        elif fnum == 4:
            _expect(wt, WT_VARINT, fnum, "Source")
            s.size = v
        elif fnum == 5:
            _expect(wt, WT_LEN, fnum, "Source")
            s.etag = _str(v, "Source.etag")
        elif fnum == 6:
            _expect(wt, WT_VARINT, fnum, "Source")
            s.modified_not_after = _i64(v)
    return s


def decode_source_table(buf: bytes) -> list[Source]:
    out = []
    for fnum, wt, v in iter_fields(buf):
        if fnum == 1:
            _expect(wt, WT_LEN, fnum, "SourceTable")
            out.append(decode_source(v))
    return out


def decode_page(buf: bytes) -> Page:
    p = Page()
    for fnum, wt, v in iter_fields(buf):
        if fnum == 1:
            _expect(wt, WT_LEN, fnum, "Page")
            p.first_key = _str(v, "Page.first_key")
        elif fnum == 2:
            _expect(wt, WT_VARINT, fnum, "Page")
            p.offset = v
        elif fnum == 3:
            _expect(wt, WT_VARINT, fnum, "Page")
            p.length = v
    return p


def decode_pinned(buf: bytes) -> Pinned:
    p = Pinned()
    for fnum, wt, v in iter_fields(buf):
        if fnum == 1:
            _expect(wt, WT_LEN, fnum, "Pinned")
            p.key = _str(v, "Pinned.key")
        elif fnum in (2, 3, 4):
            _expect(wt, WT_VARINT, fnum, "Pinned")
            setattr(p, {2: "data_offset", 3: "size", 4: "csize"}[fnum], v)
        elif fnum == 5:
            _expect(wt, WT_VARINT, fnum, "Pinned")
            p.method = _u32(v, "Pinned.method")
    return p


def decode_cd_index(buf: bytes) -> CdIndex:
    idx = CdIndex()
    for fnum, wt, v in iter_fields(buf):
        if fnum == 1:
            _expect(wt, WT_LEN, fnum, "CdIndex")
            idx.pages.append(decode_page(v))
        elif fnum == 2:
            _expect(wt, WT_LEN, fnum, "CdIndex")
            idx.pinned.append(decode_pinned(v))
    return idx


# ---------------------------------------------------------------- encoding


def enc_varint(n: int) -> bytes:
    if n < 0:
        n += 1 << 64
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(fnum: int, wt: int) -> bytes:
    return enc_varint((fnum << 3) | wt)


def f_varint(fnum: int, v: int, always: bool = False) -> bytes:
    if v == 0 and not always:
        return b""
    return _tag(fnum, WT_VARINT) + enc_varint(v)


def f_len(fnum: int, v: bytes, always: bool = False) -> bytes:
    if not v and not always:
        return b""
    return _tag(fnum, WT_LEN) + enc_varint(len(v)) + v


def encode_range(r: Range) -> bytes:
    out = f_varint(1, r.source) + f_varint(3, r.offset) + f_varint(4, r.length)
    if r.data is not None:
        out += f_len(5, r.data, always=True)
    return out


def encode_concat(parts: list[Range]) -> bytes:
    # A repeated message element is always emitted, even if its encoding is empty.
    return b"".join(f_len(1, encode_range(p), always=True) for p in parts)


def encode_source(s: Source) -> bytes:
    out = b""
    if s.kind == "url":
        out += f_len(1, s.value.encode("utf-8"), always=True)
    elif s.kind == "key":
        out += f_len(2, s.value.encode("utf-8"), always=True)
    elif s.kind == "data":
        out += f_len(3, s.value, always=True)
    if s.size is not None:
        out += f_varint(4, s.size, always=True)
    if s.etag is not None:
        out += f_len(5, s.etag.encode("utf-8"), always=True)
    if s.modified_not_after is not None:
        out += f_varint(6, s.modified_not_after, always=True)
    return out


def encode_source_table(sources: list[Source]) -> bytes:
    return b"".join(f_len(1, encode_source(s), always=True) for s in sources)


def encode_cd_index(idx: CdIndex) -> bytes:
    out = b""
    for p in idx.pages:
        m = f_len(1, p.first_key.encode("utf-8")) + f_varint(2, p.offset) + f_varint(3, p.length)
        out += f_len(1, m, always=True)
    for p in idx.pinned:
        m = (f_len(1, p.key.encode("utf-8")) + f_varint(2, p.data_offset) + f_varint(3, p.size)
             + f_varint(4, p.csize) + f_varint(5, p.method))
        out += f_len(2, m, always=True)
    return out
