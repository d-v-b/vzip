# Zarr v2

The Zarr v3 layout of a Zarr v2 hierarchy, and its attributes. What all of
vzip's conventions share is in [spec/conventions.md](../conventions.md), cited
here as "conventions §n". How vzip produces this layout as a virtual store
from a listed store is the Zarr v2 profile,
[Part 2](#part-2-the-profile) below. The
[OME-Zarr convention](ome-zarr.md) builds on this one.

Convention version: 0 (until release, conventions §1) · UUID: `8e792619-d671-4687-ab51-752885dd3ee6` ·
Schema: [schema.json](zarr2/schema.json)

This document has two parts. [Part 1](#part-1-the-convention) is the Zarr v2
convention: the Zarr layout, cited as "the convention §n". [Part 2](#part-2-the-profile)
is the Zarr v2 profile: how vzip reads the source, which inputs it rejects, and how
each chunk references the source. The profile keeps its numbering as a section
of [spec/virtualize.md](../virtualize.md), and is cited as spec/virtualize.md §n.

# Part 1. The convention

This convention gives a layout only to the hierarchies that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a hierarchy that fails has no layout under this
convention, and the profile rejects it.

A Zarr v2 hierarchy (the Zarr storage specification, version 2) is a tree of
groups and arrays: a group has a `.zgroup` document, an array a `.zarray`
document, and either may have a `.zattrs` document of user attributes. An
array's chunks are objects at keys `<array>/<chunk key>`. The output is a
Zarr v3 hierarchy with a node at the path of every Zarr v2 node; each
array's chunks are its chunk objects, referenced whole, under the same keys
(the Zarr v3 `v2` chunk key encoding produces exactly the Zarr v2 keys).
Each node's attributes, and the members of its documents that the Zarr v3
metadata does not reproduce, are its source metadata (§4). Every other
object of the store, but consolidated metadata, is kept whole (§5).


## 1. Declaration

The root declares the convention by [conventions §2](../conventions.md#2-attributes),
with `"profile": "zarr2"`, `"version": 0`, `"revision": 24` (conventions §1), the store's URL (ending in `/`)
as `source.url`, and the root's source metadata (§4), if it has any, as
the member `"zarr2"`. Every other node that has source metadata declares it
with `{"zarr2": S}`. Its CMO is:

```json
{
  "uuid": "8e792619-d671-4687-ab51-752885dd3ee6",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/heads/main/spec/virtualize/zarr2/schema.json",
  "spec_url": "https://github.com/d-v-b/vzip/blob/main/spec/virtualize/zarr2.md",
  "name": "vzip_virtualized",
  "description": "The Zarr layout of a Zarr v2 source virtualized by vzip, and the source's metadata"
}
```

## 2. Nodes

The **candidates** are the directories `D` (the root included) for which
the store has an object `D/.zarray` or `D/.zgroup`. A candidate with both
rejects the input. Candidates are classified from the root down (in order
of their number of segments):

- a candidate inside an array (a proper descendant of a directory that is
  an array) is **not a node**, and none of its documents is read or
  checked;
- otherwise, a candidate with `.zarray` is an **array**, and one with
  `.zgroup` an **explicit group**.

Every directory that is a proper ancestor of an array or an explicit
group, and is not one itself, is an **implicit group**. No other directory
is a node.

A node's documents are read as JSON ([spec/virtualize.md §1.6](../virtualize.md#16-json-documents)):

- `.zgroup` MUST be an object whose member `zarr_format` is the integer 2.
  Its other members are kept in the group's source metadata (§4).
- `.zarray`: §3.
- `.zattrs`, if the node has one, MUST be an object: the node's
  **attributes**, `{}` if it has none. A `.zattrs` of a directory that is
  not an array or an explicit group is not read: it is one of §5's other
  objects.

**Consolidated metadata.** A `.zmetadata` object (consolidated metadata) is
never read: nodes come from the listing and documents from their own
objects, so stale consolidated metadata cannot change the output. (Chunks
must come from the listing anyway, since `.zmetadata` does not list them.)
It is kept as an other object (§5), so that consolidated metadata that
differs from the documents is not lost.

The hierarchy has one `zarr.json` per node, at `<path>/zarr.json`
(`zarr.json` for the root):

- a group, implicit or explicit:
  `{"zarr_format": 3, "node_type": "group", "attributes": A}`, where `A`
  is the declaration of §1, or `{}` for a node without source metadata;
- an array: §3.

## 3. Arrays

An array's `.zarray` MUST be an object with these members, or the input is
rejected:

- `zarr_format`: the integer 2;
- `shape`: an array of `n` integers ([spec/virtualize.md §1.6](../virtualize.md#16-json-documents)), `0 ≤ n ≤ 32`, each from 0 to
  2^53 − 1;
- `chunks`: an array of `n` integers, each from 1 to 2^53 − 1;
- `dtype`: a string in the table below;
- `order`: `"C"` or `"F"`;
- `compressor`: `null` or absent (no compression), or an object whose
  member `id` is a string (§3.1);
- `filters`: `null`, absent, or an empty array. A nonempty array rejects the
  input (no filter is supported);
- `fill_value`: see below; absent is the same as `null`;
- `dimension_separator`: `"."`, `"/"`, `null` or absent (`"."`).

Other members are not read here; they are kept in the array's source
metadata (§4).

**Data types.** `dtype` is a NumPy type string: a byte order character
(`<` little-endian, `>` big-endian, `|` not applicable), a kind and a size
in bytes.

| `dtype` | Zarr `data_type` | `b` |
|---|---|---|
| `|b1`, `<b1`, `>b1` | `bool` | 1 |
| `|i1`, `<i1`, `>i1`; `|u1`, `<u1`, `>u1` | `int8`; `uint8` | 1 |
| `<i2`, `>i2`; `<u2`, `>u2`; `<f2`, `>f2` | `int16`; `uint16`; `float16` | 2 |
| `<i4`, `>i4`; `<u4`, `>u4`; `<f4`, `>f4` | `int32`; `uint32`; `float32` | 4 |
| `<i8`, `>i8`; `<u8`, `>u8`; `<f8`, `>f8` | `int64`; `uint64`; `float64` | 8 |

Any other `dtype` rejects the input: among them strings (`S`, `U`),
objects (`O`), void and structured types (`V`, or a list), date-times
(`M8`, `m8`), complex numbers (`c8`, `c16`), and a multi-byte type with `|`.

**Fill value.** The Zarr v3 `fill_value` `F`:

- `null`: `F` is `false` for `bool` and 0 for every other type. (Zarr v2's
  null means the value of a missing chunk is undefined; Zarr v3 needs a
  value, and 0 is what Zarr v2 readers return in practice.) The null itself
  is kept in the source metadata (§4).
- `bool`: `true` or `false`, which `F` is. Any other value rejects.
- integer types: an integer ([spec/virtualize.md §1.6](../virtualize.md#16-json-documents)) within the type's range; `F` is that
  integer. Any other value rejects, including an integer above 2^53 − 1
  (such as the largest `uint64`).
- float types: a number whose magnitude is at most the type's largest
  finite value (65504 for `float16`, 3.4028234663852886e38 for `float32`),
  compared as binary64 ([spec/virtualize.md §1.6](../virtualize.md#16-json-documents)),
  which `F` is, as a binary64 value; or one of the strings `"NaN"`,
  `"Infinity"` and `"-Infinity"`, which `F` is (Zarr v3 writes these
  special values the same way). Any other value rejects. A number that is
  a negative zero (written with a fraction or an exponent, such as `-0.0`)
  is the one value whose JSON number some JSON writers do not keep (they
  write `0`): `F` is then the Zarr v3 hex fill of its bits, `"0x8000"` for
  `float16`, `"0x80000000"` for `float32` and `"0x8000000000000000"` for
  `float64`. The integer literal `-0` is the integer 0, and gives 0. (Zarr
  v2 writes NaN as the string `"NaN"`, without a payload, so no other float
  fill value depends on bits that JSON cannot write.)

**Codecs.** In this order:

1. `{"name": "transpose", "configuration": {"order": [n−1, …, 1, 0]}}` when
   `order` is `"F"` and `n ≥ 2` (a Fortran-order chunk stores the first
   index fastest);
2. `{"name": "bytes"}` when `b = 1`, else
   `{"name": "bytes", "configuration": {"endian": e}}`, with `e` `"little"`
   for `<` and `"big"` for `>`;
3. the compressor's codec (§3.1), if there is one.

**The array.** The array's `zarr.json` is the object

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": shape,
  "data_type": "...",
  "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": chunks}},
  "chunk_key_encoding": {"name": "v2", "configuration": {"separator": s}},
  "fill_value": F,
  "codecs": [...],
  "attributes": A
}
```

with no other members, where `s` is the dimension separator and `A` the
declaration of §1, with the array's source metadata (§4), or `{}` if it
has none. (Zarr v2 has no dimension names; the
xarray convention `_ARRAY_DIMENSIONS` stays an attribute.)

### 3.1 Compressors

The compressor object's `id` selects the codec:

| `id` | codec | members the codec carries |
|---|---|---|
| `zlib` | `{"name": "zlib", "configuration": {"level": L}}` | `id`, `level` |
| `gzip` | `{"name": "gzip", "configuration": {"level": L}}` | `id`, `level` |
| `zstd` | `{"name": "zstd", "configuration": {"level": L, "checksum": x}}` | `id`, `level`, `checksum` |
| `blosc` | `{"name": "blosc", "configuration": {"cname": c, "clevel": l, "shuffle": h, "typesize": b, "blocksize": k}}` | `id`, `cname`, `clevel`, `shuffle`, `blocksize` |
| anything else, among them `lz4`, `bz2`, `lzma` and `delta` | rejected | |

- `L` is the member `level`. For `zlib` and `gzip` it MUST be an integer
  from −1 to 9, and `L` is that integer, except that −1 (zlib's default
  level) is 6, the level it stands for (the Zarr v3 `gzip` and `zlib`
  codecs take 0 to 9); when the member is absent, `L` is 1, numcodecs'
  default. For `zstd` it MUST be an integer from −131072 to 22 (zstd's
  levels, which the Zarr v3 `zstd` codec takes), and `L` is that integer;
  when it is absent, `L` is 0, numcodecs' default. Any other value rejects
  the input. (A level only matters when encoding; the codec carries it so
  that the compressor's configuration can be recovered.)
- `x` is the `zstd` member `checksum`, which MUST be a boolean; `false`
  when it is absent.
- The compressor's members that its codec does not carry (numcodecs writes
  none, but another writer may) are kept in the source metadata (§4).
- `blosc` (numcodecs' Blosc): `c` is the member `cname`, one of `blosclz`,
  `lz4`, `lz4hc`, `snappy`, `zlib` and `zstd`; `l` the member `clevel`, an
  integer from 0 to 9; `k` the member `blocksize`, an integer from 0 to
  2^31 − 1, or 0 when absent; `b` the data type's size; and `h` comes from
  the member `shuffle`, an integer: 0 `"noshuffle"`, 1 `"shuffle"`, 2
  `"bitshuffle"`, and −1 (numcodecs' automatic shuffle) `"bitshuffle"` when
  `b = 1` and `"shuffle"` otherwise. A missing `cname`, `clevel` or
  `shuffle`, or any value outside these, rejects the input. (A blosc chunk
  describes itself, so this configuration does not change how chunks decode;
  it is exactly the compressor's.)

### 3.2 Chunks

**Chunks.** The chunk keys of an array at path `D` are `D/` (nothing at
the root) followed by: `0` when `n = 0`; otherwise `i0`, `i1`, …, `i(n−1)`
joined by `s`, where each `ik` is the decimal form of an integer with `0 ≤ ik
< ceil(shape[k] / chunks[k])`, without leading zeros. An array with a zero in
`shape` has no chunk keys. The **chunk objects** of the array are the
store's objects with these keys, whatever their size. A chunk is present
when its chunk object is not empty: its bytes are the whole object, the
chunk as Zarr v2 stored it. A chunk without an object reads as the fill
value, as in Zarr v2. An empty chunk object is one of §5's empty objects:
its chunk is absent too, and reads as the fill value, and its key is kept
there. (Zarr v2 readers fail on an empty chunk object, which an interrupted
write can leave behind, rather than read the fill value; the key records
that the object was there.) Every other object under `D` is one of §5's
other objects.


## 4. Source metadata

A node's source metadata `S` ([conventions §2](../conventions.md#2-attributes)) is
the object

```json
{"attributes": A, "metadata": M}
```

where each member is present only when its value is not empty, and
the node has source metadata only when `S` has a member. An implicit group
has none.

- `A` is the node's attributes (its `.zattrs` document), whole and
  unchanged: user attributes, xarray's `_ARRAY_DIMENSIONS`, and the
  metadata of other conventions (an OME-NGFF version other than 0.4, for
  example, which the [OME-Zarr convention](ome-zarr.md) does not
  migrate). Every member is under the key, so none can collide with the
  conventions of the Zarr v3 hierarchy; a document that has its own
  `vzip_virtualized` or `zarr_conventions` nests them there.
- `M` holds the members of the node's `.zgroup` or `.zarray` that its
  `zarr.json` does not reproduce:
  - for an explicit group, every member of `.zgroup` but `zarr_format`;
  - for an array, every member of `.zarray` that §3 does not read (any
    member but `zarr_format`, `shape`, `chunks`, `dtype`, `order`,
    `compressor`, `filters`, `fill_value` and `dimension_separator`), as
    written; the member `"fill_value": null` when `fill_value` is `null`
    or absent (§3 gives `F` = 0 or `false`, which is not Zarr v2's
    "undefined"); the member `fill_value`, as written, when the array's
    type is a float type and `fill_value` is an integer literal (no
    fraction, no exponent) beyond ±(2^53 − 1) (`F` is its binary64 value,
    which may differ from it, and is not written as an integer); and the member `"compressor"` when the compressor has
    members that its codec does not carry (§3.1), whose value is the object
    of those members, and blosc's `shuffle` when it is −1 (which §3.1
    resolves to a shuffle).

What `S` leaves out, the Zarr v3 metadata holds: `zarr_format`, `shape`,
`chunks`, the data type, `order`, the fill value, the compressor's `id`
and the members its codec carries, the filters and the separator. What
neither keeps is layout: the byte order character of a one-byte type,
`order` for an array of fewer than two dimensions (whose chunks C and F
order lay out alike), whether a default level was written (or was −1
rather than 6), whether no filters were `null`, `[]` or absent, whether the
compressor was `null` or absent, whether the separator `"."` was written or
was `null`, whether `fill_value` was `null` or absent, whether zstd's
`checksum` was `false` or absent, whether blosc's `blocksize` was 0 or
absent, and the documents' formatting (whitespace, member order, and how a
number that is not an integer is spelled). So a node's documents are
recovered from its `zarr.json`: `.zattrs` is `A`, and `.zgroup` or
`.zarray` is the document that §2 and §3 read back from the Zarr v3
metadata, with the members of `M` set over it (those of `M.compressor`
over the compressor object).

Members are copied as [spec/virtualize.md §1.6](../virtualize.md#16-json-documents)
reads them, except that a number written as an integer (a JSON number
with no fraction and no exponent) is kept exactly, every digit, beyond
2^53 − 1 too, and is written back so. Any other number is its binary64
value, and a number whose binary64 value is infinite still rejects the
input. Where §3 uses a number (a size, a fill value, a level), it uses its
binary64 value.

## 5. Other objects

An **other object** is an object of the store
([spec/virtualize.md §1.4](../virtualize.md#14-store-inputs)) whose size is not
0 and which is none of these:

- a document that §2 reads: the `.zgroup` of an explicit group, the
  `.zarray` of an array, and the `.zattrs` of either;
- a chunk object of an array (§3.2), whatever its size.

Among them are READMEs and other files a writer put next to the data,
OME-XML outside the place that [the OME-Zarr convention
§7](ome-zarr.md#7-chunks-and-ome-xml) gives it, a `.zattrs` of a
directory that is not a node, documents nested under an array (the
`.zgroup` or `.zarray` of a candidate inside an array), and the objects
under an array that are not chunk keys. Each is kept whole: the hierarchy has the key `vzip_source/objects/<k>`, where `k` is the
object's key, escaped, and its bytes are the object's. (An empty object
holds nothing, and has no key; §5 lists its key under "Empty objects".)

**Escaping.** A last segment that a Zarr reader would take for a node's
document must not end a key under `vzip_source/objects/`: when the last
segment of `k` is `zarr.json`, `.zarray` or `.zgroup` followed by any
number (zero included) of `~`, one `~` is appended to it. So `zarr.json`
is kept at `vzip_source/objects/zarr.json~`, `a/.zgroup` at
`vzip_source/objects/a/.zgroup~`, and `zarr.json~` at
`vzip_source/objects/zarr.json~~`. The escape is one to one: a key whose
last segment is one of these names followed by at least one `~` is the
object's key with one `~` removed, and every other key is the object's.
No Zarr v3 or Zarr v2 reader then opens a node under `vzip_source/objects/`.

**Empty objects.** The **empty objects** are the store's objects of size
0, the empty chunk objects (§3.2) among them. The **ignored keys** are the
relative keys of the listed objects that
[spec/virtualize.md §1.4](../virtualize.md#14-store-inputs) ignores and
records. Let `K` be the empty objects' keys and `I` the ignored keys, each
in ascending order of their UTF-8 bytes, and let `E` be the object
`{"empty": K, "ignored": I}`, each member present only when it is not
empty. `E` is kept when it is not empty: as the source metadata of the
group `vzip_source` below, or, when the root is an array, as the archive's
key `vzip_source/empty.json`, whose bytes are the compact JSON (no
whitespace) of `E`, in UTF-8.

When there is at least one other object, empty object or ignored key and
the root is a group, the hierarchy has the group `vzip_source`, a child of
the root (the source metadata node of
[conventions §2](../conventions.md#2-attributes)), whose `zarr.json` is
`{"zarr_format": 3, "node_type": "group", "attributes": A}`: `A` is `{}`
when `E` is empty, and otherwise declares the convention, as any other node
does (§1), with the source metadata `E`. (So the keys of many empty objects
stay off the root, whose metadata every reader opens.) The directories between it and the
objects are not nodes and have no `zarr.json`. A hierarchy that then has a
node (§2) at the path `vzip_source`, or below it, is rejected, since its
keys would mix with these. When the root is an array, which can have no
children, there is no `vzip_source` group: the objects' keys, and
`vzip_source/empty.json`, are plain keys of the archive, which Zarr
readers do not read as nodes.

## 6. Example

An array of the store `https://example.org/h.zarr/`, whose `.zarray` is

```json
{
  "zarr_format": 2,
  "shape": [4],
  "chunks": [4],
  "dtype": "<u2",
  "compressor": {"id": "blosc", "cname": "lz4", "clevel": 5, "shuffle": 1, "blocksize": 0, "nthreads": 2},
  "fill_value": 0,
  "order": "C",
  "filters": null,
  "custom": {"x": 1}
}
```

and whose `.zattrs` is `{"_ARRAY_DIMENSIONS": ["x"]}`:

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": [
    4
  ],
  "data_type": "uint16",
  "chunk_grid": {
    "name": "regular",
    "configuration": {
      "chunk_shape": [
        4
      ]
    }
  },
  "chunk_key_encoding": {
    "name": "v2",
    "configuration": {
      "separator": "."
    }
  },
  "fill_value": 0,
  "codecs": [
    {
      "name": "bytes",
      "configuration": {
        "endian": "little"
      }
    },
    {
      "name": "blosc",
      "configuration": {
        "cname": "lz4",
        "clevel": 5,
        "shuffle": "shuffle",
        "typesize": 2,
        "blocksize": 0
      }
    }
  ],
  "attributes": {
    "zarr_conventions": [
      {
        "uuid": "8e792619-d671-4687-ab51-752885dd3ee6",
        "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/heads/main/spec/virtualize/zarr2/schema.json",
        "spec_url": "https://github.com/d-v-b/vzip/blob/main/spec/virtualize/zarr2.md",
        "name": "vzip_virtualized",
        "description": "The Zarr layout of a Zarr v2 source virtualized by vzip, and the source's metadata"
      }
    ],
    "vzip_virtualized": {
      "zarr2": {
        "attributes": {
          "_ARRAY_DIMENSIONS": [
            "x"
          ]
        },
        "metadata": {
          "custom": {
            "x": 1
          },
          "compressor": {
            "nthreads": 2
          }
        }
      }
    }
  }
}
```

# Part 2. The profile

The Zarr v2 profile of [spec/virtualize.md](../virtualize.md) (revision 16),
numbered as its §10. §1 is in spec/virtualize.md and applies here, in particular
the store input rules of §1.4–§1.6, and so does §2.

The output has the convention's layout for the input
([spec/virtualize.md §2](../virtualize.md#2-the-zarr-layout)); this profile says
how the store is read, which inputs are rejected, and how each chunk
references the store. "The convention §n" below is a section of Part 1.

## 10. Zarr v2 profile

The profile was chosen because the store's root has a `.zarray` or a
`.zgroup` and the store does not declare OME-NGFF 0.4 (§1.4); a store that
does is read by the OME-Zarr profile (§11, [ome-zarr.md](ome-zarr.md#part-2-the-profile)),
which builds on this one. The store is listed (§1.5), and the documents
that [the convention §2](#2-nodes) reads are
read as JSON (§1.6).

**Consolidated metadata.** A `.zmetadata` object (consolidated metadata) is
never read: nodes come from the listing and documents from their own
objects, so stale consolidated metadata cannot change the output. (Chunks
must come from the listing anyway, since `.zmetadata` does not list them.)

**Numbers.** The documents' integer literals are kept exactly where the
convention copies them ([its §4](#4-source-metadata)),
beyond 2^53 − 1 too, in place of §1.6's last paragraph; every number the
convention uses is its binary64 value, as §1.6 says.

### 10.1 Rejection

The input is rejected when a rule of §1.4–§1.6 fails, and when the
convention gives it no layout: wherever it says that the input is
rejected, or that something MUST hold and it does not.

### 10.2 Chunks and other objects

Every chunk that the convention says is present
([the convention §3.2](#32-chunks)) is an
entry under its own key, referencing the whole chunk object through its own
`url` source (§1.4). A chunk object of size 0 has no entry: its key is
one of the convention §5's empty objects' keys.

Every other object of [the convention §5](#5-other-objects)
is referenced whole in the same way, by an entry under the key
`vzip_source/objects/<k>` (with a `~` appended when the convention §5
escapes `k`'s last segment) whose `url` source is the URL of the object `k`
(§1.4). (This is the one place where an entry's key is not its object's
key; §1.4's rules otherwise apply: the sources are in ascending order of
their entries' keys, each entry referencing its object's whole range.)
Under a root array, the empty objects' keys and the ignored keys are the
document `vzip_source/empty.json` (the convention §5), written as UTF-8
compact JSON, as `JSON.stringify` writes it. The documents under
`vzip_source/` (which hold these keys) are deflated and, like the chunks, not
among the documents a reader fetches when it opens the archive
([conventions §2](../conventions.md#2-attributes)).

### 10.3 Summary

This section is informative. Both implementations print a one-line JSON
summary: `groups` (explicit and implicit), `arrays`, `chunks` (chunk
entries), `emptyChunks` (chunk objects of size 0), `objects` (the store's
objects), `otherObjects` (the entries under `vzip_source/objects/`) and
`listingRequests`.
