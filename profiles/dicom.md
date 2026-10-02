# Virtualizing DICOM files

The DICOM profile of [VIRTUALIZE.md](../VIRTUALIZE.md) (revision 11), numbered
as its §6. §1 and §2 are in VIRTUALIZE.md and apply here.

## 6. DICOM profile

The input is one DICOM Part 10 file (PS3.10): a 128-byte preamble, `DICM`,
the File Meta Information, then a dataset whose last element read is Pixel
Data. One file is one image with one pyramid level; a whole-slide image's
other levels are other files.

### 6.1 Elements

All integers below are unsigned. An **element** at offset `p` is read in an
**encoding**: explicit or implicit VR, little or big endian.

- **Tag:** a `u16` group and a `u16` element number, in the encoding's byte
  order; the tag is written `(gggg,eeee)`.
- **Items and delimiters:** a tag of group `FFFE` is followed by a `u32`
  length in the encoding's byte order, in every encoding (without a VR); the
  value starts at `p + 8`.
- **Implicit VR:** the tag is followed by a `u32` length; the value starts at
  `p + 8`. The element has no VR.
- **Explicit VR:** the tag is followed by a VR, two bytes that MUST be one
  of `AE AS AT CS DA DS DT FD FL IS LO LT OB OD OF OL OV OW PN SH SL SQ SS
  ST SV TM UC UI UL UN UR US UT UV`, else the input is rejected. For `OB OD
  OF OL OV OW SQ SV UC UN UR UT UV`, two bytes follow that are not read, then
  a `u32` length, and the value starts at `p + 12`; for the others, a `u16`
  length, and the value starts at `p + 8`.

A length of `FFFFFFFF` is **undefined**. An element's header (its 8 or 12
bytes) MUST lie within its **container** (§6.3), and so MUST a value of
defined length: a value that starts at `w` with defined length `n` ends at
`w + n`, where the next element starts.

### 6.2 File

- **Preamble:** the file MUST be at least 132 bytes long and bytes 128–131
  MUST be `DICM` (which is how §1.2 selected this profile).
- **File Meta Information** starts at offset 132, in explicit VR little
  endian. It is the run of elements, from there, whose group (the `u16`
  little-endian at the element's start) is `0002`; it ends at the first
  element whose group is not `0002`, or where fewer than 2 bytes remain in
  the file. Its container is the file. A meta element of undefined length
  rejects the input.
- **Transfer syntax:** the meta element `(0002,0010)` (Transfer Syntax UID)
  is required, and its VR MUST be `UI`. Its value, a string (§6.4), selects
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
  their frames are not a single stream that a codec of §2.1 decodes.
- **Dataset:** the **top-level dataset** starts where the File Meta
  Information ends and is read in the transfer syntax's encoding. Its
  container is the file. The other meta elements, including the group
  length `(0002,0000)`, are walked as elements but not otherwise read.

### 6.3 Datasets and sequences

A **dataset** is a run of elements. The top-level dataset runs until its
first element with tag `(7FE0,0010)` (Pixel Data): that element's header is
read, and the walk stops there. Elements after it are not read. Reaching the
end of the file first (no Pixel Data) rejects the input.

Each element of a dataset is one of these, by the first that applies:

1. **Group `FFFE`:** `(FFFE,E00D)` ends an item of undefined length (below).
   Any other group-`FFFE` tag, and `(FFFE,E00D)` anywhere else, rejects the
   input.
2. **Undefined length.** In explicit VR, the VR MUST be `SQ` or `UN`, or
   (for tag `(7FE0,0010)` only) `OB` or `OW`, else the input is rejected.
   - Tag `(7FE0,0010)` (pixel data nested in an item, for example an icon
     image) is **encapsulated**: its value is a fragment sequence (§6.6),
     whose items are read in the dataset's byte order and are not parsed.
   - Otherwise the element is a **sequence** of undefined length, read in
     the dataset's encoding, except that a VR of `UN` means its items are
     in implicit VR little endian (PS3.5 §6.2.2).
3. **Defined length, tag `(5200,9229)` or `(0028,9110)`** (the two sequences
   §6.4 reads): a sequence of defined length, in the dataset's encoding. In
   explicit VR its VR MUST be `SQ`, else the input is rejected.
4. **Any other defined length:** the value is skipped, whatever its VR (a
   sequence of defined length is not walked).

A **sequence** is a run of **items**. Each item is an element of tag
`(FFFE,E000)`; any other tag where an item is expected rejects the input,
except that:

- a sequence of undefined length ends with the element `(FFFE,E0DD)`, whose
  length MUST be 0. Its items are within the container of the element that
  holds the sequence;
- a sequence of defined length `n` whose value starts at `w` ends exactly at
  `w + n`, which is its items' container.

An item's value is a dataset. With defined length `m` starting at `w`, that
dataset's elements run from `w` to exactly `w + m` (their container). With
undefined length, they run until an element `(FFFE,E00D)`, whose length MUST
be 0, within the item's own container.

**Depth.** The top-level dataset has depth 0, and an item's dataset has the
depth of the dataset holding its sequence, plus 1. An item at a depth above
64 rejects the input.

Of elements with the same tag in a dataset, the first is used; tags are not
otherwise required to be in order. Every element walked is checked as this
section says, whether or not §6.4 reads it.

### 6.4 Attributes

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
| `(7FE0,0010)` | Pixel Data | OB or OW | §6.6 |

Every attribute of the table present where it is read is checked as below,
whether or not its value ends up in the output.

- **VR:** in explicit VR, the element's VR MUST be the table's, else the
  input is rejected. (In implicit VR there is none to check.)
- **Length:** an attribute of the table other than Pixel Data MUST NOT have
  an undefined length (in implicit VR, §6.3 walks such an element as a
  sequence; it is then rejected here).
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
  (§1.3), optionally preceded by `+` or `-`, and is read as a decimal
  integer, which for Number of Frames MUST be from 1 to 2^53 − 1. Otherwise
  the input is rejected.
- **decimals:** the string values. A value is **valid** if it matches
  `[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?` and its binary64
  value (§1.3) is finite. Invalid values never reject the input; they make
  the attribute unusable where §6.7 says so.

An **(FG)** attribute is taken from the Pixel Measures item if it is present
there, else from the top-level dataset. (The choice is made by presence: an
unusable value in the item does not fall back to the top level.)

### 6.5 Image

- **Rows**, **Columns**, **Samples per Pixel**, **Photometric
  Interpretation**, **Bits Allocated**, **Bits Stored** and **Pixel
  Representation** are required. Rows and Columns MUST be at least 1.
- **Samples per Pixel** `S` MUST be 1 or 3. Planar Configuration is required
  when `S` is 3, and MUST then be 0 (interleaved) or 1 (planar); when `S` is
  1 it is not used (but is checked as §6.4 says).
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
  lookup table that no codec of §2.1 applies), native `YBR_*` (YCbCr, or
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

Otherwise the image is `Rows × Columns`, and frame `f` is plane `z = f`.

### 6.6 Frames

Let `B = Bits Allocated / 8` and `F = Rows × Columns × S × B`, a frame's
size. Let `v` be the start of the top-level Pixel Data value and `n` its
length.

**Native.** The Pixel Data length MUST be defined, with `n ≥ N × F` (extra
bytes, such as a padding byte, are ignored). In explicit VR big endian,
Pixel Data with VR `OW` and Bits Allocated 8 is rejected (its bytes are
swapped in 16-bit words). Frame `f` is the range `(0, v + f × F, F)`; when
`S` is 3 and Planar Configuration is 1, sample `s` of the frame is the range
`(0, v + f × F + s × F / 3, F / 3)`.

**Encapsulated.** The Pixel Data length MUST be undefined. Its value is a
**fragment sequence**, read in little endian: items `(FFFE,E000)` of defined
length (undefined rejects the input), ended by `(FFFE,E0DD)` whose length
MUST be 0; any other tag rejects the input. Item headers are 8 bytes, as in
§6.1, and an item's **data** is its value. The first item, at `v`, is the
**Basic Offset Table** (BOT): its length `L` MUST be a multiple of 4, and its
data is `L / 4` `u32` offsets. The **fragments** are the items after it,
starting at `q = v + 8 + L`. A fragment's **position** is the offset of its
item tag minus `q`.

The frames' fragments are found by the first of these that applies.

1. **Extended Offset Table.** If `(7FE0,0001)` or `(7FE0,0002)` is present,
   both MUST be, each with exactly `N` values, and the BOT MUST be empty
   (`L = 0`). Frame `f` is one fragment, whose data is the range
   `(0, q + EOT[f] + 8, EOTL[f])`. The fragments' item headers are not read
   or checked, nor is the end of the sequence.
2. Otherwise every fragment's item header is read, up to and including
   the `(FFFE,E0DD)` that ends the sequence, all within the file. There
   MUST be at least one fragment.
   - **BOT not empty:** `L / 4` MUST equal `N`; the offsets MUST start at 0,
     be strictly increasing, and each MUST be the position of a fragment.
     Frame `f` is the fragments whose positions are from its offset to
     before the next frame's (for the last frame, to the end).
   - **Empty BOT:** if there are `N` fragments, frame `f` is fragment `f`;
     else if `N = 1`, the frame is every fragment; otherwise the input is
     rejected (fragments are not split into frames by their contents).

A frame's **ranges** are, for each of its fragments in order whose data is
not empty, the range `(0, start of data, length of data)`. A frame with no
ranges rejects the input. The ranges are the frame's stream, without item
headers.

**JPEG colour.** For `jpeg` with `S` = 3, the frame's stream is made
explicit about its colour transform, as the TIFF profile does for JPEG tiles
(§3.6): its ranges become `[P, (0, o + 2, m − 2), ...the rest]`, where
`(0, o, m)` is its first range, `m` MUST be more than 2 (the frame's
first 2 bytes, its SOI marker, are dropped), and `P` is the literal range
`FF D8 FF EE 00 0E 41 64 6F 62 65 00 64 00 00 00 00 T`: SOI and an Adobe
APP14 marker with transform `T` = 0 for `RGB` and 1 for `YBR_FULL` and
`YBR_FULL_422`. (Markers of the stream's own, such as JFIF, still apply by
§2.1's conventions.)

Every range MUST lie within the file, and every chunk's reference payload
MUST be at most 65519 bytes (§1.2); otherwise the input is rejected.

### 6.7 Output

One image at the archive root (§2.2), with no `name` and one level, the
array `"0"`.

- **Axes:** `c` if `S` = 3; `z` if the image is not a whole-slide image and
  `N > 1`; then `y`, `x`.
- **Shape:** 3 for `c`, `N` for `z`; then `H`, `W` for a whole-slide image,
  else Rows, Columns.
- **Chunk shape:** for `c`, 1 if the pixel data is native with Planar
  Configuration 1, else 3; 1 for `z`; then Rows, Columns.
- **Codecs** (§2.1): `transpose` when `c` is present and its chunk size is
  3 (interleaved, as every decoded JPEG and JPEG 2000 frame is); then
  `bytes` (with `endian` `little` or `big` by the encoding when Bits
  Allocated is above 8), `imagecodecs_jpeg` or `imagecodecs_jpeg2k`.
- **Scale and units:**
  - `y`, `x`: if Pixel Spacing (FG, §6.4) is **usable**, the scales are its
    first and second values (the spacing between rows, then between
    columns), with unit `millimeter`. It is usable if it has exactly two
    string values, both valid decimals and positive. Otherwise the scales
    are 1 with no unit.
  - `z`: if Spacing Between Slices (FG) has a first value that is a valid,
    positive decimal, that value with unit `millimeter`; otherwise 1 with
    no unit.
  - `c`: scale 1, no unit.
- **Translation:** none. A whole-slide image's origin is given in the slide
  coordinate system, whose axes are rotated or mirrored against the image's
  by Image Orientation (Slide); a cross-sectional image's position is in the
  patient's frame, oriented by Image Orientation (Patient). Neither is a
  translation along the image's own axes in general.
- **omero:** `M` has `"omero": {"channels": [...]}`, one object per channel
  (one when `S` = 1, even though there is no `c` axis):
  `{"label": ..., "color": ..., "active": true, "window": {"min": A, "max": Z, "start": a, "end": e}}`.
  - Labels and colours: `S` = 1 gives `gray`, `FFFFFF`; `S` = 3 gives `R`,
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
- **Chunks:** frame `f` (or sample `s` of it) is the entry `0/c/<coords>`
  with its ranges (§6.6). Coords are: for `c`, the sample `s` when planar,
  else 0; for `z`, `f`; then the frame's tile row and column for a
  whole-slide image, else 0 and 0.

### 6.8 Limits

These are deliberate, and follow from the rules above.

- Only the transfer syntaxes of §6.2. Lossless JPEG, JPEG-LS, RLE, deflate
  and HTJ2K are rejected, as is any file whose frames are not single
  streams that a codec of §2.1 decodes.
- A file is one level. A whole-slide pyramid spans several files (one per
  level); `TILED_SPARSE` images, and those with several focal planes or
  optical paths, are rejected.
- The frames of a multi-frame image that is not a whole-slide image are
  placed along `z`, whatever they are (slices, time points, cine frames).
- With an empty Basic Offset Table, no Extended Offset Table, and a number
  of fragments other than 1 or `N`, the input is rejected rather than split
  at JPEG markers, which would read pixel data.
- Without an Extended Offset Table, every fragment's item header is read:
  on a large encapsulated file, that is many small reads.
- No modality LUT, VOI LUT, palette or overlay is applied to the pixels;
  only the omero window uses the VOI attributes.
- A file whose preamble starts with a TIFF header, as some toolkits write
  it (`II*` and a zero byte), is read by the TIFF profile (§1.2), and is
  usually rejected there.
