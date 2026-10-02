"""Virtualizing TIFF, NDPI, ND2 and DICOM files as OME-Zarr in vzip archives (VIRTUALIZE.md).

Each profile is a subpackage: tiff (profiles/tiff.md), ndpi (profiles/ndpi.md),
nd2 (profiles/nd2.md) and dicom (profiles/dicom.md); common holds what they share.

    python -m vzip.virtualize <url or path> <out.vzip> [--url <source url>]

Exits with status 3 if the input is rejected.

The archive's source is the input URL (or, with --url, the given one, which is
how a local file is described by the URL it will be served from).
"""

from __future__ import annotations

from vzip.virtualize import ndpi, nifti
from vzip.virtualize.common import Output, Rejected, file_reader, http_reader
from vzip.virtualize.dicom import is_dicom, virtualize_dicom
from vzip.virtualize.ims import virtualize_ims
from vzip.virtualize.nd2 import is_nd2, virtualize_nd2
from vzip.virtualize.tiff import virtualize_tiff

# The HDF5 signature, which starts Imaris IMS files (§1.2).
HDF5 = b"\x89HDF\r\n\x1a\n"
# The first bytes of a TIFF or BigTIFF file, in either byte order (§1.2).
TIFF_MAGIC = (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+")
NOT_SUPPORTED = "not a TIFF, NDPI, ND2, DICOM, NIfTI or IMS file"

__all__ = ["Output", "Rejected", "virtualize", "virtualize_dicom", "virtualize_ims", "virtualize_nd2", "virtualize_tiff"]


def virtualize(location: str, url: str | None = None) -> tuple[str, Output]:
    """(format, output) for the file at `location`, by its first bytes."""
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
