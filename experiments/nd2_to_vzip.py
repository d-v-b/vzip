"""Virtualize a remote Nikon ND2 file (format version 3, the modern chunked
layout) as a vzip archive, without reading its pixel data.

The ND2's chunk map and metadata chunks are read over HTTP with range
requests (through fsspec) and decoded by the `nd2` package
(https://github.com/tlambert03/nd2). Each image frame is stored in the file as
one chunk: a chunk header, an 8-byte timestamp, then the pixels, either raw
rows of interleaved components (`[y, x, component]`) or a zlib stream of them
("lossless" compression). Each frame becomes one Zarr chunk:

* uncompressed: one range reference to the pixel bytes; if rows are padded
  (`widthBytes` larger than a row), a Concat of one range per row, which
  drops the padding;
* compressed: one range reference to the zlib stream, decoded by the `zlib`
  codec. (Compressed frames with padded rows are not supported.)

Layout (OME-NGFF 0.5, bioformats2raw layout): one multiscale image per stage
position, `<p>/` with a single array `<p>/0` of axes `[t, c, z, y, x]` (only
those present). A frame is `[y, x, c]` on disk; a `transpose` codec moves the
channel axis. Channel names go in `omero`, the time step and pixel sizes in
the scales. The ND2's own metadata is kept as JSON in `nd2/metadata.json`.

Usage: uv run --with nd2 python experiments/nd2_to_vzip.py <url> <out.vzip>
"""

from __future__ import annotations

import json
import struct
import sys
import time
from collections import namedtuple

import fsspec
import nd2

from vzip.archive import VZipWriter
from vzip.pb import Range, Source

CHUNK_MAGIC = 0x0ABECEDA
Voxel = namedtuple("Voxel", "x y z")


def chunk_data_offset(f, offset: int) -> tuple[int, int]:
    """(data start, data length) of the ND2 chunk whose header is at `offset`."""
    f.seek(offset)
    magic, name_len, data_len = struct.unpack("<IIQ", f.read(16))
    if magic != CHUNK_MAGIC:
        raise ValueError(f"no ND2 chunk header at {offset}")
    return offset + 16 + name_len, data_len


def main(url: str, out: str) -> None:
    t0 = time.time()
    fs = fsspec.filesystem("http")
    f = fs.open(url, block_size=1 << 16, cache_type="blockcache")
    nf = nd2.ND2File(f)
    if nf.is_legacy:
        raise SystemExit("legacy (JPEG 2000) ND2 files are not supported")
    rdr = nf._rdr
    a = nf.attributes
    sizes = dict(nf.sizes)
    dtype = nf.dtype
    itemsize = dtype.itemsize
    ncomp = a.componentCount
    height, width = a.heightPx, a.widthPx
    row_bytes = width * ncomp * itemsize
    width_bytes = a.widthBytes or row_bytes
    compressed = a.compressionType == "lossless"
    if a.compressionType not in (None, "lossless"):
        raise SystemExit(f"unsupported compression {a.compressionType!r}")
    if compressed and width_bytes != row_bytes:
        raise SystemExit("compressed frames with padded rows are not supported")

    # Frames: sequence index -> loop coordinates, and the chunk holding it.
    loop_indices = rdr.loop_indices()
    chunkmap = rdr.chunkmap
    frames = {}
    for key, (offset, _) in chunkmap.items():
        if key.startswith(b"ImageDataSeq|"):
            frames[int(key[13:-1])] = offset
    loops = [d for d in sizes if d not in ("C", "Y", "X", "S")]
    unsupported = [d for d in loops if d not in ("T", "P", "Z")]
    if unsupported:
        raise SystemExit(f"unsupported loop dimensions {unsupported}")
    n_t, n_p, n_z = sizes.get("T", 1), sizes.get("P", 1), sizes.get("Z", 1)

    # Chunk headers: read every frame's when compressed (the stream length
    # varies); otherwise check the first and last against the fixed layout.
    def locate(seq: int) -> tuple[int, int]:
        start, length = chunk_data_offset(f, frames[seq])
        return start + 8, length - 8  # skip the 8-byte timestamp

    frame_bytes = height * width_bytes
    if not compressed:
        first = locate(min(frames))
        layout = first[0] - frames[min(frames)]
        for seq in (max(frames), sorted(frames)[len(frames) // 2]):
            got = locate(seq)
            if got[0] - frames[seq] != layout or got[1] < frame_bytes:
                raise SystemExit(f"frame {seq}: chunk layout differs from frame {min(frames)}")

    # Axes of each position's array.
    axes = []
    if n_t > 1:
        axes.append("t")
    axes.append("c")
    if n_z > 1:
        axes.append("z")
    axes += ["y", "x"]
    shape = {"t": n_t, "c": ncomp, "z": n_z, "y": height, "x": width}
    chunk = {"t": 1, "c": ncomp, "z": 1, "y": height, "x": width}
    # On disk a frame is [y, x, c] (with t and z of length 1 around it).
    encoded = [d for d in axes if d != "c"] + ["c"]
    order = [axes.index(d) for d in encoded]
    codecs = [
        {"name": "transpose", "configuration": {"order": order}},
        {"name": "bytes", "configuration": {"endian": "little"}} if itemsize > 1 else {"name": "bytes"},
    ]
    if compressed:
        codecs.append({"name": "zlib", "configuration": {"level": a.compressionLevel or 1}})

    # Some files' metadata cannot be decoded by `nd2` (its channel table, for
    # example); the pixels can still be virtualized, with generic labels and
    # unit scales.
    warnings = []
    try:
        voxel = nf.voxel_size()
    except Exception as e:  # noqa: BLE001
        warnings.append(f"no voxel size: {type(e).__name__}: {e}")
        voxel = Voxel(1.0, 1.0, 1.0)
    period_s = None
    for loop in nf.experiment:
        if loop.type == "TimeLoop":
            period_s = (getattr(loop.parameters, "periodMs", None) or 0) / 1000 or None
    scale = {"t": period_s or 1.0, "c": 1.0, "z": voxel.z, "y": voxel.y, "x": voxel.x}
    units = {"t": "second" if period_s else None, "z": "micrometer", "y": "micrometer", "x": "micrometer"}
    types = {"t": "time", "c": "channel", "z": "space", "y": "space", "x": "space"}
    try:
        channel_names = [c.channel.name for c in nf.metadata.channels] if nf.metadata.channels else []
    except Exception as e:  # noqa: BLE001
        warnings.append(f"no channel names: {type(e).__name__}: {e}")
        channel_names = []
    if "S" in sizes:  # RGB: components are channel-major, then R, G, B
        n_c = sizes.get("C", 1)
        names = channel_names or [f"C{i}" for i in range(n_c)]
        labels = [f"{names[i]} {rgb}" if n_c > 1 else rgb for i in range(n_c) for rgb in "RGB"]
    else:
        labels = channel_names or [f"C{i}" for i in range(ncomp)]
    labels = labels[:ncomp] + [f"C{i}" for i in range(len(labels), ncomp)]

    # References.
    refs: dict[str, list[Range]] = {}
    missing = 0
    for seq, coords in enumerate(loop_indices):
        if seq not in frames:
            missing += 1
            continue
        p, t, z = coords.get("P", 0), coords.get("T", 0), coords.get("Z", 0)
        index = {"t": t, "c": 0, "z": z, "y": 0, "x": 0}
        key = f"{p}/0/c/" + "/".join(str(index[d]) for d in axes)
        if compressed:
            start, length = locate(seq)
            refs[key] = [Range(source=0, offset=start, length=length)]
        else:
            start = frames[seq] + layout
            if width_bytes == row_bytes:
                refs[key] = [Range(source=0, offset=start, length=frame_bytes)]
            else:
                refs[key] = [Range(source=0, offset=start + r * width_bytes, length=row_bytes)
                             for r in range(height)]

    def group(attrs: dict) -> bytes:
        return json.dumps({"zarr_format": 3, "node_type": "group", "attributes": attrs}, indent=2).encode()

    positions = [str(p) for p in range(n_p)]
    meta: dict[str, bytes] = {
        "zarr.json": group({"ome": {"version": "0.5", "bioformats2raw.layout": 3}}),
        "OME/zarr.json": group({"ome": {"version": "0.5", "series": positions}}),
    }
    array = {
        "zarr_format": 3,
        "node_type": "array",
        "shape": [shape[d] for d in axes],
        "data_type": str(dtype),
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [chunk[d] for d in axes]}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": 0,
        "codecs": codecs,
        "dimension_names": axes,
        "attributes": {},
    }
    for p in positions:
        meta[f"{p}/zarr.json"] = group({"ome": {
            "version": "0.5",
            "multiscales": [{
                "name": f"position {p}",
                "axes": [{"name": d, "type": types[d], **({"unit": units[d]} if units.get(d) else {})} for d in axes],
                "datasets": [{"path": "0", "coordinateTransformations": [{"type": "scale", "scale": [scale[d] for d in axes]}]}],
            }],
            "omero": {"channels": [{"label": label, "active": True} for label in labels]},
        }})
        meta[f"{p}/0/zarr.json"] = json.dumps(array, indent=2).encode()
    meta["nd2/metadata.json"] = json.dumps({
        "attributes": a._asdict() if hasattr(a, "_asdict") else str(a),
        "sizes": sizes,
        "experiment": [str(loop) for loop in nf.experiment],
        "channels": channel_names,
        "voxel_size_um": [voxel.x, voxel.y, voxel.z],
        "warnings": warnings,
    }, indent=1, default=str).encode()

    size = f.size
    nf.close()
    with open(out, "wb") as fh:
        w = VZipWriter(fh, page_size=1 << 16)
        w.source(Source(url=url))
        for k in sorted(refs):
            w.add_ranges(k, refs[k])
        for k, v in sorted(meta.items()):
            w.add_bytes(k, v, compress=k.startswith("nd2/"), late=k.endswith("zarr.json"))
        w.close()
    import os

    print(json.dumps({
        "archive": out, "bytes": os.path.getsize(out), "source_bytes": size, "sizes": sizes,
        "dtype": str(dtype), "compressed": compressed, "padded_rows": width_bytes != row_bytes,
        "references": len(refs), "missing_frames": missing, "positions": n_p, "axes": axes,
        "channels": labels, "warnings": warnings, "seconds": round(time.time() - t0, 1),
    }))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
