"""A caching HTTP server for the virtualization harness (HARNESS.md).

Serves, over plain HTTP:

* `/f/<name>`: the file `<name>` of a local directory (the synthetic fixtures);
* `/u/<id>/<name>`: the remote http(s) object whose URL is the base64url
  decoding of `<id>` (`<name>` is only there to keep the file's name visible).

Both answer `HEAD` (with `Content-Length`) and `GET` with a single
`Range: bytes=a-b` (206 with `Content-Range`); a `GET` without `Range` is
refused (403), since no virtualizer needs one. Remote bytes are fetched in
64 KiB blocks, once, and cached on disk, so that every implementation reads
the same bytes and upstream servers see each block only once.

Usage: python conformance/virtualize/proxy.py <fixture dir> <cache dir> [port]
"""

from __future__ import annotations

import base64
import hashlib
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BLOCK = 1 << 16
UA = "vzip-virtualize-harness (https://github.com/d-v-b/vzip)"


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
                    self.sizes[url] = int(total) if total.isdigit() else int(r.headers["Content-Length"])
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


def make_handler(fixtures: Path, upstream: Upstream):
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

        def do_HEAD(self):
            t = self._target()
            if t is None:
                return self._send(404, {"Content-Length": "0"})
            self._send(200, {"Content-Length": str(t[0]), "Accept-Ranges": "bytes"})

        def do_GET(self):
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

    def __init__(self, fixtures: Path, cache: Path, port: int = 0) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(fixtures, Upstream(cache)))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def local(self, name: str) -> str:
        return f"{self.base}f/{name}"

    def remote(self, url: str) -> str:
        name = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "file"
        if name == "content":  # Zenodo: .../files/<name>/content
            name = url.split("/files/")[-1].split("/")[0]
        return f"{self.base}u/{encode_id(url)}/{name}"


if __name__ == "__main__":
    p = Proxy(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 8765)
    print(p.base, flush=True)
    threading.Event().wait()
