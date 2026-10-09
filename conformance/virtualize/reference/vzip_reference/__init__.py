"""The frozen reference oracle of the TIFF, ND2 and CZI profiles: the Python
implementations `vzip.virtualize` shipped before it moved these three formats to
the Rust IR (rust/vzip-ir), kept unchanged (but for their import paths) so that
`compare.py` can compare the IR path against an independent implementation.
Nothing shipped imports this package. Every other format, and what the profiles
share (`vzip.virtualize.common`), is the shipped code.

`virtualize(location, url, checksums=...)` is `vzip.virtualize.virtualize` as it
was: the same dispatch by first bytes, the same readers (no reader policy), with
TIFF, ND2 and CZI going to the frozen modules. A store is read by the shipped
store reader, under `policy` (default: `Policy()`, private hosts refused)."""

from __future__ import annotations

from vzip.policy import Policy, crc32c
from vzip.virtualize import is_store, virtualize_store
from vzip.virtualize.common import Output, Rejected, file_reader, http_reader
from vzip.virtualize.dicom import is_dicom, virtualize_dicom
from vzip.virtualize.ims import virtualize_ims
from vzip.virtualize import ndpi, nifti
from vzip.virtualize.safe import SafeOutput, is_zip, virtualize_safe_zip
from vzip_reference.czi import is_czi, virtualize_czi
from vzip_reference.nd2 import is_nd2, virtualize_nd2
from vzip_reference.tiff import virtualize_tiff

HDF5 = b"\x89HDF\r\n\x1a\n"
TIFF_MAGIC = (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+")
NOT_SUPPORTED = "not a TIFF, NDPI, ND2, DICOM, NIfTI, IMS, CZI or SAFE zip file"


def virtualize(location: str, url: str | None = None, *, checksums: bool = False,
               policy: Policy | None = None):
    if is_store(location):
        return virtualize_store(location, url, checksums=checksums, policy=policy)
    read, size = http_reader(location) if location.startswith(("http://", "https://")) else file_reader(location)
    url = url or location
    fmt, out = _virtualize_file(url, read, size)
    out.size = size
    out.etag = read.etag() if url == location and hasattr(read, "etag") else None
    if checksums:
        exact = getattr(read, "uncached", read)
        out.checksum = lambda u, offset, length: crc32c(exact(offset, length))
    return fmt, out


def _virtualize_file(url: str, read, size: int) -> tuple[str, Output | SafeOutput]:
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
    if is_czi(head):
        return "czi", virtualize_czi(url, read, size)
    if is_zip(head):
        return "safe", virtualize_safe_zip(url, read, size)
    raise Rejected(NOT_SUPPORTED)
