# SPEC_NOTES: vzip format version 0, revision 6, TypeScript implementation

These notes list every place where the spec (SPEC.md) or the harness (HARNESS.md)
left me guessing, plus places that were surprisingly hard to implement. Each
item gives the section, the question, my choice, and why. Items are roughly
ordered by how likely they are to cause two implementations to disagree.

## A. Likely interoperability issues

### A1. §6.2 Which `Last-Modified` formats count as a "valid HTTP-date"
**Question:** RFC 9110 §5.6.7 defines HTTP-date as IMF-fixdate *or* the two
obsolete formats (rfc850-date, asctime-date), and says recipients MUST accept
all three. §6.2 only says "a valid HTTP-date". Must a reader accept
`Sunday, 06-Nov-94 08:49:37 GMT` and `Sun Nov  6 08:49:37 1994`? rfc850-date
has a two-digit year, which RFC 9110 resolves relative to the *current
clock* (more than 50 years in the future means the previous century), so the
result depends on when the reader runs.
**Choice:** I accept all three formats, using the RFC 9110 50-year rule for
rfc850-date. I do not check that the weekday name matches the date, and I
reject out-of-range fields (Feb 30, hour 24).
**Suggestion:** Say explicitly whether obsolete formats are accepted, whether
the weekday must match, and whether a leap second (`:60`) is accepted. A
reader that accepts only IMF-fixdate gets a different result (resolution
error) for servers that send obsolete formats.

### A2. §4.3 / §8.6 Reader behaviour for a reference payload larger than 65519 bytes
**Question:** §4.3 says a payload MUST NOT exceed 65519 bytes, but this is not
a payload error in §5 or §8.4, and §8.6 does not list it among the violations
readers need not detect. A payload of 65520 to 65531 bytes fits in the extra
field when no ZIP64 block is present. Is `get` then a payload error, or must
it succeed?
**Choice:** Readers do not check the limit (the payload is decoded normally).
**Suggestion:** Either add "payload longer than 65519 bytes" to the payload
error list or add §4.3's size limit to §8.6.

### A3. §3.2 / §8.4 ZIP64 handling when a *size* field (not the offset) is 0xFFFFFFFF
**Question:** The entry-error rules are written only for "a record whose
offset is all ones". A record whose compressed or uncompressed size is
0xFFFFFFFF violates §3.1 rule 7, which §8.6 says readers need not detect. But
should the reader take the 64-bit size from the `0x0001` block (APPNOTE), use
0xFFFFFFFF literally, or report an entry error?
**Choice:** I follow APPNOTE: every all-ones field among (usize, csize,
offset) is read from the single `0x0001` block in that order. A missing,
duplicated or too-short block is an entry error in all three cases.
**Suggestion:** State it, or explicitly say the result is unspecified.

### A4. §3.2 More than one `0x0001` block when the offset is *not* all ones
**Question:** "has more than one 0x0001 block" is an entry error only for a
record whose offset is all ones. For other records, is a duplicate `0x0001`
block allowed?
**Choice:** Ignored (no error), since §4.1 says other header IDs "MAY appear
and do not affect the kind".

### A5. §7.2 / §8.2 Which pages `list(prefix)` must read
**Question:** The rule "reads every page whose range includes at least one
key starting with `prefix`" is correct but leaves implementers to work out
the interval test. Errors here change results (an entry error versus a
listing), because a page that cannot be parsed fails the whole `list`.
**Choice:** Page `i` covers `[lo, hi)` (`hi` absent for the last page). It
intersects the set of keys with prefix `p` iff `(lo <= p && (hi absent || p < hi))
|| (lo > p && lo starts with p)`. With the empty prefix every page is read.
Pages before the first `first_key` are never relevant (such keys are missing).
**Suggestion:** Give the formula in the spec, plus a conformance test where a
page holding only hidden keys, or a page whose `first_key` lacks the prefix
but whose range contains prefixed keys, cannot be parsed.

### A6. §6 `file:` mapping edge cases
- **`localhost` with a port or userinfo** (`file://localhost:/x`,
  `file://u@localhost/x`): the authority is not exactly `localhost`. I treat
  both as non-local (resolution error).
- **Percent-encoded host** (`file://%6Cocalhost/x`): not decoded; resolution
  error.
- **Decoded path bytes that are not valid UTF-8**: the spec doesn't say. I
  pass the raw bytes to the OS (Node accepts `Buffer` paths). Some
  implementations may decode to a string first and fail.
- **Empty segments in the resolved path** (`file:///a//b`): kept as they are
  and handed to the OS, which collapses them. The spec only forbids `.`/`..`
  segments after decoding.
- **A source that resolves to a directory**: I report a resolution error
  ("not a regular file"). The spec only says "if the object cannot be read".

### A7. §6.2 HTTP URL details the spec doesn't cover
- **Empty or absent host** (`http:///x`, `http:x`): resolution error (my choice).
- **Userinfo** (`http://u:p@host/`): I don't send it (no `Authorization`
  header). The spec doesn't say.
- **Fragment**: not sent (standard).
- **`Content-Encoding`** compared case-insensitively after trimming, and only
  the exact single value `identity` (or no header) is accepted. A list such as
  `identity, identity` is rejected.
- **206 whose `Content-Range` total is not greater than its last byte**, or
  whose body length differs from the `Content-Range`: resolution error (my
  choice; the spec only covers "the returned range is not the one
  requested").
- **Redirect without `Location`, or with a `Location` that is not a valid
  URI-reference**: resolution error (not stated).
- **Redirect counting**: "up to 5 in a row; a sixth is a resolution error". I
  read this as: 5 redirect responses followed by a final response is OK; a
  sixth redirect response is an error.
- **200 fallback** means the reader must buffer the whole object. I cap
  response bodies at 1 GiB (resolution error beyond that). The spec says this
  is a MUST-accept case, so a resource-limit class for it would help.

### A8. §3.4 Comment whose sixth byte is not a digit
**Question:** A comment that starts `vzip/` followed by a non-digit (`vzip/x`)
is neither clearly "not a vzip archive" nor "an unsupported version". Both
are archive errors, so this only affects the message. I report it as an
unsupported version.

## B. Ambiguities with an obvious choice

### B1. §3.2 What "lie within the file" means for the zip64 end record
I require `zip64_offset + 56 <= file_size`. I do not require the record to end
before the locator or to avoid overlapping the central directory. Likewise
the central directory only has to satisfy `cd_offset + cd_size <= file_size`,
not end before the end records.

### B2. §8.1 / §8.2 `raw` of format entries
`raw("__vz__/sources")` and `raw("__vz__/index")` return the body that the
comment locates, inflated. Its uncompressed size is not checked against any
record, as §8.1 says for open. In an unpaged archive `raw("__vz__/index")` is
*missing*. The spec says format entries are visible in raw but doesn't say
whether raw goes through the comment or the central directory record. I used
the comment because §3.4 says readers MUST read them through the comment.

### B3. §7.2 Duplicate names inside a page, or in an unpaged directory
Unspecified (§8.6). I use the first record with that name.

### B4. §7.1 Pinned STORED entry with `size != csize`
Not in the open-time "malformed" list, so it is a body error when the key is
read (the same rule as a STORED record).

### B5. §8.1 A paged archive's central directory is never parsed as a whole
Only pages are parsed, lazily, and only when a lookup or list needs them. So
garbage after the last page (where the format records should be) is never
detected. That is consistent with §7.2 ("not required to check"), but worth
stating.

### B6. §8.4 Unexpected I/O errors (EIO on the archive after open)
The error classes don't cover these. They can't really happen in tests. A
failed read of a key's body would most naturally be a body error. My CLI maps
any non-vzip exception to class `resolution` and logs to stderr.

### B7. §9.2 "Version needed to extract" in local headers of ZIP64 entries
§9.2 recommends 45 "for records with a ZIP64 extra field". The local header
never has a ZIP64 extra field (rule 4), so I write 20 in local headers and 45
in the central record. For the zip64 end record's "version made by" I also
write 45. The spec only fixes "version needed" there.

### B8. §5.1 Non-minimal tag varints and the varint overflow rule
"Non-minimal varints are accepted" applies to tags too (I accept them). A
10-byte varint whose last byte is 0x01 is valid (2^63 bit). One whose last
byte is >= 0x02 exceeds 2^64 − 1 and is malformed.

### B9. §6.1 Pin checks per read versus per open
I check pins on every read (file: re-stat; HTTP: every response), which the
SHOULD allows. Nothing is cached.

### B10. §8.3 Concat of ranges into the same object
I issue one request per overlapping range, in order, and do not merge
requests (merging is allowed). The first failure is reported, matching
"ranges, in order" in §8.4 step 5.

### B11. §10 Resource limit
I document a 1 GiB limit (`MEMORY_LIMIT` in `src/common.ts`) on a single
`get` window, on any DEFLATE body (inflated or compressed), on an unpaged
central directory and on a page. Beyond it, `get`/`raw` is a request error
(an unpaged directory over the limit is an archive error at open). HTTP
bodies over 1 GiB are resolution errors.

## C. Things that were unnatural or hard to implement (TypeScript / Node)

- **Detecting trailing bytes after a DEFLATE stream (§8.1).** Node's
  `zlib.inflateRawSync` silently ignores trailing input. I had to use the
  little-documented `{ info: true }` option and compare `engine.bytesWritten`
  with the input length. Truncated input does throw ("unexpected end of
  file"). The spec's warning about this was very useful.
- **Strict JSON integers (HARNESS).** `JSON.parse` maps `1.0` and `1` to the
  same number. Rejecting `1.0` needed the JSON.parse source-text-access
  reviver (`context.source`), which is only in recent engines (it works in
  Node 26). Older runtimes would need a hand-written JSON parser.
- **UTF-8 ordering and UTF-8 validity in JS strings.** JS sorts strings by
  UTF-16 code units, so every key comparison goes through UTF-8 bytes
  (`Buffer.compare`). A JSON description can contain lone surrogates
  (`"\ud800"`), which JS keeps as-is and `TextEncoder` silently replaces with
  U+FFFD. "Key is not valid UTF-8" (§9.1) has to be checked with
  `String.prototype.isWellFormed`. Decoding protobuf strings needs
  `TextDecoder('utf-8', {fatal: true, ignoreBOM: true})`: the default strips
  a leading BOM, which §3.3 warns about.
- **uint64 everywhere.** Offsets, lengths and `offset + length <= 2^64 − 1`
  checks need `BigInt`. JS numbers lose precision above 2^53.
- **RFC 3986 validation.** No built-in (WHATWG `URL` is not RFC 3986 and
  normalises), so I hand-wrote the grammar, including IPv6/IPvFuture
  literals and §5.2.2 resolution.
- **HTTP.** `fetch` follows redirects itself, may add `Accept-Encoding`, and
  decodes bodies, so I used `node:http`/`node:https` directly with manual
  redirect handling.
- **IMF-fixdate for years 1–999.** These need zero-padded 4-digit years and
  `Date.setUTCFullYear` (the `Date` constructor maps years 0–99 to 1900–1999).

## D. HARNESS.md

- **`get_raw` with a `range` member**: the table says "(no `range`)". It isn't
  clear whether a `range` makes the query invalid or is ignored. I ignore it.
- **Unknown members in query objects and range objects**: the description
  section says unknown members are ignored, but the query section doesn't say.
  I ignore them, except that a range must contain exactly one of the forms
  `{start,end}`, `{offset}`, `{suffix}`. `{start}` alone, or `start` together
  with `suffix`, is an invalid queries file.
- **`range: null`, negative numbers, or non-integers in queries**: I treat all
  of them as an invalid queries file (exit 2), based on "only
  `modified_not_after` may be negative".
- **`start > end`** is a valid query and returns a request-error result.
- **Missing `key` on `list`**: list uses `prefix`; a missing `prefix` is invalid.
- **Pins on `key`/`data` sources in a description**: the harness lists pins
  only for `url` sources. I treat pins elsewhere as invalid (§9.1).
- **Range validation in the description**: a range naming a source index that
  doesn't exist is invalid, even for zero-length ranges (§9.1).
- **Exit codes**: I use 2 for invalid input or usage and 1 for I/O failures;
  the harness only requires "non-zero".
- **Unexpected errors after a successful open** (see B6) aren't covered;
  results stay per query.
- **`page_size` larger than 2^53**: can't happen given the 2^53 rule; I reject it.
- **Grouping into pages**: I start a new page when adding a record would make
  the current page exceed `page_size`, so a page exceeds `page_size` only if
  it holds a single larger record.
- **Entry order in the file** (not specified by the harness): non-pinned
  entries in description order, then `__vz__/sources`, then pinned entries,
  then `__vz__/index` (the §9.2 layout). Body records in the central
  directory are sorted by UTF-8 key in both paged and unpaged archives, then
  `__vz__/sources`, then `__vz__/index`.
