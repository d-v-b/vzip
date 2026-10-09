# The TIFF convention

The Zarr layout of a TIFF or BigTIFF file, including OME-TIFF and JPEG-tiled
slides such as Aperio SVS, and the translation of its tags into JSON. What
all of vzip's conventions share is in [conventions/README.md](../README.md),
cited here as "conventions §n". How vzip produces this layout as a virtual
store is the TIFF profile, [profiles/tiff.md](../../profiles/tiff.md).

Convention version: 0 (until release, README §1) · UUID: `48e9ac4e-1156-4a62-955e-20467d9c2700` ·
Schema: [schema.json](schema.json)

This convention gives a layout only to the files that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a file that fails has no layout under this
convention, and the profile rejects it.

## 1. Declaration

The root declares the convention by [conventions §2](../README.md#2-attributes),
with `"profile": "tiff"`, `"version": 0`, `"revision": 24` (README §1), the file's URL as `source.url`,
and the source metadata of §5 as the member `"tiff"`. Its CMO is:

```json
{
  "uuid": "48e9ac4e-1156-4a62-955e-20467d9c2700",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/heads/main/conventions/tiff/schema.json",
  "spec_url": "https://github.com/d-v-b/vzip/blob/main/conventions/tiff/README.md",
  "name": "vzip_virtualized",
  "description": "The Zarr layout of a TIFF source virtualized by vzip, and the source's metadata"
}
```

`vzip_source` declares it too, with the IR mirror's description (§5,
[conventions §8](../README.md#8-the-ir-mirror)), and so does each group of
the mirror's view that has a member; the mirror's arrays have no
attributes.

## 2. The source

The file is a TIFF (magic 42) or BigTIFF (magic 43), in either byte order.
Its **IFDs** are the image file directories of its **main chain**, which
starts at the header's first-IFD offset and ends at a next-IFD offset of 0,
and, for each of them, its **SubIFDs**: the IFDs whose offsets its
`SubIFDs` tag (330) lists, in order, and theirs in turn. Main-chain IFDs
are at **depth** 0 and the SubIFDs of an IFD at depth `d` at depth `d + 1`;
the SubIFDs of an IFD at depth 4 are not read. (A SubIFD's next IFD is not
part of the source.) The main chain MUST have at least one IFD. **IFD 0**
is the first IFD of the main chain. Of duplicate tags in an IFD, the first
is used.

The layout uses these tags (absent tags take the defaults shown); field
types and counts are as the profile checks them
([profiles/tiff.md §3.1](../../profiles/tiff.md#31-reading)):

| tag | name | kind | default |
|---|---|---|---|
| 256, 257 | ImageWidth, ImageLength | scalar | required |
| 258 | BitsPerSample | array | required |
| 259 | Compression | scalar | 1 |
| 262 | PhotometricInterpretation | scalar | none (required for JPEG with 3 samples, §4.3) |
| 266 | FillOrder | scalar | 1 |
| 270 | ImageDescription | text | none |
| 277 | SamplesPerPixel | scalar | 1 |
| 284 | PlanarConfiguration | scalar | 1 |
| 317 | Predictor | scalar | 1 |
| 322, 323 | TileWidth, TileLength | scalar | required for tiled images |
| 324, 325 | TileOffsets, TileByteCounts | array | required for tiled images |
| 330 | SubIFDs | array | none |
| 282, 283 | XResolution, YResolution | scalar | none |
| 296 | ResolutionUnit | scalar | none (2 for TIFF, but see §4.4) |
| 339 | SampleFormat | array | 1 |
| 347 | JPEGTables | bytes | none |
| 530 | YCbCrSubsampling | array | `[2, 2]` (TIFF 6.0) |

**Format.** An IFD's **format** is the tuple (BitsPerSample, SamplesPerPixel,
SampleFormat, PlanarConfiguration, Compression, Predictor). Computing it
requires:

- BitsPerSample, with at least one value, all equal and at least 1;
- SamplesPerPixel at least 1;
- SampleFormat values all equal (when the tag is present it MUST have at
  least one value).

When SamplesPerPixel is 1, PlanarConfiguration is taken as 1. Otherwise it
MUST be 1 or 2. When Compression is 7, the format also includes
PhotometricInterpretation (absent counts as a value of its own). Counts of BitsPerSample and SampleFormat are not otherwise
checked. If any of these fails, the input is rejected. Formats are computed
for IFD 0 (always, even when it is not a plane), for every IFD that becomes
a plane or a level (§4.1, §4.2), and for every candidate of the level scan
(§4.2).

**Size.** An IFD that becomes a plane or a level, or is a candidate of the
level scan, MUST have ImageWidth and ImageLength, and both MUST be at least
1. A tiled one (§4.2) MUST have TileWidth, TileLength, TileOffsets and
TileByteCounts, with TileWidth and TileLength at least 1. An IFD is
**tiled** if it has TileWidth and TileOffsets.

## 3. OME-XML

If IFD 0's ImageDescription has field type ASCII (2), let `D` be its bytes up
to the first NUL. If `D` is valid UTF-8 and the tag scan below finds a start
tag named `OME` in it, `D` is the **OME-XML**, and `X` is its text.

**Tag scan.** `X` is read as a sequence of tags, not as a validated XML
document, in one left-to-right scan: at each `<`, a skipped section (1) if
one starts there, else a tag (2) if the grammar matches, else text; the
scan continues after whichever was found. So a `<!--` inside a tag's
attribute value is part of the value, not a comment.

1. **Skipped sections:** scanning from the start, the first of these to
   begin at each point is skipped, up to and including its end:
   - comments, `<!--` to the next `-->`;
   - CDATA sections, `<![CDATA[` to the next `]]>`;
   - processing instructions, `<?` to the next `?>`;
   - other declarations, `<!` to the next `>`.

   The search for the end starts after the whole start marker (so `<!-->`
   does not end itself). A section without an end runs to the end of `X`.
2. **Tags:** outside skipped sections, a tag is a match of this grammar
   starting at a `<` (whitespace and digits as in [VIRTUALIZE.md §1.3](../../VIRTUALIZE.md#13-arithmetic)):

   ```
   tag    = "<" ["/"] qname *(ws1 attr) [ws] ["/"] ">"
   qname  = [name ":"] name
   name   = 1*(ASCII letter / digit / "_" / "." / "-")
   attr   = aname [ws] "=" [ws] ( DQUOTE *(not DQUOTE) DQUOTE / "'" *(not "'") "'" )
   aname  = 1*(any character except whitespace, "=", "/", ">", DQUOTE, "'", "<")
   ws     = 1*whitespace ; ws1 is the same
   ```

   A `<` where the grammar does not match is text. Scanning continues after
   the end of each tag.
3. A tag is a **start tag** if it does not begin with `</`, and an **end
   tag** if it does. It is **self-closing** if it ends with `/>`. Its
   **name** is the last `name` of its `qname`, without the namespace prefix.
   Attribute names are compared exactly. Of duplicate attribute names in a
   tag, the first is used.
4. **Attribute values** have references decoded: the five XML predefined
   entities (`&lt;` `&gt;` `&amp;` `&quot;` `&apos;`), and numeric character
   references (`&#` digits `;` or `&#x` hex digits `;`) that denote a Unicode
   scalar value other than U+0000. Anything else, including other entity
   references and references to U+0000, surrogates or values above
   U+10FFFF, is left as it is. Whitespace is not normalized.

From `X` the virtualizer uses:

- the `Name` attribute of the first `Image` start tag (the image name);
- the attributes of the first `Pixels` start tag:
  - `SizeZ`, `SizeC`, `SizeT` and `DimensionOrder`;
  - `PhysicalSizeX/Y/Z` and `PhysicalSizeX/Y/ZUnit`;
- the first `Plane` start tag after that `Pixels` start tag and before the
  first `Pixels` end tag after it whose `TheZ`, `TheC` and `TheT` are each
  absent or `0` (as integer attributes): its `PositionX`, `PositionY`,
  `PositionXUnit` and `PositionYUnit` (the stage position of the plane);
- the `TiffData` start tags after that `Pixels` start tag and before the
  first `Pixels` end tag after it (or the end of `X`), in order:
  - with attributes `IFD`, `FirstZ`, `FirstC`, `FirstT` and `PlaneCount`;
  - if the `Pixels` start tag is self-closing, there are none;
- for each `TiffData` that is not self-closing, the first `UUID` start tag
  after it and before the next `TiffData` end tag, `TiffData` start tag or
  `Pixels` end tag:
  - its `FileName` attribute;
  - its **text**: empty if it is self-closing; otherwise the characters of `X` from the
    end of the `UUID` tag to the start of the next tag, with skipped
    sections (including the content of CDATA sections) removed,
    references decoded as in attribute values, and
    leading and trailing whitespace removed.

Without a `Pixels` start tag, every `Pixels` attribute is absent and there
are no `TiffData` tags.

**Values.**

- **Integer attributes** (`SizeZ`, `SizeC`, `SizeT`, `First*`, `IFD`,
  `PlaneCount`; `SizeX` and `SizeY` are not used): the whole
  value MUST be one or more digits, optionally preceded and followed by
  whitespace, and at most 2^53 − 1. `SizeZ`, `SizeC`, `SizeT` and
  `PlaneCount` MUST be at least 1. Otherwise the input is rejected.
- **`PositionX`, `PositionY`:** like `PhysicalSize*` below, but any finite
  value counts (zero and negative too); the unit has no default (OME's
  default, "reference frame", is not a length).
- **`PhysicalSize*`:** a value counts as present only if the whole value
  (with no whitespace) matches
  `[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?` and its value is
  finite and positive. Otherwise it is treated as absent.
- **`DimensionOrder`:** MUST be `XY` followed by a permutation of `ZCT`, or
  the input is rejected.
- **Unit attributes:** matched exactly (by code points) against [conventions §5](../README.md#5-units).

The OME-XML's other content (for example `Interleaved`) is not used: the
TIFF tags decide.

## 4. The image

### 4.1 Planes

Let `spp` be IFD 0's SamplesPerPixel. Without OME-XML there is one plane,
IFD 0. With OME-XML:

- `SizeZ` and `SizeT` default to 1; `SizeC` defaults to `spp`.
- If `spp > 1`: `SizeC` 1 is read as `spp`; any other `SizeC ≠ spp` is
  rejected (several RGB channels are not supported). Channels are then the
  samples, and there is one plane per (z, t).
- If `spp = 1`, there is one plane per (z, c, t).

Let `Cp` be 1 if `spp > 1`, else `SizeC`. Planes have positions (z, c, t)
with `z < SizeZ`, `c < Cp`, `t < SizeT`; the plane count is
`SizeZ × Cp × SizeT`, which MUST be at most 100000. Planes are mapped to main-chain IFDs (by their index
in the main chain, IFD 0 being index 0) by the `TiffData` elements, in
document order (with one implicit `TiffData` without attributes if there
are none):

- **Multi-file datasets:** a `TiffData` with a `UUID` names the file
  identified by the `UUID`'s `FileName` if it has one, else by its text (two
  identifiers are the same file only if they are equal strings). If the
  `TiffData` elements name more than one distinct file, the input is
  rejected. Otherwise `UUID`s are ignored, so a file that names itself is
  accepted, and a `TiffData` without a `UUID` names no file.
- **Coverage:** a `TiffData` starts at position (`FirstZ`, `FirstC`,
  `FirstT`) (each default 0, and each MUST be less than `SizeZ`, `Cp`,
  `SizeT` respectively, else the input is rejected) and IFD index `IFD`
  (default 0). It covers `PlaneCount` planes. `PlaneCount` defaults to the
  plane count when it is the only `TiffData` and has no `IFD` attribute, and
  to 1 otherwise.
- **Stepping:** successive planes step through positions in
  `DimensionOrder` (default `XYZCT`; the letters after `XY` are fastest
  first, with sizes `SizeZ`, `Cp`, `SizeT`) and through consecutive IFD
  indices. Planes past the last position are ignored: with `P` the plane
  count and `L` the start position's index in that order (`L = a + A × (b
  + B × c)` for the letters after `XY` with positions `a`, `b`, `c` and
  sizes `A`, `B`), a `TiffData` covers `min(PlaneCount, P − L)` planes.
- **Cost:** the planes covered, summed over all `TiffData` elements, MUST
  be at most `4 × P + 1000`, else the input is rejected (so that a short
  document cannot step through many more planes than the image has).
- A plane mapped twice takes the later mapping.

After all `TiffData` elements are applied, every plane MUST be mapped to an
existing main-chain IFD, else the input is rejected. (An index past the
chain that a later mapping replaces is not an error.)

### 4.2 Pyramid levels

- If IFD 0 has SubIFDs, there are `1 + s` levels, where `s` is IFD 0's
  SubIFD count: level 0 is the planes' IFDs, and level `k ≥ 1` is, for every
  plane, its IFD's `k`-th SubIFD. A plane IFD with fewer than `s` SubIFDs
  rejects the input.
- Otherwise level 0 is the planes. Without OME-XML, the later main-chain
  IFDs are scanned in order. An IFD that is tiled and has BitsPerSample is a
  **candidate**: its format and size are checked as §2 says (a failure
  rejects the input). A candidate becomes the next level if it has IFD 0's
  format and is strictly smaller in both width and length than the last
  level. Other IFDs, and other candidates, are skipped. This finds the
  pyramids of Aperio SVS files, whose stripped thumbnails, labels and macros
  are skipped. (A tiled image of the right format that is not part of the
  pyramid would be taken as a level.)

Within a level, all planes MUST have the same width, length, tile size and
format. Every image of every level MUST be tiled and have IFD 0's format.

### 4.3 Data types and codecs

The data type is `uint`, `int` or `float` (SampleFormat 1, 2, 3) followed by
BitsPerSample, which MUST be 8, 16, 32 or 64 (32 or 64 for `float`).

| Compression | Predictor | codecs ([conventions §3](../README.md#3-arrays)) |
|---|---|---|
| 1 | 1 | bytes |
| 8, 32946 (Deflate) | 1 | bytes, zlib |
| 50000 (zstd) | 1 | bytes, zstd |
| 33003, 33004, 33005, 34712 (JPEG 2000) | any | imagecodecs_jpeg2k |
| 7 (JPEG) | 1 | imagecodecs_jpeg |

Anything else is rejected. JPEG (Compression 7) additionally requires
BitsPerSample 8, SampleFormat 1, and either SamplesPerPixel 1, or
SamplesPerPixel 3 with PlanarConfiguration 1 and PhotometricInterpretation
2 (RGB) or 6 (YCbCr); otherwise the input is rejected. (Old-style JPEG,
Compression 6, is rejected.)

The codecs read the bytes of a tile as they are, filled from each byte's
most significant bit (FillOrder 1), and decode samples at full resolution.
So, for every IFD that is a plane or a level:
- when Compression is 1, 8, 32946 or 50000, FillOrder MUST be 1;
- when PhotometricInterpretation is 6 (YCbCr) and Compression is neither 7
  nor JPEG 2000 (whose streams hold their own sampling), YCbCrSubsampling
  MUST be present and exactly `[1, 1]` (absent, it is TIFF 6.0's default
  `[2, 2]`).

Otherwise the input is rejected. `bytes` has `endian` from the TIFF's byte order
(when the data type is larger than 1 byte). When `spp > 1` and
PlanarConfiguration is 1 (interleaved), `transpose` comes first, for every
compression including JPEG 2000.

### 4.4 Arrays

The hierarchy is one image at the root ([conventions §4](../README.md#4-images)), with one array per level at
path `"<level>"`.

- **Axes:** `t` if `SizeT > 1`; `c` if the channel count (`SizeC` after
  §4.1, or `spp` without OME-XML) is more than 1; `z` if `SizeZ > 1`; then
  `y`, `x`.
- **Pixel size.** `PX`, `PY`, `PZ` and the units of `x`, `y`, `z` come from
  the first of these that applies:
  1. **OME-XML:** `PX`, `PY`, `PZ` are `PhysicalSizeX/Y/Z`, and `x`, `y`, `z`
     have the unit ([conventions §5](../README.md#5-units)) of `PhysicalSizeXUnit` etc. (default `µm`), each
     only when that `PhysicalSize` is present.
  2. **Aperio:** IFD 0 has an ImageDescription of type ASCII whose bytes up
     to the first NUL start with `Aperio` and are valid UTF-8. Its fields are
     the parts of that text between `|` characters; a field `name = value`
     is split at its first `=`, with whitespace around each part removed, and
     of fields with the same name the first is used. A field named `MPP`
     whose value is a
     present decimal (as `PhysicalSize*`, §3) gives `PX = PY` = that
     value, and `x`, `y` the unit `micrometer`.
  3. **Resolution tags:** IFD 0 has XResolution `n / d` with `n` and `d`
     positive and a ResolutionUnit tag of 2 (inch) or 3 (centimeter):
     `PX` is `25400 / (n / d)` or `10000 / (n / d)`, with unit
     `micrometer`, if that is less than 25.4. `PY` likewise from
     YResolution. (The ResolutionUnit default is not used, and neither is a
     pixel of 25.4 µm or more, 1000 dpi or less: a resolution of 72, 96 or
     300 dpi is the one photo and document software writes, not a pixel
     size.)

  Otherwise `PX`, `PY`, `PZ` are 1 with no unit. `t` and `c` have no unit.
- **Position.** The image has a translation ([conventions §5](../README.md#5-units)), the same at every level,
  when `x` and `y` have units and either
  - the OME-XML's `Plane` has `PositionX` and `PositionY` with units that
    are lengths ([conventions §5](../README.md#5-units)): their values converted to the units of `x` and `y`
    are the centre of the image; or, without OME-XML,
  - the Aperio description has fields `Left` and `Top` (millimetres, the
    scanned area's top-left on the slide) whose values are present decimals
    or zero: the translation is `x = Left × 1000`, `y = Top × 1000`
    (micrometres).
- **Shape:** the counts of `t`, `c`, `z`, then the level's length and width.
- **Chunk shape:** 1 for `t` and `z`; for `c`, `spp` if interleaved, else 1;
  then the level's TileLength and TileWidth (levels may differ).
- **Scale** of level `L` (level 0 has width `W0`, length `H0`):
  `y = PY × (H0 / HL)`, `x = PX × (W0 / WL)`, `z = PZ`, `t = c = 1`. (The
  division is computed first.)
- **Name:** the OME `Image` name, if present and not empty.
- **Chunks:** for each plane (t, c, z) of a level, its IFD's tiles are numbered
  `k = s × T + j`, where `T` is the number of tiles per sample plane
  (`ceil(H/TileLength) × ceil(W/TileWidth)`), `j` runs row-major over the
  tile grid, and `s` is the sample when `spp > 1` and planar (else 0). Both
  TileOffsets and TileByteCounts MUST have `T` times the number of sample
  planes values. The chunk with coords `t`, `c` (the plane's channel, or the
  sample `s`, or 0 when interleaved), `z` (each only when present), then the
  tile row and column, is tile `k`. It is present when TileByteCounts`[k]` is
  more than 0, and absent (the fill value) otherwise. Its bytes are the
  tile's, encoded as the codecs say; for JPEG, a JPEG tile is made a complete
  stream with the IFD's JPEGTables and an explicit color transform for its
  PhotometricInterpretation (2: RGB as stored; 6: YCbCr). For JPEG,
  JPEGTables, when present, MUST be at least 4 bytes long, start with
  `FF D8` and end with `FF D9`, and every present tile MUST be more than 2
  bytes long.

## 5. Source metadata

The root's source metadata `S` is `{"byte_order": "little" | "big",
"bigtiff": true | false}`. Everything else the file holds is on
`vzip_source`, which is the file's **IR mirror**
([conventions §8](../README.md#8-the-ir-mirror)): its table, from which the
file is rebuilt byte for byte, and its view, `vzip_source/tree`. This
section is the TIFF **source model**: the elements of a TIFF's IR. Nothing is
left out, layout and dead space included (they are elements too, the dead
space as gaps).

### 5.1 Elements

Paths are relative to the IR's root, whose path is `""`; `<i>`, `<j>`,
`<tag>` are decimal name indexes (conventions §8.1). Types are those of conventions §8.6, with `E` the
file's byte order (`<` little, `>` big), `W` the offset type (`u4`, or `u8`
for BigTIFF) and `C` the entry-count type (`u2`, or `u8`).

| path | kind | type | extent: what |
|---|---|---|---|
| `""` | struct | | the root: the whole file, `(0, size)` |
| `header` | struct | | the header (8 or 16 bytes) |
| `header/byte_order` | value | `ascii[2]` | `II` or `MM` |
| `header/magic` | value | `Eu2` | 42 or 43 |
| `header/offset_size`, `header/reserved` | value | `Eu2` | BigTIFF only |
| `header/first_ifd` | value | `EW` | the first IFD's offset |
| `ifds/<i>` | struct | | main-chain IFD `i` (§2): its entry count, entries and next-IFD offset |
| `…/entry_count` | value | `EC` | the entry count `n` |
| `…/tags/<tag>` | struct | | the first entry of tag `<tag>` (12 or 20 bytes); a later one of the same tag is `…/tags/<tag>~<k>`, `k` = 1, 2, … |
| `…/tags/<tag>/entry` | value | record | the entry's tag, field type and count (`{tag:Eu2,type:Eu2,count:EW}`), with `offset:EW` when the value is out of line, or `field:bytes[4\|8]` when the field type is unknown |
| `…/tags/<tag>/value` | value | by field type | the value: in the entry's field (the bytes it uses; the rest of the field is a gap), or at its offset when it lies within the file. Its type is `ascii[n]` (ASCII), `Eu1[n]`, `Eu2[n]`, `Eu4[n]`, `Eu8[n]`, `Ei1[n]` … (integers), `Eu4[n,2]`, `Ei4[n,2]` (rationals), `Ef4[n]`, `Ef8[n]`, or `bytes[n]` (UNDEFINED) |
| `…/next_ifd` | value | `EW` | the next-IFD offset |
| `…/tiles`, `…/strips` | struct | | an IFD's tiles or strips (by its TileOffsets and TileByteCounts, or StripOffsets and StripByteCounts), when it has both tables |
| `…/tiles/<j>`, `…/strips/<j>` | data | | tile or strip `j`, when its byte count is not 0 and it lies within the file; its form's geometry is `{"shape": [rows, columns], "samples": s, "bits": b}`, its codec `{"compression": c}`, and its recipe the bytes, or, for JPEG (7) with JPEGTables, the first 2 bytes, the tables without their SOI and EOI (a shared data source), and the rest |
| `…/subifds/<j>` | struct | | SubIFD `j` of the IFD (to depth 4), as an IFD |
| `…/exif`, `…/gps`, `…/interoperability`, `…/ifd_<tag>` | struct | | the IFD a pointer tag (§5.2) leads to (`…/<name>/<j>` when the tag has several values), as an IFD |
| `ome/planes/<p>` | alias | | plane `p` of the OME-XML (§4.1): the main-chain IFD its `TiffData` names |
| `gaps/<offset>` | gap | | bytes no other element claims (padding, unused space, image data of tables that do not lie within the file) |

An IFD whose tile (or strip) tables are at the offsets of an earlier IFD's,
with the same counts and codec, names that IFD's `tiles` by an alias. An
element whose bytes an earlier element claims (an IFD read twice through a
pointer, a tile at another's offset) is an alias of that element.

### 5.2 Which IFDs

The IFDs of §2, at depth 0 (`ifds/<i>`) and their SubIFDs (to depth 4); then
the IFDs that pointer tags lead to: SubIFDs (330), ExifIFD (34665),
GPSIFD (34853), InteroperabilityIFD (40965) when their field type is an
unsigned integer type, GlobalParametersIFD (400) when it is LONG, and any
other tag of field type IFD (13) or IFD8 (18) that is neither a tag the
layout uses nor a layout table. The IFDs of §2 are visited in order (each
IFD, then its SubIFDs, depth first), and each pointer value in order; an IFD
that a value leads to is read when it lies within the file, has at least one
entry and does not overlap an IFD already read, at depth at most 4, while at
most 100000 IFDs are read and 10000 offsets tried. An IFD already read is an
alias.

### 5.3 Values

An entry's value is an element when it lies within the file. Its bytes are
in the source; the view shows it when it is a number, a record, a GUID or
text (ASCII) of at most 1024 bytes (conventions §8.7), and a producer reads
every such value.

### 5.4 Equivalence

The elements are determined by the file, and the mirror by its elements
(conventions §8.8: one order, one folding, one encoding): hierarchies of one
file are compared entry for entry, `vzip_source` included
([VIRTUALIZE.md §1.1](../../VIRTUALIZE.md#11-output-and-equivalence)).

## 6. Example

A one-plane RGB OME-TIFF at `https://example.org/image.ome.tif`, with zstd
tiles and one SubIFD level (the OME-XML is shortened here). The root's
property:

```json
{
  "profile": "tiff",
  "version": 0,
  "revision": 24,
  "source": {
    "url": "https://example.org/image.ome.tif"
  },
  "tiff": {
    "byte_order": "little",
    "bigtiff": false
  }
}
```

`vzip_source` is the IR mirror (§5). For the 5,319-byte JPEG-tiled file
`jpeg_gray.tif` of vzip's test fixtures, its property is:

```json
{
 "tiff": {
  "ir": {
   "version": 2,
   "size": 5319,
   "elements": 73,
   "rows": 14,
   "names": ["", "byte_order", "entry", "entry_count", "first_ifd", "gaps/", "header", "ifds/", "magic",
             "next_ifd", "tags/", "tiles", "value"],
   "types": ["", "<u2", "<u2[12]", "<u2[1]", "<u4", "<u4[1,2]", "<u4[12]", "<u4[1]", "ascii[12]",
             "ascii[21]", "ascii[2]", "{tag:<u2,type:<u2,count:<u4,offset:<u4}", "{tag:<u2,type:<u2,count:<u4}"],
   "forms": ["{\"geometry\":{\"shape\":[32,32],\"samples\":1,\"bits\":8},\"codec\":{\"compression\":7},\"recipe\":[[\"src\",0,null]]}"]
  }
 }
}
```

Its 73 elements are 14 rows: IFD 0's 15 tags fold into one column run, its
12 tiles into another, whose starts and lengths are stored as differences
(conventions §8.8: encoding 2, which reads them from TileOffsets and
TileByteCounts, needs 16 tiles or more), and the 7 gaps into a third, whose
name indexes are their starts (encoding 3). The names and types are sorted,
so `names[0]` is `""`, the root's name; the root is row 0, with the extent
`(0, 5319)`. The view, `vzip_source/tree`, is the root's document, and
begins:

```json
{
 "tiff": {
  "header": {"byte_order": "II", "magic": 42, "first_ifd": 8},
  "ifds/0": {
   "entry_count": 15,
   "tags/256": {"entry": {"tag": 256, "type": 4, "count": 1}, "value": [128]},
   "tags/257": {"entry": {"tag": 257, "type": 4, "count": 1}, "value": [96]},
   "tags/305": {"entry": {"tag": 305, "type": 2, "count": 12, "offset": 246}, "value": "tifffile.py"},
   "tags/324": {"entry": {"tag": 324, "type": 4, "count": 12, "offset": 258},
                "value": [336, 752, 1166, 1581, 1997, 2413, 2827, 3242, 3658, 4074, 4488, 4903]},
   "next_ifd": 0,
   "tiles": {}
  }
 }
}
```

(abridged: the other tags are like 256's; a text value is shown up to its first
NUL, so tag 305's 12 bytes are `tifffile.py`).
