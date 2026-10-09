# Virtualizing Imaris IMS files

The IMS profile of [spec/virtualize.md](../../virtualize.md) (revision 16), numbered
as its §8. §1 and §2 are in spec/virtualize.md and apply here.

Profile version: 1 · Convention: [spec/virtualize/ims.md](../ims.md),
version 1. The output has the convention's layout for the input
([spec/virtualize.md §2](../../virtualize.md#2-the-zarr-layout)); this profile says
how the file is read, which inputs are rejected, and how each chunk
references the file. "The convention §n" below is a section of
spec/virtualize/ims.md.

## 8. IMS profile

An Imaris file (`.ims`) is an HDF5 file holding a fixed layout of groups and
datasets ([the convention §2.2](../ims.md#22-the-imaris-layout)): one chunked 3-D dataset per resolution level, time point and
channel, with text attributes for the sizes and the metadata ([the convention §3](../ims.md#3-metadata)). The
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
(filter 32004), alone or after byte shuffling (filter 2); no codec of [conventions §3](../../conventions.md#3-arrays)
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
  allocated read as 0, as in Zarr ([conventions §3](../../conventions.md#3-arrays)). (No fill value message, or no fill
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
