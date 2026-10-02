"""Check a vzip made from an ND2 file (python -m vzip.virtualize) against the nd2 package reading the ND2.

Reads a sample of frames (first, last, and random ones) through the archive
(vzip reference reader + zarr-python) and with `nd2` from the remote file, and
compares them.

Usage: uv run python experiments/verify_nd2_vzip.py <archive.vzip> <nd2 url> [frames]
"""

from __future__ import annotations

import sys
import time
import zlib
from dataclasses import dataclass

import fsspec
import nd2
import numpy as np
import zarr
from zarr.abc.codec import BytesBytesCodec
from zarr.registry import register_codec

from vzip.store import VZipStore


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


def main(archive: str, url: str, n: int = 6) -> None:
    store = VZipStore(archive)
    root = zarr.open_group(store, mode="r", zarr_format=3)
    series = root["OME"].attrs["ome"]["series"]
    fs = fsspec.filesystem("http")
    with fs.open(url, block_size=1 << 20, cache_type="none") as f, nd2.ND2File(f) as nf:
        indices = nf._rdr.loop_indices()
        rng = np.random.default_rng(0)
        picks = sorted({0, len(indices) - 1, *rng.integers(0, len(indices), n - 2).tolist()})
        ok = 0
        t0 = time.time()
        for seq in picks:
            c = indices[seq]
            arr = root[f"{c.get('P', 0)}/0"]
            axes = arr.metadata.dimension_names
            sel = tuple(c.get(d.upper(), 0) if d in ("t", "z") else slice(None) for d in axes)
            got = arr[sel]  # [c, y, x]
            want = np.moveaxis(nf._rdr.read_frame(seq).reshape(arr.shape[-2], arr.shape[-1], -1), -1, 0)
            same = got.shape == want.shape and np.array_equal(got, want)
            ok += same
            print(f"frame {seq:4d} {c} position {c.get('P', 0)}: {'identical' if same else 'DIFFERENT'} "
                  f"{got.shape} {got.dtype} mean {got.mean():.1f}")
    print(f"{ok}/{len(picks)} frames identical ({len(series)} positions) in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], *(int(a) for a in sys.argv[3:]))
