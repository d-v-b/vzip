# Migrating OME-Zarr 0.4 to OME-Zarr 0.5

The OME-Zarr profile of [VIRTUALIZE.md](../VIRTUALIZE.md) (revision 13),
numbered as its §11. §1 is in VIRTUALIZE.md and applies here, in particular
the store input rules of §1.4–§1.6; §2 is not used. The profile builds on
the Zarr v2 profile ([zarr2.md](zarr2.md), §10).

## 11. OME-Zarr profile

The other profiles virtualize data that is not Zarr. This one is a
different use of vzip: **format migration without copying data**. Its input
is already Zarr: an OME-Zarr 0.4 hierarchy
([OME-NGFF 0.4](https://ngff.openmicroscopy.org/0.4/)) on Zarr v2 storage.
Its output is the same data as OME-Zarr 0.5
([OME-NGFF 0.5](https://ngff.openmicroscopy.org/0.5/)) on Zarr v3. Every
chunk is referenced in place, unchanged; only the metadata is rewritten.
The archive is a few hundred bytes per chunk, and a reader of OME-Zarr 0.5
reads the 0.4 dataset through it without the dataset being copied or
changed.

The profile was chosen because the store's root has a `.zarray` or a
`.zgroup` and the store declares OME-NGFF 0.4 (§1.4). It checks the input
against the requirements (MUST) of OME-NGFF 0.4 that the output needs to
be a correct OME-Zarr 0.5 dataset, and rejects an input that breaks one
(§11.3). What 0.4 only recommends (SHOULD) is not checked.

Why the chunks can stay where they are: OME-Zarr 0.5 allows every feature
of Zarr v3, "including codecs, chunk grids, chunk key encodings, data types
and storage transformers", unless it disallows one (OME-NGFF 0.5, §1
"Storage format"), and it disallows none of those used here. Zarr v3
defines the `v2` chunk key encoding
([Zarr v3 core specification, chunk key encodings](https://zarr-specs.readthedocs.io/en/latest/v3/chunk-key-encodings/v2/index.html)),
whose keys are exactly Zarr v2's for either separator, and its `transpose`,
`bytes`, `zlib`, `gzip`, `zstd` and `blosc` codecs decode every chunk that
§10.2 accepts.

### 11.1 Hierarchy

The nodes, the arrays and the chunks are §10's. Every rule of §10.1–§10.3
applies, with the same rejections: the candidates and nodes (explicit and
implicit groups, arrays, nothing read inside an array), the `.zgroup`,
`.zarray` and `.zattrs` documents, each array's `zarr.json` (shape, data
type, chunk grid, the `v2` chunk key encoding with the array's separator,
fill value, codecs and attributes), and its chunk keys and chunk objects.
The output has the same keys as §10's output, plus §11.6's OME-XML
objects, and the same `zarr.json` documents except for:

- the attributes of OME groups (§11.4);
- the member `dimension_names` of image levels (§11.5).

Every other node, called **not OME** here, is converted by §10 unchanged:
implicit groups, explicit groups without OME members (such as the row
groups of a plate, or a group of tables next to an image), and arrays that
are not image levels, their attributes copied as they are. Objects that are
not part of §10's hierarchy (other than §11.6's) are not part of the
output either.

### 11.2 OME groups

A **relative path** is a string of one or more segments separated by `/`,
none of them empty, `.` or `..`; relative to a group at path `G`, it names
the node at `G` joined with it (§10.1). An **image** is an explicit group
whose attributes have the member `multiscales`.

The **OME members** are `multiscales`, `omero`, `labels`, `image-label`,
`plate`, `well` and `bioformats2raw.layout`. An **OME group** is an
explicit group whose attributes have at least one of them, or whose path is
`C/OME` (`OME` when `C` is the root) for a group `C` whose attributes have
`bioformats2raw.layout`, when its attributes have the member `series` (then
`series` is an OME member of that group too: OME-NGFF 0.4 §3.2 puts it
there).

An explicit group whose attributes have a member `ome` rejects the input
(its output would mix an existing `ome` with the converted one, and
OME-NGFF 0.5 requires one version in a hierarchy).

The groups are:

- **images** (`multiscales`);
- **labels groups** (`labels`), and **label images**: the images that a
  labels group lists, and the groups with `image-label`;
- **plates** (`plate`) and **wells** (`well`); the **fields** of a well are
  the images its `images` list;
- **collections** (`bioformats2raw.layout`).

A group may be several of these at once (a bioformats2raw collection is
often a plate too); each applies.

### 11.3 Validation

Each rule below is a requirement of OME-NGFF 0.4 (section numbers are
OME-NGFF 0.4's) that OME-Zarr 0.5 also has; an input that breaks one is
rejected. "Integer" is §1.6's integer. The rules apply to every OME group,
wherever it is in the hierarchy, and a rule's position in this list does
not matter (§1.2, evaluation order).

**Versions.** The member `version` of a multiscale (an element of
`multiscales`), and of `plate`, `well` and `image-label`, MUST be the
string `"0.4"` when it is present. (It is optional in 0.4: the member is a
SHOULD. Another version is not converted, and 0.5 requires one version in a
hierarchy.)

**Images** (§3.1, §3.3, §3.4). For each image at path `G`:

- I1. `multiscales` is a nonempty array of objects.
- For each multiscale `m`, with `n` its number of axes:
  - I2. `axes` is an array of 2 to 5 objects; each axis's member `name` is
    a string, and the names are distinct.
  - I3. Each axis's member `type` is absent, `null` or a string. The axis
    is a **space** axis if `type` is `"space"`, a **time** axis if it is
    `"time"`, and an **other** axis otherwise (a channel axis, a custom
    type, `null` or no type: 0.4 allows "one additional entry of
    type:channel or a null / custom type"). In order, the axes are at most
    one time axis, then at most one other axis, then 2 or 3 space axes.
  - I4. `datasets` is a nonempty array of objects; each one's member `path`
    is a relative path naming an array (a node of §10.1, not a group or a
    directory) under `G`, with `n` dimensions.
  - I5. Each dataset's `coordinateTransformations` is an array of one or
    two objects: the first has `type` `"scale"` and `scale`, an array of
    `n` numbers; the second, if there is one, has `type` `"translation"`
    and `translation`, an array of `n` numbers. (So a translation before
    the scale, an `identity`, or a third transformation rejects.)
    Transformations given by `path` instead of a vector (0.4 allows "binary
    data at a location in this container", in a format it does not
    define) are not supported and reject the input.
  - I6. `m`'s own `coordinateTransformations`, if `m` has the member,
    follows I5.
  - I7. The datasets are ordered from highest resolution to lowest (0.4:
    "from largest (i.e. highest resolution) to smallest"): along every
    axis, each dataset's scale is at least the previous dataset's.
- I8. No array is a level (a dataset's path) of two multiscales whose axis
  names differ (0.5 gives each level the `dimension_names` of its image,
  §11.5, which cannot be two lists).

The member `unit` of an axis is not read: 0.4 says an axis SHOULD have one,
and it SHOULD be a UDUNITS-2 name. The members `name`, `type` and
`metadata` of a multiscale are SHOULDs and are not read either.

**omero** (§3.5). For each OME group with `omero`:

- O1. `omero` is an object whose member `channels` is an array of objects;
  each channel's `color` is a string of 6 hexadecimal digits, and its
  `window` an object whose members `min`, `max`, `start` and `end` are
  numbers.

**Labels** (§3.6, §3.7). For each labels group at path `L` and each group
with `image-label`:

- L1. `labels` is an array of relative paths, each naming an image under
  `L`.
- L2. `image-label` is an object, and its group is an image ("image-label
  groups MUST also contain multiscales metadata").
- L3. `colors`, if present, is an array of objects whose member
  `label-value` is an integer, distinct across `colors`; a color's `rgba`,
  if present, is an array of 4 integers from 0 to 255.
- L4. `properties`, if present, is an array of objects whose member
  `label-value` is an integer.
- L5. `source`, if present, is an object whose member `image`, if present,
  is a string.

For each label image `X`, its **source image** is its `image-label`'s
`source.image` resolved against `X`'s path (segments `..` remove a segment,
`.` and empty ones are skipped), or `../../` resolved the same way if `X`
has no `source.image` (0.4's default):

- L6. A `source.image` that is given names an image (resolved within the
  store).
- L7. If the source image is an image, each multiscale of `X` has as many
  datasets as the source image's first multiscale ("the two datasets series
  MUST have the same number of entries").
- L8. Every level of `X` has an integer data type (`int8` to `uint64`; 0.4:
  "only integer values are supported"; 0.5 requires it).

**Plates and wells** (§2.2, §3.8, §3.9). For each plate at path `P`:

- P1. `plate` is an object; `columns` and `rows` are arrays of objects
  whose member `name` is a string of one or more ASCII letters and digits,
  the names distinct within each array.
- P2. `wells` is an array of objects; each well's `rowIndex` and
  `columnIndex` are integers indexing `rows` and `columns`, and its `path`
  is the string `<row name>/<column name>` of those two; the path names a
  well (an explicit group with `well`) under `P`.
- P3. `acquisitions`, if present, is an array of objects; each `id` is an
  integer of at least 0, distinct; `maximumfieldcount`, if present, an
  integer of at least 1; `name` and `description`, if present, strings;
  `starttime` and `endtime`, if present, integers.
- P4. `field_count`, if present, is an integer of at least 1, and `name`,
  if present, a string.

For each well at path `W`:

- P5. `well` is an object whose member `images` is an array of objects;
  each one's `path` is a string of one or more ASCII letters and digits,
  the paths distinct, and names an image under `W` (a field); its
  `acquisition`, if present, is an integer.
- P6. When the well is listed by a plate that has `acquisitions`: each
  field's `acquisition`, if present, is the `id` of one of them, and when
  there is more than one acquisition every field has an `acquisition`.

**Collections** (§3.2). For each collection at path `C`:

- B1. `bioformats2raw.layout` is the integer 3.
- B2. If the OME group `C/OME` has `series`: it is an array of relative
  paths, each naming an image under `C`.
- B3. Otherwise, if `C` has no `plate`: the images whose paths are `C`
  joined with a decimal number (no leading zeros) are numbered `0`, `1`,
  …, `k − 1` for some `k ≥ 1` ("consecutively numbered groups starting
  from 0").

### 11.4 Group attributes

The output attributes of an OME group with attributes `A` are `A` without
its OME members, plus the member `ome`:

```json
{"version": "0.5", "multiscales": [...], "omero": ..., ...}
```

which has, for each OME member `k` of `A`, the member `k` with `A`'s value
changed only as follows:

- `multiscales`: each multiscale without its member `version` (0.5 has no
  per-multiscale version; the hierarchy's version is `ome.version`);
- `omero`, `image-label`, `plate` and `well`: the object without its member
  `version`, if it has one (0.5 dropped these: its examples, and its JSON
  schemas, have none);
- `labels`, `bioformats2raw.layout` and `series`: unchanged.

Every other member of `A` (for example `_creator`, or a user's own) stays
outside `ome`, unchanged. A group that is not an OME group keeps its
attributes (§11.1).

### 11.5 Arrays

OME-NGFF 0.5 requires every level of an image to have `dimension_names`
equal to the image's axis names (§2.1 of 0.5: "MUST be included in the
zarr.json of the Zarr array of a multiscale level and MUST match the names
in the axes metadata"). Each array that is a level of a multiscale (the
array a dataset's path names, label images included) gets the member
`"dimension_names"`: that multiscale's axis names, in order. By I8 every
multiscale of which it is a level gives the same names. No other array gets
`dimension_names`. The array's attributes are unchanged.

### 11.6 Chunks and OME-XML

Chunks are §10's: each nonempty chunk object is an entry under its own key,
referencing the whole object through its own `url` source (§1.4), and the
arrays keep the `v2` chunk key encoding with their own separator, so the
keys are unchanged.

A bioformats2raw collection SHOULD hold its OME-XML metadata in
`OME/METADATA.ome.xml`, which OME-Zarr 0.5 keeps at the same place. For each
collection `C`, the object `C/OME/METADATA.ome.xml` (`OME/METADATA.ome.xml`
for the root), if the store has it, is referenced whole in the same way:
an entry under that key, with its own source, in the order of §1.4 among
the chunk entries (an object of size 0 has no entry). It is not read. (The
group `OME` itself is a node only if it has a `.zgroup`; the object is kept
either way.)

### 11.7 Not checked

This section is informative. The profile does not check:

- what 0.4 says SHOULD (or MAY) be: units, axis types beyond I3, the names
  and `type` of multiscales, `version` members, `field_count` and `name` of
  a plate, every well and label being listed, labels' `colors` existing, and
  the advice on row and column names on case-insensitive file systems;
- what 0.5 requires that 0.4 does not: that groups between a labels group
  and its label images hold no metadata;
- what neither version makes checkable from metadata alone: that each
  dimension of a label equals its image's or is 1, that an image's levels
  shrink in shape as their scales grow, that a field's acquisition count
  matches `maximumfieldcount`, and the OME-XML (`Image` elements matching
  the series).

### 11.8 Summary

This section is informative. Both implementations print a one-line JSON
summary: `groups` (explicit and implicit), `arrays`, `chunks` (chunk
entries), `emptyChunks` (chunk objects of size 0), `objects` (the store's
objects), `images`, `labels` (label images), `plates`, `wells`, `fields`
(the images wells list), `omeXml` (OME-XML entries) and `listingRequests`.
