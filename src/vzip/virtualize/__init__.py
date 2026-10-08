"""Virtualizing image files and N5 / Zarr v2 stores in vzip archives (VIRTUALIZE.md).

Each profile is a subpackage: tiff, ndpi, nd2, dicom, nifti, ims (file inputs)
and n5, zarr2, ome_zarr (store inputs, profiles/n5.md, profiles/zarr2.md and
profiles/ome-zarr.md); common holds what they share, and store the store
machinery (§1.4–§1.6).

    python -m vzip.virtualize <url or path> <out.vzip> [--url <source url>]

Exits with status 3 if the input is rejected.

The archive's source is the input URL (or, with --url, the given one, which is
how a local file is described by the URL it will be served from). A URL ending
in `/`, or a local directory, is a store input.
"""

from __future__ import annotations

from vzip.virtualize import ndpi, nifti
from vzip.virtualize.common import Output, Rejected, file_reader, http_reader
from vzip.virtualize.dicom import is_dicom, virtualize_dicom
from vzip.virtualize.ims import virtualize_ims
from vzip.virtualize.n5 import virtualize_n5
from vzip.virtualize.nd2 import is_nd2, virtualize_nd2
from vzip.virtualize.ome_zarr import declares_04, virtualize_ome_zarr
from vzip.virtualize.store import WORKERS, StoreOutput, choose_profile, open_store
from vzip.virtualize.tiff import virtualize_tiff
from vzip.virtualize.zarr2 import virtualize_zarr2

# The HDF5 signature, which starts Imaris IMS files (§1.2).
HDF5 = b"\x89HDF\r\n\x1a\n"
# The first bytes of a TIFF or BigTIFF file, in either byte order (§1.2).
TIFF_MAGIC = (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+")
NOT_SUPPORTED = "not a TIFF, NDPI, ND2, DICOM, NIfTI or IMS file"

__all__ = ["Output", "Rejected", "StoreOutput", "virtualize", "virtualize_store", "virtualize_dicom", "virtualize_ims", "virtualize_nd2", "virtualize_tiff"]


def virtualize_store(location: str, url: str | None = None, *, max_objects: int | None = None,
                     workers: int = WORKERS) -> tuple[str, StoreOutput]:
    """(format, output) for the store at `location` (§1.4), its documents read
    by up to `workers` requests at a time."""
    store = open_store(location, url, max_objects=max_objects, workers=workers)
    try:
        fmt = choose_profile(store)
        if fmt == "n5":
            return fmt, virtualize_n5(store)
        if declares_04(store):
            return "ome-zarr", virtualize_ome_zarr(store)
        return fmt, virtualize_zarr2(store)
    finally:
        store.close()


def is_store(location: str) -> bool:
    if location.startswith(("http://", "https://")):
        return location.split("?", 1)[0].split("#", 1)[0].endswith("/")
    import os

    return os.path.isdir(location)


def virtualize(location: str, url: str | None = None, *,
               workers: int = WORKERS) -> tuple[str, Output | StoreOutput]:
    """(format, output) for the file at `location`, by its first bytes, or for
    the store at `location` (a URL ending in `/`, or a directory)."""
    if is_store(location):
        return virtualize_store(location, url, workers=workers)
    if location.startswith(("http://", "https://")):
        read, size = http_reader(location)
    else:
        read, size = file_reader(location)
    url = url or location
    head = read(0, min(552, size))
    if is_dicom(head):
        return "dicom", virtualize_dicom(url, read, size)
    if head[:4] in TIFF_MAGIC:
        first = ndpi.detect(read, size)
        if first is not None:
            return "ndpi", ndpi.virtualize_ndpi(url, read, size, first)
        return "tiff", virtualize_tiff(url, read, size)
    if is_nd2(head):
        return "nd2", virtualize_nd2(url, read, size)
    if head[:8] == HDF5:
        return "ims", virtualize_ims(url, read, size)
    if nifti.detect(head) is not None:
        return "nifti", nifti.virtualize_nifti(url, read, size)
    raise Rejected(NOT_SUPPORTED)
