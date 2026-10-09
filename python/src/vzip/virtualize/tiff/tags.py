"""Every tag of a TIFF's IFDs as JSON: the source metadata of the TIFF and NDPI
conventions (spec/virtualize/tiff.md §5)."""

from __future__ import annotations

import bisect
import json
import struct
from typing import Callable, NamedTuple

from vzip.virtualize.common import (
    MAX_CHUNK, SOURCE_NODE, Output, Plan, Reader, blob_chunks, declare, emit_plans, family_plans, json_base64,
    json_number, metadata_group, row_chunks, text_json,
)

# Bytes per value of each TIFF 6.0 and BigTIFF field type.
SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}
NUMBERS = {3: "H", 4: "I", 6: "b", 8: "h", 9: "i", 11: "f", 12: "d", 13: "I", 16: "Q", 17: "q", 18: "Q"}
# The layout: tags whose values only locate the file's own bytes (strips,
# tiles, free space, a JPEG interchange stream, old-style JPEG tables). They
# are not recorded; the bytes they locate are, where the convention says so.
LAYOUT = frozenset({273, 279, 288, 289, 324, 325, 513, 514, 519, 520, 521})
# JPEGQTables, JPEGDCTables and JPEGACTables: each value is the offset of a
# table, kept as the family <path>/<name> (spec/virtualize/tiff.md §5).
TABLES = {519: "jpeg_q_tables", 520: "jpeg_dc_tables", 521: "jpeg_ac_tables"}
# The pointer tags (spec/virtualize/tiff.md §5): SubIFDs, these, and any
# other tag of type IFD or IFD8 that is not in the convention's table. The
# IFDs they point to are recorded, each in its own group, at <path>/<name>.
POINTERS = {34665: "exif", 34853: "gps", 40965: "interoperability"}
SUBIFDS = 330
GLOBAL_PARAMETERS = 400  # GlobalParametersIFD: a pointer when LONG, IFD or IFD8
UNSIGNED = frozenset({1, 3, 4, 13, 16, 18})  # the field types of offsets
MAX_DEPTH = 4  # the deepest an IFD is read: main-chain IFDs are at depth 0
MAX_IFDS = 100000
MAX_POINTED = 10000  # the most offsets tried through pointer tags
INLINE = 64  # the most values of a numeric tag (or of a pointer tag's IFD list) kept as JSON
MAX_TEXT = 1 << 16  # the longest ASCII value kept as JSON
IFD_BUDGET = 1 << 16  # the most bytes of values as JSON in an IFD object
TOTAL_BUDGET = 1 << 20  # with the file's size (at most 2^24 in all), the most bytes of values measured for JSON
MAX_TOTAL_BUDGET = 1 << 24
DTYPES = {1: "uint8", 7: "uint8", 6: "int8", 2: "uint8", 3: "uint16", 8: "int16", 4: "uint32", 13: "uint32",
          9: "int32", 16: "uint64", 18: "uint64", 17: "int64", 11: "float32", 12: "float64", 5: "uint32",
          10: "int32"}


class Entry(NamedTuple):
    """An IFD entry: its value is `inline` (the value field's bytes), or at `offset`
    (None when it cannot be in the file), or, for NDPI's 64-bit LONGs, `value`.
    `field` is the entry's raw value field (for NDPI, followed by its high word)."""
    tag: int
    type: int
    count: int
    inline: bytes | None = None
    offset: int | None = None
    value: tuple | None = None
    field: bytes = b""


def value_json(data: bytes, typ: int, count: int, order: str):
    if typ == 2:
        return text_json(data[:-1] if data.endswith(b"\0") else data)
    if typ in (5, 10):
        v = struct.unpack(order + ("I" if typ == 5 else "i") * (2 * count), data)
        return [[v[2 * i], v[2 * i + 1]] for i in range(count)]
    fmt = "B" if typ in (1, 7) else NUMBERS[typ]
    return [json_number(v) for v in struct.unpack(order + fmt * count, data)]


def json_size(v) -> int:
    """The bytes of `v`'s compact JSON in UTF-8 (spec/virtualize/tiff.md §5): no
    whitespace, strings escaping only what JSON requires, numbers as ECMAScript's
    Number.prototype.toString writes them."""
    if v is None:
        return 4
    if isinstance(v, str):
        return len(json.dumps(v, ensure_ascii=False).encode())
    if isinstance(v, int):
        return len(str(v))
    if isinstance(v, float):
        return _float_size(v)
    if isinstance(v, list):
        return 2 + max(0, len(v) - 1) + sum(json_size(x) for x in v)
    return 2 + max(0, len(v) - 1) + sum(json_size(k) + 1 + json_size(x) for k, x in v.items())


def _float_size(x: float) -> int:
    """The length of ECMAScript's Number.prototype.toString of a finite `x`."""
    if x == 0:
        return 1  # -0 too
    mant, _, exp = repr(abs(x)).partition("e")  # the shortest digits that round-trip, as ECMAScript's
    whole, _, frac = mant.partition(".")
    digits = (whole + frac).lstrip("0")
    n = len(whole) + int(exp or 0) - (len(whole + frac) - len(digits))  # the decimal point after n digits
    k = len(digits.rstrip("0"))
    if k <= n <= 21:
        size = n
    elif 0 < n <= 21:
        size = k + 1
    elif -6 < n <= 0:
        size = 2 - n + k
    else:  # d[.ddd]e±x
        size = k + (1 if k > 1 else 0) + 2 + len(str(abs(n - 1)))
    return size + (x < 0)


def least_size(e: Entry) -> int:
    """A lower bound of the size of a value's JSON (json_size), from its type and count."""
    if e.type == 2:
        return e.count + 1  # its bytes, perhaps without a final NUL, in quotes
    return 6 * e.count + 1 if e.type in (5, 10) else 2 * e.count + 1


class Translator:
    """Translates IFDs (spec/virtualize/tiff.md §5): each IFD's source metadata
    (collected in `groups`, by path), small values as JSON and large ones as arrays
    of the source metadata node (collected in `arrays`). `table` holds the tags of
    the convention's table, which are never pointers; `recorded` maps the offsets
    of the IFDs recorded (the main chain, SubIFDs, then pointer targets) to their paths,
    and `numbers` their paths to their record numbers."""

    def __init__(self, read: Reader, size: int, order: str, layout=LAYOUT, table=frozenset(),
                 entries_at: Callable[[int], tuple[int, int, Callable[[], list[Entry]]] | None] | None = None,
                 recorded: list[tuple[int, int, str]] = ()) -> None:
        """`entries_at` finds the IFD at an offset (its entry count, the end of its extent and a
        function that reads its entries), for the pointer tags; `recorded` lists the IFDs already
        read as (offset, end of extent, path), in order."""
        self.read, self.size, self.order, self.layout, self.table = read, size, order, layout, table
        self.entries_at = entries_at
        self.arrays: list[Plan] = []
        self.groups: dict[str, dict] = {}
        self.recorded: dict[int, str] = {}  # offset -> path
        self.numbers: dict[str, int] = {}  # path -> record number
        self.referenced: set[str] = set()  # the paths a pointer's array refers to
        self.tries = 0  # the offsets tried through pointer tags
        self.failed: set[int] = set()  # the offsets tried that led to no IFD
        self.total = min(MAX_TOTAL_BUDGET, TOTAL_BUDGET + size)  # what is left of the total budget
        self.kept: dict[tuple, str | None] = {}  # the families kept, by the entries that locate them
        self.pointer_arrays: dict[tuple, list[tuple[bytes, str]]] = {}  # (type, count, field) -> (numbers, path)
        self.resolved: dict[tuple, tuple[int, list]] = {}  # (type, count, field, deep) -> (tries, paths)
        self.starts: list[int] = []  # the extents of the IFDs recorded, merged, sorted
        self.ends: list[int] = []
        for offset, end, path in recorded:
            self.recorded.setdefault(offset, path)
            self.numbers[path] = len(self.numbers)
        for offset, end, _ in sorted(recorded):
            if self.starts and offset < self.ends[-1]:
                self.ends[-1] = max(self.ends[-1], end)
            else:
                self.starts.append(offset)
                self.ends.append(end)

    def overlaps(self, start: int, end: int) -> bool:
        """Whether [start, end) overlaps the extent of an IFD recorded."""
        i = bisect.bisect_left(self.starts, end) - 1
        return i >= 0 and self.ends[i] > start

    def value(self, e: Entry) -> list | None:
        """An entry's integer values, if it has them within the file (for the layout and pointer tags)."""
        size = SIZES.get(e.type)
        if e.value is not None:
            return list(e.value)
        if size is None or e.type not in UNSIGNED:
            return None
        n = size * e.count
        if e.inline is not None:
            data = e.inline[:n]
        elif e.offset is not None and e.offset + n <= self.size:
            data = self.read(e.offset, n)
        else:
            return None
        return list(struct.unpack(self.order + ("B" if e.type == 1 else NUMBERS[e.type]) * e.count, data))

    def pointer(self, e: Entry) -> bool:
        if e.tag == SUBIFDS or e.tag in POINTERS:
            return e.type in UNSIGNED
        if e.tag == GLOBAL_PARAMETERS and e.type == 4:
            return True
        return e.type in (13, 18) and e.tag not in self.table and e.tag not in self.layout

    def spend(self, budget: list[int], size: int) -> bool:
        """Whether a value of JSON `size` is JSON within the IFD's `budget` and the total
        (spec/virtualize/tiff.md §5); either way, it spends the total."""
        fits = size <= budget[0] and size <= self.total
        if fits:
            budget[0] -= size
        self.total -= min(size, self.total)
        return fits

    def ifd(self, entries: list[Entry], path: str, depth: int = 0, data: bool = True) -> dict:
        """Records the IFD at `path` (and its image data if `data`), and the IFDs its pointer tags
        lead to; returns its source metadata."""
        first: dict[int, Entry] = {}
        later: list[Entry] = []
        for e in entries:
            if e.tag in first:
                later.append(e)
            else:
                first[e.tag] = e
        out: dict = {"tags": {}}
        self.groups[path] = out
        budget = [IFD_BUDGET]
        same: dict = {}
        pointers: dict = {}
        for tag in sorted(first):
            e = first[tag]
            if self.pointer(e):
                pointers[str(tag)] = self.follow(e, path, depth, budget, same)
            elif tag not in self.layout:
                out["tags"][str(tag)] = self.tag(e, f"{path}/{tag}", budget)
        if pointers:
            out["pointers"] = pointers
        duplicates = [e for e in later if not self.pointer(e) and e.tag not in self.layout]
        if duplicates:
            out["duplicates"] = [{"tag": e.tag, **self.tag(e, f"{path}/duplicates/{k}", budget)}
                                 for k, e in enumerate(duplicates)]
        if 513 in first and 514 in first:  # JPEGInterchangeFormat and its length
            start, length = self.value(first[513]), self.value(first[514])
            if start and length and length[0] >= 1 and start[0] + length[0] <= self.size:
                chunk, chunks = blob_chunks(start[0], length[0])
                self.arrays.append(Plan(f"{path}/jpeg_interchange", "uint8", [length[0]], [chunk], ["byte"], chunks))
        for tag, name in TABLES.items():
            if tag in first:
                self.tables(first[tag], path, name, tag == 519, same)
        tiles, strips = 324 in first and 325 in first, 273 in first and 279 in first
        if data and (tiles or strips):
            self.family(first[324 if tiles else 273], first[325 if tiles else 279], path, "data", same)
        if tiles and strips:  # a tiled IFD's strips (spec/virtualize/tiff.md §5)
            self.family(first[273], first[279], path, "strips", same)
        if same:
            out["same_as"] = same
        return out

    def keep(self, key: tuple, path: str, name: str, same: dict, members: Callable[[], list | None]) -> None:
        """The family `<path>/<name>` of `members()`, or, when a family with the same `key`
        was kept earlier, the member `name` of `same`: its path."""
        if key in self.kept:
            if self.kept[key] is not None:
                same[name] = self.kept[key]
            return
        found = members()
        self.kept[key] = f"{path}/{name}" if found else None
        if found:
            self.arrays += family_plans(f"{path}/{name}", found, self.read)

    def tables(self, e: Entry, path: str, name: str, quantization: bool, same: dict) -> None:
        """Old-style JPEG tables (JPEGQTables, JPEGDCTables, JPEGACTables): member i of the
        family at `path/name` is the table at value i, 64 bytes for a quantization table, else
        16 bytes of counts and as many values as they sum to (spec/virtualize/tiff.md §5)."""

        def members():
            offsets = self.value(e)
            if offsets is None:
                return None
            found = []
            for i, o in enumerate(offsets):
                if quantization:
                    n = 64
                elif o + 16 <= self.size:
                    n = 16 + sum(self.read(o, 16))
                else:
                    continue
                if o + n <= self.size:
                    found.append((i, o, n))
            return found

        self.keep(("q" if quantization else "huffman", e.type, e.count, e.field), path, name, same, members)

    def family(self, offsets: Entry, counts: Entry, path: str, name: str, same: dict) -> None:
        """Strips or tiles as the family of bytes `<path>/<name>` (spec/virtualize/tiff.md §5)."""

        def members():
            o, n = self.value(offsets), self.value(counts)
            if o is None or n is None:
                return None
            return [(i, a, b) for i, (a, b) in enumerate(zip(o, n)) if b > 0 and a + b <= self.size]

        key = ("data", offsets.type, offsets.count, offsets.field, counts.type, counts.count, counts.field)
        self.keep(key, path, name, same, members)

    def follow(self, e: Entry, path: str, depth: int, budget: list[int], same: dict) -> dict:
        """A pointer tag: its type, count and the IFDs it leads to, as paths (at most
        INLINE values, within the budget) or as the record numbers of the array <path>/<tag>."""
        m: dict = {"type": e.type, "count": json_number(e.count)}
        name = "subifds" if e.tag == SUBIFDS else POINTERS.get(e.tag, f"ifd_{e.tag}")
        # The same values, resolved as deep with no offset tried since, lead to the same IFDs.
        memo = (e.type, e.count, e.field, depth < MAX_DEPTH)
        done = self.resolved.get(memo)
        if done is not None and done[0] == self.tries:
            paths = done[1]
        else:
            values = self.value(e)
            if values is None:
                return m
            paths = [self.lead(at, f"{path}/{name}/{j}" if e.tag == SUBIFDS or len(values) > 1
                               else f"{path}/{name}", depth) for j, at in enumerate(values)]
            self.resolved[memo] = (self.tries, paths)
        if len(paths) <= INLINE and self.spend(budget, json_size(paths)):
            m["ifds"] = paths
            return m
        numbers = [-1 if p is None else self.numbers[p] for p in paths]
        packed = struct.pack(f"<{len(numbers)}i", *numbers)
        earlier = self.pointer_arrays.setdefault((e.type, e.count, e.field), [])
        for other, at in earlier:
            if other == packed:
                same[str(e.tag)] = at
                return m
        earlier.append((packed, f"{path}/{e.tag}"))
        self.referenced.update(p for p in paths if p is not None)
        n = len(numbers)
        k = -(-4 * n // MAX_CHUNK)
        c = -(-n // k)
        chunks = {(q,): packed[4 * q * c:4 * (q + 1) * c].ljust(4 * c, b"\0") for q in range(k)}
        self.arrays.append(Plan(f"{path}/{e.tag}", "int32", [n], [c], ["value"], chunks))
        return m

    def lead(self, at: int, child: str, depth: int) -> str | None:
        """The path of the IFD a pointer value `at` leads to, recording it at `child` (and
        translating it) when it is new (spec/virtualize/tiff.md §5), or None."""
        if at in self.recorded:
            return self.recorded[at]
        if (at in self.failed or depth >= MAX_DEPTH or len(self.recorded) >= MAX_IFDS
                or self.tries >= MAX_POINTED or self.entries_at is None):
            return None
        self.tries += 1
        found = self.entries_at(at)
        # Its extent comes from its entry count alone: an IFD that overlaps one recorded is not read.
        if found is None or found[0] == 0 or self.overlaps(at, found[1]):
            self.failed.add(at)
            return None
        self.recorded[at] = child
        self.numbers[child] = len(self.numbers)
        i = bisect.bisect_left(self.starts, at)
        self.starts.insert(i, at)
        self.ends.insert(i, found[1])
        self.ifd(found[2](), child, depth + 1)
        return child

    def tag(self, e: Entry, array: str, budget: list[int]) -> dict:
        """A tag as JSON; a large value, or one past the budget, is the array at `array`,
        referenced where the file holds it."""
        m: dict = {"type": e.type, "count": json_number(e.count)}
        size = SIZES.get(e.type)
        if size is None:
            m["field"] = json_base64(e.field)  # a field type TIFF does not define: its size is unknown
            return m
        nbytes = size * e.count
        if e.value is not None:
            m["value"] = [json_number(v) for v in e.value]
            return m
        if e.inline is not None:  # in the entry's value field: always JSON
            m["value"] = value_json(e.inline[:nbytes], e.type, e.count, self.order)
            return m
        if e.offset is None or e.offset + nbytes > self.size:
            return m  # its bytes cannot be found
        small = (e.type == 2 and nbytes <= MAX_TEXT) or (e.type != 2 and e.count <= INLINE)
        if small:
            if least_size(e) > self.total:  # past the total budget, whatever its size
                self.total = 0
            else:
                value = value_json(self.read(e.offset, nbytes), e.type, e.count, self.order)
                if self.spend(budget, json_size(value)):
                    m["value"] = value
                    return m
        pair = e.type in (5, 10)
        rows, chunks = row_chunks(e.offset, e.count, size, (0,) if pair else ())
        self.arrays.append(Plan(
            array, DTYPES[e.type], [e.count, 2] if pair else [e.count], [rows, 2] if pair else [rows],
            ["value", "part"] if pair else ["value"], chunks,
            endian="little" if self.order == "<" else "big"))
        return m

    def emit(self, out: Output, profile: str, node: dict) -> None:
        """The source metadata node (with its own source metadata `node`), one group
        per IFD, and the arrays."""
        for p in self.referenced:
            self.groups[p]["record"] = self.numbers[p]
        emit_plans(out, self.arrays, declare({}, profile, None, node))
        parents = {"/".join(p.split("/")[:k]) for p in self.groups for k in range(1, p.count("/") + 1)}
        for g in sorted(parents - set(self.groups)):
            metadata_group(out, f"{SOURCE_NODE}/{g}")
        for p, s in self.groups.items():
            metadata_group(out, f"{SOURCE_NODE}/{p}", declare({}, profile, None, s))
