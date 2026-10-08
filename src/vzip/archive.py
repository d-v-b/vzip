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
    [EOCD]                      archive comment = b"vzip/0" + u64 offset + u64 size
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

from vzip.pb import (
    _put_bytes,
    _put_uint,
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

MAX_PAYLOAD = 65519
RANGE_EXTRA_ID = 0x7A76  # "vz": payload is a Range
CONCAT_EXTRA_ID = 0x7A77  # payload is a Concat
ZIP64_EXTRA_ID = 0x0001
FORMAT_VERSION = 0
MAGIC_COMMENT = b"vzip/%d" % FORMAT_VERSION
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
            if len(raw) > MAX_PAYLOAD:  # spec §4.3: a payload error, even if it fits
                raise ValueError(f"reference payload of {len(raw)} bytes exceeds {MAX_PAYLOAD}")
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
        self._range_checks: list = []  # (key, Range) for spec §9.1 bounds checks
        self._sources: dict[Source, int] = {}
        # every source in table order: a Source, or a url string from add_url_refs
        self._source_list: list[Source | str] = []
        self._late: list[tuple[str, bytes, bool]] = []

    def source(self, src: Source) -> int:
        """Intern `src` into the source table and return its index for `Range.source`.

        Index 0 is free on the wire, so register the most common source first.
        """
        i = self._sources.get(src)
        if i is None:
            i = self._sources[src] = len(self._source_list)
            self._source_list.append(src)
        return i

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
            if len(stored) >= _U32:
                raise ValueError("compressed size of 0xFFFFFFFF bytes or more (spec §3.1)")
        off = self._pos
        self._write(
            _LFH.pack(_SIG_LFH, 20, _FLAG_UTF8, method, 0, _DOS_DATE, crc, len(stored), len(body),
                      len(bname), 0)
        )
        self._write(bname)
        self._write(stored)
        self._cd.append((name, off, len(body), len(stored), method, crc, extra))

    def _entry_raw(self, name: str, body: bytes, stored: bytes) -> None:
        """Test hook: a DEFLATE entry whose stored bytes are given verbatim."""
        bname = name.encode()
        off = self._pos
        crc = zlib.crc32(body)
        self._names.add(name)
        self._write(_LFH.pack(_SIG_LFH, 20, _FLAG_UTF8, 8, 0, _DOS_DATE, crc, len(stored),
                              len(body), len(bname), 0))
        self._write(bname)
        self._write(stored)
        self._cd.append((name, off, len(body), len(stored), 8, crc, b""))

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

    def add_hidden(
        self, name: str, data: bytes, *, compress: bool = True, late: bool = False
    ) -> str:
        """Add an entry outside the zarr key space; returns its key (see `internal`)."""
        key = RESERVED_PREFIX + name
        if key in (SOURCES_KEY, INDEX_KEY):
            raise ValueError(f"{key!r} is written by the writer itself")
        if late:
            if key in self._names or any(k == key for k, *_ in self._late):
                raise ValueError(f"duplicate key {key!r}")
            self._late.append((key, bytes(data), compress))
        else:
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
            if r.data is None and r.source >= len(self._source_list):
                raise ValueError(f"range of {key!r} uses unregistered source {r.source}")
        self._range_checks.extend((key, r) for r in ranges if r.data is None)
        if len(ranges) == 1:
            hid, payload = RANGE_EXTRA_ID, ranges[0].encode()
        else:
            hid, payload = CONCAT_EXTRA_ID, Concat(tuple(ranges)).encode()
        # spec §4.3: fixed limit, leaving room for a ZIP64 block wherever the entry lands
        if len(payload) > MAX_PAYLOAD:
            raise ValueError("reference payload too large for a zip extra field (spec §4.3)")
        extra = struct.pack("<HH", hid, len(payload)) + payload
        self._entry(key, payload if self._mirror else b"", extra)
        self._ref_names.add(key)

    def add_url_refs(self, items) -> None:
        """Bulk path for many objects referenced whole: for each (key, url, size),
        a new url source (not interned) and a reference entry `(source, 0, size)`.

        Avoids a Source object, an interning lookup and a deferred bounds check per
        entry (url sources are not bounds-checked), so millions of entries fit.
        """
        for key, url, size in items:
            if key.startswith(RESERVED_PREFIX):
                raise ValueError(f"{RESERVED_PREFIX!r} is reserved")
            i = len(self._source_list)
            self._source_list.append(url)
            payload = bytearray()
            _put_uint(payload, 1, i)
            _put_uint(payload, 4, size)
            payload = bytes(payload)
            extra = struct.pack("<HH", RANGE_EXTRA_ID, len(payload)) + payload
            self._entry(key, payload if self._mirror else b"", extra)
            self._ref_names.add(key)

    def _check_sources(self, all_sources: list) -> None:
        """Writer requirements on the source table (spec §9.1)."""
        from vzip.uri import is_uri_reference

        bulk = [x for x in all_sources if isinstance(x, str)]
        bad = [u for u in bulk if not u or not is_uri_reference(u)]
        if bad:
            raise ValueError(f"empty or invalid url sources: {bad[:5]}")
        placeholder = Source(url="x")  # bulk urls are checked above
        sources = [x if isinstance(x, Source) else placeholder for x in all_sources]
        if any(x.key == "" for x in sources):
            raise ValueError("empty key source")
        sizes = {name: size for name, _, size, *_ in self._cd}
        sizes.update((k, len(d)) for k, d, _ in self._late)
        for key, r in self._range_checks:
            src = sources[r.source]
            n = len(src.data) if src.data is not None else sizes.get(src.key)
            if n is not None and src.url is None and r.offset + r.length > n:
                raise ValueError(f"{key!r}: range [{r.offset}, {r.offset + r.length}) is past the "
                                 f"end of source {r.source} ({n} bytes)")
        missing = sorted(x.key for x in sources if x.key is not None and x.key not in self._names)
        if missing:
            raise ValueError(f"internal references to missing entries: {missing}")
        fmt = sorted(x.key for x in sources if x.key in (SOURCES_KEY, INDEX_KEY))
        if fmt:
            raise ValueError(f"key sources naming format entries: {fmt}")
        bad = [x.url for x in sources if x.url is not None and x is not placeholder
               and not is_uri_reference(x.url)]
        if any(x.url == "" for x in sources) or bad:
            raise ValueError(f"empty or invalid url sources: {bad}")
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
        sources = self._source_list
        # test hooks (conformance/cases.py builds deliberately broken archives)
        table = getattr(self, "_source_table_override", None) or _encode_sources(sources)
        if getattr(self, "_sources_trailing_junk", False):
            c = zlib.compressobj(9, zlib.DEFLATED, -15)
            self._entry_raw(SOURCES_KEY, table, c.compress(table) + c.flush() + b"junk")
        else:
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
        if getattr(self, "_trailer_first", False) and not self._page_size:
            body = [r for r in self._cd if r[0] in trailer] + body
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
        if not (getattr(self, "_trailer_first", False) and not self._page_size):
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


def _encode_sources(sources: list) -> bytes:
    """The SourceTable of `sources` (Source objects, or bare url strings)."""
    if all(isinstance(x, Source) for x in sources):
        return encode_source_table(sources)
    out = bytearray()
    for x in sources:
        if isinstance(x, Source):
            _put_bytes(out, 1, x.encode())
        else:
            u = x.encode()
            sub = bytearray()
            _put_bytes(sub, 1, u)
            _put_bytes(out, 1, bytes(sub))
    return bytes(out)


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
    if not comment.startswith(b"vzip/"):
        raise ValueError(f"not a vzip archive (comment starts {comment[:6]!r})")
    if not comment.startswith(MAGIC_COMMENT):
        raise ValueError(f"unsupported vzip format version {comment[5:6]!r}; this reader "
                         f"implements version {FORMAT_VERSION}")
    n_disk = struct.unpack_from("<H", tail, i + 8)[0]
    # spec §3.2: zip64 records are used iff an EOCD count/size/offset is all ones
    if _U16 in (n, n_disk) or cd_size == _U32 or cd_off == _U32:
        loc = i - _LOC64.size
        if loc < 0 or tail[loc : loc + 4] != struct.pack("<I", _SIG_LOC64):
            raise ValueError("end of central directory needs zip64 records, which are missing")
        _, _, eocd64_off, _ = _LOC64.unpack_from(tail, loc)
        j = eocd64_off - (file_size - len(tail))
        if j < 0 or eocd64_off + _EOCD64.size > file_size:
            raise ValueError("zip64 EOCD outside the file or the tail buffer")
        (sig, rec_size, _, _, _, _, _, n, cd_size, cd_off) = _EOCD64.unpack_from(tail, j)
        if sig != _SIG_EOCD64 or rec_size != 44:
            raise ValueError("bad zip64 EOCD")
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
        if pos + nlen + xlen + clen > len(cd):
            raise ValueError("central directory record runs past the directory")
        try:
            name = bytes(mv[pos : pos + nlen]).decode("utf-8")
        except UnicodeDecodeError:
            name = ""
        pos += nlen
        extra = bytes(mv[pos : pos + xlen])
        pos += xlen + clen
        if not name:  # spec §3.3: such records name no key
            continue
        if name in entries:
            raise ValueError(f"duplicate central directory record for {name!r}")
        payload, error, ref_blocks, z64 = None, None, 0, []
        try:
            for hid, data in _iter_extra(extra):
                if hid in (RANGE_EXTRA_ID, CONCAT_EXTRA_ID):
                    ref_blocks += 1
                    payload = (hid, data)
                elif hid == ZIP64_EXTRA_ID:
                    z64.append(data)
        except (ValueError, IndexError, struct.error) as e:
            error = f"unparseable extra field: {e}"
        # spec §3.2: sizes never use ZIP64; only the offset may, in one 8-byte block
        if error is None and len(z64) > 1:
            error = "more than one ZIP64 extra block"
        elif error is None and _U32 in (size, csize):
            error = "a size field is 0xFFFFFFFF"
        elif error is None and off == _U32:
            if not z64 or len(z64[0]) < 8:
                error = "ZIP64 extra block missing or too short"
            else:
                (off,) = struct.unpack_from("<Q", z64[0])
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


def inflate_clean(raw: bytes) -> bytes:
    """Inflate one complete raw DEFLATE stream that fills `raw` exactly (spec §8.1)."""
    d = zlib.decompressobj(-15)
    out = d.decompress(raw)
    if not d.eof:
        raise ValueError("DEFLATE stream is incomplete")
    if d.unused_data:
        raise ValueError(f"{len(d.unused_data)} bytes follow the DEFLATE stream")
    return out


def parse_local_header(buf: bytes) -> int:
    """Return the length of a local file header (i.e. offset of the body)."""
    sig, *_, nlen, xlen = _LFH.unpack_from(buf, 0)
    if sig != _SIG_LFH:
        raise ValueError("bad local file header")
    return _LFH.size + nlen + xlen


LFH_SIZE = _LFH.size


def read_source_table(body: bytes) -> list[Source]:
    return decode_source_table(body)
