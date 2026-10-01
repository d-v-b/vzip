"""A read-only zarr store over a vzip archive.

The store is an overlay of two key spaces held in one ZIP file:

* bytes keys      -> the body of the zip entry
* reference keys  -> the concatenation of byte ranges of other objects

`VZipStore(url, resolve=False)` behaves like a plain zip reader: reference
entries return their serialized `Range`/`Concat` payload. `resolve=True` (the default)
follows references with additional range requests.
"""

from __future__ import annotations

import asyncio
import bisect
import os
import re
import zlib
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import obstore
from obstore.store import HTTPStore, LocalStore, from_url
from zarr.abc.store import (
    ByteRequest,
    OffsetByteRequest,
    RangeByteRequest,
    Store,
    SuffixByteRequest,
)
from zarr.core.buffer import Buffer, BufferPrototype

from refstore.archive import (
    LFH_SIZE,
    RESERVED_PREFIX,
    TAIL_GUESS,
    INDEX_KEY,
    SOURCES_KEY,
    Entry,
    parse_central_directory,
    parse_local_header,
    parse_tail,
    read_source_table,
)
from refstore.pb import Reference, Source, decode_cd_index, parts


@dataclass
class Stats:
    """Request accounting, split by archive vs. referenced objects."""

    archive_requests: int = 0
    archive_bytes: int = 0
    external_requests: int = 0
    external_bytes: int = 0
    log: list[tuple[str, int, int]] = field(default_factory=list)

    def record(self, url: str, start: int, n: int, *, external: bool) -> None:
        if external:
            self.external_requests += 1
            self.external_bytes += n
        else:
            self.archive_requests += 1
            self.archive_bytes += n
        self.log.append((url, start, n))

    def reset(self) -> None:
        self.__init__()


# RFC 3986 URI-reference characters: unreserved, reserved, and percent-encodings
_URI_REF = re.compile(r"^(?:[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=]|%[0-9A-Fa-f]{2})*$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


def resolve_reference(base: str, ref: str) -> str:
    """Strict RFC 3986 §5.2.2 resolution of `ref` against `base` (spec §6)."""
    if not _URI_REF.match(ref):
        raise ValueError(f"not a valid URI reference: {ref!r}")
    if _SCHEME.match(ref):  # strict: a reference with a scheme is used as is
        return ref
    return urljoin(base, ref)


class Resolver:
    """Map URLs to (obstore store, path). Pre-register stores for credentials."""

    def __init__(self, stores: dict[str, object] | None = None) -> None:
        self._stores: dict[str, object] = dict(stores or {})

    def resolve(self, url: str) -> tuple[object, str]:
        p = urlparse(url)
        if p.scheme == "file":
            if p.netloc not in ("", "localhost"):
                raise ValueError(f"file: URL with authority {p.netloc!r}")
            if p.query:
                raise ValueError("file: URL with a query")
            if not p.path.startswith("/"):
                raise ValueError("file: URL with a relative path")
            return self._stores.setdefault("file://", LocalStore()), unquote(p.path)
        if p.scheme not in ("http", "https", "s3", "gs", "az", "abfs"):
            raise ValueError(f"unsupported URL scheme {p.scheme!r}")
        root = f"{p.scheme}://{p.netloc}"
        if root not in self._stores:
            self._stores[root] = (
                HTTPStore.from_url(root, client_options={"allow_http": p.scheme == "http"})
                if p.scheme in ("http", "https")
                else from_url(root)
            )
        return self._stores[root], p.path.lstrip("/")


def _abs_range(size: int, br: ByteRequest | None) -> tuple[int, int]:
    """Translate a zarr ByteRequest on a value of `size` bytes into [start, end)."""
    if br is None:
        return 0, size
    if isinstance(br, RangeByteRequest):
        if br.start > br.end:
            raise ValueError(f"byte range start {br.start} > end {br.end}")
        return min(br.start, size), min(br.end, size)
    if isinstance(br, OffsetByteRequest):
        return min(br.offset, size), size
    if isinstance(br, SuffixByteRequest):
        return max(size - br.suffix, 0), size
    raise TypeError(f"unexpected byte request {br!r}")


class VZipStore(Store):
    supports_writes = False
    supports_deletes = False
    supports_partial_writes = False
    supports_listing = True

    def __init__(
        self,
        url: str,
        *,
        resolve: bool = True,
        resolver: Resolver | None = None,
        stats: Stats | None = None,
    ) -> None:
        super().__init__(read_only=True)
        # spec §6: absolute, lexically normalised, symlinks not resolved
        self.url = url if urlparse(url).scheme else Path(os.path.abspath(url)).as_uri()
        self.resolve = resolve
        self.resolver = resolver or Resolver()
        self.stats = stats or Stats()
        self._entries: dict[str, Entry] = {}
        self._keys: list[str] = []
        self._sources: list[Source] = []
        self.is_vzip = False
        self._buf, self._buf_start = b"", 0  # bytes fetched while opening
        self._key_cache: dict[str, bytes] = {}  # values of hidden entries used by key ranges
        self._cd_offset = 0
        self._pages: list = []  # CdIndex pages, when the archive has one
        self._page_keys: list[str] = []
        self._loaded: set[int] = set()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, VZipStore) and other.url == self.url

    def __repr__(self) -> str:
        return f"VZipStore({self.url!r}, resolve={self.resolve})"

    # ------------------------------------------------------------ raw IO

    async def _read(
        self, url: str, start: int, end: int, *, external: bool, exact: bool = True
    ) -> bytes:
        """Bytes [start, end) of `url`; with exact=False a short read is allowed."""
        if end <= start:
            return b""
        store, path = self.resolver.resolve(url)
        try:
            data = bytes(await obstore.get_range_async(store, path, start=start, end=end))
        except Exception as e:  # e.g. the range starts past the end of the object
            raise ValueError(f"cannot read [{start}, {end}) of {url}: {e}") from None
        self.stats.record(url, start, len(data), external=external)
        if exact and len(data) != end - start:
            raise ValueError(f"{url} is shorter than {end} bytes")
        return data

    async def _open(self) -> None:
        if self._is_open:
            return
        store, path = self.resolver.resolve(self.url)
        res = await obstore.get_async(store, path, options={"range": {"suffix": TAIL_GUESS}})
        tail = bytes(await res.bytes_async())
        size = res.meta["size"]
        tail_start = size - len(tail)
        self.stats.record(self.url, tail_start, len(tail), external=False)
        d = parse_tail(tail, size)
        self.is_vzip = d.is_vzip
        self._cd_offset = d.cd_offset
        if d.index_offset is not None:
            # Paged: fetch URL table + late entries + CdIndex (contiguous, just before
            # the CD), but not the CD itself; pages are loaded on demand.
            end = d.index_offset + d.index_size
            if d.sources_offset < tail_start:
                head = await self._read(self.url, d.sources_offset, min(end, tail_start), external=False)
                if end >= tail_start:
                    head = head[: tail_start - d.sources_offset] + tail
                self._buf, self._buf_start = head, d.sources_offset
            else:
                self._buf, self._buf_start = tail, tail_start
            index_body = self._slice(d.index_offset, d.index_size, 8)
            sources_body = self._slice(d.sources_offset, d.sources_size, 8)
            pages, pinned = decode_cd_index(index_body)
            # the trailer records are in no page; the comment locates both entries
            for k, off, csize, body in [
                (SOURCES_KEY, d.sources_offset, d.sources_size, sources_body),
                (INDEX_KEY, d.index_offset, d.index_size, index_body),
            ]:
                self._entries[k] = Entry(k, -1, len(body), csize, 8, None, off)
            self._pages = pages
            self._page_keys = [p.first_key for p in pages]
            for e in pinned:
                self._entries[e.key] = Entry(
                    e.key, -1, e.size, e.csize, e.method, None, e.data_offset
                )
            self._sources = read_source_table(self._slice(d.sources_offset, d.sources_size, 8))
        else:
            # Unpaged: the vzip comment locates the URL table, so one more read (at
            # most) gets both it and the whole central directory.
            want = d.cd_offset if d.sources_offset is None else min(d.cd_offset, d.sources_offset)
            if want < tail_start:
                head = await self._read(self.url, want, tail_start, external=False)
                tail, tail_start = head + tail, want
            self._buf, self._buf_start = tail, tail_start
            cd = self._slice(d.cd_offset, d.cd_size, 0)
            self._entries = parse_central_directory(cd, trust_offsets=True)
            if INDEX_KEY in self._entries:
                raise ValueError("__vz__/index entry in an archive without a page index")
            if d.sources_offset is not None:
                self._sources = read_source_table(self._slice(d.sources_offset, d.sources_size, 8))
            elif SOURCES_KEY in self._entries:
                e = self._entries[SOURCES_KEY]
                self._sources = read_source_table(await self._entry_bytes(e, 0, e.size))
        if any(src.url == "" for src in self._sources):
            raise ValueError("empty url in the source table")
        self._refresh_keys()
        await super()._open()

    def _slice(self, offset: int, n: int, method: int) -> bytes:
        lo = offset - self._buf_start
        raw = self._buf[lo : lo + n]
        return zlib.decompress(raw, -15) if method == 8 else raw

    def _refresh_keys(self) -> None:
        self._keys = sorted(
            k for k in self._entries if self.resolve is False or not k.startswith(RESERVED_PREFIX)
        )

    async def _load_pages(self, idxs: Iterable[int]) -> None:
        todo = [i for i in idxs if i not in self._loaded]
        if not todo:
            return
        datas = await asyncio.gather(*(
            self._read(self.url, self._cd_offset + self._pages[i].offset,
                       self._cd_offset + self._pages[i].offset + self._pages[i].length,
                       external=False)
            for i in todo
        ))
        for i, data in zip(todo, datas):
            for k, e in parse_central_directory(data, trust_offsets=True).items():
                self._entries.setdefault(k, e)
            self._loaded.add(i)
        self._refresh_keys()

    async def _lookup(self, key: str) -> None:
        """Make sure `key`'s central directory record is loaded, if it exists."""
        if key in self._entries or not self._pages:
            return
        i = bisect.bisect_right(self._page_keys, key) - 1
        if i >= 0:
            await self._load_pages([i])

    async def _load_prefix(self, prefix: str) -> None:
        if not self._pages:
            return
        lo = max(bisect.bisect_right(self._page_keys, prefix) - 1, 0)
        hi = bisect.bisect_left(self._page_keys, prefix + "\U0010ffff")
        await self._load_pages(range(lo, max(hi, lo + 1)))

    async def _entry_bytes(self, e: Entry, start: int, end: int) -> bytes:
        """Bytes [start, end) of an entry's *body* (the naive view)."""
        if (
            e.data_offset is not None
            and self._buf_start <= e.data_offset
            and e.data_offset + e.csize <= self._buf_start + len(self._buf)
        ):
            # already fetched by _open (e.g. metadata written with late=True)
            lo = e.data_offset - self._buf_start
            raw = self._buf[lo : lo + e.csize]
            return (zlib.decompress(raw, -15) if e.method == 8 else raw)[start:end]
        if e.method == 8:  # deflated: inflate the whole entry
            if e.data_offset is None:
                await self._locate(e)
            raw = await self._read(self.url, e.data_offset, e.data_offset + e.csize, external=False)
            return zlib.decompress(raw, -15)[start:end]
        if e.method != 0:
            raise NotImplementedError(f"zip compression method {e.method}")
        if e.data_offset is None:
            await self._locate(e)
        return await self._read(self.url, e.data_offset + start, e.data_offset + end, external=False)

    async def _locate(self, e: Entry) -> None:
        hdr = await self._read(
            self.url, e.header_offset, e.header_offset + LFH_SIZE + 1024, external=False,
            exact=False,
        )
        e.data_offset = e.header_offset + parse_local_header(hdr)

    async def _key_bytes(self, key: str, start: int, end: int) -> bytes:
        """Bytes [start, end) of another entry's value (a `key` source, spec §6).

        The target must be a bytes entry. Hidden entries (shard indexes etc.) are
        cached whole; other STORED entries are range-read directly.
        """
        if key not in self._key_cache:
            await self._lookup(key)
            e = self._entries.get(key)
            if e is None:
                raise ValueError(f"key source {key!r} is missing")
            if key in (SOURCES_KEY, INDEX_KEY):
                raise ValueError(f"key source {key!r} names a format entry")
            if e.error:
                raise ValueError(f"key source {key!r} has an entry error: {e.error}")
            if e.is_ref:
                raise ValueError(f"key source {key!r} is a reference entry")
            if end > e.size:
                raise ValueError(f"key source {key!r} is shorter than {end} bytes")
            if not key.startswith(RESERVED_PREFIX) and e.method == 0:
                return await self._entry_bytes(e, start, end)
            self._key_cache[key] = await self._entry_bytes(e, 0, e.size)
        data = self._key_cache[key]
        if end > len(data):
            raise ValueError(f"key source {key!r} is shorter than {end} bytes")
        return data[start:end]

    async def _ref_bytes(self, ref: Reference, start: int, end: int) -> bytes:
        """Bytes [start, end) of the value described by `ref`."""
        for r in parts(ref):  # payload errors: every range, even ones outside [start, end)
            if r.data is None and r.source >= len(self._sources):
                raise ValueError(f"range uses source {r.source}; the table has {len(self._sources)}")
        pieces = []
        pos = 0
        for r in parts(ref):
            lo, hi = max(start, pos), min(end, pos + r.size)
            if lo < hi:
                a, b = r.offset + lo - pos, r.offset + hi - pos
                if r.data is not None:
                    pieces.append(r.data[lo - pos : hi - pos])
                    pos += r.size
                    continue
                src = self._sources[r.source]
                if src.data is not None:
                    if b > len(src.data):
                        raise ValueError(f"data source {r.source} is shorter than {b} bytes")
                    pieces.append(src.data[a:b])
                elif src.key is not None:
                    pieces.append(self._key_bytes(src.key, a, b))
                else:  # relative URLs resolve against the archive
                    url = resolve_reference(self.url, src.url)
                    pieces.append(self._read(url, a, b, external=True))
            pos += r.size
        coros = [p for p in pieces if not isinstance(p, bytes)]
        # key ranges are coroutines too; gather runs them alongside external reads
        done = iter(await asyncio.gather(*coros))
        return b"".join(p if isinstance(p, bytes) else next(done) for p in pieces)

    # ------------------------------------------------------------ zarr API

    def classify(self, key: str) -> str | None:
        """The overlay's decision function: 'bytes', 'ref', or None (spec §4.1).

        Raises ValueError for an entry error (spec §8.4).
        """
        e = self._entries.get(key)
        if e is None or (self.resolve and key.startswith(RESERVED_PREFIX)):
            return None
        if e.error:
            raise ValueError(f"{key!r}: {e.error}")
        return "ref" if (self.resolve and e.is_ref) else "bytes"

    async def get(
        self, key: str, prototype: BufferPrototype, byte_range: ByteRequest | None = None
    ) -> Buffer | None:
        await self._ensure_open()
        _abs_range(0, byte_range)  # reject malformed requests even for missing keys
        await self._lookup(key)
        kind = self.classify(key)
        if kind is None:
            return None
        e = self._entries[key]
        if kind == "ref":
            start, end = _abs_range(e.ref.size, byte_range)
            data = await self._ref_bytes(e.ref, start, end)
        else:
            start, end = _abs_range(e.size, byte_range)
            data = await self._entry_bytes(e, start, end)
        return prototype.buffer.from_bytes(data)

    async def get_partial_values(
        self,
        prototype: BufferPrototype,
        key_ranges: Iterable[tuple[str, ByteRequest | None]],
    ) -> list[Buffer | None]:
        return await asyncio.gather(*(self.get(k, prototype, br) for k, br in key_ranges))

    async def exists(self, key: str) -> bool:
        await self._ensure_open()
        await self._lookup(key)
        return self.classify(key) is not None

    async def set(self, key: str, value: Buffer) -> None:
        raise PermissionError("VZipStore is read-only")

    async def delete(self, key: str) -> None:
        raise PermissionError("VZipStore is read-only")

    async def set_partial_values(self, key_start_values) -> None:
        raise PermissionError("VZipStore is read-only")

    async def list(self) -> AsyncIterator[str]:
        await self._ensure_open()
        await self._load_prefix("")
        for k in self._keys:
            yield k

    async def list_prefix(self, prefix: str) -> AsyncIterator[str]:
        await self._ensure_open()
        await self._load_prefix(prefix)
        i = bisect.bisect_left(self._keys, prefix)
        while i < len(self._keys) and self._keys[i].startswith(prefix):
            yield self._keys[i]
            i += 1

    async def list_dir(self, prefix: str) -> AsyncIterator[str]:
        prefix = prefix.rstrip("/")
        prefix = prefix + "/" if prefix else ""
        seen: set[str] = set()
        async for k in self.list_prefix(prefix):
            child = k[len(prefix) :].split("/", 1)[0]
            if child not in seen:
                seen.add(child)
                yield child

    # ------------------------------------------------------------ helpers

    @property
    def sources(self) -> list[Source]:
        return list(self._sources)

    def entry(self, key: str) -> Entry | None:
        return self._entries.get(key)
