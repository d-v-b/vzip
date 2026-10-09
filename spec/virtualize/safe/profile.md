# Virtualizing Sentinel-2 SAFE products

The SAFE profile of [spec/virtualize.md](../../virtualize.md) (revision 17, draft),
numbered as its §12. (Conformance is spec/virtualize.md's §14.)
§1 and §2 are in spec/virtualize.md and apply here, with the changes this
document states.

Profile version: 1 · Convention: [spec/virtualize/safe.md](../safe.md),
version 1. The output has the convention's layout for the input
([spec/virtualize.md §2](../../virtualize.md#2-the-zarr-layout)); this profile says
how the product is read, which inputs are rejected, and how each chunk
references the product. "The convention §n" below is a section of
spec/virtualize/safe.md.

## 12. SAFE profile

A Sentinel-2 Level-1C or Level-2A product in the SAFE format is read in one
of two forms:

- **the directory form**, a store input (§1.4): `U` is the URL of the
  `.SAFE` directory, ending in `/`. It is listed as §1.5 says, and the
  profile is chosen there (§12.1);
- **the zip form**, a file input (§1.2): `U` is the URL of a `.SAFE.zip`
  file, and its entries are the product's objects (§12.4).

The two forms give the same hierarchy for the same product, but for
`source.url` and the source table: every document is the same, and every
chunk holds the same bytes.

### 12.1 Choosing the profile

**Directory form.** spec/virtualize.md §1.4's table gains a row, after the N5
row:

| root keys | |
|---|---|
| `manifest.safe` | SAFE profile (§12) |

**Zip form.** spec/virtualize.md §1.2's table gains a row, before the last:

| test on `H` | |
|---|---|
| bytes 0–3 are `50 4B 03 04` (a ZIP local file header) | SAFE profile (§12), zip form |

A zip file that is not a SAFE product is rejected by §12.4 or by the
convention.

### 12.2 Objects and reading

The product's objects are, in the directory form, the store's objects
(§1.4), and in the zip form its entries (§12.4). The profile reads:

- the product metadata and the tile metadata
  ([the convention §2](../safe.md#2-the-source)), whole;
- the XML documents of at most 65536 bytes (the candidates of
  [the convention §6.3](../safe.md#63-the-source-metadata-node)),
  whole, for their text values, in the order in which §6.3 takes them, as
  long as they can still become text: reading stops at the first candidate
  of `n` bytes for which `J + n > 65536`, where `J` is the size of the JSON
  of `S` with only its members `xml` (the texts chosen so far), `empty`,
  `empty_dirs` and `ignored`. (`S` with that candidate as text would be
  larger than `J + n`, and neither it nor any later candidate, which is no
  smaller, can be text.) A virtualizer reads at most 16 candidates ahead of
  the one it takes;
- of each band file, its boxes and codestream main header (§12.3) and the
  first 12 bytes of each tile-part (its SOT marker segment).

It reads no other bytes (but, in the zip form, those of §12.4). It does not
read the folder markers
([the convention §2.1](../safe.md#21-objects)), the
other objects, or the other XML documents but the product and tile
metadata. The reads of a band file include no coded data
but what the blocks a reader fetches happen to hold (**Structure only**,
§1.2). In the directory form, this replaces §1.4's rule that an object is
read whole, and the listing's empty objects whose keys end in `/` (which
§1.4 ignores without recording them) name directories (the convention
§2.1): a band file is read in ranges, with HTTP range requests. A
response that is not of the requested length is a failure.

### 12.3 Band files

A band file of `n` bytes is read as a JP2 file (ISO/IEC 15444-1, Annex I).
All integers are big-endian.

**Boxes.** A box at offset `o` is a `u32` `LBox` and a 4-byte type `TBox`,
then, when `LBox` is 1, a `u64` `XLBox`. Its length is `LBox`, or `XLBox`
when `LBox` is 1 (which MUST then be at least 16), or, when `LBox` is 0,
`n − o`. A length other than these, below the box's header (8 or 16 bytes),
or reaching beyond `n`, rejects the input. The boxes are read from offset 0,
one after another (superboxes are not entered). The first MUST be
`LBox = 12`, `TBox = "jP  "` (`6A 50 20 20`), content `0D 0A 87 0A`, and the
second MUST have `TBox = "ftyp"`. The boxes are read until the first whose
`TBox` is `"jp2c"`, which MUST be within the first 1024 boxes, MUST end at
`n`, and whose contents (after its header), `[c0, c1)` with `c1 = n`, are
the **codestream**. The bytes `[0, c0)` are the band file's **JP2 header**.

**Main header.** All markers below must lie within the codestream, or the
input is rejected.

- At `c0`: SOC, `FF 4F`.
- At `c0 + 2`: the SIZ marker `FF 51` and its segment of length `Lsiz`
  (the 2-byte field after the marker, counting itself): `Rsiz` (`u16`),
  `Xsiz`, `Ysiz`, `XOsiz`, `YOsiz`, `XTsiz`, `YTsiz`, `XTOsiz`, `YTOsiz`
  (`u32`), `Csiz` (`u16`), then `Csiz` triples `Ssiz`, `XRsiz`, `YRsiz`
  (`u8`). `Lsiz` MUST be `38 + 3 × Csiz`. Bits 14 and 15 of `Rsiz` (High
  Throughput, Part 2 extensions) MUST be 0. `XOsiz`, `YOsiz`, `XTOsiz` and
  `YTOsiz` MUST be 0, and `Xsiz`, `Ysiz`, `XTsiz` and `YTsiz` at least 1.
  `Csiz` MUST be 1 or 3. Every `Ssiz` MUST be the same, with bit 7 (signed)
  0, giving the precision `p = (Ssiz & 0x7F) + 1`, which MUST be at most
  16. Every `XRsiz` and `YRsiz` MUST be 1. The SIZ **segment** is the
  `2 + Lsiz` bytes from its marker.
- Then marker segments, each a marker `FF xx` followed by a `u16` length
  `L ≥ 2` and `L − 2` more bytes, up to the first SOT marker `FF 90`. Their
  markers MUST be among COD `FF 52` (exactly one), COC `FF 53`, QCD `FF 5C`
  (exactly one), QCC `FF 5D`, RGN `FF 5E`, POC `FF 5F`, CRG `FF 63` and COM
  `FF 64`. Any other (TLM `FF 55`, PLM `FF 57`, PPM `FF 60`, CAP `FF 50`,
  CPF `FF 59`, or one this list does not name) rejects the input: TLM and
  PLM describe the file's tile-parts and would be wrong in a chunk, and PPM
  moves the packet headers out of the tiles. The bytes from the end of the
  SIZ segment to the first SOT are the **main header rest** `R`, which is
  at least the COD and QCD segments, and MUST be at most 65536 bytes (it is
  a data source, §12.6, copied into the output; Kakadu's is 148 bytes).
- **COD**: `Scod` (`u8`), which MUST be 0 or 1 (no SOP or EPH markers, and
  default precinct anchors), the progression order (`u8`), the number of
  layers `Lay` (`u16`, at least 1), the multiple component transform
  (`u8`), the number of decomposition levels `N` (`u8`, at most 32), the
  code-block width and height exponents, the code-block style and the
  transform (`u8` each), and, when `Scod` is 1, `N + 1` precinct size bytes
  `PP_r` (for `r = 0 … N`): `PPx_r = PP_r & 0x0F` and `PPy_r = PP_r >> 4`.
  When `Scod` is 0, every `PPx_r` and `PPy_r` is 15. The segment's length
  MUST be `12`, or `13 + N` when `Scod` is 1.
- **COC**: `Ccoc` (`u8`, since `Csiz < 257`), which MUST be less than
  `Csiz`, with at most one COC per component, then `Scoc` (`u8`, 0 or 1)
  and the same fields as COD's from `N` on. A component with a COC has its
  `N` and `PP_r`; any other has COD's.

**Tile-parts.** The number of tiles is `nt = nx × ny`, with
`nx = ceil(Xsiz / XTsiz)` and `ny = ceil(Ysiz / YTsiz)`, and it MUST be at
most 65535. The tile-parts are read from the first SOT, at `s_0`: for
`k = 0, 1, …, nt − 1`, the 12 bytes at `s_k` MUST be `FF 90`, `Lsot = 10`,
`Isot = k` (`u16`), `Psot` (`u32`) at least 14, `TPsot = 0` and `TNsot = 1`,
with `s_k + Psot ≤ c1 − 2`; then `s_{k+1} = s_k + Psot`. The two bytes at
`s_nt` MUST be EOC `FF D9`, and `s_nt + 2` MUST be `c1`. So each tile has
one tile-part, in raster order, and the codestream ends there. The
tile-part of tile `k` is the bytes `[s_k, s_k + Psot)`. Its body (the rest
of its header, its PLT markers among them, and its packets) is
`[s_k + 12, s_k + Psot)`.

### 12.4 The zip form

The file is a ZIP archive (PKWARE APPNOTE 6.3), read from its end. All
integers are little-endian.

- **End of central directory.** The EOCD record is the last occurrence of
  `50 4B 05 06` in the file's last `min(n, 65557)` bytes for which the 22
  bytes of the record and its comment (of the length its last field gives)
  end exactly at the end of the file. If there is none, the input is
  rejected. Its disk numbers MUST be 0, and its two entry counts equal.
  When the entry count is `FFFF`, or the central directory's size or offset
  is `FFFFFFFF`, the 20 bytes before the EOCD MUST be a ZIP64 EOCD locator
  (`50 4B 06 07`, disk 0, one disk), and the record it locates a ZIP64
  EOCD record (`50 4B 06 06`), which gives the entry count, the size and
  the offset (`u64`). The central directory MUST lie within the file, and
  end where the ZIP64 record or the EOCD starts.
- **Central directory.** It is exactly `count` file headers
  (`50 4B 01 02`), filling its size. Of each the profile reads the flags,
  the method, the CRC-32, the compressed size `cs`, the uncompressed size
  `us`, the name, extra and comment lengths, the disk number (which MUST
  be 0) and the local header offset `lho`. For the fields that are
  `FFFFFFFF`, the ZIP64 extended information extra field (`0x0001`) MUST be
  present and give them, in the order `us`, `cs`, `lho`. Flag bit 0
  (encrypted) and bit 6 (strong encryption) MUST be 0. The name MUST be
  UTF-8 (the zip flag for UTF-8 is not consulted). No two entries may have
  the same name.
- **The product.** Every name MUST start with the same segment `D` followed
  by `/`, where `D` ends in `.SAFE`. An entry's key is its name without
  `D/`. An entry whose key is empty or ends in `/` is a directory entry. It
  is ignored, and its key is recorded unless its size is 0 (as §1.4 does
  for the listing). Its key, if not empty, names a directory (the
  convention §2.1). Otherwise a key MUST NOT contain an empty segment, `.`
  or `..`, nor a `\`. The product's objects are the other entries, with
  size `us`.
- **Empty objects.** An entry of size `us = 0` that is not a directory
  entry is an empty object: its method, its sizes and its local header are
  not read or checked.
- **Local headers.** For each other object, the 30 bytes at `lho` MUST be a
  local file header (`50 4B 03 04`), whose name length `nn` and extra
  length `ne` (bytes 26–29) give the object's data start `ds = lho + 30 + nn + ne`,
  and `ds + cs` MUST be at most the central directory's offset. The local
  header's other fields are not read: the central directory's are used. The
  objects' ranges `[lho, ds + cs)` MUST NOT overlap: entries that share
  bytes (several central entries naming one local entry, a zip bomb) are
  rejected.
- **Methods.** An object with method 0 (stored) MUST have `cs = us`, and
  its bytes are `[ds, ds + us)` of the file, which the output references.
  An object with method 8 (deflate) MUST be an XML document
  ([the convention §2.1](../safe.md#21-objects)), with
  `us` at most 2^26 (a datastrip metadata is 15–25 MB), and the `us` of all
  of them together MUST be at most 2^27; both are checked before anything is
  inflated. It is read whole and inflated when it is needed (the product
  and tile metadata, a candidate for text, an array's bytes), one at a time,
  not all at once. The raw deflate stream
  MUST end exactly at `cs` bytes and give exactly `us` bytes, whose CRC-32
  MUST be the entry's. The output copies its bytes (§12.7). Any other
  method, and method 8 for any other object, rejects the input. (The
  pixels can only be referenced in place when they are stored. ESA's SAFE
  zips store every entry. The bytes copied from a deflated XML document are
  metadata, not pixels.)

The local headers and the deflated XML documents (each when it is needed)
are read, and the reads of §12.2 apply to the stored XML documents and band
files, at their data start.

### 12.5 Rejection and limits

The input is rejected when a check of §1.2 (zip form) or §1.4–§1.6
(directory form), §12.3 or §12.4 fails, and when the convention gives it no
layout: wherever it says that the input is rejected, or that something MUST
hold and it does not. In particular:

- a product in the format used before December 2016, which has no
  `MTD_MSIL1C.xml` or `MTD_MSIL2A.xml`, or image files in more than one
  granule directory;
- a band file that is not a JP2 file, whose codestream has a TLM, PLM or
  PPM marker, SOP or EPH markers, more than one tile-part for some tile,
  tile-parts out of order, a nonzero image or tile origin, or a component
  that is signed, subsampled or of more than 16 bits;
- a band file whose size matches no resolution of the tile geocoding;
- a band file whose chunks would need empty tiles of more than 4096 bytes
  in one chunk, or more than the band file's size in all (§12.6);
- an XML document that the convention reads whose elements nest more than
  256 deep (§1.5);
- in the zip form, an encrypted entry, a band file or other object that is
  compressed, entries whose bytes overlap, a deflated XML document of more
  than 2^26 bytes, or deflated XML documents of more than 2^27 bytes
  together.

**Limits.** Every reference entry's payload MUST be at most 65519 bytes
(§1.2). A chunk's payload is about 100 bytes for an interior tile, and
grows with the empty tiles of an edge chunk (§12.6), which are bounded
there: at most 4096 bytes per chunk, and at most the band file's size for
all its chunks, before they are made. Every Sentinel-2 tiling is well
within these: the most measured is 1439 bytes, the corner chunk of a
Level-2A 60 m true-color image (1830 pixels in tiles of 256: an edge of 38,
so 48 empty tiles), and a band's are at most 4.3 kB in all, for band files
of 0.2–160 MB. An output key MUST be at most 65535 bytes and
MUST NOT start with `__vz__/` (§1.4). The browser implementation fails, as
for the other store profiles, on a listing of more than 100000 objects
(§14). A product has 60–1000 objects.

### 12.6 Chunks

The chunk of band array `<group>/<band>` at tile `t = v × nx + u`
([the convention §4](../safe.md#4-arrays)), with
`T = XTsiz`, `U = YTsiz`, `x0 = u × T`, `y0 = v × U`,
`w = min(T, Xsiz − x0)`, `h = min(U, Ysiz − y0)`, `a = ceil(T / w)` and
`b = ceil(U / h)`, is the concatenation of these ranges:

1. a literal: `FF 4F` (SOC), then the SIZ segment with `Rsiz = 0`,
   `Xsiz = x0 + T`, `Ysiz = y0 + U`, `XOsiz = x0`, `YOsiz = y0`,
   `XTsiz = w`, `YTsiz = h`, `XTOsiz = x0`, `YTOsiz = y0`, and its `Csiz`
   and component triples unchanged. (`x0 + T` and `y0 + U` MUST be at most
   2^32 − 1. `Rsiz` is 0 because a profile's restrictions on the tile size
   may not hold for the new tiles. The file's own is kept in the segment
   `siz` of the source metadata.)
2. the main header rest `R`, a shared byte string (§1.2): the whole of the
   data source that holds it;
3. a literal: the SOT segment `FF 90 00 0A`, `Isot = 0`, the tile-part's
   `Psot`, `00 01`;
4. the tile-part's body `[s_t + 12, s_t + Psot)`, a range of the band file
   (in the zip form, offset by its data start `ds`);
5. the **tail**: for `k = 1, …, a × b − 1`, an empty tile-part for the tile
   `k` (`i = k mod a` across, `j = k div a` down): `FF 90 00 0A`,
   `Isot = k`, `Psot = 14 + e_k`, `00 01`, then SOD `FF 93`, then `e_k`
   bytes `00`; then EOC `FF D9`. An interior chunk's tail is `FF D9`, a
   literal. An edge chunk's (`a × b > 1`) is a shared byte string (§1.2),
   like piece 2.

**Bounds.** A tail is `2 + Σ_k (14 + e_k)` bytes. Before it is made, its
length is computed (first `2 + 14 × (a × b − 1)`, then with each `e_k`),
and it MUST be at most 4096 bytes. The tails of all the chunks of a band
file MUST be at most the band file's size `n` in all, which is checked
before any chunk of it is made. (So an `e_k` above 2^32 − 15, whose `Psot`
would not fit, is rejected, and the output's literals and data sources stay
within the input's size.)

**Empty tiles.** The tile `k` covers `[ex0, ex1) × [ey0, ey1)`, with
`ex0 = x0 + i × w`, `ex1 = min(x0 + (i + 1) × w, x0 + T)`, and `ey0`,
`ey1` likewise from `y0`, `j`, `h` and `U`. Its `e_k` empty packets, each
the single byte `00` (a packet header whose first bit says the packet is
empty), are one per layer, component, resolution and precinct. With
`N_c` and `PPx_r`, `PPy_r` those of component `c` (§12.3):

```
e_k = Lay × Σ_c Σ_{r=0}^{N_c} nprec(ex0, ex1, N_c − r, PPx_r) × nprec(ey0, ey1, N_c − r, PPy_r)
nprec(z0, z1, d, e) = ceil(ceil(z1 / 2^d) / 2^e) − floor(ceil(z0 / 2^d) / 2^e)   when ceil(z1 / 2^d) > ceil(z0 / 2^d),
                      0 otherwise
```

(ISO/IEC 15444-1 B.5 and B.6: the resolution `r` of a tile-component
covers `[ceil(z0 / 2^(N_c − r)), ceil(z1 / 2^(N_c − r)))` and is cut into
precincts of `2^PP` anchored at 0.) This is the number of packets that
Kakadu writes for a tile of that area: it equals the number of packet
lengths in the PLT markers of every tile of the files examined in the
design of this profile, edge tiles included. Since Sentinel-2's tile sizes
are multiples of `2^N`, the empty tiles have the same structure as the
file's own edge tiles. The pixels of an empty tile decode to
`2^(p − 1)`, lie outside the array, and are not read.

So the chunk is a valid codestream with `a × b` tiles, of which tile 0 is
the file's tile `t` with its own coding unchanged. Its area on the
reference grid is the one it has in the file, so its packets decode
identically. An interior tile (`w = T`, `h = U`) has `a = b = 1` and no
empty tile. Chunk keys are `<group>/<band>/c/<v>/<u>`, or
`<group>/<band>/c/0/<v>/<u>` for a 3-component band.

**Data sources.** Piece 2 is the same in every chunk of a band, and in
every band file written with the same parameters (all the 15-bit bands of
one resolution, in the products examined). It is held once, in a data
source, so that reading a chunk reads only its tile-part from the band
file. Piece 2 sits at the start of the file, far from most tiles, so as a
range of the file it would cost a reader a separate request per chunk. An
edge chunk's tail is the same along an edge of a band, and in every band
of the same tiling and coding (2–5 distinct tails per band, in the products
examined), so it is held once too. The data sources are the distinct main
header rests and edge tails, numbered as §1.2 says. They are COD, QCD and
COM marker segments (coding parameters and the encoder's comments), and
empty tile-parts, not pixel data (§1.2, **Structure only**).

### 12.7 Metadata arrays and other objects

- **XML arrays.** The array `vzip_source/xml/<i>` holds the bytes of an
  XML document that the convention §6.3 keeps as an array (one of more than
  65536 bytes, or one past the budget), as an array of bytes (conventions
  §7): `k = ceil(len / 2^24)`
  chunks of `ceil(len / k)` bytes. Each chunk references its bytes in the
  object (the last padded with literal zero bytes), or, for a deflated
  entry of a zip file, holds them as copied bytes (an entry with bytes, not
  a reference).
- **JP2 headers.** The array `vzip_source/jp2/<group>/<band>` holds the JP2
  header `[0, c0)` of its band file, as an array of bytes. Each chunk
  references its bytes in the band file.
- **Other objects.** Each other object is the entry `vzip_source/objects/<k>`
  ([the convention §6.3](../safe.md#63-the-source-metadata-node)),
  which references the whole object: in the directory form, the range
  `(i, 0, size)` of its own source, and in the zip form `(0, ds, size)`.
  An empty object has no entry; its key is in `empty`.

The XML texts are in the documents (`vzip_source/zarr.json`). The
documents under `vzip_source/` are deflated and, like the chunks, not among
the documents a reader fetches when it opens the archive (conventions §2).

### 12.8 The source table

References are listed in ascending order of their entries' keys (as UTF-8
byte strings), and within a reference in order of its ranges. This order
numbers the data sources (§1.2's order of first use).

- **Zip form.** Source 0 is `U` (§1.2), and the data sources follow, from
  1.
- **Directory form.** The sources are first one `url` source per object
  that some reference uses (every band file, every XML document with an
  array, every other object), with a `size` pin, the object's listed size,
  and the object's URL (§1.4),
  in ascending order of the objects' keys, numbered from 0. The data
  sources follow, numbered from the count `m` of url sources in order of
  first use (§1.2's rule, starting at `m` rather than 1). This
  replaces §1.4's rule that a store input's sources are one per entry and
  that it has no data sources: a band file is used by many chunks, by its
  JP2 header array, and by nothing else.

### 12.9 Summary

This section is informative. Both implementations print a one-line JSON
summary: `level` (`"L1C"` or `"L2A"`), `form` (`"store"` or `"zip"`),
`groups` (resolution groups), `bands` (band arrays), `chunks`,
`edgeChunks` (chunks with empty tiles), `dataSources`, `objects` (the
product's objects, folder markers included), `folderMarkers`, `emptyDirs`,
`xmlText`,
`xmlArrays`, `otherObjects`, `emptyObjects`,
`tileParts` (SOT segments read), `listingRequests`, and `readRequests` and
`readBytes` (the range requests made, and the bytes they returned, when the
implementation counts them).

### 12.10 Cost

This section is informative. With no TLM marker in the band files (Kakadu
writes none for Sentinel-2), the tile-parts are found by following each
`Psot` to the next: one 12-byte read per tile, each depending on the one
before. A Level-1C product has about 3100 tiles (121 for each 10 m band,
81 for each 20 m band, 100 for each 60 m band, and 1849 in the 256 × 256
tiles of its 10 m TCI). A Level-2A product has about 3300 (its 36 band files
include every band at 60 m, and its TCI has 1024 × 1024 tiles). The band files
are independent, so a virtualizer reads them concurrently. A 64 KiB read
block holds several tile headers only when the tiles are small (masks,
mostly empty tiles). The output is about 3100–3300 chunk entries, 10–40 data
sources (main header rests and edge tails), and the product's 60–150 other entries.

Measured on the 12 accepted products of `corpus_safe.txt` (revision 17): a
product takes 857–3126 range requests and 1.6–22 MB of reads (the reads
after a small tile-part fetch a 64 KiB block, which holds the next headers;
after a larger one, 12 bytes). The archive is 1.0–1.13 MB, the root
`zarr.json` 3.2–4.0 KB, and the largest document, `vzip_source/zarr.json`,
at most 64.4 KB, within its budget. Level-1C products take longest: their
true-color image's 1849 tile-part headers are read one after another.
