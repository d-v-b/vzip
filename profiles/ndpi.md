# Virtualizing NDPI files

The NDPI profile of [VIRTUALIZE.md](../VIRTUALIZE.md) (revision 10), numbered
as its §4. It builds on the TIFF profile (§3,
[tiff.md](tiff.md)). §1 and §2 are in VIRTUALIZE.md and apply here.

## 4. NDPI profile

Hamamatsu NDPI files are little-endian classic TIFFs (magic 42) with a 64-bit
extension and pyramids stored as single JPEG strips. They are read by §3.1
([tiff.md](tiff.md)) with the changes below, and their images by this section instead of §3.2–§3.6.

**Detection.** Let `V` be the little-endian `u64` at bytes 4–11 of the
header. The file is NDPI if it is little-endian with magic 42, `16 ≤ V`,
`V + 2 ≤` the file's size, and the IFD at `V`, read as below, has tag 65420
among its first `n` entries (its entry count `n` and the `12 n` bytes of
entries MUST lie within the file for this test; if they do not, the file is
not NDPI). Otherwise it is a TIFF (§3.1–§3.6). This test never rejects.

**IFDs.** The main chain starts at `V`. An IFD at `o` is a `u16` entry count
`n`, `n` entries of 12 bytes as in TIFF, then a `u64` next-IFD offset, then
`n` `u32` **high words**, one per entry. An entry's value field is its 4
bytes plus its high word `h`: an out-of-line value is at offset
`low + h × 2^32`, and an inline value of count 1 and type LONG (4) or IFD
(13) is `low + h × 2^32`. Other inline values ignore `h`. NDPI files have no
SubIFDs; the limits and checks of §3.1 apply.

Tags used, besides 256, 257, 258, 259, 262 and 277 of §3.1:

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

**Levels.** The levels are the main-chain IFDs whose Magnification is
positive, in chain order. Each MUST have Compression 7,
PhotometricInterpretation 6, SamplesPerPixel 3, BitsPerSample 8 and one
strip, and each MUST be strictly smaller in both width and length than the
one before. If two levels have the same Magnification (focal planes), the
input is rejected. Other IFDs (the macro image, the slide map) are ignored.
The array has axes `c`, `y`, `x`, data type `uint8`, and codecs
`transpose` and `imagecodecs_jpeg` (§2.1).

**Scale.** With XResolution `rx` and ResolutionUnit 3 (centimetre), `x` has
unit `micrometer` and level 0 scale `10000 / rx`; with unit 2 (inch),
`25400 / rx`; otherwise no unit and scale 1. `y` likewise from YResolution.
Level `L`'s scale is level 0's times `(W0 / WL)` (x) or `(H0 / HL)` (y), as
in §3.6.

**Position.** When `x` and `y` have units and level 0's IFD has
XOffsetFromSlideCenter (65422) and YOffsetFromSlideCenter (65423), in
nanometres, the image's centre is at `(X / 1000, Y / 1000)` micrometres,
which gives its translation (§2.3).

**Strips.** A level's strip is the JPEG stream `S` = bytes
`[StripOffsets, StripOffsets + StripByteCounts)` of the file.

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
down, so the chunk shape is `[3, b × mh, a × R × mw]`. `b` is the largest
number from 1 to `min(r, floor(1024 / mh))` (at least 1) for which every
chunk's payload is at most 65519 bytes (§1.2). Chunk `(u, v)` (row `u`,
column `v`) is the JPEG stream:

1. the header up to its SOF0 segment (a range of the file);
2. a literal SOF0 segment: the header's, with its height set to `b × mh` and
   its width to `a × R × mw`;
3. the rest of the header after the SOF0 segment (a range of the file);
4. for `y` from 0 to `b − 1` and, within it, `x` from 0 to `a − 1`: the
   interval at row `min(u × b + y, r − 1)` and column `min(v × a + x, q − 1)`
   (a range of the file), followed, except after the last, by the literal
   restart marker `FF D0 + (t mod 8)`, where `t` counts the intervals placed
   so far from 0;
5. the literal EOI `FF D9`.

Clamping repeats the last row or column of intervals in the chunks at the
image's bottom and right edges; those pixels lie outside the array and are
not read. (Every interval decodes on its own after a restart marker.)
Chunk keys are `<level>/c/0/<u>/<v>`.
