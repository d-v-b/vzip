# Imaris IMS

vzip turns an image file into an OME-Zarr dataset by writing a small archive
of references to the file's bytes, without copying the pixels
([overview](../README.md)). This page is about Imaris `.ims` files.

## Who this is for

Imaris files come from Oxford Instruments' Imaris and its file converter,
and directly from Andor's Fusion software (for example on Dragonfly
spinning-disk systems). They hold multi-resolution, multi-channel time-lapses, often
tens or hundreds of gigabytes, on facility storage, and are shared on Zenodo
and the OME sample-image server. A `.ims` file is HDF5, which most
web-based viewers and Zarr tools cannot read without the HDF5 library and a
local copy.

vzip reads the file's HDF5 structure itself (superblock, groups, attributes
and chunk B-trees, never the pixels) and writes an archive in which every
Zarr chunk is one HDF5 chunk of the file. Imaris's own resolution pyramid
becomes the OME-Zarr levels.

## What you get

One OME-Zarr 0.5 image at the archive root, with one array per Imaris
resolution level.

- **Axes:** `t` (more than one time point), `c` (more than one channel),
  `z` (more than one plane, or z-chunks deeper than 1), `y`, `x`.
- **Data types:** `uint8`, `uint16`, `uint32`, `int8`, `int16`, `int32`,
  `float32`.
- **Chunks:** the HDF5 chunks as stored (Imaris pads the dataset to whole
  chunks; chunks wholly in the padding are left out).
- **Codecs:** `bytes`, or `bytes` then `zlib` for deflate-compressed files.
- **Scale and translation:** from the image extents (`ExtMin`, `ExtMax`),
  so each level's voxel size is the extent over its size, and the
  translation is the extents' corner; the unit from `Unit` (micrometres by
  default).
- **Time step:** the mean interval between the first and last time points'
  timestamps.
- **Name and omero:** the image name, and each channel's name, colour and
  contrast range.

## Try it

`retina_large.ims` from the OME sample images (47 MB, 4 levels, 2 channels,
deflate; CC BY 4.0), from
[corpus_ims.txt](../../../conformance/virtualize/corpus_ims.txt):

```bash
uv run python -m vzip.virtualize https://downloads.openmicroscopy.org/images/Imaris-IMS/eliana/retina_large.ims retina.vzip
```

```
{"format": "ims", "levels": 4, "sizes": {"t": 1, "c": 2, "z": 64, "y": 1567, "x": 2048}, "dataType": "uint8", "chunkShape": [16, 256, 256], "compressed": [true, true, true, true], "chunks": 612, "channels": ["CollagenIV (TxRed)", "GFAP (FITC)"]}
```

This took 6 s and wrote an 80 KB archive.

```python
import zarr
from vzip import VZipStore

g = zarr.open_group(VZipStore("retina.vzip"), mode="r")
ms = g.attrs["ome"]["multiscales"][0]
print([(a["name"], a.get("unit")) for a in ms["axes"]])
for d in ms["datasets"]:
    print(d["path"], g[d["path"]].shape, g[d["path"]].chunks, d["coordinateTransformations"])
print([(c["label"], c["color"]) for c in g.attrs["ome"]["omero"]["channels"]])
print(g["3"].metadata.codecs)
print(g["3"][:, 4, 100, 120:126])
```

```
[('c', None), ('z', 'micrometer'), ('y', 'micrometer'), ('x', 'micrometer')]
0 (2, 64, 1567, 2048) (1, 16, 256, 256) [{'type': 'scale', 'scale': [1, 0.19999999999999998, 0.022898532227185703, 0.02290576171875]}, {'type': 'translation', 'translation': [0, -0.1, -0.082, -0.082]}]
1 (2, 64, 783, 1024) (1, 16, 256, 256) [{'type': 'scale', 'scale': [1, 0.19999999999999998, 0.04582630906768838, 0.0458115234375]}, {'type': 'translation', 'translation': [0, -0.1, -0.082, -0.082]}]
2 (2, 64, 391, 512) (1, 16, 256, 256) [{'type': 'scale', 'scale': [1, 0.19999999999999998, 0.091769820971867, 0.091623046875]}, {'type': 'translation', 'translation': [0, -0.1, -0.082, -0.082]}]
3 (2, 32, 195, 256) (1, 16, 256, 256) [{'type': 'scale', 'scale': [1, 0.39999999999999997, 0.1840102564102564, 0.18324609375]}, {'type': 'translation', 'translation': [0, -0.1, -0.082, -0.082]}]
[('CollagenIV (TxRed)', 'FF0000'), ('GFAP (FITC)', '00FF00')]
(BytesCodec(endian=None), ZlibCodec(level=1))
[[ 0  0  0  0  0  0]
 [22 20 18 16 16 16]]
```

h5py reads the same values from `DataSet/ResolutionLevel 3/TimePoint 0/Channel {0,1}/Data`.

**In the browser.** Not on the live demo yet: it virtualizes only TIFF,
NDPI and ND2. downloads.openmicroscopy.org sends no CORS headers, so the OME
sample files work only from the command line. Files on Zenodo do work in a
local build of the demo (see the [overview](../README.md)): it virtualized
`HeLa_H2BmCherry_GFPtubulin_Mitotracker.ims` (Zenodo 4449687, 8 MB, 10 time
points, 3 channels) in about 5 s and showed it in Neuroglancer.

## Viewing

The Neuroglancer fork (<https://github.com/d-v-b/neuroglancer>, branch `vzip`)
reads every data type and codec this profile writes; the HeLa time-lapse
above renders through a local build of the demo, one layer per channel. It
reads the chunks from the file's host, so that host must allow cross-origin
reads.

## Supported and not supported

Supported:

- Files from Imaris 5.5 to 10, ImarisWriter, ImarisFileConverter and Andor
  Fusion: HDF5 superblock versions 0 to 3, compact, dense and symbol-table
  groups, soft links (Imaris 10), chunks indexed by version 1 B-trees, fixed
  arrays or as a single chunk.
- Uncompressed or deflate chunks.

Rejected, with the message the command prints:

- LZ4 compression, alone or after byte shuffling, as Imaris 10 can write:
  `unsupported HDF5 filters [32004]` (for the OME sample
  `croppedRetinaLz4.ims`). No Zarr codec decodes these chunks as stored.
- HDF5 files that are not Imaris images, including Imaris scene files
  without image data: `not an Imaris file (no DataSet group)`.
- Datasets that are compact or contiguous, other chunk indexes, and data
  types other than those above.
- More than 64 resolution levels, or more than 100000 datasets.

## Performance

- Virtualizing reads the HDF5 metadata only (groups, attributes and chunk
  indexes), so its cost grows with the number of datasets and chunks, not
  with the file size: 6 s for this 47 MB file, 11 s for the 8 MB HeLa
  time-lapse on Zenodo.
- Reading a chunk is one range request.
- Imaris chunks are often deep in `z` (16 planes here), so reading one
  `z`-plane decodes 16.

## How it's verified

`web/test/ims/verify.py` virtualizes 28 synthetic IMS files (including
inputs to reject) with the browser code, reads them back through `src/vzip`
and zarr-python, and compares the pixels with h5py (cropped to the image
size). `compare.py` checks that the Python and browser outputs are
equivalent on those files and on the 21 public files of
[corpus_ims.txt](../../../conformance/virtualize/corpus_ims.txt), two of
which must be rejected (the LZ4 file and a scene file).

- Profile: [profiles/ims.md](../../../profiles/ims.md)
- Python: [src/vzip/virtualize/ims/](../../../src/vzip/virtualize/ims/)
- Browser: [web/src/virtualize/ims/](../../../web/src/virtualize/ims/)
- Fixtures: [web/test/fixtures/ims/](../../../web/test/fixtures/ims/)
