"""Hand-written protobuf wire format for the vzip messages (spec section 5)."""

from dataclasses import dataclass, field
from typing import Optional

U64MAX = (1 << 64) - 1
U32MAX = (1 << 32) - 1
MAX_FIELD = (1 << 29) - 1


class Malformed(Exception):
    pass


# ---------------------------------------------------------------- decoding

def _varint(buf, pos, end):
    result = 0
    shift = 0
    for _ in range(10):
        if pos >= end:
            raise Malformed("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            if result > U64MAX:
                raise Malformed("varint exceeds 2^64-1")
            return result, pos
        shift += 7
    raise Malformed("varint longer than 10 bytes")


def iter_fields(buf):
    """Yield (field_number, wire_type, value) for every field of a message."""
    buf = bytes(buf)
    pos = 0
    end = len(buf)
    while pos < end:
        key, pos = _varint(buf, pos, end)
        wt = key & 7
        fn = key >> 3
        if wt in (3, 4, 6, 7):
            raise Malformed(f"unsupported wire type {wt}")
        if fn == 0 or fn > MAX_FIELD:
            raise Malformed(f"invalid field number {fn}")
        if wt == 0:
            v, pos = _varint(buf, pos, end)
        elif wt == 1:
            if pos + 8 > end:
                raise Malformed("truncated I64")
            v = buf[pos:pos + 8]
            pos += 8
        elif wt == 2:
            ln, pos = _varint(buf, pos, end)
            if ln > end - pos:
                raise Malformed("LEN field extends past end of message")
            v = buf[pos:pos + ln]
            pos += ln
        else:  # 5
            if pos + 4 > end:
                raise Malformed("truncated I32")
            v = buf[pos:pos + 4]
            pos += 4
        yield fn, wt, v


INT_TYPES = ("uint32", "uint64", "int64")


def _convert(typ, wt, v):
    expected = 0 if typ in INT_TYPES else 2
    if wt != expected:
        raise Malformed(f"wire type {wt} for field of type {typ}")
    if typ == "uint32":
        if v > U32MAX:
            raise Malformed("uint32 value exceeds 2^32-1")
    elif typ == "int64":
        if v >= 1 << 63:
            v -= 1 << 64
    elif typ == "string":
        try:
            bytes(v).decode("utf-8", "strict")
        except UnicodeDecodeError:
            raise Malformed("string is not valid UTF-8") from None
        v = bytes(v)  # strings are kept as their (validated) UTF-8 bytes
    return v


# ---------------------------------------------------------------- messages

@dataclass
class Range:
    source: int = 0
    offset: int = 0
    length: int = 0
    data: Optional[bytes] = None  # present => literal range

    @property
    def size(self):
        return len(self.data) if self.data is not None else self.length


def decode_range(buf):
    r = Range()
    for fn, wt, v in iter_fields(buf):
        if fn == 1:
            r.source = _convert("uint32", wt, v)
        elif fn == 3:
            r.offset = _convert("uint64", wt, v)
        elif fn == 4:
            r.length = _convert("uint64", wt, v)
        elif fn == 5:
            r.data = _convert("bytes", wt, v)
        # field 2 is reserved, skipped like any unknown field
    return r


def decode_concat(buf):
    parts = []
    for fn, wt, v in iter_fields(buf):
        if fn == 1:
            parts.append(decode_range(_convert("bytes", wt, v)))
    return parts


def validate_ranges(parts, nsources):
    """Semantic checks of section 5.2/5.3; raises Malformed."""
    total = 0
    for r in parts:
        if r.data is not None:
            if r.source or r.offset or r.length:
                raise Malformed("literal range with non-zero source/offset/length")
        else:
            if r.source >= nsources:
                raise Malformed(f"source index {r.source} out of range ({nsources} sources)")
            if r.offset + r.length > U64MAX:
                raise Malformed("offset + length exceeds 2^64-1")
        total += r.size
    if total > U64MAX:
        raise Malformed("concat size exceeds 2^64-1")
    return total


@dataclass
class Source:
    kind: Optional[str] = None  # 'url' | 'key' | 'data'
    value: Optional[bytes] = None
    size: Optional[int] = None
    etag: Optional[bytes] = None
    modified_not_after: Optional[int] = None

    def has_pins(self):
        return self.size is not None or self.etag is not None or self.modified_not_after is not None


def decode_source(buf):
    s = Source()
    for fn, wt, v in iter_fields(buf):
        if fn == 1:
            s.kind, s.value = "url", _convert("string", wt, v)
        elif fn == 2:
            s.kind, s.value = "key", _convert("string", wt, v)
        elif fn == 3:
            s.kind, s.value = "data", _convert("bytes", wt, v)
        elif fn == 4:
            s.size = _convert("uint64", wt, v)
        elif fn == 5:
            s.etag = _convert("string", wt, v)
        elif fn == 6:
            s.modified_not_after = _convert("int64", wt, v)
    return s


def decode_source_table(buf):
    out = []
    for fn, wt, v in iter_fields(buf):
        if fn == 1:
            out.append(decode_source(_convert("bytes", wt, v)))
    return out


@dataclass
class Page:
    first_key: bytes = b""
    offset: int = 0
    length: int = 0


@dataclass
class Pinned:
    key: bytes = b""
    data_offset: int = 0
    size: int = 0
    csize: int = 0
    method: int = 0


@dataclass
class CdIndex:
    pages: list = field(default_factory=list)
    pinned: list = field(default_factory=list)


def decode_cdindex(buf):
    idx = CdIndex()
    for fn, wt, v in iter_fields(buf):
        if fn == 1:
            p = Page()
            for f2, w2, v2 in iter_fields(_convert("bytes", wt, v)):
                if f2 == 1:
                    p.first_key = _convert("string", w2, v2)
                elif f2 == 2:
                    p.offset = _convert("uint64", w2, v2)
                elif f2 == 3:
                    p.length = _convert("uint64", w2, v2)
            idx.pages.append(p)
        elif fn == 2:
            p = Pinned()
            for f2, w2, v2 in iter_fields(_convert("bytes", wt, v)):
                if f2 == 1:
                    p.key = _convert("string", w2, v2)
                elif f2 == 2:
                    p.data_offset = _convert("uint64", w2, v2)
                elif f2 == 3:
                    p.size = _convert("uint64", w2, v2)
                elif f2 == 4:
                    p.csize = _convert("uint64", w2, v2)
                elif f2 == 5:
                    p.method = _convert("uint32", w2, v2)
            idx.pinned.append(p)
    return idx


# ---------------------------------------------------------------- encoding

def enc_varint(v):
    if v < 0:
        v += 1 << 64
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _key(fn, wt):
    return enc_varint((fn << 3) | wt)


def f_int(fn, v, always=False):
    if v == 0 and not always:
        return b""
    return _key(fn, 0) + enc_varint(v)


def f_len(fn, b, always=False):
    if not b and not always:
        return b""
    return _key(fn, 2) + enc_varint(len(b)) + bytes(b)


def encode_range(r):
    return (f_int(1, r.source) + f_int(3, r.offset) + f_int(4, r.length)
            + (f_len(5, r.data, always=True) if r.data is not None else b""))


def encode_concat(parts):
    return b"".join(f_len(1, encode_range(r), always=True) for r in parts)


def encode_source(s):
    fn = {"url": 1, "key": 2, "data": 3}[s.kind]
    out = f_len(fn, s.value, always=True)
    if s.size is not None:
        out += f_int(4, s.size, always=True)
    if s.etag is not None:
        out += f_len(5, s.etag, always=True)
    if s.modified_not_after is not None:
        out += f_int(6, s.modified_not_after, always=True)
    return out


def encode_source_table(sources):
    return b"".join(f_len(1, encode_source(s), always=True) for s in sources)


def encode_cdindex(idx):
    out = b""
    for p in idx.pages:
        out += f_len(1, f_len(1, p.first_key) + f_int(2, p.offset) + f_int(3, p.length), always=True)
    for p in idx.pinned:
        body = (f_len(1, p.key) + f_int(2, p.data_offset) + f_int(3, p.size)
                + f_int(4, p.csize) + f_int(5, p.method))
        out += f_len(2, body, always=True)
    return out
