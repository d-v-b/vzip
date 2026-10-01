# Notes on the vzip specification (version 0, revision 5)

These notes come from writing a Rust reader, writer and harness CLI using only
SPEC.md and HARNESS.md. Each item gives the section, the question, and the
choice this implementation made. Items are roughly ordered by how much they
could affect interoperability. The most important ones come first.

## A. Ambiguities that change observable results

### A1. §7.2 / §8.4: a lookup that lands in an unparseable page, for a key that may not exist
§8.4 says an entry error happens when "the page that holds the key's record
cannot be parsed". For a key that is *not* in the archive there is no "key's
record", but the reader can't know that without parsing the page. So does
`classify("absent-key")` return `missing` or an entry error when §7.2 selects a
broken page?
**Choice:** entry error for *any* key whose §7.2 page selection lands on an
unparseable page, present or not. (Keys that sort before the first
`first_key` are `missing` without reading anything.) Proposed wording: "or
the page that §7.2 selects for the key cannot be parsed."

### A2. §6.1 / §6.2: HTTP pins depend on the server and can fail open
On HTTP, `etag` and `modified_not_after` are checked only by sending `If-Match`
/ `If-Unmodified-Since` and treating a 412 response as failure. A server (or
cache or proxy) that ignores conditional headers returns 206 or 200 with the
new bytes, and the pin passes silently. That is the fail-open behaviour §1.3
warns against. This is especially likely on the 200 path (a server that
ignores `Range` often ignores preconditions too). The spec does not say
whether a reader may or must also compare the response's `ETag` /
`Last-Modified` headers.
**Choice:** only 412 fails the pin, as written. The implementation does not
inspect response `ETag`/`Last-Modified`. Two readers that differ here give
different results against such a server. The spec should either forbid that
extra check or require it (for example, "if the response carries `ETag`, it
MUST strongly match").

### A3. §8.2 list: which pages to read, and what "key" means there
"`list(prefix)` reads every page whose range includes at least one key
starting with `prefix`". Is "key" any possible string, any *valid* key
(non-empty UTF-8), or any *non-hidden* key? This matters for
`list("__vz__/")` (result always empty) and `list("")`. If one of the selected
pages is unparseable, the readings differ between an entry error and an empty
list.
**Choice:** any non-empty UTF-8 string, hidden or not. So `list("__vz__/")`
reads the pages covering that interval and can fail. Page `i` is read iff the
smallest string `>= first_key_i` that starts with `prefix` (that is, `prefix`
itself if `first_key_i <= prefix`, else `first_key_i` if it starts with
`prefix`, else none) is `< first_key_{i+1}`. An explicit algorithm in the
spec would remove any doubt.

### A4. §3.2 ZIP64 extra field: size fields that are all ones
§3.2 defines entry errors only for a record whose *offset* is all ones. A
record whose compressed or uncompressed size is 0xFFFFFFFF breaks §3.1 rule 7,
which §8.6 lists as not required to be detected. But the block layout ("those
fields among uncompressed size, compressed size and local header offset that
are all ones") means such a record's offset is stored at a different position
in the block. Readers that only look for the offset will read the wrong
8 bytes.
**Choice:** follow APPNOTE in general. Any all-ones field among
usize/csize/offset is taken from a single `0x0001` block in APPNOTE order. A
missing, short or duplicated block is an entry error whichever field needed
it. Please state whether all-ones sizes are an entry error, take the APPNOTE
path, or are unspecified.

### A5. §8.1: "the central directory (offset and size) lies within the file"
Within the file, or before the end records? A CD that overlaps the zip64 end
record or the EOCD passes the first reading and fails the second.
**Choice:** `cd_offset + cd_size <= file_size`, nothing more. Same question
for "the zip64 record ... MUST lie within the file": must it lie before the
locator? **Choice:** only `offset + 56 <= file_size`.

### A6. §8.2 raw of the format entries
`raw("__vz__/sources")` and `raw("__vz__/index")` must return "the inflated
body of the key's entry". Readers "need not consult those entries' central
directory records" (§3.4). So is the raw body located through the comment or
through the record? For valid archives they agree. For archives that break
§3.4 (which §8.6 does not require readers to detect) the results differ, and
the "must inflate to the record's uncompressed size" check is either applied
or not.
**Choice:** comment offsets, no size check (the already-inflated bytes from
open). In an unpaged archive, `raw("__vz__/index")` is `missing`, because a
record with that name would have failed open.

### A7. §6.2: redirects that change scheme
"Readers MAY follow up to 5 redirects." A `Location: file:///etc/passwd` (or
`https:`) redirect is not addressed. Following it into `file:` is a security
problem (§10).
**Choice:** only redirects to `http:` are followed (301/302/303/307/308,
`Location` parsed as a URI-reference and resolved against the current URL).
Anything else is a resolution error. Pin headers are re-sent on every hop.

### A8. §6.2: response shapes not in the table
- 206 without `Content-Range`, or with `multipart/byteranges`: not listed.
  **Choice:** resolution error. This implementation never combines ranges, so
  it never asks for multipart.
- 206 whose body length differs from the `Content-Range` length: **Choice:**
  resolution error.
- `Transfer-Encoding: chunked` is not `Content-Encoding`, so it is accepted.
  Worth saying explicitly, because `Accept-Encoding: identity` might suggest
  otherwise.
- `Content-Encoding: identity` (explicit) is accepted. **Choice:** an empty
  `Content-Encoding` value is also accepted.
- 1xx interim responses are skipped.

### A9. §6 file: URI mapping details
- `file://localhost:8080/x` or `file://user@localhost/x`: authority is "absent,
  empty or `localhost`". **Choice:** the whole authority must equal `localhost`
  (case-insensitive), so these are rejected.
- Empty path segments (`file:///a//b`, or `file:////x` after resolution):
  allowed and passed to the OS unchanged. POSIX gives a leading `//`
  implementation-defined meaning. The spec could say whether empty segments
  are rejected.
- Percent-decoding is case-insensitive (`%2f` is also rejected). Assumed.

### A10. §6 base URI from a local path
- "Form `file://` followed by the path's UTF-8 bytes": POSIX paths need not
  be UTF-8. **Choice:** use the raw path bytes and percent-encode every byte
  outside the allowed set. Non-UTF-8 bytes then become `%XX`.
- `..` at the root: **Choice:** dropped (`/../a` gives `/a`).
- The base URI is normalised lexically, but the archive itself is opened
  with the path as given. With symlinks, `..` can then point at a different
  directory than the one the OS used. That is what the spec implies, but it
  might surprise people.

### A11. §8.4: what happens at step 4 for empty windows
"A DEFLATE body is always inflated and checked in full, even for a small
window." Does this include an *empty* window (for example `range(5, 5)`, or
`offset(n)`)? **Choice:** yes. The body is always checked (bounds, sizes,
full inflate for DEFLATE), even when zero bytes are returned. Readers that
short-circuit empty windows would return `""` instead of a body error.

### A12. §6 / §8.3 key sources: the order of checks
For a `key` source whose target has a body error *and* is shorter than
`offset + j`, both outcomes are resolution errors, so the class matches. But
the spec doesn't say whether the body is validated before the length check.
**Choice:** length check from the record first, then body. This matters
only for messages.

## B. Underspecified details, where any reasonable choice interoperates

- **§3.4, version digit:** a comment `vzip/x` (non-digit) could be "not a vzip
  archive" or "unsupported version". Both are archive errors, so it doesn't
  matter for the harness. **Choice:** unsupported version.
- **§3.2, `eocd_offset < 20`** when ZIP64 is required: no room for a locator.
  **Choice:** archive error ("locator missing").
- **§3.2, zip64 EOCD "version made by":** not specified. **Choice:** 45. Local
  headers' "version needed": not specified (§9.2 talks about records only).
  **Choice:** 20.
- **§3.3, duplicate names:** unspecified per §8.6. **Choice:** first record
  wins (unpaged: first in CD; paged: first in the page).
- **§7.1, records of format entries inside pages:** not validated. **Choice:**
  `raw` of format entries always uses the comment. A page record with a format
  name is hidden in every other operation anyway.
- **§7.2, pinned key that also appears in a page:** the pinned entry wins,
  and `list` deduplicates.
- **§8.1, inflating the format entries:** "Their uncompressed sizes are not
  checked", so there is no natural bound on inflation. **Choice:** cap at
  4 GiB of output (archive error beyond that).
- **§8.4 / §10, resource limit:** this reader's documented limit is a 1 GiB
  window per `get` (request error), plus a 4 GiB inflate bound per entry.
- **§9.1, writer URL checks:** the writer must reject non-`URI-reference`
  URLs. It is not asked to reject URLs that will always fail the `file:`
  rules (`file://host/x`, `%2E%2E`). **Choice:** they are accepted, as written.
- **§9.1 / §9.2, page grouping:** "about `page_size` bytes". **Choice:** a page
  is closed when the next record would push it over `page_size`, so a page
  holds at least one record and is at most `page_size` bytes unless a single
  record is larger.
- **§9.2, entry order:** this writer uses body entries (description order,
  except pinned), then `__vz__/sources`, then pinned entries, then
  `__vz__/index`, then the CD sorted in UTF-8 order (both paged and unpaged),
  then format records.
- **§6.2, HTTP details not covered:** userinfo in an `http:` URL (dropped;
  `Host` is the authority without userinfo), percent-encoded reg-names
  (decoded for the DNS lookup), connection errors and timeouts (resolution
  error), `Connection: close` (one request per range, no keep-alive).
- **§6.2, `https:`:** SHOULD-level support is not implemented (no TLS crate
  was added). `https:` URLs give a resolution error, "unsupported scheme".
- **§6.1, `modified_not_after` on `file:`:** POSIX `st_mtime` is already the
  floor of the timestamp, so no extra rounding is needed. For years outside
  1–9999 the `file:` check still works. Only HTTP has the IMF-fixdate range
  restriction, an asymmetry worth stating explicitly.

## C. Things that were surprising or unexpectedly fiddly

- **§3.4, EOCD location:** fixed 44/60-byte offsets and no fallback from step 1
  to step 2. The rationale paragraph helps; without it the "no fallback" rule
  looks risky.
- **§3.4, comment vs. ZIP64:** the comment lengths are fixed, but the EOCD
  comment-length field is a u16 the reader must still check (22/38). Clear
  enough.
- **§5.1, protobuf rules:** clear and complete. A hand-written decoder was
  easy. One pitfall is the 10th varint byte: values up to `0x01` only, and the
  continuation bit there means "longer than 10 bytes". A test vector for
  `ff ff ff ff ff ff ff ff ff 01` (= 2^64−1, valid) vs `... 02` (invalid) would
  help.
- **§8.1, "inflates cleanly":** with Rust's `flate2`, calling `decompress_vec`
  with `FlushDecompress::Finish` and an output buffer that fills up returns
  a hard error instead of "need more space". An earlier version of this
  implementation rejected valid >1 KiB format entries because of it. The
  trailing-bytes check (`total_in == body.len()`) must be explicit, as the
  spec warns.
- **§7.1, page index writing:** pages depend only on CD record sizes, and
  those depend on whether each body entry needs a ZIP64 offset. All body
  entries precede `__vz__/index`, so there is no circularity, but only
  because of the §9.1 ordering rule. Worth keeping that rule prominent.
- **§4.3, 65519-byte payload limit:** the derivation (65535 − 4 − 12) is
  clear. Note that the writer then has 16 spare bytes that it never uses.
- **§1.3, link to `conformance/REVISIONS.md`:** not available to implementers
  working from the spec alone.
- **§6, fragment-only URL:** resolving to the archive itself falls out of
  RFC 3986 naturally. Note that `?x` would also resolve to the archive but
  then fails the `file:` no-query rule.

## D. HARNESS.md

- **`list` without `prefix`:** the table shows `prefix` as present. Is it
  required? **Choice:** required. A missing `prefix` is an invalid queries file
  (non-zero exit).
- **`range` on `get_raw` / `classify`:** not stated. **Choice:** `range` on
  `get_raw` is an invalid queries file. Other unknown members of a query
  object are ignored. The spec says unknown members are ignored for
  *descriptions* only, not for queries.
- **Unknown members inside a `range` object:** **Choice:** ignored. Only the
  known forms are counted for "exactly one form".
- **Negative numbers in queries** ("Only `modified_not_after` may be
  negative"): **Choice:** a negative `start`/`end`/`offset`/`suffix` makes the
  queries file invalid (non-zero exit). Integers ≥ 2^53 are accepted if they
  fit in u64.
- **`null` in queries:** treated as an invalid query (for example
  `"key": null`).
- **Descriptions: lone surrogates** (`"\ud800"`) are rejected by the JSON parser,
  so the description is invalid. This is the only way to express "key is not
  valid UTF-8" in JSON.
- **`source` index type:** any non-negative integer is accepted and range-checked
  against the number of sources (so huge indices are "out of range", not
  "invalid type").
- **Where pinned entries go and the record order** are left to the writer,
  which is fine. The runner's "validates its structure" step presumably uses
  the §7.1 rules only.
- **Exit codes:** this CLI uses exit status 2 for invalid descriptions, invalid
  queries files and usage errors.
