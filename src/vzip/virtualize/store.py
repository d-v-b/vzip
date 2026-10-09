"""Store inputs (VIRTUALIZE.md §1.4–§1.6): the listing, object reads, the
strict JSON reader, and the output of a store profile (one url source per
chunk object).
"""

from __future__ import annotations

import http.client
import json
import os
import re
import stat
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from vzip.errors import ResolutionError
from vzip.policy import Policy
from vzip.uri import is_uri_reference
from vzip.virtualize.common import CONVENTION_KEY, Rejected, _retry, declare

MAX_SAFE = 2**53 - 1
MAX_DOCUMENT = 1 << 24
MAX_DEPTH = 256
UA = "vzip-virtualize"

_SPLIT = re.compile(r"^(?:([^:/?#]+):)?(?://([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#(.*))?\Z")
_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PATH_SAFE = _UNRESERVED | frozenset(b"!$&'()*+,;=:@/")


class ListingFailed(OSError):
    """The listing could not be read (a network error or a 408/429/5xx status): a failure, §1.4."""


class StoreLimit(OSError):
    """An implementation's resource limit (§1.2, §14): a failure, not a rejection."""


# ---------------------------------------------------------------- URLs (§1.4, §1.5)


def _pct(data: bytes, safe: frozenset) -> str:
    return "".join(chr(b) if b in safe else f"%{b:02X}" for b in data)


def object_url(store_url: str, key: str) -> str:
    """The URL of the object with relative key `key` (§1.4)."""
    return store_url + _pct(key.encode(), _PATH_SAFE)


def query_encode(s: str) -> str:
    """`enc` of §1.5: every byte but the unreserved ones percent-encoded."""
    return _pct(s.encode(), _UNRESERVED)


def check_store_url(url: str) -> tuple[str, str, str]:
    """(scheme, authority, path) of a valid store URL (§1.4), or Rejected."""
    m = _SPLIT.match(url)
    if not is_uri_reference(url) or m is None:
        raise Rejected(f"{url!r} is not a valid URI")
    scheme, authority, path, query, fragment = m.groups()
    if scheme is None or scheme.lower() not in ("http", "https"):
        raise Rejected(f"store URL {url!r} is not http or https")
    if authority is None or "@" in authority:
        raise Rejected(f"store URL {url!r} has no authority, or has userinfo")
    if query is not None or fragment is not None:
        raise Rejected(f"store URL {url!r} has a query or a fragment")
    if not path.endswith("/"):
        raise Rejected(f"store URL {url!r} does not end in /")
    return scheme, authority, path


def _percent_decode(s: str) -> bytes:
    out = bytearray()
    i = 0
    while i < len(s):
        if s[i] == "%":
            out.append(int(s[i + 1 : i + 3], 16))
            i += 3
        else:
            out.append(ord(s[i]))
            i += 1
    return bytes(out)


def listing_endpoint(url: str) -> tuple[str, str]:
    """(endpoint, prefix P) of a store URL (§1.5)."""
    scheme, authority, path = check_store_url(url)
    host = authority
    if host.startswith("["):
        host = host[: host.index("]") + 1]
    else:
        host = host.rsplit(":", 1)[0] if ":" in host else host
    host = host.lower()
    labels = host.split(".")
    virtual = host.endswith(".amazonaws.com") and any(
        lab == "s3" or lab.startswith("s3-") for lab in labels[1:])
    if virtual:
        endpoint, prefix_path = f"{scheme}://{authority}/", path[1:]
    else:
        if path == "/":
            raise Rejected(f"path-style store URL {url!r} names no bucket")
        bucket, _, rest = path[1:].partition("/")
        if not bucket:
            raise Rejected(f"path-style store URL {url!r} has an empty bucket")
        endpoint, prefix_path = f"{scheme}://{authority}/{bucket}/", rest
    try:
        prefix = _percent_decode(prefix_path).decode("utf-8")
    except UnicodeDecodeError:
        raise Rejected(f"the key prefix of {url!r} is not UTF-8") from None
    return endpoint, prefix


# ---------------------------------------------------------------- listing bodies (§1.5)


def _is_char(c: int) -> bool:
    return c in (0x9, 0xA, 0xD) or 0x20 <= c <= 0xD7FF or 0xE000 <= c <= 0xFFFD or 0x10000 <= c <= 0x10FFFF


_NAME_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_:")
_NAME_CHAR = _NAME_START | frozenset("0123456789.-")
_WS = frozenset(" \t\r\n")
_ENTITIES = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}
_HEX = frozenset("0123456789abcdefABCDEF")
_DEC = frozenset("0123456789")


@dataclass
class Element:
    name: str
    children: list[Element] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    # The attributes, (name, value) in document order, values with references replaced.
    attributes: list[tuple[str, str]] = field(default_factory=list)


class _Xml:
    """The XML subset of §1.5. `what` names the document in rejections."""

    def __init__(self, s: str, what: str = "listing") -> None:
        self.s = s
        self.pos = 0
        self.what = what
        self.depth = 0  # of the element being read

    def fail(self, why: str):
        raise Rejected(f"{self.what} is not well formed: {why} at {self.pos}")

    def ws(self) -> None:
        s, n = self.s, len(self.s)
        while self.pos < n and s[self.pos] in _WS:
            self.pos += 1

    def at(self, t: str) -> bool:
        return self.s.startswith(t, self.pos)

    def comment(self) -> None:
        end = self.s.find("--", self.pos + 4)
        if end < 0 or not self.s.startswith("-->", end):
            self.fail("bad comment")
        self.chars(self.s[self.pos + 4 : end])
        self.pos = end + 3

    def misc(self) -> None:
        while True:
            self.ws()
            if self.at("<!--"):
                self.comment()
            else:
                return

    def chars(self, t: str) -> None:
        for ch in t:
            if not _is_char(ord(ch)):
                self.fail(f"character U+{ord(ch):04X}")

    def name(self) -> str:
        s, start = self.s, self.pos
        if start >= len(s) or s[start] not in _NAME_START:
            self.fail("expected a name")
        self.pos += 1
        while self.pos < len(s) and s[self.pos] in _NAME_CHAR:
            self.pos += 1
        return s[start : self.pos]

    def reference(self) -> str:
        s = self.s
        end = s.find(";", self.pos)
        if end < 0:
            self.fail("unterminated reference")
        body = s[self.pos + 1 : end]
        self.pos = end + 1
        if body in _ENTITIES:
            return _ENTITIES[body]
        if body.startswith("#x") and len(body) > 2 and all(c in _HEX for c in body[2:]):
            v = int(body[2:], 16) if len(body) < 12 else -1
        elif body.startswith("#") and len(body) > 1 and all(c in _DEC for c in body[1:]):
            v = int(body[1:]) if len(body) < 12 else -1
        else:
            self.fail(f"unknown reference &{body};")
        if not _is_char(v):
            self.fail(f"reference &{body}; is not a character")
        return chr(v)

    def chardata(self, stop: str) -> str:
        """Text up to `<` or `&` (or the quote `stop`), checked."""
        s, start = self.s, self.pos
        while self.pos < len(s) and s[self.pos] not in "<&" and s[self.pos] != stop:
            self.pos += 1
        t = s[start : self.pos]
        self.chars(t)
        return t

    def element(self) -> Element:
        if not self.at("<"):
            self.fail("expected an element")
        self.depth += 1
        if self.depth > MAX_DEPTH:
            self.fail(f"elements nest more than {MAX_DEPTH} deep")
        el = self._element()
        self.depth -= 1
        return el

    def _element(self) -> Element:
        self.pos += 1
        el = Element(self.name())
        s = self.s
        while True:
            if self.pos < len(s) and s[self.pos] in _WS:
                self.ws()
                if self.pos < len(s) and s[self.pos] in _NAME_START:
                    name = self.name()
                    self.ws()
                    if not self.at("="):
                        self.fail("expected =")
                    self.pos += 1
                    self.ws()
                    q = s[self.pos] if self.pos < len(s) else ""
                    if q not in ("'", '"'):
                        self.fail("expected a quoted value")
                    self.pos += 1
                    value = []
                    while True:
                        value.append(self.chardata(q))
                        if self.at("&"):
                            value.append(self.reference())
                        elif self.at(q):
                            self.pos += 1
                            break
                        else:
                            self.fail("bad attribute value")
                    el.attributes.append((name, "".join(value)))
                    continue
            break
        if self.at("/>"):
            self.pos += 2
            return el
        if not self.at(">"):
            self.fail("expected >")
        self.pos += 1
        while True:
            if self.pos >= len(s):
                self.fail("unterminated element")
            if self.at("</"):
                self.pos += 2
                if self.name() != el.name:
                    self.fail("mismatched end tag")
                self.ws()
                if not self.at(">"):
                    self.fail("expected >")
                self.pos += 1
                return el
            if self.at("<!--"):
                self.comment()
            elif self.at("<!") or self.at("<?"):
                self.fail("unsupported markup")
            elif self.at("<"):
                el.children.append(self.element())
            elif self.at("&"):
                el.text.append(self.reference())
            else:
                t = self.chardata("<")
                if "]]>" in t:
                    self.fail("]]> in character data")
                el.text.append(t)

    def document(self) -> Element:
        self.ws()
        if self.at("<?xml") and self.pos + 5 < len(self.s) and self.s[self.pos + 5] in _WS:
            end = self.s.find("?>", self.pos + 5)
            if end < 0:
                self.fail("unterminated XML declaration")
            self.chars(self.s[self.pos : end])
            self.pos = end + 2
        self.misc()
        root = self.element()
        self.misc()
        if self.pos != len(self.s):
            self.fail("content after the root element")
        return root


def parse_xml(text: str, what: str) -> Element:
    """The root element of a document in the XML subset of §1.5, or Rejected (naming `what`)."""
    return _Xml(text, what).document()


def _text(el: Element) -> str:
    if el.children:
        raise Rejected(f"listing: <{el.name}> has a child element")
    return "".join(el.text)


def parse_listing(body: bytes, prefix: str) -> tuple[list[tuple[str, int]], str | None]:
    """The objects listed by one ListObjectsV2 response, and the continuation
    token if the listing is truncated (§1.5)."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise Rejected("listing is not UTF-8") from None
    root = _Xml(text).document()
    if root.name != "ListBucketResult":
        raise Rejected(f"listing root element is <{root.name}>, not <ListBucketResult>")
    truncated = [c for c in root.children if c.name == "IsTruncated"]
    if len(truncated) != 1 or _text(truncated[0]) not in ("true", "false"):
        raise Rejected("listing needs exactly one IsTruncated of true or false")
    tokens = [c for c in root.children if c.name == "NextContinuationToken"]
    if len(tokens) > 1:
        raise Rejected("listing has more than one NextContinuationToken")
    token = _text(tokens[0]) if tokens else None
    objects = []
    for c in root.children:
        if c.name != "Contents":
            continue
        keys = [x for x in c.children if x.name == "Key"]
        sizes = [x for x in c.children if x.name == "Size"]
        if len(keys) != 1 or len(sizes) != 1:
            raise Rejected("listing Contents needs exactly one Key and one Size")
        key, size = _text(keys[0]), _text(sizes[0])
        if not size or not all(ch in _DEC for ch in size) or len(size.lstrip("0")) > 16 or int(size) > MAX_SAFE:
            raise Rejected(f"listing Size {size[:40]!r} is not a size")
        if not key.startswith(prefix):
            raise Rejected(f"listed key {key!r} is outside the prefix {prefix!r}")
        objects.append((key, int(size)))
    if _text(truncated[0]) == "true":
        if not token:
            raise Rejected("truncated listing without a NextContinuationToken")
        if not objects:  # a page that lists nothing could be followed forever
            raise Rejected("truncated listing without a Contents")
        return objects, token
    return objects, None


# ---------------------------------------------------------------- JSON (§1.6)


def _bad_constant(name: str):
    raise Rejected(f"JSON literal {name} is not allowed")


def _float(s: str) -> float:
    v = float(s)
    if v in (float("inf"), float("-inf")):
        raise Rejected(f"JSON number {s[:40]} is not finite in binary64")
    return v


def _int(s: str) -> int:
    """An integer literal, kept exact (conventions/n5, zarr2, ome-zarr: copied values keep
    every digit), and rejected when its binary64 value is infinite (§1.6)."""
    try:
        v = int(s)
    except ValueError:  # more digits than Python converts: far beyond binary64
        raise Rejected("JSON number is not finite in binary64") from None
    if not -MAX_SAFE <= v <= MAX_SAFE:
        try:
            float(v)
        except OverflowError:
            raise Rejected("JSON number is not finite in binary64") from None
    return v


_TOKENS = re.compile(r'"(?:[^"\\]|\\.)*"|[\[\]{}]', re.S)


def parse_json(data: bytes):
    """A metadata document by §1.6, or Rejected."""
    if len(data) > MAX_DOCUMENT:
        raise Rejected(f"JSON document of {len(data)} bytes exceeds {MAX_DOCUMENT}")
    if data.startswith(b"\xef\xbb\xbf"):
        raise Rejected("JSON document starts with a byte order mark")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Rejected("JSON document is not UTF-8") from None
    depth = deepest = 0
    for m in _TOKENS.finditer(text):
        t = m.group()
        if t in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif t in "]}":
            depth -= 1
    if deepest > MAX_DEPTH:
        raise Rejected(f"JSON nests more than {MAX_DEPTH} deep")
    try:
        return json.loads(text, parse_constant=_bad_constant, parse_float=_float, parse_int=_int)
    except json.JSONDecodeError as e:
        raise Rejected(f"invalid JSON: {e}") from None


def as_int(v, lo: int = -MAX_SAFE, hi: int = MAX_SAFE) -> int | None:
    """`v` as an integer of §1.6 within [lo, hi], or None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, float):
        if not v.is_integer():
            return None
        v = int(v)
    if not isinstance(v, int) or not -MAX_SAFE <= v <= MAX_SAFE:
        return None
    return v if lo <= v <= hi else None


def is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def num(v):
    """A number as the layout reads it (§1.6): its binary64 value. Integers beyond
    2^53 − 1 are parsed exactly, so that copied values keep their digits; this is
    the value the layout's checks and arithmetic use."""
    return float(v) if isinstance(v, int) and not isinstance(v, bool) and not -MAX_SAFE <= v <= MAX_SAFE else v


# ---------------------------------------------------------------- stores (§1.4, §1.5)


def _ignored(rel: str) -> bool:
    return rel == "" or rel.endswith("/") or any(s in ("", ".", "..") for s in rel.split("/"))


def _folder_marker(rel: str, size: int) -> bool:
    """An ignored object whose key is not recorded (§1.4): an empty one whose relative key is empty or ends in /."""
    return size == 0 and (rel == "" or rel.endswith("/"))


WORKERS = 16
PREFETCH_BYTES = 64 << 20


class _Prefetch:
    """Reads the objects of `plan` (keys, in the order a profile will read them)
    ahead of the profile with up to `workers` threads, holding at most `budget`
    bytes read or being read and not yet taken (but always one object, so that
    a document of MAX_DOCUMENT bytes is read). Each result, the bytes or the
    exception the read raised, is handed over once, by `take`, when the
    profile reads that key; so a failed read fails the profile exactly where
    the sequential read would, and only if the profile reads that key. Each
    thread calls `release`, if given, as it ends (to close the connections it
    kept alive)."""

    def __init__(self, read: Callable[[str], bytes], plan: list[tuple[str, int]], workers: int, budget: int,
                 wanted: Callable[[str], bool | None] | None, release: Callable[[], None] | None = None) -> None:
        self._read, self._budget, self._wanted, self._release = read, budget, wanted, release
        self._queue = [k for k, _ in reversed(plan)]  # popped from the end: the next key in plan order
        self._size = dict(plan)
        self._state: dict[str, str] = {k: "queued" for k, _ in plan}  # queued, running, done
        self._result: dict[str, tuple[bool, object]] = {}
        self._held = 0
        self._closed = False
        self._cv = threading.Condition()
        for _ in range(min(workers, len(plan))):
            threading.Thread(target=self._work, daemon=True).start()

    def _next(self) -> str | None:
        with self._cv:
            while True:
                if self._closed or not self._queue:
                    return None
                key = self._queue[-1]
                if self._state.get(key) != "queued":  # taken by the profile before it was started
                    self._queue.pop()
                    continue
                wanted = True if self._wanted is None else self._wanted(key)
                if wanted is None:  # not known yet: asked again after the profile's next read
                    self._cv.wait()
                    continue
                if not wanted:
                    self._queue.pop()
                    del self._state[key]
                    continue
                size = self._size[key]
                if self._held == 0 or self._held + size <= self._budget:
                    self._queue.pop()
                    self._state[key] = "running"
                    self._held += size
                    return key
                self._cv.wait()

    def _work(self) -> None:
        try:
            while (key := self._next()) is not None:
                try:
                    r = (True, self._read(key))
                except Exception as e:  # handed over as is by take()
                    r = (False, e)
                with self._cv:
                    self._state[key] = "done"
                    self._result[key] = r
                    self._cv.notify_all()
        finally:
            if self._release is not None:
                self._release()

    def take(self, key: str) -> tuple[bool, object] | None:
        """The result of reading `key`, waiting for it; None if `key` was not
        planned, was taken already, or was not started (the caller reads it)."""
        with self._cv:
            self._cv.notify_all()  # the profile has read on: `wanted` may now know more
            state = self._state.pop(key, None)
            if state is None or state == "queued":
                return None
            while key not in self._result:
                self._cv.wait()
            self._held -= self._size[key]
            self._cv.notify_all()
            return self._result.pop(key)

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()


class Store:
    """A listed store: `objects` maps each relative key to its size (§1.4), `ignored`
    holds the relative keys of the ignored objects that are recorded, and `folders`
    those of the empty objects whose keys end in `/` (directories, to the SAFE profile)."""

    url: str
    objects: dict[str, int]
    ignored: list[str]
    folders: list[str]
    listed: int = 0
    requests: int = 0
    workers: int = 1
    prefetch_bytes: int = PREFETCH_BYTES
    _prefetch: _Prefetch | None = None
    _kept: dict[str, tuple[bool, object]] | None = None

    def read(self, key: str) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def read_range(self, key: str, offset: int, length: int) -> bytes:  # pragma: no cover - interface
        """The bytes [offset, offset + length) of the object `key` (profiles/safe.md §12.2)."""
        raise NotImplementedError

    def prefetch(self, keys, wanted: Callable[[str], bool | None] | None = None) -> None:
        """Starts reading the documents `keys`, in that order (the order the
        profile reads them), concurrently if `workers` > 1; once per store.
        `wanted(key)`, asked before a read starts, is False for a key the
        profile will not read after all, and None while that is not known yet
        (asked again after each document the profile reads). Empty documents and documents §1.6 rejects for
        their size are not read."""
        if self.workers <= 1 or self._prefetch is not None:
            return
        plan = [(k, self.objects[k]) for k in dict.fromkeys(keys) if 0 < self.objects[k] <= MAX_DOCUMENT]
        self._prefetch = _Prefetch(self.read, plan, self.workers, self.prefetch_bytes, wanted, self._release)

    def _release(self) -> None:
        """Closes the connections the calling thread keeps alive, if any."""

    def close(self) -> None:
        """Stops reading ahead, and closes the calling thread's kept-alive connections
        (each reading-ahead thread closes its own as it ends). The store can still be
        read afterwards."""
        if self._prefetch is not None:
            self._prefetch.close()
        self._release()

    def document(self, key: str, *, keep: bool = False):
        """The JSON document at `key` (§1.6). With `keep`, its bytes are kept
        for the next read of `key` (each document is read once)."""
        size = self.objects[key]
        if size > MAX_DOCUMENT:
            raise Rejected(f"{key}: JSON document of {size} bytes exceeds {MAX_DOCUMENT}")
        if not size:
            return parse_json(b"")
        if self._kept is None:
            self._kept = {}
        r = self._kept.get(key)
        if r is None and self._prefetch is not None:
            r = self._prefetch.take(key)
        if r is None:
            try:
                r = (True, self.read(key))
            except Exception as e:
                r = (False, e)
        if keep:
            self._kept[key] = r
        else:
            self._kept.pop(key, None)
        ok, value = r
        if not ok:
            raise value
        return parse_json(value)

    def _add(self, listed: list[tuple[str, int]], prefix: str, seen: set[str]) -> None:
        for key, size in listed:
            if key in seen:
                raise Rejected(f"key {key!r} is listed twice")
            seen.add(key)
            rel = key[len(prefix):]
            if not _ignored(rel):
                self.objects[rel] = size
            elif not _folder_marker(rel, size):
                self.ignored.append(rel)
            elif rel and not _ignored(rel[:-1]):
                self.folders.append(rel)
        self.listed = len(seen)


class _ThreadConnections:
    """Kept-alive connections, one per thread and origin, with the interface of
    `vzip.store._Pool`: each thread that reads closes its own (`release`) as it
    ends. A new TLS connection per range costs a round trip or more, which
    dominates the many small reads of a SAFE product's band files."""

    def __init__(self) -> None:
        self._local = threading.local()

    def _conns(self) -> dict:
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = self._local.conns = {}
        return conns

    def get(self, scheme: str, host: str, port: int | None):
        from vzip.store import new_connection

        conn = self._conns().pop((scheme, host, port), None)
        if conn is not None:
            return conn, True
        return new_connection(scheme, host, port, timeout=120), False

    def put(self, scheme: str, host: str, port: int | None, conn) -> None:
        old = self._conns().pop((scheme, host, port), None)
        if old is not None:
            old.close()
        self._conns()[(scheme, host, port)] = conn

    def release(self) -> None:
        conns = self._conns()
        while conns:
            conns.popitem()[1].close()


REDIRECTS = (301, 302, 303, 307, 308)


class HttpStore(Store):
    """A store listed by S3 ListObjectsV2 (§1.5), every request under the reader
    policy (SPEC.md §8.7): the listing's, each object read's (the prefetch
    threads' too), and each redirect target's, which is checked before it is
    requested. Unless the policy has `allow_private_hosts`, the address each
    request is sent to is checked, on a new connection and on a kept-alive one,
    and a request that would go through a proxy is refused unless the policy has
    `allow_unchecked_proxy`."""

    def __init__(self, url: str, *, max_objects: int | None = None,
                 opener: Callable | None = None, workers: int = WORKERS, policy: Policy | None = None) -> None:
        self.workers = workers  # concurrent document reads (prefetch)
        self.policy = policy or Policy()
        # Where the store is listed and read; `url`, the store's name in the
        # output, may be changed afterwards (--url).
        self.location = url
        self.url = url
        self.endpoint, self.prefix = listing_endpoint(url)
        self.objects = {}
        self.ignored = []
        self.folders = []
        # A test's urlopen, in place of the network: the policy then checks URLs as written.
        self._opener = opener
        self._conns = _ThreadConnections()
        seen: set[str] = set()
        tokens: set[str] = set()
        token = None
        while True:
            q = f"?list-type=2&prefix={query_encode(self.prefix)}"
            if token is not None:
                q += f"&continuation-token={query_encode(token)}"
            body = self._get_listing(self.endpoint + q)
            listed, token = parse_listing(body, self.prefix)
            self._add(listed, self.prefix, seen)
            if max_objects is not None and self.listed > max_objects:
                raise StoreLimit(f"the store lists more than {max_objects} objects")
            if token is None:
                break
            if token in tokens:  # the listing would repeat itself (§1.5)
                raise Rejected(f"the listing gives the continuation token {token[:80]!r} again")
            tokens.add(token)

    def _send(self, url: str, headers: dict[str, str]):
        """One GET of `url`, not following redirects: (status, headers, body). A
        network error is a URLError, as urllib's; a refusal a ResolutionError."""
        if self._opener is not None:  # it follows redirects, and raises HTTPError, as urlopen does
            with self._opener(urllib.request.Request(url, headers=headers)) as r:
                return r.status, getattr(r, "headers", None), r.read()
        from vzip import store

        check = None
        if not self.policy.allow_private_hosts:
            check = lambda ip: self.policy.check_address(url, ip)  # noqa: E731
        try:
            return store._send(url, headers, check, self.policy.allow_unchecked_proxy, self._conns)
        except (OSError, http.client.HTTPException) as e:
            if isinstance(e, urllib.error.URLError):
                raise
            raise urllib.error.URLError(e) from None

    def _get(self, url: str, headers: dict[str, str]) -> tuple[int, bytes]:
        """(status, body) of a GET of `url`, following at most 5 redirects; the policy
        checks `url`, and each redirect's target before it is requested."""
        from vzip.store import _check_http_url

        headers = {"User-Agent": UA, **headers}
        for hop in range(6):
            self.policy.check(self.location, url)
            status, msg, body = self._send(url, headers)
            if status not in REDIRECTS:
                return status, body
            locations = (msg.get_all("Location") if msg is not None else None) or []
            if len(locations) != 1:
                raise ResolutionError(f"{url}: HTTP {status} with {len(locations)} Location fields")
            if not is_uri_reference(locations[0]):
                raise ResolutionError(f"redirect with an invalid Location: {locations[0]!r}")
            new = urllib.parse.urljoin(url, locations[0]).split("#", 1)[0]
            if urllib.parse.urlsplit(new).scheme.lower() not in ("http", "https"):
                raise ResolutionError(f"redirect to a non-http URL: {new}")
            _check_http_url(new)
            if hop == 5:
                raise ResolutionError(f"{self.location}: more than 5 redirects")
            url = new
        raise AssertionError("unreachable")

    def _get_listing(self, url: str) -> bytes:
        self.requests += 1

        def get():
            status, body = self._get(url, {})
            if status != 200:
                raise urllib.error.HTTPError(url, status, f"HTTP {status}", None, None)
            return body

        try:
            return _retry(get)
        except urllib.error.HTTPError as e:
            if e.code in (408, 429) or e.code >= 500:
                raise ListingFailed(f"{url}: HTTP {e.code}") from None
            raise Rejected(f"the store has no listing: {url} answered HTTP {e.code}") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise ListingFailed(f"{url}: {e}") from None

    def _get_range(self, url: str, start: int, end: int) -> bytes:
        """The bytes [start, end) of the object at `url`, over the calling thread's
        kept-alive connection to its host."""

        def get():
            status, body = self._get(url, {"Range": f"bytes={start}-{end - 1}"})
            if status not in (200, 206):
                raise urllib.error.HTTPError(url, status, f"HTTP {status}", None, None)
            return body

        return _retry(get)

    def _release(self) -> None:
        self._conns.release()

    def read(self, key: str) -> bytes:
        size = self.objects[key]
        if size == 0:
            return b""
        url = object_url(self.location, key)
        data = self._get_range(url, 0, size)
        if len(data) != size:
            raise OSError(f"{url}: read {len(data)} bytes, the listing says {size}")
        return data

    def read_range(self, key: str, offset: int, length: int) -> bytes:
        if length == 0:
            return b""
        url = object_url(self.location, key)
        data = self._get_range(url, offset, offset + length)
        if len(data) != length:
            raise OSError(f"{url}: read {len(data)} bytes at {offset}, not {length}")
        return data


class DirStore(Store):
    """A local directory, read as the store at `url` (§1.5)."""

    def __init__(self, path: str, url: str) -> None:
        check_store_url(url)
        self.url = url
        self.root = Path(path)
        self.objects = {}
        self.ignored = []
        self.folders = []
        listed = []
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            rel_dir = os.path.relpath(dirpath, path)
            for name in filenames:
                full = os.path.join(dirpath, name)
                st = os.lstat(full)
                if not stat.S_ISREG(st.st_mode):
                    continue
                rel = name if rel_dir == "." else f"{rel_dir}/{name}"
                rel = rel.replace(os.sep, "/")
                try:
                    rel.encode("utf-8")
                except UnicodeEncodeError:  # a name that is not UTF-8 (surrogate-escaped)
                    continue
                listed.append((rel, st.st_size))
        self._add(listed, "", set())

    def read(self, key: str) -> bytes:
        data = (self.root / key).read_bytes()
        if len(data) != self.objects[key]:
            raise OSError(f"{key}: the file changed while it was read")
        return data

    def read_range(self, key: str, offset: int, length: int) -> bytes:
        with open(self.root / key, "rb") as fh:
            fh.seek(offset)
            data = fh.read(length)
        if len(data) != length:
            raise OSError(f"{key}: the file changed while it was read")
        return data


def open_store(location: str, url: str | None = None, *, max_objects: int | None = None,
               opener: Callable | None = None, workers: int = WORKERS, policy: Policy | None = None) -> Store:
    """The store at an http(s) URL ending in `/` (listed and read there under the
    reader policy `policy`, default `Policy()`, and named `url` in the output if
    given, its documents read by up to `workers` requests at a time), or a local
    directory served at `url` (§1.2), read sequentially."""
    if location.startswith(("http://", "https://")):
        store = HttpStore(location, max_objects=max_objects, opener=opener, workers=workers, policy=policy)
        if url is not None:
            check_store_url(url)
            store.url = url
        return store
    if url is None:
        raise ValueError("a local directory needs --url, the URL it is served from")
    return DirStore(location, url)


def choose_profile(store: Store) -> str:
    """The store profile that the root keys select (§1.4)."""
    if ".zarray" in store.objects or ".zgroup" in store.objects:
        return "zarr2"
    if "attributes.json" in store.objects:
        return "n5"
    if "manifest.safe" in store.objects:
        return "safe"
    raise Rejected("not an N5, Zarr v2 or SAFE store: the root has no .zarray, .zgroup, attributes.json "
                   "or manifest.safe")


# ---------------------------------------------------------------- hierarchy and output


def directories_of(keys) -> set[str]:
    """Every directory of the store (§1.4): the root and each proper key prefix before a `/`."""
    out = {""}
    for k in keys:
        i = k.find("/")
        while i >= 0:
            out.add(k[:i])
            i = k.find("/", i + 1)
    return out


def depth(path: str) -> int:
    return 0 if path == "" else path.count("/") + 1


def join(parent: str, name: str) -> str:
    return name if parent == "" else f"{parent}/{name}"


def parent_of(path: str) -> str:
    return path.rpartition("/")[0]


class PathTrie:
    """Paths (the root `""` included), each with a value, as a tree of their
    segments: finding a key's nearest ancestor among them takes time linear in
    the key's length, without rebuilding any prefix."""

    __slots__ = ("children", "value")

    def __init__(self) -> None:
        self.children: dict[str, PathTrie] = {}
        self.value = None

    def add(self, path: str, value) -> None:
        node = self
        if path:
            for s in path.split("/"):
                child = node.children.get(s)
                if child is None:
                    child = node.children[s] = PathTrie()
                node = child
        node.value = value

    def nearest(self, segs: list[str]) -> tuple[object, int] | None:
        """(value, j) of the nearest proper ancestor of the key with segments
        `segs` that has a value, at the path `segs[:j]`; or None."""
        node, best = self, None
        if self.value is not None and segs != [""]:
            best = (self.value, 0)
        for j in range(len(segs) - 1):
            node = node.children.get(segs[j])
            if node is None:
                break
            if node.value is not None:
                best = (node.value, j + 1)
        return best


def implicit_groups(nodes) -> set[str]:
    """The proper ancestors of `nodes` that are not nodes. Each walk stops at a
    path already seen, so the time is that of the paths it adds."""
    nodes = set(nodes)
    implicit: set[str] = set()
    for path in nodes:
        i = len(path)
        while i > 0:
            i = path.rfind("/", 0, i)
            p = path[:i] if i > 0 else ""
            if p in nodes or p in implicit:
                break
            implicit.add(p)
            i = max(i, 0)
    return implicit


def classify(candidates: dict[str, str]) -> tuple[dict[str, str], set[str]]:
    """Nodes from candidates {path: kind}, kind 'array' or 'group', classified
    from the root down (conventions/n5/README.md §2, conventions/zarr2/README.md §2): candidates inside an array are dropped.
    Returns ({path: kind}, implicit groups)."""
    nodes: dict[str, str] = {}
    arrays = PathTrie()
    for path in sorted(candidates, key=lambda p: (depth(p), p)):
        if arrays.nearest(path.split("/")) is not None:
            continue
        nodes[path] = candidates[path]
        if candidates[path] == "array":
            arrays.add(path, True)
    return nodes, implicit_groups(nodes)


def canonical_index(s: str, limit: int) -> bool:
    """Is `s` a decimal integer without leading zeros below `limit` (conventions/n5/README.md §3.1, conventions/zarr2/README.md §3)?"""
    return (s.isascii() and s.isdecimal() and (s == "0" or s[0] != "0")
            and len(s) <= 17 and int(s) < limit)


def grid(shape, chunks) -> list[int]:
    return [-(-s // c) for s, c in zip(shape, chunks)]


def find_chunks(objects: dict[str, int], arrays: dict[str, Callable[[str], bool]]) -> list[tuple[str, int]]:
    """The chunk objects (§1.4): for each object, the array that is its
    nearest ancestor decides with `is_chunk(rest)` whether the rest of the key
    is one of its chunk keys. Returns [(key, size)] in key order."""
    trie = PathTrie()
    for path, test in arrays.items():
        trie.add(path, test)
    out = []
    for key, size in objects.items():
        segs = key.split("/")
        found = trie.nearest(segs)
        if found is not None and found[0]("/".join(segs[found[1]:])):
            out.append((key, size))
    out.sort()
    return out


GROUP_IMPLICIT = {"zarr_format": 3, "node_type": "group", "attributes": {}}
SOURCE_GROUP = "vzip_source"
OBJECTS = "vzip_source/objects/"
EMPTY_KEY = "vzip_source/empty.json"  # the empty objects' keys under a root array (conventions/zarr2/README.md §5)
# A last segment that a Zarr reader takes for a node's document, followed by any number of `~`.
_NODE_NAME = re.compile(r"(?:zarr\.json|\.zarray|\.zgroup)~*\Z")


def object_key(key: str) -> str:
    """The hierarchy's key of the other object `key` (conventions/zarr2/README.md §5,
    conventions/n5/README.md §6): `vzip_source/objects/<key>`, with `~` appended to a last
    segment that is `zarr.json`, `.zarray` or `.zgroup` followed by any number of `~`, so
    that no Zarr reader opens a node there, and the key maps back one to one."""
    return OBJECTS + key + ("~" if _NODE_NAME.match(key.rpartition("/")[2]) else "")


def source_metadata(attributes: dict, metadata: dict, unversioned: list[str] | None = None) -> dict:
    """A store node's source metadata S (conventions/zarr2/README.md §4, conventions/n5/README.md §5,
    conventions/ome-zarr/README.md §8): `{"attributes": A, "metadata": M, "unversioned": U}`,
    each member only when it is not empty."""
    s = {}
    if attributes:
        s["attributes"] = attributes
    if metadata:
        s["metadata"] = metadata
    if unversioned:
        s["unversioned"] = unversioned
    return s


def doc_key(path: str) -> str:
    return join(path, "zarr.json")


@dataclass
class StoreOutput:
    """A store profile's output (§1.4): JSON documents, and the chunk entries,
    each referencing its whole object through its own url source."""

    url: str
    docs: dict[str, object] = field(default_factory=dict)
    chunks: list[tuple[str, int]] = field(default_factory=list)  # (key, size), sorted, size > 0
    summary: dict = field(default_factory=dict)
    # The source key of each entry whose key is not its object's (the objects under vzip_source/objects/).
    origins: dict[str, str] = field(default_factory=dict)
    # The CRC-32C of a url source's bytes, when the references are to carry
    # checksums (SPEC.md §5.2); set by vzip.virtualize.virtualize_store.
    checksum: Callable[[str, int, int], int] | None = None

    def declare(self, profile: str, omes: dict[str, dict] | None = None) -> None:
        """Declares the profile's convention (conventions §2). Each document's
        `attributes` holds, until then, the attributes copied from the source;
        they move into the property `vzip_virtualized` (as its member named
        after the profile), and the node's attributes are the member `ome` that
        `omes` gives for its path, if any, and the convention's members. The
        root always declares the convention; any other node only when it has
        copied attributes."""
        omes = omes or {}
        for key, doc in self.docs.items():
            path = key[: -len("zarr.json")].rstrip("/")
            target = {"ome": omes[path]} if path in omes else {}
            url = self.url if path == "" else None
            self.docs[key] = {**doc, "attributes": declare(target, profile, url, doc["attributes"])}

    def check_keys(self) -> None:
        for key in [*self.docs, *(k for k, _ in self.chunks)]:
            if len(key.encode()) > 65535:
                raise Rejected("an output key is longer than 65535 bytes")
            if key.startswith("__vz__/"):
                raise Rejected(f"output key {key!r} is in the reserved __vz__/ space")

    def keep_objects(self, objects: dict[str, int], used: set[str], nodes, ignored=()) -> int:
        """Adds every nonempty object of the store that is not in `used` (the node
        documents the hierarchy represents, the nonempty chunk objects, the objects
        referenced under their own key, and those the convention leaves out), whole,
        at its `object_key`, lists the keys of the empty ones (empty chunk objects
        included) and the recorded `ignored` keys (§1.4), and adds the group
        `vzip_source` when there is one (conventions/zarr2/README.md §5,
        conventions/n5/README.md §6). Returns how many objects are kept. A hierarchy with a
        node at `vzip_source` or below it then rejects the input (`nodes`: every node path)."""
        empty = sorted(k for k, n in objects.items() if n == 0 and k not in used)
        kept = [(k, n) for k, n in objects.items() if n > 0 and k not in used]
        ignored = sorted(ignored)
        if not kept and not empty and not ignored:
            return 0
        own = {**({"empty": empty} if empty else {}), **({"ignored": ignored} if ignored else {})} or None
        if self.docs.get(doc_key(""), {}).get("node_type") != "array":
            for p in nodes:
                if p == SOURCE_GROUP or p.startswith(SOURCE_GROUP + "/"):
                    raise Rejected(f"the node {p} is where the store's other objects go ({OBJECTS}...)")
            # The keys of empty and ignored objects are the source metadata of vzip_source.
            profile = self.docs[doc_key("")]["attributes"][CONVENTION_KEY]["profile"]
            self.docs[doc_key(SOURCE_GROUP)] = dict(GROUP_IMPLICIT, attributes=declare({}, profile, None, own))
        elif own:
            # An array has no children: under a root array, the keys are plain keys of the archive.
            self.docs[EMPTY_KEY] = own
        for k, n in kept:
            self.origins[object_key(k)] = k
        self.chunks = sorted(self.chunks + [(object_key(k), n) for k, n in kept])
        return len(kept)

    @property
    def sources(self) -> list[str]:
        return [object_url(self.url, self.origins.get(k, k)) for k, _ in self.chunks]

    def refs(self) -> dict[str, list[tuple[int, int, int]]]:
        return {k: [(i, 0, n)] for i, (k, n) in enumerate(self.chunks)}

    def write(self, out_path: str) -> None:
        from vzip.archive import VZipWriter

        with open(out_path, "wb") as fh:
            w = VZipWriter(fh, page_size=1 << 16, checksum=self.checksum)
            # each source pins its object's listed size (§1.4)
            w.add_url_refs(((k, object_url(self.url, self.origins.get(k, k)), n) for k, n in self.chunks),
                           pin_size=True)
            for key in sorted(self.docs):
                # The empty objects' keys are UTF-8, as JSON.stringify writes them (the documents
                # may hold strings that are not, and are escaped).
                data = json.dumps(self.docs[key], separators=(",", ":"), ensure_ascii=key != EMPTY_KEY).encode()
                # The hierarchy's documents are read when the archive opens; those of vzip_source
                # (the empty objects' keys), like chunks, only when asked for.
                source = key.startswith(SOURCE_GROUP + "/")
                w.add_bytes(key, data, compress=source, late=not source)
            w.close()
