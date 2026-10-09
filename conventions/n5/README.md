# The N5 convention

The Zarr v3 layout of an N5 container, and its attributes. What all of
vzip's conventions share is in [conventions/README.md](../README.md), cited
here as "conventions §n". How vzip produces this layout as a virtual store
from a listed store is the N5 profile, [profiles/n5.md](../../profiles/n5.md).

Convention version: 0 (until release, README §1) · UUID: `ad5d4c39-c69e-48f7-a3ef-4cc8c607d416` ·
Schema: [schema.json](schema.json)

This convention gives a layout only to the containers that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a container that fails has no layout under this
convention, and the profile rejects it.

An N5 container (the N5 specification, version 4.0.0 and the 2.x versions
before it) is a hierarchy of groups and datasets. Each may have an
`attributes.json` document; a dataset's holds its shape, block size, data
type and compression. A dataset's blocks are objects at keys
`<dataset>/<i0>/<i1>/…`, each a header (mode, number of dimensions and the
block's size along each) followed by the elements in column-major (first
index fastest) order, big-endian, compressed as a whole.

The output is a Zarr v3 hierarchy with a node at the path of every N5 node.
Each N5 dataset becomes an array that reads the blocks with the
**`n5_default`** codec of the zarr-extensions registry
(`codecs/n5_default`), and each chunk holds a whole block object, header
included: the codec parses the header, so a block that is smaller than the
block size (the truncated edge blocks Java N5 writes) or larger (padded
ones) reads correctly. Every object of the container that is neither a
node's document nor a block is kept whole (§6).

## 1. Declaration

The root declares the convention by [conventions §2](../README.md#2-attributes),
with `"profile": "n5"`, `"version": 0`, `"revision": 23` (README §1), the store's URL (ending in `/`) as
`source.url`, and the root's source metadata (§5), if it has any, as the
member `"n5"`. Every other node that has source metadata (§5) declares it
with `{"n5": S}`. Its CMO is:

```json
{
  "uuid": "ad5d4c39-c69e-48f7-a3ef-4cc8c607d416",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/heads/main/conventions/n5/schema.json",
  "spec_url": "https://github.com/d-v-b/vzip/blob/main/conventions/n5/README.md",
  "name": "vzip_virtualized",
  "description": "The Zarr layout of a N5 source virtualized by vzip, and the source's metadata"
}
```

## 2. Nodes

The **candidates** are the directories `D` (the root included) for which
the store has an object `D/attributes.json` (`attributes.json` for the
root). Each candidate's document is read ([VIRTUALIZE.md §1.6](../../VIRTUALIZE.md#16-json-documents)) and MUST be a JSON object;
otherwise the input is rejected.

Candidates are classified from the root down (in order of their number of
segments):

- a candidate inside a dataset (a proper descendant of a directory that is
  a dataset) is **not a node**, and its document is neither read nor
  checked: it, and the blocks of such a nested dataset, are other objects
  (§6);
- otherwise, a candidate whose document has a member `dimensions` is a
  **dataset**;
- otherwise, it is an **explicit group**.

Every directory that is a proper ancestor of a dataset or an explicit
group, and is not one itself, is an **implicit group** (N5 has no document
for a group without attributes; any directory on a file system is a group,
and an object store has directories only as key prefixes). No other
directory is a node: a directory that leads to no `attributes.json` (for
example a directory of other files) is not a node, and its objects are
other objects (§6).

The hierarchy has one `zarr.json` per node, at `<path>/zarr.json`
(`zarr.json` for the root):

- an implicit group:
  `{"zarr_format": 3, "node_type": "group", "attributes": {}}` (the
  root's has the declaration of §1);
- an explicit group:
  `{"zarr_format": 3, "node_type": "group", "attributes": A}`, where `A`
  holds the member `ome` when §4 applies, and the declaration of §1;
- a dataset: §3.

## 3. Datasets

A dataset's document MUST have these members, or the input is rejected:

- `dimensions`: an array of `n` integers ([VIRTUALIZE.md §1.6](../../VIRTUALIZE.md#16-json-documents)), `1 ≤ n ≤ 32`, each from 0
  to 2^53 − 1: the size along each dimension;
- `blockSize`: an array of `n` integers, each from 1 to 2^31 − 1;
- `dataType`: one of the strings in the table below;
- the compression: the member `compression`, an object whose member `type`
  is a string; or, if there is no member `compression`, the member
  `compressionType`, a string `t`, which is read as the object
  `{"type": t}` (N5 before version 1.0.0).

| `dataType` | Zarr `data_type` | bytes per element `b` |
|---|---|---|
| `uint8`, `int8` | `uint8`, `int8` | 1 |
| `uint16`, `int16` | `uint16`, `int16` | 2 |
| `uint32`, `int32`, `float32` | `uint32`, `int32`, `float32` | 4 |
| `uint64`, `int64`, `float64` | `uint64`, `int64`, `float64` | 8 |

Any other `dataType` (such as `string` or `object`) rejects the input.

**Dimension order.** The array's axes are the N5 dimensions in the order
`dimensions` lists them: the shape is `dimensions` and the chunk shape is
`blockSize`, neither reversed. N5 stores a block's elements with the first
dimension fastest; the `transpose` codec inside `n5_default` (order
`[n−1, …, 0]`) turns that layout into the array's. This is the convention of
the `n5_default` README's example (`examples/zstd_array`, where
`dimensions [256, 128]` becomes `shape [256, 128]`) and of `zarr-n5`, and it
is what makes block keys and chunk keys agree: the block at `i0/i1/…/i(n−1)`
is the chunk whose index along dimension `k` is `ik`.

**Compression.** The compression object's `type` selects the bytes-to-bytes
codec `C`, or none:

| `type` | `C` | members `C` carries |
|---|---|---|
| `raw` | none | `type` |
| `gzip` | `{"name": "gzip", "configuration": {"level": L}}` if the member `useZlib` is absent or `false`; `{"name": "zlib", "configuration": {"level": L}}` if it is `true` | `type`, `useZlib`, `level` |
| `zstd` | `{"name": "zstd", "configuration": {"level": L, "checksum": false}}` | `type`, `level` |
| `blosc` | `{"name": "blosc", "configuration": {"cname": c, "clevel": l, "shuffle": s, "typesize": b, "blocksize": k}}`, see below | `type`, `cname`, `clevel`, `shuffle`, `blocksize` |
| anything else, among them `lz4`, `xz`, `bzip2` and `jpeg` | rejected | |

- `gzip`: a `useZlib` member that is not a boolean rejects the input. Java
  N5 writes a gzip stream (RFC 1952) unless `useZlib` is true, when it
  writes a zlib stream (RFC 1950).
- `blosc` (the n5-blosc compression): `c` is the member `cname`, which MUST
  be one of `blosclz`, `lz4`, `lz4hc`, `snappy`, `zlib` and `zstd`; `l` is
  the member `clevel`, an integer from 0 to 9; `s` is `"noshuffle"`,
  `"shuffle"` or `"bitshuffle"` for the member `shuffle`, an integer 0, 1
  or 2; `k` is the member `blocksize`, an integer from 0 to 2^31 − 1, or 0
  if it is absent; `b` is the bytes per element. A missing `cname`, `clevel`
  or `shuffle`, or any value outside these, rejects the input. (A blosc
  stream describes itself, so every n5-blosc block decodes with the Zarr
  `blosc` codec; these checks make its configuration exactly the
  compressor's.)
- `L` is the member `level`. For `gzip` it MUST be an integer from −1 to
  9, and `L` is that integer, except that −1 (the default of Java's
  `Deflater`, which Java N5 writes) is 6, the level it stands for (the
  Zarr v3 `gzip` and `zlib` codecs take 0 to 9); when the member is absent,
  `L` is 6 too. For `zstd` it MUST be an integer from −131072 to 22 (zstd's
  levels, which the Zarr v3 `zstd` codec takes), and `L` is that integer;
  when it is absent, `L` is 3, the default of n5-zstandard. Any other value
  rejects the input. (A level only matters when encoding; the codec
  carries it so that the compression can be recovered.)

The compression object's members that `C` does not carry (such as
n5-blosc's `nthreads`) are not read here; they are kept in the source
metadata (§5).

**The array.** The dataset's `zarr.json` is the object

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": dimensions,
  "data_type": "...",
  "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": blockSize}},
  "chunk_key_encoding": {"name": "v2", "configuration": {"separator": "/"}},
  "fill_value": 0,
  "codecs": [{"name": "n5_default", "configuration": {"codecs": [T, B, C]}}],
  "attributes": A
}
```

with the member `"dimension_names"` only when §4 gives the array axis
names, and no other members, where:

- `T` is `{"name": "transpose", "configuration": {"order": [n−1, …, 1, 0]}}`;
- `B` is `{"name": "bytes", "configuration": {"endian": "big"}}`, or
  `{"name": "bytes"}` when `b = 1`;
- `C` is the compression codec, and is left out (the list is `[T, B]`)
  when there is none;
- `A` holds the declaration of §1, with the dataset's source metadata
  (§5), when it has any.

N5 has no fill value; blocks that do not exist read as 0.

### 3.1 Chunks

The **chunk keys** of a dataset at path `D` are `D/i0/i1/…/i(n−1)` (or
`i0/…/i(n−1)` for a dataset at the root), where each `ik` is the decimal
form of an integer with `0 ≤ ik < ceil(dimensions[k] / blockSize[k])`,
without leading zeros (`0` itself is the only form of zero). A dataset with
a zero in `dimensions` has no chunk keys. Its chunk objects ([VIRTUALIZE.md §1.4](../../VIRTUALIZE.md#14-store-inputs)) are the
store's objects with these keys, whatever their size; every other object
under `D` (an object whose name is not a chunk key, or whose index is
outside the grid) is an other object (§6).

Each chunk is present when its object is (and is not empty): its bytes
are the whole block object, header included, which the `n5_default` codec
decodes. Every other chunk is absent and reads as 0. An empty chunk object
is one of §6's empty objects, whose key is kept there. (N5 readers fail on
an empty block, which an interrupted write can leave behind, rather than
read it as absent; the key records that the object was there.)

An N5 block declares its **mode** in its header: default (0), varlength (1)
or object (2). Only default-mode blocks hold an array of the dataset's
shape, and nothing in a dataset's attributes says which mode its blocks
use, so the virtualizer, which reads no block ([VIRTUALIZE.md §1.4](../../VIRTUALIZE.md#14-store-inputs)), cannot know it. The
output always uses `n5_default`. A reader decoding a block whose mode is
not default, or whose header's number of dimensions is not `n`, fails on
that chunk (the `n5_default` codec rejects it, as its README requires);
the rest of the array reads normally.

## 4. Multiscale images

A group may describe a multiscale image in one of two widely used N5
conventions: COSEM (as on OpenOrganelle) and n5-viewer (as written by
BigDataViewer, Fiji's N5 plugins and Paintera). For each explicit group `G`
whose document has no member `ome`, the virtualizer tries COSEM first and
then n5-viewer; the first that **recognizes** `G` gives the OME-NGFF 0.5
object `M`, and `G`'s attributes get the member `"ome": M`. If neither
does, `G` has no `ome`. (A group recognized by both, such as
OpenOrganelle's, which have COSEM `multiscales` and n5-viewer `scales`,
uses COSEM.) Whatever is recognized, the N5 attributes themselves are in the source metadata (§5).

**Levels.** A **level path** is a string of one or more segments separated
by `/`, none of them empty, `.` or `..`. It names the node at `G`'s path
joined with it, which MUST be a dataset.

**Axis types.** An axis named `x`, `y` or `z` is a space axis, `t` a time
axis, and `c` a channel axis; any other name is a custom axis (no type).
The axes are **valid** when their names are nonempty and distinct and, in
array order, they are at most one time axis, then at most one channel or
custom axis, then two or three space axes, as OME-NGFF 0.5 requires.

**Units.** A unit string maps by [conventions §5](../README.md#5-units)'s table; a string that is already one
of that table's unit names (`micrometer`, `nanometer`, …, `hour`) is kept
as it is. Any other string gives no unit.

**COSEM.** `G` is recognized when all of these hold:

- its member `multiscales` is a nonempty array whose first element `m` is
  an object whose member `datasets` is a nonempty array of objects (only
  `m` is read);
- every element `d` of `datasets` has a member `path` that is a level path,
  and its **transform** is `d`'s member `transform` if `d` has one, and
  otherwise the member `transform` of the level's dataset document; the
  transform is an object with:
  - `axes`, an array of `n` strings, where `n` is the level's number of
    dimensions;
  - `scale`, an array of `n` numbers, each positive;
  - `translate`, an array of `n` numbers, or absent (zeros);
  - `units`, an array of `n` strings, or absent (no units);
  - `order`, the string `"C"` or `"F"`, or absent (`"C"`);
- every level has the same `n`, and after the reordering below the same
  axes and the same units;
- the axes are valid.

COSEM lists a transform's values in C order, the reverse of N5's dimension
order (OpenOrganelle's `fibsem-uint16/s0` has `dimensions` `[12000, 1600,
6368]` in the order x, y, z, and the transform `"axes": ["z", "y", "x"]`),
unless its `order` is `"F"`. So, for order `"C"`, `axes`, `scale`,
`translate` and `units` are each reversed onto the dimension order; for
`"F"` they are used as they are. Then `M` is

```json
{"version": "0.5", "multiscales": [{
  "name": ...,
  "axes": [{"name": a, "type": ..., "unit": ...}, ...],
  "datasets": [{"path": path, "coordinateTransformations": [
    {"type": "scale", "scale": [...]},
    {"type": "translation", "translation": [...]}]}, ...]
}]}
```

with `name` only when `m`'s member `name` is a string (it is that string);
each axis with `type` only when it has one and `unit` only when its unit
maps; and the datasets in `datasets`' order, each with its path as written
and the scale and translation of its transform (the translation always
present, zeros when `translate` is absent).

**n5-viewer.** `G` is recognized when all of these hold:

- the **levels** are found by the first of these that applies:
  - `G` has a member `scales`: it is a nonempty array of `k` arrays, each of
    `n` positive numbers, the **factors** of level `i`; the levels are the
    datasets `s0`, …, `s(k−1)` (level paths), each with `n` dimensions;
  - otherwise: `s0` and `s1` are datasets, and the levels are `s0`, `s1`,
    …, `s(k−1)` for the largest `k` such that each of them is a dataset;
    the factors of `s0` are its member `downsamplingFactors` if it has one,
    else `n` ones; the factors of each later level are its member
    `downsamplingFactors`, which it MUST have; each `downsamplingFactors` is
    an array of `n` positive numbers, and every level has the `n`
    dimensions of `s0`;
- the **resolution** `r` and the **unit**: the member `pixelResolution` of
  `G`, or, if `G` has none, of `s0`'s document. It is either an object with
  `dimensions`, an array of `n` positive numbers (`r`), and optionally
  `unit`, a string; or an array of `n` positive numbers (`r`, no unit). If
  neither document has `pixelResolution`, `r` is `n` ones and there is no
  unit. (`pixelResolution` is in dimension order.)
- the **axes**: `G`'s member `axes`, an array of `n` strings, if it has
  one; else `["x", "y"]` for `n = 2` and `["x", "y", "z"]` for `n = 3`
  (for any other `n`, `G` is not recognized). The axes are valid.

Any member above that is present but not of the stated form means `G` is
not recognized. Then `M` is as for COSEM, without `name`, with the datasets
`s0`, …, `s(k−1)`, each with a scale and a translation: along dimension
`j`, level `i`'s scale is `r[j] × factors_i[j]` and its translation is
`((factors_i[j] − 1) / 2) × r[j]`, since N5's downsampling (as BigDataViewer
and the N5 tools do it) centers a downsampled voxel on the voxels it
averages. The unit, if it maps, is the unit of every space axis.

**Dimension names.** OME-NGFF 0.5 requires every level array to have
`dimension_names` equal to its image's axis names. Groups are considered in
ascending order of path (compared as UTF-8). A group that is recognized,
but one of whose levels is already a level of an earlier recognized group
with different axis names, is not recognized after all: it gets no `ome`. (An array can only have one set of
`dimension_names`, so the later image could not be valid 0.5.) Each level of
each recognized group then gets `"dimension_names"`: the axis names in
array order.

## 5. Source metadata

A node's source metadata `S` ([conventions §2](../README.md#2-attributes)) is
the object

```json
{"attributes": A, "metadata": M}
```

where each member is present only when its value is not empty, and
the node has source metadata only when `S` has a member. An implicit group,
which has no document, has none.

- `A` is the node's `attributes.json` document without its **layout
  members**: the container's version `n5`, and, for a dataset,
  `dimensions`, `blockSize`, `dataType`, `compression` and
  `compressionType`. It is the user's attributes and those of the
  multiscale conventions (`pixelResolution`, `transform`, `scales`, ...),
  unchanged. (A group's `dataType` or `compression` is not a layout member:
  it describes no dataset.) Every member is under the key, so none can
  collide with `ome` or with the conventions of the Zarr v3 hierarchy; a
  document that has its own `vzip_virtualized` or `zarr_conventions` nests
  them there.
- `M` holds the layout members that the Zarr v3 metadata does not
  reproduce, in this order:
  - `n5`, the member as written, when the document has it;
  - for a dataset, `compression`: the object of the compression object's
    members that its codec does not carry (§3), when there is any;
  - for a dataset whose document has both `compression` and
    `compressionType`, the member `compressionType` as written (§3 reads
    only `compression`).

What `S` leaves out, the Zarr v3 metadata holds: `dimensions`, `blockSize`,
`dataType`, and the compression's `type` and the members its codec
carries. What neither keeps is layout: whether a default level was written
(or was −1 rather than 6), whether `useZlib` was `false` or absent, and
whether an old container wrote `compressionType` rather than `compression`.
So a node's document is recovered from its `zarr.json`: the members that §3
reads back from the Zarr v3 metadata (`dimensions` the shape, `blockSize`
the chunk shape, `dataType` the data type, `compression` from the codec),
then those of `A`, and those of `M` set over them (the members of
`M.compression` over the compression object).

Members are copied as [VIRTUALIZE.md §1.6](../../VIRTUALIZE.md#16-json-documents)
reads them, except that a number written as an integer (a JSON number
with no fraction and no exponent) is kept exactly, every digit, beyond
2^53 − 1 too, and is written back so. Any other number is its binary64
value, and a number whose binary64 value is infinite still rejects the
input. Where §3 and §4 use a number (a size, a level, a scale), they use
its binary64 value.

## 6. Other objects

An **other object** is an object of the store
([VIRTUALIZE.md §1.4](../../VIRTUALIZE.md#14-store-inputs)) whose size is not
0 and which is neither the `attributes.json` of a node (an explicit group
or a dataset, §2) nor a chunk object of a dataset (§3.1), whatever its
size. Among them are READMEs and other files a writer put next to the
data, the objects of directories that are not nodes, the objects under a
dataset that are not chunk keys, and a dataset nested in a dataset (its
`attributes.json` and its blocks), which is not a node (§2). Each is kept
whole: the hierarchy has the key `vzip_source/objects/<k>`, where `k` is the
object's key, escaped, and its bytes are the object's. (An empty object
holds nothing, and has no key; §6 lists its key under "Empty objects".)

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
0, the empty chunk objects (§3.1) among them. The **ignored keys** are the
relative keys of the listed objects that
[VIRTUALIZE.md §1.4](../../VIRTUALIZE.md#14-store-inputs) ignores and
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
[conventions §2](../README.md#2-attributes)), whose `zarr.json` is
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

## 7. Example

The level `s0` of a COSEM multiscale image `em/fibsem-uint8` of the store
`https://example.org/c.n5/`, whose compression is
`{"type": "gzip", "level": -1, "useZlib": false}`:

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": [
    12,
    10,
    8
  ],
  "data_type": "uint8",
  "chunk_grid": {
    "name": "regular",
    "configuration": {
      "chunk_shape": [
        12,
        10,
        8
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
      "name": "n5_default",
      "configuration": {
        "codecs": [
          {
            "name": "transpose",
            "configuration": {
              "order": [
                2,
                1,
                0
              ]
            }
          },
          {
            "name": "bytes"
          },
          {
            "name": "gzip",
            "configuration": {
              "level": 6
            }
          }
        ]
      }
    }
  ],
  "dimension_names": [
    "x",
    "y",
    "z"
  ],
  "attributes": {
    "zarr_conventions": [
      {
        "uuid": "ad5d4c39-c69e-48f7-a3ef-4cc8c607d416",
        "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/heads/main/conventions/n5/schema.json",
        "spec_url": "https://github.com/d-v-b/vzip/blob/main/conventions/n5/README.md",
        "name": "vzip_virtualized",
        "description": "The Zarr layout of a N5 source virtualized by vzip, and the source's metadata"
      }
    ],
    "vzip_virtualized": {
      "n5": {
        "attributes": {
          "transform": {
            "axes": [
              "z",
              "y",
              "x"
            ],
            "scale": [
              5.24,
              4.0,
              4.0
            ],
            "translate": [
              0.0,
              0.0,
              0.0
            ],
            "units": [
              "nm",
              "nm",
              "nm"
            ]
          },
          "pixelResolution": {
            "dimensions": [
              4.0,
              4.0,
              5.24
            ],
            "unit": "nm"
          }
        }
      }
    }
  }
}
```
