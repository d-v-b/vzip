"""Compare vzip with kerchunk JSON, kerchunk parquet and icechunk.

Synthetic virtual dataset: one uint8 array of N chunks of 1 KiB, spread over
N/1000 (sparse) local files, so every format can really resolve a chunk.

Usage: uv run python experiments/bench.py [N ...]
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import xarray as xr
import zarr
from virtualizarr.manifests import ChunkManifest, ManifestArray
from virtualizarr.manifests.utils import create_v3_array_metadata

from refstore.convert import write_vzip
from refstore.shards import write_vzip_sharded
from refstore.store import VZipStore

warnings.filterwarnings("ignore")
ROOT = Path(__file__).parent / "_bench"
CHUNK = 1024
PER_FILE = 1000


def layout(n: int, seed: int = 0):
    """Realistic chunk layout: compressed chunks of 50-500 kB, small gaps between them.

    The probed chunk (n // 2) is exactly CHUNK bytes so the uint8/bytes array decodes.
    """
    rng = np.random.default_rng(seed)
    lengths = rng.integers(50_000, 500_000, n).astype("u8")
    lengths[n // 2] = CHUNK
    gaps = rng.integers(0, 4096, n).astype("u8")
    offsets = np.empty(n, "u8")
    file_ends = []
    for f in range(0, n, PER_FILE):
        seg = slice(f, f + PER_FILE)
        ends = np.cumsum(gaps[seg] + lengths[seg])
        offsets[seg] = ends - lengths[seg]
        file_ends.append(int(ends[-1]))
    return offsets, lengths, file_ends


def make_targets(file_ends: list[int]) -> Path:
    tdir = ROOT / "targets"
    tdir.mkdir(parents=True, exist_ok=True)
    for i, end in enumerate(file_ends):
        p = tdir / f"model_output_v2_ensemble_member_{i:05d}.nc"
        if not p.exists() or p.stat().st_size < end:
            with open(p, "wb") as f:
                f.truncate(end)  # sparse
    return tdir


def synthetic(n: int, tdir: Path | str, offsets, lengths) -> xr.Dataset:
    """`tdir` is a directory (-> file:// URLs) or a URL prefix ending in '/'."""
    prefix = tdir if isinstance(tdir, str) else f"file://{tdir}/"
    i = np.arange(n)
    paths = np.array(
        [f"{prefix}model_output_v2_ensemble_member_{k:05d}.nc" for k in i // PER_FILE],
        dtype=np.dtypes.StringDType(),
    ).reshape(n, 1)
    manifest = ChunkManifest.from_arrays(
        paths=paths, offsets=offsets.reshape(n, 1), lengths=lengths.reshape(n, 1)
    )
    md = create_v3_array_metadata(
        shape=(n, CHUNK), data_type=np.dtype("u1"), chunk_shape=(1, CHUNK), fill_value=0,
        codecs=[{"name": "bytes"}],
    )
    return xr.Dataset({"v": (("y", "x"), ManifestArray(md, manifest))})


def dir_size(p: Path) -> int:
    return p.stat().st_size if p.is_file() else sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


# ----------------------------------------------------------------- writers/readers


def w_vzip(vds, out, mirror=True):
    write_vzip(vds, out, mirror_refs=mirror)


def r_vzip(out, idx):
    s = VZipStore(str(out))
    a = zarr.open_array(s, path="v", mode="r")
    val = a[idx]
    return val, {"requests": s.stats.archive_requests + s.stats.external_requests,
                 "index_bytes_read": s.stats.archive_bytes}


def w_kjson(vds, out):
    vds.vz.to_kerchunk(str(out), format="json")


def w_kparq(vds, out):
    vds.vz.to_kerchunk(str(out), format="parquet")


def _fsspec_store(out):
    import fsspec
    from fsspec.implementations.asyn_wrapper import AsyncFileSystemWrapper

    fs = fsspec.filesystem("reference", fo=str(out), remote_protocol="file", lazy=True)
    return zarr.storage.FsspecStore(AsyncFileSystemWrapper(fs), read_only=True, path="")


def r_kerchunk(out, idx):
    a = zarr.open_array(_fsspec_store(out), path="v", mode="r", zarr_format=2)
    return a[idx], {}


def _ic_config(tdir):
    import icechunk

    prefix = f"file://{tdir}/"
    cfg = icechunk.RepositoryConfig.default()
    cfg.set_virtual_chunk_container(
        icechunk.VirtualChunkContainer(prefix, icechunk.local_filesystem_store(str(tdir)))
    )
    return cfg, {prefix: None}


def w_icechunk(vds, out, tdir):
    import icechunk

    cfg, auth = _ic_config(tdir)
    repo = icechunk.Repository.create(
        icechunk.local_filesystem_storage(str(out)), cfg, authorize_virtual_chunk_access=auth
    )
    session = repo.writable_session("main")
    vds.vz.to_icechunk(session.store)
    session.commit("write")


def r_icechunk(out, idx, tdir):
    import icechunk

    _, auth = _ic_config(tdir)
    repo = icechunk.Repository.open(
        icechunk.local_filesystem_storage(str(out)), authorize_virtual_chunk_access=auth
    )
    store = repo.readonly_session("main").store
    return zarr.open_array(store, path="v", mode="r")[idx], {}


def run(n: int) -> list[dict]:
    offsets, lengths, file_ends = layout(n)
    tdir = make_targets(file_ends)
    vds = synthetic(n, tdir, offsets, lengths)
    out_dir = ROOT / f"n{n}"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    idx = n // 2
    formats = {
        "vzip": (lambda o: w_vzip(vds, o), r_vzip, "x.vzip"),
        "vzip (no mirror)": (lambda o: w_vzip(vds, o, mirror=False), r_vzip, "nomirror.vzip"),
        "vzip (paged CD)": (lambda o: write_vzip(vds, o, page_size=1 << 16), r_vzip, "paged.vzip"),
        "vzip shards, inline idx": (
            lambda o: write_vzip_sharded(vds, o, {"v": (PER_FILE, 1)}, inline_index=True),
            r_vzip, "shards_inline.vzip"),
        "vzip shards, lazy idx": (
            lambda o: write_vzip_sharded(vds, o, {"v": (PER_FILE, 1)}), r_vzip, "shards.vzip"),
        "kerchunk json": (lambda o: w_kjson(vds, o), r_kerchunk, "refs.json"),
        "kerchunk parquet": (lambda o: w_kparq(vds, o), r_kerchunk, "refs.parq"),
        "icechunk": (lambda o: w_icechunk(vds, o, tdir),
                     lambda o, i: r_icechunk(o, i, tdir), "ic"),
    }
    rows = []
    for name, (w, r, fname) in formats.items():
        out = out_dir / fname
        t0 = time.perf_counter()
        w(out)
        t_write = time.perf_counter() - t0
        t0 = time.perf_counter()
        val, extra = r(out, idx)
        t_read = time.perf_counter() - t0
        assert val.shape == (CHUNK,)
        size = dir_size(out)
        n_objects = 1 if out.is_file() else sum(1 for f in out.rglob("*") if f.is_file())
        rows.append({
            "n_refs": n, "format": name, "bytes": size, "bytes_per_ref": size / n,
            "objects": n_objects, "write_s": t_write, "open_and_read_one_chunk_s": t_read, **extra,
        })
        print(f"{n:>9} {name:24s} {size / n:7.1f} B/ref  {size / 1e6:8.2f} MB  "
              f"objs={n_objects:<4d} write={t_write:6.2f}s  open+read={t_read:6.3f}s  {extra}",
              flush=True)
    return rows


if __name__ == "__main__":
    ns = [int(x) for x in sys.argv[1:]] or [10_000, 100_000, 1_000_000]
    rows = [r for n in ns for r in run(n)]
    res = Path(__file__).parent / "results"
    res.mkdir(exist_ok=True)
    (res / "bench.json").write_text(json.dumps(rows, indent=1))
