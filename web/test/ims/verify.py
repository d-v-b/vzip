"""Checks the browser IMS virtualizer against h5py.

Each web/test/fixtures/ims/*.ims (or each .ims file given as an argument) is
served over local HTTP and virtualized by web/conformance/virtualize.ts (the
browser code, run under Node). Every level of the archive is read through the
reference reader and zarr-python, and must equal, for every time point and
channel, what h5py reads from `/DataSet/ResolutionLevel r/TimePoint t/Channel
c/Data`, cropped to the channel's ImageSize (unallocated chunks read as 0 in
both). `ims_reject_*` files must be rejected.

Usage: uv run python web/test/ims/verify.py [<file.ims> ...]
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path

import h5py
import numpy as np
import zarr
from zarr.abc.codec import BytesBytesCodec
from zarr.registry import register_codec

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "conformance"))
from http_server import Server  # noqa: E402

from vzip.store import VZipStore  # noqa: E402


@dataclass(frozen=True)
class ZlibCodec(BytesBytesCodec):
    """'zlib' (as Neuroglancer names it), which zarr-python lacks."""

    is_fixed_size = False
    level: int = 1

    @classmethod
    def from_dict(cls, data):
        return cls(**data.get("configuration", {}))

    def to_dict(self):
        return {"name": "zlib", "configuration": {"level": self.level}}

    async def _decode_single(self, chunk_bytes, chunk_spec):
        return chunk_spec.prototype.buffer.from_bytes(zlib.decompress(chunk_bytes.to_bytes()))

    async def _encode_single(self, chunk_bytes, chunk_spec):
        return chunk_spec.prototype.buffer.from_bytes(zlib.compress(chunk_bytes.to_bytes()))

    def compute_encoded_size(self, input_byte_length, chunk_spec):
        raise NotImplementedError


register_codec("zlib", ZlibCodec)


def image_size(group) -> tuple[int, ...]:
    return tuple(int(b"".join(group.attrs[f"ImageSize{a}"].tolist()).split(b"\0")[0]) for a in "ZYX")


def check(path: Path, out: Path) -> list[str]:
    """The differences between the archive at 'out' and h5py's reading of 'path'."""
    problems = []
    root = zarr.open_group(VZipStore(str(out)), mode="r", zarr_format=3)
    levels = root.attrs["ome"]["multiscales"][0]["datasets"]
    with h5py.File(path, "r") as f:
        for r in range(len(levels)):
            arr = root[str(r)]
            axes = list(arr.metadata.dimension_names)
            sizes = dict(zip(axes, arr.shape))
            for t in range(sizes.get("t", 1)):
                for c in range(sizes.get("c", 1)):
                    group = f[f"DataSet/ResolutionLevel {r}/TimePoint {t}/Channel {c}"]
                    z, y, x = image_size(group)
                    want = group["Data"][:z, :y, :x]
                    want = want.astype(want.dtype.newbyteorder("="))  # zarr reads in native byte order
                    if "z" not in axes:
                        want = want[0]
                    index = tuple(t if a == "t" else c if a == "c" else slice(None) for a in axes)
                    got = arr[index]
                    if got.dtype != want.dtype or not np.array_equal(got, want):
                        problems.append(f"level {r} t {t} c {c}: {got.dtype} {got.shape} != {want.dtype} {want.shape}")
    return problems


def main(paths: list[str]) -> int:
    files = [Path(p) for p in paths] or sorted((HERE.parent / "fixtures" / "ims").glob("ims_*.ims"))
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        servers: dict[Path, Server] = {}
        for path in files:
            server = servers.setdefault(path.parent, Server(path.parent))
            out = Path(tmp) / f"{path.stem}.vzip"
            p = subprocess.run(["node", str(ROOT / "web" / "conformance" / "virtualize.ts"),
                                server.base + path.name, str(out)], capture_output=True, text=True)
            if path.stem.startswith("ims_reject"):
                ok = p.returncode == 3 and not out.exists()
                failures += not ok
                print(f"{path.name:36s} {'rejected: ' + p.stderr.strip()[-90:] if ok else 'NOT REJECTED ' + p.stderr[-200:]}")
                continue
            if p.returncode:
                failures += 1
                print(f"{path.name:36s} FAILED: {p.stderr.strip()[-300:]}")
                continue
            problems = check(path, out)
            failures += bool(problems)
            print(f"{path.name:36s} {problems[:3] if problems else 'ok'} {p.stdout.strip()[:160]}")
    print(f"\n{failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
