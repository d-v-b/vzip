"""The intermediate representation (design/ARCHITECTURE.md §3): a stream of element
records with parent ids, kept as columns, and the one budget of a run.

Kinds (design/ARCHITECTURE.md §3.2):

- `struct`: a container; its extent, if any, is an envelope, not a claim;
- `value`: a field, with its extents, its declared type (vzip.ir.types) and,
  when read, its decoded value;
- `data`: a decodable unit, with geometry, codec and a recipe of source ranges,
  literals and shared data sources; its extents are the bytes it claims;
- `derived`: content that exists after a transform of its extents;
- `alias`: a second name for something already described, with a `target`
  (an earlier element). An alias claims nothing. It keeps the fields of what it
  names (its type, value, geometry, recipe) and its own extents, since two names
  of the same bytes may decode differently or overlap only in part;
- `gap`: bytes no element claims.

The leaves (`value`, `data`, `derived`, `gap`) partition [0, size): `finish()`
makes every claim of bytes already claimed an alias of their first claimant
(ordered by offset, then by emission), and fills what is left with gaps.
"""

from __future__ import annotations

import sys
from array import array
from dataclasses import dataclass

from vzip.ir.types import parse as parse_type
from vzip.virtualize.common import Rejected

KINDS = ("struct", "value", "data", "derived", "alias", "gap")
STRUCT, VALUE, DATA, DERIVED, ALIAS, GAP = range(6)
LEAVES = (VALUE, DATA, DERIVED, GAP)
MAX_DECODE = 1 << 16  # values up to this many bytes are decoded when the parser has them


class Budget:
    """One budget per run (design/ARCHITECTURE.md §3.5): what a run may spend. Every
    limit is a resource limit, the same for every format; exceeding one rejects."""

    def __init__(self, size: int) -> None:
        self.limits = {
            "records": (1 << 20) + size // 8,  # IR records: one per 8 bytes of the source, at most
            "read": (1 << 24) + 2 * size,  # bytes the parser reads
            "reads": (1 << 20) + size // 64,  # requests to the source
            "refs": (1 << 22) + 4 * size,  # output references
            "repeats": 1 << 20,  # references to bytes already referenced (through an alias)
            "documents": 1 << 24,  # bytes of the mirror's documents
        }
        self.spent = dict.fromkeys(self.limits, 0)

    def charge(self, what: str, n: int = 1) -> None:
        self.spent[what] += n
        if self.spent[what] > self.limits[what]:
            raise Rejected(f"budget: more than {self.limits[what]} {what}")

    def left(self, what: str) -> int:
        return self.limits[what] - self.spent[what]


WINDOW = 1 << 16  # a sequential reader reads ahead this far
NEAR = 1 << 9  # a read that starts this close after the last one is sequential


def budgeted(read, budget: Budget, size: int | None = None):
    """The parser's reader: every request to the source is charged (calls and bytes).
    While the parser says it walks (`r.ahead = True`), a read that starts just after
    the previous one is served from one window read ahead, so a walk over small
    segments costs one request per 64 KiB; the window is the only buffer, so no
    byte is held past the next read. Otherwise each read is exact."""
    window = [0, b""]

    def fetch(offset: int, length: int) -> bytes:
        budget.charge("reads")
        budget.charge("read", length)
        return read(offset, length)

    def r(offset: int, length: int) -> bytes:
        start, buf = window
        if start <= offset and offset + length <= start + len(buf):
            return buf[offset - start:offset - start + length]
        end = start + len(buf)
        if r.ahead and length < WINDOW and end <= offset <= end + NEAR and offset + WINDOW <= (size or 0):
            window[:] = [offset, fetch(offset, WINDOW)]
            return window[1][:length]
        data = fetch(offset, length)
        window[:] = [offset, data if length <= WINDOW else b""]
        return data

    r.ahead = False
    if hasattr(read, "prefetch"):  # a planner's reader: batches go straight to it
        def prefetch(ranges):
            budget.charge("reads", len(ranges))
            budget.charge("read", sum(n for _, n in ranges))
            return read.prefetch(ranges)

        r.prefetch, r.release = prefetch, read.release
    return r


class Values:
    """Decoded values, kept as their bytes and decoded when asked for (a few recent
    ones cached): a record costs its bytes, not a Python object tree."""

    def __init__(self, types: list) -> None:
        self.types, self.raw, self.cache = types, {}, {}

    def put(self, i: int, raw: bytes) -> None:
        self.raw[i] = bytes(raw)

    def __contains__(self, i) -> bool:
        return i in self.raw

    def __getitem__(self, i: int):
        if i not in self.cache:
            if len(self.cache) >= 1024:
                self.cache.pop(next(iter(self.cache)))
            self.cache[i] = parse_type(self.types[i]).decode(self.raw[i])
        return self.cache[i]

    def get(self, i, default=None):
        return self[i] if i is not None and i in self.raw else default

    def items(self):
        return ((i, self[i]) for i in self.raw)

    def __len__(self) -> int:
        return len(self.raw)


@dataclass
class Element:
    """One record, as read back from the columns."""
    id: int
    kind: str
    parent: int
    name: str
    extents: list[tuple[int, int]]
    type: str | None = None
    value: object = None
    info: dict | None = None
    target: int = -1


class IR:
    """The records of one source, emitted in order (a parent before its children)."""

    def __init__(self, size: int, budget: Budget | None = None) -> None:
        self.size = size
        self.budget = budget or Budget(size)
        self.kind = array("B")
        self.parent = array("q")
        self.target = array("q")
        self.ext_index = array("q", [0])  # element i's extents are ext[2*ext_index[i] : 2*ext_index[i+1]]
        self.ext = array("q")
        self.name: list[str] = []
        self.type: list[str | None] = []
        self.values = Values(self.type)
        self.info: dict[int, dict] = {}  # data, derived (and their aliases): geometry, codec, recipe
        self.was: dict[int, int] = {}  # an alias made by finish(): the kind it had
        self.shared: list[bytes] = []
        self._shared: dict[bytes, int] = {}
        self.kids: dict[int, list[int]] = {}
        self.by_name: dict[int, dict[str, int]] = {}  # built for a parent when first asked
        self.finished = False
        self.root = self.struct(-1, "", [(0, size)])

    # ---- emission

    def _add(self, kind: int, parent: int, name: str, extents, type_=None, target: int = -1) -> int:
        if self.finished:
            raise AssertionError("the IR is finished")
        self.budget.charge("records")
        i = len(self.kind)
        self.kind.append(kind)
        self.parent.append(parent)
        self.target.append(target)
        self.name.append(name)
        self.type.append(sys.intern(type_) if type_ else None)
        for o, n in extents:
            if n == 0 and kind != STRUCT:
                continue  # an empty extent claims nothing
            self.ext.append(o)
            self.ext.append(n)
        self.ext_index.append(len(self.ext) // 2)
        if parent >= 0:
            self.kids.setdefault(parent, []).append(i)
            if parent in self.by_name:
                self.by_name[parent].setdefault(name, i)
        return i

    def struct(self, parent: int, name: str, envelope=()) -> int:
        return self._add(STRUCT, parent, name, envelope)

    def value(self, parent: int, name: str, type_: str, extents, raw: bytes | None = None) -> int:
        """A value; it is decoded when its bytes `raw` are given."""
        t = parse_type(type_)
        if sum(n for _, n in extents) != t.size:
            raise AssertionError(f"{name}: extents of {sum(n for _, n in extents)} bytes for {type_}")
        i = self._add(VALUE, parent, name, extents, type_)
        if raw is not None:
            if len(raw) != t.size:
                raise ValueError(f"{name}: {len(raw)} bytes for {type_}")
            self.values.put(i, raw)
        return i

    def data(self, parent: int, name: str, extents, geometry: dict | None, codec: list | None, recipe: list) -> int:
        """A decodable unit. `recipe` parts: ("src", offset, length), ("lit", bytes), ("shared", k)."""
        i = self._add(DATA, parent, name, extents)
        self.info[i] = {"geometry": geometry, "codec": codec, "recipe": recipe}
        return i

    def derived(self, parent: int, name: str, transform: str, extents) -> int:
        i = self._add(DERIVED, parent, name, extents)
        self.info[i] = {"transform": transform}
        return i

    def alias(self, parent: int, name: str, target: int) -> int:
        return self._add(ALIAS, parent, name, (), target=target)

    def gap(self, parent: int, name: str, extents) -> int:
        return self._add(GAP, parent, name, [(o, n) for o, n in extents if n > 0])

    def share(self, b: bytes) -> int:
        """A shared data source, for recipes."""
        b = bytes(b)
        if b not in self._shared:
            self._shared[b] = len(self.shared)
            self.shared.append(b)
        return self._shared[b]

    # ---- reading

    def __len__(self) -> int:
        return len(self.kind)

    def extents(self, i: int) -> list[tuple[int, int]]:
        a, b = self.ext_index[i], self.ext_index[i + 1]
        return [(self.ext[2 * k], self.ext[2 * k + 1]) for k in range(a, b)]

    def element(self, i: int) -> Element:
        return Element(i, KINDS[self.kind[i]], self.parent[i], self.name[i], self.extents(i), self.type[i],
                       self.values.get(i), self.info.get(i), self.target[i])

    def child(self, parent: int | None, name: str) -> int | None:
        if parent is None:
            return None
        if parent not in self.by_name:
            index: dict[str, int] = {}
            for k in self.kids.get(parent, []):
                index.setdefault(self.name[k], k)
            self.by_name[parent] = index
        return self.by_name[parent].get(name)

    def children(self, parent: int) -> list[int]:
        return self.kids.get(parent, [])

    def get(self, parent: int | None, name: str, default=None):
        """The decoded value of a value child (or of what an alias child names)."""
        i = self.child(parent, name)
        return self.values.get(i, default) if i is not None else default

    def resolve(self, i: int) -> int:
        """What an alias names, followed to an element that is not an alias."""
        while self.kind[i] == ALIAS and self.target[i] >= 0:
            i = self.target[i]
        return i

    def path(self, i: int) -> str:
        parts = []
        while 0 < i < len(self.name) and len(parts) < 64:
            parts.append(self.name[i])
            i = self.parent[i]
        return "/".join(reversed(parts))

    def claims(self, i: int) -> bool:
        return self.kind[i] in LEAVES

    # ---- the invariants

    def spans(self):
        """The leaves' extents sorted by (offset, element): three lists (offsets, ends, ids)."""
        import numpy as np

        counts = np.diff(np.frombuffer(self.ext_index, np.int64))
        ids = np.repeat(np.arange(len(self.kind), dtype=np.int64), counts)
        ext = np.frombuffer(self.ext, np.int64).reshape(-1, 2)
        kinds = np.frombuffer(self.kind, np.uint8)[ids]
        keep = np.isin(kinds, LEAVES)
        ids, o, n = ids[keep], ext[keep, 0], ext[keep, 1]
        order = np.lexsort((ids, o))
        return o[order].tolist(), (o + n)[order].tolist(), ids[order].tolist()

    def finish(self) -> "IR":
        """Injectivity, then coverage: claims of claimed bytes become aliases of
        their first claimant, and the bytes nobody claims become gaps."""
        multi = {i for i in range(len(self.kind)) if self.ext_index[i + 1] - self.ext_index[i] > 1}
        while True:
            starts, ends, ids = self.spans()
            end, owner, again = 0, -1, False
            for o, e, i in zip(starts, ends, ids):
                if self.kind[i] == ALIAS or o == e:
                    continue
                if o < end:
                    self.was[i] = self.kind[i]
                    self.kind[i] = ALIAS
                    self.target[i] = owner
                    again = again or i in multi  # one of its extents may have been accepted: sweep again
                elif e > end:
                    end, owner = e, i
            if not again:
                break
        pos = 0
        for o, e, i in zip(starts, ends, ids):
            if self.kind[i] == ALIAS:
                continue
            if o > pos:
                self.gap(self.root, f"gaps/{pos}", [(pos, o - pos)])
            pos = max(pos, e)
        if pos < self.size:
            self.gap(self.root, f"gaps/{pos}", [(pos, self.size - pos)])
        self.finished = True
        return self
