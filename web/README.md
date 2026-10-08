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
[TIFF](../profiles/tiff.md), [NDPI](../profiles/ndpi.md),
[ND2](../profiles/nd2.md), [DICOM](../profiles/dicom.md),
[NIfTI](../profiles/nifti.md), [IMS](../profiles/ims.md),
[N5](../profiles/n5.md), [Zarr v2](../profiles/zarr2.md) and
[OME-Zarr](../profiles/ome-zarr.md), each implemented in its own directory of
`src/virtualize/` (`store.ts` holds what the store profiles share). The
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

DICOM (Part 10 files):

- Native pixel data in implicit or explicit VR, little or big endian, and
  JPEG Baseline or JPEG 2000 frames, including frames split over several
  fragments, with or without a Basic or Extended Offset Table.
- Multi-frame images along `z`; whole-slide images (TILED_FULL) as one
  pyramid level per file, with frames as tiles.
- Pixel spacing as scale; window centre and width in `omero`.

Not supported, and refused with HTTP 422: other transfer syntaxes (JPEG-LS,
lossless JPEG, RLE, deflate, HTJ2K), palette colour, TILED_SPARSE slides, and
slides with several focal planes or optical paths.

NIfTI (NIfTI-1 and NIfTI-2 single files):

- Up to five dimensions (`t`, `c`, `z`, `y`, `x`), either byte order, all
  integer and float types, and RGB/RGBA voxels.
- One chunk per z-slice, split into row blocks when a slice is over 128 KiB.
- Pixel size and units as scale; an axis-aligned qform or sform as
  translation; intensity scaling recorded beside the OME metadata.

Not supported, and refused with HTTP 422: gzipped files (`.nii.gz`),
header-and-image pairs, complex types, and dimensions 6 and 7 (CIFTI).

Imaris IMS (HDF5):

- Files from Imaris 5.5 to 10 and its converters: every resolution level,
  time point and channel, with extents, units, time step, channel names,
  colours and contrast ranges.
- Chunks as stored, uncompressed or deflate (`zlib`).

Not supported, and refused with HTTP 422: LZ4 or shuffle compression (as
Imaris 10 can write), and HDF5 features Imaris files do not use.

N5, Zarr v2 and OME-Zarr 0.4 stores (a URL ending in `/`, listed with S3 ListObjectsV2):

- Every group and array of the store under that URL, as a Zarr v3 hierarchy
  at the same paths; each chunk is the whole chunk object, one url source per
  chunk, so the archive grows with the number of chunks.
- N5: datasets through the `n5_default` codec (raw, gzip, zlib, zstd and
  blosc blocks, truncated or padded at the edges); COSEM and n5-viewer
  multiscale metadata as OME-NGFF 0.5.
- Zarr v2: C and F order, either separator, numeric and bool types in either
  byte order, zlib, gzip, zstd and blosc; attributes copied unchanged.
- OME-Zarr 0.4 (a Zarr v2 store whose root declares OME-NGFF 0.4): migrated to
  OME-Zarr 0.5 without copying data. The arrays and chunks are the Zarr v2
  profile's; images, labels, plates, wells and bioformats2raw collections are
  checked against OME-NGFF 0.4 and their metadata rewritten as 0.5 (under
  `ome`, with `dimension_names` on every level).

Not supported, and refused with HTTP 422: stores without a listing (a plain
web server), N5 lz4, xz, bzip2 and jpeg blocks, Zarr v2 filters, lz4 and other
compressors, string, object, structured, complex and date-time types, and
OME-Zarr 0.4 stores that break a rule of OME-NGFF 0.4 that 0.5 also has.
A store that lists more than 100000 objects is refused with HTTP 507 (a
limit of this implementation, [VIRTUALIZE.md §12](../VIRTUALIZE.md#12-conformance)).

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
uv run python web/test/dicom/verify.py                       # DICOM fixtures vs pydicom
uv run python web/test/nifti/verify.py                       # NIfTI fixtures vs nibabel
uv run python web/test/ims/verify.py                         # IMS fixtures vs h5py
uv run python conformance/virtualize/compare.py /tmp/vcmp    # browser vs Python virtualizer
node web/demo/e2e.mjs <image file url> <out dir>             # demo + Neuroglancer in Chromium
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
- `dicom/verify.py`, `nifti/verify.py` and `ims/verify.py` do the same for
  their synthetic files, against pydicom, nibabel (unscaled data) and h5py
  (cropped to the image size).
- `compare.py` runs both virtualizers on the synthetic files, every OME-TIFF of
  IDR idr0096 and the public files of every format
  (`conformance/virtualize/corpus_*.txt`), and compares their outputs.
