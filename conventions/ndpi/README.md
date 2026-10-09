# The NDPI convention

The Zarr layout of a Hamamatsu NDPI slide, and the translation of its tags
into JSON. NDPI is a variant of TIFF; this convention builds on the
[TIFF convention](../tiff/README.md). What all of vzip's conventions share is
in [conventions/README.md](../README.md), cited here as "conventions §n".
How vzip produces this layout as a virtual store is the NDPI profile,
[profiles/ndpi.md](../../profiles/ndpi.md).

Convention version: 0 (until release, README §1) · UUID: `6cac71ef-dbb2-4acd-b60c-00389aa4238a` ·
Schema: [schema.json](schema.json)

This convention gives a layout only to the files that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a file that fails has no layout under this
convention, and the profile rejects it.

## 1. Declaration

The root declares the convention by [conventions §2](../README.md#2-attributes),
with `"profile": "ndpi"`, `"version": 0`, `"revision": 23` (README §1), the file's URL as `source.url`,
and the source metadata of §5 as the member `"ndpi"`. Its CMO is:

```json
{
  "uuid": "6cac71ef-dbb2-4acd-b60c-00389aa4238a",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/heads/main/conventions/ndpi/schema.json",
  "spec_url": "https://github.com/d-v-b/vzip/blob/main/conventions/ndpi/README.md",
  "name": "vzip_virtualized",
  "description": "The Zarr layout of a NDPI source virtualized by vzip, and the source's metadata"
}
```

`vzip_source` and the group of each IFD under it declare it too, with their
source metadata (§5); the arrays and the other groups under `vzip_source`
have none.

## 2. The source

An NDPI file is a little-endian classic TIFF (magic 42) whose IFDs have
64-bit offsets ([profiles/ndpi.md §4](../../profiles/ndpi.md#4-ndpi-profile)
says how they are read). Its **IFDs** are those of its main chain, in
order. Of duplicate tags in an IFD, the first is used.

The layout uses these tags, besides ImageWidth (256), ImageLength (257), BitsPerSample (258), Compression (259), PhotometricInterpretation (262) and SamplesPerPixel (277), whose defaults are the [TIFF convention's](../tiff/README.md#2-the-source); field types and counts are as the profile checks them:

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
`transpose` and `imagecodecs_jpeg` ([conventions §3](../README.md#3-arrays)).


## 4. The image

The hierarchy is one image at the root ([conventions §4](../README.md#4-images)),
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
which gives its translation ([conventions §5](../README.md#5-units)).

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
[TIFF convention §5](../tiff/README.md#5-source-metadata) says, with
these differences:
- the main-chain IFDs are the only IFDs of [the TIFF convention
  §2](../tiff/README.md#2-the-source): an NDPI's SubIFDs tag (330) is
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
