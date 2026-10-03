# Virtualizing TIFF files

The TIFF profile of [VIRTUALIZE.md](../VIRTUALIZE.md) (revision 10), numbered
as its §3. §1 and §2 are in VIRTUALIZE.md and apply here.

## 3. TIFF profile

### 3.1 Reading

The input is a TIFF (magic 42) or BigTIFF (magic 43, offset size 8 and
reserved word 0), in either byte order. An NDPI file is read with the changes given in the NDPI profile (§4,
[ndpi.md](ndpi.md)).

- **IFDs:** the virtualizer reads the chain of image file directories (IFDs)
  from the header (the **main chain**, which ends at a next-IFD offset of 0;
  it MUST have at least one IFD).
  Then, for every main-chain IFD in order, it reads the IFDs whose offsets
  that IFD's `SubIFDs` tag (330) lists, in order. Only the listed offsets are
  read: a SubIFD's own `SubIFDs` tag is checked like any table tag but not
  followed, and its next-IFD offset field MUST lie within the file but its
  value is not used or checked. (A main-chain next-IFD offset is an offset,
  so it MUST be at most 2^53 − 1.) A `SubIFDs`
  tag with no values means no SubIFDs.
- **Limits:** every IFD offset read (main chain or SubIFD) MUST be at least
  8 (16 for BigTIFF) and MUST differ from every other IFD offset read, so a
  cycle, or a SubIFD shared by two IFDs, rejects the input. At most 100000
  IFDs are read; reading the 100001st rejects the input.
- **Tags** not in the table below are ignored: neither their field types nor
  their values are read or checked. Of duplicate tags in an IFD, the first is
  used and the others are ignored.

Tags used (absent tags take the defaults shown):

| tag | name | kind | default |
|---|---|---|---|
| 256, 257 | ImageWidth, ImageLength | scalar | required |
| 258 | BitsPerSample | array | required |
| 259 | Compression | scalar | 1 |
| 262 | PhotometricInterpretation | scalar | none (required for JPEG with 3 samples, §3.5) |
| 270 | ImageDescription | text | none |
| 277 | SamplesPerPixel | scalar | 1 |
| 284 | PlanarConfiguration | scalar | 1 |
| 317 | Predictor | scalar | 1 |
| 322, 323 | TileWidth, TileLength | scalar | required for tiled images |
| 324, 325 | TileOffsets, TileByteCounts | array | required for tiled images |
| 330 | SubIFDs | array | none |
| 282, 283 | XResolution, YResolution | scalar | none |
| 296 | ResolutionUnit | scalar | 2 |
| 339 | SampleFormat | array | 1 |
| 347 | JPEGTables | bytes | none |

**Checks on every IFD read.** For every tag of the table present in an IFD
read (whether or not that IFD is used):

- **Field type:** ImageDescription may have any field type that TIFF 6.0 or
  BigTIFF defines (1–12, 16–18), or 13 (IFD). JPEGTables MUST have type
  BYTE (1) or UNDEFINED (7); its value is its bytes. XResolution and
  YResolution MUST have type RATIONAL (5). Every other tag MUST
  have an unsigned integer type: BYTE (1), SHORT (3), LONG (4), IFD (13), LONG8
  (16) or IFD8 (18), in either TIFF variant. Any other type rejects.
- **Value:** its value MUST lie within the file, and each of its values
  of an integer type MUST be at most 2^53 − 1 in magnitude.
- **Count:** a scalar tag MUST have at least one value (only the first is
  used). An array tag may have any count; BitsPerSample and SampleFormat
  with no values reject when they are used.

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
a plane or a level (§3.3, §3.4), and for every candidate of the level scan
(§3.4).

**Size.** An IFD that becomes a plane or a level, or is a candidate of the
level scan, MUST have ImageWidth and ImageLength, and both MUST be at least
1. A tiled one (§3.4) MUST have TileWidth, TileLength, TileOffsets and
TileByteCounts, with TileWidth and TileLength at least 1. An IFD is
**tiled** if it has TileWidth and TileOffsets.

**IFD 0** below is the first IFD of the main chain.

### 3.2 OME-XML

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
   starting at a `<` (whitespace and digits as in §1.3):

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
- **Unit attributes:** matched exactly (by code points) against §2.3.

The OME-XML's other content (for example `Interleaved`) is not used: the
TIFF tags decide.

### 3.3 Planes

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
  indices. Planes past the last position are ignored.
- A plane mapped twice takes the later mapping.

After all `TiffData` elements are applied, every plane MUST be mapped to an
existing main-chain IFD, else the input is rejected. (An index past the
chain that a later mapping replaces is not an error.)

### 3.4 Pyramid levels

- If IFD 0 has SubIFDs, there are `1 + s` levels, where `s` is IFD 0's
  SubIFD count: level 0 is the planes' IFDs, and level `k ≥ 1` is, for every
  plane, its IFD's `k`-th SubIFD. A plane IFD with fewer than `s` SubIFDs
  rejects the input.
- Otherwise level 0 is the planes. Without OME-XML, the later main-chain
  IFDs are scanned in order. An IFD that is tiled and has BitsPerSample is a
  **candidate**: its format and size are checked as §3.1 says (a failure
  rejects the input). A candidate becomes the next level if it has IFD 0's
  format and is strictly smaller in both width and length than the last
  level. Other IFDs, and other candidates, are skipped. This finds the
  pyramids of Aperio SVS files, whose stripped thumbnails, labels and macros
  are skipped. (A tiled image of the right format that is not part of the
  pyramid would be taken as a level.)

Within a level, all planes MUST have the same width, length, tile size and
format. Every image of every level MUST be tiled and have IFD 0's format.

### 3.5 Data types and codecs

The data type is `uint`, `int` or `float` (SampleFormat 1, 2, 3) followed by
BitsPerSample, which MUST be 8, 16, 32 or 64 (32 or 64 for `float`).

| Compression | Predictor | codecs (§2.1) |
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
Compression 6, is rejected.) `bytes` has `endian` from the TIFF's byte order
(when the data type is larger than 1 byte). When `spp > 1` and
PlanarConfiguration is 1 (interleaved), `transpose` comes first, for every
compression including JPEG 2000.

### 3.6 Output

One image at the archive root (§2.2), with one array per level at path
`"<level>"`.

- **Axes:** `t` if `SizeT > 1`; `c` if the channel count (`SizeC` after
  §3.3, or `spp` without OME-XML) is more than 1; `z` if `SizeZ > 1`; then
  `y`, `x`.
- **Pixel size.** `PX`, `PY`, `PZ` and the units of `x`, `y`, `z` come from
  the first of these that applies:
  1. **OME-XML:** `PX`, `PY`, `PZ` are `PhysicalSizeX/Y/Z`, and `x`, `y`, `z`
     have the unit (§2.3) of `PhysicalSizeXUnit` etc. (default `µm`), each
     only when that `PhysicalSize` is present.
  2. **Aperio:** IFD 0 has an ImageDescription of type ASCII whose bytes up
     to the first NUL start with `Aperio` and are valid UTF-8. Its fields are
     the parts of that text between `|` characters; a field `name = value`
     is split at its first `=`, with whitespace around each part removed, and
     of fields with the same name the first is used. A field named `MPP`
     whose value is a
     present decimal (as `PhysicalSize*`, §3.2) gives `PX = PY` = that
     value, and `x`, `y` the unit `micrometer`.
  3. **Resolution tags:** IFD 0 has XResolution `n / d` with `n` and `d`
     positive and ResolutionUnit 2 (inch) or 3 (centimetre): `PX` is
     `25400 / (n / d)` or `10000 / (n / d)`, with unit `micrometer`. `PY`
     likewise from YResolution.

  Otherwise `PX`, `PY`, `PZ` are 1 with no unit. `t` and `c` have no unit.
- **Position.** The image has a translation (§2.3), the same at every level,
  when `x` and `y` have units and either
  - the OME-XML's `Plane` has `PositionX` and `PositionY` with units that
    are lengths (§2.3): their values converted to the units of `x` and `y`
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
  planes values. Tile `k` with TileByteCounts `n > 0` is the entry
  `<level>/c/<coords>` with one range `(0, TileOffsets[k], n)` (for JPEG,
  see below); coords are
  `t`, `c` (the plane's channel, or the sample `s`, or 0 when interleaved),
  `z` (each only when present), then the tile row and column.
- **JPEG tiles.** A JPEG-in-TIFF tile is a JPEG stream that may omit its
  tables (which are in the IFD's JPEGTables) and the colour transform (which
  is given by PhotometricInterpretation). Each tile's reference makes it a
  complete stream: the ranges `[P, (0, TileOffsets[k] + 2, n − 2)]`, where
  `n` MUST be more than 2 (the tile's first 2 bytes, its SOI marker, are
  dropped), and `P` is the literal range of
  - `FF D8` (SOI);
  - for 3 samples, the Adobe marker `FF EE 00 0E 41 64 6F 62 65 00 64 00 00
    00 00 T`, with transform `T` = 0 for PhotometricInterpretation 2 (the
    samples are RGB as stored) and 1 for 6 (YCbCr);
  - if the IFD has JPEGTables: its bytes without the first 2 and the last 2.
    JPEGTables MUST then be at least 4 bytes long, start with `FF D8` and
    end with `FF D9`.

  `P` depends only on the IFD, so every tile of an IFD has the same `P`.
- **OME-XML:** if present, the entry `OME/METADATA.ome.xml` holds `D`, the
  ImageDescription's bytes up to the first NUL, unchanged.
