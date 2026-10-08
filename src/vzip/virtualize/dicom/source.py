"""A DICOM file's File Meta Information and dataset in the DICOM JSON Model
(PS3.18 §F.2): the source metadata of the DICOM convention
(conventions/dicom/README.md §5)."""

from __future__ import annotations

import re
import struct

from vzip.virtualize.common import Reader, Rejected, decode_text, json_base64, json_number
from vzip.virtualize.dicom.dataset import (
    EXPLICIT_LE, IMPLICIT_LE, ITEM, ITEM_END, MAX_DEPTH, PIXEL_DATA, SEQUENCE_END, UNDEFINED, Element, Encoding,
    Walker,
)

MAX_VALUE_BYTES = 1 << 26  # the most value bytes recorded per file
MULTI = {"AE", "AS", "CS", "DA", "DS", "DT", "IS", "LO", "PN", "SH", "TM", "UC", "UI"}
TEXT = MULTI | {"LT", "ST", "UT", "UR"}
LEADING = {"AE", "AS", "CS", "DA", "DS", "DT", "IS", "LO", "PN", "SH", "TM", "UI"}  # leading spaces dropped
NUMBERS = {"FL": "f", "FD": "d", "SL": "i", "SS": "h", "UL": "I", "US": "H", "SV": "q", "UV": "Q"}
DECIMAL = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")
INTEGER = re.compile(r"[+-]?[0-9]+")
PN_GROUPS = ("Alphabetic", "Ideographic", "Phonetic")


def text_value(vr: str, b: bytes):
    """One string value: trimmed, decoded, then typed (PS3.18 §F.2.3)."""
    b = b.rstrip(b" \0")
    if vr in LEADING:
        b = b.lstrip(b" ")
    if not b:
        return None
    t = decode_text(b)
    if vr == "PN":
        groups = {k: g for k, g in zip(PN_GROUPS, t.split("=", 2)) if g}
        return groups or None
    if vr == "DS" and DECIMAL.fullmatch(t):
        v = float(t)
        return json_number(v) if v - v == 0 else t
    if vr == "IS" and INTEGER.fullmatch(t):
        return json_number(int(t))
    return t


def value_json(vr: str, data: bytes, little: bool) -> dict:
    """An element of defined length with VR `vr` and value `data` (never SQ)."""
    if not data:
        return {"vr": vr}
    order = "<" if little else ">"
    if vr in TEXT:
        parts = data.split(b"\\") if vr in MULTI else [data]
        return {"vr": vr, "Value": [text_value(vr, p) for p in parts]}
    if vr == "AT":
        v = struct.unpack(order + "HH" * (len(data) // 4), data[: len(data) // 4 * 4])
        return {"vr": vr, "Value": [f"{v[i]:04X}{v[i + 1]:04X}" for i in range(0, len(v), 2)]}
    if vr in NUMBERS:
        f = NUMBERS[vr]
        n = len(data) // struct.calcsize(f)
        return {"vr": vr, "Value": [json_number(x) for x in struct.unpack(order + f * n, data[: n * struct.calcsize(f)])]}
    return {"vr": vr, "InlineBinary": json_base64(data)}  # OB OD OF OL OV OW UN


class Translator:
    """Walks the file again, after the profile has accepted its structure, and
    translates every element; one value budget is shared across the file."""

    def __init__(self, read: Reader, size: int) -> None:
        self.read = read
        self.walker = Walker(read, size)
        self.used = 0

    def take(self, n: int) -> bool:
        if self.used + n > MAX_VALUE_BYTES:
            return False
        self.used += n
        return True

    def meta(self, start: int) -> dict:
        out: dict = {}
        pos = 132
        while pos < start:
            el = self.walker.header(pos, start, EXPLICIT_LE)
            self.defined(el, EXPLICIT_LE, out, el.value + el.length, 0)
            pos = el.value + el.length
        return dict(sorted(out.items()))

    def defined(self, el: Element, enc: Encoding, out: dict, end: int, depth: int) -> None:
        """An element of defined length, recorded in `out` unless a duplicate or a group length."""
        record = f"{el.tag:08X}" not in out and el.tag & 0xFFFF != 0
        if not record:
            return
        key = f"{el.tag:08X}"
        if not enc.explicit:
            out[key] = {"vr": "UN"} if el.length == 0 or not self.take(el.length) else {
                "vr": "UN", "InlineBinary": json_base64(self.read(el.value, el.length))}
        elif el.vr == "SQ":
            saved = self.used
            try:
                items, _ = self.sequence(el.value, el.value + el.length, end, enc, depth, True)
                out[key] = {"vr": "SQ", "Value": items}
            except Rejected:
                self.used = saved  # a sequence without a Value uses none of the budget
                out[key] = {"vr": "SQ"}
        elif el.length == 0 or not self.take(el.length):
            out[key] = {"vr": el.vr}
        else:
            out[key] = value_json(el.vr, self.read(el.value, el.length), el.little)

    def dataset(self, pos: int, end: int, defined: bool, enc: Encoding, depth: int, top: bool = False,
                record: bool = True) -> tuple[dict, int]:
        out: dict = {}
        while True:
            if defined and pos == end:
                return dict(sorted(out.items())), pos
            el = self.walker.header(pos, end, enc)
            key = f"{el.tag:08X}"
            if el.tag >> 16 == 0xFFFE:
                if el.tag == ITEM_END and not defined and not top and el.length == 0:
                    return dict(sorted(out.items())), el.value
                raise Rejected(f"unexpected item tag at {pos}")
            first = record and key not in out
            if top and el.tag == PIXEL_DATA:
                if first:
                    out[key] = {"vr": el.vr or "OW"}
                return dict(sorted(out.items())), el.value
            if el.length == UNDEFINED:
                if enc.explicit and el.vr not in ("SQ", "UN") and not (el.tag == PIXEL_DATA and el.vr in ("OB", "OW")):
                    raise Rejected(f"undefined length with VR {el.vr}")
                if el.tag == PIXEL_DATA:
                    pos = self.walker.fragments(el.value, end, enc.little)[1]
                    member = {"vr": el.vr or "OB"}
                else:
                    inner = IMPLICIT_LE if el.vr == "UN" else enc
                    items, pos = self.sequence(el.value, None, end, inner, depth, first)
                    member = {"vr": "SQ", "Value": items}
                if first:
                    out[key] = member
            else:
                if el.value + el.length > end:
                    raise Rejected(f"element at {pos} runs past its container")
                if record:
                    self.defined(el, enc, out, end, depth)
                pos = el.value + el.length

    def sequence(self, pos: int, end: int | None, limit: int, enc: Encoding, depth: int,
                 record: bool) -> tuple[list, int]:
        container = limit if end is None else end
        items: list = []
        while True:
            if end is not None and pos == end:
                return items, pos
            el = self.walker.header(pos, container, enc)
            if end is None and el.tag == SEQUENCE_END and el.length == 0:
                return items, el.value
            if el.tag != ITEM or depth + 1 > MAX_DEPTH:
                raise Rejected(f"expected an item at {pos}")
            if el.length == UNDEFINED:
                ds, pos = self.dataset(el.value, container, False, enc, depth + 1, record=record)
            else:
                if el.value + el.length > container:
                    raise Rejected(f"the item at {pos} runs past its container")
                ds, _ = self.dataset(el.value, el.value + el.length, True, enc, depth + 1, record=record)
                pos = el.value + el.length
            items.append(ds)


def source_json(read: Reader, size: int, start: int, encoding: Encoding) -> dict:
    """{"meta": ..., "dataset": ...}, for a file whose structure the profile has accepted."""
    t = Translator(read, size)
    meta = t.meta(start)
    dataset, _ = t.dataset(start, size, False, encoding, 0, top=True)
    return {"meta": meta, "dataset": dataset}
