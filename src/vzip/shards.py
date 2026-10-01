"""Virtual shards: one reference per (array, shard) instead of one per chunk.

A zarr v3 shard is `[inner chunks...][index]`, where the index lists
(offset, nbytes) of every inner chunk relative to the start of the shard.
If every chunk in a block of the chunk grid lives in the same source file,
that block can be exposed as a shard whose value is the concatenation

    [source file bytes 0..end] ++ [index bytes (inline)]

and whose index offsets are simply the chunk offsets in the source file.
zarr's own sharding codec then does the lookup, and the vzip store maps its
range requests onto the source file. The per-chunk manifest becomes a
standard zarr shard index (16 bytes per chunk).
"""

from __future__ import annotations

import itertools
import math
from typing import Literal
from pathlib import Path

import numpy as np
import xarray as xr
import zarr
from virtualizarr.manifests import ManifestArray
from virtualizarr.writers.icechunk import update_attributes
from zarr.codecs import BytesCodec, ShardingCodec
from zarr.storage import MemoryStore

from vzip.archive import VZipWriter
from vzip.pb import Range

_MISSING = np.uint64(2**64 - 1)


def _shard_refs(ma: ManifestArray, chunks_per_shard: tuple[int, ...], shift: int = 0):
    """Yield (shard index, url, end of last chunk, shard index bytes) per non-empty shard.

    `shift` is added to every chunk offset in the index: the number of bytes that
    precede the source file in the shard value (the index size, when the index
    is at the start).
    """
    m = ma.manifest
    paths, offsets, lengths = m._paths, m._offsets, m._lengths
    grid = m.shape_chunk_grid
    n_shards = [-(-g // c) for g, c in zip(grid, chunks_per_shard)]
    for sidx in itertools.product(*map(range, n_shards)):
        block = tuple(slice(i * c, (i + 1) * c) for i, c in zip(sidx, chunks_per_shard))
        p, o, n = paths[block], offsets[block], lengths[block]
        present = p != ""
        if not present.any():
            continue
        urls = np.unique(p[present])
        if len(urls) != 1:
            raise ValueError(f"shard {sidx} spans {len(urls)} files; choose a smaller shard")
        # pad partial edge shards to the full shard shape
        idx = np.full((*chunks_per_shard, 2), _MISSING, dtype="<u8")
        sl = tuple(slice(0, s) for s in p.shape)
        idx[sl + (0,)] = np.where(present, o + np.uint64(shift), _MISSING)
        idx[sl + (1,)] = np.where(present, n, _MISSING)
        end = int((o[present] + n[present]).max())
        yield sidx, str(urls[0]), end, idx.tobytes()


def write_vzip_sharded(
    vds: xr.Dataset,
    path: str | Path,
    chunks_per_shard: dict[str, tuple[int, ...]],
    *,
    inline_index: bool = False,
    page_size: int | None = None,
    index_location: Literal["start", "end"] = "end",
) -> None:
    """Like `write_vzip`, but store each listed variable as virtual shards.

    By default each shard index is a hidden DEFLATE-compressed entry that the
    shard's reference points at through an internal source, so the central directory
    stays small and indexes load lazily. `inline_index=True` puts the index
    bytes in the reference itself (all indexes are then read on open).

    `index_location` is written to the sharding codec metadata, and the shard
    value is built to match: `[file] ++ [index]` for "end", `[index] ++ [file]`
    for "start" (with chunk offsets shifted past the index).
    """
    mem = MemoryStore()
    virtual = {k: v for k, v in vds.variables.items() if isinstance(v.data, ManifestArray)}
    loadable = {k: v for k, v in vds.variables.items() if k not in virtual}
    if loadable:
        xr.Dataset(loadable).to_zarr(mem, zarr_format=3, consolidated=False, mode="a")
    group = zarr.open_group(mem, mode="a", zarr_format=3)

    refs: dict[str, tuple[str, int, bytes]] = {}
    for name, var in virtual.items():
        ma: ManifestArray = var.data
        md = ma.metadata
        cps = chunks_per_shard[name]
        sharding = ShardingCodec(
            chunk_shape=md.chunks,
            codecs=list(md.codecs),
            index_codecs=[BytesCodec()],
            index_location=index_location,
        )
        arr = group.create_array(
            name=name,
            shape=md.shape,
            chunks=tuple(c * k for c, k in zip(md.chunks, cps)),
            dtype=md.data_type.to_native_dtype(),
            serializer=sharding,
            compressors=None,
            filters=None,
            dimension_names=var.dims,
            fill_value=md.fill_value,
        )
        update_attributes(arr, var.attrs, encoding=var.encoding)
        if ma.manifest._inlined:
            raise NotImplementedError("inlined chunks + virtual shards")
        # 16 bytes (offset, nbytes as u64) per inner chunk; no index checksum
        shift = 16 * math.prod(cps) if index_location == "start" else 0
        for sidx, url, end, index in _shard_refs(ma, cps, shift):
            refs[f"{name}/{arr.metadata.encode_chunk_key(sidx)}"] = (url, end, index)
    update_attributes(group, vds.attrs, coords=vds.coords)

    with open(path, "wb") as f, VZipWriter(f, page_size=page_size) as w:
        for k, v in sorted(mem._store_dict.items()):
            w.add_bytes(k, v.to_bytes(), late=k.endswith("zarr.json"))
        for k in sorted(refs):
            url, end, index = refs[k]
            if inline_index:
                idx_range = Range(data=index)
            else:
                hidden = w.add_hidden(f"idx/{k}", index)
                idx_range = Range(source=w.internal(hidden), length=len(index))
            file_range = Range(source=w.url(url), offset=0, length=end)
            at_start = index_location == "start"
            w.add_ranges(k, [idx_range, file_range] if at_start else [file_range, idx_range])
