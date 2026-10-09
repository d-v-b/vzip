# Nikon ND2

vzip turns an image file into an OME-Zarr dataset by writing a small archive
of references to the file's bytes, without copying the pixels
([overview](../README.md)). This page is about Nikon ND2 files.

## Who this is for

Nikon's NIS-Elements writes every acquisition of a Nikon microscope as an
`.nd2` file: time-lapses, z-stacks, multi-position screens and spectral
images, from a few megabytes to tens of gigabytes. They pile up on
acquisition PCs and lab servers, and are deposited as-is in the BioImage
Archive and on Zenodo. The people who have them want to look at a deposited
dataset, or analyze it with zarr-python, without downloading 20 GB and
converting it with Bio-Formats.

vzip reads the file's chunk map, metadata and frame headers (a few batches
of range requests, even for a 4.6 GB time-lapse) and writes an archive in
which every Zarr chunk is one frame of the file, or a block of its rows.
Stage positions become separate images placed where the stage was.

ND2 files are virtualized by vzip's Rust core,
[rust/vzip-ir](../../../rust/vzip-ir/), the same code in Python and (built
for WebAssembly) in the browser: it parses the file into an intermediate
representation (IR), plans the range requests, and projects the IR onto the
ND2 convention. The Python command needs it built first, with
`just ir-build` ([overview](../README.md#two-ways-to-run-it)).

## What you get

A bioformats2raw layout of OME-Zarr 0.5 images: `OME/` lists the series, and
each stage position `p` is an image at `<p>/` with one array, `<p>/0`.

- **Axes:** `t` (when there is a time loop), `c` (when there is more than
  one component), `z` (when there is a z loop), `y`, `x`.
- **Data types:** `uint8`, `uint16` or `float32`.
- **Chunks:** one frame per chunk, all channels together (behind a
  `transpose` codec, since ND2 interleaves them). Frames with padded rows are
  split into row blocks, one range per row, so the padding is skipped.
- **Codecs:** `bytes`, or `bytes` then `zlib` for losslessly compressed
  files.
- **Scale:** the pixel size (with its aspect ratio), the z step and the time
  step, in micrometers and seconds.
- **Translation:** each position's stage coordinates, through the camera's
  stage matrix, when the image is calibrated.
- **omero:** channel names, colors and a display window from the
  significant bits.
- **Metadata:** the root's `vzip_virtualized.nd2` holds the file's signature
  and its decoded metadata chunks (image attributes, experiment, picture
  metadata, text info and the like) that fit the root's budget. Everything
  else is on `vzip_source`, the file's IR mirror
  ([conventions §8](../../../spec/conventions.md#8-the-ir-mirror)): a
  table under `vzip_source/ir/` that accounts for every byte of the file
  (every chunk, the chunk map, the frames and their timestamps, padding), so
  that the file can be rebuilt from it and the file's bytes, and a JSON view
  of it under `vzip_source/tree`.

## Try it

The outputs below were recorded at spec/virtualize.md revision 16
([overview](../README.md#formats)). The summary line now also has the
members `planner` (the read planner's counts of batches, ranges and
requests), `elements` (the size of the file's IR) and `folded` (how the
mirror's table was folded), shown as `…` here: their values were not
measured again.

A five-channel z-stack from BioImage Archive S-BIAD2077 (263 MB), from
[corpus_nd2.txt](../../../conformance/virtualize/corpus_nd2.txt):

```bash
uv run python -m vzip.virtualize https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/077/S-BIAD2077/Files/373_230614_A1_Blk_Reg2_40x.nd2 373_A1.vzip
```

```
{"format": "nd2", "sizes": {"z": 25, "c": 5, "y": 1024, "x": 1024}, "dataType": "uint16", "compressed": false, "paddedRows": false, "rowBlock": 1024, "positions": 1, "frames": 25, "missing": 0, "channels": ["DAPI", "EGFP", "dTomato", "Alx647", "TD"], "planner": {…}, "elements": …, "folded": {…}}
```

This took 3 s and wrote an 8 KB archive at revision 16; the archive now also
holds the file's IR mirror, so it is larger.

```python
import zarr
from vzip import VZipStore

root = zarr.open_group(VZipStore("373_A1.vzip"), mode="r")
print(root["OME"].attrs["ome"]["series"])
img = root["0"]
ms = img.attrs["ome"]["multiscales"][0]
print([a["name"] for a in ms["axes"]], ms["datasets"][0]["coordinateTransformations"])
print([c["label"] for c in img.attrs["ome"]["omero"]["channels"]])
a = img["0"]
print(a.shape, a.dtype, a.chunks)
print(a[:, 12, 512, 512])
```

```
['0']
['c', 'z', 'y', 'x'] [{'type': 'scale', 'scale': [1, 0.65, 0.10347661828209107, 0.10347661828209107]}, {'type': 'translation', 'translation': [0, 0, -344.2956664885127, -6211.658030298999]}]
['DAPI', 'EGFP', 'dTomato', 'Alx647', 'TD']
(5, 25, 1024, 1024) uint16 (5, 1, 1024, 1024)
[ 167  183 2091    0 1574]
```

**In the browser.** The live demo opens the same file and shows it in
Neuroglancer with one composited layer, a checkbox, color and contrast per
channel:
<https://d-v-b.github.io/vzip-demo/image-to-zarr/?url=https%3A%2F%2Fftp.ebi.ac.uk%2Fbiostudies%2Ffire%2FS-BIAD%2F077%2FS-BIAD2077%2FFiles%2F373_230614_A1_Blk_Reg2_40x.nd2>.
It was ready in about 1 s, from 9 range requests (at revision 16, before
the read planner). Every ND2 file of the
corpus is on ftp.ebi.ac.uk or Zenodo, which both allow cross-origin reads.

## Viewing

The Neuroglancer fork (<https://github.com/d-v-b/neuroglancer>, branch `vzip`)
displays ND2 archives: all three data types and both codecs are among those
it reads. The z-stack above renders through the live demo. A multi-position
file is shown as one layer per position, placed at its stage coordinates.

## Supported and not supported

Supported:

- ND2 format version 3 and later.
- Time loops (including multi-phase ones), stage-position loops and z
  loops; spectral loops become channels.
- 8- and 16-bit integer and 32-bit float pixels, with any number of
  components.
- Uncompressed frames, padded or not, and losslessly (zlib) compressed
  frames. Frames missing from the file read as zeros.

Rejected:

- Legacy ND2 files (JPEG 2000 containers from NIS-Elements before version
  3): `not a TIFF, NDPI, ND2, DICOM, NIfTI, IMS, CZI or SAFE zip file`.
- Uncompressed files whose frame headers differ in name length, or whose
  frame data is shorter than the frame.
- More than 65536 stage positions.
- Lossy compression and tiled frames.
- Other loop types, and two loops of the same kind.

## Performance

- Virtualizing reads the chunk map, the metadata chunks and the header of
  every frame. The read planner batches these reads, runs up to 8 requests
  at a time in Python (`--connections`) and packs ranges into multi-range
  requests where the server supports them. (At revision 16, which read only
  the first and last frames' headers of an uncompressed file, this 263 MB
  file took 9 requests, and a 4.6 GB, 525-frame time-lapse 12.)
- A chunk is one contiguous range of the file, so a frame read is one
  request. Padded frames are the exception: one range per row, which the
  vzip readers merge into one request when the padding between rows is
  under their 64 KiB merge gap.

## How it's verified

`js/test/nd2/verify.py` virtualizes 76 synthetic ND2 files (compressed
frames, padded rows, multi-phase time loops, disabled positions, missing
frames, float data, and inputs to reject) with the browser code (the Rust
core as WebAssembly) and compares every chunk with the pixels their
generator wrote; the `nd2` package reads the same files. It also rebuilds
each file from its archive's IR mirror and checks that it is the file, byte
for byte. `design/experiments/verify_nd2_vzip.py` compares archives of public files
with the `nd2` package. `compare.py` compares three virtualizers
([overview](../README.md#the-implementations-agree)) on the synthetic files
and the 17 public files of
[corpus_nd2.txt](../../../conformance/virtualize/corpus_nd2.txt): the Python
and browser hosts of the core must write the same archive, `vzip_source`
included, and both must match, outside `vzip_source`, the frozen reference:
the Python ND2 profile vzip shipped before the core.

- Profile: [spec/virtualize/nd2/profile.md](../../../spec/virtualize/nd2/profile.md); convention:
  [spec/virtualize/nd2.md](../../../spec/virtualize/nd2.md)
- Rust core: [rust/vzip-ir/src/nd2.rs](../../../rust/vzip-ir/src/nd2.rs)
  (the parser) and [rust/vzip-ir/src/project/nd2.rs](../../../rust/vzip-ir/src/project/nd2.rs)
  (the projection)
- Python host: [python/src/vzip/ir/](../../../python/src/vzip/ir/); browser host:
  [js/src/virtualize/ir/](../../../js/src/virtualize/ir/)
- Frozen reference: [conformance/virtualize/reference/vzip_reference/nd2/](../../../conformance/virtualize/reference/vzip_reference/nd2/)
  and [conformance/virtualize/reference/ts/nd2/](../../../conformance/virtualize/reference/ts/nd2/)
- Fixtures: [fixtures/nd2/](../../../fixtures/nd2/)
