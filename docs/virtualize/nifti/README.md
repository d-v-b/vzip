# NIfTI

vzip turns an image file into an OME-Zarr dataset by writing a small archive
of references to the file's bytes, without copying the pixels
([overview](../README.md)). This page is about NIfTI volumes.

## Who this is for

NIfTI (`.nii`) is the working format of neuroimaging: anatomical and
functional MRI, diffusion data, templates and atlases. It comes out of
dcm2niix, SPM, FSL and AFNI, and is shared through OpenNeuro, GitHub
repositories of tools and test data, and lab servers. Someone who wants to
look at a volume in an OME-Zarr viewer next to microscopy data, or read one
slice of a large 4-D series lazily, has to convert it today.

A NIfTI file is a header followed by one block of voxels, so vzip reads the
header (348 or 540 bytes) and writes an archive in which every Zarr chunk is
a run of at most 128 KiB of that block: several z-slices, one slice, or
some rows of a large slice. The SPM `avg152T1.nii` template became a 12 KB
archive in under 4 s (at spec/virtualize.md revision 16).

## What you get

One OME-Zarr 0.5 image at the archive root, with one array, `0`.

- **Axes:** `t` (4-D and up), `c` (5-D, or RGB/RGBA voxels), `z` (3-D and
  up), `y`, `x`. NIfTI's first dimension, which varies fastest, is `x`.
- **Data types:** every NIfTI integer and float type (`uint8` to `uint64`,
  `int8` to `int64`, `float32`, `float64`), and RGB24 or RGBA32 as `uint8`
  with a `c` axis.
- **Chunks:** the voxels as the file stores them, cut into equal runs of at
  most 128 KiB along the outermost axis that allows it: whole volumes or
  blocks of slices when they are small, else blocks of rows of a slice (13
  slices per chunk for the template below).
- **Codecs:** `bytes` in the file's byte order (`transpose` first for
  color voxels and 5-D files). No compression: the file has none.
- **Scale:** from the sform or qform when it is axis-aligned with a positive
  diagonal, else from `pixdim`; units from `xyzt_units` (meters,
  millimeters, micrometers; seconds, milliseconds and microseconds).
- **Translation:** the affine's offset, only when the affine is
  axis-aligned. A rotated or flipped affine, such as the common radiological
  sform with a negative x step, gives no translation.
- **Intensity scaling:** the raw values are stored. A nontrivial
  `scl_slope`/`scl_inter` is recorded in the root's source metadata (the
  `vzip_virtualized` convention) as
  `"nifti": {..., "scaling": {"slope": s, "inter": i}}`; compute
  `s × raw + i`.
- **Header:** every field of the NIfTI header, its extensions and its
  affine, under `vzip_virtualized.nifti` (large extensions as arrays on
  `vzip_source`). With them, every byte of the file but the voxels and
  padding is in the hierarchy.
- **omero:** a display window from `cal_min`/`cal_max`, or R, G, B (A)
  channels for color voxels.

## Try it

SPM's `avg152T1.nii` template (903 KB, GPL-2.0), from
[corpus_nifti.txt](../../../conformance/virtualize/corpus_nifti.txt):

```bash
uv run python -m vzip.virtualize https://raw.githubusercontent.com/spm/spm/main/canonical/avg152T1.nii avg152T1.vzip
```

```
{"format": "nifti", "version": 1, "byteOrder": "little", "sizes": {"z": 91, "y": 109, "x": 91}, "dataType": "uint8", "color": false, "chunkShape": [13, 109, 91], "chunks": 7, "scaling": {"slope": 0.003921568859368563, "inter": 0.0}, "affine": "sform", "translation": false, "extensions": false}
```

(This is the current summary, with the chunking of revision 23; it was
derived from the profile rather than run again. At revision 16 each slice
was a chunk.) The sform is radiological (x runs right to left), so there is
no translation. Read it:

```python
import zarr
from vzip import VZipStore

g = zarr.open_group(VZipStore("avg152T1.vzip"), mode="r")
print(g.attrs["ome"]["multiscales"][0]["axes"])
print(g.attrs["ome"]["multiscales"][0]["datasets"])
print(g.attrs["vzip_virtualized"]["nifti"]["scaling"])
a = g["0"]
print(a.shape, a.dtype, a.chunks)
print(a[45, 50, 40:48])
```

```
[{'name': 'z', 'type': 'space', 'unit': 'millimeter'}, {'name': 'y', 'type': 'space', 'unit': 'millimeter'}, {'name': 'x', 'type': 'space', 'unit': 'millimeter'}]
[{'path': '0', 'coordinateTransformations': [{'type': 'scale', 'scale': [2.0, 2.0, 2.0]}]}]
{'slope': 0.003921568859368563, 'inter': 0.0}
(91, 109, 91) uint8 (13, 109, 91)
[ 93 106 127 148 150 148 147 131]
```

These are nibabel's unscaled values (`img.dataobj.get_unscaled()[40:48, 50, 45]`);
note the reversed axis order.

**In the browser.** Not on the live demo yet: it virtualizes only TIFF,
NDPI and ND2, and answers `422: not a TIFF or ND2 file` for this URL. A local
build of the demo (see the [overview](../README.md)) virtualizes it in about
0.1 s and shows it in Neuroglancer; raw.githubusercontent.com allows
cross-origin reads.

## Viewing

The Neuroglancer fork (<https://github.com/d-v-b/neuroglancer>, branch `vzip`)
displays NIfTI archives of `uint8`, `int8`, `uint16`, `int16`, `uint32`,
`int32`, `uint64` and `float32` data; `avg152T1.nii` renders through a local
build of the demo, with millimeter scales. It has no `int64` or `float64`
data type, so `float64` volumes (which some pipelines write) cannot be shown
there; zarr-python reads them. Neuroglancer shows the raw values: apply the
`nifti` scaling yourself where it matters.

## Supported and not supported

Supported:

- NIfTI-1 and NIfTI-2 single files (`.nii`), either byte order, up to five
  dimensions.
- Extensions after the header are kept in the source metadata; they do not
  change where the voxels start.

Rejected:

- Gzipped files (`.nii.gz`), the usual way NIfTI is distributed: `not a
  TIFF, NDPI, ND2, DICOM, NIfTI, IMS, CZI or SAFE zip file`. A gzip stream has no byte
  ranges to reference; decompress the file first (and host the `.nii`).
- Header-and-image pairs (`.hdr`/`.img`, magic `ni1`, `ni2`).
- Complex, binary and 128-bit types.
- Data along dimensions 6 and 7, such as CIFTI-2 files.
- A `vox_offset` inside the header, or voxel data past the end of the file.

## Performance

- Virtualizing reads the first 552 bytes of the file: one range request,
  after one request for the file's size.
- Reading a chunk is one range request of at most 128 KiB.
- The archive has one entry per chunk: 7 for this template.

## How it's verified

`js/test/nifti/verify.py` virtualizes 88 synthetic NIfTI files (including
inputs to reject) with the browser code, reads them back through `python/src/vzip`
and zarr-python, and compares the values with nibabel's unscaled data.
`compare.py` checks that the Python and browser outputs are equivalent on
those files and on the 12 public files of
[corpus_nifti.txt](../../../conformance/virtualize/corpus_nifti.txt)
(nibabel's and NiiVue's test data, SPM templates and dcm2niix regression
data).

- Profile: [spec/virtualize/nifti.md](../../../spec/virtualize/nifti.md#part-2-the-profile)
- Python: [python/src/vzip/virtualize/nifti/](../../../python/src/vzip/virtualize/nifti/)
- Browser: [js/src/virtualize/nifti/](../../../js/src/virtualize/nifti/)
- Fixtures: [fixtures/nifti/](../../../fixtures/nifti/)
