"""Write virtualizarr virtual datasets as vzip archives.

The conversion literally builds the two key spaces separately and overlays
them: zarr metadata and any loadable (non-virtual) variables go into an
in-memory zarr store (the bytes key space); every manifest entry becomes a
reference (the reference key space).
"""

from __future__ import annotations

import warnings
from collections import Counter
from pathlib import Path

import xarray as xr
import zarr
from virtualizarr.manifests import ManifestArray
from virtualizarr.writers.icechunk import extract_codecs, update_attributes
from zarr.storage import MemoryStore

from vzip.archive import VZipWriter


def consolidate(store) -> None:
    """Write Zarr consolidated metadata into the root zarr.json, so readers
    that know it find every array there instead of listing the archive's
    every key to discover them. It is a zarr-python convention, not part of
    the Zarr v3 specification; readers that don't know it list as before."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Consolidated metadata is currently not part")
        zarr.consolidate_metadata(store, zarr_format=3)


def split_virtual_dataset(
    vds: xr.Dataset,
) -> tuple[dict[str, bytes], dict[str, tuple[str, int, int]]]:
    """Split a virtual dataset into (bytes key space, reference key space).

    References are (url, offset, length): virtualizarr only has single ranges.
    """
    mem = MemoryStore()
    virtual = {k: v for k, v in vds.variables.items() if isinstance(v.data, ManifestArray)}
    loadable = {k: v for k, v in vds.variables.items() if k not in virtual}
    if loadable:
        xr.Dataset(loadable).to_zarr(mem, zarr_format=3, consolidated=False, mode="a")
    group = zarr.open_group(mem, mode="a", zarr_format=3)

    refs: dict[str, tuple[str, int, int]] = {}
    inlined: dict[str, bytes] = {}
    for name, var in virtual.items():
        ma: ManifestArray = var.data
        md = ma.metadata
        filters, serializer, compressors = extract_codecs(md.inner_codecs)
        arr = group.require_array(
            name=name,
            shape=md.shape,
            chunks=md.chunks,
            shards=md.shards,
            dtype=md.data_type.to_native_dtype(),
            filters=filters,
            compressors=compressors,
            serializer=serializer,
            dimension_names=var.dims,
            fill_value=md.fill_value,
        )
        update_attributes(arr, var.attrs, encoding=var.encoding)
        for idx, entry in ma.manifest.iter_refs():
            key = f"{name}/{arr.metadata.encode_chunk_key(idx)}"
            refs[key] = (entry["path"], entry["offset"], entry["length"])
        for idx, data in ma.manifest._inlined.items():
            inlined[f"{name}/{arr.metadata.encode_chunk_key(idx)}"] = bytes(data)
    update_attributes(group, vds.attrs, coords=vds.coords)
    consolidate(mem)

    concrete = {k: v.to_bytes() for k, v in mem._store_dict.items()}
    concrete.update(inlined)
    return concrete, refs


def read_at_open(key: str, vds: xr.Dataset) -> bool:
    """Whether xarray reads `key` when it opens the dataset: the metadata
    documents, and the chunks of indexed coordinates such as `time`.

    Writers add these with `late=True`, so that they sit in the part of the
    archive a reader fetches when it opens it (spec §9.2) and cost no request.
    """
    return key.endswith("zarr.json") or key.split("/", 1)[0] in vds.xindexes


def write_vzip(
    vds: xr.Dataset,
    path: str | Path,
    *,
    mirror_refs: bool = True,
    relative_to: str | None = None,
    page_size: int | None = None,
) -> None:
    """Write a virtual dataset to `path`.

    If `relative_to` is given (a URL prefix, e.g. the directory that will hold
    the archive) reference URLs under it are stored relative to the archive.
    """
    concrete, refs = split_virtual_dataset(vds)
    with open(path, "wb") as f, VZipWriter(f, mirror_refs=mirror_refs, page_size=page_size) as w:
        for k in sorted(concrete):
            w.add_bytes(k, concrete[k], late=read_at_open(k, vds))
        if relative_to:
            refs = {k: (u.removeprefix(relative_to), o, n) for k, (u, o, n) in refs.items()}
        # most-used URL first: source index 0 costs nothing on the wire
        for url, _ in Counter(u for u, _, _ in refs.values()).most_common():
            w.url(url)
        for k in sorted(refs):
            w.add_ref(k, *refs[k])


def read_vzip(path: str, **kwargs) -> xr.Dataset:
    from vzip.store import VZipStore

    return xr.open_zarr(VZipStore(str(path), **kwargs), zarr_format=3)


__all__ = ["consolidate", "read_at_open", "read_vzip", "split_virtual_dataset", "write_vzip"]
