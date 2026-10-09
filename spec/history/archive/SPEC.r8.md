# vzip: a ZIP container for byte-range references

Format version: 0 (**provisional**) · Specification revision: 8

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

### 1.3 Versioning

vzip has an integer **format version**, written in every archive's comment as
the magic `vzip/<version>` (§3.4). This document specifies format version
**0**.

- **What a version is:** a format version fixes the meaning of every valid
  archive. Readers implement a set of versions and MUST reject an archive
  whose version they don't implement, as an archive error. Readers MUST NOT
  guess at an unknown version, even a higher one.
- **Revisions of this document:** the document itself has revisions, such as
  "version 0, revision 5". A revision may:
  - clarify text;
  - resolve an ambiguity by choosing one of the readings implementations
    already used;
  - fix a contradiction;
  - add conformance tests.

  A revision does not change the result of any operation on a valid archive
  whose result was already fully determined by the previous revision.
- **What needs a new version:** anything else, such as new message fields,
  new pins, new source kinds, or changed layout rules. This is necessary
  because readers skip unknown protobuf fields (§5.1): a field added within a
  version would be silently ignored by older readers. For a field like a pin,
  that would turn "fail closed" into "fail open".
- **Writers** SHOULD write the lowest format version that can express the
  archive, so that the most readers can read it.
- **Conformance:** a conformance suite states the format version and the
  spec revision it tests.

**Status.** A format version is in one of three states:

| status | meaning |
|---|---|
| draft | Anything may change. |
| **provisional** | The format is believed complete, and implementers may rely on it. Revisions may still change it in response to feedback from outside the project. Every such change is listed, with its reason, in the [changelog](conformance/REVISIONS.md). An incompatible change (one that changes the result of an operation on an archive that was valid before) must also be announced there as incompatible, with the conformance suite updated to match. Incompatible changes are avoided wherever a compatible one would do. |
| final | No incompatible change is ever made. Changes of that kind become the next format version. |

**Format version 0 is provisional** as of revision 8. Its draft history,
revisions 1–7, was driven by seven rounds of independent implementations
(see the changelog). It becomes final once it has gone through a period of
outside review with no incompatible change needed.

**Feedback** — ambiguities, implementation reports, test cases, objections —
is welcome as issues or pull requests on the specification's repository. A
report is most useful when it names the section, the input, and the
behaviour each reading would produce.

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
  directory record (44 bytes after its size field, so its size field is 44;
  no extensible data sector; "version needed to extract" 45) and a zip64 end
  of central directory locator (total number of disks 1). In the end of central directory record, each field
  whose value is too large is set to all ones (0xFFFF or 0xFFFFFFFF); the
  other fields hold their actual values.

In a central directory record, the ZIP64 extra block (`0x0001`) holds the
64-bit local header offset when the 32-bit field is all ones. In a valid
vzip archive that is the only value it can hold, because sizes never reach
0xFFFFFFFF (§3.1 rule 7), so the block is exactly 8 bytes. A record has an
entry error if:

- its compressed or uncompressed size field is 0xFFFFFFFF;
- its offset field is all ones and it has no `0x0001` block, or one shorter
  than 8 bytes;
- it has more than one `0x0001` block, whatever its offset.

A `0x0001` block on a record whose offset is not all ones is otherwise
ignored. When the offset comes from a block longer than 8 bytes, it is the
block's first 8 bytes; the rest is ignored.

Readers MUST ignore the entry counts of the end records, beyond using them to
decide whether ZIP64 records are used.

Readers MUST support these structures. A reader uses the zip64 end of central
directory record if and only if at least one of the end of central directory
record's entry counts (fields "entries on this disk" and "total entries"), its
central directory size, or its central directory offset is all ones. Then:

- a zip64 locator MUST immediately precede the end of central directory
  record;
- the zip64 record it points to MUST lie within the file, have the right
  signature and a size field of 44;
- all four values (both entry counts, the directory's size and offset) are
  taken from the zip64 record.

Otherwise the bytes before the end of central directory record are not
examined. A locator signature appearing there, for example inside a file
name in an archive without one, has no meaning. Any failure of these rules
is an archive error.

Readers MUST ignore disk-number fields, "version made by" and "version
needed to extract" values, timestamps, and internal and external
attributes.

### 3.3 Keys

The key of an entry is its file name, decoded as UTF-8. A key MUST NOT be
empty and MUST be valid UTF-8. Keys are otherwise unrestricted; for example,
`a/../b` and `x/` are ordinary distinct keys (§10).

Keys are compared and sorted as byte strings (UTF-8 order), never as
decoded text. In particular:

- A leading U+FEFF (byte order mark) is part of the key. Decoders that strip
  it must be told not to.
- UTF-8 order is code point order, which differs from UTF-16 code unit order
  for characters above U+FFFF. For example, `"\u{1F600}"` (😀) sorts after
  `"\u{FF5E}"` (～) in UTF-8 order but before it in UTF-16 order. Several
  languages' default string sort uses UTF-16 order.

A central directory record whose file name is empty or not valid UTF-8 is
ignored by readers: it names no key and appears in no operation (§8.6).

### 3.4 End of central directory record and archive comment

The ZIP archive comment of a vzip archive is one of:

| length | layout                                                           |
|--------|------------------------------------------------------------------|
| 22     | `magic`, `sources_offset: u64`, `sources_size: u64`              |
| 38     | `magic`, `sources_offset: u64`, `sources_size: u64`, `index_offset: u64`, `index_size: u64` |

- `magic` is the 6 ASCII bytes `vzip/0`: the prefix `vzip/` followed by the
  format version as one decimal digit (§1.3). A future version 10 or above
  will need a different comment layout, which that version will define.
- `sources_offset` and `sources_size` are the body offset and the compressed
  size of the `__vz__/sources` entry.
- `index_offset` and `index_size`, if present, are the same for
  `__vz__/index`. The 38-byte form is used if and only if the archive has a
  page index (§7).

Because the comment has one of two lengths, the end of central directory
record starts either 44 or 60 bytes before the end of the file. A reader
locates it as follows:

1. If offset `file_size − 60` holds the 4-byte signature `0x06054B50` and
   that record's comment length field is 38, the end of central directory
   record is there.
2. Otherwise, if offset `file_size − 44` holds the signature and a comment
   length of 22, it is there.
3. Otherwise the file is not a vzip archive.

The reader then checks that the comment starts with `vzip/` and a version
the reader implements (`vzip/0` for this document). It does not fall back
from step 1 to step 2 when the magic doesn't match. If no record is found, or
the comment does not start with `vzip/`, the file is not a vzip archive.
If it starts with `vzip/` and a version the reader doesn't implement, the
archive is of an unsupported version. Either way, opening it is an archive
error (§8.4).

In a valid archive, step 1 cannot match a record that isn't the real one. A
record found there would take its comment length from the real record's
disk-number field, which is 0 in a single-disk archive (§3.1). An invalid
archive whose disk-number field is 38 can make step 1 match a fake record;
readers then reject it (magic mismatch, no fallback), which is the intended
result.

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
entry error. Extra field blocks may appear in any order. Its body MUST be either empty or byte-identical to the reference
payload. Writers SHOULD make it identical, so that ZIP tools which ignore
extra fields still see the payload. Readers MUST take the payload from the
central directory, and MUST NOT read or check the body except in the raw view
(§8.2).

A reference payload MUST NOT exceed 65519 bytes; a reader treats a longer
one as malformed (a payload error, §8.4), even though it may fit in the
extra field. That limit leaves room in
the 65535-byte extra field for the payload's own block header and a ZIP64
block, whether or not the entry ends up needing one, so a writer can check a
reference before deciding where to put it.

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
  once, the last occurrence wins, but every occurrence must be valid: an
  invalid earlier occurrence (for example a `uint32` overflow or a string
  that is not UTF-8) makes the message malformed even though it is
  overridden. "Valid UTF-8" is as defined in RFC 3629: overlong forms and
  encoded surrogates are invalid. If more than one member of a `oneof`
  appears, the last one on the wire wins. Non-minimal varints are accepted.
  (The schema has no singular message fields, so protobuf's rule of merging
  repeated occurrences of an embedded message never applies.) Field 2 of
  `Range` is reserved: it is skipped like any unknown field.

Implementations built on a protobuf library get most of these rules for
free, but typically must add checks for three of them: groups (wire types 3
and 4) on unknown fields, `uint32` overflow (libraries truncate), and UTF-8
validity of strings (some runtimes don't check).

Encoding:

- Encoders MUST emit fields in field-number order, use minimal varints, and
  not emit a scalar field equal to its default value (0 or empty), with two
  exceptions: a set `oneof` member and a set `optional` field are always
  emitted, even when empty. With these rules each message has exactly one
  encoding, so implementations can compare encodings byte for byte.
- Every element of a repeated message field is emitted, including one whose
  own encoding is empty. For example, a `Concat` part that is a `Range` with
  all fields 0 is written `0a 00`.
- An `int64` is encoded as the 64-bit two's complement of its value, as a
  varint, so a negative value always takes 10 bytes. For example, −1 is
  `ff ff ff ff ff ff ff ff ff 01`.

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
  `source`, `offset` and `length` MUST all have the value 0; otherwise the
  payload is malformed. Only the values are checked: a field explicitly
  encoded as 0 is accepted, like any non-canonical encoding (§5.1).
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
  // Pins (§6.1); allowed only on `url` sources.
  optional uint64 size               = 4;
  optional string etag               = 5;
  optional int64  modified_not_after = 6;
}

message SourceTable {
  repeated Source sources = 1;
}
```

Sources are numbered from 0 in wire order. When the table is read (§8.1),
any of the following is an archive error:

- the table is malformed;
- a Source has no `kind` member;
- a `url` or a `key` is empty;
- a pin appears on a `key` or `data` source;
- an `etag` pin is not a strong entity tag (§6.1).

The **source value** of each kind:

- **`url`**: the bytes of an external object.
  - `url` MUST match the `URI-reference` rule of RFC 3986 §4.1 exactly. It
    is therefore ASCII only, with every other byte percent-encoded (non-ASCII
    characters as their UTF-8 bytes). For example, a file named `a b.bin` is
    referenced as `a%20b.bin`, and `é.bin` as `%C3%A9.bin`.
  - It is resolved against the archive's base URI using the strict algorithm
    of RFC 3986 §5.2.2: a reference with a scheme is used as it is, even if
    the scheme matches the base's.
  - When an archive is opened from a local path, its base URI is built in
    two steps:
    - Make the path absolute and normalise it. A relative path is joined to
      the process's current directory as the operating system reports it
      (`getcwd`, not `$PWD`). Empty segments (`//`) and `.` segments are
      removed, `..` segments are removed lexically together with the segment
      before them, and symbolic links are not resolved.
    - Form `file://` followed by the path's bytes: its UTF-8 bytes, or the
      raw bytes on systems where paths are not text. Each byte that is not an
      unreserved character, a sub-delim, `:`, `@` or `/` is written as `%`
      and two uppercase hex digits. For example, `/data/my file.vzip` becomes
      `file:///data/my%20file.vzip`. A `..` segment at the root is removed,
      so `/../a` becomes `/a`.
  - When opened from a URL, the base URI is that URL.
  - A `file:` URI maps to a local path as follows:
    - the scheme and the host `localhost` are case-insensitive;
    - its authority MUST be absent (`file:/x`), empty (`file:///x`) or
      exactly `localhost` (in any case): no userinfo, no port, and no
      percent-encoding;
    - its path MUST be absolute (start with `/`);
    - it MUST have no query component, not even an empty one (`?`);
    - any fragment is ignored;
    - its path is percent-decoded to bytes, which MUST NOT contain `/` from
      a decoded `%2F` or a NUL byte. The bytes are used as the local path
      as they are; they need not be UTF-8;
    - after decoding, no path segment may be `.` or `..`. Resolution has
      already removed literal dot segments, so this only rejects encoded ones
      such as `%2E%2E`, which would otherwise escape the directory after
      decoding.

    This mapping is for POSIX paths; other systems follow RFC 8089.

  Readers MUST support `file:`, SHOULD support `http:` and `https:` (byte
  ranges via HTTP Range requests), and MAY support other schemes. It is a
  resolution error if the reference is not a valid URI reference, uses an
  unsupported scheme, or violates the `file:` rules, or if the object cannot
  be read (§6.2 for HTTP). Readers check URL syntax only when they resolve a
  range of the source, not at open; writers check it when writing (§9.1). A reference with only a fragment (such as `#x`) resolves to the
  archive itself; that is valid but rarely useful.
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

### 6.1 Pins

A reference names bytes the archive does not control. If the object behind a
`url` is replaced, for example a forecast file re-published under the same
name, the reference silently returns bytes from the new object. HDF5 and
netCDF files regenerated with the same layout decode cleanly to the wrong
values. **Pins** let a writer record what it saw, so that a reader detects
the change.

A `url` source may carry any combination of three pins. Each one asserts
something about the object at resolution time:

| pin | assertion |
|---|---|
| `size` | the object's total size in bytes equals `size` |
| `etag` | the object's current entity tag equals `etag`, by strong comparison (RFC 9110 §8.8.3.2) |
| `modified_not_after` | the object's last modification time, in whole seconds since the Unix epoch (UTC, rounded down, i.e. towards negative infinity), is less than or equal to `modified_not_after` |

A pin is present if its field is present on the wire, even with the value 0
(the fields are `optional`). Implementations built on protobuf libraries
must use the library's presence tracking for them, not compare with 0.

`etag` holds a strong entity tag exactly as it appears in an HTTP `ETag`
header, including its double quotes, for example `"abc123"`. It MUST match
`DQUOTE *etagc DQUOTE`, where `etagc` is any ASCII character from `!` to `~`
except `"` (RFC 9110 §8.8.3, without `obs-text`). Anything else, including a
weak tag (`W/"..."`), is an archive error (§6).

Before returning any byte read from a pinned source, a reader MUST check
every pin on that source. A pin that fails, or that the reader cannot check
for that scheme, is a resolution error. A reader MUST NOT skip a pin it
does not understand: pins fail closed. Checks, by scheme:

| pin | `http:`/`https:` | `file:` |
|---|---|---|
| `size` | the object's size, from the response (§6.2) | the file's size |
| `etag` | send `If-Match: <etag>` with the range request; a 412 response fails the pin | cannot be checked: resolution error |
| `modified_not_after` | send `If-Unmodified-Since: <HTTP-date of the pin>` with the range request; a 412 response fails the pin | the file's modification time, rounded down to whole seconds (as above) |

Over HTTP the checks ride on the range requests the reader makes anyway, so
pins cost no extra requests. A reader that checks a pin once per source
SHOULD do so per opening of the archive, not cache the result across opens.

Writers SHOULD:

- take `modified_not_after` from the object itself (its `Last-Modified`
  value, or its file modification time), not from the writer's clock. A
  value from the writer's clock fails whenever the server's clock or the
  object's timestamp is ahead of it;
- pin `size` whenever they pin anything. Modification times have one-second
  resolution, so a rewrite within the same second as `modified_not_after` is
  not detected; `size` catches appends and most rewrites;
- prefer `etag` where the object store provides strong entity tags;
- pin only `size` in archives meant to be copied together with their data
  (§6, relative URLs). Copying an object changes its modification time and
  may change its entity tag.

### 6.2 Reading over HTTP

This section applies to readers that support `http:` and `https:`.

- **Requests:** a read of bytes `[a, b)` of an HTTP object is a GET with
  `Range: bytes=a-(b−1)` and `Accept-Encoding: identity`. A reader MAY
  combine reads of nearby ranges of the same object into one request. It
  MUST NOT make any other request to resolve a range or check its pins: no
  HEAD, and no request for the whole object.
- **Pins** add `If-Match` and `If-Unmodified-Since` headers to those same
  requests (§6.1). Servers and proxies may ignore those headers, so a reader
  also checks every successful (200 or 206) response itself:
  - an `etag` pin requires the response's `ETag` header to equal the pin
    (strong comparison);
  - a `modified_not_after` pin requires its `Last-Modified` header to be an
    IMF-fixdate (RFC 9110 §5.6.7, e.g. `Sun, 06 Nov 1994 08:49:37 GMT`) no
    later than the pin.

    The two obsolete HTTP-date formats are not accepted. RFC 850 dates
    resolve two-digit years against the reader's clock, so accepting them
    would make results depend on when the reader runs. The day name must be
    the correct one for the date. Day and month names are case-sensitive,
    as written in RFC 9110. A second of `60` (a leap second) is accepted and
    counts as the following second.

  A missing or unparseable header means the pin cannot be checked, which is
  a resolution error.
- **Responses:**

  | response | meaning |
  |---|---|
  | 206 with one `Content-Range: bytes a-z/total` | the requested bytes. `total` is the object's size. It is a resolution error if the returned range is not the one requested, if `z` is not less than `total`, or if the body's length is not `z − a + 1`. |
  | 206 with `Content-Range: bytes a-z/*` | the requested bytes, with the same range and body-length checks; the size is unknown, so a `size` pin cannot be checked (resolution error) |
  | 200 | the server ignored `Range` and sent the whole object. The reader takes the requested bytes from the body; the size is the body's length. Readers MUST accept this. If the body is shorter than the end of the requested range, it is a resolution error. |
  | 412 | a pin failed: resolution error |
  | 416 | the object is shorter than the requested range: resolution error |
  | 206 without exactly one valid `Content-Range`, including `multipart/byteranges` | resolution error |
  | any other status | resolution error |

  Header rules for the final 200 or 206 response:
  - **`Content-Range`** follows RFC 9110 §14.4: the unit `bytes` is
    case-insensitive and is followed by exactly one space.
  - **`Content-Encoding`:** if any value, trimmed and compared
    case-insensitively, is anything but `identity`, it is a resolution error.
    Repeated `Content-Encoding` fields combine into one list (RFC 9110
    §5.3), so two `identity` fields are the list `identity, identity`,
    which is also an error. Redirects, 412 and 416 responses are not
    checked for `Content-Encoding`.
  - **Repeated fields:** more than one `Content-Range`, `ETag` or
    `Last-Modified` field is a resolution error. So is more than one
    `Location` field on a redirect.
  - **Chunked transfer coding** is part of HTTP/1.1 framing, not of the
    representation, and readers MUST accept it.

- **Redirects:** readers MUST follow redirects:
  - statuses 301, 302, 303, 307 and 308. A reader follows at most 5
    redirects, so it makes at most 6 requests for one range; a redirect
    received in reply to the 6th request is a resolution error;
  - the `Location` header is resolved against the URL that was requested
    (RFC 3986 §5.2.2); a redirect without a `Location` header, or with one
    that is not a valid URI reference, is a resolution error;
  - only to `http:` or `https:`; a redirect to any other scheme is a
    resolution error;
  - each hop re-sends `Range`, `Accept-Encoding` and the pin headers. Pins
    and the size apply to the final response.
- **URLs:** an `http:` or `https:` URL is a resolution error if it has
  userinfo (`user@`), an empty or absent host (`http:/x` and `http:///x`
  included), or a port above 65535. An empty port (`host:`) means the
  default port. These rules apply to every URL requested, including
  redirect targets.
- **Memory:** a 200 response delivers the whole object. A reader MAY refuse
  to read a body larger than a documented resource limit (§10); that is a
  request error.
- **Timestamps:** `If-Unmodified-Since` carries `modified_not_after` as an
  IMF-fixdate (RFC 9110 §5.6.7). A value outside the years 1–9999 cannot be
  sent, so the pin cannot be checked: resolution error.

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
   - `first_key` is the key of a page's first record, so `first_key` values
     strictly increase in UTF-8 order;
   - if there are no body records, there are no pages.
3. `pinned` lists zero or more `bytes` entries, other than the format entries,
   with no key listed twice. Pinned entries may be hidden. Each one gives:
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

The page index itself is checked at open (§8.1). It is **malformed**, an
archive error, if any of the following hold:

- it does not decode;
- a page has length 0, or lies outside the central directory;
- the pages are not contiguous from offset 0;
- `first_key` values do not strictly increase in UTF-8 order;
- a page's `first_key` or a pinned `key` is empty;
- a pinned key is listed twice or is a format entry;
- a pinned `method` is not 0 or 8;
- a pinned body (`data_offset`, `csize`) lies outside the file.

## 8. Reading

### 8.1 Opening

Opening an archive reads and checks the following. Any failure is an
archive error:

- **The end records:**
  - the end of central directory record is found as in §3.4, and the magic
    is `vzip/0`;
  - the zip64 records are used when, and only when, §3.2 says so; their
    signatures are correct and they lie within the file;
  - the central directory (offset and size) lies within the file.

  Here and throughout, "lies within the file" means the region
  `[offset, offset + size)` is inside `[0, file size]`. Readers do not check
  whether regions overlap each other or the end records.

  A resource limit (§10) reached while opening, for example while inflating
  a format entry, is an archive error.

  The same corruption of a central directory record is an archive error in
  an archive without a page index (the whole directory is parsed at open),
  and an entry error, or nothing at all, in one with a page index (only the
  pages that are read are parsed). This is intentional: it is the cost of
  opening large archives without reading their whole directory.
- **The format entries:** the bodies the comment locates lie within the
  file and inflate cleanly (below). Their uncompressed sizes are not checked,
  since readers need not read their records. The source table is valid (§6). The page index,
  if there is one, is not malformed (§7.2).
- **The central directory, for an archive without a page index:** it parses
  as a sequence of records, each with the right signature and with its
  variable-length fields inside the directory, ending exactly at its
  declared size.
  - A problem confined to a single record's contents is not an archive
    error. Such problems include its extra field, method or flags, and a
    local header offset of 0xFFFFFFFF without a ZIP64 extra block holding
    the offset; they are entry errors (§8.4), in archives with or without a
    page index.
  - An `__vz__/index` record is an archive error here (§7).

A body **inflates cleanly** if it is a single complete raw DEFLATE stream
(its final block ends it) and the stream ends exactly at the end of the
body: no bytes follow it, and none are missing. For a bytes entry it must
also inflate to the record's uncompressed size. Some libraries ignore
trailing bytes by default, so implementations must check for them
explicitly.

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
  all keys. Keys with entry errors are listed. In an archive with a page
  index, "present" is as defined by §7.2:
  - pinned keys are listed;
  - a record found in a page is listed only if lookup would find it there,
    which in a valid archive is every record;
  - page `i` covers the keys from `first_key` of page `i` up to, but not
    including, `first_key` of page `i + 1`; the last page has no upper bound;
  - `list(prefix)` reads every page whose range includes at least one key
    starting with `prefix`, including pages that turn out to hold only
    hidden keys, and fails with an entry error if one of them cannot be
    parsed.

  Exactly, with byte strings compared in UTF-8 order: page `i` with range
  `[lo, hi)` is read if and only if
  - `hi` is absent or `prefix < hi`, and
  - `lo <= prefix`, or `lo` starts with `prefix`.

  (Every key starting with `prefix` is at least `prefix`, and the keys
  starting with `prefix` form one contiguous run in UTF-8 order.) Example:
  for pages with first keys `a/0`, `a/5`, `b/0` and the prefix `a/`, the
  first page is read (`a/0` starts with `a/`), and so is the second (`a/5`
  starts with `a/`). The third page is not read: `b/0` doesn't start with
  `a/`, and `a/` is less than `b/0`.

  A page **cannot be parsed** if it is not a sequence of whole central
  directory records with correct signatures that exactly fills the page.
- **raw(key)** → the inflated body of the key's entry, or *missing*. In this
  view every present entry is visible, including hidden and format entries,
  and a reference entry yields its body (empty, or its payload). This is the
  view of a ZIP tool that knows nothing about vzip. The two format entries
  are read through the comment's offsets and sizes (§3.4), like everywhere
  else; `raw("__vz__/index")` is *missing* in an archive without a page
  index.

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
     a resolution error if the source cannot be resolved (§6), if a pin on
     the source fails or cannot be checked (§6.1), or if the source value is
     shorter than `offset + j`.
4. Ranges that do not overlap the window are not resolved and cause no
   error, even if their source is missing or too short. This includes every
   zero-length range.

Readers SHOULD fetch only the bytes in step 3. A reader MUST NOT return bytes
for a request it could not resolve completely.

### 8.4 Errors

There are six classes of error. Each says which operations it makes fail.

| class | causes | effect |
|---|---|---|
| **archive error** | everything §8.1 checks | opening fails |
| **entry error** | the key's record violates §4.1 (unparseable extra field, several reference blocks), uses a method other than 0 and 8, has bit 0 set, is a reference entry with method 8 (§4.3), or breaks the ZIP64 block rules (§3.2); or the page that lookup (§7.2) selects for the key cannot be parsed. In that last case the key is reported as an entry error even if it isn't in the page, because the reader cannot tell. | classify, get and raw fail for that key; other keys are unaffected |
| **body error** | an entry's body (`[body offset, body offset + compressed size)`, judged as a whole even for a small window) lies outside the file; a DEFLATE body does not inflate cleanly (§8.1); a STORED entry's compressed and uncompressed sizes differ. This applies to bytes entries for get and raw, and to reference entries for raw. | get and raw of that key fail; a range of a `key` source naming it fails with a resolution error |
| **payload error** | the reference payload is malformed (§5) | get fails for that key |
| **resolution error** | §6, §6.1, §8.3 step 3 | get fails for the requests that need the range |
| **request error** | `start > end`; a request exceeding a resource limit the reader documents (§10) | that operation fails |

Resource limits are implementation-defined. Conformance suites do not test
them, and the classes of errors they cause may differ between readers.

Each operation checks in this order, and reports the first failure. A
resource limit is the one exception: it may be reported at whatever step the
reader reaches it.

1. the request (request error);
2. whether the key is hidden: hidden keys are *missing* for classify and get;
3. the lookup of the key's record, and that record's entry error;
4. for get of a bytes entry, or raw: the body (body error). A DEFLATE body
   is always inflated and checked in full, even for a small window, so
   corruption anywhere in it is reported. A STORED body is read only within
   the window.
5. for get of a reference: the payload (payload error), then the ranges, in
   order (resolution errors). Payload checks cover every range, including
   zero-length ranges and ranges outside the window: a `source` index out of
   bounds is a payload error even when that range would not be resolved.

### 8.5 Error reporting

A failed operation returns no bytes. A missing key is not an error. Readers
SHOULD report the class of an error along with its message.

### 8.6 Violations readers need not detect

For archives that break these writer requirements, reader results are
unspecified:

- §3.1 rules 1, 2, 4, 5 (except bit 0), 6, 7 and 8: readers use body offsets
  computed from the central directory, need not read local headers, and need
  not verify CRC-32 values. Duplicate names are included here. A reader that
  does verify a CRC-32 and finds a mismatch reports a body error.
- §3.2: whether ZIP64 structures were used only where needed.
- §3.4 and §4.1: whether the format entries' records agree with the comment,
  are `bytes` entries, and use method 8.
- §4.3: whether a reference entry's body is empty or equal to its payload.
  Readers MUST still ignore the body, except in the raw view.
- §7.1: whether the central directory matches the page index (beyond the
  checks in §7.2), and whether pinned values match their records.

## 9. Writing

### 9.1 Requirements

A writer produces an archive from a list of entries (each either bytes or a
list of ranges) and a source table. It MUST produce a valid archive, and it
MUST reject its input, producing no archive, if:

- a key is empty, not valid UTF-8, duplicated, or a format entry's key;
- a range's `source` is not less than the number of sources (this includes
  every source range when there are no sources);
- a `url` source is empty or does not match RFC 3986 `URI-reference`;
- a `key` source is empty, or names a key that is absent, is a reference
  entry, or is a format entry;
- a source range of a `key` or `data` source extends past the end of that
  source's value, that is, `offset + length` is greater than the value's
  size. This includes a zero-length range whose offset is past the end. The
  writer knows these values; `url` sources are not checked;
- a pin is on a `key` or `data` source, or an `etag` pin is not a strong
  entity tag in double quotes (§6.1);
- a reference payload exceeds 65519 bytes (§4.3);
- a source range's `offset + length`, or a reference's total size, exceeds
  2^64 − 1 (§5.2, §5.3);
- a key's UTF-8 encoding is longer than 65535 bytes;
- a bytes entry's uncompressed or compressed size is 0xFFFFFFFF or more
  (§3.1 rule 7);
- a pinned entry is not a bytes entry, is a format entry, or is listed
  twice.

`__vz__/index` MUST follow every pinned entry: it records their body
offsets, so writing it earlier would shift them by its own size. The page
offsets it records are relative to the central directory, which always
follows every entry, so they don't constrain where the index goes.

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
  can use, and document the bound; a request beyond it is a request error.
- Pins (§6.1) detect accidental replacement of a referenced object, not
  tampering: the archive itself is not signed, and a `modified_not_after`
  pin cannot see a rewrite within the same second.

## Appendix A: Complete schema

```proto
syntax = "proto3";
package vzip.v0;

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
  optional uint64 size               = 4;
  optional string etag               = 5;
  optional int64  modified_not_after = 6;
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
| comment magic             | `vzip/0` (6 bytes) |
| comment lengths           | 22 (no page index), 38 (page index) |
