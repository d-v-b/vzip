# SPEC_NOTES: ambiguities and choices (TypeScript implementation)

Each item gives the section, the question, and what this implementation chose and why.
Items are roughly ranked by how much they could affect interoperability.

## High impact

### 1. §8.2 / §3.1: when are errors reported (open vs. operation)?
§8.2 says an error "may be rejected when it is opened, or ... raised by the operation that first
encounters it", and §3.1 says a reader "MAY reject" archives violating §3. Two conforming readers
can therefore disagree on whether `open` succeeds and on which queries fail. A conformance runner
that compares `open.ok` or per-query `ok` cannot expect a single answer.

Chosen split:
- **At open** (whole central directory is parsed anyway): EOCD/ZIP64 structure, archive comment,
  duplicate names, invalid/empty UTF-8 names, encryption flags, bit 3, missing bit 11, methods other
  than 0/8, malformed extra fields, invalid classification (§4.1), reference entries with method != 0,
  zip64 extra problems, `__vz__/sources` missing/not method 8/mismatch with comment, source table
  decode errors (incl. empty url / Source with no member), page-index decoding and full §7.1 layout
  validation, pinned values vs. CD.
- **Lazily, per query**: local header checks (rule 3, sizes, name), CRC-32 and size checks of bodies,
  reference payload decoding, literal-range non-zero fields, source index out of bounds, `key`
  source missing / is a reference, out-of-bounds reads, I/O errors.
Suggestion: the spec should pin down at least which errors are open-time vs. per-key (or say
explicitly that tests must accept either).

### 2. §5.2: out-of-bounds ranges — "MAY report an error otherwise"
A reader MUST error only if a returned byte lies past the end, and MAY error otherwise. So `get` of the
in-bounds prefix of an out-of-bounds range is either bytes or an error depending on the reader.
Same for ranges with `length` 0 and an `offset` past the end, and for ranges the request does not
touch at all. Chosen: error only when a byte that must be returned lies past the end (minimal
behaviour, no extra I/O). For `data` and `key` sources the size is known cheaply, so a reader could
easily choose the other way. This directly produces different `get` results across implementations.

### 3. §5.2 / §6 / §8.2: what is "resolving a range" for zero-overlap parts?
If a request does not overlap a part (or the part has size 0), does the reader still have to
validate the part's source (missing `key` source, key naming a reference, unsupported scheme,
unreachable URL)? Chosen: no — only parts with a non-empty overlap touch their source. But
**structural** payload errors (source index >= table size, literal range with non-zero fields) are
checked for every part as soon as the payload is decoded, because §8.2 lists them independently of
I/O. So `get(k, range(0,0))` on a reference with a bad source index errors, but one naming a missing
`key` source does not. The spec should say which errors are payload-level (always) and which are
resolution-level (only when bytes are needed).

### 4. §3.1 rule 3 vs. ZIP64 for large entries (contradiction)
Rule 3 forbids any local extra field, but APPNOTE 4.5.3 requires the ZIP64 extra field in the local
header when the local sizes are 0xFFFFFFFF. A vzip entry whose (un)compressed size is >= 4 GiB is
therefore impossible to write in a way that satisfies both. ZIP64 is only possible for the
*local header offset* (CD-only) and for the end records. Chosen: the writer rejects entries >= 4 GiB;
the reader accepts local headers whose size fields are 0xFFFFFFFF and takes sizes from the CD.
The spec should either forbid >= 4 GiB entries, or allow the 0x0001 block in local headers (and
then rule 3's body-offset formula breaks).

### 5. §3.1: "does not fit in its 32-bit or 16-bit field" — is 0xFFFFFFFF / 0xFFFF a fit?
The value 0xFFFFFFFF fits literally but is APPNOTE's sentinel. Chosen: values >= 0xFFFFFFFF
(resp. >= 0xFFFF for entry counts) use ZIP64. "MUST NOT use ZIP64 otherwise" makes this exact
boundary normative, so it should be spelled out. Also unspecified: when ZIP64 EOCD is used, which
EOCD fields get the sentinel (only overflowing ones, or all)? The reader triggers ZIP64 on any
sentinel; the writer puts sentinels only in overflowing fields.

### 6. §4.3 / §8.2: reference-entry rules not listed as errors
§8.2 lists violations of §3, §4.1, §5.1, §6, §7 but not §4.3 (method 0, body empty or equal to the
payload) nor §5.2's "source MUST be less than the table size" (that one is listed separately).
Chosen: method != 0 on a reference is rejected at open; the body content is **not** checked (the
reader MUST NOT require the body, and checking costs a read). In the raw view the body is returned
as stored. Is a reference with a garbage body an error? Unclear.

### 7. §5.1: uint32 fields holding values > 2^32-1
Protobuf implementations usually truncate a varint to 32 bits for `uint32`. The spec does not say.
Chosen: reject as malformed (`Range.source`, `Pinned.method`). A truncating reader would resolve
a different source. Similarly, a 10-byte varint whose high bits exceed 64 bits: chosen to mask to 64
bits (protobuf behaviour) rather than reject. Field number 0 and > 2^29-1: rejected (protobuf
behaviour), not mentioned in the spec.

### 8. §8.1: request validation beyond `start > end`
Negative values, non-integers, `suffix` negative: the spec defines only `0 <= start <= end`.
Chosen: any negative/non-integer bound is an error (even for missing keys, like `start > end`).
Also: hidden keys with `start > end` — error (validation happens before lookup).

### 9. §6 URL resolution details
- WHATWG URL (Node's `URL`) is used instead of a strict RFC 3986 resolver. They differ for inputs
  that are not valid URI references (spaces, backslashes, `|`), which WHATWG silently fixes.
  The spec should say whether an invalid URI reference is an error.
- `file:` URLs with a non-local host (`file://server/x`): error (cannot be mapped).
- Percent-encoding is decoded when mapping to a path (`a%20b.bin` -> `a b.bin`).
  Query/fragment in `file:` URLs: ignored by the path mapping. Not specified.
- The base URI for an archive opened via a symlink/relative path: chosen `pathToFileURL(resolve(path))`,
  i.e. absolute but not symlink-resolved. The spec says "absolute path" but not "canonical/real path".
- Reading a range from a `file:` source that is shorter than requested is an error; reading
  0 bytes never opens the file.

## Medium impact

### 10. §3.1 / §3.4: offsets are "absolute file offsets"
With prepended data (self-extracting stubs, which APPNOTE permits), CD offsets, `sources_offset`,
`index_offset` and `data_offset` would not agree. Chosen: require `cd_offset + cd_size` to equal the
start of the end records (no gaps, no prepended data) and treat all offsets as absolute.

### 11. §3.4: should the reader cross-check the comment against the CD?
The comment gives `sources_offset` and `sources_size`; the CD record gives the same information.
Chosen: reject at open if they differ. The spec does not require the check (but a reader that only
uses the comment would read different bytes than one using the CD, so mismatches are dangerous).

### 12. §3.4 / §7: `__vz__/index` present with a 22-byte comment
"The 38-byte form is used iff the archive has a page index." Is an archive that contains an
`__vz__/index` entry but a 22-byte comment malformed, or just an unpaged archive with an extra
hidden entry? Chosen: unpaged; the entry is an ordinary hidden bytes entry.

### 13. §7.1 validation depth / §7.3 pinned
The spec does not say how much of §7.1 a reader must verify. This reader (which reads the whole CD
anyway) verifies: last two records are the trailer, body strictly sorted, pages contiguous from 0,
non-empty, end on record boundaries, cover exactly the body records, `first_key` matches, and every
pinned entry exists, is a body record, is `bytes`, and matches the CD. A page-lookup-only reader
could not detect most of these. Unspecified: duplicate pinned keys (accepted), order of `pinned`
(writer sorts by key), whether hidden keys may be pinned (accepted; writer allows it),
whether `__vz__/sources`/`__vz__/index` may be pinned (rejected: "Pinned entries also appear in the
body records").

### 14. §7.1: what is a "page" size target / page_size 0
The harness says a page "SHOULD hold about page_size bytes". Chosen grouping: start a new page when
adding the next record would exceed `page_size` and the page is non-empty (so a record larger than
`page_size` gets its own page; `page_size: 0` gives one record per page). Negative page_size rejected.

### 15. §4.3 / §4.1: payload size limit vs. other extra blocks
65531 is the limit of one extra block, but the whole CD extra field is also limited to 65535 bytes.
A 65531-byte payload plus a ZIP64 `0x0001` block (12 bytes) cannot fit. Chosen: writer rejects with
an "extra field exceeds 65535 bytes" error in that case. The spec should state the combined limit.

### 16. §3.1 rule 6 and CRC verification by readers
The spec does not say whether readers must verify CRC-32. Chosen: CRC and uncompressed size are
verified whenever a whole body is materialised (whole reads, DEFLATE entries, source table, index).
Partial reads of STORED entries read only the requested slice and are *not* CRC-checked. Therefore a
corrupt stored entry gives an error for `get(whole)` but bytes for `get(range)` — inconsistent but
cheap. A spec statement ("readers MAY skip CRC checks for partial reads") would help.

### 17. §4.1: what about the local header's extra field / flags for references?
Since rule 3 forbids local extras, the reference extra blocks exist only in the CD. That is implied
but worth stating explicitly in §4.1/§4.3, since ZIP tools usually mirror extras into the local
header.

### 18. §6: `key` source edge cases
- A `key` source with an empty string: chosen "missing" at resolution time (not an open-time error).
- A `key` source naming `__vz__/sources` or `__vz__/index`: allowed (they are bytes entries). The
  writer accepts `__vz__/index` only when paged.
- Hidden key sources are allowed (explicit in spec), and non-hidden ones too.

## Low impact / editorial

### 19. ZIP fields the spec is silent on
DOS date/time (chosen fixed 1980-01-01 00:00 for deterministic output), version made by (20,
MS-DOS, so `unzip` does not apply Unix permissions of 0), version needed (20, or 45 for records
with ZIP64 extra), external attributes (0), entry comments (none written; accepted when reading).
Keys ending in `/` look like directories to ZIP tools; keys with `..`, leading `/` or `\` are
accepted by both writer and reader. The spec could say whether such keys are allowed.

### 20. §3.4 comment: binary comment shown by ZIP tools
`unzip -t`/`zipinfo` print the archive comment, so the binary u64 fields come out as garbage on the
terminal. Harmless, but surprising; maybe worth noting.

### 21. §3.4: EOCD search
With a 38-byte binary comment, the comment could contain `PK\5\6`. Chosen: scan backwards and accept
the first EOCD whose comment length reaches exactly end-of-file. The spec could say "the comment is
the last 22 or 38 bytes of the file" to make this deterministic.

### 22. §5.1: canonical encoding vs. last-wins decoding
Encoders are canonical, decoders accept non-canonical input (any field order, repeats). OK, but the
spec could say whether a reader may reject non-canonical encodings (it says nothing).

### 23. §7.2: lookup when `k` is less than every `first_key`
Clear. But the spec does not say what a reader should do if the CD is not actually laid out as the
index says — answered here by validating at open (see 13).

### 24. §9 "informative, except where marked" — nothing is marked
The "MUST reject" list is clearly normative but the section header says informative. Also missing
from the list (but implied by §3–§7): pinned non-bytes entries, empty url, literal ranges with
non-zero fields, `key` sources pointing at reserved writer-owned keys.

### 25. §8.1 raw view
"Its value is the inflated entry body" — for STORED it is just the body. Raw view of a reference
entry when the body is empty yields empty bytes; fine. Raw-view list is not defined (not needed).

### 26. §2: hidden-prefix comparison
Hidden-ness is `startsWith("__vz__/")` on the decoded string, equivalent to a UTF-8 byte prefix
check. A key exactly equal to `__vz__` (no slash) is not hidden. Fine but worth an example.

### 27. Large numbers
uint64 offsets/lengths above 2^53 cannot be represented in JS numbers. The reader decodes them as
BigInt and errors only when actually needed as a file offset; value sizes > 2^53 raise an error.

## HARNESS.md notes

- JSON numbers for `offset`/`length` cannot carry full uint64 values in many JSON libraries; strings
  might be safer.
- A source range element without `source`: the harness says only `offset`/`length` default to 0.
  Chosen: reject (an element must have `data` or `source`). A missing `source` could also mean 0.
- A range element with both `data` and `source`/`offset`/`length`: rejected.
- An entry with both `bytes` and `ranges`, or neither: rejected. `compress: true` on a reference
  entry: rejected (references MUST use method 0). `pinned` on a reference: rejected (as stated).
- "do not leave a file at `<out-path>`": if a file already existed there, this CLI deletes it on
  failure. The harness could say whether a pre-existing file must be preserved.
- `get_raw` with a `range` is not listed; this CLI accepts it.
- Unknown/extra fields in the description are ignored.
- `list` without `prefix`: treated as empty prefix.
- Non-hex or odd-length hex strings are rejected by `write`. Uppercase hex is accepted.
- Error message text is free-form; the runner presumably only checks `ok`.
