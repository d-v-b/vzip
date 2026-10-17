# phkv: the perfect-hash binding of refkv

Format version: 0 (**provisional**) · Specification revision: 11.2 · Part 3
status: **draft**

This document is Part 3 of the refkv specification: the **perfect-hash
binding**, which keeps a store in a single file, a **phkv store**, whose keys
are found through a perfect hash in one hop.
[Part 1](../refkv.md), in spec/refkv.md, specifies the reference value type,
and its introduction ([§1](../refkv.md#1-introduction)) covers every part:
the conventions, the conformance of readers and writers, and the format
version and revisions. The sections are numbered as one specification, so this
part starts at [§16](#16-what-the-phkv-binding-defines).

Part 3 is a **draft**: unlike Parts 1 and 2, anything in it may still change,
and no conformance suite tests it yet.

A phkv store is either a file of its own (the **standalone** profile) or a
vzip archive ([Part 2](vzip.md)) that also carries a phkv container in a
hidden entry (the **hybrid** profile, [§21](#21-hybrid-profile)). A hybrid
store is a valid vzip archive, which readers that know only vzip read as
before.

# Part 3. The perfect-hash binding

## 16. What the phkv binding defines

A **phkv store** is a store kept in a single file, indexed by a perfect hash of
its keys. The phkv binding answers the three questions of
[§3.1](../refkv.md#31-what-a-binding-defines) as follows:

| | in a phkv store |
|---|---|
| marking | each key has a **record** ([§19](#19-records)); its `kind` field says whether the value is bytes, or a reference whose payload is a `Range` or a `Concat`. The record holds the reference payload itself. |
| source table | the SOURCES section ([§17.4](#174-sections)), found through the section directory, which the superblock locates ([§17.1](#171-superblock)). The superblock starts with the magic `PHKV` and records the format version. The binding's auxiliary data are its other sections: the records, the page index, the perfect hash, the slot table, properties and metadata. |
| names | a key is stored as its UTF-8 bytes, exactly. A standalone store reserves no names. A hybrid store reserves vzip's `__vz__/` prefix ([§21](#21-hybrid-profile)). |

What a phkv store adds over the ZIP binding:

- **One-hop lookup.** A reader holds the perfect hash's pilots in memory. To
  find a key it computes a slot, reads that slot's 8-byte record offset from
  the slot table, and reads the record, whose key it compares with the one
  asked for ([§19.5](#195-lookup)).
- **Sorted keys.** Records are stored in UTF-8 order with a page index, so
  `list(prefix)` and binary search work as in a paged vzip archive
  ([§19.4](#194-page-index)).
- **Properties.** Optional typed columns, such as modification times, hold
  data about keys outside the values ([§20](#20-properties)). They never
  change the result of a refkv operation.
- **Compression.** A bytes value may be stored as is, or compressed with
  DEFLATE or Zstandard ([§19.2](#192-bytes-records)).

### 16.1 Notation

All multi-byte integers are unsigned and little-endian unless stated
otherwise. `u8`, `u16`, `u32`, `u64` are unsigned integers of 8, 16, 32 and 64
bits. A **varint** is an unsigned LEB128 integer, as protobuf's VARINT
([§4.1](../refkv.md#41-encoding)): at most 10 bytes, with a value below
2^64. Writers write minimal varints; readers accept non-minimal ones.

- **XXH64** is the 64-bit xxHash of
  [the xxHash specification](https://github.com/Cyan4973/xxHash/blob/dev/doc/xxhash_spec.md),
  with a 64-bit seed.
- **CRC-32C** is the checksum of [§4.2](../refkv.md#42-range).
- `mulhi(a, m)` is `floor(a × m / 2^64)` for 64-bit `a` and `m`: the high 64
  bits of their 128-bit product. It maps `a` from `[0, 2^64)` to `[0, m)`.
- `×`, `+` and `⊕` (exclusive or) on 64-bit values wrap modulo 2^64 unless
  stated otherwise.

### 16.2 Profiles

- **Standalone:** the file holds only the store. Its layout is up to the
  writer, within the rules of [§17](#17-container);
  [§23.2](#232-recommended-layout-informative) recommends one.
- **Hybrid:** the file is a valid vzip archive, and the phkv container is the
  body of its hidden entry `__vz__/phkv` ([§21](#21-hybrid-profile)). Bytes
  values and the source table are the ZIP entries' bodies, which the
  container's records and directory point at, so nothing is stored twice.

Readers open both profiles the same way ([§22.1](#221-opening)). The
superblock's `hybrid` flag tells them which one a file is.

### 16.3 Comparison with the ZIP binding (informative)

The same store can be written three ways. The sizes are those of the
reference writers (rust/refkv-vzip, rust/refkv-phkv) for 100,000 single-range
references with keys of about 11 bytes; the vzip archive has a page index with
64 KiB pages.

| | vzip ([Part 2](vzip.md)) | phkv, standalone | phkv, hybrid |
|---|---|---|---|
| the file is | a ZIP archive | a phkv store | a ZIP archive and a phkv store |
| read by ZIP tools | yes | no | yes; `__vz__/phkv` is one more hidden entry |
| read by vzip readers | yes | no | yes, unchanged |
| read by phkv readers | no | yes | yes, through the container alone |
| found at open by | the end of central directory record, at one of two fixed offsets from the end | a superblock at a probe distance from the end ([§17.2](#172-finding-the-superblock)) | both |
| read at open | the end records, the source table, and the page index or the whole central directory | the superblock, directory, source table, page index and perfect hash ([§22.1](#221-opening)) | per reader, as in its own column |
| finding a key | without a page index: a map of the central directory built at open; with one: bisect the index, read one page (about 64 KiB here), scan it | compute a slot, read 8 bytes, read one record (one hop, [§19.5](#195-lookup)) | per reader |
| `list(prefix)` | the page index, or the central directory | the page index ([§19.4](#194-page-index)) | per reader |
| per-key cost | a 30-byte local header and a 46-byte central directory record, each with the key; a reference's payload in the record's extra field, and again in the body when mirrored | a record (about 6 bytes plus the key and payload), an 8-byte slot, about 0.34 bytes of pilot, and the page index's share | the vzip archive's, plus the container's records and slots |
| measured size | 122 bytes per key | 37 bytes per key | 172 bytes per key |
| bytes values | STORED or DEFLATE | none, DEFLATE or Zstandard, optionally inline in the record | STORED or DEFLATE (ZIP methods 0 and 8) |
| value checksum | the CRC-32 of every entry (readers need not check it) | an optional CRC-32C per value, which readers check whenever they have the whole value ([§19.2](#192-bytes-records)) | both |
| per-key properties | none (readers ignore ZIP timestamps and attributes) | typed, optional columns ([§20](#20-properties)) | as standalone, for phkv readers |
| reserved names | `__vz__/` | none | `__vz__/` |
| reference payload limit | 65,519 bytes | 2^24 bytes | 65,519 bytes (the ZIP side's) |
| `raw(key)` of a compressed value | the inflated body | the stored bytes, with `entry_info` to interpret them | the inflated body, as vzip |
| a writer must | sort the central directory, if it writes a page index | sort the records, build the perfect hash | both, and size the container's padding to the central directory |
| status | provisional; the conformance suite tests it | draft | draft |

Which to write:

- **vzip**, for the widest compatibility, and when the archive will be read
  by ZIP tools or by readers that know only vzip.
- **standalone phkv**, for the fastest lookups and the smallest file, when
  every reader knows phkv: about a third the size of the vzip archive above,
  with one hop per key instead of a page read.
- **hybrid**, to keep vzip readers and ZIP tools working while phkv readers
  get one-hop lookups and properties. It costs the most space, since the
  container's records repeat what the central directory already says.

## 17. Container

### 17.1 Superblock

The superblock is 64 bytes:

| offset | field | type | value |
|---|---|---|---|
| 0 | `magic` | 4 bytes | ASCII `PHKV` |
| 4 | `version` | `u16` | the format version ([§1.3](../refkv.md#13-versioning)): 0 |
| 6 | `flags` | `u16` | bit 0: `hybrid`. Other bits are 0. |
| 8 | `distance` | `u64` | `D`, the superblock's distance from the end of the file ([§17.2](#172-finding-the-superblock)) |
| 16 | `key_count` | `u64` | `n`, the number of records ([§19](#19-records)) |
| 24 | `directory_offset` | `u64` | the offset of the section directory |
| 32 | `directory_length` | `u64` | its length in bytes, a multiple of 40 |
| 40 | `region_offset` | `u64` | the offset of the container region (below) |
| 48 | `region_length` | `u64` | its length |
| 56 | `directory_crc32c` | `u32` | the CRC-32C of the directory |
| 60 | `superblock_crc32c` | `u32` | the CRC-32C of bytes 0–59 of the superblock |

All offsets in a phkv store are absolute file offsets.

The **container region** is the bytes the container occupies:

- in a hybrid store, the body of the entry `__vz__/phkv`
  ([§21](#21-hybrid-profile)), padding included;
- in a standalone store, from `region_offset` (0, unless the writer placed the
  store after other data) to the end of the superblock.

Readers use it only for `raw("__vz__/phkv")` in a hybrid store
([§22.2](#222-operations)).

### 17.2 Finding the superblock

The superblock ends `D` bytes before the end of the file: it occupies
`[file_size − D − 64, file_size − D)`. `D` is one of the **probe distances**:
0, and 2^k for each k from 10 to 30 (1 KiB, 2 KiB, 4 KiB, …, 1 GiB). The bytes
after the superblock do not belong to the store, and readers do not read them;
in a hybrid store they are padding and the ZIP central directory and end
records ([§21](#21-hybrid-profile)). In the manner of HDF5's user block,
this lets a store live where its last bytes are not its own. A standalone
writer usually writes the superblock last (`D` = 0), but may reserve `D` bytes
after it for data that is not the store's, such as a signature added later.

A reader finds the superblock as follows. For each probe distance `D`, in
increasing order, with `D + 64 ≤ file_size`, the 64 bytes at
`file_size − D − 64` are a **match** if:

1. they start with the magic `PHKV`;
2. bytes 60–63 hold the CRC-32C of bytes 0–59; and
3. `distance` equals `D`.

The first match is the superblock. If none matches, the file is not a phkv
store. Once a match is found, a reader does not probe further: if its
`version` is not one the reader implements, or `flags` has a bit set other
than bit 0, the store is of an unsupported version. Either way, opening fails
with an archive error ([§6.4](../refkv.md#64-errors)).

The three conditions together make a false match vanishingly rare: random
bytes match with probability about 2^-64 per candidate, and a superblock
written for another distance does not match at this one. A reader reads the
last 64 KiB + 64 bytes first, which holds every candidate up to 64 KiB, and
reads 64 bytes per larger candidate.

### 17.3 Section directory

The directory is a sequence of 40-byte entries, `directory_length / 40` of
them, each describing one **section**:

| offset | field | type | value |
|---|---|---|---|
| 0 | `type` | `u32` | the section type ([§17.4](#174-sections)) |
| 4 | `codec` | `u8` | how the section's bytes are stored ([§17.5](#175-codecs)) |
| 5 | `flags` | `u8` | bit 0: `must_understand`. Other bits are 0. |
| 6 | reserved | `u16` | 0 |
| 8 | `offset` | `u64` | the offset of the section's stored bytes |
| 16 | `length` | `u64` | their length |
| 24 | `size` | `u64` | the length of the section once decoded; equal to `length` for codec 0 |
| 32 | `crc32c` | `u32` | the CRC-32C of the stored bytes |
| 36 | reserved | `u32` | 0 |

The directory MUST lie within the file and match `directory_crc32c`. Each
section's stored bytes MUST lie within the file. Sections MAY overlap other
data of the file: in a hybrid store the SOURCES section is the body of
`__vz__/sources` ([§21](#21-hybrid-profile)).

A reader skips an entry whose `type` it does not know, unless the entry's
`must_understand` flag is set, which is an archive error. Every section is
immutable once written: no rule of this part requires a writer to change a
section after it has written the directory, so that a future version may
append to a store without rewriting it.

### 17.4 Sections

| type | name | codecs | how many | holds |
|---|---|---|---|---|
| 1 | SOURCES | any | exactly one | the source table, a `SourceTable` ([§5](../refkv.md#5-source-table)) |
| 2 | RECORDS | 0 | one if `n ≥ 1`, else none | the records, in key order ([§19](#19-records)) |
| 3 | PAGES | any | one if `n ≥ 1`, else none | the page index ([§19.4](#194-page-index)) |
| 4 | PTRHASH | any | one if `n ≥ 1`, else none | the perfect hash's parameters and pilots ([§18.1](#181-ptrhash-section)) |
| 5 | SLOTS | 0 | one if `n ≥ 1`, else none | the slot table ([§18.2](#182-slots-section)) |
| 6 | PROPERTY | 0 | any number | one property's values ([§20.1](#201-property-sections)) |
| 7 | METADATA | any | at most one | the store's own metadata ([§20.3](#203-metadata-section)) |

Types 0 and 8 to 0x7FFFFFFF are reserved for future versions. Types
0x80000000 and above are for private use: a writer MAY write them, and readers
that do not know them skip them (or fail, if `must_understand` is set).

A known section in the wrong number, or with a codec its row does not allow,
is an archive error.

The sections a reader reads **whole** are SOURCES, PAGES, PTRHASH and
METADATA. It reads them at open ([§22.1](#221-opening)), decodes them, and
checks their `crc32c` and `size`. The others are read piece by piece and are
never decoded, so their `crc32c` is checked only by a reader that happens to
read one whole (which it MAY do).

### 17.5 Codecs

A **codec** says how stored bytes encode a sequence of `size` bytes:

| codec | name | stored bytes |
|---|---|---|
| 0 | none | the bytes themselves: `length` equals `size` |
| 1 | deflate | one raw DEFLATE stream (RFC 1951), without a zlib or gzip wrapper, that ends exactly at the end of the stored bytes |
| 2 | zstd | one Zstandard frame (RFC 8878) that ends exactly at the end of the stored bytes; skippable frames are not allowed |

Codecs 3 to 255 are reserved. Stored bytes **decode cleanly** if they are
exactly as the table says and decode to exactly `size` bytes. As for a vzip
DEFLATE body ([§13.1](vzip.md#131-opening)), readers must check explicitly
that nothing follows the stream, and SHOULD stop decoding once the output
passes `size`. A Zstandard frame MAY omit its content size; a reader then
relies on `size`.

The codecs apply both to sections ([§17.3](#173-section-directory)) and to
bytes values ([§19.2](#192-bytes-records)).

## 18. Perfect hash

A store with `n ≥ 1` keys has a perfect hash: a function, computed from the
key and the PTRHASH section, that gives every key of the store a slot of its
own. It is PtrHash's query (Groot Koerkamp, 2025) with three choices fixed:
XXH64 as the key hash, one byte of pilot per bucket, and a slot table in place
of PtrHash's remap, so that the hash need not be minimal.

### 18.1 PTRHASH section

Decoded, the PTRHASH section is:

| offset | field | type | value |
|---|---|---|---|
| 0 | `hash_id` | `u8` | 1: XXH64. Other values are reserved. |
| 1 | reserved | 7 bytes | 0 |
| 8 | `seed` | `u64` | the XXH64 seed |
| 16 | `parts` | `u64` | `P`, the number of parts |
| 24 | `slots` | `u64` | `S`, the slots per part |
| 32 | `buckets` | `u64` | `B`, the buckets per part |
| 40 | `pilots` | `P × B` bytes | the pilot of each bucket, part by part |

The section is **malformed**, an archive error, if:

- `hash_id` is not 1, or a reserved byte is not 0;
- `P`, `S` or `B` is 0;
- `P × S` or `P × B` exceeds 2^64 − 1, or `P × S` is less than `n`;
- its size is not `40 + P × B`.

### 18.2 SLOTS section

The SLOTS section is `P × S` slots of 8 bytes. Slot `i` holds either a
`u64` offset of a record ([§19.1](#191-record-encoding)), or 2^64 − 1 for an
empty slot. Its length MUST be `8 × P × S`, else it is an archive error. The
offset SHOULD be a multiple of 8, for readers that map the file.

### 18.3 Query

The slot of a key `k`, a sequence of UTF-8 bytes, is computed as follows:

1. `h = XXH64(k, seed)`.
2. `p = mulhi(h, P)`: the part.
3. `b = mulhi(h × P, B)`, where `h × P` is taken modulo 2^64: the bucket
   within the part. (`h × P mod 2^64` is the fraction of `h` that `mulhi`
   left over, so `b` is independent of `p`.)
4. `pilot = pilots[p × B + b]`.
5. `x = fmix64(h ⊕ (pilot × 0x517CC1B727220A95))`, where `fmix64` is
   MurmurHash3's 64-bit finalizer:

   ```
   x ^= x >> 33;  x *= 0xFF51AFD7ED558CCD;
   x ^= x >> 33;  x *= 0xC4CEB9FE1A85EC53;
   x ^= x >> 33
   ```
6. `s = mulhi(x, S)`: the slot within the part.
7. The key's slot is `p × S + s`.

In a valid store, the slot of every record's key holds that record's offset,
and every other slot is empty. Appendix C gives test vectors.

### 18.4 Construction (informative)

The specification requires only the property of [§18.3](#183-query); how a
writer finds a seed and pilots is its own choice. The reference writer
(rust/refkv-phkv) does as PtrHash does:

- **Parameters.** `P = ceil(n / 65536)`. Then with `m` the number of keys in
  the fullest part, `S = ceil(m / 0.98)` and `B = ceil(m / 3)`: about 2.7 bits
  of pilot per key, and about 2–3% of slots empty.
- **Search.** It tries seeds 0, 1, 2, … in turn. For each part, it places
  buckets largest first. A bucket takes the first pilot, from 0 to 255, that
  puts its keys in distinct free slots. If no pilot does, it takes the pilot
  that collides with the fewest keys, counting a bucket of `c` keys as
  `c²`, and evicts the buckets it collides with, which go back in the queue.
  It never evicts any of the 16 buckets it placed most recently. If the
  evictions pass a bound, it gives up on that seed.

With these parameters seed 0 almost always succeeds. Two equal keys can never
be placed, but a writer rejects duplicate keys first
([§23.1](#231-requirements)).

## 19. Records

### 19.1 Record encoding

The RECORDS section is the concatenation of `n` records, sorted in strictly
increasing UTF-8 order of their keys, with nothing between them. A record is:

| field | type | present | value |
|---|---|---|---|
| `key_length` | varint | always | the length of `key`, at least 1 |
| `kind` | `u8` | always | 0: bytes; 1: a reference whose payload is a `Range`; 2: a reference whose payload is a `Concat` |
| `flags` | `u8` | always | bit 0: `inline`; bit 1: `crc32c`; bit 2: `hidden`. Other bits are 0. |
| `rank` | varint | always | the record's position in the RECORDS section: 0 for the first |
| `codec` | `u8` | kind 0 | the value's codec ([§17.5](#175-codecs)) |
| `offset` | varint | kind 0, not `inline` | the offset of the value's stored bytes |
| `stored_length` | varint | kind 0 | the length of the stored bytes |
| `size` | varint | kind 0 | the value's size |
| `crc32c` | `u32` | kind 0 with `crc32c` | the CRC-32C of the value (decoded) |
| `payload_length` | varint | kinds 1 and 2 | the length of the payload |
| `key` | `key_length` bytes | always | the key, in UTF-8 |
| `data` | `stored_length` bytes | kind 0 with `inline` | the value's stored bytes |
| `payload` | `payload_length` bytes | kinds 1 and 2 | the reference payload ([§4.4](../refkv.md#44-reference-payloads)) |

The fields come in this order. The fixed part comes before the key, so a
reader that reads a few hundred bytes at a record's offset usually gets the
whole record, and can compare its key and find its value in one read. A record
is self-contained: its key is never compressed against another record's, so
any record decodes from its own offset.

`inline`, `crc32c` and `codec` are for bytes records only: a reference record
with `inline` or `crc32c` set is malformed. `hidden` is for the hybrid profile
only ([§21](#21-hybrid-profile)): a standalone store has no hidden record.

A record is **malformed** if a field does not decode (a truncated or
overlong varint), it extends past the end of the RECORDS section, `kind` or
`codec` is not one of the values above, `flags` breaks the rules above,
`key_length` is 0, or the key is not valid UTF-8.

### 19.2 Bytes records

The value of a kind-0 record is its stored bytes, decoded with its codec. The
stored bytes are the record's `data` if `inline` is set, and otherwise the
`stored_length` bytes at `offset`, which MUST lie within the file. A writer
places them where it likes: in the standalone profile, typically before the
RECORDS section; in the hybrid profile, in the body of the key's ZIP entry.

If `crc32c` is set, the record's `crc32c` field is the CRC-32C of the value.
A reader MUST check it whenever it has the whole value: on decoding a
compressed value (always whole), and on reading the whole of a codec-0 value.
A reader that reads only part of a codec-0 value does not check it.

Writers SHOULD set `crc32c` and SHOULD inline small values that readers fetch
together with their keys, such as Zarr metadata documents.

### 19.3 Reference records

The value of a kind-1 or kind-2 record is described by its payload, a `Range`
(kind 1) or a `Concat` (kind 2), and resolved by
[§6.3](../refkv.md#63-resolving-a-reference). Writers MUST use kind 1 when a
reference has exactly one range ([§4.4](../refkv.md#44-reference-payloads)).

A payload MUST NOT exceed 16,777,216 bytes (2^24); a reader treats a longer
one as malformed (a payload error).

### 19.4 Page index

The PAGES section lets a reader find the records of a range of keys without a
perfect hash. Decoded, it is a sequence of **pages**, each:

| field | type | value |
|---|---|---|
| `first_key_length` | varint | at least 1 |
| `first_key` | `first_key_length` bytes | the key of the page's first record |
| `offset` | varint | the offset of the page's first record |
| `length` | varint | the length of the page, at least 1 |

The pages partition the RECORDS section: the first page starts at the
section's offset, each later page starts where the previous one ends, and
the last ends where the section ends. Each page holds whole records. Writers
SHOULD make pages of about 4 KiB.

The page index is **malformed**, an archive error, if it does not decode, a
`first_key` or `length` is 0, the pages are not contiguous from the start of
RECORDS to its end, or the `first_key` values are not strictly increasing in
UTF-8 order.

A page **cannot be parsed** if it is not a sequence of whole, well-formed
records that exactly fills it, or its first record's key is not its
`first_key`.

### 19.5 Lookup

A reader determines whether a key `k` has a record as follows:

1. If `n = 0`, `k` has none.
2. Compute `k`'s slot ([§18.3](#183-query)) and read the slot's 8 bytes. If
   the slot is empty (2^64 − 1), `k` has no record.
3. The slot's offset MUST lie within the RECORDS section, and the record
   there MUST be well-formed ([§19.1](#191-record-encoding)); otherwise it is
   an entry error for `k`, since the reader cannot tell whether `k` is
   present.
4. If the record's key is `k`, that is `k`'s record. Otherwise `k` has none.

A reader MAY instead find `k` through the page index, by selecting the last
page whose `first_key` is at most `k` and searching it, as in a paged vzip
archive ([§12.2](vzip.md#122-lookup)). In a valid store both give the same
result.

## 20. Properties

Properties are data about keys that are not part of their values, such as
modification times. They are optional, a reader need not support them, and
they never change the result of `classify`, `get`, `list` or `raw`.

### 20.1 PROPERTY sections

Each PROPERTY section holds one property, a column of values indexed by
record rank. It is:

| offset | field | type | value |
|---|---|---|---|
| 0 | `name_length` | `u32` | the length of `name`, at least 1 |
| 4 | `value_type` | `u8` | 1: `u64`; 2: `i64` (two's complement); 3: bytes; 4: UTF-8 string |
| 5 | reserved | 3 bytes | 0 |
| 8 | `name` | `name_length` bytes | the property's name ([§20.2](#202-property-names)), in UTF-8, then zero bytes to a multiple of 8 |
| … | `present` | `ceil(n / 8)` bytes | bit `r mod 8` (least significant first) of byte `floor(r / 8)` is set if the record of rank `r` has a value; then zero bytes to a multiple of 8 |
| … | `values` | | types 1 and 2: `n` values of 8 bytes; types 3 and 4: `n + 1` `u64` offsets, then the bytes they index |

For types 3 and 4, value `r` is bytes `[off[r], off[r + 1])` of the bytes
after the offsets; `off[0]` is 0, and the offsets do not decrease. A record
without a value has a 0 (types 1 and 2) or an empty range (types 3 and 4) in
its place. A type-4 value MUST be valid UTF-8.

The section is **malformed** if its length is not what its fields imply, a
value type is not one of the above, a reserved byte is not 0, or the offsets
are out of order or past its end. A malformed PROPERTY section is an error of
the property operation that reads it ([§22.2](#222-operations)), not of the
store.

### 20.2 Property names

A name without a `:` is a **registered** name. This part registers:

| name | type | value |
|---|---|---|
| `mtime` | `i64` | the value's last modification time, in nanoseconds since the Unix epoch (UTC) |
| `mode` | `u64` | POSIX permission bits (`st_mode & 0o7777`) |
| `media_type` | UTF-8 string | the value's media type (RFC 6838), such as `application/json` |

A registered name with a different type is malformed. Every other name
contains a `:`, and SHOULD start with a domain or URI that its author controls,
such as `org.example:checksum`. A store MUST NOT have two PROPERTY sections
with the same name.

### 20.3 METADATA section

The METADATA section holds data about the store as a whole. Decoded, it is a
sequence of entries, each a varint name length, the name (UTF-8, at least one
byte, named as in [§20.2](#202-property-names)), a varint value length and the
value's bytes, in strictly increasing UTF-8 order of name. This part registers
one name, `writer`: the name and version of the program that wrote the store,
in UTF-8. A METADATA section that does not decode, or breaks these rules, is
an archive error.

## 21. Hybrid profile

A hybrid store is a vzip archive ([Part 2](vzip.md)) that is also a phkv
store:

1. **The entry.** The archive has a hidden STORED entry `__vz__/phkv`, the
   last entry before the central directory. Its body is the container
   region: the phkv sections that are not other entries' bodies, the
   directory and the superblock, followed by zero bytes. `region_offset` and
   `region_length` are its body offset and size.
2. **Distance.** The zero bytes make the superblock end at a probe distance
   `D` from the end of the file ([§17.2](#172-finding-the-superblock)): `D`
   is the zero bytes, the central directory and the end records. A writer
   picks the smallest probe distance at least as large as the central
   directory and end records, and pads to it.
3. **Records.** The store has one record for every entry of the archive
   except `__vz__/phkv`, so its `n` counts hidden entries too. A record has
   `hidden` set if and only if its key starts with `__vz__/`.
   - A bytes entry has a kind-0 record, not `inline`, with codec 0 (method 0)
     or 1 (method 8), whose `offset`, `stored_length` and `size` are the
     entry's body offset, compressed size and uncompressed size.
   - A reference entry has a kind-1 (extra field 0x7A76) or kind-2 (0x7A77)
     record with the same payload. Its body MUST be byte-identical to the
     payload ([§11.3](vzip.md#113-reference-entries) allows an empty body;
     this profile does not).
4. **The source table.** The SOURCES section is the body of
   `__vz__/sources`: its directory entry has codec 1 and the entry's body
   offset and compressed size.
5. **Agreement.** The phkv operations ([§22.2](#222-operations)) give the
   same results as vzip's ([§13.2](vzip.md#132-operations)) for every key.

A reader that knows only vzip sees `__vz__/phkv` as one more hidden entry,
and reads the archive as before. A phkv reader never reads the ZIP structures.

Bytes values cannot use codec 2 (Zstandard) in a hybrid store, since a vzip
archive allows only methods 0 and 8 ([§10.1](vzip.md#101-structure)).

## 22. Reading a phkv store

### 22.1 Opening

Opening a phkv store ([§6.1](../refkv.md#61-opening)) reads and checks the
following. Any failure is an archive error:

- the superblock, found as in [§17.2](#172-finding-the-superblock), of format
  version 0 with no unknown flags;
- the directory: within the file, matching `directory_crc32c`, a whole number
  of 40-byte entries with reserved fields 0 and no unknown flags, and no
  unknown section with `must_understand` set;
- the sections, as [§17.4](#174-sections) requires them by type, count and
  codec, each within the file;
- the sections read whole (SOURCES, PAGES, PTRHASH and METADATA): their
  stored bytes match `crc32c` and decode cleanly to `size` bytes
  ([§17.5](#175-codecs)), within the limit for data read at open
  ([§6.7](../refkv.md#67-reader-policy) rule 6);
- the source table, valid as [§5](../refkv.md#5-source-table) requires; the
  page index and the PTRHASH section, not malformed
  ([§19.4](#194-page-index), [§18.1](#181-ptrhash-section)); the SLOTS
  section's length ([§18.2](#182-slots-section)); and the METADATA section, if
  there is one ([§20.3](#203-metadata-section)).

A reader MAY read more, but MUST NOT report an error at open that this
section and [§6.1](../refkv.md#61-opening) do not list. In particular,
records, slots and PROPERTY sections are not read at open.

### 22.2 Operations

On a phkv store, the operations of [§6.2](../refkv.md#62-operations) work as
follows.

- **classify(key)** finds the key's record ([§19.5](#195-lookup)), and returns
  its kind: `bytes` for kind 0, `reference` for kinds 1 and 2. A key without a
  record, or whose record is `hidden`, is *missing*.
- **get(key, request)**, for a bytes record, returns the requested window of
  its value ([§19.2](#192-bytes-records)). A codec-0 value is read only within
  the window; a compressed one is decoded whole. A reference is resolved
  ([§6.3](../refkv.md#63-resolving-a-reference)). A key without a record, or
  whose record is `hidden`, is *missing*.
- **list(prefix)** returns the keys of the records that are not `hidden` and
  start with `prefix`, in UTF-8 order. It reads the pages whose key range may
  hold such a key, with the rule of [§13.2](vzip.md#132-operations), and fails
  with an entry error if one of them cannot be parsed
  ([§19.4](#194-page-index)).
- **raw(key)**, in a standalone store, returns a bytes record's **stored**
  bytes, not decoded, and a reference record's payload. It does not decode
  the stored bytes, so the only body errors it reports are stored bytes
  outside the file and, for a codec-0 value with `crc32c` set, a checksum
  mismatch. In a hybrid store it returns what vzip's raw view returns
  ([§13.2](vzip.md#132-operations)): a bytes record's value, decoded; a
  reference record's payload (which is its entry's body,
  [§21](#21-hybrid-profile)); the same for `hidden` records; and for
  `__vz__/phkv`, the container region. In either profile a key without a
  record is *missing*.

A phkv reader also provides two operations of its own, which are not refkv
operations and look up keys as raw does, `hidden` records included:

- **entry_info(key)** → *missing*, or the record's kind; for a bytes record,
  also its codec, `stored_length`, `size`, and `crc32c` if present; for a
  reference, its `payload_length`. With it, a caller can interpret what raw
  returns.
- **property(key, name)** → *missing* (no such key), *absent* (no PROPERTY
  section named `name`, or no value for this record), or the record's value,
  typed by the section's `value_type`. It fails if the section is malformed
  ([§20.1](#201-property-sections)) or the record's `rank` is not less than
  `n`.

### 22.3 Errors

In a phkv store, the error classes of [§6.4](../refkv.md#64-errors) have these
causes, besides those of Part 1:

| class | causes in a phkv store |
|---|---|
| **archive error** | everything [§22.1](#221-opening) checks |
| **entry error** | the slot of the key points outside the RECORDS section, or at a malformed record ([§19.5](#195-lookup)); for list, a page that cannot be parsed ([§19.4](#194-page-index)) |
| **body error** | a bytes value's stored bytes lie outside the file (judged as a whole, even for a small window); compressed stored bytes do not decode cleanly ([§17.5](#175-codecs)); a codec-0 value whose `stored_length` and `size` differ; a `crc32c` mismatch ([§19.2](#192-bytes-records)) |
| **payload error** | a payload longer than 2^24 bytes ([§19.3](#193-reference-records)) |

In the order of checks of [§6.4](../refkv.md#64-errors), step 3 is the
lookup of [§19.5](#195-lookup) and the record's entry error, and step 4 reads
the stored bytes.

### 22.4 Violations readers need not detect

For stores that break these writer requirements, reader results are
unspecified (besides those of
[§6.6](../refkv.md#66-violations-readers-need-not-detect)):

- [§17.3](#173-section-directory): the `crc32c` of a section the reader does
  not read whole.
- [§18.3](#183-query) and [§19.1](#191-record-encoding): whether the slot
  table matches the records (a key that only the page index finds, or two
  slots with one record); whether records are sorted and their ranks are
  their positions.
- [§19.4](#194-page-index): whether the pages match the records beyond the
  checks there.
- [§20](#20-properties): duplicate PROPERTY names, and whether a value matches
  its registered meaning.
- [§21](#21-hybrid-profile): whether a hybrid store's container matches its
  ZIP structures. A phkv reader reads only the container; a vzip reader only
  the ZIP structures.
- [§17.1](#171-superblock): `region_offset` and `region_length`, except in
  `raw("__vz__/phkv")`.

## 23. Writing a phkv store

### 23.1 Requirements

A writer of a phkv store follows [§7](../refkv.md#7-writing). It MUST also
reject its input, producing no store, if:

- a reference payload exceeds 2^24 bytes ([§19.3](#193-reference-records));
- in the hybrid profile, a key is `__vz__/phkv`, or the input breaks a
  requirement of the ZIP binding ([§14.1](vzip.md#141-requirements));
- in the hybrid profile, a bytes value would use codec 2
  ([§21](#21-hybrid-profile)).

It writes the perfect hash of [§18](#18-perfect-hash) for every store with a
key; a writer that finds no seed and pilots for its keys fails.

### 23.2 Recommended layout (informative)

```
[bytes values]          not inline, in key order
[RECORDS]               in key order
[SLOTS]                 8-byte aligned
[PROPERTY sections]     8-byte aligned
[PAGES]
[PTRHASH]
[SOURCES]
[METADATA]
[section directory]
[superblock]            D = 0
```

With this layout, what a reader reads at open is at the end of the file. It
reads the last 64 KiB, which holds the superblock and directory, and then the
range from PAGES to the directory, if the first read did not cover it. Each
`get` then reads one slot and one record, plus the value if it is not inline.

### 23.3 File names

A standalone store SHOULD be named with the extension `.phkv`. A hybrid store
is a vzip archive, and SHOULD be named `.vzip`
([§14.3](vzip.md#143-archive-file-names)). Readers MUST NOT depend on the file
name.

## 24. Security considerations for phkv

- Every offset and length in a phkv store comes from the file. Readers MUST
  check that each region lies within the file before reading it, and MUST
  NOT allocate for a section, record or value more than the limits of
  [§6.7](../refkv.md#67-reader-policy) and
  [§8](../refkv.md#8-security-considerations) allow. In particular, the
  PTRHASH section is read at open, so its size is bounded by rule 6, which
  also bounds the number of keys whose store a reader will open.
- A compressed value can expand greatly. Readers stop decoding at its `size`
  ([§17.5](#175-codecs)).
- A key's slot is computed from its bytes with a public hash and a seed
  stored in the file. Someone who chooses the keys can make construction
  slow (by choosing keys that collide), but cannot make lookup slower than
  one slot and one record.

## Appendix C: Constants and test vectors

| name | value | defined by |
|---|---|---|
| superblock magic | `PHKV` (4 bytes) | phkv binding |
| superblock size | 64 | phkv binding |
| probe distances | 0, 2^10 … 2^30 | phkv binding |
| directory entry size | 40 | phkv binding |
| hash_id XXH64 | 1 | phkv binding |
| pilot multiplier | 0x517CC1B727220A95 | phkv binding |
| empty slot | 2^64 − 1 | phkv binding |
| payload limit | 2^24 | phkv binding |
| hybrid entry | `__vz__/phkv` | phkv binding |

**XXH64** ([§16.1](#161-notation)):

| input | seed | XXH64 |
|---|---|---|
| empty | 0 | 0xEF46DB3751D8E999 |
| `abc` | 0 | 0x44BC2CF5AD770999 |
| `zarr.json` | 1 | 0xB7C230675AB7BA48 |

**fmix64** ([§18.3](#183-query) step 5): `fmix64(0) = 0`;
`fmix64(1) = 0xB456BCFC34C2CB2C`;
`fmix64(0x0123456789ABCDEF) = 0x87CBFBFE89022CEA`.

**mulhi**: `mulhi(0x8000000000000000, 10) = 5`;
`mulhi(0xFFFFFFFFFFFFFFFF, 7) = 6`.

**Query** ([§18.3](#183-query)). A PTRHASH section with `seed` 0, `P` = 1,
`S` = 6, `B` = 2 and pilots `[1, 3]` places five keys as follows:

| key | `h` | bucket | pilot | slot |
|---|---|---|---|---|
| `zarr.json` | 0xABE4EB424F90BC87 | 1 | 3 | 2 |
| `a/zarr.json` | 0xD620761F49F4923C | 1 | 3 | 3 |
| `a/c/0` | 0xE9D3CD35D106A3DD | 1 | 3 | 4 |
| `a/c/1` | 0xAF20BBFEDC2FC6A0 | 1 | 3 | 1 |
| `b` | 0x78452AA11AF39F9B | 0 | 1 | 5 |

Slot 0 is empty. These are the parameters and pilots the reference writer
finds for these five keys ([§18.4](#184-construction-informative)).
