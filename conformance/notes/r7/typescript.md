# SPEC_NOTES: vzip format version 0, revision 7, TypeScript implementation

These notes list every place where the spec (SPEC.md) or the harness
(HARNESS.md) left the implementer to guess, plus anything that was hard or
unnatural to implement in TypeScript/Node. For each item: the section, the
question, and what this implementation chose and why.

Items are grouped. Section A lists the ones most likely to cause two
implementations to disagree.

---

## A. Most likely to affect interoperability

### A1. §9.1: when does a zero-length range "extend past the end" of a `key`/`data` source?
The writer must reject "a source range of a `key` or `data` source [that]
extends past the end of that source's value". For a zero-length range
`{offset: 10, length: 0}` on a 5-byte source, the range is empty, but its
start and end (10) are both past the end. The reader never resolves
zero-length ranges (§8.3 step 4), so a reader would accept the archive either
way.
**Choice:** reject when `offset + length > size`, including for zero-length
ranges. A runner that expects such descriptions to be accepted will see a
failure. The spec should say which reading it means, for example "`offset +
length` exceeds the size of the source's value".

### A2. §8.1 / §10: resource limits during open
The format entries are inflated at open, and "their uncompressed sizes are
not checked", so a small `__vz__/sources` can expand without bound. §8.4 says
resource limits cause *request* errors and "may be reported at whatever step
the reader reaches", but open is not an operation with a request, and the
harness only allows the `archive` class at open.
**Choice:** inflate format entries up to 1 GiB. Anything larger fails the
open with an archive error. The spec should state the class, or require
that open has no resource-limit failures.

### A3. §6.2: details of HTTP header parsing that the spec doesn't fix
- **`Content-Range` syntax:** RFC 9110 range units are case-insensitive and
  allow whitespace in some places. **Choice:** after trimming the header
  value, require exactly `bytes <a>-<z>/<total|*>`, with lowercase `bytes`
  and no other whitespace. Anything else counts as "not a valid
  `Content-Range`", which is a resolution error.
- **Several `Content-Encoding` header lines**, each `identity`: the spec only
  covers one header whose value is a list (`identity, identity`). RFC 9110
  says repeated header lines are the same as a comma-joined list.
  **Choice:** more than one `Content-Encoding` header is a resolution error,
  which matches the list rule.
- **Several `ETag` or `Last-Modified` headers:** not covered. **Choice:** a
  pin can only be checked if there is exactly one such header; otherwise the
  pin "cannot be checked", which is a resolution error.
- **Leading or trailing whitespace on `ETag` and `Last-Modified`:** trimmed,
  because RFC 9110 field values exclude surrounding OWS.
- **Leap second `:60` in `Last-Modified`:** IMF-fixdate allows second 60.
  **Choice:** accept it and count it as `:59 + 1`, which is the next minute's
  `:00`.
- **206 with a `Content-Type: multipart/byteranges` header and also exactly
  one `Content-Range`:** **Choice:** a resolution error (multipart is
  rejected whatever else is present).
- **Content-Encoding on non-2xx responses:** checked only on 200/206.

### A4. HARNESS.md: what counts as a JSON integer
"numbers are JSON integers (`1.0` is invalid)". The harness does not say
whether `1e2`, `1E0` or `-0` are integers.
**Choice:** a number is an integer only if it has no fraction and no
exponent part, so `1e2` is invalid. `-0` is accepted as 0, so it is not
treated as negative. Because the description language is JSON, the harness
should give the grammar explicitly.

### A5. HARNESS.md: query `key`/`prefix` that is not a string, or not a well-formed Unicode string
- If `key` is present but is a number, null, etc., is that "missing `key`"
  (malformed)? **Choice:** yes, malformed, so exit non-zero.
- If `key` is a JSON string with a lone surrogate (`"\ud800"`), it has no
  UTF-8 encoding. **Choice:** such a key is *missing* for
  classify/get/get_raw, and such a prefix lists nothing (`[]`). The writer
  rejects such keys as "not valid UTF-8".

### A6. §3.2: ZIP64 extra block longer than 8 bytes
A record whose offset field is all ones is an entry error if the `0x0001`
block is "shorter than 8 bytes". The spec doesn't say what happens when the
block is longer (APPNOTE allows a disk-number field after the offset).
**Choice:** accept it and read the offset from its first 8 bytes. Sizes are
never all ones in a valid archive, so the offset is always first.

### A7. §3.2: where the zip64 EOCD record may be
The spec requires the locator to immediately precede the EOCD, and the record
it points to to "lie within the file". It does not say how many bytes "the
record" is, or whether the record must immediately precede the locator.
**Choice:** the 56 bytes `[offset, offset+56)` must lie within the file, and
the record may be anywhere. The record's own disk fields and version fields
are ignored.

---

## B. Ambiguities or omissions with low interoperability impact

### B1. §3.4: comment magic `vzip/` followed by a non-digit
`vzip/x` is neither "a version the reader implements" nor clearly "a version
the reader doesn't implement". Both are archive errors, so the only effect is
the message. **Choice:** "unsupported vzip format version".

### B2. §6, §8.1: `url` syntax is checked lazily but emptiness is checked eagerly
An empty `url` is an archive error at open, but `"a b"` is only a resolution
error when a range is resolved. This is deliberate in the spec, but the
asymmetry is surprising. Implemented as written.

### B3. §6 `file:` mapping: empty path segments
After decoding, `.` and `..` segments are rejected. Empty segments
(`file:///a//b`) aren't mentioned. **Choice:** passed through to the OS
unchanged, which on POSIX means the same as one slash.

### B4. §6.2: percent-encoded or IPv6 hosts in `http:` URLs
Not covered. **Choice:** a reg-name host is percent-decoded before the
connection is made. IPv6 literals have their brackets stripped. An empty
port (`host:`) means the default port.

### B5. §6.2: redirect `Location` with a fragment, and the original URL's fragment
**Choice:** fragments are dropped before each request and never sent. A
`Location` with a scheme in a different case (`HTTP://`) is accepted, since
schemes are case-insensitive.

### B6. §6.2: is the 200-response "source too short" check against the requested end?
On a 200 the size is the body length. If it is less than the requested end,
I report "source value is shorter than `offset + j`", a resolution error
(§8.3). The size pin is checked against the body length.

### B7. §6.1: if a pin is checked "once per source" vs. per request
**Choice:** pins are checked on every read, with no caching. That is always
allowed.

### B8. §7.1/§7.2: "the last page ends where the format entries' records begin"
This writer requirement is not on the list of things a reader checks
(§7.2), so a reader cannot find out whether pages run into the format
records, and §8.1 forbids extra open-time checks. Not checked. If a page did
contain format records, they are hidden and do not show up anywhere.

### B9. §7.2 / §8.2: a pinned key that also appears in a page
The pinned description wins (lookup checks pinned first), and `list` reports
the key once.

### B10. §8.2: duplicate names inside one page, or in an unpaged directory
Unspecified (§8.6). **Choice:** the first record wins.

### B11. §8.2 `raw` on format entries
`raw("__vz__/sources")` and `raw("__vz__/index")` never consult the central
directory, so they cannot fail with entry errors. This follows §3.4 and
§8.2, but it means the format entries' records could be garbage and `raw`
would still succeed.

### B12. §8.4: order of checks inside "the lookup of the key's record, and that record's entry error"
Several entry-error causes can apply to one record (bad method and two
reference blocks, say). The class is the same, so only the message differs.

### B13. §9.2 / §3.2: "version made by" in the zip64 EOCD record
§9.2 suggests 20 for records. The zip64 EOCD record's "version made by" is
not specified. **Choice:** 45.

### B14. HARNESS.md: unknown members inside a `range` object
"Unknown members of a query are ignored" does not say whether this covers
members nested inside `range`. **Choice:** unknown members inside `range`
are ignored too. Only `start`/`end`/`offset`/`suffix` decide which form it
is.

### B15. HARNESS.md: invalid queries file and unopenable archive at the same time
**Choice:** the queries file is validated first. If it is invalid, the CLI
exits non-zero without printing JSON.

### B16. HARNESS.md: a JSON file with a UTF-8 BOM
Not covered. **Choice:** rejected as invalid JSON, since RFC 8259 says
senders MUST NOT add one.

### B17. HARNESS.md: page grouping
"a page SHOULD hold about `page_size` bytes". **Choice:** records are
grouped greedily. A new page starts when adding the next record would make
the page exceed `page_size`, so a record larger than `page_size` gets a page
of its own.

### B18. §9.1: modified_not_after range
The harness limits numbers to |n| < 2^53, so int64 overflow cannot happen
through the CLI. The library rejects values outside int64.

### B19. §8.4 resource limits of this implementation (documented as §10 asks)
- A single `get`/`raw` returns at most 1 GiB. A DEFLATE bytes entry whose
  uncompressed size is over 1 GiB cannot be read, even for a small window.
  These are request errors.
- An HTTP response body over 1 GiB (a 200 for a large object) is a request
  error.
- Format entries inflate to at most 1 GiB; beyond that the open fails with an
  archive error (see A2).
- There is no restriction on which `file:` paths may be resolved (§10 says
  SHOULD allow restricting; the CLI does not need it). This is the default.

---

## C. Things that were unnatural or hard to implement in TypeScript/Node

1. **Detecting trailing bytes after a DEFLATE stream (§8.1).**
   `zlib.inflateRawSync` silently ignores data after the final block. The
   workaround is the undocumented-feeling `{info: true}` option: compare
   `result.engine.bytesWritten` (the bytes zlib consumed) with the body
   length. Truncated streams do throw (`Z_BUF_ERROR`). `maxOutputLength` is
   set to the expected size + 1 so that a DEFLATE bomb stops early.
2. **UTF-8 byte order of keys (§3.3).** JS strings compare in UTF-16 order.
   All keys are held internally as latin1 "byte strings" (one char per UTF-8
   byte), so `<` and `sort()` give UTF-8 order. `TextDecoder` strips a BOM by
   default and needs `ignoreBOM: true`. The spec's warnings about both were
   accurate and useful.
3. **64-bit values.** Everything that can exceed 2^53 (offsets, lengths,
   sizes, varints) is a `bigint`. Node's `fs` APIs take numbers, which
   effectively limits files to 2^53 bytes.
4. **Strict JSON.** `JSON.parse` silently accepts duplicate member names and
   turns `1.0` into `1`, so the harness rules forced a hand-written JSON
   parser.
5. **HTTP with `fetch`.** Node's `fetch` (undici) sends `Accept-Encoding:
   gzip, deflate` by default, decompresses transparently (which hides the
   `Content-Encoding` check), and handles redirects in ways hard to bound to
   exactly 5 while re-sending the same headers. This implementation uses
   `node:http`/`node:https` directly and handles redirects itself. `rawHeaders`
   is needed to count duplicate headers, because `res.headers` merges them.
6. **RFC 3986 strict parsing and resolution.** WHATWG `URL` normalises
   (lowercases hosts, percent-encodes spaces, accepts backslashes, treats
   `file:` specially), so it cannot be used to validate `URI-reference` or
   to resolve strictly. A validator (including IPv6 literals) and §5.2.2
   resolution were written by hand.
7. **IMF-fixdate for years 1–99.** `Date.UTC` maps years 0–99 to 1900–1999.
   `setUTCFullYear` avoids that. Formatting must pad the year to 4 digits.
8. **Protobuf.** Writing a decoder by hand was simple. The spec's list of
   malformed conditions was complete enough to implement directly.

---

## D. Process note

While cleaning up a stray temporary directory I created by mistake one level
above the working directory, I ran `ls` on the parent scratchpad directory
once. I did not open or read any of the files listed there, and nothing in
this implementation is based on them.
