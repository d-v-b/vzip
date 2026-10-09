# The IMS convention

The Zarr layout of an Imaris file (`.ims`, HDF5), and the translation of
its HDF5 attributes into JSON. What all of vzip's conventions share is in
[conventions/README.md](../README.md), cited here as "conventions §n". How
vzip produces this layout as a virtual store, and the subset of HDF5 that
it reads, is the IMS profile, [profiles/ims.md](../../profiles/ims.md).

Convention version: 0 (until release, README §1) · UUID: `5067a535-8261-4b25-a93c-1985ed333bde` ·
Schema: [schema.json](schema.json)

This convention gives a layout only to the files that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a file that fails has no layout under this
convention, and the profile rejects it.

## 1. Declaration

The root declares the convention by [conventions §2](../README.md#2-attributes),
with `"profile": "ims"`, `"version": 0`, `"revision": 23` (README §1) and the file's URL as `source.url`.
The root has no source metadata of its own: the whole HDF5 file is described
on the source metadata node (§5). Its CMO is:

```json
{
  "uuid": "5067a535-8261-4b25-a93c-1985ed333bde",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/heads/main/conventions/ims/schema.json",
  "spec_url": "https://github.com/d-v-b/vzip/blob/main/conventions/ims/README.md",
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
[profiles/ims.md §8.1](../../profiles/ims.md#81-hdf5-structures)–[profiles/ims.md §8.6](../../profiles/ims.md#86-attributes) describe; whatever falls outside the subset rejects the input. The
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
opened. Each of that group's links `Image`, `Channel c` (for `c < C`,
when `C` is at most 64) and `TimeInfo` that exists is followed, the group opened, and its
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
([conventions §6](../README.md#6-source-metadata-as-json)): UTF-8 if valid,
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
2. a link of another type than hard and soft ([profiles/ims.md §8.9](../../profiles/ims.md#89-source-metadata-structures)): an
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
([profiles/ims.md §8.9](../../profiles/ims.md#89-source-metadata-structures)) is the committed datatype's own. When the walk met
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
`unsupported`): `null` if the profile cannot read them ([profiles/ims.md §8.6](../../profiles/ims.md#86-attributes)), else an object with one
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
    ([conventions §6](../README.md#6-source-metadata-as-json)), when no NUL
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
      an object reference gives it), `S` its selection ([profiles/ims.md §8.9](../../profiles/ims.md#89-source-metadata-structures)):
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

  A chunk whose filter mask ([profiles/ims.md §8.9](../../profiles/ims.md#89-source-metadata-structures))
  has bit `i` set is stored without the pipeline's filter `i` (HDF5 skips
  the shuffle for variable-length data, say): it is decoded without the
  filters its mask skips, and a dataset with such a chunk has all its
  chunks decoded and copied, as for shuffle, whatever its filters (its
  allocated chunks' byte counts totaling at most 2^24 bytes). A **partial
  edge chunk** (one that reaches past the dataset's dimensions along some
  dimension) of a dataset whose layout stores partial edge chunks
  unfiltered (`H5Pset_chunk_opts`, [profiles/ims.md §8.5](../../profiles/ims.md#85-datasets-and-chunks))
  is such a chunk with every filter's bit set: it is copied raw, and the
  dataset's other chunks are decoded.

  An **implicit** chunk index (profiles/ims.md §8.5) whose chunks, in
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
`"virtual"`, its mappings ([profiles/ims.md §8.9](../../profiles/ims.md#89-source-metadata-structures)) in order, each `{"file": T,
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
  "revision": 23,
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
