# TIFF, OME-TIFF and Aperio SVS

vzip turns an image file into an OME-Zarr dataset by writing a small archive
of references to the file's bytes, without copying the pixels
([overview](../README.md)). This page is about tiled TIFF files.

## Who this is for

Microscopy and pathology labs keep a great deal of their data as tiled TIFF:
OME-TIFF from Bio-Formats, scanners and acquisition software, plain tiled
TIFFs and BigTIFFs from image-processing pipelines, and Aperio SVS slides
from whole-slide scanners. The files sit on a lab server, in the IDR (whose
idr0096 study alone has 205 OME-TIFFs on ftp.ebi.ac.uk), on Zenodo, or on
OpenSlide's test-data server. The people who use them want to open a slide in
an OME-Zarr viewer, or slice it with zarr-python or dask, without running a
conversion that duplicates hundreds of gigabytes.

vzip reads the TIFF's image file directories and, for OME-TIFF, its OME-XML,
and writes an archive in which every Zarr chunk is one tile of the file. A
487 MB, 9-level OME-TIFF from the IDR becomes a 624 KB archive in a few
seconds; the tiles are read from the IDR's server only when a reader asks
for them.

## What you get

One OME-Zarr 0.5 image at the archive root, with one array per pyramid
level, `0` the full resolution.

- **Axes:** a subsequence of `t`, `c`, `z`, `y`, `x`: `t`, `c` and `z` only
  when the image has more than one time point, channel or z-plane.
- **Levels:** OME-TIFF SubIFDs; without OME-XML, the later tiled images of
  the same format that get strictly smaller (how SVS stores its pyramid, so
  its label, macro and thumbnail images are skipped).
- **Chunks:** one TIFF tile per chunk, for each plane and sample plane.
  Interleaved RGB keeps its samples in one chunk, behind a `transpose`
  codec.
- **Codecs:** none, DEFLATE (`zlib`), zstd, JPEG 2000
  (`imagecodecs_jpeg2k`) and JPEG (`imagecodecs_jpeg`), the tiles as stored.
- **Scale:** from the OME-XML physical sizes, else Aperio's `MPP`, else the
  TIFF resolution tags; each level's scale grows with its downsampling.
- **Translation:** the stage position of the first plane (OME-XML), or the
  slide position of an Aperio scan (`Left`, `Top`).
- **Name:** the OME `Image` name.
- **Metadata:** every tag of every IFD, under `vzip_virtualized.tiff`,
  including the OME-XML or the Aperio description (tag 270).

### JPEG-tiled slides (Aperio SVS)

SVS tiles are JPEG streams with their tables missing: the tables are in the
TIFF's `JPEGTables` tag, and the colour transform in a separate tag. vzip
makes each chunk a complete JPEG stream by prepending, through the chunk's
reference, the start-of-image marker, an Adobe colour marker and the tables.
That prefix is the same for every tile of a level (often of every level), so
it is held once in the archive as a `data` source, and reading a tile still
reads only the tile from the file. SVS slides with JPEG 2000 tiles
(compression 33003 and 33005) need nothing of the kind.

## Try it

An OME-TIFF from IDR study idr0096 (487 MB, 9 levels, JPEG 2000 tiles; CC BY
4.0), from the [corpus](../../../conformance/virtualize/corpus_tiff.txt)'s
idr0096 set:

```bash
uv run python -m vzip.virtualize "https://ftp.ebi.ac.uk/pub/databases/IDR/idr0096-tratwal-marrowquant/20210609-ftp-ome-tiffs/4000_d11_m5_LT_2%20(20x_01).ome.tiff" idr0096.vzip
```

```
{"format": "tiff", "axes": ["c", "y", "x"], "levels": [[3, 39916, 32625], [3, 19958, 16312], [3, 9979, 8156], [3, 4989, 4078], [3, 2494, 2039], [3, 1247, 1019], [3, 623, 509], [3, 311, 254], [3, 155, 127]], "references": 5037}
```

This took 5 s and wrote a 624 KB archive. Read it with zarr-python:

```python
import zarr
from vzip import VZipStore

g = zarr.open_group(VZipStore("idr0096.vzip"), mode="r")
ms = g.attrs["ome"]["multiscales"][0]
print(ms["name"], [a["name"] for a in ms["axes"]])
print(ms["datasets"][0]["coordinateTransformations"])
a = g["8"]
print(a.shape, a.dtype, a.chunks)
print(a[:, 70, 60:64])
```

```
20x_01 ['c', 'y', 'x']
[{'type': 'scale', 'scale': [1, 0.34443162790564397, 0.3444387545489271]}, {'type': 'translation', 'translation': [0, -74254.97875886058, -107090.14672281338]}]
(3, 155, 127) uint8 (1, 155, 127)
[[215 217 215 218]
 [215 215 215 216]
 [217 218 215 219]]
```

An Aperio SVS slide with JPEG tiles, OpenSlide's `CMU-1-Small-Region.svs`
(1.9 MB, CC0):

```bash
uv run python -m vzip.virtualize https://openslide.cs.cmu.edu/download/openslide-testdata/Aperio/CMU-1-Small-Region.svs CMU-1-Small-Region.vzip
```

```
{"format": "tiff", "axes": ["c", "y", "x"], "levels": [[3, 2967, 2220]], "references": 130}
```

```python
import zarr
from vzip import VZipStore

g = zarr.open_group(VZipStore("CMU-1-Small-Region.vzip"), mode="r")
print(g.attrs["ome"]["multiscales"][0]["datasets"][0]["coordinateTransformations"])
a = g["0"]
print(a.shape, a.dtype, a.chunks, a.metadata.codecs)
print(a[:, 1000, 1000:1004])
```

```
[{'type': 'scale', 'scale': [1, 0.499, 0.499]}, {'type': 'translation', 'translation': [0, 23449.873, 25691.574]}]
(3, 2967, 2220) uint8 (3, 240, 240) (TransposeCodec(order=(1, 2, 0)), JpegCodec())
[[67 18 38 85]
 [34  5  0 24]
 [65 53 55 83]]
```

These are the values tifffile decodes from the same file. The archive is
20 KB.

**A local file.** A path works too; the archive then refers to the file by
that path, resolved against the archive's own location, so keep the two
together (or pass `--url <url>` with the URL the file will be served from).
For a plain 2048 × 1536 `uint16` TIFF with 512 × 512 DEFLATE tiles:

```bash
uv run python -m vzip.virtualize plain.tif plain.vzip
```

```
{"format": "tiff", "axes": ["y", "x"], "levels": [[1536, 2048]], "references": 12}
```

**In the browser.** The live demo virtualizes the same OME-TIFF and opens it
in Neuroglancer:
<https://d-v-b.github.io/vzip-demo/image-to-zarr/?url=https%3A%2F%2Fftp.ebi.ac.uk%2Fpub%2Fdatabases%2FIDR%2Fidr0096-tratwal-marrowquant%2F20210609-ftp-ome-tiffs%2F4000_d11_m5_LT_2%2520(20x_01).ome.tiff>.
It was ready in about 1 s, from 17 range requests. The OpenSlide SVS files
do not work there: openslide.cs.cmu.edu sends no CORS headers, so the
browser cannot read them ("500: Failed to fetch"). The demo's own SVS example,
a TCGA slide on Zenodo with JPEG 2000 tiles, does work.

## Viewing

The Neuroglancer fork (<https://github.com/d-v-b/neuroglancer>, branch `vzip`)
displays these archives: it decodes `imagecodecs_jpeg` (8-bit),
`imagecodecs_jpeg2k` (8- and 16-bit integers), `zlib` and `zstd`, and the
idr0096 OME-TIFF above renders through the live demo. It has no `int64` or
`float64` data types, so a TIFF with 64-bit integer or float samples cannot
be shown there (zarr-python reads it).

## Supported and not supported

Supported:

- TIFF and BigTIFF, either byte order, with tiled images.
- Pyramids as SubIFDs (OME-TIFF), or as a sequence of smaller tiled images
  (SVS).
- OME-TIFF planes over Z, C and T in any `DimensionOrder`, single-file.
- Unsigned and signed integers of 8 to 64 bits, `float32` and `float64`,
  planar or interleaved samples.
- Compression: none, DEFLATE, zstd, JPEG 2000, and JPEG (8-bit, gray or RGB
  or YCbCr) with or without `JPEGTables`.

Rejected:

- Images in strips rather than tiles.
- LZW, old-style JPEG (compression 6), and any predictor.
- Multi-file OME-TIFF (`TiffData` naming other files).
- An OME-TIFF with several RGB channels (`SizeC` other than 1 or the
  samples per pixel).
- More than 100000 IFDs or planes.

A file that is not TIFF, NDPI, ND2, DICOM, NIfTI or IMS is rejected with
`not a TIFF, NDPI, ND2, DICOM, NIfTI or IMS file` (exit status 3).

## Performance

- Virtualizing reads only the IFDs and the OME-XML: 17 range requests for
  the 487 MB idr0096 file in the browser.
- Reading a chunk is one range request for one tile. For JPEG tiles the
  shared header is in the archive, so it costs no request.
- The archive grows with the number of tiles: CMU-1.svs (24,813 tiles,
  178 MB) gives a 3.4 MB archive, in 10 s.

## How it's verified

`web/test/tiff/verify.py` virtualizes each synthetic file with the browser
code, reads every level back through `src/vzip` and zarr-python, and
compares it with tifffile; `compare.py` checks that the Python and browser
outputs are equivalent on the 62 synthetic files (including inputs to
reject), the 205 OME-TIFFs of idr0096 and the 4 SVS slides of
[corpus_tiff.txt](../../../conformance/virtualize/corpus_tiff.txt).

- Profile: [profiles/tiff.md](../../../profiles/tiff.md)
- Python: [src/vzip/virtualize/tiff/](../../../src/vzip/virtualize/tiff/)
- Browser: [web/src/virtualize/tiff/](../../../web/src/virtualize/tiff/)
- Fixtures: [web/test/fixtures/tiff/](../../../web/test/fixtures/tiff/)
