# Virtualizing CZI files

The CZI profile of [VIRTUALIZE.md](../VIRTUALIZE.md) (revision 18), numbered
as its §13. (Conformance is VIRTUALIZE.md's §14.) §1 and §2 are in
VIRTUALIZE.md and apply here.

Profile version: 1 · Convention: [conventions/czi](../conventions/czi/README.md),
version 1. The output has the convention's layout for the input
([VIRTUALIZE.md §2](../VIRTUALIZE.md#2-the-zarr-layout)); this profile says
how the file is read, which inputs are rejected, and how each chunk
references the file. "The convention §n" below is a section of
conventions/czi/README.md.

## 13. CZI profile

### 13.1 Detection

A file input is read by this profile when bytes 0–15 of `H` (VIRTUALIZE.md
§1.2) are `5A 49 53 52 41 57 46 49 4C 45` (`ZISRAWFILE`) followed by six
`00` bytes. (This row goes before the "anything else" row of §1.2's table;
no other profile's test matches these bytes.)

### 13.2 Structures and the walk

Reading a segment header at `o` means reading its 32 bytes, which MUST lie
within the file; when the segment is referenced (steps 1–5), its id MUST
be the one expected, and its AllocatedSize and UsedSize MUST be from 0 to
2^53 − 1. Reading a structure means reading its bytes, which MUST lie
within the file; every offset and length read MUST be from 0 to
2^53 − 1 and every size field the convention says is at least 0 MUST be.
The virtualizer reads, in any order (VIRTUALIZE.md §1.2, evaluation order):

1. the file header: the segment header at 0 and its 80 bytes of data (the
   convention §2.2);
2. the directory: the segment header at SubBlockDirectoryPosition, then the
   4-byte EntryCount and the entries, from `o + 160` to at most
   `o + 32 + U` (`U` its UsedSize, or AllocatedSize when `U` is 0). An entry
   that does not end within these bytes rejects the input. EntryCount is
   checked (0 to 2^21) before any entry is read;
3. for each entry `i`: the segment header at its FilePosition and the
   subblock's first `L` bytes (`L` from its `d'`, the convention §2.4; the
   first 256 bytes MUST lie within the file before `d'` is read), and,
   when its copy of its entry agrees with the directory, the codec header of
   its data (§13.4);
4. when MetadataPosition is not 0, the metadata segment's header and first
   8 bytes of data, and the XML when §2.7 of the convention reads it;
5. when AttachmentDirectoryPosition is not 0, the attachment directory's
   header, EntryCount (0 to 2^16, checked first) and entries; for each `A1`
   entry, the attachment segment's header and first 256 bytes of data; and
   the data of attachments whose form the convention decodes (`CZTIMS`,
   `CZFOC`: only bytes 0–7; `CZEVL`: all of it, when `s` is at most 2^26);
6. **the walk.** From `o = 0`: if `o` is the file's size, the walk ends with
   an empty tail. Otherwise the segment header at `o` is a **segment** when
   it lies within the file, its id is 1 to 16 bytes of `A`–`Z`, `0`–`9` and
   `_` followed by NUL bytes to 16, its AllocatedSize `A` and UsedSize `U`
   are at least 0, and `o + 32 + A` is at most the file's size; the walk
   then continues at `o + 32 + A`. When it is not a segment, or after
   2^23 segments, the walk ends and the tail is the bytes from `o` to the
   end of the file. Only segment headers are read; a virtualizer MAY take
   those of referenced segments from step 1–5's reads, and read only the
   others, in order.

### 13.3 Rejection

The input is rejected when a read of §13.2 steps 1–5 lies outside the file,
when a rule of this profile fails, and wherever the convention says the
input is rejected or that something MUST hold: Major and FilePart (§2.2),
the directory and its entries (§2.3: segment id, EntryCount, schema `DV`,
dimension ids, counts and sizes), the subblock segments (§2.4: id, sizes,
schema `DV` and `d'` of the subblock's copy, parts within the file), the
metadata segment and attachment directory when present (§2.5, §2.6: ids,
counts, parts within the file, FilePart), pixel types and compressions
(§3.1), and the limits below. What only §3.2 decides (a subblock's coded
size) never rejects: such a subblock is unplaced. The walk (§13.2 step 6)
never rejects. Neither does what only the source metadata reads (the
attachment forms, the budget), nor the metadata XML.

**Limits.** These bound memory, time and the documents; exceeding one
rejects the input:

| limit | value | bounds |
|---|---|---|
| directory entries `N` | 2^21 | requests (one subblock header each), the mirror's table (conventions §8) |
| attachment entries `K` | 2^16 | the mirror's table |
| series with an image | 2^16 | the `OME` series list (about 7 bytes per series), as for ND2 positions |
| levels per image | 64 | the image's `multiscales` (the layer tables give at most 18) |
| array extent | every dimension of an image's or tile array's shape (`t`, `c` with its samples, `z`, and the stored extent of `y` and `x`) at most 2^31 | shapes |

The walk stops at 2^23 segments, the XML is read for the layout only when
at most 2^26 bytes, codec headers are scanned within 2^16 bytes, and event
lists are decoded only up to 2^20 events and 2^26 bytes; past those the
convention keeps the bytes as they are, without rejecting. Every document
is then bounded: the root's and each image's by the limits above (`omero`
has at most 64 channels), each tile array's by its fixed members (the
convention §4.4), whatever its number of planes, and `vzip_source`'s by
the mirror's budgets (conventions §8.7). The tile arrays number at most `N`.

### 13.4 Codec headers

Each is read from the start of the subblock's data `D` (its `n` bytes); a
read past `n` bytes, or past 2^16 bytes into `D`, means the subblock has no
coded size (the convention §3.2). They are coding parameters, which the
output may depend on (VIRTUALIZE.md §1.2, structure only).

- **Zstd1 header:** `D[0]` is 1 (length 1, no packing), or `D[0]` is 3,
  `D[1]` is 1, and `D[2]` gives the hi-lo flag in bit 0 (length 3). Any
  other first bytes, or `n` not more than the header length, give no coded
  size.
- **Zstd frame header**, at the start of the frame (after the Zstd1 header
  for Zstd1): the magic `28 B5 2F FD`, then the frame header descriptor
  byte `h`, with `h` bit 3 (reserved) clear and dictionary flag `h & 3` equal
  to 0; a window descriptor byte when bit 5 (single segment) is clear; then
  the content size field, of 8, 4, 2 or (with single segment) 1 bytes for
  `h >> 6` of 3, 2, 1 and 0 (no field for 0 without single segment, which
  gives no coded size), little-endian, plus 256 for the 2-byte field. The
  subblock has its stored size as coded size when that content size is
  `W × H × q` (the convention §3.2).
- **JPEG:** `D` starts with `FF D8`. Then markers are scanned: at each, one
  or more `FF` bytes and a marker byte `M`; for `M` in `D0`–`D7` or `01`
  nothing follows; otherwise a 2-byte big-endian length `l ≥ 2` and `l − 2`
  bytes. The first `M` in `C0`–`CF` other than `C4`, `C8` and `CC` is the
  frame header: precision (1 byte), height and width (2 bytes each,
  big-endian) and the component count (1 byte). There is a coded size when
  `M` is `C0`, `C1` or `C2`, the precision is 8, the height and width are at
  least 1 and the component count is the pixel type's `p`; a start of scan
  (`DA`) or end of image (`D9`) before the frame header gives none.
- **JPEG XR:** `D` starts with `49 49 BC 01`; the `u32` at byte 4 is the
  offset of the image directory: a `u16` count `e` and `e` entries of 12
  bytes (`u16` tag, `u16` type, `u32` count, `u32` value or offset). Of the
  first entries with tags `BC01` (PixelFormat: type 1, count 16, its offset
  giving the 16-byte GUID), `BC80` (ImageWidth) and `BC81` (ImageHeight,
  each of type 3 or 4 and count 1, the value in the first 2 or 4 bytes of the
  value field), all MUST be present. The pixel formats each pixel type
  admits are, by the GUID's bytes as stored (the common prefix
  `24 C3 DD 6F 03 4E FE 4B B1 85 3D 77 76 8D C9` and a last byte):
  Gray8 `08`; Gray16 `0B`; Gray32Float `11`; Bgr24 `0C` (24bppBGR) or `0D`
  (24bppRGB); Bgr48 `15`; Bgra32 `0F`; Bgr96Float the GUID
  `8F D7 FE E3 DB E8 CF 4A 84 C1 E9 7F 61 36 B3 27`. The coded size is
  (ImageWidth, ImageHeight), each at least 1.

### 13.5 Chunk references

All references are ranges of source 0; this profile has no data sources.
Let `P = o + 32 + L + m` be subblock `i`'s data offset (the convention §2.4).

- **Image chunks and tiles** (the convention §4.3, §4.4): an uncompressed
  subblock is the single range `(0, P, W × H × q)`, or with row bands, band
  `k` of its rows is `(0, P + k × b × W × q, b × W × q)`. A Zstd0 subblock
  is `(0, P, n)`, a Zstd1 subblock `(0, P + l, n − l)` (`l` its header
  length), a JPEG or JPEG XR subblock `(0, P, n)`. Pixels are never copied.
- **Directory columns** (the convention §5.3) and `segments/id` are copied:
  their elements are scattered over the entries. The event list's `time`
  and `type` are copied likewise.
- **Families** (`subblocks/...`, `segments/data`, an event list's
  `description`): each member is the range of its bytes in the file, and the
  family's chunks are made as conventions §7 says: referenced, with adjacent
  members merged into one range, and a `data` chunk copied when its ranges
  exceed the 65519-byte payload (VIRTUALIZE.md §1.2), as when many small
  metadata fragments share a chunk.
- **Bytes** (`metadata/xml`, `metadata/attachment`, an attachment's data,
  `tail`) and **contiguous values** (TimeStamps, FocusPositions): referenced
  in place, cut as conventions §7 says.
- **Documents** of `tiles/<n>` are stored, like those of `vzip_source`, so
  that a reader fetches them only when it opens the node (conventions §2):
  there is one per tile position and copy.

### 13.6 Equivalence notes

The hi-lo flag, Zstd frame content size, JPEG frame header and JPEG XR
directory decide whether a subblock is placed, so two producers read them
alike, and only them: a producer MUST NOT decompress a subblock to decide
anything (structure only). A zstd stream that, after a valid frame header,
holds more than one frame or trailing bytes is placed all the same, and
fails when a reader decodes it; a writer of such files is not known.

### 13.7 Reading the output

The layout uses two things that are not in every Zarr reader:

- **Clipped levels** use the zarr-extensions `rectilinear` chunk grid (the
  convention §4.3). zarr-python 3.4 reads them after
  `zarr.config.set({"array.rectilinear_chunks": True})`. Where a reader has
  no rectilinear support, a clipped level can still be read chunk by chunk:
  the chunk at `y` index `j` and `x` index `i` holds the tile of that row
  and column, of the stored size `(W, H)`, or `W'` wide in the last column
  and `H'` high in the last row (vzip's verifier,
  `web/test/czi/verify.py`, checks both ways).
- **Codecs.** `imagecodecs_jpegxr` (JPEG XR subblocks) and
  `numcodecs.shuffle` (Zstd1 with hi-lo packing) are not in the
  zarr-extensions registry, nor in the Neuroglancer fork vzip's viewer uses.
  zarr-python 3 has `numcodecs.shuffle`; `vzip.codecs` registers
  `imagecodecs_jpegxr`. Files with these codecs are accepted, not
  rejected: their chunks are the subblocks' bytes, so the archive is
  complete whatever a reader supports. Their registration, and decoders in
  the Neuroglancer fork, are a follow-up.

### 13.8 Measured cost

On the public corpus (`conformance/virtualize/corpus_czi.txt`, 24 files
of 1 MB to 2.1 GB), the Python reference makes about one range request per
subblock (2 KiB at its segment, which holds its header, and usually its
metadata and codec header; ranges less than 16 KiB apart are merged into
reads of up to 1 MiB), plus the metadata XML and the attachment headers:
498 requests and 1.7 MB read for a 505 MB slide of 481 subblocks, 31
requests for a 5.9 MB line scan of 3575 subblocks. The archives are 29 KB
to 1.7 MB; no document exceeds 1.9 KB. Details per file are in
`conformance/virtualize/REVISIONS.md` (revision 18).
