"""Writes the TIFF fixtures in web/test/fixtures/ with tifffile.

Usage: uv run python web/test/tiff_fixtures.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import tifffile

OUT = Path(__file__).parent / "fixtures"
rng = np.random.default_rng(0)


def image(shape, dtype):
    y, x = np.mgrid[0 : shape[-2], 0 : shape[-1]]
    base = (x * 3 + y * 5) % 251
    out = np.broadcast_to(base, shape).astype(np.float64)
    out = out + np.arange(np.prod(shape[:-2]) or 1).reshape(shape[:-2] + (1, 1)) * 17
    out = out + rng.integers(0, 4, shape)
    return out.astype(dtype)


def pyramid(path, data, levels, *, axes, downsample_axes=2, **kw):
    """An OME-TIFF with `levels` SubIFD levels, each half the size."""
    ome_meta = {"axes": axes, "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5}
    if "Z" in axes:
        ome_meta["PhysicalSizeZ"] = 2.0
    bigtiff = kw.pop("bigtiff", False)
    byteorder = kw.pop("byteorder", "<")
    with tifffile.TiffWriter(path, bigtiff=bigtiff, byteorder=byteorder, ome=True) as tif:
        tif.write(data, subifds=levels, metadata=ome_meta, **kw)
        for i in range(1, levels + 1):
            s = 2**i
            sl = (slice(None),) * (data.ndim - downsample_axes) + (slice(None, None, s),) * 2
            if axes.endswith("S"):
                sl = (slice(None),) * (data.ndim - 3) + (slice(None, None, s),) * 2 + (slice(None),)
            tif.write(data[sl], subfiletype=1, **kw)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    rgb = image((3, 96, 128), np.uint8)
    pyramid(OUT / "rgb_planar_jpeg2000_bigtiff_be.ome.tif", rgb, 2, axes="CYX",
            bigtiff=True, byteorder=">", tile=(32, 32), compression="jpeg2000",
            photometric="rgb", planarconfig="separate")
    pyramid(OUT / "tczyx_uint16_deflate.ome.tif", image((2, 2, 3, 96, 128), np.uint16), 1,
            axes="TCZYX", tile=(32, 32), compression="zlib", photometric="minisblack")
    pyramid(OUT / "rgb_contig_zstd.ome.tif", np.moveaxis(rgb, 0, -1).copy(), 1, axes="YXS",
            tile=(32, 48), compression="zstd", photometric="rgb", planarconfig="contig")
    pyramid(OUT / "float32_uncompressed_be.ome.tif", image((80, 112), np.float32), 0,
            axes="YX", byteorder=">", tile=(32, 32), photometric="minisblack")

    # Not OME: levels as later main-chain IFDs, with a stripped thumbnail
    # between them (as in SVS).
    with tifffile.TiffWriter(OUT / "svs_like_int16.tif") as tif:
        data = image((96, 128), np.int16) - 100
        tif.write(data, tile=(32, 32), compression="zlib", photometric="minisblack")
        tif.write(data[::8, ::8], photometric="minisblack")  # thumbnail, not tiled
        tif.write(data[::2, ::2], tile=(32, 32), compression="zlib", photometric="minisblack")

    # Unsupported.
    small = image((64, 64), np.uint8)
    tifffile.imwrite(OUT / "unsupported_lzw.tif", small, tile=(32, 32), compression="lzw")
    tifffile.imwrite(OUT / "unsupported_predictor.tif", small, tile=(32, 32),
                     compression="zlib", predictor=True)
    tifffile.imwrite(OUT / "unsupported_strips.tif", small)
    for p in sorted(OUT.glob("*.tif")):
        print(p.name, p.stat().st_size)


if __name__ == "__main__":
    main()
