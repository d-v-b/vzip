"""A DICOM file's File Meta Information and dataset in the DICOM JSON Model
(PS3.18 §F.2): the source metadata of the DICOM convention
(spec/virtualize/dicom.md §5)."""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass, replace
from pathlib import Path

from vzip.virtualize.common import (
    MAX_CHUNK, MAX_PAYLOAD, SOURCE_NODE, Plan, Reader, Rejected, family_plans, grid_chunks, json_base64, json_number,
    payload_size, row_chunks, text_json,
)
from vzip.virtualize.dicom.dataset import (
    EXPLICIT_LE, IMPLICIT_LE, ITEM, ITEM_END, MAX_DEPTH, PIXEL_DATA, SEQUENCE_END, UNDEFINED, Element, Encoding,
    Walker,
)

MULTI = {"AE", "AS", "CS", "DA", "DS", "DT", "IS", "LO", "PN", "SH", "TM", "UC", "UI"}
TEXT = MULTI | {"LT", "ST", "UT", "UR"}
LEADING = {"AE", "AS", "CS", "DA", "DS", "DT", "IS", "LO", "PN", "SH", "TM", "UI"}  # leading spaces dropped
NUMBERS = {"FL": "f", "FD": "d", "SL": "i", "SS": "h", "UL": "I", "US": "H", "SV": "q", "UV": "Q"}
DECIMAL = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")
INTEGER = re.compile(r"[+-]?[0-9]+")
PN_GROUPS = ("Alphabetic", "Ideographic", "Phonetic")
# Specific Character Set, and the character sets in which the bytes 5C (\) and
# 3D (=) can occur inside a character.
CHARSET = 0x00080005
MULTIBYTE = {b"GB18030", b"GBK", b"ISO 2022 IR 58", b"ISO 2022 IR 87", b"ISO 2022 IR 149", b"ISO 2022 IR 159"}
# The VRs whose values are split at 5C (PN also at 3D) and to which Specific
# Character Set applies: kept as bytes under a multibyte character set.
CHARSET_SPLIT = {"LO", "PN", "SH", "UC"}
_DICTIONARY = json.loads((Path(__file__).parent / "dictionary.json").read_text())
TAGS: dict[str, str] = {s[i:i + 8]: vr for vr, s in _DICTIONARY["tags"].items() for i in range(0, len(s), 8)}
MASKS: list[tuple[str, str]] = list(_DICTIONARY["masks"].items())


def dictionary_vr(tag: int) -> str | None:
    """The VR the data dictionary gives a tag, or None (spec/virtualize/dicom.md §5)."""
    if tag >> 16 & 1:  # a private group: only its private creators have a known VR
        return "LO" if 0x0010 <= tag & 0xFFFF <= 0x00FF else None
    key = f"{tag:08X}"
    if key in TAGS:
        return TAGS[key]
    for mask, vr in MASKS:
        if all(m == "x" or m == k for m, k in zip(mask, key)):
            return vr
    return None


def is_multibyte(value: bytes) -> bool:
    """Whether a Specific Character Set value names a character set in which
    5C or 3D can occur inside a character."""
    return any(v.strip(b" \0") in MULTIBYTE for v in value.split(b"\\"))


_DECIMAL_PARTS = re.compile(r"([+-]?)([0-9]*)(?:\.([0-9]*))?(?:[eE]([+-]?)([0-9]+))?")
# The most digits of a DS exponent, without its sign and leading zeros, that
# can leave a value of at most 2^16 bytes in binary64's range; and the most
# digits of an IS value, without its sign and leading zeros, that can be at
# most 2^53 - 1 (spec/virtualize/dicom.md §5). Python's int() refuses
# strings of over 4300 digits, so longer ones are never converted.
MAX_EXPONENT_DIGITS = 5
MAX_INTEGER_DIGITS = 16


def decimal_key(t: str) -> tuple[str, str, int] | None:
    """A decimal string's value, normalized: (sign, digits without leading or
    trailing zeros, exponent of the last digit); zero is ("", "", 0). None for
    a value that is not zero and whose exponent has more than
    MAX_EXPONENT_DIGITS digits: out of binary64's range."""
    m = _DECIMAL_PARTS.fullmatch(t)
    assert m is not None
    frac = m[3] or ""
    digits = (m[2] + frac).lstrip("0")
    if not digits:
        return "", "", 0
    exponent_digits = (m[5] or "").lstrip("0")
    if len(exponent_digits) > MAX_EXPONENT_DIGITS:
        return None
    stripped = digits.rstrip("0")
    exponent = (-1 if m[4] == "-" else 1) * int(exponent_digits or "0") - len(frac) + len(digits) - len(stripped)
    return "-" if m[1] == "-" else "", stripped, exponent


def ds_number(t: str):
    """A DS value as a JSON number when that number is the text's decimal
    value exactly, else None (spec/virtualize/dicom.md §5)."""
    if not DECIMAL.fullmatch(t):
        return None
    key = decimal_key(t)
    if key is None:
        return None
    v = float(t)
    if v - v != 0 or key != decimal_key(repr(v)):
        return None
    return json_number(v)


def text_value(vr: str, b: bytes):
    """One string value: trimmed, decoded, then typed (PS3.18 §F.2.3)."""
    b = b.rstrip(b" \0")
    if vr in LEADING:
        b = b.lstrip(b" ")
    if not b:
        return None
    t = text_json(b)
    if not isinstance(t, str):
        return t  # not UTF-8: its ISO 8859-1 reading, tagged, and not typed further
    if vr == "PN":
        groups = {k: g for k, g in zip(PN_GROUPS, t.split("=", 2)) if g}
        return groups or None
    if vr == "DS":
        v = ds_number(t)
        return t if v is None else v
    if vr == "IS" and INTEGER.fullmatch(t):
        return is_number(t)
    return t


INT64 = 1 << 63


def typed_values(vr: str, data: bytes) -> tuple[str, bytes] | None:
    """A DS or IS value as float64 or int64 values in little endian, when
    every value is a number exactly: a DS value that §5 writes as a number, an
    IS value of the form [+-]?[0-9]+ within int64's range; else None
    (spec/virtualize/dicom.md §5, Per-frame values)."""
    out: list = []
    for p in data.split(b"\\"):
        p = p.rstrip(b" \0").lstrip(b" ")
        if not p.isascii():
            return None
        t = p.decode("ascii")
        if vr == "DS":
            v = ds_number(t)
            if v is None:
                return None
            out.append(float(v))
        else:
            if not INTEGER.fullmatch(t) or len(t.lstrip("+-").lstrip("0")) > 19:
                return None
            v = int(t)
            if not -INT64 <= v < INT64:
                return None
            out.append(v)
    if vr == "DS":
        return "float64", struct.pack(f"<{len(out)}d", *out)
    return "int64", struct.pack(f"<{len(out)}q", *out)


def is_number(t: str):
    """An IS value of the form [+-]?[0-9]+ as spec/conventions.md §6 writes
    an integer: a number up to 2^53 - 1, else the string of its digits."""
    negative = t[0] == "-"
    digits = t.lstrip("+-").lstrip("0")
    if len(digits) > MAX_INTEGER_DIGITS:  # above 2^53 - 1
        return ("-" if negative else "") + digits
    return json_number(-int(digits or "0") if negative else int(digits or "0"))


def value_size(vr: str) -> int | None:
    """The size of one value of an AT or numeric VR."""
    return 4 if vr == "AT" else struct.calcsize(NUMBERS[vr]) if vr in NUMBERS else None


def value_json(vr: str, data: bytes, little: bool, multibyte: bool = False) -> dict:
    """An element of defined length with VR `vr` and value `data` (never SQ),
    under a multibyte character set or not."""
    if not data:
        return {"vr": vr}
    order = "<" if little else ">"
    if vr in TEXT:
        if multibyte and vr in CHARSET_SPLIT:
            return {"vr": vr, "InlineBinary": json_base64(data)}  # not split: decoded by its character set
        parts = data.split(b"\\") if vr in MULTI else [data]
        return {"vr": vr, "Value": [text_value(vr, p) for p in parts]}
    size = value_size(vr)
    if size is not None and len(data) % size:
        return {"vr": vr, "InlineBinary": json_base64(data)}  # not whole values: as stored
    if vr == "AT":
        v = struct.unpack(order + "HH" * (len(data) // 4), data)
        return {"vr": vr, "Value": [f"{v[i]:04X}{v[i + 1]:04X}" for i in range(0, len(v), 2)]}
    if vr in NUMBERS:
        values = struct.unpack(order + NUMBERS[vr] * (len(data) // size), data)
        return {"vr": vr, "Value": [json_number(x) for x in values]}
    return {"vr": vr, "InlineBinary": json_base64(data)}  # OB OD OF OL OV OW UN


def _js_number(x) -> str:
    """A number as ECMAScript's Number::toString writes it (integers are at
    most 2^53 here)."""
    if isinstance(x, int) or x == 0:
        return str(int(x))
    r = repr(x)
    key = decimal_key(r)
    assert key is not None  # repr's exponent has at most 3 digits
    _, digits, exponent = key
    k = len(digits)
    n = exponent + k  # the value is 0.digits x 10^n
    if k <= n <= 21:
        s = digits + "0" * (n - k)
    elif 0 < n <= 21:
        s = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        s = "0." + "0" * -n + digits
    else:
        s = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("+" if n > 0 else "-") + str(abs(n - 1))
    return ("-" if r[0] == "-" else "") + s


def json_size(v) -> int:
    """The length of `v` in UTF-8 as ECMAScript's JSON.stringify writes it
    (spec/virtualize/dicom.md §5)."""
    if v is None or v is True:
        return 4
    if v is False:
        return 5
    if isinstance(v, str):
        return len(json.dumps(v, ensure_ascii=False).encode())
    if isinstance(v, (int, float)):
        return len(_js_number(v))
    if isinstance(v, list):
        return 2 + max(len(v) - 1, 0) + sum(map(json_size, v))
    return 2 + max(len(v) - 1, 0) + sum(json_size(k) + 1 + json_size(x) for k, x in v.items())


BINARY = {"OB": ("uint8", 1), "UN": ("uint8", 1), "OW": ("uint16", 2), "OF": ("float32", 4),
          "OD": ("float64", 8), "OL": ("uint32", 4), "OV": ("uint64", 8)}
NUMERIC_TYPES = {"FL": "float32", "FD": "float64", "SL": "int32", "SS": "int16", "UL": "uint32", "US": "uint16",
                 "SV": "int64", "UV": "uint64"}
INLINE = 64  # the most bytes of a binary value, or values of a numeric, DS or IS one, kept as JSON
MAX_TEXT = 1 << 16  # the longest text value kept as JSON
MAX_MEMBER = 1 << 14  # the largest top-level attribute, as JSON, kept on the root
MAX_ROOT = 1 << 16  # the largest the root's meta and dataset, as JSON, may be together
# Data Set Trailing Padding: dead space. (Group lengths and the Extended Offset
# Table are layout only where spec/virtualize/dicom.md §5 says.)
PADDING = 0xFFFCFFFC
EOT_TAGS = (0x7FE00001, 0x7FE00002)  # the Extended Offset Table and its lengths
ITEM_SIZES = {"uint8": 1, "int16": 2, "uint16": 2, "int32": 4, "uint32": 4, "float32": 4, "float64": 8, "int64": 8,
              "uint64": 8}
PER_FRAME = "52009230"  # Per-frame Functional Groups Sequence: on vzip_source, always gathered
GATHER_ITEMS = 64  # a sequence of more items is gathered
# The group, under a gathered sequence's path, of its gathered arrays: no
# item index, so no item's path.
ITEMS = "items"
# The name of a gathered array (or family) within its path's group: no tag
# path has it, so no gathered path is a prefix of another.
GATHERED = "value"
# The array of each item's structure, beside the gathered values' groups
# (whose names are tags or `duplicates`).
STRUCTURE = "structure"
# The bounds of a gathered sequence, past which it keeps its bytes: its
# distinct paths, the JSON size of its Structures, and its arrays' cost, per
# byte of the sequence (spec/virtualize/dicom.md §5, Bounds).
MAX_PATHS = 1024
MAX_STRUCTURES = 1 << 16
MAX_COST = 16
# The members of an attribute whose value a reader takes as bytes: marked
# `LittleEndian` when they are an explicit UN value's, in big endian.
AS_BYTES = ("InlineBinary", "BulkDataURI", "Gathered")


def dimension(data_type: str) -> str:
    """The dimension name of an array of values: `byte` for bytes, else `value`."""
    return "byte" if data_type == "uint8" else "value"


@dataclass
class Extent:
    """A value kept as an array: `n` bytes at `offset`, values of `data_type`
    of `size` bytes each; `attribute` names the array in its BulkDataURI."""
    path: str
    attribute: dict
    offset: int
    n: int
    data_type: str = "uint8"
    size: int = 1
    little: bool = True
    values: tuple[str, bytes] | None = None  # a gathered DS or IS value's typed values
    dropped: bool = False  # a gathered group length that is layout
    item: int = 0  # a gathered value's item index
    rest: str = ""  # a gathered value's path within its item


def sorted_dataset(out: dict) -> dict:
    return dict(sorted(out.items()))


class GroupLengths:
    """The group lengths of one dataset (spec/virtualize/dicom.md §5): each
    is layout, and dropped, when it is the only element of its tag in the
    dataset, of an even group, UL (or implicit) with 4 bytes, and its value is
    the length of the run of elements of its group that follows it."""

    def __init__(self) -> None:
        # group, key, value, where the run starts, and the value's gathered extent
        self.open: tuple[int, str, int | None, int, Extent | None] | None = None
        self.closed: list[tuple[str, int | None, int, Extent | None]] = []  # key, value, the run's length, extent

    def step(self, tag: int, pos: int) -> None:
        """An element of tag `tag` starts at `pos`."""
        if self.open is not None and tag >> 16 != self.open[0]:
            self.close(pos)

    def close(self, pos: int) -> None:
        if self.open is not None:
            _, key, value, start, extent = self.open
            self.closed.append((key, value, pos - start, extent))
            self.open = None

    def begin(self, el: Element, enc: Encoding, read: Reader, extent: Extent | None) -> None:
        """After the first element of its tag in the dataset, a group length
        (`extent`: its value, when gathered)."""
        value = None
        if el.tag >> 16 & 1 == 0 and el.length == 4 and (not enc.explicit or el.vr == "UL"):
            value = struct.unpack("<I" if el.little else ">I", read(el.value, 4))[0]
        self.open = (el.tag >> 16, f"{el.tag:08X}", value, el.value + el.length, extent)

    def finish(self, pos: int, out: dict) -> None:
        """The dataset's elements end at `pos`: drops the group lengths that are layout."""
        self.close(pos)
        duplicated = {k for d in out.get("duplicates", []) for k in d}
        for key, value, length, extent in self.closed:
            if value is not None and value == length and key not in duplicated:
                del out[key]
                if extent is not None:
                    extent.dropped = True


class Translator:
    """Walks the file again, after the profile has accepted its structure, and
    translates every element (spec/virtualize/dicom.md §5): small values as
    JSON, large ones as arrays of the source metadata node, named in the
    element's BulkDataURI."""

    def __init__(self, read: Reader, size: int) -> None:
        self.read = read
        self.size = size
        self.walker = Walker(read, size)
        self.arrays: list[Plan] = []  # the families of encapsulated pixel data in items
        self.extents: list[Extent] = []  # every other array
        # The top-level dataset, and the tags of the offset tables omitted from it
        # as layout: a later element of such a tag is a duplicate.
        self.top: dict | None = None
        self.omitted: set[str] = set()
        # The path of the gathered sequence whose items are being walked.
        self.within: str | None = None

    def mark(self) -> tuple[int, int]:
        return len(self.arrays), len(self.extents)

    def rollback(self, mark: tuple[int, int]) -> None:
        del self.arrays[mark[0]:]
        del self.extents[mark[1]:]

    def array(self, vr: str, path: str, offset: int, n: int, data_type: str = "uint8", size: int = 1,
              little: bool = True) -> dict:
        attribute = {"vr": vr, "BulkDataURI": f"{SOURCE_NODE}/{path}"}
        self.extents.append(Extent(path, attribute, offset, n, data_type, size, little))
        return attribute

    def gather(self, vr: str, el: Element, path: str) -> dict:
        """A value of an item of a gathered sequence, gathered with the others
        of its path (spec/virtualize/dicom.md §5)."""
        assert self.within is not None
        index, rest = path[len(self.within) + 1:].split("/", 1)
        n = el.length
        if vr in BINARY:
            data_type, size = BINARY[vr]
        elif vr in NUMERIC_TYPES:
            data_type, size = NUMERIC_TYPES[vr], struct.calcsize(NUMBERS[vr])
        else:
            data_type, size = "uint8", 1
        if n % size:
            data_type, size = "uint8", 1
        values = typed_values(vr, self.read(el.value, n)) if vr in ("DS", "IS") and n <= MAX_TEXT else None
        self.extents.append(Extent(path, {}, el.value, n, data_type, size, el.little or data_type == "uint8", values,
                                   item=int(index), rest=rest))
        return {"vr": vr, "Gathered": True}

    def member(self, vr: str, el: Element, path: str, multibyte: bool) -> dict:
        """An element of defined length with VR `vr`: not a sequence, or the
        bytes of one that breaks a rule (`SQ`)."""
        n = el.length
        if n == 0:
            return {"vr": vr}
        if self.within is not None:
            return self.gather(vr, el, path)
        size = value_size(vr)
        if vr in BINARY or vr == "SQ" or (size is not None and n % size):
            if n <= INLINE:
                return {"vr": vr, "InlineBinary": json_base64(self.read(el.value, n))}
            dtype, size = BINARY.get(vr, ("uint8", 1))
            if n % size:
                dtype, size = "uint8", 1
            return self.array(vr, path, el.value, n, dtype, size, el.little)
        if vr in NUMERIC_TYPES and n // size > INLINE:
            return self.array(vr, path, el.value, n, NUMERIC_TYPES[vr], size, el.little)
        if vr in TEXT and n > MAX_TEXT:
            return self.array(vr, path, el.value, n)
        data = self.read(el.value, n)
        if vr in ("DS", "IS") and data.count(b"\\") >= INLINE:  # more than 64 values
            return self.array(vr, path, el.value, n)
        return value_json(vr, data, el.little, multibyte)

    def sequence_or_bytes(self, vr_if_bytes: str, el: Element, enc: Encoding, end: int, depth: int,
                          path: str, multibyte: bool) -> dict:
        """A sequence of defined length; if it breaks a rule, its bytes."""
        mark = self.mark()
        try:
            attribute, _ = self.items(el, el.value + el.length, end, enc, depth, path, multibyte, vr_if_bytes)
            return attribute
        except Rejected:
            self.rollback(mark)  # what the failed walk planned
            return self.member(vr_if_bytes, el, path, multibyte)  # its bytes, by the binary rule

    def defined(self, el: Element, enc: Encoding, end: int, depth: int, path: str, multibyte: bool) -> dict:
        """An element of defined length."""
        if not enc.explicit or el.vr == "UN":
            # The file does not state the VR: the dictionary's, else UN. An
            # explicit UN value is in little endian in every transfer syntax
            # (PS3.5 §6.2.2), as its items are in implicit VR little endian.
            if el.vr == "UN":
                el = replace(el, little=True)
            vr = dictionary_vr(el.tag) or "UN"
            if vr == "SQ":
                return self.sequence_or_bytes("UN", el, IMPLICIT_LE if enc.explicit else enc, end, depth, path,
                                              multibyte)
            return self.member(vr, el, path, multibyte)
        if el.vr == "SQ":
            return self.sequence_or_bytes("SQ", el, enc, end, depth, path, multibyte)
        return self.member(el.vr, el, path, multibyte)

    def element(self, el: Element, enc: Encoding, out: dict, end: int, depth: int, base: str,
                multibyte: bool) -> int:
        """Translates the element `el` of the dataset `out`, at path `base` and
        within `end`, into it, and returns where the next element starts. An
        element with a tag already in `out` goes to its `duplicates`."""
        key = f"{el.tag:08X}"
        duplicate = key in out or (out is self.top and key in self.omitted)
        path = f"{base}/{key}" if not duplicate else f"{base}/duplicates/{len(out.get('duplicates', []))}/{key}"
        if el.length == UNDEFINED:
            if enc.explicit and el.vr not in ("SQ", "UN") and not (el.tag == PIXEL_DATA and el.vr in ("OB", "OW")):
                raise Rejected(f"undefined length with VR {el.vr}")
            if el.tag == PIXEL_DATA:
                fragments, pos = self.walker.fragments(el.value, end, enc.little)
                vr = el.vr or "OB"
                members = [(i, o, n) for i, (_, o, n) in enumerate(fragments)]
                attribute: dict = {"vr": vr}
                if members:
                    self.arrays += family_plans(path, members, self.read)
                    attribute["BulkDataURI"] = f"{SOURCE_NODE}/{path}"
            else:
                inner = IMPLICIT_LE if el.vr == "UN" else enc
                attribute, pos = self.items(el, None, end, inner, depth, path, multibyte,
                                            "SQ" if enc.explicit and el.vr == "SQ" else "UN")
        else:
            if el.value + el.length > end:
                raise Rejected(f"the element at {el.value} runs past its container")
            pos = el.value + el.length
            if el.tag == PADDING:
                return pos  # dead space: not recorded
            attribute = self.defined(el, enc, end, depth, path, multibyte)
        if enc.explicit and not enc.little and el.vr == "UN" and any(k in attribute for k in AS_BYTES):
            attribute["LittleEndian"] = True  # an explicit UN value: in little endian (PS3.5 §6.2.2)
        if duplicate:
            out.setdefault("duplicates", []).append({key: attribute})
        else:
            out[key] = attribute
        return pos

    def translate(self, el: Element, enc: Encoding, out: dict, end: int, depth: int, base: str, multibyte: bool,
                  pos: int, groups: GroupLengths) -> int:
        """`element`, for the element at `pos`, keeping track of the dataset's group lengths."""
        groups.step(el.tag, pos)
        first, planned = f"{el.tag:08X}" not in out, len(self.extents)
        after = self.element(el, enc, out, end, depth, base, multibyte)
        if el.tag & 0xFFFF == 0 and el.length != UNDEFINED and first and f"{el.tag:08X}" in out:
            groups.begin(el, enc, self.read, self.extents[-1] if len(self.extents) > planned else None)
        return after

    def meta(self, start: int) -> dict:
        out: dict = {}
        groups = GroupLengths()
        pos = 132
        while pos < start:
            el = self.walker.header(pos, start, EXPLICIT_LE)
            pos = self.translate(el, EXPLICIT_LE, out, start, 0, "meta", False, pos, groups)
        groups.finish(pos, out)
        return sorted_dataset(out)

    def dataset(self, pos: int, end: int, defined: bool, enc: Encoding, depth: int, base: str,
                multibyte: bool = False) -> tuple[dict, int]:
        """An item's dataset, under the character set of the dataset that holds
        it (`multibyte`) unless its own Specific Character Set says otherwise."""
        out: dict = {}
        groups = GroupLengths()
        charset = False  # whether the dataset's Specific Character Set has been read
        while True:
            if defined and pos == end:
                groups.finish(pos, out)
                return sorted_dataset(out), pos
            el = self.walker.header(pos, end, enc)
            if el.tag >> 16 == 0xFFFE:
                if el.tag == ITEM_END and not defined and el.length == 0:
                    groups.finish(pos, out)
                    return sorted_dataset(out), el.value
                raise Rejected(f"unexpected item tag at {pos}")
            pos = self.translate(el, enc, out, end, depth, base, multibyte, pos, groups)
            if el.tag == CHARSET and el.length != UNDEFINED and not charset:
                charset, multibyte = True, is_multibyte(self.read(el.value, el.length))

    def items(self, el: Element, end: int | None, limit: int, enc: Encoding, depth: int, path: str,
              multibyte: bool, vr_if_bytes: str) -> tuple[dict, int]:
        """A sequence's attribute, and where it ends: its items, or, gathered
        (the top-level Per-frame Functional Groups Sequence, or one of more
        than 64 items outside a gathered item), its structures, unless it is
        past the bounds of one, when it keeps its bytes (`vr_if_bytes`)."""
        if self.within is not None:
            items, pos = self.sequence(el.value, end, limit, enc, depth, path, multibyte)
            return {"vr": "SQ", "Value": items}, pos
        mark = self.mark()
        if path != f"dataset/{PER_FRAME}":
            some, pos = self.sequence(el.value, end, limit, enc, depth, path, multibyte, GATHER_ITEMS)
            if some is not None:
                return {"vr": "SQ", "Value": some}, pos
            self.rollback(mark)  # more than 64 items: walked again, gathered
        self.within = path
        try:
            items, pos = self.sequence(el.value, end, limit, enc, depth, path, multibyte)
        finally:
            self.within = None
        length = (end if end is not None else pos - 8) - el.value  # without the sequence delimiter
        attribute = self.gathered(path, items, mark, length)
        if attribute is None:  # past the bounds: its bytes
            self.rollback(mark)
            return self.member(vr_if_bytes, replace(el, length=length), path, multibyte), pos
        return attribute, pos

    def gathered(self, path: str, items: list, mark: tuple[int, int], length: int) -> dict | None:
        """The attribute of the gathered sequence at `path`, whose `length`
        bytes hold `items`, with its arrays planned; or None when it is past
        the bounds (spec/virtualize/dicom.md §5, Bounds)."""
        count = len(items)
        columns: dict[str, list[tuple[int, Extent]]] = {}
        for e in self.extents[mark[1]:]:
            if not e.dropped:
                columns.setdefault(e.rest, []).append((e.item, e))
        structures: list[dict] = []
        index: dict[str, int] = {}
        each = []
        for item in items:
            k = json.dumps(item, separators=(",", ":"))
            each.append(index.setdefault(k, len(structures)))
            if each[-1] == len(structures):
                structures.append(item)
        if (len(columns) > MAX_PATHS or json_size(structures) > MAX_STRUCTURES
                or sum(gathered_cost(members, count) for members in columns.values()) > MAX_COST * length):
            return None
        del self.extents[mark[1]:]
        for rest, members in columns.items():
            self.arrays += gathered_plans(f"{path}/{ITEMS}/{rest}/{GATHERED}", members, count, self.read)
        if count:
            self.arrays.append(copied_plan(f"{path}/{ITEMS}/{STRUCTURE}", "int32", [count], ["index"],
                                           struct.pack(f"<{count}i", *each), 4))
            return {"vr": "SQ", "Structures": structures}
        return {"vr": "SQ", "Value": []}

    def sequence(self, pos: int, end: int | None, limit: int, enc: Encoding, depth: int, base: str,
                 multibyte: bool, most: int | None = None) -> tuple[list | None, int]:
        """A sequence's items, and where it ends; with `most`, None at the
        item after the first `most`."""
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
            if most is not None and len(items) == most:
                return None, pos
            path = f"{base}/{len(items)}"
            if el.length == UNDEFINED:
                ds, pos = self.dataset(el.value, container, False, enc, depth + 1, path, multibyte)
            else:
                if el.value + el.length > container:
                    raise Rejected(f"the item at {pos} runs past its container")
                ds, _ = self.dataset(el.value, el.value + el.length, True, enc, depth + 1, path, multibyte)
                pos = el.value + el.length
            items.append(ds)

    def plans(self) -> list[Plan]:
        """The arrays."""
        plans = list(self.arrays)
        for e in self.extents:
            rows, chunks = row_chunks(e.offset, e.n // e.size, e.size)
            plans.append(Plan(e.path, e.data_type, [e.n // e.size], [rows], [dimension(e.data_type)], chunks,
                              endian="little" if e.little else "big"))
        return plans


def gathered_kind(members: list[tuple[int, Extent]]) -> tuple[str, int, bool] | None:
    """The array of one path of a gathered sequence's items: (data type, row
    bytes, little endian) of a 2-D array, or None for a family
    (spec/virtualize/dicom.md §5, Gathered values)."""
    kinds = {(e.values[0], len(e.values[1]), True) if e.values is not None else (e.data_type, e.n, e.little)
             for _, e in members}
    if len(kinds) == 1:
        ((data_type, n, little),) = kinds
        if n <= MAX_CHUNK:
            return data_type, n, little
    lengths = {e.n for _, e in members}
    if len(lengths) == 1 and members[0][1].n <= MAX_CHUNK:
        return "uint8", members[0][1].n, True
    return None


def gathered_cost(members: list[tuple[int, Extent]], count: int) -> int:
    """The bytes that one path's array holds (spec/virtualize/dicom.md §5,
    Bounds): a family's offsets and data; a 2-D array's rows, only those with
    a value when they are sparse."""
    kind = gathered_kind(members)
    if kind is None:
        return 8 * (count + 1) + sum(e.n for _, e in members)
    return (len(members) if 2 * len(members) < count else count) * kind[1]


def gathered_plans(path: str, members: list[tuple[int, Extent]], count: int, read: Reader) -> list[Plan]:
    """The values of one path of the per-frame items (spec/virtualize/dicom.md
    §5, Per-frame values): a 2-D array of their data type when they all have
    one data type, length and byte order (a DS or IS value its typed values,
    when it has them), else of their bytes as stored when they all have one
    length, else a family of those bytes."""
    kind = gathered_kind(members)
    if kind is not None:
        data_type, n, little = kind
        rows = {i: e.values[1] if e.values is not None and data_type != "uint8" else (e.offset, e.n)
                for i, e in members}
        size = ITEM_SIZES[data_type]
        return [gathered_rows(path, data_type, size, count, n // size, rows, little, read)]
    return family_plans(path, [(i, e.offset, e.n) for i, e in members], read, count, ragged=True)


def gathered_rows(path: str, data_type: str, size: int, count: int, m: int, rows: dict, little: bool,
                  read: Reader) -> Plan:
    """A 2-D array [count, m] of `size`-byte values, whose row i is `rows[i]`:
    a range of the file, or bytes; a row without a value is zero bytes
    (spec/virtualize/dicom.md §5, Gathered values)."""
    row = m * size
    dims = ["index", dimension(data_type)]
    endian = "little" if little else "big"
    ordered = sorted(rows.items())
    if len(ordered) == count and all(isinstance(r, tuple) and r[0] == ordered[0][1][0] + j * row
                                     for j, (_, r) in enumerate(ordered)):
        # Every row, adjacent in the file in index order: contiguous values.
        shape, chunks = grid_chunks(ordered[0][1][0], [count, m], size, read)
        return Plan(path, data_type, [count, m], shape, dims, chunks, endian=endian)
    # A row takes at most max(16, b + 8) bytes of a reference payload: as a
    # range, or as bytes of a literal.
    k = -(-count // max(1, MAX_PAYLOAD // max(16, row + 8)))
    c = -(-count // k) if 2 * len(rows) >= count else 1  # sparse: a chunk per row with a value
    chunks: dict = {}
    for q in sorted({i // c for i in rows}):  # a chunk none of whose rows has a value is absent
        parts: list = []
        for i in range(q * c, (q + 1) * c):
            r = rows.get(i)
            if r is None:
                r = bytes(row)
            if isinstance(r, tuple):
                if parts and isinstance(parts[-1], tuple) and parts[-1][0] + parts[-1][1] == r[0]:
                    parts[-1] = (parts[-1][0], parts[-1][1] + r[1])  # adjacent in the file: one range
                else:
                    parts.append(r)
            elif parts and isinstance(parts[-1], bytearray):
                parts[-1] += r  # bytes after bytes: one literal
            else:
                parts.append(bytearray(r))
        parts = [bytes(p) if isinstance(p, bytearray) else p for p in parts]
        if payload_size(parts) > MAX_PAYLOAD:
            chunks[(q, 0)] = b"".join(read(*p) if isinstance(p, tuple) else p for p in parts)
        else:
            chunks[(q, 0)] = parts
    return Plan(path, data_type, [count, m], [c, m], dims, chunks, endian=endian)


def copied_plan(path: str, data_type: str, shape: list[int], dims: list[str], packed: bytes, item: int) -> Plan:
    """Values that the hierarchy holds itself, `packed` in C order, cut as
    contiguous values (spec/conventions.md §7), every chunk copied."""
    chunk_shape, cut = grid_chunks(0, shape, item, lambda o, n: packed[o:o + n])
    chunks = {k: v if isinstance(v, bytes) else b"".join(packed[r[0]:r[0] + r[1]] if isinstance(r, tuple) else r
                                                         for r in v) for k, v in cut.items()}
    return Plan(path, data_type, shape, chunk_shape, dims, chunks)


def object_size(sizes: dict[str, int]) -> int:
    """The JSON size of an object whose members' values have the JSON sizes
    `sizes`, by member name."""
    return 2 + max(len(sizes) - 1, 0) + sum(json_size(k) + 1 + n for k, n in sizes.items())


def move_large(root: dict[str, dict], node: dict[str, dict]) -> None:
    """Moves the members of the root's `meta` and `dataset` that would make
    the root large to the node's (spec/virtualize/dicom.md §5): each over
    MAX_MEMBER, then, while the two are over MAX_ROOT together, the largest
    left (of equal sizes, the first by name, `meta`'s before `dataset`'s)."""
    sizes: dict[str, dict[str, int]] = {}
    for name, members in root.items():
        sizes[name] = {}
        for k in list(members):
            n = json_size(members[k])
            if n > MAX_MEMBER:
                node.setdefault(name, {})[k] = members.pop(k)
            else:
                sizes[name][k] = n
    order = list(root)  # meta, then dataset
    total = sum(map(object_size, sizes.values()))
    largest = sorted((-n, k, order.index(name)) for name, s in sizes.items() for k, n in s.items())
    for negative, k, i in largest:
        if total <= MAX_ROOT:
            break
        name = order[i]
        total -= json_size(k) + 1 - negative + (1 if len(root[name]) > 1 else 0)  # the member and its comma
        node.setdefault(name, {})[k] = root[name].pop(k)


def source_metadata(read: Reader, size: int, start: int, encoding: Encoding, pixel_end: int,
                    pixel_extra: tuple[int, int] | None = None,
                    unreferenced: tuple[list[tuple[int, int, int]], int, bool] | None = None,
                    eot_layout: bool = False, fragments: list[tuple[int, int]] | None = None,
                    offset_table: bool = False):
    """(root S, node S, arrays) for a file whose structure the profile has
    accepted (spec/virtualize/dicom.md §5). The dataset is read from its
    start to the end of the file; past Pixel Data (which ends at `pixel_end`),
    what does not parse is kept as bytes. `pixel_extra` is the (offset, length)
    of native pixel data past the frames, and `unreferenced` the members and
    count of the family of bytes that an Extended Offset Table skips, and
    whether they hold the frames' item headers. `eot_layout` says whether the
    profile read the frames from the Extended Offset Table; `fragments` is
    each fragment's (frame, data length), when a frame has more than one, and
    `offset_table` whether the Basic Offset Table is not empty."""
    t = Translator(read, size)
    root: dict = {}
    preamble = read(0, 128)
    if any(preamble):
        root["preamble"] = json_base64(preamble)
    meta = t.meta(start)
    dataset: dict = {}
    t.top = dataset
    groups = GroupLengths()
    seen: set[int] = set()  # the tags of the top-level dataset
    multibyte, charset = False, False
    # The dataset up to Pixel Data: the profile has walked it.
    pos = start
    while True:
        el = t.walker.header(pos, size, encoding)
        if el.tag == PIXEL_DATA:
            groups.step(el.tag, pos)
            dataset[f"{el.tag:08X}"] = {"vr": el.vr or "OW"}  # the image itself
            seen.add(el.tag)
            break
        if eot_layout and el.tag in EOT_TAGS and el.tag not in seen:
            groups.step(el.tag, pos)  # the offset table that the profile read the frames from: layout
            t.omitted.add(f"{el.tag:08X}")
            pos = el.value + el.length
        else:
            pos = t.translate(el, encoding, dataset, size, 0, "dataset", multibyte, pos, groups)
        seen.add(el.tag)
        if el.tag == CHARSET and el.length != UNDEFINED and not charset:
            charset, multibyte = True, is_multibyte(read(el.value, el.length))
    # After Pixel Data: elements as long as they parse and are not duplicates,
    # then the rest as bytes.
    tail_start = pos = pixel_end
    mark = t.mark()
    try:
        while pos < size:
            el = t.walker.header(pos, size, encoding)
            if el.tag >> 16 == 0xFFFE or el.tag in seen:
                raise Rejected("not an element of the dataset")
            pos = t.translate(el, encoding, dataset, size, 0, "dataset", multibyte, pos, groups)
            seen.add(el.tag)
            tail_start, mark = pos, t.mark()
    except Rejected:
        t.rollback(mark)
    groups.finish(tail_start, dataset)
    # The per-frame groups (gathered), and the large members, are vzip_source's.
    node: dict[str, dict] = {}
    if PER_FRAME in dataset:
        node["dataset"] = {PER_FRAME: dataset.pop(PER_FRAME)}
    move_large({"meta": meta, "dataset": dataset}, node)
    root["meta"] = sorted_dataset(meta)
    root["dataset"] = sorted_dataset(dataset)
    if pixel_extra is not None:
        root["pixel_extra"] = t.array("OB", "pixel_extra", *pixel_extra)["BulkDataURI"]
    plans: list[Plan] = []
    if unreferenced is not None:
        plans += family_plans("pixel_unreferenced", unreferenced[0], read, unreferenced[1])
        root["pixel_unreferenced"] = f"{SOURCE_NODE}/pixel_unreferenced"
        if unreferenced[2]:
            root["pixel_unreferenced_headers"] = True
    if fragments is not None:
        packed = struct.pack(f"<{2 * len(fragments)}q", *(v for fragment in fragments for v in fragment))
        plans.append(copied_plan("pixel_fragments", "int64", [len(fragments), 2], ["index", "value"], packed, 8))
        root["pixel_fragments"] = f"{SOURCE_NODE}/pixel_fragments"
    if offset_table:
        root["pixel_offset_table"] = True
    if tail_start < size:
        root["trailing"] = t.array("UN", "trailing", tail_start, size - tail_start)["BulkDataURI"]
    plans += t.plans()
    return root, {name: sorted_dataset(node[name]) for name in ("meta", "dataset") if name in node}, plans
