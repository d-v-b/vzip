"""Reading the structure of an HDF5 file (§8.1–§8.6): the superblock, object
headers and their messages, the links of groups, attributes, and the chunk
index of a chunked dataset. Pixel data is never read, and checksums are not
checked."""

from __future__ import annotations

from dataclasses import dataclass

from vzip.virtualize.common import Reader, Rejected, json_number, text_json

SIGNATURE = b"\x89HDF\r\n\x1a\n"
UNDEFINED = 2**64 - 1
MAX_SAFE = 2**53 - 1
MAX_BLOCKS = 1000  # blocks of one object header (§8.2)
MAX_HEADERS = 4096  # object headers held in memory at once
MAX_BTREE2_DEPTH = 16  # §8.4
# The record size of each type of version 2 B-tree read: huge fractal heap
# objects, link names and attribute names (§8.4); chunk records (types 10 and
# 11) have the size the dataset gives them (§8.5).
RECORD_SIZES = {1: 24, 5: 11, 8: 17}

# Message types (§8.2).
DATASPACE, LINK_INFO, DATATYPE, FILL_VALUE, LINK, LAYOUT = 0x01, 0x02, 0x03, 0x05, 0x06, 0x08
FILTERS, ATTRIBUTE, CONTINUATION, SYMBOL_TABLE, ATTRIBUTE_INFO = 0x0B, 0x0C, 0x10, 0x11, 0x15
EXTERNAL_FILES = 0x07


def le(data: bytes, at: int, n: int) -> int:
    """The little-endian unsigned integer of `n` bytes at `at`, which MUST lie within `data`."""
    if at < 0 or at + n > len(data):
        raise Rejected("truncated HDF5 structure")
    return int.from_bytes(data[at : at + n], "little")


def address(v: int) -> int | None:
    """An address: None if undefined (all bits set), else at most 2^53 - 1."""
    if v == UNDEFINED:
        return None
    if v > MAX_SAFE:
        raise Rejected(f"HDF5 address {v} is too large")
    return v


def length(v: int) -> int:
    if v > MAX_SAFE:
        raise Rejected(f"HDF5 length {v} is too large")
    return v


def log2(v: int) -> int:
    """floor(log2(v)) for v >= 1."""
    return v.bit_length() - 1


# A link's target: an object header address (a hard link), a path (a soft
# link), or (type, value) for any other type of link (§8.9).
Target = int | bytes | tuple[int, bytes | None]


@dataclass
class Message:
    type: int
    flags: int
    data: bytes
    offset: int = 0  # where the file holds the data


@dataclass
class Datatype:
    cls: int  # 0 fixed-point, 1 floating-point, 3 string, ...
    size: int
    bits: int  # the class bit fields
    props: bytes


@dataclass
class Dataspace:
    dims: list[int] | None  # None for a null dataspace; [] for a scalar
    maxdims: list[int] | None


@dataclass
class Dataset:
    dims: list[int] | None  # None for a null dataspace (source metadata only)
    maxdims: list[int] | None
    datatype: Datatype
    chunk: list[int]  # chunk dimensions, without the element size
    grid: list[int]  # chunks per dimension
    filters: list[int]  # filter identifiers, in pipeline order
    index: str  # "btree1", "single", "implicit", "farray", "earray" or "btree2"
    index_address: int | None
    single_size: int | None  # single chunk index: the filtered chunk's size
    layout: str = "chunked"  # "compact", "contiguous" or "chunked"
    compact: bytes = b""  # a compact dataset's data
    data_address: int | None = None  # a contiguous dataset's data, or None if unallocated
    fill: bytes | None = None  # the fill value, or None for the default (zero bytes)
    type_message: bytes = b""  # the datatype message
    named: int | None = None  # the committed datatype's object header, for a shared datatype
    single_mask: int = 0  # single chunk index: the filtered chunk's filter mask
    raw_edges: bool = False  # partial edge chunks are stored unfiltered (layout flag 1, §8.5)


def parse_datatype(d: bytes) -> Datatype:
    cv = le(d, 0, 1)
    if not 1 <= cv >> 4 <= 5:
        raise Rejected(f"unsupported datatype message version {cv >> 4}")
    return Datatype(cv & 15, le(d, 4, 4), le(d, 1, 3), d[8:])


def parse_dataspace(d: bytes) -> Dataspace:
    version, rank, flags = le(d, 0, 1), le(d, 1, 1), le(d, 2, 1)
    if version == 1:
        kind, p = (1 if rank else 0), 8
    elif version == 2:
        kind, p = le(d, 3, 1), 4
        if kind > 2 or (kind != 1 and rank != 0):
            raise Rejected(f"invalid dataspace type {kind}")
    else:
        raise Rejected(f"unsupported dataspace message version {version}")
    dims = [length(le(d, p + 8 * i, 8)) for i in range(rank)]
    maxdims = [le(d, p + 8 * (rank + i), 8) for i in range(rank)] if flags & 1 else None
    return Dataspace(None if kind == 2 else dims, maxdims)


class Hdf5:
    """An HDF5 file read through `read` (§8.1)."""

    def __init__(self, read: Reader, size: int) -> None:
        self.read = read
        self.size = size
        self._headers: dict[int, list[Message]] = {}
        self._heaps: dict[int, FractalHeap] = {}
        self._collections: dict[int, dict[int, tuple[int, int]]] = {}
        head = read(0, 9)
        if head[:8] != SIGNATURE:
            raise Rejected("not an HDF5 file")
        version = head[8]
        if version in (0, 1):
            sb = read(0, 76 if version == 1 else 72)
            base_at = 28 if version == 1 else 24
            sizes = (sb[13], sb[14])
            root_at = base_at + 40
        elif version in (2, 3):
            sb = read(0, 48)
            base_at, sizes, root_at = 12, (sb[9], sb[10]), 36
        else:
            raise Rejected(f"unsupported HDF5 superblock version {version}")
        if sizes != (8, 8):
            raise Rejected(f"HDF5 offsets and lengths of {sizes[0]} and {sizes[1]} bytes are not supported")
        if le(sb, base_at, 8) != 0:
            raise Rejected("HDF5 base address is not 0")
        root = address(le(sb, root_at, 8))
        if root is None:
            raise Rejected("no HDF5 root group")
        self.root = root

    # ---- object headers (§8.2)

    def header(self, at: int) -> list[Message]:
        if at not in self._headers:
            if len(self._headers) >= MAX_HEADERS:
                self._headers.clear()  # a cache: memory bounded, whatever the number of objects
            self._headers[at] = self._read_header(at)
        return self._headers[at]

    def _read_header(self, at: int) -> list[Message]:
        read = self.read
        if read(at, 4) == b"OHDR":
            prefix = read(at, 6)
            if prefix[4] != 2:
                raise Rejected(f"unsupported object header version {prefix[4]}")
            flags = prefix[5]
            p = at + 6 + (16 if flags & 0x20 else 0) + (4 if flags & 0x10 else 0)
            n = 1 << (flags & 3)
            first = le(read(p, n), 0, n)
            read(p + n, first + 4)  # the block and its checksum
            blocks = [(p + n, first)]
            v2, message_header = True, 6 if flags & 4 else 4
        else:
            prefix = read(at, 16)
            if prefix[0] != 1:
                raise Rejected(f"no object header at {at}")
            blocks = [(at + 16, le(prefix, 8, 4))]
            v2, message_header = False, 8
        messages: list[Message] = []
        seen = {blocks[0][0]}
        i = 0
        while i < len(blocks):
            start, n = blocks[i]
            i += 1
            data = read(start, n)
            pos = 0
            while pos < n:
                if n - pos < message_header:
                    if v2:
                        break  # a gap
                    raise Rejected("truncated object header message")
                if v2:
                    typ, size, mflags = data[pos], le(data, pos + 1, 2), data[pos + 3]
                else:
                    typ, size, mflags = le(data, pos, 2), le(data, pos + 2, 2), data[pos + 4]
                body = pos + message_header
                if body + size > n:
                    raise Rejected("object header message runs past its block")
                pos = body + size
                if typ != CONTINUATION:
                    messages.append(Message(typ, mflags, data[body:pos], start + body))
                    continue
                if mflags & 2:
                    raise Rejected("shared continuation message")
                to, ln = address(le(data[body:pos], 0, 8)), length(le(data[body:pos], 8, 8))
                if to is None:
                    raise Rejected("continuation to an undefined address")
                if v2:
                    if ln < 8 or read(to, 4) != b"OCHK":
                        raise Rejected(f"no continuation block at {to}")
                    read(to, ln)
                    block = (to + 4, ln - 8)
                else:
                    block = (to, ln)
                if block[0] in seen:
                    raise Rejected(f"object header block at {to} read twice")
                if len(blocks) >= MAX_BLOCKS:
                    raise Rejected("too many object header blocks")
                seen.add(block[0])
                blocks.append(block)
        return messages

    @staticmethod
    def message(messages: list[Message], typ: int, required: bool = True) -> bytes | None:
        """The data of the one message of type `typ` (§8.2)."""
        found = [m for m in messages if m.type == typ]
        if len(found) > 1:
            raise Rejected(f"object header has {len(found)} messages of type {typ}")
        if not found:
            if required:
                raise Rejected(f"object header has no message of type {typ}")
            return None
        if found[0].flags & 2:
            raise Rejected(f"shared message of type {typ}")
        return found[0].data

    def committed(self, shared: bytes) -> tuple[bytes, int]:
        """The datatype message of the committed datatype that a shared
        datatype message leads to, and its object header (§8.9)."""
        version = le(shared, 0, 1)
        if version == 1:
            at = address(le(shared, 8, 8))
        elif version == 2 or (version == 3 and le(shared, 1, 1) == 2):
            at = address(le(shared, 2, 8))
        else:
            raise Rejected("a datatype shared other than as a committed datatype")
        if at is None:
            raise Rejected("a shared datatype at an undefined address")
        return self.message(self.header(at), DATATYPE), at

    def datatype(self, messages: list[Message]) -> tuple[bytes, int | None]:
        """The datatype message of an object header, through a committed datatype
        when it is shared, and that datatype's object header (or None) (§8.9)."""
        found = [m for m in messages if m.type == DATATYPE]
        if len(found) != 1:
            raise Rejected(f"object header has {len(found)} messages of type {DATATYPE}")
        if found[0].flags & 2:
            return self.committed(found[0].data)
        return found[0].data, None

    # ---- groups (§8.3)

    def links(self, at: int) -> dict[bytes, Target]:
        """A group's links: name -> target."""
        messages = self.header(at)
        table = self.message(messages, SYMBOL_TABLE, False)
        info = self.message(messages, LINK_INFO, False)
        if table is not None and info is not None:
            raise Rejected("group has both a symbol table and link info")
        links: dict[bytes, Target] = {}

        def add(name: bytes, target: Target) -> None:
            if name in links:
                raise Rejected(f"group has two links named {name!r}")
            links[name] = target

        if table is not None:
            btree, heap = address(le(table, 0, 8)), address(le(table, 8, 8))
            if btree is None or heap is None:
                raise Rejected("symbol table with an undefined address")
            names = self._local_heap(heap)
            for snod in self._btree1(btree, 0, 8, set(), None):
                head = self.read(snod, 8)
                if head[:4] != b"SNOD" or head[4] != 1:
                    raise Rejected(f"no symbol table node at {snod}")
                count = le(head, 6, 2)
                entries = self.read(snod + 8, 40 * count)
                for k in range(count):
                    offset = le(entries, 40 * k, 8)
                    end = names.find(b"\0", offset) if offset < len(names) else -1
                    if end < 0:
                        raise Rejected("link name outside the local heap")
                    target = address(le(entries, 40 * k + 8, 8))
                    if target is None:
                        raise Rejected("symbol table entry with an undefined address")
                    add(names[offset:end], target)
        elif info is not None:
            if le(info, 0, 1) != 0:
                raise Rejected("unsupported link info message version")
            p = 2 + (8 if le(info, 1, 1) & 1 else 0)
            heap, btree = address(le(info, p, 8)), address(le(info, p + 8, 8))
            if heap is None:
                for m in messages:
                    if m.type == LINK:
                        if m.flags & 2:
                            raise Rejected("shared link message")
                        add(*parse_link(m.data))
            else:
                if btree is None:
                    raise Rejected("dense links without a name index")
                fh = self.fractal_heap(heap)
                for record in self.btree2(btree, 5):
                    add(*parse_link(fh.get(record[4:])))
        else:
            raise Rejected(f"object at {at} is not a group")
        return links

    def follow(self, links: dict[bytes, Target], name: bytes) -> int:
        """The object header that the link `name` of a group leads to (§8.3)."""
        target = links[name]
        if isinstance(target, bytes):
            if not target.startswith(b"/"):
                raise Rejected(f"soft link {name!r} to a relative path")
            at = self.root
            for part in target[1:].split(b"/") if target != b"/" else []:
                step = self.links(at).get(part, b"")
                if not isinstance(step, int):
                    raise Rejected(f"soft link {name!r} to {target!r} does not resolve through hard links")
                at = step
            return at
        if not isinstance(target, int):
            raise Rejected(f"link {name!r} is neither hard nor soft")
        return target

    def _local_heap(self, at: int) -> bytes:
        head = self.read(at, 32)
        if head[:4] != b"HEAP" or head[4] != 0:
            raise Rejected(f"no local heap at {at}")
        data = address(le(head, 24, 8))
        if data is None:
            raise Rejected("local heap without a data segment")
        return self.read(data, length(le(head, 8, 8)))

    def _btree1(self, at: int, node_type: int, key_size: int, seen: set[int], level: int | None):
        """The leaves' children of the version 1 B-tree at `at`: addresses
        (type 0, groups), or (key, address) pairs (type 1, chunks) (§8.3, §8.5)."""
        if at in seen:
            raise Rejected(f"B-tree node at {at} read twice")
        seen.add(at)
        head = self.read(at, 24)
        if head[:4] != b"TREE" or head[4] != node_type:
            raise Rejected(f"no B-tree node of type {node_type} at {at}")
        node_level, entries = head[5], le(head, 6, 2)
        if level is not None and node_level != level:
            raise Rejected(f"B-tree node at {at} has level {node_level}, not {level}")
        step = key_size + 8
        body = self.read(at + 24, entries * step + key_size)
        for i in range(entries):
            child = address(le(body, i * step + key_size, 8))
            if child is None:
                raise Rejected("B-tree child with an undefined address")
            if node_level > 0:
                yield from self._btree1(child, node_type, key_size, seen, node_level - 1)
            elif node_type == 0:
                if child in seen:
                    raise Rejected(f"symbol table node at {child} read twice")
                seen.add(child)
                yield child
            else:
                yield body[i * step : i * step + key_size], child

    # ---- version 2 B-trees and fractal heaps (§8.4)

    def btree2(self, at: int, typ: int, sizes=None) -> list[bytes]:
        """The records of the version 2 B-tree at `at`, which MUST have type `typ`
        and a record size in `sizes` (by default the type's, RECORD_SIZES)."""
        h = self.read(at, 38)
        if h[:4] != b"BTHD" or h[4] != 0 or h[5] != typ:
            raise Rejected(f"no version 2 B-tree of type {typ} at {at}")
        node_size, record_size, depth = le(h, 6, 4), le(h, 10, 2), le(h, 12, 2)
        root, root_count = address(le(h, 16, 8)), le(h, 24, 2)
        if depth > MAX_BTREE2_DEPTH:
            raise Rejected(f"version 2 B-tree depth {depth}")
        if record_size not in (sizes if sizes is not None else (RECORD_SIZES[typ],)) or node_size < 10 + record_size:
            raise Rejected("invalid version 2 B-tree node or record size")
        leaf_max = (node_size - 10) // record_size
        count_size = log2(leaf_max) // 8 + 1
        # pointer[d]: the size of a child pointer in a node of depth d.
        cum, cum_size, pointer = [leaf_max], [0], [0]
        for d in range(1, depth + 1):
            pointer.append(8 + count_size + (cum_size[d - 1] if d > 1 else 0))
            most = (node_size - 10 - pointer[d]) // (record_size + pointer[d])
            if most < 1:
                raise Rejected("version 2 B-tree nodes too small")
            cum.append((most + 1) * cum[d - 1] + most)
            cum_size.append(log2(cum[d]) // 8 + 1)
        records: list[bytes] = []
        seen: set[int] = set()

        def node(node_at: int, count: int, d: int) -> None:
            if node_at in seen:
                raise Rejected(f"B-tree node at {node_at} read twice")
            seen.add(node_at)
            data = self.read(node_at, 6 + count * record_size + (count + 1) * pointer[d] if d else 6 + count * record_size)
            if data[:4] != (b"BTIN" if d else b"BTLF") or data[4] != 0 or data[5] != typ:
                raise Rejected(f"no version 2 B-tree node at {node_at}")
            records.extend(data[6 + k * record_size : 6 + (k + 1) * record_size] for k in range(count))
            p = 6 + count * record_size
            for _ in range(count + 1 if d else 0):
                child = address(le(data, p, 8))
                if child is None:
                    raise Rejected("B-tree child with an undefined address")
                node(child, le(data, p + 8, count_size), d - 1)
                p += pointer[d]

        if root is not None:
            node(root, root_count, depth)
        return records

    def fractal_heap(self, at: int) -> FractalHeap:
        if at not in self._heaps:
            self._heaps[at] = FractalHeap(self, at)
        return self._heaps[at]

    # ---- attributes (§8.6)

    def attributes(self, at: int, where: dict[bytes, int] | None = None) -> dict[bytes, bytes]:
        """An object's attributes: name -> attribute message; `where` receives
        name -> the message's address in the file."""
        messages = self.header(at)
        out: dict[bytes, bytes] = {}

        def add(data: bytes, offset: int) -> None:
            name = attribute_name(data)
            if name in out:
                raise Rejected(f"two attributes named {name!r}")
            out[name] = data
            if where is not None:
                where[name] = offset

        for m in messages:
            if m.type == ATTRIBUTE:
                if m.flags & 2:
                    raise Rejected("shared attribute message")
                add(m.data, m.offset)
        info = self.message(messages, ATTRIBUTE_INFO, False)
        if info is not None:
            if le(info, 0, 1) != 0:
                raise Rejected("unsupported attribute info message version")
            p = 2 + (2 if le(info, 1, 1) & 1 else 0)
            heap, btree = address(le(info, p, 8)), address(le(info, p + 8, 8))
            if heap is not None:
                if btree is None:
                    raise Rejected("dense attributes without a name index")
                fh = self.fractal_heap(heap)
                for record in self.btree2(btree, 8):
                    if record[8] & 2:
                        raise Rejected("shared dense attribute")
                    found = fh.where(record[:8])
                    add(self.read(*found), found[0])
        return out

    # ---- datasets (§8.5)

    def dataset(self, at: int, any_layout: bool = False) -> Dataset:
        """A dataset: a chunked one with a zero fill value (§8.5), or, with
        `any_layout`, one of any layout and fill value (the source metadata)."""
        messages = self.header(at)
        space = parse_dataspace(self.message(messages, DATASPACE))
        type_message, named = self.datatype(messages) if any_layout else (self.message(messages, DATATYPE), None)
        datatype = parse_datatype(type_message)
        fill_message = self.message(messages, FILL_VALUE, False)
        filters = parse_filters(self.message(messages, FILTERS, False))
        layout = self.message(messages, LAYOUT)
        fill = fill_value(fill_message) if fill_message is not None else None
        if any_layout and fill is not None and len(fill) not in (0, datatype.size):
            raise Rejected("the fill value is not of the datatype's size")
        fill = fill or None
        if not any_layout and fill is not None and any(fill):
            raise Rejected("the fill value is not zero")
        if space.dims is None:
            if any_layout:
                return Dataset(None, None, datatype, [], [], filters, "", None, None, "null", fill=fill,
                               type_message=type_message, named=named)
            raise Rejected("dataset with a null dataspace")
        rank = len(space.dims)
        version, cls = le(layout, 0, 1), le(layout, 1, 1)
        if version not in (3, 4, 5):
            raise Rejected(f"unsupported layout message version {version}")
        if cls == 3:
            raise Rejected("a virtual dataset, whose data is in other datasets")
        if cls != 2 and any_layout and cls in (0, 1):
            ds = Dataset(space.dims, space.maxdims, datatype, [], [], filters, "", None, None,
                         "compact" if cls == 0 else "contiguous", fill=fill, type_message=type_message, named=named)
            if cls == 0:
                n = le(layout, 2, 2)
                if 4 + n > len(layout):
                    raise Rejected("truncated compact dataset")
                ds.compact = layout[4 : 4 + n]
            else:
                ds.data_address = address(le(layout, 2, 8))
                length(le(layout, 10, 8))
            return ds
        if cls != 2:
            raise Rejected(f"dataset layout class {cls} is not chunked")
        single_size, single_mask, raw_edges = None, 0, False
        if version == 3:
            ndims = le(layout, 2, 1)
            index, index_address = "btree1", address(le(layout, 3, 8))
            dims = [le(layout, 11 + 4 * i, 4) for i in range(ndims)]
        else:
            flags, ndims, enc = le(layout, 2, 1), le(layout, 3, 1), le(layout, 4, 1)
            if not 1 <= enc <= 8:
                raise Rejected(f"invalid chunk dimension size length {enc}")
            dims = [le(layout, 5 + enc * i, enc) for i in range(ndims)]
            p = 5 + enc * ndims
            itype = le(layout, p, 1)
            p += 1
            if itype == 1:
                index = "single"
                if flags & 2:
                    single_size, single_mask = length(le(layout, p, 8)), le(layout, p + 8, 4)
                    p += 12
            elif itype == 2:
                index = "implicit"
            elif itype == 3:
                index = "farray"
                le(layout, p, 1)  # page bits: the fixed array header's are used
                p += 1
            elif itype == 4:
                index = "earray"
                le(layout, p, 5)  # the extensible array header's parameters are used
                p += 5
            elif itype == 5:
                index = "btree2"
                le(layout, p, 6)  # node size, split and merge percents: the B-tree header's are used
                p += 6
            else:
                raise Rejected(f"unsupported chunk index type {itype}")
            index_address = address(le(layout, p, 8))
            raw_edges = bool(filters and flags & 1)
            if filters and index == "single" and not flags & 2:
                raise Rejected("single filtered chunk without its size")
            if filters and index == "implicit":
                raise Rejected("implicit chunk index with filters")
        if ndims != rank + 1 or dims[-1] != datatype.size:
            raise Rejected("chunk dimensions do not match the dataspace and datatype")
        chunk = dims[:-1]
        if any(not 1 <= c <= MAX_SAFE for c in chunk):
            raise Rejected("a chunk dimension is not from 1 to 2^53 - 1")
        grid, total = [], 1
        for n, c in zip(space.dims, chunk):
            grid.append(-(-n // c))
            total *= grid[-1]
            if total > MAX_SAFE:
                raise Rejected("more than 2^53 - 1 chunks")
        return Dataset(space.dims, space.maxdims, datatype, chunk, grid, filters, index, index_address, single_size,
                       fill=fill, type_message=type_message, named=named, single_mask=single_mask, raw_edges=raw_edges)

    # ---- the global heap (§8.9)

    def global_object(self, at: int, index: int) -> tuple[int, int]:
        """The (address, size) of the object `index` of the global heap collection at `at`."""
        if at not in self._collections:
            head = self.read(at, 16)
            if head[:4] != b"GCOL" or head[4] != 1:
                raise Rejected(f"no global heap collection at {at}")
            size = length(le(head, 8, 8))
            if size < 16 or at + size > self.size:
                raise Rejected("a global heap collection outside the file")
            data = self.read(at, size)
            objects: dict[int, tuple[int, int]] = {}
            p = 16
            while p + 16 <= size:
                i, n = le(data, p, 2), length(le(data, p + 8, 8))
                if i == 0:
                    break  # free space
                if p + 16 + n > size:
                    raise Rejected("a global heap object outside its collection")
                objects.setdefault(i, (at + p + 16, n))
                p += 16 + _pad8(n)
            self._collections[at] = objects
        found = self._collections[at].get(index)
        if found is None:
            raise Rejected(f"no global heap object {index} at {at}")
        return found

    def virtual_mapping(self, layout: bytes) -> list[dict]:
        """The mappings of a virtual dataset's layout message, from the global heap (§8.9)."""
        if le(layout, 0, 1) not in (4, 5) or le(layout, 1, 1) != 3:
            raise Rejected("not a virtual dataset layout")
        at = address(le(layout, 2, 8))
        if at is None:
            return []
        where, n = self.global_object(at, le(layout, 10, 4))
        d = self.read(where, n)
        if le(d, 0, 1) != 0:
            raise Rejected("unsupported virtual dataset mapping version")
        count, p = le(d, 1, 8), 9
        out = []
        for _ in range(count):
            names = []
            for _ in range(2):
                end = d.find(b"\0", p)
                if end < 0:
                    raise Rejected("truncated virtual dataset mapping")
                names.append(text_json(d[p:end]))
                p = end + 1
            source, p = parse_selection(d, p)
            selection, p = parse_selection(d, p)
            out.append({"file": names[0], "dataset": names[1], "source": source, "selection": selection})
        return out

    def chunks(self, ds: Dataset, masks: dict | None = None) -> dict[tuple[int, ...], tuple[int, int]]:
        """The allocated chunks: grid coordinates -> (address, size) (§8.5). A
        chunk's filter mask MUST be 0, unless `masks` receives the chunks' that
        are not (§8.9); a partial edge chunk stored unfiltered has every
        filter's bit set."""
        out: dict[tuple[int, ...], tuple[int, int]] = {}
        nbytes = ds.datatype.size
        for c in ds.chunk:
            nbytes *= c
        filtered = bool(ds.filters)

        def add(coords: tuple[int, ...], at: int, size: int, mask: int = 0) -> None:
            if ds.raw_edges and any((c + 1) * k > n for c, k, n in zip(coords, ds.chunk, ds.dims)):
                mask = (1 << len(ds.filters)) - 1  # a partial edge chunk, stored unfiltered: every filter skipped
            if mask:
                if masks is None:
                    raise Rejected("a chunk's filter mask is not 0")
                masks[coords] = mask
            if coords in out:
                raise Rejected(f"chunk {_coords(coords)} indexed twice")
            if not filtered and size != nbytes:
                raise Rejected(f"unfiltered chunk {_coords(coords)} of {size} bytes, not {nbytes}")
            if size < 1:
                raise Rejected(f"chunk {_coords(coords)} is empty")
            if at + size > self.size:
                raise Rejected(f"chunk {_coords(coords)} lies outside the file")
            out[coords] = (at, size)

        at = ds.index_address
        if at is None:
            return out
        rank = len(ds.dims)
        if ds.index == "btree1":
            for key, child in self._btree1(at, 1, 8 + 8 * (rank + 1), set(), None):
                size, mask = le(key, 0, 4), le(key, 4, 4)
                offsets = [le(key, 8 + 8 * i, 8) for i in range(rank + 1)]
                if offsets[-1] != 0 or any(o % c or o >= n for o, c, n in zip(offsets, ds.chunk, ds.dims)):
                    raise Rejected(f"invalid chunk offset {_coords(offsets)}")
                add(tuple(o // c for o, c in zip(offsets, ds.chunk)), child, size, mask)
        elif ds.index == "single":
            if any(g != 1 for g in ds.grid):
                raise Rejected("single chunk index for more than one chunk")
            add((0,) * rank, at, ds.single_size if filtered else nbytes, ds.single_mask if filtered else 0)
        elif ds.index == "implicit":
            maxgrid = self.implicit(ds)
            total = _product(ds.grid)
            for i in range(total):
                coords = _unravel(i, ds.grid)
                add(coords, at + _ravel(coords, maxgrid) * nbytes, nbytes)
        elif ds.index == "farray":
            self._fixed_array(ds, at, filtered, nbytes, add)
        elif ds.index == "earray":
            self._extensible_array(ds, at, filtered, nbytes, add)
        else:
            self._btree2_chunks(ds, at, filtered, add)
        return out

    def implicit(self, ds: Dataset) -> list[int]:
        """The maximum grid of an implicit chunk index, whose last chunk MUST lie within the file (§8.5)."""
        maxgrid = _max_grid(ds, False)
        nbytes = _product(ds.chunk) * ds.datatype.size
        # The last chunk is the furthest: when it lies within the file, so do the others.
        if _product(ds.grid) and ds.index_address + (_ravel([g - 1 for g in ds.grid], maxgrid) + 1) * nbytes > self.size:
            raise Rejected("an implicit chunk index past the end of the file")
        return maxgrid

    def _entry(self, data: bytes, k: int, entry: int, filtered: bool, nbytes: int) -> tuple[int | None, int, int]:
        """An array index's entry: (the chunk's address or None, its size, its filter mask) (§8.5)."""
        chunk_at = address(le(data, k * entry, 8))
        if chunk_at is None or not filtered:
            return chunk_at, nbytes, 0
        return chunk_at, length(le(data, k * entry + 8, entry - 12)), le(data, k * entry + entry - 4, 4)

    def _btree2_chunks(self, ds: Dataset, at: int, filtered: bool, add) -> None:
        rank = len(ds.dims)
        sizes = range(13 + 8 * rank, 21 + 8 * rank) if filtered else (8 + 8 * rank,)
        for record in self.btree2(at, 11 if filtered else 10, sizes):
            chunk_at = address(le(record, 0, 8))
            if chunk_at is None:
                raise Rejected("a chunk record with an undefined address")
            p, size, mask = 8, None, 0
            if filtered:
                n = len(record) - 12 - 8 * rank
                size, mask = length(le(record, 8, n)), le(record, 8 + n, 4)
                p += n + 4
            coords = tuple(le(record, p + 8 * i, 8) for i in range(rank))
            if any(c >= g for c, g in zip(coords, ds.grid)):
                raise Rejected(f"invalid chunk coordinates {_coords(coords)}")
            add(coords, chunk_at, size if filtered else _product(ds.chunk) * ds.datatype.size, mask)

    def _extensible_array(self, ds: Dataset, at: int, filtered: bool, nbytes: int, add) -> None:
        maxgrid = _max_grid(ds, True)
        unlimited = [i for i, m in enumerate(maxgrid) if m is None]
        if len(unlimited) != 1:
            raise Rejected("an extensible array index without exactly one unlimited dimension")
        u = unlimited[0]
        order = [u, *[i for i in range(len(maxgrid)) if i != u]]  # the unlimited dimension first
        down = _product([maxgrid[i] for i in order[1:]])
        limit = ds.grid[u] * down  # no index at or beyond it is in the grid
        h = self.read(at, 68)
        if h[:4] != b"EAHD" or h[4] != 0:
            raise Rejected(f"no extensible array header at {at}")
        client, entry, bits, ib_count, db_min, sb_min, page_bits = h[5], h[6], h[7], h[8], h[9], h[10], h[11]
        if client != int(filtered) or ((not 13 <= entry <= 20) if filtered else entry != 8):
            raise Rejected("the extensible array does not match the dataset")
        if not (_power(db_min) and _power(sb_min) and log2(db_min) <= bits <= 64 and page_bits <= 64):
            raise Rejected("invalid extensible array parameters")
        iblock = address(le(h, 60, 8))
        if iblock is None:
            return
        offset_size = (bits + 7) // 8
        page = 1 << page_bits
        nsblks = 1 + bits - log2(db_min)
        ib_sblks = 2 * log2(sb_min)  # the super blocks whose data blocks the index block holds
        ib_dblks = 2 * (sb_min - 1)
        if ib_sblks > nsblks:
            raise Rejected("invalid extensible array parameters")

        def found(first: int, data: bytes, n: int) -> None:
            for k in range(n):
                chunk_at, size, mask = self._entry(data, k, entry, filtered, nbytes)
                if chunk_at is None:
                    continue
                rest = first + k
                outer, inner = divmod(rest, down)
                coords = [0] * len(maxgrid)
                coords[u] = outer
                for i, c in zip(order[1:], _unravel(inner, [maxgrid[i] for i in order[1:]])):
                    coords[i] = c
                if all(c < g for c, g in zip(coords, ds.grid)):
                    add(tuple(coords), chunk_at, size, mask)

        def data_block(block_at: int, first: int, n: int, pages: bytes | None, d: int) -> None:
            prefix = 14 + offset_size
            head = self.read(block_at, prefix)
            if head[:4] != b"EADB" or head[4] != 0 or head[5] != client:
                raise Rejected(f"no extensible array data block at {block_at}")
            want = min(n, limit - first)
            if n <= page:
                found(first, self.read(block_at + prefix, want * entry), want)
                return
            if pages is None:
                raise Rejected("a paged extensible array data block in the index block")
            npages = n // page
            for j in range(npages):
                if j * page >= want:
                    break
                bit = d * npages + j
                if pages[bit // 8] & (0x80 >> (bit % 8)):
                    m = min(page, want - j * page)
                    found(first + j * page, self.read(block_at + prefix + 4 + j * (page * entry + 4), m * entry), m)

        head = self.read(iblock, 14)
        if head[:4] != b"EAIB" or head[4] != 0 or head[5] != client:
            raise Rejected(f"no extensible array index block at {iblock}")
        body = self.read(iblock + 14, ib_count * entry + 8 * ib_dblks + 8 * (nsblks - ib_sblks))
        found(0, body, min(ib_count, limit))
        dblks = body[ib_count * entry : ib_count * entry + 8 * ib_dblks]
        sblks = body[ib_count * entry + 8 * ib_dblks :]
        start, k = ib_count, 0
        for s in range(nsblks):
            count, n = 1 << (s // 2), (1 << ((s + 1) // 2)) * db_min
            if start >= limit:
                break
            needed = min(count, -(-(limit - start) // n))  # the data blocks that hold indexes below the limit
            if s < ib_sblks:
                for d in range(needed):
                    block_at = address(le(dblks, 8 * (k + d), 8))
                    if block_at is not None:
                        data_block(block_at, start + d * n, n, None, d)
                k += count
            else:
                sblock = address(le(sblks, 8 * (s - ib_sblks), 8))
                if sblock is not None:
                    npages = n // page if n > page else 0
                    bitmap = count * ((npages + 7) // 8)
                    prefix = 14 + offset_size
                    shead = self.read(sblock, prefix + bitmap + 8 * count)
                    if shead[:4] != b"EASB" or shead[4] != 0 or shead[5] != client:
                        raise Rejected(f"no extensible array super block at {sblock}")
                    pages = shead[prefix : prefix + bitmap]
                    for d in range(needed):
                        block_at = address(le(shead, prefix + bitmap + 8 * d, 8))
                        if block_at is not None:
                            data_block(block_at, start + d * n, n, pages, d)
            start += count * n

    def _fixed_array(self, ds: Dataset, at: int, filtered: bool, nbytes: int, add) -> None:
        maxgrid = _max_grid(ds, False)
        h = self.read(at, 28)
        if h[:4] != b"FAHD" or h[4] != 0:
            raise Rejected(f"no fixed array header at {at}")
        client, entry, page_bits, count = h[5], h[6], h[7], le(h, 8, 8)
        total = _product(maxgrid)
        if client != int(filtered) or count != total:
            raise Rejected("the fixed array does not match the dataset")
        if (not 13 <= entry <= 20) if filtered else entry != 8:
            raise Rejected(f"invalid fixed array entry size {entry}")
        block = address(le(h, 16, 8))
        if block is None:
            return
        prefix = self.read(block, 14)
        if prefix[:4] != b"FADB" or prefix[4] != 0 or prefix[5] != client:
            raise Rejected(f"no fixed array data block at {block}")
        page = 1 << page_bits
        pages: list[tuple[int, int, int]] = []  # (first entry, entries, address)
        if count > page:
            npages = -(-count // page)
            bitmap = self.read(block + 14, (npages + 7) // 8)
            start = block + 14 + len(bitmap) + 4
            for j in range(npages):
                if bitmap[j // 8] & (0x80 >> (j % 8)):
                    pages.append((j * page, min(page, count - j * page), start + j * (page * entry + 4)))
        else:
            pages.append((0, count, block + 14))
        for first, n, page_at in pages:
            data = self.read(page_at, n * entry)
            for k in range(n):
                chunk_at, size, mask = self._entry(data, k, entry, filtered, nbytes)
                if chunk_at is None:
                    continue
                coords = _unravel(first + k, maxgrid)
                if all(c < g for c, g in zip(coords, ds.grid)):
                    add(coords, chunk_at, size, mask)


def _coords(values) -> str:
    """Coordinates as reasons give them: `[1, 2]`."""
    return "[" + ", ".join(str(v) for v in values) + "]"


def _product(values) -> int:
    n = 1
    for v in values:
        n *= v
    return n


def _power(v: int) -> bool:
    return v >= 1 and v & (v - 1) == 0


def _unravel(i: int, grid: list[int]) -> tuple[int, ...]:
    """The coordinates of index `i` in row-major order over `grid`."""
    coords = []
    for g in reversed(grid):
        coords.append(i % g)
        i //= g
    return tuple(reversed(coords))


def _ravel(coords, grid: list[int]) -> int:
    i = 0
    for c, g in zip(coords, grid):
        i = i * g + c
    return i


def _max_grid(ds: Dataset, unlimited: bool) -> list[int | None]:
    """The number of chunks along each dimension at the maximum dimensions
    (§8.5): None for an unlimited one, which only `unlimited` allows."""
    if ds.maxdims is None:
        return list(ds.grid)
    out: list[int | None] = []
    for n, m, c in zip(ds.dims, ds.maxdims, ds.chunk):
        if m == UNDEFINED and unlimited:
            out.append(None)
        elif m == UNDEFINED or m > MAX_SAFE or m < n:
            raise Rejected("a maximum dimension that the chunk index does not allow")
        else:
            out.append(-(-m // c))
    if _product(g for g in out if g is not None) > MAX_SAFE:
        raise Rejected("more than 2^53 - 1 chunks")
    return out


class FractalHeap:
    """The managed objects of a fractal heap (§8.4)."""

    def __init__(self, f: Hdf5, at: int) -> None:
        self.f = f
        h = f.read(at, 146)
        if h[:4] != b"FRHP" or h[4] != 0:
            raise Rejected(f"no fractal heap at {at}")
        self.id_length, filters = le(h, 5, 2), le(h, 7, 2)
        max_managed = le(h, 10, 4)
        self.width, self.start = le(h, 110, 2), length(le(h, 112, 8))
        self.max_direct, heap_bits = length(le(h, 120, 8)), le(h, 128, 2)
        self.root, self.root_rows = address(le(h, 132, 8)), le(h, 140, 2)
        if filters:
            raise Rejected("filtered fractal heaps are not supported")

        def power(v: int) -> bool:
            return v >= 1 and v & (v - 1) == 0

        if not (power(self.width) and power(self.start) and power(self.max_direct)
                and self.max_direct >= self.start and 1 <= heap_bits <= 64 and max_managed >= 1):
            raise Rejected("invalid fractal heap parameters")
        self.offset_size = (heap_bits + 7) // 8
        self.length_size = min((log2(self.max_direct) + 7) // 8, log2(max_managed) // 8 + 1)
        self.direct_rows = log2(self.max_direct) - log2(self.start) + 2
        self._blocks: set[tuple[int, int]] = set()
        self.huge_tree = address(le(h, 22, 8))
        self._huge_objects: dict[int, tuple[int, int]] | None = None

    def get(self, heap_id: bytes) -> bytes:
        """The object with this heap ID."""
        return self.f.read(*self.where(heap_id))

    def where(self, heap_id: bytes) -> tuple[int, int]:
        """The (address, size) of the object with this heap ID: one run of the file."""
        if len(heap_id) != self.id_length:
            raise Rejected("invalid fractal heap ID")
        kind = heap_id[0] >> 4
        if kind == 1:
            return self._huge(le(heap_id, 1, min(self.id_length - 1, 8)))
        if kind != 0 or len(heap_id) < 1 + self.offset_size + self.length_size:
            raise Rejected("only managed and huge fractal heap objects are supported")
        offset = le(heap_id, 1, self.offset_size)
        n = le(heap_id, 1 + self.offset_size, self.length_size)
        if self.root is None:
            raise Rejected("empty fractal heap")
        if self.root_rows == 0:
            block, start, size = self.root, 0, self.start
        else:
            block, start, size = self._locate(self.root, self.root_rows, 0, offset)
        if (block, start) not in self._blocks:
            head = self.f.read(block, 13 + self.offset_size)
            if head[:4] != b"FHDB" or head[4] != 0 or le(head, 13, self.offset_size) != start:
                raise Rejected(f"no fractal heap direct block at {block}")
            self._blocks.add((block, start))
        if offset < start or offset + n > start + size:
            raise Rejected("fractal heap object outside its block")
        return block + offset - start, n

    def _huge(self, key: int) -> tuple[int, int]:
        """The huge object with this key, from the huge objects' B-tree."""
        if self._huge_objects is None:
            if self.huge_tree is None:
                raise Rejected("huge fractal heap object without a B-tree")
            self._huge_objects = {}
            for record in self.f.btree2(self.huge_tree, 1):
                at, n, k = address(le(record, 0, 8)), length(le(record, 8, 8)), le(record, 16, 8)
                if at is None or k in self._huge_objects:
                    raise Rejected("invalid huge object record")
                self._huge_objects[k] = (at, n)
        if key not in self._huge_objects:
            raise Rejected(f"no huge fractal heap object {key}")
        return self._huge_objects[key]

    def _locate(self, at: int, rows: int, start: int, offset: int) -> tuple[int, int, int]:
        """The direct block (address, heap offset, size) holding `offset`,
        under the indirect block at `at` with `rows` rows starting at `start`."""
        head = self.f.read(at, 13 + self.offset_size)
        if head[:4] != b"FHIB" or head[4] != 0 or le(head, 13, self.offset_size) != start:
            raise Rejected(f"no fractal heap indirect block at {at}")
        pos = start
        for r in range(rows):
            size = self.start if r == 0 else self.start << (r - 1)
            if offset < pos + size * self.width:
                col = (offset - pos) // size
                entry = self.f.read(at + 13 + self.offset_size + 8 * (r * self.width + col), 8)
                child = address(le(entry, 0, 8))
                if child is None:
                    raise Rejected("fractal heap object in an unallocated block")
                if r < self.direct_rows:
                    return child, pos + col * size, size
                child_rows = log2(size) - log2(self.start * self.width) + 1
                if child_rows < 1:
                    raise Rejected("invalid fractal heap indirect block")
                return self._locate(child, child_rows, pos + col * size, offset)
            pos += size * self.width
        raise Rejected("fractal heap offset outside the heap")


# ---- message bodies

def parse_link(d: bytes) -> tuple[bytes, Target]:
    """A link message: (name, target) (§8.3)."""
    if le(d, 0, 1) != 1:
        raise Rejected("unsupported link message version")
    flags, p, kind = le(d, 1, 1), 2, 0
    if flags & 8:
        kind, p = le(d, p, 1), p + 1
    p += (8 if flags & 4 else 0) + (1 if flags & 16 else 0)
    n = 1 << (flags & 3)
    name_length = le(d, p, n)
    p += n
    if p + name_length > len(d):
        raise Rejected("truncated link message")
    name = d[p : p + name_length]
    if kind == 1:
        n = le(d, p + name_length, 2)
        if p + name_length + 2 + n > len(d):
            raise Rejected("truncated soft link")
        return name, d[p + name_length + 2 : p + name_length + 2 + n]
    if kind != 0:
        # Any other type: its value, or None when it does not lie within the message (§8.9).
        q = p + name_length
        n = le(d, q, 2) if q + 2 <= len(d) else None
        return name, (kind, d[q + 2 : q + 2 + n] if n is not None and q + 2 + n <= len(d) else None)
    target = address(le(d, p + name_length, 8))
    if target is None:
        raise Rejected("hard link to an undefined address")
    return name, target


def _attribute_layout(d: bytes) -> tuple[int, int, int, int, int]:
    """(flags, name size, datatype size, dataspace size, name start) of an attribute message."""
    version = le(d, 0, 1)
    if version not in (1, 2, 3):
        raise Rejected(f"unsupported attribute message version {version}")
    return le(d, 1, 1) if version > 1 else 0, le(d, 2, 2), le(d, 4, 2), le(d, 6, 2), 9 if version == 3 else 8


def attribute_name(d: bytes) -> bytes:
    _, name_size, _, _, p = _attribute_layout(d)
    if p + name_size > len(d):
        raise Rejected("truncated attribute message")
    return d[p : p + name_size].split(b"\0", 1)[0]


def _pad8(n: int) -> int:
    return (n + 7) // 8 * 8


def attribute_value(d: bytes) -> tuple[Datatype, int, bytes]:
    """An attribute's (datatype, element count, data) (§8.6)."""
    type_message, dims, data = attribute_parts(d)
    count = 1
    for n in dims:
        count *= n
    return parse_datatype(type_message), count, data


def attribute_parts(d: bytes) -> tuple[bytes, list[int], bytes]:
    """An attribute's (datatype message, dimensions, data) (§8.6)."""
    flags, name_size, type_size, space_size, p = _attribute_layout(d)
    if flags & 3:
        raise Rejected("attribute with a shared datatype or dataspace")
    pad = _pad8 if le(d, 0, 1) == 1 else (lambda n: n)
    p += pad(name_size)
    if p + type_size > len(d):
        raise Rejected("truncated attribute message")
    q = p
    datatype = parse_datatype(d[p : p + type_size])
    p += pad(type_size)
    if p + space_size > len(d):
        raise Rejected("truncated attribute message")
    space = parse_dataspace(d[p : p + space_size])
    p += pad(space_size)
    if space.dims is None:
        raise Rejected("attribute with a null dataspace")
    count = 1
    for n in space.dims:
        count *= n
    if p + count * datatype.size > len(d):
        raise Rejected("truncated attribute data")
    return d[q : q + type_size], space.dims, d[p : p + count * datatype.size]


def attribute_full(d: bytes, committed=None) -> tuple[bytes, list[int] | None, bytes, int | None, int]:
    """An attribute's (datatype message, dimensions or None for a null
    dataspace, data, committed datatype's object header or None, where the
    data starts in the message), its shared datatype resolved by `committed`
    (§8.9)."""
    flags, name_size, type_size, space_size, p = _attribute_layout(d)
    if flags & 2:
        raise Rejected("an attribute with a shared dataspace")
    pad = _pad8 if le(d, 0, 1) == 1 else (lambda n: n)
    p += pad(name_size)
    if p + type_size > len(d):
        raise Rejected("truncated attribute message")
    type_message, named = d[p : p + type_size], None
    if flags & 1:
        if committed is None:
            raise Rejected("an attribute with a shared datatype")
        type_message, named = committed(type_message)
    datatype = parse_datatype(type_message)
    p += pad(type_size)
    if p + space_size > len(d):
        raise Rejected("truncated attribute message")
    space = parse_dataspace(d[p : p + space_size])
    p += pad(space_size)
    if space.dims is None:
        return type_message, None, b"", named, p
    count = 1
    for n in space.dims:
        count *= n
    if p + count * datatype.size > len(d):
        raise Rejected("truncated attribute data")
    return type_message, space.dims, d[p : p + count * datatype.size], named, p


def fill_value(d: bytes) -> bytes:
    """The fill value of a fill value message, or no bytes when it gives none (§8.5)."""
    version = le(d, 0, 1)
    if version in (1, 2):
        if version == 2 and not le(d, 3, 1):
            return b""
        size, p = le(d, 4, 4), 8
    elif version == 3:
        if not le(d, 1, 1) & 0x20:
            return b""
        size, p = le(d, 2, 4), 6
    else:
        raise Rejected(f"unsupported fill value message version {version}")
    if p + size > len(d):
        raise Rejected("truncated fill value message")
    return d[p : p + size]


def fill_is_zero(d: bytes) -> bool:
    """Whether a fill value message gives no fill value, or zero (§8.5)."""
    return not any(fill_value(d))


def parse_filters(d: bytes | None) -> list[int]:
    if d is None:
        return []
    version, count = le(d, 0, 1), le(d, 1, 1)
    if version not in (1, 2):
        raise Rejected(f"unsupported filter pipeline message version {version}")
    p = 8 if version == 1 else 2
    ids = []
    for _ in range(count):
        fid = le(d, p, 2)
        if version == 1 or fid >= 256:
            name_length, p = le(d, p + 2, 2), p + 4
        else:
            name_length, p = 0, p + 2
        values = le(d, p + 2, 2)
        p += 4
        p += _pad8(name_length) if version == 1 else name_length
        p += 4 * values + (4 if version == 1 and values % 2 else 0)
        if p > len(d):
            raise Rejected("truncated filter pipeline message")
        ids.append(fid)
    return ids


def parse_selection(d: bytes, p: int) -> tuple[dict, int]:
    """The serialized dataspace selection at `p`: its description (spec/virtualize/ims.md
    §5.3) and where it ends (§8.9)."""
    kind, version = le(d, p, 4), le(d, p + 4, 4)
    p += 8
    if kind in (0, 3):
        if version != 1:
            raise Rejected(f"unsupported selection version {version}")
        le(d, p, 8)
        return {"select": "none" if kind == 0 else "all"}, p + 8
    if kind == 1:
        if version == 1:
            enc, p = 4, p + 8
        elif version == 2:
            enc, p = le(d, p, 1), p + 1
        else:
            raise Rejected(f"unsupported selection version {version}")
        flags = 0
    elif kind == 2:
        if version == 1:
            flags, enc, p = 0, 4, p + 8
        elif version == 2:
            flags, enc, p = le(d, p, 1), 8, p + 5
        elif version == 3:
            flags, enc, p = le(d, p, 1), le(d, p + 1, 1), p + 2
        else:
            raise Rejected(f"unsupported selection version {version}")
        if flags & ~1:
            raise Rejected("unknown selection flags")
    else:
        raise Rejected(f"unknown selection type {kind}")
    if enc not in (2, 4, 8):
        raise Rejected(f"invalid selection encoding size {enc}")
    rank = le(d, p, 4)
    p += 4
    ones = (1 << (8 * enc)) - 1
    if not flags & 1 and rank == 0:
        raise Rejected("a selection of rank 0")

    def values(q: int, n: int) -> list:
        return [json_number(le(d, q + enc * i, enc)) for i in range(n)]

    if flags & 1:  # a regular hyperslab: start, stride, count and block of each dimension
        if 4 * rank * enc > len(d) - p:
            raise Rejected("truncated selection")
        out: dict = {"select": "hyperslab", "rank": rank, "start": [], "stride": [], "count": [], "block": []}
        for i in range(rank):
            for j, name in enumerate(("start", "stride", "count", "block")):
                v = le(d, p + enc * (4 * i + j), enc)
                out[name].append("unlimited" if j >= 2 and v == ones else json_number(v))
        return out, p + 4 * rank * enc
    n = le(d, p, enc)
    p += enc
    width = (2 if kind == 2 else 1) * rank * enc  # a block's start and end, or a point
    if n > len(d) - p or n * width > len(d) - p:
        raise Rejected("truncated selection")
    if kind == 1:
        points = [values(p + width * i, rank) for i in range(n)]
        return {"select": "points", "rank": rank, "points": points}, p + n * width
    blocks = [[values(p + width * i, rank), values(p + width * i + rank * enc, rank)] for i in range(n)]
    return {"select": "hyperslab", "rank": rank, "blocks": blocks}, p + n * width
