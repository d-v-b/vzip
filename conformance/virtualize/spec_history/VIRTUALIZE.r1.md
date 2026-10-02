# Virtualizing image files as OME-Zarr in vzip

Profiles version: 0 (**draft**) · Revision: 1

## 1. Introduction

A **virtualizer** reads the structure of an image file (TIFF or Nikon ND2)
and writes a vzip archive ([SPEC.md](SPEC.md)) that presents the file's
pixels as an OME-Zarr dataset. The pixels stay in the original file: each
Zarr chunk is a reference to byte ranges of it. This document specifies, for
each supported input format (a **profile**), exactly which archive a
virtualizer produces, so that independent virtualizers produce the same
output from the same input.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are to be
interpreted as described in RFC 2119.

### 1.1 Output and equivalence

A virtualizer's **output** is an archive description:

- a **source table**, and
- a set of **entries**: keys, each with either bytes or a list of ranges
  (SPEC.md §2, §5).

Two outputs are **equivalent** when they have the same source table, the same
set of keys, and for every key:

- **reference entries:** the same list of ranges, in order;
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

A virtualizer reads only the file's structure, never its pixel data. It
MUST reject an input the profile does not accept, producing no output.

### 1.3 Arithmetic

Where this document computes a number (a scale, a size), it is computed in
IEEE 754 binary64 arithmetic, in the order written, with each operation
rounded to nearest. Decimal strings are converted to binary64 by correct
rounding. Integers are exact.

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
   - `{"name": "bytes"}` for 1-byte data types;
   - `{"name": "bytes", "configuration": {"endian": "little"}}` or
     `"big"` for larger ones;
   - `{"name": "imagecodecs_jpeg2k"}` for JPEG 2000 (one codestream per
     chunk, decoding to the chunk's `[y, x]` or `[y, x, c]`).
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
  `"0"`, `"1"`, ...; each has exactly one `scale` transformation, one number
  per axis.
- `M` has an `"omero"` member only where a profile says so.

### 2.3 Units

OME-XML and ND2 unit symbols map to OME-NGFF units:

| symbol | unit |
|---|---|
| `µm` (U+00B5), `μm` (U+03BC), `um` | `micrometer` |
| `nm` | `nanometer` |
| `mm` | `millimeter` |
| `cm` | `centimeter` |
| `m` | `meter` |
| `Å` | `angstrom` |
| `pm` | `picometer` |
| `in` | `inch` |
| `ft` | `foot` |
| `s` | `second` |
| `ms` | `millisecond` |
| `min` | `minute` |
| `h` | `hour` |

Any other symbol gives no unit.

## 3. TIFF profile

### 3.1 Reading

The input is a TIFF (magic 42) or BigTIFF (magic 43, offset size 8), in
either byte order. The virtualizer reads the chain of image file directories
(IFDs) from the header, and for every IFD in the chain the IFDs listed in its
`SubIFDs` tag (330), if any. IFD values are read as TIFF 6.0 and BigTIFF
define them. An IFD cycle, or more than 100000 IFDs, is rejected.

Tags used (absent tags take the defaults shown):

| tag | name | default |
|---|---|---|
| 256, 257 | ImageWidth, ImageLength | required |
| 258 | BitsPerSample | required |
| 259 | Compression | 1 |
| 270 | ImageDescription | none |
| 277 | SamplesPerPixel | 1 |
| 284 | PlanarConfiguration | 1 (only read when SamplesPerPixel > 1) |
| 317 | Predictor | 1 |
| 322, 323 | TileWidth, TileLength | required (the image is tiled) |
| 324, 325 | TileOffsets, TileByteCounts | required |
| 330 | SubIFDs | none |
| 339 | SampleFormat | 1 |

An image is **tiled** if it has TileWidth and TileOffsets. Its **format** is
the tuple (BitsPerSample, SamplesPerPixel, SampleFormat,
PlanarConfiguration, Compression, Predictor). All BitsPerSample values of an
image MUST be equal.

### 3.2 OME-XML

If the first IFD's ImageDescription contains `<OME`, it is the **OME-XML**
`X` (the tag's bytes up to the first NUL, as UTF-8). From `X` the virtualizer
uses:

- the `Name` attribute of the first `Image` element (the image name);
- the attributes of the first `Pixels` element: `SizeZ`, `SizeC`, `SizeT`,
  `DimensionOrder`, `PhysicalSizeX/Y/Z` and `PhysicalSizeX/Y/ZUnit`;
- the `TiffData` elements inside it, with attributes `IFD`, `FirstZ`,
  `FirstC`, `FirstT`, `PlaneCount`, and the `FileName` of a `UUID` child.

Element names may carry a namespace prefix. Attribute values have the five
XML predefined entities and numeric character references decoded.

### 3.3 Planes

Let `spp` be the first IFD's SamplesPerPixel. Without OME-XML there is one
plane, the first IFD. With OME-XML:

- `SizeZ`, `SizeT` default to 1; `SizeC` defaults to `spp`.
- If `spp > 1`: `SizeC` 1 is read as `spp`; any other `SizeC ≠ spp` is
  rejected. Channels are then the samples, and there is one plane per (z, t).
- If `spp = 1`, there is one plane per (z, c, t).

The plane count is `SizeZ × SizeT × Cp`, with `Cp = 1` if `spp > 1`, else
`SizeC`. Planes are mapped to main-chain IFDs by the `TiffData` elements, in
document order (one implicit empty `TiffData` if there are none):

- A `TiffData` with a `UUID` `FileName` is rejected (multi-file datasets).
- It covers `PlaneCount` planes, starting at position (`FirstZ`, `FirstC`,
  `FirstT`) (default 0) and IFD `IFD` (default 0). `PlaneCount` defaults to
  all planes when it is the only `TiffData` and has no `IFD`, else to 1.
- Successive planes step through positions in `DimensionOrder` (default
  `XYZCT`; the letters after `XY` are fastest first) and through consecutive
  IFDs.

Every plane MUST be mapped to an existing IFD, else the input is rejected.

### 3.4 Pyramid levels

- If the first IFD has SubIFDs, level 0 is the planes' IFDs and level `k ≥ 1`
  is, for every plane, its IFD's `k`-th SubIFD.
- Otherwise level 0 is the planes. Without OME-XML, every later main-chain
  IFD that is tiled, has the first IFD's format, and is smaller in both
  width and length than the previous level, becomes the next level (as in
  Aperio SVS files, whose thumbnails, labels and macros are skipped).

Within a level, all planes MUST have the same width, length, tile size and
format. Every image of every level MUST be tiled and have the first IFD's
format.

### 3.5 Data types and codecs

The data type is `uint`, `int` or `float` (SampleFormat 1, 2, 3) followed by
BitsPerSample, which MUST be 8, 16, 32 or 64 (32 or 64 for `float`).

| Compression | Predictor | codecs (§2.1) |
|---|---|---|
| 1 | 1 | bytes |
| 8, 32946 (Deflate) | 1 | bytes, zlib |
| 50000 (zstd) | 1 | bytes, zstd |
| 33003, 33004, 33005, 34712 (JPEG 2000) | any | imagecodecs_jpeg2k |

Anything else is rejected. `bytes` has `endian` from the TIFF's byte order.
When `spp > 1` and PlanarConfiguration is 1 (interleaved), `transpose` comes
first.

### 3.6 Output

One image at the archive root (§2.2), with one array per level at path
`"<level>"`.

- **Axes:** `t` if `SizeT > 1`; `c` if the channel count (`SizeC`, or `spp`)
  is more than 1; `z` if `SizeZ > 1`; then `y`, `x`.
- **Units:** `z`, `y`, `x` have the unit of `PhysicalSizeZUnit` etc.
  (default `µm`) when that `PhysicalSize` is present; `t` and `c` have none.
- **Shape:** the counts of `t`, `c`, `z`, then the level's length and width.
- **Chunk shape:** 1 for `t` and `z`; for `c`, `spp` if interleaved, else 1;
  then TileLength, TileWidth.
- **Scale** of level `L` (level 0 has width `W0`, length `H0`):
  `y = (PhysicalSizeY or 1) × (H0 / HL)`, `x = (PhysicalSizeX or 1) × (W0 / WL)`,
  `z = PhysicalSizeZ or 1`, `t = c = 1`.
- **Name:** the OME `Image` name, if any.
- **Chunks:** for each plane (t, c, z) of a level, its IFD's tiles are numbered
  `k = s × T + j`, where `T` is the number of tiles per sample plane
  (`ceil(H/TileLength) × ceil(W/TileWidth)`), `j` runs row-major over the
  tile grid, and `s` is the sample when `spp > 1` and planar (else 0). The
  IFD's tile count MUST be `T` times the number of sample planes. Tile `k` with
  TileByteCounts `n > 0` is the entry `<level>/c/<coords>` with one range
  `(0, TileOffsets[k], n)`; coords are `t`, `c` (the plane's channel, or the
  sample `s`, or 0 when interleaved), `z` (each only when present), then the
  tile row and column.
- **OME-XML:** if present, the entry `OME/METADATA.ome.xml` holds `X` as UTF-8.

## 4. ND2 profile

### 4.1 Chunks

An ND2 file (format version 3 or later) is a sequence of **chunks**. A chunk
at offset `o` has a 16-byte header: `u32` magic `0x0ABECEDA`, `u32` name
length `n`, `u64` data length `d` (all little-endian), then `n` bytes of name
(ASCII, NUL-padded), then `d` bytes of data starting at `o + 16 + n`.

- **Signature:** the chunk at offset 0 MUST be named
  `ND2 FILE SIGNATURE CHUNK NAME01!` (`n = 32`, `d = 64`); its data starts
  with `VerM.m`, and the major version `M` MUST be at least 3. Files that
  start with a JPEG 2000 signature (version 1, "legacy") are rejected.
- **Chunk map:** the file's last 40 bytes are the 32 bytes
  `ND2 CHUNK MAP SIGNATURE 0000001!` and a `u64` offset `m`. The chunk at `m`
  is named `ND2 FILEMAP SIGNATURE NAME 0001!`; its data is a sequence of
  records, each a name ending in `!`, then `u64` offset and `u64` size, ending
  with the record named `ND2 CHUNK MAP SIGNATURE 0000001!`. The map gives
  each named chunk's header offset.

### 4.2 Lite variant

Metadata chunks hold a **lite variant** (LV) structure: a sequence of
records. A record is `u8` type, `u8` name length `k` (in UTF-16 code units,
including a terminating NUL), the name (`2k` bytes of UTF-16LE), and a value:

| type | value |
|---|---|
| 1 | bool: `u8` |
| 2, 3 | `i32`, `u32` |
| 4, 5 | `i64`, `u64` |
| 6 | binary64 |
| 7 | `u64` (a pointer; the value is meaningless) |
| 8 | string: UTF-16LE code units up to and excluding the first NUL unit |
| 9 | byte array: `u64` length `b`, then `b` bytes |
| 11 | level: `u32` item count `c`, `u64` length `L`, then `c` records, then `8c` bytes to skip; `L` counts from the start of this record to the end of the records |
| 76 | compressed: the name-length byte is followed by no name and 10 bytes to skip; the rest of the data is a zlib stream (RFC 1950) of an LV structure, which replaces this one |

All integers are little-endian. A level is an object whose members are its
records, by name (a repeated name keeps its last value). A level whose
records all have empty names is a list of their values instead; byte arrays
are lists of their bytes. "The members of" a level or list are its values in
order. A chunk's data
is one top-level LV structure. Paths below name members, for example
`SLxImageAttributes/uiWidth`; `a<i>` names a member like `a0`.

### 4.3 Metadata

- **Attributes** (chunk `ImageAttributesLV!`, member `SLxImageAttributes`):
  `uiWidth`, `uiHeight`, `uiWidthBytes`, `uiComp` (components per pixel),
  `uiBpcInMemory`, `uiBpcSignificant`, `uiSequenceCount`, `eCompression`
  (default 2), `uiTileWidth`, `uiTileHeight`.
  - `uiBpcInMemory` 8, 16, 32 gives `uint8`, `uint16`, `float32`; other values
    are rejected.
  - `eCompression` 2 is uncompressed and 0 is lossless (zlib); 1 (lossy) is
    rejected.
  - A tile width or height that is positive and differs from the image's is
    rejected.
- **Experiment** (chunk `ImageMetadataLV!`, member `SLxExperiment`; absent
  means no loops) is a tree. A node has `eType`, `uLoopPars` and children in
  `ppNextLevelEx` (members in order). Each node's loop is given by:

  | `eType` | loop | count |
  |---|---|---|
  | 1 | time | `uLoopPars/uiCount`; period `uLoopPars/dPeriod` (ms) |
  | 8 | time | the sum of `uiCount` over the members `p` of `uLoopPars/pPeriod` whose entry in `uLoopPars/pPeriodValid` is nonzero; period: the first such member's `dPeriod` |
  | 2 | position | the number of members of `uLoopPars/Points`, counting only those whose entry in the node's `pItemValid` byte array is nonzero when `pItemValid` is present |
  | 4 | z | `uLoopPars/uiCount`; step `abs(uLoopPars/dZStep)`, or if that is 0 and the count is more than 1, `abs(dZHigh − dZLow) / (count − 1)` |
  | 6 | (spectral) | `uLoopPars/uiCount`, else `uLoopPars/pPlanes/uiCount`, else 0; not a loop of the output (its planes are pixel components) |

  Any other `eType` is rejected. The tree is flattened into a list of loops
  by visiting nodes depth first, each node before its children. The root has
  depth 0 and children are one deeper than their node, except that the
  children of a spectral node have the spectral node's depth. For each node:
  - without `uLoopPars`, or with a count of 0: the node and its children are
    skipped;
  - spectral: the node is skipped and its children are visited;
  - otherwise: if the list is empty or the last loop's depth is less than the
    node's, the node's loop is appended. If the last loop has the same depth
    and type (`eType`) and a smaller count, the node's loop replaces it.
    Otherwise the node's loop is dropped. Either way its children are visited.

  Two loops of the same kind (two time loops, for example) in the final list
  are rejected.
- **Picture metadata** (chunk `ImageMetadataSeqLV|0!`, member
  `SLxPictureMetadata`; optional):
  - planes: `sPicturePlanes/sPlaneNew/a<i>` for `i < sPicturePlanes/uiCount`,
    each with `sDescription`, `uiColor` and `uiCompCount` (default 1);
  - calibration: `dCalibration` (µm per pixel) when `bCalibrated` is true, and
    `dAspect` (default 1).

### 4.4 Frames

The frames are numbered `0 ≤ f < N`, where `N` is the product of the loops'
counts (1 if there are none). Frame `f`'s coordinates are its row-major
index over the loops in list order (the last loop fastest). Frame `f` is the
chunk named `ImageDataSeq|<f>!` (decimal); a frame absent from the chunk map
is missing (no entry). Chunks with `f ≥ N` are ignored.

A frame's data is an 8-byte timestamp followed by its pixels: `uiHeight`
rows of `uiWidthBytes` bytes, each holding `uiWidth × uiComp` samples
(interleaved by component) of `uiBpcInMemory / 8` little-endian bytes,
then padding. Let `R = uiWidth × uiComp × uiBpcInMemory / 8`.

- **Uncompressed:** the pixels start at `o + 16 + n + 8`, where `o` is the
  frame chunk's offset from the chunk map and `n` its name length. The
  virtualizer MUST read the headers of the first and the last frame and
  reject the file if their name lengths differ; it then uses the first
  frame's name length for every frame. If `uiWidthBytes = R`, the frame is one
  range `(0, start, uiHeight × R)`. Otherwise it is `uiHeight` ranges
  `(0, start + r × uiWidthBytes, R)` for rows `r = 0, 1, ...` in order.
- **Compressed:** the virtualizer reads every frame's header; the frame is
  one range `(0, o + 16 + n + 8, d − 8)`, a zlib stream of the pixels. A
  compressed file with `uiWidthBytes ≠ R` is rejected.

### 4.5 Channels

Channels are the components. If the planes' `uiCompCount` values add up to
`uiComp`, the components are labeled plane by plane:

- a plane with one component gives one channel labeled `sDescription`,
  colored by `uiColor`;
- a plane with three components gives three channels labeled
  `<sDescription> R`, `<sDescription> G`, `<sDescription> B`, colored
  `FF0000`, `00FF00`, `0000FF`.

Otherwise channel `k` is labeled `C<k>` and colored `FFFFFF`. A `uiColor` is
`0xAABBGGRR`; its color is the six uppercase hexadecimal digits of red,
green and blue.

### 4.6 Output

The output is a bioformats2raw layout:

- `zarr.json`: a group with attributes
  `{"ome": {"version": "0.5", "bioformats2raw.layout": 3}}`.
- `OME/zarr.json`: a group with attributes
  `{"ome": {"version": "0.5", "series": ["0", "1", ...]}}`, one series per
  position (one if there is no position loop).
- For each position `p`, `"<p>/zarr.json"`: an image (§2.2) with
  `"name": "position <p>"` and one level, the array `"<p>/0"`.

Each array:

- **Axes:** `t` if there is a time loop, `c` if `uiComp > 1`, `z` if there is
  a z loop, then `y`, `x`. Shape: the loops' counts, `uiComp`, then
  `uiHeight`, `uiWidth` (each only for the axes present).
- **Chunk shape:** 1 for `t` and `z`, `uiComp` for `c`, then `uiHeight`,
  `uiWidth`: one frame per chunk.
- **Codecs:** transpose when `c` is present (frames are interleaved), bytes
  (little-endian), and zlib if compressed.
- **Units and scale:** `x` and `y` are `micrometer` with scales
  `dCalibration` and `dCalibration × dAspect` when calibrated, and otherwise
  have no unit and scale 1. `z` is `micrometer` with its step as scale when
  the step is positive, else no unit and scale 1. `t` is `second` with
  scale `period / 1000` when the period is positive, else no unit and
  scale 1. `c` has scale 1.
- **omero:** the image's `M` has
  `"omero": {"channels": [...]}`, one object per channel:
  `{"label": ..., "color": ..., "active": true, "window": {"min": 0, "max": V, "start": 0, "end": V}}`
  with `V = 2^uiBpcSignificant − 1`; for `float32` there is no `window`.
- **Chunks:** frame `f` at position `p` (0 without a position loop) is the
  entry `<p>/0/c/<coords>` with its ranges (§4.4); coords are its `t`, 0 for
  `c`, its `z` (each only when its axis is present), then 0, 0.

## 5. Conformance

There are two implementations:
- the Python reference, `python -m vzip.virtualize <url> <out.vzip>`
  (`src/vzip/virtualize/`, decoding LV with the `nd2` package);
- the browser one, `web/src/virtualize.ts` and `web/src/nd2.ts`, run under
  Node by `web/conformance/virtualize.ts`, with its own LV decoder.

Both exit with status 3 when they reject an input.

`conformance/virtualize/compare.py` runs both on a corpus and compares their
outputs by §1.1. The corpus has:
- the synthetic TIFF and ND2 files in `web/test/fixtures/`, including inputs
  each profile rejects;
- the 205 OME-TIFFs of IDR idr0096;
- 17 public ND2 files (`conformance/virtualize/corpus_nd2.txt`).

On revision 1, all 240 inputs agree: 231 equivalent outputs, and 9 inputs
both reject.

Pixel correctness is checked separately, against independent readers:
`web/test/verify_tiff.py` against tifffile, `web/test/verify_nd2.py` against
the synthetic files' known pixels, and `experiments/verify_nd2_vzip.py`
against the `nd2` package on public files.
