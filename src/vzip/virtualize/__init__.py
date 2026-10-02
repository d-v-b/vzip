"""Virtualizing TIFF, NDPI and ND2 files as OME-Zarr in vzip archives (VIRTUALIZE.md).

Each profile is a subpackage: tiff (profiles/tiff.md), ndpi (profiles/ndpi.md) and
nd2 (profiles/nd2.md); common holds what they share.

    python -m vzip.virtualize <url or path> <out.vzip> [--url <source url>]

Exits with status 3 if the input is rejected.

The archive's source is the input URL (or, with --url, the given one, which is
how a local file is described by the URL it will be served from).
"""

from __future__ import annotations

from vzip.virtualize import ndpi
from vzip.virtualize.common import Output, Rejected, file_reader, http_reader
from vzip.virtualize.nd2 import is_nd2, virtualize_nd2
from vzip.virtualize.tiff import virtualize_tiff

__all__ = ["Output", "Rejected", "virtualize", "virtualize_nd2", "virtualize_tiff"]


def virtualize(location: str, url: str | None = None) -> tuple[str, Output]:
    """(format, output) for the file at `location`, by its first bytes."""
    if location.startswith(("http://", "https://")):
        read, size = http_reader(location)
    else:
        read, size = file_reader(location)
    url = url or location
    head = read(0, min(8, size))
    if head[:2] in (b"II", b"MM"):
        first = ndpi.detect(read, size)
        if first is not None:
            return "ndpi", ndpi.virtualize_ndpi(url, read, size, first)
        return "tiff", virtualize_tiff(url, read, size)
    if is_nd2(head):
        return "nd2", virtualize_nd2(url, read, size)
    raise Rejected("not a TIFF or ND2 file")
