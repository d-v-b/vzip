"""Check a vzip made by tiff_to_vzip.py against tifffile reading the TIFF directly.

Usage: uv run python experiments/verify_tiff_vzip.py <archive.vzip> <tiff url>
"""

from __future__ import annotations

import sys
import time

import fsspec
import numpy as np
import tifffile
import zarr

import refstore.codecs  # noqa: F401  (registers imagecodecs_jpeg2k)
from refstore.archive import VZipWriter  # noqa: F401
from refstore.store import VZipStore


def main(archive: str, url: str) -> None:
    store = VZipStore(archive)
    t0 = time.time()
    root = zarr.open_group(store, mode="r", zarr_format=3)
    ms = root.attrs["ome"]["multiscales"][0]
    print(f"opened in {time.time() - t0:.2f}s with {store.stats.archive_requests} archive "
          f"requests; levels: {[root[d['path']].shape for d in ms['datasets']][:3]} ...")

    fs = fsspec.filesystem("http")
    rng = np.random.default_rng(0)
    with fs.open(url, block_size=1 << 20, cache_type="readahead") as f:
        tf = tifffile.TiffFile(f)
        checks = []
        for level in (0, 0, 3, 8):
            arr = root[str(level)]
            _, h, w = arr.shape
            hh, ww = min(1500, h), min(1500, w)
            y = int(rng.integers(0, h - hh + 1))
            x = int(rng.integers(0, w - ww + 1))
            store.stats.reset()
            t0 = time.time()
            got = arr[:, y : y + hh, x : x + ww]
            dt = time.time() - t0
            reqs = store.stats.external_requests
            mb = store.stats.external_bytes / 1e6
            want = zarr.open_array(tf.aszarr(level=level), mode="r")[:, y : y + hh, x : x + ww]
            ok = np.array_equal(got, want)
            checks.append(ok)
            print(f"level {level}: window [{y}:{y + hh}, {x}:{x + ww}] x3 channels -> {ok}; "
                  f"{reqs} range requests, {mb:.1f} MB, {dt:.1f}s; mean {got.mean():.1f}")
    print("all windows identical to tifffile:", all(checks))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
