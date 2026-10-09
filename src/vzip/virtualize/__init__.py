"""Virtualizing image files and N5 / Zarr v2 stores in vzip archives (VIRTUALIZE.md).

TIFF, ND2 and CZI files go through the IR (`vzip.ir`): the Rust core's sans-IO
parser of the format, driven by its read planner over this package's I/O, projected
to the convention's hierarchy and mirrored under `vzip_source` by the Rust core.
Every other profile is a subpackage: ndpi, dicom, nifti, ims (file inputs), n5,
zarr2, ome_zarr (store inputs, profiles/n5.md, profiles/zarr2.md and
profiles/ome-zarr.md), and safe (Sentinel-2 SAFE products, profiles/safe.md: a
store input, or a zip file); common holds what they share, and store the store
machinery (§1.4–§1.6). (`tiff/` keeps the IFD reader and tag translator NDPI uses.)

    python -m vzip.virtualize <url or path> <out.vzip> [--url <source url>] [--checksums]
        [--allow-private-hosts] [--connections N] [--byte-cost SECONDS_PER_MB]

Exits with status 3 if the input is rejected.

A file at an http(s) URL is opened under the reader policy (SPEC.md §8.7): a host
that is, or resolves to, a loopback, private, link-local or special address is
refused unless the policy has `allow_private_hosts` (`--allow-private-hosts`), and
a request that would go through a proxy unless `allow_unchecked_proxy`. TIFF, ND2
and CZI sources are read entirely under it; the other profiles' readers are
today's, after the policy has admitted the source's first request.

Every url source pins its size, and the input file's source its ETag when
every response for it gave the same strong one (VIRTUALIZE.md §1.2, §1.4).
`--checksums` also records the CRC-32C of every range of a url source
(SPEC.md §5.2), which reads all the bytes the archive references: off by
default.

The archive's source is the input URL (or, with --url, the given one, which is
how a local file is described by the URL it will be served from). A URL ending
in `/`, or a local directory, is a store input.
"""

from __future__ import annotations

from vzip.policy import Policy, crc32c
from vzip.virtualize import ndpi, nifti
from vzip.virtualize.common import Output, Rejected, file_reader, http_reader
from vzip.virtualize.dicom import is_dicom, virtualize_dicom
from vzip.virtualize.ims import virtualize_ims
from vzip.virtualize.n5 import virtualize_n5
from vzip.virtualize.ome_zarr import declares_04, virtualize_ome_zarr
from vzip.virtualize.safe import SafeOutput, is_zip, virtualize_safe_store, virtualize_safe_zip
from vzip.virtualize.store import WORKERS, StoreOutput, choose_profile, object_url, open_store
from vzip.virtualize.zarr2 import virtualize_zarr2

# The HDF5 signature, which starts Imaris IMS files (§1.2).
HDF5 = b"\x89HDF\r\n\x1a\n"
# The first bytes of a TIFF or BigTIFF file, in either byte order (§1.2).
TIFF_MAGIC = (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+")
ND2_MAGIC = bytes.fromhex("DACEBE0A")
CZI_MAGIC = b"ZISRAWFILE" + bytes(6)
NOT_SUPPORTED = "not a TIFF, NDPI, ND2, DICOM, NIfTI, IMS, CZI or SAFE zip file"

__all__ = ["Output", "Rejected", "StoreOutput", "virtualize", "virtualize_store", "virtualize_dicom", "virtualize_ims"]


def virtualize_store(location: str, url: str | None = None, *, max_objects: int | None = None,
                     checksums: bool = False, workers: int = WORKERS) -> tuple[str, StoreOutput | SafeOutput]:
    """(format, output) for the store at `location` (§1.4), its documents read
    by up to `workers` requests at a time; with `checksums`, the output's ranges
    carry the CRC-32C of their bytes (SPEC.md §5.2)."""
    store = open_store(location, url, max_objects=max_objects, workers=workers)
    try:
        fmt = choose_profile(store)
        if fmt == "safe":
            fmt, out = fmt, virtualize_safe_store(store)
        elif fmt == "n5":
            fmt, out = fmt, virtualize_n5(store)
        elif declares_04(store):
            fmt, out = "ome-zarr", virtualize_ome_zarr(store)
        else:
            fmt, out = fmt, virtualize_zarr2(store)
    finally:
        store.close()
    if checksums:
        keys = {object_url(store.url, k): k for k in store.objects}
        out.checksum = lambda u, offset, length: crc32c(store.read_range(keys[u], offset, length))
    return fmt, out


def is_store(location: str) -> bool:
    if location.startswith(("http://", "https://")):
        return location.split("?", 1)[0].split("#", 1)[0].endswith("/")
    import os

    return os.path.isdir(location)


def virtualize(location: str, url: str | None = None, *, checksums: bool = False,
               policy: Policy | None = None, connections: int | None = None,
               byte_cost: float | None = None, workers: int = WORKERS) -> tuple[str, Output | StoreOutput | SafeOutput]:
    """(format, output) for the file at `location`, by its first bytes, or for
    the store at `location` (a URL ending in `/`, or a directory).

    Source 0 pins the file's size, and its ETag when every response gave the
    same strong one and the output names the URL that was read (§1.2). With
    `checksums`, the output's ranges of source 0 carry the CRC-32C of their
    bytes (SPEC.md §5.2), which reads them. `policy` is the reader policy a file
    at an http(s) URL is read under (default: `Policy()`, private hosts refused).
    For TIFF, ND2 and CZI, `connections` is how many requests run at a time to a
    remote source (default 8), and `byte_cost` the seconds of wall time the read
    planner charges a byte fetched beyond those asked (default 0.1 s per MB).
    A store's documents are read by up to `workers` requests at a time."""
    if is_store(location):
        return virtualize_store(location, url, checksums=checksums, workers=workers)
    from vzip.ir.planner import open_transport

    transport = open_transport(location, policy)
    size = transport.size
    held = transport.head[1]

    def first(offset: int, length: int) -> bytes:
        """The source's bytes, from those the transport opened it with when it holds them."""
        if offset < 0 or offset + length > size:
            raise Rejected(f"read of [{offset}, {offset + length}) outside the {size}-byte file")
        if offset + length <= len(held):
            return held[offset:offset + length]
        return transport.get(offset, offset + length)

    head = first(0, min(552, size))
    ir_format = None
    if not is_dicom(head):
        if head[:4] in TIFF_MAGIC:
            ndpi_first = ndpi.detect(first, size)
            ir_format = "tiff" if ndpi_first is None else None
        elif head[:4] == ND2_MAGIC:
            ir_format = "nd2"
        elif head[:16] == CZI_MAGIC:
            ir_format = "czi"
    if ir_format is not None:
        from vzip.ir import virtualize as via_ir

        fmt, out, _ = via_ir(location, url, transport=transport, connections=connections, byte_cost=byte_cost)
        if checksums:
            out.checksum = lambda u, offset, length: crc32c(transport.get(offset, offset + length))
        return fmt, out
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
    """The profiles that are not the IR's, by the file's first bytes."""
    head = read(0, min(552, size))
    if is_dicom(head):
        return "dicom", virtualize_dicom(url, read, size)
    if head[:4] in TIFF_MAGIC:
        first = ndpi.detect(read, size)
        if first is not None:
            return "ndpi", ndpi.virtualize_ndpi(url, read, size, first)
    if head[:8] == HDF5:
        return "ims", virtualize_ims(url, read, size)
    if nifti.detect(head) is not None:
        return "nifti", nifti.virtualize_nifti(url, read, size)
    if is_zip(head):
        return "safe", virtualize_safe_zip(url, read, size)
    raise Rejected(NOT_SUPPORTED)
