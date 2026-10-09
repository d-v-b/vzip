"""The lite variant (LV) metadata encoding of ND2 files (conventions/nd2/README.md §2.2)."""

from __future__ import annotations

import base64
import struct
import zlib

from vzip.virtualize.common import Rejected


class LVList(list):
    """A level whose records all have empty names."""


class LVBytes(bytes):
    """A byte array (type 9), as one byte string: as a list, each byte is a
    value of type 3."""


class LVObject(dict):
    """Any other level: its members by name (first position, last value), as the
    profile reads them. `records` keeps every record in order, with its exact
    name (conventions/nd2/README.md §2.2), for the source metadata."""

    records: list


def _well_formed(units: bytes) -> bool:
    """True if UTF-16LE `units` have no unpaired surrogate."""
    i, n = 0, len(units) // 2
    while i < n:
        u = units[2 * i] | units[2 * i + 1] << 8
        if 0xD800 <= u <= 0xDBFF:
            if i + 1 < n and 0xDC00 <= (units[2 * i + 2] | units[2 * i + 3] << 8) <= 0xDFFF:
                i += 2
                continue
            return False
        if 0xDC00 <= u <= 0xDFFF:
            return False
        i += 1
    return True


def _units(data: bytes) -> bytes:
    """UTF-16LE code units up to the first NUL unit."""
    for i in range(0, len(data) - 1, 2):
        if data[i] == 0 and data[i + 1] == 0:
            return data[:i]
    return data


def exact_text(units: bytes):
    """A string's exact JSON value: the text, or {"utf16": base64} when it is
    not well-formed UTF-16 (conventions/nd2/README.md §5.1)."""
    if _well_formed(units):
        return units.decode("utf-16-le")
    return {"utf16": base64.b64encode(units).decode("ascii")}


def exact_name(units: bytes) -> str:
    """A name's exact JSON form: the text, or U+0000 followed by the base64 of
    its UTF-16LE units when it is not well-formed (a name holds no NUL)."""
    if _well_formed(units):
        return units.decode("utf-16-le")
    return "\0" + base64.b64encode(units).decode("ascii")


class Scalar(tuple):
    """A scalar value with its LV record type: (type, value), and for a string
    (type 8) its exact JSON value third."""

    @property
    def type(self) -> int:
        return self[0]

    @property
    def value(self):
        return self[1]


MAX_DEPTH = 100
QUIET_NAN = struct.pack("<Q", 0x7FF8000000000000)  # the NaN that {"float": "NaN"} stands for


class Lossy(Rejected):
    """LV data whose JSON would not keep every byte (conventions/nd2/README.md §5.1)."""


class TooLarge(Rejected):
    """LV data with more records and array bytes than its JSON may have bytes."""


DEFLATE_RATIO = 1032  # the most bytes one byte of a deflate stream inflates to


def _offset_table(records: list[tuple[int, bytes]], level: int, table: bytes) -> bool:
    """True if a level's skipped bytes are its offset table: each record's offset
    from the level record's start, as a u64, each record once, in any order
    (conventions/nd2/README.md §5.1)."""
    offsets = sorted(struct.unpack(f"<{len(table) // 8}Q", table))
    return offsets == sorted(start - level for start, _ in records)


def _records(data: bytes, pos: int, end: int, count: int | None, depth: int = 0, exact: bool = False,
             layout: list | None = None, room: list | None = None):
    """The records in data[pos:end], which are at `depth` (conventions/nd2/README.md §2.2).
    With `exact`, data whose JSON would lose bytes raises Lossy. Each record's
    (start, name units) is appended to `layout`. `room` ([n]) is spent by 1 per
    record and by a byte array's length, each at least that many bytes of JSON:
    past n, TooLarge."""
    if depth > MAX_DEPTH:
        raise Rejected(f"LV levels nested more than {MAX_DEPTH} deep")
    out = []
    while pos < end and (count is None or len(out) < count):
        start = pos
        if room is not None:
            room[0] -= 1
            if room[0] < 0:
                raise TooLarge("LV data with more JSON than its budget")
        if pos + 2 > end:
            raise Rejected("truncated LV record")
        typ, k = data[pos], data[pos + 1]
        pos += 2
        if typ == 76:
            raise Rejected("compressed LV record inside a structure")
        if pos + 2 * k > end:
            raise Rejected("truncated LV record name")
        units = _units(data[pos : pos + 2 * k])
        # The canonical name: none (k = 0) when empty, else its units and one NUL.
        if exact and (len(units) != 2 * (k - 1) if units else k != 0):
            raise Lossy("an LV name other than its units and one NUL, or k = 0 when empty")
        if layout is not None:
            layout.append((start, units))
        name = units.decode("utf-16-le", "replace")
        written_name = exact_name(units)
        pos += 2 * k

        def take(n):
            nonlocal pos
            if pos + n > end:
                raise Rejected("truncated LV value")
            v = data[pos : pos + n]
            pos += n
            return v

        if typ == 1:
            b = take(1)[0]
            if exact and b > 1:
                raise Lossy("an LV bool other than 0 or 1")
            value = Scalar((1, b != 0))
        elif typ in (2, 3, 4, 5, 6, 7):
            fmt = {2: "<i", 3: "<I", 4: "<q", 5: "<Q", 6: "<d", 7: "<Q"}[typ]
            raw = take(4 if typ in (2, 3) else 8)
            value = Scalar((typ, struct.unpack(fmt, raw)[0]))
            if exact and typ == 6 and value.value != value.value and raw != QUIET_NAN:
                raise Lossy("an LV NaN other than 0x7FF8000000000000")
        elif typ == 8:
            units = bytearray()
            while True:
                u = take(2)
                if u == b"\0\0":
                    break
                units += u
            value = Scalar((8, units.decode("utf-16-le", "replace"), exact_text(bytes(units))))
        elif typ == 9:
            n = struct.unpack("<Q", take(8))[0]
            if room is not None:
                room[0] -= n
                if room[0] < 0:
                    raise TooLarge("LV data with more JSON than its budget")
            value = LVBytes(take(n))
        elif typ == 11:
            c, length = struct.unpack("<IQ", take(12))
            level_end = start + length
            if level_end > end or level_end < pos:
                raise Rejected("LV level length outside the data")
            records: list = []
            members, after = _records(data, pos, level_end, c, depth + 1, exact, records, room)
            if len(members) != c or after != level_end:
                raise Rejected("LV level records do not end at its length")
            pos = level_end
            table = take(8 * c)
            if exact and any(table) and not _offset_table(records, start, table):
                raise Lossy("an LV level whose skipped bytes are neither zero nor its offset table")
            if members and all(n == "" for n, _, _ in members):
                value = LVList(v for _, _, v in members)
            else:
                value = _object(members)
        else:
            raise Rejected(f"unknown LV record type {typ}")
        out.append((name, written_name, value))
    return out, pos


def _object(members: list) -> LVObject:
    value = LVObject()
    for n, _, v in members:
        value[n] = v  # first position, last value
    value.records = [(e, v) for _, e, v in members]
    return value


def _inflate(data: bytes, limit: int | None) -> bytes:
    d = zlib.decompressobj()
    try:
        if limit is None:
            inner = d.decompress(data[12:]) + d.flush()
        else:
            inner = d.decompress(data[12:], limit + 1)
            if len(inner) > limit:
                raise Rejected(f"compressed LV data inflates to more than {limit} bytes")
    except zlib.error as e:
        raise Rejected(f"invalid zlib stream in compressed LV data: {e}") from None
    if not d.eof or d.unused_data:
        raise Rejected("compressed LV data does not end with its zlib stream")
    return inner


def decode_lv(data: bytes, limit: int | None = None, inflated: list[int] | None = None,
              exact: bool = False, room: int | list[int] | None = None) -> LVObject:
    """A chunk's LV structure: a tree of LVObjects (objects), LVLists (lists),
    LVBytes (byte arrays) and Scalars. The top level is always an object.
    Compressed data may inflate to at most `limit` bytes; the inflated size is
    appended to `inflated`, or, when the stream does not inflate (it is invalid,
    or inflates past `limit`), the most it could: min(limit, 1032 x the data's
    length) (conventions/nd2/README.md §5.1, the budget). With `exact`, data
    that its JSON would not keep whole (up to the layout of compression and
    offset tables) raises Lossy (the lossless test of conventions/nd2/README.md
    §5.1). With `room`, data of more records and array bytes than that raises
    TooLarge, its JSON being longer; `room` given as [n] is shared, and what
    the data spends is taken from it."""
    if len(data) >= 1 and data[0] == 76:
        if len(data) < 12:
            raise Rejected("truncated compressed LV record")
        try:
            inner = _inflate(data, limit)
        except Rejected:
            if inflated is not None:
                most = DEFLATE_RATIO * len(data)
                inflated.append(min(limit, most) if limit is not None else most)
            raise
        if inflated is not None:
            inflated.append(len(inner))
        if inner[:1] == b"\x4c":
            raise Rejected("compressed LV data inside compressed LV data")
        data = inner
    shared = room if isinstance(room, list) else None if room is None else [room]
    members, _ = _records(data, 0, len(data), None, 0, exact, None, shared)
    return _object(members)
