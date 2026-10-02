# Virtualizing Imaris IMS files

The IMS profile of [VIRTUALIZE.md](../VIRTUALIZE.md) (revision 11), numbered
as its §8. §1 and §2 are in VIRTUALIZE.md and apply here.

## 8. IMS profile

An Imaris file (`.ims`) is an HDF5 file holding a fixed layout of groups and
datasets (§8.7): one chunked 3-D dataset per resolution level, time point and
channel, with text attributes for the sizes and the metadata (§8.8). The
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
fixed arrays or as a single chunk. Imaris 10 can also compress chunks with LZ4
(filter 32004), alone or after byte shuffling (filter 2); no codec of §2.1
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
  more is read.

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
objects of a fractal heap, indexed by a version 2 B-tree.

**Version 2 B-trees.** A tree of type `T` at `a`: the 38 bytes at `a`
start with `BTHD`, then version 0 and type `T` (byte 5). Its node size `N`
is the 4 bytes at 6, its record size `R` the 2 bytes at 10, its depth `D`
the 2 bytes at 12, the root node's address the 8 bytes at 16 (if undefined,
the tree has no records) and the root's number of records the 2 bytes at 24.
`R` MUST be 24, 11 or 17 for type 1, 5 or 8, `N` MUST be at least
`10 + R`, and `D` at most 16.

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
  allocated read as 0, as in Zarr (§2.1). (No fill value message, or no fill
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
      a length) and filter mask (4 bytes, which MUST be 0);
    - 3, a **fixed array**: 1 byte, not read (the page bits of the fixed
      array header are used).

    Other index types (implicit, extensible array, version 2 B-tree) reject
    the input. With filters, flag bit 0 (edge chunks not filtered) rejects
    the input, and so does a single chunk index without flag bit 1.
  - Other versions reject the input.

  The dimensionality `m` MUST be the rank plus 1, and the last dimension
  MUST equal the datatype's size; the others are the **chunk shape**, each of
  which MUST be from 1 to 2^53 − 1. A null dataspace rejects the input.

The **chunk grid** has `ceil(n / c)` chunks along each dimension, of size
`n` and chunk size `c`; the number of chunks, their product, MUST be at
most 2^53 − 1. A chunk's **byte count** is the product of the chunk shape and
the datatype's size.

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
- **Fixed array:** the dataspace's maximum sizes, if present, MUST equal its
  sizes. The 28 bytes at the index address start with `FAHD` and version 0;
  byte 5 is the client ID, which MUST be 1 with filters and 0 without; byte 6
  is the entry size `E`, which MUST be 8 without filters and from 13 to 20
  with; byte 7 is the page bits `g`; the 8 bytes at 8 are the number of
  entries `n`, which MUST equal the number of chunks; and the 8 bytes at 16
  are the data block's address `d` (if undefined, no chunk is allocated).
  The 14 bytes at `d` start with `FADB`, version 0, and the client ID.
  Entry `i` is the chunk whose coordinates are `i` in row-major order
  over the grid (the last dimension fastest).
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
input. An attribute's **value** is read only where §8.7 and §8.8 read the
attribute: its flag bits 0 and 1 (shared datatype or dataspace) MUST be
clear; its datatype and dataspace (§8.5) MUST lie within the message; the
dataspace MUST NOT be null; its element count is the product of its sizes (1
for a scalar); and its data, the next count × datatype size bytes, MUST lie
within the message.

**Text.** Imaris writes each attribute as a 1-dimensional array of
1-character strings. An attribute read as **text** MUST have a string
datatype (class 3), of any size, padding and character set; any other
datatype rejects the input. Its text is its data up to the first NUL byte
(all of it if there is none), decoded as UTF-8 if it is valid UTF-8, and
otherwise byte by byte as the code points U+0000 to U+00FF (ISO 8859-1).

- A **decimal** is a text that, with leading and trailing whitespace (§1.3)
  removed, matches `[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?` and
  whose value (§1.3) is finite. Any other text is not a decimal.
- A **list of `n` decimals** is a text that, with leading and trailing
  whitespace removed and split at each run of whitespace, has exactly `n`
  parts, each a decimal.
- An **integer** is a text that, with leading and trailing whitespace
  removed, is one or more digits.

### 8.7 The Imaris layout

Names below are ASCII; a number in a name is in decimal without leading
zeros. The virtualizer opens (§8.3) these groups, following each link (§8.3)
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
  attributes are read (§8.6), and it MUST have a link `Data`, which leads to
  the **dataset** `(r, t, c)`, read by §8.5 with its allocated chunks.
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

### 8.8 Metadata

If the root group has a link `DataSetInfo`, it is followed and the group
opened. Each of that group's links `Image`, `Channel c` (for `c < C`)
and `TimeInfo` that exists is followed, the group opened, and its
attributes read; a missing group has no attributes. These attributes are
read as text (§8.6) where present:

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
  (§1.3), and if `e > 0` the axis has the **extent** `e`.
- **Unit.** If `Unit` is absent or empty, the unit is `micrometer`, Imaris's
  default. Otherwise it is the unit of `Unit` by
  §2.3 (matched exactly) if that is a length unit, and there is none if not.
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
  fractions converted to binary64 (§1.3), and the subtraction and addition
  are binary64 operations in that order. If
  `Δ > 0` the **time step** is `Δ / (T − 1)` seconds, the mean interval
  between time points. Otherwise there is no time step.

### 8.9 Output

One image at the archive root (§2.2), with one array per level `r` at path
`"<r>"`.

- **Axes:** `t` if `T > 1`; `c` if `C > 1`; `z` if level 0's `Z` is
  more than 1 or some level's `cz` is more than 1 (a chunk of several z
  planes keeps them, so that its bytes decode to its Zarr chunk); then `y`,
  `x`. When level 0's `Z` is 1, every level's `Z` MUST be 1.
- **Array** of level `r` (§2.1): shape `T`, `C`, `Zr`, `Yr`, `Xr`
  and chunk shape 1, 1, `cz`, `cy`, `cx` (each only for the axes
  present); the data type of §8.7; codecs `bytes` (with the byte order as
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
  has the translation (§2.2) `ExtMin0` for `x`, `ExtMin1` for
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
- **Chunks:** each allocated chunk of dataset `(r, t, c)` at coordinates
  `(i, j, k)` that is not wholly outside the image (that is,
  `i × cz < Zr`, `j × cy < Yr` and `k × cx < Xr`) is the entry
  `<r>/c/<coords>` with the single range `(0, address, size)`; the coords
  are `t`, `c`, `i` (each only when its axis is present), `j`, `k`.
  Chunks in the padding are not output, and unallocated chunks have no entry.
  HDF5 stores edge chunks whole, like Zarr, so a chunk's bytes decode to its
  Zarr chunk. (A single range's payload is at most 21 bytes, within the limit
  of §1.2.)
