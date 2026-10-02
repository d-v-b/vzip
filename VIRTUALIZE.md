# Virtualizing image files as OME-Zarr in vzip

Profiles version: 0 (**draft**) · Revision: 11

## 1. Introduction

A **virtualizer** reads the structure of an image file (TIFF, Hamamatsu NDPI, Nikon ND2,
DICOM, NIfTI or Imaris IMS) and writes a vzip archive ([SPEC.md](SPEC.md)) that presents the file's
pixels as an OME-Zarr dataset. The pixels stay in the original file: each
Zarr chunk is a reference to byte ranges of it. This document specifies, for
each supported input format (a **profile**), exactly which archive a
virtualizer produces, so that independent virtualizers produce the same
output from the same input.

This document holds what every profile shares: the input, the output and how
outputs compare (§1), and the common parts of the output (§2). Each profile
is a document of its own, numbered as a section of this one:

| § | profile | inputs |
|---|---|---|
| 3 | [TIFF](profiles/tiff.md) | TIFF and BigTIFF, including OME-TIFF and JPEG-tiled slides such as Aperio SVS |
| 4 | [NDPI](profiles/ndpi.md) | Hamamatsu NDPI slides, a TIFF variant with 64-bit offsets |
| 5 | [ND2](profiles/nd2.md) | Nikon ND2, format version 3 and later |
| 6 | [DICOM](profiles/dicom.md) | DICOM Part 10 files with native or JPEG-encapsulated pixel data, including whole-slide images |
| 7 | [NIfTI](profiles/nifti.md) | NIfTI-1 and NIfTI-2 single files (`.nii`) |
| 8 | [IMS](profiles/ims.md) | Imaris IMS files (HDF5) |

§9 is informative: the implementations and how they are compared.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are to be
interpreted as described in RFC 2119.

### 1.1 Output and equivalence

A virtualizer's **output** is an archive description:

- a **source table**, and
- a set of **entries**: keys, each with either bytes or a list of ranges
  (SPEC.md §2, §5).

Two outputs are **equivalent** when they have the same source table, the same
set of keys, and for every key:

- **reference entries:** the same list of ranges, in order. A range is a
  source range `(0, offset, length)` or a **literal** range of given bytes
  (SPEC.md §5.2), and two ranges are the same only if both are source ranges
  with equal offsets and lengths, or both are literal ranges with identical
  bytes;
- **JSON documents** (keys ending in `zarr.json`): equal JSON values. Objects
  compare by key, arrays by order, and numbers as IEEE 754 binary64 values;
  whitespace and key order do not matter;
- **other bytes entries:** identical bytes.

The archive's byte layout (entry order, compression, page index) is the
writer's choice and is not part of the output. Two virtualizers conform to
the same profile revision if they produce equivalent outputs for every input
that the profile accepts, and both reject every input it rejects.

### 1.2 Input

A virtualizer is given the input file's URL, `U`. The output's source table
is exactly one `url` source whose value is `U` as given, without pins.
(`U` MUST be a valid absolute URI; virtualizers do not normalize it.)
Every reference in the output is a range of source 0.

**Choosing the profile.** The file's first bytes decide, by the first row
of this table that matches. `H` is the file's first `min(552, size)` bytes;
a test that needs a byte beyond `H` does not match.

| test on `H` | |
|---|---|
| bytes 128–131 are `44 49 43 4D` (`DICM`) | DICOM profile (§6) |
| bytes 0–3 are `49 49 2A 00`, `4D 4D 00 2A`, `49 49 2B 00` or `4D 4D 00 2B` | NDPI profile (§4) if the file passes its detection test (§4), else TIFF profile (§3) |
| bytes 0–3 are `DA CE BE 0A` (the ND2 chunk magic, §5.1) | ND2 profile (§5) |
| bytes 0–7 are `89 48 44 46 0D 0A 1A 0A` (the HDF5 signature) | IMS profile (§8) |
| bytes 0–3 are the 32-bit integer 348 in either byte order, and bytes 344–347 are `6E 2B 31 00` (`n+1`) | NIfTI profile (§7), NIfTI-1 |
| bytes 0–3 are the 32-bit integer 540 in either byte order, and bytes 4–11 are `6E 2B 32 00 0D 0A 1A 0A` (`n+2`) | NIfTI profile (§7), NIfTI-2 |
| anything else, including the JPEG 2000 signature box `00 00 00 0C 6A 50 20 20 0D 0A 87 0A` that starts legacy ND2 files, and NIfTI header-and-image pairs (`ni1`, `ni2`) | rejected |

A DICOM file is read by the DICOM profile even when its 128-byte preamble
holds a TIFF header, as dual-personality files and some writers' preambles
do. An HDF5 file that is not an Imaris file is rejected by the IMS profile.

**Rejection.** A virtualizer MUST reject an input the profile does not
accept, producing no output. That includes every input that is not well
formed as this document describes it: a read outside the file, a structure
that is truncated or inconsistent, a value of the wrong type or out of range,
an offset or length above 2^53 − 1. Being unable to read the file (a network
error) is not a rejection; the virtualizer fails instead.

**Structure only.** The output MUST NOT depend on the file's pixel data.
(Reading blocks that happen to include pixel bytes is fine.)

**Evaluation order.** Whether an input is rejected never depends on the
order in which a virtualizer reads or checks things: each profile lists what
is checked, and every listed check applies whether or not its value ends up
in the output.

**References stay in the file.** Every range of the output MUST lie within
the file (`offset + length ≤` the file's size), and every reference entry's
payload MUST be at most 65519 bytes; otherwise the input is rejected. The
payload (SPEC.md §4.3, §5) of a single range is its `Range` message: for a
source range `(0, o, n)`, `0x18 varint(o)` if `o > 0`, then `0x20 varint(n)`
if `n > 0` (fields 3 and 4); for a literal range of bytes `d`, `0x2A varint(len(d)) d`.
The payload of several ranges is a `Concat` message: for each range, `0x0A
varint(len(r)) r`, where `r` is that range's `Range` message. `varint` is the
protobuf base-128 encoding (1 byte below 2^7, 2 below 2^14, and so on).

### 1.3 Arithmetic

Where this document computes a number (a scale, a size), it is computed in
IEEE 754 binary64 arithmetic, in the order written, with each operation
rounded to nearest. Decimal strings are converted to binary64 by correct
rounding. Integers are exact. A number that would be infinite or NaN rejects
the input. In JSON documents, integers (shapes, counts, codec parameters) are
written as integers; a computed number such as a scale MAY be written either
way (`1` or `1.0`), since outputs compare numbers by value (§1.1).

**Whitespace** in this document means the XML whitespace characters: space,
tab, CR and LF (U+0020, U+0009, U+000D, U+000A). **Digits** are the ASCII
digits `0`–`9`.

## 2. Common output

### 2.1 Arrays

Every array's `zarr.json` is the JSON object:

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": [...],
  "data_type": "...",
  "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [...]}},
  "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
  "fill_value": 0,
  "codecs": [...],
  "dimension_names": [...],
  "attributes": {}
}
```

with no other members. Chunk keys are `<array path>/c/<i0>/<i1>/...`. A chunk
whose data the file does not contain has no entry (it reads as the fill
value).

**Axes.** An image's axes are a subsequence of `t, c, z, y, x`, in that
order: `t` (time), `c` (channel), `z`, `y`, `x` (space). `y` and `x` are
always present; the profile says when the others are.

**Codecs.** `codecs` is built from these, in this order:

1. `{"name": "transpose", "configuration": {"order": [...]}}` when a chunk's
   bytes hold the channel axis last ("interleaved"). `order` lists the array's
   axis indices in stored order: every axis except `c` in array order, then
   `c`.
2. The array-to-bytes codec, one of:
   - `{"name": "bytes"}` (no `configuration`) for 1-byte data types;
   - `{"name": "bytes", "configuration": {"endian": "little"}}` or
     `"big"` for larger ones;
   - `{"name": "imagecodecs_jpeg2k"}` for JPEG 2000 (one codestream per
     chunk, decoding to the chunk's `[y, x]`, or, when interleaved, to
     `[y, x, c]`, after which `transpose` applies). Decoding follows OpenJPEG,
     as imagecodecs does: a 3-component codestream whose first component is
     at full resolution and whose other two are subsampled holds YCbCr, and
     decodes to RGB (as in Aperio's compression 33003);
   - `{"name": "imagecodecs_jpeg"}` for JPEG (one complete JPEG stream, ISO/IEC
     10918-1 with the JFIF/Adobe colour conventions, per chunk, decoding to
     the chunk's `[y, x]` or `[y, x, c]` like JPEG 2000).
3. A compressor, when the bytes are compressed:
   - `{"name": "zlib", "configuration": {"level": 1}}` (zlib streams);
   - `{"name": "zstd", "configuration": {"level": 0, "checksum": false}}`.

### 2.2 Images

An image is a group whose `zarr.json` is
`{"zarr_format": 3, "node_type": "group", "attributes": {"ome": M}}`, where `M`
is the OME-NGFF 0.5 object:

```json
{
  "version": "0.5",
  "multiscales": [{
    "name": "...",
    "axes": [{"name": "t", "type": "time", "unit": "second"}, ...],
    "datasets": [{"path": "0", "coordinateTransformations": [{"type": "scale", "scale": [...]}]}, ...]
  }]
}
```

- `name` is present only when the profile gives one.
- Each axis has `name` and `type` (`"time"`, `"channel"` or `"space"`), and
  `unit` only when the profile gives one.
- `datasets` lists the pyramid levels, full resolution first, with paths
  `"0"`, `"1"`, ...; each has a `scale` transformation, one number per axis,
  followed by a `{"type": "translation", "translation": [...]}`
  transformation (one number per axis) only where the profile gives one.
- `M` has an `"omero"` member, next to `multiscales`, only where a profile
  says so.

### 2.3 Units

OME-XML unit symbols map to OME-NGFF units:

| symbol | unit |
|---|---|
| `µm` (U+00B5), `μm` (U+03BC), `um` | `micrometer` |
| `nm` | `nanometer` |
| `mm` | `millimeter` |
| `cm` | `centimeter` |
| `m` | `meter` |
| `Å` (U+00C5), `Å` (U+212B) | `angstrom` |
| `pm` | `picometer` |
| `in` | `inch` |
| `ft` | `foot` |
| `s` | `second` |
| `ms` | `millisecond` |
| `min` | `minute` |
| `h` | `hour` |

Any other symbol gives no unit (the scale is still used).

A **length** in one of these units converts to another by the units'
sizes in metres: micrometer 1e-6, nanometer 1e-9, millimeter 1e-3,
centimeter 1e-2, meter 1, angstrom 1e-10, picometer 1e-12, inch 0.0254,
foot 0.3048. A value `v` in unit `a` is `v × (size(a) / size(b))` in unit
`b` (the division first).

**Translations.** Where a profile places an image in space (§2.2), the
translation applies to every level, and is 0 for every axis it does not
name. A position given for the centre of an image of level-0 size
`W0 × H0` with x and y scales `sx`, `sy` becomes the translation
`x = cx − W0 × sx / 2`, `y = cy − H0 × sy / 2` (products first).

## 3–8. Profiles

Each profile is a separate document:

- §3, the TIFF profile: [profiles/tiff.md](profiles/tiff.md);
- §4, the NDPI profile: [profiles/ndpi.md](profiles/ndpi.md);
- §5, the ND2 profile: [profiles/nd2.md](profiles/nd2.md);
- §6, the DICOM profile: [profiles/dicom.md](profiles/dicom.md);
- §7, the NIfTI profile: [profiles/nifti.md](profiles/nifti.md);
- §8, the IMS profile: [profiles/ims.md](profiles/ims.md).

## 9. Conformance

This section is informative. There are two maintained implementations:
- the Python reference, `python -m vzip.virtualize <url> <out.vzip>`
  (`src/vzip/virtualize/`);
- the browser one, `web/src/virtualize/`, run under Node by
  `web/conformance/virtualize.ts`.

Both are organized by profile: `tiff/`, `ndpi/` and `nd2/`, next to the
parts they share (`common`).

Implementations written from this document alone, round by round, are in
`impls/virtualize/`; `conformance/virtualize/REVISIONS.md` records what each
round found and how this document changed.

`conformance/virtualize/compare.py` runs implementations on a corpus and
compares their outputs by §1.1 (`conformance/virtualize/HARNESS.md`
describes the command an implementation provides). The corpus has:
- the synthetic files in `web/test/fixtures/tiff/`, `ndpi/` and `nd2/`,
  including inputs each profile rejects;
- the 205 OME-TIFFs of IDR idr0096;
- public TIFF, SVS and NDPI files (`conformance/virtualize/corpus_tiff.txt`)
  and ND2 files (`conformance/virtualize/corpus_nd2.txt`).

Pixel correctness is checked separately, against independent readers:
`web/test/tiff/verify.py` and `web/test/ndpi/verify.py` against tifffile,
`web/test/nd2/verify.py` against the synthetic files' known pixels, and `experiments/verify_nd2_vzip.py`
against the `nd2` package on public files.
