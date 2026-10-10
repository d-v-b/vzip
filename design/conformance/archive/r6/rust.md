# Notes on the vzip spec (format version 0, revision 6)

These notes come from a Rust implementation (reader, writer, harness CLI)
written from SPEC.md and HARNESS.md alone. Each item gives the section, the
question, what this implementation chose, and why. Items are grouped by how
likely they are to cause two implementations to disagree.

## A. Could change observable results (interoperability risks)

### A1. §3.2 / §8.4: the ZIP64 block when sizes are also all ones
**Question.** "A record whose offset is all ones has an entry error if its
`0x0001` block is ... too short for the values it must hold." The values it
must hold depend on which of uncompressed size, compressed size and offset
are all ones. A record whose sizes are also 0xFFFFFFFF breaks §3.1 rule 7, so
§8.6 makes the result unspecified. But the entry-error rule is classified
behaviour, and it depends on how the sizes are counted. Take a record with
`csize = 0xFFFFFFFF`, `lho = 0xFFFFFFFF` and an 8-byte block. A reader that
counts both fields sees a block that is too short, which is an entry error. A
reader that looks only at the offset reads those 8 bytes as the offset.
**Choice.** The reader counts every all-ones field (usize, csize, offset, in
APPNOTE order) to get the required length. When the block is long enough, it
uses the 64-bit sizes from the block. "Disk number start" = 0xFFFF is ignored,
because disk numbers are ignored.
**Suggestion.** Say that only the offset is looked up and that it is the
*last* 8 bytes of the required values. Or say outright that a record with
all-ones sizes is out of scope.

### A2. §7.2 / §8.4: a missing key whose page cannot be parsed
**Question.** The entry-error cause is "the page that holds the key's record
cannot be parsed". When the page cannot be parsed, the reader cannot know
whether the key has a record there. Is `classify("absent")` then `missing` or
an entry error?
**Choice.** Entry error, for every key that §7.2 lookup sends to that page.
This is the only choice that does not depend on partial parsing.
**Suggestion.** Write "the page that lookup selects for the key".

### A3. §6.1 / §6.2: what counts as a "valid HTTP-date"
**Question.** RFC 9110 requires recipients to accept IMF-fixdate, rfc850-date
and asctime-date. rfc850 has two-digit years, which are read relative to the
*current* date, so the result depends on the clock. Other open points:
whether the day name must match the date, whether `sec = 60` is accepted, and
whether case-insensitive month names are accepted.
**Choice.** The reader accepts all three formats, case-sensitively (RFC 9110
says HTTP-date is case-sensitive). It checks that the date exists (no 31 Feb)
but does not check that the day name matches the date. It accepts second 60.
rfc850 years follow RFC 9110's "more than 50 years in the future" rule.
**Suggestion.** Either allow only IMF-fixdate (simplest and deterministic) or
explicitly allow the obsolete formats and pin down the two-digit-year rule.

### A4. §6.2: response shapes the table does not cover
- A 206 response whose body length differs from its `Content-Range`: this
  reader rejects it (resolution error).
- A 206 `multipart/byteranges` response, which has no top-level
  `Content-Range`: rejected.
- A 206 response covering a *superset* of the requested range: rejected,
  because "not the one requested" is read literally. Slicing would also have
  been reasonable.
- `Content-Encoding: identity`, or a list such as `identity, identity`:
  accepted. Any other coding is rejected. Comparison is case-insensitive.
- `Transfer-Encoding: chunked` is decoded. Other transfer codings are not
  supported.
- A redirect without `Location`, or with a `Location` that is not a valid
  URI-reference: resolution error. Neither is mentioned in the spec.
- Redirects to `https:` must be followed, but this reader does not support
  https, so they are a resolution error ("unsupported scheme"). An http-only
  reader therefore fails on servers that upgrade to https. That is allowed,
  because https is only a SHOULD, but a test suite should not expect such a
  read to succeed.
- Statuses 1xx are skipped as interim responses. 304, 300, 305 and other 3xx
  statuses count as "any other status" and are resolution errors.

### A5. §8.1 / §3.2: what "lies within the file" means
**Question.** The central directory must "lie within the file". Must it also
end before the zip64 records and the end of central directory record? May it
overlap them? May the zip64 record overlap the locator or the central
directory? The same question applies to entry bodies: §8.4 says "outside the
file", not "before the central directory".
**Choice.** Only `offset + size <= file_size` (with overflow checks), as
written. §8.1 also says a reader "MUST NOT report an error at open that this
section does not list".

### A6. §3.4: a comment that starts with `vzip/` followed by a non-digit
`vzip/x` is neither "not a vzip archive" nor clearly "a version the reader
doesn't implement". Both are archive errors, so the class is the same; only
the message differs. Choice: report it as an unsupported version.

### A7. §8.2 raw view of the format entries
**Question.** Should `raw("__vz__/sources")` use the central directory record
or the comment? §3.4 says readers read format entries "through the comment's
offsets and sizes", and §8.6 says records that disagree are unspecified.
**Choice.** Through the comment (inflated at open). The raw view of a format
entry therefore never fails after a successful open, even when the record is
missing or broken.

### A8. §7.2: pinned keys versus page records
A pinned key is "a present `bytes` entry, described by its `Pinned` values",
even when the page holds a *reference* record with the same name, or no record
at all. The reader follows this for classify, get, raw, list and `key`
sources. The spec could state outright that pinned values win for every
operation, `key` sources included.

## B. Underspecified, but the choice is unlikely to diverge

### B1. §5.2: literal range with explicit zero fields
A literal range whose wire data contains `source = 0` explicitly (non-minimal,
but valid on the wire) is accepted, because proto3 cannot tell an explicit 0
from an absent field. The text "MUST all be 0" supports this reading.

### B2. §6: a `key` source with an empty key
An empty `key` is not an archive error at open (it is not in the §6 list). It
fails at resolution as a missing key. Writers reject it as "absent".
Suggestion: make it an archive error, like an empty `url`, for symmetry.

### B3. §6 base URI construction
- `..` at the root (`/../a`): the spec says "removed ... together with the
  segment before them", and there is none. The reader drops the `..`, which
  matches remove_dot_segments.
- Non-UTF-8 POSIX path bytes: the spec says "the path's UTF-8 bytes". The
  reader percent-encodes the raw bytes, so any path works.
- The spec says `getcwd`, not `$PWD`. Rust's `std::env::current_dir` uses
  getcwd.

### B4. §6 `file:` mapping
- A path that names a directory or another non-regular file is a resolution
  error. The spec only says "cannot be read".
- The percent-decoded path is used with literal bytes. Empty segments
  (`file:///a//b`) are passed to the OS unchanged.
- `file://localhost:80/x` and `file://user@localhost/x` are rejected, because
  their authority is not exactly `localhost`.

### B5. §6.1 `modified_not_after` on `file:`
The reader uses `st_mtime`, which is already floored to whole seconds on
POSIX, including before 1970. A writer-side note: HARNESS.md's
`modified_not_after` is limited to magnitudes below 2^53, but valid int64
values outside years 1–9999 can still be written. Those fail over HTTP (they
cannot be sent) but work for `file:`. The same archive therefore behaves
differently depending on the scheme. This is intended, but surprising.

### B6. §8.1 resource limits at open
This reader inflates format entries up to 1 GiB. A larger table is reported
as an archive error, which is a resource limit surfacing at open. §8.4 allows
resource limits "at whatever step", but the harness only allows class
`archive` for open failures. The spec could say that resource limits at open
are archive errors.

### B7. §8.4 order: key-source shortness versus body errors
For a `key` source, the reader checks "value shorter than offset + j" (from the
record's uncompressed size) before reading and checking the body. Both
problems are resolution errors, so the order is not observable. If messages
were ever compared, the order would matter.

### B8. §7.1 grouping of records into pages
"A page SHOULD hold about `page_size` bytes". The writer starts a new page when
adding the next record would push the page past `page_size`. A record larger
than `page_size` gets a page of its own.

### B9. §9.2 / §3.2 zip64 end record header fields
The spec fixes "version needed to extract" = 45 but not "version made by" for
the zip64 end of central directory record. The writer uses 20 (from §9.2).
Local headers always say "version needed" = 20, because local headers never
contain ZIP64 data.

### B10. §4.3 CRC of a reference entry with `mirror: false`
The body is empty, so CRC = 0 and both sizes are 0. This is implied but not
stated. `unzip -t` accepts it.

### B11. §3.1 rule 6 / §8.6: CRC-32
The reader never verifies CRC-32. This is allowed, but a reader that does
verify CRCs reports a body error where this reader returns data. The two only
disagree on invalid archives, but conformance tests should avoid wrong CRCs
unless a body error is expected anyway.

## C. Unnatural or harder to implement than expected

- **Strict URI-reference validation (§6, §9.1).** No small Rust crate does
  exactly RFC 3986 `URI-reference`, including IPv6, IPvFuture and the
  `path-noscheme` rule. I wrote a validator and resolver by hand, about 300
  lines. Common URL libraries (WHATWG-style) accept and normalise far more,
  so implementations built on them will disagree on edge cases such as `a b`,
  `\`, `[` in paths, and `1a:b`.
- **"Inflates cleanly" (§8.1).** flate2's high-level readers silently ignore
  trailing bytes and some truncation. I drove the low-level `Decompress` API
  and checked `total_in == len` and `StreamEnd` by hand.
- **Redirects with re-sent headers and a hop limit.** Common HTTP clients
  (ureq, reqwest) follow redirects themselves and may drop `Range`/`If-Match`
  headers or add `Accept-Encoding: gzip`. Because §6.2 requires exact header
  control, the HTTP/1.1 client here is hand-written.
- **list() over pages (§8.2).** Working out which pages "include at least
  one key starting with prefix" needs care. For page range `[lo, hi)`, the
  smallest candidate key is `max(lo, prefix)` if `lo <= prefix` or `lo`
  starts with `prefix`; otherwise no key with that prefix can be in the
  page. The candidate must also be `< hi`. An example in the spec would help.
- **Order of checks (§8.4)** is spread over §8.2, §8.3 and §8.4. A single
  numbered pseudo-algorithm per operation would be easier to follow.

## D. HARNESS.md

- **`get_raw` with a `range`.** The query table says "(no `range`)" but does
  not say whether a `range` makes the query malformed or is ignored. Choice:
  malformed (exit non-zero).
- **Unknown members in query objects** (for example `range` on `classify`)
  are ignored. The harness states this rule for descriptions but not for
  queries.
- **`list` without `prefix`** is treated as malformed. The harness only gives
  "missing `key`" as an example.
- **`range` with `start` but no `end`** (or the reverse) is malformed. The
  harness calls `start`/`end` one form but does not say both are required.
- **Overflow tests are impossible through the CLI.** All numbers are below
  2^53, so a description cannot reach the §9.1 overflow rejections
  (`offset + length` > 2^64 − 1, total size > 2^64 − 1). They are tested
  through the library API instead.
- **Pins on `key`/`data` sources in a description.** The harness says "A
  `url` element may also have pins". It does not say whether pins on other
  elements are invalid or ignored as unknown members. Choice: invalid, per
  §9.1 ("a pin is on a `key` or `data` source" must be rejected).
- **A description with a `null` pin** (`"size": null`) is invalid, because the
  harness allows `null` only for `page_size`.
- **Exit status.** `write` uses 1 for an invalid description and 2 for I/O or
  usage errors. The harness only requires non-zero.
- **Archive opened by path, sources over HTTP.** In `read`, the base URI is
  always `file:`, so HTTP can only be exercised with absolute `http:` URLs in
  the source table. Relative-URL resolution against an `http:` base can only
  be tested through the library (`Archive::open_with_base`).
