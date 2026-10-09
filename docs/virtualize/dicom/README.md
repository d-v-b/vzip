# DICOM, including whole-slide images

vzip turns an image file into an OME-Zarr dataset by writing a small archive
of references to the file's bytes, without copying the pixels
([overview](../README.md)). This page is about DICOM files.

## Who this is for

DICOM is the format of clinical imaging: CT and MR series from hospital
PACS, and, increasingly, digital pathology, where a whole-slide image is
stored as one DICOM file per pyramid level. Large public collections, such as
the NCI Imaging Data Commons (IDC), serve these files from cloud buckets
(`storage.googleapis.com/idc-open-data/...`). Researchers who want to analyze
a slide or a volume with Zarr tools, or put it next to OME-Zarr data, would
otherwise have to decode and rewrite every frame.

vzip reads the DICOM header and the frame table and writes an archive in which
every Zarr chunk is one frame (or one tile of a slide) of the file, still
JPEG, JPEG 2000 or raw as stored. A 60 MB whole-slide level from the IDC
became a 704 KB archive (at spec/virtualize.md revision 16, before the archive
kept every element of the file).

## What you get

One OME-Zarr 0.5 image at the archive root, with one array, `0`. A DICOM
file is one level: a whole-slide pyramid is one file per level, and each file
gives its own archive.

- **Axes:** `c` for RGB; for a multi-frame image that is not a slide, its
  frames along `t` when the Frame Increment Pointer names Frame Time or
  Frame Time Vector (cine ultrasound, angiography, fluoroscopy), else along
  `z`, whatever they are; `y`, `x`.
- **Whole-slide images** (VL Whole Slide Microscopy, `TILED_FULL`): the
  frames are the tiles of the total pixel matrix, so the array has the full
  slide's shape and one tile per chunk.
- **Data types:** `uint8`, `int8`, `uint16`, `int16`, `uint32`, `int32` (the
  stored cells, not rescaled).
- **Codecs:** `bytes` (either byte order) for native pixel data,
  `imagecodecs_jpeg` for JPEG Baseline, `imagecodecs_jpeg2k` for JPEG 2000;
  `transpose` first for interleaved RGB. JPEG frames get a color marker
  prepended so they decode to RGB.
- **Scale:** Pixel Spacing and Spacing Between Slices, in millimeters
  (including from the shared functional groups of enhanced and slide
  files), and Frame Time, in seconds, for frames along `t`.
- **No translation:** DICOM positions an image in the patient's or the
  slide's frame, which is in general rotated against the image axes.
- **omero:** a gray or R, G, B channel with the stored value range; for
  grayscale images, the Window Center and Width, converted to stored values.
  `MONOCHROME1` is marked `inverted`.
- **Metadata:** every element of the file, private ones included, in the
  DICOM JSON Model under the root's `vzip_virtualized.dicom`; the members
  that would make the root large, and values kept as arrays, are on
  `vzip_source`.

## Try it

A CT slice from pydicom's test data (39 KB), from
[corpus_dicom.txt](../../../conformance/virtualize/corpus_dicom.txt):

```bash
uv run python -m vzip.virtualize https://raw.githubusercontent.com/pydicom/pydicom/v3.0.1/src/pydicom/data/test_files/CT_small.dcm CT_small.vzip
```

```
{"format": "dicom", "axes": ["y", "x"], "shape": [128, 128], "dataType": "int16", "transferSyntax": "1.2.840.10008.1.2.1", "photometric": "MONOCHROME2", "frames": 1, "wholeSlide": false, "references": 1}
```

```python
import zarr
from vzip import VZipStore

g = zarr.open_group(VZipStore("CT_small.vzip"), mode="r")
ome = g.attrs["ome"]
print(ome["multiscales"][0]["axes"], ome["multiscales"][0]["datasets"][0]["coordinateTransformations"])
print(ome["omero"]["channels"])
a = g["0"]
print(a.shape, a.dtype, a.chunks, a[64, 60:66])
```

```
[{'name': 'y', 'type': 'space', 'unit': 'millimeter'}, {'name': 'x', 'type': 'space', 'unit': 'millimeter'}] [{'type': 'scale', 'scale': [0.661468, 0.661468]}]
[{'label': 'gray', 'color': 'FFFFFF', 'active': True, 'window': {'min': -32768, 'max': 32767, 'start': -32768, 'end': 32767}}]
(128, 128) int16 (128, 128) [2189 2191 2122 2023 1928 1864]
```

pydicom's `pixel_array` gives the same values.

A whole-slide level from the IDC (TCGA-THYM, JPEG tiles, 60 MB, 4230
frames):

```bash
uv run python -m vzip.virtualize https://storage.googleapis.com/idc-open-data/76f1a2b6-bb09-4736-aa91-bf89ef57f877/e0e21b65-c170-4d76-85f4-1302414cd758.dcm idc-thym.vzip
```

```
{"format": "dicom", "axes": ["c", "y", "x"], "shape": [3, 10592, 22410], "dataType": "uint8", "transferSyntax": "1.2.840.10008.1.2.4.50", "photometric": "RGB", "frames": 4230, "wholeSlide": true, "references": 4230}
```

This took 5.4 minutes (see Performance) and wrote a 704 KB archive, at
revision 16 ([overview](../README.md#formats)).

```python
import zarr
from vzip import VZipStore

g = zarr.open_group(VZipStore("idc-thym.vzip"), mode="r")
ome = g.attrs["ome"]
print([(a["name"], a.get("unit")) for a in ome["multiscales"][0]["axes"]])
print(ome["multiscales"][0]["datasets"][0]["coordinateTransformations"])
a = g["0"]
print(a.shape, a.dtype, a.chunks, a.metadata.codecs)
tile = a[:, 960:1200, 18240:18480]  # one tile, row 4, column 76
print(tile.mean(axis=(1, 2)).round(1))
```

```
[('c', None), ('y', 'millimeter'), ('x', 'millimeter')]
[{'type': 'scale', 'scale': [1, 0.00101, 0.00101]}]
(3, 10592, 22410) uint8 (3, 240, 240) (TransposeCodec(order=(1, 2, 0)), JpegCodec())
[77.5 58.5 98.1]
```

**In the browser.** Not on the live demo yet: it virtualizes only TIFF,
NDPI and ND2. A local build of the demo (see the [overview](../README.md))
virtualizes `CT_small.dcm` and shows it in Neuroglancer. The IDC's bucket
sends no CORS headers, so its slides cannot be read from a browser at all;
use the command line.

## Viewing

The Neuroglancer fork (<https://github.com/d-v-b/neuroglancer>, branch `vzip`)
reads every data type and codec this profile writes: integers of up to 32
bits, `imagecodecs_jpeg` for `uint8`, and `imagecodecs_jpeg2k` for 8- and
16-bit data. `CT_small.dcm` renders through a local build of the demo. The
default view uses the stored value range, so a CT looks flat until you adjust
the contrast. For IDC slides the obstacle is the host, not the viewer: the
chunks are read from a bucket without CORS.

## Supported and not supported

Supported:

- DICOM Part 10 files in Implicit VR Little Endian, Explicit VR Little or
  Big Endian, JPEG Baseline (`1.2.840.10008.1.2.4.50`) and JPEG 2000
  (`.4.90`, `.4.91`).
- Single- and multi-frame images; `MONOCHROME1`, `MONOCHROME2`, `RGB`, and
  the YBR forms that JPEG and JPEG 2000 decode to RGB.
- Encapsulated frames over several fragments, with or without a Basic or
  Extended Offset Table.
- Whole-slide images organized `TILED_FULL`, one level per file.

Rejected, with the message the command prints:

- Other transfer syntaxes, among them JPEG-LS and RLE:
  `unsupported transfer syntax 1.2.840.10008.1.2.4.80` (JPEG-LS lossless),
  `... 1.2.840.10008.1.2.5` (RLE). Lossless JPEG, deflate and HTJ2K are
  rejected the same way.
- `PALETTE COLOR` and native `YBR_*` images.
- `TILED_SPARSE` slides, and slides with several focal planes or optical
  paths.
- Encapsulated multi-frame files with an empty offset table and neither one
  fragment nor one fragment per frame (vzip does not split fragments by
  reading the JPEG data).

Not applied to the pixels: Rescale Slope and Intercept, modality and VOI
LUTs, palettes and overlays. The values are the stored ones.

## Performance

- **Large encapsulated files without an Extended Offset Table are slow to
  virtualize.** The frame boundaries are found by reading every fragment's
  8-byte item header, one small read per frame. The 4230-frame IDC level
  above took 5.4 minutes from storage.googleapis.com. With an Extended Offset
  Table, or native pixel data, nothing past the header is read.
- Reading a chunk is one range request (one per fragment, merged when they
  are adjacent).
- A pyramid needs one archive per level file; the archives are independent.

## How it's verified

`js/test/dicom/verify.py` virtualizes 68 synthetic DICOM files (including
inputs to reject) with the browser code, reads them back through `python/src/vzip`
and zarr-python, and compares the pixels with pydicom. `compare.py` checks
that the Python and browser outputs are equivalent on those files and on the
17 public files of
[corpus_dicom.txt](../../../conformance/virtualize/corpus_dicom.txt):
pydicom's test files and six whole-slide levels from the IDC, with JPEG,
JPEG 2000 and native tiles.

- Profile: [spec/virtualize/dicom/profile.md](../../../spec/virtualize/dicom/profile.md)
- Python: [python/src/vzip/virtualize/dicom/](../../../python/src/vzip/virtualize/dicom/)
- Browser: [js/src/virtualize/dicom/](../../../js/src/virtualize/dicom/)
- Fixtures: [fixtures/dicom/](../../../fixtures/dicom/)
