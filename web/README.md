# vzip in the browser

A service worker that turns a remote TIFF, Nikon ND2 file or `.vzip` archive
into a plain HTTP Zarr store, without a server and without copying pixel data:

1. A page asks for `<scope>vz/image/<id>/zarr.json`, where `<id>` is the
   base64url encoding of the file's URL.
2. The service worker reads the file's structure with a few range requests
   (7 for a 487 MB, 9-level OME-TIFF; 12 for a 4.6 GB, 525-frame ND2) and
   writes a vzip archive in memory by [VIRTUALIZE.md](../VIRTUALIZE.md): an
   OME-NGFF 0.5 dataset whose chunks are references to the file's TIFF tiles or
   ND2 frames. The format is detected from the file's first bytes
   (`vz/tiff/<id>/` accepts TIFF only).
3. It answers every request under that URL from the archive. Metadata comes
   from the archive; a chunk request becomes one range request to the file.

Any Zarr reader on the same origin can then use the URL; it needs no vzip
support. The archive itself can be downloaded from
`<zarr url>__vz__/archive.vzip`. Existing archives are served the same way
from `<scope>vz/archive/<id>/`.

## Use

```js
import { registerVzipWorker, imageZarrUrl, archiveDownloadUrl } from "./src/client.ts";

const prefix = await registerVzipWorker("/vzip-sw.js"); // waits until the page is controlled
const zarr = imageZarrUrl(prefix, "https://example.org/slide.ome.tiff"); // or an .nd2
// e.g. in Neuroglancer: `${zarr}|zarr3:`
```

Build and run the demo, which also serves the Neuroglancer fork
(https://github.com/d-v-b/neuroglancer, branch `vzip`) at `/neuroglancer/`.
The fork is read from `$NEUROGLANCER`, by default a clone next to this
repository, built with `npm run build`:

```bash
(cd web && npm install && node build.mjs)
node web/serve.mjs 8080
```

Live demo: https://d-v-b.github.io/vzip-demo/image-to-zarr/ (formerly `tiff-to-zarr/`, which now redirects). `web/pages.sh`
publishes it, with the Neuroglancer build, to its own directory of the demo
site (the `gh-pages` branch of `d-v-b/vzip-demo`, which hosts each vzip demo
in a directory and lists them at https://d-v-b.github.io/vzip-demo/).

## What is supported

The rules are [VIRTUALIZE.md](../VIRTUALIZE.md)'s profiles:
[TIFF](../profiles/tiff.md), [NDPI](../profiles/ndpi.md) and
[ND2](../profiles/nd2.md), each implemented in its own directory of
`src/virtualize/`. The
Python reference implementation (`python -m vzip.virtualize`) produces
equivalent archives; `conformance/virtualize/compare.py` checks that.

TIFF:

- Tiled TIFF and BigTIFF, either byte order.
- Pyramids as SubIFDs (OME-TIFF), or, without OME-XML, as later tiled images of
  decreasing size (as in SVS).
- OME-TIFF planes over Z, C and T in any `DimensionOrder`, with physical pixel
  sizes and units.
- Samples per pixel stored planar (one chunk per channel) or interleaved
  (through a `transpose` codec).
- Compression: none, DEFLATE (`zlib`), zstd, JPEG 2000 (`imagecodecs_jpeg2k`),
  and JPEG (`imagecodecs_jpeg`), without a predictor. JPEG tiles that keep
  their tables in `JPEGTables`, as in Aperio SVS, become complete JPEG streams
  through references that prepend the tables and a colour marker.

Not supported, and refused with HTTP 422: images in strips, LZW, old-style
JPEG, predictors, and multi-file OME-TIFF.

Hamamatsu NDPI:

- Pyramids of single JPEG strips, including in files over 4 GB: each level's
  strip is cut at its restart markers into chunks of about 1024 × 1024 pixels, each a JPEG stream rebuilt
  from byte ranges of the file.

Not supported, and refused with HTTP 422: NDPI focal planes.

ND2 (format version 3 and later):

- Time (including multi-phase), stage-position and Z loops; positions become
  a bioformats2raw layout with one image per position, placed at its stage
  coordinates.
- Channels and RGB components, with names, colours and contrast windows in
  `omero`; pixel size, Z step and time step as scales.
- Uncompressed frames (padded rows become one range per row, dropping the
  padding) and losslessly compressed (zlib) frames.

Not supported, and refused with HTTP 422: legacy (JPEG 2000) ND2 files,
lossy compression, tiled frames, and other loop types.

## Limits

- **Same origin only.** A service worker only controls pages from its own
  origin, so the viewer must be served from the same origin as the worker.
- **Codecs are the viewer's job.** The chunks are the TIFF's tiles as they
  are. The Neuroglancer fork decodes `imagecodecs_jpeg2k`; other viewers
  need their own decoder for it.
- **No pins.** Archives are written without pins: cross-origin servers rarely
  expose `ETag`, `Last-Modified` or `Content-Range` to scripts. For the same
  reason, a 206 response with no visible `Content-Range` is accepted when its
  body has the requested length (see `src/http.ts`).
- **Kept in memory.** Archives are rebuilt if the browser stops the worker.
- **No page index.** The writer does not write a page index, and the reader
  holds the whole archive in memory.

## Tests

```bash
node --test web/test/                                        # unit tests
uv run python web/conformance/run_write.py /tmp/vzip-write   # the kit's write cases
uv run python web/test/tiff/verify.py                        # TIFF fixtures vs tifffile
uv run python web/test/ndpi/verify.py                        # NDPI fixtures vs tifffile
uv run python web/test/nd2/verify.py                         # synthetic ND2 fixtures vs their pixels
uv run python conformance/virtualize/compare.py /tmp/vcmp    # browser vs Python virtualizer
node web/demo/e2e.mjs <tiff or nd2 url> <out dir>            # demo + Neuroglancer in Chromium
```

- `run_write.py` checks every unpaged write case with `conformance/validate.py`
  and the reference reader, plus a zip64 archive and every invalid description.
- `tiff/verify.py` virtualizes each fixture with the browser code. It then
  reads every level through the reference implementation (`src/vzip`) and
  zarr-python and compares it with tifffile. `ndpi/verify.py` does the same
  for the NDPI files.
- `nd2/verify.py` does the same for the synthetic ND2 files written by
  `nd2/write_fixtures.py` (compressed frames, padded rows, multi-phase time loops,
  disabled positions, missing frames, float data, and inputs to reject),
  comparing every chunk with the pixels the generator wrote. The `nd2` package
  reads the generator's files too.
- `compare.py` runs both virtualizers on the synthetic files, every OME-TIFF of
  IDR idr0096 and 17 public ND2 files, and compares their outputs.
