"""Reading the structure of a TIFF or BigTIFF file (§3.1): its image file
directories (IFDs) and their SubIFDs. Pixel data is never read."""

from __future__ import annotations

import struct

from vzip.virtualize.common import Reader, Rejected
from vzip.virtualize.tiff.tags import Entry

TAGS = {256, 257, 258, 259, 262, 270, 277, 282, 283, 284, 296, 317, 322, 323, 324, 325, 330, 339, 347}
RATIONAL_TAGS = {282, 283}
SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}
FORMATS = {1: "B", 6: "b", 7: "B", 3: "H", 8: "h", 4: "I", 13: "I", 9: "i", 16: "Q", 18: "Q", 17: "q",
           11: "f", 12: "d"}
MAX_IFDS = 100000
MAX_SAFE = 2**53 - 1
INTEGER_TYPES = {1, 3, 4, 13, 16, 18}
SCALARS = {256, 257, 259, 262, 277, 282, 283, 284, 296, 317, 322, 323}


class Ifd:
    def __init__(self, offset: int, tags: dict, types: dict, entries: list[Entry] | None = None) -> None:
        self.offset = offset
        self.entries = entries or []  # every entry, for the source metadata
        self.tags = tags
        self.types = types
        self.sub: list[Ifd] = []

    def num(self, tag: int, default: int | None = None) -> int:
        v = self.tags.get(tag)
        if isinstance(v, list) and v:
            return v[0]
        if default is None:
            raise Rejected(f"IFD at {self.offset} has no tag {tag}")
        return default

    def nums(self, tag: int) -> list:
        v = self.tags.get(tag)
        if not isinstance(v, list):
            raise Rejected(f"IFD at {self.offset} has no tag {tag}")
        return v


def read_tiff(read: Reader, size: int):
    """(little_endian, bigtiff, main-chain IFDs with their SubIFDs) (§3.1)."""
    head = read(0, min(16, size))
    if len(head) < 8:
        raise Rejected("file too short for a TIFF header")
    if head[:2] not in (b"II", b"MM"):
        raise Rejected("not a TIFF file")
    e = "<" if head[:2] == b"II" else ">"
    magic = struct.unpack(e + "H", head[2:4])[0]
    if magic == 42:
        big, first = False, struct.unpack(e + "I", head[4:8])[0]
    elif magic == 43:
        if len(head) < 16 or struct.unpack(e + "HH", head[4:8]) != (8, 0):
            raise Rejected("invalid BigTIFF header")
        big, first = True, struct.unpack(e + "Q", head[8:16])[0]
    else:
        raise Rejected("not a TIFF file")
    count_size, entry_size, field_size = (8, 20, 8) if big else (2, 12, 4)
    seen: set[int] = set()

    def values(data: bytes, typ: int, n: int):
        if typ in (5, 10):
            v = struct.unpack(e + ("I" if typ == 5 else "i") * (2 * n), data[: 8 * n])
            return [v[2 * i] / v[2 * i + 1] if v[2 * i + 1] else 0.0 for i in range(n)]
        if typ == 2:
            return data
        out = list(struct.unpack(e + FORMATS[typ] * n, data[: SIZES[typ] * n]))
        if typ in (16, 17, 18) and any(abs(v) > MAX_SAFE for v in out):
            raise Rejected("a tag value is more than 2^53 - 1")
        return out

    def read_ifd(offset: int):
        if offset < (16 if big else 8):
            raise Rejected(f"IFD offset {offset} is inside the header")
        if offset in seen:
            raise Rejected(f"IFD offset {offset} read twice")
        if len(seen) >= MAX_IFDS:
            raise Rejected("too many IFDs")
        seen.add(offset)
        count = struct.unpack(e + ("Q" if big else "H"), read(offset, count_size))[0]
        body = read(offset + count_size, count * entry_size + field_size)
        tags, types, entries = {}, {}, []
        for i in range(count):
            at = i * entry_size
            tag, typ = struct.unpack(e + "HH", body[at : at + 4])
            n = struct.unpack(e + ("Q" if big else "I"), body[at + 4 : at + 4 + (8 if big else 4)])[0]
            vat = at + 4 + (8 if big else 4)
            if typ in SIZES and n * SIZES[typ] <= field_size:
                entries.append(Entry(tag, typ, n, inline=body[vat : vat + field_size]))
            else:
                where = struct.unpack(e + ("Q" if big else "I"), body[vat : vat + field_size])[0]
                entries.append(Entry(tag, typ, n, offset=where))
            if tag not in TAGS or tag in tags:
                continue  # unused, or a duplicate (the first is used)
            allowed = SIZES if tag == 270 else (1, 7) if tag == 347 else (5,) if tag in RATIONAL_TAGS else INTEGER_TYPES
            if typ not in allowed:
                raise Rejected(f"tag {tag} has field type {typ}")
            n = struct.unpack(e + ("Q" if big else "I"), body[at + 4 : at + 4 + (8 if big else 4)])[0]
            if tag in SCALARS and n == 0:
                raise Rejected(f"tag {tag} has no value")
            vat = at + 4 + (8 if big else 4)
            if n * SIZES[typ] <= field_size:
                tags[tag] = values(body[vat : vat + n * SIZES[typ]], typ, n)
            else:
                where = struct.unpack(e + ("Q" if big else "I"), body[vat : vat + field_size])[0]
                tags[tag] = values(read(where, n * SIZES[typ]), typ, n)
            if tag == 347:
                tags[tag] = bytes(tags[tag])  # JPEGTables: its bytes
            elif tag in RATIONAL_TAGS:  # (numerator, denominator) pairs
                raw = (body[vat : vat + 8 * n] if 8 * n <= field_size
                       else read(struct.unpack(e + ("Q" if big else "I"), body[vat : vat + field_size])[0], 8 * n))
                v = struct.unpack(e + "I" * (2 * n), raw)
                tags[tag] = [(v[2 * i], v[2 * i + 1]) for i in range(n)]
            types[tag] = typ
        nxt = struct.unpack(e + ("Q" if big else "I"), body[count * entry_size : count * entry_size + field_size])[0]
        return Ifd(offset, tags, types, entries), nxt

    ifds = []
    offset = first
    while offset:
        if offset > MAX_SAFE:
            raise Rejected("IFD offset too large")
        ifd, offset = read_ifd(offset)
        ifds.append(ifd)
    for ifd in ifds:
        subs = ifd.tags.get(330)
        if isinstance(subs, list):
            ifd.sub = [read_ifd(o)[0] for o in subs]
    return e == "<", big, ifds
