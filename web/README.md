# vzip in the browser

A service worker that turns a remote TIFF (or a `.vzip` archive) into a plain
HTTP Zarr store, without a server and without copying pixel data:

1. A page asks for `<scope>vz/tiff/<id>/zarr.json`, where `<id>` is the
   base64url encoding of a TIFF's URL.
2. The service worker reads the TIFF's directories with a few range requests
   (7 for a 487 MB, 9-level OME-TIFF) and writes a vzip archive in memory: an
   OME-NGFF 0.5 multiscale image whose chunks are references to the TIFF's
   tiles.
3. It answers every request under that URL from the archive. Metadata comes
   from the archive; a chunk request becomes one range request to the TIFF.

Any Zarr reader on the same origin can then use the URL; it needs no vzip
support. The archive itself can be downloaded from
`<zarr url>__vz__/archive.vzip`. Existing archives are served the same way
from `<scope>vz/archive/<id>/`.

## Use

```js
import { registerVzipWorker, tiffZarrUrl, archiveDownloadUrl } from "./src/client.ts";

const prefix = await registerVzipWorker("/vzip-sw.js"); // waits until the page is controlled
const zarr = tiffZarrUrl(prefix, "https://example.org/slide.ome.tiff");
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

Live demo: https://d-v-b.github.io/vzip-demo/tiff-to-zarr/. `web/pages.sh`
publishes it, with the Neuroglancer build, to its own directory of the demo
site (the `gh-pages` branch of `d-v-b/vzip-demo`, which hosts each vzip demo
in a directory and lists them at https://d-v-b.github.io/vzip-demo/).

## What is supported

- Tiled TIFF and BigTIFF, either byte order.
- Pyramids as SubIFDs (OME-TIFF), or, without OME-XML, as later tiled images of
  decreasing size (as in SVS).
- OME-TIFF planes over Z, C and T in any `DimensionOrder`, with physical pixel
  sizes and units.
- Samples per pixel stored planar (one chunk per channel) or interleaved
  (through a `transpose` codec).
- Compression: none, DEFLATE (`zlib`), zstd, JPEG 2000 (`imagecodecs_jpeg2k`),
  without a predictor.

Not supported, and refused with HTTP 422: images in strips, LZW, JPEG,
predictors, and multi-file OME-TIFF.

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
uv run python web/test/verify_tiff.py                        # TIFF fixtures vs tifffile
node web/demo/e2e.mjs <tiff url> <out dir>                   # demo + Neuroglancer in Chromium
```

- `run_write.py` checks every unpaged write case with `conformance/validate.py`
  and the reference reader, plus a zip64 archive and every invalid description.
- `verify_tiff.py` virtualizes each fixture with the browser code. It then
  reads every level through the reference implementation (`src/vzip`) and zarr-python and compares it with
  tifffile.
