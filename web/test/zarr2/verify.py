"""Checks the browser Zarr v2 virtualizer's output against zarr-python's
own Zarr v2 reader.

Each store in web/test/fixtures/zarr2/ is served by the harness proxy (with
its S3 listing) and virtualized by web/conformance/virtualize.ts (the browser
code, run under Node). Every array of the archive is read through VZipStore
and zarr-python as Zarr v3 and must equal the same array read from the
fixture directory by zarr-python as Zarr v2 (`zarr_format=2`, which reads
`.zarray` itself and decodes chunks with numcodecs), with the same data type
and fill value. Zero-size chunk objects have no entry (VIRTUALIZE.md §1.4),
so the reference reads a copy of the store without them. `zarr2_reject_*`
stores must be rejected.

With `--remote <store URL> <array path>`, a public store is virtualized
directly and that array is compared with zarr-python reading the Zarr v2
array over HTTP (the pixel check of conformance/virtualize/corpus_zarr2.txt).

Usage: uv run python web/test/zarr2/verify.py [<store dir> ...]
       uv run python web/test/zarr2/verify.py --remote <store URL> <array path>
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import zarr
from zarr.storage import FsspecStore, LocalStore

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
FIXTURES = HERE.parent / "fixtures"
sys.path.insert(0, str(ROOT / "conformance" / "virtualize"))
from proxy import Proxy  # noqa: E402

import vzip.codecs  # noqa: E402,F401  (registers zlib)
from vzip.store import VZipStore  # noqa: E402


def arrays_of(out: Path) -> list[str]:
    z = zipfile.ZipFile(out)
    return sorted(name[: -len("zarr.json")].rstrip("/") for name in z.namelist()
                  if name.endswith("zarr.json") and json.loads(z.read(name)).get("node_type") == "array")


def virtualize(url: str, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["node", str(ROOT / "web" / "conformance" / "virtualize.ts"), url, str(out)],
                          capture_output=True, text=True)


def same(got, want) -> bool:
    return (got.dtype == want.dtype.newbyteorder("=") and got.shape == want.shape
            and np.array_equal(got, want, equal_nan=got.dtype.kind == "f"))


def check_store(store_dir: Path, out: Path, tmp: str) -> list[str]:
    copy = Path(tmp) / f"{store_dir.name}.ref"
    shutil.copytree(store_dir, copy)
    for f in copy.rglob("*"):
        if f.is_file() and f.stat().st_size == 0:
            f.unlink()
    problems = []
    vz = VZipStore(str(out))
    for path in arrays_of(out):
        got_arr = zarr.open_array(vz, path=path, mode="r", zarr_format=3)
        want_arr = zarr.open_array(LocalStore(copy, read_only=True), path=path, mode="r", zarr_format=2)
        got, want = got_arr[...], want_arr[...]
        if not same(got, want):
            problems.append(f"{path}: {got.dtype}{got.shape} differs from Zarr v2's {want.dtype}{want.shape}")
        fv, wf = got_arr.metadata.fill_value, want_arr.metadata.fill_value
        if wf is not None and not (fv == wf or (np.isnan(fv) and np.isnan(wf)) if got.dtype.kind == "f" else fv == wf):
            problems.append(f"{path}: fill value {fv} != {wf}")
    return problems


def remote(store_url: str, path: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "remote.vzip"
        p = virtualize(store_url, out)
        if p.returncode:
            print(f"FAILED: {p.stderr[-300:]}")
            return 1
        print(p.stdout.strip()[:300])
        got = zarr.open_array(VZipStore(str(out)), path=path, mode="r", zarr_format=3)[...]
        want = zarr.open_array(FsspecStore.from_url(store_url, read_only=True), path=path, mode="r",
                               zarr_format=2)[...]
        ok = same(got, want)
        print(f"{path}: {got.dtype}{got.shape}, {'equal to' if ok else 'DIFFERS from'} zarr-python's Zarr v2 "
              f"reading, {int(np.count_nonzero(want))} nonzero values")
        return 0 if ok else 1


def main(argv: list[str]) -> int:
    if argv[:1] == ["--remote"]:
        return remote(argv[1], argv[2])
    stores = [Path(a) for a in argv] or sorted(d for d in (FIXTURES / "zarr2").iterdir() if d.is_dir())
    proxy = Proxy(FIXTURES, Path(tempfile.mkdtemp()))
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for store_dir in stores:
            out = Path(tmp) / f"{store_dir.name}.vzip"
            p = virtualize(proxy.local_store(store_dir.resolve().relative_to(FIXTURES.resolve()).as_posix()), out)
            if store_dir.name.startswith("zarr2_reject"):
                ok = p.returncode == 3 and not out.exists()
                failures += not ok
                print(f"{store_dir.name:40s} {'rejected: ' + p.stderr.strip()[-90:] if ok else 'NOT REJECTED ' + p.stderr[-200:]}")
                continue
            if p.returncode:
                failures += 1
                print(f"{store_dir.name:40s} FAILED: {p.stderr.strip()[-300:]}")
                continue
            problems = check_store(store_dir, out, tmp)
            failures += bool(problems)
            print(f"{store_dir.name:40s} {problems[:3] if problems else 'ok'} ({len(arrays_of(out))} arrays)")
    print(f"\n{failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
