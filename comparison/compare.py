"""Store one virtual dataset as kerchunk JSON, kerchunk Parquet, Icechunk and
vzip, then read each back over HTTP and compare.

1. Source files: real netCDF4/HDF5 files with compressed chunks, written here
   (deterministic), or your own with `--input`.
2. Virtualize them once with VirtualiZarr's HDF parser. Every format stores
   this same virtual dataset; its chunk references are `http://` URLs of the
   source files.
3. Write each format with its own library (one small function per format,
   below), and measure size on disk and object count.
4. Serve everything from a local HTTP server that honors Range requests,
   adds a fixed latency to every request (a stand-in for an object store)
   and logs every request.
5. For each format, from a cold start: open the dataset with xarray, read
   one value, then read every variable in full. Count requests and bytes for
   each step, split into requests for the format's own files ("index") and
   requests for the source files ("data").
6. Check every value of every variable against the source files.

Usage:
    uv run python comparison/compare.py                  # generated files
    uv run python comparison/compare.py --files 48 --days 31
    uv run python comparison/compare.py --input 'data/*.nc' --concat-dim time
"""

from __future__ import annotations

import argparse
import glob
import heapq
import json
import multiprocessing
import shutil
import sys
import threading
import time
import urllib.request
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
import zarr

warnings.filterwarnings("ignore")

HERE = Path(__file__).parent


# --------------------------------------------------------------------- server


class Server:
    """Serves `root` with Range support, `latency` seconds per request, and a
    log of (method, path, bytes sent).

    The server runs in its own process, so that the readers being measured
    don't compete with it for the GIL.
    """

    def __init__(self, root: Path, latency: float):
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe()
        ctx.Process(target=_serve, args=(root, latency, child), daemon=True).start()
        self.base = f"http://127.0.0.1:{parent.recv()}"

    def take(self) -> dict:
        """Requests since the last call: index (format files) and data (sources)."""
        with urllib.request.urlopen(f"{self.base}{_LOG_PATH}") as r:
            entries = json.loads(r.read())
        index = [e for e in entries if not e[1].startswith("/sources/")]
        data = [e for e in entries if e[1].startswith("/sources/")]
        return {"index_requests": len(index), "index_bytes": sum(e[2] for e in index),
                "data_requests": len(data), "data_bytes": sum(e[2] for e in data)}


_LOG_PATH = "/__log__"  # answers with the log so far (unlogged, no latency), and clears it


class _Delay:
    """Holds callers for `latency` seconds, accurately.

    `time.sleep` can't be trusted for this: macOS coalesces timers, and a
    20 ms sleep can last 150 ms or more. One thread instead watches the clock
    and releases each waiting request when its time comes. Waiting on an
    Event without a timeout needs no timer.
    """

    def __init__(self, latency: float):
        self.latency = latency
        self._due: list[tuple[float, int, threading.Event]] = []
        self._cond = threading.Condition()
        self._seq = 0
        threading.Thread(target=self._run, daemon=True).start()

    def wait(self) -> None:
        if self.latency <= 0:
            return
        ev = threading.Event()
        with self._cond:
            self._seq += 1
            heapq.heappush(self._due, (time.perf_counter() + self.latency, self._seq, ev))
            self._cond.notify()
        ev.wait()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._due:
                    self._cond.wait()
                now = time.perf_counter()
                while self._due and self._due[0][0] <= now:
                    heapq.heappop(self._due)[2].set()
            time.sleep(0)  # let the handler threads run


def _serve(root: Path, latency: float, conn) -> None:
    log: list[tuple[str, str, int]] = []
    lock = threading.Lock()
    delay = _Delay(latency)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, status: int, headers: dict, data: bytes, logged: int | None) -> None:
            if logged is not None:  # before the body, so a client that has it sees the entry
                with lock:
                    log.append((self.command, self.path, logged))
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _serve(self, body: bool):
            if self.path == _LOG_PATH:
                with lock:
                    out = json.dumps(log).encode()
                    log.clear()
                return self._send(200, {"Content-Length": str(len(out))}, out, None)
            delay.wait()
            p = root / self.path.split("?")[0].lstrip("/")
            if not p.is_file():
                return self._send(404, {"Content-Length": "0"}, b"", 0)
            size = p.stat().st_size
            start, end = 0, size
            status, headers = 200, {}
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                a, b = rng[6:].split(",")[0].split("-")
                if a == "":
                    start = max(size - int(b), 0)
                else:
                    start, end = int(a), min(int(b) + 1, size) if b else size
                status, headers = 206, {"Content-Range": f"bytes {start}-{end - 1}/{size}"}
            headers.update({"Content-Length": str(end - start), "Accept-Ranges": "bytes"})
            data = b""
            if body:
                with open(p, "rb") as f:
                    f.seek(start)
                    data = f.read(end - start)
            self._send(status, headers, data, len(data))

        def do_GET(self):
            self._serve(True)

        def do_HEAD(self):
            self._serve(False)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    conn.send(httpd.server_address[1])
    httpd.serve_forever()


# -------------------------------------------------------------------- sources


def make_sources(out: Path, n_files: int, days: int, ny: int, nx: int, split: int) -> list[Path]:
    """Daily gridded fields, one netCDF4 file per `days` days, like a model
    archive: smooth fields with noise, zlib + shuffle, the grid of each day
    split into `split` × `split` chunks."""
    out.mkdir(parents=True, exist_ok=True)
    lat = np.linspace(-89.5, 89.5, ny)
    lon = np.linspace(0, 360, nx, endpoint=False)
    paths = []
    for i in range(n_files):
        p = out / f"model_{i:04d}.nc"
        paths.append(p)
        if p.exists():
            continue
        rng = np.random.default_rng(i)
        t = pd.date_range("2000-01-01", periods=days, freq="D") + pd.Timedelta(days=days * i)
        doy = t.dayofyear.to_numpy()[:, None, None]
        base = 288 - 30 * np.sin(np.deg2rad(lat))[None, :, None] ** 2
        season = 10 * np.cos(2 * np.pi * (doy - 200) / 365) * np.sin(np.deg2rad(lat))[None, :, None]
        temp = base + season + rng.normal(0, 1.5, (days, ny, nx))
        precip = np.clip(rng.gamma(0.4, 4, (days, ny, nx)) - 1, 0, None)
        ds = xr.Dataset(
            {"tas": (("time", "lat", "lon"), temp.astype("f4"), {"units": "K"}),
             "pr": (("time", "lat", "lon"), precip.astype("f4"), {"units": "mm/day"})},
            coords={"time": t, "lat": ("lat", lat, {"units": "degrees_north"}),
                    "lon": ("lon", lon, {"units": "degrees_east"})},
            attrs={"title": "synthetic daily fields", "file": i},
        )
        enc = {v: {"zlib": True, "shuffle": True, "complevel": 4,
                   "chunksizes": (1, -(-ny // split), -(-nx // split))} for v in ("tas", "pr")}
        ds.to_netcdf(p, engine="h5netcdf", encoding=enc)
    return paths


def virtualize(paths: list[Path], url_prefix: str, concat_dim: str, src_root: Path) -> xr.Dataset:
    """One virtual dataset for all files, its references pointing at
    `url_prefix` + the file's path relative to `src_root`."""
    from obspec_utils.registry import ObjectStoreRegistry
    from obstore.store import LocalStore
    from virtualizarr import open_virtual_dataset
    from virtualizarr.parsers import HDFParser

    registry = ObjectStoreRegistry({"file://": LocalStore()})
    vdss = []
    for p in paths:
        with xr.open_dataset(p, engine="h5netcdf") as ds:
            loadable = [c for c in ds.coords if c in ds.dims or ds[c].ndim <= 1]
        vds = open_virtual_dataset(url=f"file://{p.resolve()}", registry=registry,
                                   parser=HDFParser(), loadable_variables=loadable)
        vdss.append(vds)
    vds = vdss[0] if len(vdss) == 1 else xr.concat(
        vdss, dim=concat_dim, coords="minimal", compat="override", combine_attrs="override")
    # point the references at the HTTP server instead of the local files
    local = f"file://{src_root.resolve()}/"
    return vds.vz.rename_paths(lambda u: url_prefix + u.removeprefix(local))


# -------------------------------------------------------------------- formats
#
# One writer and one reader per format: everything a user of that format
# writes, given a virtual dataset (writer) or a URL (reader). Readers return a
# zarr store and the zarr format to open it with.


def write_kerchunk_json(vds: xr.Dataset, out: Path, ctx: dict) -> None:
    vds.vz.to_kerchunk(str(out), format="json")


def write_kerchunk_parquet(vds: xr.Dataset, out: Path, ctx: dict) -> None:
    vds.vz.to_kerchunk(str(out), format="parquet")


def open_kerchunk(url: str, ctx: dict):
    import fsspec
    from fsspec.implementations.asyn_wrapper import AsyncFileSystemWrapper

    fs = fsspec.filesystem("reference", fo=url, target_protocol="http", remote_protocol="http",
                           skip_instance_cache=True, lazy=True)
    return zarr.storage.FsspecStore(AsyncFileSystemWrapper(fs), read_only=True, path=""), 2


def _icechunk_container(ctx: dict):
    import icechunk

    return icechunk.VirtualChunkContainer(ctx["sources_url"], icechunk.http_store())


def write_icechunk(vds: xr.Dataset, out: Path, ctx: dict) -> None:
    import icechunk

    cfg = icechunk.RepositoryConfig.default()
    cfg.set_virtual_chunk_container(_icechunk_container(ctx))
    repo = icechunk.Repository.create(icechunk.local_filesystem_storage(str(out)), cfg,
                                      authorize_virtual_chunk_access={ctx["sources_url"]: icechunk.Credentials.HttpAccess()})
    session = repo.writable_session("main")
    vds.vz.to_icechunk(session.store)
    session.commit("virtualize")


def open_icechunk(url: str, ctx: dict):
    import icechunk

    repo = icechunk.Repository.open(icechunk.http_storage(url),
                                    authorize_virtual_chunk_access={ctx["sources_url"]: icechunk.Credentials.HttpAccess()})
    return repo.readonly_session("main").store, 3


def write_vzip(vds: xr.Dataset, out: Path, ctx: dict) -> None:
    from vzip.convert import write_vzip as write

    write(vds, out)


def write_vzip_paged(vds: xr.Dataset, out: Path, ctx: dict) -> None:
    from vzip.convert import write_vzip as write

    # with a page index, readers fetch the central directory a 64 KiB page at a time
    write(vds, out, page_size=1 << 16)


def open_vzip(url: str, ctx: dict):
    from vzip.store import VZipStore

    return VZipStore(url), 3


@dataclass
class Format:
    name: str
    path: str  # file or directory name under the format directory
    write: Callable
    open: Callable


FORMATS = [
    Format("kerchunk JSON", "refs.json", write_kerchunk_json, open_kerchunk),
    Format("kerchunk Parquet", "refs.parq", write_kerchunk_parquet, open_kerchunk),
    Format("Icechunk", "repo.icechunk", write_icechunk, open_icechunk),
    Format("vzip", "refs.vzip", write_vzip, open_vzip),
    Format("vzip, paged", "paged.vzip", write_vzip_paged, open_vzip),
]


# -------------------------------------------------------------------- measure


def disk_usage(p: Path) -> tuple[int, int]:
    files = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file()]
    return sum(f.stat().st_size for f in files), len(files)


def check(ds: xr.Dataset, expected: xr.Dataset) -> list[str]:
    """Variables whose values or coordinates differ from the source files."""
    bad = []
    for name in expected.data_vars:
        if name not in ds:
            bad.append(f"{name}: missing")
        elif not np.array_equal(ds[name].values, expected[name].values, equal_nan=True):
            bad.append(f"{name}: values differ")
    for name in expected.coords:
        if name not in ds.coords or not np.array_equal(ds[name].values, expected[name].values):
            bad.append(f"coordinate {name} differs")
    return bad


def measure(fmt: Format, url: str, ctx: dict, server: Server, probe: dict, full: bool) -> dict:
    server.take()
    row: dict = {}

    t0 = time.perf_counter()
    store, zarr_format = fmt.open(url, ctx)
    # xarray's default: use consolidated metadata if the store has it, else list the store
    ds = xr.open_zarr(store, zarr_format=zarr_format)
    row["open"] = {"seconds": time.perf_counter() - t0, **server.take()}

    t0 = time.perf_counter()
    var = probe["var"]
    value = ds[var].isel(probe["at"]).values
    row["one_value"] = {"seconds": time.perf_counter() - t0, **server.take()}
    expected = ctx["expected"]
    row["problems"] = [] if value == expected[var].isel(probe["at"]).values else [f"{var}: probed value differs"]

    if full:
        t0 = time.perf_counter()
        ds = ds.load()
        row["everything"] = {"seconds": time.perf_counter() - t0, **server.take()}
        row["problems"] += check(ds, expected)
    return row


# ------------------------------------------------------------------------ run


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", help="glob of netCDF4/HDF5 files to use instead of generated ones")
    ap.add_argument("--concat-dim", default="time", help="dimension to concatenate --input files on")
    ap.add_argument("--files", type=int, default=12, help="generated files (default 12)")
    ap.add_argument("--days", type=int, default=30, help="days per generated file (default 30)")
    ap.add_argument("--grid", type=int, nargs=2, default=(180, 360), metavar=("NY", "NX"))
    ap.add_argument("--split", type=int, default=2,
                    help="split each generated day into SPLIT x SPLIT chunks (default 2)")
    ap.add_argument("--no-full-read", action="store_true",
                    help="skip reading every variable in full (for large runs); only the probed value is checked")
    ap.add_argument("--latency", type=float, default=0.02, help="seconds added to every request")
    ap.add_argument("--only", action="append", help="run only formats whose name contains this")
    ap.add_argument("--out", help="JSON results file (default comparison/results/<run>.json)")
    ap.add_argument("--work", default=str(HERE / "_work"),
                    help="directory for the source files and the written formats")
    args = ap.parse_args()
    full = not args.no_full_read

    # import every format's libraries now, so that no format's timings include them
    import fsspec.implementations.reference  # noqa: F401
    import icechunk
    import virtualizarr  # noqa: F401
    from fsspec.implementations.asyn_wrapper import AsyncFileSystemWrapper  # noqa: F401

    import vzip.store  # noqa: F401

    icechunk.set_logs_filter("error")

    work = Path(args.work)
    shutil.rmtree(work / "formats", ignore_errors=True)
    src_root = work / "sources"
    if args.input:
        paths = [Path(p) for p in sorted(glob.glob(args.input))]
        if not paths:
            sys.exit(f"no files match {args.input}")
        shutil.rmtree(src_root, ignore_errors=True)
        src_root.mkdir(parents=True)
        for p in paths:  # served from here; hard links cost nothing
            dst = src_root / p.name
            try:
                dst.hardlink_to(p.resolve())
            except OSError:
                shutil.copy(p, dst)
        paths = [src_root / p.name for p in paths]
    else:
        ny, nx = args.grid
        paths = make_sources(src_root / f"gen_{args.files}x{args.days}_{ny}x{nx}_{args.split}",
                             args.files, args.days, ny, nx, args.split)

    server = Server(work, args.latency)
    ctx = {"sources_url": f"{server.base}/sources/"}
    print(f"{len(paths)} source files, {sum(p.stat().st_size for p in paths) / 1e6:.1f} MB", flush=True)
    vds = virtualize(paths, ctx["sources_url"], args.concat_dim, src_root)
    n_refs = sum(int(np.prod(v.data.manifest.shape_chunk_grid)) for v in vds.data_vars.values()
                 if hasattr(v.data, "manifest"))
    print(f"{n_refs} chunk references", flush=True)
    ctx["expected"] = xr.open_mfdataset(paths, engine="h5netcdf", combine="nested",
                                        concat_dim=args.concat_dim)
    if full:
        ctx["expected"] = ctx["expected"].load()
    var = next(n for n, v in ctx["expected"].data_vars.items() if v.ndim > 0)
    probe = {"var": var, "at": {d: s // 2 for d, s in ctx["expected"][var].sizes.items()}}

    fdir = work / "formats"
    fdir.mkdir(parents=True)
    rows = []
    for fmt in FORMATS:
        if args.only and not any(o.lower() in fmt.name.lower() for o in args.only):
            continue
        out = fdir / fmt.path
        t0 = time.perf_counter()
        fmt.write(vds, out, ctx)
        write_s = time.perf_counter() - t0
        size, objects = disk_usage(out)
        row = {"format": fmt.name, "n_refs": n_refs, "bytes": size, "objects": objects,
               "write_seconds": write_s,
               **measure(fmt, f"{server.base}/formats/{fmt.path}", ctx, server, probe, full)}
        rows.append(row)
        print(f"{fmt.name:17s} {size / 1e3:9.1f} kB  {objects:4d} objects  "
              + "  ".join(f"{k}: {row[k]['index_requests'] + row[k]['data_requests']:3d} req "
                          f"{row[k]['index_bytes'] / 1e3:8.1f} kB idx {row[k]['seconds']:6.2f}s"
                          for k in ("open", "one_value", "everything") if k in row)
              + ("  OK" if not row["problems"] else f"  MISMATCH {row['problems']}"), flush=True)

    meta = {"sources": len(paths), "source_bytes": sum(p.stat().st_size for p in paths),
            "n_refs": n_refs, "latency": args.latency, "full_read": full,
            "versions": {m: version(m) for m in ("vzip", "virtualizarr", "kerchunk", "fsspec",
                                                 "icechunk", "zarr", "xarray")},
            "input": args.input or (f"generated: {args.files} files x {args.days} days, grid "
                                    f"{args.grid[0]}x{args.grid[1]} in {args.split}x{args.split} chunks")}
    out = Path(args.out) if args.out else HERE / "results" / (
        f"{Path(args.input).stem if args.input else 'generated'}_{n_refs}refs.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"run": meta, "rows": rows}, indent=1))
    print()
    print(table(meta, rows))
    return 1 if any(r["problems"] for r in rows) else 0


def version(module: str) -> str:
    from importlib.metadata import version as v

    return v(module)


def table(meta: dict, rows: list[dict]) -> str:
    """The results as a Markdown table. Each step's cell: requests (of which
    to the format's own files), bytes of the format's own files, seconds."""

    def cell(r, k):
        if k not in r:
            return "–"
        x = r[k]
        n = x["index_requests"] + x["data_requests"]
        return f"{n} ({x['index_requests']}) · {x['index_bytes'] / 1e3:,.1f} kB · {x['seconds']:.2f} s"

    lines = [
        (f"{meta['n_refs']:,} chunk references into {meta['sources']} files "
         f"({meta['source_bytes'] / 1e6:,.0f} MB); {meta['latency'] * 1000:.0f} ms per request."),
        "",
        "| format | size | objects | open | first value | everything | values |",
        "|---|---:|---:|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['format']} | {r['bytes'] / 1e3:,.1f} kB | {r['objects']} | {cell(r, 'open')} "
            f"| {cell(r, 'one_value')} | {cell(r, 'everything')} "
            f"| {'identical' if not r['problems'] else 'DIFFER: ' + '; '.join(r['problems'])} |")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
