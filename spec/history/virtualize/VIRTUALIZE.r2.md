# Virtualizing image files as OME-Zarr in vzip

Profiles version: 0 (**draft**) · Revision: 2

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

**Choosing the profile.** The file's first bytes decide:

| first bytes | |
|---|---|
| `49 49 2A 00`, `4D 4D 00 2A`, `49 49 2B 00`, `4D 4D 00 2B` | TIFF profile (§3) |
| `DA CE BE 0A` (the ND2 chunk magic, §4.1) | ND2 profile (§4) |
| anything else, including the JPEG 2000 signature box `00 00 00 0C 6A 50 20 20 0D 0A 87 0A` that starts legacy ND2 files | rejected |

**Rejection.** A virtualizer MUST reject an input the profile does not
accept, producing no output. That includes every input that is not well
formed as this document describes it: a read outside the file, a structure
that is truncated or inconsistent, a value of the wrong type or out of range,
an offset or length above 2^53 − 1. Being unable to read the file (a network
error) is not a rejection; the virtualizer fails instead.

**Structure only.** The output MUST NOT depend on the file's pixel data.
(Reading blocks that happen to include pixel bytes is fine.)

**References stay in the file.** Every range of the output MUST lie within
the file (`offset + length ≤` the file's size), and every reference entry's
payload (SPEC.md §4.3, as encoded by SPEC.md §5) MUST be at most 65519 bytes;
otherwise the input is rejected.

### 1.3 Arithmetic

Where this document computes a number (a scale, a size), it is computed in
IEEE 754 binary64 arithmetic, in the order written, with each operation
rounded to nearest. Decimal strings are converted to binary64 by correct
rounding. Integers are exact. A number that would be infinite or NaN rejects
the input. In JSON documents, integers are written as integers.

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
     `[y, x, c]`, after which `transpose` applies).
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

## 3. TIFF profile

### 3.1 Reading

The input is a TIFF (magic 42) or BigTIFF (magic 43, offset size 8 and
reserved word 0), in either byte order.

- **IFDs:** the virtualizer reads the chain of image file directories (IFDs)
  from the header (the **main chain**), and then, for every main-chain IFD,
  the IFDs whose offsets its `SubIFDs` tag (330) lists, in order. Only the
  listed offsets are read: a SubIFD's own next-IFD offset and SubIFDs are
  ignored. Every IFD read counts toward a limit of 100000, and reading the
  same offset twice (anywhere) is a cycle; either rejects the input. A
  `SubIFDs` tag with no values means no SubIFDs.
- **Values** are read as TIFF 6.0 and BigTIFF define them, for the field
  types they define. A used tag with an unknown field type rejects the input.
  Of duplicate tags in an IFD, the first is used.

Tags used (absent tags take the defaults shown):

| tag | name | default |
|---|---|---|
| 256, 257 | ImageWidth, ImageLength | required |
| 258 | BitsPerSample | required |
| 259 | Compression | 1 |
| 270 | ImageDescription | none |
| 277 | SamplesPerPixel | 1 |
| 284 | PlanarConfiguration | 1 (only read when SamplesPerPixel > 1; then it MUST be 1 or 2) |
| 317 | Predictor | 1 |
| 322, 323 | TileWidth, TileLength | required for tiled images |
| 324, 325 | TileOffsets, TileByteCounts | required for tiled images |
| 330 | SubIFDs | none |
| 339 | SampleFormat | 1 |

An IFD is **tiled** if it has TileWidth and TileOffsets. "Required" applies
to the IFDs that become planes or levels (§3.3, §3.4): a missing required
tag there rejects the input. All BitsPerSample values of such an IFD MUST be
equal, and all its SampleFormat values MUST be equal; their counts are not
checked. Its **format** is the tuple (BitsPerSample, SamplesPerPixel,
SampleFormat, PlanarConfiguration, Compression, Predictor), with
PlanarConfiguration taken as 1 when SamplesPerPixel is 1.

**IFD 0** below is the first IFD of the main chain.

### 3.2 OME-XML

If IFD 0's ImageDescription has field type ASCII (2), let `D` be its bytes up
to the first NUL. If `D` is valid UTF-8 and its text contains a start tag
whose name, without a namespace prefix, is `OME` (`<OME` or `<prefix:OME`,
followed by whitespace, `/` or `>`), `D` is the **OME-XML** `X`.

`X` is read as a sequence of tags, not as a validated XML document:

- comments (`<!--` to `-->`), CDATA sections (`<![CDATA[` to `]]>`),
  processing instructions (`<?` to `?>`) and other declarations (`<!` to
  `>`) are skipped;
- an element's name is compared without its namespace prefix; attribute
  names are compared exactly (they are unprefixed in OME-XML);
- attribute values have the five XML predefined entities (`&lt;` `&gt;`
  `&amp;` `&quot;` `&apos;`) and numeric character references decoded;
  other entity references are left as they are, and whitespace is not
  normalized;
- malformed XML is not detected; the scan uses the tags it finds.

From `X` the virtualizer uses:

- the `Name` attribute of the first `Image` start tag (the image name);
- the attributes of the first `Pixels` start tag (in document order):
  `SizeZ`, `SizeC`, `SizeT`, `DimensionOrder`, `PhysicalSizeX/Y/Z` and
  `PhysicalSizeX/Y/ZUnit`;
- the `TiffData` start tags between that `Pixels` start tag and its end tag
  (`</Pixels>`, or the end of `X`), in order, with attributes `IFD`,
  `FirstZ`, `FirstC`, `FirstT` and `PlaneCount`, and the `UUID` start tag
  between each `TiffData` start tag and its end (if it is not self-closing),
  with its `FileName` attribute and its text.

**Values.** Integer attributes (`Size*`, `First*`, `IFD`, `PlaneCount`) MUST
be decimal digits, optionally surrounded by whitespace; `SizeZ`, `SizeC`,
`SizeT` and `PlaneCount` MUST be at least 1. Otherwise the input is
rejected. A `PhysicalSize*` value counts as present only if it matches
`[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?` and its value is
finite and positive; otherwise it is treated as absent. `DimensionOrder`
MUST be `XY` followed by a permutation of `ZCT`, or the input is rejected.

The OME-XML's other content (for example `Interleaved`) is not used: the
TIFF tags decide.

### 3.3 Planes

Let `spp` be IFD 0's SamplesPerPixel. Without OME-XML there is one plane,
IFD 0. With OME-XML:

- `SizeZ` and `SizeT` default to 1; `SizeC` defaults to `spp`.
- If `spp > 1`: `SizeC` 1 is read as `spp`; any other `SizeC ≠ spp` is
  rejected (several RGB channels are not supported). Channels are then the
  samples, and there is one plane per (z, t).
- If `spp = 1`, there is one plane per (z, c, t).

Let `Cp` be 1 if `spp > 1`, else `SizeC`. Planes have positions (z, c, t)
with `z < SizeZ`, `c < Cp`, `t < SizeT`; the plane count is
`SizeZ × Cp × SizeT`. Planes are mapped to main-chain IFDs (by their index
in the main chain, IFD 0 being index 0) by the `TiffData` elements, in
document order (with one implicit `TiffData` without attributes if there
are none):

- **Multi-file datasets:** if the `TiffData` elements' `UUID`s name more
  than one distinct file (by `FileName`, or by UUID text when there is no
  `FileName`), the input is rejected. Otherwise `UUID`s are ignored, so a
  file that names itself is accepted.
- **Coverage:** a `TiffData` starts at position (`FirstZ`, `FirstC`,
  `FirstT`) (each default 0, and each MUST be less than `SizeZ`, `Cp`,
  `SizeT` respectively, else the input is rejected) and IFD index `IFD`
  (default 0). It covers `PlaneCount` planes. `PlaneCount` defaults to the
  plane count when it is the only `TiffData` and has no `IFD` attribute, and
  to 1 otherwise.
- **Stepping:** successive planes step through positions in
  `DimensionOrder` (default `XYZCT`; the letters after `XY` are fastest
  first, with sizes `SizeZ`, `Cp`, `SizeT`) and through consecutive IFD
  indices. Planes past the last position are ignored.
- A plane mapped twice takes the later mapping.

Every plane MUST be mapped to an existing main-chain IFD, else the input is
rejected.

### 3.4 Pyramid levels

- If IFD 0 has SubIFDs, there are `1 + s` levels, where `s` is IFD 0's
  SubIFD count: level 0 is the planes' IFDs, and level `k ≥ 1` is, for every
  plane, its IFD's `k`-th SubIFD. A plane IFD with fewer than `s` SubIFDs
  rejects the input.
- Otherwise level 0 is the planes. Without OME-XML, the later main-chain
  IFDs are scanned in order. One becomes the next level if it is tiled, has
  BitsPerSample, has IFD 0's format, and is strictly smaller in both width
  and length than the last level; others are skipped. This finds the
  pyramids of Aperio SVS files, whose stripped thumbnails, labels and macros
  are skipped. (A tiled image of the right format that is not part of the
  pyramid would be taken as a level.)

Within a level, all planes MUST have the same width, length, tile size and
format. Every image of every level MUST be tiled and have IFD 0's format.

### 3.5 Data types and codecs

The data type is `uint`, `int` or `float` (SampleFormat 1, 2, 3) followed by
BitsPerSample, which MUST be 8, 16, 32 or 64 (32 or 64 for `float`).

| Compression | Predictor | codecs (§2.1) |
|---|---|---|
| 1 | 1 | bytes |
| 8, 32946 (Deflate) | 1 | bytes, zlib |
| 50000 (zstd) | 1 | bytes, zstd |
| 33003, 33004, 33005, 34712 (JPEG 2000) | any | imagecodecs_jpeg2k |

Anything else is rejected. `bytes` has `endian` from the TIFF's byte order
(when the data type is larger than 1 byte). When `spp > 1` and
PlanarConfiguration is 1 (interleaved), `transpose` comes first, for every
compression including JPEG 2000.

### 3.6 Output

One image at the archive root (§2.2), with one array per level at path
`"<level>"`.

- **Axes:** `t` if `SizeT > 1`; `c` if the channel count (`SizeC` after
  §3.3, or `spp` without OME-XML) is more than 1; `z` if `SizeZ > 1`; then
  `y`, `x`.
- **Units** (only with OME-XML): `z`, `y`, `x` have the unit (§2.3) of
  `PhysicalSizeZUnit` etc. (default `µm`) when that `PhysicalSize` is
  present; `t` and `c` have none.
- **Shape:** the counts of `t`, `c`, `z`, then the level's length and width.
- **Chunk shape:** 1 for `t` and `z`; for `c`, `spp` if interleaved, else 1;
  then the level's TileLength and TileWidth (levels may differ).
- **Scale** of level `L` (level 0 has width `W0`, length `H0`):
  `y = PY × (H0 / HL)`, `x = PX × (W0 / WL)`, `z = PZ`, `t = c = 1`, where
  `PX`, `PY`, `PZ` are `PhysicalSizeX/Y/Z` when present, else 1. (The
  division is computed first.)
- **Name:** the OME `Image` name, if present and not empty.
- **Chunks:** for each plane (t, c, z) of a level, its IFD's tiles are numbered
  `k = s × T + j`, where `T` is the number of tiles per sample plane
  (`ceil(H/TileLength) × ceil(W/TileWidth)`), `j` runs row-major over the
  tile grid, and `s` is the sample when `spp > 1` and planar (else 0). Both
  TileOffsets and TileByteCounts MUST have `T` times the number of sample
  planes values. Tile `k` with TileByteCounts `n > 0` is the entry
  `<level>/c/<coords>` with one range `(0, TileOffsets[k], n)`; coords are
  `t`, `c` (the plane's channel, or the sample `s`, or 0 when interleaved),
  `z` (each only when present), then the tile row and column.
- **OME-XML:** if present, the entry `OME/METADATA.ome.xml` holds `D`, the
  ImageDescription's bytes up to the first NUL, unchanged.

## 4. ND2 profile

### 4.1 Chunks

An ND2 file (format version 3 or later) is a sequence of **chunks**. A chunk
at offset `o` has a 16-byte header: `u32` magic `0x0ABECEDA`, `u32` name
length `n`, `u64` data length `d` (all little-endian), then `n` bytes of name
(ASCII, NUL-padded), then `d` bytes of data starting at `o + 16 + n`. Every
chunk the virtualizer reads MUST start with the magic, else the input is
rejected; names are not checked except as stated.

- **Signature:** the chunk at offset 0 MUST be named
  `ND2 FILE SIGNATURE CHUNK NAME01!`, with `n = 32` and `d = 64`. Its data
  starts with `Ver`, decimal digits (the major version `M`), and `.`; `M`
  MUST be at least 3.
- **Chunk map:** the file MUST be at least 40 bytes long; its last 40 bytes
  are the 32 bytes `ND2 CHUNK MAP SIGNATURE 0000001!` and a `u64` offset
  `m`. The chunk at `m` MUST be named `ND2 FILEMAP SIGNATURE NAME 0001!`
  (after removing NUL padding). Its data is a sequence of records, each a
  name running up to and including the first `!`, then a `u64` offset and a
  `u64` size (unused), ending with the record named
  `ND2 CHUNK MAP SIGNATURE 0000001!`. The map gives each named chunk's header
  offset; of duplicate names, the last is used.

### 4.2 Lite variant

Metadata chunks hold a **lite variant** (LV) structure: a sequence of
records, running to the end of the chunk's data. A record is `u8` type,
`u8` name length `k` (in UTF-16 code units, including a terminating NUL),
the name (`2k` bytes of UTF-16LE; the name is its units up to the first
NUL), and a value:

| type | value |
|---|---|
| 1 | bool: `u8` (nonzero is true) |
| 2, 3 | `i32`, `u32` |
| 4, 5 | `i64`, `u64` |
| 6 | binary64 |
| 7 | `u64` (a pointer; the value is meaningless) |
| 8 | string: UTF-16LE code units up to the first NUL unit, which ends the record. Unpaired surrogates are replaced by U+FFFD. |
| 9 | byte array: `u64` length `b`, then `b` bytes |
| 11 | level: `u32` item count `c`, `u64` length `L`, then exactly `c` records, which MUST end at `L` bytes from the start of this record (its type byte); then `8c` bytes to skip |

Any other type rejects the input, except that a chunk's data MAY consist of
a single **compressed** record, type 76: the type byte and a name-length
byte, 10 bytes to skip, then a zlib stream (RFC 1950) that MUST end exactly
at the end of the chunk's data. The stream inflates to the chunk's LV
structure (which MUST NOT itself be compressed).

**Values.** All integers are little-endian. A level with at least one
record, all with empty names, is a **list** of their values. Any other level
(including an empty one) is an **object**: its members are its records by
name, in the order of each name's first appearance, and a repeated name has
its last value. A byte array is a list of its bytes. "The members of" an
object or a list are its values in order. Paths name members, for example
`SLxImageAttributes/uiWidth`; `a<i>` names a member like `a0`. A member
used as a number MUST have type 2–6, and as a flag type 1–5 (true if
nonzero); a missing member takes the default given, and a missing member
without one rejects the input.

### 4.3 Metadata

- **Attributes** (chunk `ImageAttributesLV!`, member `SLxImageAttributes`;
  required): `uiWidth`, `uiHeight`, `uiWidthBytes`, `uiComp` (components per
  pixel), `uiBpcInMemory`, `uiBpcSignificant` (all required, `uiWidth`,
  `uiHeight` and `uiComp` at least 1), `eCompression` (default 2),
  `uiTileWidth` and `uiTileHeight` (default 0). Other members, such as
  `uiSequenceCount` and `ePixelType`, are not used.
  - `uiBpcInMemory` 8, 16, 32 gives `uint8`, `uint16`, `float32`; other values
    are rejected.
  - `eCompression` 2 is uncompressed and 0 is lossless (zlib); any other
    value (1 is lossy) is rejected.
  - A tile width or height that is positive and differs from the image's is
    rejected.
- **Experiment** (chunk `ImageMetadataLV!`, member `SLxExperiment`; if the
  chunk is absent there are no loops) is a tree. A node has `eType`
  (required), `uLoopPars` and children, the members of `ppNextLevelEx`. Each
  node's loop is given by:

  | `eType` | loop | count |
  |---|---|---|
  | 1 | time | `uLoopPars/uiCount` (default 0); period `uLoopPars/dPeriod` (ms, default 0) |
  | 8 | time | the sum of `uiCount` over the members `p` of `uLoopPars/pPeriod` (default: none) that are valid; period: the first valid member's `dPeriod` (default 0) |
  | 2 | position | the number of members of `uLoopPars/Points` (default: none) that are valid |
  | 4 | z | `uLoopPars/uiCount` (default 0); step `abs(dZStep)`, or if that is 0 and the count is more than 1, `abs(dZHigh − dZLow) / (count − 1)` (each in `uLoopPars`, default 0) |
  | 6 | (spectral) | `uLoopPars/uiCount`, else `uLoopPars/pPlanes/uiCount`, else 0 |

  The `i`-th member of `pPeriod` (`Points`) is **valid** if
  `uLoopPars/pPeriodValid` (the node's `pItemValid`) is absent, or if its
  `i`-th member exists and is nonzero.

  Any other `eType` is rejected. The tree is flattened into a list of loops
  by visiting nodes depth first, each node before its children. The root has
  depth 0 and children are one deeper than their node, except that the
  children of a spectral node have the spectral node's depth. For each node,
  in this order:
  1. its `eType` is checked;
  2. without `uLoopPars`, or with a count of 0 (spectral nodes included): the
     node and its children are skipped;
  3. spectral: the node is skipped and its children are visited (its planes
     are pixel components, not a loop of the output);
  4. otherwise: if the list is empty or the last loop's depth is less than
     the node's, the node's loop is appended. If the last loop has the same
     depth and type (`eType`), and its count is less than the node's count,
     the node's loop replaces it. Otherwise the node's loop is dropped.
     Either way its children are visited.

  Rule 4 merges sibling branches that repeat one loop: a time loop with two
  position-loop children of 25 and 25 points gives [time, position]; the
  second position loop is dropped, and its children are still visited one
  level deeper.

  Two loops of the same kind (two time loops, for example) in the final list
  are rejected.
- **Picture metadata** (chunk `ImageMetadataSeqLV|0!`, member
  `SLxPictureMetadata`; optional):
  - planes: `sPicturePlanes/sPlaneNew/a<i>` for `i < sPicturePlanes/uiCount`
    (default 0), each with `sDescription` (default empty), `uiColor`
    (default `0xFFFFFF`) and `uiCompCount` (default 1);
  - calibration: the image is **calibrated** if `bCalibrated` is true and
    `dCalibration` (µm per pixel) is present and positive; `dAspect`
    defaults to 1, and a value that is not positive counts as 1.

### 4.4 Frames

The frames are numbered `0 ≤ f < N`, where `N` is the product of the loops'
counts (1 if there are none). Frame `f`'s coordinates are its row-major
index over the loops in list order (the last loop fastest). Frame `f` is the
chunk named `ImageDataSeq|<f>!` (decimal); a frame absent from the chunk map
is missing (no entry). Chunks with `f ≥ N` are ignored.

A frame's data is an 8-byte timestamp followed by its pixels: `uiHeight`
rows of `uiWidthBytes` bytes, each holding `uiWidth × uiComp` samples
(interleaved by component) of `uiBpcInMemory / 8` little-endian bytes,
then padding. Let `R = uiWidth × uiComp × uiBpcInMemory / 8`;
`uiWidthBytes` MUST be at least `R`.

- **Uncompressed:** the virtualizer MUST read the headers of the present
  frames with the lowest and the highest numbers, and reject the file if
  their name lengths differ, or if either's data length `d` is less than
  `8 + uiHeight × uiWidthBytes`. It uses the first one's name length `n` for
  every frame: frame `f`'s pixels start at `start = o + 16 + n + 8`, where
  `o` is its chunk's offset from the chunk map. If `uiWidthBytes = R`, the
  frame is one range `(0, start, uiHeight × R)`. Otherwise it is `uiHeight`
  ranges `(0, start + r × uiWidthBytes, R)` for rows `r = 0, 1, ...` in
  order.
- **Compressed:** `uiWidthBytes` MUST equal `R`. The virtualizer reads every
  present frame's header; the frame is one range `(0, o + 16 + n + 8, d − 8)`,
  a zlib stream of the pixels, and `d` MUST be more than 8.

### 4.5 Channels

Channels are the components. If there is at least one plane, every plane
exists, every plane's `uiCompCount` is 1 or 3, and they add up to `uiComp`,
the components are labeled plane by plane:

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

- **Axes:** `t` if there is a time loop (even of count 1), `c` if
  `uiComp > 1`, `z` if there is a z loop, then `y`, `x`. Shape: the loops'
  counts, `uiComp`, then `uiHeight`, `uiWidth` (each only for the axes
  present).
- **Chunk shape:** 1 for `t` and `z`, `uiComp` for `c`, then `uiHeight`,
  `uiWidth`: one frame per chunk.
- **Codecs** (§2.1): transpose when `c` is present (frames are interleaved),
  bytes (with `"endian": "little"` when the data type is larger than 1 byte),
  and zlib if compressed.
- **Units and scales:**
  - `x`: `micrometer` with scale `dCalibration` when calibrated, else no unit
    and scale 1;
  - `y`: `micrometer` with scale `dCalibration × dAspect` when calibrated,
    else no unit and scale 1;
  - `z`: `micrometer` with its step as scale when the step is positive, else
    no unit and scale 1;
  - `t`: `second` with scale `period / 1000` when the period is positive,
    else no unit and scale 1;
  - `c`: scale 1.
- **omero:** every image's `M` has
  `"omero": {"channels": [...]}`, one object per channel (even when there is
  no `c` axis):
  `{"label": ..., "color": ..., "active": true, "window": {"min": 0, "max": V, "start": 0, "end": V}}`
  with `V = 2^b − 1`, where `b` is `uiBpcSignificant` if it is between 1 and
  `uiBpcInMemory`, else `uiBpcInMemory`; for `float32` there is no `window`.
- **Chunks:** frame `f` at position `p` (0 without a position loop) is the
  entry `<p>/0/c/<coords>` with its ranges (§4.4); coords are its `t`, 0 for
  `c`, its `z` (each only when its axis is present), then 0, 0.

## 5. Conformance

There are two maintained implementations:
- the Python reference, `python -m vzip.virtualize <url> <out.vzip>`
  (`src/vzip/virtualize/`);
- the browser one, `web/src/virtualize.ts` and `web/src/nd2.ts`, run under
  Node by `web/conformance/virtualize.ts`.

Implementations written from this document alone, round by round, are in
`impls/virtualize/`; `conformance/virtualize/REVISIONS.md` records what each
round found and how this document changed.

`conformance/virtualize/compare.py` runs implementations on a corpus and
compares their outputs by §1.1 (`conformance/virtualize/HARNESS.md`
describes the command an implementation provides). The corpus has:
- the synthetic TIFF and ND2 files in `web/test/fixtures/`, including inputs
  each profile rejects;
- the 205 OME-TIFFs of IDR idr0096;
- 17 public ND2 files (`conformance/virtualize/corpus_nd2.txt`).

Pixel correctness is checked separately, against independent readers:
`web/test/verify_tiff.py` against tifffile, `web/test/verify_nd2.py` against
the synthetic files' known pixels, and `experiments/verify_nd2_vzip.py`
against the `nd2` package on public files.
