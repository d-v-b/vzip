# Notes on vzip spec (format version 0, revision 7), from a Rust implementation

Each item gives the section, the question, and what this implementation does
and why. Items are grouped roughly by how much they could affect
interoperability. The most important ones come first.

## A. Items that can make two conforming implementations disagree

1. **§9.1, zero-length or out-of-range "past the end" ranges on `key`/`data` sources.**
   "a source range of a `key` or `data` source extends past the end of that
   source's value". Does a zero-length range at `offset > len` (e.g. offset 100,
   length 0 on a 3-byte `data` source) "extend past the end"? The reader side
   (§8.3 step 4) never resolves zero-length ranges, so a reader would accept the
   archive either way, but writers can disagree on whether to reject the
   description. **Chosen:** reject when `offset + length > len`, even for
   `length = 0`. This is the literal reading of "extends past" for the interval
   `[offset, offset+length)` taken as positions. Suggest the spec say so
   explicitly.

2. **§5.1, validating fields that a later occurrence overrides.** "If a
   non-repeated field appears more than once, the last occurrence wins". If an
   *earlier* occurrence is invalid (a `uint32` above 2^32−1, a string that is not
   UTF-8), is the message malformed? Similarly for an earlier `oneof` member.
   **Chosen:** every occurrence is validated, so an invalid overridden value
   makes the message malformed. This matches protobuf runtimes, which parse
   each occurrence before merging. Worth one sentence in the spec.

3. **§6.2, Content-Encoding check scope.** "A response with a
   `Content-Encoding` header whose value ... is anything but `identity` is a
   resolution error". Does this apply to redirect (3xx) responses and to 412/416,
   or only to the 200/206 response whose body is used? And what about several
   `Content-Encoding` header *fields* (rather than one field with a list)?
   **Chosen:** checked only on 200/206. Multiple fields are joined with ", " (RFC
   9110 list semantics), so two `identity` fields count as `identity, identity`
   and fail.

4. **§6.2, duplicate `ETag` / `Last-Modified` / `Location` header fields.** The
   spec says there must be exactly one valid `Content-Range`, but it does not say
   what happens with two `ETag` or two `Last-Modified` fields when a pin needs
   them, or with two `Location` fields. **Chosen:** more than one field is a
   resolution error. An `ETag` value is compared after the usual OWS trimming
   of header values.

5. **§3.2, ZIP64 extra block longer than 8 bytes.** An entry error arises if the
   block is "shorter than 8 bytes". A longer block (for example one that also
   carries a disk-start number, or sizes written by a generic ZIP tool) is not
   an error. Which 8 bytes are the offset? APPNOTE puts fields in the order
   usize, csize, offset, disk, each included only when its 32-bit field is all
   ones. Since sizes can't be all ones here, the offset comes first. **Chosen:**
   the first 8 bytes. Suggest stating this explicitly.

6. **§8.1 / §8.4 / §10, resource limits while opening.** The format entries'
   uncompressed sizes are explicitly unchecked, so a reader must bound the
   inflation of `__vz__/sources`/`__vz__/index` itself. §8.4 says a resource
   limit is a *request* error, but opening has no request, and the harness can
   only report archive errors for open. **Chosen:** inflating a format entry
   beyond 1 GiB is an archive error. Suggest: "a resource limit reached while
   opening is an archive error".

7. **§6.2, `http:` URL without an authority.** "an `http:` URL with userinfo or
   an empty host is a resolution error". `http:/x` and `http:x` have no
   authority at all, rather than an empty host. **Chosen:** treated like an
   empty host (resolution error).

8. **§6, base URI for paths that are not UTF-8.** "Form `file://` followed by
   the path's UTF-8 bytes". POSIX paths are arbitrary bytes. **Chosen:** the raw
   bytes are percent-encoded as they are, so non-UTF-8 bytes become `%XX`. Then
   `file:` mapping decodes back to the same bytes, which round-trips.

9. **§8.6, duplicate names.** Results are unspecified. That is fine for
   conformance, but implementations will differ. **Chosen:** in an unpaged archive
   the *first* record with a given name wins. In a page, the first matching record
   in that page wins. `list` de-duplicates.

10. **§6.2, IMF-fixdate leap seconds.** RFC 9110 allows second `60`. The spec says
    the date must be an IMF-fixdate with a correct day name, but it does not say
    whether `:60` is accepted or how it compares with the pin. **Chosen:** accepted,
    and treated as the next minute's `:00` (one second later than `:59`).

11. **§6.2, empty port (`http://h:/x`).** The spec is silent. **Chosen:** default port
    80, per RFC 3986 §6.2.3.

12. **§6.2 vs §6.1, when `https:` is unsupported.** A reader that does not
    implement `https:` treats an `https:` URL, and a redirect from `http:` to
    `https:`, as a resolution error (unsupported scheme). The spec says readers
    SHOULD support https, so conformance tests involving https will
    legitimately differ. Suggest marking such tests optional.

## B. Places that were unclear but where the spec text, read carefully, decides

13. **§3.4, comment version byte not a digit** (`vzip/x`). It is unclear whether
    this is "not a vzip archive" or "unsupported version". Both are archive
    errors, so it doesn't matter. The text implies the version is "one decimal
    digit" but doesn't say what a non-digit is.

14. **§4.3 / §8.2: classify of a reference entry using method 8.** §8.2 says
    classify "looks only at the central directory record", and a reference with
    a malformed payload still classifies as `reference`. Method 8 on a reference
    is an *entry* error (§4.3, §8.4), so classify fails rather than returning
    `reference`. This is consistent, but it's easy to miss that classify can fail
    for a reference.

15. **§7.2 lookup / §8.2 list, pinned keys that also appear in a page.** In a
    valid archive every pinned entry also has a record in some page. Lookup
    goes to `pinned` first, and list must de-duplicate. "a record found in a
    page is listed only if lookup would find it there". Strictly, lookup of a
    pinned key never looks in a page, so taken literally the page record is not
    listed. Because the key is listed anyway as pinned, the result is the same.
    The wording could say "listed once".

16. **§8.2 list with pinned keys and an unparseable page.** If `list(prefix)` must
    read a page that cannot be parsed, the whole `list` fails with an entry error,
    even if the only matching keys are pinned. Implemented that way, because the
    spec says it "fails".

17. **§7.2 malformed checks.** "the last page ends where the format entries'
    records begin" (§7.1) is not in the §7.2 list, and checking it requires
    parsing records. **Chosen:** not checked at open. Likewise "no pages but body
    records exist" is not detected.

18. **§8.4, order for a `key` source that is both too short and has a body
    error.** Both map to resolution errors, so the order doesn't matter. This
    implementation checks the record's uncompressed size first, then the body.

19. **§8.4 / §6.1, pins on `file:` sources and ranges outside the file.** Both
    are resolution errors. The order (pins, then length) doesn't matter.

20. **§6, `key` source that names a pinned (and possibly hidden) entry.** Allowed.
    It is resolved through `pinned` values like any other lookup.

21. **§8.2 raw of a reference entry.** "the inflated body" of a reference entry.
    References must be STORED, so this is the body as it is, after the usual body
    checks (within the file; STORED sizes equal). A mismatched mirror body is
    returned as it is (§8.6).

22. **§5.1 and §6, `optional` pins with default values.** "a pin appears on a
    `key` or `data` source" is about *presence*: an explicitly encoded `size = 0`
    on a `data` source is an archive error. That follows from `optional`, but
    an example would help, because the rest of §5.1 says explicit zeros are
    "accepted".

## C. Things that were unnatural or unexpectedly hard to implement

23. **HTTP client.** Off-the-shelf Rust clients (ureq, reqwest) by default send
    `Accept-Encoding: gzip`, decompress transparently, follow redirects
    themselves (sometimes dropping headers across origins), and hide multiple
    header fields. Meeting §6.2 exactly (resend `Range`/pin headers on every
    hop, count hops, resolve `Location` with strict RFC 3986, reject any
    non-identity `Content-Encoding`, check `ETag`/`Last-Modified` on 200 *and*
    206) was easiest with a ~300-line HTTP/1.1 client on `TcpStream` with
    chunked decoding. Implementers should be warned that library defaults
    violate §6.2.

24. **RFC 3986 `URI-reference` validation "exactly"** requires a full grammar
    check, including IPv6 literals, IPvFuture, and `path-noscheme` (no `:` in the
    first segment of a relative reference). Most URL libraries implement WHATWG
    URL parsing, which accepts and rewrites inputs RFC 3986 rejects (spaces,
    backslashes, non-ASCII) and normalises paths. So a hand-written validator and
    resolver (§5.2.2 strict) was needed. A list of test vectors in the conformance
    suite would help a lot.

25. **DEFLATE "inflates cleanly".** flate2/miniz stops at the final block and
    reports how much input it consumed. Detecting trailing bytes and truncation
    requires checking `total_in` against the body length and handling the
    "no progress with input exhausted" state. The spec's warning about this
    was useful.

26. **Strict JSON (HARNESS).** `serde_json::Value` silently keeps the last
    duplicate member, and with default features it doesn't keep `1.0` distinct
    from `1`. A small hand-written JSON parser was simpler than working around
    both.

27. **§6 base URI.** `std::env::current_dir()` is `getcwd(3)` (good). Lexical
    `..` removal had to be hand-written, because `std::fs::canonicalize`
    resolves symlinks and `Path::components` keeps `..`.

28. **§3.1 rule 7 / Info-ZIP.** Keys up to 65535 bytes are valid, but Info-ZIP
    `unzip -t` warns "filename too long--truncating" and exits non-zero for
    very long names. Not a spec problem, but conformance runners using
    `unzip -t` should avoid such keys.

## D. HARNESS.md

29. **Unknown members inside a `range` object.** "Unknown members of a query are
    ignored", but it isn't clear whether that covers members of the nested
    `range` object. **Chosen:** ignored. A member of a form that is present but
    `null` (for example `{"start":1,"end":2,"offset":null}`) counts as present,
    so the range has several forms and the query is malformed.

30. **Non-string `key`/`prefix`, non-string `op`.** Not listed as malformed.
    **Chosen:** malformed (exit non-zero).

31. **Numbers of 2^53 or more.** The harness says all numbers are below 2^53.
    **Chosen:** in queries, a larger number makes the query malformed. In
    descriptions, any integer that fits the target field (u64/u32/i64) is
    accepted, so §9.1's overflow checks can still be exercised by a direct test.

32. **Strings that are not valid Unicode** (lone surrogate escapes like
    `"\ud800"`) in keys. The harness doesn't say. **Chosen:** the JSON file is
    invalid (exit non-zero for queries, rejection for descriptions). This
    matches §9.1 "not valid UTF-8".

33. **`pinned: true` on a reference entry with `page_size` set.** It is covered by
    both §9.1 ("a pinned entry is not a bytes entry") and the harness. Rejected.
    `pinned: false` on reference entries is accepted.

34. **Page grouping.** "a page SHOULD hold about `page_size` bytes". **Chosen:**
    greedy. Records are added to the current page while its length stays
    ≤ `page_size`, and every page has at least one record. With `page_size: 1`
    each record is its own page.

35. **Entry order in the written file.** Not specified (only the CD order is
    constrained in paged archives). **Chosen:** §9.2 layout. Non-pinned entries go in
    description order, then `__vz__/sources`, then pinned entries, then
    `__vz__/index`. Unpaged CD records are in description order, followed by
    the format records.

36. **"Do not create a file at `<out-path>`" on failure.** All validation happens
    before any output. Output then goes to a temporary sibling file that is
    renamed into place, so an I/O failure cannot leave a partial archive.

37. **Archive path that is a directory or unreadable.** Reported as an open
    failure with class `archive`, as the harness says.

## E. Other implementation choices (documented resource limits, §10)

- Resource limit: 1 GiB per operation. That bounds the value window returned by
  `get`, the inflated size of any DEFLATE body (including format entries at
  open), and HTTP response bodies. Exceeding it is a request error (an archive
  error at open, see A.6).
- No restriction on which local files `file:` URLs may read (§10 SHOULD).
  The CLI has no configuration for it.
- Pins are checked on every HTTP response and every file read. No caching
  across reads, which satisfies "not cached across opens".
- The ZIP64 end of central directory record is written with "version made by"
  45 and "version needed" 45. §9.2 only recommends values for CD records.
