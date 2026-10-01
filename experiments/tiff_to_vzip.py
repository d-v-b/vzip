"""Virtualize a remote tiled (pyramidal, OME-) TIFF as a vzip archive.

Reads only the TIFF's IFDs over HTTP range requests, then writes:

* `zarr.json`: an OME-NGFF 0.5 multiscales group
* `<level>/zarr.json`: one array per pyramid level, chunk = TIFF tile
* `<level>/c/<c>/<ty>/<tx>`: a Range reference to that tile's bytes
* `OME/METADATA.ome.xml`: the OME-XML, as a real (deflated) bytes entry

The URL source is pinned to the object's size, ETag and Last-Modified (spec §6.1),
unless `--no-pins` is given. Browsers can only check pins on servers that expose
those headers to cross-origin pages, which many (EBI's included) do not.

Usage: uv run python experiments/tiff_to_vzip.py [--no-pins] <url> <out.vzip>
"""

from __future__ import annotations

import email.utils
import json
import sys
import time
import urllib.request

import fsspec
import numpy as np
import tifffile
import zarr
from zarr.storage import MemoryStore

from vzip.archive import VZipWriter
from vzip.codecs import Jpeg2kCodec
from vzip.pb import Range, Source

JPEG2K = {33003, 33004, 33005, 34712}


def head(url: str) -> dict:
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        return {k.lower(): v for k, v in r.headers.items()}


def main(url: str, out: str, pin: bool = True) -> None:
    t0 = time.time()
    h = head(url)
    size = int(h["content-length"])
    etag = h.get("etag")
    mtime = int(email.utils.parsedate_to_datetime(h["last-modified"]).timestamp())

    fs = fsspec.filesystem("http")
    with fs.open(url, block_size=1 << 16, cache_type="readahead") as f:
        tf = tifffile.TiffFile(f)
        series = tf.series[0]
        ome_xml = tf.ome_metadata
        levels = []
        for lvl in series.levels:
            p = lvl.pages[0]
            if p.compression not in JPEG2K or p.planarconfig != 2 or not p.is_tiled:
                raise SystemExit(f"unsupported layout: compression {p.compression}, "
                                 f"planar {p.planarconfig}, tiled {p.is_tiled}")
            levels.append((lvl.shape, (p.tilelength, p.tilewidth),
                           np.array(p.dataoffsets), np.array(p.databytecounts), lvl.dtype))
    print(f"read IFDs of {len(levels)} levels in {time.time() - t0:.1f}s")

    mem = MemoryStore()
    group = zarr.open_group(mem, mode="w", zarr_format=3)
    datasets = []
    refs: dict[str, tuple[int, int]] = {}
    full = levels[0][0]
    for i, (shape, tile, offsets, counts, dtype) in enumerate(levels):
        c, h_, w_ = shape
        ty, tx = -(-h_ // tile[0]), -(-w_ // tile[1])
        assert len(offsets) == c * ty * tx, (len(offsets), c, ty, tx)
        arr = group.create_array(
            name=str(i), shape=shape, chunks=(1, *tile), dtype=dtype,
            serializer=Jpeg2kCodec(), compressors=None, filters=None, fill_value=0,
            dimension_names=["c", "y", "x"],
        )
        for k, (off, n) in enumerate(zip(offsets, counts)):
            if n == 0:
                continue  # missing tile -> fill value
            ci, rest = divmod(k, ty * tx)
            yi, xi = divmod(rest, tx)
            refs[f"{i}/{arr.metadata.encode_chunk_key((ci, yi, xi))}"] = (int(off), int(n))
        datasets.append({"path": str(i), "coordinateTransformations": [
            {"type": "scale", "scale": [1.0, full[1] / shape[1], full[2] / shape[2]]}]})
    group.attrs["ome"] = {
        "version": "0.5",
        "multiscales": [{
            "name": series.name,
            "axes": [{"name": "c", "type": "channel"}, {"name": "y", "type": "space"},
                     {"name": "x", "type": "space"}],
            "datasets": datasets,
        }],
    }

    pins = {"size": size, "modified_not_after": mtime} if pin else {}
    if pin and etag and not etag.startswith("W/"):
        pins["etag"] = etag
    with open(out, "wb") as fh:
        w = VZipWriter(fh, page_size=1 << 16)
        src = w.source(Source(url=url, **pins))
        for k in sorted(refs):
            off, n = refs[k]
            w.add_ranges(k, [Range(source=src, offset=off, length=n)])
        w.add_bytes("OME/METADATA.ome.xml", ome_xml.encode(), compress=True)
        for k, v in sorted(mem._store_dict.items()):
            w.add_bytes(k, v.to_bytes(), late=k.endswith("zarr.json"))
        w.close()
    import os

    print(json.dumps({"archive": out, "bytes": os.path.getsize(out), "references": len(refs),
                      "levels": len(levels), "source_bytes": size, "pins": pins,
                      "seconds": round(time.time() - t0, 1)}, indent=1))


if __name__ == "__main__":
    args = sys.argv[1:]
    pin = "--no-pins" not in args
    url, out = [a for a in args if a != "--no-pins"]
    main(url, out, pin)
