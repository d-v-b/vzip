# OME-Zarr 0.4 to 0.5, in place

vzip presents a chunked array store as a Zarr v3 hierarchy by writing a
small archive whose chunk entries point at the store's own objects, without
copying them ([overview](../README.md)). This page is about migrating
OME-Zarr 0.4 data to OME-Zarr 0.5.

## Who this is for

Most published OME-Zarr is version 0.4 on Zarr v2: the IDR's OME-NGFF
samples on EMBL-EBI's Embassy S3, BioImage Archive submissions, and the
output of bioformats2raw and omero-zarr. OME-Zarr 0.5 moved to Zarr v3, and
newer tools and viewers increasingly expect it. Migrating means rewriting
every chunk's metadata path and, in practice, copying the data; for a public
dataset you do not own, it means a second copy.

Here the input is already Zarr, so this is format migration rather than
virtualization. vzip lists the 0.4 store, checks it against the parts of
OME-NGFF 0.4 that 0.5 also requires, and writes an archive that is the same
data as OME-Zarr 0.5: new `zarr.json` documents with the metadata rewritten
under `ome`, and every chunk referenced in place, unchanged. The archive is
a few hundred bytes per chunk.

## What you get

The hierarchy of the [Zarr v2 profile](../zarr2/README.md) (same groups,
arrays, chunks and codecs, with the `v2` chunk key encoding), with:

- **OME groups rewritten as 0.5:** images, labels, plates, wells and
  bioformats2raw collections get `"ome": {"version": "0.5", ...}` holding
  their `multiscales`, `omero`, `labels`, `image-label`, `plate`, `well`,
  `bioformats2raw.layout` and `series`, without the old per-object
  `version` members. Other attributes are kept under
  `vzip_virtualized["ome-zarr"].attributes` on the same node, with the
  Zarr v2 profile's other source metadata
  ([zarr2](../zarr2/README.md#what-you-get)).
- **`dimension_names`** on every image level, from the multiscale's axes, as
  0.5 requires.
- **Label images with one level too many** (omero-zarr gives each label one
  more level than its image) keep only as many levels as the image in their
  multiscale; the extra array stays in the hierarchy as a plain array.
- **OME-XML:** `OME/METADATA.ome.xml` of a bioformats2raw collection is kept,
  referenced in place.

## Try it

An IDR image with a label image (idr0062, 1475 objects; CC BY 4.0), from
[corpus_ome_zarr.txt](../../../conformance/virtualize/corpus_ome_zarr.txt).
Note the trailing `/`:

```bash
uv run python -m vzip.virtualize https://uk1s3.embassy.ebi.ac.uk/idr/zarr/v0.4/idr0062A/6001240.zarr/ 6001240.vzip
```

```
{"format": "ome-zarr", "groups": 3, "arrays": 7, "chunks": 1462, "emptyChunks": 0, "objects": 1475, "images": 2, "labels": 1, "droppedLabelLevels": 1, "plates": 0, "wells": 0, "fields": 0, "omeXml": 0, "otherObjects": 0, "listingRequests": 2}
```

This took 4 s and wrote a 201 KB archive, at VIRTUALIZE.md revision 16
([overview](../README.md#formats)). (`otherObjects`, the objects kept under
`vzip_source/objects/`, is new since then; it is 0 here, as every object is
a document or a chunk.) The label image had 4 levels to
its image's 3; one was dropped from its multiscale.

```python
import zarr
from vzip import VZipStore

g = zarr.open_group(VZipStore("6001240.vzip"), mode="r")
ome = g.attrs["ome"]
print(ome["version"], [a["name"] for a in ome["multiscales"][0]["axes"]], g["labels"].attrs["ome"]["labels"])
a = g["0"]
print(a.shape, a.dtype, a.chunks, a.metadata.dimension_names, a.metadata.chunk_key_encoding)
lab = g["labels/0"]
print(len(lab.attrs["ome"]["multiscales"][0]["datasets"]), sorted(lab.array_keys()), lab["0"].dtype)
print(a[0, 1, 100, 100:106])
```

```
0.5 ['c', 'z', 'y', 'x'] ['0']
(2, 236, 275, 271) uint16 (1, 1, 275, 271) ('c', 'z', 'y', 'x') V2ChunkKeyEncoding(separator='/')
3 ['0', '1', '2', '3'] int8
[ 9  9  9  9  8 10]
```

zarr-python's Zarr v2 reader gives the same values from the 0.4 store.

**In the browser.** Not on the live demo yet: it virtualizes only TIFF,
NDPI and ND2. A local build of the demo (see the [overview](../README.md))
migrates this store in about 2 s and shows the image in Neuroglancer, one
layer per channel. The demo page shows images only: for a plate it does not
draw the wells. A store that lists more than 100000 objects fails in the
browser with HTTP 507 (the IDR's `idr0001A/2551.zarr` plate, 102114
objects, is one).

## Viewing

The Neuroglancer fork (<https://github.com/d-v-b/neuroglancer>, branch `vzip`)
reads OME-Zarr 0.5 multiscales, the `v2` chunk key encoding and the `blosc`,
`zlib`, `gzip` and `zstd` codecs; the image above renders through a local
build of the demo. It has no `bool`, `float16`, `int64` or `float64` data
type: images or label images of those types cannot be shown there.

## Supported and not supported

Supported:

- A Zarr v2 store whose root is an OME-NGFF 0.4 image, plate, well, or
  bioformats2raw collection (whose first image is 0.4); everything the
  [Zarr v2 page](../zarr2/README.md) lists for arrays and codecs.
- Images of 2 to 5 axes with scale and translation transformations; labels;
  HCS plates and wells with acquisitions; bioformats2raw layouts with or
  without `series`.

Rejected, because the output would not be valid OME-Zarr 0.5:

- A `version` other than `"0.4"` on a multiscale, plate, well or label, or a
  group that already has an `ome` attribute.
- Multiscales with invalid axes, missing or misordered transformations,
  transformations given by `path`, or levels not ordered from highest to
  lowest resolution.
- Label images with non-integer data, or with fewer levels than their image.
- Plates and wells whose paths, indices or acquisitions do not match.
- Everything the [Zarr v2 profile](../zarr2/README.md#supported-and-not-supported)
  rejects (filters, other compressors, other data types).

Not checked: what 0.4 only recommends (units, names, axis types beyond the
order rule), and the OME-XML. A 0.4 group below a root that is not 0.4 is
not migrated: the Zarr v2 profile keeps its attributes, unchanged, under
`vzip_virtualized.zarr2.attributes`.

## Performance

- **Listing:** S3 returns 1000 keys per request, sequentially. The IDR's
  Embassy S3 is path-style (bucket `idr`).
- **The archive grows with the number of chunks:** one `url` source and one
  entry per chunk, about 140 bytes each here.
- **Plates have many documents.** Every row, well and field is a group with
  its own documents. At revision 16 they were read one at a time, and for
  the IDR's `idr0072B/9512.zarr` (72 wells, 1440 fields) the Python command
  had not finished after 30 minutes, nor the local browser build after 2.
  The documents are now read concurrently, 16 requests at a time by default
  (`--workers N` in Python, `concurrency` in the browser), holding at most
  64 MiB ahead; this plate has not been timed since.
- Reading a chunk is one GET of the same object a 0.4 reader would fetch.

## How it's verified

`web/test/ome-zarr/verify.py` virtualizes the synthetic OME-Zarr 0.4 stores in
[web/test/fixtures/ome-zarr/](../../../web/test/fixtures/ome-zarr/)
(including stores to reject) with the browser code, validates every accepted
input as OME-Zarr 0.4 and every output group as OME-Zarr 0.5 with the
`ome-zarr-models` package, and compares every array with zarr-python's Zarr
v2 reader. `compare.py` checks that the Python and browser outputs are
equivalent on those stores and on the 15 IDR stores of
[corpus_ome_zarr.txt](../../../conformance/virtualize/corpus_ome_zarr.txt)
(images, labels, plates and a bioformats2raw plate; the 102114-object plate
with the Python command only).

- Profile: [profiles/ome-zarr.md](../../../profiles/ome-zarr.md), which
  builds on [profiles/zarr2.md](../../../profiles/zarr2.md)
- Python: [src/vzip/virtualize/ome_zarr/](../../../src/vzip/virtualize/ome_zarr/)
- Browser: [web/src/virtualize/ome-zarr/](../../../web/src/virtualize/ome-zarr/)
- Fixtures: [web/test/fixtures/ome-zarr/](../../../web/test/fixtures/ome-zarr/)
