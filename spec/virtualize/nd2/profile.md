# Virtualizing ND2 files

The ND2 profile of [spec/virtualize.md](../../virtualize.md) (revision 16), numbered
as its §5. §1 and §2 are in spec/virtualize.md and apply here.

Profile version: 1 · Convention: [spec/virtualize/nd2.md](../nd2.md),
version 1. The output has the convention's layout for the input
([spec/virtualize.md §2](../../virtualize.md#2-the-zarr-layout)); this profile says
how the file is read, which inputs are rejected, and how each chunk
references the file. "The convention §n" below is a section of
spec/virtualize/nd2.md.

## 5. ND2 profile

### 5.1 Chunks

An ND2 file (format version 3 or later) is a sequence of **chunks**. A chunk
at offset `o` has a 16-byte header: `u32` magic `0x0ABECEDA`, `u32` name
length `n`, `u64` data length `d` (all little-endian), then `n` bytes of name
(ASCII, NUL-padded), then `d` bytes of data starting at `o + 16 + n`. Every
chunk the virtualizer reads MUST start with the magic, else the input is
rejected; names are not checked except as stated. Reading a chunk's header
means reading its 16 bytes and its `n` bytes of name (both MUST lie within
the file), not its data.

- **Signature:** the chunk at offset 0 MUST be named
  `ND2 FILE SIGNATURE CHUNK NAME01!`, with `n = 32` and `d = 64`. Its data
  starts with `Ver`, decimal digits (the major version `M`), and `.`; `M`
  MUST be at least 3.
- **Chunk map:** the file MUST be at least 40 bytes long; its last 40 bytes
  are the 32 bytes `ND2 CHUNK MAP SIGNATURE 0000001!` and a `u64` offset
  `m`. The chunk at `m` MUST be named `ND2 FILEMAP SIGNATURE NAME 0001!`
  (its name bytes up to the first NUL, or all `n` if there is none). Its
  data is a sequence of records, each a name running up to and including the
  first `!`, then a `u64` offset and a `u64` size (unused). The record named
  `ND2 CHUNK MAP SIGNATURE 0000001!` ends the map: its offset and size, and
  anything after them, are not read. A record (other than the last) that
  runs past the end of the data, or data without that last record, rejects
  the input. The map gives each named chunk's header offset; of duplicate
  names, the last is used. An offset is checked (≤ 2^53 − 1, chunk magic)
  only when that chunk is read.
- **Inflation limit:** a chunk that the convention's §3 reads
  (`ImageAttributesLV!`, `ImageMetadataLV!`, `ImageMetadataSeqLV|0!`) and
  that is a compressed record (the convention §2.2) MUST inflate to at most
  2^26 bytes (64 MiB, the source metadata's budget, the convention §5.1),
  else the input is rejected; a virtualizer stops inflating there.
- **Record budget:** those three chunks, read in that order, hold at most
  2^20 LV records and byte-array bytes together: each record counts 1, at
  every depth (a level and each of its records), and a byte array (type 9)
  counts its length too. A file whose three chunks hold more is rejected; a
  virtualizer stops decoding there.

### 5.2 Rejection

The input is rejected when a rule of §5.1 or §5.3 fails, and when the
convention gives it no layout: wherever its §2.2 (for the chunks that its
§3 reads), §3 or §4 says that the input is rejected, or that something
MUST hold and it does not. The chunks that only the source metadata reads
(the convention §5) never reject the input.

### 5.3 Frames

Frame `f` ([the convention §4.1](../nd2.md#41-frames)) is
the chunk named `ImageDataSeq|<f>!`. A frame's data is an 8-byte timestamp followed by its pixels: `uiHeight`
rows of `uiWidthBytes` bytes, each holding `uiWidth × uiComp` samples
(interleaved by component) of `uiBpcInMemory / 8` little-endian bytes,
then padding. Let `R = uiWidth × uiComp × uiBpcInMemory / 8`;
`uiWidthBytes` MUST be at least `R`.

- **Uncompressed:** the virtualizer MUST read the header of every present
  frame (a frame `f < N` that the chunk map names), and reject the file if
  their name lengths `n` are not all the same, or if any one's data length
  `d` is less than `8 + uiHeight × uiWidthBytes` (a header without the
  magic rejects it too, §5.1): the image takes every frame's pixels at the
  same distance from its chunk's offset. Frame `f`'s pixels start at
  `start = o + 16 + n + 8`, where `o` is its chunk's offset from the chunk
  map. (A frame's ranges MUST lie within the file, §1.2; its chunk's data
  need not.) Row `r` of the frame is the range
  `(0, start + r × uiWidthBytes, R)`.
  - If `uiWidthBytes = R`, the frame is one chunk, the single range
    `(0, start, uiHeight × R)`.
  - Otherwise (padded rows) the frame is split into **row blocks** of `h`
    rows: block `j` is the ranges of rows `j × h` to `j × h + h − 1`, in
    order. `h` is the largest divisor of `uiHeight` such that every block of
    every present frame has a payload of at most 65519 bytes (§1.2). (`h = 1`
    always qualifies, so padded frames are never rejected for their payload.)
- **Compressed:** `uiWidthBytes` MUST equal `R`. The virtualizer reads every
  present frame's header; the frame is one chunk, the single range
  `(0, o + 16 + n + 8, d − 8)`, a zlib stream of the pixels, and `d` MUST be
  more than 8.

Let `h` be `uiHeight` except for padded rows, where it is the block height.

**Positions.** Each position is an image of two documents
([the convention §4.3](../nd2.md#43-output)), whether or not
any frame is placed at it, and its `omero` lists every component. The number
of positions `P` (the position loop's count, 1 without one) MUST be at most
2^16, and `P × uiComp` at most 2^20, else the input is rejected.

### 5.4 Chunk references

Frame `f` at position `p` (0 without a position loop) is the
entry `<p>/0/c/<coords>` with its ranges (§5.3); coords are its `t`, 0 for
`c`, its `z` (each only when its axis is present), then 0, 0. With row
blocks, block `j` is the entry with coords ..., `j`, 0.
