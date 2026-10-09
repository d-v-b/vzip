# The ND2 convention

The Zarr layout of a Nikon ND2 file (format version 3 and later), and the
translation of its metadata chunks into JSON. What all of vzip's
conventions share is in [spec/conventions.md](../conventions.md), cited here as
"conventions §n". How vzip produces this layout as a virtual store is the
ND2 profile, [spec/virtualize/nd2/profile.md](nd2/profile.md).

Convention version: 0 (until release, conventions §1) · UUID: `59612f14-e314-4207-ba00-8f422ba71490` ·
Schema: [schema.json](nd2/schema.json)

This convention gives a layout only to the files that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a file that fails has no layout under this
convention, and the profile rejects it.

## 1. Declaration

The root declares the convention by [conventions §2](../conventions.md#2-attributes),
with `"profile": "nd2"`, `"version": 0`, `"revision": 24` (conventions §1), the file's URL as `source.url`, and
the source metadata of §5 as the member `"nd2"`. Its CMO is:

```json
{
  "uuid": "59612f14-e314-4207-ba00-8f422ba71490",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/heads/main/spec/virtualize/nd2/schema.json",
  "spec_url": "https://github.com/d-v-b/vzip/blob/main/spec/virtualize/nd2.md",
  "name": "vzip_virtualized",
  "description": "The Zarr layout of a ND2 source virtualized by vzip, and the source's metadata"
}
```

`vzip_source` declares it too, with the IR mirror's description (§5.2,
[conventions §8](../conventions.md#8-the-ir-mirror)), and so does each group of
the mirror's view that has a member.

## 2. The source

### 2.1 Chunks

An ND2 file is a sequence of **chunks**, each a 16-byte header (`u32` magic
`0x0ABECEDA`, `u32` name length `n`, `u64` data length `d`, little-endian),
`n` bytes of name, and `d` bytes of data. The file's **chunk map**, at its
end, gives each named chunk's offset; of duplicate names, the last is used.
The profile says how the signature and the map are read
([spec/virtualize/nd2/profile.md §5.1](nd2/profile.md#51-chunks)). Image frames are
the chunks named `ImageDataSeq|<f>!`; the others hold metadata.

### 2.2 Lite variant

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
| 8 | string: UTF-16LE code units up to the first NUL unit, which ends the record |
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

**Text.** A name or a string is **well-formed** when its code units hold
no unpaired surrogate. As this section and §3 read them, a name or a string
is its text with each unpaired surrogate replaced by U+FFFD; the source
metadata keeps them exactly (§5.1).

**Values.** All integers are little-endian. A level with at least one
record, all with empty names, is a **list** of their values. Any other level
(including an empty one) is an **object**: as this section and §3 read it,
its members are its records by name (read as above), in the order of each
name's first appearance, and a repeated name has its last value. The
source metadata keeps every record, repeated names included (§5.1). A
chunk's top-level records always form an object. A byte array is a list of
its bytes; each byte counts as a value of type 3. "The members of" an object
or a list are its values in order.

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

## 3. Metadata

Every member listed in this section is read and checked (with the kind
given, §2.2) wherever it is present in the places described, whether or not
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
    | 8 | time | the members `p` of `pPeriod` (object or list, default none) MUST be objects. The count is the sum of integer `p/uiCount` (required) over the valid `p`. The period is number `p/dPeriod` (default 0) of the valid `p` when they all have the same one, and 0 (no time step) when they differ or none is valid. |
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

## 4. The image

### 4.1 Frames

The frames are numbered `0 ≤ f < N`, where `N` is the product of the loops'
counts (1 if there are none), which MUST be at most 2^53 − 1. Frame `f`'s coordinates are its row-major
index over the loops in list order (the last loop fastest). Frame `f` is the
chunk named `ImageDataSeq|<f>!` (`f` in decimal, without leading zeros); a
frame absent from the chunk map is missing (no entry). The image ignores
other chunk names, and frames with `f ≥ N`; the source metadata keeps them
(§5.3).

### 4.2 Channels

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

### 4.3 Output

The output is a bioformats2raw layout:

- `zarr.json`: the root, a group with attributes
  `{"ome": {"version": "0.5", "bioformats2raw.layout": 3}}`, with the
  convention declared (§1). The images below it do not declare it.
- `OME/zarr.json`: a group with attributes
  `{"ome": {"version": "0.5", "series": ["0", "1", ...]}}`, one series per
  position (one if there is no position loop).
- For each position `p`, `"<p>/zarr.json"`: an image ([conventions §4](../conventions.md#4-images)) with
  `"name": "position <p>"` and one level, the array `"<p>/0"`.

Each array:

- **Axes:** `t` if there is a time loop (even of count 1), `c` if
  `uiComp > 1`, `z` if there is a z loop, then `y`, `x`. Shape: the loops'
  counts, `uiComp`, then `uiHeight`, `uiWidth` (each only for the axes
  present).
- **Chunk shape:** 1 for `t` and `z`, `uiComp` for `c`, then `h` and
  `uiWidth`: one frame, or one row block, per chunk. `h` is `uiHeight`,
  except for uncompressed frames with padded rows (`uiWidthBytes` more than
  a row), which the profile splits into blocks of `h` rows, a divisor of
  `uiHeight` that it chooses to keep each chunk's reference within vzip's
  limits ([spec/virtualize/nd2/profile.md §5.3](nd2/profile.md#53-frames)).
- **Codecs** ([conventions §3](../conventions.md#3-arrays)): transpose when `c` is present (frames are interleaved),
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
- **Stage positions:** each image also has a translation ([conventions §4](../conventions.md#4-images)) placing it
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
  differences.) The position loop is the one in the flattened list (§3):
  when rule 3 replaces a loop, the replacing node's `Points` are used.
- **omero:** every image's `M` has
  `"omero": {"channels": [...]}`, one object per channel (even when there is
  no `c` axis):
  `{"label": ..., "color": ..., "active": true, "window": {"min": 0, "max": V, "start": 0, "end": V}}`
  with `V = 2^b − 1`, where `b` is `uiBpcSignificant` if it is an integer
  from 1 to `uiBpcInMemory`, else `uiBpcInMemory`; for `float32` there is no
  `window`.
- **Chunks:** frame `f` at position `p` (0 without a position loop) is the
  chunk with coords its `t`, 0 for `c`, its `z` (each only when its axis is
  present), then 0, 0. When the z loop's `dZStep` is negative (the stage
  moves down), the z index is flipped: index `k` of the loop is `z = Z − 1 −
  k`, so that z increases with the stage position, as its positive scale
  says; with row blocks, block `j` (rows `j × h` to
  `j × h + h − 1`) has coords ..., `j`, 0. A chunk holds the frame's pixels:
  `uiHeight` rows of `uiWidth × uiComp` samples, interleaved by component,
  of `uiBpcInMemory / 8` little-endian bytes, without the row padding (and
  compressed by zlib when `eCompression` is 0). A frame that the chunk map
  does not list is missing: its chunks are absent (the fill value).

## 5. Source metadata

The root's source metadata `S` is `{"signature": T, "chunks": {...}}`, `T`
being the signature chunk's 64 bytes of data up to the first NUL, as a text
value, and `chunks` the decoded chunks of §5.1 that the root keeps. Everything
the file holds is on `vzip_source`, the file's **IR mirror** (§5.2), from
which it is rebuilt byte for byte.

The chunks are taken in the order of each name's first record in the map
(**map order**), from the offset its last record gives. A chunk takes part
only when its 16-byte header lies within the file and starts with the
magic, and its `d` bytes of data (after its name) lie within the file. A
chunk's name is its bytes up to and including the `!`; where a name is a
key of `chunks`, it is read by
[conventions §6](../conventions.md#6-source-metadata-as-json). Of chunks whose
names read alike, the first in map order is the one `chunks` names.

### 5.1 Decoded chunks

These chunks are decoded, unless the budget below is spent:
- a chunk whose name ends in `LV!` or contains `LV|`, as a lite variant
  (§2.2);
- any other chunk whose name starts with `CustomData|` and that is not a
  **stream's** chunk, as a lite variant, when it passes the **lossless
  test** (below): such a chunk holds a lite variant only by guess, so it is
  decoded only when its JSON keeps every byte of it, up to layout;
- a chunk whose name starts with `CustomDataVar|`, as an **XML variant**
  (below).

A stream's chunk is `CustomData|<ID>!` (its ID in UTF-8) for the IDs every
writer uses without declaring them, `AcqTimesCache`, `AcqTimes2Cache`, `X`,
`Y`, `Z`, `Z1`, `Z2` and `AcqFramesCache`, and for each ID that
`CustomDataVar|CustomDataV2_0!` declares: each member of its
`CustomTagDescription_v1.0` that is an object with a string `ID` and a
`Type` that is the number 2 or 3 (the document, its
`CustomTagDescription_v1.0` and each member read as objects; pairs read as
the object of their names, a repeated name having its last value). The
chunks of a family (`CustomDataSeq|<name>|<i>!`), streams and frames are
not decoded. `CustomDataVar|CustomDataV2_0!` is decoded first; the
others in map order.

A chunk that decodes is a member, named by its name, of the root's
`chunks`, except the events (`ImageEventsLV!`,
`CustomData|ExperimentEventsV1_0!`), the picture metadata of frames after
the first (`ImageMetadataSeqLV|<n>!`, where `<n>` is decimal digits whose
value as an integer is at least 1, leading zeros allowed), and every chunk
whose JSON is larger than 16384 bytes, which are in the mirror only.

**The root's budget.** While the root's `chunks` object (its JSON, with the
size below) is larger than 65536 bytes, the chunk of the root with the
largest member (the size of its name as a JSON string, plus 1, plus the
size of its JSON), and of members of equal size the first in map order,
leaves it (it is in the mirror only). The members of `chunks` are in map
order, and its size counts 2 for the braces, each member's size, and 1
between members. Since each LV record, and each byte of a byte array, is at
least one byte of its chunk's JSON, a producer may stop decoding a chunk's
LV data for the root once it has met more than 16384 of them.

The **size** of a chunk's JSON is the length in bytes of the UTF-8 encoding
of the text that ECMAScript's `JSON.stringify` writes for it, without
whitespace: numbers in the form of ECMAScript's `Number::toString` (`1`,
`2.5`, `1e-7`, `1e+21`), and strings with its escapes (`\"`, `\\`, `\b`,
`\f`, `\n`, `\r`, `\t`, and `\u00XX` for the other code points below
U+0020).

A chunk that does not decode is in the mirror as bytes (§5.3).

**Budget.** The chunks above share a budget of 2^26 bytes (64 MiB), in the
order above. A chunk is **tried** when its `d`, added to the bytes charged
before it, is at most 2^26; a compressed LV record may then inflate to at
most the **limit**, what is left after its `d`. Every chunk tried is
charged, whether or not it decodes: its `d`, plus, for a compressed LV
record, the size of the data it inflates to, or, when its zlib stream does
not inflate within the limit (the stream is invalid, does not end, is
followed by other bytes, or inflates past the limit), the most it could
have inflated to: `1032 × d` bytes (deflate makes at most 1032 bytes of each
byte), capped at the limit. A chunk that is not tried, or that does not
decode, is in the mirror as bytes.

**The lossless test.** A chunk's data passes when it decodes as a lite
variant (§2.2) and its JSON keeps every byte of it, up to **layout**: what a
reader rebuilds from the JSON by the canonical choices below. A compressed
record is layout (its first 12 bytes and its zlib stream, as for every
chunk), so a compressed chunk is tested on the LV structure it
inflates to. Every record, at every depth, MUST then be canonical:
- its name: when the name is empty, the name length `k` is 0 (no units, no
  NUL), and otherwise the name's units followed by one NUL unit, its first
  (`k` is the units plus 1). An empty name of `k ≥ 1` does not pass;
- a bool (type 1): its byte is 0 or 1;
- a binary64 (type 6) that is a NaN: its bits are `0x7FF8000000000000`, the
  NaN that `{"float": "NaN"}` stands for;
- a level (type 11) of `c` records: the `8c` bytes it skips are all zero, or
  are an **offset table**: `c` little-endian `u64`s that are the offsets of
  its records from the start of the level record (its type byte), each
  record's exactly once, in any order. The table is an index of the
  records' positions: layout, and its order is not kept. (Nikon's writer
  orders it by the records' names compared by their UTF-16 code units, and
  records of equal names in no order a reader can tell.)

A reader rebuilds such a chunk's bytes from its JSON, up to layout, given
the record type of each scalar (which the JSON does not keep; the mirror does): it writes each
level's skipped bytes as zero or as an offset table in any valid order (in
name order to match Nikon's writer for distinct names), and compresses the
chunk or not.

**LV values as JSON.**
- **Names.** A well-formed name (§2.2) is its text. Any other name is
  U+0000 followed by the base64 (RFC 4648 §4, with padding) of its UTF-16LE
  code units, up to its first NUL unit. Since a name ends at its first NUL,
  no well-formed name holds U+0000, and the form reads back.
- **Objects.** An object (a level that is not a list, or a chunk's
  top-level records) whose records all have different names (as written
  above) is a JSON object, with a member per record, in order, except that
  it is pairs (below) when it is one record named `utf16`, `int` or
  `float`, which would read as a tag (below), and when a record's name is
  an **array index**: `0`, or a digit 1–9 followed by digits (`1`, `10`,
  but not `01`), which an ECMAScript object would move before the other
  members. An object in which two records have the same name is a JSON
  array of **pairs**, one per record, in order: each pair is the array
  `[name, value]`.
- **Lists.** A list is a JSON array of its members' values, unless every
  member's value is a JSON array of two elements whose first is a string:
  such a list is written as pairs with empty names, `[["", v0], ["", v1],
  ...]`.
- **Reading back.** A chunk's top level is always an object (pairs there
  are an object, even when their names are all empty). Elsewhere, a
  non-empty JSON array whose every element is an array of two elements, the
  first a string, is pairs. Pairs whose names are all empty are a list (an
  object with a repeated name has a name that is not empty); other pairs
  are an object. Any other array is a list or a byte array. A JSON object
  with one member, named `utf16`, `int` or `float`, is a **tag** (below);
  any other JSON object is an object.
- A byte array is a JSON array of its bytes (decoded as one value, however
  long).
- A bool (type 1) is `true` or `false`. A string (type 8) is a JSON string
  when it is well-formed, and otherwise the tag `{"utf16": B}`, where `B`
  is the base64 of its UTF-16LE code units (without the NUL).
- An integer (types 2–5, and 7) from −(2^53 − 1) to 2^53 − 1 is a JSON
  number; any other is the tag `{"int": D}`, where `D` is its value in
  decimal (a `-` sign when negative, no leading zeros), so that it reads
  apart from a string of digits.
- A binary64 (type 6) is a JSON number with its exact value when it is
  finite and not −0, and otherwise the tag `{"float": "NaN"}`,
  `{"float": "Infinity"}`, `{"float": "-Infinity"}` or `{"float": "-0"}`,
  so that it reads apart from a string, and −0 apart from 0 (which
  ECMAScript's `JSON.stringify` writes for both; this replaces the latitude
  of conventions §6).

**XML variants.** A `CustomDataVar` chunk is a UTF-8 XML document whose
element `variant` holds elements, each with a `runtype` attribute and
either a `value` attribute or child elements.
- Its tags are read as the TIFF convention reads OME-XML
  ([TIFF §3](tiff.md#3-ome-xml), tag scan and attribute values).
- The document is its `variant` element as JSON:
  - an element with a `value` attribute is that value;
  - any other element is the object of its child elements, a member per
    child by name, in order; or, when two of its children have the same
    name, a child's name is an array index (as for LV), or it has one child
    named `utf16`, `int` or `float`, the array of pairs `[name, value]`, one
    per child, in order. (A document has no lists, so any array in it is
    pairs.)
- A value is:
  - an integer, if the runtype is `lx_int8` … `lx_int64` or `lx_uint8` …
    `lx_uint64` and the value matches `[+-]?[0-9]+`: its value as a JSON
    number when it is from −(2^53 − 1) to 2^53 − 1, and otherwise the tag
    `{"int": D}` (as for LV, above);
  - a number, if the runtype is `double` or `float` and the value is a
    decimal (TIFF §3): its binary64 value (rounding to nearest), or, when
    that is not finite or is −0, the tag `{"float": "Infinity"}`,
    `{"float": "-Infinity"}` or `{"float": "-0"}`;
  - `true` or `false`, if the runtype is `bool` and the value is that word;
  - otherwise the value as a string.
- A chunk that is not valid UTF-8 does not decode. Neither does one with a
  mismatched or missing end tag, a tag after the `variant` element's end,
  a document element other than `variant`, a `variant` element with a
  `value` attribute, or an element nested more than 100 deep (the `variant`
  element is at depth 0, its children at depth 1).

### 5.2 The IR mirror

`vzip_source` is the file's **IR mirror**
([conventions §8](../conventions.md#8-the-ir-mirror)): its table, from which the
file is rebuilt byte for byte, and its view, `vzip_source/tree`. The IR's
elements (the ND2 **source model**) are below; nothing is left out: every
chunk, the chunk map, frames beyond the image, padding and dead space.

### 5.3 Elements

Paths are relative to the IR's root, whose path is `""`
([conventions §8.1](../conventions.md#81-elements)); `<i>`, `<f>` are decimal name
indexes. Every chunk struct holds `header`
(`{magic:<u4,name_length:<u4,data_length:<u8}`) and `name` (`bytes[n]`).

| path | kind | type | what |
|---|---|---|---|
| `""` | struct | | the root: the whole file, extent `(0, size)` |
| `signature` | struct | | the signature chunk: `header`, `name` (`ascii[32]`), `data` (`ascii[64]`) |
| `map` | struct | | the chunk map: `header`, `name`, `records/<i>` (`{name:bytes[k],offset:<u8,size:<u8}`), `end` (`bytes[32]`), `rest` |
| `tail` | value | `{signature:ascii[32],offset:<u8}` | the file's last 40 bytes |
| `chunks/<name>` | struct | | each chunk of the map (its name's last record) that is not a frame, in map order, named (below) by its name read as text (conventions §6) |
| `chunks/<name>/lv` | struct or derived | | a chunk that decodes as a lite variant (§5.1): its records in the source, or, for a compressed record (derived, transform `nd2-lv-zlib`, its inflated size in its form), in its inflated bytes |
| `chunks/<name>/xml` | derived | | an XML variant (transform `xml-variant`): its elements as structs, its `value` attributes as values of type `xml:<runtype>` |
| `chunks/<name>/data` | value | `bytes[d]` | a chunk that does not decode |
| an LV scalar | value | `{lv:u1,k:u1,name:bytes[2k],v:T}` | named (below) by the record's name as JSON (§5.1); `T` its value's type (`u1` bool, `<i4`, `<u4`, `<u8`, `<f8`, `utf16[n]` string, `{n:<u8,v:bytes[n]}` byte array) |
| an LV level | struct | | named as a scalar is; `header` (`{lv:u1,k:u1,name:bytes[2k],count:<u4,length:<u8}`), its records, and `table` (`bytes[8c]`, the bytes it skips) |
| an XML element | struct or value | `xml:<runtype>` or `xml` | under `chunks/<name>/xml`, named (below) by its tag: an element with a `value` attribute is a value (its attribute's text), any other a struct of its child elements |
| `frames/<f>` | struct | | each chunk `ImageDataSeq|<f>!`: `header`, `name`, and for a placed frame (§4.1) `timestamp` (`<f8`), `pixels` (data: the frame's pixels; its form's recipe the range, or the rows of padded rows) and `trailing` (`bytes`, data after the pixels), else `data` (`bytes[d]`) |
| `gaps/<offset>` | gap | | name padding, replaced map records, row padding and any other bytes no element claims |

A chunk whose bytes an earlier element claims is an alias of it.

**Names.** The chunks under `chunks`, the records of a decoded chunk or of a
level, and the child elements of an XML element take their names from the
source, which may be empty (a list's members), repeat (an object with a
repeated name; chunk names that read alike), or hold `/`; they are named so
that conventions §8.1's rules hold. Among such siblings, in the source's
order (map order for chunks, record order for records, document order for
elements), one whose
text `t` is not empty, holds no `/` or `~`, is not `header` or `table` (in
an LV level, whose own children have those names) and is the first of its
siblings with text `t` is named `t`, with no name index. Any other is named
`t` with each `%`, `/` and `~` percent-encoded (`%25`, `%2F`, `%7E`) and
then `~`, with the number of earlier siblings of text `t` as its name index.
So the members of a list are `~0`, `~1`, …, the second record named
`uiWidth` in a level is `uiWidth~1`, and a chunk `CustomData|a/b!` is
`CustomData|a%2Fb!~0`. The rule is reversible: a name that ends in `~` and
has an index is a text so encoded, any other is its text. The records' own
names are in their `name` fields (and the chunks' in their `name` values),
so the rule loses nothing.

### 5.4 Equivalence

The elements are determined by the file, and the mirror by its elements
(conventions §8.8: one order, one folding, one encoding): hierarchies of one
file are compared entry for entry, `vzip_source` included
([spec/virtualize.md §1.1](../virtualize.md#11-output-and-equivalence)).

## 6. Example

The root of a file at `https://example.org/a.nd2` with a time loop of 3,
a z loop of 4 (with a negative step, so its z index is flipped) and two
channels:

```json
"vzip_virtualized": {
  "profile": "nd2",
  "version": 0,
  "revision": 24,
  "source": {
    "url": "https://example.org/a.nd2"
  },
  "nd2": {
    "signature": "Ver3.0",
    "chunks": {
      "ImageAttributesLV!": {
        "SLxImageAttributes": {
          "uiWidth": 5,
          "uiWidthBytes": 20,
          "uiHeight": 6,
          "uiComp": 2,
          "uiBpcInMemory": 16,
          "uiBpcSignificant": 12,
          "uiSequenceCount": 12,
          "uiTileWidth": 5,
          "uiTileHeight": 6,
          "eCompression": 2,
          "dCompressionParam": -1.0,
          "ePixelType": 1,
          "uiVirtualComponents": 2
        }
      },
      "ImageMetadataLV!": {
        "SLxExperiment": {
          "eType": 1,
          "uLoopPars": {
            "uiCount": 3,
            "dPeriod": 2500.0,
            "dStart": 0.0,
            "dDuration": 0.0
          },
          "ppNextLevelEx": [
            {
              "eType": 4,
              "uLoopPars": {
                "uiCount": 4,
                "dZStep": -0.5,
                "dZLow": 0.0,
                "dZHigh": 1.5
              }
            }
          ]
        }
      },
      "ImageMetadataSeqLV|0!": {
        "SLxPictureMetadata": {
          "dCalibration": 0.25,
          "dAspect": 1.0,
          "bCalibrated": true,
          "sPicturePlanes": {
            "uiCount": 2,
            "uiCompCount": 2,
            "sPlaneNew": {
              "a0": {
                "sDescription": "DAPI",
                "uiColor": 16711680,
                "uiCompCount": 1
              },
              "a1": {
                "sDescription": "GFP",
                "uiColor": 65280,
                "uiCompCount": 1
              }
            }
          }
        }
      }
    }
  }
}
```

Its `vzip_source` is the IR mirror (§5.2), whose elements hold every chunk,
the frames' timestamps (`frames/<f>/timestamp`) among them.
