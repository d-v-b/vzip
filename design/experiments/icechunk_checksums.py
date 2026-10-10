"""How does icechunk use the checksum fields of a virtual chunk reference?

Serves a directory over HTTP with ETag / Last-Modified headers and support for
conditional requests (If-Match, If-None-Match, If-Unmodified-Since,
If-Modified-Since), logs the headers icechunk sends, and checks what happens
when the referenced object changes after the reference was written.

Usage: uv run python design/experiments/icechunk_checksums.py
"""

from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import os
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import icechunk
import numpy as np
import zarr

ROOT = Path(tempfile.mkdtemp())
PORT = 8766
BASE = f"http://127.0.0.1:{PORT}"
LOG: list[dict] = []
SLEEP = float(os.environ.get("SLEEP", "2"))


def etag_of(p: Path) -> str:
    st = p.stat()
    return '"' + hashlib.md5(f"{st.st_mtime_ns}-{st.st_size}".encode()).hexdigest() + '"'


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
        p = ROOT / self.path.split("?")[0].lstrip("/")
        cond = {k: v for k, v in self.headers.items()
                if k.lower().startswith("if-") or k.lower() == "range"}
        entry = {"method": self.command, "path": self.path, "headers": cond}
        LOG.append(entry)
        if not p.is_file():
            entry["status"] = 404
            return self._reply(404)
        etag = etag_of(p)
        mtime = p.stat().st_mtime
        meta = {"ETag": etag, "Last-Modified": email.utils.formatdate(mtime, usegmt=True),
                "Accept-Ranges": "bytes"}
        h = {k.lower(): v for k, v in self.headers.items()}
        failed = (
            ("if-match" in h and h["if-match"].strip('"') not in ("*", etag.strip('"')))
            or ("if-unmodified-since" in h
                and int(mtime) > email.utils.parsedate_to_datetime(h["if-unmodified-since"]).timestamp())
        )
        if failed:
            entry["status"] = 412
            return self._reply(412, b"precondition failed", meta)
        if ("if-none-match" in h and h["if-none-match"] in ("*", etag)) or (
            "if-modified-since" in h
            and int(mtime) <= email.utils.parsedate_to_datetime(h["if-modified-since"]).timestamp()
        ):
            entry["status"] = 304
            return self._reply(304, b"", meta)
        data = p.read_bytes()
        rng = h.get("range")
        if rng and rng.startswith("bytes="):
            a, b = rng[6:].split("-")
            start = int(a) if a else max(len(data) - int(b), 0)
            end = (int(b) + 1 if a and b else len(data))
            entry["status"] = 206
            return self._reply(206, data[start:end],
                               {**meta, "Content-Range": f"bytes {start}-{end - 1}/{len(data)}"})
        entry["status"] = 200
        self._reply(200, data, meta)

    do_GET = _serve
    do_HEAD = _serve


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    target = ROOT / "data.bin"
    target.write_bytes(bytes(range(256)) * 16)
    os.utime(target, (time.time() - 3600, time.time() - 3600))  # modified an hour ago
    prefix = f"{BASE}/"
    url = f"{BASE}/data.bin"

    cfg = icechunk.RepositoryConfig.default()
    cfg.set_virtual_chunk_container(icechunk.VirtualChunkContainer(prefix, icechunk.http_store()))
    repo = icechunk.Repository.create(
        icechunk.local_filesystem_storage(str(ROOT / "repo")), cfg,
        authorize_virtual_chunk_access={prefix: None})
    s = repo.writable_session("main")
    arr = zarr.create_array(s.store, name="a", shape=(4 * 16,), chunks=(16,), dtype="u1",
                            compressors=None, fill_value=0)
    now = dt.datetime.now(dt.timezone.utc)
    specs = [
        icechunk.VirtualChunkSpec([0], url, 0, 16),                                    # no checksum
        icechunk.VirtualChunkSpec([1], url, 16, 16, last_updated_at_checksum=now),     # timestamp
        icechunk.VirtualChunkSpec([2], url, 32, 16, etag_checksum=etag_of(target)),    # etag
        icechunk.VirtualChunkSpec([3], url, 48, 16,                                     # stale timestamp
                                  last_updated_at_checksum=now - dt.timedelta(hours=2)),
    ]
    s.store.set_virtual_refs("a", specs, validate_containers=True)
    s.commit("refs")

    def read_all(label):
        repo2 = icechunk.Repository.open(icechunk.local_filesystem_storage(str(ROOT / "repo")),
                                         authorize_virtual_chunk_access={prefix: None})
        a = zarr.open_array(repo2.readonly_session("main").store, path="a", mode="r")
        print(f"\n== {label}")
        for i, name in enumerate(["no checksum", "last_updated_at=now", "etag=current",
                                  "last_updated_at=2h ago"]):
            LOG.clear()
            try:
                v = a[i * 16:(i + 1) * 16]
                res = f"ok {bytes(v)[:4].hex()}..."
            except Exception as e:  # noqa: BLE001
                res = f"ERROR {type(e).__name__}: {str(e).splitlines()[0][:110]}"
            reqs = [(e["method"], e.get("status"), e["headers"]) for e in LOG if "data.bin" in e["path"]]
            print(f"  chunk {i} ({name:22s}): {res}")
            for r in reqs:
                print(f"      request: {r}")

    read_all("source unchanged (modified 1h ago)")
    time.sleep(SLEEP)
    target.write_bytes(bytes(255 - (i % 256) for i in range(4096)))  # rewrite in place, now
    read_all(f"source rewritten {SLEEP}s after the references were made")
    srv.shutdown()
    shutil.rmtree(ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
