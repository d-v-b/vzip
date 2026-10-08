# The IMS convention

The Zarr layout of an Imaris file (`.ims`, HDF5), and the translation of
its HDF5 attributes into JSON. What all of vzip's conventions share is in
[conventions/README.md](../README.md), cited here as "conventions §n". How
vzip produces this layout as a virtual store, and the subset of HDF5 that
it reads, is the IMS profile, [profiles/ims.md](../../profiles/ims.md).

Convention version: 1 · UUID: `5067a535-8261-4b25-a93c-1985ed333bde` ·
Schema: [schema.json](schema.json)

This convention gives a layout only to the files that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a file that fails has no layout under this
convention, and the profile rejects it.

## 1. Declaration

The root declares the convention by [conventions §2](../README.md#2-attributes),
with `"profile": "ims"`, `"version": 1`, the file's URL as `source.url`, and
the source metadata of §5 as the member `"ims"`. Its CMO is:

```json
{
  "uuid": "5067a535-8261-4b25-a93c-1985ed333bde",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/tags/virtualize-ims-v1/conventions/ims/schema.json",
  "spec_url": "https://github.com/d-v-b/vzip/blob/virtualize-ims-v1/conventions/ims/README.md",
  "name": "vzip_virtualized",
  "description": "The Zarr layout of a IMS source virtualized by vzip, and the source's metadata"
}
```

No other node declares it: the arrays have no source metadata.

## 2. The source

An Imaris file (`.ims`) is an HDF5 file holding a fixed layout of groups and
datasets (§2.2): one chunked 3-D dataset per resolution level, time point and
channel, with text attributes for the sizes and the metadata (§3). The
virtualizer reads the HDF5 structure itself, by the subset of the HDF5 file
format (The HDF Group's *HDF5 File Format Specification*, version 3) that
[profiles/ims.md §8.1](../../profiles/ims.md#81-hdf5-structures)–[profiles/ims.md §8.6](../../profiles/ims.md#86-attributes) describe; whatever falls outside the subset rejects the input. The
pixels are never read.

The subset is what real Imaris files use. Files written by Imaris 7 to 9,
ImarisWriter and Andor Fusion have superblock version 2, version 2 object
headers, compact groups and, for groups of more than 8 links or attributes,
dense ones (a fractal heap and a version 2 B-tree), long attributes as huge
heap objects, and chunks indexed by version 1 B-trees, uncompressed or with
deflate. Imaris 5.5 files have version 1 object headers and symbol-table
groups. Imaris 10 makes the root group's `DataSet` and `DataSetInfo` soft
links to `/Workflows/InitialImages/...`. Files written with newer HDF5
format bounds (superblock version 3, layout versions 4 and 5) index chunks by
fixed arrays or as a single chunk. Imaris 10 can also compress chunks with LZ4
(filter 32004), alone or after byte shuffling (filter 2); no codec of [conventions §3](../README.md#3-arrays)
decodes those, so such files are rejected.


### 2.1 Attributes as text

**Text.** Imaris writes each attribute as a 1-dimensional array of
1-character strings. An attribute read as **text** MUST have a string
datatype (class 3), of any size, padding and character set; any other
datatype rejects the input. Its text is its data up to the first NUL byte
(all of it if there is none), decoded as UTF-8 if it is valid UTF-8, and
otherwise byte by byte as the code points U+0000 to U+00FF (ISO 8859-1).

- A **decimal** is a text that, with leading and trailing whitespace ([VIRTUALIZE.md §1.3](../../VIRTUALIZE.md#13-arithmetic))
  removed, matches `[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?` and
  whose value ([VIRTUALIZE.md §1.3](../../VIRTUALIZE.md#13-arithmetic)) is finite. Any other text is not a decimal.
- A **list of `n` decimals** is a text that, with leading and trailing
  whitespace removed and split at each run of whitespace, has exactly `n`
  parts, each a decimal.
- An **integer** is a text that, with leading and trailing whitespace
  removed, is one or more digits.

### 2.2 The Imaris layout

Names below are ASCII; a number in a name is in decimal without leading
zeros. The virtualizer opens ([profiles/ims.md §8.3](../../profiles/ims.md#83-groups-and-links)) these groups, following each link ([profiles/ims.md §8.3](../../profiles/ims.md#83-groups-and-links))
to reach them:

- The root group. Without a link `DataSet`, the file is not an Imaris file
  and is rejected. That link leads to the group `DataSet`, which MUST have a
  link `ResolutionLevel 0` (else the file is not an Imaris file).
- **Levels:** `R` is the number of consecutive links `ResolutionLevel 0`,
  `ResolutionLevel 1`, ... of `DataSet`, which MUST be at most 64 (a link
  `ResolutionLevel 64` after 64 consecutive ones rejects the input).
- **Time points:** `T` is the number of consecutive links `TimePoint 0`,
  `TimePoint 1`, ... of the group `ResolutionLevel 0` (at least 1).
- **Channels:** `C` is the number of consecutive links `Channel 0`, ... of
  the group `ResolutionLevel 0/TimePoint 0` (at least 1).
- `R × T × C` MUST be at most 100000.
- For every level `r < R`, time point `t < T` and channel `c < C`, the
  link `ResolutionLevel r` of `DataSet`, `TimePoint t` of the group it
  leads to and `Channel c` of the group that leads to MUST exist; each group
  is opened. Other links of these groups (beyond `T` time points or `C`
  channels, `Histogram`, ...) are not followed. The channel group's
  attributes are read ([profiles/ims.md §8.6](../../profiles/ims.md#86-attributes)), and it MUST have a link `Data`, which leads to
  the **dataset** `(r, t, c)`, read by [profiles/ims.md §8.5](../../profiles/ims.md#85-datasets-and-chunks) with its allocated chunks.
  - The channel group's attributes `ImageSizeZ`, `ImageSizeY` and
    `ImageSizeX`, read as text, MUST be integers from 1 to 2^53 − 1: the
    image's size `(Z, Y, X)`. (Imaris pads the dataset to whole chunks; the
    image is its part from the origin.)
  - The dataset MUST have rank 3, its sizes (in order z, y, x) MUST be at
    least `Z`, `Y` and `X`, and its filters MUST be none, or exactly one
    with identifier 1 (deflate, whose chunks are zlib streams). Other filters
    (shuffle 2, Fletcher32 3, LZ4 32004, ...) reject the input.
  - Its **data type:** a datatype of class 0 (fixed-point) and size 1, 2 or 4
    whose bit offset (2 bytes at property byte 0) is 0 and precision (2 bytes
    at 2) is 8 × the size is `uint8`, `uint16` or `uint32`, or `int8`,
    `int16` or `int32` if bit 3 (signed) of the bit fields is set. A
    datatype of class 1 (floating-point) and size 4 is `float32` if its bit
    fields have bit 6 clear, bits 4–5 (mantissa normalization) equal to 2 and
    bits 8–15 (sign location) equal to 31, and its properties are the bit
    offset 0 and precision 32 (2 bytes each), exponent location 23, exponent
    size 8, mantissa location 0 and mantissa size 23 (1 byte each), and
    exponent bias 127 (4 bytes). Any other datatype rejects the input. Bit 0
    of the bit fields is the **byte order**: big-endian if set, else
    little-endian.

All datasets MUST have the same data type and, for data types of 2 or 4
bytes, the same byte order. All datasets of a level `r` MUST have the same
image size `(Zr, Yr, Xr)`, the same chunk shape `(cz, cy, cx)` and the same
filters (both none or both deflate). Levels may differ in chunk shape and
filters.

## 3. Metadata

If the root group has a link `DataSetInfo`, it is followed and the group
opened. Each of that group's links `Image`, `Channel c` (for `c < C`)
and `TimeInfo` that exists is followed, the group opened, and its
attributes read; a missing group has no attributes. These attributes are
read as text ([profiles/ims.md §8.6](../../profiles/ims.md#86-attributes)) where present:

- of `Image`: `ExtMin0`, `ExtMin1`, `ExtMin2`, `ExtMax0`,
  `ExtMax1`, `ExtMax2` (each a decimal, else absent), `Unit` and
  `Name`;
- of `Channel c`: `Name`, `Color` (a list of 3 decimals, else absent)
  and `ColorRange` (a list of 2 decimals, else absent);
- of `TimeInfo`, only if `T > 1`: `TimePoint1` and `TimePoint<T>`.

Other attributes, and the other groups (`Thumbnail`, `Scene`,
`DataSetTimes`, ...), are not read.

- **Extents.** For the axes `x`, `y` and `z` (`i` = 0, 1, 2): if
  `ExtMin<i>` and `ExtMax<i>` are both present, let
  `e = ExtMax<i> − ExtMin<i>`; if `e` is infinite the input is rejected
  ([VIRTUALIZE.md §1.3](../../VIRTUALIZE.md#13-arithmetic)), and if `e > 0` the axis has the **extent** `e`.
- **Unit.** If `Unit` is absent or empty, the unit is `micrometer`, Imaris's
  default. Otherwise it is the unit of `Unit` by
  [conventions §5](../README.md#5-units) (matched exactly) if that is a length unit, and there is none if not.
- **Time step.** Imaris records the date and time of each time point, not a
  period. A time point's text is **valid** if it is exactly `YYYY-MM-DD
  hh:mm:ss`, optionally followed by `.` and one or more digits, with each
  letter a digit; with month `MM` from 1 to 12, day `DD` from 1 to the
  month's length (February has 29 days in years divisible by 4 but not by
  100, and in years divisible by 400), `hh` at most 23 and `mm` and `ss`
  at most 59. Its whole seconds are
  `I = 86400 d + 3600 hh + 60 mm + ss`, where `d` is the number of days
  from 1970-01-01 to its date in the proleptic Gregorian calendar (exact
  integers), and its fraction `F` is the decimal `0.` followed by its
  digits after the `.` (0 without them). If `T > 1` and `TimePoint1`
  (`I1`, `F1`) and `TimePoint<T>` (`IT`, `FT`) are both valid, let
  `Δ = D + (FT − F1)`, where `D = IT − I1` is the integer difference
  computed exactly and then converted to binary64, `FT` and `F1` are the
  fractions converted to binary64 ([VIRTUALIZE.md §1.3](../../VIRTUALIZE.md#13-arithmetic)), and the subtraction and addition
  are binary64 operations in that order. If
  `Δ > 0` the **time step** is `Δ / (T − 1)` seconds, the mean interval
  between time points. Otherwise there is no time step.

## 4. Output

One image at the archive root ([conventions §4](../README.md#4-images)), with one array per level `r` at path
`"<r>"`.

- **Axes:** `t` if `T > 1`; `c` if `C > 1`; `z` if level 0's `Z` is
  more than 1 or some level's `cz` is more than 1 (a chunk of several z
  planes keeps them, so that its bytes decode to its Zarr chunk); then `y`,
  `x`. When level 0's `Z` is 1, every level's `Z` MUST be 1.
- **Array** of level `r` ([conventions §3](../README.md#3-arrays)): shape `T`, `C`, `Zr`, `Yr`, `Xr`
  and chunk shape 1, 1, `cz`, `cy`, `cx` (each only for the axes
  present); the data type of §2.2; codecs `bytes` (with the byte order as
  its `endian` for data types of 2 or 4 bytes), followed by `zlib` for
  deflate.
- **Scale** of level `r`: a spatial axis with an extent `e` has the scale
  `e / Nr`, where `Nr` is the level's size along it (`Xr`, `Yr` or
  `Zr`), and the unit; one without has the scale 1 and no unit. (Imaris's
  extents span the image at every level, so a level's voxel size is the extent
  over its size.) `t` has the scale of the time step and the unit
  `second` if there is a time step, else the scale 1 and no unit. `c` has
  the scale 1.
- **Translation:** when every spatial axis present has an extent, every level
  has the translation ([conventions §4](../README.md#4-images)) `ExtMin0` for `x`, `ExtMin1` for
  `y`, `ExtMin2` for `z` and 0 for `t` and `c`: Imaris's extents are
  the outer corners of the image. Otherwise there is none.
- **Name:** `Name` of `Image`, if present and not empty.
- **omero:** `M` has `"omero": {"channels": [...]}`, one object per
  channel `c` (even when there is no `c` axis):
  `{"label": ..., "color": ..., "active": true, "window": {...}}`.
  - The label is the channel's `Name` if present and not empty, else
    `Channel <c>`.
  - The color: if `Color` is present and its three values are each from 0
    to 1, the six uppercase hexadecimal digits of `floor(v × 255 + 0.5)` for
    its red, green and blue values `v`; else `FFFFFF`.
  - The window: for an integer data type, `{"min": lo, "max": hi,
    "start": s, "end": e}`, where `lo` and `hi` are the smallest and
    largest values of the data type (0 and 2^n − 1 unsigned, −2^(n−1) and
    2^(n−1) − 1 signed, for `n` bits), and `s`, `e` are the two values of
    `ColorRange` if it is present, else `lo` and `hi`. For `float32`,
    `{"min": s, "max": e, "start": s, "end": e}` if `ColorRange` is
    present, else no window.
- **Chunks:** the chunk of level `r` with coords `t`, `c`, `i` (each only
  when its axis is present), `j`, `k` is the chunk at grid coordinates
  `(i, j, k)` of dataset `(r, t, c)`, as HDF5 stores it (zlib-compressed
  for deflate). It is present when that chunk is allocated and not wholly
  outside the image (`i × cz < Zr`, `j × cy < Yr` and `k × cx < Xr`);
  otherwise it is absent (the fill value, 0). HDF5 stores edge chunks
  whole, like Zarr, so a chunk's bytes decode to its Zarr chunk.

## 5. Source metadata

The root's source metadata `S` ([conventions §2](../README.md#2-attributes))
holds the attributes that Imaris writes, so that what the file records
(the image's extents and units, the channels' names, colors, ranges and
acquisition settings, the time points, the microscope and the Imaris
version, the histogram range of each channel) is kept. It is an object
with these members:

- `root`: the root group's attributes;
- `DataSetInfo`: an object with one member per link of the group
  `DataSetInfo` (none if the root has no such link), named by the link's
  name (as text by [conventions §6](../README.md#6-source-metadata-as-json)),
  in ascending byte order of the names: the attributes of the group the
  link leads to (followed as the profile follows links), or `null` if it
  does not lead to a group the profile can open;
- `DataSet`: an object with one member per dataset `(r, t, c)` of §2.2,
  named `ResolutionLevel r/TimePoint t/Channel c`, in the order of §2.2
  (`r`, then `t`, then `c`): the channel group's attributes.

An object's **attributes** are `null` if the profile cannot read them
([profiles/ims.md §8.6](../../profiles/ims.md#86-attributes): for example
a shared attribute message); otherwise a JSON object with one member per
attribute, named by its name as text, in ascending byte order of the
names, whose value is:

- `null` if the value cannot be read (a shared datatype or dataspace, a
  null dataspace, a datatype or dataspace that does not parse, data
  outside the message), or if the budget below is spent;
- for a string datatype (class 3) of size 1, the text of its data up to
  the first NUL (the form Imaris writes: a 1-dimensional array of
  1-character strings); for a string datatype of size `s > 1`, an array of
  the texts of its elements, each its `s` bytes up to the first NUL. Text
  is by [conventions §6](../README.md#6-source-metadata-as-json) (UTF-8 if
  valid, else ISO 8859-1);
- for a fixed-point datatype (class 0) of size 1, 2, 4 or 8, an array of
  its elements as integers, signed if bit 3 of the bit fields is set, in
  the byte order of bit 0;
- for a floating-point datatype (class 1) of size 4 or 8, an array of its
  elements as IEEE 754 binary32 or binary64 numbers, in the byte order of
  bit 0;
- otherwise (and for a datatype of size 0), `{"class": c, "size": s,
  "data": B}`: the datatype's class and size, and its data in base64.

Numbers follow conventions §6. Elements are in the dataspace's row-major
order.

**Budget.** The attributes are taken in the order of `S` (`root`, then
`DataSetInfo`, then `DataSet`, each object's attributes in their order),
and a value is read only when the size of its data, added to the sizes of
the values read before it, is at most 2^26 bytes (64 MiB); otherwise it
is `null`.

Datasets other than the image data (the thumbnail, `DataSetTimes`,
`Scene`) are not in `S`: their storage is outside the subset of HDF5 that
the profile reads.

## 6. Example

The root of a file at `https://example.org/a.ims`:

```json
"vzip_virtualized": {
  "profile": "ims",
  "version": 1,
  "source": {
    "url": "https://example.org/a.ims"
  },
  "ims": {
    "root": {
      "DataSetDirectoryName": "DataSet",
      "DataSetInfoDirectoryName": "DataSetInfo",
      "ImarisDataSet": "ImarisDataSet",
      "ImarisVersion": "5.5.0",
      "NumberOfDataSets": [
        1
      ]
    },
    "DataSetInfo": {
      "Image": {
        "Description": "test image",
        "ExtMax0": "1.5",
        "ExtMax1": "1.25",
        "ExtMax2": "3.0",
        "ExtMin0": "0.0",
        "ExtMin1": "0.0",
        "ExtMin2": "0.0",
        "Name": "synthetic",
        "RecordingDate": "2021-03-04 05:06:07.000",
        "Unit": "um",
        "X": "6",
        "Y": "5",
        "Z": "3"
      }
    },
    "DataSet": {
      "ResolutionLevel 0/TimePoint 0/Channel 0": {
        "ImageSizeX": "6",
        "ImageSizeY": "5",
        "ImageSizeZ": "3"
      },
      "ResolutionLevel 0/TimePoint 0/Channel 1": {
        "ImageSizeX": "6",
        "ImageSizeY": "5",
        "ImageSizeZ": "3"
      },
      "ResolutionLevel 0/TimePoint 0/Channel 2": {
        "ImageSizeX": "6",
        "ImageSizeY": "5",
        "ImageSizeZ": "3"
      },
      "ResolutionLevel 0/TimePoint 0/Channel 3": {
        "ImageSizeX": "6",
        "ImageSizeY": "5",
        "ImageSizeZ": "3"
      },
      "ResolutionLevel 0/TimePoint 0/Channel 4": {
        "ImageSizeX": "6",
        "ImageSizeY": "5",
        "ImageSizeZ": "3"
      }
    }
  }
}
```
