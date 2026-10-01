# Notes on the vzip specification (draft, revision 2)

These notes come from writing a Python implementation (`vzip_impl/`) using only
SPEC.md and HARNESS.md. Each item gives the section, the question, and the
choice made. Items are grouped by topic and roughly ordered by how much they
could affect interoperability. A ranked summary is at the end.

---

## A. Error classification

### A1. §8.1 / §8.4: are record-level problems in an *unpaged* archive archive errors or entry errors? (high impact)
§8.1 says that opening an unpaged archive "reads and checks ... the whole
central directory". §8.4 then says "In an archive with a page index, a
record's problems are found when its page is read, so they surface as entry
errors, not archive errors." That sentence implies that in an unpaged archive
the same problems (unparseable extra field, two reference blocks, method 12,
bit 0 set, reference with method 8) *are* archive errors. But the entry-error
row of the table and §8.2 `list` ("Keys with entry errors are listed") both
assume that an archive with entry errors can be opened.
**Choice:** record-level semantic problems are always entry errors, in both
kinds of archive. Only structural damage to the central directory (bad record
signature, truncated record, record count or byte size not matching the end
record) is an archive error in an unpaged archive.
**Suggestion:** say this explicitly: "In an archive without a page index,
reading the whole central directory at open does not turn entry errors into
archive errors."

### A2. §8.4: what is "bad end records" exactly? (medium)
The term is not defined. **Choice:** these are archive errors: a file shorter
than 44 bytes; a zip64 locator whose record offset is outside the file or whose
zip64 EOCD signature is wrong; `cd_offset + cd_size` beyond the start of the
end records; and comment offsets or sizes that point outside the file. Disk
numbers and the zip64 "total disks" field are ignored. In an unpaged archive a
central directory that does not parse into exactly `total entries` records
filling exactly `cd_size` bytes is also an archive error. It would help to list
the checks that are required, the ones that are allowed, and the ones that are
forbidden. §8.1 says a reader "MUST NOT report an error at open that this
section does not list", so it matters whether, for example, the `cd_offset +
cd_size` bounds check is allowed for a *paged* archive. This implementation
does that check.

### A3. Errors that have no class
None of the five classes covers these cases, so a choice had to be made for
each:
- **A corrupt DEFLATE body of a bytes entry** (inflate fails, the stream is
  truncated, or there are trailing bytes), or a body that extends past the end
  of the file. **Choice:** entry error for `get`/`raw`, and resolution error
  when it happens through a `key` source. §8.6 says readers need not check
  CRCs, but an inflate failure must still be reported somehow.
- **Inflate failure on `__vz__/sources` or `__vz__/index`.** **Choice:** archive
  error ("malformed source table/page index").
- **`list` when a page cannot be parsed.** The entry-error row only mentions
  classify/get/raw. **Choice:** `list` fails (reported as an entry error).
- **A `Pinned.method` other than 0/8.** **Choice:** entry error for that key.
- **Records whose file name is not valid UTF-8, or is empty** (§3.3 forbids
  these, but neither §8.4 nor §8.6 mentions them). **Choice:** such records are
  kept under their raw bytes. A JSON (Unicode) query can never match them, and
  `list` prints them with surrogate escapes. This should be put in §8.6 or made
  an entry error.
- **Bit 3 (data descriptor) set, or other APPNOTE features (strong encryption
  bit 6, a version-needed value above 45).** §8.6 covers rule 5 "except bit 0",
  so these are ignored.

### A4. §8.2: order of checks in `get`
Request error, hidden-key check, missing, entry error and payload error can
all apply at once. §8.2 settles that `start > end` is an error even for a
missing key. **Choice:** request error, then hidden → missing, then lookup
(where page or entry errors can occur), then entry error, then payload error,
then resolution. Stating the order would make error results deterministic.

### A5. §6 vs §8.1: when URL validity is checked
An empty `url` is an archive error at open, but a syntactically invalid URL
(`a b.bin`, `%zz`) is only a resolution error when it is used. This is
workable but asymmetric. Note that §9.1 also does not require writers to
reject invalid URI references (see C3).

---

## B. Protocol Buffers (§5.1)

### B1. Malformed wire types 3/4 on *unknown* fields (medium)
§5.1 makes wire types 3 and 4 (groups) malformed even when the field is
unknown. Standard protobuf libraries skip unknown groups. **Choice:** followed
the spec (malformed). Implementations built on a protobuf library need an
extra pre-pass. This deserves a sentence of warning.

### B2. `reserved 2` in `Range`
Is field 2 "a field number not in the schema" (skipped), or malformed?
**Choice:** skipped like any unknown field. A test covers this.

### B3. "Last occurrence wins" for message-typed fields
Real protobuf *merges* repeated occurrences of a non-repeated embedded message.
No non-repeated message fields exist in the current schema, so this is moot
for now, but the rule as written conflicts with protobuf semantics if one is
ever added.

### B4. Oneof and the empty-url check
If a `Source` contains `url` and then `key` on the wire, `key` wins. Should the
empty-`url` archive error look at a `url` member that lost?
**Choice:** only the winning member is checked.

### B5. Encoding rule wording
"not emit a scalar field equal to its default value" covers scalars only.
Elements of repeated message fields (Concat parts, Sources, Pages, Pinned) are
always emitted, even when the element encodes to zero bytes. For example, the
source range `{source:0, offset:0, length:0}` encodes as an empty message, so
a 0x7A76 payload can be 0 bytes long. That is legal but surprising. It is
worth saying explicitly that repeated elements are always emitted.

### B6. Literal range with explicitly-encoded zero fields
`source=0` written explicitly (non-canonical) in a literal range: accepted,
because the value is 0. The rule says "MUST all be 0", not "absent".

### B7. Concat size overflow class
§5.3 says the sum "MUST NOT exceed 2^64−1" but does not say this makes the
payload malformed (§5.2 does say so for `offset + length`).
**Choice:** payload error.

### B8. UTF-8 validity
"Valid UTF-8" is assumed to mean strict RFC 3629 (no surrogates, no overlong
forms), which is what Python's decoder enforces. Naming the RFC would help.

---

## C. URLs and base URIs (§6)

### C1. How to form the base `file:` URI from a path (high impact)
- **Percent-encoding of the base path** is not specified (which characters?
  what about non-ASCII names?). **Choice:** every byte of the UTF-8 or
  filesystem-encoded path is percent-encoded except unreserved characters,
  sub-delims, `:`, `@` and `/`. When the resolved path is percent-decoded the
  result is the same, but a relative reference like `x%2Fy` would interact
  differently with different encodings.
- **"Relative paths are made absolute against the current directory" plus
  "Symbolic links are not resolved"**: which current directory? The physical
  `getcwd()` (on macOS `/tmp/x` is really `/private/tmp/x`) or the logical
  `$PWD`? With `..` in a relative URL these give different files.
  **Choice:** `os.getcwd()` (physical). This could matter in a test harness
  that runs from a symlinked temp directory.
- `normpath` on POSIX keeps a leading `//`. **Choice:** collapsed to `/`.

### C2. What is a "valid URI reference"?
**Choice:** the RFC 3986 grammar, checked with a moderately strict validator:
ASCII only, every `%` followed by two hex digits, a valid scheme syntax, path,
query and fragment characters from `pchar`/`/`/`?`, and a numeric port.
Non-ASCII characters (IRIs) are rejected, so `café.bin` stored raw is a
resolution error. Other implementations, such as those based on WHATWG URL
parsers, will probably accept it. **Suggestion:** state that non-ASCII
characters are invalid, or require writers to validate.

### C3. §9.1 does not require writers to validate URL syntax
A writer may produce an archive whose `url` is `a b.bin`, and every reader
must then fail on it. **Choice:** the writer accepts any non-empty string,
following the list in §9.1 literally.

### C4. `file:` URI details
- Is `localhost` case-insensitive (`LOCALHOST`)? **Choice:** yes. The scheme is
  also compared case-insensitively.
- After percent-decoding, `%2F` becomes `/`, and `%00` gives a NUL that cannot
  appear in a path. **Choice:** NUL is a resolution error, and `%2F` is
  decoded to `/`.
- Fragment: ignored for `file:`. For `http:` the fragment is stripped before
  the request.

### C5. HTTP
"Byte ranges via HTTP Range requests": what about a server that ignores Range
and returns 200? And how is "source value is shorter" detected? **Choice:** a
200 response is sliced locally. A short 206 response, or 416, is a resolution
error. Python's `http.server` ignores Range, and that case is tested.

### C6. `key` sources and partial reads
A `key` source whose entry uses method 8 must be inflated in full to read any
range of it, so the "SHOULD fetch only the bytes in step 3" in §8.3 cannot be
met. That is fine, but worth noting next to the advice in §6 to use `key`
sources for shared headers.

---

## D. ZIP container

### D1. §4.3 payload limit depends on layout
The 65535-byte limit includes "the record's other extra blocks". The only
possible other block is the ZIP64 offset block (12 bytes), and whether it is
needed depends on where the entry lands (offset ≥ 4 GiB). So "MUST reject"
cannot be decided before layout. **Choice:** the payload is checked against
65531 during validation, and the full extra field is checked again when each
central directory record is built. The order of the blocks inside the extra
field is unspecified. **Choice:** the ZIP64 block comes first, then the
reference block.

### D2. §3.2 zip64 EOCD details
"version 1, no extensible data" is assumed to mean record format v1 with
size-of-record = 44. The version made by / needed values are not stated.
**Choice:** 45 and 45. On reading, all counts, sizes and offsets are taken from
the zip64 record whenever the locator is present, not only the fields set to
all ones.

### D3. §9.2 versions and the local header
The local header can never have a ZIP64 block, so its "version needed" is 20,
while the central directory record of the same entry says 45 when its offset
needs ZIP64. APPNOTE expects 4.5 for "uses ZIP64 extensions". Some tools may
warn about the mismatch. `unzip -t` and Python's `zipfile` accept it. The
1980-01-01 DOS date could be given as its encoded value (date `0x0021`, time
`0x0000`).

### D4. §3.4 locating the EOCD: magic checked before or after fallback?
Step 3 reads "If neither matches, or the comment does not start with
`vzip/1`". If step 1 matches (signature plus length 38) but the magic is
wrong, should step 2 be tried? **Choice:** no; the open fails. For a
22-byte-comment archive, a false positive at `size−60` is impossible, because
the length field read there falls on the real EOCD's "disk number" field, which
is 0. So the two readings agree on valid input. A sentence saying so would help.

### D5. §9.1 4 GiB limit: compressed or uncompressed?
"a bytes entry's size is 0xFFFFFFFF or more": with `compress: true` the
*compressed* size can exceed the uncompressed size for incompressible data.
**Choice:** reject if either size is 0xFFFFFFFF or more.

### D6. §3.4 no uncompressed size for format entries
The comment gives only the compressed size of `__vz__/sources` and
`__vz__/index`, so a reader cannot bound memory before inflating, which is a
concern of §10. Adding the uncompressed size, or a recommended cap, would help.

---

## E. Page index (§7)

### E1. What is checked at open?
"malformed page index" is an archive error, but is a structurally inconsistent
index malformed? Examples: pages not sorted, not contiguous, outside the
central directory, an empty `first_key`, or duplicate pinned keys.
**Choice:** only protobuf-level malformedness is an archive error. Everything
else falls under §8.6 (unspecified). A page outside the central directory gives
an entry error when it is read.

### E2. Lookup in unsorted pages
"Select the last page whose `first_key` ≤ k": for sorted pages this is a binary
search, which is what is implemented. For unsorted pages, "last in wire order"
and "binary search" disagree. This is covered by §8.6.

### E3. Pinned keys vs pages
If a key is pinned, its central directory record is never looked at, so entry
errors in that record are invisible in a paged reader but visible in a
whole-directory reader. This is covered by §8.6, but the consequence is easy to
miss. `list` in a paged archive takes the union of page records and pinned keys.

### E4. Page grouping (HARNESS)
"About `page_size` bytes": a greedy algorithm is used. A page never exceeds
`page_size` unless one record alone is larger. With `page_size: 1` every record
gets its own page.

---

## F. Writer (§9)

### F1. Requirements missing from §9.1
§9.1 does not list these, but they are needed to "produce a valid archive":
- `offset + length > 2^64−1` and a Concat sum above 2^64−1 (writers reject
  them);
- a `source` that does not fit in uint32.

All are rejected.

### F2. Layout
The recommended layout is followed: non-pinned entries in input order,
`__vz__/sources`, pinned entries, `__vz__/index`, then the central directory.
The central directory is sorted by UTF-8 key in unpaged archives too, which is
allowed.

---

## G. HARNESS.md

- **Error messages are free text.** If the runner compares error *classes*, it
  cannot do so from the message. This implementation prefixes messages with the
  class name (`"payload error: ..."`). A required `"class"` member would make
  conformance checks much more precise.
- **Hex case.** "Hex strings are lowercase" leaves open whether uppercase input
  is invalid or merely non-canonical. **Choice:** invalid (rejected).
- **Non-boolean `compress`/`pinned`/`mirror`** (`null`, `0`, `"true"`): not
  specified. **Choice:** invalid.
- **`page_size: 1.0`.** JSON does not distinguish integers from floats.
  **Choice:** only JSON integers are accepted (Python parses `1.0` as a
  float, which is rejected), and `true` is rejected.
- **`range: null` in a get query.** Treated as no range (whole value).
- **Archive path that does not exist or cannot be read.** Reported as an open
  failure (archive error, exit 0), not as a crash.
- **"All numbers are below 2^53"** means the 2^64 overflow rules cannot be
  exercised through the harness. The unit tests cover them directly.
- **Extra keys in a range object** (`{"start":1,"end":2,"suffix":3}`): treated
  as an invalid queries file (exit 2), because "exactly one of these forms" is
  required.
- `get_raw` on hidden keys and format entries returns their bodies. For
  `__vz__/index` in an unpaged archive it returns `null`.

---

## Ranked summary (by interoperability impact)

1. **A1**: whether record problems in unpaged archives are archive or entry
   errors. Readers will diverge on `open.ok` for the same file.
2. **C1/C2/C3**: base-URI construction (physical vs logical cwd,
   percent-encoding of the base path), what counts as a valid URI reference
   (non-ASCII), and writers not being required to validate URLs.
3. **A3**: errors with no class (corrupt DEFLATE bodies, `list` over a broken
   page, invalid pinned method, invalid UTF-8 names).
4. **B1/B2/B3**: protobuf strictness that differs from stock protobuf
   libraries (unknown groups malformed, reserved field, last-wins on
   messages).
5. **A2/E1**: the scope of open-time checks ("bad end records", "malformed
   page index") combined with the rule that readers "MUST NOT report other
   errors at open".
6. **D1**: the payload size limit depends on layout (ZIP64 block).
7. **G**: harness error reporting carries no machine-readable class, and the
   strictness of description validation is unspecified.
