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
dataset, or analyse it with zarr-python, without downloading 20 GB and
converting it with Bio-Formats.

vzip reads the file's chunk map and metadata (a dozen range requests even
for a 4.6 GB time-lapse) and writes an archive in which every Zarr chunk is
one frame of the file, or a block of its rows. Stage positions become
separate images placed where the stage was.

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
  step, in micrometres and seconds.
- **Translation:** each position's stage coordinates, through the camera's
  stage matrix, when the image is calibrated.
- **omero:** channel names, colours and a display window from the
  significant bits.

## Try it

A five-channel z-stack from BioImage Archive S-BIAD2077 (263 MB), from
[corpus_nd2.txt](../../../conformance/virtualize/corpus_nd2.txt):

```bash
uv run python -m vzip.virtualize https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/077/S-BIAD2077/Files/373_230614_A1_Blk_Reg2_40x.nd2 373_A1.vzip
```

```
{"format": "nd2", "sizes": {"z": 25, "c": 5, "y": 1024, "x": 1024}, "dataType": "uint16", "compressed": false, "paddedRows": false, "rowBlock": 1024, "positions": 1, "frames": 25, "missing": 0, "channels": ["DAPI", "EGFP", "dTomato", "Alx647", "TD"]}
```

This took 3 s and wrote an 8 KB archive.

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
Neuroglancer with one composited layer, a checkbox, colour and contrast per
channel:
<https://d-v-b.github.io/vzip-demo/image-to-zarr/?url=https%3A%2F%2Fftp.ebi.ac.uk%2Fbiostudies%2Ffire%2FS-BIAD%2F077%2FS-BIAD2077%2FFiles%2F373_230614_A1_Blk_Reg2_40x.nd2>.
It was ready in about 1 s, from 9 range requests. Every ND2 file of the
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
  3): `not a TIFF, NDPI, ND2, DICOM, NIfTI or IMS file`.
- Lossy compression and tiled frames.
- Other loop types, and two loops of the same kind.

## Performance

- Virtualizing reads the chunk map, the metadata chunks and the headers of
  the first and last frames (all frames' headers for compressed files): 9
  requests for this 263 MB file, 12 for a 4.6 GB, 525-frame time-lapse.
- A chunk is one contiguous range of the file, so a frame read is one
  request. Padded frames are the exception: one range per row, which the
  vzip readers merge into one request when the padding between rows is
  under their 64 KiB merge gap.

## How it's verified

`web/test/nd2/verify.py` virtualizes 53 synthetic ND2 files (compressed
frames, padded rows, multi-phase time loops, disabled positions, missing
frames, float data, and inputs to reject) with the browser code and compares
every chunk with the pixels their generator wrote; the `nd2` package reads
the same files. `experiments/verify_nd2_vzip.py` compares archives of public
files with the `nd2` package. `compare.py` checks that the Python and
browser outputs are equivalent on the synthetic files and the 17 public files
of [corpus_nd2.txt](../../../conformance/virtualize/corpus_nd2.txt).

- Profile: [profiles/nd2.md](../../../profiles/nd2.md)
- Python: [src/vzip/virtualize/nd2/](../../../src/vzip/virtualize/nd2/)
- Browser: [web/src/virtualize/nd2/](../../../web/src/virtualize/nd2/)
- Fixtures: [web/test/fixtures/nd2/](../../../web/test/fixtures/nd2/)
