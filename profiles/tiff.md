# Virtualizing TIFF files

The TIFF profile of [VIRTUALIZE.md](../VIRTUALIZE.md) (revision 16), numbered
as its §3. §1 and §2 are in VIRTUALIZE.md and apply here.

Profile version: 1 · Convention: [conventions/tiff](../conventions/tiff/README.md),
version 1. The output has the convention's layout for the input
([VIRTUALIZE.md §2](../VIRTUALIZE.md#2-the-zarr-layout)); this profile says
how the file is read, which inputs are rejected, and how each chunk
references the file. "The convention §n" below is a section of
conventions/tiff/README.md.

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
- **Tags:** the tags that the convention's layout uses (the table of
  [the convention §2](../conventions/tiff/README.md#2-the-source)) are
  checked below. Every other tag is read only for the source metadata
  ([the convention §5](../conventions/tiff/README.md#5-source-metadata)),
  which never rejects the input: its field type and value are not checked.
  Of duplicate tags in an IFD, the first is used and the others are ignored.

**Checks on every IFD read.** For every tag of the convention's table present in an IFD
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

### 3.2 Rejection

The input is rejected when a check of §3.1 fails, and when the convention
gives it no layout: wherever the convention says that the input is
rejected, or that something MUST hold and it does not
([the convention §2–§4](../conventions/tiff/README.md#2-the-source)).

### 3.3 Chunks

Every chunk that the convention says is present
([the convention §4.4](../conventions/tiff/README.md#44-arrays)) is the
entry `<level>/c/<coords>`. Tile `k`, with TileByteCounts `n > 0`, has the
single range `(0, TileOffsets[k], n)` (for JPEG, see below). The references
are listed (for §1.2's order of first use) level by level, within a level
by `t`, then `c`, then `z`, and within a plane by `k`. The archive has no
other entries than the root's and the arrays' `zarr.json`.

**JPEG tiles.** A JPEG-in-TIFF tile is a JPEG stream that may omit its
tables (which are in the IFD's JPEGTables) and the color transform (which
is given by PhotometricInterpretation). Each tile's reference makes it the
complete stream that the convention's chunk holds: the ranges
`[P, (0, TileOffsets[k] + 2, n − 2)]`, where `n` MUST be more than 2 (the
tile's first 2 bytes, its SOI marker, are dropped), and `P` is a range of
the data source (§1.2) that holds the shared byte string:

- `FF D8` (SOI);
- for 3 samples, the Adobe marker `FF EE 00 0E 41 64 6F 62 65 00 64 00 00
  00 00 T`, with transform `T` = 0 for PhotometricInterpretation 2 (the
  samples are RGB as stored) and 1 for 6 (YCbCr);
- if the IFD has JPEGTables: its bytes without the first 2 and the last 2.
  JPEGTables MUST then be at least 4 bytes long, start with `FF D8` and
  end with `FF D9`.

These bytes depend only on the IFD, so every tile of an IFD has the same
`P`, and IFDs with the same bytes (as several levels of a slide often
have) share one data source.
