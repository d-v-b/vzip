# Virtualizing DICOM files

The DICOM profile of [VIRTUALIZE.md](../VIRTUALIZE.md) (revision 16), numbered
as its §6. §1 and §2 are in VIRTUALIZE.md and apply here.

Profile version: 1 · Convention: [conventions/dicom](../conventions/dicom/README.md),
version 1. The output has the convention's layout for the input
([VIRTUALIZE.md §2](../VIRTUALIZE.md#2-the-zarr-layout)); this profile says
how the file is read, which inputs are rejected, and how each chunk
references the file. "The convention §n" below is a section of
conventions/dicom/README.md.

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
  is required, and its VR MUST be `UI`. Its value, a string (the
  convention §2.2), MUST be one of the table of
  [the convention §2.1](../conventions/dicom/README.md#21-transfer-syntax),
  which gives the dataset's encoding and the pixel data's form.
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
     image) is **encapsulated**: its value is a fragment sequence (§6.5),
     whose items are read in the dataset's byte order and are not parsed.
   - Otherwise the element is a **sequence** of undefined length, read in
     the dataset's encoding, except that a VR of `UN` means its items are
     in implicit VR little endian (PS3.5 §6.2.2).
3. **Defined length, tag `(5200,9229)` or `(0028,9110)`** (the two sequences
   the convention §2.2 reads): a sequence of defined length, in the dataset's encoding. In
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

Of elements with the same tag in a dataset, the first is used (the
convention's source metadata keeps the others); tags are not otherwise
required to be in order. Every element walked is checked as this
section says, whether or not the convention §2.2 reads it.

### 6.4 Rejection

The input is rejected when a rule of this profile fails, and when the
convention gives it no layout: wherever its §2–§4 say that the input is
rejected, or that something MUST hold and it does not. The source
metadata (the convention §5) never rejects the input.

### 6.5 Frames

Let `B = Bits Allocated / 8` and `F = Rows × Columns × S × B`, a frame's
size. Let `v` be the start of the top-level Pixel Data value and `n` its
length.

**Native.** The Pixel Data length MUST be defined, its value MUST lie
within the file (`v + n` at most the file's size; this is checked before
anything is computed from `n` or `N`), and `n ≥ N × F` (extra bytes, such
as a padding byte, are not part of a frame: the convention §5 keeps them).
In explicit VR big endian,
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
   `(0, q + EOT[f] + 8, EOTL[f])`. The frames' items, the ranges
   `(q + EOT[f], 8 + EOTL[f])` of each frame's 8 bytes before its data and
   its data, MUST NOT overlap (taken in order of offset, then of length,
   each MUST start at or after the end of the one before it); otherwise the
   input is rejected. The fragments' item headers do not
   decide the frames, nor does the end of the sequence: neither is checked,
   and the input is not rejected for them. The convention §5 reads each
   frame's 8 bytes at `q + EOT[f]`, and keeps them, with the bytes between
   the frames' items, when one is not an item header `(FFFE,E000)` of
   length `EOTL[f]`.
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

**JPEG color.** For `jpeg` with `S` = 3, the frame's stream is made
explicit about its color transform, as the TIFF profile does for JPEG tiles
([tiff.md §3.3](tiff.md#33-chunks)): its ranges become `[P, (0, o + 2, m − 2), ...the rest]`, where
`(0, o, m)` is its first range, `m` MUST be more than 2 and the range's
first 2 bytes MUST be `FF D8`, the SOI marker (they are dropped, and `P`
starts with its own), and `P` is the literal range
`FF D8 FF EE 00 0E 41 64 6F 62 65 00 64 00 00 00 00 T`: SOI and an Adobe
APP14 marker with transform `T` = 0 for `RGB` and 1 for `YBR_FULL` and
`YBR_FULL_422`. (Markers of the stream's own, such as JFIF, still apply by
[conventions §3](../conventions/README.md#3-arrays)'s conventions.)

Every range MUST lie within the file, and every chunk's reference payload
MUST be at most 65519 bytes (§1.2); otherwise the input is rejected.

### 6.6 Chunk references

Frame `f` (or sample `s` of it) is the entry `0/c/<coords>` of
[the convention §4](../conventions/dicom/README.md#4-output), with its ranges (§6.5). Coords are: for `c`, the sample `s` when planar,
else 0; for the frame axis, `f`; then the frame's tile row and column for a
whole-slide image, else 0 and 0.


### 6.7 Limits

These are deliberate, and follow from the rules above.

- Only the transfer syntaxes of the convention §2.1. Lossless JPEG, JPEG-LS, RLE, deflate
  and HTJ2K are rejected, as is any file whose frames are not single
  streams that a codec of [conventions §3](../conventions/README.md#3-arrays) decodes.
- A file is one level. A whole-slide pyramid spans several files (one per
  level); `TILED_SPARSE` images, and those with several focal planes or
  optical paths, are rejected.
- The frames of a multi-frame image that is not a whole-slide image are
  placed along `t` when the Frame Increment Pointer names Frame Time or
  Frame Time Vector, and along `z` otherwise, whatever they are. An
  enhanced image whose frames span several dimensions (slices, echoes,
  b-values) is still one axis.
- With an empty Basic Offset Table, no Extended Offset Table, and a number
  of fragments other than 1 or `N`, the input is rejected rather than split
  at JPEG markers, which would read pixel data.
- Without an Extended Offset Table, every fragment's item header is read:
  on a large encapsulated file, that is many small reads.
- No modality LUT, VOI LUT, palette or overlay is applied to the pixels;
  only the omero window uses the VOI attributes.

A file whose preamble holds a TIFF header, as dual-personality files and
some toolkits write it, is still read by this profile: §1.2 tests for `DICM`
first.
