# refkv: a reference value type for key-value stores

Format version: 0 (**provisional**) · Specification revision: 11.2

## 1. Introduction

refkv ("reference key value") is a reference value type for key-value
stores: a store in which every value is either bytes or a **reference**, a
description of how to assemble the value from byte ranges of other objects.
Because the reference type does not depend on any backend, any storage
backend that can tell a bytes value from a reference value can hold the same
reference store.

The specification has three parts, in three documents:

- [Part 1](#part-1-reference-values), this document, specifies the reference
  value type and everything that follows from it without a backend: the data
  model, the encoded references, the table of the objects they read, how
  readers resolve them, the errors, the reader policy and the writer's checks.
- [Part 2](bindings/vzip.md#part-2-the-zip-binding), in
  [spec/bindings/vzip.md](bindings/vzip.md), is the **ZIP binding**: how a
  store is kept in a single ZIP file, a **vzip archive**.
- [Part 3](bindings/phkv.md#part-3-the-perfect-hash-binding), in
  [spec/bindings/phkv.md](bindings/phkv.md), is the **perfect-hash binding**,
  a **draft**: how a store is kept in a single file indexed by a perfect hash,
  a **phkv store**, on its own or inside a vzip archive.

The documents share this introduction, one format version and one
revision, and number their sections as one specification: Part 2 starts at
[§9](bindings/vzip.md#9-what-the-zip-binding-defines), and Part 3 at
[§16](bindings/phkv.md#16-what-the-phkv-binding-defines).

A binding defines only three things ([§3](#3-bindings)): how a value is
marked as a reference, where the store's source table is kept, and how keys
map to the backend's names. ZIP is the one binding the specification makes
normative; the perfect-hash binding is a draft.

A reference reads its bytes from external objects named by URL, from other
values of the same store, or from literal bytes it carries. The motivating
use is "virtual" Zarr datasets, whose chunks live inside other files (HDF5,
netCDF, TIFF, ...). Nothing in this specification depends on Zarr; keys are
opaque strings.

### 1.1 Conventions

The key words MUST, MUST NOT, REQUIRED, SHOULD, SHOULD NOT, MAY, and OPTIONAL
are to be interpreted as described in RFC 2119.

A **store** is the key-value mapping of [§2](#2-data-model), as a backend
holds it. A **binding** says how a backend holds a store
([§3](#3-bindings)). A store held by the ZIP binding is a **vzip archive**,
or an **archive** for short. Part 1 says "store" wherever any binding is
meant.

Byte ranges are written `[a, b)`: start inclusive, end exclusive. Strings are
compared by their UTF-8 bytes, lexicographically ("UTF-8 order").

### 1.2 Conformance

This document places requirements on **writers** (programs that create
stores) and **readers** (programs that read them). A **valid store**
satisfies every requirement placed on writers, by Part 1 and by its binding.
A **valid archive** is a valid store held by the ZIP binding.

Reader behavior is fully specified for valid stores, and for the invalid
stores that [§6](#6-reading) and the binding ([§13](bindings/vzip.md#13-reading-a-vzip-archive)
for ZIP) classify. [§6.6](#66-violations-readers-need-not-detect) and
[§13.4](bindings/vzip.md#134-violations-readers-need-not-detect) list the writer violations
that readers are not required to detect; for those, the results are
unspecified.

A reader or a writer follows Part 1 and a binding. The specification defines
one binding, ZIP ([spec/bindings/vzip.md](bindings/vzip.md)), and its
conformance suite tests vzip archives.

### 1.3 Versioning

refkv has an integer **format version**, recorded where the binding says
([§3.1](#31-what-a-binding-defines)): a vzip archive writes it in its comment
as the magic `vzip/<version>`
([§10.4](bindings/vzip.md#104-end-of-central-directory-record-and-archive-comment)). This
document specifies format version **0**. A format version covers Part 1 and
the ZIP binding together: a change to either is a change to the format.

- **What a version is:** a format version fixes the meaning of every valid
  store. Readers implement a set of versions and MUST reject a store whose
  version they don't implement, as an archive error. Readers MUST NOT guess
  at an unknown version, even a higher one.
- **Revisions of the specification:** the specification itself has revisions, such as
  "version 0, revision 5". A revision may:
  - clarify text;
  - resolve an ambiguity by choosing one of the readings implementations
    already used;
  - fix a contradiction;
  - add conformance tests.

  A revision does not change the result of any operation on a valid store
  whose result was already fully determined by the previous revision.
- **What needs a new version:** anything else, such as new message fields,
  new pins, new source kinds, or changed layout rules. This is necessary
  because readers skip unknown protobuf fields ([§4.1](#41-encoding)): a
  field added within a version would be silently ignored by older readers.
  For a field like a pin, that would turn "fail closed" into "fail open".
- **Writers** SHOULD write the lowest format version that can express the
  store, so that the most readers can read it.
- **Conformance:** a conformance suite states the format version and the
  spec revision it tests.
- **The recorded revision:** every store records, in its source table
  ([§5](#5-source-table)), the revision of this document its writer followed
  (the whole number: 9 for revision 9, 9.1 or 9.2). Readers MUST NOT reject a
  store because of its recorded revision, or because it has none; they MAY
  report it. It tells apart stores written under different revisions of a
  format version, which matters while that version is provisional and
  revisions may still change results.

**Releases.** Until refkv's first release, every store is format version 0
and records its revision as above. The release rule is:

1. At the first release, the text of this document becomes format version
   1, and the commit is tagged `refkv-format-v1` in the specification's
   repository.
2. After that, every **breaking change** (any change that
   [§1.3](#13-versioning) says needs a new version, and any change to the
   result of an operation on a valid store) increments the format version
   recorded by the binding (the magic of a vzip archive,
   [§10.4](bindings/vzip.md#104-end-of-central-directory-record-and-archive-comment)), and
   the commit that makes it is tagged `refkv-format-v<N>`. Changes that are
   not breaking are revisions of the current version, and need no tag.
3. Readers MUST reject a store whose format version they don't implement
   ([§10.4](bindings/vzip.md#104-end-of-central-directory-record-and-archive-comment)),
   which is what keeps every breaking change from being read with the wrong
   meaning.

**Status.** A format version is in one of three states:

| status | meaning |
|---|---|
| draft | Anything may change. |
| **provisional** | The format is believed complete, and implementers may rely on it. Revisions may still change it in response to feedback from outside the project. Every such change is listed, with its reason, in the [changelog](history/archive/REVISIONS.md). An incompatible change (one that changes the result of an operation on a store that was valid before) must also be announced there as incompatible, with the conformance suite updated to match. Incompatible changes are avoided wherever a compatible one would do. |
| final | No incompatible change is ever made. Changes of that kind become the next format version. |

**Format version 0 is provisional** as of revision 8. Its draft history,
revisions 1–7, was driven by seven rounds of independent implementations
(see the changelog). It becomes final once it has gone through a period of
outside review with no incompatible change needed.

Revision 11 made an **incompatible** change to the ZIP binding in response
to outside review: the ZIP64 end records are present in every archive
([§10.2](bindings/vzip.md#102-zip64)), so an archive written to an earlier revision without
them is no longer valid. It also allowed entries of 4 GiB or more
([§10.1](bindings/vzip.md#101-structure) rule 7), which changes nothing for an archive that
was valid before.

**Feedback** — ambiguities, implementation reports, test cases, objections —
is welcome as issues or pull requests on the specification's repository. A
report is most useful when it names the section, the input, and the
behavior each reading would produce.

### 1.4 Prior work (informative)

refkv is a binary encoding for a model of references that other projects
established. This section credits them; nothing in it is normative.

- **kerchunk.** Virtual Zarr originates with
  [kerchunk](https://github.com/fsspec/kerchunk) (Martin Durant and
  contributors, fsspec). kerchunk records where the chunks of HDF5, netCDF,
  GRIB, TIFF and FITS files live, and fsspec's `ReferenceFileSystem` serves
  them so that Zarr can read the files in place. refkv's data model
  ([§2](#2-data-model)) is the one defined by kerchunk's
  [reference specification](https://fsspec.github.io/kerchunk/spec.html):

  | kerchunk reference specification | refkv |
  |---|---|
  | a string value: inline data | a `bytes` value ([§2](#2-data-model)) |
  | `[url, offset, length]` | a reference with one source range ([§4.2](#42-range)) |
  | a `base64:` string: inline binary data | a literal range ([§4.2](#42-range)), or a `data` source ([§5](#5-source-table)) |
  | `templates` shared by many URLs | the source table ([§5](#5-source-table)) |

  kerchunk's version 1 specification anticipated "future possible binary
  storage" of references. This document specifies one. kerchunk's Parquet
  layout showed how compactly references can be stored.
- **VirtualiZarr.**
  [VirtualiZarr](https://github.com/zarr-developers/VirtualiZarr) (started
  by Tom Nicholas, a zarr-developers project, grown out of kerchunk) recast
  virtual Zarr as chunk manifests: a path, offset and length for every chunk
  of a Zarr array. Those manifests are what a store's references record,
  and the reference implementation's converters take VirtualiZarr datasets
  as input. The need for a Zarr-native on-disk manifest format, raised in
  [zarr-specs#287](https://github.com/zarr-developers/zarr-specs/issues/287),
  motivated this work.
- **Icechunk.** Source pins ([§5.1](#51-pins)) adapt
  [Icechunk](https://icechunk.io/)'s checks of virtual chunks against the
  ETag or modification time of the object they reference.

What refkv contributes is a reference value type with a binary encoding that
does not depend on the backend; its binding to a single ZIP file; and a
specification with a conformance suite that is independent of any one
language or library.

# Part 1. Reference values

This part does not depend on any backend, and uses no ZIP terms. Where a
rule needs the backend, it says what the binding defines
([§3.1](#31-what-a-binding-defines)).

## 2. Data model

A store defines a partial function from **keys** to **values**:

- A **key** is a non-empty Unicode string ([§2.1](#21-keys)).
- A **value** is a finite byte sequence (possibly empty).

Each key present in the store has a **kind**:

| kind        | value                                                         |
|-------------|---------------------------------------------------------------|
| `bytes`     | the bytes the store holds under the key                       |
| `reference` | the bytes described by the key's reference payload ([§4.4](#44-reference-payloads)) |

A key that is not present has kind `missing` and no value. Asking for a
missing key is not an error.

Both kinds share one key space. The binding says how a reader tells them
apart ([§3.1](#31-what-a-binding-defines)).

A store also has exactly one **source table** ([§5](#5-source-table)), the
list of objects that references read. It is not a value of the mapping.

### 2.1 Keys

A key is stored as its UTF-8 encoding, which the binding maps to the
backend's names ([§3.1](#31-what-a-binding-defines)). A key MUST NOT be
empty and MUST be valid UTF-8. Keys are otherwise unrestricted; for example,
`a/../b` and `x/` are ordinary distinct keys
([§8](#8-security-considerations)).

Keys are compared and sorted as byte strings (UTF-8 order), never as
decoded text. In particular:

- A leading U+FEFF (byte order mark) is part of the key. Decoders that strip
  it must be told not to.
- UTF-8 order is code point order, which differs from UTF-16 code unit order
  for characters above U+FFFF. For example, `"\u{1F600}"` (😀) sorts after
  `"\u{FF5E}"` (～) in UTF-8 order but before it in UTF-16 order. Several
  languages' default string sort uses UTF-16 order.

## 3. Bindings

A **binding** says how a backend holds a store.

### 3.1 What a binding defines

A binding defines exactly three things:

1. **Marking:** how a value is marked as a reference rather than bytes,
   where its reference payload is kept, and whether that payload is a
   `Range` or a `Concat` ([§4.4](#44-reference-payloads)).
2. **The source table:** where and how the store's source table
   ([§5](#5-source-table)) is stored, and how a reader finds it, together
   with any auxiliary data of the binding's own, such as ZIP's page index.
   This is also where the binding identifies the store as refkv and records
   its format version ([§1.3](#13-versioning)).
3. **Names:** how keys map to the backend's names, and back, including any
   names the binding reserves for itself (for its source table or its
   auxiliary data) and how it keeps them out of the mapping.

Everything else comes from Part 1: the messages, the contents of the source
table and its pins, how references resolve, the operations, the error
classes and their order, the reader policy, and the writer's checks.

A binding may add causes to the error classes of [§6.4](#64-errors) for what
its backend can get wrong, and limits that follow from its encoding, such as
a maximum payload size. It MUST NOT add an error class, change what an
operation returns for a valid store, or relax a requirement of Part 1.

ZIP is the one normative binding. The perfect-hash binding
([Part 3](bindings/phkv.md), [§16](bindings/phkv.md#16-what-the-phkv-binding-defines))
is a draft, and its own document says what it defines. [§9](bindings/vzip.md#9-what-the-zip-binding-defines)
gives its three answers, and the rest of Part 2, in
[spec/bindings/vzip.md](bindings/vzip.md), what the ZIP container needs.

### 3.2 Other backends (informative)

Nothing in Part 1 needs ZIP. Besides ZIP, the specification has only the
draft perfect-hash binding (Part 3), but other backends could hold a store:

- **Object stores** (S3 and similar): each key is an object. A reference is
  an object whose `Content-Type` is a refkv media type, or whose user metadata
  holds a marker, with the payload in its body or in that metadata.
- **File systems:** each key is a file. A reference is a file with an
  extended attribute (xattr) that holds the payload, or marks the file's
  contents as one.
- **Databases:** a table of `(key, kind, value)` rows, where the `kind`
  column says whether `value` holds bytes or a reference payload.
- **HTTP:** a server returns each value with a response header that says
  whether its body is bytes or a reference payload.

In each case the backend needs only a way to tell a bytes value from a
reference value. A key-value backend could keep the source table under the
ZIP binding's name for it, `__vz__/sources` ([§11.4](bindings/vzip.md#114-hidden-entries)),
reserved in the same way; that is a suggestion, not a rule. A binding for
one of them would also have to settle what ZIP settles by being a single
file that is written once:

- **Atomicity of the source table.** A vzip archive's source table always
  matches its references. In a backend whose values change one at a time,
  a reference and the source table it numbers into can change separately. A
  binding must say how a reader sees a consistent pair, for example by
  never renumbering or removing sources, by writing new sources before the
  references that use them, or by versioning the table.
- **Listing.** `list` returns keys in UTF-8 order
  ([§6.2](#62-operations)). A binding for a backend that lists in another
  order, or not at all, must say how a reader produces that result, and
  what it costs.
- **The base URI.** Relative `url` sources resolve against the store's base
  URI ([§5](#5-source-table)). A store that is not one file must say what
  that URI is, for example the prefix of its objects.
- **Names.** Which keys the backend can name (length limits, reserved names,
  case folding), and how the names the binding reserves for itself stay out
  of the mapping.
- **Recognition.** Where a reader finds the format version before it reads
  anything else, and how it tells a store from unrelated data.
- **Payload size.** How large a payload the marking can hold (user metadata
  and extended attributes are small), which is the binding's payload limit
  ([§4.4](#44-reference-payloads)).
- **Errors and the raw view.** Which of the backend's failures are archive,
  entry and body errors ([§6.4](#64-errors)), and what `raw` returns for a
  reference ([§6.2](#62-operations)).

## 4. Messages

### 4.1 Encoding

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

### 4.2 Range

```proto
message Range {
  uint32 source = 1;
  reserved 2;
  uint64 offset = 3;
  uint64 length = 4;
  optional bytes data = 5;
  optional uint32 crc32c = 6;
}
```

A Range describes a byte sequence of a known **size**:

- **Literal range**: field 5 (`data`) is present, even with zero length. The
  bytes are `data`, and the size is `len(data)`. In a literal range,
  `source`, `offset` and `length` MUST all have the value 0; otherwise the
  payload is malformed. Only the values are checked: a field explicitly
  encoded as 0 is accepted, like any non-canonical encoding
  ([§4.1](#41-encoding)).
- **Source range**: field 5 is absent. The bytes are
  `[offset, offset + length)` of the **source value** of source number
  `source` of the source table ([§5](#5-source-table)), and the size is `length`.
  `source` MUST be less than the number of sources, and `offset + length`
  MUST NOT exceed 2^64 − 1; otherwise the payload is malformed.

The size is known without I/O.

**Checksum.** A source range MAY carry field 6, `crc32c`: the CRC-32C of its
bytes, that is of bytes `[offset, offset + length)` of the source value. A
literal range with field 6 present is malformed. CRC-32C is the CRC of
RFC 3720 §B.4 (iSCSI): the Castagnoli polynomial 0x1EDC6F41, reflected
input and output, initial value and final XOR 0xFFFFFFFF. Its check value,
for the nine ASCII bytes `123456789`, is 0xE3069283; for the empty sequence
it is 0. Readers check it after they read the range
([§6.3](#63-resolving-a-reference)), so it detects a changed source wherever
the range can be read, including where pins cannot be checked because the
reader cannot see the response's headers (a browser reading a cross-origin
object, [§5.2](#52-reading-over-http)). It covers only the bytes of the
range: a change elsewhere in the object goes unnoticed.

### 4.3 Concat

```proto
message Concat {
  repeated Range parts = 1;
}
```

A Concat describes the concatenation of its parts, in order. Its size is the
sum of the parts' sizes, which MUST NOT exceed 2^64 − 1. A Concat with no
parts describes the empty value.

### 4.4 Reference payloads

The **reference payload** of a reference is one encoded message:

- a `Range` ([§4.2](#42-range)), describing a value made of that one range;
  or
- a `Concat` ([§4.3](#43-concat)), describing a value made of zero or more
  ranges.

The binding records which of the two a payload is
([§3.1](#31-what-a-binding-defines)). Writers MUST use a `Range` when a
reference has exactly one range, and a `Concat` otherwise. Readers MUST
accept a `Concat` with any number of parts.

A payload is **malformed** if it breaks a rule of [§4.1](#41-encoding),
[§4.2](#42-range) or [§4.3](#43-concat). A binding may limit the size of a
payload ([§11.3](bindings/vzip.md#113-reference-entries) for ZIP); a payload over that limit
is malformed too.

### 4.5 Where bytes that no source holds go (informative)

Most of a value usually comes from source ranges, but some values also need
bytes that exist in no source: a header rewritten for one tile, so that the
tile decodes on its own; a shard index that a format never wrote; a decoding
header that a file stores once and every tile needs. A reference can carry
such bytes three ways, and each fits a different case:

| bytes that are | put them in | because |
|---|---|---|
| used by one value only, such as one tile's rewritten header or one shard's index | a literal range ([§4.2](#42-range)), in that value's reference | they are read with the reference that needs them, and only when it is read |
| shared by many values, such as the JPEG tables every tile of a slide needs | a `data` source ([§5](#5-source-table)), named by a source range of each reference | they are stored once, however many references use them |
| a value of the store in their own right | a `bytes` value, named by a `key` source ([§5](#5-source-table)) | a reader can get them by their key, and references reuse them without a copy |

The first two trade storage against what a reader reads at open. The source
table is read in full when a store is opened ([§6.1](#61-opening)), while a
reference is read only when its key is. A literal range used by one value
costs nothing until that value is read; the same bytes as a `data` source
would be read by every reader at open, so a store with one such header per
chunk would load all of them before reading any. A `data` source shared by
many values is read once instead of once per reference. Literal ranges also
represent the inline values of other reference formats, such as kerchunk's
`base64:` strings ([§1.4](#14-prior-work-informative)).

## 5. Source table

A store has exactly one source table. The `source` field of every source
range ([§4.2](#42-range)) is an index into it. The binding says where the
table is stored ([§3.1](#31-what-a-binding-defines)); a vzip archive keeps it
in the entry `__vz__/sources` ([§11.4](bindings/vzip.md#114-hidden-entries)). The table is
a `SourceTable`:

```proto
message Source {
  oneof kind {
    string url  = 1;
    string key  = 2;
    bytes  data = 3;
  }
  // Pins (§5.1); allowed only on `url` sources.
  optional uint64 size               = 4;
  optional string etag               = 5;
  optional int64  modified_not_after = 6;
}

message SourceTable {
  repeated Source sources = 1;
  optional uint32 revision = 2;
}
```

`revision` is the revision of this document the writer followed
([§1.3](#13-versioning)). Writers MUST write it; readers only report it.

Sources are numbered from 0 in wire order. When the table is read
([§6.1](#61-opening)), any of the following is an archive error:

- the table is malformed;
- a Source has no `kind` member;
- a `url` or a `key` is empty;
- a pin appears on a `key` or `data` source;
- an `etag` pin is not a strong entity tag ([§5.1](#51-pins)).

The **source value** of each kind:

- **`url`**: the bytes of an external object.
  - `url` MUST match the `URI-reference` rule of RFC 3986 §4.1 exactly. It
    is therefore ASCII only, with every other byte percent-encoded (non-ASCII
    characters as their UTF-8 bytes). For example, a file named `a b.bin` is
    referenced as `a%20b.bin`, and `é.bin` as `%C3%A9.bin`.
  - It is resolved against the store's **base URI** using the strict
    algorithm of RFC 3986 §5.2.2: a reference with a scheme is used as it
    is, even if the scheme matches the base's.
  - When a store is opened from a local path, its base URI is built in
    two steps:
    - Make the path absolute and normalize it. A relative path is joined to
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
      percent-encoding. The one exception is a UNC path on Windows, below,
      which the application must allow;
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

    This mapping is for POSIX paths. On Windows, where a path has a drive
    letter or a UNC server and share and `\` separates its segments, the
    base URI and the mapping follow
    [RFC 8089 Appendix E](https://www.rfc-editor.org/rfc/rfc8089#appendix-E):
    - Building the base URI, the absolute path is normalized as above, and
      each `\` is written as `/`. A drive-letter path gets a leading `/`,
      so `C:\data\my file.vzip` becomes `file:///C:/data/my%20file.vzip`.
      A UNC path's server is the authority, so `\\server\share\x.vzip`
      becomes `file://server/share/x.vzip`. A path is UTF-8 here, and the
      same bytes are percent-encoded as on POSIX.
    - Mapping a `file:` URI, the rules above apply, and also: the decoded
      path MUST be UTF-8, MUST NOT contain `\` (as from `%5C`), and its first
      segment MUST be a drive letter followed by `:` (or `|`, as
      RFC 8089 allows), so `file:///C:/data/x` maps to `C:\data\x`. A path
      without a drive letter, such as `file:///data/x`, is a resolution
      error: it would name a file on whichever drive is current.
    - An authority other than `localhost` names a UNC server:
      `file://server/share/x` maps to `\\server\share\x`, provided the
      authority is a host name alone (no userinfo, port or
      percent-encoding) and the path names a share. Reading it connects to
      that server over the network, which may send it the user's
      credentials, so a reader MUST refuse it, as a resolution error,
      unless the application allows **file hosts**: a setting of the
      policy ([§6.7](#67-reader-policy)) whose name says that it is unsafe,
      separate from the one that lets a remote store read `file:` URLs.
      A store opened from a UNC path reads itself without it, since the
      application named that path; the sources it names need it. On other
      systems such a URI has no mapping and is always a resolution error.

  Readers MUST support `file:`, SHOULD support `http:` and `https:` (byte
  ranges via HTTP Range requests), and MAY support other schemes. It is a
  resolution error if the reference is not a valid URI reference, uses an
  unsupported scheme, or violates the `file:` rules, if the reader's policy
  refuses the URL ([§6.7](#67-reader-policy)), or if the object cannot be
  read ([§5.2](#52-reading-over-http) for HTTP). Readers check URL syntax
  only when they resolve a range of the source, not at open; writers check
  it when writing ([§7](#7-writing)). A reference with only a fragment (such
  as `#x`) resolves to the store's own location (for a vzip archive, the
  archive file); that is valid but rarely useful.
- **`key`**: the value of the `bytes` key of this store with that name. A
  binding may also let it name bytes that the binding keeps out of the
  mapping ([§11.4](bindings/vzip.md#114-hidden-entries) for ZIP). It is a resolution error
  if the key is missing, is a reference, or has an entry error or a body
  error ([§6.4](#64-errors)). References therefore never chain.
- **`data`**: the bytes of `data`. Writers SHOULD use this for small byte
  strings shared by many ranges, such as a decoding header. Bytes used by a
  single range belong in a literal range instead.

Readers MUST NOT resolve sources except as
[§6.3](#63-resolving-a-reference) requires. A store whose source table names
an unreachable URL is fully readable, except for the ranges that use that
URL.

### 5.1 Pins

A reference names bytes the store does not control. If the object behind a
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
weak tag (`W/"..."`), is an archive error ([§5](#5-source-table)).

Before returning any byte read from a pinned source, a reader MUST check
every pin on that source. A pin that fails, or that the reader cannot check
for that scheme, is a resolution error. A reader MUST NOT skip a pin it
does not understand: pins fail closed. The one exception is a pin that the
response's headers would check but that the reader cannot see, which an
application may allow the reader to skip ([§6.7](#67-reader-policy)).
Checks, by scheme:

| pin | `http:`/`https:` | `file:` |
|---|---|---|
| `size` | the object's size, from the response ([§5.2](#52-reading-over-http)) | the file's size |
| `etag` | send `If-Match: <etag>` with the range request; a 412 response fails the pin | cannot be checked: resolution error |
| `modified_not_after` | send `If-Unmodified-Since: <HTTP-date of the pin>` with the range request; a 412 response fails the pin | the file's modification time, rounded down to whole seconds (as above) |

Over HTTP the checks ride on the range requests the reader makes anyway, so
pins cost no extra requests. A reader that checks a pin once per source
SHOULD do so per opening of the store, not cache the result across opens.

Writers SHOULD:

- pin `size` on every `url` source whose size they know. It costs nothing
  to check over HTTP (the size is in every range response) and catches
  appends and most rewrites. Modification times have one-second
  resolution, so a rewrite within the same second as `modified_not_after` is
  not detected, while `size` may catch it;
- pin `etag` when they saw a strong entity tag for the object, from a
  response that also gave them the bytes they describe;
- take `modified_not_after` from the object itself (its `Last-Modified`
  value, or its file modification time), not from the writer's clock. A
  value from the writer's clock fails whenever the server's clock or the
  object's timestamp is ahead of it;
- add a `crc32c` ([§4.2](#42-range)) to ranges whose bytes they read anyway,
  or when asked to: it is the only check that does not depend on headers;
- pin only `size` in stores meant to be copied together with their data
  ([§5](#5-source-table), relative URLs). Copying an object changes its
  modification time and may change its entity tag.

### 5.2 Reading over HTTP

This section applies to readers that support `http:` and `https:`.

- **Requests:** a read of bytes `[a, b)` of an HTTP object is a GET with
  `Range: bytes=a-(b−1)` and `Accept-Encoding: identity`. A reader SHOULD
  combine reads of nearby ranges of the same object into one request
  ([§6.3](#63-resolving-a-reference)). It MUST NOT make any other request
  to resolve a range or check its pins: no HEAD, and no request for the
  whole object.
- **Pins** add `If-Match` and `If-Unmodified-Since` headers to those same
  requests ([§5.1](#51-pins)). Servers and proxies may ignore those headers,
  so a reader also checks every successful (200 or 206) response itself:
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
  - only to a URL the reader's policy allows ([§6.7](#67-reader-policy)); a
    redirect to any other URL is a resolution error, and the reader SHOULD
    detect it before it sends the request. A reader that cannot see a
    redirect before it is followed (a browser's `fetch`) checks the final
    URL instead;
  - each hop re-sends `Range`, `Accept-Encoding` and the pin headers. Pins
    and the size apply to the final response.
- **URLs:** an `http:` or `https:` URL is a resolution error if it has
  userinfo (`user@`), an empty or absent host (`http:/x` and `http:///x`
  included), or a port above 65535. An empty port (`host:`) means the
  default port. These rules apply to every URL requested, including
  redirect targets.
- **Memory:** a 200 response delivers the whole object. A reader MAY refuse
  to read a body larger than a documented resource limit
  ([§8](#8-security-considerations)); that is a request error.
- **Timestamps:** `If-Unmodified-Since` carries `modified_not_after` as an
  IMF-fixdate (RFC 9110 §5.6.7). A value outside the years 1–9999 cannot be
  sent, so the pin cannot be checked: resolution error.

## 6. Reading

### 6.1 Opening

Opening a store reads and checks the following. Any failure is an
**archive error**, and opening fails:

- what the binding checks at open: that the store is one, of a format
  version the reader implements, and that its source table and auxiliary
  data can be found and read ([§13.1](bindings/vzip.md#131-opening) for ZIP);
- the source table, which must be valid ([§5](#5-source-table));
- the resource limits that apply at open ([§6.7](#67-reader-policy),
  [§8](#8-security-considerations)). A resource limit reached while
  opening, for example while reading the source table, or a source table
  with more sources than the reader allows, is an archive error;
- for a reader that applies the host rule to the store's own requests
  ([§6.7](#67-reader-policy) rule 3), that it allows the addresses
  and redirects that opening reaches.

A reader MAY read more, but MUST NOT report an error at open that this
section and its binding do not list.

### 6.2 Operations

A conforming reader provides the following operations on an opened store:

- **classify(key)** → `bytes` | `reference` | `missing`. A name the binding
  keeps out of the mapping ([§3.1](#31-what-a-binding-defines)) classifies
  as `missing`. It looks only at how the key is marked, so a reference with
  a malformed payload still classifies as `reference`.
- **get(key, request)** → a byte sequence, *missing*, or an error. A name
  the binding keeps out of the mapping is *missing*. `request` is one of:
  - *whole*: the whole value;
  - *range(start, end)* with `start <= end`: bytes `[min(start, n), min(end, n))`
    of the value, where `n` is the value's size;
  - *offset(start)*: bytes `[min(start, n), n)`;
  - *suffix(count)*: bytes `[max(n − count, 0), n)`.

  `start > end` is a request error, whether or not the key is present.
- **list(prefix)** → every present key of the mapping that starts with
  `prefix` (compared on UTF-8 bytes), sorted in UTF-8 order. The empty
  prefix lists all keys. Keys with entry errors are listed. The binding says
  how a reader finds the keys, and which of its failures make `list` fail
  ([§13.2](bindings/vzip.md#132-operations) for ZIP).
- **raw(key)** → what the backend holds under the key's name, without
  interpreting references, or *missing*. This view also shows the names the
  binding reserves. The binding says what it yields for those and for a
  reference. It is the view of a client of the backend that knows nothing
  about refkv ([§13.2](bindings/vzip.md#132-operations) for ZIP).

### 6.3 Resolving a reference

`get` on a reference proceeds as follows:

1. Decode the payload. If it is malformed
   ([§4.4](#44-reference-payloads)), the `get` fails, whatever the request.
2. Compute the requested window `[a, b)` of the value
   ([§6.2](#62-operations)).
3. For each range whose bytes overlap `[a, b)` in a non-empty interval, take
   those bytes from the range:
   - A literal range needs no I/O.
   - A source range reads bytes `[offset + i, offset + j)` of its source value,
     where `[i, j)` is the overlap relative to the range's start. The read is
     a resolution error if the source cannot be resolved
     ([§5](#5-source-table)), if the reader's policy refuses it
     ([§6.7](#67-reader-policy)), if a pin on the source fails or cannot be
     checked ([§5.1](#51-pins)), or if the source value is shorter than
     `offset + j`.
   - A source range with a `crc32c` ([§4.2](#42-range)) is read whole,
     `[offset, offset + length)`, whatever part of it the window needs. If
     the CRC-32C of the bytes read differs from `crc32c`, it is a
     resolution error. The reader returns the window's part only after the
     check.
4. Ranges that do not overlap the window are not resolved and cause no
   error, even if their source is missing or too short. This includes every
   zero-length range.

**Coalescing.** A reader SHOULD combine reads of step 3 that fall in the
same source value and lie close together into one read that also covers the
bytes between them, and take each range's bytes from that read. Otherwise a
reference made of many short ranges of one object, such as one range per row
of an image whose rows are padded, costs one request per range. The bytes
between the ranges are read but never returned. How close counts as "close"
is up to the reader; the implementations in the specification's repository
combine reads at most 64 KiB apart.

Coalescing never changes the result of a `get`: a combined read ends at the
end of one of the ranges it covers, so it fails only where that range's own
read would. Apart from such gaps, readers SHOULD fetch only the bytes in
step 3. A reader MUST NOT return bytes for a request it could not resolve
completely.

### 6.4 Errors

There are six classes of error. Each says which operations it makes fail.

| class | causes | effect |
|---|---|---|
| **archive error** | everything [§6.1](#61-opening) checks | opening fails |
| **entry error** | the reader cannot tell the key's kind, or the key's own record in the backend breaks a rule of the binding ([§13.3](bindings/vzip.md#133-errors) for ZIP) | classify, get and raw fail for that key; other keys are unaffected |
| **body error** | the bytes the store holds under the key cannot be read as the binding requires ([§13.3](bindings/vzip.md#133-errors) for ZIP). This applies to `bytes` keys for get and raw, and to references for raw. | get and raw of that key fail; a range of a `key` source naming it fails with a resolution error |
| **payload error** | the reference payload is malformed ([§4.4](#44-reference-payloads)) | get fails for that key |
| **resolution error** | [§5](#5-source-table), [§5.1](#51-pins), [§6.3](#63-resolving-a-reference) step 3, a URL the reader's policy refuses ([§6.7](#67-reader-policy)) | get fails for the requests that need the range |
| **request error** | `start > end`; a request exceeding a resource limit the reader documents ([§6.7](#67-reader-policy), [§8](#8-security-considerations)), such as the requests one get may make | that operation fails |

The names are the same in every binding, and are the ones readers report
([§6.5](#65-error-reporting)). They come from the ZIP binding, in which the
store is an archive and a key's record is its entry. A binding adds causes
to the archive, entry and body errors, and may add causes to the payload
error ([§3.1](#31-what-a-binding-defines)).

Resource limits are implementation-defined. Conformance suites do not test
them, and the classes of errors they cause may differ between readers.

Each operation checks in this order, and reports the first failure. A
resource limit is the one exception: it may be reported at whatever step the
reader reaches it.

1. the request (request error);
2. whether the binding keeps the key's name out of the mapping
   ([§3.1](#31-what-a-binding-defines)): such a key is *missing* for
   classify and get;
3. the lookup of the key's record in the backend, and that record's entry
   error;
4. for get of a `bytes` key, or raw: the bytes the store holds (body error).
   The binding says how much of them is read and checked
   ([§13.3](bindings/vzip.md#133-errors) for ZIP);
5. for get of a reference: the payload (payload error), then the ranges, in
   order (resolution errors). Payload checks cover every range, including
   zero-length ranges and ranges outside the window: a `source` index out of
   bounds is a payload error even when that range would not be resolved.

### 6.5 Error reporting

A failed operation returns no bytes. A missing key is not an error. Readers
SHOULD report the class of an error along with its message.

### 6.6 Violations readers need not detect

For stores that break these writer requirements, reader results are
unspecified:

- [§5](#5-source-table): whether the source table records the revision
  ([§1.3](#13-versioning)).

A binding lists its own ([§13.4](bindings/vzip.md#134-violations-readers-need-not-detect)
for ZIP).

### 6.7 Reader policy

A store names the objects its references read, and a reader resolves
them with whatever access it has: local files, private networks, ambient
credentials. A store from an untrusted place could therefore make a
reader read, and return, data its user never meant to expose. Readers MUST
apply a **policy** to every `url` source they resolve, and to every URL they
request on its behalf (redirect targets included). The application chooses
the policy; this section says what it contains and what readers do with
it. A URL the policy refuses is a resolution error for the ranges that need
it; the other limits are resource limits ([§6.4](#64-errors)).

1. **Schemes.** A store is **local** if its base URI
   ([§5](#5-source-table)) has the scheme `file`, that is, if it was opened
   from a local path, and **remote** otherwise. Schemes are compared without
   regard to case. By default a reader allows:
   - `http` and `https` URLs, for every store;
   - `file` URLs, for a local store only.

   Every other scheme, such as `s3`, `gs` or `data`, is refused unless the
   application allows that scheme. An application MAY also allow `file`
   URLs for a remote store, but a reader MUST NOT let the setting that
   allows other schemes do it: it is a separate setting, whose name says
   that it is unsafe. So by default a remote store cannot name
   `file:///etc/passwd`, and no store can name an `s3:` bucket the reader
   has credentials for. A `file:` URL with a host, a UNC path on Windows,
   is refused for every store, whatever the scheme setting or a prefix
   list allows, unless the application allows file hosts
   ([§5](#5-source-table)).
2. **Prefixes.** An application MAY give a list of URL **prefixes**. When it
   does, the list replaces rule 1: a URL is allowed if and only if it
   matches one of the prefixes. A URL `u` matches a prefix `p` if `u` starts
   with `p` (compared as strings, exactly) and either `p` ends with `/`, or
   `u` equals `p`, or the character of `u` after `p` is `/`, `?` or `#`. So
   `https://data.example/a` matches `https://data.example/a/x.bin` but not
   `https://data.example/ab`, and `https://data.example` does not match
   `https://data.example.evil.test/`. A URL whose path holds `%2E`, `%2F`
   or `%5C` (in either case) matches no prefix, since a server may decode
   them into a dot segment or a separator that the comparison did not see.
   Prefixes should be written as resolution produces URLs
   ([§5](#5-source-table)), with a lowercase scheme and host; a URL that is
   spelled differently does not match, which fails closed. A prefix list
   does not lift rule 3.
3. **Hosts.** An IP address belongs to the first of these classes that
   contains it:

   | class | addresses |
   |---|---|
   | loopback | `127.0.0.0/8`, `::1/128` |
   | private | `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7` |
   | link-local | `169.254.0.0/16`, `fe80::/10` (this includes cloud metadata services) |
   | special | `0.0.0.0/8`, `100.64.0.0/10`, `192.0.0.0/24`, `192.0.2.0/24`, `198.18.0.0/15`, `198.51.100.0/24`, `203.0.113.0/24`, `224.0.0.0/3`; `::/8`, `64:ff9b:1::/48`, `100::/63`, `2001::/23`, `2001:db8::/32`, `3fff::/20`, `5f00::/16`, `fec0::/10`, `ff00::/8` |
   | public | every other address |

   An IPv6 address that embeds an IPv4 address belongs to that address's
   class: an IPv4-mapped address (`::ffff:0:0/96`) and a NAT64 address
   (`64:ff9b::/96`) embed their last 32 bits, and a 6to4 address
   (`2002::/16`) the 32 bits after its first 16. So `::ffff:127.0.0.1` is
   loopback. The special class holds the unspecified, shared, reserved,
   documentation, benchmarking, multicast and broadcast ranges. With the
   embedding rule, the non-public classes include every range that the
   IANA special-purpose address registries mark as not globally
   reachable.

   A reader MUST NOT send a request, redirects included, to an address of
   a non-public class, whatever the store's own location (a local store,
   or one on such a host, is no exception), unless the application allows
   **private hosts**. That permission is a setting whose name says that it
   is unsafe, and it allows every class. A refused address is a resolution
   error. The rule applies to the address the request is actually sent
   to, not to the host name in the URL:
   - a reader that resolves names itself MUST resolve the name, check each
     address, and connect only to an address it has checked, keeping the
     URL's host for the `Host` header and the TLS server name (or check
     the connected socket's peer address before it sends the request). A
     connection kept alive from an earlier request is checked again before
     it is reused. A host that is an IP literal is checked as the address it
     denotes, however it is written: a URL parser that accepts decimal,
     octal or hexadecimal IPv4 forms such as `2130706433` or `0x7f.1`
     yields the address `127.0.0.1`;
   - **proxies:** a request sent through a proxy cannot be checked, since
     the proxy connects. While this rule applies, a reader MUST refuse to
     send a request through a proxy (resolution error) unless the
     application allows **unchecked proxies**, a second setting whose name
     says that it is unsafe; the error names that setting. With it, the
     reader checks the addresses the name resolves to where the reader
     runs, which catches literal addresses but not DNS rebinding. A request
     that the reader's proxy settings exempt (for example through
     `no_proxy`) is sent directly, and checked. When the application allows
     private hosts, the rule does not apply, and proxies are used as
     configured;
   - a reader that cannot see addresses at all (a browser's `fetch`)
     checks the host as written in the URL, before the request and on the
     final URL after redirects ([§5.2](#52-reading-over-http)): an IP
     literal by its class, and `localhost` and names under `.localhost` as
     loopback. For names that resolve to other classes, and for redirect
     hops it cannot see, it relies on the platform: browsers that implement
     Private Network Access refuse such requests from public pages unless
     the private server consents. It cannot see the browser's proxy either.

   The store's own location is the application's choice, and this rule
   does not require a reader to check it. A reader MAY apply the rule to
   the store's own requests as well, under the same setting: to the
   host of the store's location, and to every address and redirect target
   those requests reach, as for a source. A refusal while opening is then
   an archive error ([§6.1](#61-opening)); a later one fails the operation
   that needed the request, as any failure to read the store does. A
   store's location may itself come from an untrusted place, such as a
   link, and a public host can redirect the reader's request for it to a
   private one.
4. **Sources.** A reader MUST have a maximum number of sources. Opening a
   store whose source table has more is an archive error.
5. **Requests per value.** A reader MUST have a maximum number of reads of
   `url` sources that one `get` may make, counted after coalescing
   ([§6.3](#63-resolving-a-reference)), with each read counting once however
   many redirects it follows. A `get` that would need more is a request
   error, reported before any of its reads is made.
6. **Data read at open.** A reader MUST have a maximum size for the source
   table and for each piece of the binding's auxiliary data, and MUST stop
   reading or decompressing either as soon as its output passes that size,
   so that a small stored form cannot make it allocate more. Passing it is
   an archive error. The binding says how values are bounded
   ([§13.1](bindings/vzip.md#131-opening) for ZIP).
7. **Unverifiable pins.** An application MAY allow the reader to skip a pin
   that the response's headers would check ([§5.2](#52-reading-over-http))
   when the reader cannot see those headers: a browser reading a
   cross-origin object whose server does not expose `Content-Range`, `ETag`
   or `Last-Modified`. A pin that the reader can check, and that fails,
   still fails, and every `crc32c` is still checked. Without that
   permission, such a pin fails ([§5.1](#51-pins)).

The limits of rules 4–6 are implementation-defined, and readers MUST
document them. The readers in the specification's repository use these
defaults:

| | default |
|---|---|
| schemes (rule 1) | `http` and `https` for every store, `file` for a local one; no other scheme |
| `file` for a remote store | refused |
| a `file:` URL's host (a UNC path on Windows, [§5](#5-source-table)) | refused |
| prefixes | none (rule 1 applies) |
| hosts (rule 3) | public addresses only, for every store; the Python reader applies the rule to a store it reads over HTTP itself, its location and redirects included |
| proxies while rule 3 applies | refused |
| sources per store | 4,194,304 (2^22) |
| reads per `get` | 1024 |
| the source table, or auxiliary data (in a vzip archive, a format entry, inflated) | 256 MiB |
| unverifiable pins | fail |

They name the unsafe settings `allow_files_from_remote_archives`,
`allow_file_hosts`, `allow_private_hosts` and `allow_unchecked_proxy`
(Python), and
`allowFilesFromRemoteArchives`, `allowPrivateHosts` and
`allowUncheckedProxy` (TypeScript). The reference command-line tool takes
`--allow-private-hosts`, which the conformance harness passes, since its
archives' sources are served over HTTP on the loopback interface.

## 7. Writing

A writer produces a store from a list of keys, each with either bytes or a
list of ranges, and a source table. It MUST produce a valid store, and it
MUST reject its input, producing no store, if:

- a key is empty, not valid UTF-8, or duplicated;
- a range's `source` is not less than the number of sources (this includes
  every source range when there are no sources);
- a `url` source is empty or does not match RFC 3986 `URI-reference`;
- a `key` source is empty, or names a key that is absent or is a reference;
- a source range of a `key` or `data` source extends past the end of that
  source's value, that is, `offset + length` is greater than the value's
  size. This includes a zero-length range whose offset is past the end. The
  writer knows these values; `url` sources are not checked;
- a pin is on a `key` or `data` source, or an `etag` pin is not a strong
  entity tag in double quotes ([§5.1](#51-pins));
- a literal range has a `crc32c` ([§4.2](#42-range));
- a reference payload exceeds the binding's limit
  ([§4.4](#44-reference-payloads));
- a source range's `offset + length`, or a reference's total size, exceeds
  2^64 − 1 ([§4.2](#42-range), [§4.3](#43-concat)).

A binding adds the reasons that come from its backend
([§14.1](bindings/vzip.md#141-requirements) for ZIP).

## 8. Security considerations

- A reader resolves the URLs a store names with the reader's own access.
  The policy of [§6.7](#67-reader-policy) limits what that access reaches by
  default:
  - a remote store cannot read local files, and no store can use other
    schemes, such as an `s3:` bucket the reader holds credentials for
    (rule 1);
  - no store, local or remote, can reach loopback, private, link-local
    (which includes cloud metadata services) or other special addresses
    (rule 3). A reader that resolves names checks the address it connects
    to, so DNS rebinding does not get past the check, and it does not send
    requests through a proxy, whose connections it cannot check.

  It does not cover:
  - public hosts: any store can make the reader send requests to any
    public host. The requests carry no credentials of the reader's, but
    they reveal that the store was read;
  - readers that cannot see addresses: a browser checks hosts written as
    IP literals or `localhost`, and relies on Private Network Access for
    names that resolve to private addresses and for redirect hops;
  - the store's own requests, for a reader that does not apply rule 3 to
    them: a store's location from an untrusted place, or a redirect of it,
    can name a private address;
  - applications that set an unsafe setting. With unchecked proxies, a
    name that resolves to a public address where the reader runs, and to
    a private one where the proxy runs, is not detected. With private
    hosts, a store from an untrusted place, local files included, can
    send requests to services on the reader's machine and network; an
    application that needs private hosts for some stores SHOULD give
    those a prefix list (rule 2) as well.

  Applications that read stores from untrusted places SHOULD give a
  prefix list ([§6.7](#67-reader-policy) rule 2).
- One reference can name thousands of ranges, and a source table millions
  of sources; the limits of [§6.7](#67-reader-policy) bound the requests one
  `get` makes and the memory a store takes to open.
- Keys are not file names. Tools that extract a store to disk MUST NOT
  trust keys such as `../x` or `/etc/passwd`.
- A reference can describe a value of up to 2^64 − 1 bytes. Readers SHOULD
  bound the memory a single request can use, and document the bound; a
  request beyond it is a request error.
- Pins ([§5.1](#51-pins)) and range checksums ([§4.2](#42-range)) detect
  accidental replacement of a referenced object, not tampering: the store
  itself is not signed, a `modified_not_after` pin cannot see a rewrite
  within the same second, and CRC-32C is not a cryptographic hash.

## Appendix A: Complete schema

`CdIndex` is used only by the ZIP binding ([§12](bindings/vzip.md#12-page-index-optional)).

```proto
syntax = "proto3";
package refkv.v0;

message Range {
  uint32 source = 1;
  reserved 2;
  uint64 offset = 3;
  uint64 length = 4;
  optional bytes data = 5;
  optional uint32 crc32c = 6;
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
  optional uint32 revision = 2;
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
