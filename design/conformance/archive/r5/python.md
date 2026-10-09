# SPEC_NOTES: vzip format version 0, revision 5 (Python implementation)

Places where the spec (SPEC.md) or the harness (HARNESS.md) was ambiguous,
underspecified, contradictory or surprising, or where implementing it took a
guess. Each item gives the section, the question, and what this implementation
chose and why. Items are ranked roughly by how likely they are to cause two
independent implementations to disagree.

## A. Issues likely to affect interoperability

### A1. §6.1 / §6.2: `etag` and `modified_not_after` pins fail open over HTTP
The spec says "pins fail closed", but over HTTP the only failure signal for
`etag` and `modified_not_after` is a 412. Three common cases give no 412:
- the server ignores `Range` and conditionals and answers 200, which §6.2
  says readers MUST accept. Python's stock `http.server` does this. I checked:
  an archive pinned to `etag: "zzz"` reads successfully from it;
- the server ignores `If-Match` (some static servers and CDNs);
- the resource has no `Last-Modified`. RFC 9110 §13.1.4 then requires the
  server to ignore `If-Unmodified-Since`.

The reader could catch most of these by comparing the response's `ETag` and
`Last-Modified` headers with the pins, but §6.2 neither requires nor allows
that. **Choice:** I followed §6.2 exactly and added no header comparison.
Adding one would turn a 200 that §6.2 says to accept into an error.
**Suggest:** say whether a reader MAY or MUST check `ETag` and `Last-Modified`
on 200 and 206 responses. Or state plainly that HTTP pins are only as strong
as the server's support for conditional requests.

### A2. §6.2: redirects are optional, so readers disagree
"Readers MAY follow up to 5 redirects." A reader that doesn't follow them
treats a 3xx as "any other status", which is a resolution error. A reader that
does gets the bytes. The same archive and server therefore give different
results. The spec also doesn't say:
- which status codes count as redirects;
- whether `If-Match` and `If-Unmodified-Since` are sent again on each hop (the
  pins "apply to the final response");
- whether a redirect may change scheme (http to https, or to `file:`).

**Choice:** I follow 301, 302, 303, 307 and 308, up to 5 hops. I resolve
`Location` against the current URL, send the same headers on every hop, and
allow only http and https targets. The 6th redirect is a resolution error.
**Suggest:** make following redirects either MUST or MUST NOT, and list the
codes.

### A3. §6.2: response cases the table doesn't cover
- A 206 with no `Content-Range`, for example `multipart/byteranges`:
  resolution error (my choice).
- A 206 whose body length differs from its `Content-Range`: resolution error.
- A 206 whose `Content-Range` total is smaller than the requested end. This is
  covered only indirectly: the server must then clamp the range, which hits
  "not the one requested".
- A `Content-Encoding: identity` header is accepted, and an empty one is also
  accepted (my choice).
- §3.4 and §6.2 never say whether the size pin is checked against the
  `Content-Range` total even when the range itself is wrong. I check the
  range first.

### A4. §8.2 `list` on paged archives: which pages to read
"Reads every page whose range includes at least one key starting with
`prefix`" means intersecting the half-open interval `[first_key_i,
first_key_{i+1})` with the set of strings that have the prefix. That set is
the interval `[p, succ(p))`, where `succ(p)` comes from dropping trailing 0xFF
bytes and incrementing the last byte; it is unbounded for an empty prefix.
This is correct but not obvious, and it decides which pages are read, and so
which broken pages cause an entry error. **Suggest:** give the formula, or an
example such as "prefix `b/` reads the page with `first_key` `a` if the next
page's `first_key` is greater than `b/`".

### A5. §3.2: ZIP64 sizes in central directory records
The spec defines the `0x0001` block in terms of all three all-ones fields, but
lists entry errors only for "a record whose offset is all ones". A record
whose `csize` or `usize` is 0xFFFFFFFF breaks rule 7, which "need not be
detected". **Choice:** I follow APPNOTE. Every all-ones field (usize, csize,
offset, in that order) is read from a single `0x0001` block. A missing,
duplicate or too-short block is an entry error whichever field is all ones.
A `0x0001` block on a record that needs none is ignored, and a longer block is
fine.

### A6. Writer rejection of ranges the writer could check (§9.1)
§9.1 doesn't require the writer to reject ranges that are known to be out of
bounds when it writes:
- a `key` source range past the end of the named bytes entry;
- a `data` source range past the end of `data`.

A reader then reports a resolution error. **Choice:** I don't reject them.
These are valid archives with failing ranges. A conformance runner that
expects rejection would disagree. **Suggest:** say explicitly that this is
not rejected, or that it is.

### A7. §8.4 / §3.4: `raw` of the format entries
`raw("__vz__/sources")` and `raw("__vz__/index")` must return "the inflated
body", but §3.4 only says readers read these entries *through the comment*
when opening. In archives where the comment and the record disagree (allowed
by §8.6), readers could return different bytes. **Choice:** raw of a format
entry uses the comment's offset and size, with no uncompressed-size check, so
it can never fail after open. In an unpaged archive, `raw("__vz__/index")` is
*missing*, because such a record is an archive error anyway.

### A8. §8.1: what "lies within the file" means
For the central directory, the zip64 end record, the format bodies and pinned
bodies, the spec only says "lies within the file". It doesn't say whether:
- the central directory must end before the EOCD or zip64 record;
- the zip64 record must precede the locator;
- regions may overlap.

**Choice:** I check only `offset + size <= file_size`. Anything stricter would
be an open error the spec doesn't list ("MUST NOT report an error at open that
this section does not list").

### A9. §8.4: the resource limit
Readers SHOULD bound memory and report a request error when a request goes
over the bound. The bound and the place it is checked are
implementation-defined, and §8.4 admits the error class may differ.
**Choice:** 1 GiB for one request window or one inflated body. Inflating a
format entry above 1 GiB is reported as an archive error, because it happens
at open.

## B. Ambiguities and surprises in the text (lower interoperability risk)

### B1. §3.4: "step 1 cannot match a record that isn't the real one"
This holds only because the real EOCD's disk-number field is 0. §3.2 says
readers MUST *ignore* disk-number fields. I built a file whose real EOCD has
disk number 38 and a valid `vzip/0` comment at `size−44`, with a second EOCD
signature 16 bytes earlier. Step 1 matches the fake record, the magic fails,
and (with no fallback) the file is rejected. Without the fake signature, the
same file opens fine. This is well defined, but the justification in §3.4 is
weaker than it reads (`tests/test_reader_errors.py::test_no_fallback_from_step_1`).

### B2. §3.4: the comment is binary
The 22- and 38-byte comments contain raw u64 values. `unzip` prints the
archive comment, so every `unzip -t` / `unzip -l` shows garbage bytes, and a
test harness that decodes `unzip` output as UTF-8 crashes (mine did). This is
surprising for a format that advertises "always a valid ZIP file".

### B3. §3.3 / §5.1: what "valid UTF-8" means
The spec doesn't say whether overlong forms, encoded surrogates
(`ED A0 80`) or code points above U+10FFFF are invalid. **Choice:** a strict
decoder (Python's `utf-8` codec) rejects all three, for file names and for
protobuf strings. **Suggest:** reference RFC 3629.

### B4. §5.1: encoding empty repeated messages
"Not emit a scalar field equal to its default" doesn't mention repeated
*message* elements. A `Range` with all-default fields (source 0, offset 0,
length 0, which is a valid zero-length range) encodes to zero bytes. Inside a
`Concat` it must still be emitted as `0a 00`, or the part disappears.
**Choice:** I always emit repeated message elements. **Suggest:** say so,
since it is needed for the "exactly one encoding" claim.

### B5. §5.2 / Appendix A: `int64` negative values
`modified_not_after` is `int64`. The spec relies on protobuf's convention that
negative values are 10-byte two's-complement varints, never zigzag. Worth
stating, since this is a hand-written codec in every implementation.

### B6. §6: a `key` source with an empty key
At open, an empty `url` is an archive error, but an empty `key` (or `data`)
is not mentioned. **Choice:** an empty `key` is accepted at open, and
resolving it is a resolution error (missing key). The writer rejects it as
"names a key that is absent".

### B7. §6: building the base URI
- The spec doesn't say how to handle `..` at the root, as in `/../x`.
  **Choice:** drop it, like POSIX: `/../x` becomes `/x`.
- Symbolic links are not resolved. So an archive opened through a symlink in
  another directory resolves relative URLs against the *link's* directory, not
  the target's. That follows the spec, but users may not expect it (tested in
  `test_symlinks_not_resolved`).
- A path beginning with exactly two slashes (`//x`) is normalised to `/x`.
  POSIX `normpath` keeps `//`, so naive implementations get this wrong.

### B8. §6: `file:` authority forms
"Absent, empty or `localhost`" doesn't cover `localhost:` (empty port),
`localhost:80`, `user@localhost`, or a percent-encoded `localhos%74`.
**Choice:** only an exact `localhost` (case-insensitive) is accepted.
Everything else is a resolution error.

### B9. §6: what counts as "a valid URI reference"
The spec says only that the reference must match `URI-reference`. It doesn't
say whether the *resolved* http(s) URL must also be usable. For example,
`http:x` or `http:///x` have no host. **Choice:** both give a resolution
error. For http(s) URLs with userinfo, the userinfo is silently ignored and
never sent as credentials. The spec doesn't say what to do with userinfo.

### B10. §6.2: `https:` certificate verification
This is not mentioned. **Choice:** Python's default context, which verifies
certificates.

### B11. §6.1: when pins are checked for `file:`
"Before returning any byte read from a pinned source, a reader MUST check
every pin." For `file:`, I check the pins on every read: I `fstat` the opened
file, then read it. Between two reads of the same get, the file may change.
The spec allows per-open caching only for HTTP (as a SHOULD).

### B12. §8.2: negative request values
`range`, `offset` and `suffix` take integers, but the spec never says they are
non-negative. **Choice:** a negative bound is a request error. (The harness
says numbers are below 2^53 in magnitude and only `modified_not_after` may be
negative, so this shouldn't be tested.)

### B13. §7.1: why `__vz__/index` must come last
§9.1 says `__vz__/index` "MUST follow every entry ... whose record it indexes
or pins; otherwise its own size would change the offsets it records". Page
offsets are relative to the central directory, so they don't depend on where
the index sits. Only `Pinned.data_offset` does. The stated reason applies only
to pinned entries, but the rule applies to all of them. I complied: the index
is the last local entry.

### B14. §7.2: page lookup versus records outside their page's key range
The text is clear: such a record is not found and not listed. But it is easy
to get wrong, because a reader that reads the whole central directory finds
it. The spec says those readers get "the same result for every valid archive",
which is true but invites divergence on invalid ones. I implemented the paged
semantics strictly (`test_record_outside_its_page_range`).

### B15. §1.3: a dangling link
§1.3 links to `conformance/REVISIONS.md`, which wasn't provided.

### B16. §3.2: the zip64 locator's "total number of disks"
Writers set it to 1, and readers ignore it as a disk-number field. That's
consistent, but the locator's disk-number field (the disk holding the zip64
record) isn't mentioned. I write 0 and ignore it.

### B17. §9.1: rejection depends on the compressor
"A bytes entry's compressed size is 0xFFFFFFFF or more" depends on the
writer's compressor, so whether a near-4 GiB incompressible input is rejected
differs between implementations. This is minor.

### B18. Unnatural or hard to implement
- Strict RFC 3986 parsing and resolution (§5.2.2 with `remove_dot_segments`)
  had to be hand-written. `urllib.parse.urljoin` isn't strict, and it differs
  from the RFC on edge cases like `g;x=1/../y` and `http:g`.
- Checking that a DEFLATE body "inflates cleanly" needs explicit checks for
  `eof`, `unused_data` and the output length. zlib ignores trailing bytes by
  default, as the spec warns.
- Python's `os.path.normpath` keeps a leading `//`, so I wrote the base-URI
  normalisation myself.
- Testing writer ZIP64 *offsets* needs a file over 4 GiB. My writer builds
  archives in memory, so only the reader's handling of offsets above 4 GiB is
  tested (with a hand-made sparse file, which `unzip -t` also accepts). The
  writer's 65535-entry zip64 end record path is tested.

## C. HARNESS.md

- **`list` without `prefix`:** the harness lists "missing `key`" as malformed
  but says nothing about a missing `prefix`. **Choice:** invalid queries file
  (exit non-zero).
- **`range` on `classify`, `get_raw` or `list`:** unspecified. **Choice:**
  invalid queries file. Unknown members of query and range objects are
  ignored. The harness says unknown members are ignored only for
  descriptions.
- **Duplicate JSON member names:** unspecified. Python's `json` keeps the last
  one.
- **Null `bytes`, `ranges`, `key`, `data`:** treated as invalid, per "null is
  allowed only for page_size".
- **`"source"` with a literal `"data"`:** a range with `"data"` plus any of
  `source`, `offset`, `length` is "a mix", even when their values are 0.
  Treated as invalid.
- **Archives behind HTTP:** `read` takes only a local path, so HTTP is
  exercised only through absolute `http://` URLs inside archives. Relative
  URLs against an HTTP base URI can't be tested through the harness. The
  library supports a `base_uri` override and is tested that way.
- **Key strings that can't be UTF-8:** a query key with a lone surrogate (JSON
  `"\ud800"`) can't be UTF-8. **Choice:** such a key is never present
  (classify returns `missing`). For writing, it is an invalid key.
- **The exit-status rule for `write`:** "exit with a non-zero status" doesn't
  separate an invalid description from an I/O failure. I use exit code 1 for
  both and 2 for an unreadable or non-JSON description file.
