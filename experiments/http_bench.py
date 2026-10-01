"""Cold "open + read one chunk" over HTTP, with per-request latency.

Serves experiments/_bench over a local HTTP server that supports Range
requests, logs every request, and sleeps LATENCY seconds before answering
(a stand-in for an object-store round trip). All formats reference their
chunks by http:// URL, so every byte any reader touches is counted.

Usage: uv run python experiments/http_bench.py [N ...]
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import fsspec  # noqa: F401  (import before timing)
import fsspec.implementations.reference  # noqa: F401
import icechunk  # noqa: F401
import zarr
from fsspec.implementations.asyn_wrapper import AsyncFileSystemWrapper  # noqa: F401

sys.path.insert(0, str(Path(__file__).parent))
from bench import PER_FILE, ROOT, dir_size, layout, make_targets, synthetic  # noqa: E402

from vzip.convert import write_vzip
from vzip.shards import write_vzip_sharded
from vzip.store import VZipStore

warnings.filterwarnings("ignore")
LATENCY = float(os.environ.get("LATENCY", "0.02"))
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
LOG: list[tuple[str, str, int]] = []
_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _serve(self, body: bool):
        time.sleep(LATENCY)
        p = ROOT / self.path.split("?")[0].lstrip("/")
        if not p.is_file():
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            with _lock:
                LOG.append((self.command, self.path, 0))
            return
        size = p.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size
        if rng and rng.startswith("bytes="):
            a, b = rng[6:].split(",")[0].split("-")
            if a == "":
                start = max(size - int(b), 0)
            else:
                start = int(a)
                end = min(int(b) + 1, size) if b else size
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end - 1}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(end - start))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        n = 0
        if body:
            with open(p, "rb") as f:
                f.seek(start)
                data = f.read(end - start)
            self.wfile.write(data)
            n = len(data)
        with _lock:
            LOG.append((self.command, self.path, n))

    def do_GET(self):
        self._serve(True)

    def do_HEAD(self):
        self._serve(False)


def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------- readers


def r_vzip(url, idx):
    return zarr.open_array(VZipStore(url), path="v", mode="r")[idx]


def r_kerchunk(url, idx):
    import fsspec
    from fsspec.implementations.asyn_wrapper import AsyncFileSystemWrapper

    fs = fsspec.filesystem(
        "reference", fo=url, remote_protocol="http", target_protocol="http",
        skip_instance_cache=True, lazy=True,
    )
    store = zarr.storage.FsspecStore(AsyncFileSystemWrapper(fs), read_only=True, path="")
    return zarr.open_array(store, path="v", mode="r", zarr_format=2)[idx]


def _ic_container(prefix):
    import icechunk

    return icechunk.VirtualChunkContainer(prefix, icechunk.http_store())


def w_icechunk(vds, out, prefix):
    import icechunk

    cfg = icechunk.RepositoryConfig.default()
    cfg.set_virtual_chunk_container(_ic_container(prefix))
    repo = icechunk.Repository.create(
        icechunk.local_filesystem_storage(str(out)), cfg, authorize_virtual_chunk_access={prefix: None}
    )
    s = repo.writable_session("main")
    vds.vz.to_icechunk(s.store)
    s.commit("write")


def r_icechunk(url, idx, prefix):
    import icechunk

    repo = icechunk.Repository.open(
        icechunk.http_storage(url), authorize_virtual_chunk_access={prefix: None}
    )
    return zarr.open_array(repo.readonly_session("main").store, path="v", mode="r")[idx]


def run(n: int) -> list[dict]:
    offsets, lengths, ends = layout(n)
    make_targets(ends)
    prefix = f"{BASE}/targets/"
    vds = synthetic(n, prefix, offsets, lengths)
    d = ROOT / f"http_n{n}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    rel = f"http_n{n}"
    idx = n // 2
    formats = {
        "vzip (chunk refs)": (lambda o: write_vzip(vds, o), r_vzip, "x.vzip"),
        "vzip (chunk refs, paged)": (
            lambda o: write_vzip(vds, o, page_size=1 << 16), r_vzip, "xp.vzip"),
        "vzip (virtual shards)": (
            lambda o: write_vzip_sharded(vds, o, {"v": (PER_FILE, 1)}), r_vzip, "s.vzip"),
        "kerchunk json": (lambda o: vds.vz.to_kerchunk(str(o), format="json"), r_kerchunk, "refs.json"),
        "kerchunk parquet": (
            lambda o: vds.vz.to_kerchunk(str(o), format="parquet"), r_kerchunk, "refs.parq"),
        "icechunk": (lambda o: w_icechunk(vds, o, prefix),
                     lambda u, i: r_icechunk(u, i, prefix), "ic"),
    }
    rows = []
    for name, (w, r, fname) in formats.items():
        out = d / fname
        w(out)
        LOG.clear()
        t0 = time.perf_counter()
        val = r(f"{BASE}/{rel}/{fname}", idx)
        dt = time.perf_counter() - t0
        assert val.shape == (1024,)
        meta = [e for e in LOG if "/targets/" not in e[1]]
        row = {
            "n_refs": n, "format": name, "size_bytes": dir_size(out),
            "requests": len(LOG), "index_requests": len(meta),
            "index_bytes": sum(e[2] for e in meta), "seconds": dt,
        }
        rows.append(row)
        print(f"{n:>9} {name:25s} reqs={row['requests']:<3d} (index {row['index_requests']:<3d}) "
              f"index bytes={row['index_bytes'] / 1e6:8.3f} MB  time={dt:6.3f}s", flush=True)
        if os.environ.get("VERBOSE"):
            for e in LOG:
                print("      ", e)
    return rows


if __name__ == "__main__":
    srv = serve()
    ns = [int(x) for x in sys.argv[1:]] or [10_000, 1_000_000]
    rows = [r for n in ns for r in run(n)]
    srv.shutdown()
    res = Path(__file__).parent / "results"
    res.mkdir(exist_ok=True)
    (res / f"http_bench_latency{int(LATENCY * 1000)}ms.json").write_text(json.dumps(rows, indent=1))
