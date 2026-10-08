"""The OME-Zarr profile (profiles/ome-zarr.md, §11): OME-Zarr 0.4 on Zarr v2 as OME-Zarr 0.5 on Zarr v3."""

from vzip.virtualize.ome_zarr.virtualize import declares_04, virtualize_ome_zarr

__all__ = ["declares_04", "virtualize_ome_zarr"]
