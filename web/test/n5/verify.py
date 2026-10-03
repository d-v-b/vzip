"""Checks the browser N5 virtualizer's output against an independent N5 reader.

Each store in web/test/fixtures/n5/ is served by the harness proxy (with its
S3 listing) and virtualized by web/conformance/virtualize.ts (the browser
code, run under Node). Every array of the archive is read through VZipStore
and zarr-python (with vzip.codecs' n5_default codec) and must equal:

- the dataset as read by `read_n5` below, a block reader written from the N5
  specification alone (header, column-major big-endian elements, each
  compression), which places each block, truncated or padded, into an array
  of the dataset's dimensions (missing and empty blocks read as 0); and
- the dataset as zarr-n5 reads it (an independent n5_default implementation),
  where zarr-n5 supports it.

`n5_reject_*` stores must be rejected; reading `n5_edge_varlength_block`'s
array must fail (its varlength block is refused by the codec).

With `--remote <store URL> <array path>`, a public store is virtualized
directly and that array is compared with `read_n5` reading the blocks over
HTTP (the pixel check of conformance/virtualize/corpus_n5.txt).

Usage: uv run python web/test/n5/verify.py [<store dir> ...]
       uv run python web/test/n5/verify.py --remote <store URL> <array path>
"""

from __future__ import annotations

import gzip
import json
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zlib
from pathlib import Path

import numcodecs
import numpy as np
import zarr

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
FIXTURES = HERE.parent / "fixtures"
sys.path.insert(0, str(ROOT / "conformance" / "virtualize"))
from proxy import Proxy  # noqa: E402

import vzip.codecs  # noqa: E402,F401  (registers n5_default and zlib)
from vzip.store import VZipStore  # noqa: E402


def decompress(body: bytes, compression: dict) -> bytes:
    t = compression["type"]
    if t == "raw":
        return body
    if t == "gzip":
        return zlib.decompress(body) if compression.get("useZlib") else gzip.decompress(body)
    if t == "zstd":
        return numcodecs.Zstd().decode(body)
    if t == "blosc":
        return numcodecs.Blosc().decode(body)
    raise ValueError(f"compression {t}")


def read_n5(attrs: dict, get) -> np.ndarray:
    """The dataset with attributes `attrs`, whose block at key "i/j/..." is
    `get(key)` (None if missing). Indexed in N5 dimension order."""
    dims, block = attrs["dimensions"], attrs["blockSize"]
    compression = attrs.get("compression") or {"type": attrs["compressionType"]}
    dtype = np.dtype(attrs["dataType"]).newbyteorder(">")
    out = np.zeros(dims, dtype=dtype.newbyteorder("="))
    for idx in np.ndindex(*[-(-d // b) for d, b in zip(dims, block)]):
        raw = get("/".join(map(str, idx)))
        if not raw:
            continue
        mode, ndim = struct.unpack_from(">HH", raw)
        if mode != 0:
            raise ValueError(f"block {idx} has mode {mode}")
        shape = struct.unpack_from(f">{ndim}I", raw, 4)
        body = decompress(raw[4 + 4 * ndim :], compression)
        n = int(np.prod(shape))
        # Column-major: the first dimension varies fastest.
        values = np.frombuffer(body, dtype=dtype, count=n).reshape(shape[::-1]).T
        dest = tuple(slice(i * b, min((i + 1) * b, d)) for i, b, d in zip(idx, block, dims))
        take = tuple(slice(0, min(s.stop - s.start, m)) for s, m in zip(dest, shape))
        dest = tuple(slice(s.start, s.start + t.stop) for s, t in zip(dest, take))
        out[dest] = values[take]
    return out


def zarr_n5_read(store_dir: Path, path: str) -> np.ndarray | None:
    """The dataset as zarr-n5 reads it, or None if it cannot."""
    try:
        from zarr.storage import LocalStore
        from zarr_n5 import N5WrapperStore
    except ImportError:
        return None
    with zarr.config.set({"codecs.n5_default": "zarr_n5.codec.default.N5DefaultCodec"}):
        try:
            a = zarr.open_array(N5WrapperStore(LocalStore(store_dir, read_only=True)), path=path, mode="r",
                                zarr_format=3)
            return a[...]
        except Exception:  # noqa: BLE001  (unsupported by zarr-n5: legacy compressionType, ...)
            return None


def arrays_of(out: Path) -> list[str]:
    import zipfile

    z = zipfile.ZipFile(out)
    paths = []
    for name in z.namelist():
        if name.endswith("zarr.json") and json.loads(z.read(name)).get("node_type") == "array":
            paths.append(name[: -len("zarr.json")].rstrip("/"))
    return sorted(paths)


def virtualize(url: str, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["node", str(ROOT / "web" / "conformance" / "virtualize.ts"), url, str(out)],
                          capture_output=True, text=True)


def check_store(store_dir: Path, out: Path) -> list[str]:
    problems = []
    vz = VZipStore(str(out))
    for path in arrays_of(out):
        attrs = json.loads((store_dir / path / "attributes.json").read_text())
        arr = zarr.open_array(vz, path=path, mode="r", zarr_format=3)
        if store_dir.name == "n5_edge_varlength_block":
            try:
                arr[...]
            except Exception:  # noqa: BLE001
                continue
            problems.append(f"{path}: the varlength block was read")
            continue
        got = arr[...]

        def get(key, base=store_dir / path):
            f = base / key
            return f.read_bytes() if f.is_file() else None

        want = read_n5(attrs, get)
        if got.dtype != want.dtype or got.shape != want.shape or not np.array_equal(got, want, equal_nan=True):
            problems.append(f"{path}: differs from read_n5 ({got.dtype}{got.shape} vs {want.dtype}{want.shape})")
        other = zarr_n5_read(store_dir, path)
        if other is not None and not np.array_equal(other, want, equal_nan=True):
            problems.append(f"{path}: zarr-n5 differs from read_n5")
    return problems


def remote(store_url: str, path: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "remote.vzip"
        p = virtualize(store_url, out)
        if p.returncode:
            print(f"FAILED: {p.stderr[-300:]}")
            return 1
        print(p.stdout.strip()[:300])
        base = store_url + path + "/"
        attrs = json.loads(urllib.request.urlopen(base + "attributes.json", timeout=60).read())

        def get(key):
            try:
                return urllib.request.urlopen(base + key, timeout=120).read()
            except urllib.error.HTTPError as e:
                if e.code in (403, 404):
                    return None
                raise

        want = read_n5(attrs, get)
        got = zarr.open_group(VZipStore(str(out)), mode="r", zarr_format=3)[path][...]
        ok = got.shape == want.shape and np.array_equal(got, want)
        print(f"{path}: {got.dtype}{got.shape}, {'equal to read_n5' if ok else 'DIFFERS from read_n5'}, "
              f"{int(np.count_nonzero(want))} nonzero values")
        return 0 if ok else 1


def main(argv: list[str]) -> int:
    if argv[:1] == ["--remote"]:
        return remote(argv[1], argv[2])
    stores = [Path(a) for a in argv] or sorted(d for d in (FIXTURES / "n5").iterdir() if d.is_dir())
    proxy = Proxy(FIXTURES, Path(tempfile.mkdtemp()))
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for store_dir in stores:
            out = Path(tmp) / f"{store_dir.name}.vzip"
            p = virtualize(proxy.local_store(store_dir.resolve().relative_to(FIXTURES.resolve()).as_posix()), out)
            if store_dir.name.startswith("n5_reject"):
                ok = p.returncode == 3 and not out.exists()
                failures += not ok
                print(f"{store_dir.name:40s} {'rejected: ' + p.stderr.strip()[-90:] if ok else 'NOT REJECTED ' + p.stderr[-200:]}")
                continue
            if p.returncode:
                failures += 1
                print(f"{store_dir.name:40s} FAILED: {p.stderr.strip()[-300:]}")
                continue
            problems = check_store(store_dir, out)
            failures += bool(problems)
            print(f"{store_dir.name:40s} {problems[:3] if problems else 'ok'} ({len(arrays_of(out))} arrays)")
    print(f"\n{failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
