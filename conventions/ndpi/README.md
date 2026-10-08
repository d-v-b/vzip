# The NDPI convention

The Zarr layout of a Hamamatsu NDPI slide, and the translation of its tags
into JSON. NDPI is a variant of TIFF; this convention builds on the
[TIFF convention](../tiff/README.md). What all of vzip's conventions share is
in [conventions/README.md](../README.md), cited here as "conventions §n".
How vzip produces this layout as a virtual store is the NDPI profile,
[profiles/ndpi.md](../../profiles/ndpi.md).

Convention version: 1 · UUID: `6cac71ef-dbb2-4acd-b60c-00389aa4238a` ·
Schema: [schema.json](schema.json)

This convention gives a layout only to the files that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a file that fails has no layout under this
convention, and the profile rejects it.

## 1. Declaration

The root declares the convention by [conventions §2](../README.md#2-attributes),
with `"profile": "ndpi"`, `"version": 1`, the file's URL as `source.url`,
and the source metadata of §5 as the member `"ndpi"`. Its CMO is:

```json
{
  "uuid": "6cac71ef-dbb2-4acd-b60c-00389aa4238a",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/tags/virtualize-ndpi-v1/conventions/ndpi/schema.json",
  "spec_url": "https://github.com/d-v-b/vzip/blob/virtualize-ndpi-v1/conventions/ndpi/README.md",
  "name": "vzip_virtualized",
  "description": "The Zarr layout of a NDPI source virtualized by vzip, and the source's metadata"
}
```

No other node declares it: the arrays have no source metadata.

## 2. The source

An NDPI file is a little-endian classic TIFF (magic 42) whose IFDs have
64-bit offsets ([profiles/ndpi.md §4](../../profiles/ndpi.md#4-ndpi-profile)
says how they are read). Its **IFDs** are those of its main chain, in
order; it has no SubIFDs. Of duplicate tags in an IFD, the first is used.

The layout uses these tags, besides ImageWidth (256), ImageLength (257), BitsPerSample (258), Compression (259), PhotometricInterpretation (262) and SamplesPerPixel (277), whose defaults are the [TIFF convention's](../tiff/README.md#2-the-source); field types and counts are as the profile checks them:

| tag | name | kind | type | default |
|---|---|---|---|---|
| 273, 279 | StripOffsets, StripByteCounts | scalar | integer | required |
| 282, 283 | XResolution, YResolution | scalar | RATIONAL (5) | absent |
| 296 | ResolutionUnit | scalar | integer | 2 |
| 65420 | NDPI format flag | scalar | integer | required |
| 65421 | Magnification | scalar | FLOAT (11) or DOUBLE (12) | required |
| 65422, 65423 | X, YOffsetFromSlideCenter | scalar | SHORT, LONG, SSHORT, SLONG (3, 4, 8, 9) | absent |
| 65426 | McuStarts | array | integer | absent |
| 65432 | McuStartsHighBytes | array | integer | absent |


## 3. Levels

**Levels.** The levels are the main-chain IFDs whose Magnification is
positive, in chain order. Each MUST have Compression 7,
PhotometricInterpretation 6, SamplesPerPixel 3, BitsPerSample 8 and one
strip, and each MUST be strictly smaller in both width and length than the
one before. If two levels have the same Magnification (focal planes), the
input is rejected. Other IFDs (the macro image, the slide map) are ignored.
The array has axes `c`, `y`, `x`, data type `uint8`, and codecs
`transpose` and `imagecodecs_jpeg` ([conventions §3](../README.md#3-arrays)).


## 4. The image

The hierarchy is one image at the root ([conventions §4](../README.md#4-images)),
without a name, with one array per level at path `"<level>"`.

**Scale.** With XResolution `rx` and ResolutionUnit 3 (centimetre), `x` has
unit `micrometer` and level 0 scale `10000 / rx`; with unit 2 (inch),
`25400 / rx`; otherwise no unit and scale 1. `y` likewise from YResolution.
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

## 5. Source metadata

The root's source metadata `S` ([conventions §2](../README.md#2-attributes))
holds every tag of every IFD, as the
[TIFF convention §5](../tiff/README.md#5-source-metadata) translates them,
including NDPI's private tags (the magnification, the slide offsets, the
scanner's properties). It is an object with one member:

- `ifds`: an array of the IFDs, in chain order, each an object with only
  `tags`.

Two things differ from the TIFF convention:

- An inline value of count 1 and type LONG (4) or IFD (13) is
  `low + h × 2^32`, its 4 bytes plus the entry's high word `h`, and an
  out-of-line value is at offset `low + h × 2^32`, as the profile reads them.
- McuStarts (65426) and McuStartsHighBytes (65432), which locate the
  strips' restart markers, are structure tags too: they are recorded by
  type and count only.

## 6. Example

The first IFD of a slide at `https://example.org/slide.ndpi` (its level 0):

```json
{"tags": {
  "256": {"type": 4, "count": 1, "value": [1280]},
  "257": {"type": 4, "count": 1, "value": [1200]},
  "258": {"type": 3, "count": 3, "value": [8, 8, 8]},
  "259": {"type": 3, "count": 1, "value": [7]},
  "262": {"type": 3, "count": 1, "value": [6]},
  "273": {"type": 4, "count": 1},
  "277": {"type": 3, "count": 1, "value": [3]},
  "279": {"type": 4, "count": 1},
  "282": {"type": 5, "count": 1, "value": [[5477, 1]]},
  "283": {"type": 5, "count": 1, "value": [[5477, 1]]},
  "296": {"type": 3, "count": 1, "value": [3]},
  "65420": {"type": 4, "count": 1, "value": [1]},
  "65421": {"type": 11, "count": 1, "value": [20.0]},
  "65422": {"type": 9, "count": 1, "value": [4876667]},
  "65423": {"type": 9, "count": 1, "value": [-2340000]},
  "65426": {"type": 4, "count": 1500}
}}
```
