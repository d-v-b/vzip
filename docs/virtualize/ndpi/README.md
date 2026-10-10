# Hamamatsu NDPI

vzip turns an image file into an OME-Zarr dataset by writing a small archive
of references to the file's bytes, without copying the pixels
([overview](../README.md)). This page is about Hamamatsu NDPI slides.

## Who this is for

Hamamatsu NanoZoomer scanners write whole-slide images as `.ndpi` files,
often several gigabytes each, onto the scanner's PC or a pathology lab's
file server; public examples are on OpenSlide's test-data server and on
Zenodo (for example QuPath's tutorial slides). Pathologists and image
analysts want to view such a slide in an OME-Zarr viewer, or tile it for
analysis in Python, without converting every slide first.

NDPI is a TIFF variant that most Zarr tooling cannot read as tiles: each
pyramid level is a single JPEG strip, which can be tens of thousands of
pixels wide. vzip cuts each strip into chunks of about 1024 × 1024 pixels at
the JPEG restart markers the scanner wrote, and makes every chunk a complete
JPEG stream out of byte ranges of the file and the strip's shared header. The
198 MB CMU-1.ndpi became a 5.5 MB archive in about 15 s (at spec/virtualize.md
revision 16, before the archive kept every tag on `vzip_source`).

## What you get

One OME-Zarr 0.5 image at the archive root, with one array per level.

- **Axes:** `c`, `y`, `x`; `uint8` RGB.
- **Levels:** every image of the file with a positive magnification, full
  resolution first; the macro image and slide map are left out.
- **Chunks:** with restart offsets (`McuStarts`), a level is cut into chunks
  of whole restart intervals, about 1024 pixels on a side (2048 × 1024 at
  CMU-1's level 0, where one interval is 2048 pixels wide). A level without
  them, usually the smallest, is one chunk.
- **Codecs:** `transpose`, then `imagecodecs_jpeg`. Each chunk is decoded as
  one JPEG stream.
- **Data sources:** the parts of each level's JPEG header (quantization and
  Huffman tables; restart interval and scan header) are stored once in the
  archive as `data` sources, and each chunk's height and width are a
  literal. Reading a chunk reads only its own intervals from the file.
- **Scale:** from the TIFF resolution tags, in micrometers, when they give a
  pixel under 25.4 µm.
- **Translation:** from the slide-center offsets the scanner records.
- **Metadata:** every tag of every IFD (the macro image's and slide map's
  included), under `vzip_source/ifds/<i>`, with the strips of the images
  that are not levels; the root holds none.

## Try it

OpenSlide's CMU-1.ndpi (198 MB, CC0), from
[corpus_tiff.txt](../../../conformance/virtualize/corpus_tiff.txt):

```bash
uv run python -m vzip.virtualize https://openslide.cs.cmu.edu/download/openslide-testdata/Hamamatsu/CMU-1.ndpi CMU-1.vzip
```

```
{"format": "ndpi", "axes": ["c", "y", "x"], "levels": [[3, 38144, 51200], [3, 9536, 12800], [3, 2384, 3200], [3, 596, 800]], "references": 1093, "codec": "imagecodecs_jpeg"}
```

This took 14 s (openslide.cs.cmu.edu answers each request in about half a
second) and wrote a 5.5 MB archive, at revision 16
([overview](../README.md#formats)); the archive now also keeps the file's
tags, so it is larger.

```python
import zarr
from vzip import VZipStore

g = zarr.open_group(VZipStore("CMU-1.vzip"), mode="r")
ms = g.attrs["ome"]["multiscales"][0]
for d in ms["datasets"]:
    a = g[d["path"]]
    print(d["path"], a.shape, a.chunks, d["coordinateTransformations"][0]["scale"])
print(ms["datasets"][0]["coordinateTransformations"][1])
img = g["3"][:]
print(img.shape, img.dtype, img.mean(axis=(1, 2)).round(1))
```

```
0 (3, 38144, 51200) (3, 1024, 2048) [1, 0.4550625711035267, 0.45641259698767683]
1 (3, 9536, 12800) (3, 1024, 1024) [1, 1.8202502844141069, 1.8256503879507073]
2 (3, 2384, 3200) (3, 1024, 1024) [1, 7.281001137656427, 7.302601551802829]
3 (3, 596, 800) (3, 596, 800) [1, 29.12400455062571, 29.210406207211317]
{'type': 'translation', 'translation': [0, -11018.953356086462, -6807.495482884527]}
(3, 596, 800) uint8 [193.7 176.  173. ]
```

**In the browser.** The corpus slides are on openslide.cs.cmu.edu, which
sends no CORS headers, so the browser cannot read them. The live demo does
work for NDPI files on hosts that allow it, such as its own example, a
lymphoma slide from QuPath's tutorial data on Zenodo (221 MB, CC BY 4.0):
<https://d-v-b.github.io/vzip-demo/image-to-zarr/?url=https%3A%2F%2Fzenodo.org%2Fapi%2Frecords%2F18302140%2Ffiles%2Fki67_lymphoma.ndpi%2Fcontent>.
It was ready in about 2 s, with 3 levels.

## Viewing

The Neuroglancer fork (<https://github.com/d-v-b/neuroglancer>, branch `vzip`)
displays NDPI archives: it decodes `imagecodecs_jpeg` for `uint8`, and reads
`data` sources. The Zenodo slide above renders through the live demo. Expect
slow panning at full resolution on hosts without HTTP/2 or multi-range
support (below).

## Supported and not supported

Supported:

- NDPI files of any size, including over 4 GB (64-bit offsets).
- Every level with a positive magnification, each a single JPEG strip, with
  or without restart offsets.

Rejected:

- Slides with several focal planes (two levels of the same
  magnification).
- Levels that are not one baseline JPEG strip of 8-bit YCbCr, or that do not
  get strictly smaller.
- A strip header without exactly one SOF0 segment and a restart interval, or
  restart offsets that do not match the strip.

## Performance

The layout makes reading costly, and nothing in the archive can fix that.
A chunk is about 128 pieces of the file, one per row of JPEG blocks
(MCUs), and consecutive pieces are a whole strip row apart. The
[NDPI access study](https://github.com/d-v-b/vzip/blob/research/ndpi-access/experiments/ndpi_access/README.md)
(branch `research/ndpi-access`) measured what that costs:

- **Over-read or many requests.** Today's readers merge the ranges of a
  chunk when they are less than 64 KiB apart. Where a strip row is smaller
  than that (all of CMU-1), every chunk read fetches the full strip width:
  reading 4 × 4 chunks of CMU-1's level 0 reads 89 MB for 1.4 MB of chunk
  data. Where a row is larger (the 6.9 GB Hamamatsu-1, levels 0–3), a chunk
  costs 128 requests.
- **The floor.** Any reader that fetches only the bytes it needs makes at
  least one request per row of the view (135 to 270 for a 1080-pixel-high
  view). Chunk shape cannot change that; only multi-range requests, HTTP/2,
  or reading the bytes between rows can make those requests cheap.
- **What helps.** Planning all of a view's chunk reads together (a batch
  window), choosing the gap to merge by a cost model of round-trip time and
  bandwidth, and caching the fetched gaps (which the next pan uses). Over 56
  modeled views of CMU-1 at 20 ms and 50 Mbit/s, that takes the total from
  41 s to 14 s. These are prototypes behind flags on that branch, not yet
  the default readers.
- **The header is free.** Since spec/virtualize.md revision 14 the JPEG header
  is a `data` source in the archive, so a chunk read is one request (or one
  merged request) to the file instead of two. Reading all 130 chunks of
  CMU-1's level 1 went from 247 requests to 130.

The hosts matter as much as the reader: openslide.cs.cmu.edu takes about
0.5 s per request at 5–7 Mbit/s and supports neither HTTP/2 nor multi-range
requests.

## How it's verified

`js/test/ndpi/verify.py` virtualizes the 9 synthetic NDPI files (including
inputs to reject) with the browser code, reads every level back through `python/src/vzip`
and zarr-python, and compares the pixels with tifffile. `compare.py` checks
that the Python and browser outputs are equivalent on those files and on the
3 NDPI slides of
[corpus_tiff.txt](../../../conformance/virtualize/corpus_tiff.txt), among
them the 6.9 GB Hamamatsu-1.ndpi.

- Profile: [spec/virtualize/ndpi.md](../../../spec/virtualize/ndpi.md#part-2-the-profile), which builds on
  [spec/virtualize/tiff.md](../../../spec/virtualize/tiff.md#part-2-the-profile)
- Python: [python/src/vzip/virtualize/ndpi/](../../../python/src/vzip/virtualize/ndpi/)
- Browser: [js/src/virtualize/ndpi/](../../../js/src/virtualize/ndpi/)
- Fixtures: [fixtures/ndpi/](../../../fixtures/ndpi/)
