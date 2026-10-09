# Virtualizing NDPI files

The NDPI profile of [spec/virtualize.md](../../virtualize.md) (revision 16), numbered
as its §4. It builds on the TIFF profile (§3, [tiff.md](../tiff/profile.md)). §1 and §2
are in spec/virtualize.md and apply here.

Profile version: 1 · Convention: [spec/virtualize/ndpi.md](../ndpi.md),
version 1. The output has the convention's layout for the input
([spec/virtualize.md §2](../../virtualize.md#2-the-zarr-layout)); this profile says
how the file is read, which inputs are rejected, and how each chunk
references the file. "The convention §n" below is a section of
spec/virtualize/ndpi.md.

## 4. NDPI profile

Hamamatsu NDPI files are little-endian classic TIFFs (magic 42) with a 64-bit
extension and pyramids stored as single JPEG strips. They are read by §3.1
([tiff.md](../tiff/profile.md#31-reading)) with the changes below, with the tags of
[the convention §2](../ndpi.md#2-the-source) as the
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
§5](../ndpi.md#5-source-metadata)). The limits and checks
of §3.1 apply, with these values read (§3.1, **Values read**): the
Magnification of every main-chain IFD, and every tag of the table of each
level ([the convention §3](../ndpi.md#3-levels)); of a
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
([the convention §5](../ndpi.md#5-source-metadata)).
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
