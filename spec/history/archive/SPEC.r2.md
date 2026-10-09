# vzip: a ZIP container for byte-range references

Version: 1 (draft, revision 2)

## 1. Introduction

A vzip archive is a ZIP file that stores a key-value mapping in which every
value is either

- **bytes**: stored in the archive, or
- a **reference**: a description of how to assemble the value from byte ranges
  of other objects (external objects named by URL, other entries of the same
  archive, or literal bytes).

The motivating use is "virtual" Zarr datasets, whose chunks live inside other
files (HDF5, netCDF, TIFF, ...). Nothing in this specification depends on
Zarr; keys are opaque strings.

A vzip archive is always a valid ZIP file. A ZIP reader that knows nothing
about vzip sees every key as an ordinary file. For a reference, that file
contains the encoded reference (or nothing) rather than the referenced bytes.

### 1.1 Conventions

The key words MUST, MUST NOT, REQUIRED, SHOULD, SHOULD NOT, MAY, and OPTIONAL
are to be interpreted as described in RFC 2119.

All multi-byte integers in ZIP structures and in the archive comment are
unsigned little-endian. `u16`, `u32`, `u64` denote unsigned integers of 16,
32, 64 bits.

"APPNOTE" means the PKWARE .ZIP File Format Specification, version 6.3.10 or
later. Where this document is silent about ZIP structures, APPNOTE applies.

Byte ranges are written `[a, b)`: start inclusive, end exclusive. Strings are
compared by their UTF-8 bytes, lexicographically ("UTF-8 order").

### 1.2 Conformance

This document places requirements on **writers** (programs that create
archives) and **readers** (programs that read them). A **valid archive**
satisfies every requirement placed on writers.

Reader behaviour is fully specified for valid archives, and for the invalid
archives that §8 classifies. §8.6 lists the writer violations that readers
are not required to detect; for those, the results are unspecified.

## 2. Data model

An archive defines a partial function from **keys** to **values**:

- A **key** is a non-empty Unicode string, stored as UTF-8 (§3.3).
- A **value** is a finite byte sequence (possibly empty).

Each key present in the archive has a **kind**:

| kind        | value                                                         |
|-------------|---------------------------------------------------------------|
| `bytes`     | the (decompressed) body of the key's ZIP entry                |
| `reference` | the bytes described by the entry's reference payload (§5)     |

A key that is not present has kind `missing` and no value. Asking for a
missing key is not an error.

Keys beginning with the **reserved prefix** `__vz__/` are **hidden**. They
hold data used by the format itself or by references (§6, §7), and they are
not part of the archive's public mapping (§8.2). Two hidden keys are defined
by this specification and are written by the writer itself: `__vz__/sources`
(§6) and `__vz__/index` (§7). They are the **format entries**.

## 3. ZIP container

### 3.1 Structure

A valid archive is a single-disk ZIP archive conforming to APPNOTE that
also satisfies the following:

1. The archive begins at file offset 0. There is no prepended data, and all
   offsets in this document are absolute file offsets.
2. Every entry has exactly one local file header and one central directory
   record, and no two central directory records have the same file name.
3. Every entry uses compression method 0 (STORED) or 8 (DEFLATE). DEFLATE
   bodies are raw DEFLATE streams (RFC 1951) with no zlib or gzip wrapper.
4. Every local file header has an extra field length of 0. The body of an
   entry therefore starts at `local_header_offset + 30 + file_name_length`,
   computed from the central directory record alone (the **body offset**).
5. No entry is encrypted (general purpose bit 0 clear), and general purpose
   bit 3 (data descriptor) is clear. General purpose bit 11 (UTF-8 file
   names) is set in both headers of every entry.
6. The CRC-32, compressed size and uncompressed size in both headers are
   correct for the entry's body.
7. Every entry's compressed and uncompressed sizes are less than 0xFFFFFFFF.
   Entries of 4 GiB or more cannot be stored, because they would need a ZIP64
   extra field in the local header, which rule 4 forbids.
8. Central directory records have no file comment (comment length 0).
9. The archive has no central directory encryption, digital signature or
   archive extra data record.
10. The archive comment is the vzip comment (§3.4).

### 3.2 ZIP64

A writer MUST use ZIP64 structures exactly where a value is too large for its
field, and nowhere else:

- In a central directory record, a local header offset of 0xFFFFFFFF or more
  is stored as 0xFFFFFFFF, with the actual value in a ZIP64 extended
  information extra field (`0x0001`) containing only that value. Sizes never
  need ZIP64 (§3.1 rule 7).
- If the number of entries is 0xFFFF or more, or the central directory's size
  or offset is 0xFFFFFFFF or more, the writer writes a zip64 end of central
  directory record (version 1, no extensible data) and a zip64 end of central
  directory locator. In the end of central directory record, each field
  whose value is too large is set to all ones (0xFFFF or 0xFFFFFFFF); the
  other fields hold their actual values.

Readers MUST support these structures. A reader MUST use the zip64 end of
central directory record whenever a zip64 locator immediately precedes the
end of central directory record.

### 3.3 Keys

The key of an entry is its file name, decoded as UTF-8. A key MUST NOT be
empty and MUST be valid UTF-8. Keys are otherwise unrestricted; for example,
`a/../b` and `x/` are ordinary distinct keys (§10).

### 3.4 End of central directory record and archive comment

The ZIP archive comment of a vzip archive is one of:

| length | layout                                                           |
|--------|------------------------------------------------------------------|
| 22     | `magic`, `sources_offset: u64`, `sources_size: u64`              |
| 38     | `magic`, `sources_offset: u64`, `sources_size: u64`, `index_offset: u64`, `index_size: u64` |

- `magic` is the 6 ASCII bytes `vzip/1`.
- `sources_offset` and `sources_size` are the body offset and the compressed
  size of the `__vz__/sources` entry.
- `index_offset` and `index_size`, if present, are the same for
  `__vz__/index`. The 38-byte form is used if and only if the archive has a
  page index (§7).

Because the comment has one of two lengths, the end of central directory
record starts either 44 or 60 bytes before the end of the file. A reader
locates it as follows:

1. Check offset `file_size − 60`: the 4-byte signature `0x06054B50` and a
   comment length field of 38.
2. Otherwise check offset `file_size − 44`: the signature and a comment
   length of 22.
3. If neither matches, or the comment does not start with `vzip/1`, the file
   is not a vzip version 1 archive, and opening it is an archive error
   (§8.4).

Readers MUST read `__vz__/sources` and `__vz__/index` through the comment's
offsets and sizes. They need not consult those entries' central directory
records. Writers MUST make the two agree.

## 4. Entries

### 4.1 Classification

The kind of a present key is decided by its central directory record alone.
The record's extra field is parsed per APPNOTE as a sequence of
`(header_id: u16, data_size: u16, data[data_size])` blocks that exactly fills
the declared extra field length. Let `R` be the number of blocks whose header
ID is 0x7A76 or 0x7A77:

| condition | kind |
|---|---|
| `R = 0` | `bytes` |
| `R = 1` | `reference` |
| `R > 1`, or the extra field does not parse | entry error (§8.4) |

Other header IDs (such as `0x0001`) MAY appear and do not affect the kind.
The format entries MUST be `bytes` entries with method 8. Readers locate and
inflate them through the comment (§3.4).

### 4.2 Bytes entries

The value of a `bytes` entry is its body, inflated if the entry uses method 8.

### 4.3 Reference entries

The data of the 0x7A76 or 0x7A77 block is the **reference payload**:

- header ID 0x7A76: an encoded `Range` (§5.2), describing a value made of that
  one range.
- header ID 0x7A77: an encoded `Concat` (§5.3), describing a value made of zero
  or more ranges.

Writers MUST use 0x7A76 when a reference has exactly one range, and 0x7A77
otherwise. Readers MUST accept a 0x7A77 payload with any number of parts.

A reference entry MUST use method 0; a reference entry with method 8 is an
entry error. Its body MUST be either empty or byte-identical to the reference
payload. Writers SHOULD make it identical, so that ZIP tools which ignore
extra fields still see the payload. Readers MUST take the payload from the
central directory, and MUST NOT read or check the body except in the raw view
(§8.2).

A record's entire extra field is at most 65535 bytes. A writer MUST reject a
reference whose payload, together with the record's other extra blocks,
does not fit. Without other blocks the payload limit is 65531 bytes.

## 5. Messages

### 5.1 Encoding

Messages use the Protocol Buffers binary wire format with proto3 semantics.
Appendix A gives the schema, which is normative together with these rules.

Decoding:

- A decoder MUST skip fields whose field number is not in the schema, for
  wire types VARINT (0), I64 (1), LEN (2) and I32 (5).
- The following make the message malformed:
  - wire types 3, 4, 6 and 7;
  - field number 0, or above 2^29 − 1;
  - truncated input, or a LEN field extending past the end of its enclosing
    message;
  - a varint longer than 10 bytes, or whose value exceeds 2^64 − 1;
  - a known field whose wire type differs from the one its type uses (VARINT
    for integers, LEN for strings, bytes and messages);
  - a `uint32` field whose value exceeds 2^32 − 1;
  - a string that is not valid UTF-8.
- Fields may appear in any order. If a non-repeated field appears more than
  once, the last occurrence wins. If more than one member of a `oneof`
  appears, the last one on the wire wins. Non-minimal varints are accepted.

Encoding:

- Encoders MUST emit fields in field-number order, use minimal varints, and
  not emit a scalar field equal to its default value (0 or empty), with two
  exceptions: a set `oneof` member and a set `optional` field are always
  emitted, even when empty. With these rules each message has exactly one
  encoding, so implementations can compare encodings byte for byte.

### 5.2 Range

```proto
message Range {
  uint32 source = 1;
  reserved 2;
  uint64 offset = 3;
  uint64 length = 4;
  optional bytes data = 5;
}
```

A Range describes a byte sequence of a known **size**:

- **Literal range**: field 5 (`data`) is present, even with zero length. The
  bytes are `data`, and the size is `len(data)`. In a literal range,
  `source`, `offset` and `length` MUST all be 0; otherwise the payload is
  malformed.
- **Source range**: field 5 is absent. The bytes are
  `[offset, offset + length)` of the **source value** of source table entry
  number `source` (§6), and the size is `length`. `source` MUST be less than
  the number of sources, and `offset + length` MUST NOT exceed 2^64 − 1;
  otherwise the payload is malformed.

The size is known without I/O.

### 5.3 Concat

```proto
message Concat {
  repeated Range parts = 1;
}
```

A Concat describes the concatenation of its parts, in order. Its size is the
sum of the parts' sizes, which MUST NOT exceed 2^64 − 1. A Concat with no
parts describes the empty value.

## 6. Source table

The entry `__vz__/sources` (method 8) holds a `SourceTable`:

```proto
message Source {
  oneof kind {
    string url  = 1;
    string key  = 2;
    bytes  data = 3;
  }
}

message SourceTable {
  repeated Source sources = 1;
}
```

Sources are numbered from 0 in wire order. When the table is read (§8.1), it
is an archive error if it is malformed, if a Source has no `kind` member, or
if a `url` is empty.

The **source value** of each kind:

- **`url`**: the bytes of an external object.
  - `url` is a URI reference (RFC 3986), percent-encoded where RFC 3986
    requires it. For example, a file named `a b.bin` is referenced as
    `a%20b.bin`.
  - It is resolved against the archive's base URI using the strict algorithm
    of RFC 3986 §5.2.2.
  - When an archive is opened from a local path, its base URI is the `file:`
    URI of the absolute form of that path. Relative paths are made absolute
    against the current directory, and `.` and `..` segments are removed
    lexically. Symbolic links are not resolved.
  - When opened from a URL, the base URI is that URL.
  - A `file:` URI maps to a local path as follows:
    - its authority MUST be empty or `localhost`;
    - its path MUST be absolute (start with `/`);
    - it MUST have no query;
    - any fragment is ignored;
    - its path is percent-decoded.

  Readers MUST support `file:`, SHOULD support `http:` and `https:` (byte
  ranges via HTTP Range requests), and MAY support other schemes. It is a
  resolution error if the reference is not a valid URI reference, uses an
  unsupported scheme, or violates the `file:` rules, or if the object cannot
  be read.
- **`key`**: the value of the bytes entry of this archive with that key. The
  key MAY be hidden. It is a resolution error if the key is missing, is a
  reference entry, has an entry error, or is a format entry. References
  therefore never chain.
- **`data`**: the bytes of `data`. Writers SHOULD use this for small byte
  strings shared by many ranges, such as a decoding header. Bytes used by a
  single range belong in a literal range instead.

Readers MUST NOT resolve sources except as §8.3 requires. An archive whose
source table names an unreachable URL is fully readable, except for the
ranges that use that URL.

## 7. Page index (optional)

A page index lets a reader find the central directory record of a key by
reading one bounded slice of the central directory, instead of the whole
directory. An archive has a page index if and only if its comment has the
38-byte form; then it has an entry `__vz__/index` (method 8) holding a
`CdIndex`. An archive with a 22-byte comment MUST NOT have an entry named
`__vz__/index`.

```proto
message CdIndex {
  message Page {
    string first_key = 1;
    uint64 offset    = 2;
    uint64 length    = 3;
  }
  message Pinned {
    string key         = 1;
    uint64 data_offset = 2;
    uint64 size        = 3;
    uint64 csize       = 4;
    uint32 method      = 5;
  }
  repeated Page   pages  = 1;
  repeated Pinned pinned = 2;
}
```

### 7.1 Layout

In an archive with a page index:

1. The central directory consists of the **body records** (the records of
   every entry except the two format entries), sorted in UTF-8 order of
   their file names. These are followed by the two format entries' records,
   in either order.
2. The pages partition the body records:
   - page `i` covers bytes `[offset_i, offset_i + length_i)` of the central
     directory, with offsets relative to its first byte;
   - the first page starts at 0, each later page starts where the previous one
     ends, and the last page ends where the format entries' records begin;
   - pages are non-empty and hold whole records;
   - `first_key` is the key of a page's first record;
   - if there are no body records, there are no pages.
3. `pinned` lists zero or more `bytes` entries, other than the format entries,
   with no key listed twice. Each one gives:
   - `data_offset`: the entry's body offset;
   - `size` and `csize`: its uncompressed and compressed sizes;
   - `method`: its method.

   These MUST match the entry's central directory record. Writers SHOULD pin
   the small entries that readers fetch when opening, such as Zarr metadata
   documents (§9.2).

### 7.2 Lookup

In an archive with a page index, readers MUST determine whether a key is
present as follows:

- The format entries are known from the comment (§3.4).
- A pinned key is a present `bytes` entry, described by its `Pinned` values.
- To find any other key `k`, select the last page whose `first_key` is less
  than or equal to `k` in UTF-8 order. If there is no such page, `k` is
  missing. Otherwise `k` is present if and only if that page contains a record
  named `k`.

A reader that reads the whole central directory anyway gets the same result
for every valid archive. Readers are not required to check that the
central directory matches §7.1 (§8.6).

## 8. Reading

### 8.1 Opening

Opening an archive reads and checks:

- the end records (§3.2, §3.4);
- the source table (§6);
- the page index (§7), if there is one;
- for an archive without a page index, the whole central directory.

A reader MAY read more, but MUST NOT report an error at open that this
section does not list.

### 8.2 Operations

A conforming reader provides the following operations on an opened archive:

- **classify(key)** → `bytes` | `reference` | `missing`. Hidden keys
  classify as `missing`. It looks only at the central directory record
  (§4.1), so a reference with a malformed payload still classifies as
  `reference`.
- **get(key, request)** → a byte sequence, *missing*, or an error. Hidden
  keys are *missing*. `request` is one of:
  - *whole*: the whole value;
  - *range(start, end)* with `start <= end`: bytes `[min(start, n), min(end, n))`
    of the value, where `n` is the value's size;
  - *offset(start)*: bytes `[min(start, n), n)`;
  - *suffix(count)*: bytes `[max(n − count, 0), n)`.

  `start > end` is a request error, whether or not the key is present.
- **list(prefix)** → every non-hidden present key that starts with `prefix`
  (compared on UTF-8 bytes), sorted in UTF-8 order. The empty prefix lists
  all keys. Keys with entry errors are listed.
- **raw(key)** → the inflated body of the key's entry, or *missing*. In this
  view every present entry is visible, including hidden and format entries,
  and a reference entry yields its body (empty, or its payload). This is the
  view of a ZIP tool that knows nothing about vzip.

### 8.3 Resolving a reference

`get` on a reference entry proceeds as follows:

1. Decode the payload. If it is malformed (§5), the `get` fails, whatever
   the request.
2. Compute the requested window `[a, b)` of the value (§8.2).
3. For each range whose bytes overlap `[a, b)` in a non-empty interval, take
   those bytes from the range:
   - A literal range needs no I/O.
   - A source range reads bytes `[offset + i, offset + j)` of its source value,
     where `[i, j)` is the overlap relative to the range's start. The read is
     a resolution error if the source cannot be resolved (§6), or if the
     source value is shorter than `offset + j`.
4. Ranges that do not overlap the window are not resolved and cause no
   error, even if their source is missing or too short. This includes every
   zero-length range.

Readers SHOULD fetch only the bytes in step 3. A reader MUST NOT return bytes
for a request it could not resolve completely.

### 8.4 Errors

There are five classes of error. Each says which operations it makes fail.

| class | examples | effect |
|---|---|---|
| **archive error** | not a vzip version 1 archive (§3.4); bad end records; malformed source table, Source without a member, empty `url` (§6); malformed page index; an `__vz__/index` entry in an archive with a 22-byte comment | opening fails |
| **entry error** | the record of the key violates §4.1 (unparseable extra field, or several reference blocks), uses a method other than 0 and 8, has bit 0 set, or is a reference entry with method 8 (§4.3) | classify, get and raw fail for that key; other keys are unaffected |
| **payload error** | the reference payload is malformed (§5) | get fails for that key |
| **resolution error** | §6, §8.3 step 3 | get fails for the requests that need the range |
| **request error** | `start > end` | that operation fails |

In an archive with a page index, a record's problems are found when its page
is read, so they surface as entry errors, not archive errors.

### 8.5 Request results

A failed operation returns no bytes. A missing key is not an error.

### 8.6 Violations readers need not detect

For archives that break these writer requirements, reader results are
unspecified:

- §3.1 rules 1, 2, 4, 5 (except bit 0), 6, 7 and 8: readers use body offsets
  computed from the central directory, need not read local headers, and need
  not verify CRC-32 values. Duplicate names are included here.
- §3.2: whether ZIP64 structures were used only where needed.
- §3.4 and §4.1: whether the format entries' records agree with the comment,
  are `bytes` entries, and use method 8.
- §4.3: whether a reference entry's body is empty or equal to its payload.
  Readers MUST still ignore the body, except in the raw view.
- §7.1: whether the central directory matches the page index, and whether
  pinned values match their records.

## 9. Writing

### 9.1 Requirements

A writer produces an archive from a list of entries (each either bytes or a
list of ranges) and a source table. It MUST produce a valid archive, and it
MUST reject its input, producing no archive, if:

- a key is empty, not valid UTF-8, duplicated, or a format entry's key;
- a range's `source` is not less than the number of sources;
- a `url` source is empty;
- a `key` source names a key that is absent, is a reference entry, or is a
  format entry;
- a reference's extra field would exceed 65535 bytes (§4.3);
- a bytes entry's size is 0xFFFFFFFF or more (§3.1 rule 7);
- a pinned entry is not a bytes entry, or is listed twice.

`__vz__/index` MUST follow every entry in the archive whose record it indexes
or pins; otherwise its own size would change the offsets it records.

### 9.2 Recommended layout (informative)

```
[entries ...]                     bytes and reference entries
[__vz__/sources]                  DEFLATE
[pinned entries]                  small entries readers fetch when opening (e.g. zarr.json)
[__vz__/index]                    if paged
[central directory]
[zip64 end records]               if needed
[end of central directory + vzip comment]
```

With this layout a reader can open an archive with two range requests. It
reads the last 64 KiB, which holds the end records and comment, and then the
bytes from `sources_offset` to the end of the index (for a paged archive) or
to the end of the file (for an unpaged one).

For reproducible output, writers SHOULD use the DOS date 1980-01-01 00:00 and
"version made by" 20, and SHOULD use "version needed to extract" 45 for
records with a ZIP64 extra field and 20 otherwise. Readers MUST NOT depend on
any of these values.

## 10. Security considerations

- A reader that supports `file:` URLs will read any local file an archive
  names. Readers SHOULD let applications restrict which base directories or
  URL prefixes may be resolved, and SHOULD document their default.
- Keys are not file names. Tools that extract an archive to disk MUST NOT
  trust keys such as `../x` or `/etc/passwd`.
- A reference can describe a value of up to 2^64 − 1 bytes, and a DEFLATE
  body can expand greatly. Readers SHOULD bound the memory a single request
  can use.

## Appendix A: Complete schema

```proto
syntax = "proto3";
package vzip.v1;

message Range {
  uint32 source = 1;
  reserved 2;
  uint64 offset = 3;
  uint64 length = 4;
  optional bytes data = 5;
}

message Concat {
  repeated Range parts = 1;
}

message Source {
  oneof kind {
    string url  = 1;
    string key  = 2;
    bytes  data = 3;
  }
}

message SourceTable {
  repeated Source sources = 1;
}

message CdIndex {
  message Page {
    string first_key = 1;
    uint64 offset    = 2;
    uint64 length    = 3;
  }
  message Pinned {
    string key         = 1;
    uint64 data_offset = 2;
    uint64 size        = 3;
    uint64 csize       = 4;
    uint32 method      = 5;
  }
  repeated Page   pages  = 1;
  repeated Pinned pinned = 2;
}
```

## Appendix B: Constants

| name                      | value        |
|---------------------------|--------------|
| single-range extra ID     | `0x7A76`     |
| concat extra ID           | `0x7A77`     |
| reserved prefix           | `__vz__/`    |
| source table key          | `__vz__/sources` |
| page index key            | `__vz__/index`   |
| comment magic             | `vzip/1` (6 bytes) |
| comment lengths           | 22 (no page index), 38 (page index) |
