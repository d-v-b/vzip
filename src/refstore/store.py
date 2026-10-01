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
import datetime
import math
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
    inflate_clean,
    parse_tail,
    read_source_table,
)
from refstore.errors import (
    ArchiveError,
    BodyError,
    EntryError,
    PayloadError,
    RequestError,
    ResolutionError,
    VzipError,
)
from refstore.pb import Reference, Source, decode_cd_index, parts
from refstore.uri import file_path, file_uri
from refstore.uri import resolve as resolve_reference


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


class Resolver:
    """Map URLs to (obstore store, path). Pre-register stores for credentials."""

    def __init__(self, stores: dict[str, object] | None = None) -> None:
        self._stores: dict[str, object] = dict(stores or {})

    def resolve(self, url: str) -> tuple[object, str]:
        p = urlparse(url)
        if p.scheme == "file":
            return self._stores.setdefault("file://", LocalStore()), file_path(url)
        if p.scheme not in ("http", "https", "s3", "gs", "az", "abfs"):
            raise ValueError(f"unsupported URL scheme {p.scheme!r}")
        root = f"{p.scheme}://{p.netloc}"
        if root not in self._stores:
            self._stores[root] = (
                HTTPStore.from_url(root, client_options={"allow_http": p.scheme == "http"})
                if p.scheme in ("http", "https")
                else from_url(root)
            )
        # object_store paths are decoded; it percent-encodes them itself when building URLs
        return self._stores[root], unquote(p.path).lstrip("/")


def _abs_range(size: int, br: ByteRequest | None) -> tuple[int, int]:
    """Translate a zarr ByteRequest on a value of `size` bytes into [start, end)."""
    if br is None:
        return 0, size
    if isinstance(br, RangeByteRequest):
        if br.start > br.end:
            raise RequestError(f"byte range start {br.start} > end {br.end}")
        return min(br.start, size), min(br.end, size)
    if isinstance(br, OffsetByteRequest):
        return min(br.offset, size), size
    if isinstance(br, SuffixByteRequest):
        return max(size - br.suffix, 0), size
    raise TypeError(f"unexpected byte request {br!r}")


def http_range(url: str, start: int, end: int, src: Source) -> tuple[bytes, int | None]:
    """Bytes [start, end) of an http(s) object, and its size if known (spec §6.2)."""
    import email.utils
    import urllib.error
    import urllib.request

    _check_http_url(url)
    headers = {"Range": f"bytes={start}-{end - 1}", "Accept-Encoding": "identity"}
    if src.etag is not None:
        headers["If-Match"] = src.etag
    if src.modified_not_after is not None:
        t = src.modified_not_after
        if not -62135596800 <= t <= 253402300799:  # years 1..9999
            raise ResolutionError("modified_not_after cannot be written as an HTTP-date")
        when = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(
            seconds=t)
        headers["If-Unmodified-Since"] = email.utils.format_datetime(when, usegmt=True)
    class Redirects(urllib.request.HTTPRedirectHandler):
        max_redirections = 5  # spec §6.2

        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            from refstore.uri import is_uri_reference

            if not is_uri_reference(hdrs.get("Location", "")):
                raise ResolutionError(f"redirect with an invalid Location: {hdrs.get('Location')!r}")
            if urlparse(newurl).scheme.lower() not in ("http", "https"):
                raise ResolutionError(f"redirect to a non-http URL: {newurl}")
            if len(hdrs.get_all("Location") or []) > 1:
                raise ResolutionError("redirect with more than one Location field")
            _check_http_url(newurl)
            return super().redirect_request(req, fp, code, msg, hdrs, newurl)

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.build_opener(Redirects).open(req, timeout=60) as r:
            status, body = r.status, r.read()
            for name in ("Content-Range", "ETag", "Last-Modified"):
                if len(r.headers.get_all(name) or []) > 1:
                    raise ResolutionError(f"{url}: more than one {name} field")
            encs = r.headers.get_all("Content-Encoding") or []
            enc = ", ".join(e.strip() for e in encs).lower() if encs else "identity"
            crange = r.headers.get("Content-Range")
            etag, last_modified = r.headers.get("ETag"), r.headers.get("Last-Modified")
    except urllib.error.HTTPError as e:
        what = {412: "a pin failed (412)", 416: "the object is shorter than the range (416)"}
        raise ResolutionError(f"{url}: {what.get(e.code, f'HTTP {e.code}')}") from None
    except ResolutionError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ResolutionError(f"{url}: {type(e).__name__}: {e}") from None
    # spec §6.2: servers may ignore conditional headers, so check the response itself
    if src.etag is not None and etag != src.etag:
        raise ResolutionError(f"{url}: ETag {etag!r} does not match the pin {src.etag!r}")
    if src.modified_not_after is not None:
        lm = imf_fixdate(last_modified)
        if lm is None:
            raise ResolutionError(f"{url}: Last-Modified {last_modified!r} is not an IMF-fixdate")
        if lm > src.modified_not_after:
            raise ResolutionError(f"{url}: Last-Modified {last_modified} is after the pin")
    if enc != "identity":
        raise ResolutionError(f"{url}: response has Content-Encoding {enc!r}")
    if status == 200:  # the server ignored Range
        if len(body) < end:
            raise ResolutionError(f"{url} is shorter than {end} bytes")
        return body[start:end], len(body)
    if status != 206 or not crange:
        raise ResolutionError(f"{url}: unexpected response {status} {crange!r}")
    m = re.fullmatch(r"(?i:bytes) (\d+)-(\d+)/(\d+|\*)", crange.strip())
    if not m:
        raise ResolutionError(f"{url}: invalid Content-Range {crange!r}")
    a, z, total = int(m[1]), int(m[2]), m[3]
    if a != start or z != end - 1 or len(body) != z - a + 1 or (total != "*" and z >= int(total)):
        raise ResolutionError(f"{url}: server returned {crange!r} ({len(body)} bytes) for "
                              f"[{start}, {end})")
    return body, None if total == "*" else int(total)


def _check_http_url(url: str) -> None:
    """spec §6.2: no userinfo, a non-empty host, a port of at most 65535."""
    pu = urlparse(url)
    if "@" in pu.netloc or not pu.hostname:
        raise ResolutionError(f"{url}: userinfo or empty host in an http(s) URL")
    port = pu.netloc.rpartition(":")[2] if ":" in pu.netloc.rsplit("]", 1)[-1] else ""
    if port and (not port.isdigit() or int(port) > 65535):
        raise ResolutionError(f"{url}: invalid port {port!r}")


_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def imf_fixdate(v: str | None) -> int | None:
    """Seconds since the epoch for an IMF-fixdate (RFC 9110 §5.6.7), else None."""
    m = re.fullmatch(r"(\w{3}), (\d{2}) (\w{3}) (\d{4}) (\d{2}):(\d{2}):(\d{2}) GMT", v or "")
    if not m or m[1] not in _DAYS or m[3] not in _MONTHS:
        return None
    leap = int(m[7]) == 60  # a leap second counts as the following second (§6.2)
    try:
        t = datetime.datetime(int(m[4]), _MONTHS.index(m[3]) + 1, int(m[2]), int(m[5]),
                              int(m[6]), 59 if leap else int(m[7]), tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    if _DAYS[t.weekday()] != m[1]:
        return None
    if leap:
        t += datetime.timedelta(seconds=1)
    return int((t - datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)).total_seconds())


def _check_index(pages, pinned, cd_size: int, file_size: int) -> None:
    """Spec §7.2: a malformed page index is an archive error."""
    pos, prev = 0, None
    for p in pages:
        if not p.first_key:
            raise ArchiveError("a page has an empty first_key")
        if p.length == 0 or p.offset != pos or p.offset + p.length > cd_size:
            raise ArchiveError("pages are not contiguous, empty, or outside the directory")
        if prev is not None and p.first_key.encode() <= prev.encode():
            raise ArchiveError("page first_key values do not strictly increase")
        pos, prev = p.offset + p.length, p.first_key
    seen = set()
    for e in pinned:
        if not e.key:
            raise ArchiveError("a pinned key is empty")
        if e.key in seen or e.key in (SOURCES_KEY, INDEX_KEY):
            raise ArchiveError(f"pinned key {e.key!r} is duplicated or a format entry")
        if e.method not in (0, 8):
            raise ArchiveError(f"pinned {e.key!r} has method {e.method}")
        if e.data_offset + e.csize > file_size:
            raise ArchiveError(f"pinned {e.key!r} lies outside the file")
        seen.add(e.key)


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
        self.url = url if urlparse(url).scheme else file_uri(url)
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
        self._bad_pages: dict[int, str] = {}
        self._file_size = 0

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
        try:
            await self._open_archive()
        except ArchiveError:
            raise
        except Exception as e:  # noqa: BLE001 - spec §8.1: every open failure
            raise ArchiveError(f"{type(e).__name__}: {e}") from None
        self._refresh_keys()
        await super()._open()

    async def _open_archive(self) -> None:
        store, path = self.resolver.resolve(self.url)
        res = await obstore.get_async(store, path, options={"range": {"suffix": TAIL_GUESS}})
        tail = bytes(await res.bytes_async())
        size = res.meta["size"]
        tail_start = size - len(tail)
        self.stats.record(self.url, tail_start, len(tail), external=False)
        d = parse_tail(tail, size)
        self.is_vzip = d.is_vzip
        self._cd_offset = d.cd_offset
        self._file_size = size
        if d.cd_offset + d.cd_size > size:
            raise ArchiveError("central directory lies outside the file")
        for off, n in [(d.sources_offset, d.sources_size), (d.index_offset or 0, d.index_size)]:
            if off + n > size:
                raise ArchiveError("a format entry's body lies outside the file")
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
            _check_index(pages, pinned, d.cd_size, size)
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
        if any(src.url == "" or src.key == "" for src in self._sources):
            raise ValueError("empty url or key in the source table")

    def _slice(self, offset: int, n: int, method: int) -> bytes:
        lo = offset - self._buf_start
        raw = self._buf[lo : lo + n]
        return inflate_clean(raw) if method == 8 else raw

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
            try:
                parsed = parse_central_directory(data, trust_offsets=True)
            except ValueError as e:
                self._bad_pages[i] = str(e)
                parsed = {}
            for k, e in parsed.items():
                if bisect.bisect_right(self._page_keys, k) - 1 == i:  # spec §7.2
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
            if i in self._bad_pages:
                raise EntryError(f"page {i}, which holds {key!r}, cannot be parsed: "
                                 f"{self._bad_pages[i]}")

    async def _load_prefix(self, prefix: str) -> None:
        if not self._pages:
            return
        # spec §8.2: page [lo, hi) is read iff (no hi or prefix < hi) and
        # (lo <= prefix or lo starts with prefix), comparing UTF-8 bytes
        pb = prefix.encode()
        keys = [k.encode() for k in self._page_keys]
        idxs = [i for i, lo in enumerate(keys)
                if (i + 1 == len(keys) or pb < keys[i + 1]) and (lo <= pb or lo.startswith(pb))]
        await self._load_pages(idxs)
        bad = [i for i in idxs if i in self._bad_pages]
        if bad:
            raise EntryError(f"pages {bad} cannot be parsed")

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
            return (inflate_clean(raw) if e.method == 8 else raw)[start:end]
        if e.method == 8:  # deflated: inflate the whole entry
            if e.data_offset is None:
                await self._locate(e)
            raw = await self._read(self.url, e.data_offset, e.data_offset + e.csize, external=False)
            return inflate_clean(raw)[start:end]
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

    async def _body(self, e: Entry, start: int, end: int) -> bytes:
        """A bytes entry's (inflated) body slice; failures are body errors (spec §8.4)."""
        try:
            if e.method == 0 and e.csize != e.size:
                raise ValueError(f"STORED entry with sizes {e.csize} != {e.size}")
            if e.method == 8:
                data = await self._entry_bytes(e, 0, 1 << 64)  # whole inflated body
                if len(data) != e.size:
                    raise ValueError(f"inflates to {len(data)} bytes, record says {e.size}")
                return data[start:end]
            return await self._entry_bytes(e, start, end)
        except VzipError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BodyError(f"{e.name!r}: {type(exc).__name__}: {exc}") from None

    async def _key_bytes(self, key: str, start: int, end: int) -> bytes:
        """Bytes [start, end) of another entry's value (a `key` source, spec §6).

        The target must be a bytes entry. Hidden entries (shard indexes etc.) are
        cached whole; other STORED entries are range-read directly.
        """
        if key not in self._key_cache:
            await self._lookup(key)
            e = self._entries.get(key)
            if e is None:
                raise ResolutionError(f"key source {key!r} is missing")
            if key in (SOURCES_KEY, INDEX_KEY):
                raise ResolutionError(f"key source {key!r} names a format entry")
            if e.error:
                raise ResolutionError(f"key source {key!r} has an entry error: {e.error}")
            if e.is_ref:
                raise ResolutionError(f"key source {key!r} is a reference entry")
            if end > e.size:
                raise ResolutionError(f"key source {key!r} is shorter than {end} bytes")
            if not key.startswith(RESERVED_PREFIX) and e.method == 0:
                return await self._body(e, start, end)
            self._key_cache[key] = await self._body(e, 0, e.size)
        data = self._key_cache[key]
        if end > len(data):
            raise ResolutionError(f"key source {key!r} is shorter than {end} bytes")
        return data[start:end]

    async def _url_bytes(self, src: Source, start: int, end: int) -> bytes:
        """Bytes [start, end) of a url source, checking its pins (spec §6.1)."""
        url = resolve_reference(self.url, src.url)
        if urlparse(url).scheme.lower() in ("http", "https"):  # spec §6.2, pinned or not
            data, size = await asyncio.to_thread(http_range, url, start, end, src)
            self.stats.record(url, start, len(data), external=True)
            if src.size is not None and size != src.size:
                raise ResolutionError(f"size pin {src.size} != {size}")
            return data
        if not src.pinned:
            return await self._read(url, start, end, external=True)
        if urlparse(url).scheme.lower() == "file":
            path = file_path(url)
            st = os.stat(path)
            if src.etag is not None:
                raise ResolutionError("an etag pin cannot be checked for a file: URL")
            if src.size is not None and st.st_size != src.size:
                raise ResolutionError(f"size pin {src.size} != {st.st_size}")
            if (src.modified_not_after is not None
                    and math.floor(st.st_mtime) > src.modified_not_after):
                raise ResolutionError("modified_not_after pin failed: the file changed")
            return await self._read(url, start, end, external=True)
        store, path = self.resolver.resolve(url)
        options: dict = {"range": (start, end)}
        if src.etag is not None:
            options["if_match"] = src.etag
        if src.modified_not_after is not None:
            options["if_unmodified_since"] = datetime.datetime.fromtimestamp(
                src.modified_not_after, datetime.timezone.utc)
        try:
            res = await obstore.get_async(store, path, options=options)
            data = bytes(await res.bytes_async())
        except Exception as exc:  # noqa: BLE001 - 412 Precondition Failed and friends
            raise ResolutionError(f"pin check or read failed for {url}: {exc}") from None
        self.stats.record(url, start, len(data), external=True)
        if src.size is not None and res.meta["size"] != src.size:
            raise ResolutionError(f"size pin {src.size} != {res.meta['size']}")
        if len(data) != end - start:
            raise ResolutionError(f"{url} is shorter than {end} bytes")
        return data

    async def _ref_bytes(self, ref: Reference, start: int, end: int) -> bytes:
        """Bytes [start, end) of the value described by `ref` (spec §8.3)."""
        for r in parts(ref):  # payload errors: every range, even ones outside [start, end)
            if r.data is None and r.source >= len(self._sources):
                raise PayloadError(f"range uses source {r.source}; the table has "
                                   f"{len(self._sources)}")
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
                        raise ResolutionError(f"data source {r.source} is shorter than {b} bytes")
                    pieces.append(src.data[a:b])
                elif src.key is not None:
                    pieces.append(self._key_bytes(src.key, a, b))
                else:
                    pieces.append(self._url_bytes(src, a, b))
            pos += r.size
        coros = [p for p in pieces if not isinstance(p, bytes)]
        try:
            done = iter(await asyncio.gather(*coros))
        except ResolutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - spec §8.4: failures resolving a range
            raise ResolutionError(f"{type(exc).__name__}: {exc}") from None
        return b"".join(p if isinstance(p, bytes) else next(done) for p in pieces)

    # ------------------------------------------------------------ zarr API

    def classify(self, key: str) -> str | None:
        """The overlay's decision function: 'bytes', 'ref', or None (spec §4.1).

        Raises EntryError for an entry error (spec §8.4).
        """
        e = self._entries.get(key)
        if e is None or (self.resolve and key.startswith(RESERVED_PREFIX)):
            return None
        if e.error:
            raise EntryError(f"{key!r}: {e.error}")
        return "ref" if (self.resolve and e.is_ref) else "bytes"

    async def kind(self, key: str) -> str | None:
        """classify() with the lookup, in the order of spec §8.4."""
        await self._ensure_open()
        if self.resolve and key.startswith(RESERVED_PREFIX):
            return None
        await self._lookup(key)
        return self.classify(key)

    async def get(
        self, key: str, prototype: BufferPrototype, byte_range: ByteRequest | None = None
    ) -> Buffer | None:
        await self._ensure_open()
        _abs_range(0, byte_range)  # 1. request errors, even for missing keys
        kind = await self.kind(key)  # 2-3. hidden, lookup, entry error
        if kind is None:
            return None
        e = self._entries[key]
        if kind == "ref":
            try:
                ref = e.ref
            except ValueError as exc:
                raise PayloadError(f"{key!r}: {exc}") from None
            start, end = _abs_range(ref.size, byte_range)
            data = await self._ref_bytes(ref, start, end)
        else:
            start, end = _abs_range(e.size, byte_range)
            data = await self._body(e, start, end)
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
