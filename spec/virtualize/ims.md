# IMS

The Zarr layout of an Imaris file (`.ims`, HDF5), and the translation of
its HDF5 attributes into JSON. What all of vzip's conventions share is in
[spec/conventions.md](../conventions.md), cited here as "conventions §n". How
vzip produces this layout as a virtual store, and the subset of HDF5 that
it reads, is the IMS profile, [Part 2](#part-2-the-profile) below.

Convention version: 0 (until release, conventions §1) · UUID: `5067a535-8261-4b25-a93c-1985ed333bde` ·
Schema: [schema.json](ims/schema.json)

This document has two parts. [Part 1](#part-1-the-convention) is the IMS
convention: the Zarr layout, cited as "the convention §n". [Part 2](#part-2-the-profile)
is the IMS profile: how vzip reads the source, which inputs it rejects, and how
each chunk references the source. The profile keeps its numbering as a section
of [spec/virtualize.md](../virtualize.md), and is cited as spec/virtualize.md §n.

# Part 1. The convention

This convention gives a layout only to the files that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a file that fails has no layout under this
convention, and the profile rejects it.

## 1. Declaration

The root declares the convention by [conventions §2](../conventions.md#2-attributes),
with `"profile": "ims"`, `"version": 0`, `"revision": 24` (conventions §1) and the file's URL as `source.url`.
The root has no source metadata of its own: the whole HDF5 file is described
on the source metadata node (§5). Its CMO is:

```json
{
  "uuid": "5067a535-8261-4b25-a93c-1985ed333bde",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/heads/main/spec/virtualize/ims/schema.json",
  "spec_url": "https://github.com/d-v-b/vzip/blob/main/spec/virtualize/ims.md",
  "name": "vzip_virtualized",
  "description": "The Zarr layout of a IMS source virtualized by vzip, and the source's metadata"
}
```

Every node under `vzip_source` that has source metadata (§5) declares it
with `{"ims": S}`.

## 2. The source

An Imaris file (`.ims`) is an HDF5 file holding a fixed layout of groups and
datasets (§2.2): one chunked 3-D dataset per resolution level, time point and
channel, with text attributes for the sizes and the metadata (§3). The
virtualizer reads the HDF5 structure itself, by the subset of the HDF5 file
format (The HDF Group's *HDF5 File Format Specification*, version 3) that
[spec/virtualize.md §8.1](#81-hdf5-structures)–[spec/virtualize.md §8.6](#86-attributes) describe; whatever falls outside the subset rejects the input. The
image's pixels are never read.

The subset is what real Imaris files use. Files written by Imaris 7 to 9,
ImarisWriter and Andor Fusion have superblock version 2, version 2 object
headers, compact groups and, for groups of more than 8 links or attributes,
dense ones (a fractal heap and a version 2 B-tree), long attributes as huge
heap objects, and chunks indexed by version 1 B-trees, uncompressed or with
deflate. Imaris 5.5 files have version 1 object headers and symbol-table
groups. Imaris 10 makes the root group's `DataSet` and `DataSetInfo` soft
links to `/Workflows/InitialImages/...`. Files written with newer HDF5
format bounds (superblock version 3, layout versions 4 and 5) index chunks by
fixed arrays or as a single chunk, by extensible arrays or version 2
B-trees when dimensions are unlimited, and implicitly when chunks are
allocated early. Imaris 10 can also compress chunks with LZ4
(filter 32004), alone or after byte shuffling (filter 2); no codec of [conventions §3](../conventions.md#3-arrays)
decodes those, so such files are rejected.


### 2.1 Attributes as text

**Text.** Imaris writes each attribute as a 1-dimensional array of
1-character strings. An attribute read as **text** MUST have a string
datatype (class 3), of any size, padding and character set; any other
datatype rejects the input. Its text is its data up to the first NUL byte
(all of it if there is none), decoded as UTF-8 if it is valid UTF-8, and
otherwise byte by byte as the code points U+0000 to U+00FF (ISO 8859-1).

- A **decimal** is a text that, with leading and trailing whitespace ([spec/virtualize.md §1.3](../virtualize.md#13-arithmetic))
  removed, matches `[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?` and
  whose value ([spec/virtualize.md §1.3](../virtualize.md#13-arithmetic)) is finite. Any other text is not a decimal.
- A **list of `n` decimals** is a text that, with leading and trailing
  whitespace removed and split at each run of whitespace, has exactly `n`
  parts, each a decimal.
- An **integer** is a text that, with leading and trailing whitespace
  removed, is one or more digits.

### 2.2 The Imaris layout

Names below are ASCII; a number in a name is in decimal without leading
zeros. The virtualizer opens ([spec/virtualize.md §8.3](#83-groups-and-links)) these groups, following each link ([spec/virtualize.md §8.3](#83-groups-and-links))
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
  attributes are read ([spec/virtualize.md §8.6](#86-attributes)), and it MUST have a link `Data`, which leads to
  the **dataset** `(r, t, c)`, read by [spec/virtualize.md §8.5](#85-datasets-and-chunks) with its allocated chunks.
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
opened. Each of that group's links `Image`, `Channel c` (for `c < C`,
when `C` is at most 64) and `TimeInfo` that exists is followed, the group opened, and its
attributes read; a missing group has no attributes. These attributes are
read as text ([spec/virtualize.md §8.6](#86-attributes)) where present:

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
  ([spec/virtualize.md §1.3](../virtualize.md#13-arithmetic)), and if `e > 0` the axis has the **extent** `e`.
- **Unit.** If `Unit` is absent or empty, the unit is `micrometer`, Imaris's
  default. Otherwise it is the unit of `Unit` by
  [conventions §5](../conventions.md#5-units) (matched exactly) if that is a length unit, and there is none if not.
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
  fractions converted to binary64 ([spec/virtualize.md §1.3](../virtualize.md#13-arithmetic)), and the subtraction and addition
  are binary64 operations in that order. If
  `Δ > 0` the **time step** is `Δ / (T − 1)` seconds, the mean interval
  between time points. Otherwise there is no time step.

## 4. Output

One image at the archive root ([conventions §4](../conventions.md#4-images)), with one array per level `r` at path
`"<r>"`.

- **Axes:** `t` if `T > 1`; `c` if `C > 1`; `z` if level 0's `Z` is
  more than 1 or some level's `cz` is more than 1 (a chunk of several z
  planes keeps them, so that its bytes decode to its Zarr chunk); then `y`,
  `x`. When level 0's `Z` is 1, every level's `Z` MUST be 1.
- **Array** of level `r` ([conventions §3](../conventions.md#3-arrays)): shape `T`, `C`, `Zr`, `Yr`, `Xr`
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
  has the translation ([conventions §4](../conventions.md#4-images)) `ExtMin0` for `x`, `ExtMin1` for
  `y`, `ExtMin2` for `z` and 0 for `t` and `c`: Imaris's extents are
  the outer corners of the image. Otherwise there is none.
- **Name:** `Name` of `Image`, if present, not empty and at most 256 bytes
  long in UTF-8.
- **omero:** when `C` is at most 64, `M` has `"omero": {"channels": [...]}`,
  one object per channel `c` (even when there is no `c` axis):
  `{"label": ..., "color": ..., "active": true, "window": {...}}`. With more
  channels `M` has no `"omero"` member, so that the root does not grow with
  `C` (the channels' metadata is kept in the source metadata, §5).
  - The label is the channel's `Name` if present, not empty and at most 256
    bytes long in UTF-8, else `Channel <c>`.
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

An Imaris file is an HDF5 file, and the image (§4) is only part of it. The
rest (the attributes of every group, which hold the extents, units,
channels, acquisition settings and time points; the histograms; the
thumbnail; `DataSetTimes`; the objects of `Scene` and `Scene8`; and
whatever else a writer adds) is kept by mirroring the HDF5 file as a Zarr
hierarchy under `vzip_source/hdf5`: each HDF5 group is a Zarr group, each
dataset is a Zarr array (variable-length data is a family of byte values,
references are indexes into a table of the objects, a compound with such
members and a dataset with a null dataspace are groups), and attributes,
links and datatypes are JSON. A reader can rebuild every group, dataset (its data,
datatype, shape and fill value), attribute (its value, datatype and shape),
link and named datatype of the file from it, with every text and name as
its bytes, except for what §5.9 lists. Large values are arrays, and each
document has a budget, past which its entries are moved to an array, so
that no document grows with the data or with the file's names (conventions §7).

### 5.1 The tree

The groups are walked from the root group, depth first: a group's members
are taken in ascending byte order of their names, and a member group is
walked after its parent's other members, before the parent's next group.
The **path** of an object is `/` for the root group, else its parent's path
(without a final `/`) followed by `/` and its key.

**Names.** A member's **key** is its name read as text, plainly
([conventions §6](../conventions.md#6-source-metadata-as-json)): UTF-8 if valid,
else ISO 8859-1. A member whose key is an earlier member's (in the order
above) is not described; its name, as a text value, is listed in
`collisions`, in that order. A kept member whose name is not valid UTF-8
has the member `key` in `names`, its name as a text value (`{"latin1":
...}`), so that its bytes are known.

The group at path `P` is the Zarr group `vzip_source/hdf5` followed by `P`
without a final `/` (`vzip_source/hdf5` for the root group), and each of
its members with key `name` is, in this order of tests:

1. a **soft link**: the member `name` of `links` is `{"soft": T}`, `T` the
   link's path as a text value;
2. a link of another type than hard and soft ([spec/virtualize.md §8.9](#89-source-metadata-structures)): an
   **external link** (type 64) whose value is a 0 byte, then a file name
   up to its first NUL, then an object path up to its first NUL, which is
   the value's last byte, is `{"external": {"file": F, "path": Q}}` (text
   values, without the NULs); any other is
   `{"user": {"type": n, "value": B}}`, `n` the link's type and `B` its
   value in base64 (`null` if the value does not lie within the link
   message). Either is the member `name` of `links`;
3. a hard link to an object that the walk met before: the member `name` of
   `links` is `{"hard": i}`, `i` the object's index in the object table
   (§5.7), which gives the path where the walk met it;
4. a member beyond the walk's `100,000 + 5 × N`th object, `N` the number
   of the image's `Data` datasets (§2.2, each object counted once), the
   root group the first: not described; the group's `overflow` is the
   number of such members. The 5 objects per `Data` dataset are room for
   it, its channel group, `Histogram`, `Histogram1024` and a time point
   group, so that the image's tree does not use up the walk before
   `DataSetInfo`, `Scene` or `Thumbnail` (since `R × T × C` is at most
   100,000, the walk meets at most 600,000 objects);
5. otherwise, the object (an **object** is met here, once), by its kind,
   which its object header gives (a layout message: a dataset; a symbol table
   or link info message: a group; else a datatype message: a named
   datatype):
   - a **named datatype**: the member `name` of `datatypes` is
     `{"datatype": D}` with its description (§5.2), and its attributes
     (§5.3);
   - one of the image's `Data` datasets (§2.2): the member `name` of
     `images` is `{"level": r, "t": t, "c": c, "shape": [z, y, x]}`, the
     shape being the dataset's dimensions, with its attributes (§5.3).
     Its data within the image is the image's (§4); what lies outside it
     (the chunks' padding past `ImageSize`) is not kept;
   - a group or a dataset whose key is not a Zarr node name of the
     hierarchy (empty, `.`, `..`, starting with `__`, holding a `/`, or
     `zarr.json`): the member `name` of `unsupported` (§5.5);
   - a **group**: walked, as the Zarr group at its path;
   - any other **dataset**: §5.4.
   If the object cannot be read (a structure outside the profile's subset,
   or a dataset that §5.4 cannot map), the member `name` of `unsupported`
   is its record (§5.5) instead.

The group's source metadata `S` is the object with the members
`attributes` and `attribute_collisions` (§5.3), `names`, `collisions`,
`links`, `images`, `datatypes`, `unsupported`, `overflow` and `spilled`
(§5.6), each present only when it is not empty (or 0).

### 5.2 Datatypes

A datatype is described by a JSON object `D` with `"class"` and `"size"`
(its size in bytes), and, by class (`order` is `"little"`, or `"big"` when
bit 0 of the class bit fields is set; `signed` is bit 3):

| HDF5 class | description |
|---|---|
| 0 fixed-point | `{"class": "integer", "size", "order", "signed"}`, with `"offset"`, `"precision"` and `"padding"` (bits 1 and 2) when the bit offset is not 0, the precision is not 8 × size, or a padding bit is set |
| 1 floating-point | `{"class": "float", "size", "order"}` (`"order": "vax"` when bit 6 is set), with `"layout": {"offset", "precision", "sign", "exponent": [location, size, bias], "mantissa": [location, size, normalization], "padding": [bits 1, 2, 3]}` unless it is IEEE 754 binary16, binary32 or binary64 |
| 2 time | `{"class": "time", "size", "order", "precision"}` |
| 3 string | `{"class": "string", "size", "padding", "charset"}`: `"null-terminated"`, `"null-padded"` or `"space-padded"`; `"ascii"` or `"utf-8"` (the number for other values) |
| 4 bitfield | as fixed-point, with `"class": "bitfield"` and no `"signed"` |
| 5 opaque | `{"class": "opaque", "size", "tag"}`, the tag a text value up to its first NUL |
| 6 compound | `{"class": "compound", "size", "members": [{"name", "offset", "type"}, ...]}`, in the message's order; a version 1 member with dimensions has an array type |
| 7 reference | `{"class": "reference", "size", "kind"}`, `kind` bits 0–3 (0 an object reference, 1 a region reference) |
| 8 enumeration | `{"class": "enum", "size", "base", "members": {name: value}}`, the base an integer datatype, names in the message's order (the first of equal names kept) |
| 9 variable-length | `{"class": "variable-length", "size", "base"}`, with `"string": true`, `"padding"` and `"charset"` for a string |
| 10 array | `{"class": "array", "size", "shape", "base"}` |

Datatype messages of versions 1 to 5 are read; nesting deeper than 16
levels, an enumeration whose base is not an integer, and other classes make
the datatype unreadable. A **number** datatype is an integer of 1, 2, 4 or
8 bytes with no `offset`, or an IEEE float of 2, 4 or 8 bytes, little- or
big-endian; its Zarr data type is `int8` … `uint64`, `float16`, `float32`
or `float64`. A datatype **holds addresses** when it is a reference or
variable-length, or a compound or array with such a member or base at any
depth: its elements hold file addresses or heap IDs, which are never kept.
An **object reference** is a reference of kind 0 and size 8, a **region
reference** one of kind 1 and size 12.

**Named datatypes.** A dataset's or an attribute's datatype that is shared
([spec/virtualize.md §8.9](#89-source-metadata-structures)) is the committed datatype's own. When the walk met
the committed datatype, the object that describes it (a dataset's `S`, an
attribute's value, an unsupported record) has `"named": i`, the committed
datatype's index in the object table (§5.7), instead of `"datatype": D`:
`D` is in that datatype's entry of `datatypes`, once. Otherwise it has
`"named": null` and `"datatype": D`. Indexes are given once the walk is
over, so a datatype met after its users is named all the same. Below, the
**datatype** of such an object is `D` either way.

### 5.3 Attributes

An object's attributes are the member `attributes` of its description (the
group's or dataset's `S`, or its entry in `images`, `datatypes` or
`unsupported`): `null` if the profile cannot read them ([spec/virtualize.md §8.6](#86-attributes)), else an object with one
member per attribute, named by its key (its name as text, as §5.1 reads
member names), in ascending byte order of the names. An attribute whose
key is an earlier attribute's is listed instead in the member
`attribute_collisions` (an array, in that order), its value with the
member `"name"`, its name as a text value. Any other attribute whose name is
not valid UTF-8 is kept in `attributes`, its value with `"name": {"latin1":
...}`. Both members are present only when they are not empty.

**Values.** With `dims` the attribute's dataspace dimensions, `n` their
product and `D` its datatype (§5.2), an attribute's value is the object
`{"datatype": D, ...}` (or `"named"`, §5.2) with one of these:

- **null dataspace**: `"shape": null`, and nothing else;
- **value form**: `"value": V`, where `V` is the single value when `dims`
  is empty, else the list of the `n` values in row-major order, with
  `"shape": dims` when `dims` has more than one dimension. The values are:
  - for a **string** datatype: a 1-dimensional attribute of size 1 whose
    data has no NUL byte has the text value of all its data as `V` (the form
    Imaris writes: a 1-dimensional array of 1-character strings). Otherwise
    each element's text: its bytes without the trailing padding bytes
    (spaces for `"space-padded"`, else NUL), as a text value
    ([conventions §6](../conventions.md#6-source-metadata-as-json)), when no NUL
    is left in any element; else the data form. A string datatype of size 0
    has `n` empty texts;
  - for a **number** datatype: its numbers (conventions §6), when `n` is
    at most 64 and JSON holds each exactly: a NaN only as the quiet NaN with
    no payload and the sign bit clear (`0x7E00`, `0x7FC00000`,
    `0x7FF8000000000000`), and no negative zero; else the data form;
  - for a **variable-length** datatype whose base does not hold addresses:
    each element, read from the global heap: a string's text value (all its
    bytes), a sequence of numbers' list of numbers (held exactly, as above),
    or else the base64 of the sequence's bytes;
  - for an **object reference**: the index in the object table (§5.7) of
    the object the reference holds (an object header's address), or `null`
    if the walk met none there (the null reference, 0, included);
  - for **any other datatype that holds addresses**, each element's value
    `E` by its datatype `t`:
    - `t` does not hold addresses: a number when `t` is a number datatype
      and JSON holds it exactly (as above), else the base64 of its bytes;
    - an object reference: as above;
    - a region reference: `null` for the null reference, else `{"object": i,
      "selection": S}`, `i` the object its global heap object refers to (as
      an object reference gives it), `S` its selection ([spec/virtualize.md §8.9](#89-source-metadata-structures)):
      `{"select": "all"}` or `{"select": "none"}`; `{"select": "points",
      "rank": k, "points": [[c, ...], ...]}`, the points' coordinates;
      `{"select": "hyperslab", "rank": k, "start": [...], "stride": [...],
      "count": [...], "block": [...]}` for a regular hyperslab (a count or
      block `"unlimited"` when all its bits are set); or `{"select":
      "hyperslab", "rank": k, "blocks": [[[start, ...], [end, ...]], ...]}`,
      each block's first and last coordinates, inclusive (each number as
      conventions §6 writes integers);
    - another reference: none (the attribute has a reason);
    - variable-length: a string's text value; a sequence whose base does
      not hold addresses as above; else the list of its elements' `E`;
    - compound: an object with one member per compound member, by its name
      (as the description gives it), its `E`; members whose names are equal
      leave the attribute with a reason;
    - array: the list of its elements' `E`, in row-major order.

    Global heap objects are read where an element refers to one. A region
    reference counts the size of its global heap object against a budget of
    2^22 bytes (4 MiB) of selections per file, each time it is read (a
    global heap object is read once, so that the references that share it
    count its size but do not read it again): first the datasets' (§5.4),
    as the walk reads them, then the attributes', in the order the
    documents are settled. A reference past the budget cannot be read; the
    selections of an attribute or a dataset that cannot be kept (a reason,
    or an unsupported record) are not counted;
- **data form**: `"data": B`, the attribute's data in base64, with
  `"shape": dims` unless `dims` has one dimension: for any other datatype
  that does not hold addresses, and for strings and numbers that the value
  form cannot hold;
- **array form**: `"array": "attributes/<i>"` (or `"json"`, below), the
  `i`th such array (from 0, in the order the documents are settled, §5.6)
  under `vzip_source`, holding the attribute's data (conventions §7), with
  `"shape": dims` unless `dims` has one dimension and is the array's shape
  (or, for a family, its number of members):
  - a string's data bytes: a `uint8` array of the data's length;
  - numbers: an array of their Zarr data type, in the attribute's byte
    order, of shape `dims`;
  - variable-length data whose base does not hold addresses: a family of
    byte values (conventions §7) at that path, one member per element, each
    referenced where the global heap holds it;
  - object references: an `int32` array of shape `dims`, each the index of
    the referenced object in the object table (§5.7), or −1 when the value
    form has `null`, copied;
  - any other datatype that holds addresses: `"json": "attributes/<i>"`, a
    `uint8` array of the JSON text (§5.6) of the value form's `V`, copied,
    with `"shape": dims` when `dims` has more than one dimension;
  - any other data: `uint8` of shape `dims` followed by the datatype's size.

  Each array is cut as conventions §7 cuts contiguous values. The arrays of
  a string's data bytes, of numbers and of any other data reference the
  attribute's data where the file holds it, one run of bytes (in the block
  of the object header that holds the attribute message, or in the fractal
  heap object of a dense attribute, managed or huge); the others are
  copied;
- **reason**: `"reason": T`, an informative English text, with `"shape":
  dims` unless `dims` has one dimension, when the data cannot be kept: a
  reference other than an object or region reference, a variable-length
  element, a global heap object or a selection that cannot be read (a
  region reference past the budget of selections, above, included),
  compound members of equal names, more than 2^20 elements of a datatype of
  size 0, or when the walk's budget is spent (it
  reads at most 2^26 bytes, 64 MiB, of attribute data, counting each
  attribute's data, and each global heap object read for its value, as it
  reads it, in the order the documents are settled).

An attribute whose datatype or dataspace cannot be read is `{"reason": T}`.
The one exception to the object is **Imaris's form**: an attribute whose
datatype is not shared, `D` being `{"class": "string", "size": 1,
"padding": "null-terminated", "charset": "ascii"}`, of one dimension, whose
data has no NUL byte and whose name is valid UTF-8 and not a collision, is
the bare text value of its data.

**Which form.** An attribute is **large** when it is a string of data over
2^20 bytes, more than 64 numbers or object references, data held in the
data form over 4096 bytes, or variable-length data whose base does not hold
addresses whose elements total over 2^16 bytes. An attribute that is not large is held as
JSON (the value form, the data form or Imaris's form) when that fits in
what its document has left (§5.6); otherwise it is in the array form. A
large variable-length attribute's elements are not read.

### 5.4 Datasets

A dataset at path `P` that is not an image dataset is the Zarr array
`vzip_source/hdf5` followed by `P` (conventions §7), with no dimension
names and the source metadata `S`, `{"datatype": D}` (or `"named"`, §5.2),
with `"fill"` (below) and its attributes (§5.3), when present:

- **Data type and shape.** For a number datatype, its Zarr data type and
  byte order, and the dataset's dimensions. For an enumeration of a number,
  or an array of numbers (at any depth), the base number's data type and
  byte order, and the dataset's dimensions followed by the array's.
  Otherwise `uint8`, and the dataset's dimensions followed by the
  datatype's size: each element's bytes as the file holds them.
- **Fill value.** When the dataset has a fill value, and its elements of
  that data type are all the same bytes and are exactly a Zarr fill value,
  that value: an integer as it is (beyond 2^53 too), a float's number, NaN
  only as the quiet NaN with no payload and the sign bit clear, and never
  −0; for `uint8`, the byte. Otherwise the fill value is 0 and `S` has
  `"fill": B`, the dataset's fill value's bytes in base64, which a reader
  uses for the elements of absent chunks. Without a fill value it is 0.
- **Chunks.** A chunked dataset keeps its chunk shape (followed by the
  extra dimensions). By its filters, in pipeline order:
  - none, deflate (1), Fletcher32 (3), or deflate then Fletcher32: each
    chunk is referenced where HDF5 stores it, without Fletcher32's last 4
    bytes (its checksum, which with deflate follows the compressed bytes),
    with `zlib` after `bytes` for deflate. The referenced part MUST be at
    least 1 byte, and without deflate the chunk's byte count;
  - shuffle (2), alone or followed by any of the above: each chunk is
    **decoded** (Fletcher32's checksum removed, deflate inflated, which MUST
    give one zlib stream of the chunk's byte count, and the shuffle undone:
    of the `m` whole elements of the datatype's size, byte `j` of element
    `i` is at `j × m + i`, and the bytes after them stay) and copied, when
    the allocated chunks' byte counts total at most 2^24 bytes;
  - any other filters (scale-offset, N-bit, LZF, LZ4, ...): the dataset is
    unsupported.

  A chunk whose filter mask ([spec/virtualize.md §8.9](#89-source-metadata-structures))
  has bit `i` set is stored without the pipeline's filter `i` (HDF5 skips
  the shuffle for variable-length data, say): it is decoded without the
  filters its mask skips, and a dataset with such a chunk has all its
  chunks decoded and copied, as for shuffle, whatever its filters (its
  allocated chunks' byte counts totaling at most 2^24 bytes). A **partial
  edge chunk** (one that reaches past the dataset's dimensions along some
  dimension) of a dataset whose layout stores partial edge chunks
  unfiltered (`H5Pset_chunk_opts`, [spec/virtualize.md §8.5](#85-datasets-and-chunks))
  is such a chunk with every filter's bit set: it is copied raw, and the
  dataset's other chunks are decoded.

  An **implicit** chunk index (spec/virtualize.md §8.5) whose chunks, in
  row-major order, hold the dataset's elements in row-major order is read
  as contiguous storage: the dataset is cut as conventions §7 cuts
  contiguous values, of its shape (followed by the extra dimensions) and
  its data type's size, from the index address. They do when the chunk
  shape is 1 along every dimension before some dimension `a` and is the
  whole size along every dimension after it, the maximum grid has the
  grid's chunk count along every dimension but the first, and `a`'s size is
  a multiple of its chunk shape or every dimension before `a` has size 1;
  and for a dataset of no dimensions. Otherwise, when the chunk shape is 1
  along every dimension but the last, along which the grid has more than one
  chunk, the chunks of a row lie one after the other: each row of the
  dataset (its last dimension whole, at most 2^24 bytes) is one chunk,
  referenced as one run, with the chunk shape 1, ..., 1 and the last
  dimension's size (followed by the extra dimensions). Any other implicit
  index keeps its chunks, as above.

  A contiguous dataset is cut as conventions §7 cuts contiguous values, of
  its shape and its data type's size, each chunk referenced; a compact
  dataset is one chunk of its whole shape, copied. Unallocated storage has
  no chunks.

A dataset whose dataspace is **null** is a Zarr group at that path, with
`S` `{"datatype": D, "shape": null}` (and `"named"`, its fill value's bytes
as `"fill"` when it has one and `D` does not hold addresses, and its
attributes).

**Addresses.** A dataset whose datatype holds addresses, of at most 2^20
elements, has its data read and decoded (its chunks as the shuffle above
decodes them, whatever its filters among those; a chunk's byte count MUST
be at most 2^24, else the dataset is unsupported, so that no chunk decodes
to more) and mapped by its datatype (its `S` has no fill value):

- **variable-length** whose base does not hold addresses: a family of byte
  values (conventions §7) at that path, one member per element in
  row-major order, each the bytes of its global heap object (the string's
  bytes, or the sequence's elements), or empty for an element of length 0.
  The 2-D array, or the group of `offsets` and `data` (by which form the
  family has, even when no element has a byte), carries `S` with
  `"shape": dims`;
- **object references**: an `int32` array of the dataset's dimensions, each
  element the index of the referenced object in the object table (§5.7), or
  −1 for a reference to none, cut as contiguous values and copied, with
  `S`;
- **region references**: a family of the JSON texts (§5.6) of the elements'
  values (§5.3: `null` or `{"object", "selection"}`), copied, carrying `S`
  with `"shape": dims`; when an element cannot be read (past the budget of
  selections of §5.3, say), the dataset is unsupported;
- **compound** whose members each either do not hold addresses or are of
  one of the three datatypes above (and lie within the compound), of at
  most 2^24 bytes: a Zarr group at
  that path with `S`, holding the array `data`, `uint8` of the dataset's
  dimensions followed by the datatype's size, the elements' bytes with the
  bytes of the members that hold addresses set to 0, cut as contiguous
  values and copied; and, for each member `m` (its place in the compound,
  from 0) that holds addresses, `<m>`, its values as above (a family's
  group or 2-D array, or an `int32` array), without source metadata.

A dataset whose datatype otherwise holds addresses (variable-length data
of references, references of other kinds, ...), of more elements, one whose
data is in external files (an external data files message), a virtual
dataset, and one with other filters, are unsupported (§5.5).

### 5.5 Unsupported objects

An object that §5.1 lists in `unsupported` has the record `{"reason": T}`,
`T` an informative English text, with what can be read of it (each when
it can be read, and the object header can): `"datatype": D` (or
`"named"`) for a dataset or a named datatype; for a dataset, `"shape"`
(its dimensions, or `null` for a null dataspace), `"fill"` (its fill
value's bytes in base64, when it has one and `D` does not hold addresses),
`"external"`, the external files of an external data files message, in
order, each `{"file": T, "offset": n, "size": n}` (its name as a text
value, and the offset and size of its part), and, for a virtual dataset,
`"virtual"`, its mappings ([spec/virtualize.md §8.9](#89-source-metadata-structures)) in order, each `{"file": T,
"dataset": T, "source": S, "selection": S}`: the source file's and
dataset's names as text values, and the source and virtual selections
(§5.3); and its attributes (§5.3).

### 5.6 Documents

Each document (the `zarr.json` of a Zarr group or array that carries an
`S`) holds `S` as a sequence of **entries**, in order:

- an object's attributes: one entry per attribute in ascending byte order
  of the names, `{"attributes": {key: value}}`, or
  `{"attribute_collisions": [value]}` for a collision, or one entry
  `{"attributes": null}` when they cannot be read;
- for a group, its attributes, then, for each member in the members' order:
  a collision `{"collisions": [T]}`; else `{"names": {key: T}}` when its
  name is not valid UTF-8, then its entry `{"links": {key: L}}`,
  `{"images": {key: I}}`, `{"datatypes": {key: N}}` or `{"unsupported":
  {key: R}}`, if any (the member's attributes in its value);
- for a dataset, `{"datatype": D}` (or `{"named": i}`, or `{"named": null,
  "datatype": D}`), then `{"shape": ...}` and `{"fill": B}` when `S` has
  them, then its attributes.

`overflow` and `spilled` are not entries. The **size** of a JSON value is
the length in bytes of its **JSON text**: UTF-8, with no whitespace, the
members of each object in ascending byte order of their keys' UTF-8,
strings with only `"`, `\` (escaped by a backslash) and the characters
below U+0020 escaped (`\b`, `\t`, `\n`, `\f`, `\r`, else `\u` and four
lowercase hexadecimal digits), and numbers as ECMAScript's
`Number::toString` writes them (the shortest digits that round-trip, for
example `1e-7`, `0.000001`, `100000000000000000000` and `1e+21`). An
entry's **cost** is, for an attribute, the size of its key plus the size of
its value (for a collision, the size of its value), and for attributes that
cannot be read, 4 (the size of `null`); for a member's entry in `images`,
`datatypes` or `unsupported`, the size of its key plus the size of its
value without its attributes, plus its attributes' costs; for any other
entry with a key, the size of the key plus the size of the value; for a
collision, the size of its text; and else the size of the entry.

A document has a **budget** of 2^16 bytes (64 KiB). Its entries are taken
in order, adding to `K`, from 0: an entry is **kept** in the document (as
a member of `S`) when no entry before it was spilled and `K` plus its cost
is at most 2^16, its cost then added to `K`; else it, and every entry
after it, is **spilled**. An attribute **fits** (§5.3) when an entry
before it was spilled (spilled entries are data, not the document), or
when `K`, plus the costs of the parts of its entry before it (a member's
key and value, and its earlier attributes), plus its own cost as JSON, is
at most 2^16.

A document with spilled entries has `"spilled": "spilled/<j>"`: the `j`th
(from 0, in the order the documents are settled) family of byte values
(conventions §7) under `vzip_source`, one member per spilled entry, in
order, its JSON text, copied. A reader rebuilds `S` by adding each to it:
the members of `attributes`, `names`, `links`, `images`, `datatypes` and
`unsupported` to that member's, the items of `attribute_collisions` and
`collisions` after that member's, and any other member as it is.

The documents are **settled** (their entries' forms decided) after the
walk, when every path is known, in the order the walk creates them: a
group's document when the walk reaches the group, then those of its
member datasets in the members' order.

### 5.7 The object table

The objects the walk met are numbered from 0 in the order it met them (the
root group first); an object's number is its **index**. Hard links (§5.1),
named datatypes (§5.2) and object and region references (§5.3, §5.4) give
an object by its index, so that a long path is written once. The
object table, the group `vzip_source/objects`, holds each object's parent
and key, so that its size grows with the keys, not with the paths:

- `parent`: an `int32` array of one element per object, the index of the
  group where the walk met it (−1 for the root group), cut as contiguous
  values and copied;
- `name`: a family of byte values (conventions §7) of one member per
  object, its key in UTF-8 (empty for the root group), copied.

The path of object `i` is `/` for the root group, else the path of its
parent (without a final `/`) followed by `/` and its name.

### 5.8 Repeatability

The walk, the order of members, the keys, the documents' entries and
budgets, the numbering of attribute arrays and spilled families, the object
table, the chunking, the indexes of references and named datatypes, the
budget of selections and the reasons depend only on the file, so the same
file always gives the same hierarchy.

### 5.9 What is not kept

The storage itself (chunk indices, B-trees, heaps, object headers, the
order the file lists things in, and dead space), the filters' settings
(and Fletcher32's checksums, and the chunks' filter masks), maximum dimensions, fill and allocation
times, the padding of the image's datasets past `ImageSize`, and object
comments. Of the objects listed in `unsupported`, only their record (§5.5)
is kept: the data of datasets (of virtual datasets, their mappings are
kept) and the members of groups (the members of a group named `zarr.json`,
say) are lost. Members beyond the walk's `100,000 + 5 × N` objects (§5.1)
are counted, not described; objects reached only through a member that collides are not
described; the fill values of datatypes that hold addresses, and data
whose datatype holds addresses other than as §5.3 and §5.4 map it, are
lost.

## 6. Example

The root of a file at `https://example.org/a.ims`:

```json
"vzip_virtualized": {
  "profile": "ims",
  "version": 0,
  "revision": 24,
  "source": {
    "url": "https://example.org/a.ims"
  }
}
```

The channel group `vzip_source/hdf5/DataSet/ResolutionLevel 0/TimePoint 0/Channel 0`:

```json
{
  "zarr_format": 3,
  "node_type": "group",
  "attributes": {
    "zarr_conventions": [
      "(the CMO of §1)"
    ],
    "vzip_virtualized": {
      "ims": {
        "attributes": {
          "HistogramMax": "255.000",
          "HistogramMin": "0.000",
          "ImageSizeX": "11",
          "ImageSizeY": "9",
          "ImageSizeZ": "4"
        },
        "images": {
          "Data": {
            "level": 0,
            "t": 0,
            "c": 0,
            "shape": [
              4,
              16,
              16
            ]
          }
        }
      }
    }
  }
}
```

and its `Histogram` dataset, the array at
`vzip_source/hdf5/DataSet/ResolutionLevel 0/TimePoint 0/Channel 0/Histogram`:

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": [
    16
  ],
  "data_type": "uint64",
  "chunk_grid": {
    "name": "regular",
    "configuration": {
      "chunk_shape": [
        16
      ]
    }
  },
  "chunk_key_encoding": {
    "name": "default",
    "configuration": {
      "separator": "/"
    }
  },
  "fill_value": 0,
  "codecs": [
    {
      "name": "bytes",
      "configuration": {
        "endian": "little"
      }
    }
  ],
  "attributes": {
    "zarr_conventions": [
      "(the CMO of §1)"
    ],
    "vzip_virtualized": {
      "ims": {
        "datatype": {
          "class": "integer",
          "size": 8,
          "order": "little",
          "signed": false
        }
      }
    }
  }
}
```

# Part 2. The profile

The IMS profile of [spec/virtualize.md](../virtualize.md) (revision 16), numbered
as its §8. §1 and §2 are in spec/virtualize.md and apply here.

The output has the convention's layout for the input
([spec/virtualize.md §2](../virtualize.md#2-the-zarr-layout)); this profile says
how the file is read, which inputs are rejected, and how each chunk
references the file. "The convention §n" below is a section of Part 1.

## 8. IMS profile

An Imaris file (`.ims`) is an HDF5 file holding a fixed layout of groups and
datasets ([the convention §2.2](#22-the-imaris-layout)): one chunked 3-D dataset per resolution level, time point and
channel, with text attributes for the sizes and the metadata ([the convention §3](#3-metadata)). The
virtualizer reads the HDF5 structure itself, by the subset of the HDF5 file
format (The HDF Group's *HDF5 File Format Specification*, version 3) that
§8.1–§8.6 describe; whatever falls outside the subset rejects the input. The
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
fixed arrays or as a single chunk, by extensible arrays or version 2
B-trees when dimensions are unlimited, and implicitly when chunks are
allocated early. Imaris 10 can also compress chunks with LZ4
(filter 32004), alone or after byte shuffling (filter 2); no codec of [conventions §3](../conventions.md#3-arrays)
decodes those, so such files are rejected.

### 8.1 HDF5 structures

- **Fields.** Every integer in an HDF5 structure is unsigned and
  little-endian. Addresses and lengths are 8 bytes (the superblock below
  requires it). An address with all 64 bits set is **undefined**; any other
  address, and every field this section calls a length, MUST be at most
  2^53 − 1. Where this section reads `n` bytes at an
  address `a`, they MUST lie within the file; where it reads a field at an
  offset of a structure (a message, a node, a record), the field MUST lie
  within the structure. Otherwise the input is rejected.
- **Not read:** checksums, reserved bytes, timestamps, and every field this
  section does not name. A structure's version or signature is checked only
  where this section says so.

**Superblock.** The file starts with the HDF5 signature `89 48 44 46 0D 0A
1A 0A`; byte 8 is the superblock version.

- Version 0 or 1: the first 72 bytes (version 0) or 76 bytes (version 1) are
  read. Byte 13 is the size of addresses and byte 14 the size of lengths. The
  base address is the 8 bytes at 24 (version 0) or 28 (version 1), and the
  root group's object header address is the 8 bytes at 64 (version 0) or 68
  (version 1), in the root group's symbol table entry.
- Version 2 or 3: the first 48 bytes are read: sizes at 9 and 10, base
  address at 12, root group object header address at 36. The superblock
  extension is not read.
- Any other version rejects the input.

Both sizes MUST be 8, the base address MUST be 0, and the root group's
address MUST be defined.

### 8.2 Object headers

An object header at address `a` is a list of **blocks** that hold its
messages.

- **Version 2**, if the 4 bytes at `a` are `OHDR`: byte `a + 4` MUST be
  2 and byte `a + 5` holds flags `F`. Starting at byte `a + 6` come 16 bytes if
  `F & 0x20`, then 4 bytes if `F & 0x10`, then the length `s` of the first
  block in `2^(F & 3)` bytes. The first block is the `s` bytes after that
  length; it and the 4 bytes after it (a checksum) MUST lie within the file. A
  message starts with a header of 4 bytes, or 6 if `F & 4`: the type (1
  byte), the size of its data (2 bytes), its flags (1 byte) and, if 6, 2 bytes
  not read.
- **Version 1**, otherwise: the 16 bytes at `a` are read, byte `a` MUST be
  1, and the first block is the `n` bytes at `a + 16`, where `n` is the
  4 bytes at `a + 8`. A message starts with a header of 8 bytes: the type
  (2 bytes), the size of its data (2 bytes), its flags (1 byte) and 3 bytes not
  read. The message count (at `a + 2`) is not read.

Messages follow one another from the start of each block: a message header,
then the data, which MUST end within the block. In version 1 the messages
fill the block: a remainder shorter than a message header rejects the input.
In version 2 such a remainder is a gap, and ends the block.

A **continuation message** (type 0x10) adds a block. Its data is an address
`o`, which MUST be defined, and a length `l`. In version 1 the block is
the `l` bytes at `o`. In version 2, `l` MUST be at least 8, the `l`
bytes at `o` MUST lie within the file and start with `OCHK`, and the
block is the `l − 8` bytes at `o + 4`. Blocks are read in the order they
are added. A continuation message with flag bit 1 (0x02, shared), a block
that starts where an earlier block of the same header starts, and a header of
more than 1000 blocks reject the input.

The header's **messages** are the messages of all its blocks other than
continuation messages, in order. Message types that §8.3–§8.6 do not read are
ignored, their data not decoded. Where those sections read **the** message of
a type, the header MUST have at most one message of that type (exactly one
where it is required), and that message's flag bit 1 (shared) MUST be clear.

### 8.3 Groups and links

A group's **links** map names (byte strings) to targets: an object header
address (a **hard** link), a path (a **soft** link), or neither (any other
kind of link). **Opening** a group reads all its links from its object header
(§8.2), and every check below applies to every link, whether or not it is
used.

- The header MUST NOT have both a symbol table message (type 0x11) and a link
  info message (type 0x02). With neither, the object is not a group and the
  input is rejected.
- **Symbol table** (old-style groups). The message's data is the address of a
  version 1 B-tree and the address of a local heap; both MUST be defined.
  - The local heap: the 32 bytes at its address start with `HEAP` and then
    version 0. Its data segment is the `n` bytes at the address at byte 24,
    which MUST be defined, where `n` is the length at byte 8.
  - The B-tree is read (below) with node type 0 and keys of 8 bytes. The
    children of its leaves are symbol table nodes.
  - A symbol table node at `b`: the 8 bytes at `b` start with `SNOD`,
    byte 4 MUST be 1, and bytes 6–7 are its number of entries `k`. The
    entries are the `40k` bytes at `b + 8`. Each is a name offset (8 bytes)
    and an object header address (8 bytes, which MUST be defined); its other
    24 bytes are not read. The name is the data segment's bytes from the
    offset up to the first NUL: the offset MUST be less than the segment's
    size, and a NUL MUST occur at or after it within the segment. The link is
    hard.
- **Link info** (new-style groups). Byte 0 (the version) MUST be 0; byte 1
  holds flags. After 8 more bytes if flag bit 0 is set come the address of a
  fractal heap and the address of a name index.
  - **Compact:** if the heap's address is undefined, the links are the
    header's link messages (type 0x06), none of which may have flag bit 1
    (shared) set.
  - **Dense:** otherwise the name index MUST be defined; it is a version 2
    B-tree of type 5 (§8.4), each of whose records is a 4-byte hash (not read)
    and a 7-byte heap ID. The links are the fractal heap's objects (§8.4) with
    those IDs, each a link message. Link messages in the header are not read.
- **Link message.** Byte 0 (the version) MUST be 1; byte 1 holds flags `G`.
  Then come: the link's type (1 byte) if `G & 8`, else type 0; 8 bytes if
  `G & 4`; 1 byte if `G & 16`; the length `n` of the name in
  `2^(G & 3)` bytes; the name, `n` bytes. A link of type 0 is hard: an
  address follows, which MUST be defined. A link of type 1 is soft: a 2-byte
  length `m` follows, then its path, `m` bytes. For any other type nothing
  more is read here (the source metadata reads its value, §8.9).

Two links of a group with the same name reject the input.

**Following** a group's link: a hard link leads to its address. A soft
link's path MUST start with `/`: the path `/` leads to the root group;
otherwise the bytes after the first `/` are split at every `/`, and the
parts are looked up in turn, from the root group, as links of the group each
leads to (each group is opened); each part MUST be a hard link, and the path
leads where the last one does. A soft link with a relative path, a link that
is neither hard nor soft, and a part that is missing or not hard reject the
input.

**Version 1 B-trees.** A tree has a node type and a key size `K`. A node at
`b`: the 24 bytes at `b` start with `TREE`, byte 4 MUST be the node type,
byte 5 is its level `L` and bytes 6–7 its number of entries `e` (the
sibling addresses are not read). Its entries are the `e(K + 8) + K` bytes at
`b + 24`: key 0, child 0, key 1, child 1, ..., child `e − 1`, key `e`,
each child an address, which MUST be defined. The children of a node of level
`L > 0` are nodes, whose level MUST be `L − 1` (the root's level is not
checked); the children of a leaf (`L = 0`) are symbol table nodes (type 0)
or chunks (type 1, §8.5), where key `i` describes child `i`. Within one
tree, a node or a symbol table node read twice (at the same address) rejects
the input.

### 8.4 Version 2 B-trees and fractal heaps

Dense groups and dense attributes keep their links and attribute messages as
objects of a fractal heap, indexed by a version 2 B-tree; datasets of
several unlimited dimensions index their chunks by one (§8.5).

**Version 2 B-trees.** A tree of type `T` at `a`: the 38 bytes at `a`
start with `BTHD`, then version 0 and type `T` (byte 5). Its node size `N`
is the 4 bytes at 6, its record size `R` the 2 bytes at 10, its depth `D`
the 2 bytes at 12, the root node's address the 8 bytes at 16 (if undefined,
the tree has no records) and the root's number of records the 2 bytes at 24.
`R` MUST be 24, 11 or 17 for type 1, 5 or 8 (§8.5 gives the record sizes
of types 10 and 11), `N` MUST be at least `10 + R`, and `D` at most 16.

- **Sizes** (exact integer arithmetic): a leaf holds at most
  `M(0) = floor((N − 10) / R)` records, and a record count takes
  `c = floor(log2(M(0)) / 8) + 1` bytes. For each depth `d` from 1 to
  `D`, a child pointer in a node of depth `d` takes
  `P(d) = 8 + c + S(d − 1)` bytes, where `S(0) = 0`; such a node holds at
  most `M(d) = floor((N − 10 − P(d)) / (R + P(d)))` records, which MUST be
  at least 1; `C(0) = M(0)`, `C(d) = (M(d) + 1) × C(d − 1) + M(d)`, and
  `S(d) = floor(log2(C(d)) / 8) + 1`.
- **Nodes.** A node of depth `d` with `k` records at `b`: a leaf
  (`d = 0`) is the `6 + kR` bytes at `b` and starts with `BTLF`; an
  internal node is the `6 + kR + (k + 1)P(d)` bytes at `b` and starts with
  `BTIN`. Byte 4 MUST be 0 and byte 5 MUST be `T`. Its records are the
  `k` records of `R` bytes from byte 6. An internal node's records are
  followed by `k + 1` child pointers: an address, which MUST be defined; the
  child's number of records, in `c` bytes; and `S(d − 1)` bytes, not
  read. Each child is a node of depth `d − 1` with that many records.
- The root is a node of depth `D`. The tree's records are the records of all
  its nodes. A node read twice (at the same address) rejects the input.

**Fractal heaps.** A fractal heap at `a`: the 146 bytes at `a` start with
`FRHP` and version 0. Its fields are the heap ID length (2 bytes at 5), the
size of the I/O filters' information (2 bytes at 7), the maximum size of a
managed object `Q` (4 bytes at 10), the address of the huge objects'
B-tree (8 bytes at 22), the table width `W` (2 bytes at 110), the starting
block size `S` (a length at 112), the maximum direct block size `X` (a
length at 120), the maximum heap size `H`, in bits (2 bytes at 128), the
root block's address (8 bytes at 132) and the root indirect block's number of
rows `Rr` (2 bytes at 140). The I/O filters' size MUST be 0 (filtered heaps
are rejected); `W`, `S` and `X` MUST be powers of 2, with `X ≥ S`;
`H` MUST be from 1 to 64 and `Q` at least 1. Let `O = ceil(H / 8)`,
`Ln = min(ceil(log2(X) / 8), floor(log2(Q) / 8) + 1)` and
`Dr = log2(X) − log2(S) + 2` (the number of rows of direct blocks).

An object is read by its **heap ID**, which MUST have the heap ID length. Its
first byte shifted right by 4 bits gives its kind:

- 0, a **managed** object. The ID MUST have at least `1 + O + Ln` bytes:
  after the first byte, `O` bytes are the object's heap offset `x` and
  `Ln` bytes its length `n`. The root block's address MUST be defined. If
  `Rr = 0` the root is a direct block of size `S` at heap offset 0, and
  holds the object; otherwise the root is an indirect block of `Rr` rows at
  heap offset 0, which leads to the direct block holding `x`:
  - An **indirect block** at `b`, with `r` rows, at heap offset `p`: the
    `13 + O` bytes at `b` start with `FHIB` and version 0, and its block
    offset (the `O` bytes at 13) MUST equal `p`. Row `i` holds `W`
    blocks of size `B(0) = S` or `B(i) = S × 2^(i − 1)` (`i ≥ 1`); row 0
    starts at heap offset `p` and each row where the previous one ends. The
    row `i` that holds `x` (if none does, the input is rejected) and the
    column `j = floor((x − q) / B(i))`, where `q` is the row's start, select
    the block's address, the 8 bytes at `b + 13 + O + 8(iW + j)`, which MUST
    be defined. If `i < Dr` it is the direct block of size `B(i)` at heap
    offset `q + jB(i)`; otherwise it is an indirect block at that heap
    offset with `log2(B(i)) − log2(S × W) + 1` rows (which MUST be at least
    1), searched the same way.
  - A **direct block** at `b`, of size `z`, at heap offset `p`: the
    `13 + O` bytes at `b` start with `FHDB` and version 0, and its block
    offset (the `O` bytes at 13) MUST equal `p`. The object MUST lie within
    the block (`p ≤ x` and `x + n ≤ p + z`); it is the `n` bytes at
    `b + x − p`.
- 1, a **huge** object: after the first byte, `min(L − 1, 8)` bytes (`L`
  being the ID length) are its key. The huge objects' B-tree MUST be defined;
  it is a version 2 B-tree of type 1, read in full the first time one of the
  heap's huge objects is read, whose records are an address (which MUST be
  defined), a length and a key, 8 bytes each, with no two keys equal. The
  object is the record with its key, which MUST exist: the record's length
  in bytes at its address.
- Any other kind (tiny objects, other ID versions) rejects the input.

### 8.5 Datasets and chunks

A dataset's object header has the dataspace message (type 0x01), the datatype
message (type 0x03) and the data layout message (type 0x08), all required,
and may have the fill value message (type 0x05) and the filter pipeline
message (type 0x0B) (§8.2).

- **Dataspace.** Byte 0 is the version, byte 1 the rank `k`, byte 2 flags.
  Version 1: the sizes start at byte 8, and rank 0 is a scalar. Version 2:
  byte 3 is the type (0 scalar, 1 simple, 2 null; any other type, or a scalar
  or null type with a rank other than 0, rejects the input), and the sizes
  start at byte 4. Other versions reject. Then come `k` sizes (8 bytes each,
  lengths) and, if flag bit 0 is set, `k` maximum sizes (8 bytes each).
- **Datatype.** The low 4 bits of byte 0 are the class and its high 4 bits the
  version, which MUST be 1, 2 or 3. Bytes 1–3 are the class bit fields (a
  24-bit integer), bytes 4–7 the size in bytes, and the properties start at
  byte 8.
- **Fill value.** Version 1 or 2: in version 2, if byte 3 is 0 there is no
  fill value; otherwise the value's size `s` is the 4 bytes at 4 and the value the `s`
  bytes at 8. Version 3: with bit 5 of byte 1 clear there is no fill value;
  otherwise `s` is the 4 bytes at 2 and the value the `s` bytes at 6. Other
  versions reject. Every byte of the value MUST be 0: chunks that are not
  allocated read as 0, as in Zarr ([conventions §3](../conventions.md#3-arrays)). (No fill value message, or no fill
  value, also reads as 0.)
- **Filter pipeline.** Byte 0 is the version (1 or 2; others reject), byte 1
  the number of filters, and the filters start at byte 8 (version 1) or 2
  (version 2). A filter is its identifier (2 bytes); then, in version 1 or
  for an identifier of 256 or more, the length of its name (2 bytes); its
  flags (2 bytes, not read); its number of client data values `v` (2
  bytes); its name (in version 1 padded with zeros to a multiple of 8 bytes);
  `4v` bytes of client data; and in version 1, 4 more bytes if `v` is odd.
  Every filter MUST lie within the message. Without the message there are no
  filters.
- **Data layout.** Byte 0 is the version and byte 1 the layout class, which
  MUST be 2, chunked (compact and contiguous datasets are rejected).
  - Version 3: byte 2 is the dimensionality `m`, the 8 bytes at 3 the index
    address, and `m` dimensions of 4 bytes follow. The index is a version 1
    B-tree.
  - Version 4 or 5 (which HDF5 2.0 writes, with the same encoding of these
    fields): byte 2 holds flags, byte 3 is the dimensionality `m`, byte 4
    the size `e` of each dimension (from 1 to 8), then come `m` dimensions
    of `e` bytes, the index type (1 byte), the index's fields, and the index
    address (8 bytes). The index types are:
    - 1, a **single chunk**: if flag bit 1 is set, the chunk's size (8 bytes,
      a length) and filter mask (4 bytes, which MUST be 0, §8.9);
    - 2, **implicit**: no fields;
    - 3, a **fixed array**: 1 byte, not read (the page bits of the fixed
      array header are used);
    - 4, an **extensible array**: 5 bytes, not read (the extensible array
      header's parameters are used);
    - 5, a **version 2 B-tree**: 6 bytes (node size, split and merge
      percents), not read (the B-tree header's are used).

    Other index types reject the input. With filters, flag bit 0 (partial
    edge chunks not filtered, which `H5Pset_chunk_opts` sets) means that
    each **partial edge chunk**, one whose grid coordinate `g` along some
    dimension of size `n` and chunk shape `k` has `(g + 1) × k > n`, is
    stored unfiltered, whatever filter mask the index records: it is read
    as a chunk whose filter mask has the bit of every filter of the
    pipeline set (§8.9), so an image dataset (whose masks MUST be 0) with
    such a chunk rejects the input. A single chunk index without flag bit
    1, and an implicit index, with filters reject the input.
  - Other versions reject the input.

  The dimensionality `m` MUST be the rank plus 1, and the last dimension
  MUST equal the datatype's size; the others are the **chunk shape**, each of
  which MUST be from 1 to 2^53 − 1. A null dataspace rejects the input.

The **chunk grid** has `ceil(n / c)` chunks along each dimension, of size
`n` and chunk size `c`; the number of chunks, their product, MUST be at
most 2^53 − 1. A chunk's **byte count** is the product of the chunk shape and
the datatype's size. Where an index needs it, the **maximum grid** has
`ceil(m / c)` chunks along each dimension, `m` its maximum size (its size
when the dataspace has no maximum sizes); a maximum with all 64 bits set is
**unlimited**, which only the extensible array index allows; any other
maximum MUST be at least the size and at most 2^53 − 1, and the product of
the limited dimensions' chunk counts MUST be at most 2^53 − 1. A **row-major
index** over a grid numbers its coordinates with the last dimension fastest.

**Allocated chunks.** If the index address is undefined, no chunk is
allocated. Otherwise the index gives the allocated chunks, each with grid
coordinates, an address and a size. No two may have the same coordinates;
without filters, the size MUST equal the byte count; the size MUST be at least
1; and the chunk (its size in bytes at its address) MUST lie within the file.

- **Version 1 B-tree:** read (§8.3) with node type 1 and keys of
  `8 + 8m` bytes. A leaf's child `i` is a chunk's address, and its key
  `i` holds the chunk's size (4 bytes), its filter mask (4 bytes, which MUST
  be 0) and `m` offsets (8 bytes each). The last offset MUST be 0, and each
  of the others MUST be a multiple of the chunk shape along its dimension and
  less than the dimension's size; divided by the chunk shape they are the
  chunk's coordinates.
- **Single chunk:** the grid MUST have exactly one chunk. It is the chunk at
  the index address, at coordinates 0, whose size is the layout message's with
  filters, and the byte count without.
- **Implicit:** every chunk of the grid is allocated, the byte count in
  size: the chunk at coordinates `x` is at `a + iB`, `a` the index
  address, `B` the byte count and `i` the row-major index of `x` over the
  maximum grid (which MUST have no unlimited dimension). The grid's last chunk
  MUST lie within the file.
- **Fixed array:** the maximum grid MUST have no unlimited dimension. The
  28 bytes at the index address start with `FAHD` and version 0;
  byte 5 is the client ID, which MUST be 1 with filters and 0 without; byte 6
  is the entry size `E`, which MUST be 8 without filters and from 13 to 20
  with; byte 7 is the page bits `g`; the 8 bytes at 8 are the number of
  entries `n`, which MUST equal the number of chunks of the maximum grid;
  and the 8 bytes at 16 are the data block's address `d` (if undefined, no
  chunk is allocated). The 14 bytes at `d` start with `FADB`, version 0,
  and the client ID. Entry `i` is the chunk whose row-major index over the
  maximum grid is `i`; an entry whose coordinates lie outside the grid (data
  beyond the dataspace) is not a chunk of the dataset.
  - If `n ≤ 2^g`, the entries are the `nE` bytes at `d + 14`.
  - Otherwise the entries are in `P = ceil(n / 2^g)` pages. The
    `ceil(P / 8)` bytes at `d + 14` are a bitmap: page `j` is
    initialized if bit `0x80 >> (j mod 8)` of its byte `floor(j / 8)` is
    set. Page `j` is at `d + 14 + ceil(P / 8) + 4 + j(2^g E + 4)` and holds
    entries `j 2^g` to `min((j + 1) 2^g, n) − 1`, `E` bytes each. An
    initialized page is read; a page that is not has no allocated chunks.

  An entry is the chunk's address (8 bytes; if undefined, the chunk is not
  allocated) and, with filters, its size (the next `E − 12` bytes, a length)
  and its filter mask (the last 4 bytes, which MUST be 0).
- **Extensible array:** the maximum grid MUST have exactly one unlimited
  dimension `u`. Let `D` be the product of the other dimensions' maximum
  chunk counts and `L = G × D`, `G` the grid's chunk count along `u`. The
  chunk at coordinates `x` is the array's element `x_u × D + j`, `j` the
  row-major index of the other coordinates over their maximum grid. Elements
  from `L` on hold no chunk of the grid and are not read; an element whose
  coordinates lie outside the grid is not a chunk of the dataset. An element
  is an entry as for a fixed array.
  - **Header:** the 68 bytes at the index address start with `EAHD` and
    version 0; byte 5 is the client ID (1 with filters, 0 without), byte 6
    the element size `E` (8 without filters, 13 to 20 with), byte 7 the
    maximum index bits `b`, byte 8 the index block's element count `I`,
    byte 9 the data blocks' minimum element count `M`, byte 10 the super
    blocks' minimum data block count `P`, byte 11 the page bits `g`, and
    the 8 bytes at 60 the index block's address (if undefined, no chunk is
    allocated). `M` and `P` MUST be powers of 2, `b` from `log2(M)` to 64,
    `g` at most 64, and `2 log2(P)` at most `S = 1 + b − log2(M)`, the
    number of super blocks. Super block `s` (from 0) has `2^floor(s / 2)`
    data blocks of `N(s) = 2^floor((s + 1) / 2) × M` elements each; its
    elements follow those of the super blocks before it, which follow the
    `I` elements of the index block. Block offsets take `O = ceil(b / 8)`
    bytes.
  - **Index block:** the 14 bytes at its address start with `EAIB`, version
    0 and the client ID; its `I` elements follow, then the addresses of the
    `2(P − 1)` data blocks of super blocks 0 to `2 log2(P) − 1` in order,
    then those of super blocks `2 log2(P)` to `S − 1`, 8 bytes each.
  - **Super block** `s`, at a defined address: the `14 + O` bytes at it
    start with `EASB`, version 0 and the client ID. When `N(s) > 2^g` its
    data blocks are **paged**, in `K = N(s) / 2^g` pages each, and
    `ceil(K / 8)` bytes per data block follow: page `k` of data block `d`
    is initialized when bit `0x80 >> (q mod 8)` of byte `floor(q / 8)` is
    set, `q = dK + k`. The addresses of its data blocks follow, 8 bytes
    each.
  - **Data block** of `N` elements, at a defined address: the `14 + O`
    bytes at it start with `EADB`, version 0 and the client ID. Unless it is
    paged, its elements follow. A paged data block's pages start 4 bytes
    later, each of `2^g` elements followed by 4 bytes; a page that is not
    initialized holds no chunk and is not read. A data block of the index
    block MUST NOT be paged.
- **Version 2 B-tree:** read as §8.4 says, of type 10 without filters,
  whose records are `8 + 8k` bytes for rank `k`, or of type 11 with,
  whose records are from `13 + 8k` to `20 + 8k` bytes. A record is the
  chunk's address (8 bytes, which MUST be defined); with filters, its size
  (a length of the record's size minus `12 + 8k` bytes) and filter mask (4
  bytes, which MUST be 0); then its `k` coordinates (8 bytes each), each
  less than the grid's chunk count along its dimension.

### 8.6 Attributes

Reading an object's **attributes** reads its object header's attribute
messages (type 0x0C), none of which may have flag bit 1 (shared) set, and its
attribute info message (type 0x15), if any:

- **Attribute info.** Byte 0 (the version) MUST be 0; byte 1 holds flags.
  After 2 more bytes if flag bit 0 is set come the address of a fractal heap
  and the address of a name index. If the heap's address is defined (**dense
  attributes**), the name index MUST be defined; it is a version 2 B-tree of
  type 8 (§8.4), each of whose records is a heap ID (8 bytes), message flags
  (1 byte, whose bit 1, shared, MUST be clear) and 8 bytes not read. The
  attributes are then also the fractal heap's objects with those IDs, each an
  attribute message.
- **Attribute message.** Byte 0 is the version, which MUST be 1, 2 or 3;
  byte 1 holds flags (in versions 2 and 3; version 1 has none); the 2-byte
  sizes of the name, the datatype and the dataspace are at 2, 4 and 6. The
  name starts at byte 8, or 9 in version 3. Then come the datatype, the
  dataspace and the data, each right after the previous; in version 1 the
  name, the datatype and the dataspace are each padded to a multiple of 8
  bytes. The attribute's **name** is the name's bytes up to the first NUL (all
  of them if there is none).

Every attribute's name is read; two attributes with the same name reject the
input. An attribute's **value** is read only where the convention reads the
attribute: its flag bits 0 and 1 (shared datatype or dataspace) MUST be
clear; its datatype and dataspace (§8.5) MUST lie within the message; the
dataspace MUST NOT be null; its element count is the product of its sizes (1
for a scalar); and its data, the next count × datatype size bytes, MUST lie
within the message.

### 8.7 Rejection

The input is rejected when a rule of §8.1–§8.6 fails for a structure that
the convention's §2–§4 read, and when the convention gives it no layout:
wherever it says that the input is rejected, or that something MUST hold
and it does not. The source metadata (the convention §5) never rejects the
input: what it cannot read is `null`, or a member listed as unsupported.

### 8.8 Chunk references

Each allocated chunk of dataset `(r, t, c)` at coordinates
`(i, j, k)` that is not wholly outside the image (that is,
`i × cz < Zr`, `j × cy < Yr` and `k × cx < Xr`) is the entry
`<r>/c/<coords>` with the single range `(0, address, size)`; the coords
are `t`, `c`, `i` (each only when its axis is present), `j`, `k`.
Chunks in the padding are not output, and unallocated chunks have no entry.
HDF5 stores edge chunks whole, like Zarr, so a chunk's bytes decode to its
Zarr chunk. (A single range's payload is at most 21 bytes, within the limit
of §1.2.)

### 8.9 Source metadata structures

The source metadata (the convention §5) reads, besides §8.1–§8.6:

- **Datasets of every layout.** A layout message of version 3, 4 or 5 of
  class 0 (compact: the data's size in 2 bytes at byte 2, the data from
  byte 4), class 1 (contiguous: the address at byte 2, undefined when the
  storage is not allocated, and the size at byte 10) or class 2 (chunked,
  as §8.5). Class 3 is a **virtual dataset** (below), whose data is not
  read. The fill value message's value is its data (none when the
  message says it has none), which MUST be empty or of the datatype's size.
- **Datatype messages** of versions 1 to 5 (§8.5 reads versions 1 to 3 for
  the image; later versions keep the same fields).
- **The global heap.** A collection at address `a` starts with `GCOL`,
  version 1, three reserved bytes and the collection's size (8 bytes),
  which MUST be at least 16 and lie within the file. Its objects follow
  from byte 16, each with its index (2 bytes), a reference count (2), 4
  reserved bytes, its size (8) and its data, padded to a multiple of 8
  bytes; an index of 0 ends them, and of two objects with the same index
  the first counts. Each element of a variable-length datatype is 16
  bytes: its length (4 bytes: characters for a string, else elements of
  the base datatype), the collection's address (8) and the object's index
  (4); a length of 0 is an empty element.
- **Filter masks.** A chunk's filter mask (§8.5: in a version 1 B-tree's
  key, a single chunk's layout message, an array index's entry or a version
  2 B-tree's record) may be any value: bit `i` set means that the pipeline's
  filter `i` (from 0, in pipeline order) was not applied to the chunk. A
  partial edge chunk of a dataset with layout flag bit 0 (§8.5) has every
  filter's bit set.
- **Attribute data.** An attribute message lies in the file in one run:
  in the block of the object header that holds it (§8.2), or as a fractal
  heap object (§8.4: a managed object within its direct block, a huge object
  at the address its record gives). Its data is the run's bytes from where
  §8.6 places it.
- **Null dataspaces.** A dataset's or an attribute's null dataspace (§8.5)
  is read as such: it has no data.
- **Shared datatypes.** A dataset's datatype message with flag bit 1
  (shared) set, and the datatype of an attribute message with flag bit 0
  (shared datatype) set, hold a shared message instead of a datatype: byte 0
  is its version; in version 1 the address is the 8 bytes at byte 8, in
  version 2, and in version 3 when byte 1 (the type) is 2, the 8 bytes at
  byte 2; any other shared message leaves the datatype unread. The address
  MUST be defined; it is a committed datatype's object header (§8.2), whose
  datatype message (which MUST NOT be shared) is the datatype. An attribute
  message with flag bit 1 (shared dataspace) set is not read.
- **Links of other types.** A link message of a type other than 0 and 1
  (§8.3) has, after its name, a 2-byte length `m` and its value, `m`
  bytes; when they do not lie within the message the link has no value.
- **External data files.** The external data files message (type 0x07):
  byte 0 (the version) MUST be 1; the number of used slots is the 2 bytes at
  6 and the address of a local heap (§8.3) the 8 bytes at 8; slot `i` is
  the 24 bytes at `16 + 24i`: the offset of its file name in the heap's
  data segment (8 bytes; the name runs to the first NUL, which MUST occur
  within the segment), the offset in that file (8) and the size (8).
- **Object references.** An object reference (datatype class 7, kind 0) is
  8 bytes, a little-endian object header address; 0, the undefined address
  and addresses beyond 2^53 − 1 lead to no object.
- **Region references.** A region reference (class 7, kind 1, of 12 bytes)
  is a global heap collection's address (8 bytes) and an object's index
  (4 bytes); the address 0 or the undefined address is the null reference.
  The object is an object reference (8 bytes, as above) followed by a
  serialized selection of that object's dataspace.
- **Virtual datasets.** In a layout message of version 4 or 5 and class 3,
  the 8 bytes at 2 are a global heap collection's address and the 4 bytes at
  10 an object's index; an undefined address has no mappings. The object's
  byte 0, its version, MUST be 0; its number of mappings is the 8 bytes at 1,
  and the mappings follow: each is the source file's name and the source
  dataset's name (each the bytes up to a NUL, which MUST occur within the
  object), then the source selection and the virtual selection, serialized.
- **Serialized selections.** A selection starts with its type and version,
  4 bytes each:
  - type 0 (none) or 3 (all): version 1, then 8 bytes not read;
  - type 1 (points): version 1, 8 bytes not read and an encoding size `e`
    of 4; or version 2 and `e` in 1 byte. Then the rank `k` (4 bytes), the
    number of points `n` (`e` bytes) and the points, `k` coordinates of `e`
    bytes each;
  - type 2 (hyperslab): version 1, 8 bytes not read, no flags and `e = 4`;
    version 2, its flags (1 byte), 4 bytes not read and `e = 8`; or version
    3, its flags and `e` (1 byte each). Flags other than bit 0 reject the
    selection. Then the rank `k` (4 bytes). With flag bit 0 (regular), for
    each dimension in turn, its start, stride, count and block, `e` bytes
    each (a count or block with all bits set is unlimited); without, the
    number of blocks `n` (`e` bytes) and the blocks, each its `k` start
    coordinates and then its `k` end coordinates, `e` bytes each.

  The rank of points and of a hyperslab without flag bit 0 MUST be at least
  1, and their number `n` at most the number of bytes after it (so that no
  count goes unbounded by the selection's bytes).

  Any other type, version or encoding size (other than 2, 4 or 8), and a
  selection that does not lie within the object, leave it unread.
