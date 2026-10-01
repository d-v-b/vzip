"""Minimal ZIP writer/parser for vzip archives.

Layout written by `VZipWriter`:

    [local header + body]*      one per entry, STORED unless compress=True, no local
                                extra field
    [SourceTable]               reserved entry "__vz__/sources" (deflated)
    [late entries]              entries added with late=True (e.g. zarr.json), so a
                                reader's open request covers them
    [CdIndex]                   only with page_size: sparse index over the CD
    [central directory]         sorted; __vz__/sources and __vz__/index records last.
                                Reference entries carry extra field 0x7a76 with a
                                serialized Range, or 0x7a77 with a serialized Concat
    [zip64 EOCD + locator]      only when needed
    [EOCD]                      archive comment = b"vzip/1" + u64 offset + u64 size
                                of the (deflated) SourceTable body [+ the same for
                                the CdIndex]

The comment promises that every local header has an empty extra field, so a
reader can compute the offset of any body from the central directory alone,
and lets a reader fetch the central directory and URL table in one request.
Archives without the comment are still readable; the reader then reads each
local header before trusting the body offset.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import BinaryIO

from refstore.pb import (
    Page,
    Pinned,
    Concat,
    Range,
    Reference,
    Source,
    decode_source_table,
    encode_cd_index,
    encode_source_table,
)

RANGE_EXTRA_ID = 0x7A76  # "vz": payload is a Range
CONCAT_EXTRA_ID = 0x7A77  # payload is a Concat
ZIP64_EXTRA_ID = 0x0001
MAGIC_COMMENT = b"vzip/1"
SOURCES_KEY = "__vz__/sources"
INDEX_KEY = "__vz__/index"
RESERVED_PREFIX = "__vz__/"

_LFH = struct.Struct("<IHHHHHIIIHH")  # 30 bytes
_CDH = struct.Struct("<IHHHHHHIIIHHHHHII")  # 46 bytes
_EOCD = struct.Struct("<IHHHHIIH")  # 22 bytes
_EOCD64 = struct.Struct("<IQHHIIQQQQ")  # 56 bytes
_LOC64 = struct.Struct("<IIQI")  # 20 bytes

_SIG_LFH = 0x04034B50
_SIG_CDH = 0x02014B50
_SIG_EOCD = 0x06054B50
_SIG_EOCD64 = 0x06064B50
_SIG_LOC64 = 0x07064B50

_U32 = 0xFFFFFFFF
_U16 = 0xFFFF
_FLAG_UTF8 = 0x0800
_DOS_DATE = (0 << 9) | (1 << 5) | 1  # 1980-01-01, for reproducible archives
_MADE_BY = 20  # spec §9.2
_EXT_ATTR = 0


@dataclass
class Entry:
    name: str
    header_offset: int
    size: int  # uncompressed
    csize: int
    method: int
    # (extra field header ID, payload) for reference entries; decoded lazily
    payload: tuple[int, bytes] | None = None
    # offset of the body, if it is known without reading the local header
    data_offset: int | None = None
    # spec §8.4 entry error: every operation on this key fails with it
    error: str | None = None
    _ref: Reference | None = None

    @property
    def is_ref(self) -> bool:
        return self.payload is not None

    @property
    def ref(self) -> Reference | None:
        """The decoded payload; raises ValueError if it is malformed (a payload error)."""
        if self.payload is None:
            return None
        if self._ref is None:
            hid, raw = self.payload
            self._ref = (Range if hid == RANGE_EXTRA_ID else Concat).decode(raw)
        return self._ref


class VZipWriter:
    """Stream a vzip archive to a binary file object."""

    def __init__(
        self, f: BinaryIO, *, mirror_refs: bool = True, page_size: int | None = None
    ) -> None:
        """`page_size`: if set, also write a CdIndex with pages of ~page_size bytes."""
        self._f = f
        self._page_size = page_size
        self._pos = 0
        self._mirror = mirror_refs
        # name, header offset, size, compressed size, method, crc, extra
        self._cd: list[tuple[str, int, int, int, int, int, bytes]] = []
        self._names: set[str] = set()
        self._ref_names: set[str] = set()
        self._sources: dict[Source, int] = {}
        self._late: list[tuple[str, bytes, bool]] = []

    def source(self, src: Source) -> int:
        """Intern `src` into the source table and return its index for `Range.source`.

        Index 0 is free on the wire, so register the most common source first.
        """
        return self._sources.setdefault(src, len(self._sources))

    def url(self, url: str) -> int:
        """Source index of an external object (absolute, or relative to the archive)."""
        return self.source(Source(url=url))

    def internal(self, key: str) -> int:
        """Source index of another entry of this archive (checked at close)."""
        return self.source(Source(key=key))

    def blob(self, data: bytes) -> int:
        """Source index of literal bytes stored in the table, e.g. a shared header."""
        return self.source(Source(data=bytes(data)))

    def _write(self, b: bytes) -> None:
        self._f.write(b)
        self._pos += len(b)

    def _entry(self, name: str, body: bytes, extra: bytes = b"", compress: bool = False) -> None:
        if not name:
            raise ValueError("empty key")
        if name in self._names:
            raise ValueError(f"duplicate key {name!r}")
        if len(body) >= _U32:
            raise ValueError("entries of 0xFFFFFFFF bytes or more cannot be stored (spec §3.1)")
        self._names.add(name)
        bname = name.encode()
        crc = zlib.crc32(body)
        method, stored = 0, body
        if compress:
            c = zlib.compressobj(9, zlib.DEFLATED, -15)
            method, stored = 8, c.compress(body) + c.flush()
        off = self._pos
        self._write(
            _LFH.pack(_SIG_LFH, 20, _FLAG_UTF8, method, 0, _DOS_DATE, crc, len(stored), len(body),
                      len(bname), 0)
        )
        self._write(bname)
        self._write(stored)
        self._cd.append((name, off, len(body), len(stored), method, crc, extra))

    def add_bytes(self, key: str, data: bytes, *, compress: bool = False, late: bool = False) -> None:
        """Add a concrete entry. Compressed entries can only be read whole.

        `late=True` defers the entry to just before the central directory, where
        the reader's open request will fetch it (use for small metadata documents).
        """
        if key.startswith(RESERVED_PREFIX):
            raise ValueError(f"{RESERVED_PREFIX!r} is reserved")
        if late:
            if key in self._names or any(k == key for k, *_ in self._late):
                raise ValueError(f"duplicate key {key!r}")
            self._late.append((key, bytes(data), compress))
        else:
            self._entry(key, bytes(data), compress=compress)

    def add_hidden(self, name: str, data: bytes, *, compress: bool = True) -> str:
        """Add an entry outside the zarr key space; returns its key (see `internal`)."""
        key = RESERVED_PREFIX + name
        if key in (SOURCES_KEY, INDEX_KEY):
            raise ValueError(f"{key!r} is written by the writer itself")
        self._entry(key, bytes(data), compress=compress)
        return key

    def add_ref(self, key: str, url: str, offset: int, length: int) -> None:
        self.add_ranges(key, [Range(source=self.url(url), offset=offset, length=length)])

    def add_ranges(self, key: str, ranges: list[Range]) -> None:
        """Add a reference whose value is the concatenation of `ranges`.

        One range is stored as a bare Range (extra field 0x7a76); several as a
        Concat (0x7a77). `Range.source` must come from `source`/`url`/`internal`/`blob`.
        """
        if key.startswith(RESERVED_PREFIX):
            raise ValueError(f"{RESERVED_PREFIX!r} is reserved")
        for r in ranges:
            if r.data is None and r.source >= len(self._sources):
                raise ValueError(f"range of {key!r} uses unregistered source {r.source}")
        if len(ranges) == 1:
            hid, payload = RANGE_EXTRA_ID, ranges[0].encode()
        else:
            hid, payload = CONCAT_EXTRA_ID, Concat(tuple(ranges)).encode()
        # the whole extra field is <= 65535 bytes, including a ZIP64 block if needed
        limit = _U16 - 4 - (12 if self._pos >= _U32 else 0)
        if len(payload) > limit:
            raise ValueError("reference payload too large for a zip extra field (spec §4.3)")
        extra = struct.pack("<HH", hid, len(payload)) + payload
        self._entry(key, payload if self._mirror else b"", extra)
        self._ref_names.add(key)

    def _check_sources(self, sources: list[Source]) -> None:
        """Writer requirements on the source table (spec §9.1)."""
        missing = sorted(x.key for x in sources if x.key is not None and x.key not in self._names)
        if missing:
            raise ValueError(f"internal references to missing entries: {missing}")
        fmt = sorted(x.key for x in sources if x.key in (SOURCES_KEY, INDEX_KEY))
        if fmt:
            raise ValueError(f"key sources naming format entries: {fmt}")
        if any(x.url == "" for x in sources):
            raise ValueError("empty url source")
        to_refs = sorted(x.key for x in sources if x.key is not None and x.key in self._ref_names)
        if to_refs:
            raise ValueError(f"internal references to reference entries: {to_refs}")

    @staticmethod
    def _cd_record(name, off, size, csize, method, crc, extra) -> bytes:
        bname = name.encode()
        if off >= _U32:
            extra = struct.pack("<HHQ", ZIP64_EXTRA_ID, 8, off) + extra
            off32, need = _U32, 45
        else:
            off32, need = off, 20
        hdr = _CDH.pack(
            _SIG_CDH, _MADE_BY, need, _FLAG_UTF8, method, 0, _DOS_DATE, crc, csize, size,
            len(bname), len(extra), 0, 0, 0, _EXT_ATTR, off32,
        )
        return hdr + bname + extra

    def _data_offset(self, i: int) -> tuple[int, int]:
        name, off, _, csize, *_ = self._cd[i]
        return off + _LFH.size + len(name.encode()), csize

    def close(self) -> None:
        sources = sorted(self._sources, key=self._sources.__getitem__)
        # test hooks (conformance/cases.py builds deliberately broken archives)
        table = getattr(self, "_source_table_override", None) or encode_source_table(sources)
        self._entry(SOURCES_KEY, table, compress=True)
        comment = MAGIC_COMMENT + struct.pack("<QQ", *self._data_offset(-1))
        pinned = []
        for key, data, compress in self._late:
            self._entry(key, data, compress=compress)
            name, off, size, csize, method, *_ = self._cd[-1]
            pinned.append(Pinned(key, self._data_offset(-1)[0], size, csize, method))

        if not getattr(self, "_skip_checks", False):  # test hook: see conformance/cases.py
            self._check_sources(sources)

        trailer = {SOURCES_KEY, INDEX_KEY}
        body = sorted(r for r in self._cd if r[0] not in trailer)
        records = [self._cd_record(*r) for r in body]
        if self._page_size:
            pages = []
            first, start, pos = 0, 0, 0
            for i, rec in enumerate(records):
                if pos - start >= self._page_size:
                    pages.append(Page(body[first][0], start, pos - start))
                    first, start = i, pos
                pos += len(rec)
            if records:
                pages.append(Page(body[first][0], start, pos - start))
            self._entry(INDEX_KEY, encode_cd_index(pages, pinned), compress=True)
            comment += struct.pack("<QQ", *self._data_offset(-1))
        records += [self._cd_record(*r) for r in self._cd if r[0] in trailer]

        cd_start = self._pos
        for rec in records:
            self._write(rec)
        cd_size = self._pos - cd_start
        n = len(self._cd)

        if n >= _U16 or cd_start >= _U32 or cd_size >= _U32:
            eocd64_off = self._pos
            self._write(_EOCD64.pack(_SIG_EOCD64, 44, _MADE_BY, 45, 0, 0, n, n, cd_size, cd_start))
            self._write(_LOC64.pack(_SIG_LOC64, 0, eocd64_off, 1))
            n16 = min(n, _U16)
            size32 = min(cd_size, _U32)
            start32 = min(cd_start, _U32)
        else:
            n16, size32, start32 = n, cd_size, cd_start
        self._write(_EOCD.pack(_SIG_EOCD, 0, 0, n16, n16, size32, start32, len(comment)))
        self._write(comment)

    def __enter__(self) -> VZipWriter:
        return self

    def __exit__(self, *exc) -> None:
        if exc[0] is None:
            self.close()


# ---------------------------------------------------------------- reading


TAIL_GUESS = 1 << 16  # first speculative read from the end of the archive


@dataclass(slots=True)
class Directory:
    """Locations of the central directory, decoded from the archive tail."""

    cd_offset: int
    cd_size: int
    n_entries: int
    is_vzip: bool
    sources_offset: int | None = None  # body of the deflated SourceTable, if is_vzip
    sources_size: int = 0
    index_offset: int | None = None  # body of the deflated CdIndex, if paged
    index_size: int = 0


def parse_tail(tail: bytes, file_size: int) -> Directory:
    """Decode the end records of a vzip archive from its last bytes (spec §3.4)."""
    for clen in (38, 22):
        i = len(tail) - _EOCD.size - clen
        if i >= 0 and tail[i : i + 4] == struct.pack("<I", _SIG_EOCD):
            if _EOCD.unpack_from(tail, i)[7] == clen:
                break
    else:
        raise ValueError("not a vzip archive (no end of central directory record "
                         "with a 22- or 38-byte comment)")
    sig, _, _, _, n, cd_size, cd_off, _ = _EOCD.unpack_from(tail, i)
    comment = tail[i + _EOCD.size :]
    if not comment.startswith(MAGIC_COMMENT):
        raise ValueError(f"not a vzip version 1 archive (comment starts {comment[:6]!r})")
    loc = i - _LOC64.size
    if loc >= 0 and tail[loc : loc + 4] == struct.pack("<I", _SIG_LOC64):
        _, _, eocd64_off, _ = _LOC64.unpack_from(tail, loc)
        j = eocd64_off - (file_size - len(tail))
        if j < 0:
            raise ValueError("zip64 EOCD outside the tail buffer")
        (sig, _, _, _, _, _, n, _, cd_size, cd_off) = _EOCD64.unpack_from(tail, j)
        if sig != _SIG_EOCD64:
            raise ValueError("bad zip64 EOCD")
    elif n == _U16 or cd_size == _U32 or cd_off == _U32:
        raise ValueError("end of central directory needs zip64 records, which are missing")
    soff, ssize = struct.unpack_from("<QQ", comment, len(MAGIC_COMMENT))
    ioff, isize = (None, 0)
    if clen == 38:
        ioff, isize = struct.unpack_from("<QQ", comment, len(MAGIC_COMMENT) + 16)
    return Directory(cd_off, cd_size, n, True, soff, ssize, ioff, isize)


def _iter_extra(extra: bytes):
    pos = 0
    while pos < len(extra):
        if pos + 4 > len(extra):
            raise ValueError("truncated extra field block header")
        hid, n = struct.unpack_from("<HH", extra, pos)
        if pos + 4 + n > len(extra):
            raise ValueError("extra field block runs past the end of the extra field")
        yield hid, extra[pos + 4 : pos + 4 + n]
        pos += 4 + n


def parse_central_directory(cd: bytes, *, trust_offsets: bool) -> dict[str, Entry]:
    """Decode the central directory into {key: Entry}.

    This is the classifier for the overlay (spec §4.1): an entry with one
    0x7a76 or 0x7a77 extra field block belongs to the reference key space, any
    other entry to the bytes key space, and a key with no entry to neither.
    Problems confined to one record become that entry's `error` (spec §8.4).
    """
    entries: dict[str, Entry] = {}
    mv = memoryview(cd)
    pos = 0
    while pos < len(cd):
        if pos + _CDH.size > len(cd):
            raise ValueError("truncated central directory record")
        (sig, _, _, flags, method, _, _, _, csize, size, nlen, xlen, clen, _, _, _, off) = (
            _CDH.unpack_from(mv, pos)
        )
        if sig != _SIG_CDH:
            raise ValueError(f"bad central directory record at {pos}")
        pos += _CDH.size
        name = bytes(mv[pos : pos + nlen]).decode("utf-8", "replace")
        pos += nlen
        extra = bytes(mv[pos : pos + xlen])
        pos += xlen + clen
        if name in entries:
            raise ValueError(f"duplicate central directory record for {name!r}")
        payload, error, ref_blocks = None, None, 0
        try:
            for hid, data in _iter_extra(extra):
                if hid in (RANGE_EXTRA_ID, CONCAT_EXTRA_ID):
                    ref_blocks += 1
                    payload = (hid, data)
                elif hid == ZIP64_EXTRA_ID:
                    vals = list(struct.unpack_from(f"<{len(data) // 8}Q", data))
                    if size == _U32:
                        size = vals.pop(0)
                    if csize == _U32:
                        csize = vals.pop(0)
                    if off == _U32:
                        off = vals.pop(0)
        except (ValueError, IndexError, struct.error) as e:
            error = f"unparseable extra field: {e}"
        if error is None:
            if ref_blocks > 1:
                error = "more than one reference extra field block"
            elif method not in (0, 8):
                error = f"unsupported compression method {method}"
            elif flags & 1:
                error = "encrypted entry"
            elif payload is not None and method != 0:
                error = "reference entry is not STORED"
        data_offset = off + _LFH.size + nlen if trust_offsets else None
        entries[name] = Entry(name, off, size, csize, method, payload, data_offset, error)
    return entries


def parse_local_header(buf: bytes) -> int:
    """Return the length of a local file header (i.e. offset of the body)."""
    sig, *_, nlen, xlen = _LFH.unpack_from(buf, 0)
    if sig != _SIG_LFH:
        raise ValueError("bad local file header")
    return _LFH.size + nlen + xlen


LFH_SIZE = _LFH.size


def read_source_table(body: bytes) -> list[Source]:
    return decode_source_table(body)
