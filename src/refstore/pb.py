"""Hand-written protobuf codec for the messages in proto/vzip.proto.

No protoc / generated code needed; the wire format is checked against the
official protobuf runtime in tests/test_pb.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_VARINT = 0
_LEN = 2


def _put_varint(out: bytearray, n: int) -> None:
    if n < 0:
        raise ValueError("negative varint")
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)


def _get_varint(buf: bytes | memoryview, pos: int) -> tuple[int, int]:
    result = shift = 0
    for _ in range(10):
        if pos >= len(buf):
            raise ValueError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            if result > 0xFFFFFFFFFFFFFFFF:
                raise ValueError("varint exceeds 2^64 - 1")
            return result, pos
        shift += 7
    raise ValueError("varint longer than 10 bytes")


def _put_tag(out: bytearray, field_no: int, wire: int) -> None:
    _put_varint(out, (field_no << 3) | wire)


def _put_bytes(out: bytearray, field_no: int, data: bytes) -> None:
    _put_tag(out, field_no, _LEN)
    _put_varint(out, len(data))
    out += data


def _put_uint(out: bytearray, field_no: int, n: int) -> None:
    if n:  # proto3: default values are not serialized
        _put_tag(out, field_no, _VARINT)
        _put_varint(out, n)


def _fields(buf: bytes | memoryview):
    """Yield (field_no, wire type, value); value is int for varints, memoryview for LEN."""
    buf = memoryview(buf)
    pos, end = 0, len(buf)
    while pos < end:
        key, pos = _get_varint(buf, pos)
        field_no, wire = key >> 3, key & 7
        if wire == _VARINT:
            val, pos = _get_varint(buf, pos)
        elif wire == _LEN:
            n, pos = _get_varint(buf, pos)
            if pos + n > end:
                raise ValueError("length-delimited field runs past the end of the message")
            val, pos = buf[pos : pos + n], pos + n
        elif wire in (1, 5):  # I64 / I32: never used by the schema, skip
            n = 8 if wire == 1 else 4
            if pos + n > end:
                raise ValueError("truncated fixed-width field")
            val, pos = bytes(buf[pos : pos + n]), pos + n
        else:
            raise ValueError(f"unsupported wire type {wire}")
        if field_no == 0 or field_no > (1 << 29) - 1:
            raise ValueError(f"invalid field number {field_no}")
        yield field_no, wire, val


def _known(buf, schema: dict[int, int]):
    """Yield (field_no, value) for fields in `schema` ({field_no: wire type}).

    Unknown field numbers are skipped; a known field with the wrong wire type
    is an error.
    """
    for f, wire, val in _fields(buf):
        if f in schema:
            if wire != schema[f]:
                raise ValueError(f"field {f} has wire type {wire}, expected {schema[f]}")
            yield f, val


@dataclass(frozen=True, slots=True)
class Range:
    """Bytes [offset, offset+length) of source table entry `source`, or literal `data`."""

    source: int = 0
    offset: int = 0
    length: int = 0
    data: bytes | None = None

    def encode(self) -> bytes:
        out = bytearray()
        _put_uint(out, 1, self.source)
        _put_uint(out, 3, self.offset)
        _put_uint(out, 4, self.length)
        if self.data is not None:
            _put_bytes(out, 5, self.data)
        return bytes(out)

    @classmethod
    def decode(cls, buf) -> Range:
        source = offset = length = 0
        data = None
        for f, v in _known(buf, {1: _VARINT, 3: _VARINT, 4: _VARINT, 5: _LEN}):
            if f == 1:
                source = _u32(v)
            elif f == 3:
                offset = v
            elif f == 4:
                length = v
            elif f == 5:
                data = bytes(v)
        if data is not None and (source or offset or length):
            raise ValueError("literal range with non-zero source/offset/length")
        if offset + length > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("offset + length exceeds 2^64 - 1")
        return cls(source, offset, length, data)

    @property
    def size(self) -> int:
        return len(self.data) if self.data is not None else self.length


@dataclass(frozen=True, slots=True)
class Concat:
    """A value made of several ranges, e.g. `[header bytes] ++ [chunk bytes]`."""

    parts: tuple[Range, ...] = field(default_factory=tuple)

    def encode(self) -> bytes:
        out = bytearray()
        for r in self.parts:
            _put_bytes(out, 1, r.encode())
        return bytes(out)

    @classmethod
    def decode(cls, buf) -> Concat:
        c = cls(tuple(Range.decode(v) for _, v in _known(buf, {1: _LEN})))
        if c.size > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("concat size exceeds 2^64 - 1")
        return c

    @property
    def size(self) -> int:
        return sum(r.size for r in self.parts)


Reference = Range | Concat


def parts(ref: Reference) -> tuple[Range, ...]:
    return ref.parts if isinstance(ref, Concat) else (ref,)


@dataclass(frozen=True, slots=True)
class Source:
    """One entry of the archive's source table. Exactly one field is set."""

    url: str | None = None  # external object; may be relative to the archive
    key: str | None = None  # another entry of this archive (internal reference)
    data: bytes | None = None  # literal bytes, e.g. a shared decoding header

    def __post_init__(self) -> None:
        if sum(x is not None for x in (self.url, self.key, self.data)) != 1:
            raise ValueError("a Source has exactly one of url, key, data")

    def encode(self) -> bytes:
        out = bytearray()
        if self.url is not None:
            _put_bytes(out, 1, self.url.encode())
        elif self.key is not None:
            _put_bytes(out, 2, self.key.encode())
        else:
            _put_bytes(out, 3, self.data)
        return bytes(out)

    @classmethod
    def decode(cls, buf) -> Source:
        kind = None
        for f, v in _known(buf, {1: _LEN, 2: _LEN, 3: _LEN}):
            kind = (f, v)  # oneof: the last member on the wire wins
        if kind is None:
            raise ValueError("Source has no kind set")
        f, v = kind
        if f == 1:
            return cls(url=_utf8(v))
        if f == 2:
            return cls(key=_utf8(v))
        return cls(data=bytes(v))


def _u32(v: int) -> int:
    if v > 0xFFFFFFFF:
        raise ValueError("uint32 field exceeds 2^32 - 1")
    return v


def _utf8(v) -> str:
    try:
        return bytes(v).decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"invalid UTF-8 in string field: {e}") from None


def encode_source_table(sources: list[Source]) -> bytes:
    out = bytearray()
    for src in sources:
        _put_bytes(out, 1, src.encode())
    return bytes(out)


def decode_source_table(buf) -> list[Source]:
    return [Source.decode(v) for _, v in _known(buf, {1: _LEN})]


@dataclass(frozen=True, slots=True)
class Page:
    """A slice of the (sorted) central directory, relative to its start."""

    first_key: str
    offset: int
    length: int


@dataclass(frozen=True, slots=True)
class Pinned:
    """Location of an entry the reader should know without loading any page."""

    key: str
    data_offset: int
    size: int
    csize: int
    method: int


def encode_cd_index(pages: list[Page], pinned: list[Pinned]) -> bytes:
    out = bytearray()
    for p in pages:
        sub = bytearray()
        _put_bytes(sub, 1, p.first_key.encode())
        _put_uint(sub, 2, p.offset)
        _put_uint(sub, 3, p.length)
        _put_bytes(out, 1, bytes(sub))
    for e in pinned:
        sub = bytearray()
        _put_bytes(sub, 1, e.key.encode())
        _put_uint(sub, 2, e.data_offset)
        _put_uint(sub, 3, e.size)
        _put_uint(sub, 4, e.csize)
        _put_uint(sub, 5, e.method)
        _put_bytes(out, 2, bytes(sub))
    return bytes(out)


def decode_cd_index(buf) -> tuple[list[Page], list[Pinned]]:
    pages, pinned = [], []
    sub = {1: _LEN, 2: _VARINT, 3: _VARINT, 4: _VARINT, 5: _VARINT}
    for f, v in _known(buf, {1: _LEN, 2: _LEN}):
        vals = dict(_known(v, sub if f == 2 else {1: _LEN, 2: _VARINT, 3: _VARINT}))
        name = _utf8(vals.get(1, b""))
        if f == 1:
            pages.append(Page(name, vals.get(2, 0), vals.get(3, 0)))
        elif f == 2:
            pinned.append(Pinned(name, vals.get(2, 0), vals.get(3, 0), vals.get(4, 0),
                                 _u32(vals.get(5, 0))))
    return pages, pinned
