# vzip's Zarr conventions

A Zarr hierarchy made from a source in another format (a TIFF, an ND2 file, an
N5 store, ...) looks like any other Zarr hierarchy. These conventions make
it describe itself: which source it presents, which format that source is
in, and everything the source's own header or metadata says. There is one
convention per source format, each specified in `<format>/README.md` next to
its JSON Schema `<format>/schema.json`:

| format | convention | UUID |
|---|---|---|
| TIFF, OME-TIFF, Aperio SVS | [tiff](tiff/README.md) | `48e9ac4e-1156-4a62-955e-20467d9c2700` |
| Hamamatsu NDPI | [ndpi](ndpi/README.md) | `6cac71ef-dbb2-4acd-b60c-00389aa4238a` |
| Nikon ND2 | [nd2](nd2/README.md) | `59612f14-e314-4207-ba00-8f422ba71490` |
| DICOM | [dicom](dicom/README.md) | `acf17198-e5a5-48d3-8187-22ec4bb40ea5` |
| NIfTI-1 and NIfTI-2 | [nifti](nifti/README.md) | `06e5809d-4d54-4b72-afd0-6bf61a7b4c85` |
| Imaris IMS | [ims](ims/README.md) | `5067a535-8261-4b25-a93c-1985ed333bde` |
| N5 | [n5](n5/README.md) | `ad5d4c39-c69e-48f7-a3ef-4cc8c607d416` |
| Zarr v2 | [zarr2](zarr2/README.md) | `8e792619-d671-4687-ab51-752885dd3ee6` |
| OME-Zarr 0.4 | [ome-zarr](ome-zarr/README.md) | `b74ea302-65bb-49ae-b81f-f9bb52cd4eed` |
| Sentinel-2 SAFE (Level-1C, Level-2A) | [safe](safe/README.md) | `ef81346c-19e8-42ad-93b0-a279ccaf44c1` |
| Zeiss CZI | [czi](czi/README.md) | `7a0733c7-d4be-4482-a64f-6904d9354ea5` |

A format's convention specifies the hierarchy's whole **Zarr layout**: which
groups and arrays it has and where, each array's metadata (shape, data
type, chunk grid, codecs, dimension names), the OME-NGFF metadata, and the
attributes under the convention's key, including the translation of the
source's header into JSON. It says what each chunk holds in terms of the
source (for example, "the chunk at `0/c/t/0/z/0/0` is frame `f`"), but not
how a store produces those bytes. vzip produces these hierarchies as
virtual stores whose chunks are byte ranges of the source; that is
specified separately, in [VIRTUALIZE.md](../VIRTUALIZE.md) and its
profiles, which refer to these conventions for the layout.

Every convention is held to two requirements:

1. **Reconstruction.** A reader of the hierarchy can recover all the
   information of the source. This means its data, and its metadata, and
   every other value it holds, whether or not the layout uses it. What may
   be left out is only what can be derived from the rest, or that holds
   nothing:
   - the **layout**: where the bytes are and how they are framed (offsets,
     byte counts, chunk maps, block headers, the encoding tables a chunk
     already carries);
   - **dead space**: padding and unused bytes.

   Byte-for-byte reconstruction of the file is not required.
2. **Efficiency.** The hierarchy reads well:
   - Small typed metadata is JSON, in attributes.
   - Metadata that is large, or that grows with the data, is in arrays
     (§7), or on the source metadata node (§2), which a reader only opens
     when it wants it.
   - Values are referenced where the source holds them. They are copied
     only when they are scattered or must be reordered.

This document holds what every convention shares. The key words MUST, MUST
NOT, SHOULD, SHOULD NOT and MAY are to be interpreted as described in RFC
2119. Numbers follow [VIRTUALIZE.md §1.3](../VIRTUALIZE.md#13-arithmetic)
(IEEE 754 binary64, correctly rounded).

## 1. Conventions

Each convention follows the [Zarr conventions
framework](https://github.com/zarr-conventions/zarr-conventions-spec).

**Versions.** Each convention has a **version**, an integer stated in its
document's header.

- **Until vzip's first release**, every convention is at version **0**, and
  hierarchies also record the **revision** of
  [VIRTUALIZE.md](../VIRTUALIZE.md) (the number in its header) that
  produced them (§2). Version 0 promises nothing: any revision may change a
  layout, and the revision is what tells two layouts apart.
- **At the release**, every convention becomes version 1, tagged
  `virtualize-<p>-v1` in the vzip repository. From then on a convention's
  version increases by 1 whenever the layout it specifies, or the virtual
  store that vzip's profile for that format specifies, changes for some
  source, including whether the source is accepted (a **breaking change**);
  an editorial change, or a change to another format, leaves it as it is.
  Version `N` of the convention for format `p` is tagged
  `virtualize-<p>-v<N>`, and hierarchies no longer record a revision.
- A reader that relies on a convention MUST reject a hierarchy whose
  version it does not know, as each convention's JSON Schema does.

**The convention metadata object.** Format `p` at version `N` is declared by
the Convention Metadata Object (CMO)

```json
{
  "uuid": "<the UUID above>",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/tags/virtualize-<p>-v<N>/conventions/<p>/schema.json",
  "spec_url": "https://github.com/d-v-b/vzip/blob/virtualize-<p>-v<N>/conventions/<p>/README.md",
  "name": "vzip_virtualized",
  "description": "The Zarr layout of a <T> source virtualized by vzip, and the source's metadata"
}
```

with no other members. At version 0, which has no tag, the URLs name the
development branch instead: `.../refs/heads/main/conventions/<p>/schema.json`
and `.../blob/main/conventions/<p>/README.md`. `T` is the format's title (`TIFF`, `NDPI`,
`ND2`, `DICOM`, `NIfTI`, `IMS`, `N5`, `Zarr v2`, `OME-Zarr`, `Sentinel-2
SAFE`, `CZI`). The UUID is the same for every version; the version is in the
URLs. All eleven conventions nest their property under one key, `vzip_virtualized`, which is
also their `name`: the framework asks a nesting convention's `name` to be
its key, and a key without a colon is not mistaken for a namespace prefix.

## 2. Attributes

**Which nodes.** The root node of the hierarchy (the document `zarr.json`)
always declares its format's convention. Any other node declares it when,
and only when, the node has source metadata (below). A node that declares
it has, in its attributes, the member `zarr_conventions`, whose value is
`[C]` with `C` the format's CMO (for the SAFE convention, `C` followed by
the CMOs of the GeoZarr conventions the node uses), and the member
`vzip_virtualized`.

**The property.** On the root, `vzip_virtualized` is the object

```json
{"profile": "<p>", "version": N, "revision": R, "source": {"url": "<U>"}, "<p>": S}
```

and on any other node `{"<p>": S}`, where:

- `p` and `N` are the format and its convention's version;
- `R` is the revision of VIRTUALIZE.md that produced the hierarchy, an
  integer, present at version 0 only (§1);
- `U` is the URL of the source: of the file for a file, and of the store,
  ending in `/`, for a store;
- `S` is the node's **source metadata**, an object whose content the
  format's convention specifies (its "Source metadata" section): what the
  source's header or metadata holds for that node, translated by §6. The
  member `"<p>"` is present only when `S` has at least one member.

There are no other members. The property holds no time and nothing about
the program that made the hierarchy, so that two conforming producers make
equivalent hierarchies from the same source.

**The source metadata node.** A file format's metadata that grows with the
data (one item per frame, plane or channel), and the source's values kept as
arrays (§7), are on the group `vzip_source`, a child of the root. It declares
the convention, with its own source metadata, when it has any, and holds the
arrays at paths the format's convention gives. OME-NGFF readers ignore it.
Producers SHOULD store its documents so that a reader fetches them only when
it opens the node (vzip writes them with the chunks, not with the documents
read when an archive is opened). The node exists only when it has
source metadata or arrays, or, for the store conventions, other objects of
the store, which it holds whole under `vzip_source/objects/`, or the keys of
the store's empty objects. For the TIFF, ND2 and CZI conventions,
`vzip_source` is the source's IR mirror (§8), from which the source is
rebuilt byte for byte.

**Nothing else.** Outside `vzip_virtualized`, a node's attributes hold only
members that the hierarchy's target formats define, where the format's
convention places them: `ome` (OME-NGFF 0.5, §4), the GeoZarr members of
the zarr-conventions `proj`, `spatial` and `multiscales` conventions
(`proj:code`, `proj:wkt2`, `spatial:*` and `multiscales`, for the SAFE
convention, which lists their CMOs in `zarr_conventions` after its own),
and `zarr_conventions`. Everything
taken from the source goes under the key, on the node it belongs to,
including attributes a source store had (and the conventions they
declare), so it can neither collide with nor masquerade as the target
formats' metadata. A source that is itself such a hierarchy therefore
nests its own record.

## 3. Arrays

Unless the format's convention says otherwise (the store conventions, N5,
Zarr v2 and OME-Zarr, define their arrays themselves; the CZI convention
gives clipped levels the zarr-extensions `rectilinear` chunk grid and adds
the codecs `imagecodecs_jpegxr` and `numcodecs.shuffle`), every array's
`zarr.json` is the JSON object:

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
always present; the format's convention says when the others are.

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
     `[y, x, c]`, after which `transpose` applies). Decoding follows OpenJPEG,
     as imagecodecs does: a 3-component codestream whose first component is
     at full resolution and whose other two are subsampled holds YCbCr, and
     decodes to RGB (as in Aperio's compression 33003);
   - `{"name": "imagecodecs_jpeg"}` for JPEG (one complete JPEG stream, ISO/IEC
     10918-1 with the JFIF/Adobe color conventions, per chunk, decoding to
     the chunk's `[y, x]` or `[y, x, c]` like JPEG 2000).
3. A compressor, when the bytes are compressed:
   - `{"name": "zlib", "configuration": {"level": 1}}` (zlib streams);
   - `{"name": "zstd", "configuration": {"level": 0, "checksum": false}}`.

## 4. Images

An image is a group whose `zarr.json` is
`{"zarr_format": 3, "node_type": "group", "attributes": {"ome": M}}`, where `M`
is the OME-NGFF 0.5 object below. The image's other
attributes are those of §2 (the convention's declaration and property), and
no other member is allowed. `M` is:

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

- `name` is present only when the format's convention gives one.
- Each axis has `name` and `type` (`"time"`, `"channel"` or `"space"`), and
  `unit` only when the format's convention gives one.
- `datasets` lists the pyramid levels, full resolution first, with paths
  `"0"`, `"1"`, ...; each has a `scale` transformation, one number per axis,
  followed by a `{"type": "translation", "translation": [...]}`
  transformation (one number per axis) only where the format's convention gives one.
- `M` has an `"omero"` member, next to `multiscales`, only where the format's
  convention says so.

## 5. Units

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

A **length** in one of these units converts to another by the units'
sizes in metres: micrometer 1e-6, nanometer 1e-9, millimeter 1e-3,
centimeter 1e-2, meter 1, angstrom 1e-10, picometer 1e-12, inch 0.0254,
foot 0.3048. A value `v` in unit `a` is `v × (size(a) / size(b))` in unit
`b` (the division first).

**Translations.** Where a convention places an image in space (§4), the
translation applies to every level, and is 0 for every axis it does not
name. A position given for the centre of an image of level-0 size
`W0 × H0` with x and y scales `sx`, `sy` becomes the translation
`x = cx − W0 × sx / 2`, `y = cy − H0 × sy / 2` (products first).

## 6. Source metadata as JSON

A convention's "Source metadata" section lists, for each node, the source's
fields and how each becomes a member of `S`. Unless it says otherwise:

- **Names.** A field's member is named as the format's own specification
  names the field, and members appear in the order the section lists them.
  A field the source does not have is absent (not `null`). The order of an
  object's members is not significant: producers compare JSON values
  ([VIRTUALIZE.md §1.1](../VIRTUALIZE.md#11-output-and-equivalence)), and a
  JavaScript producer cannot keep integer-like names in place.
- **Integers** become JSON numbers when their magnitude is at most
  2^53 − 1, and otherwise the string of their decimal digits (with a
  leading `-` if negative).
- **Floating-point numbers** become JSON numbers, the source's value
  converted to binary64 exactly (a binary32 value included). NaN, +∞ and
  −∞ become the strings `"NaN"`, `"Infinity"` and `"-Infinity"`, as Zarr
  fill values do. −0 may be written as `0` or `-0.0`; the two are equal.
- **Text** fields become **text values**: for a fixed-size character
  field, its bytes up to the first NUL (all of them if there is none). If
  the bytes are valid UTF-8, the text value is the JSON string they
  encode. Otherwise it is the object `{"latin1": T}`, where `T` is the
  string in which each byte `b` is the code point U+00`bb` (ISO 8859-1, not
  windows-1252, which some decoders give for that label). The tag keeps two
  byte strings that would read alike apart (`C3 A9` is `"é"`, `E9` is
  `{"latin1": "é"}`), so the bytes can always be recovered.
- **Names** taken from the source (an attribute's or a chunk's name)
  become JSON strings by the same reading, without the tag: UTF-8 if
  valid, else ISO 8859-1. When two names of one object read alike, the
  first, in the order the convention gives, is kept and the others are
  left out (their values are not read).
- **Arrays** of values become JSON arrays, in order.
- **Opaque bytes** (binary fields and payloads a convention does not
  decode) become the string of their base64 encoding (RFC 4648 §4, with
  padding).

Producers compare these as JSON values: numbers by their binary64 values,
strings by their characters (VIRTUALIZE.md §1.1).

These rules apply to values the convention translates from the source's
bytes. Members copied from a JSON document of the source (a store's
attributes) are copied as JSON values, not translated: their numbers are
read as [VIRTUALIZE.md §1.6](../VIRTUALIZE.md#16-json-documents) says. A
number written as an integer (no fraction or exponent) is copied exactly,
every digit, even beyond 2^53 − 1, and written back so; any other number is
its binary64 value. Where a convention uses a number in the layout, it uses
the binary64 value.

## 7. Source values as arrays

A value that is too large for JSON, that is a column of numbers, or that is
opaque is an array of the source metadata node (§2). Each such array's
`zarr.json` is as in §3, with `fill_value` 0 unless the convention says
otherwise, `codecs` `[{"name": "bytes", "configuration": {"endian": E}}]`
(`[{"name": "bytes"}]` for one-byte data types), where `E` is `"little"`
unless the convention gives the source's byte order, followed by `zlib`
(`{"name": "zlib", "configuration": {"level": 1}}`) when the convention
keeps the source's deflate-compressed chunks, and the `dimension_names` the
convention gives (none when it gives none). Its attributes are empty, or the
declaration of the convention with the array's own source metadata.

- **A stream** (one typed value per frame, or per plane) has the data type
  of its values. It is shaped to the image's frame grid, as the convention
  defines it, so that its index along each axis is the image's.
- **Bytes** (a value the convention does not decode) are a 1-D `uint8`
  array, dimension `byte`, of the value's length `n` (at least 1). With
  `k = ceil(n / 2^24)` chunks of `ceil(n / k)` bytes each, the last chunk
  is padded with zero bytes: Zarr stores edge chunks whole.
- **Contiguous values** (an array of shape `S`, of rank `r`, whose elements
  of `e` bytes the source holds contiguously in row-major order, as rows of
  numbers or of fixed-size records) are cut so that no chunk holds more than
  2^24 bytes and none lies outside the array. With `s_j = e × S[j+1] × … ×
  S[r−1]`, the bytes of one index along axis `j`, and `L` the chunk limit
  (2^24 bytes unless the convention gives another), an axis `j` with
  `s_j ≤ L` and `n = S[j]` is cut into `k = ceil(n / floor(L / s_j))`
  chunks, or rather, among the counts `q = k`, …, `min(2k, n)` (each with
  chunks of `c = ceil(n / q)` indices and the edge padding
  `(ceil(n / c) × c − n) × s_j` bytes), the first whose padding is at most
  1024 bytes, or, if none is, the one with the least padding (the first of
  equals), when that padding is at most 64512 bytes (2^16 − 2^10) and,
  times the number of edge chunks (`S[0] × … × S[j−1]`), at most 2^20
  bytes or 1/64 of the array's bytes (`floor(e × S[0] × … × S[r−1] / 64)`),
  whichever is larger. The cut axis `a` is the first `j` with
  `s_j ≤ L` (`e` is at most `L`) that some such count fits, or the last axis,
  with `k` chunks, if none does. The chunk shape is
  1 along the axes before `a`, `c` along `a` and `S[j]` along those after,
  and the grid has `ceil(n / c)` chunks along `a`. The chunk at
  `(i_0, …, i_{a−1}, q, 0, …, 0)` is the `m × s_a` bytes, `m = min(c, n −
  q × c)`, that start `((… (i_0 × S[1] + i_1) × … ) × S[a−1] + i_{a−1}) × n
  × s_a + q × c × s_a` bytes into the values; when `m < c` it is padded with
  `(c − m) × s_a` zero bytes, a literal of the reference when the reference
  fits in a payload ([VIRTUALIZE.md §1.2](../VIRTUALIZE.md#12-input)), and otherwise the chunk is copied with
  its padding. An array of rank 0 is one chunk; an array with a size of 0
  has no chunks, and the chunk shape `max(1, S[j])`.
- **A family of byte values** (one value per index `i`, such as a per-frame
  blob) is:
  - when every member has the same length `L`, a 2-D `uint8` array of shape
    `[count, L]`, dimensions `index` and `byte`, where `count` is the largest
    index plus 1. When every index has a member and each member starts where
    the one before it ends in the source, the array is contiguous values
    (below), cut as they are. Otherwise, when `L` is at most 2^24, its chunks
    are `[1, L]`, one per member, and an index without a member has no
    chunk; and when `L` is over 2^24, the family takes the next form.
  - otherwise, a group with two arrays:
    - `offsets`: `int64`, of shape `[count + 1]`, cut as contiguous values
      (below). `offsets[i]`
      is where member `i` starts in `data`, and `offsets[count]` is the total
      length (an absent member is empty).
    - `data`: `uint8`, of that total length, holding the members in index
      order, in `k = ceil(total / 2^20)` chunks of `ceil(total / k)` bytes
      (the last padded with zero bytes). It is present only when
      the total is at least 1.

What each chunk holds is these bytes. A producer references them in the
source where it can, and copies them where it must: when the values are
scattered, or when the convention reorders them. Within a chunk of a
family's `data`, the pieces of members that are adjacent in the source (one
ends where the next starts) are one range, so members stored one after
another, such as a TIFF's contiguous strips, are referenced, not copied.

## 8. The IR mirror

The TIFF, ND2 and CZI conventions keep the source's metadata as one
format-free structure on `vzip_source`: the source's **IR** (intermediate
representation), a tree of **elements** whose leaves partition the source's
bytes, stored as a table (the **mirror**) and shown as JSON (the **view**).
A reader rebuilds the source byte for byte from the table and the bytes of
source 0, with no knowledge of the format; the format's convention says what
the elements are (its **source model**). Sections 8.1 to 8.7 are all a reader
needs to read the table, check it, and rebuild the source; 8.8 is how a
producer writes it, one way only, so that the mirror of a source is the same
whoever writes it.

### 8.1 Elements

An element has a **kind**: `0` struct (a container), `1` value (a field, of
a declared type, 8.6), `2` data (a decodable unit: a tile, a frame, a
subblock's pixels, with a form, 8.5), `3` derived (bytes that exist only
after a transform, such as a deflated record, with a form that names the
transform and may give their `size`), `4` alias (a second name for an
element already described, its **target**; it claims no bytes) or `5` gap
(bytes no other element claims). It has a **parent** (every element but the
root, which is element 0), a **name** and an optional **name index** (the
element's full name is the name followed by the index in decimal, so
`tags/` with index 256 is `tags/256`), a **type** (values), a **space** (0:
its extent is in the source; `d`: in the derived bytes of element `d`), and
an **extent** (start, length; length 0: none). An element's **path** is the
full names of its ancestors below the root and its own, joined by `/`; the
root's path is `""`.

**The root.** Element 0, the **root**, is a struct with the name `""`, no
name index, space 0 and the extent `(0, Z)`, `Z` the source's size (8.2):
its path, `""`, denotes the whole source, as `""` is the whole document in
JSON Pointer (RFC 6901) and the root node in Zarr. The root is not a run.
Its extent states the range the leaves partition; it claims nothing itself,
since a struct is not a leaf, so coverage and injectivity (below) concern
the leaves alone and are unchanged by it. (When `Z` is 0 the root's length
is 0, so it has no extent.) The empty path is an IR path only: archive keys
stay non-empty ([SPEC.md §3.3](../SPEC.md#33-keys)).

**Names.** Every element but the root has a full name that is not empty.
Among the children of one element (each member of a run being one child),
no full name equals another's, and none equals another's followed by `/`
and more characters (so `tags/256` and `tags/256~1` may be siblings, but
not `a` and `a/0`). Hence every element's path is its own, and no element
but the root has the path `""`: two elements with one path would have, at
the first depth where their chains from the root differ, two siblings
(ancestors, or the elements themselves) whose full names are equal or one
of which continues the other with `/`. A table that breaks these rules is
invalid (8.3). A path
therefore names one element; the view (8.7) gives an alias's target by its
path. A format whose names come from the source (a chunk's name, a record's
key, an XML tag), which may be empty, repeat, or hold `/`, names those
elements so that this holds; [ND2 §5.3](nd2/README.md#53-elements) gives one
rule for that, which any format may adopt.

These rules are those of every single-file profile's IR: TIFF, ND2 and CZI
today, and any format whose source model a later revision moves onto the
IR (such as DICOM or NIfTI), without restating them. A format's convention
lists its elements by their paths below the root.

The **leaves** are the elements of kinds 1, 2, 3 and 5 with a length, in
space 0. The invariants: **coverage**, the leaves' extents partition
`[0, size)` of the source (they cover it and none overlap); **injectivity**,
no byte is claimed by two leaves (two names for the same bytes are an alias
and its target); every parent precedes its children; every alias's target
chain ends at an element that is not an alias, within 64 steps; for each
derived element whose form is a JSON object with a member `size`, the leaves
in its space partition `[0, size)` of its derived bytes; and the root and
the names are as above.

A **run** is an element whose subtree stands for `count` copies of itself
(its **members**): member `j` has every extent of the subtree shifted by
`j × stride` and the run's root's name index increased by `j`. A run's
leaves are its members' leaves. Runs do not nest. A parser may fold its IR
into runs; the canonical table (8.8) expands them, and a reader accepts them.

### 8.2 The table

`vzip_source` declares the convention (§2). Its source metadata `S` is
`{"ir": I}`, where `I` is the object

```json
{"version": 2, "size": Z, "elements": n, "rows": m, "names": [...], "types": [...], "forms": [...]}
```

- `Z`: the source's size in bytes; `n`: the elements, with every column
  run expanded (8.3); `m`: the rows the table stores;
- `names`, `types`, `forms`: the interned strings, whose indexes the rows
  and tables use; `names[0]` and `types[0]` are `""` (`names[0]` is the
  root's name, 8.1).

The table is two arrays under `vzip_source/ir/` (§3 documents, data type
`int64`, `fill_value` 0, codecs `bytes` little-endian, which a reader also
accepts followed by `zlib` at any level, though the canonical table, 8.8, has
none; edge chunks padded with zeros; no attributes):

- **`rows`**, shape `[8, m]`, dimension names `column` and `index`, chunk
  shape `[8, c]` with `c = min(m, 16384)` (at least 1): row `k` of the
  stored rows has, in column `0` its kind, `1` its **up** (its expanded id
  minus its parent's; 0 for the root), `2` its name (into `names`), `3` its
  name index (the 64-bit two's-complement pattern of an unsigned index;
  `-1`: none), `4` its type (into `types`), `5` its space (0, or the
  expanded id of a derived element), `6` its **start** and `7` its length.
  The start is coded: for a row with a length, it is the distance from the
  **end** (start + length) of the last earlier stored row with a length (0
  before the first), usually 0, since leaves are mostly in order; for a row
  without a length, it is the start itself.
- **`tables`**, shape `[k, 7]`, dimension names `index` and `part`, chunk
  shape `[min(k, 18724), 7]` (at least 1; no chunk when `k` is 0): each row
  is a table code and up to six values (unused ones 0):
  - `0`, a run: (root, count, stride), `count` at least 1;
  - `1`, an alias: (alias, target), `-1`: no target;
  - `2`, a form: (element, form), into `forms`;
  - `3`, a **column run**: (first row, count, size), 8.3;
  - `4`, a column: (column run, row offset, field, encoding, x, y), 8.3;
  - `5`, six values of `column_values`: the rows of code 5, in order,
    concatenated, are the list `column_values` (padded with zeros to a
    multiple of six).

  Ids in the tables are expanded ids, and the alias, form, run and column
  run rows name stored rows. A row of another code makes the table invalid.

When the IR has shared data sources (bytes several recipes name, 8.5),
`vzip_source/ir/shared` is a group of two arrays holding them, in order:
`offsets`, `int64` `[s + 1]` (where each starts in `data`, then the total),
and `data`, `uint8`, the sources one after another (chunks of at most
131072 and 1048576 values, encoded as the table's). **The rows are
in depth-first order**: every element's subtree is contiguous, its id after
its parent's and before its next sibling's; ids are the order of the
expanded table (the canonical order of 8.8). Derived content is not needed to
rebuild the source: the table may leave out the elements of derived spaces
(the derived elements themselves stay), as 8.8 says.

### 8.3 Column runs

Consecutive siblings of one shape fold into a **column run**: `count`
members of `size` rows each, a member being one to four consecutive sibling
subtrees (so that, for example, a directory's alternating entries and
dimensions fold together). Member 0 is stored as ordinary rows starting at
its **first row**; members 1 to `count − 1` are not stored: their `size`
rows follow member 0 in the expanded order, member `j` at ids
`first + j × size` to `first + (j + 1) × size − 1`. Member `j`'s rows are
member 0's, except:

- **parent**: a row whose parent in member 0 is the first row's parent (a
  sibling at the member's top) has that parent; any other row's parent is
  shifted by `j × size`;
- **fields with a column** (a table row of code 4 for this column run and
  row offset `o`, the row at `first + o` of member 0) take the column's
  value for member `j`. Fields: `0` name index, `1` type, `2` start
  (absolute), `3` length, `4` form (`-1`: none), `5` alias target (`-1`:
  none), `6` start relative to the member's first row (the member's first
  row's start plus the value). Encodings, for member `j`:
  - `0`: `x + j × y` (wrapping 64-bit arithmetic);
  - `1`: `column_values[x] + … + column_values[x + j]` (the column stored as
    differences from the previous member's value; `column_values[x]` is
    member 0's);
  - `2`: entry `y + j` of the array of unsigned integers that row `x`
    holds: row `x` MUST be a value (kind 1) in space 0, with a length, of a
    type that is a one-dimensional array of unsigned integers (`<u2[n]`,
    `>u4[n]`, …, 8.6), not in a column run that has a column of encoding 2;
    its bytes are those of source 0 at its extent, and the column is entries
    `y` to `y + count − 1` (so a TIFF's tiles take their offsets and byte
    counts from its TileOffsets and TileByteCounts, which the source holds);
  - `3` (name index only): the row's start.
- **start, without a column**: a row with a length, in a member whose
  first row has a length, is shifted as its member's first row is (its start
  plus member `j`'s first-row start minus member 0's); any other row keeps
  member 0's.

Forms and alias targets without a column are member 0's. A reader expands
the column runs that have no column of encoding 2 first, then the others.

**Limits.** The expanded table has at most `2^22 + floor(Z / 4)` rows; a
table that would have more is invalid. A producer **rejects** a source whose
canonical mirror (8.8: runs expanded, derived spaces left out) would have more
than `N = 2^22 + floor(Z / 4)` rows, with the message
`budget: the mirror would have more than N rows` (N in decimal), and may do so
before expanding when the elements outside derived spaces, every run expanded,
are already more than `N`. (No source the TIFF, ND2 and CZI parsers accept
comes near it today: they describe fewer than one element per four bytes of
the source; the limit bounds what a reader must expand.) So is one whose columns' lengths
differ, whose column refers to values past `column_values` or entries past
its array, whose column run's member 0 is not `size` rows of sibling
subtrees (every row's parent is the first row's parent or an earlier row of
the member), whose up is 0 for a row other than the root or larger than the
row's id, or whose kinds, names, types or forms are out of range. So is one
whose expanded rows break the rules of the root or of the names (8.1): a
root that is not a struct named `""` with no name index in space 0 with the
extent `(0, Z)`; an element other than the root with an empty full name; or
siblings whose full names are equal, or one of which is another's followed
by `/` and more. A reader rejects such a table (8.4) and a validator flags it
as invalid.

### 8.4 Reading the source back

A reader that rebuilds the source:

1. reads `rows` and `tables`, and expands the column runs (8.3);
2. checks the invariants of 8.1, with the runs expanded member by member
   (a run whose members' leaves overlap one another is invalid), the root
   and the names among them;
3. concatenates, for the leaves in order of their start, the bytes of
   source 0 at their extents.

The result is the source, byte for byte: its length is `Z`, which source 0
pins (VIRTUALIZE.md §1.2), and a range checksum, when the archive carries
one, checks each range read. The extents index source 0 directly; there is
no array of the source's bytes.

### 8.5 Forms and recipes

A form is a JSON object: for a data element, `geometry` (the format's
description of the unit, such as its shape and data type), `codec` and
`recipe`; for a derived element, the transform and, when known, `size`.
A **recipe** is the bytes of the unit as a list of parts: `["src", o, n]`,
the `n` bytes (`null`: up to the element's end) at offset `o` from the
element's start; and `["shared", k]`, shared data source `k` (8.2). The
hierarchy's chunks are references built from recipes; the rebuild does not
need forms.

### 8.6 Types

A value's type is a string of this grammar, and its value is its bytes
decoded by it:

```
type  = base ["[" n {"," n} "]"]
base  = ["<" | ">"] num | "ascii" | "cstr" | "bytes" | "utf16" | "guid" | "{" field {"," field} "}"
num   = "u1" | "i1" | "u2" | "i2" | "u4" | "i4" | "u8" | "i8" | "f4" | "f8"
field = name ":" type
```

`<` and `>` are little- and big-endian (default little); `[n, m]` makes an
array (row-major) of a number or record type, and gives the length of
`ascii`, `cstr`, `bytes` and `utf16`. `ascii[n]` is text (UTF-8 when valid,
else its bytes); `cstr[n]` is the text up to the first NUL, or, when bytes
after it are not all NUL, the record `{text, after}`; `bytes[n]` and
`utf16[n]` are bytes; `guid` is 16 bytes, written
`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` from a little-endian u32, two u16
and eight bytes; a record is its fields in order. The types `xml` and
`xml:<runtype>` are the UTF-8 text of an XML attribute value (in a derived
space).

### 8.7 The view

`vzip_source/tree` and the groups under it show the canonical IR (8.8) as
JSON. Each is a group whose source metadata `S` (§2) is a struct's **view
document**, so it declares the convention when the document has a member. The
document of the root, the element whose path is `""` (8.1), is `tree`'s.
Sizes are those of the JSON text
ECMAScript's `JSON.stringify` writes (no whitespace), in UTF-8 bytes.

A document has a member per child, in canonical order, by the child's full
name, where a name that starts with `$` gets another `$` in front (so the
members `$vz`, `$form` and `$partial` below are never a child's):

- a **value** is **shown** when its type is a number type, a record type,
  `guid`, `xml`, or a **text type** (`ascii[n]`, `cstr[n]` or `utf16[n]`;
  `bytes[n]` is not text), its length is at most 1024 bytes, and, for ND2,
  it is not a frame's `timestamp` (`frames/<f>/timestamp`, which an ND2
  parser need not read). A producer reads every shown value (its parser
  holds those it reads; the others are read after it). A shown value is, as
  JSON:
  - of a text type, its §6 text value `T`: for `ascii` and `cstr`, its
    bytes up to the first NUL (all of them if there is none), the JSON
    string they encode when they are UTF-8, else `{"latin1": L}` with each
    byte `b` the code point U+00`bb`; for `utf16`, its 16-bit little-endian
    units up to the first 0 unit, the string they encode when they are
    UTF-16, else `{"$vz": "utf16", "b64": B}` of those bytes. When the bytes
    from that NUL (or 0 unit) on are not all zero, the value is
    `{"text": T, "after": {"$vz": "bytes", "b64": A}}`, `A` the base64 (RFC
    4648 §4, with padding) of every byte from the NUL on, the NUL included:
    the form a record's `cstr` field takes, so that the view loses nothing;
  - of any other type, its decoded value (a record whose field `v` is its
    value shown as that field; a `v` of type `utf16[n]`, its last two bytes
    dropped, as its text);

  when that JSON is at most 4096 bytes; any other value is the spill form
  `{"$vz": "element", "id": i}`, row `i` of the expanded table, whose bytes
  the source holds;
- a **struct or a derived element**: its own document, inline when at most
  16384 bytes; else `{"$vz": "group", "path": p}`, the group
  `vzip_source/p` holding it, while there are fewer than 4096 such groups,
  the documents stored so far and this one total at most 2^24 bytes, and no
  group has the path `p` yet (paths are the escaped names, each segment
  percent-encoded outside `[A-Za-z0-9._~-]`, an empty one as `%`); else the
  spill form. A derived element's document starts with `"$vz": "derived"`
  and `"$form"`, its form (`null`: none). A document is complete before it
  is sized: the groups its descendants store come first;
- an **alias** whose target is neither data nor a gap:
  `{"$vz": "alias", "path": p}`, the target's path, which names it alone
  (8.1; `""` for the root);
- data, gaps and aliases of data or gaps are not shown.

Values JSON cannot hold are tagged: `{"$vz": "int", "v": digits}` past
2^53 − 1, `{"$vz": "float", "v": "NaN" | "Infinity" | "-Infinity"}`,
`{"$vz": "bytes", "b64": ...}` and `{"$vz": "utf16", "b64": ...}`.

**Budgets.** A document holds at most 65536 bytes, and at most 2^20
elements are visited in all. Each document starts with 65536 bytes left;
for each child in order: the child is visited (counted); if fewer than 1024
bytes are left, or more than 2^20 children have been visited, the document
gets `"$partial": true` and stops. Otherwise the child's member is computed
as above (data and gaps add none), and its size `n` is the member's value's
size plus its key's (as a JSON string) plus 2; when `n` is more than what is
left, the value becomes the spill form and `n` is recomputed; then `n` (or
all that is left) is spent. A key already in the document keeps its first
value. The view need not be complete; the table is. Groups that only lead to
a group are present, with no attributes.

### 8.8 Canonical form

A producer writes the **canonical** mirror: the one table, description and
view this section derives from the source's IR (its elements, and the
decoded bytes of the values the view shows), whatever order the producer
found the elements in and however it folded them. Two conforming producers
therefore write identical `ir/rows`, `ir/tables` and `ir/shared`, the same
description in `vzip_source`'s source metadata and the same view for one
source, and [VIRTUALIZE.md §1.1](../VIRTUALIZE.md#11-output-and-equivalence)
compares mirrors entry for entry. In order:

1. **Runs.** Every run (8.1) is expanded into its members: member `j`'s root
   has the run root's name index plus `j`, and each element of member `j`
   the extent of member 0's shifted by `j × stride` (the same length), and
   its type, value and form. The table has no row of code 0.
2. **Order.** The elements are numbered depth first from the root (id 0), a
   parent before its children, each subtree contiguous. An element's **first
   byte** is its start when it has a length, else the least first byte of its
   children (none when no element of its subtree has a length; none comes
   after every byte; siblings share a space, so their starts compare). The
   siblings of one name form a **name group**, whose first byte is the least
   of its members'. Siblings are ordered by
   1. their name group: the groups by first byte, then by name (its UTF-8
      bytes);
   2. then, within a group, by name index, as an unsigned integer, with no
      index last;
   3. then by first byte.

   So differently named siblings follow the source's layout, and a family
   (`tiles/0`, `tiles/1`, …) its indexes, wherever its members lie. No two
   siblings tie: siblings of one name differ in name index, since their full
   names differ (8.1), so this order is total and depends on the IR alone.
   Where two leaves claim one byte, the one first in this order keeps it and
   the other becomes an alias (8.1, injectivity).
3. **Derived spaces.** The derived elements are taken in order, skipping
   those inside another derived element. Each keeps its descendants when
   they, with the descendants kept so far, number at most 2^20; otherwise its
   descendants are left out (it stays, with its extent and form). The kept
   elements are numbered again in the same order.
4. **Strings.** `names` and `types` are the distinct names and types of the
   elements, sorted by their UTF-8 bytes (so `""` is first). The shared data
   sources are numbered by first use: for each element in order that has a
   form, each part `["shared",k]` of its recipe in order (as the form's text
   writes it, without spaces) gives source `k` the next number when it has
   none yet. Each form's text is the producer's with every such `k` replaced
   by its number, nothing else changed; `forms` are the distinct texts,
   sorted by their UTF-8 bytes, and `ir/shared` holds the sources by number.
5. **Column runs.** The elements are taken in order; for each element that
   is in no member of a column run found so far, its children
   `c_0, …, c_{k−1}` (in order) are folded from `a = 0`:
   - a child is **eligible** when its subtree has no derived element and no
     element in a derived space; two subtrees have **one shape** when they
     have the same number of elements and, element by element in order, the
     same kind, the same name, both or neither a length, and (all but the
     first) the same parent relative to the subtree's first element;
   - for each **period** `p` from 1 to 4, while `c_a … c_{a+p−1}` exist and
     are eligible: `r` is the largest count such that for every member
     `q < r` and every `t < p`, `c_{a+q·p+t}` exists, is eligible and has
     `c_{a+t}`'s shape;
   - the period with the largest `r × p` among those with `r ≥ 3` wins (the
     smaller period on a tie): children `c_a … c_{a+r·p−1}` are a column run
     of `r` members of `p` siblings each, and `a` moves past them; with none,
     `a` moves to the next child.

   Members' subtrees are not searched for column runs of their own: a column
   run found folds whole subtrees, outermost first. Column runs are listed in
   order of their first row.
6. **Columns.** For each column run, for each row offset `o` of a member,
   the fields are, in this order: name index (`0`), type (`1`), start (`6`,
   relative to the member's first row, when `o > 0` and both row `o` and
   the member's first row have a length; else `2`), length (`3`), form
   (`4`) and alias target (`5`). Member `j`'s value `v_j` is the row's field
   (`-1` for no form or target; field `6` its start minus its member's
   first row's start, wrapping). When every `v_j` is `v_0`, there is no
   column; otherwise the column takes the first encoding that applies:
   - `3`, for a name index: every member's row has a length and `v_j` is
     its start;
   - `0`: `v_j = v_0 + j × (v_1 − v_0)` for every `j` (wrapping), with
     `x = v_0` and `y = v_1 − v_0`;
   - `2`, when one of the profile's **named array rules** (below) gives,
     for this column, a value row `x` and an index `y`, and entries `y` to
     `y + count − 1` of the array `x` holds are `v_0` to `v_{count−1}`;
   - `1`: `x` is the length of `column_values` so far, `y = 0`, and
     `v_0, v_1 − v_0, …, v_{count−1} − v_{count−2}` (wrapping) are appended
     to it.

   Columns are listed by column run, then row offset, then field, in that
   order.

   **Named array rules.** Encoding 2 reads a column from an array the source
   holds, which pays only where a format's layout keeps such arrays; each
   profile lists its rules, and a column takes encoding 2 only under one of
   them. A rule has a name and says, for a column run and a field: the
   conditions on the column run (at least 16 members, so that reading the
   array costs less than storing the column), the value row `x` (a value in
   space 0, with a length, of a one-dimensional unsigned integer array type,
   8.6) and the index `y`. The rules today:

   | profile | rule | applies to | `x` | `y` |
   |---|---|---|---|---|
   | TIFF | layout tables | the start or length of a column run of at least 16 members of one data row each, with a name index, in a struct `tiles` (or `strips`) with no index | the first child `value` (no index) of the first struct child `tags/` with index 324 for starts and 325 for lengths (273 and 279 for `strips`) of that struct's parent: TileOffsets and TileByteCounts (StripOffsets, StripByteCounts) | member 0's name index |

   ND2 and CZI have none. A format adds a rule by a revision of this
   section and its convention: a new row in this table, whose conditions
   depend only on the canonical IR (so that every producer finds the same
   columns), and whose array a reader can read from source 0.
7. **Rows and tables.** The stored rows are every row but those of members
   1 and later of column runs, in id order. `tables` holds, in this order:
   an alias row (code 1) for each stored alias, in id order (target `-1`
   when the target was left out, step 3); a form row (code 2) for each
   stored row with a form, in id order; the column runs (code 3) and the
   columns (code 4) as above; and `column_values` (code 5), padded with
   zeros to a multiple of six.
8. **Arrays.** The chunk shapes are those of 8.2, and the codecs `bytes`
   (little-endian) alone: no `zlib`. The archive's own entry compression
   (SPEC.md) does the work, and is not part of the output.
9. **Description and view.** `S` is `{"ir": I}` of 8.2 with `version` 2,
   and the view is 8.7's, from the elements in this order.

**Readers accept, validators flag.** A reader MUST NOT reject a valid table
(8.2 to 8.4) for not being canonical, and a validator SHOULD flag one that
is not. Canonical form serves producers: it makes outputs comparable entry
for entry and the archive of a source reproducible. It adds nothing a reader
needs: the rebuild, the elements and the view are read the same from any
valid table. Checking it means folding the whole table again and, for the
columns of encoding 2, reading arrays from the source, a cost a reader that
wants one value or a rebuild should not pay; and a reader that rejected a
valid but non-canonical table would turn a producer's defect into a reader's
loss of the data. So the check belongs where producers are checked:
`conformance/virtualize/compare.py` loads each mirror, folds it again and
compares the result with what is stored, flagging the first difference (a
run row, a compressed chunk, unsorted strings, rows out of order, a fold or
an encoding other than the one this section gives, a different entry). A
canonical table is a fixed point: loaded and folded again, it is the same,
byte for byte. The validator also rebuilds the view (8.7) from the table and
the source: each shown value's bytes from source 0 at its extent, or, in a
derived space, from that space's bytes, which it derives again by the
derived element's transform (`nd2-lv-zlib`: the record inflated;
`xml-variant`: the chunk; an `xml` value is then its span with its
references decoded); it compares the rebuilt view with the stored one group
by group and member by member, and flags the first group or member missing,
extra or different. So **a derived transform may hold shown values only if
validators can derive it again** from source 0: of the transforms the
parsers emit, `nd2-lv-zlib` and `xml-variant` can, and `gzip` (a CZI's
compressed attachment, whose space holds nothing) may hold none. A new
transform that would hold shown values comes with its re-derivation, in a
revision of this section.
