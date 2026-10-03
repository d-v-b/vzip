"""A caching HTTP server for the virtualization harness (HARNESS.md).

Serves, over plain HTTP:

* `/f/<name>`: the file `<name>` of a local directory (the synthetic fixtures);
* `/u/<id>/<name>`: the remote http(s) object whose URL is the base64url
  decoding of `<id>` (`<name>` is only there to keep the file's name visible);
* `/u/<id>/<key>`, when that URL ends in `/` (a remote store): the store's
  object `<key>`, at that URL followed by `<key>`.

Both answer `HEAD` (with `Content-Length`) and `GET` with a single
`Range: bytes=a-b` (206 with `Content-Range`); a `GET` without `Range` is
refused (403), since no virtualizer needs one. Remote bytes are fetched in
64 KiB blocks, once, and cached on disk, so that every implementation reads
the same bytes and upstream servers see each block only once.

Stores (VIRTUALIZE.md §1.4, §1.5) are listed with S3 ListObjectsV2, path
style, in the buckets `f` and `u`: `GET /f/?list-type=2&prefix=n5/x/` lists
the fixture directory `n5/x/`, and `GET /u/?list-type=2&prefix=<id>/<rest>`
lists the remote store that `<id>` names (its own listing, fetched once and
cached), under `<rest>`, with keys rewritten to start with `<id>/`. Pages
hold `page_size` keys (default 100, so that the small fixtures exercise
pagination); the continuation token is opaque.

Usage: python conformance/virtualize/proxy.py <fixture dir> <cache dir> [port]
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

BLOCK = 1 << 16
UA = "vzip-virtualize-harness"
ROOT = Path(__file__).resolve().parents[2]


def encode_id(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def decode_id(i: str) -> str:
    return base64.urlsafe_b64decode(i + "=" * (-len(i) % 4)).decode()


class Upstream:
    def __init__(self, cache: Path) -> None:
        self.cache = cache
        cache.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.sizes: dict[str, int] = {}

    def _open(self, req):
        for attempt in range(6):
            try:
                return urllib.request.urlopen(req, timeout=120)
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504) or attempt == 5:
                    raise
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                if attempt == 5:
                    raise
            time.sleep(2**attempt)

    def size(self, url: str) -> int:
        if url not in self.sizes:
            f = self.cache / (hashlib.sha256(url.encode()).hexdigest() + ".size")
            if f.exists():
                self.sizes[url] = int(f.read_text())
            else:
                req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": UA})
                with self._open(req) as r:
                    total = (r.headers.get("Content-Range") or "").rpartition("/")[2]
                    self.sizes[url] = int(total) if total.isdecimal() else int(r.headers["Content-Length"])
                f.write_text(str(self.sizes[url]))
        return self.sizes[url]

    def block(self, url: str, i: int) -> bytes:
        f = self.cache / f"{hashlib.sha256(url.encode()).hexdigest()}.{i}"
        if f.exists():
            return f.read_bytes()
        start = i * BLOCK
        end = min(self.size(url), start + BLOCK)
        req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end - 1}", "User-Agent": UA})
        with self._open(req) as r:
            data = r.read()
        if len(data) != end - start:
            raise OSError(f"{url}: short block {i}")
        f.write_bytes(data)
        return data

    def read(self, url: str, start: int, end: int) -> bytes:
        out = bytearray()
        pos = start
        while pos < end:
            b = self.block(url, pos // BLOCK)
            o = pos % BLOCK
            take = min(len(b) - o, end - pos)
            out += b[o : o + take]
            pos += take
        return bytes(out)


def local_listing(fixtures: Path, prefix: str) -> list[tuple[str, int]]:
    """The regular files under `fixtures` whose relative path starts with `prefix`."""
    base = prefix.rpartition("/")[0]
    top = fixtures / base if base else fixtures
    if not top.resolve().is_relative_to(fixtures.resolve()) or not top.is_dir():
        return []
    out = []
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            full = Path(dirpath) / name
            if full.is_symlink() or not full.is_file():
                continue
            key = full.relative_to(fixtures).as_posix()
            if key.startswith(prefix):
                out.append((key, full.stat().st_size))
    return sorted(out)


def listing_xml(prefix: str, keys: list[tuple[str, int]], token: str | None) -> bytes:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>\n<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
             f"<Prefix>{escape(prefix)}</Prefix><KeyCount>{len(keys)}</KeyCount>",
             f"<IsTruncated>{'true' if token else 'false'}</IsTruncated>"]
    if token:
        parts.append(f"<NextContinuationToken>{escape(token)}</NextContinuationToken>")
    for k, n in keys:
        parts.append(f"<Contents><Key>{escape(k)}</Key><Size>{n}</Size><StorageClass>STANDARD</StorageClass></Contents>")
    parts.append("</ListBucketResult>")
    return "".join(parts).encode()


class RemoteListings:
    """Complete listings of remote stores, fetched once and cached on disk."""

    def __init__(self, upstream: Upstream) -> None:
        self.upstream = upstream

    def keys(self, store_url: str, rest: str) -> list[tuple[str, int]]:
        """(relative key, size) of the objects of the store at `store_url` whose
        relative key starts with `rest`."""
        sys.path.insert(0, str(ROOT / "src"))
        from vzip.virtualize.store import listing_endpoint, parse_listing, query_encode

        f = self.upstream.cache / (hashlib.sha256(f"list:{store_url}|{rest}".encode()).hexdigest() + ".json")
        if f.exists():
            return [tuple(x) for x in json.loads(f.read_text())]
        endpoint, prefix = listing_endpoint(store_url)
        full = prefix + rest
        out, token = [], None
        while True:
            q = f"?list-type=2&prefix={query_encode(full)}"
            if token is not None:
                q += f"&continuation-token={query_encode(token)}"
            req = urllib.request.Request(endpoint + q, headers={"User-Agent": UA})
            with self.upstream._open(req) as r:
                body = r.read()
            listed, token = parse_listing(body, full)
            out += [(k[len(prefix):], n) for k, n in listed]
            if token is None:
                break
        out.sort()
        f.write_text(json.dumps(out))
        return out


def make_handler(fixtures: Path, upstream: Upstream, page_size: int = 100):
    remote = RemoteListings(upstream)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _target(self):
            parts = self.path.split("?")[0].split("/")
            if len(parts) >= 3 and parts[1] == "f":
                path = fixtures / urllib.request.url2pathname("/".join(parts[2:]))
                if not path.resolve().is_relative_to(fixtures.resolve()) or not path.is_file():
                    return None
                data = path.read_bytes()
                return len(data), lambda a, b: data[a:b]
            if len(parts) >= 3 and parts[1] == "u":
                url = decode_id(parts[2])
                if url.endswith("/"):  # a remote store: the object under it
                    key = "/".join(parts[3:])
                    if not key:
                        return None
                    url += key
                return upstream.size(url), lambda a, b: upstream.read(url, a, b)
            return None

        def _send(self, status, headers, body=b""):
            self.send_response(status)
            headers = {"Content-Length": str(len(body)), **headers}
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _listing(self) -> bool:
            """Answers a ListObjectsV2 request (§1.5); False if this is not one."""
            path, _, query = self.path.partition("?")
            if path not in ("/f/", "/u/") or not query:
                return False
            q = urllib.parse.parse_qs(query, keep_blank_values=True)
            if q.get("list-type") != ["2"]:
                self._send(400, {}, b"list-type=2 only")
                return True
            prefix = q.get("prefix", [""])[0]
            try:
                if path == "/f/":
                    keys = local_listing(fixtures, prefix)
                else:
                    sid, _, rest = prefix.partition("/")
                    store_url = decode_id(sid)
                    keys = ([(f"{sid}/{k}", n) for k, n in remote.keys(store_url, rest)]
                            if store_url.endswith("/") else [])
            except Exception as e:  # noqa: BLE001
                self._send(502, {}, str(e).encode())
                return True
            start = 0
            if "continuation-token" in q:
                start = int(base64.urlsafe_b64decode(q["continuation-token"][0]))
            page = keys[start : start + page_size]
            end = start + len(page)
            token = base64.urlsafe_b64encode(str(end).encode()).decode() if end < len(keys) else None
            self._send(200, {"Content-Type": "application/xml"}, listing_xml(prefix, page, token))
            return True

        def do_HEAD(self):
            t = self._target()
            if t is None:
                return self._send(404, {"Content-Length": "0"})
            self._send(200, {"Content-Length": str(t[0]), "Accept-Ranges": "bytes"})

        def do_GET(self):
            if self._listing():
                return
            t = self._target()
            if t is None:
                return self._send(404, {}, b"not found")
            size, read = t
            rng = self.headers.get("Range", "")
            if not rng.startswith("bytes=") or "," in rng:
                return self._send(403, {}, b"send a single Range")
            a, _, b = rng[6:].partition("-")
            if a == "":
                start, end = max(0, size - int(b)), size
            else:
                start, end = int(a), (size if b == "" else min(size, int(b) + 1))
            if start >= size or end <= start:
                return self._send(416, {"Content-Range": f"bytes */{size}"})
            try:
                body = read(start, end)
            except Exception as e:  # noqa: BLE001
                return self._send(502, {}, str(e).encode())
            self._send(206, {"Content-Range": f"bytes {start}-{end - 1}/{size}", "Accept-Ranges": "bytes"}, body)

    return Handler


class Proxy:
    """Runs the server in a thread; `base` is its URL, ending in "/"."""

    def __init__(self, fixtures: Path, cache: Path, port: int = 0, page_size: int = 100) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(fixtures, Upstream(cache), page_size))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def local(self, name: str) -> str:
        return f"{self.base}f/{name}"

    def local_store(self, name: str) -> str:
        """The store URL of a fixture directory (`name` relative to the fixtures)."""
        return f"{self.base}f/{urllib.parse.quote(name)}/"

    def remote_store(self, url: str) -> str:
        """The proxy's store URL for the remote store at `url` (ending in /)."""
        return f"{self.base}u/{encode_id(url)}/"

    def remote(self, url: str) -> str:
        name = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "file"
        if name == "content":  # Zenodo: .../files/<name>/content
            name = url.split("/files/")[-1].split("/")[0]
        return f"{self.base}u/{encode_id(url)}/{name}"


if __name__ == "__main__":
    p = Proxy(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 8765)
    print(p.base, flush=True)
    threading.Event().wait()
