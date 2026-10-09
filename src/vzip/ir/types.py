"""The declared types of `value` elements, and the one generic decoder.

A type is a string, decoded without any format code:

    type   = base ["[" n {"," n} "]"]
    base   = ["<" | ">"] num | "ascii" | "cstr" | "bytes" | "guid" | "{" field {"," field} "}"
    num    = "u1" | "i1" | "u2" | "i2" | "u4" | "i4" | "u8" | "i8" | "f4" | "f8"
    field  = name ":" type

- numbers decode to ints and floats; with dimensions, to nested lists;
- `ascii[n]` is n bytes of text (UTF-8 when valid, else the bytes), `cstr[n]`
  the same up to the first NUL; `bytes[n]` stays bytes;
- `guid` is 16 bytes written as Windows writes a GUID;
- a record `{...}` decodes to an object of its fields, in order.

Every `value` element's decoded value is `decode(type, its bytes)`.
"""

from __future__ import annotations

import re
import struct
from functools import lru_cache

NUMS = {"u1": "B", "i1": "b", "u2": "H", "i2": "h", "u4": "I", "i4": "i", "u8": "Q", "i8": "q", "f4": "f", "f8": "d"}
# The Zarr data type of each number, for the mirror's arrays.
DTYPES = {"u1": "uint8", "i1": "int8", "u2": "uint16", "i2": "int16", "u4": "uint32", "i4": "int32",
          "u8": "uint64", "i8": "int64", "f4": "float32", "f8": "float64"}
TOKEN = re.compile(r"\s*([{}\[\],:]|[<>]?[A-Za-z_][A-Za-z0-9_]*|[0-9]+)")


class Type:
    """A parsed type: its size in bytes and how it decodes."""

    def __init__(self, kind: str, size: int, *, code: str = "", order: str = "<", dims: tuple = (),
                 fields: tuple = (), num: str = "") -> None:
        self.kind, self.size, self.code, self.order, self.dims, self.fields, self.num = (
            kind, size, code, order, dims, fields, num)

    @property
    def dtype(self) -> str | None:
        """The Zarr data type of a numeric type's elements."""
        return DTYPES[self.num] if self.kind == "num" else None

    @property
    def endian(self) -> str:
        return "big" if self.order == ">" else "little"

    def decode(self, raw: bytes):
        if len(raw) != self.size:
            raise ValueError(f"{len(raw)} bytes for a type of {self.size}")
        if self.kind == "num":
            flat = struct.unpack(f"{self.order}{_count(self.dims)}{self.code}", raw)
            return _shape(list(flat), self.dims) if self.dims else flat[0]
        if self.kind in ("ascii", "cstr"):
            b = raw.split(b"\0", 1)[0] if self.kind == "cstr" else raw
            try:
                return b.decode("utf-8")
            except UnicodeDecodeError:
                return bytes(b)
        if self.kind == "bytes":
            return bytes(raw)
        if self.kind == "guid":
            a, b, c = struct.unpack_from("<IHH", raw)
            return f"{a:08x}-{b:04x}-{c:04x}-{raw[8:10].hex()}-{raw[10:16].hex()}"
        # a record, or an array of records
        one = sum(t.size for _, t in self.fields)
        items = []
        for k in range(_count(self.dims)):
            at, item = k * one, {}
            for name, t in self.fields:
                item[name] = t.decode(raw[at:at + t.size])
                at += t.size
            items.append(item)
        return _shape(items, self.dims) if self.dims else items[0]


def _count(dims: tuple) -> int:
    n = 1
    for d in dims:
        n *= d
    return n


def _shape(flat: list, dims: tuple):
    for d in reversed(dims[1:]):
        flat = [flat[i:i + d] for i in range(0, len(flat), d)]
    return flat


@lru_cache(maxsize=4096)
def parse(s: str) -> Type:
    tokens = TOKEN.findall(s)
    pos = 0

    def take() -> str:
        nonlocal pos
        pos += 1
        return tokens[pos - 1]

    def one() -> Type:
        t = take()
        if t == "{":
            fields = []
            while True:
                name = take()
                if take() != ":":
                    raise ValueError(f"bad type {s!r}")
                fields.append((name, one()))
                sep = take()
                if sep == "}":
                    break
            base = Type("record", sum(f.size for _, f in fields), fields=tuple(fields))
        else:
            order = t[0] if t[0] in "<>" else "<"
            name = t.lstrip("<>")
            if name in NUMS:
                base = Type("num", struct.calcsize(NUMS[name]), code=NUMS[name], order=order, num=name)
            elif name in ("ascii", "cstr", "bytes"):
                base = Type(name, 1)
            elif name == "guid":
                base = Type("guid", 16)
            else:
                raise ValueError(f"unknown type {s!r}")
        dims = []
        if pos < len(tokens) and tokens[pos] == "[":
            take()
            while True:
                dims.append(int(take()))
                if take() == "]":
                    break
        if not dims:
            return base
        if base.kind in ("ascii", "cstr", "bytes"):
            return Type(base.kind, dims[0])
        return Type(base.kind, base.size * _count(tuple(dims)), code=base.code, order=base.order,
                    dims=tuple(dims), fields=base.fields, num=base.num)

    t = one()
    if pos != len(tokens):
        raise ValueError(f"bad type {s!r}")
    return t


def decode(type_: str, raw: bytes):
    return parse(type_).decode(raw)


def size(type_: str) -> int:
    return parse(type_).size
