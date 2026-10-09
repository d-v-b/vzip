"""Walking a DICOM file's elements (spec/virtualize/dicom/profile.md §6.1–§6.3)."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from vzip.virtualize.common import Reader, Rejected

LONG_VRS = {"OB", "OD", "OF", "OL", "OV", "OW", "SQ", "SV", "UC", "UN", "UR", "UT", "UV"}
VRS = LONG_VRS | {"AE", "AS", "AT", "CS", "DA", "DS", "DT", "FD", "FL", "IS", "LO", "LT", "PN", "SH", "SL",
                  "SS", "ST", "TM", "UI", "UL", "US"}
UNDEFINED = 0xFFFFFFFF
ITEM, ITEM_END, SEQUENCE_END = 0xFFFEE000, 0xFFFEE00D, 0xFFFEE0DD
PIXEL_DATA = 0x7FE00010
# The sequences of defined length that are walked (§6.3, rule 3): Shared
# Functional Groups and Pixel Measures.
WALKED = {0x52009229, 0x00289110}
MAX_DEPTH = 64


def tag_name(tag: int) -> str:
    return f"({tag >> 16:04X},{tag & 0xFFFF:04X})"


@dataclass(frozen=True)
class Encoding:
    explicit: bool
    little: bool


IMPLICIT_LE = Encoding(False, True)
EXPLICIT_LE = Encoding(True, True)


@dataclass
class Element:
    tag: int
    vr: str | None  # None in implicit VR
    value: int  # where the value starts
    length: int  # the defined length, or UNDEFINED
    little: bool  # the byte order of the value
    items: list[dict[int, Element]] | None = None  # a walked sequence's datasets


Dataset = dict[int, Element]


class Walker:
    def __init__(self, read: Reader, size: int) -> None:
        self.read = read
        self.size = size

    def header(self, pos: int, end: int, enc: Encoding) -> Element:
        """The element at `pos`, whose header must end by `end` (§6.1)."""
        if pos + 8 > end:
            raise Rejected(f"the element at {pos} runs past its container")
        b = self.read(pos, 8)
        order = "<" if enc.little else ">"
        group, number = struct.unpack(order + "HH", b[:4])
        tag = group << 16 | number
        if group == 0xFFFE or not enc.explicit:
            return Element(tag, None, pos + 8, struct.unpack(order + "I", b[4:])[0], enc.little)
        vr = b[4:6].decode("latin-1")
        if vr not in VRS:
            raise Rejected(f"unknown VR {vr!r} at {pos}")
        if vr in LONG_VRS:
            if pos + 12 > end:
                raise Rejected(f"the element at {pos} runs past its container")
            return Element(tag, vr, pos + 12, struct.unpack(order + "I", self.read(pos + 8, 4))[0], enc.little)
        return Element(tag, vr, pos + 8, struct.unpack(order + "H", b[6:])[0], enc.little)

    def meta(self) -> tuple[Dataset, int]:
        """The File Meta Information, and where the dataset starts (§6.2)."""
        elements: Dataset = {}
        pos = 132
        while pos + 2 <= self.size and struct.unpack("<H", self.read(pos, 2))[0] == 2:
            el = self.header(pos, self.size, EXPLICIT_LE)
            if el.length == UNDEFINED:
                raise Rejected(f"meta element {tag_name(el.tag)} has an undefined length")
            if el.value + el.length > self.size:
                raise Rejected(f"meta element {tag_name(el.tag)} runs past the end of the file")
            elements.setdefault(el.tag, el)
            pos = el.value + el.length
        return elements, pos

    def dataset(self, pos: int, end: int, defined: bool, enc: Encoding, depth: int,
                top: bool = False) -> tuple[Dataset, int]:
        """The elements from `pos`: to `end` (defined), or to an item
        delimiter within `end` (§6.3). The top-level dataset stops at Pixel
        Data, whose header is the last one read."""
        elements: Dataset = {}
        while True:
            if defined and pos == end:
                return elements, pos
            if top and pos >= end:
                raise Rejected("no Pixel Data element")
            el = self.header(pos, end, enc)
            if el.tag >> 16 == 0xFFFE:
                if el.tag == ITEM_END and not defined and not top:
                    if el.length != 0:
                        raise Rejected(f"the item delimiter at {pos} has a nonzero length")
                    return elements, el.value
                raise Rejected(f"unexpected {tag_name(el.tag)} at {pos}")
            if top and el.tag == PIXEL_DATA:
                elements.setdefault(el.tag, el)
                return elements, el.value
            if el.length == UNDEFINED:
                if enc.explicit and el.vr not in ("SQ", "UN") and not (
                        el.tag == PIXEL_DATA and el.vr in ("OB", "OW")):
                    raise Rejected(f"{tag_name(el.tag)} with VR {el.vr} has an undefined length")
                if el.tag == PIXEL_DATA:
                    pos = self.fragments(el.value, end, enc.little)[1]
                else:
                    inner = IMPLICIT_LE if el.vr == "UN" else enc
                    el.items, pos = self.sequence(el.value, None, end, inner, depth)
            else:
                if el.value + el.length > end:
                    raise Rejected(f"{tag_name(el.tag)} at {pos} runs past its container")
                if el.tag in WALKED:
                    if enc.explicit and el.vr != "SQ":
                        raise Rejected(f"{tag_name(el.tag)} has VR {el.vr}, not SQ")
                    el.items, _ = self.sequence(el.value, el.value + el.length, end, enc, depth)
                pos = el.value + el.length
            elements.setdefault(el.tag, el)

    def sequence(self, pos: int, end: int | None, limit: int, enc: Encoding,
                 depth: int) -> tuple[list[Dataset], int]:
        """A sequence's items: to `end` if its length is defined, else to a
        sequence delimiter within `limit` (§6.3)."""
        container = limit if end is None else end
        items: list[Dataset] = []
        while True:
            if end is not None and pos == end:
                return items, pos
            el = self.header(pos, container, enc)
            if end is None and el.tag == SEQUENCE_END:
                if el.length != 0:
                    raise Rejected(f"the sequence delimiter at {pos} has a nonzero length")
                return items, el.value
            if el.tag != ITEM:
                raise Rejected(f"expected an item at {pos}, found {tag_name(el.tag)}")
            if depth + 1 > MAX_DEPTH:
                raise Rejected(f"items nested more than {MAX_DEPTH} deep")
            if el.length == UNDEFINED:
                ds, pos = self.dataset(el.value, container, False, enc, depth + 1)
            else:
                if el.value + el.length > container:
                    raise Rejected(f"the item at {pos} runs past its container")
                ds, _ = self.dataset(el.value, el.value + el.length, True, enc, depth + 1)
                pos = el.value + el.length
            items.append(ds)

    def fragments(self, pos: int, limit: int, little: bool) -> tuple[list[tuple[int, int, int]], int]:
        """The items of a fragment sequence at `pos`, as (item offset, data
        offset, data length), and where the sequence ends (§6.5)."""
        order = "<" if little else ">"
        items = []
        while True:
            if pos + 8 > limit:
                raise Rejected(f"the fragment sequence runs past its container at {pos}")
            group, number, length = struct.unpack(order + "HHI", self.read(pos, 8))
            tag = group << 16 | number
            if tag == SEQUENCE_END:
                if length != 0:
                    raise Rejected(f"the sequence delimiter at {pos} has a nonzero length")
                return items, pos + 8
            if tag != ITEM:
                raise Rejected(f"expected a fragment item at {pos}, found {tag_name(tag)}")
            if length == UNDEFINED or pos + 8 + length > limit:
                raise Rejected(f"the fragment at {pos} has an undefined length or runs past its container")
            items.append((pos, pos + 8, length))
            pos += 8 + length
