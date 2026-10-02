"""Checks the browser ND2 virtualizer's output on the synthetic ND2 files.

Each web/test/fixtures/nd2_*.nd2 is served over local HTTP and virtualized by
web/conformance/virtualize.ts (the browser code, run under Node). Every chunk
of the archive is read through the reference reader and zarr-python and must
equal the pixels nd2_fixtures.py wrote (<name>.npz); chunks absent from the
npz (missing frames) must read as the fill value. `nd2_reject_*` files must be
rejected.

Usage: uv run python web/test/verify_nd2.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import zarr
from zarr.abc.codec import BytesBytesCodec
from zarr.registry import register_codec

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "conformance"))
from http_server import Server  # noqa: E402

from vzip.store import VZipStore  # noqa: E402


@dataclass(frozen=True)
class ZlibCodec(BytesBytesCodec):
    """`zlib` (as Neuroglancer names it), which zarr-python lacks."""

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


def main() -> int:
    fixtures = HERE / "fixtures"
    server = Server(fixtures)
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for nd2 in sorted(fixtures.glob("nd2_*.nd2")):
            out = Path(tmp) / f"{nd2.stem}.vzip"
            p = subprocess.run(["node", str(ROOT / "web" / "conformance" / "virtualize.ts"),
                                server.base + nd2.name, str(out)], capture_output=True, text=True)
            if nd2.stem.startswith("nd2_reject"):
                ok = p.returncode == 3 and not out.exists()
                failures += not ok
                print(f"{nd2.name:34s} {'rejected: ' + p.stderr.strip()[-90:] if ok else 'NOT REJECTED ' + p.stderr[-200:]}")
                continue
            if p.returncode:
                failures += 1
                print(f"{nd2.name:34s} FAILED: {p.stderr.strip()[-300:]}")
                continue
            expected = {k.replace("|", "/"): v for k, v in np.load(nd2.with_suffix(".npz")).items()}
            root = zarr.open_group(VZipStore(str(out)), mode="r", zarr_format=3)
            problems, chunks = [], 0
            for series in root["OME"].attrs["ome"]["series"]:
                arr = root[f"{series}/0"]
                grid = [s // c for s, c in zip(arr.shape, arr.chunks)]
                for index in np.ndindex(*grid):
                    key = f"{series}/0/c/" + "/".join(map(str, index))
                    sel = tuple(slice(i * c, (i + 1) * c) for i, c in zip(index, arr.chunks))
                    got = np.squeeze(arr[sel], axis=tuple(d for d, c in enumerate(arr.chunks) if c == 1 and d < arr.ndim - 2))
                    want = expected.get(key)
                    chunks += 1
                    if want is None:
                        if np.any(got != 0):
                            problems.append(f"{key}: expected the fill value")
                    elif got.dtype != want.dtype or not np.array_equal(got, want):
                        problems.append(f"{key}: differs ({got.dtype} {got.shape} vs {want.dtype} {want.shape})")
            failures += bool(problems)
            print(f"{nd2.name:34s} {'ok' if not problems else problems[:3]} ({chunks} chunks, "
                  f"{len(expected)} with data, axes {root[root['OME'].attrs['ome']['series'][0] + '/0'].metadata.dimension_names})")
    print(f"\n{failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
