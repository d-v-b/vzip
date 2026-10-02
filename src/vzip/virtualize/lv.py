"""The lite variant (LV) metadata encoding of ND2 files (VIRTUALIZE.md §4.2)."""

from __future__ import annotations

import struct
import zlib

from vzip.virtualize.common import Rejected


class LVList(list):
    """A level whose records all have empty names, or a byte array."""


class Scalar(tuple):
    """A scalar value with its LV record type: (type, value)."""

    @property
    def type(self) -> int:
        return self[0]

    @property
    def value(self):
        return self[1]


MAX_DEPTH = 100


def _records(data: bytes, pos: int, end: int, count: int | None, depth: int = 0):
    """The records in data[pos:end], which are at `depth` (§4.2)."""
    if depth > MAX_DEPTH:
        raise Rejected(f"LV levels nested more than {MAX_DEPTH} deep")
    out = []
    while pos < end and (count is None or len(out) < count):
        start = pos
        if pos + 2 > end:
            raise Rejected("truncated LV record")
        typ, k = data[pos], data[pos + 1]
        pos += 2
        if typ == 76:
            raise Rejected("compressed LV record inside a structure")
        if pos + 2 * k > end:
            raise Rejected("truncated LV record name")
        name = data[pos : pos + 2 * k].decode("utf-16-le", "replace").split("\0", 1)[0]
        pos += 2 * k

        def take(n):
            nonlocal pos
            if pos + n > end:
                raise Rejected("truncated LV value")
            v = data[pos : pos + n]
            pos += n
            return v

        if typ == 1:
            value = Scalar((1, take(1)[0] != 0))
        elif typ in (2, 3, 4, 5, 6, 7):
            fmt = {2: "<i", 3: "<I", 4: "<q", 5: "<Q", 6: "<d", 7: "<Q"}[typ]
            value = Scalar((typ, struct.unpack(fmt, take(4 if typ in (2, 3) else 8))[0]))
        elif typ == 8:
            units = bytearray()
            while True:
                u = take(2)
                if u == b"\0\0":
                    break
                units += u
            value = Scalar((8, units.decode("utf-16-le", "replace")))
        elif typ == 9:
            n = struct.unpack("<Q", take(8))[0]
            value = LVList(Scalar((3, b)) for b in take(n))  # a byte counts as type 3
        elif typ == 11:
            c, length = struct.unpack("<IQ", take(12))
            level_end = start + length
            if level_end > end or level_end < pos:
                raise Rejected("LV level length outside the data")
            members, after = _records(data, pos, level_end, c, depth + 1)
            if len(members) != c or after != level_end:
                raise Rejected("LV level records do not end at its length")
            pos = level_end
            take(8 * c)
            if members and all(n == "" for n, _ in members):
                value = LVList(v for _, v in members)
            else:
                value = {}
                for n, v in members:
                    value[n] = v  # first position, last value
        else:
            raise Rejected(f"unknown LV record type {typ}")
        out.append((name, value))
    return out, pos


def decode_lv(data: bytes) -> dict:
    """A chunk's LV structure: a tree of dicts (objects), LVLists (lists) and
    Scalars. The top level is always an object."""
    if len(data) >= 1 and data[0] == 76:
        if len(data) < 12:
            raise Rejected("truncated compressed LV record")
        d = zlib.decompressobj()
        try:
            inner = d.decompress(data[12:]) + d.flush()
        except zlib.error as e:
            raise Rejected(f"invalid zlib stream in compressed LV data: {e}") from None
        if not d.eof or d.unused_data:
            raise Rejected("compressed LV data does not end with its zlib stream")
        if inner[:1] == b"\x4c":
            raise Rejected("compressed LV data inside compressed LV data")
        data = inner
    members, _ = _records(data, 0, len(data), None)
    out: dict = {}
    for n, v in members:
        out[n] = v
    return out
