"""A local HTTP server for the conformance suite (spec §6.2).

Serves files under `root` with:

* `Range: bytes=a-b` → 206 with `Content-Range: bytes a-b/total`; 416 if `a` is
  past the end;
* a strong `ETag` (a hash of the content) and `Last-Modified` (the file's mtime);
* `If-Match` and `If-Unmodified-Since` → 412 when they fail;
* a log of every request (method, path, headers).

A first path segment selects a quirk, then the rest of the path is served as
usual:

| prefix       | behaviour |
|--------------|-----------|
| `/norange/`  | ignores `Range`: 200 with the whole object |
| `/nototal/`  | 206 with `Content-Range: bytes a-b/*` (size unknown) |
| `/gzip/`     | adds `Content-Encoding: gzip` (a reader must refuse the body) |
| `/nocond/`   | ignores `If-Match` / `If-Unmodified-Since` (a reader must check the headers itself) |
| `/noetag/`   | sends no `ETag` and no `Last-Modified` |
| `/redirect/N/` | answers 302 to `/redirect/N-1/...`, and `/redirect/0/...` to the plain path |
"""

from __future__ import annotations

import email.utils
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

QUIRKS = ("norange", "nototal", "gzip", "nocond", "noetag", "redirect")


def etag_for(data: bytes) -> str:
    """The strong entity tag the server sends for `data` (with its quotes)."""
    return '"' + hashlib.sha1(data).hexdigest()[:16] + '"'


class Server:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.log: list[dict] = []
        self._lock = threading.Lock()
        srv = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _reply(self, code: int, body: bytes = b"", headers: dict | None = None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _serve(self):
                with srv._lock:
                    srv.log.append({"method": self.command, "path": self.path,
                                    "headers": {k.lower(): v for k, v in self.headers.items()}})
                parts = unquote(self.path.split("?")[0]).lstrip("/").split("/", 1)
                quirk = parts[0] if parts[0] in QUIRKS else None
                rel = parts[1] if quirk and len(parts) > 1 else "/".join(parts)
                if quirk == "redirect":
                    n, _, rest = rel.partition("/")
                    target = f"/redirect/{int(n) - 1}/{rest}" if int(n) > 0 else f"/{rest}"
                    return self._reply(302, b"", {"Location": target})
                p = (srv.root / rel).resolve()
                if srv.root.resolve() not in p.parents or not p.is_file():
                    return self._reply(404)
                data = p.read_bytes()
                mtime = int(p.stat().st_mtime)
                meta = {"ETag": etag_for(data),
                        "Last-Modified": email.utils.formatdate(mtime, usegmt=True),
                        "Accept-Ranges": "bytes"}
                if quirk == "gzip":
                    meta["Content-Encoding"] = "gzip"
                if quirk == "noetag":
                    del meta["ETag"], meta["Last-Modified"]
                h = {k.lower(): v for k, v in self.headers.items()}
                if quirk == "nocond":
                    h.pop("if-match", None)
                    h.pop("if-unmodified-since", None)
                if "if-match" in h and h["if-match"] not in ("*", etag_for(data)):
                    return self._reply(412, b"", meta)
                if "if-unmodified-since" in h:
                    t = email.utils.parsedate_to_datetime(h["if-unmodified-since"]).timestamp()
                    if mtime > t:
                        return self._reply(412, b"", meta)
                rng = h.get("range")
                if not rng or quirk == "norange":
                    return self._reply(200, data, meta)
                a, _, b = rng.removeprefix("bytes=").partition("-")
                start = int(a) if a else max(len(data) - int(b), 0)
                end = min(int(b) + 1, len(data)) if a and b else len(data)
                if start >= len(data):
                    return self._reply(416, b"", {**meta, "Content-Range": f"bytes */{len(data)}"})
                total = "*" if quirk == "nototal" else str(len(data))
                return self._reply(206, data[start:end],
                                   {**meta, "Content-Range": f"bytes {start}-{end - 1}/{total}"})

            do_GET = _serve
            do_HEAD = _serve

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}/"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def shutdown(self) -> None:
        self._srv.shutdown()
