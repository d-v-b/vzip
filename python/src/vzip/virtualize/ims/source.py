"""The HDF5 tree of an Imaris file as the source metadata node of the IMS
convention (spec/virtualize/ims.md §5): every group, dataset, attribute,
link and named datatype, with each dataset's data as an array."""

from __future__ import annotations

import json
import struct
import zlib

from vzip.virtualize.common import (
    MAX_CHUNK, RAGGED_CHUNK, SOURCE_NODE, Output, Plan, Reader, Rejected, declare, decode_text, family_plans, grid_chunks,
    json_base64, json_number, json_text, metadata_array, metadata_group, text_json,
)
from vzip.virtualize.ims.hdf5 import (
    DATASPACE, DATATYPE, EXTERNAL_FILES, FILL_VALUE, LAYOUT, LINK_INFO, MAX_SAFE, SYMBOL_TABLE, Dataset, Hdf5,
    address, attribute_full, fill_value, le, parse_dataspace, parse_selection,
)

MAX_VALUE_BYTES = 1 << 26  # the most attribute data read per file
MAX_OBJECTS = 100000  # objects walked per file, besides PER_IMAGE for each Data dataset of the image
PER_IMAGE = 5  # its channel group, Data, Histogram, Histogram1024, and a time point group (§5.1)
MAX_ELEMENTS = 1 << 20  # elements of a dataset whose datatype holds addresses (peak memory: under 1 KB each)
MAX_DEPTH = 16  # nesting of datatypes
INLINE = 64  # the most numbers of an attribute held as JSON
MAX_TEXT = 1 << 20  # the longest text of an attribute held as JSON
MAX_RAW = 4096  # the most bytes of an attribute held as base64
BUDGET = 1 << 16  # the cost of the entries one document holds (§5.6)
MAX_HEAP = 1 << 16  # the most bytes of variable-length elements of an attribute held as JSON
MAX_COPY = 1 << 24  # the most bytes of a dataset decoded and copied
MAX_SELECTIONS = 1 << 22  # the most bytes of region references' selections read per file
# The datatype of Imaris's attributes, whose text is their value (§5.3).
IMARIS_TEXT = {"class": "string", "size": 1, "padding": "null-terminated", "charset": "ascii"}
VLEN_REASON = "variable-length data of references or variable-length data"
PADDING = ("null-terminated", "null-padded", "space-padded")
CHARSETS = ("ascii", "utf-8")
# IEEE 754 binary16, 32 and 64: (sign location, exponent location, exponent
# size, exponent bias, mantissa location, mantissa size).
IEEE = {2: (15, 10, 5, 15, 0, 10), 4: (31, 23, 8, 127, 0, 23), 8: (63, 52, 11, 1023, 0, 52)}
FORMATS = {1: "b", 2: "h", 4: "i", 8: "q"}
FLOATS = {2: "e", 4: "f", 8: "d"}
ZLIB = {"name": "zlib", "configuration": {"level": 1}}


# ---- datatypes (spec/virtualize/ims.md §5.2)

def _choice(names: tuple, i: int):
    return names[i] if i < len(names) else i


def _member_name(d: bytes, q: int, version: int) -> tuple[str, int]:
    end = d.find(b"\0", q)
    if end < 0:
        raise Rejected("truncated datatype")
    return decode_text(d[q:end]), (q + (end - q + 8) // 8 * 8 if version < 3 else end + 1)


def describe_type(d: bytes, p: int = 0, depth: int = 0) -> tuple[dict, int]:
    """The JSON description of the datatype message at `p`, and where it ends."""
    if depth > MAX_DEPTH:
        raise Rejected("datatypes nested too deeply")
    cv = le(d, p, 1)
    cls, version = cv & 15, cv >> 4
    if not 1 <= version <= 5:
        raise Rejected(f"unsupported datatype message version {version}")
    bits, size, q = le(d, p + 1, 3), le(d, p + 4, 4), p + 8
    order = "big" if bits & 1 else "little"
    if cls in (0, 4):
        offset, precision = le(d, q, 2), le(d, q + 2, 2)
        t: dict = {"class": "integer" if cls == 0 else "bitfield", "size": size, "order": order}
        if cls == 0:
            t["signed"] = bool(bits & 8)
        if offset or precision != 8 * size or bits & 6:
            t.update(offset=offset, precision=precision, padding=[bits >> 1 & 1, bits >> 2 & 1])
        return t, q + 4
    if cls == 1:
        offset, precision = le(d, q, 2), le(d, q + 2, 2)
        eloc, esize, mloc, msize, bias = le(d, q + 4, 1), le(d, q + 5, 1), le(d, q + 6, 1), le(d, q + 7, 1), le(d, q + 8, 4)
        sign, norm = bits >> 8 & 0xFF, bits >> 4 & 3
        t = {"class": "float", "size": size, "order": "vax" if bits & 0x40 else order}
        if (bits & 0x4E or norm != 2 or offset or precision != 8 * size
                or IEEE.get(size) != (sign, eloc, esize, bias, mloc, msize)):
            t["layout"] = {"offset": offset, "precision": precision, "sign": sign, "exponent": [eloc, esize, bias],
                           "mantissa": [mloc, msize, norm], "padding": [bits >> 1 & 1, bits >> 2 & 1, bits >> 3 & 1]}
        return t, q + 12
    if cls == 2:
        return {"class": "time", "size": size, "order": order, "precision": le(d, q, 2)}, q + 2
    if cls == 3:
        return {"class": "string", "size": size, "padding": _choice(PADDING, bits & 15),
                "charset": _choice(CHARSETS, bits >> 4 & 15)}, q
    if cls == 5:
        n = bits & 0xFF
        le(d, q, n) if n else None
        return {"class": "opaque", "size": size, "tag": json_text(d[q : q + n])}, q + n
    if cls == 6:
        members = []
        for _ in range(bits & 0xFFFF):
            name, q = _member_name(d, q, version)
            if version >= 3:
                k = max(1, (size.bit_length() + 7) // 8)
                offset, q = le(d, q, k), q + k
                member, q = describe_type(d, q, depth + 1)
            elif version == 2:
                offset, q = le(d, q, 4), q + 4
                member, q = describe_type(d, q, depth + 1)
            else:
                offset, rank = le(d, q, 4), le(d, q + 4, 1)
                if rank > 4:
                    raise Rejected("a compound member of more than 4 dimensions")
                dims = [le(d, q + 16 + 4 * i, 4) for i in range(rank)]
                member, q = describe_type(d, q + 32, depth + 1)
                if rank:
                    n = 1
                    for v in dims:
                        n *= v
                    member = {"class": "array", "size": member["size"] * n, "shape": dims, "base": member}
            members.append({"name": name, "offset": offset, "type": member})
        return {"class": "compound", "size": size, "members": members}, q
    if cls == 7:
        return {"class": "reference", "size": size, "kind": bits & 15}, q
    if cls == 8:
        base, q = describe_type(d, q, depth + 1)
        if base["class"] != "integer":
            raise Rejected("an enumeration of a datatype that is not an integer")
        names = []
        for _ in range(bits & 0xFFFF):
            name, q = _member_name(d, q, version)
            names.append(name)
        n = base["size"]
        le(d, q, len(names) * n) if names and n else None
        values: dict = {}
        for i, name in enumerate(names):
            values.setdefault(name, integer(base, d[q + i * n : q + (i + 1) * n]))
        return {"class": "enum", "size": size, "base": base, "members": values}, q + len(names) * n
    if cls == 9:
        base, q = describe_type(d, q, depth + 1)
        t = {"class": "variable-length", "size": size, "base": base}
        if bits & 15 == 1:
            t.update(string=True, padding=_choice(PADDING, bits >> 4 & 15), charset=_choice(CHARSETS, bits >> 8 & 15))
        return t, q
    if cls == 10:
        rank = le(d, q, 1)
        if version < 3:
            dims, q = [le(d, q + 4 + 4 * i, 4) for i in range(rank)], q + 4 + 8 * rank
        else:
            dims, q = [le(d, q + 1 + 4 * i, 4) for i in range(rank)], q + 1 + 4 * rank
        base, q = describe_type(d, q, depth + 1)
        return {"class": "array", "size": size, "shape": dims, "base": base}, q
    raise Rejected(f"unknown datatype class {cls}")


def integer(t: dict, b: bytes):
    """An integer of datatype `t`, as JSON."""
    return json_number(int.from_bytes(b, "big" if t["order"] == "big" else "little", signed=t.get("signed", False)))


def numeric(t: dict) -> tuple[str, str] | None:
    """(Zarr data type, struct format) of an integer or IEEE float datatype, else None."""
    if t.get("order") not in ("little", "big"):
        return None
    if t["class"] == "integer" and "offset" not in t and t["size"] in FORMATS:
        f = FORMATS[t["size"]]
        return (f"int{8 * t['size']}", f) if t["signed"] else (f"uint{8 * t['size']}", f.upper())
    if t["class"] == "float" and "layout" not in t and t["size"] in FLOATS:
        return f"float{8 * t['size']}", FLOATS[t["size"]]
    return None


def element(t: dict) -> tuple[dict, list[int]] | None:
    """The number datatype and the extra dimensions of a dataset's elements of
    datatype `t`: its own for a number, its base's for an enumeration, and
    those of its base, after its own dimensions, for an array; else None."""
    if t["class"] == "enum":
        return element(t["base"])
    if t["class"] == "array":
        inner = element(t["base"])
        return (inner[0], [*t["shape"], *inner[1]]) if inner is not None else None
    return (t, []) if numeric(t) is not None else None


def numbers(t: dict, count: int, data: bytes) -> list | None:
    n = numeric(t)
    if n is None:
        return None
    order = ">" if t["order"] == "big" else "<"
    return [json_number(v) for v in struct.unpack(f"{order}{count}{n[1]}", data)]


def exact_numbers(t: dict, count: int, data: bytes) -> list | None:
    """The numbers of datatype `t`, as JSON, when JSON holds them exactly:
    None when one is a NaN other than the canonical quiet NaN or a negative zero."""
    values = numbers(t, count, data)
    if values is None or t["class"] != "float":
        return values
    size = t["size"]
    nan, sign = _canonical_nan(size, t["order"]), 0 if t["order"] == "big" else size - 1
    for i, v in enumerate(values):
        b = data[i * size : (i + 1) * size]
        if (v == "NaN" and b != nan) or (v == 0 and b[sign] & 0x80):
            return None
    return values


def _product(dims: list[int]) -> int:
    n = 1
    for v in dims:
        n *= v
    return n


# ---- JSON texts and their sizes (spec/virtualize/ims.md §5.6)

def js_number(v: float) -> str:
    """A finite binary64 number as ECMAScript's Number::toString writes it."""
    if v == 0:
        return "0"
    if v.is_integer() and abs(v) < 2**53:  # exactly its digits; larger ones as the shortest that round-trip
        return str(int(v))
    r = repr(v)
    sign = "-" if r.startswith("-") else ""
    mantissa, _, exponent = r.lstrip("-").partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = whole + fraction
    n = len(whole) + (int(exponent) if exponent else 0) - (len(digits) - len(digits.lstrip("0")))
    digits = digits.strip("0")
    k = len(digits)
    if k <= n <= 21:
        s = digits + "0" * (n - k)
    elif 0 < n <= 21:
        s = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        s = "0." + "0" * -n + digits
    else:
        e = n - 1
        s = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return sign + s


def canonical(v) -> str:
    """The JSON text of a value (§5.6): no whitespace, object members in ascending
    order of their keys' UTF-8 bytes, characters other than those JSON escapes
    as themselves, and numbers as ECMAScript writes them."""
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return js_number(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ",".join(canonical(x) for x in v) + "]"
    return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + canonical(v[k])
                          for k in sorted(v, key=lambda k: k.encode())) + "}"


def size(v) -> int:
    """The size of a value: the UTF-8 length of its JSON text."""
    return len(canonical(v).encode())


# ---- the tree (spec/virtualize/ims.md §5.1)

def holds_addresses(t: dict) -> bool:
    """Whether elements of datatype `t` hold file addresses: references and
    variable-length data, at any depth."""
    if t["class"] in ("reference", "variable-length"):
        return True
    if t["class"] == "compound":
        return any(holds_addresses(m["type"]) for m in t["members"])
    return t["class"] == "array" and holds_addresses(t["base"])


def column(t: dict) -> str | None:
    """How a dataset's elements of datatype `t`, which holds addresses, are kept
    (§5.4): a family of byte values, indexes into the object table, or a family
    of region references; None when they are not."""
    if t["class"] == "variable-length" and not holds_addresses(t["base"]):
        return "family"
    if t["class"] == "reference" and (t["kind"], t["size"]) == (0, 8):
        return "paths"
    if t["class"] == "reference" and (t["kind"], t["size"]) == (1, 12):
        return "regions"
    return None


def node_name(key: str) -> bool:
    """Whether a member's name can name a Zarr node of the hierarchy (§5.1)."""
    return bool(key) and key not in (".", "..", "zarr.json") and not key.startswith("__") and "/" not in key


def _element_text(e: bytes, padding) -> bytes | None:
    """A fixed-size string element's text: its bytes without the trailing
    padding (spaces for a space-padded string, else NUL), if no NUL is left."""
    text = e.rstrip(b" " if padding == "space-padded" else b"\0")
    return None if b"\0" in text else text


def _put(entry: dict, dims: list[int], values) -> dict:
    """The value form: one value for a scalar, the list for one dimension,
    else the list and the shape."""
    if not dims:
        entry["value"] = values[0]
    else:
        if len(dims) != 1:
            entry["shape"] = dims
        entry["value"] = values
    return entry


def _shape(entry: dict, dims: list[int], more: dict) -> dict:
    if len(dims) != 1:
        entry["shape"] = dims
    entry.update(more)
    return entry


def _canonical_nan(size: int, order: str) -> bytes:
    v = {2: 0x7E00, 4: 0x7FC00000, 8: 0x7FF8000000000000}[size]
    return v.to_bytes(size, order)


def exact_fill(base: dict | None, fill: bytes):
    """The Zarr fill value that is exactly the dataset's fill value (§5.4), or
    None: the elements' number when they are all the same bytes (an integer
    as it is, NaN only as the canonical quiet NaN, never -0), and the byte for
    an array of bytes when they are all equal."""
    size = base["size"] if base is not None else 1
    first = fill[:size]
    if any(fill[i : i + size] != first for i in range(0, len(fill), size)):
        return None
    if base is None:
        return first[0]
    order = "big" if base["order"] == "big" else "little"
    if base["class"] == "integer":
        return int.from_bytes(first, order, signed=base["signed"])
    v = struct.unpack(f"{'>' if order == 'big' else '<'}{FLOATS[size]}", first)[0]
    if v != v:
        return "NaN" if first == _canonical_nan(size, order) else None
    if v == 0 and first[0 if order == "big" else -1] & 0x80:
        return None
    return json_number(v)


def ref_address(b: bytes) -> int | None:
    """The object header address of an object reference, or None (§8.9)."""
    v = le(b, 0, 8)
    return v if 0 < v <= MAX_SAFE else None


def unshuffle(data: bytes, item: int) -> bytes:
    """Undoes HDF5's shuffle filter: byte j of element i is at j * n + i."""
    n = len(data) // item
    if item <= 1 or n <= 1:
        return data
    out = bytearray(len(data))
    for j in range(item):
        out[j : n * item : item] = data[j * n : (j + 1) * n]
    out[n * item :] = data[n * item :]
    return bytes(out)


KEYED = ("attributes", "names", "links", "images", "datatypes", "unsupported")
LISTS = ("attribute_collisions", "collisions")


def merge(s: dict, entry: dict) -> None:
    """Adds an entry to a source metadata object (§5.6)."""
    for k, v in entry.items():
        if k in KEYED and v is not None:
            s.setdefault(k, {}).update(v)
        elif k in LISTS:
            s.setdefault(k, []).extend(v)
        else:
            s[k] = v


class Doc:
    """A document of the source metadata node: its source metadata `s`, which
    settling fills, and its entries in order (§5.6)."""

    def __init__(self, s: dict) -> None:
        self.s = s
        self.entries: list[tuple] = []


class Tree:
    """Walks the HDF5 tree from the root group; a failure to read an object
    records it, and never rejects. The documents are settled after the walk,
    when every path is known."""

    def __init__(self, f: Hdf5, read: Reader, images: dict[int, dict]) -> None:
        self.f, self.read, self.images = f, read, images
        self.used = 0
        self.selections = 0  # bytes of selections read for region references (MAX_SELECTIONS)
        self.regions: dict[tuple[int, int], tuple] = {}  # (collection, index) -> (object header, selection)
        self.objects = 0
        self.seen: dict[int, str] = {}  # object header -> the path where the walk met it
        self.order: dict[int, int] = {}  # object header -> its place in the walk, its index in the object table
        self.parents: list[int] = []  # the object table: each object's parent's index (-1 for the root)
        self.names: list[bytes] = []  # and its key
        self.groups: list[tuple[str, dict]] = []  # (path under the node, source metadata)
        self.plans: list[Plan] = []
        self.attribute_plans: list[Plan] = []
        self.attribute_groups: list[str] = []
        self.docs: list[Doc] = []
        self.jobs: list = []  # arrays built once the object table is known
        self.arrays = 0  # attributes/<i> numbered so far
        self.spills = 0  # spilled/<i> numbered so far

    def meet(self, at: int, path: str, parent: int | None, key: str) -> None:
        """Numbers an object the walk meets, in the object table (§5.7)."""
        self.order[at] = len(self.order)
        self.seen[at] = path
        self.parents.append(-1 if parent is None else self.order[parent])
        self.names.append(key.encode())

    def index_of(self, at: int | None):
        """The index in the object table of the object at `at`, or None."""
        return self.order.get(at) if at is not None else None

    def doc(self, s: dict) -> Doc:
        d = Doc(s)
        self.docs.append(d)
        return d

    def type_part(self, t: dict, named: int | None) -> dict:
        """A datatype's members (§5.2): its description, or, when it is named and
        the walk met the committed datatype, its path alone."""
        if named is None:
            return {"datatype": t}
        index = self.order.get(named)
        return {"named": index} if index is not None else {"named": None, "datatype": t}

    # ---- attributes (§5.3)

    def attribute_items(self, at: int) -> list | None:
        """An object's attributes, in ascending byte order of their names: (key,
        name as a text value when it must be given, message, collision, the
        message's address), or None when they cannot be read."""
        where: dict[bytes, int] = {}
        try:
            raw = self.f.attributes(at, where)
        except Rejected:
            return None
        items, keys = [], set()
        for name in sorted(raw):
            key, text = decode_text(name), text_json(name)
            clash = key in keys
            keys.add(key)
            items.append((key, text if clash or not isinstance(text, str) else None, raw[name], clash, where[name]))
        return items

    def attribute_entry(self, item: tuple, room: int) -> tuple:
        """An attribute's value and its cost, held as JSON only within `room` (§5.6)."""
        key, name, raw, clash, at = item

        def cost(v) -> int:
            return size(v) + (0 if clash else size(key))

        value = self.attribute(raw, name, lambda v: cost(v) <= room, at)
        return value, cost(value)

    def attribute(self, raw: bytes, name, fits, at: int):
        """An attribute's value; held as JSON when not large and `fits` allows it.
        `at` is the message's address, so that the array form references its data."""
        try:
            type_message, dims, data, named, start = attribute_full(raw, self.f.committed)
            t, _ = describe_type(type_message)
        except Rejected as e:
            return {"reason": str(e), **({"name": name} if name is not None else {})}
        entry = self.type_part(t, named)
        if name is not None:
            entry["name"] = name
        if dims is None:
            entry["shape"] = None
            return entry
        if self.used + len(data) > MAX_VALUE_BYTES:
            return _shape(entry, dims, {"reason": "over the budget of attribute data"})
        self.used += len(data)
        count = _product(dims)
        if t["size"] == 0 and count > MAX_TEXT:
            return _shape(entry, dims, {"reason": "more than 2^20 elements of no bytes"})
        selections = self.selections
        try:
            held, large, array = self.forms(t, dims, data, count, name is None and named is None, at + start)
        except Rejected as e:
            self.selections = selections  # what an object that fails read is not counted
            return _shape(entry, dims, {"reason": str(e)})
        if held is not None and not large:
            value = held[1] if isinstance(held, tuple) else {**entry, **held}
            if fits(value):
                return value
        path = f"attributes/{self.arrays}"
        self.arrays += 1
        entry.update(array(path))
        return entry

    def forms(self, t: dict, dims: list[int], data: bytes, count: int, plain: bool, at: int):
        """(the attribute's value as JSON, or None; whether it is large; the
        array form, a function of its path giving its members) (§5.3). The
        file holds the data at `at`, where the array form references it."""
        cls = t["class"]
        if cls == "string":
            def array(path):
                self.data_job(path, "uint8", [len(data)], 1, at, "little")
                return {"array": path, **({"shape": dims} if dims != [len(data)] else {})}

            if len(data) > MAX_TEXT:
                return None, True, array
            large = False
            if len(dims) == 1 and t["size"] == 1 and b"\0" not in data:
                text = text_json(data)
                if plain and t == IMARIS_TEXT:
                    return ("bare", text), large, array
                return {"value": text}, large, array
            if t["size"] == 0:
                texts = [b""] * count  # a string of no bytes is empty
            else:
                texts = [_element_text(data[i : i + t["size"]], t["padding"]) for i in range(0, len(data), t["size"])]
            if None not in texts:
                return _put({}, dims, [text_json(b) for b in texts]), large, array
            return self.data_form(dims, data), large or len(data) > MAX_RAW, array
        if numeric(t) is not None:
            def array(path):
                self.data_job(path, numeric(t)[0], dims, t["size"], at, t["order"])
                return _shape({}, dims, {"array": path})

            if count > INLINE:
                return None, True, array
            values = exact_numbers(t, count, data)
            if values is None:
                return self.data_form(dims, data), len(data) > MAX_RAW, array
            return _put({}, dims, values), False, array
        if cls == "variable-length" and not holds_addresses(t["base"]):
            heap = [self.heap_object(t, data[16 * i : 16 * i + 16]) for i in range(count)]

            def array(path):
                self.attribute_family(path, family_plans(path, [(i, w, n) for i, (w, n) in enumerate(heap)],
                                                         self.read, count))
                return _shape({}, dims, {"array": path})

            if sum(n for _, n in heap) > MAX_HEAP:
                return None, True, array
            values = [self.vlen_value(t, self.heap_bytes(where, n, True)) for where, n in heap]
            return _put({}, dims, values), False, array
        if column(t) == "paths":
            refs = [ref_address(data[8 * i : 8 * i + 8]) for i in range(count)]

            def array(path):
                self.jobs.append((self.index_array, path, refs, dims, None, False))
                return _shape({}, dims, {"array": path})

            return _put({}, dims, [self.index_of(a) for a in refs]), count > INLINE, array
        if holds_addresses(t):
            n = t["size"]
            values = [self.structured(t, data[i * n : (i + 1) * n], True) for i in range(count)]

            def array(path):
                text = canonical(values[0] if not dims else values).encode()
                self.attribute_array(path, "uint8", [len(text)], 1, text, "little")
                return {"json": path, **({"shape": dims} if len(dims) > 1 else {})}

            return _put({}, dims, values), False, array

        def array(path):
            self.data_job(path, "uint8", [*dims, t["size"]], 1, at, "little")
            return {"array": path, "shape": dims}

        if len(data) > MAX_RAW:
            return None, True, array
        return self.data_form(dims, data), False, array

    @staticmethod
    def data_form(dims: list[int], data: bytes) -> dict:
        return {"data": json_base64(data), **({"shape": dims} if len(dims) != 1 else {})}

    def heap_object(self, t: dict, ref: bytes) -> tuple[int, int]:
        """(address, size) of a variable-length element's bytes."""
        n, at = le(ref, 0, 4), address(le(ref, 4, 8))
        size = n if t.get("string") else n * t["base"]["size"]
        if not n:
            return 0, 0
        if at is None:
            raise Rejected("a variable-length element at an undefined address")
        where, stored = self.f.global_object(at, le(ref, 12, 4))
        if stored < size:
            raise Rejected("a variable-length element longer than its heap object")
        return where, size

    def heap_bytes(self, where: int, n: int, counted: bool) -> bytes:
        """Bytes of the global heap; `counted` reads them within the budget of attribute data."""
        if counted:
            if self.used + n > MAX_VALUE_BYTES:
                raise Rejected("over the budget of attribute data")
            self.used += n
        return self.read(where, n) if n else b""

    @staticmethod
    def vlen_value(t: dict, data: bytes):
        """A variable-length element whose base holds no addresses, as JSON."""
        if t.get("string"):
            return text_json(data)
        base = t["base"]
        values = exact_numbers(base, len(data) // base["size"] if base["size"] else 0, data)
        return values if values is not None else json_base64(data)

    def structured(self, t: dict, b: bytes, counted: bool):
        """An element of a datatype that holds addresses, as JSON (§5.3)."""
        if not holds_addresses(t):
            if numeric(t) is not None:
                v = exact_numbers(t, 1, b)
                if v is not None:
                    return v[0]
            return json_base64(b)
        cls = t["class"]
        if cls == "reference":
            kind = column(t)
            if kind == "paths":
                return self.index_of(ref_address(b))
            if kind == "regions":
                return self.region_value(self.region(b))
            raise Rejected("a reference that is neither an object nor a region reference")
        if cls == "variable-length":
            data = self.heap_bytes(*self.heap_object(t, b), counted)
            if not holds_addresses(t["base"]):
                return self.vlen_value(t, data)
            n = t["base"]["size"]
            return [self.structured(t["base"], data[i * n : (i + 1) * n], counted) for i in range(le(b, 0, 4))]
        if cls == "compound":
            out: dict = {}
            for m in t["members"]:
                mt, o = m["type"], m["offset"]
                if o + mt["size"] > len(b):
                    raise Rejected("a compound member outside its compound")
                if m["name"] in out:
                    raise Rejected("compound members of the same name")
                out[m["name"]] = self.structured(mt, b[o : o + mt["size"]], counted)
            return out
        n, count = t["base"]["size"], _product(t["shape"])
        if n * count > len(b):
            raise Rejected("an array larger than its datatype")
        return [self.structured(t["base"], b[i * n : (i + 1) * n], counted) for i in range(count)]

    def region(self, b: bytes) -> tuple[int | None, dict] | None:
        """A region reference's (object header address, selection), or None for
        the null reference (§8.9). Each reference counts the size of its global
        heap object against the budget of selections; each object is read once."""
        at = address(le(b, 0, 8))
        if at is None or at == 0:
            return None
        key = (at, le(b, 8, 4))
        where, n = self.f.global_object(*key)
        if self.selections + n > MAX_SELECTIONS:
            raise Rejected("over the budget of region selections")
        self.selections += n
        if key not in self.regions:
            d = self.read(where, n) if n else b""
            self.regions[key] = (ref_address(d), parse_selection(d, 8)[0])
        return self.regions[key]

    def region_value(self, found: tuple | None):
        """A region reference as JSON (§5.3): null, or its object's index and its selection."""
        return None if found is None else {"object": self.index_of(found[0]), "selection": found[1]}

    def attribute_array(self, path: str, data_type: str, shape: list[int], item: int, data: bytes, endian: str,
                        attributes: dict | None = None, plans: list | None = None):
        """A copy of in-memory data as an array (conventions §7)."""
        chunk_shape, ranges = grid_chunks(0, shape, item, lambda o, n: data[o : o + n])
        chunks = {k: r if isinstance(r, bytes) else b"".join(data[p[0] : p[0] + p[1]] if isinstance(p, tuple) else p
                                                              for p in r)
                  for k, r in ranges.items()}
        (self.attribute_plans if plans is None else plans).append(
            Plan(path, data_type, shape, chunk_shape, None, chunks, attributes=attributes, endian=endian))

    def data_job(self, path: str, data_type: str, shape: list[int], item: int, at: int, endian: str) -> None:
        """An attribute's data as an array, built once the documents are settled."""
        self.jobs.append((self.attribute_data, path, data_type, shape, item, at, endian))

    def attribute_data(self, path: str, data_type: str, shape: list[int], item: int, at: int, endian: str) -> None:
        """An attribute's data as an array, referenced where the file holds it (conventions §7)."""
        chunk_shape, chunks = grid_chunks(at, shape, item, self.read)
        self.attribute_plans.append(Plan(path, data_type, shape, chunk_shape, None, chunks, endian=endian))

    def attribute_family(self, path: str, plans: list[Plan]) -> None:
        if plans[0].path != path:
            self.attribute_groups.append(path)
        self.attribute_plans.extend(plans)

    # ---- settling the documents (§5.6)

    def settle(self, doc: Doc) -> None:
        kept, spilling, spilled = 0, False, []

        def offer(entry: dict, cost: int) -> None:
            nonlocal kept, spilling
            if not spilling and kept + cost <= BUDGET:
                merge(doc.s, entry)
                kept += cost
            else:
                spilling = True
                spilled.append(entry)

        def room() -> int:
            # Spilled entries are data, not the document: their attributes are JSON unless large.
            return 1 << 62 if spilling else BUDGET - kept

        for e in doc.entries:
            kind = e[0]
            if kind == "type":
                part = self.type_part(e[1], e[2])
                offer(part, size(part))
            elif kind == "set":
                offer(e[1], size(e[1]))
            elif kind == "keyed":
                offer({e[1]: {e[2]: e[3]}}, size(e[2]) + size(e[3]))
            elif kind == "list":
                offer({e[1]: [e[2]]}, size(e[2]))
            elif kind == "attrs":
                if e[1] is None:
                    offer({"attributes": None}, size(None))
                for item in e[1] or []:
                    value, cost = self.attribute_entry(item, room())
                    offer({"attribute_collisions": [value]} if item[3] else {"attributes": {item[0]: value}}, cost)
            else:  # "member": an entry of images, datatypes or unsupported, with the member's attributes
                _, section, key, make, items = e
                value = make()
                cost = size(key) + size(value)
                if items is None:
                    value["attributes"] = None
                    cost += size(None)
                held: dict = {}
                collided: list = []
                for item in items or []:
                    v, c = self.attribute_entry(item, room() - cost)
                    cost += c
                    if item[3]:
                        collided.append(v)
                    else:
                        held[item[0]] = v
                if held:
                    value["attributes"] = held
                if collided:
                    value["attribute_collisions"] = collided
                offer({section: {key: value}}, cost)
        if spilled:
            path = f"spilled/{self.spills}"
            self.spills += 1
            doc.s["spilled"] = path
            self.attribute_family(path, bytes_family(path, [canonical(e).encode() for e in spilled]))

    def finish(self) -> None:
        """Settles every document, then builds the object table and the arrays of indexes into it."""
        for d in self.docs:
            self.settle(d)
        data = struct.pack(f"<{len(self.parents)}i", *self.parents)
        self.attribute_array("objects/parent", "int32", [len(self.parents)], 4, data, "little")
        self.attribute_family("objects/name", bytes_family("objects/name", self.names))
        for job in self.jobs:
            job[0](*job[1:])

    def index_array(self, path: str, refs: list, dims: list[int], s: dict | None, dataset: bool) -> None:
        """Object references as an int32 array of their indexes in the object table, -1 for none (§5.3)."""
        data = struct.pack(f"<{len(refs)}i", *[self.order.get(a, -1) if a is not None else -1 for a in refs])
        self.attribute_array(path, "int32", dims, 4, data, "little", s, self.plans if dataset else None)

    def region_family(self, path: str, regions: list, s: dict | None) -> None:
        texts = [canonical(self.region_value(r)).encode() for r in regions]
        self.family(path, bytes_family(path, texts), s)

    # ---- datasets (§5.4)

    def dataset(self, at: int, path: str) -> None:
        """A dataset's array, or its group: a family, a compound of several, or a null dataspace."""
        f = self.f
        if any(m.type == EXTERNAL_FILES for m in f.header(at)):
            raise Rejected("its data is in external files")
        ds = f.dataset(at, any_layout=True)
        t, _ = describe_type(ds.type_message)
        s: dict = {}
        doc = self.doc(s)
        doc.entries.append(("type", t, ds.named))
        attributes = ("attrs", self.attribute_items(at))
        if ds.dims is None:
            doc.entries.append(("set", {"shape": None}))
            if ds.fill is not None and not holds_addresses(t):
                doc.entries.append(("set", {"fill": json_base64(ds.fill)}))
            doc.entries.append(attributes)
            self.groups.append((path, s))
            return
        count = _product(ds.dims)
        if holds_addresses(t):
            kind = column(t)
            if kind is None and t["class"] != "compound":
                raise Rejected(VLEN_REASON if t["class"] == "variable-length"
                               else "a reference that is neither an object nor a region reference"
                               if t["class"] == "reference" else "a datatype that holds references or variable-length data")
            if count > MAX_ELEMENTS:
                raise Rejected(f"more than {MAX_ELEMENTS} elements that hold addresses")
            if kind is not None:
                if kind != "paths":
                    doc.entries.append(("set", {"shape": ds.dims}))
                doc.entries.append(attributes)
                n = t["size"]
                self.column(path, t, kind, self.stored(ds, n * count), n, 0, count, ds.dims, s)
                return
            self.compound(ds, t, path, count, s)
            doc.entries.append(attributes)
            return
        e = element(t)
        if e is not None:
            base, extra = e
            data_type, endian, item = numeric(base)[0], base["order"], base["size"]
        else:
            base, extra, data_type, endian, item = None, [t["size"]], "uint8", "little", 1
        fill = 0
        if ds.fill is not None:
            exact = exact_fill(base, ds.fill)
            if exact is None:
                doc.entries.append(("set", {"fill": json_base64(ds.fill)}))
            else:
                fill = exact
        doc.entries.append(attributes)
        shape = [*ds.dims, *extra]
        size = count * t["size"]
        compressor = None
        if ds.layout == "chunked":
            if ds.filters not in ([], [1], [3], [1, 3], *SHUFFLED):
                raise Rejected(f"unsupported HDF5 filters {ds.filters}")
            chunk_shape = [*ds.chunk, *extra]
            tail = (0,) * len(extra)
            nbytes = _product(ds.chunk) * t["size"]
            implicit = self.implicit(ds, shape, item, t["size"]) if ds.index == "implicit" else None
            if implicit is not None:
                chunk_shape, chunks = implicit
            else:
                masks: dict = {}
                found = sorted(f.chunks(ds, masks).items())
                if ds.filters in ([], [1], [3], [1, 3]) and not masks:
                    compressor = ZLIB if 1 in ds.filters else None
                    chunks = {}
                    for k, (where, n) in found:
                        n -= 4 if 3 in ds.filters else 0
                        if n < 1 or (1 not in ds.filters and n != nbytes):
                            raise Rejected(f"chunk {_coords(k)} of {n} bytes, not {nbytes}")
                        chunks[(*k, *tail)] = [(where, n)]
                else:
                    if len(found) * nbytes > MAX_COPY:
                        raise Rejected("data to decode of more than 2^24 bytes")
                    chunks = {(*k, *tail): self.decode_chunk(ds, self.read(where, n), nbytes, masks.get(k, 0))
                              for k, (where, n) in found}
        elif ds.layout == "contiguous":
            if ds.data_address is not None and ds.data_address + size > f.size:
                raise Rejected("data outside the file")
            if size == 0 or ds.data_address is None:
                chunk_shape, chunks = [max(1, v) for v in shape], {}
            else:
                chunk_shape, chunks = grid_chunks(ds.data_address, shape, item, self.read)
        else:
            if len(ds.compact) < size:
                raise Rejected("truncated compact dataset")
            chunk_shape = [max(1, v) for v in shape]
            chunks = {(0,) * len(shape): ds.compact[:size]} if size else {}
        self.plans.append(Plan(path, data_type, shape, chunk_shape, None, chunks, fill, s, endian, compressor))

    def implicit(self, ds: Dataset, shape: list[int], item: int, size: int) -> tuple | None:
        """(chunk shape, chunks) of an implicitly indexed dataset (§5.4) whose
        chunks are its elements in row-major order: cut as contiguous values;
        or whose chunk shape is 1 but along its last dimension: a chunk per row,
        its chunks' adjacent runs one range. None for any other."""
        f, rank = self.f, len(ds.dims)
        if ds.index_address is None or 0 in ds.dims:
            return None
        maxgrid = f.implicit(ds)
        nbytes = _product(ds.chunk) * size
        if rank == 0 or maxgrid[1:] == ds.grid[1:] and _row_major(ds):
            return grid_chunks(ds.index_address, shape, item, self.read)
        n = ds.dims[-1] * size
        if any(c != 1 for c in ds.chunk[:-1]) or ds.grid[-1] < 2 or n > MAX_CHUNK:
            return None
        chunks = {}
        tail = (0,) * (len(shape) - rank)
        for i in range(_product(ds.grid[:-1])):
            coords = _unravel(i, ds.grid[:-1])
            chunks[(*coords, 0, *tail)] = [(ds.index_address + _ravel([*coords, 0], maxgrid) * nbytes, n)]
        return [*([1] * (rank - 1)), ds.dims[-1], *shape[rank:]], chunks

    def column(self, path: str, t: dict, kind: str, data: bytes, stride: int, offset: int, count: int,
               dims: list[int], s: dict | None) -> None:
        """Elements that hold addresses as an array (§5.4): a family of their
        variable-length values, the indexes of the objects they refer to in the
        object table, or a family of region references. Element `i` of the
        `count` is the bytes of `data` at `i * stride + offset`, read in place
        (no copy of each is kept: memory per element is its result's)."""
        n = t["size"]
        elements = (data[i * stride + offset : i * stride + offset + n] for i in range(count))
        if kind == "family":
            members = [(i, *self.heap_object(t, e)) for i, e in enumerate(elements)]
            self.family(path, family_plans(path, members, self.read, count), s)
        elif kind == "paths":
            self.jobs.append((self.index_array, path, [ref_address(e) for e in elements], dims, s, True))
        else:
            self.jobs.append((self.region_family, path, [self.region(e) for e in elements], s))

    def compound(self, ds: Dataset, t: dict, path: str, count: int, s: dict) -> None:
        """A compound dataset with members that hold addresses (§5.4): the group of
        its bytes, those members' zeroed, and of each such member's array."""
        members = []
        for i, m in enumerate(t["members"]):
            if holds_addresses(m["type"]):
                kind = column(m["type"])
                if kind is None or m["offset"] + m["type"]["size"] > t["size"]:
                    raise Rejected("a compound member that holds addresses other than as variable-length data "
                                   "or a reference")
                members.append((i, m["offset"], m["type"], kind))
        n = t["size"]
        if count * n > MAX_COPY:
            raise Rejected("a compound of more than 2^24 bytes")
        data = bytearray(self.stored(ds, count * n))
        for i, o, mt, kind in members:
            self.column(f"{path}/{i}", mt, kind, data, n, o, count, ds.dims, None)
        for _, o, mt, _ in members:
            for k in range(count):
                data[k * n + o : k * n + o + mt["size"]] = bytes(mt["size"])
        self.attribute_array(f"{path}/data", "uint8", [*ds.dims, n], 1, bytes(data), "little", plans=self.plans)
        self.groups.append((path, s))

    def family(self, path: str, plans: list[Plan], s: dict | None) -> None:
        """A family (conventions §7) with its source metadata: on its 2-D array, or on its group."""
        if plans and plans[0].path == path:
            plans[0].attributes = s
        else:
            self.groups.append((path, s if s is not None else {}))
        self.plans.extend(plans)

    def decode_chunk(self, ds: Dataset, data: bytes, nbytes: int, mask: int) -> bytes:
        """A chunk's bytes, its filters undone: Fletcher32's checksum removed, deflate inflated
        (one zlib stream of the chunk's size) and the shuffle undone, but for the filters its
        filter mask skips."""
        if ds.filters not in ([], [1], [3], [1, 3], *SHUFFLED):
            raise Rejected(f"unsupported HDF5 filters {ds.filters}")
        filters = [f for i, f in enumerate(ds.filters) if not mask >> i & 1]
        if 3 in filters:
            data = data[:-4] if len(data) > 4 else b""
        if 1 in filters:
            z = zlib.decompressobj()
            try:
                data = z.decompress(data, nbytes + 1)
            except zlib.error:
                data = b""
            if not z.eof or z.unused_data:
                data = b""
        if len(data) != nbytes:
            raise Rejected("a chunk that does not decode to its size")
        return unshuffle(data, ds.datatype.size) if 2 in filters else data

    def stored(self, ds: Dataset, size: int) -> bytes:
        """The whole data of a dataset of `size` bytes, read and decoded."""
        f = self.f
        if ds.layout == "compact":
            if len(ds.compact) < size:
                raise Rejected("truncated compact dataset")
            return ds.compact[:size]
        if ds.layout == "contiguous":
            if ds.data_address is None:
                return bytes(size)
            if ds.data_address + size > f.size:
                raise Rejected("data outside the file")
            return self.read(ds.data_address, size)
        if ds.filters not in ([], [1], [3], [1, 3], *SHUFFLED):
            raise Rejected(f"unsupported HDF5 filters {ds.filters}")
        e = ds.datatype.size
        chunk_bytes = _product(ds.chunk) * e
        if chunk_bytes > MAX_COPY:
            raise Rejected("a chunk of more than 2^24 bytes to decode")
        out = bytearray(size)
        masks: dict = {}
        for coords, (at, n) in sorted(f.chunks(ds, masks).items()):
            data = self.decode_chunk(ds, self.read(at, n), chunk_bytes, masks.get(coords, 0))
            _scatter(out, ds.dims, e, data, ds.chunk, [c * k for c, k in zip(coords, ds.chunk)])
        return bytes(out)

    # ---- unsupported objects (§5.5)

    def unsupported(self, at: int, reason: str):
        """What can be read of an object that is not mapped: its datatype,
        shape, fill value, external files or virtual mappings, and attributes."""
        f = self.f
        record: dict = {"reason": reason}
        try:
            messages = f.header(at)
        except Rejected:
            return lambda: dict(record), []
        types = {m.type for m in messages}
        typed = None
        if LAYOUT in types or DATATYPE in types:
            try:
                type_message, named = f.datatype(messages)
                typed = describe_type(type_message)[0], named
            except Rejected:
                pass
        if LAYOUT in types:
            try:
                record["shape"] = parse_dataspace(f.message(messages, DATASPACE)).dims
            except Rejected:
                pass
            try:
                message = f.message(messages, FILL_VALUE, False)
                fill = fill_value(message) if message is not None else b""
                if fill and typed is not None and not holds_addresses(typed[0]):
                    record["fill"] = json_base64(fill)
            except Rejected:
                pass
            try:
                efl = f.message(messages, EXTERNAL_FILES, False)
                if efl is not None:
                    record["external"] = self.external_files(efl)
            except Rejected:
                pass
            try:
                layout = f.message(messages, LAYOUT)
                if le(layout, 1, 1) == 3:
                    record["virtual"] = f.virtual_mapping(layout)
            except Rejected:
                pass

        def make() -> dict:
            out = {"reason": reason}
            if typed is not None:
                out.update(self.type_part(*typed))
            out.update({k: v for k, v in record.items() if k != "reason"})
            return out

        return make, self.attribute_items(at)

    def external_files(self, d: bytes) -> list[dict]:
        """The files of an external data files message (§8.9)."""
        if le(d, 0, 1) != 1:
            raise Rejected("unsupported external data files message version")
        used, heap = le(d, 6, 2), address(le(d, 8, 8))
        if heap is None:
            raise Rejected("external data files without a local heap")
        names = self.f._local_heap(heap)
        out = []
        for i in range(used):
            p = 16 + 24 * i
            offset = le(d, p, 8)
            end = names.find(b"\0", offset) if offset < len(names) else -1
            if end < 0:
                raise Rejected("an external file name outside the local heap")
            out.append({"file": text_json(names[offset:end]), "offset": json_number(le(d, p + 8, 8)),
                        "size": json_number(le(d, p + 16, 8))})
        return out

    # ---- groups (§5.1)

    def kind(self, at: int) -> str:
        types = {m.type for m in self.f.header(at)}
        if LAYOUT in types:
            return "dataset"
        if SYMBOL_TABLE in types or LINK_INFO in types:
            return "group"
        if DATATYPE in types:
            return "datatype"
        raise Rejected("an object that is not a group, a dataset or a named datatype")

    def walk(self) -> None:
        f = self.f
        self.meet(f.root, "/", None, "")
        self.objects = 1  # the root group is the first object
        stack = [(f.root, "/", "hdf5")]
        while stack:
            at, path, zpath = stack.pop()
            s: dict = {}
            doc = self.doc(s)
            doc.entries.append(("attrs", self.attribute_items(at)))
            links = f.links(at)
            keys: set[str] = set()
            overflow = 0
            children: list = []
            for name in sorted(links):
                key = decode_text(name)
                if key in keys:
                    doc.entries.append(("list", "collisions", text_json(name)))  # reads like an earlier name
                    continue
                keys.add(key)
                if not isinstance(text_json(name), str):
                    doc.entries.append(("keyed", "names", key, text_json(name)))
                target = links[name]
                child, zchild = (path if path != "/" else "") + "/" + key, f"{zpath}/{key}"
                if isinstance(target, bytes):
                    doc.entries.append(("keyed", "links", key, {"soft": text_json(target)}))
                elif isinstance(target, tuple):
                    doc.entries.append(("keyed", "links", key, other_link(*target)))
                elif target in self.seen:
                    doc.entries.append(("keyed", "links", key, {"hard": self.order[target]}))
                elif self.objects >= MAX_OBJECTS + PER_IMAGE * len(self.images):
                    overflow += 1
                else:
                    self.objects += 1
                    self.meet(target, child, at, key)
                    try:
                        self.member(target, child, zchild, key, doc, children)
                    except Rejected as e:
                        doc.entries.append(("member", "unsupported", key, *self.unsupported(target, str(e))))
            if overflow:
                s["overflow"] = overflow
            self.groups.append((zpath, s))
            stack.extend(reversed(children))
        self.finish()

    def member(self, at: int, path: str, zpath: str, key: str, doc: Doc, children: list) -> None:
        """A group's member: a named datatype, an image dataset, or (named as a
        Zarr node) a group (walked later) or a dataset."""
        kind = self.kind(at)
        if kind == "datatype":
            t = describe_type(self.f.message(self.f.header(at), DATATYPE))[0]
            doc.entries.append(("member", "datatypes", key, lambda: {"datatype": t}, self.attribute_items(at)))
            return
        if at in self.images:
            image = self.images[at]
            doc.entries.append(("member", "images", key, lambda: dict(image), self.attribute_items(at)))
            return
        if not node_name(key):
            raise Rejected("the name is not a Zarr node name")
        if kind == "group":
            self.f.links(at)
            children.append((at, path, zpath))
            return
        mark = (len(self.plans), len(self.groups), len(self.docs), len(self.jobs), len(self.attribute_plans),
                len(self.attribute_groups), self.selections)
        try:
            self.dataset(at, zpath)
        except Rejected:
            # Undo what the dataset that failed added, and the selections it read.
            del self.plans[mark[0]:], self.groups[mark[1]:], self.docs[mark[2]:], self.jobs[mark[3]:]
            del self.attribute_plans[mark[4]:], self.attribute_groups[mark[5]:]
            self.selections = mark[6]
            raise

    def emit(self, out: Output) -> None:
        metadata_group(out, SOURCE_NODE)
        for path, s in self.groups:
            metadata_group(out, f"{SOURCE_NODE}/{path}", declare({}, "ims", None, s) if s else None)
        if self.arrays:
            metadata_group(out, f"{SOURCE_NODE}/attributes")
        if self.spills:
            metadata_group(out, f"{SOURCE_NODE}/spilled")
        metadata_group(out, f"{SOURCE_NODE}/objects")
        for path in self.attribute_groups:
            metadata_group(out, f"{SOURCE_NODE}/{path}")
        for a in [*self.plans, *self.attribute_plans]:
            metadata_array(out, f"{SOURCE_NODE}/{a.path}", a.data_type, a.shape, a.chunk_shape, a.dims, a.chunks,
                           a.fill, declare({}, "ims", None, a.attributes) if a.attributes else None, a.endian,
                           a.compressor)


SHUFFLED = ([2], [2, 1], [2, 3], [2, 1, 3])  # pipelines with shuffle, decoded and copied (§5.4)


def other_link(kind: int, value: bytes | None) -> dict:
    """A link of a type other than hard or soft (§5.1): an external link's
    file and path, else the type and its value in base64 (null if unread)."""
    if value is None:
        return {"user": {"type": kind, "value": None}}
    if kind == 64 and value[:1] == b"\0":
        file_end = value.find(b"\0", 1)
        path_end = value.find(b"\0", file_end + 1) if file_end > 0 else -1
        if path_end == len(value) - 1:
            file, path = value[1:file_end], value[file_end + 1 : path_end]
            return {"external": {"file": text_json(file), "path": text_json(path)}}
    return {"user": {"type": kind, "value": json_base64(value)}}


def bytes_family(path: str, values: list[bytes]) -> list[Plan]:
    """A family of byte values held in memory (conventions §7), each chunk copied."""
    count = len(values)
    lengths = {len(v) for v in values}
    if len(lengths) == 1 and lengths != {0} and max(lengths) <= MAX_COPY:
        (length,) = lengths
        return [Plan(path, "uint8", [count, length], [1, length], ["index", "byte"],
                     {(i, 0): v for i, v in enumerate(values)})]
    starts, total = [], 0
    for v in values:
        starts.append(total)
        total += len(v)
    starts.append(total)
    plans = [Plan(f"{path}/offsets", "int64", [count + 1], [count + 1], ["index"],
                  {(0,): struct.pack(f"<{count + 1}q", *starts)})]
    if total:
        blob = b"".join(values)
        size = -(-total // -(-total // RAGGED_CHUNK))  # balanced: ceil(total / 2^20) chunks
        chunks = {}
        for c in range(-(-total // size)):
            part = blob[c * size : (c + 1) * size]
            chunks[(c,)] = part + bytes(size - len(part))
        plans.append(Plan(f"{path}/data", "uint8", [total], [size], ["byte"], chunks))
    return plans


def _coords(values) -> str:
    """Coordinates as reasons give them: `[1, 2]`."""
    return "[" + ", ".join(str(v) for v in values) + "]"


def _unravel(i: int, grid: list[int]) -> list[int]:
    """The coordinates of index `i` in row-major order over `grid`."""
    coords = []
    for g in reversed(grid):
        coords.append(i % g)
        i //= g
    return coords[::-1]


def _ravel(coords, grid) -> int:
    i = 0
    for c, g in zip(coords, grid):
        i = i * g + c
    return i


def _row_major(ds: Dataset) -> bool:
    """Whether chunks of `ds` in row-major order hold its elements in row-major
    order: a chunk shape of 1 before some dimension `a` and of the whole
    dimension after it, and `a`'s size a multiple of its chunk shape unless the
    dimensions before it are all 1 (the maximum grid aside, §5.4)."""
    rank = len(ds.dims)
    a = max((i for i in range(rank) if ds.chunk[i] != ds.dims[i]), default=0)
    if any(c != 1 for c in ds.chunk[:a]):
        return False
    return ds.dims[a] % ds.chunk[a] == 0 or _product(ds.dims[:a]) == 1


def _scatter(out: bytearray, dims: list[int], e: int, chunk: bytes, chunk_shape: list[int], origin: list[int]) -> None:
    """Copies a chunk into the C-order array `out`, clipped to `dims`."""
    rank = len(dims)
    if rank == 0:
        out[:e] = chunk[:e]
        return
    run = min(chunk_shape[-1], dims[-1] - origin[-1]) * e
    index = [0] * (rank - 1)
    while True:
        if all(origin[i] + index[i] < dims[i] for i in range(rank - 1)):
            src = dst = 0
            for i in range(rank - 1):
                src = src * chunk_shape[i] + index[i]
                dst = dst * dims[i] + origin[i] + index[i]
            src = src * chunk_shape[-1] * e
            dst = (dst * dims[-1] + origin[-1]) * e
            out[dst : dst + run] = chunk[src : src + run]
        i = rank - 2
        while i >= 0:
            index[i] += 1
            if index[i] < chunk_shape[i]:
                break
            index[i] = 0
            i -= 1
        if i < 0:
            return


def source_tree(f: Hdf5, read: Reader, images: dict[int, dict]) -> Tree:
    tree = Tree(f, read, images)
    tree.walk()
    return tree
