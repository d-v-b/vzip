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
- **Attributes:** each node's `.zattrs`, whole and unchanged, under
  `vzip_virtualized.zarr2.attributes` on the node, including OME-NGFF 0.4
  metadata of groups below the store's root and xarray's
  `_ARRAY_DIMENSIONS` (Zarr v2 has no dimension names). They are not mixed
  into the node's own attributes, so they cannot collide with Zarr v3
  conventions or be read as OME-NGFF 0.5. Members of `.zgroup` or `.zarray`
  that the `zarr.json` does not reproduce are under
  `vzip_virtualized.zarr2.metadata`.
- **Other objects:** any object of the store that is neither a document nor
  a chunk (a README, `.zmetadata`) is kept whole, referenced in place, under
  `vzip_source/objects/`.

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
{"format": "zarr2", "groups": 0, "arrays": 1, "chunks": 24, "emptyChunks": 0, "objects": 25, "otherObjects": 0, "listingRequests": 1}
```

This took about 1 s and wrote a 3.6 KB archive, at spec/virtualize.md revision 16
([overview](../README.md#formats)). (`otherObjects`, the objects kept under
`vzip_source/objects/`, is new since then; it is 0 here, as the store's 25
objects are its `.zarray` and 24 chunks.) The store's root is an
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
worker still serves the Zarr v3 store, and `node js/conformance/virtualize.ts
<url> out.vzip` writes the archive with the browser code. A store that lists
more than 100000 objects fails in the browser with HTTP 507.

## Viewing

The Neuroglancer fork (<https://github.com/d-v-b/neuroglancer>, branch `vzip`)
reads the `v2` chunk key encoding and the `transpose`, `bytes`, `zlib`,
`gzip`, `zstd` and `blosc` codecs, and `uint8` to `uint32`, `int8` to
`int32`, `uint64` and `float32` data. It has no `bool`, `float16`, `int64`
or `float64` data type, so arrays of those types cannot be shown there. The
demo page needs an OME-Zarr image; groups with OME-NGFF 0.4 metadata below
the root keep it only under `vzip_virtualized`, where an OME-NGFF reader does
not look, so they are not shown as images. (A store whose root is OME-NGFF
0.4 is migrated instead: see [ome-zarr](../ome-zarr/README.md).)

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
  listing, so stale consolidated metadata cannot change the output. It is
  kept, like any other object, under `vzip_source/objects/`.

## Performance

- **Listing:** S3 returns 1000 keys per request, sequentially. An
  OpenOrganelle N5 level of 382288 chunks took 328 s, almost all listing,
  and gave a 43.7 MB archive
  ([REVISIONS.md](../../../spec/history/virtualize/REVISIONS.md), revision
  12).
- **Many small groups cost one request per document.** The `.zgroup`,
  `.zarray` and `.zattrs` documents are read concurrently, 16 requests at a
  time by default (`--workers N` in Python, `concurrency` in the browser),
  holding at most 64 MiB of documents ahead. At revision 16, when they were
  read one at a time, OpenOrganelle's `labels/groundtruth/crop155/` (65
  groups, 384 arrays, 22484 objects) took 4.4 minutes with only 23 listing
  requests, and gave a 3.3 MB archive; it has not been measured since.
- **The archive grows with the number of chunks:** one `url` source and one
  entry per chunk. Virtualize the arrays or groups you need.
- **The browser stops at 100000 listed objects** (HTTP 507).
- Reading a chunk is one GET of one chunk object.

## How it's verified

`js/test/zarr2/verify.py` virtualizes the synthetic Zarr v2 stores in
[fixtures/zarr2/](../../../fixtures/zarr2/) (including
stores to reject) with the browser code and compares every array, read
through `python/src/vzip` and zarr-python, with zarr-python's own Zarr v2 reader.
`compare.py` checks that the Python and browser outputs are equivalent on
those stores and on the 6 OpenOrganelle stores of
[corpus_zarr2.txt](../../../conformance/virtualize/corpus_zarr2.txt).

- Profile: [spec/virtualize/zarr2/profile.md](../../../spec/virtualize/zarr2/profile.md)
- Python: [python/src/vzip/virtualize/zarr2/](../../../python/src/vzip/virtualize/zarr2/)
- Browser: [js/src/virtualize/zarr2/](../../../js/src/virtualize/zarr2/)
- Fixtures: [fixtures/zarr2/](../../../fixtures/zarr2/)
