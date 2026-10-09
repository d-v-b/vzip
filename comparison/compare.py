"""Store one virtual dataset as kerchunk JSON, kerchunk Parquet, Icechunk and
vzip, then read each back over HTTP and compare.

1. Source files: real netCDF4/HDF5 files with compressed chunks, written here
   (deterministic), or your own with `--input`.
2. Virtualize them once with VirtualiZarr's HDF parser. Every format stores
   this same virtual dataset; its chunk references are `http://` URLs of the
   source files.
3. Write each format with its own library (one small function per format,
   below), and measure size on disk and object count.
   Every format is written twice: with its library's defaults, and with one
   documented setting changed for this workload (see VARIANTS).
4. Serve everything from a local HTTP server that honors Range requests,
   holds each response for a fixed latency plus its size over a per-response
   bandwidth (a stand-in for an object store), and logs every request.
5. For each format and each access pattern (open; one value; a time series
   at one grid point; one time step's map; everything), from a cold open,
   several times: count requests and bytes, split into the format's own
   files ("index") and the source files ("data"), and time it.
6. Check every value read against the source files.

Usage:
    uv run python comparison/compare.py                  # generated files
    uv run python comparison/compare.py --files 48 --days 31 --variables 20
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
    """Serves `root` with Range support and a log of (method, path, bytes
    sent). Each response is held for `latency` seconds plus its size divided
    by `bandwidth` (bytes per second, per response; 0 for unlimited).

    The server runs in its own process, so that the readers being measured
    don't compete with it for the GIL.
    """

    def __init__(self, root: Path, latency: float, bandwidth: float = 0):
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe()
        ctx.Process(target=_serve, args=(root, latency, bandwidth, child), daemon=True).start()
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
    """Holds callers for a given number of seconds, accurately.

    `time.sleep` can't be trusted for this: macOS coalesces timers, and a
    20 ms sleep can last 150 ms or more. One thread instead watches the clock
    and releases each waiting request when its time comes. Waiting on an
    Event without a timeout needs no timer.
    """

    def __init__(self):
        self._due: list[tuple[float, int, threading.Event]] = []
        self._cond = threading.Condition()
        self._seq = 0
        threading.Thread(target=self._run, daemon=True).start()

    def wait(self, seconds: float) -> None:
        if seconds <= 0:
            return
        ev = threading.Event()
        with self._cond:
            self._seq += 1
            heapq.heappush(self._due, (time.perf_counter() + seconds, self._seq, ev))
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


def _serve(root: Path, latency: float, bandwidth: float, conn) -> None:
    log: list[tuple[str, str, int]] = []
    lock = threading.Lock()
    delay = _Delay()

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
            p = root / self.path.split("?")[0].lstrip("/")
            if not p.is_file():
                delay.wait(latency)
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
            delay.wait(latency + (len(data) / bandwidth if bandwidth else 0))
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


def make_sources(out: Path, n_files: int, days: int, ny: int, nx: int, split: int,
                 n_vars: int = 2) -> list[Path]:
    """Daily gridded fields, one netCDF4 file per `days` days, like a model
    archive: smooth fields with noise, zlib + shuffle, the grid of each day
    split into `split` × `split` chunks. `tas` and `pr`, then `n_vars - 2`
    more variables like `tas`."""
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
        data = {"tas": (("time", "lat", "lon"), temp.astype("f4"), {"units": "K"}),
                "pr": (("time", "lat", "lon"), precip.astype("f4"), {"units": "mm/day"})}
        for k in range(n_vars - 2):
            data[f"v{k:03d}"] = (("time", "lat", "lon"),
                                 (temp + rng.normal(0, 1, temp.shape)).astype("f4"), {"units": "1"})
        ds = xr.Dataset(
            data,
            coords={"time": t, "lat": ("lat", lat, {"units": "degrees_north"}),
                    "lon": ("lon", lon, {"units": "degrees_east"})},
            attrs={"title": "synthetic daily fields", "file": i},
        )
        enc = {v: {"zlib": True, "shuffle": True, "complevel": 4,
                   "chunksizes": (1, -(-ny // split), -(-nx // split))} for v in data}
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
# One writer and one opener per format and setting: everything a user of that
# format writes, given a virtual dataset (writer) or a URL (opener). Openers
# return a zarr store and the zarr format to open it with.
#
# Each format runs with its library's defaults, and with one documented
# setting changed for this workload. For the formats with an index of
# references, that setting is the index's granularity, aimed at tens of
# kilobytes per index read: Parquet partitions of 10,000 references, one
# Icechunk manifest per 30 time steps (one source file), 64 KiB vzip pages.
# kerchunk JSON has no granularity to set; its setting is URL templates and
# gzip, as kerchunk's documentation suggests for large reference sets.


def write_kerchunk_json(vds, out, ctx, cfg) -> None:
    if cfg == "default":
        vds.vz.to_kerchunk(str(out), format="json")
        return
    import gzip
    import os

    refs = vds.vz.to_kerchunk(format="dict")
    urls = [v[0] for v in refs["refs"].values() if isinstance(v, list)]
    prefix = os.path.commonprefix(urls)
    refs["templates"] = {"u": prefix}
    refs["refs"] = {k: ["{{u}}" + v[0][len(prefix):], *v[1:]] if isinstance(v, list) else v
                    for k, v in refs["refs"].items()}
    out.write_bytes(gzip.compress(json.dumps(refs, separators=(",", ":")).encode(), 6))


def write_kerchunk_parquet(vds, out, ctx, cfg) -> None:
    vds.vz.to_kerchunk(str(out), format="parquet",
                       record_size=100_000 if cfg == "default" else 10_000)


def open_kerchunk(url, ctx, cfg):
    import fsspec

    target = {"compression": "gzip"} if url.endswith(".gz") else {}
    fs = fsspec.filesystem("reference", fo=url, target_protocol="http", target_options=target,
                           remote_protocol="http", remote_options={"asynchronous": True},
                           asynchronous=True)
    return zarr.storage.FsspecStore(fs, read_only=True, path=""), 2


def write_icechunk(vds, out, ctx, cfg) -> None:
    import icechunk

    rc = icechunk.RepositoryConfig.default()
    rc.set_virtual_chunk_container(
        icechunk.VirtualChunkContainer(ctx["sources_url"], icechunk.http_store()))
    if cfg == "tuned":
        rc.manifest = icechunk.ManifestConfig(splitting=icechunk.ManifestSplittingConfig.from_dict(
            {icechunk.ManifestSplitCondition.AnyArray(): {
                icechunk.ManifestSplitDimCondition.DimensionName("time"): ctx["days_per_file"]}}))
    repo = icechunk.Repository.create(icechunk.local_filesystem_storage(str(out)), rc,
                                      authorize_virtual_chunk_access=_icechunk_access(ctx))
    session = repo.writable_session("main")
    vds.vz.to_icechunk(session.store)
    session.commit("virtualize")


def open_icechunk(url, ctx, cfg):
    import icechunk

    rc = icechunk.RepositoryConfig.default()
    if cfg == "tuned":
        # one request per object; by default the HTTP store splits reads into ranges
        rc.storage = icechunk.StorageSettings(concurrency=icechunk.StorageConcurrencySettings(
            max_concurrent_requests_for_object=1, ideal_concurrent_request_size=64 << 20))
    repo = icechunk.Repository.open(icechunk.http_storage(url), config=rc,
                                    authorize_virtual_chunk_access=_icechunk_access(ctx))
    return repo.readonly_session("main").store, 3


def _icechunk_access(ctx):
    import icechunk

    return {ctx["sources_url"]: icechunk.Credentials.HttpAccess()}


def write_vzip(vds, out, ctx, cfg) -> None:
    from vzip.convert import write_vzip as write

    write(vds, out, page_size=None if cfg == "default" else 1 << 16)


def open_vzip(url, ctx, cfg):
    from vzip.policy import Policy
    from vzip.store import VZipStore

    # the benchmark serves the archive and its sources on 127.0.0.1, which the default
    # policy refuses (SPEC.md §8.7 rule 3)
    return VZipStore(url, policy=Policy(allow_private_hosts=True)), 3


@dataclass
class Format:
    name: str
    config: str  # "default" or "tuned"
    setting: str  # what "tuned" changes
    path: str  # file or directory name under the format directory
    write: Callable
    open: Callable

    @property
    def label(self) -> str:
        return f"{self.name} ({self.config})"


def _variants(name, setting, path, write, open_):
    stem, dot, ext = path.partition(".")
    return [Format(name, "default", "", path, write, open_),
            Format(name, "tuned", setting,
                   f"{stem}_tuned.{ext}.gz" if name == "kerchunk JSON" else f"{stem}_tuned{dot}{ext}",
                   write, open_)]


VARIANTS = [
    *_variants("kerchunk JSON", "URL templates, gzip", "refs.json", write_kerchunk_json, open_kerchunk),
    *_variants("kerchunk Parquet", "record_size=10,000 (default 100,000)", "refs.parq",
               write_kerchunk_parquet, open_kerchunk),
    *_variants("Icechunk", "manifest split every 30 time steps; one request per object",
               "repo.icechunk", write_icechunk, open_icechunk),
    *_variants("vzip", "page index, 64 KiB pages", "refs.vzip", write_vzip, open_vzip),
]


# -------------------------------------------------------------------- measure


def disk_usage(p: Path) -> tuple[int, int]:
    files = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file()]
    return sum(f.stat().st_size for f in files), len(files)


def history_bytes(p: Path) -> int:
    """Bytes of an Icechunk repository that record history and are not read
    to serve data (transaction logs)."""
    d = p / "transactions"
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.is_dir() else 0


def patterns(ds_expected: xr.Dataset, var: str, full: bool) -> dict[str, Callable]:
    """Access patterns: name -> fn(ds) returning the values read."""
    sizes = ds_expected[var].sizes
    mid = {d: s // 2 for d, s in sizes.items()}
    point = dict(mid)
    series = {d: i for d, i in mid.items() if d != "time"}
    out = {
        "point": lambda ds: ds[var].isel(point).values,
        "time series": lambda ds: ds[var].isel(series).values,
        "map": lambda ds: ds[var].isel(time=mid["time"]).values,
    }
    if full:
        out["everything"] = lambda ds: {n: ds[n].values for n in ds.variables}
    return out


def same(got, want) -> bool:
    if isinstance(want, dict):
        return all(n in got and np.array_equal(got[n], want[n], equal_nan=n not in ("time",))
                   for n in want)
    return np.array_equal(got, want, equal_nan=True)


def measure(fmt: Format, url: str, ctx: dict, server: Server, pats: dict, repeats: int) -> dict:
    """For each pattern, `repeats` cold opens, each followed by the pattern."""

    def open_ds():
        store, zarr_format = fmt.open(url, ctx, fmt.config)
        return xr.open_zarr(store, zarr_format=zarr_format, chunks=None)

    out: dict = {"problems": []}
    for name in ["open", *pats]:
        samples = []
        for _ in range(repeats):
            server.take()
            t0 = time.perf_counter()
            ds = open_ds()
            t_open, c_open = time.perf_counter() - t0, server.take()
            if name == "open":
                samples.append({"seconds": t_open, **c_open})
                continue
            t0 = time.perf_counter()
            got = pats[name](ds)
            samples.append({"seconds": time.perf_counter() - t0, **server.take()})
            if not same(got, ctx["expected"][name]):
                out["problems"].append(f"{name}: values differ")
        out[name] = samples
    return out


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
    ap.add_argument("--variables", type=int, default=2,
                    help="generated variables (default 2: tas, pr)")
    ap.add_argument("--no-full-read", action="store_true",
                    help="skip reading every variable in full (for large runs)")
    ap.add_argument("--latency", type=float, default=0.02, help="seconds added to every response")
    ap.add_argument("--bandwidth", type=float, default=100,
                    help="MB/s per response; 0 for unlimited (default 100)")
    ap.add_argument("--repeats", type=int, default=5, help="cold opens per access pattern")
    ap.add_argument("--only", action="append", help="run only variants whose label contains this")
    ap.add_argument("--out", help="JSON results file (default comparison/results/<run>.json)")
    ap.add_argument("--work", default=str(HERE / "_work"),
                    help="directory for the source files and the written formats")
    args = ap.parse_args()
    full = not args.no_full_read

    # import every format's libraries now, so that no format's timings include them
    import fsspec.implementations.reference  # noqa: F401
    import icechunk
    import virtualizarr  # noqa: F401

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
        paths = make_sources(
            src_root / f"gen_{args.files}x{args.days}_{ny}x{nx}_{args.split}_{args.variables}v",
            args.files, args.days, ny, nx, args.split, args.variables)

    server = Server(work, args.latency, args.bandwidth * 1e6)
    print(f"{len(paths)} source files, {sum(p.stat().st_size for p in paths) / 1e6:.1f} MB", flush=True)
    ctx = {"sources_url": f"{server.base}/sources/"}
    vds = virtualize(paths, ctx["sources_url"], args.concat_dim, src_root)
    n_refs = sum(int(np.prod(v.data.manifest.shape_chunk_grid)) for v in vds.data_vars.values()
                 if hasattr(v.data, "manifest"))
    print(f"{n_refs} chunk references", flush=True)
    with xr.open_dataset(paths[0], engine="h5netcdf") as first:
        ctx["days_per_file"] = first.sizes.get(args.concat_dim, 1)
    source = xr.open_mfdataset(paths, engine="h5netcdf", combine="nested", concat_dim=args.concat_dim)
    var = next(n for n, v in source.data_vars.items() if v.ndim == 3)
    pats = patterns(source, var, full)
    ctx["expected"] = {n: fn(source) for n, fn in pats.items()}

    fdir = work / "formats"
    fdir.mkdir(parents=True)
    rows = []
    for fmt in VARIANTS:
        if args.only and not any(o.lower() in fmt.label.lower() for o in args.only):
            continue
        out = fdir / fmt.path
        t0 = time.perf_counter()
        fmt.write(vds, out, ctx, fmt.config)
        write_s = time.perf_counter() - t0
        size, objects = disk_usage(out)
        row = {"format": fmt.name, "config": fmt.config, "setting": fmt.setting,
               "n_refs": n_refs, "bytes": size, "history_bytes": history_bytes(out),
               "objects": objects, "write_seconds": write_s,
               **measure(fmt, f"{server.base}/formats/{fmt.path}", ctx, server, pats, args.repeats)}
        rows.append(row)
        print(f"{fmt.label:28s} {size / 1e3:9.1f} kB  "
              + "  ".join(f"{k}: {_count(row[k])} req {_med(row[k], 'index_bytes') / 1e3:.1f} kB "
                          f"{_med(row[k], 'seconds'):.2f}s" for k in ["open", *pats])
              + ("  OK" if not row["problems"] else f"  MISMATCH {row['problems']}"), flush=True)

    meta = {"sources": len(paths), "source_bytes": sum(p.stat().st_size for p in paths),
            "n_refs": n_refs, "variable": var, "latency": args.latency,
            "bandwidth_mb_s": args.bandwidth, "repeats": args.repeats, "full_read": full,
            "versions": {m: version(m) for m in ("vzip", "virtualizarr", "kerchunk", "fsspec",
                                                 "icechunk", "zarr", "xarray")},
            "input": args.input or (f"generated: {args.files} files x {args.days} days, "
                                    f"{args.variables} variables, grid {args.grid[0]}x{args.grid[1]} "
                                    f"in {args.split}x{args.split} chunks")}
    out = Path(args.out) if args.out else HERE / "results" / (
        f"{Path(args.input).stem if args.input else 'generated'}_{args.variables}v_{n_refs}refs.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"run": meta, "rows": rows}, indent=1))
    print()
    print(tables(meta, rows))
    return 1 if any(r["problems"] for r in rows) else 0


def version(module: str) -> str:
    from importlib.metadata import version as v

    return v(module)


def _med(samples: list[dict], key: str) -> float:
    return float(np.median([s[key] for s in samples]))


def _count(samples: list[dict]) -> str:
    """Requests in each repeat: one number if they agree, else min–max."""
    n = sorted(s["index_requests"] + s["data_requests"] for s in samples)
    return f"{n[0]:,}" if n[0] == n[-1] else f"{n[0]:,}–{n[-1]:,}"


def _kb(b: float) -> str:
    return "0" if b == 0 else f"{b / 1e6:,.2f} MB" if b >= 1e6 else f"{b / 1e3:,.1f} kB"


def tables(meta: dict, rows: list[dict]) -> str:
    """Markdown: sizes; requests and bytes (deterministic up to concurrency);
    seconds as median (min–max) over the repeats."""
    steps = [k for k in ("open", "point", "time series", "map", "everything") if k in rows[0]]
    head = "| format | setting | " + " | ".join(steps) + " |"
    sep = "|---|---|" + "---|" * len(steps)
    lines = [
        (f"{meta['n_refs']:,} chunk references into {meta['sources']} files "
         f"({meta['source_bytes'] / 1e6:,.0f} MB); {meta['latency'] * 1000:.0f} ms + "
         f"{meta['bandwidth_mb_s']:g} MB/s per response; {meta['repeats']} cold opens per step."),
        "",
        "**Size and write time**",
        "",
        "| format | setting | size | objects | write |",
        "|---|---|---:|---:|---:|",
    ]
    for r in rows:
        hist = f" (of which history {_kb(r['history_bytes'])})" if r["history_bytes"] else ""
        lines.append(f"| {r['format']} | {r['config']} | {_kb(r['bytes'])}{hist} | {r['objects']} "
                     f"| {r['write_seconds']:.2f} s |")
    lines += ["", "**Requests (of which to the format's own files) · bytes of the format's own files**",
              "", head, sep]
    for r in rows:
        cells = []
        for k in steps:
            idx = sorted({s["index_requests"] for s in r[k]})
            idx_s = f"{idx[0]}" if len(idx) == 1 else f"{idx[0]}–{idx[-1]}"
            cells.append(f"{_count(r[k])} ({idx_s}) · {_kb(_med(r[k], 'index_bytes'))}")
        lines.append(f"| {r['format']} | {r['config']} | " + " | ".join(cells) + " |")
    lines += ["", "**Seconds, median (min–max)**", "", head, sep]
    for r in rows:
        cells = []
        for k in steps:
            t = sorted(s["seconds"] for s in r[k])
            cells.append(f"{_med(r[k], 'seconds'):.2f} ({t[0]:.2f}–{t[-1]:.2f})")
        lines.append(f"| {r['format']} | {r['config']} | " + " | ".join(cells) + " |")
    bad = [f"{r['format']} ({r['config']}): {'; '.join(r['problems'])}" for r in rows if r["problems"]]
    lines += ["", "Values: " + ("every value read matched the source files." if not bad
                               else "MISMATCH " + "; ".join(bad))]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
