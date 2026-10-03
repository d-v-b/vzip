"""Store inputs (VIRTUALIZE.md §1.4–§1.6): the listing, object reads, the
strict JSON reader, and the output of a store profile (one url source per
chunk object).
"""

from __future__ import annotations

import json
import os
import re
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from vzip.uri import is_uri_reference
from vzip.virtualize.common import Rejected, _retry

MAX_SAFE = 2**53 - 1
MAX_DOCUMENT = 1 << 24
MAX_DEPTH = 256
UA = "vzip-virtualize"

_SPLIT = re.compile(r"^(?:([^:/?#]+):)?(?://([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#(.*))?$")
_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PATH_SAFE = _UNRESERVED | frozenset(b"!$&'()*+,;=:@/")


class ListingFailed(OSError):
    """The listing could not be read (a network error or a 408/429/5xx status): a failure, §1.4."""


class StoreLimit(OSError):
    """An implementation's resource limit (§1.2, §11): a failure, not a rejection."""


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


class _Xml:
    """The XML subset of §1.5."""

    def __init__(self, s: str) -> None:
        self.s = s
        self.pos = 0

    def fail(self, why: str):
        raise Rejected(f"listing is not well formed: {why} at {self.pos}")

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
        self.pos += 1
        el = Element(self.name())
        s = self.s
        while True:
            if self.pos < len(s) and s[self.pos] in _WS:
                self.ws()
                if self.pos < len(s) and s[self.pos] in _NAME_START:
                    self.name()
                    self.ws()
                    if not self.at("="):
                        self.fail("expected =")
                    self.pos += 1
                    self.ws()
                    q = s[self.pos] if self.pos < len(s) else ""
                    if q not in ("'", '"'):
                        self.fail("expected a quoted value")
                    self.pos += 1
                    while True:
                        self.chardata(q)
                        if self.at("&"):
                            self.reference()
                        elif self.at(q):
                            self.pos += 1
                            break
                        else:
                            self.fail("bad attribute value")
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


def _int(s: str) -> int | float:
    try:
        v = int(s)
    except ValueError:  # more digits than Python converts: far beyond binary64
        raise Rejected("JSON number is not finite in binary64") from None
    if -MAX_SAFE <= v <= MAX_SAFE:
        return v
    try:
        return float(v)
    except OverflowError:
        raise Rejected("JSON number is not finite in binary64") from None


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


# ---------------------------------------------------------------- stores (§1.4, §1.5)


def _ignored(rel: str) -> bool:
    return rel == "" or rel.endswith("/") or any(s in ("", ".", "..") for s in rel.split("/"))


class Store:
    """A listed store: `objects` maps each relative key to its size (§1.4)."""

    url: str
    objects: dict[str, int]
    listed: int = 0
    requests: int = 0

    def read(self, key: str) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def document(self, key: str):
        """The JSON document at `key` (§1.6)."""
        size = self.objects[key]
        if size > MAX_DOCUMENT:
            raise Rejected(f"{key}: JSON document of {size} bytes exceeds {MAX_DOCUMENT}")
        return parse_json(self.read(key) if size else b"")

    def _add(self, listed: list[tuple[str, int]], prefix: str, seen: set[str]) -> None:
        for key, size in listed:
            if key in seen:
                raise Rejected(f"key {key!r} is listed twice")
            seen.add(key)
            rel = key[len(prefix):]
            if not _ignored(rel):
                self.objects[rel] = size
        self.listed = len(seen)


class HttpStore(Store):
    """A store listed by S3 ListObjectsV2 (§1.5)."""

    def __init__(self, url: str, *, max_objects: int | None = None,
                 opener: Callable | None = None) -> None:
        self.url = url
        self.endpoint, self.prefix = listing_endpoint(url)
        self.objects = {}
        self._open = opener or (lambda req: urllib.request.urlopen(req, timeout=120))
        seen: set[str] = set()
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

    def _get_listing(self, url: str) -> bytes:
        self.requests += 1
        req = urllib.request.Request(url, headers={"User-Agent": UA})

        def get():
            with self._open(req) as r:
                return r.status, r.read()

        try:
            status, body = _retry(get)
        except urllib.error.HTTPError as e:
            if e.code in (408, 429) or e.code >= 500:
                raise ListingFailed(f"{url}: HTTP {e.code}") from None
            raise Rejected(f"the store has no listing: {url} answered HTTP {e.code}") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise ListingFailed(f"{url}: {e}") from None
        if status != 200:
            raise Rejected(f"the store has no listing: {url} answered HTTP {status}")
        return body

    def read(self, key: str) -> bytes:
        size = self.objects[key]
        if size == 0:
            return b""
        url = object_url(self.url, key)
        req = urllib.request.Request(url, headers={"Range": f"bytes=0-{size - 1}", "User-Agent": UA})

        def get():
            with self._open(req) as r:
                return r.read()

        data = _retry(get)
        if len(data) != size:
            raise OSError(f"{url}: read {len(data)} bytes, the listing says {size}")
        return data


class DirStore(Store):
    """A local directory, read as the store at `url` (§1.5)."""

    def __init__(self, path: str, url: str) -> None:
        check_store_url(url)
        self.url = url
        self.root = Path(path)
        self.objects = {}
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


def open_store(location: str, url: str | None = None, *, max_objects: int | None = None) -> Store:
    """The store at an http(s) URL ending in `/` (listed there, and named `url`
    in the output if given), or a local directory served at `url` (§1.2)."""
    if location.startswith(("http://", "https://")):
        store = HttpStore(location, max_objects=max_objects)
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
    raise Rejected("not an N5 or Zarr v2 store: the root has no .zarray, .zgroup or attributes.json")


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


def classify(candidates: dict[str, str]) -> tuple[dict[str, str], set[str]]:
    """Nodes from candidates {path: kind}, kind 'array' or 'group', classified
    from the root down (§9.1, §10.1): candidates inside an array are dropped.
    Returns ({path: kind}, implicit groups)."""
    nodes: dict[str, str] = {}
    arrays: set[str] = set()
    for path in sorted(candidates, key=lambda p: (depth(p), p)):
        p, inside = path, False
        while p:
            p = parent_of(p)
            if p in arrays:
                inside = True
                break
        if inside:
            continue
        nodes[path] = candidates[path]
        if candidates[path] == "array":
            arrays.add(path)
    implicit: set[str] = set()
    for path in nodes:
        p = path
        while p:
            p = parent_of(p)
            if p not in nodes:
                implicit.add(p)
    return nodes, implicit


def canonical_index(s: str, limit: int) -> bool:
    """Is `s` a decimal integer without leading zeros below `limit` (§9.3, §10.2)?"""
    return (s.isascii() and s.isdecimal() and (s == "0" or s[0] != "0")
            and len(s) <= 17 and int(s) < limit)


def grid(shape, chunks) -> list[int]:
    return [-(-s // c) for s, c in zip(shape, chunks)]


def find_chunks(objects: dict[str, int], arrays: dict[str, Callable[[str], bool]]) -> list[tuple[str, int]]:
    """The chunk objects (§1.4): for each object, the array that is its
    nearest ancestor decides with `is_chunk(rest)` whether the rest of the key
    is one of its chunk keys. Returns [(key, size)] in key order."""
    out = []
    for key, size in objects.items():
        segs = key.split("/")
        for j in range(len(segs) - 1, -1, -1):
            d = "/".join(segs[:j])
            test = arrays.get(d)
            if test is not None:
                if test("/".join(segs[j:])):
                    out.append((key, size))
                break
    out.sort()
    return out


GROUP_IMPLICIT = {"zarr_format": 3, "node_type": "group", "attributes": {}}


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

    def check_keys(self) -> None:
        for key in [*self.docs, *(k for k, _ in self.chunks)]:
            if len(key.encode()) > 65535:
                raise Rejected("an output key is longer than 65535 bytes")
            if key.startswith("__vz__/"):
                raise Rejected(f"output key {key!r} is in the reserved __vz__/ space")

    @property
    def sources(self) -> list[str]:
        return [object_url(self.url, k) for k, _ in self.chunks]

    def refs(self) -> dict[str, list[tuple[int, int, int]]]:
        return {k: [(i, 0, n)] for i, (k, n) in enumerate(self.chunks)}

    def write(self, out_path: str) -> None:
        from vzip.archive import VZipWriter

        with open(out_path, "wb") as fh:
            w = VZipWriter(fh, page_size=1 << 16)
            w.add_url_refs((k, object_url(self.url, k), n) for k, n in self.chunks)
            for key in sorted(self.docs):
                w.add_bytes(key, json.dumps(self.docs[key], indent=2).encode(), late=True)
            w.close()
