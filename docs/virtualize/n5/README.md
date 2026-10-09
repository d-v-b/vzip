# N5

vzip presents a chunked array store as a Zarr v3 hierarchy by writing a
small archive whose chunk entries point at the store's own objects, without
copying them ([overview](../README.md)). This page is about N5 containers.

## Who this is for

N5 is the chunked format of the Java imaging world: BigDataViewer, Fiji's N5
plugins, Paintera, and Janelia's pipelines write it, and OpenOrganelle
publishes its FIB-SEM volumes and segmentations as N5 in the public S3 bucket
`janelia-cosem-datasets`. The people with N5 data want to open it with Zarr
v3 tools and OME-Zarr viewers, which do not read N5, without rewriting
terabytes of blocks.

vzip lists the store, reads its `attributes.json` documents, and writes an
archive in which every N5 dataset is a Zarr v3 array and every block is one
chunk entry referencing the block object as it is. COSEM and n5-viewer
multiscale metadata become OME-NGFF 0.5 multiscales.

## What you get

A Zarr v3 hierarchy with a node at the path of every N5 group and dataset.

- **Arrays:** the dataset's `dimensions` as the shape and `blockSize` as the
  chunk shape, in N5's order (not reversed); data types `uint8` to `uint64`,
  `int8` to `int64`, `float32`, `float64`.
- **Codec:** `n5_default` from the zarr-extensions registry, wrapping
  `transpose`, big-endian `bytes` and the block's compression (`gzip`,
  `zlib`, `zstd` or `blosc`). The codec parses each block's header, so the
  truncated edge blocks Java N5 writes read correctly.
- **Chunk keys:** the `v2` encoding with `/`, so chunk keys are the N5 block
  keys and every chunk entry is the block object at the same key.
- **Attributes:** each group's and dataset's attributes are kept on its
  node, as `vzip_virtualized.n5.attributes` (without the N5 structural
  members, which the Zarr v3 metadata holds; any the Zarr v3 metadata does
  not reproduce, such as the container's `n5` version, are under
  `vzip_virtualized.n5.metadata`). They are not mixed into the node's own
  attributes, so they cannot be mistaken for OME-NGFF metadata.
- **Other objects:** any object of the store that is neither an
  `attributes.json` nor a block (a README, say) is kept whole, referenced in
  place, under `vzip_source/objects/`.
- **Multiscales:** a group with COSEM `multiscales` (as on OpenOrganelle) or
  n5-viewer `scales`/`downsamplingFactors` (BigDataViewer, Paintera) also
  gets `"ome"`: an OME-NGFF 0.5 multiscale with axes, units, scales and
  translations, the COSEM C-order transforms reversed onto N5's dimension
  order; each level array gets `dimension_names`.

## Try it

A segmentation from OpenOrganelle's HeLa-2 dataset, a COSEM multiscale group
of 5 levels and 1612 blocks (CC BY 4.0), from
[corpus_n5.txt](../../../conformance/virtualize/corpus_n5.txt). Note the
trailing `/`, which makes the URL a store:

```bash
uv run python -m vzip.virtualize https://janelia-cosem-datasets.s3.amazonaws.com/jrc_hela-2/jrc_hela-2.n5/labels/mito_seg/ mito_seg.vzip
```

```
{"format": "n5", "groups": 1, "arrays": 5, "chunks": 1612, "emptyChunks": 0, "objects": 1618, "otherObjects": 0, "images": [{"path": "", "convention": "cosem"}], "listingRequests": 2}
```

This took 5 s and wrote a 198 KB archive, at VIRTUALIZE.md revision 16
([overview](../README.md#formats)). (`otherObjects`, the objects kept under
`vzip_source/objects/`, is new since then; it is 0 here, as the store's 1618
objects are its 6 documents and 1612 blocks.)

```python
import zarr
from vzip import VZipStore

g = zarr.open_group(VZipStore("mito_seg.vzip"), mode="r")
ms = g.attrs["ome"]["multiscales"][0]
print([(a["name"], a.get("unit")) for a in ms["axes"]])
for d in ms["datasets"]:
    a = g[d["path"]]
    print(d["path"], a.shape, a.chunks, a.dtype, d["coordinateTransformations"][0]["scale"])
a = g["s4"]
print(a.metadata.codecs)
print(a.metadata.dimension_names, a.attrs["vzip_virtualized"]["n5"]["attributes"]["transform"]["axes"])
print(a[200, 50, 180:190])
```

```
[('x', 'nanometer'), ('y', 'nanometer'), ('z', 'nanometer')]
s0 (12000, 1600, 6368) (512, 512, 512) uint16 [4.0, 4.0, 5.24]
s1 (6000, 800, 3184) (256, 256, 256) uint16 [8.0, 8.0, 10.48]
s2 (3000, 400, 1592) (128, 128, 128) uint16 [16.0, 16.0, 20.960000000000004]
s3 (1500, 200, 796) (64, 64, 64) uint16 [32.0, 32.0, 41.92]
s4 (750, 100, 398) (64, 64, 64) uint16 [64.0, 64.0, 83.84]
(N5DefaultCodec(codecs=(TransposeCodec(order=(2, 1, 0)), BytesCodec(endian='big'), GzipCodec(level=1))),)
('x', 'y', 'z') ['z', 'y', 'x']
[203 203 203 203   0 203 203 203   0   0]
```

The COSEM transform, one of the dataset's own attributes, lists its axes as
`z, y, x` (C order); the OME axes and `dimension_names` are `x, y, z`, the N5
dimension order. The values match
the block `s4/3/0/2` decoded by hand (gunzip, big-endian `uint16`).

**In the browser.** Not on the live demo yet: it virtualizes only TIFF,
NDPI and ND2. A local build of the demo (see the [overview](../README.md))
virtualizes this group in about 2 s and lists its 5 levels, but its
Neuroglancer view stays empty (next section). A store that lists more than
100000 objects fails in the browser with HTTP 507.

## Viewing

The Neuroglancer fork (<https://github.com/d-v-b/neuroglancer>, branch `vzip`)
cannot display N5 archives yet: it has no `n5_default` codec, so the layer
loads no data. zarr-python, with `vzip` imported (which registers `n5_default`), reads them.
Neuroglancer's own N5 data source (`<url>|n5:`), which the fork keeps,
reads the original store directly, and is the way to view N5 data today.

## Supported and not supported

Supported:

- N5 containers (specification 2.x to 4.0.0) on any S3-compatible host that
  answers `ListObjectsV2` anonymously, or in a local directory (with
  `--url`, the store URL it will be served from).
- Groups (explicit and implicit) and datasets of 1 to 32 dimensions.
- Raw, gzip, zlib (`useZlib`), zstd and blosc blocks; the old
  `compressionType` form.

Rejected:

- lz4, xz, bzip2 and jpeg compression, and blosc configurations that do not
  map exactly onto the Zarr `blosc` codec.
- `string` and `object` data types.
- A store URL whose server has no listing operation (a plain web server, or
  a directory listing page):
  `rejected: listing is not well formed: ...`.

Notes:

- **The URL must end in `/`.** Without it, the URL is read as a single file
  and fails (HTTP 404).
- **Virtual-hosted and path-style URLs both work:**
  `https://janelia-cosem-datasets.s3.amazonaws.com/...` and
  `https://s3.amazonaws.com/janelia-cosem-datasets/...` give the same
  objects. Hosts outside `amazonaws.com` (such as Ceph or MinIO) are read as
  path-style, so the first path segment is the bucket. A bucket whose name
  has a label `s3` or `s3-…` is not supported in a path-style URL.
- Varlength and object-mode blocks are not detected (the blocks are not
  read); such a chunk fails when it is decoded, and the rest of the array
  reads.

## Performance

- **Listing dominates.** S3 returns 1000 keys per listing request, and the
  requests are sequential. (The `attributes.json` documents are then read
  concurrently, 16 at a time by default, `--workers N`.) OpenOrganelle's
  full-resolution
  `jrc_hela-2.n5/em/fibsem-uint16/s0` (382288 blocks) took 328 s, almost all
  of it the 383 listing requests, and gave a 43.7 MB archive (40.1 MB of it
  block URLs, which deflate to 0.98 MB) that opens in 1.2 s
  ([REVISIONS.md](../../../conformance/virtualize/REVISIONS.md), revision 12).
- **The archive grows with the number of blocks:** one `url` source and one
  entry per block, about 115 bytes each. Prefer virtualizing the levels or
  groups you need.
- **The browser stops at 100000 listed objects** (HTTP 507), which bounds
  its archive at about 25 MB and the listing at 100 requests. The Python
  command has no limit.
- Reading a chunk is one GET of one block object.

## How it's verified

`web/test/n5/verify.py` virtualizes the synthetic N5 stores in
[web/test/fixtures/n5/](../../../web/test/fixtures/n5/) (including stores to
reject) with the browser code, and compares every array, read through
`src/vzip` and zarr-python's `n5_default`, with an independent block reader
in the script and with `zarr-n5`. `compare.py` checks that the Python and
browser outputs are equivalent on those stores and on the 7 OpenOrganelle
stores of [corpus_n5.txt](../../../conformance/virtualize/corpus_n5.txt)
(the full-resolution level with the Python command only).

- Profile: [profiles/n5.md](../../../profiles/n5.md)
- Python: [src/vzip/virtualize/n5/](../../../src/vzip/virtualize/n5/)
- Browser: [web/src/virtualize/n5/](../../../web/src/virtualize/n5/)
- Fixtures: [web/test/fixtures/n5/](../../../web/test/fixtures/n5/)
