# Zarr v2

vzip presents a chunked array store as a Zarr v3 hierarchy by writing a
small archive whose chunk entries point at the store's own objects, without
copying them ([overview](../README.md)). This page is about Zarr v2
hierarchies.

## Who this is for

A large share of the world's Zarr data is Zarr v2: xarray and dask
pipelines, napari and CellMap datasets, and OpenOrganelle's newer releases in
the `janelia-cosem-datasets` S3 bucket. Tools built for Zarr v3, and readers
that expect Zarr v3 metadata, cannot use it directly, and rewriting the
metadata in place is not possible on a bucket you do not own.

vzip lists the store, reads its `.zgroup`, `.zarray` and `.zattrs`
documents, and writes an archive with a Zarr v3 `zarr.json` for every group
and array. Each chunk entry references the existing chunk object under the
same key, through Zarr v3's `v2` chunk key encoding, so nothing is copied or
re-encoded.

## What you get

A Zarr v3 hierarchy with a node at the path of every Zarr v2 group and
array.

- **Arrays:** the same shape, chunks and data type (`bool`, `int8` to
  `int64`, `uint8` to `uint64`, `float16`, `float32`, `float64`, either byte
  order); `fill_value` carried over (`null` becomes 0 or `false`).
- **Codecs:** `transpose` for `order: "F"`, `bytes` with the byte order,
  then the compressor: `zlib`, `gzip`, `zstd` or `blosc` (any of blosc's
  internal compressors, including `lz4`).
- **Chunk keys:** the `v2` encoding with the array's own separator (`.` or
  `/`), so the keys are unchanged.
- **Attributes:** copied unchanged, including OME-NGFF 0.4 metadata of
  groups below the store's root. Zarr v2 has no dimension names; xarray's
  `_ARRAY_DIMENSIONS` stays an attribute.

A store whose **root** declares OME-NGFF 0.4 is migrated to OME-Zarr 0.5
instead; see [ome-zarr](../ome-zarr/README.md).

## Try it

OpenOrganelle's HeLa-2 FIB-SEM at its lowest resolution, one array of 24
blosc chunks (CC BY 4.0), from
[corpus_zarr2.txt](../../../conformance/virtualize/corpus_zarr2.txt). Note
the trailing `/`:

```bash
uv run python -m vzip.virtualize https://janelia-cosem-datasets.s3.amazonaws.com/jrc_hela-2/jrc_hela-2.zarr/recon-1/em/fibsem-uint8/s5/ s5.vzip
```

```
{"format": "zarr2", "groups": 0, "arrays": 1, "chunks": 24, "emptyChunks": 0, "objects": 25, "listingRequests": 1}
```

This took about 1 s and wrote a 3.6 KB archive. The store's root is an
array, so open it as one:

```python
import zarr
from vzip import VZipStore

a = zarr.open_array(VZipStore("s5.vzip"), mode="r")
print(a.shape, a.dtype, a.chunks)
print(a.metadata.chunk_key_encoding, a.metadata.codecs)
print(a[100, 25, 180:188])
```

```
(199, 50, 375) uint8 (64, 64, 64)
V2ChunkKeyEncoding(separator='/') (BytesCodec(endian=None), BloscCodec(_tunable_attrs=set(), typesize=1, cname='lz4', clevel=5, shuffle='shuffle', blocksize=0))
[139 139 141 138 140 140 140 139]
```

zarr-python's own Zarr v2 reader gives the same values from the store.

**In the browser.** Not on the live demo yet: it virtualizes only TIFF,
NDPI and ND2. A local build of the demo (see the [overview](../README.md))
virtualizes this store, but its page shows only OME-Zarr images, so for a
bare array it reports `not an OME-Zarr multiscale image`; the service
worker still serves the Zarr v3 store, and `node web/conformance/virtualize.ts
<url> out.vzip` writes the archive with the browser code. A store that lists
more than 100000 objects fails in the browser with HTTP 507.

## Viewing

The Neuroglancer fork (<https://github.com/d-v-b/neuroglancer>, branch `vzip`)
reads the `v2` chunk key encoding and the `transpose`, `bytes`, `zlib`,
`gzip`, `zstd` and `blosc` codecs, and `uint8` to `uint32`, `int8` to
`int32`, `uint64` and `float32` data. It has no `bool`, `float16`, `int64`
or `float64` data type, so arrays of those types cannot be shown there. The
demo page needs an OME-Zarr image; groups with OME-NGFF 0.4 metadata below
the root keep it as 0.4 attributes on Zarr v3, which the fork's OME parser
accepts, but this was not checked with a real store.

## Supported and not supported

Supported:

- Zarr v2 groups and arrays of 0 to 32 dimensions, C or F order, `.` or `/`
  separators, on any S3-compatible host that answers `ListObjectsV2`
  anonymously, or in a local directory (with `--url`).
- `zlib`, `gzip`, `zstd` and `blosc` compressors, or none.

Rejected:

- Filters (a nonempty `filters` list).
- Other compressors: `lz4`, `bz2`, `lzma`, `delta`, ...
- String, object, structured, complex and date-time data types, and fill
  values the data type cannot hold.
- A group and an array at the same path.
- A store URL whose server has no listing operation (a plain web server).

Notes:

- **The URL must end in `/`.** Without it, the URL is read as a single file
  and fails (HTTP 404).
- **Virtual-hosted and path-style URLs both work**
  (`https://janelia-cosem-datasets.s3.amazonaws.com/...` and
  `https://s3.amazonaws.com/janelia-cosem-datasets/...`); see the
  [N5 page](../n5/README.md#supported-and-not-supported) for the rule.
- Consolidated metadata (`.zmetadata`) is never read: nodes come from the
  listing, so stale consolidated metadata cannot change the output.

## Performance

- **Listing:** S3 returns 1000 keys per request, sequentially. An
  OpenOrganelle N5 level of 382288 chunks took 328 s, almost all listing,
  and gave a 43.7 MB archive
  ([REVISIONS.md](../../../conformance/virtualize/REVISIONS.md), revision
  12).
- **Many small groups cost one request per document.** OpenOrganelle's
  `labels/groundtruth/crop155/` (65 groups, 384 arrays, 22484 objects) took
  4.4 minutes with only 23 listing requests: the Python command reads the
  `.zgroup`, `.zarray` and `.zattrs` documents one at a time. The archive
  is 3.3 MB.
- **The archive grows with the number of chunks:** one `url` source and one
  entry per chunk. Virtualize the arrays or groups you need.
- **The browser stops at 100000 listed objects** (HTTP 507).
- Reading a chunk is one GET of one chunk object.

## How it's verified

`web/test/zarr2/verify.py` virtualizes the synthetic Zarr v2 stores in
[web/test/fixtures/zarr2/](../../../web/test/fixtures/zarr2/) (including
stores to reject) with the browser code and compares every array, read
through `src/vzip` and zarr-python, with zarr-python's own Zarr v2 reader.
`compare.py` checks that the Python and browser outputs are equivalent on
those stores and on the 6 OpenOrganelle stores of
[corpus_zarr2.txt](../../../conformance/virtualize/corpus_zarr2.txt).

- Profile: [profiles/zarr2.md](../../../profiles/zarr2.md)
- Python: [src/vzip/virtualize/zarr2/](../../../src/vzip/virtualize/zarr2/)
- Browser: [web/src/virtualize/zarr2/](../../../web/src/virtualize/zarr2/)
- Fixtures: [web/test/fixtures/zarr2/](../../../web/test/fixtures/zarr2/)
