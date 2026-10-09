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
| SAFE | Sentinel-2 Level-1C and Level-2A products, as a `.SAFE` directory or a `.SAFE.zip` | [convention](../../conventions/safe/README.md) | [§12](../../profiles/safe.md) |
| CZI | Zeiss CZI files | [convention](../../conventions/czi/README.md) | [§13](../../profiles/czi.md) |

TIFF, NDPI, ND2, DICOM, NIfTI, IMS and CZI read one file (a URL, or a local
path). N5, Zarr v2 and OME-Zarr read a store: a URL ending in `/` that
answers the S3 `ListObjectsV2` operation anonymously, such as a public S3
bucket. SAFE reads either: a product's `.SAFE` directory as a store, or its
`.SAFE.zip` as a file.

SAFE and CZI have no user-story page yet; their conventions describe the
output. A Sentinel-2 product becomes a GeoZarr hierarchy (one group per
resolution, one array per band, one chunk per JPEG 2000 tile, with the
zarr-conventions `proj` and `spatial` metadata) rather than OME-Zarr, and
keeps every file of the product. A CZI file becomes a bioformats2raw layout
of OME-Zarr 0.5 images, one per series (scene, block, ...), whose chunks are
the file's subblocks (uncompressed, JPEG, JPEG XR or zstd); subblocks that
are in no pyramid level of an image are kept as tile arrays under `tiles/`.
Their public test inputs are listed in [corpus_safe.txt](../../conformance/virtualize/corpus_safe.txt) and
[corpus_czi.txt](../../conformance/virtualize/corpus_czi.txt).

**Numbers in these pages.** The outputs, timings, request counts and archive
sizes quoted in the format pages were measured at VIRTUALIZE.md revision 16.
The current revision is 23. Since revision 16, the archives also keep the
source's whole metadata (below), so they are larger than the sizes quoted.
TIFF and ND2 are now read through a read planner, so their request counts
differ too. Where a summary line or an attribute path has changed since
then, the pages show its current form, and say so where a value could not
be measured again.

## Two ways to run it

**The Python command line.** From a clone of this repository, after
`uv sync` and `just ir-build`:

```bash
uv run python -m vzip.virtualize <url or path> out.vzip
# or
just virtualize <url or path> out.vzip
```

TIFF (and SVS), ND2 and CZI are virtualized by vzip's Rust core,
[rust/vzip-ir](../../rust/vzip-ir/), which `just ir-build` builds into the
project's environment as the Python module `vzip_ir` (it needs a Rust
toolchain). The core parses the file into an intermediate representation (IR),
plans the range requests, projects the IR onto the format's convention, and
writes the source's IR mirror ([below](#the-sources-metadata-and-where-it-came-from-vzip_virtualized)).
The other formats are implemented in Python and do not need it.

The command prints a one-line JSON summary. It exits with status 3 if the
profile rejects the input, and with status 1 if the reader policy refuses the
source (below). The format is chosen by the file's first bytes, or for a
store by the objects at its root. Options:

- `--url <url>`: the URL to record as the source, for a local file or
  directory that will be served from that URL (a local directory needs it);
- `--checksums`: also record the CRC-32C of every referenced range
  ([SPEC.md §5.2](../../SPEC.md#52-range)), which reads every byte the
  archive references (off by default);
- `--allow-private-hosts`: let a file at an http(s) URL be read from a
  loopback, private or link-local host (below; unsafe);
- `--connections N` and `--byte-cost SECONDS_PER_MB`: for TIFF, ND2 and CZI,
  how many requests the read planner runs at a time (default 8), and how
  much wall time it charges for a megabyte read beyond those asked (default
  0.1 s);
- `--workers N`: how many of a store's metadata documents are read at a time
  (default 16).

Every `url` source records the size of the object it names. The input
file's source also records its ETag when every response gave the same
strong one ([SPEC.md §6.1](../../SPEC.md#61-pins)). A reader then notices a
source that has changed since the archive was written.

**Reader policy.** A file at an http(s) URL is read under the reader policy
of [SPEC.md §8.7](../../SPEC.md#87-reader-policy). A host that is, or
resolves to, a loopback, private, link-local or other special address is
refused, and so is a request that would go through a proxy the reader cannot
check. To virtualize a file served from your own machine or network (for
example `http://127.0.0.1:8000/...`), pass `--allow-private-hosts`. Reading
an archive applies the same policy to its sources, so an archive that refers
to such a host opens only with `VZipStore(path,
policy=Policy(allow_private_hosts=True))` (`from vzip.policy import
Policy`). A local file or directory needs neither.

Read the archive in Python with `vzip.VZipStore` and zarr-python. Importing
`vzip` registers the codecs that some formats need (JPEG, JPEG 2000,
JPEG XR, `zlib`, `n5_default`):

```python
import zarr
from vzip import VZipStore

g = zarr.open_group(VZipStore("out.vzip"), mode="r")
```

**The browser.** The same virtualizers run in a service worker
([web/](../../web/README.md)): the Rust core, built for WebAssembly, for
TIFF, ND2 and CZI, and TypeScript implementations of the other formats. The
service worker serves the
result to any Zarr reader on the page's origin as a plain HTTP Zarr store. The
live demo, <https://d-v-b.github.io/vzip-demo/image-to-zarr/>, takes a URL
(`?url=<url>`), shows the levels, opens them in a Neuroglancer fork
(<https://github.com/d-v-b/neuroglancer>, branch `vzip`) and offers the
archive for download. Two limits apply:

- The live demo currently virtualizes TIFF, NDPI and ND2 files only. CZI,
  DICOM, NIfTI, IMS, SAFE and the store formats work in a local build of the
  demo (`just web::build && node web/serve.mjs 8080`; the build compiles the
  Rust core to WebAssembly first, `dist/vzip_ir.wasm`, so it needs a Rust
  toolchain with the `wasm32` target), which is not yet deployed.
- The host must allow cross-origin reads (CORS). zenodo.org, ftp.ebi.ac.uk,
  raw.githubusercontent.com, OpenOrganelle's S3 bucket and the IDR's Embassy
  S3 do; openslide.cs.cmu.edu, downloads.openmicroscopy.org and the NCI
  Imaging Data Commons bucket on storage.googleapis.com do not, so their
  files can be virtualized only with the command line.

The browser code also runs under Node:
`node web/conformance/virtualize.ts <url> out.vzip [--checksums]
[--allow-private-hosts]` writes an archive equivalent to the Python one. For
TIFF, ND2 and CZI it runs the same Rust core as Python, built for wasm32
(`just web::wasm`).

**Store reads.** Listing a store is sequential (S3 returns 1000 keys per
request). Its metadata documents (`attributes.json`, `.zgroup`, `.zarray`,
`.zattrs`) are then read concurrently, 16 requests at a time by default
(`--workers N` in Python, `concurrency` in the browser's `openHttpStore`),
holding at most 64 MiB of documents ahead of the virtualizer.

## The source's metadata, and where it came from: `vzip_virtualized`

A virtual hierarchy looks like any other Zarr, so it says what it is. Each
format has a [Zarr convention](https://github.com/zarr-conventions/zarr-conventions-spec)
of its own, specified in [conventions/](../../conventions/README.md): the
hierarchy's whole Zarr layout, and the translation of the source's header
into JSON. Every output's root records, under the key `vzip_virtualized`,
the format, the convention's version, the revision of VIRTUALIZE.md that
produced it, and the source. The nodes also hold what the source says about
them, under the same key. Metadata that is large, or that grows with the
data, is on the group `vzip_source`, a child of the root that OME-NGFF
readers ignore and whose documents are fetched only when it is opened. Where
each format's metadata is:

- **TIFF (and SVS), ND2 and CZI:** `vzip_source` is the source's **IR
  mirror** ([conventions §8](../../conventions/README.md#8-the-ir-mirror)).
  It is a table under `vzip_source/ir/` that accounts for every byte of the
  file, so that, with the source's bytes, the file is rebuilt byte for byte
  with no knowledge of the format, and a JSON view of it under
  `vzip_source/tree`: every TIFF tag of every IFD, every ND2 chunk, every CZI
  segment. Values of more than 1024 bytes, such as a long OME-XML, are not
  copied into the view; it points to them in the source. The root keeps a
  little: a TIFF's byte order and BigTIFF flag, an ND2 file's decoded
  metadata chunks (attributes, experiment, picture metadata, text info) that
  fit its budget, and a CZI file's header.
- **NDPI:** every tag of every IFD, under `vzip_source/ifds/<i>`.
- **DICOM:** the file's attributes, in the DICOM JSON Model, on the root;
  the ones that would make the root large are on `vzip_source`.
- **NIfTI:** the header, field by field, its extensions and the affine, on
  the root (large extensions as arrays on `vzip_source`).
- **IMS:** the whole HDF5 file, mirrored as a Zarr hierarchy under
  `vzip_source/hdf5` (groups, datasets, attributes, links).
- **N5, Zarr v2 and OME-Zarr:** each node's own attributes and the layout
  members its `zarr.json` does not reproduce, on the node, as
  `{"attributes": ..., "metadata": ...}` (with `"unversioned"` for an
  OME-Zarr group); every other object of the store, kept whole under
  `vzip_source/objects/`.
- **SAFE:** the product's identity on the root, each band's radiometric
  values on its array, and every file of the product kept on `vzip_source`.

So a reader who needs, say, an ND2 file's objective or a DICOM series'
acquisition parameters finds them in the hierarchy. For the NIfTI example
in [nifti/](nifti/README.md):

```python
v = g.attrs["vzip_virtualized"]
print({k: v[k] for k in ("profile", "version", "revision", "source")})
print(v["nifti"]["header"]["descrip"], v["nifti"]["scaling"])
```

```
{'profile': 'nifti', 'version': 0, 'revision': 23, 'source': {'url': 'https://raw.githubusercontent.com/spm/spm/main/canonical/avg152T1.nii'}}
NIFTI-1 Image {'slope': 0.003921568859368563, 'inter': 0.0}
```

The record stays with the hierarchy if it is copied out of the archive. The
format, version and revision name the exact rules that produced it, so it
can be regenerated from the source and checked. Until vzip's first release,
every convention is at version 0 and promises nothing: any revision may
change a layout, and the recorded revision tells two layouts apart. From the
release, conventions start at version 1, a convention's version increases
only when its layout changes for some source, and hierarchies no longer
record a revision ([conventions §1](../../conventions/README.md#1-conventions)).
The record names the source by its URL only; the archive's source table
pins the sources (above). Each convention has a JSON Schema next to it,
`conventions/<format>/schema.json`.

## The implementations agree

`conformance/virtualize/compare.py` runs three virtualizers on synthetic
files of every profile (including inputs each must reject) and on the public
files and stores listed in
[conformance/virtualize/](../../conformance/virtualize/) (`corpus_*.txt`),
and checks that their outputs are equivalent (VIRTUALIZE.md §1.1):

- `ref`, the reference (`conformance/virtualize/reference/cli.py`): for
  TIFF, ND2 and CZI, the Python profiles vzip shipped before these formats
  moved to the Rust core, frozen; for every other format, the shipped code;
- `py`, `python -m vzip.virtualize`;
- `web`, the browser code under Node.

For TIFF, ND2 and CZI, `py` and `web` run one core, so the independent check
is `ref`, compared with both everywhere but `vzip_source`. `py` and `web` are
compared entirely, the mirror included, which shows that the two hosts drive
the core alike. The mirror has no second implementation. Instead,
`compare.py` checks every mirror on its own: it holds the mirror's
invariants, it describes a source of the size the archive pins, and it is
canonical. The
verify scripts check that the source rebuilt from it is the file, byte for
byte. For the other formats, `py` and `web` are still two hand-written
implementations ([HARNESS.md](../../conformance/virtualize/HARNESS.md)). The
pixels are checked separately, per format, against an independent reader;
each README says which.
