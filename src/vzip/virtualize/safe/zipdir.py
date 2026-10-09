"""The zip form of a SAFE product (profiles/safe.md §12.4): the ZIP archive's
end records, its central directory, its local headers, and its deflated XML
documents."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from vzip.virtualize.common import Reader, Rejected

EOCD, ZIP64_LOCATOR, ZIP64_EOCD = b"PK\x05\x06", b"PK\x06\x07", b"PK\x06\x06"
CENTRAL, LOCAL = b"PK\x01\x02", b"PK\x03\x04"
MAX_TAIL = 65557  # an EOCD record (22 bytes) and the longest comment
MAX_DEFLATED = 1 << 26  # the largest deflated XML document (a datastrip metadata is 15–25 MB)
MAX_INFLATED = 1 << 27  # the most bytes of all the deflated XML documents together


@dataclass
class Entry:
    """A central directory file header, as §12.4 reads it."""

    name: str
    flags: int
    method: int
    crc: int
    cs: int  # compressed size
    us: int  # uncompressed size
    lho: int  # local header offset
    ds: int = -1  # data start, from the local header


@dataclass
class Directory:
    entries: list[Entry]
    cd_offset: int


def _zip64(extra: bytes, wanted: int) -> list[int]:
    """The first `wanted` values of the ZIP64 extended information extra field."""
    p = 0
    while p + 4 <= len(extra):
        hid, size = struct.unpack_from("<HH", extra, p)
        if p + 4 + size > len(extra):
            break
        if hid == 1:
            if size < 8 * wanted:
                raise Rejected("a zip entry's ZIP64 extra field is too short")
            return list(struct.unpack_from(f"<{wanted}Q", extra, p + 4))
        p += 4 + size
    raise Rejected("a zip entry needs a ZIP64 extra field and has none")


def central_directory(read: Reader, n: int) -> Directory:
    """The entries of the zip file of `n` bytes (§12.4: end of central directory,
    ZIP64, central directory)."""
    tail_len = min(n, MAX_TAIL)
    tail = read(n - tail_len, tail_len)
    at = len(tail)
    while True:
        at = tail.rfind(EOCD, 0, at)
        if at < 0:
            raise Rejected("not a zip file: no end of central directory record")
        if at + 22 <= len(tail) and at + 22 + struct.unpack_from("<H", tail, at + 20)[0] == len(tail):
            break
    eocd = n - tail_len + at
    disk, cd_disk, here, count, cd_size, cd_offset = struct.unpack_from("<HHHHII", tail, at + 4)
    if disk or cd_disk:
        raise Rejected("a zip file that spans several disks")
    if here != count:
        raise Rejected("a zip file's entry counts differ")
    end = eocd
    if count == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        if eocd < 20:
            raise Rejected("a ZIP64 zip file has no ZIP64 end of central directory locator")
        loc = read(eocd - 20, 20)
        sig, ldisk, rec, disks = struct.unpack("<4sIQI", loc)
        if sig != ZIP64_LOCATOR or ldisk != 0 or disks != 1:
            raise Rejected("a ZIP64 zip file has no valid ZIP64 end of central directory locator")
        if rec + 56 > eocd - 20:
            raise Rejected("a zip file's ZIP64 end of central directory record is outside it")
        body = read(rec, 56)
        sig, _, _, _, d1, d2, here, count, cd_size, cd_offset = struct.unpack("<4sQHHIIQQQQ", body)
        if sig != ZIP64_EOCD:
            raise Rejected("a zip file has no ZIP64 end of central directory record where its locator says")
        if d1 or d2:
            raise Rejected("a zip file that spans several disks")
        if here != count:
            raise Rejected("a zip file's entry counts differ")
        end = rec
    if cd_offset + cd_size != end:
        raise Rejected("a zip file's central directory does not end where its end records start")
    cd = read(cd_offset, cd_size)
    entries, p = [], 0
    for _ in range(count):
        if p + 46 > len(cd) or cd[p : p + 4] != CENTRAL:
            raise Rejected("a zip file's central directory has fewer entries than it says")
        (flags, method, _, _, crc, cs, us, nn, ne, nc, disk, _, _, lho) = struct.unpack_from(
            "<HHHHIIIHHHHHII", cd, p + 8)
        if p + 46 + nn + ne + nc > len(cd):
            raise Rejected("a zip file's central directory entry is truncated")
        raw_name = cd[p + 46 : p + 46 + nn]
        extra = cd[p + 46 + nn : p + 46 + nn + ne]
        wanted = [v == 0xFFFFFFFF for v in (us, cs, lho)]
        if any(wanted):
            values = iter(_zip64(extra, sum(wanted)))
            us = next(values) if wanted[0] else us
            cs = next(values) if wanted[1] else cs
            lho = next(values) if wanted[2] else lho
        if disk:
            raise Rejected("a zip entry on another disk")
        if flags & 0x41:
            raise Rejected("a zip entry is encrypted")
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError:
            raise Rejected("a zip entry's name is not UTF-8") from None
        entries.append(Entry(name, flags, method, crc, cs, us, lho))
        p += 46 + nn + ne + nc
    if p != len(cd):
        raise Rejected("a zip file's central directory has more bytes than its entries")
    return Directory(entries, cd_offset)


def product_entries(d: Directory) -> tuple[str, dict[str, Entry], list[str], list[str]]:
    """The product's root directory `D`, its objects by key, the recorded keys of the
    directory entries, and the keys of every directory entry but `D`'s (§12.4)."""
    names = set()
    root = None
    objects: dict[str, Entry] = {}
    ignored: list[str] = []
    dirs: list[str] = []
    for e in d.entries:
        if e.name in names:
            raise Rejected(f"a zip file has two entries named {e.name!r}")
        names.add(e.name)
        head, sep, key = e.name.partition("/")
        if not sep or not head.endswith(".SAFE"):
            raise Rejected(f"a zip entry {e.name[:200]!r} is not under a .SAFE directory")
        if root is None:
            root = head
        elif head != root:
            raise Rejected("a zip file has entries under two root directories")
        if key == "" or key.endswith("/"):
            if e.us:
                ignored.append(key)
            if key:
                dirs.append(key)
            continue
        if "\\" in key or any(s in ("", ".", "..") for s in key.split("/")):
            raise Rejected(f"a zip entry's name {e.name[:200]!r} has an empty, . or .. segment, or a backslash")
        objects[key] = e
    if root is None:
        raise Rejected("an empty zip file")
    return root, objects, ignored, dirs


def locate(read: Reader, d: Directory, objects: dict[str, Entry]) -> None:
    """Each nonempty object's data start, from its local header (§12.4)."""
    todo = [e for e in objects.values() if e.us]
    prefetch = getattr(read, "prefetch", None)
    if prefetch is not None:
        prefetch([(e.lho, 30) for e in todo if e.lho + 30 <= d.cd_offset])
    for e in todo:
        if e.lho + 30 > d.cd_offset:
            raise Rejected("a zip entry's local header is beyond the entries")
        head = read(e.lho, 30)
        if head[:4] != LOCAL:
            raise Rejected("a zip entry has no local header where the central directory says")
        nn, ne = struct.unpack_from("<HH", head, 26)
        e.ds = e.lho + 30 + nn + ne
        if e.ds + e.cs > d.cd_offset:
            raise Rejected("a zip entry's data reaches beyond the entries")
    # Each object's local header and data are its own: two entries that share bytes would
    # make the output repeat them.
    end = 0
    for e in sorted(todo, key=lambda e: e.lho):
        if e.lho < end:
            raise Rejected("two zip entries' local headers and data overlap")
        end = e.ds + e.cs


def inflate(data: bytes, e: Entry) -> bytes:
    """A deflated entry's bytes (§12.4): the raw deflate stream must end exactly at
    its compressed size and give exactly its size, with its CRC-32."""
    d = zlib.decompressobj(-15)
    try:
        out = d.decompress(data, e.us + 1)
    except zlib.error as err:
        raise Rejected(f"a zip entry's deflate stream is invalid: {err}") from None
    if not d.eof or d.unused_data or d.unconsumed_tail or len(out) != e.us:
        raise Rejected("a zip entry's deflate stream does not end at its compressed size with its size")
    if zlib.crc32(out) != e.crc:
        raise Rejected("a zip entry's CRC-32 does not match its bytes")
    return out
