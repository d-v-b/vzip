# Virtualizing ND2 files

The ND2 profile of [VIRTUALIZE.md](../VIRTUALIZE.md) (revision 10), numbered
as its §5. §1 and §2 are in VIRTUALIZE.md and apply here.

## 5. ND2 profile

### 5.1 Chunks

An ND2 file (format version 3 or later) is a sequence of **chunks**. A chunk
at offset `o` has a 16-byte header: `u32` magic `0x0ABECEDA`, `u32` name
length `n`, `u64` data length `d` (all little-endian), then `n` bytes of name
(ASCII, NUL-padded), then `d` bytes of data starting at `o + 16 + n`. Every
chunk the virtualizer reads MUST start with the magic, else the input is
rejected; names are not checked except as stated. Reading a chunk's header
means reading its 16 bytes and its `n` bytes of name (both MUST lie within
the file), not its data.

- **Signature:** the chunk at offset 0 MUST be named
  `ND2 FILE SIGNATURE CHUNK NAME01!`, with `n = 32` and `d = 64`. Its data
  starts with `Ver`, decimal digits (the major version `M`), and `.`; `M`
  MUST be at least 3.
- **Chunk map:** the file MUST be at least 40 bytes long; its last 40 bytes
  are the 32 bytes `ND2 CHUNK MAP SIGNATURE 0000001!` and a `u64` offset
  `m`. The chunk at `m` MUST be named `ND2 FILEMAP SIGNATURE NAME 0001!`
  (its name bytes up to the first NUL, or all `n` if there is none). Its
  data is a sequence of records, each a name running up to and including the
  first `!`, then a `u64` offset and a `u64` size (unused). The record named
  `ND2 CHUNK MAP SIGNATURE 0000001!` ends the map: its offset and size, and
  anything after them, are not read. A record (other than the last) that
  runs past the end of the data, or data without that last record, rejects
  the input. The map gives each named chunk's header offset; of duplicate
  names, the last is used. An offset is checked (≤ 2^53 − 1, chunk magic)
  only when that chunk is read.

### 5.2 Lite variant

Metadata chunks hold a **lite variant** (LV) structure: a sequence of
records, running to the end of the chunk's data. A record is `u8` type,
`u8` name length `k` (in UTF-16 code units, including a terminating NUL),
the name (`2k` bytes of UTF-16LE; the name is its units up to the first
NUL, decoded like a string), and a value:

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
a single **compressed** record, type 76: the type byte, a name-length byte
(whatever its value), 10 bytes to skip, then from offset 12 a zlib stream
(RFC 1950) that MUST end exactly at the end of the chunk's data. The stream
inflates to the chunk's LV structure (which MUST NOT itself start with a
compressed record).

Levels MUST NOT be nested more than 100 deep: a chunk's top-level records
are at depth 0, the records of a level at depth `d` are at depth `d + 1`,
and a level record (type 11) at depth 100 or more rejects the input, even
if it is empty.

**Values.** All integers are little-endian. A level with at least one
record, all with empty names, is a **list** of their values. Any other level
(including an empty one) is an **object**: its members are its records by
name, in the order of each name's first appearance, and a repeated name has
its last value. A chunk's top-level records always form an object. A byte
array is a list of its bytes; each byte counts as a value of type 3. "The
members of" an object or a list are its values in order.

**Paths** name members, for example `SLxImageAttributes/uiWidth`; `a<i>`
names a member like `a0` (`i` in decimal, without leading zeros). Each step
of a path looks up a name in an object; a step from a value that is not an
object (a list, a byte array, a scalar) rejects the input. A missing member
takes the default given, and a missing member without one rejects the
input. A member read as:

- a **number** MUST have type 2–6; its value is converted to binary64
  (rounding to nearest) and MUST be finite;
- an **integer** MUST be a number with an integral value from 0 to
  2^53 − 1 (a **color** is an integer from −2^31 to 2^32 − 1, taken modulo
  2^32);
- a **flag** MUST have type 1–5 (true if nonzero);
- a **string** MUST have type 8;
- an **object** or a **list** MUST be one (a byte array is a list).

Any other value rejects the input.

### 5.3 Metadata

Every member listed in this section is read and checked (with the kind
given, §5.2) wherever it is present in the places described, whether or not
its value ends up in the output.

- **Attributes** (chunk `ImageAttributesLV!`, member `SLxImageAttributes`;
  required):
  - integers `uiWidth`, `uiHeight`, `uiWidthBytes`, `uiComp` (components per
    pixel), `uiBpcInMemory` (all required; `uiWidth`, `uiHeight` and
    `uiComp` at least 1, and `uiComp` at most 1024);
  - number `uiBpcSignificant` (required);
  - integers `eCompression` (default 2), `uiTileWidth` and `uiTileHeight`
    (default 0).

  Other members, such as `uiSequenceCount` and `ePixelType`, are not read.
  - `uiBpcInMemory` 8, 16, 32 gives `uint8`, `uint16`, `float32`; other values
    are rejected.
  - `eCompression` 2 is uncompressed and 0 is lossless (zlib); any other
    value (1 is lossy) is rejected.
  - A tile width or height that is positive and differs from the image's is
    rejected.
- **Experiment** (chunk `ImageMetadataLV!`, member `SLxExperiment`; if the
  chunk or the member is absent there are no loops) is a tree of objects
  (any other value rejects). **Every node of the tree** (the root, and the
  children of every node) is checked, whether or not the flattening below
  visits it: its `eType` MUST be one of those in the table, and its members
  are read as the table says.
  - A **node** has:
    - `eType` (integer, required);
    - `uLoopPars` (object, default absent);
    - `pItemValid` (a list of flags, default absent), a member of the node
      itself, checked on every node;
    - children, the members of `ppNextLevelEx` (object or list, default
      none), each of which MUST be an object.
  - Each node's loop has a **kind** and a count. Its members are in
    `uLoopPars`:

    | `eType` | kind | count, and period or step |
    |---|---|---|
    | 1 | time | integer `uiCount` (default 0); period: number `dPeriod` (ms, default 0) |
    | 8 | time | the members `p` of `pPeriod` (object or list, default none) MUST be objects. The count is the sum of integer `p/uiCount` (required) over the valid `p`. The period is number `p/dPeriod` (default 0) of the first valid `p`, or 0 if none is valid. |
    | 2 | position | the number of valid members of `Points` (object or list, default none). Each valid member `q` MUST be an object; its **stage position** is numbers `q/dPosX` and `q/dPosY` (µm, default absent). |
    | 4 | z | integer `uiCount` (default 0); numbers `dZStep`, `dZLow`, `dZHigh` (default 0); step `abs(dZStep)`, or if that is 0 and the count is more than 1, `abs(dZHigh − dZLow) / (count − 1)` |
    | 6 | (spectral) | integer `uiCount`; if it is absent, integer `pPlanes/uiCount` (`pPlanes` is read only then); if that is absent, 0 |

    For eType 8, `uiCount` and `dPeriod` are read from every valid member,
    and only from those; the sum MUST be at most 2^53 − 1.
  - **Validity:** the `i`-th member of `pPeriod` (of `Points`) is
    **valid** if the list `uLoopPars/pPeriodValid` (for `Points`: the node's
    `pItemValid`, not a member of `uLoopPars`) is absent, or if its `i`-th
    member exists and is true. Every member of a validity list is a flag.

  The tree is flattened into a list of loops
  by visiting nodes depth first, each node before its children. The root has
  depth 0 and children are one deeper than their node, except that the
  children of a spectral node have the spectral node's depth. A loop is its
  kind, depth, count, and period or step. For each node visited, in this
  order:
  1. without `uLoopPars`, or with a count of 0 (spectral nodes included): the
     node and its children are skipped (not visited, though still checked);
  2. spectral: the node is skipped and its children are visited (its planes
     are pixel components, not a loop of the output);
  3. otherwise: if the list is empty or the last loop's depth is less than
     the node's, the node's loop is appended. If the last loop has the same
     depth and kind, and its count is less than the node's count, the node's
     loop (with its period or step) replaces it. Otherwise the node's loop is
     dropped. Either way its children are visited.

  Rule 3 merges sibling branches that repeat one loop. A time loop with two
  position-loop children of 25 and 25 points gives [time, position]; the
  second position loop is dropped, and its children are still visited one
  level deeper. Rule 3 compares only with the last loop: if the first
  position loop had a z-loop child, the last loop is that z loop when the
  second position loop is visited, so the second is dropped whatever its
  count.

  Two loops of the same kind (two time loops, for example) in the final list
  are rejected.
- **Picture metadata** (chunk `ImageMetadataSeqLV|0!`, member
  `SLxPictureMetadata`; if the chunk or the member is absent there are no
  planes and the image is not calibrated):
  - flag `bCalibrated` (default false), number `dCalibration` (default
    absent), number `dAspect` (default 1), numbers `dXPos` and `dYPos` (the
    stage position, default absent);
  - planes: object `sPicturePlanes` (default absent: no planes), with
    integer `uiCount` (default 0) and object `sPlaneNew` (default absent).
    Plane `i` is the member `sPlaneNew/a<i>`, for `i < uiCount`. Each one
    present MUST be an object, with string `sDescription` (default empty),
    color `uiColor` (default `0xFFFFFF`) and integer `uiCompCount`
    (default 1).
  - calibration: the image is **calibrated** if `bCalibrated` is true and
    `dCalibration` is present and positive. A `dAspect` that is not positive
    counts as 1.
  - camera matrix: numbers `dStgLgCT11`, `dStgLgCT12`, `dStgLgCT21`,
    `dStgLgCT22` (defaults 1, 0, 0, 1), the matrix
    `C = [[dStgLgCT11, dStgLgCT12], [dStgLgCT21, dStgLgCT22]]` that maps
    image x and y offsets to stage x and y offsets.

### 5.4 Frames

The frames are numbered `0 ≤ f < N`, where `N` is the product of the loops'
counts (1 if there are none), which MUST be at most 2^53 − 1. Frame `f`'s coordinates are its row-major
index over the loops in list order (the last loop fastest). Frame `f` is the
chunk named `ImageDataSeq|<f>!` (`f` in decimal, without leading zeros); a
frame absent from the chunk map is missing (no entry). Other chunk names,
and frames with `f ≥ N`, are ignored.

A frame's data is an 8-byte timestamp followed by its pixels: `uiHeight`
rows of `uiWidthBytes` bytes, each holding `uiWidth × uiComp` samples
(interleaved by component) of `uiBpcInMemory / 8` little-endian bytes,
then padding. Let `R = uiWidth × uiComp × uiBpcInMemory / 8`;
`uiWidthBytes` MUST be at least `R`.

- **Uncompressed:** the virtualizer MUST read the headers of the present
  frames with the lowest and the highest numbers, and reject the file if
  their name lengths differ, or if either's data length `d` is less than
  `8 + uiHeight × uiWidthBytes`. It reads no other frame's header: it uses
  the first one's name length `n` for every frame, and frame `f`'s pixels
  start at `start = o + 16 + n + 8`, where `o` is its chunk's offset from the
  chunk map. (Other frames' chunks are not checked, except that their ranges
  MUST lie within the file, §1.2. A frame chunk's data need not lie within
  the file; only the ranges taken from it must.) Row `r` of the frame is the range
  `(0, start + r × uiWidthBytes, R)`.
  - If `uiWidthBytes = R`, the frame is one chunk, the single range
    `(0, start, uiHeight × R)`.
  - Otherwise (padded rows) the frame is split into **row blocks** of `h`
    rows: block `j` is the ranges of rows `j × h` to `j × h + h − 1`, in
    order. `h` is the largest divisor of `uiHeight` such that every block of
    every present frame has a payload of at most 65519 bytes (§1.2). (`h = 1`
    always qualifies, so padded frames are never rejected for their payload.)
- **Compressed:** `uiWidthBytes` MUST equal `R`. The virtualizer reads every
  present frame's header; the frame is one chunk, the single range
  `(0, o + 16 + n + 8, d − 8)`, a zlib stream of the pixels, and `d` MUST be
  more than 8.

Let `h` be `uiHeight` except for padded rows, where it is the block height.

### 5.5 Channels

Channels are the components. If `uiCount` is at least 1, every plane
`i < uiCount` exists, every plane's `uiCompCount` is 1 or 3, and they add up to `uiComp`,
the components are labeled plane by plane:

- a plane with one component gives one channel labeled `sDescription`,
  colored by `uiColor`;
- a plane with three components gives three channels labeled
  `<sDescription> R`, `<sDescription> G`, `<sDescription> B`, colored
  `FF0000`, `00FF00`, `0000FF`.

Otherwise channel `k` is labeled `C<k>` and colored `FFFFFF`. A `uiColor` is
`0xAABBGGRR`; its color is the six uppercase hexadecimal digits of red,
green and blue.

### 5.6 Output

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
- **Chunk shape:** 1 for `t` and `z`, `uiComp` for `c`, then `h` (§5.4) and
  `uiWidth`: one frame, or one row block, per chunk.
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
- **Stage positions:** each image also has a translation (§2.2) placing it
  where the stage was, when all of these hold: the image is calibrated;
  every position's stage position is present (with a position loop,
  `dPosX` and `dPosY` of the position loop's `p`-th valid member of
  `Points`, for position `p`; without one, the picture metadata's numbers
  `dXPos` and `dYPos`, default absent); and `d = dStgLgCT11 × dStgLgCT22 −
  dStgLgCT12 × dStgLgCT21` is not 0. Otherwise no image has one. The
  stage position `(sx, sy)` is the centre of the field of view; in image
  coordinates it is
  - `u = (dStgLgCT22 × sx − dStgLgCT12 × sy) / d`,
  - `v = (dStgLgCT11 × sy − dStgLgCT21 × sx) / d`

  (that is, `C⁻¹ (sx, sy)`), and the translation is
  `x = u − uiWidth × sx_scale / 2` and `y = v − uiHeight × sy_scale / 2`,
  where `sx_scale` and `sy_scale` are the x and y scales, and 0 for the other
  axes. (Each expression is computed left to right, products before
  differences.) The position loop is the one in the flattened list (§5.3):
  when rule 3 replaces a loop, the replacing node's `Points` are used.
- **omero:** every image's `M` has
  `"omero": {"channels": [...]}`, one object per channel (even when there is
  no `c` axis):
  `{"label": ..., "color": ..., "active": true, "window": {"min": 0, "max": V, "start": 0, "end": V}}`
  with `V = 2^b − 1`, where `b` is `uiBpcSignificant` if it is an integer
  from 1 to `uiBpcInMemory`, else `uiBpcInMemory`; for `float32` there is no
  `window`.
- **Chunks:** frame `f` at position `p` (0 without a position loop) is the
  entry `<p>/0/c/<coords>` with its ranges (§5.4); coords are its `t`, 0 for
  `c`, its `z` (each only when its axis is present), then 0, 0. With row
  blocks, block `j` is the entry with coords ..., `j`, 0.
