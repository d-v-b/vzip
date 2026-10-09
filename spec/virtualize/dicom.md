# The DICOM convention

The Zarr layout of a DICOM Part 10 file (PS3.10) with native or JPEG or
JPEG 2000 encapsulated pixel data, including whole-slide images, and the
translation of its File Meta Information and dataset into the DICOM JSON
Model. What all of vzip's conventions share is in
[spec/conventions.md](../conventions.md), cited here as "conventions §n". How
vzip produces this layout as a virtual store is the DICOM profile,
[spec/virtualize/dicom/profile.md](dicom/profile.md).

Convention version: 0 (until release, conventions §1) · UUID: `acf17198-e5a5-48d3-8187-22ec4bb40ea5` ·
Schema: [schema.json](dicom/schema.json)

This convention gives a layout only to the files that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a file that fails has no layout under this
convention, and the profile rejects it.

## 1. Declaration

The root declares the convention by [conventions §2](../conventions.md#2-attributes),
with `"profile": "dicom"`, `"version": 0`, `"revision": 24` (conventions §1), the file's URL as `source.url`,
and the source metadata of §5 as the member `"dicom"`. Its CMO is:

```json
{
  "uuid": "acf17198-e5a5-48d3-8187-22ec4bb40ea5",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/heads/main/spec/virtualize/dicom/schema.json",
  "spec_url": "https://github.com/d-v-b/vzip/blob/main/spec/virtualize/dicom.md",
  "name": "vzip_virtualized",
  "description": "The Zarr layout of a DICOM source virtualized by vzip, and the source's metadata"
}
```

No other node declares it: the array has no source metadata.

## 2. The source

The input is one DICOM Part 10 file (PS3.10): a 128-byte preamble, `DICM`,
the File Meta Information, then a dataset whose last element read is Pixel
Data. One file is one image with one pyramid level; a whole-slide image's
other levels are other files.


### 2.1 Transfer syntax

The File Meta Information is the file's group `0002` elements, from offset
132; the top-level dataset follows it and runs until Pixel Data
`(7FE0,0010)`. ([spec/virtualize/dicom/profile.md §6.2](dicom/profile.md#62-file)
says how they are read.)

- **Transfer syntax:** the meta element `(0002,0010)` (Transfer Syntax UID)
  is required, and its VR MUST be `UI`. Its value, a string (§2.2), selects
  the dataset's encoding and the pixel data's form, and MUST be one of:

  | UID | name | encoding | pixel data |
  |---|---|---|---|
  | `1.2.840.10008.1.2` | Implicit VR Little Endian | implicit, little | native |
  | `1.2.840.10008.1.2.1` | Explicit VR Little Endian | explicit, little | native |
  | `1.2.840.10008.1.2.2` | Explicit VR Big Endian (retired) | explicit, big | native |
  | `1.2.840.10008.1.2.4.50` | JPEG Baseline (Process 1) | explicit, little | encapsulated, `jpeg` |
  | `1.2.840.10008.1.2.4.90` | JPEG 2000 (lossless only) | explicit, little | encapsulated, `jpeg2k` |
  | `1.2.840.10008.1.2.4.91` | JPEG 2000 | explicit, little | encapsulated, `jpeg2k` |

  Any other value, including Deflated Explicit VR Little Endian, RLE
  Lossless, the other JPEG processes, JPEG-LS and HTJ2K, rejects the input:
  their frames are not a single stream that a codec of [conventions §3](../conventions.md#3-arrays) decodes.

### 2.2 Attributes

The attributes read are these. Those marked **(FG)** are read from two
places: the first item of `(0028,9110)` (Pixel Measures Sequence) in the
first item of `(5200,9229)` (Shared Functional Groups Sequence) of the
top-level dataset, and the top-level dataset itself. All others are read
only from the top-level dataset. An element with length 0 counts as absent.
Attributes elsewhere (in other items, or deeper) are not read.

| tag | name | VR | kind |
|---|---|---|---|
| `(0008,0016)` | SOP Class UID | UI | string |
| `(0018,0088)` | Spacing Between Slices (FG) | DS | decimals |
| `(0018,1063)` | Frame Time | DS | decimals |
| `(0028,0009)` | Frame Increment Pointer | AT | tag |
| `(0020,9311)` | Dimension Organization Type | CS | string |
| `(0028,0002)` | Samples per Pixel | US | integer |
| `(0028,0004)` | Photometric Interpretation | CS | string |
| `(0028,0006)` | Planar Configuration | US | integer |
| `(0028,0008)` | Number of Frames | IS | integer string |
| `(0028,0010)`, `(0028,0011)` | Rows, Columns | US | integer |
| `(0028,0030)` | Pixel Spacing (FG) | DS | decimals |
| `(0028,0100)` | Bits Allocated | US | integer |
| `(0028,0101)` | Bits Stored | US | integer |
| `(0028,0102)` | High Bit | US | integer |
| `(0028,0103)` | Pixel Representation | US | integer |
| `(0028,1050)`, `(0028,1051)` | Window Center, Window Width | DS | decimals |
| `(0028,1052)`, `(0028,1053)` | Rescale Intercept, Rescale Slope | DS | decimals |
| `(0048,0006)`, `(0048,0007)` | Total Pixel Matrix Columns, Rows | UL | integer |
| `(0048,0302)` | Number of Optical Paths | UL | integer |
| `(0048,0303)` | Total Pixel Matrix Focal Planes | UL | integer |
| `(7FE0,0001)` | Extended Offset Table | OV | 64-bit integers |
| `(7FE0,0002)` | Extended Offset Table Lengths | OV | 64-bit integers |
| `(7FE0,0010)` | Pixel Data | OB or OW | [spec/virtualize/dicom/profile.md §6.5](dicom/profile.md#65-frames) |

Every attribute of the table present where it is read is checked as below,
whether or not its value ends up in the output.

- **VR:** in explicit VR, the element's VR MUST be the table's, else the
  input is rejected. (In implicit VR there is none to check.)
- **Length:** an attribute of the table other than Pixel Data MUST NOT have
  an undefined length (in implicit VR, [spec/virtualize/dicom/profile.md §6.3](dicom/profile.md#63-datasets-and-sequences) walks such an element as a
  sequence; it is then rejected here).
- **tag:** the value is `u16` group and `u16` element pairs in the dataset's
  byte order; its length MUST be a multiple of 4, and the first pair is
  used, as the tag `(group,element)`.
- **integer:** the value is `u16` (US) or `u32` (UL) values in the dataset's
  byte order; its length MUST be a multiple of 2 (4), and the first value
  is used.
- **64-bit integers:** `u64` values in the dataset's byte order; the length
  MUST be a multiple of 8, and every value MUST be at most 2^53 − 1.
- **string:** the value's bytes, split at each `\` (byte `5C`) into string
  values, each with its leading and trailing bytes `20` (space) and `00`
  removed. The first is used, as bytes (compared byte for byte with the
  ASCII text given here).
- **integer string:** the first string value MUST be one or more digits
  ([spec/virtualize.md §1.3](../virtualize.md#13-arithmetic)), optionally preceded by `+` or `-`, and is read as a decimal
  integer, which for Number of Frames MUST be from 1 to 2^53 − 1. Otherwise
  the input is rejected.
- **decimals:** the string values. A value is **valid** if it matches
  `[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?` and its binary64
  value ([spec/virtualize.md §1.3](../virtualize.md#13-arithmetic)) is finite. Invalid values never reject the input; they make
  the attribute unusable where §4 says so.

An **(FG)** attribute is taken from the Pixel Measures item if it is present
there, else from the top-level dataset. (The choice is made by presence: an
unusable value in the item does not fall back to the top level.)

## 3. The image

- **Rows**, **Columns**, **Samples per Pixel**, **Photometric
  Interpretation**, **Bits Allocated**, **Bits Stored** and **Pixel
  Representation** are required. Rows and Columns MUST be at least 1.
- **Samples per Pixel** `S` MUST be 1 or 3. Planar Configuration is required
  when `S` is 3, and MUST then be 0 (interleaved) or 1 (planar); when `S` is
  1 it is not used (but is checked as §2.2 says).
- **Number of Frames** `N` defaults to 1.
- **Bits:** Bits Stored MUST be from 1 to Bits Allocated. High Bit, when
  present, MUST be Bits Stored − 1. Pixel Representation MUST be 0
  (unsigned) or 1 (signed).
- **Data type:** `uint` (Pixel Representation 0) or `int` (1), then Bits
  Allocated. Bits Allocated MUST be 8, 16 or 32 for native pixel data, 8
  for `jpeg`, and 8 or 16 for `jpeg2k`. For `jpeg`, Bits Stored MUST be 8 and
  Pixel Representation 0.
- **Cells:** native pixels are read as whole cells of Bits Allocated bits.
  When Bits Stored is less, the cells are not masked or sign-extended: the
  output equals the stored values only when the unused high bits are 0
  (unsigned) or copies of the sign bit (signed), as writers produce them.
- **Photometric Interpretation** (a string) and `S` MUST be one of:

  | pixel data | `S` = 1 | `S` = 3 |
  |---|---|---|
  | native | `MONOCHROME1`, `MONOCHROME2` | `RGB` |
  | `jpeg` | `MONOCHROME1`, `MONOCHROME2` | `RGB`, `YBR_FULL`, `YBR_FULL_422` |
  | `jpeg2k` | `MONOCHROME1`, `MONOCHROME2` | `RGB`, `YBR_ICT`, `YBR_RCT` |

  Anything else is rejected: `PALETTE COLOR` (its values are indices into a
  lookup table that no codec of [conventions §3](../conventions.md#3-arrays) applies), native `YBR_*` (YCbCr, or
  subsampled, as stored), and the combinations a JPEG or JPEG 2000 decoder
  does not turn into RGB. Every accepted three-sample image decodes to RGB.

**Whole-slide images.** The file is a **whole-slide image** if its SOP Class
UID is `1.2.840.10008.5.1.4.1.1.77.1.6` (VL Whole Slide Microscopy Image
Storage). Then:

- Dimension Organization Type MUST be `TILED_FULL` (`TILED_SPARSE`, whose
  frames are placed by per-frame positions, and an absent value are
  rejected);
- Total Pixel Matrix Columns `W` and Rows `H` are required and MUST be at
  least 1;
- Number of Optical Paths and Total Pixel Matrix Focal Planes default to 1
  and MUST be 1;
- the frames are tiles of `Rows × Columns` in row-major order: with
  `TC = ceil(W / Columns)` and `TR = ceil(H / Rows)`, `N` MUST equal
  `TR × TC`, and frame `f` is tile row `f div TC`, column `f mod TC`.

Otherwise the image is `Rows × Columns`, and frame `f` is index `f` of the
**frame axis**: `t` (time) if the Frame Increment Pointer is `(0018,1063)`
(Frame Time) or `(0018,1065)` (Frame Time Vector), as in cine ultrasound,
angiography and fluoroscopy; otherwise `z` (space).

## 4. Output

One image at the archive root ([conventions §4](../conventions.md#4-images)), with no `name` and one level, the
array `"0"`.

- **Axes:** `c` if `S` = 3; the frame axis (§3), `t` or `z`, if the image
  is not a whole-slide image and `N > 1`; then `y`, `x`.
- **Shape:** 3 for `c`, `N` for the frame axis; then `H`, `W` for a
  whole-slide image, else Rows, Columns.
- **Chunk shape:** for `c`, 1 if the pixel data is native with Planar
  Configuration 1, else 3; 1 for the frame axis; then Rows, Columns.
- **Codecs** ([conventions §3](../conventions.md#3-arrays)): `transpose` when `c` is present and its chunk size is
  3 (interleaved, as every decoded JPEG and JPEG 2000 frame is); then
  `bytes` (with `endian` `little` or `big` by the encoding when Bits
  Allocated is above 8), `imagecodecs_jpeg` or `imagecodecs_jpeg2k`.
- **Scale and units:**
  - `y`, `x`: if Pixel Spacing (FG, §2.2) is **usable**, the scales are its
    first and second values (the spacing between rows, then between
    columns), with unit `millimeter`. It is usable if it has exactly two
    string values, both valid decimals and positive. Otherwise the scales
    are 1 with no unit.
  - `z`: if Spacing Between Slices (FG) has a first value that is a valid,
    positive decimal, that value with unit `millimeter`; otherwise 1 with
    no unit.
  - `t`: if the Frame Increment Pointer is Frame Time and Frame Time's
    first value is a valid, positive decimal `ms`, the scale `ms / 1000`
    with unit `second`; otherwise 1 with no unit (a Frame Time Vector gives
    each frame's own interval, which a scale cannot).
  - `c`: scale 1, no unit.
- **Translation:** none. A whole-slide image's origin is given in the slide
  coordinate system, whose axes are rotated or mirrored against the image's
  by Image Orientation (Slide); a cross-sectional image's position is in the
  patient's frame, oriented by Image Orientation (Patient). Neither is a
  translation along the image's own axes in general.
- **omero:** `M` has `"omero": {"channels": [...]}`, one object per channel
  (one when `S` = 1, even though there is no `c` axis):
  `{"label": ..., "color": ..., "active": true, "window": {"min": A, "max": Z, "start": a, "end": e}}`.
  - Labels and colors: `S` = 1 gives `gray`, `FFFFFF`; `S` = 3 gives `R`,
    `G`, `B` with `FF0000`, `00FF00`, `0000FF`. For `MONOCHROME1` the
    channel also has `"inverted": true`.
  - `A` and `Z` are the stored values' range for Bits Stored `b`: 0 and
    `2^b − 1` when unsigned, `−2^(b−1)` and `2^(b−1) − 1` when signed.
  - `a` and `e` are `A` and `Z`, except when `S` = 1 and there is a **VOI
    window**: Window Center and Window Width are present with valid first
    values `c` and `w`, `w ≥ 1`, and Rescale Slope `m` and Rescale Intercept
    `k` are each absent (taken as 1 and 0) or have a valid first value, with
    `m ≠ 0`. The window, in rescaled units, is converted to stored values:
    `a = (c − w / 2 − k) / m` and `e = (c + w / 2 − k) / m` (`w / 2` first,
    then left to right), swapped if `m < 0`.
- **Chunks:** frame `f` (or sample `s` of it) is the chunk at
  `0/c/<coords>`. Coords are: for `c`, the sample `s` when planar, else 0;
  for the frame axis, `f`; then the frame's tile row and column for a whole-slide
  image, else 0 and 0. Every chunk is present. A chunk holds the frame's
  pixel data: for native pixel data, its `Rows × Columns × S` cells (or one
  sample's, when planar); for encapsulated pixel data, the frame's
  fragments' data, concatenated, which is one JPEG or JPEG 2000 stream (a
  JPEG stream with three samples made explicit about its color transform,
  as [spec/virtualize/dicom/profile.md §6.5](dicom/profile.md#65-frames) says).

## 5. Source metadata

Every element of the file is in the hierarchy, and so are the bytes that are
not elements, except what is **not kept**:
- Pixel Data's frames, which are the image;
- the layout: the group lengths (`gggg,0000`) that are their group's
  length, and the Extended Offset Table `(7FE0,0001)` and its lengths
  `(7FE0,0002)` that the profile reads the frames from (**Layout**, below);
  the item headers and delimiters of sequences and fragments, which
  `pixel_fragments` and `pixel_offset_table` (below) let a reader rebuild;
  one 00 byte that pads native pixel data to an even length; and the
  trailing `20` and `00` bytes (and, by VR, leading `20` bytes) that pad
  text values;
- Data Set Trailing Padding `(FFFC,FFFC)`, anywhere, which is dead space:
  neither the element nor its bytes are kept.

The root's source metadata `S` ([conventions §2](../conventions.md#2-attributes))
has these members:
- `preamble`: present when the 128 bytes before `DICM` are not all zero:
  their base64 encoding;
- `meta`: the File Meta Information, without the members that are
  `vzip_source`'s (below);
- `dataset`: the dataset, at every depth, including private attributes and
  the elements after Pixel Data, without the members that are
  `vzip_source`'s (below);
- `pixel_extra`: present when native Pixel Data has extra bytes (below):
  the path of the array that holds them;
- `pixel_unreferenced`: present when an Extended Offset Table skips bytes
  (below): the path of the family that holds them;
- `pixel_unreferenced_headers`: `true`, present when that family also holds
  the frames' item headers (below);
- `pixel_fragments`: present when a frame of encapsulated pixel data has
  more than one fragment (below): the path of the array that holds the
  fragments' lengths;
- `pixel_offset_table`: `true`, present when the Basic Offset Table of
  encapsulated pixel data is not empty (below);
- `trailing`: present when the bytes after the last element that parses
  (below) are not empty: the path of the array that holds them.

`vzip_source`'s `S` has the members `meta` and `dataset`, each present only
when not empty, which hold the members of the File Meta Information and of
the top-level dataset that would make the root large. A member's **size**
is the length in UTF-8 of what ECMAScript's `JSON.stringify` writes for its
value (no whitespace), and an object's size that of the object. They are
moved, in this order:
1. the top-level dataset's Per-frame Functional Groups Sequence
   `(5200,9230)`, gathered (**Gathered sequences**, below);
2. every other member of `meta` or `dataset` (an attribute, or
   `duplicates`) whose size is more than 16384 bytes;
3. then, while the sizes of the root's `meta` and `dataset` together are
   more than 65536 bytes, the largest member left in either; of members of
   equal size, the first by name (in code-unit order, so `duplicates` after
   the tags), and `meta`'s before `dataset`'s.

The root records nothing of the moved members: the File Meta Information is
the union of the root's `meta` and `vzip_source`'s, and the dataset that of
the two `dataset`s, which have no member in common. `S` is present only
when one of its members is.

The datasets are in the DICOM JSON Model (PS3.18 §F.2), with one addition,
`duplicates`. A dataset is a JSON object with one member per attribute,
named by its tag as eight uppercase hexadecimal digits (`"00280010"`). The
order of the members is not significant
([conventions §6](../conventions.md#6-source-metadata-as-json)). The layout
elements (**Layout**, below) are omitted, as PS3.18 §F.2 asks of group
lengths; every other element is kept, a group length or an offset table
included. Of elements with the same
tag in a dataset, the first is the attribute; the later ones are the
dataset's member `duplicates`, present only when there are any: an array, in
file order, of objects that each have one member, named by the tag, whose
value is the attribute. An attribute is `{"vr": VR}` with, when it has a
value, `"Value"`, `"InlineBinary"` or `"BulkDataURI"` (or, in the items of
a gathered sequence, `"Gathered"`; and, for a gathered sequence,
`"Structures"` in place of `"Value"`: **Gathered sequences**, below), and
`"LittleEndian"` where the next list says:

- **Explicit VR.** `VR` is the element's. Its value is translated by VR:
  - `AE AS CS DA DS DT IS LO PN SH TM UC UI`: the value's bytes are split
    at each `\` (byte `5C`); `LT ST UT UR` are one value. Each value has
    its trailing bytes `20` and `00` removed and, except for `LT ST UT UC
    UR`, its leading bytes `20`; it is then text by
    [conventions §6](../conventions.md#6-source-metadata-as-json) (UTF-8 if
    valid, else ISO 8859-1, so that its bytes are kept: Specific Character
    Set is not applied, and is in the dataset for a reader who decodes the
    bytes). An empty value is `null`. An `IS` value of the form
    `[+-]?[0-9]+` is that integer (conventions §6): with more than 16
    digits without its sign and leading zeros, it is above 2^53 − 1, and
    is the string of those digits (with a leading `-` if negative). A `DS` value that
    matches the pattern of §2.2 is a number when it is one exactly: its
    binary64 value `v` ([spec/virtualize.md §1.3](../virtualize.md#13-arithmetic))
    is finite, and the text's decimal value equals that of the shortest
    decimal string that rounds to `v` (which ECMAScript's
    `Number::toString` and Python's `repr` write). Both are compared as
    decimals, without leading and trailing zeros: `0.1`, `1.10` and
    `-0` are numbers; `9007199254740993` and
    `0.1000000000000000055511151231257827`, which no binary64 value is, are
    not. A value is at most 2^16 bytes (a longer one is an array, below),
    so one whose digits are not all zero and whose exponent has more than 5
    digits without its sign and leading zeros is out of binary64's range
    (above it, or not zero and below every subnormal), and is not a number:
    the exponent is not converted. Any other `DS` or `IS` value stays text. A `PN` value is split at
    its first two `=` into the `Alphabetic`, `Ideographic` and `Phonetic`
    groups, of which the non-empty ones are members of an object (`null` if
    none is).
  - `AT`: each 4 bytes, a `u16` group and a `u16` element in the dataset's
    byte order, as eight uppercase hexadecimal digits.
  - `FL FD SL SS UL US SV UV`: the values, numbers by conventions §6.
  - `OB OD OF OL OV OW UN`: `"InlineBinary"`, the bytes in base64, as
    stored (in the dataset's byte order; an explicit `UN` value in little
    endian, below).
  - `SQ`: `"Value"`, an array of its items' datasets.
  - **Not whole values.** An `AT` or numeric value whose length is not a
    multiple of its value size (4 for `AT`, else that of its data type
    below) is `"InlineBinary"`, its bytes as stored, keeping its VR.
- **Without a stated VR.** An element in implicit VR, or one whose
  explicit VR is `UN`, takes the VR that the **data dictionary** gives its
  tag, and is translated as above with it; if the dictionary gives none, it
  is `UN`, with `"InlineBinary"`. The value of an explicit `UN` element is
  in little endian in every transfer syntax (PS3.5 §6.2.2): in explicit VR
  big endian too, its values (and an array of them) are read in little
  endian, as its items are read in implicit VR little endian (below). The dictionary is
  [python/src/vzip/virtualize/dicom/dictionary.json](../../python/src/vzip/virtualize/dicom/dictionary.json)
  (PS3.6 as pydicom 3.0.2 ships it, written by
  [generate_dictionary.py](dicom/generate_dictionary.py)); it is normative, and a
  new edition of it is a new version of this convention. A tag's VR is,
  by the first that applies:
  - in an odd (private) group, `LO` for elements `0010` to `00FF` (the
    private creators), and none for the others;
  - its entry in `tags` (each VR's tags, as 8 uppercase hexadecimal digits
    in one string);
  - the VR of the first entry of `masks` that matches it, where `x` matches
    any hexadecimal digit (the repeating groups, such as `60xx0010`);
  - none.

  Tags whose registered VR is ambiguous (`US or SS`, `OB or OW`, ...) are
  not in the dictionary, so they stay `UN`: their VR would depend on other
  elements. An element whose dictionary VR is `SQ` and whose length is
  defined is read as a sequence (in implicit VR little endian when the
  element was explicit `UN`, PS3.5 §6.2.2); if that breaks a rule of the
  next paragraph, it is `UN`, with its bytes. An element of undefined
  length is a sequence, `SQ`.

  In explicit VR big endian, an attribute of an explicit `UN` element whose
  value is bytes for a reader to translate (`"InlineBinary"`,
  `"BulkDataURI"` or `"Gathered"`) also has the member `"LittleEndian":
  true`: those bytes are in little endian, not in the dataset's byte order
  (a `uint8` array keeps no byte order, and one gathered column can hold
  both).
- An element of length 0 has no value.
- A sequence of undefined length is `SQ` (also when its VR is `UN`, whose
  items are then in implicit VR little endian).
- **Pixel Data.** The top-level Pixel Data is `{"vr": VR}` (`"OW"` in
  implicit VR), without a value: its value is the image. Pixel Data of
  undefined length nested in an item (an encapsulated icon image) is a
  family of bytes (conventions §7), one member per item of its fragment
  sequence (the offset table first), named in its `BulkDataURI`.

**Character sets.** A dataset's **character set** is given by its first
element `(0008,0005)` (Specific Character Set) of defined length, for the
elements after it; for those before it, and in a dataset without one, it is
the character set in effect for the item's sequence in the dataset that
holds it, which the item thus inherits. The top-level dataset starts with none, and the File
Meta Information has none. It is **multibyte** if one of its values (its
bytes split at `5C`, each without leading and trailing bytes `20` and `00`)
is `GB18030`, `GBK`, `ISO 2022 IR 58`, `ISO 2022 IR 87`, `ISO 2022 IR 149` or
`ISO 2022 IR 159`: the character sets in which the bytes `5C` (`\`) and `3D`
(`=`) occur inside a character. Under a multibyte character set, a value of
VR `LO`, `PN`, `SH` or `UC` (the VRs that are split at `5C`, `PN` also at
`3D`, and to which the character set applies) is `"InlineBinary"`: its
bytes as stored, not split, trimmed or decoded, which a reader decodes with
the character set. Other values are translated as above: the other split
VRs hold only ASCII, and `LT ST UT` are not split, so that their bytes are
kept by conventions §6.

**Large values.** A value that is too large for JSON is an array of
`vzip_source` (conventions §7), and the attribute is `{"vr": VR,
"BulkDataURI": U}`, where `U` is the array's path from the root of the
hierarchy (`vzip_source/<path>`); in the items of a gathered sequence,
every value is gathered instead (below):
- a binary VR (`OB OD OF OL OV OW UN`) whose value is more than 64 bytes,
  as `uint8`, `float64`, `float32`, `uint32`, `uint64` or `uint16`, in the
  dataset's byte order (`uint8` if the length is not a multiple of the
  value size);
- an `AT` or numeric value that is not whole values and is more than 64
  bytes, as `uint8`;
- a numeric VR (`FL FD SL SS UL US SV UV`) with more than 64 values, of its
  data type (`float32`, `float64`, `int32`, `int16`, `uint32`, `uint16`,
  `int64`, `uint64`), in the dataset's byte order;
- a `DS` or `IS` value with more than 64 values (more than 63 bytes `5C`),
  as `uint8`: its text, as stored;
- a text VR whose value is more than 2^16 bytes, as `uint8`.

A `uint8` array's dimension is `byte`, and any other's is `value`. A binary
value of at most 64 bytes is `"InlineBinary"`, in base64. The array is
referenced where the file holds the value.

**Layout.** These elements are omitted, and every other element but Data Set
Trailing Padding is kept:
- a group length `(gggg,0000)` of defined length, of an even group `gggg`,
  that is the only element of its tag in its dataset, whose VR is `UL`
  (or that is in implicit VR) and whose value is 4 bytes, when its value is
  the length of its **run**: the bytes from its end to the start of the
  next element of its dataset whose group is not `gggg` (or to the end of
  the dataset's elements: the end of an item's dataset of defined length,
  the start of its item delimiter, the end of the File Meta Information,
  or, for the top-level dataset, the end of the elements after Pixel Data,
  before `trailing`). In the top-level dataset, Pixel Data is an element of
  the run that ends where Pixel Data ends (below). A group length of a
  private (odd) group, one of another VR or length, one whose value is not
  its run's length, and one of a tag that occurs twice in its dataset (all
  of that tag) are kept;
- the Extended Offset Table `(7FE0,0001)` and its lengths `(7FE0,0002)`,
  when the profile reads the frames from them (encapsulated pixel data,
  spec/virtualize/dicom/profile.md §6.5): the first element of each tag in the top-level
  dataset, before Pixel Data. A later element of such a tag is a member of
  `duplicates` (the omitted one is the first). An offset table in native
  pixel data's dataset, in an item, in the File Meta Information or after
  Pixel Data is kept.

Data Set Trailing Padding is not kept either (above).

**Gathered sequences.** A sequence is **gathered** when it is not in an
item of a gathered sequence and it is the top-level Per-frame Functional
Groups Sequence `(5200,9230)` (the sequence at path `dataset/52009230`,
whatever its items), or it has more than 64 items (at any depth of the File
Meta Information or the dataset, a duplicate or an element after Pixel Data
too). An outer sequence of more than 64 items is thus gathered, and the
sequences in its items are not. Let `s` be the sequence's path and `F` its
number of items.

Every value in its items, at any depth (every attribute of defined length
that has a value and is not a sequence, and the bytes of a sequence that
breaks a rule, below; not encapsulated pixel data, which is a family as
above), is gathered with the values of the other items, column-wise, so
that the items' JSON keeps only their structure and the values are arrays.
Each such attribute is `{"vr": VR, "Gathered": true}`. Its value is row (or
member) `i` of the array of `vzip_source` at `s/items/<p>/value`, where `i`
is its item's index and `p` its path within the item (its path, below,
without `s/<i>/`). An attribute without a value is `{"vr": VR}`, and a
sequence is `{"vr": "SQ", "Value": [...]}`, its items written so in turn.
The group `items`, which no path of an item has (an item's path ends in its
index), keeps the arrays apart from the items' paths; the leaf `value`,
which no path of an attribute has (a path's names are tags, indexes and
`duplicates`), keeps the gathered arrays from nesting: one item's `p` can
be a proper prefix of another's, as when an element is a value in one item
and a sequence that holds a value in another, and `<p>/value` is then a
sibling of the group that holds the other's array.

**Structures.** Written so, an item is its **structure**: its attributes'
VRs, its sequences' items, its `duplicates`, and the markers, without its
values; items that differ only in their values have equal structures (as
JSON values). When the sequence has at least one item, its attribute is
`{"vr": "SQ", "Structures": [...]}`, the distinct structures in order of
their first item, and the `int32` array of `vzip_source` at
`s/items/structure`, of shape `[F]`, dimension `index`, holds at index `i`
the index in `Structures` of item `i`'s structure; it holds these values
itself, cut as contiguous values (conventions §7), every chunk copied.
(`structure` is no tag and not `duplicates`, so it is no gathered path.)
The JSON thus grows with the distinct structures, not with the items. A
reader rebuilds item `i` as `Structures[structure[i]]`, in which each
attribute `{"vr": VR, "Gathered": true}` at path `p` within the item takes
as its value row (or member) `i` of `s/items/<p>/value`, read as below. A
gathered sequence without items is `{"vr": "SQ", "Value": []}`.

Each value has **typed values**, of a data type: those of a numeric VR
(`FL FD SL SS UL US SV UV`, its data type above) or a binary VR (`OB OD OF
OL OV OW UN`, its data type above), in the byte order in which it is
stored; for a `DS` value of at most 2^16 bytes whose every value (split
and trimmed as above) is a number by the rule above, those numbers as
`float64`; for an `IS` value of at most 2^16 bytes whose every value is of
the form `[+-]?[0-9]+` and from −2^63 to 2^63 − 1, those integers as
`int64` (both in little endian). A value of another VR (text, `AT`, the
bytes of a sequence), or whose length is not a multiple of its value size,
has as typed values its bytes as stored, as `uint8`. The array of a path is
the first of these that applies:
- when the values' typed values all have the same data type, length and
  byte order, of at most 2^24 bytes: a 2-D array of that data type, of
  shape `[F, m]` (`m` values each), whose row `i` is item `i`'s typed
  values (zero bytes for an item without the value), with dimensions
  `index`, then `byte` (for `uint8`) or `value`;
- otherwise, when the values' bytes as stored all have the same length, of
  at most 2^24 bytes: such a 2-D `uint8` array of those bytes;
- otherwise, a family of bytes (conventions §7) of `F` members, each the
  value's bytes as stored, in its offsets-and-data form (whatever their
  lengths).

So a `uint8` row or a member of a family holds the bytes as stored, which a
reader translates by the item's VR as above (in little endian when the
attribute has `"LittleEndian"`); any other data type holds the values: for
`DS` and `IS`, their numbers.

The chunks of a 2-D array of rows of `b` bytes, `n` of whose `F` rows have
a value: when every item has the value and the file holds the rows one
after another in index order, the array is contiguous values (conventions
§7), cut and referenced so. Otherwise, when at least half the items have
the value (`2n ≥ F`), with `R = max(1, floor(65519 / max(16, b + 8)))` (a
row takes at most `max(16, b + 8)` bytes of a reference payload, as a range
of the file or as bytes of a literal, so that a chunk's reference fits in a
payload, [spec/virtualize.md §1.2](../virtualize.md#12-input)), the array has
`k = ceil(F / R)` chunks of `c = ceil(F / k)` rows; when fewer do
(`2n < F`, **sparse**), `c = 1`, a chunk per row. The chunk shape is
`[c, m]`, and the grid has `ceil(F / c)` chunks. Chunk `q` holds rows
`q × c` to `q × c + c − 1`: each row's range of the file (or, for a `DS` or
`IS` value converted to numbers, its bytes, a literal), the rows of items
without the value, and those past `F`, as zero bytes; ranges adjacent in
the file are one range, and adjacent literals one literal. A chunk none of
whose rows has a value is absent (it reads as zeros), so a sparse array's
rows without a value cost nothing; a chunk whose reference would still be
more than 65519 bytes is copied.

**Bounds.** Let `L` be the sequence's length: its value's length, or, for
a sequence of undefined length, the bytes from its value's start to its
sequence delimiter. A path's **cost** is, for a family, `8 × (F + 1)` plus
its members' lengths; for a 2-D array of rows of `b` bytes, `n × b` when it
is sparse, else `F × b`. A sequence that would be gathered is not, and is
instead its bytes, as a sequence that breaks a rule is (below: `SQ`, or
`UN` for an implicit or `UN` element, by the binary rule; for undefined
length, the `L` bytes above), when:
- its items have more than 1024 distinct paths `p` with a gathered value
  (not counting group lengths that are layout);
- or the size of its `Structures` is more than 65536 bytes;
- or the sum of its paths' costs is more than `16 × L`.

The arrays planned in its items are then dropped. So the gathered JSON
and the number of arrays stay bounded, and the arrays' bytes stay within a
multiple of the sequence's.

**Paths.** An attribute's path is that of its dataset, then its tag:
- the File Meta Information is `meta`, the dataset `dataset`;
- item `i` of a sequence at path `p` is `p/i`;
- the attribute `(0028,3006)` of the dataset is at `dataset/00283006`;
- the `k`-th duplicate (from 0) of a dataset at path `d`, of tag `t`, is at
  `d/duplicates/<k>/<t>`.

**Sequences that the profile does not walk.** The profile checks the
structure of every element it walks:
- every element of the top-level dataset;
- the elements of sequences of undefined length;
- the elements of the two sequences of §2.2
  ([spec/virtualize/dicom/profile.md §6.3](dicom/profile.md#63-datasets-and-sequences)).

The source metadata also walks the other sequences of defined length, by
the same rules. It never rejects the input.

When one of them breaks a rule (an element runs past its container, an
unknown VR, items nested more than 64 deep, a tag other than an item where
one is expected), it is `SQ` with its bytes by the binary rule above:
`{"vr": "SQ", "InlineBinary": B}` when they are at most 64 bytes, else
`{"vr": "SQ", "BulkDataURI": U}`, a `uint8` array (gathered, in the item
of a gathered sequence). A sequence that only the dictionary types (an implicit or `UN`
element) is `UN` instead, by the same rule. Arrays planned inside a
sequence that breaks a rule are dropped with it.

**After Pixel Data.** The elements after the top-level Pixel Data are
read, in the dataset's encoding, as long as each one's header lies within
the file and has a known VR, its tag is not of group `FFFE`, no earlier
element of the top-level dataset has its tag (a duplicate ends them), and,
if its length is defined, its value lies within the file. Such an element
of defined length is translated as in the rest of the dataset: a sequence
that breaks a rule is its bytes, as above, and the elements go on after
it. An element of undefined length is a sequence with its items (an `SQ`
or `UN` element in explicit VR; any other explicit VR ends them), read as
in the rest of the dataset; if it breaks a rule outside the sequences of
defined length that it holds (which keep their bytes instead), it ends
them, since where it ends is not known. The element that ends them is not
translated, and the bytes from its start to the end of the file, if any,
are `trailing`.

Pixel Data ends:
- for native pixel data, after its value;
- for encapsulated pixel data, after its sequence delimiter. With an
  Extended Offset Table, whose fragments the profile does not walk, that is
  after the frame's item that ends last, plus the delimiter if it follows
  there.

**Pixel data that no frame holds.**
- **Native.** With `B`, `F` and `N` as in
  [spec/virtualize/dicom/profile.md §6.5](dicom/profile.md#65-frames), the bytes of
  Pixel Data's value after its `N × F` bytes of frames, unless they are
  none or one byte `00`, are a `uint8` array of `vzip_source` at
  `pixel_extra`.
- **Extended Offset Table.** The profile does not check the frames' item
  headers. When, for every frame `f`, the 8 bytes at `q + EOT[f]` are an
  item header for the frame's data: the tag `(FFFE,E000)` and the length
  `EOTL[f]` (in little endian), the frames' items are the ranges
  `(q + EOT[f], 8 + EOTL[f])` (item header and data). Otherwise, the
  frames' items are their data alone, the ranges `(q + EOT[f] + 8,
  EOTL[f])`, every frame's 8 bytes before its data are kept with the bytes
  between the items, and `pixel_unreferenced_headers` is `true`. The items
  are taken in order of offset (then of length). Member `k`, for `k` from 0
  to `N − 1`, is the bytes from the end of the items before the `k`-th (the
  largest end so far, or `q` for `k = 0`) to the start of the `k`-th, and
  is empty if that start is not after that end. When one member is not
  empty (always, with `pixel_unreferenced_headers`), they are a family of
  bytes (conventions §7) of `N` members at `pixel_unreferenced`. A reader
  rebuilds the fragments as member 0, the first item, member 1, and so on,
  where an item is an item header for the frame's length, then its data,
  or, with `pixel_unreferenced_headers`, its data alone.

**Fragments.** Without an Extended Offset Table, a frame of encapsulated
pixel data is the data of its fragments without their item headers, and of
its empty fragments nothing (spec/virtualize/dicom/profile.md §6.5). When a frame has more
than one fragment (empty ones included), `pixel_fragments` is an `int64`
array of `vzip_source` of shape `[K, 2]`, dimensions `index` and `value`,
where `K` is the number of fragments after the Basic Offset Table: row `j`
is fragment `j`'s frame and the length of its data, in file order. It holds
these values itself, cut as contiguous values (conventions §7), every chunk
copied. `pixel_offset_table` is `true` when the Basic Offset Table is not
empty; its offsets are then the positions of the frames' first fragments
(the profile requires it), which the lengths give. A reader rebuilds the
fragment sequence as the Basic Offset Table's item, then, for each frame in
turn, its stream (for `jpeg` with 3 samples, its first two bytes `FF D8`,
then the stream after the 18 bytes of `P`, spec/virtualize/dicom/profile.md §6.5) cut into
items of the frame's fragments' lengths, in order (one item of the whole
stream without `pixel_fragments`), then the sequence delimiter.

## 6. Example

The root of an explicit VR little endian secondary capture image with a
referenced image sequence and shared functional groups
(`https://example.org/a.dcm`):

```json
"vzip_virtualized": {
  "profile": "dicom",
  "version": 0,
  "revision": 24,
  "source": {
    "url": "https://example.org/a.dcm"
  },
  "dicom": {
    "meta": {
      "00020001": {
        "vr": "OB",
        "InlineBinary": "AAE="
      },
      "00020002": {
        "vr": "UI",
        "Value": [
          "1.2.840.10008.5.1.4.1.1.7"
        ]
      },
      "00020003": {
        "vr": "UI",
        "Value": [
          "1.2.826.0.1.3680043.8.498.54895542129125825857839641277016018109"
        ]
      },
      "00020010": {
        "vr": "UI",
        "Value": [
          "1.2.840.10008.1.2.1"
        ]
      },
      "00020012": {
        "vr": "UI",
        "Value": [
          "1.2.826.0.1.3680043.8.498.1"
        ]
      },
      "00020013": {
        "vr": "SH",
        "Value": [
          "PYDICOM 3.0.2"
        ]
      }
    },
    "dataset": {
      "00080016": {
        "vr": "UI",
        "Value": [
          "1.2.840.10008.5.1.4.1.1.7"
        ]
      },
      "00080018": {
        "vr": "UI",
        "Value": [
          "1.2.826.0.1.3680043.8.498.54895542129125825857839641277016018109"
        ]
      },
      "00080060": {
        "vr": "CS",
        "Value": [
          "OT"
        ]
      },
      "00081110": {
        "vr": "SQ",
        "Value": [
          {}
        ]
      },
      "00081140": {
        "vr": "SQ",
        "Value": []
      },
      "00082112": {
        "vr": "SQ",
        "Value": [
          {
            "00081150": {
              "vr": "UI",
              "Value": [
                "1.2.840.10008.5.1.4.1.1.7"
              ]
            },
            "00081155": {
              "vr": "UI",
              "Value": [
                "1.2.826.0.1.3680043.8.498.72746325067103167969493458029025673857"
              ]
            },
            "0040A170": {
              "vr": "SQ",
              "Value": [
                {
                  "00080100": {
                    "vr": "SH",
                    "Value": [
                      "121327"
                    ]
                  },
                  "00080102": {
                    "vr": "SH",
                    "Value": [
                      "DCM"
                    ]
                  },
                  "00080104": {
                    "vr": "LO",
                    "Value": [
                      "Full fidelity image"
                    ]
                  }
                }
              ]
            }
          },
          {}
        ]
      },
      "00280002": {
        "vr": "US",
        "Value": [
          1
        ]
      },
      "00280004": {
        "vr": "CS",
        "Value": [
          "MONOCHROME2"
        ]
      },
      "00280010": {
        "vr": "US",
        "Value": [
          5
        ]
      },
      "00280011": {
        "vr": "US",
        "Value": [
          3
        ]
      },
      "00280030": {
        "vr": "DS",
        "Value": [
          9.0,
          9.0
        ]
      },
      "00280100": {
        "vr": "US",
        "Value": [
          8
        ]
      },
      "00280101": {
        "vr": "US",
        "Value": [
          8
        ]
      },
      "00280102": {
        "vr": "US",
        "Value": [
          7
        ]
      },
      "00280103": {
        "vr": "US",
        "Value": [
          0
        ]
      },
      "52009229": {
        "vr": "SQ",
        "Value": [
          {
            "00289110": {
              "vr": "SQ",
              "Value": [
                {
                  "00280030": {
                    "vr": "DS",
                    "Value": [
                      0.2,
                      0.3
                    ]
                  }
                }
              ]
            }
          }
        ]
      },
      "7FE00010": {
        "vr": "OB"
      }
    }
  }
}
```
