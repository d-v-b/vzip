# NDPI

The Zarr layout of a Hamamatsu NDPI slide, and the translation of its tags
into JSON. NDPI is a variant of TIFF; this convention builds on the
[TIFF convention](tiff.md). What all of vzip's conventions share is
in [spec/conventions.md](../conventions.md), cited here as "conventions §n".
How vzip produces this layout as a virtual store is the NDPI profile,
[Part 2](#part-2-the-profile) below.

Convention version: 0 (until release, conventions §1) · UUID: `6cac71ef-dbb2-4acd-b60c-00389aa4238a` ·
Schema: [schema.json](ndpi/schema.json)

This document has two parts. [Part 1](#part-1-the-convention) is the NDPI
convention: the Zarr layout, cited as "the convention §n". [Part 2](#part-2-the-profile)
is the NDPI profile: how vzip reads the source, which inputs it rejects, and how
each chunk references the source. The profile keeps its numbering as a section
of [spec/virtualize.md](../virtualize.md), and is cited as spec/virtualize.md §n.

# Part 1. The convention

This convention gives a layout only to the files that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a file that fails has no layout under this
convention, and the profile rejects it.

## 1. Declaration

The root declares the convention by [conventions §2](../conventions.md#2-attributes),
with `"profile": "ndpi"`, `"version": 0`, `"revision": 24` (conventions §1), the file's URL as `source.url`,
and the source metadata of §5 as the member `"ndpi"`. Its CMO is:

```json
{
  "uuid": "6cac71ef-dbb2-4acd-b60c-00389aa4238a",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/heads/main/spec/virtualize/ndpi/schema.json",
  "spec_url": "https://github.com/d-v-b/vzip/blob/main/spec/virtualize/ndpi.md",
  "name": "vzip_virtualized",
  "description": "The Zarr layout of a NDPI source virtualized by vzip, and the source's metadata"
}
```

`vzip_source` and the group of each IFD under it declare it too, with their
source metadata (§5); the arrays and the other groups under `vzip_source`
have none.

## 2. The source

An NDPI file is a little-endian classic TIFF (magic 42) whose IFDs have
64-bit offsets ([spec/virtualize.md §4](#4-ndpi-profile)
says how they are read). Its **IFDs** are those of its main chain, in
order. Of duplicate tags in an IFD, the first is used.

The layout uses these tags, besides ImageWidth (256), ImageLength (257), BitsPerSample (258), Compression (259), PhotometricInterpretation (262) and SamplesPerPixel (277), whose defaults are the [TIFF convention's](tiff.md#2-the-source); field types and counts are as the profile checks them:

| tag | name | kind | type | default |
|---|---|---|---|---|
| 273, 279 | StripOffsets, StripByteCounts | scalar | integer | required |
| 282, 283 | XResolution, YResolution | scalar | RATIONAL (5) | absent |
| 296 | ResolutionUnit | scalar | integer | none |
| 65420 | NDPI format flag | scalar | integer | required |
| 65421 | Magnification | scalar | FLOAT (11) or DOUBLE (12) | required |
| 65422, 65423 | X, YOffsetFromSlideCenter | scalar | SHORT, LONG, SSHORT, SLONG (3, 4, 8, 9) | absent |
| 65426 | McuStarts | array | integer | absent |
| 65432 | McuStartsHighBytes | array | integer | absent |


## 3. Levels

**Levels.** The levels are the main-chain IFDs whose Magnification is
positive, in chain order. Each MUST have Compression 7,
PhotometricInterpretation 6, SamplesPerPixel 3, BitsPerSample 8 and one
strip (StripOffsets and StripByteCounts of one value each), which MUST lie
within the file and be at least 4 bytes long (a JPEG stream's SOI and EOI),
and each MUST be strictly smaller in both width and length than the
one before. If two levels have the same Magnification (focal planes), the
input is rejected. Other IFDs (the macro image, the slide map) are ignored.
The array has axes `c`, `y`, `x`, data type `uint8`, and codecs
`transpose` and `imagecodecs_jpeg` ([conventions §3](../conventions.md#3-arrays)).


## 4. The image

The hierarchy is one image at the root ([conventions §4](../conventions.md#4-images)),
without a name, with one array per level at path `"<level>"`.

**Scale.** With XResolution `rx` and a ResolutionUnit tag of 3
(centimeter), let `p = 10000 / rx`; of 2 (inch), `p = 25400 / rx`. If `p`
is less than 25.4, `x` has unit `micrometer` and level 0 scale `p`;
otherwise, and without a ResolutionUnit tag, no unit and scale 1, as in the
TIFF convention §4.4. `y` likewise from YResolution.
Level `L`'s scale is level 0's times `(W0 / WL)` (x) or `(H0 / HL)` (y), as
in the TIFF convention §4.4.

**Position.** When `x` and `y` have units and level 0's IFD has
XOffsetFromSlideCenter (65422) and YOffsetFromSlideCenter (65423), in
nanometres, the image's centre is at `(X / 1000, Y / 1000)` micrometres,
which gives its translation ([conventions §5](../conventions.md#5-units)).

**Chunks.** A level's strip is a JPEG stream. Without McuStarts, the
level is one chunk, the whole image (chunk shape `[3, H, W]`), which holds
the strip. With McuStarts, the strip is a sequence of restart
**intervals**, each `R` MCUs of `mw × mh` pixels wide and one MCU tall, in
row-major order, `q` per row and `r` rows (the profile defines them from
the strip's JPEG header); a chunk is `a × b` intervals, `a` =
`min(q, max(1, floor(1024 / (R × mw))))` across and `b` down, so the chunk
shape is `[3, b × mh, a × R × mw]`. (`b` is chosen by the profile, as the
largest that keeps every chunk's reference within vzip's limits.) Chunk
`(u, v)` (row `u`, column `v`), at `<level>/c/0/<u>/<v>`, holds a complete
JPEG stream of those intervals, which decodes to the chunk's pixels; at the
image's bottom and right edges, the pixels outside the array are
unspecified. Every chunk is present.

A chunk's stream holds the strip's header with a frame header (SOF0) of its
own size, its intervals, and restart markers and an EOI of its own, so:
- the chunk's width `a × R × mw` and height `b × mh` MUST each be at most
  65535, the largest a frame header holds, else the input is rejected;
- the strip's SOF0 segment is kept in the source metadata (§5);
- the strip's own restart markers and final EOI are not kept (§5): the
  hierarchy takes them to be the standard sequence that every valid JPEG
  stream has.

## 5. Source metadata

The root has no source metadata member. `vzip_source`'s is
`{"ifd_count": n}`, the number of IFDs in the main chain, and each IFD
recorded is a group under it, `ifds/<i>` for main-chain IFD `i`, with the
IFD object as its source metadata, and the strips of the IFDs that are not
levels (the macro image, the slide map), as the
[TIFF convention §5](tiff.md#5-source-metadata) says, with
these differences:
- the main-chain IFDs are the only IFDs of [the TIFF convention
  §2](tiff.md#2-the-source): an NDPI's SubIFDs tag (330) is
  followed like the other pointer tags, and the IFDs that all pointer tags
  lead to are read in NDPI's layout, as the profile reads the main chain:
  an IFD is read when its offset is at least 16 and its entry count,
  entries, 8-byte next-IFD offset and high words (one per entry) lie within
  the file;
- the pointer tags that are not SubIFDs or the three known ones are those
  of type IFD or IFD8 that are not in the table of §2;
- an inline value of count 1 and type LONG (4) or IFD (13) is
  `low + h × 2^32`, its 4 bytes plus the entry's high word `h`, and an
  out-of-line value is at offset `low + h × 2^32`, as the profile reads
  them. Each IFD's strip uses the 64-bit StripOffsets the same way, and so
  do the pointer tags' values;
- the `field` of a tag whose field type TIFF does not define is 8 bytes:
  the entry's value field, then its high word;
- McuStarts (65426) and McuStartsHighBytes (65432), which locate the
  strips' restart markers, are layout tags too;
- the extent of an IFD (for the pointer tags' IFDs) is its entry count,
  entries, next-IFD offset and high words: `2 + 16 n` bytes for `n`
  entries, plus 8;
- the IFD object of a level with McuStarts has the member `sof0`: the
  base64 (conventions §6) of its strip's SOF0 segment, from its `FF C0`
  marker to its end, which the chunks replace with their own.

Besides what the TIFF convention leaves out, these are not kept:
- the high word of an inline value other than a LONG or IFD of count 1
  (it is not used);
- the restart markers and final EOI of a McuStarts level's strip: the 2
  bytes before interval `i` (for `i ≥ 1`), taken to be the restart marker
  `FF D0 + ((i − 1) mod 8)`, and the strip's last 2 bytes, taken to be
  `FF D9`, as in every valid JPEG stream. They are not read, so a strip
  whose markers differ is rebuilt in this standard form.

## 6. Example

The first IFD (level 0) of a slide at `https://example.org/slide.ndpi`, the
group `vzip_source/ifds/0`:

```json
{
 "ndpi": {
  "tags": {
   "256": {
    "type": 4,
    "count": 1,
    "value": [
     1280
    ]
   },
   "257": {
    "type": 4,
    "count": 1,
    "value": [
     1200
    ]
   },
   "258": {
    "type": 3,
    "count": 3,
    "value": [
     8,
     8,
     8
    ]
   },
   "259": {
    "type": 3,
    "count": 1,
    "value": [
     7
    ]
   },
   "262": {
    "type": 3,
    "count": 1,
    "value": [
     6
    ]
   },
   "277": {
    "type": 3,
    "count": 1,
    "value": [
     3
    ]
   },
   "282": {
    "type": 5,
    "count": 1,
    "value": [
     [
      5477,
      1
     ]
    ]
   },
   "283": {
    "type": 5,
    "count": 1,
    "value": [
     [
      5477,
      1
     ]
    ]
   },
   "296": {
    "type": 3,
    "count": 1,
    "value": [
     3
    ]
   },
   "65420": {
    "type": 4,
    "count": 1,
    "value": [
     1
    ]
   },
   "65421": {
    "type": 11,
    "count": 1,
    "value": [
     20.0
    ]
   },
   "65422": {
    "type": 9,
    "count": 1,
    "value": [
     4876667
    ]
   },
   "65423": {
    "type": 9,
    "count": 1,
    "value": [
     -2340000
    ]
   }
  },
  "sof0": "/8AAEQgEsAUAAwARAAERAQIRAQ=="
 }
}
```

Its `sof0` is the strip's frame header: 1200 rows of 1280 pixels, three
components, each sampled 1 × 1.

`vzip_source`'s is `{"ndpi": {"ifd_count": 4}}`. Its fourth IFD, the macro
image, is not a level: its strip is the array `vzip_source/ifds/3/data`.

# Part 2. The profile

The NDPI profile of [spec/virtualize.md](../virtualize.md) (revision 16), numbered
as its §4. It builds on the TIFF profile (§3, [tiff.md](tiff.md#part-2-the-profile)). §1 and §2
are in spec/virtualize.md and apply here.

The output has the convention's layout for the input
([spec/virtualize.md §2](../virtualize.md#2-the-zarr-layout)); this profile says
how the file is read, which inputs are rejected, and how each chunk
references the file. "The convention §n" below is a section of Part 1.

## 4. NDPI profile

Hamamatsu NDPI files are little-endian classic TIFFs (magic 42) with a 64-bit
extension and pyramids stored as single JPEG strips. They are read by §3.1
([tiff.md](tiff.md#31-reading)) with the changes below, with the tags of
[the convention §2](#2-the-source) as the
checked tags.

**Detection.** Let `V` be the little-endian `u64` at bytes 4–11 of the
header. The file is NDPI if it is little-endian with magic 42, `16 ≤ V`,
`V + 2 ≤` the file's size, and the IFD at `V`, read as below, has tag 65420
among its first `n` entries (its entry count `n` and the `12 n` bytes of
entries MUST lie within the file for this test; if they do not, the file is
not NDPI). Otherwise it is a TIFF (§3). This test never rejects.

**IFDs.** The main chain starts at `V`. An IFD at `o` is a `u16` entry count
`n`, `n` entries of 12 bytes as in TIFF, then a `u64` next-IFD offset, then
`n` `u32` **high words**, one per entry. An entry's value field is its 4
bytes plus its high word `h`: an out-of-line value is at offset
`low + h × 2^32`, and an inline value of count 1 and type LONG (4) or IFD
(13) is `low + h × 2^32`. Other inline values ignore `h`. Only the main
chain is read for the layout: an NDPI's SubIFDs, like the IFDs of its other
pointer tags, are read in this layout for the source metadata only, and
never reject the input ([the convention
§5](#5-source-metadata)). The limits and checks
of §3.1 apply, with these values read (§3.1, **Values read**): the
Magnification of every main-chain IFD, and every tag of the table of each
level ([the convention §3](#3-levels)); of a
scalar tag, its first value, of McuStarts, McuStartsHighBytes and
BitsPerSample, every value. (A LONG or IFD value of count 1, with its high
word, can exceed 2^53 − 1 too.) No other value is read for the layout, so
the other IFDs' tags have only their field types, counts and places
checked.

**Rejection.** The input is rejected when a check fails, and when the
convention gives it no layout: wherever the convention says that the input
is rejected, or that something MUST hold and it does not.

**Strips.** A level's strip is the JPEG stream `S` = bytes
`[StripOffsets, StripOffsets + StripByteCounts)` of the file, which MUST
lie within the file. StripByteCounts MUST be at least 4, the SOI and EOI
markers of the shortest JPEG stream.

- **Without McuStarts,** the level is one chunk, the whole image (chunk
  shape `[3, H, W]`), with the single range of `S`.
- **With McuStarts,** let `M[i]` be its values, plus `McuStartsHighBytes[i]
  × 2^32` when that tag is present (it MUST then have as many values); they
  are offsets in `S`. `S`'s **header** is its first `M[0]` bytes: a JPEG SOI,
  then marker segments up to and including SOS, which MUST end exactly at
  `M[0]`. The header MUST have exactly one SOF0 (`FF C0`) segment and a DRI
  (`FF DD`) segment with restart interval `R > 0` (in MCUs). The SOF0
  segment's sampling factors give the MCU size `mw × mh`: 8 times the largest
  horizontal and vertical factors. The strip is then a sequence of
  **intervals**, each `R` MCUs wide and one MCU tall, in row-major order:
  `q = ceil(W / (R × mw))` per row and `r = ceil(H / mh)` rows, and McuStarts
  MUST have `q × r` values, increasing, with `M[q × r − 1] < len(S)`.
  Interval `i` is the bytes `[M[i], M[i + 1] − 2)` of `S` (`[M[i], len(S) −
  2)` for the last), dropping the 2-byte restart marker or EOI after it; each
  MUST be non-empty.

**Chunks of a strip with McuStarts.** A chunk is `a × b` intervals: `a` =
`min(q, max(1, floor(1024 / (R × mw))))` intervals across and `b` intervals
down, so the chunk shape is `[3, b × mh, a × R × mw]`. `a × R × mw` MUST be
at most 65535. The 2 bytes of `S` before `M[i]` (for `i ≥ 1`) and its last
2 bytes, its restart markers and EOI, are not read: the chunks put their
own, and the source metadata takes the strip's to be the standard sequence
`FF D0 + ((i − 1) mod 8)` and `FF D9`
([the convention §5](#5-source-metadata)).
`b` is the largest
number from 1 to `min(r, floor(1024 / mh))` (at least 1) for which every
chunk's payload is at most 65519 bytes (§1.2); `b × mh` MUST be at most
65535 (it always is, since `mh ≤ 120`). Chunk `(u, v)` (row `u`,
column `v`) is the JPEG stream:

1. the header up to its SOF0 segment, a shared byte string (§1.2): a range
   of the data source that holds it;
2. a literal SOF0 segment: the header's, with its height set to `b × mh` and
   its width to `a × R × mw`;
3. the rest of the header after the SOF0 segment, a shared byte string: a
   range of the data source that holds it;
4. for `y` from 0 to `b − 1` and, within it, `x` from 0 to `a − 1`: the
   interval at row `min(u × b + y, r − 1)` and column `min(v × a + x, q − 1)`
   (a range of the file), followed, except after the last, by the literal
   restart marker `FF D0 + (t mod 8)`, where `t` counts the intervals placed
   so far from 0;
5. the literal EOI `FF D9`.

Clamping repeats the last row or column of intervals in the chunks at the
image's bottom and right edges; those pixels lie outside the array and are
not read. (Every interval decodes on its own after a restart marker.)
Chunk keys are `<level>/c/0/<u>/<v>`. The references are listed (for
§1.2's order of first use) level by level, and within a level by `u`, then
`v`.

**Data sources.** Pieces 1 and 3 are the same in every chunk of a level,
and are held once, in data sources, so that reading a chunk reads only its
intervals from the file. They sit at the start of the strip, far from most
intervals, so as ranges of the file they would cost a reader a separate
request per chunk. By §1.2's order of first use, the first McuStarts level's
pieces 1 and 3 are sources 1 and 2; a later level adds a source only for a
piece whose bytes differ from every earlier one. (In CMU-1.ndpi every level
shares piece 1, its quantization and Huffman tables, and piece 3, DRI and
SOS, differs by its restart interval.) Both are JPEG markers and tables, not
pixel data (§1.2, **Structure only**).
