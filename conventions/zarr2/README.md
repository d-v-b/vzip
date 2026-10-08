# The Zarr v2 convention

The Zarr v3 layout of a Zarr v2 hierarchy, and its attributes. What all of
vzip's conventions share is in [conventions/README.md](../README.md), cited
here as "conventions §n". How vzip produces this layout as a virtual store
from a listed store is the Zarr v2 profile,
[profiles/zarr2.md](../../profiles/zarr2.md). The
[OME-Zarr convention](../ome-zarr/README.md) builds on this one.

Convention version: 1 · UUID: `8e792619-d671-4687-ab51-752885dd3ee6` ·
Schema: [schema.json](schema.json)

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
Attributes are copied unchanged, as source metadata (§4).


## 1. Declaration

The root declares the convention by [conventions §2](../README.md#2-attributes),
with `"profile": "zarr2"`, `"version": 1`, the store's URL (ending in `/`)
as `source.url`, and the root's source metadata (§4), if it has any, as
the member `"zarr2"`. Every other node that has source metadata declares it
with `{"zarr2": S}`. Its CMO is:

```json
{
  "uuid": "8e792619-d671-4687-ab51-752885dd3ee6",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/tags/virtualize-zarr2-v1/conventions/zarr2/schema.json",
  "spec_url": "https://github.com/d-v-b/vzip/blob/virtualize-zarr2-v1/conventions/zarr2/README.md",
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

A node's documents are read as JSON ([VIRTUALIZE.md §1.6](../../VIRTUALIZE.md#16-json-documents)):

- `.zgroup` MUST be an object whose member `zarr_format` is the integer 2.
  Its other members are not read.
- `.zarray`: §3.
- `.zattrs`, if the node has one, MUST be an object: the node's
  **attributes**, `{}` if it has none. A `.zattrs` of a directory that is
  not an array or an explicit group is not read.

**Consolidated metadata.** A `.zmetadata` object (consolidated metadata) is
never read: nodes come from the listing and documents from their own
objects, so stale consolidated metadata cannot change the output. (Chunks
must come from the listing anyway, since `.zmetadata` does not list them.)

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
- `shape`: an array of `n` integers ([VIRTUALIZE.md §1.6](../../VIRTUALIZE.md#16-json-documents)), `0 ≤ n ≤ 32`, each from 0 to
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

Other members are not read.

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
  value, and 0 is what Zarr v2 readers return in practice.)
- `bool`: `true` or `false`, which `F` is. Any other value rejects.
- integer types: an integer ([VIRTUALIZE.md §1.6](../../VIRTUALIZE.md#16-json-documents)) within the type's range; `F` is that
  integer. Any other value rejects, including an integer above 2^53 − 1
  (such as the largest `uint64`).
- float types: a number whose magnitude is at most the type's largest
  finite value (65504 for `float16`, 3.4028234663852886e38 for `float32`),
  which `F` is; or one of the strings `"NaN"`, `"Infinity"` and
  `"-Infinity"`, which `F` is (Zarr v3 writes these special values the same
  way). Any other value rejects.

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
declaration of §1, with the array's attributes as its source metadata (§4),
or `{}` if it has none. (Zarr v2 has no dimension names; the
xarray convention `_ARRAY_DIMENSIONS` stays an attribute.)

### 3.1 Compressors

The compressor object's `id` selects the codec:

| `id` | codec |
|---|---|
| `zlib` | `{"name": "zlib", "configuration": {"level": 1}}` |
| `gzip` | `{"name": "gzip", "configuration": {"level": 1}}` |
| `zstd` | `{"name": "zstd", "configuration": {"level": 0, "checksum": false}}` |
| `blosc` | `{"name": "blosc", "configuration": {"cname": c, "clevel": l, "shuffle": h, "typesize": b, "blocksize": k}}` |
| anything else, among them `lz4`, `bz2`, `lzma` and `delta` | rejected |

- The levels of `zlib`, `gzip` and `zstd` only matter when encoding; they
  are not read from the document, and the codecs carry the fixed levels
  above. No other member of these compressors is read.
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
`shape` has no chunk keys. A chunk is present when the store has an object with its key (and the
object is not empty): its bytes are the whole object, the chunk as Zarr v2
stored it. Every other object under `D` is not part of the hierarchy. A chunk without an object reads as the fill value, as in Zarr v2.


## 4. Source metadata

A node's source metadata `S` ([conventions §2](../README.md#2-attributes)) is
its attributes (its `.zattrs` document), whole and unchanged: user
attributes, xarray's `_ARRAY_DIMENSIONS`, and the metadata of other
conventions (an OME-NGFF version other than 0.4, for example, which the
[OME-Zarr convention](../ome-zarr/README.md) does not migrate). A node
without `.zattrs`, or with an empty one, has none. Every member is under
the key, so none can collide with the conventions of the Zarr v3
hierarchy; a document that has its own `vzip_virtualized` or
`zarr_conventions` nests them there.

The `.zarray` and `.zgroup` documents are not in `S`: §3 carries every
member of `.zarray` that defines the array's values, and the compressors'
levels, which only matter when encoding, are the only members it drops.

## 5. Example

An array of the store `https://example.org/h.zarr/`:

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": [
    4,
    4
  ],
  "data_type": "uint16",
  "chunk_grid": {
    "name": "regular",
    "configuration": {
      "chunk_shape": [
        2,
        4
      ]
    }
  },
  "chunk_key_encoding": {
    "name": "v2",
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
      {
        "uuid": "8e792619-d671-4687-ab51-752885dd3ee6",
        "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/tags/virtualize-zarr2-v1/conventions/zarr2/schema.json",
        "spec_url": "https://github.com/d-v-b/vzip/blob/virtualize-zarr2-v1/conventions/zarr2/README.md",
        "name": "vzip_virtualized",
        "description": "The Zarr layout of a Zarr v2 source virtualized by vzip, and the source's metadata"
      }
    ],
    "vzip_virtualized": {
      "zarr2": {
        "_ARRAY_DIMENSIONS": [
          "y",
          "x"
        ]
      }
    }
  }
}
```
