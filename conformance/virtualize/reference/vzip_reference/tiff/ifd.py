"""Reading the structure of a TIFF or BigTIFF file (§3.1): its image file
directories (IFDs) and their SubIFDs, nested. Pixel data is never read, and a
tag's values only where the layout uses them."""

from __future__ import annotations

import struct
from typing import Callable, NamedTuple

from vzip.virtualize.common import Reader, Rejected
from vzip_reference.tiff.tags import Entry

TAGS = {256, 257, 258, 259, 262, 266, 270, 277, 282, 283, 284, 296, 317, 322, 323, 324, 325, 330, 339, 347, 530}
RATIONAL_TAGS = {282, 283}
BYTE_TAGS = {270, 347}  # ImageDescription and JPEGTables: their values are bytes
SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}
FORMATS = {1: "B", 3: "H", 4: "I", 13: "I", 16: "Q", 18: "Q"}
MAX_IFDS = 100000
MAX_DEPTH = 4  # the deepest nesting of SubIFDs (and of pointer targets): main-chain IFDs are at depth 0
MAX_SAFE = 2**53 - 1
INTEGER_TYPES = {1, 3, 4, 13, 16, 18}
SCALARS = {256, 257, 259, 262, 266, 277, 282, 283, 284, 296, 317, 322, 323}
TILES = {324, 325}
# The tags a used IFD reads (spec/virtualize/tiff/profile.md §3.1): all of the table's but
# ImageDescription (IFD 0's only) and SubIFDs (read where they are followed).
USED = TAGS - {270, 330}


class Field(NamedTuple):
    """A checked entry of a tag of the table: its value is `inline` (the value
    field's bytes) or at `offset`, within the file."""
    type: int
    count: int
    inline: bytes | None
    offset: int | None


class Ifd:
    def __init__(self, offset: int, fields: dict[int, Field], entries: list[Entry] | None = None) -> None:
        self.offset = offset
        self.entries = entries or []  # every entry, for the source metadata
        self.fields = fields  # the table's tags (of duplicates, the first), checked but not read
        self.types = {tag: f.type for tag, f in fields.items()}
        self.tags: dict = {}  # the values read (load): the first of a scalar, all of an array
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


Load = Callable[..., None]


def read_tiff(read: Reader, size: int) -> tuple[bool, bool, list[Ifd], Load]:
    """(little_endian, bigtiff, main-chain IFDs with their SubIFDs, load) (§3.1);
    `load(ifd, tiles=False, description=False)` reads the values of an IFD that the
    layout uses."""
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
    word = "Q" if big else "I"
    seen: set[int] = set()
    decoded: dict[tuple, object] = {}  # (tag kind, type, count, offset) -> values, for shared tables

    def values(tag: int, f: Field):
        """The values of a field that the layout uses: bytes, the first value of a scalar
        (a pair for a RATIONAL), or every value of an array."""
        kind = "bytes" if tag in BYTE_TAGS else "pair" if tag in RATIONAL_TAGS else "one" if tag in SCALARS else "all"
        n = f.count if kind in ("bytes", "all") else 1
        key = (kind, f.type, n, f.offset)
        if f.offset is not None and key in decoded:
            return decoded[key]
        nbytes = n * SIZES[f.type]
        data = f.inline[:nbytes] if f.inline is not None else read(f.offset, nbytes)
        if kind == "bytes":
            out: object = data
        elif kind == "pair":
            out = [struct.unpack(e + "II", data)]
        else:
            out = list(struct.unpack(e + FORMATS[f.type] * n, data))
            if f.type in (16, 18) and any(v > MAX_SAFE for v in out):
                raise Rejected(f"a value of tag {tag} is more than 2^53 - 1")
        if f.offset is not None:
            decoded[key] = out
        return out

    def load(ifd: Ifd, tiles: bool = False, description: bool = False) -> None:
        for tag, f in ifd.fields.items():
            used = (tag in USED and (tiles or tag not in TILES)) or (tag == 270 and description and f.type == 2)
            if used and tag not in ifd.tags:
                ifd.tags[tag] = values(tag, f)

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
        fields: dict[int, Field] = {}
        entries = []
        for i in range(count):
            at = i * entry_size
            tag, typ = struct.unpack(e + "HH", body[at : at + 4])
            n = struct.unpack(e + word, body[at + 4 : at + 4 + field_size])[0]
            field = body[at + 4 + field_size : at + 4 + 2 * field_size]
            inline = typ in SIZES and n * SIZES[typ] <= field_size
            where = None if inline else struct.unpack(e + word, field)[0]
            entries.append(Entry(tag, typ, n, inline=field if inline else None, offset=where, field=field))
            if tag not in TAGS or tag in fields:
                continue  # unused, or a duplicate (the first is used)
            allowed = SIZES if tag == 270 else (1, 7) if tag == 347 else (5,) if tag in RATIONAL_TAGS else INTEGER_TYPES
            if typ not in allowed:
                raise Rejected(f"tag {tag} has field type {typ}")
            if tag in SCALARS and n == 0:
                raise Rejected(f"tag {tag} has no value")
            if where is not None and where + n * SIZES[typ] > size:
                raise Rejected(f"the value of tag {tag} is outside the file")
            fields[tag] = Field(typ, n, field if inline else None, where)
        nxt = struct.unpack(e + word, body[count * entry_size : count * entry_size + field_size])[0]
        return Ifd(offset, fields, entries), nxt

    ifds = []
    offset = first
    while offset:
        if offset > MAX_SAFE:
            raise Rejected("IFD offset too large")
        ifd, offset = read_ifd(offset)
        ifds.append(ifd)

    def read_subs(ifd: Ifd, depth: int) -> None:
        """The SubIFDs of an IFD at `depth`, and theirs, down to MAX_DEPTH."""
        f = ifd.fields.get(330)
        if f is not None and depth < MAX_DEPTH:
            subs = values(330, f)
            ifd.sub = [read_ifd(o)[0] for o in subs]
            for sub in ifd.sub:
                read_subs(sub, depth + 1)

    for ifd in ifds:
        read_subs(ifd, 0)
    return e == "<", big, ifds, load


def extent(count: int, big: bool) -> int:
    """The bytes an IFD of `count` entries occupies: its entry count, entries and
    next-IFD offset (spec/virtualize/tiff.md §5)."""
    return 16 + 20 * count if big else 6 + 12 * count


def entries_reader(read: Reader, size: int, big: bool, order: str):
    """A function that finds the IFD at an offset for a pointer tag (it never rejects):
    its entry count, the end of its extent, and a function that reads its entries, or
    None if its entry count and entries do not lie within the file. The entries are
    read only when that function is called."""
    count_size, entry_size, field_size = (8, 20, 8) if big else (2, 12, 4)
    word = order + ("Q" if big else "I")

    def entries(offset: int, count: int) -> list[Entry]:
        body = read(offset + count_size, count * entry_size)
        out = []
        for i in range(count):
            a = i * entry_size
            tag, typ = struct.unpack(order + "HH", body[a:a + 4])
            n = struct.unpack(word, body[a + 4:a + 4 + field_size])[0]
            field = body[a + 4 + field_size:a + 4 + 2 * field_size]
            if typ in SIZES and n * SIZES[typ] <= field_size:
                out.append(Entry(tag, typ, n, inline=field, field=field))
            else:
                out.append(Entry(tag, typ, n, offset=struct.unpack(word, field)[0], field=field))
        return out

    def at(offset: int):
        if offset < (16 if big else 8) or offset + count_size > size:
            return None
        count = struct.unpack(order + ("Q" if big else "H"), read(offset, count_size))[0]
        if offset + count_size + count * entry_size > size:
            return None
        return count, offset + extent(count, big), lambda: entries(offset, count)

    return at
