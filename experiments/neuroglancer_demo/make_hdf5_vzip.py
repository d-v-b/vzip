"""Virtualize a netCDF4 (HDF5) image as a vzip that Neuroglancer can display.

Writes, into the output directory:

* `mandelbrot.nc`: a netCDF4/HDF5 file, with a 1024 x 1024 uint8 image in
  256 x 256 chunks compressed with zlib (deflate), no shuffle filter;
* `mandelbrot.vzip`: a Zarr v3 array whose chunks are vzip references to
  byte ranges of `mandelbrot.nc` (a relative URL). Nothing is converted or
  copied: Neuroglancer reads the HDF5 chunks in place and decodes them with
  its `zlib` codec.

Usage: uv run python experiments/neuroglancer_demo/make_hdf5_vzip.py OUT_DIR
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import h5py
import numpy as np
import xarray as xr

from vzip.archive import VZipWriter


def mandelbrot(n: int = 1024, iterations: int = 96) -> np.ndarray:
    y, x = np.mgrid[-1.25 : 1.25 : n * 1j, -2.1 : 0.9 : n * 1j]
    c = x + 1j * y
    z = np.zeros_like(c)
    count = np.zeros(c.shape, dtype=np.int32)
    for i in range(iterations):
        mask = np.abs(z) <= 2
        z[mask] = z[mask] ** 2 + c[mask]
        count[mask] = i
    return (255 * np.sqrt(count / count.max())).astype(np.uint8)


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    nc = out / "mandelbrot.nc"
    image = mandelbrot()
    xr.Dataset({"image": (("y", "x"), image)}).to_netcdf(
        nc, engine="h5netcdf",
        encoding={"image": {"chunksizes": (256, 256), "zlib": True, "complevel": 4}},
    )

    # Read the chunk layout straight from HDF5 (what a virtualizer does).
    with h5py.File(nc, "r") as f:
        ds = f["image"]
        assert ds.compression == "gzip" and not ds.shuffle, "need plain deflate chunks"
        chunks = [ds.id.get_chunk_info(i) for i in range(ds.id.get_num_chunks())]

    shape, chunk_shape = image.shape, (256, 256)
    metadata = {
        "zarr_format": 3,
        "node_type": "array",
        "shape": list(shape),
        "data_type": "uint8",
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": list(chunk_shape)}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        # HDF5's deflate filter writes zlib streams: Neuroglancer's "zlib" codec
        "codecs": [{"name": "bytes"}, {"name": "zlib", "configuration": {"level": 4}}],
        "fill_value": 0,
        "dimension_names": ["y", "x"],
        "attributes": {"source": "virtualized from mandelbrot.nc (HDF5) via vzip"},
    }
    buf = io.BytesIO()
    w = VZipWriter(buf)
    w.add_bytes("zarr.json", json.dumps(metadata, indent=2).encode(), late=True)
    for info in chunks:
        cy, cx = (o // c for o, c in zip(info.chunk_offset, chunk_shape))
        w.add_ref(f"c/{cy}/{cx}", "mandelbrot.nc", info.byte_offset, info.size)
    w.close()
    (out / "mandelbrot.vzip").write_bytes(buf.getvalue())
    np.save(out / "expected.npy", image)
    print(f"wrote {nc} ({nc.stat().st_size} bytes) and mandelbrot.vzip "
          f"({len(buf.getvalue())} bytes, {len(chunks)} chunk references)")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
