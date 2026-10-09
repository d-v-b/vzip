"""Reading a CZI file's segments (conventions/czi/README.md §2, profiles/czi.md §13.2)."""

from __future__ import annotations

import re
import struct
from array import array
from dataclasses import dataclass, field

from vzip.virtualize.common import MAX_SAFE, Reader, Rejected
from vzip_reference.czi.coding import COMPRESSIONS, PIXEL_TYPES

MAX_ENTRIES = 1 << 21  # directory entries (profiles/czi.md §13.3)
MAX_ATTACHMENTS = 1 << 16  # attachment entries
MAX_WALK = 1 << 23  # segments the walk visits
LETTERS = "XYZCTRSIHVBM"
SEGMENT_ID = re.compile(rb"[A-Z0-9_]{1,16}\0*")


def seg_id(name: str) -> bytes:
    return name.encode().ljust(16, b"\0")


FILE, DIRECTORY, SUBBLOCK, METADATA, ATTDIR, ATTACH = (
    seg_id(n) for n in ("ZISRAWFILE", "ZISRAWDIRECTORY", "ZISRAWSUBBLOCK", "ZISRAWMETADATA", "ZISRAWATTDIR",
                        "ZISRAWATTACH"))


def _offset(v: int, what: str) -> int:
    if not 0 <= v <= MAX_SAFE:
        raise Rejected(f"{what} {v} is not an offset from 0 to 2^53 - 1")
    return v


def _size(v: int, what: str) -> int:
    if not 0 <= v <= MAX_SAFE:
        raise Rejected(f"{what} {v} is not a size from 0 to 2^53 - 1")
    return v


def _within(o: int, n: int, size: int, what: str) -> None:
    if o + n > size:
        raise Rejected(f"{what} [{o}, {o + n}) is outside the {size}-byte file")


def check_header(h: bytes, o: int, expected: bytes, what: str) -> tuple[int, int]:
    """(AllocatedSize, UsedSize) of the segment header `h` read at `o`, which must have the id `expected`."""
    if h[:16] != expected:
        name = expected.rstrip(b"\0").decode()
        raise Rejected(f"{what} at {o} is not a {name} segment")
    allocated, used = struct.unpack_from("<qq", h, 16)
    return _size(allocated, f"{what}'s AllocatedSize"), _size(used, f"{what}'s UsedSize")


def segment_header(read: Reader, size: int, o: int, expected: bytes, what: str) -> tuple[int, int]:
    _offset(o, f"{what}'s position")
    _within(o, 32, size, f"{what}'s segment header")
    return check_header(read(o, 32), o, expected, what)


def guid(b: bytes) -> str:
    """A GUID as text (conventions/czi/README.md §2.2)."""
    a, b1, c = struct.unpack_from("<IHH", b)
    return f"{a:08x}-{b1:04x}-{c:04x}-{b[8:10].hex()}-{b[10:16].hex()}"


@dataclass
class FileHeader:
    major: int
    minor: int
    primary_file_guid: bytes
    file_guid: bytes
    file_part: int
    directory: int
    metadata: int
    update_pending: int
    attachments: int
    allocated: int


def read_file_header(read: Reader, size: int) -> FileHeader:
    allocated, _ = segment_header(read, size, 0, FILE, "the file header")
    _within(32, 80, size, "the file header")
    d = read(32, 80)
    major, minor = struct.unpack_from("<ii", d, 0)
    file_part, directory, metadata, update_pending, attachments = struct.unpack_from("<iqqiq", d, 48)
    if major != 1:
        raise Rejected(f"CZI file version {major}.{minor} (Major MUST be 1)")
    if file_part != 0:
        raise Rejected(f"FilePart {file_part}: a CZI split over several files is not supported")
    return FileHeader(major, minor, d[16:32], d[32:48], file_part, directory, metadata, update_pending, attachments,
                      allocated)


@dataclass
class Column:
    """A dimension letter's directory columns."""
    start: array
    size: array
    stored: array
    coordinate: array  # the float32's bits
    position: array


@dataclass
class Directory:
    """The subblock directory as columns: entry i's fields at index i."""
    offset: int
    allocated: int
    count: int
    pixel_type: array
    compression: array
    pyramid_type: array
    spare: bytearray  # 5 bytes per entry: bytes 23-27
    file_position: array
    raw: bytes  # the entries, back to back
    starts: array  # entry i is raw[starts[i]:starts[i + 1]]
    columns: dict[str, Column] = field(default_factory=dict)

    def entry(self, i: int) -> bytes:
        return self.raw[self.starts[i] : self.starts[i + 1]]

    def dim(self, i: int, letter: str) -> tuple[int, int, int] | None:
        """(Start, Size, StoredSize) of entry i's dimension `letter`, or None."""
        c = self.columns.get(letter)
        if c is None or c.position[i] < 0:
            return None
        return c.start[i], c.size[i], c.stored[i]


def read_directory(read: Reader, size: int, o: int) -> Directory:
    allocated, used = segment_header(read, size, o, DIRECTORY, "the subblock directory")
    used = used or allocated
    _within(o + 32, 4, size, "the directory's EntryCount")
    count = struct.unpack("<i", read(o + 32, 4))[0]
    if not 0 <= count <= MAX_ENTRIES:
        raise Rejected(f"the directory's EntryCount {count} is not from 0 to {MAX_ENTRIES}")
    end = o + 32 + used
    zeros = bytes(4 * count)
    d = Directory(o, allocated, count, array("i", zeros), array("i", zeros), array("B", bytes(count)),
                  bytearray(5 * count), array("q", bytes(8 * count)), b"", array("q", bytes(8 * (count + 1))))
    raw = bytearray()
    pos = o + 160
    window, wstart = b"", pos  # a window of the entries' bytes, read in steps of up to 1 MiB

    def take(at: int, n: int) -> bytes:
        nonlocal window, wstart
        if at + n > end:
            raise Rejected(f"directory entry {i} does not end within the directory's {used} used bytes")
        if at < wstart or at + n > wstart + len(window):
            _within(at, n, size, "a directory entry")
            wstart = at
            window = read(at, max(n, min(1 << 20, end - at, size - at)))
        return window[at - wstart : at - wstart + n]

    for i in range(count):
        head = take(pos, 32)
        if head[:2] != b"DV":
            raise Rejected(f"directory entry {i} has schema {head[:2]!r}, not DV")
        pixel_type, file_position, file_part, compression = struct.unpack_from("<iqii", head, 2)
        dims = struct.unpack_from("<i", head, 28)[0]
        if file_part != 0:
            raise Rejected(f"directory entry {i} is in FilePart {file_part}")
        if pixel_type not in PIXEL_TYPES:
            raise Rejected(f"directory entry {i} has an unsupported PixelType {pixel_type}")
        if compression not in COMPRESSIONS:
            raise Rejected(f"directory entry {i} has an unsupported Compression {compression}")
        if pixel_type not in COMPRESSIONS[compression][1]:
            raise Rejected(f"directory entry {i}: Compression {compression} with PixelType {pixel_type}")
        if not 2 <= dims <= 12:
            raise Rejected(f"directory entry {i} has DimensionCount {dims}, not 2 to 12")
        body = take(pos + 32, 20 * dims)
        d.pixel_type[i], d.compression[i], d.file_position[i] = pixel_type, compression, file_position
        d.pyramid_type[i] = head[22]
        d.spare[5 * i : 5 * i + 5] = head[23:28]
        seen = set()
        for k in range(dims):
            ident, start, sz, coordinate, stored = struct.unpack_from("<4siiIi", body, 20 * k)
            letter = chr(ident[0]) if ident[1:] == b"\0\0\0" and 0x41 <= ident[0] <= 0x5A else None
            if letter is None or letter not in LETTERS:
                raise Rejected(f"directory entry {i} has a dimension {ident!r}")
            if letter in seen:
                raise Rejected(f"directory entry {i} has the dimension {letter} twice")
            seen.add(letter)
            if letter in "XY":
                if sz < 1 or stored < 1:
                    raise Rejected(f"directory entry {i}'s {letter} Size and StoredSize must be at least 1")
            elif letter != "M" and (sz != 1 or stored != 1):
                raise Rejected(f"directory entry {i}'s {letter} Size and StoredSize must be 1")
            c = d.columns.get(letter)
            if c is None:
                c = d.columns[letter] = Column(array("i", zeros), array("i", zeros), array("i", zeros),
                                               array("I", zeros), array("b", b"\xff" * count))
            c.start[i], c.size[i], c.stored[i], c.coordinate[i], c.position[i] = start, sz, stored, coordinate, k
        if "X" not in seen or "Y" not in seen:
            raise Rejected(f"directory entry {i} lacks the X or Y dimension")
        raw += head + body
        pos += 32 + 20 * dims
        d.starts[i + 1] = len(raw)
    d.raw = bytes(raw)
    return d


@dataclass
class Subblocks:
    """Each subblock's header: its segment's AllocatedSize, header length L, and part sizes."""
    allocated: array
    length: array
    metadata: array
    attachment: array
    data: array
    agrees: bytearray  # 1 where its copy of its entry agrees with the directory
    copy_length: array  # the length of its copy of its entry, 32 + 20 d'
    first: dict  # i -> the first bytes of its data, when read with its header


PREFETCH_HEAD = 2048  # the bytes prefetched at a subblock's segment
PREFETCH_DATA = 1024  # the bytes of a subblock's data prefetched with its header, for its codec header
MAX_GAP = 1 << 14  # prefetched ranges this close are read as one
MAX_MERGED = 1 << 20  # up to this many bytes


def coalesce(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sorted (offset, length) ranges, those less than 16 KiB apart merged into reads of up
    to 1 MiB: a file of many small subblocks then costs few requests."""
    out: list[list[int]] = []
    for o, n in sorted(ranges):
        if out and o - (out[-1][0] + out[-1][1]) <= MAX_GAP and o + n - out[-1][0] <= MAX_MERGED:
            out[-1][1] = max(out[-1][1], o + n - out[-1][0])
        else:
            out.append([o, n])
    return [(o, n) for o, n in out]


def read_subblocks(read: Reader, size: int, d: Directory) -> Subblocks:
    n = d.count
    s = Subblocks(array("q", bytes(8 * n)), array("i", bytes(4 * n)), array("i", bytes(4 * n)),
                  array("i", bytes(4 * n)), array("q", bytes(8 * n)), bytearray(n), array("i", bytes(4 * n)), {})
    prefetch = getattr(read, "prefetch", None)
    first = 32 + 288  # the segment header, and L for up to 12 dimensions
    fetched: list[tuple[int, int]] = []
    if prefetch is not None:
        # With the header, the bytes after it, which usually hold the subblock's metadata and codec header.
        fetched = coalesce([(d.file_position[i], PREFETCH_HEAD) for i in range(n) if 0 <= d.file_position[i] <= MAX_SAFE])
        prefetch(fetched)
    for i in range(n):
        o = _offset(d.file_position[i], f"subblock {i}'s FilePosition")
        _within(o, 32 + 256, size, f"subblock {i}'s header")
        h = read(o, min(first, size - o))
        s.allocated[i] = check_header(h, o, SUBBLOCK, f"subblock {i}")[0]
        m, a, data = struct.unpack_from("<iiq", h, 32)
        if m < 0 or a < 0 or not 0 <= data <= MAX_SAFE:
            raise Rejected(f"subblock {i} has a negative MetadataSize, AttachmentSize or DataSize")
        if h[48:50] != b"DV":
            raise Rejected(f"subblock {i}'s copy of its entry has schema {h[48:50]!r}, not DV")
        dims = struct.unpack_from("<i", h, 32 + 16 + 28)[0]
        if not 0 <= dims <= 40:
            raise Rejected(f"subblock {i}'s copy of its entry has DimensionCount {dims}, not 0 to 40")
        length = max(256, 48 + 20 * dims)
        _within(o, 32 + length, size, f"subblock {i}'s header")
        if 32 + length > len(h):
            h = read(o, 32 + length)
        _within(o + 32 + length, m + data + a, size, f"subblock {i}'s parts")
        copy = h[48 : 80 + 20 * dims]
        s.length[i], s.metadata[i], s.attachment[i], s.data[i] = length, m, a, data
        s.copy_length[i] = 32 + 20 * dims
        s.agrees[i] = copy == d.entry(i)
    if prefetch is not None:
        import bisect

        starts = [o for o, _ in fetched]

        def missing(o: int, k: int) -> bool:
            j = bisect.bisect_right(starts, o) - 1
            return j < 0 or o + k > fetched[j][0] + fetched[j][1]

        heads = [(d.file_position[i] + 32 + s.length[i] + s.metadata[i], min(s.data[i], PREFETCH_DATA))
                 for i in range(n) if s.agrees[i] and d.compression[i] != 0 and s.data[i]]
        prefetch(coalesce([h for h in heads if missing(*h)]))
    return s


@dataclass
class MetadataSegment:
    offset: int
    allocated: int
    xml: int  # XmlSize
    attachment: int  # AttachmentSize


def read_metadata_segment(read: Reader, size: int, o: int) -> MetadataSegment:
    allocated, _ = segment_header(read, size, o, METADATA, "the metadata segment")
    _within(o + 32, 8, size, "the metadata segment's sizes")
    x, b = struct.unpack("<ii", read(o + 32, 8))
    if x < 0 or b < 0:
        raise Rejected("the metadata segment has a negative XmlSize or AttachmentSize")
    _within(o + 32 + 256, x + b, size, "the metadata segment's parts")
    return MetadataSegment(o, allocated, x, b)


@dataclass
class Attachment:
    """Attachment entry k: its 128 bytes, and for an A1 entry its segment."""
    entry: bytes
    offset: int = -1  # its segment's offset, for an A1 entry
    allocated: int = 0
    data_size: int = 0
    segment_entry: bytes = b""  # the segment's copy of the entry

    @property
    def a1(self) -> bool:
        return self.entry[:2] == b"A1"

    @property
    def data_offset(self) -> int:
        return self.offset + 32 + 256


def read_attachments(read: Reader, size: int, o: int) -> tuple[int, list[Attachment]]:
    """(the directory's AllocatedSize, its entries)."""
    allocated, _ = segment_header(read, size, o, ATTDIR, "the attachment directory")
    _within(o + 32, 4, size, "the attachment directory's EntryCount")
    count = struct.unpack("<i", read(o + 32, 4))[0]
    if not 0 <= count <= MAX_ATTACHMENTS:
        raise Rejected(f"the attachment directory's EntryCount {count} is not from 0 to {MAX_ATTACHMENTS}")
    _within(o + 32 + 256, 128 * count, size, "the attachment entries")
    raw = read(o + 32 + 256, 128 * count)
    out = [Attachment(raw[128 * k : 128 * k + 128]) for k in range(count)]
    prefetch = getattr(read, "prefetch", None)
    if prefetch is not None:
        prefetch([(struct.unpack_from("<q", a.entry, 12)[0], 32 + 256) for a in out if a.a1])
    for k, a in enumerate(out):
        if not a.a1:
            continue
        position, file_part = struct.unpack_from("<qi", a.entry, 12)
        if file_part != 0:
            raise Rejected(f"attachment entry {k} is in FilePart {file_part}")
        a.offset = _offset(position, f"attachment {k}'s FilePosition")
        _within(a.offset, 32 + 256, size, f"attachment {k}'s header")
        h = read(a.offset, 32 + 256)
        a.allocated = check_header(h, a.offset, ATTACH, f"attachment {k}")[0]
        a.data_size = _size(struct.unpack_from("<q", h, 32)[0], f"attachment {k}'s DataSize")
        a.segment_entry = h[48:176]
        _within(a.data_offset, a.data_size, size, f"attachment {k}'s data")
    return allocated, out


def walk(read: Reader, size: int, known: dict[int, tuple[bytes, int, int]]) -> tuple[list[tuple[int, bytes, int, int]], int]:
    """The walk (profiles/czi.md §13.2 step 6): the segments as (offset, id,
    AllocatedSize, UsedSize) in file order, and where the tail starts.
    `known` holds the headers already read, by offset."""
    out = []
    o = 0
    while o < size and len(out) < MAX_WALK:
        if o in known:
            ident, allocated, used = known[o]
        else:
            if o + 32 > size:
                break
            h = read(o, 32)
            ident = h[:16]
            if not SEGMENT_ID.fullmatch(ident):
                break
            allocated, used = struct.unpack_from("<qq", h, 16)
            if allocated < 0 or used < 0:
                break
        if o + 32 + allocated > size:
            break
        out.append((o, ident, allocated, used))
        o += 32 + allocated
    return out, o
