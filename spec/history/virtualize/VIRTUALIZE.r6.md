# Virtualizing image files as OME-Zarr in vzip

Profiles version: 0 (**draft**) · Revision: 6

## 1. Introduction

A **virtualizer** reads the structure of an image file (TIFF or Nikon ND2)
and writes a vzip archive ([SPEC.md](SPEC.md)) that presents the file's
pixels as an OME-Zarr dataset. The pixels stay in the original file: each
Zarr chunk is a reference to byte ranges of it. This document specifies, for
each supported input format (a **profile**), exactly which archive a
virtualizer produces, so that independent virtualizers produce the same
output from the same input.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are to be
interpreted as described in RFC 2119.

### 1.1 Output and equivalence

A virtualizer's **output** is an archive description:

- a **source table**, and
- a set of **entries**: keys, each with either bytes or a list of ranges
  (SPEC.md §2, §5).

Two outputs are **equivalent** when they have the same source table, the same
set of keys, and for every key:

- **reference entries:** the same list of ranges, in order;
- **JSON documents** (keys ending in `zarr.json`): equal JSON values. Objects
  compare by key, arrays by order, and numbers as IEEE 754 binary64 values;
  whitespace and key order do not matter;
- **other bytes entries:** identical bytes.

The archive's byte layout (entry order, compression, page index) is the
writer's choice and is not part of the output. Two virtualizers conform to
the same profile revision if they produce equivalent outputs for every input
that the profile accepts, and both reject every input it rejects.

### 1.2 Input

A virtualizer is given the input file's URL, `U`. The output's source table
is exactly one `url` source whose value is `U` as given, without pins.
(`U` MUST be a valid absolute URI; virtualizers do not normalize it.)
Every reference in the output is a range of source 0.

**Choosing the profile.** The file's first bytes decide:

| first bytes | |
|---|---|
| `49 49 2A 00`, `4D 4D 00 2A`, `49 49 2B 00`, `4D 4D 00 2B` | TIFF profile (§3) |
| `DA CE BE 0A` (the ND2 chunk magic, §4.1) | ND2 profile (§4) |
| anything else, including the JPEG 2000 signature box `00 00 00 0C 6A 50 20 20 0D 0A 87 0A` that starts legacy ND2 files | rejected |

**Rejection.** A virtualizer MUST reject an input the profile does not
accept, producing no output. That includes every input that is not well
formed as this document describes it: a read outside the file, a structure
that is truncated or inconsistent, a value of the wrong type or out of range,
an offset or length above 2^53 − 1. Being unable to read the file (a network
error) is not a rejection; the virtualizer fails instead.

**Structure only.** The output MUST NOT depend on the file's pixel data.
(Reading blocks that happen to include pixel bytes is fine.)

**Evaluation order.** Whether an input is rejected never depends on the
order in which a virtualizer reads or checks things: each profile lists what
is checked, and every listed check applies whether or not its value ends up
in the output.

**References stay in the file.** Every range of the output MUST lie within
the file (`offset + length ≤` the file's size), and every reference entry's
payload MUST be at most 65519 bytes; otherwise the input is rejected. The
payload (SPEC.md §4.3, §5) of a single range `(0, o, n)` is a `Range`
message: `0x10 varint(o)` if `o > 0`, then `0x18 varint(n)` if `n > 0`.
The payload of several ranges is a `Concat` message: for each range, `0x0A
varint(len(r)) r`, where `r` is that range's `Range` message. `varint` is the
protobuf base-128 encoding (1 byte below 2^7, 2 below 2^14, and so on).

### 1.3 Arithmetic

Where this document computes a number (a scale, a size), it is computed in
IEEE 754 binary64 arithmetic, in the order written, with each operation
rounded to nearest. Decimal strings are converted to binary64 by correct
rounding. Integers are exact. A number that would be infinite or NaN rejects
the input. In JSON documents, integers (shapes, counts, codec parameters) are
written as integers; a computed number such as a scale MAY be written either
way (`1` or `1.0`), since outputs compare numbers by value (§1.1).

**Whitespace** in this document means the XML whitespace characters: space,
tab, CR and LF (U+0020, U+0009, U+000D, U+000A). **Digits** are the ASCII
digits `0`–`9`.

## 2. Common output

### 2.1 Arrays

Every array's `zarr.json` is the JSON object:

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": [...],
  "data_type": "...",
  "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [...]}},
  "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
  "fill_value": 0,
  "codecs": [...],
  "dimension_names": [...],
  "attributes": {}
}
```

with no other members. Chunk keys are `<array path>/c/<i0>/<i1>/...`. A chunk
whose data the file does not contain has no entry (it reads as the fill
value).

**Axes.** An image's axes are a subsequence of `t, c, z, y, x`, in that
order: `t` (time), `c` (channel), `z`, `y`, `x` (space). `y` and `x` are
always present; the profile says when the others are.

**Codecs.** `codecs` is built from these, in this order:

1. `{"name": "transpose", "configuration": {"order": [...]}}` when a chunk's
   bytes hold the channel axis last ("interleaved"). `order` lists the array's
   axis indices in stored order: every axis except `c` in array order, then
   `c`.
2. The array-to-bytes codec, one of:
   - `{"name": "bytes"}` (no `configuration`) for 1-byte data types;
   - `{"name": "bytes", "configuration": {"endian": "little"}}` or
     `"big"` for larger ones;
   - `{"name": "imagecodecs_jpeg2k"}` for JPEG 2000 (one codestream per
     chunk, decoding to the chunk's `[y, x]`, or, when interleaved, to
     `[y, x, c]`, after which `transpose` applies).
3. A compressor, when the bytes are compressed:
   - `{"name": "zlib", "configuration": {"level": 1}}` (zlib streams);
   - `{"name": "zstd", "configuration": {"level": 0, "checksum": false}}`.

### 2.2 Images

An image is a group whose `zarr.json` is
`{"zarr_format": 3, "node_type": "group", "attributes": {"ome": M}}`, where `M`
is the OME-NGFF 0.5 object:

```json
{
  "version": "0.5",
  "multiscales": [{
    "name": "...",
    "axes": [{"name": "t", "type": "time", "unit": "second"}, ...],
    "datasets": [{"path": "0", "coordinateTransformations": [{"type": "scale", "scale": [...]}]}, ...]
  }]
}
```

- `name` is present only when the profile gives one.
- Each axis has `name` and `type` (`"time"`, `"channel"` or `"space"`), and
  `unit` only when the profile gives one.
- `datasets` lists the pyramid levels, full resolution first, with paths
  `"0"`, `"1"`, ...; each has a `scale` transformation, one number per axis,
  followed by a `{"type": "translation", "translation": [...]}`
  transformation (one number per axis) only where the profile gives one.
- `M` has an `"omero"` member, next to `multiscales`, only where a profile
  says so.

### 2.3 Units

OME-XML unit symbols map to OME-NGFF units:

| symbol | unit |
|---|---|
| `µm` (U+00B5), `μm` (U+03BC), `um` | `micrometer` |
| `nm` | `nanometer` |
| `mm` | `millimeter` |
| `cm` | `centimeter` |
| `m` | `meter` |
| `Å` (U+00C5), `Å` (U+212B) | `angstrom` |
| `pm` | `picometer` |
| `in` | `inch` |
| `ft` | `foot` |
| `s` | `second` |
| `ms` | `millisecond` |
| `min` | `minute` |
| `h` | `hour` |

Any other symbol gives no unit (the scale is still used).

## 3. TIFF profile

### 3.1 Reading

The input is a TIFF (magic 42) or BigTIFF (magic 43, offset size 8 and
reserved word 0), in either byte order.

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
| 270 | ImageDescription | text | none |
| 277 | SamplesPerPixel | scalar | 1 |
| 284 | PlanarConfiguration | scalar | 1 |
| 317 | Predictor | scalar | 1 |
| 322, 323 | TileWidth, TileLength | scalar | required for tiled images |
| 324, 325 | TileOffsets, TileByteCounts | array | required for tiled images |
| 330 | SubIFDs | array | none |
| 339 | SampleFormat | array | 1 |

**Checks on every IFD read.** For every tag of the table present in an IFD
read (whether or not that IFD is used):

- **Field type:** ImageDescription may have any field type that TIFF 6.0 or
  BigTIFF defines (1–12, 16–18), or 13 (IFD). Every other tag MUST have an
  unsigned integer type: BYTE (1), SHORT (3), LONG (4), IFD (13), LONG8
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
MUST be 1 or 2. Counts of BitsPerSample and SampleFormat are not otherwise
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

Anything else is rejected. `bytes` has `endian` from the TIFF's byte order
(when the data type is larger than 1 byte). When `spp > 1` and
PlanarConfiguration is 1 (interleaved), `transpose` comes first, for every
compression including JPEG 2000.

### 3.6 Output

One image at the archive root (§2.2), with one array per level at path
`"<level>"`.

- **Axes:** `t` if `SizeT > 1`; `c` if the channel count (`SizeC` after
  §3.3, or `spp` without OME-XML) is more than 1; `z` if `SizeZ > 1`; then
  `y`, `x`.
- **Units** (only with OME-XML): `z`, `y`, `x` have the unit (§2.3) of
  `PhysicalSizeZUnit` etc. (default `µm`) when that `PhysicalSize` is
  present; `t` and `c` have none.
- **Shape:** the counts of `t`, `c`, `z`, then the level's length and width.
- **Chunk shape:** 1 for `t` and `z`; for `c`, `spp` if interleaved, else 1;
  then the level's TileLength and TileWidth (levels may differ).
- **Scale** of level `L` (level 0 has width `W0`, length `H0`):
  `y = PY × (H0 / HL)`, `x = PX × (W0 / WL)`, `z = PZ`, `t = c = 1`, where
  `PX`, `PY`, `PZ` are `PhysicalSizeX/Y/Z` when present, else 1. (The
  division is computed first.)
- **Name:** the OME `Image` name, if present and not empty.
- **Chunks:** for each plane (t, c, z) of a level, its IFD's tiles are numbered
  `k = s × T + j`, where `T` is the number of tiles per sample plane
  (`ceil(H/TileLength) × ceil(W/TileWidth)`), `j` runs row-major over the
  tile grid, and `s` is the sample when `spp > 1` and planar (else 0). Both
  TileOffsets and TileByteCounts MUST have `T` times the number of sample
  planes values. Tile `k` with TileByteCounts `n > 0` is the entry
  `<level>/c/<coords>` with one range `(0, TileOffsets[k], n)`; coords are
  `t`, `c` (the plane's channel, or the sample `s`, or 0 when interleaved),
  `z` (each only when present), then the tile row and column.
- **OME-XML:** if present, the entry `OME/METADATA.ome.xml` holds `D`, the
  ImageDescription's bytes up to the first NUL, unchanged.

## 4. ND2 profile

### 4.1 Chunks

An ND2 file (format version 3 or later) is a sequence of **chunks**. A chunk
at offset `o` has a 16-byte header: `u32` magic `0x0ABECEDA`, `u32` name
length `n`, `u64` data length `d` (all little-endian), then `n` bytes of name
(ASCII, NUL-padded), then `d` bytes of data starting at `o + 16 + n`. Every
chunk the virtualizer reads MUST start with the magic, else the input is
rejected; names are not checked except as stated. Reading a chunk's header
means reading its 16 bytes and its `n` bytes of name (both MUST lie within
the file), not its data.

- **Signature:** the chunk at offset 0 MUST be named
  `ND2 FILE SIGNATURE CHUNK NAME01!`, with `n = 32` and `d = 64`. Its data
  starts with `Ver`, decimal digits (the major version `M`), and `.`; `M`
  MUST be at least 3.
- **Chunk map:** the file MUST be at least 40 bytes long; its last 40 bytes
  are the 32 bytes `ND2 CHUNK MAP SIGNATURE 0000001!` and a `u64` offset
  `m`. The chunk at `m` MUST be named `ND2 FILEMAP SIGNATURE NAME 0001!`
  (its name bytes up to the first NUL, or all `n` if there is none). Its
  data is a sequence of records, each a name running up to and including the
  first `!`, then a `u64` offset and a `u64` size (unused). The record named
  `ND2 CHUNK MAP SIGNATURE 0000001!` ends the map: its offset and size, and
  anything after them, are not read. A record (other than the last) that
  runs past the end of the data, or data without that last record, rejects
  the input. The map gives each named chunk's header offset; of duplicate
  names, the last is used. An offset is checked (≤ 2^53 − 1, chunk magic)
  only when that chunk is read.

### 4.2 Lite variant

Metadata chunks hold a **lite variant** (LV) structure: a sequence of
records, running to the end of the chunk's data. A record is `u8` type,
`u8` name length `k` (in UTF-16 code units, including a terminating NUL),
the name (`2k` bytes of UTF-16LE; the name is its units up to the first
NUL, decoded like a string), and a value:

| type | value |
|---|---|
| 1 | bool: `u8` (nonzero is true) |
| 2, 3 | `i32`, `u32` |
| 4, 5 | `i64`, `u64` |
| 6 | binary64 |
| 7 | `u64` (a pointer; the value is meaningless) |
| 8 | string: UTF-16LE code units up to the first NUL unit, which ends the record. Unpaired surrogates are replaced by U+FFFD. |
| 9 | byte array: `u64` length `b`, then `b` bytes |
| 11 | level: `u32` item count `c`, `u64` length `L`, then exactly `c` records, which MUST end at `L` bytes from the start of this record (its type byte); then `8c` bytes to skip |

Any other type rejects the input, except that a chunk's data MAY consist of
a single **compressed** record, type 76: the type byte, a name-length byte
(whatever its value), 10 bytes to skip, then from offset 12 a zlib stream
(RFC 1950) that MUST end exactly at the end of the chunk's data. The stream
inflates to the chunk's LV structure (which MUST NOT itself start with a
compressed record).

Levels MUST NOT be nested more than 100 deep: a chunk's top-level records
are at depth 0, the records of a level at depth `d` are at depth `d + 1`,
and a level record (type 11) at depth 100 or more rejects the input, even
if it is empty.

**Values.** All integers are little-endian. A level with at least one
record, all with empty names, is a **list** of their values. Any other level
(including an empty one) is an **object**: its members are its records by
name, in the order of each name's first appearance, and a repeated name has
its last value. A chunk's top-level records always form an object. A byte
array is a list of its bytes; each byte counts as a value of type 3. "The
members of" an object or a list are its values in order.

**Paths** name members, for example `SLxImageAttributes/uiWidth`; `a<i>`
names a member like `a0` (`i` in decimal, without leading zeros). Each step
of a path looks up a name in an object; a step from a value that is not an
object (a list, a byte array, a scalar) rejects the input. A missing member
takes the default given, and a missing member without one rejects the
input. A member read as:

- a **number** MUST have type 2–6; its value is converted to binary64
  (rounding to nearest) and MUST be finite;
- an **integer** MUST be a number with an integral value from 0 to
  2^53 − 1 (a **color** is an integer from −2^31 to 2^32 − 1, taken modulo
  2^32);
- a **flag** MUST have type 1–5 (true if nonzero);
- a **string** MUST have type 8;
- an **object** or a **list** MUST be one (a byte array is a list).

Any other value rejects the input.

### 4.3 Metadata

Every member listed in this section is read and checked (with the kind
given, §4.2) wherever it is present in the places described, whether or not
its value ends up in the output.

- **Attributes** (chunk `ImageAttributesLV!`, member `SLxImageAttributes`;
  required):
  - integers `uiWidth`, `uiHeight`, `uiWidthBytes`, `uiComp` (components per
    pixel), `uiBpcInMemory` (all required; `uiWidth`, `uiHeight` and
    `uiComp` at least 1, and `uiComp` at most 1024);
  - number `uiBpcSignificant` (required);
  - integers `eCompression` (default 2), `uiTileWidth` and `uiTileHeight`
    (default 0).

  Other members, such as `uiSequenceCount` and `ePixelType`, are not read.
  - `uiBpcInMemory` 8, 16, 32 gives `uint8`, `uint16`, `float32`; other values
    are rejected.
  - `eCompression` 2 is uncompressed and 0 is lossless (zlib); any other
    value (1 is lossy) is rejected.
  - A tile width or height that is positive and differs from the image's is
    rejected.
- **Experiment** (chunk `ImageMetadataLV!`, member `SLxExperiment`; if the
  chunk or the member is absent there are no loops) is a tree of objects
  (any other value rejects). **Every node of the tree** (the root, and the
  children of every node) is checked, whether or not the flattening below
  visits it: its `eType` MUST be one of those in the table, and its members
  are read as the table says.
  - A **node** has:
    - `eType` (integer, required);
    - `uLoopPars` (object, default absent);
    - `pItemValid` (a list of flags, default absent), a member of the node
      itself, checked on every node;
    - children, the members of `ppNextLevelEx` (object or list, default
      none), each of which MUST be an object.
  - Each node's loop has a **kind** and a count. Its members are in
    `uLoopPars`:

    | `eType` | kind | count, and period or step |
    |---|---|---|
    | 1 | time | integer `uiCount` (default 0); period: number `dPeriod` (ms, default 0) |
    | 8 | time | the members `p` of `pPeriod` (object or list, default none) MUST be objects. The count is the sum of integer `p/uiCount` (required) over the valid `p`. The period is number `p/dPeriod` (default 0) of the first valid `p`, or 0 if none is valid. |
    | 2 | position | the number of valid members of `Points` (object or list, default none). Each valid member `q` MUST be an object; its **stage position** is numbers `q/dPosX` and `q/dPosY` (µm, default absent). |
    | 4 | z | integer `uiCount` (default 0); numbers `dZStep`, `dZLow`, `dZHigh` (default 0); step `abs(dZStep)`, or if that is 0 and the count is more than 1, `abs(dZHigh − dZLow) / (count − 1)` |
    | 6 | (spectral) | integer `uiCount`; if it is absent, integer `pPlanes/uiCount` (`pPlanes` is read only then); if that is absent, 0 |

    For eType 8, `uiCount` and `dPeriod` are read from every valid member,
    and only from those; the sum MUST be at most 2^53 − 1.
  - **Validity:** the `i`-th member of `pPeriod` (of `Points`) is
    **valid** if the list `uLoopPars/pPeriodValid` (for `Points`: the node's
    `pItemValid`, not a member of `uLoopPars`) is absent, or if its `i`-th
    member exists and is true. Every member of a validity list is a flag.

  The tree is flattened into a list of loops
  by visiting nodes depth first, each node before its children. The root has
  depth 0 and children are one deeper than their node, except that the
  children of a spectral node have the spectral node's depth. A loop is its
  kind, depth, count, and period or step. For each node visited, in this
  order:
  1. without `uLoopPars`, or with a count of 0 (spectral nodes included): the
     node and its children are skipped (not visited, though still checked);
  2. spectral: the node is skipped and its children are visited (its planes
     are pixel components, not a loop of the output);
  3. otherwise: if the list is empty or the last loop's depth is less than
     the node's, the node's loop is appended. If the last loop has the same
     depth and kind, and its count is less than the node's count, the node's
     loop (with its period or step) replaces it. Otherwise the node's loop is
     dropped. Either way its children are visited.

  Rule 3 merges sibling branches that repeat one loop. A time loop with two
  position-loop children of 25 and 25 points gives [time, position]; the
  second position loop is dropped, and its children are still visited one
  level deeper. Rule 3 compares only with the last loop: if the first
  position loop had a z-loop child, the last loop is that z loop when the
  second position loop is visited, so the second is dropped whatever its
  count.

  Two loops of the same kind (two time loops, for example) in the final list
  are rejected.
- **Picture metadata** (chunk `ImageMetadataSeqLV|0!`, member
  `SLxPictureMetadata`; if the chunk or the member is absent there are no
  planes and the image is not calibrated):
  - flag `bCalibrated` (default false), number `dCalibration` (default
    absent), number `dAspect` (default 1);
  - planes: object `sPicturePlanes` (default absent: no planes), with
    integer `uiCount` (default 0) and object `sPlaneNew` (default absent).
    Plane `i` is the member `sPlaneNew/a<i>`, for `i < uiCount`. Each one
    present MUST be an object, with string `sDescription` (default empty),
    color `uiColor` (default `0xFFFFFF`) and integer `uiCompCount`
    (default 1).
  - calibration: the image is **calibrated** if `bCalibrated` is true and
    `dCalibration` is present and positive. A `dAspect` that is not positive
    counts as 1.
  - camera matrix: numbers `dStgLgCT11`, `dStgLgCT12`, `dStgLgCT21`,
    `dStgLgCT22` (defaults 1, 0, 0, 1), the matrix
    `C = [[dStgLgCT11, dStgLgCT12], [dStgLgCT21, dStgLgCT22]]` that maps
    image x and y offsets to stage x and y offsets.

### 4.4 Frames

The frames are numbered `0 ≤ f < N`, where `N` is the product of the loops'
counts (1 if there are none), which MUST be at most 2^53 − 1. Frame `f`'s coordinates are its row-major
index over the loops in list order (the last loop fastest). Frame `f` is the
chunk named `ImageDataSeq|<f>!` (`f` in decimal, without leading zeros); a
frame absent from the chunk map is missing (no entry). Other chunk names,
and frames with `f ≥ N`, are ignored.

A frame's data is an 8-byte timestamp followed by its pixels: `uiHeight`
rows of `uiWidthBytes` bytes, each holding `uiWidth × uiComp` samples
(interleaved by component) of `uiBpcInMemory / 8` little-endian bytes,
then padding. Let `R = uiWidth × uiComp × uiBpcInMemory / 8`;
`uiWidthBytes` MUST be at least `R`.

- **Uncompressed:** the virtualizer MUST read the headers of the present
  frames with the lowest and the highest numbers, and reject the file if
  their name lengths differ, or if either's data length `d` is less than
  `8 + uiHeight × uiWidthBytes`. It reads no other frame's header: it uses
  the first one's name length `n` for every frame, and frame `f`'s pixels
  start at `start = o + 16 + n + 8`, where `o` is its chunk's offset from the
  chunk map. (Other frames' chunks are not checked, except that their ranges
  MUST lie within the file, §1.2. A frame chunk's data need not lie within
  the file; only the ranges taken from it must.) Row `r` of the frame is the range
  `(0, start + r × uiWidthBytes, R)`.
  - If `uiWidthBytes = R`, the frame is one chunk, the single range
    `(0, start, uiHeight × R)`.
  - Otherwise (padded rows) the frame is split into **row blocks** of `h`
    rows: block `j` is the ranges of rows `j × h` to `j × h + h − 1`, in
    order. `h` is the largest divisor of `uiHeight` such that every block of
    every present frame has a payload of at most 65519 bytes (§1.2). (`h = 1`
    always qualifies, so padded frames are never rejected for their payload.)
- **Compressed:** `uiWidthBytes` MUST equal `R`. The virtualizer reads every
  present frame's header; the frame is one chunk, the single range
  `(0, o + 16 + n + 8, d − 8)`, a zlib stream of the pixels, and `d` MUST be
  more than 8.

Let `h` be `uiHeight` except for padded rows, where it is the block height.

### 4.5 Channels

Channels are the components. If `uiCount` is at least 1, every plane
`i < uiCount` exists, every plane's `uiCompCount` is 1 or 3, and they add up to `uiComp`,
the components are labeled plane by plane:

- a plane with one component gives one channel labeled `sDescription`,
  colored by `uiColor`;
- a plane with three components gives three channels labeled
  `<sDescription> R`, `<sDescription> G`, `<sDescription> B`, colored
  `FF0000`, `00FF00`, `0000FF`.

Otherwise channel `k` is labeled `C<k>` and colored `FFFFFF`. A `uiColor` is
`0xAABBGGRR`; its color is the six uppercase hexadecimal digits of red,
green and blue.

### 4.6 Output

The output is a bioformats2raw layout:

- `zarr.json`: a group with attributes
  `{"ome": {"version": "0.5", "bioformats2raw.layout": 3}}`.
- `OME/zarr.json`: a group with attributes
  `{"ome": {"version": "0.5", "series": ["0", "1", ...]}}`, one series per
  position (one if there is no position loop).
- For each position `p`, `"<p>/zarr.json"`: an image (§2.2) with
  `"name": "position <p>"` and one level, the array `"<p>/0"`.

Each array:

- **Axes:** `t` if there is a time loop (even of count 1), `c` if
  `uiComp > 1`, `z` if there is a z loop, then `y`, `x`. Shape: the loops'
  counts, `uiComp`, then `uiHeight`, `uiWidth` (each only for the axes
  present).
- **Chunk shape:** 1 for `t` and `z`, `uiComp` for `c`, then `h` (§4.4) and
  `uiWidth`: one frame, or one row block, per chunk.
- **Codecs** (§2.1): transpose when `c` is present (frames are interleaved),
  bytes (with `"endian": "little"` when the data type is larger than 1 byte),
  and zlib if compressed.
- **Units and scales:**
  - `x`: `micrometer` with scale `dCalibration` when calibrated, else no unit
    and scale 1;
  - `y`: `micrometer` with scale `dCalibration × dAspect` when calibrated,
    else no unit and scale 1;
  - `z`: `micrometer` with its step as scale when the step is positive, else
    no unit and scale 1;
  - `t`: `second` with scale `period / 1000` when the period is positive,
    else no unit and scale 1;
  - `c`: scale 1.
- **Stage positions:** with a position loop, each image also has a
  translation (§2.2) placing it where the stage was, when all of these hold:
  the image is calibrated; every position's stage position (`dPosX` and
  `dPosY` of the position loop's `p`-th valid member of `Points`, for
  position `p`) is present; and `d = dStgLgCT11 × dStgLgCT22 −
  dStgLgCT12 × dStgLgCT21` is not 0. Otherwise no image has one. The
  stage position `(sx, sy)` is the centre of the field of view; in image
  coordinates it is
  - `u = (dStgLgCT22 × sx − dStgLgCT12 × sy) / d`,
  - `v = (dStgLgCT11 × sy − dStgLgCT21 × sx) / d`

  (that is, `C⁻¹ (sx, sy)`), and the translation is
  `x = u − uiWidth × sx_scale / 2` and `y = v − uiHeight × sy_scale / 2`,
  where `sx_scale` and `sy_scale` are the x and y scales, and 0 for the other
  axes. (Each expression is computed left to right, products before
  differences.) The position loop is the one in the flattened list (§4.3):
  when rule 3 replaces a loop, the replacing node's `Points` are used.
- **omero:** every image's `M` has
  `"omero": {"channels": [...]}`, one object per channel (even when there is
  no `c` axis):
  `{"label": ..., "color": ..., "active": true, "window": {"min": 0, "max": V, "start": 0, "end": V}}`
  with `V = 2^b − 1`, where `b` is `uiBpcSignificant` if it is an integer
  from 1 to `uiBpcInMemory`, else `uiBpcInMemory`; for `float32` there is no
  `window`.
- **Chunks:** frame `f` at position `p` (0 without a position loop) is the
  entry `<p>/0/c/<coords>` with its ranges (§4.4); coords are its `t`, 0 for
  `c`, its `z` (each only when its axis is present), then 0, 0. With row
  blocks, block `j` is the entry with coords ..., `j`, 0.

## 5. Conformance

This section is informative. There are two maintained implementations:
- the Python reference, `python -m vzip.virtualize <url> <out.vzip>`
  (`src/vzip/virtualize/`);
- the browser one, `web/src/virtualize.ts` and `web/src/nd2.ts`, run under
  Node by `web/conformance/virtualize.ts`.

Implementations written from this document alone, round by round, are in
`impls/virtualize/`; `conformance/virtualize/REVISIONS.md` records what each
round found and how this document changed.

`conformance/virtualize/compare.py` runs implementations on a corpus and
compares their outputs by §1.1 (`conformance/virtualize/HARNESS.md`
describes the command an implementation provides). The corpus has:
- the synthetic TIFF and ND2 files in `web/test/fixtures/`, including inputs
  each profile rejects;
- the 205 OME-TIFFs of IDR idr0096;
- 17 public ND2 files (`conformance/virtualize/corpus_nd2.txt`).

Pixel correctness is checked separately, against independent readers:
`web/test/verify_tiff.py` against tifffile, `web/test/verify_nd2.py` against
the synthetic files' known pixels, and `experiments/verify_nd2_vzip.py`
against the `nd2` package on public files.
