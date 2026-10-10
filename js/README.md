# vzip in the browser

A service worker that turns a remote image file (TIFF, NDPI, ND2, CZI,
DICOM, NIfTI or IMS), a Sentinel-2 SAFE product (a `.SAFE.zip` file, or a
`.SAFE` directory), an N5, Zarr v2 or OME-Zarr 0.4 store (a URL ending in `/`),
or a `.vzip` archive into a plain HTTP Zarr store, without a server and
without copying pixel data:

1. A page asks for `<scope>vz/image/<id>/zarr.json`, where `<id>` is the
   base64url encoding of the file's or store's URL.
2. The service worker reads the file's structure with a few range requests
   (for TIFF, ND2 and CZI, batched by the Rust core's read planner) and
   writes a vzip archive in memory by [spec/virtualize.md](../spec/virtualize.md): an
   OME-NGFF 0.5 dataset (or, for a store, its Zarr v3 hierarchy; for SAFE,
   a GeoZarr hierarchy) whose chunks are references to the source's tiles,
   frames, strips, subblocks or chunk objects. The
   format is detected from the file's first bytes, or from the objects at a
   store's root (`vz/tiff/<id>/` accepts TIFF only).
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
repository, built with `npm run build`. The build needs the Rust core's
wasm32 module, so a Rust toolchain with the `wasm32-unknown-unknown` target
(`just js::build` builds it first; `node build.mjs` alone stops if it is
missing):

```bash
(cd js && npm install)
just js::build
node js/demo/serve.mjs 8080
```

The demo opens images in Neuroglancer, and Sentinel-2 SAFE products on a map
(`map.html`: OpenLayers' GeoZarr source, with zarrita's registry given an
`imagecodecs_jpeg2k` codec that decodes with the fork's JPEG 2000 wasm
module, which the build copies from `$NEUROGLANCER`).

Live demo: https://d-v-b.github.io/vzip-demo/image-to-zarr/ (formerly `tiff-to-zarr/`, which now redirects). `js/demo/pages.sh`
publishes it, with the Neuroglancer build, to its own directory of the demo
site (the `gh-pages` branch of `d-v-b/vzip-demo`, which hosts each vzip demo
in a directory and lists them at https://d-v-b.github.io/vzip-demo/).

## What is supported

The rules are [spec/virtualize.md](../spec/virtualize.md)'s profiles:
[TIFF](../spec/virtualize/tiff.md#part-2-the-profile), [NDPI](../spec/virtualize/ndpi.md#part-2-the-profile),
[ND2](../spec/virtualize/nd2.md#part-2-the-profile), [DICOM](../spec/virtualize/dicom.md#part-2-the-profile),
[NIfTI](../spec/virtualize/nifti.md#part-2-the-profile), [IMS](../spec/virtualize/ims.md#part-2-the-profile),
[N5](../spec/virtualize/n5.md#part-2-the-profile), [Zarr v2](../spec/virtualize/zarr2.md#part-2-the-profile),
[OME-Zarr](../spec/virtualize/ome-zarr.md#part-2-the-profile), [SAFE](../spec/virtualize/safe.md#part-2-the-profile) and
[CZI](../spec/virtualize/czi.md#part-2-the-profile). TIFF, ND2 and CZI are virtualized by the
Rust core (`rust/vzip-ir`) built for wasm32: `src/virtualize/ir/` performs the
requests its read planner asks for (several at once, multi-range ones under
Node), under the reader policy, and writes its output. `just js::wasm` builds
the module, which `just js::build` copies to `dist/vzip_ir.wasm` (beside the
service worker, which fetches it) and which the Node tests and CLIs read from
cargo's target directory (or `$VZIP_IR_WASM`). The other profiles are each
implemented in their own directory of `src/virtualize/` (`store.ts` holds what
the store profiles share). The TypeScript TIFF, ND2 and CZI virtualizers the
core replaced are kept, frozen, in `conformance/reference/` (with CLIs
`conformance/reference/virtualize.ts` and `virtualize_file.ts`), for
comparisons. The Python implementation (`python -m vzip.virtualize`)
produces equivalent archives; `conformance/virtualize/compare.py` checks that.

A file at an http(s) URL is read under the reader policy (spec/archive.md §8.7):
loopback, private, link-local and other special hosts are refused unless
the caller opts in (`allowPrivateHosts`; `--allow-private-hosts` in the Node
CLIs). Each source pins its size, and the input file's its ETag when every
response exposed the same strong one.

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
  through references that prepend the tables and a color marker, held once
  in the archive as a `data` source.

Not supported, and refused with HTTP 422: images in strips, LZW, old-style
JPEG, predictors, and multi-file OME-TIFF.

Hamamatsu NDPI:

- Pyramids of single JPEG strips, including in files over 4 GB: each level's
  strip is cut at its restart markers into chunks of about 1024 × 1024 pixels, each a JPEG stream rebuilt
  from byte ranges of the file and the strip's header, held once in the
  archive as `data` sources.

Not supported, and refused with HTTP 422: NDPI focal planes.

ND2 (format version 3 and later):

- Time (including multi-phase), stage-position and Z loops; positions become
  a bioformats2raw layout with one image per position, placed at its stage
  coordinates.
- Channels and RGB components, with names, colors and contrast windows in
  `omero`; pixel size, Z step and time step as scales.
- Uncompressed frames (padded rows become one range per row, dropping the
  padding) and losslessly compressed (zlib) frames.

Not supported, and refused with HTTP 422: legacy (JPEG 2000) ND2 files,
lossy compression, tiled frames, and other loop types.

Zeiss CZI (file version 1, one file):

- Every series (scene, block, ...) as an image of a bioformats2raw layout,
  its pyramid levels found from the subblocks; subblocks in no level kept as
  tile arrays under `tiles/`.
- Subblocks uncompressed, JPEG, JPEG XR or zstd (`imagecodecs_jpegxr`,
  `numcodecs.shuffle` and `zstd` codecs as needed), of every ZISRAW pixel
  type: gray, BGR and BGRA; integer, float and complex.

Not supported, and refused with HTTP 422: LZW, lossless JPEG and camera raw
subblocks, and CZI files split over several files.

Sentinel-2 SAFE (Level-1C and Level-2A, a `.SAFE.zip` file or a `.SAFE`
directory listed with S3 ListObjectsV2):

- One group per resolution (`r10m`, `r20m`, `r60m`) and one array per band,
  one chunk per JPEG 2000 tile, with the zarr-conventions `proj` and
  `spatial` metadata; the radiometric offsets and quantification values on
  each band, and every file of the product kept on `vzip_source`.

DICOM (Part 10 files):

- Native pixel data in implicit or explicit VR, little or big endian, and
  JPEG Baseline or JPEG 2000 frames, including frames split over several
  fragments, with or without a Basic or Extended Offset Table.
- Multi-frame images along `z`; whole-slide images (TILED_FULL) as one
  pyramid level per file, with frames as tiles.
- Pixel spacing as scale; window center and width in `omero`.

Not supported, and refused with HTTP 422: other transfer syntaxes (JPEG-LS,
lossless JPEG, RLE, deflate, HTJ2K), palette color, TILED_SPARSE slides, and
slides with several focal planes or optical paths.

NIfTI (NIfTI-1 and NIfTI-2 single files):

- Up to five dimensions (`t`, `c`, `z`, `y`, `x`), either byte order, all
  integer and float types, and RGB/RGBA voxels.
- One chunk per z-slice, split into row blocks when a slice is over 128 KiB.
- Pixel size and units as scale; an axis-aligned qform or sform as
  translation; the whole header, and its intensity scaling, recorded under
  `vzip_virtualized`.

Not supported, and refused with HTTP 422: gzipped files (`.nii.gz`),
header-and-image pairs, complex types, and dimensions 6 and 7 (CIFTI).

Imaris IMS (HDF5):

- Files from Imaris 5.5 to 10 and its converters: every resolution level,
  time point and channel, with extents, units, time step, channel names,
  colors and contrast ranges.
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
limit of this implementation, [spec/virtualize.md §14](../spec/virtualize.md#14-conformance)).

## Limits

- **Same origin only.** A service worker only controls pages from its own
  origin, so the viewer must be served from the same origin as the worker.
- **Codecs are the viewer's job.** The chunks are the TIFF's tiles as they
  are. The Neuroglancer fork decodes `imagecodecs_jpeg2k`; other viewers
  need their own decoder for it.
- **Few ETag pins.** Every source pins its size, but an ETag only when the
  server exposes it to scripts, which cross-origin servers rarely do (nor
  `Last-Modified` or `Content-Range`). For the same reason, a 206 response
  with no visible `Content-Range` is accepted when its body has the
  requested length (see `src/http.ts`).
- **Kept in memory.** Archives are rebuilt if the browser stops the worker.
- **No page index.** The writer does not write a page index, and the reader
  holds the whole archive in memory.

## Tests

```bash
node --test js/test/                                        # unit tests
uv run python js/conformance/run_write.py /tmp/vzip-write   # the kit's write cases
uv run python js/test/tiff/verify.py                        # TIFF fixtures vs tifffile
uv run python js/test/ndpi/verify.py                        # NDPI fixtures vs tifffile
uv run python js/test/nd2/verify.py                         # synthetic ND2 fixtures vs their pixels
uv run python js/test/dicom/verify.py                       # DICOM fixtures vs pydicom
uv run python js/test/nifti/verify.py                       # NIfTI fixtures vs nibabel
uv run python js/test/ims/verify.py                         # IMS fixtures vs h5py
uv run python js/test/czi/verify.py                         # CZI fixtures vs czifile (and libCZI)
uv run python js/test/safe/verify.py                        # SAFE fixtures vs GDAL
uv run python conformance/virtualize/compare.py /tmp/vcmp    # reference vs Python vs browser virtualizer
node js/demo/e2e.mjs <image file url> <out dir>             # demo + Neuroglancer in Chromium
node js/demo/e2e_examples.mjs <out dir> [example id …]      # every demo example, with its viewer
```

- `run_write.py` checks every unpaged write case with `conformance/archive/validate.py`
  and the reference reader, plus a zip64 archive and every invalid description.
- `tiff/verify.py` virtualizes each fixture with the browser code. It then
  reads every level through the reference implementation (`python/src/vzip`) and
  zarr-python and compares it with tifffile, and rebuilds the file from the
  archive's IR mirror, byte for byte. `ndpi/verify.py` does the same for the
  NDPI files (which have no IR mirror).
- `nd2/verify.py` does the same for the synthetic ND2 files written by
  `nd2/write_fixtures.py` (compressed frames, padded rows, multi-phase time loops,
  disabled positions, missing frames, float data, and inputs to reject),
  comparing every chunk with the pixels the generator wrote. The `nd2` package
  reads the generator's files too.
- `dicom/verify.py`, `nifti/verify.py` and `ims/verify.py` do the same for
  their synthetic files, against pydicom, nibabel (unscaled data) and h5py
  (cropped to the image size).
- `compare.py` runs three virtualizers (`ref`, the frozen reference for
  TIFF, ND2 and CZI and the shipped Python code for the rest; `py`, the
  Python command; `web`, this code under Node) on the synthetic files, every
  OME-TIFF of IDR idr0096 and the public files of every format
  (`conformance/virtualize/corpus_*.txt`), and compares their outputs
  ([HARNESS.md](../conformance/virtualize/HARNESS.md)).
- The verify scripts and `compare.py` serve fixtures from 127.0.0.1, so they
  run the virtualizers with private hosts allowed.
