"""Every tag of a TIFF's IFDs as JSON: the source metadata of the TIFF and NDPI
conventions (conventions/tiff/README.md §5)."""

from __future__ import annotations

import struct
from typing import NamedTuple

from vzip.virtualize.common import Reader, decode_text, json_base64, json_number

# Bytes per value of each TIFF 6.0 and BigTIFF field type.
SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}
NUMBERS = {3: "H", 4: "I", 6: "b", 8: "h", 9: "i", 11: "f", 12: "d", 13: "I", 16: "Q", 17: "q", 18: "Q"}
# Tags whose values locate the file's own structure (strips, tiles, IFDs): only
# their type and count are recorded.
STRUCTURE = frozenset({273, 279, 288, 289, 324, 325, 330, 513, 514, 34665, 34853, 40965})
MAX_VALUE_BYTES = 1 << 26  # the most tag value bytes recorded per file


class Entry(NamedTuple):
    """An IFD entry: its value is `inline` (the value field's bytes), or at `offset`
    (None when it cannot be in the file), or, for NDPI's 64-bit LONGs, `value`."""
    tag: int
    type: int
    count: int
    inline: bytes | None = None
    offset: int | None = None
    value: tuple | None = None


def value_json(data: bytes, typ: int, count: int, order: str):
    if typ in (1, 7):
        return json_base64(data)
    if typ == 2:
        return decode_text(data[:-1] if data.endswith(b"\0") else data)
    if typ in (5, 10):
        v = struct.unpack(order + ("I" if typ == 5 else "i") * (2 * count), data)
        return [[v[2 * i], v[2 * i + 1]] for i in range(count)]
    return [json_number(v) for v in struct.unpack(order + NUMBERS[typ] * count, data)]


class Translator:
    """Translates IFDs in output order, sharing one value budget across the file."""

    def __init__(self, read: Reader, size: int, order: str, structure=STRUCTURE) -> None:
        self.read, self.size, self.order, self.structure = read, size, order, structure
        self.used = 0

    def tags(self, entries: list[Entry]) -> dict:
        first: dict[int, Entry] = {}
        for e in entries:
            first.setdefault(e.tag, e)
        out = {}
        for tag in sorted(first):
            e = first[tag]
            m: dict = {"type": e.type, "count": json_number(e.count)}
            size = SIZES.get(e.type)
            if tag not in self.structure and size is not None:
                nbytes = size * e.count
                within = e.inline is not None or (e.offset is not None and e.offset + nbytes <= self.size)
                if within and self.used + nbytes <= MAX_VALUE_BYTES:
                    self.used += nbytes
                    if e.value is not None:
                        m["value"] = [json_number(v) for v in e.value]
                    else:
                        data = e.inline[:nbytes] if e.inline is not None else self.read(e.offset, nbytes)
                        m["value"] = value_json(data, e.type, e.count, self.order)
            out[str(tag)] = m
        return out
