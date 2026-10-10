# vzip: a ZIP container for byte-range references

Version: 1 (draft, revision 1)

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
contains the encoded reference rather than the referenced bytes.

### 1.1 Conventions

The key words MUST, MUST NOT, REQUIRED, SHOULD, SHOULD NOT, MAY, and OPTIONAL
are to be interpreted as described in RFC 2119.

All multi-byte integers in ZIP structures and in the archive comment are
unsigned little-endian. `u16`, `u32`, `u64` denote unsigned integers of 16,
32, 64 bits.

"APPNOTE" means the PKWARE .ZIP File Format Specification, version 6.3.10 or
later. Where this document is silent about ZIP structures, APPNOTE applies.

Byte ranges are written `[a, b)`: start inclusive, end exclusive.

## 2. Data model

An archive defines a partial function from **keys** to **values**:

- A **key** is a non-empty Unicode string, stored as UTF-8 (§3.3).
- A **value** is a finite byte sequence (possibly empty).

Each key present in the archive has a **kind**:

| kind        | value                                                         |
|-------------|---------------------------------------------------------------|
| `bytes`     | the (decompressed) body of the key's ZIP entry                |
| `reference` | the bytes described by the entry's reference (§5)             |

A key that is not present has kind `missing` and no value. Asking for a
missing key is not an error.

Keys beginning with the **reserved prefix** `__vz__/` are **hidden**. They
hold data used by the format itself or by references (§6, §7), and they are
not part of the archive's public mapping (§8.1).

## 3. ZIP container requirements

### 3.1 General

A vzip archive MUST be a single-disk ZIP archive conforming to APPNOTE. In
addition, every vzip archive MUST satisfy the following rules. A writer MUST
produce archives that satisfy them, and a reader MAY reject archives that
violate them.

1. Every entry has exactly one local file header and one central directory
   record, and no two central directory records have the same file name.
2. Every entry uses compression method 0 (STORED) or 8 (DEFLATE). DEFLATE
   bodies are raw DEFLATE streams (RFC 1951) with no zlib or gzip wrapper.
3. Every local file header has an extra field length of 0. A reader can
   therefore compute the offset of an entry's body from its central directory
   record alone: `local_header_offset + 30 + file_name_length`.
4. No entry is encrypted, and general purpose bit 3 (data descriptor) is clear.
5. General purpose bit 11 (UTF-8 file names) is set on every entry, in both
   headers.
6. The CRC-32, compressed size and uncompressed size in both headers are
   correct for the entry's body.
7. The archive comment is the vzip comment (§3.4).
8. The archive has no central directory encryption, digital signature or
   archive extra data record.

A writer MUST use the ZIP64 structures of APPNOTE (zip64 end of central
directory record, zip64 end of central directory locator, and zip64 extended
information extra field `0x0001`) whenever a value does not fit in its
32-bit or 16-bit field. A writer MUST NOT use ZIP64 structures otherwise. A
reader MUST support ZIP64.

### 3.2 Central directory order

When the archive has a page index (§7), the central directory MUST be laid out
as specified in §7.1. Otherwise its records MAY appear in any order. Writers
SHOULD sort them by file name, comparing the UTF-8 bytes lexicographically.

### 3.3 Keys

The key of an entry is its file name, decoded as UTF-8. A key MUST NOT be
empty and MUST be valid UTF-8. A writer MUST reject duplicate keys.

### 3.4 Archive comment

The ZIP archive comment (the variable-length field at the end of the end of
central directory record) of a vzip archive is one of:

| length | layout                                                           |
|--------|------------------------------------------------------------------|
| 22     | `magic`, `sources_offset: u64`, `sources_size: u64`              |
| 38     | `magic`, `sources_offset: u64`, `sources_size: u64`, `index_offset: u64`, `index_size: u64` |

- `magic` is the 6 ASCII bytes `vzip/1`.
- `sources_offset` and `sources_size` give the absolute file offset and the
  *compressed* size of the body of the `__vz__/sources` entry (§6).
- `index_offset` and `index_size`, if present, do the same for the
  `__vz__/index` entry (§7). The 38-byte form is used if and only if the
  archive has a page index.

A ZIP file whose comment does not match one of these forms is not a vzip
archive. A vzip reader MUST reject it, or MAY treat it as an archive in which
every entry has kind `bytes` and there is no source table.

## 4. Entry kinds

### 4.1 Classification

The kind of a present key is decided by its central directory record alone.
Let `E` be the set of header IDs in the record's extra field:

| condition                         | kind                           |
|-----------------------------------|--------------------------------|
| `E` contains neither 0x7A76 nor 0x7A77 | `bytes`                   |
| `E` contains exactly one of 0x7A76, 0x7A77, exactly once | `reference` |
| otherwise                         | invalid: the archive is malformed |

The extra field is parsed per APPNOTE: a sequence of `(header_id: u16,
data_size: u16, data[data_size])` blocks. An extra field that does not parse
exactly to its declared length is malformed. Other header IDs (such as
`0x0001`) MAY appear and do not affect the kind.

### 4.2 Bytes entries

The value of a `bytes` entry is its body, inflated if the entry uses method 8.

### 4.3 Reference entries

The data of the 0x7A76 or 0x7A77 block is the **reference payload**:

- header ID 0x7A76: the payload is an encoded `Range` message (§5.2). It
  describes a value made of one range.
- header ID 0x7A77: the payload is an encoded `Concat` message (§5.3). It
  describes a value made of zero or more ranges.

A reference entry MUST use method 0. Its body MUST be either empty or
byte-identical to the reference payload. Writers SHOULD make it identical,
so that ZIP tools which ignore extra fields still see the payload. Readers
MUST take the payload from the central directory and MUST NOT require the
body.

A writer MUST use 0x7A76 when the reference has exactly one range and 0x7A77
otherwise. A reader MUST accept a 0x7A77 payload with any number of parts,
including one or zero.

A reference payload is at most 65531 bytes, which is the limit of a ZIP extra
field block (65535 bytes) after its 4-byte header.

## 5. Messages

### 5.1 Encoding

Messages use the Protocol Buffers binary wire format (proto3 semantics). The
schema is given in Appendix A, and is normative together with the rules below.

- Only wire types VARINT (0) and LEN (2) are used by this schema. A decoder
  MUST skip fields with unknown field numbers of any valid wire type (0, 1,
  2, 5). It MUST reject wire types 3, 4, 6 and 7, truncated input, varints
  longer than 10 bytes, and LEN fields that extend past the end of the
  enclosing message.
- A field with a known field number but a wire type other than the one its
  declared type uses (VARINT for integers, LEN for strings, bytes and
  messages) is malformed. Strings MUST be valid UTF-8.
- If a non-repeated scalar field appears more than once, the last occurrence
  wins. If more than one member of a `oneof` appears, the last one on the
  wire wins.
- Encoders MUST emit fields in field-number order. They MUST NOT emit a
  scalar field equal to its default value (0 or empty), with two exceptions:
  a set `oneof` member, and a set field declared `optional` (explicit
  presence). Those are always emitted, even when empty. With these rules the
  encoding of a message is unique, so implementations can compare encodings
  byte for byte.

### 5.2 Range

```proto
message Range {
  uint32 source = 1;
  uint64 offset = 3;
  uint64 length = 4;
  optional bytes data = 5;
}
```

A Range describes a byte sequence, its **range value**:

- **Literal range**: if field 5 (`data`) is present on the wire (even with
  zero length), the range value is `data`. In a literal range, `source`,
  `offset` and `length` MUST be 0. A reader MUST treat a literal range with
  any of them non-zero as an error.
- **Source range**: otherwise, the range value is bytes
  `[offset, offset + length)` of the **source value** of source table entry
  number `source` (§6). `source` MUST be less than the number of entries in
  the source table. A range whose source value is shorter than
  `offset + length` is **out of bounds**. A reader resolving any part of an
  out-of-bounds range MUST report an error if one of the bytes it would
  return lies past the end of the source value, and MAY report an error
  otherwise. Readers are not required to discover an external object's size
  before reading.

The **size** of a range is `len(data)` for a literal range and `length` for a
source range. A reader can therefore compute it without any I/O.

`source = 0` is not written on the wire. Writers SHOULD give index 0 to the
most used source.

### 5.3 Concat

```proto
message Concat {
  repeated Range parts = 1;
}
```

The value described by a Concat is the concatenation of the range values of
its `parts`, in order. A Concat with no parts describes the empty value.

## 6. Source table

Every vzip archive has exactly one entry named `__vz__/sources`. Its method
MUST be 8 (DEFLATE). Its inflated body is a `SourceTable` message:

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

Entries are numbered from 0 in wire order. Each `Source` MUST have exactly
one `kind` member set. A Source with no member is malformed. The **source
value** of each kind is:

- **`url`**: the bytes of the object identified by the URL. `url` MUST NOT be
  empty. It is a URI reference (RFC 3986). If it is relative, it is resolved
  per RFC 3986 §5 against the archive's own URL (the base URI). When an
  archive is opened from a local file path, its base URI is the `file:` URI of
  the absolute path. Readers MUST support the `file` scheme and SHOULD support
  `http` and `https` (fetching byte ranges with HTTP Range requests). Support
  for other schemes (`s3`, `gs`, ...) is OPTIONAL. Resolving a range of an
  unsupported scheme is an error.
- **`key`**: the value of the entry of this archive with that key. The key
  MUST be present and MUST have kind `bytes`. It MAY be hidden. If the key is
  missing or names a reference entry, resolving a range of this source is an
  error. References therefore never chain, and resolution always terminates.
- **`data`**: the bytes of `data`. Writers SHOULD use this for small byte
  strings shared by many ranges (for example a decoding header). Bytes used
  by a single range belong in a literal range instead.

A source that no range uses is allowed.

Readers MUST NOT resolve sources eagerly. Only resolving a range touches its
source. An archive whose source table names an unreachable URL is therefore
fully readable except for the ranges that use that URL.

## 7. Page index (optional)

A page index lets a reader find the central directory record of a key by
reading one bounded slice of the central directory, instead of the whole
directory.

### 7.1 Layout

If an archive has a page index, then:

1. It has an entry named `__vz__/index`, method 8, whose inflated body is a
   `CdIndex` message (below), and the archive comment has the 38-byte form.
2. The central directory consists of two parts:
   - the **body records**: the records of every entry except `__vz__/sources`
     and `__vz__/index`, sorted by file name, comparing UTF-8 bytes
     lexicographically, followed by
   - the **trailer records**: the records of `__vz__/sources` and
     `__vz__/index`, in either order.
3. The pages partition the body records. Page `i` covers the records that lie
   in byte range `[offset_i, offset_i + length_i)` of the central directory,
   where offsets are relative to the first byte of the central directory. The
   first page has offset 0. Each later page starts where the previous one
   ends. The last page ends where the trailer records begin. Pages are
   non-empty and contain whole records only. If there are no body records,
   there are no pages.
4. `first_key` of each page is the key of the first record in that page.

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

### 7.2 Lookup

The entries `__vz__/sources` and `__vz__/index` are in no page. A reader
knows both from the archive comment: their bodies' offsets and compressed
sizes are given there, and their method is 8.

To find any other key `k`, a reader selects the last page whose `first_key`
is less than or equal to `k` (in UTF-8 byte order). If there is no such page,
`k` is missing. Otherwise the reader reads that page and scans its records.
If no record matches, `k` is missing.

### 7.3 Pinned entries

`pinned` lists `bytes` entries that a reader may use without reading any
page. For each one:

- `data_offset` is the absolute offset of the body,
- `size` and `csize` are its uncompressed and compressed sizes, and
- `method` is its compression method.

These values MUST equal those derived from the entry's central directory
record. Pinned entries also appear in the body records as usual. Only `bytes`
entries may be pinned. Writers SHOULD pin small metadata entries that readers
fetch when opening (see §9).

## 8. Reader behaviour

### 8.1 Operations

A conforming reader provides at least the following operations on an opened
archive.

- **classify(key)** → `bytes` | `reference` | `missing`. Hidden keys
  classify as `missing`.
- **get(key, request)** → a byte sequence, or *missing*, or an error. Hidden
  keys are *missing*. `request` is one of:
  - *whole*: the whole value.
  - *range(start, end)* with `0 <= start` and `start <= end`: bytes
    `[min(start, n), min(end, n))` of the value, where `n` is the value size.
  - *offset(start)*: bytes `[min(start, n), n)`.
  - *suffix(count)*: bytes `[max(n - count, 0), n)`.

  `start > end` is an error, whether or not the key is present. A reference's value size is the sum of its range
  sizes (§5.2), so a reader can serve any request by resolving only the parts
  of the ranges that overlap it, and SHOULD do so.
- **list(prefix)** → every non-hidden present key that starts with `prefix`
  (compared on UTF-8 bytes), sorted by UTF-8 bytes. The empty prefix lists
  all keys.

A reader SHOULD also offer a **raw** (naive) view, in which every entry,
including hidden entries, has kind `bytes` and its value is the inflated
entry body. In the raw view a reference key yields its body, which is either
empty or the reference payload (§4.3).

### 8.2 Errors

The following MUST be reported as errors. The archive itself may be
rejected when it is opened, or the error may be raised by the operation that
first encounters it:

- violations of §3, §4.1, §5.1, §6, §7;
- a reference whose payload does not decode as its message type;
- a source range whose `source` is out of bounds;
- a literal range with non-zero `source`, `offset` or `length`;
- a `key` source that is missing or names a reference entry;
- reading past the end of a source value (§5.2);
- failure to read an external object.

A reader MUST NOT return bytes for a request it could not resolve
completely.

## 9. Writer behaviour (informative, except where marked)

A writer produces an archive from a set of `bytes` entries, a source table,
and a set of references. The normative requirements are in §3–§7. In
particular, a writer MUST reject:

- duplicate keys, and empty or non-UTF-8 keys;
- a range whose `source` is out of bounds;
- a `key` source naming a key that is absent from the archive or is a
  reference entry;
- a reference payload over 65531 bytes.

Recommended layout, which lets a reader open an archive with one or two range
requests:

```
[entries ...]                     bytes and reference entries
[__vz__/sources]                  DEFLATE
[metadata entries]                small entries readers fetch when opening (e.g. zarr.json)
[__vz__/index]                    if paged
[central directory]
[zip64 end records]               if needed
[end of central directory + vzip comment]
```

A reader can then fetch the last 64 KiB, parse the end records and the
comment, and fetch everything from `sources_offset` to the end of the
metadata entries (for a paged archive) or to the end of the file (for an
unpaged one). It does not need to read local headers.

## Appendix A: Complete schema

```proto
syntax = "proto3";
package vzip.v1;

message Range {
  uint32 source = 1;
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
