# Virtualizing image files and stores

vzip can present an image file, or a chunked array store, as an OME-Zarr
dataset without converting or copying its pixels. A **virtualizer** reads
only the input's structure (headers, tile tables, metadata documents) and
writes a small vzip archive ([SPEC.md](../../SPEC.md)): a Zip file whose
metadata entries are OME-Zarr 0.5 (Zarr v3) documents and whose chunk entries
are references to byte ranges of the original file, or to the store's own
chunk objects. A Zarr reader that understands vzip then reads the pixels
straight from where they already are.

Each input format has a **profile**: a specification, written for
implementers, of exactly which archive a virtualizer produces from which
input ([VIRTUALIZE.md](../../VIRTUALIZE.md)). The READMEs below are for users:
who each one is for, what you get, and how to run it.

## Formats

| format | for | README | profile |
|---|---|---|---|
| TIFF | OME-TIFF, plain tiled TIFF, and JPEG-tiled slides such as Aperio SVS | [tiff](tiff/README.md) | [§3](../../profiles/tiff.md) |
| NDPI | Hamamatsu NDPI whole-slide images | [ndpi](ndpi/README.md) | [§4](../../profiles/ndpi.md) |
| ND2 | Nikon NIS-Elements ND2 files | [nd2](nd2/README.md) | [§5](../../profiles/nd2.md) |
| DICOM | DICOM files, including whole-slide images | [dicom](dicom/README.md) | [§6](../../profiles/dicom.md) |
| NIfTI | NIfTI-1 and NIfTI-2 volumes (`.nii`) | [nifti](nifti/README.md) | [§7](../../profiles/nifti.md) |
| IMS | Imaris `.ims` files | [ims](ims/README.md) | [§8](../../profiles/ims.md) |
| N5 | N5 containers on S3 | [n5](n5/README.md) | [§9](../../profiles/n5.md) |
| Zarr v2 | Zarr v2 hierarchies on S3 | [zarr2](zarr2/README.md) | [§10](../../profiles/zarr2.md) |
| OME-Zarr | OME-Zarr 0.4 hierarchies, migrated to OME-Zarr 0.5 in place | [ome-zarr](ome-zarr/README.md) | [§11](../../profiles/ome-zarr.md) |

The first six read one file (a URL, or a local path). The last three read a
store: a URL ending in `/` that answers the S3 `ListObjectsV2` operation
anonymously, such as a public S3 bucket.

## Two ways to run it

**The Python command line.** From a clone of this repository, after
`uv sync`:

```bash
uv run python -m vzip.virtualize <url or path> out.vzip
# or
just virtualize <url or path> out.vzip
```

It prints a one-line JSON summary and exits with status 3 if the profile
rejects the input. The format is chosen by the file's first bytes, or for a
store by the objects at its root. Read the archive in Python with
`vzip.VZipStore` and zarr-python; importing `vzip` registers the codecs that
some formats need (JPEG, JPEG 2000, `zlib`, `n5_default`):

```python
import zarr
from vzip import VZipStore

g = zarr.open_group(VZipStore("out.vzip"), mode="r")
```

**The browser.** The same virtualizers are written in TypeScript
([web/](../../web/README.md)) and run in a service worker, which serves the
result to any Zarr reader on the page's origin as a plain HTTP Zarr store. The
live demo, <https://d-v-b.github.io/vzip-demo/image-to-zarr/>, takes a URL
(`?url=<url>`), shows the levels, opens them in a Neuroglancer fork
(<https://github.com/d-v-b/neuroglancer>, branch `vzip`) and offers the
archive for download. Two limits apply:

- The live demo currently virtualizes TIFF, NDPI and ND2 files only; DICOM,
  NIfTI, IMS and the store formats work in a local build of the demo
  (`(cd web && node build.mjs) && node web/serve.mjs 8080`), which is not yet
  deployed.
- The host must allow cross-origin reads (CORS). zenodo.org, ftp.ebi.ac.uk,
  raw.githubusercontent.com, OpenOrganelle's S3 bucket and the IDR's Embassy
  S3 do; openslide.cs.cmu.edu, downloads.openmicroscopy.org and the NCI
  Imaging Data Commons bucket on storage.googleapis.com do not, so their
  files can be virtualized only with the command line.

The browser code also runs under Node:
`node web/conformance/virtualize.ts <url> out.vzip` writes an archive
equivalent to the Python one.

## The source's metadata, and where it came from: `vzip_virtualized`

A virtual hierarchy looks like any other Zarr, so it says what it is. Each
format has a [Zarr convention](https://github.com/zarr-conventions/zarr-conventions-spec)
of its own, specified in [conventions/](../../conventions/README.md): the
hierarchy's whole Zarr layout, and the translation of the source's header
into JSON. Every output's root records, under the key `vzip_virtualized`,
the format, the convention's version and the source, and each node holds
what the source says about it there too:

- a TIFF's or an NDPI's tags, every IFD's (the OME-XML is the
  ImageDescription tag);
- an ND2 file's metadata chunks (attributes, experiment, picture metadata,
  text info, frame times);
- a DICOM file's attributes, in the DICOM JSON Model;
- a NIfTI header, field by field, and its extensions;
- an Imaris file's HDF5 attributes;
- a store's own attributes, on the node they belong to.

So a reader who needs, say, an ND2 file's objective or a DICOM series'
acquisition parameters finds them in the hierarchy. For the NIfTI example
in [nifti/](nifti/README.md):

```python
v = g.attrs["vzip_virtualized"]
print({k: v[k] for k in ("profile", "version", "source")})
print(list(v["nifti"]), v["nifti"]["header"]["descrip"])
```

```
{'profile': 'nifti', 'version': 1, 'source': {'url': 'https://raw.githubusercontent.com/spm/spm/main/canonical/avg152T1.nii'}}
['nifti_version', 'byte_order', 'header', 'scaling'] NIFTI-1 Image
```

The record stays with the hierarchy if it is copied out of the archive. The
format and version name the exact rules that produced it, so it can be
regenerated from the source and checked; a convention's version increases
only when its layout changes for some source. The record holds no pins: a
store's chunk objects can't be pinned one by one. Each convention has a JSON
Schema next to it, `conventions/<format>/schema.json`.

## Both implementations agree

`conformance/virtualize/compare.py` runs the Python and browser virtualizers
on synthetic files of every profile (including inputs each must reject) and
on the public files and stores listed in
[conformance/virtualize/](../../conformance/virtualize/) (`corpus_*.txt`),
and checks that their outputs are equivalent (VIRTUALIZE.md §1.1). The pixels
are checked separately, per format, against an independent reader; each
README says which.
