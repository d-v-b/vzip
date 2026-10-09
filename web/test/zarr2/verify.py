"""Checks the browser Zarr v2 virtualizer's output against zarr-python's
own Zarr v2 reader.

Each store in web/test/fixtures/zarr2/ is served by the harness proxy (with
its S3 listing) and virtualized by web/conformance/virtualize.ts (the browser
code, run under Node). Every array of the archive is read through VZipStore
and zarr-python as Zarr v3 and must equal the same array read from the
fixture directory by zarr-python as Zarr v2 (`zarr_format=2`, which reads
`.zarray` itself and decodes chunks with numcodecs), with the same data type
and fill value. Zero-size chunk objects have no entry (VIRTUALIZE.md §1.4),
so the reference reads a copy of the store without them (zarr-python fails on
them); their keys are listed with the empty objects. Each object under
`vzip_source/objects/` must read as the store's object at its unescaped key,
none may open as a Zarr node, and the keys listed as empty must be exactly
the store's empty objects, and none listed as ignored (conventions/zarr2/README.md §5). `zarr2_reject_*` stores must be
rejected.

With `--remote <store URL> <array path>`, a public store is virtualized
directly and that array is compared with zarr-python reading the Zarr v2
array over HTTP (the pixel check of conformance/virtualize/corpus_zarr2.txt).

Usage: uv run python web/test/zarr2/verify.py [<store dir> ...]
       uv run python web/test/zarr2/verify.py --remote <store URL> <array path>
"""

from __future__ import annotations

import asyncio
import json
import re
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
from vzip.policy import Policy  # noqa: E402
from vzip.store import VZipStore  # noqa: E402

# The sources are served on 127.0.0.1, which spec §8.7 rule 3 refuses by default.
LOOPBACK_SOURCES = Policy(allow_private_hosts=True)


NUMCODECS = {"blosc": {"id", "cname", "clevel", "shuffle", "blocksize"}, "zlib": {"id", "level"},
             "gzip": {"id", "level"}, "zstd": {"id", "level", "checksum"}}


def arrays_of(out: Path) -> list[str]:
    z = zipfile.ZipFile(out)
    return sorted(name[: -len("zarr.json")].rstrip("/") for name in z.namelist()
                  if name.endswith("zarr.json") and json.loads(z.read(name)).get("node_type") == "array")


def virtualize(url: str, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["node", str(ROOT / "web" / "conformance" / "virtualize.ts"), "--allow-private-hosts", url, str(out)],
                          capture_output=True, text=True)


def same(got, want) -> bool:
    return (got.dtype == want.dtype.newbyteorder("=") and got.shape == want.shape
            and np.array_equal(got, want, equal_nan=got.dtype.kind == "f"))


# A last segment escaped with ~ (conventions/zarr2/README.md §5, conventions/n5/README.md §6).
ESCAPED = re.compile(r"(?:zarr\.json|\.zarray|\.zgroup)~+\Z")
NODE_NAME = re.compile(r"(?:zarr\.json|\.zarray|\.zgroup)\Z")


def objects_problems(store_dir: Path, out: Path) -> list[str]:
    """The other objects and the empty ones against the store (conventions/zarr2/README.md §5,
    conventions/n5/README.md §6): each entry under vzip_source/objects/ is its object, whose key
    is the entry's with one ~ taken off an escaped last segment; no Zarr node opens there; and
    every key listed as empty is an object of size 0."""
    problems = []
    names = zipfile.ZipFile(out).namelist()
    vz = VZipStore(str(out), policy=LOOPBACK_SOURCES)
    kept = [n for n in names if n.startswith("vzip_source/objects/")]

    async def read_all():
        from zarr.core.buffer import default_buffer_prototype
        return [(await vz.get(n, default_buffer_prototype())).to_bytes() for n in kept]

    for name, data in zip(kept, asyncio.run(read_all())):
        key = name.removeprefix("vzip_source/objects/")
        last = key.rpartition("/")[2]
        if NODE_NAME.match(last):
            problems.append(f"{name}: a node document's name, not escaped")
        key = key[:-1] if ESCAPED.match(last) else key
        if not (store_dir / key).is_file() or (store_dir / key).read_bytes() != data:
            problems.append(f"{name}: not the object {key}")
    for d in sorted({"/".join(n.split("/")[:i]) for n in kept for i in range(2, n.count("/") + 1)}):
        for fmt in (None, 2, 3):
            try:
                zarr.open(vz, path=d, mode="r", zarr_format=fmt)
                problems.append(f"{d} opens as a Zarr node (zarr_format={fmt})")
            except Exception:  # noqa: BLE001 - not a node, as it should be
                pass
    own = {}
    if "vzip_source/zarr.json" in names:
        attrs = json.loads(zipfile.ZipFile(out).read("vzip_source/zarr.json"))["attributes"].get("vzip_virtualized", {})
        own = next(iter(attrs.values()), {})
    if "vzip_source/empty.json" in names:
        own = json.loads(zipfile.ZipFile(out).read("vzip_source/empty.json"))
    empty = own.get("empty", [])
    for key in empty:
        if not (store_dir / key).is_file() or (store_dir / key).stat().st_size:
            problems.append(f"empty {key} is not an empty object")
    # Every empty object, an empty chunk object included, is listed (a directory has no ignored keys).
    zero = {f.relative_to(store_dir).as_posix() for f in store_dir.rglob("*") if f.is_file() and not f.stat().st_size}
    if zero != set(empty):
        problems.append(f"empty objects not listed: {sorted(zero - set(empty))[:3]}")
    if "ignored" in own:
        problems.append(f"ignored keys from a directory: {own['ignored'][:3]}")
    if empty != sorted(empty, key=lambda k: k.encode()):
        problems.append("the empty keys are not in UTF-8 byte order")
    return problems


def check_store(store_dir: Path, out: Path, tmp: str) -> list[str]:
    copy = Path(tmp) / f"{store_dir.name}.ref"
    shutil.copytree(store_dir, copy)
    for f in copy.rglob("*"):
        if f.is_file() and f.stat().st_size == 0:
            f.unlink()
    # numcodecs takes no member it does not know; the archive keeps such members as source
    # metadata (conventions/zarr2/README.md §4), and they do not change how chunks decode.
    for f in copy.rglob(".zarray"):
        z = json.loads(f.read_text())
        c = z.get("compressor")
        if isinstance(c, dict) and c.get("id") in NUMCODECS:
            z["compressor"] = {k: v for k, v in c.items() if k in NUMCODECS[c["id"]]}
            f.write_text(json.dumps(z))
    problems = []
    vz = VZipStore(str(out), policy=LOOPBACK_SOURCES)
    for path in arrays_of(out):
        got_arr = zarr.open_array(vz, path=path, mode="r", zarr_format=3)
        want_arr = zarr.open_array(LocalStore(copy, read_only=True), path=path, mode="r", zarr_format=2)
        got, want = got_arr[...], want_arr[...]
        if not same(got, want):
            problems.append(f"{path}: {got.dtype}{got.shape} differs from Zarr v2's {want.dtype}{want.shape}")
        fv, wf = got_arr.metadata.fill_value, want_arr.metadata.fill_value
        if wf is not None and not (fv == wf or (np.isnan(fv) and np.isnan(wf)) if got.dtype.kind == "f" else fv == wf):
            problems.append(f"{path}: fill value {fv} != {wf}")
    return problems + objects_problems(store_dir, out)


def remote(store_url: str, path: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "remote.vzip"
        p = virtualize(store_url, out)
        if p.returncode:
            print(f"FAILED: {p.stderr[-300:]}")
            return 1
        print(p.stdout.strip()[:300])
        got = zarr.open_array(VZipStore(str(out), policy=LOOPBACK_SOURCES), path=path, mode="r", zarr_format=3)[...]
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
