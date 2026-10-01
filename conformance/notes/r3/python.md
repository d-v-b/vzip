# Notes on the vzip specification (draft, revision 3)

These notes come from writing a Python reader and writer (stdlib only) using
only SPEC.md and HARNESS.md. Each item gives the section, the question, and
what this implementation chose and why. Items are ranked roughly by how much
they could make independent implementations disagree. The first group could
change an observable result, such as an error class, a key list or an
accept/reject decision. The later groups are clarity and difficulty notes.

## A. Points where implementations could plausibly disagree

1. **§8.2 `list` / §7.2: which records in a page count as "present".**
   §7.2 defines presence through page selection: `k` is present only if the
   page selected for `k` contains `k`. A page can hold a record whose name
   falls outside `[first_key_i, first_key_{i+1})` (a writer violation that
   §8.6 says readers need not detect). Should `list` report such a key? A
   naive `list` that dumps every record in the scanned pages would report it,
   but `classify` would say `missing`.
   *Chosen:* `list` reports a key from page `i` only if
   `first_key_i <= k < first_key_{i+1}`, so `list` agrees with `classify`.
   The spec says the result is unspecified for such archives, but it would
   help to state that `list` and `classify` must agree.

2. **§7.2 / §8.2: pinned keys in `list`, and pinned keys that conflict with
   their page.** §7.2 says a pinned key "is a present bytes entry, described
   by its Pinned values". So `list` must include pinned keys even when no
   page holds them, and `classify` returns `bytes` even when the page record
   says `reference`. Is that intended? Must `list` read the pinned list as
   well as the pages?
   *Chosen:* `list` = (pinned keys ∪ in-range page keys), deduplicated,
   hidden keys removed. Pinned values always win over the page record. It
   would help to state this in §8.2.

3. **§8.4: class of a `list` failure.** `list` "fails if one of [the pages]
   cannot be parsed", but the table names no class for this. The entry error
   row mentions "the page that holds the key's record cannot be parsed", but
   `list` is not a per-key operation, and the effect column only names
   classify, get and raw.
   *Chosen:* `entry`. The harness needs a class; please state one.

4. **§3.2: which end-of-central-directory fields count for "a field of all
   ones without a locator is an archive error".** §3.2 also says readers MUST
   ignore disk-number fields. Is "number of entries on this disk" a
   disk-number field? Do the two disk-number fields count if they are
   0xFFFF?
   *Chosen:* entries-on-this-disk (0xFFFF), total entries (0xFFFF), CD size
   (0xFFFFFFFF) and CD offset (0xFFFFFFFF) trigger the error. The two
   disk-number fields are ignored. Please list the fields explicitly.

5. **§8.1: "inflate cleanly" for the format entries, and §8.4 "does not
   inflate" for bytes entries.** Is trailing data after the end of the
   DEFLATE stream (inside the declared compressed size) an error? Is a stream
   that never sets BFINAL an error? zlib-based readers easily get this wrong,
   because they return without complaint after the final block and leave the
   trailing bytes in `unused_data`.
   *Chosen:* the stream must end (BFINAL block completed) exactly at the end
   of the given compressed bytes. A truncated stream or trailing bytes is an
   error (archive error for format entries, body error for bytes entries).

6. **§4.1 / §8.4: a record with local header offset 0xFFFFFFFF but no
   (or a too-short) ZIP64 `0x0001` block.** What class is this? §8.1 says
   problems confined to one record's "extra field, method, flags or name"
   are entry errors, but the §8.4 table lists only specific causes. And does
   it make `classify` fail, given that `classify` doesn't need the offset?
   *Chosen:* entry error, for classify, get and raw alike, because it is a
   record-level parsing failure. The alternative (a body error, only when
   the body is needed) is equally defensible. Please list it in §8.4.

7. **§8.4 / §10: when the documented request-size limit is checked,
   relative to resolution errors.** The order of checks puts "the request
   (request error)" first, but a resource limit can only be judged after the
   payload is decoded and the window is known. If a request exceeds the
   limit *and* an earlier range is unresolvable, which error is reported?
   *Chosen:* the limit (1 GiB per operation, `MAX_REQUEST_BYTES` in
   `reader.py`) is checked lazily, piece by piece, after that piece's source
   has been resolved and its length checked. So resolution errors in earlier
   or current ranges come first, and an oversized but fully resolvable
   request fails with a `request` error. For bytes entries, a body whose
   record's uncompressed size exceeds the limit is a request error, checked
   after the "body outside the file" check. Please say where in the check
   order the resource limit goes.

8. **§8.1: the entry count in the end records is never checked.** The open
   checks don't include "the number of records parsed equals the declared
   total". Since readers "MUST NOT report an error at open that this section
   does not list", a mismatched count must be accepted silently.
   *Chosen:* ignore the count completely, which is what the text requires.
   If that is the intent, say so explicitly, because most ZIP readers check
   it.

9. **§8.1: "lies within the file" for the central directory and zip64
   record.** Is the central directory allowed to overlap the end records? To
   extend past the zip64 EOCD?
   *Chosen:* only `cd_offset + cd_size <= file_size` and
   `z64_offset + 56 <= file_size` are checked. Overlaps are not detected.

10. **§8.2 / §8.4: must a ranged `get` of a bytes entry detect a body that
    inflates to the wrong size?** A streaming reader serving `range(0, 10)`
    of a large DEFLATE entry would not normally inflate the whole body, so it
    would not see a size mismatch or a corrupt tail. Is that a conforming
    result?
    *Chosen:* always inflate the whole body and check its size, so the result
    does not depend on the request. Please state which is required. If
    partial inflation is allowed, a corrupt tail gives different answers
    depending on the window.

11. **§6.1: etag "strong entity tag" syntax with non-ASCII.** RFC 9110
    `etagc` allows `obs-text` (bytes 0x80–0xFF), but the `etag` field is a
    proto `string`, so it is UTF-8 text, not bytes.
    *Chosen:* allow `"`, then any run of characters that are `!`, `#`–`~` or
    any non-ASCII character, then `"`. Spaces, internal `"` and control
    characters are rejected. Say whether non-ASCII is allowed.

12. **§6.1: "truncated to whole seconds" for `file:` modification times
    before 1970.** Truncation toward zero and floor differ for negative
    times.
    *Chosen:* floor (`st_mtime_ns // 10**9`). This matters only for
    pre-1970 timestamps.

13. **§6, `file:` mapping: authority forms.** Is `file://localhost:/x` (empty
    port) or `file://%6Cocalhost/x` (percent-encoded) acceptable? Is
    `file:/x` (no authority at all, which RFC 3986 distinguishes from an
    empty authority) acceptable?
    *Chosen:* the authority must be absent, empty, or exactly `localhost`
    (case-insensitive), with no port and no percent-decoding. `file:/x` is
    accepted.

14. **§6: base URI normalisation details.** "`.` and `..` segments are
    removed lexically" doesn't say what happens to repeated slashes (`a//b`),
    a leading `//`, which POSIX leaves implementation-defined, or `..` above
    the root.
    *Chosen:* empty segments are collapsed (like `os.path.normpath`, but also
    for a leading `//`), and `..` at the root stays at the root.

15. **§8.3 / §6: does a `key` source get the "shorter than `offset + j`"
    check before or after the body checks?** Both are resolution errors, so
    the class is the same and only the message differs. *Chosen:* the
    record's declared size is checked first, then the body.

16. **§8.6 vs CRC.** Readers "need not verify CRC-32 values", and one that
    does reports a body error. So the same archive can be `ok` for one
    conforming reader and `body` error for another. If the conformance suite
    contains any archive with a wrong CRC, it must not check that result.
    *Chosen:* CRCs are not verified. The writer writes correct CRCs.

17. **§5.2: a literal range that encodes `source`/`offset`/`length` with an
    explicit 0 on the wire.** "MUST all be 0" is read as the decoded value,
    so an explicit zero is fine, and "present but zero" is not malformed.
    *Chosen:* compare decoded values only.

18. **§3.4: the comment's `sources_size` is the compressed size only.** The
    reader has no uncompressed size for the format entries, so it cannot
    bound their inflation in advance or check it against anything (§10 asks
    readers to bound memory). *Chosen:* inflate without a bound. Consider
    adding the uncompressed sizes, or saying that readers may cap them.

## B. Underspecified writer behaviour (affects byte-for-byte comparison only)

19. **§3.2 / §9.2: fields of the zip64 end of central directory record.**
    "version made by" in the zip64 EOCD is not specified (only "version
    needed 45"). *Chosen:* 20, to match §9.2.
20. **§3.2: placement of the ZIP64 `0x0001` block relative to the reference
    block.** "Any order" is allowed. *Chosen:* `0x0001` first.
21. **§7.1: order of `pinned` in the index, and page grouping.** Neither is
    specified. *Chosen:* pinned sorted in UTF-8 key order. Pages are filled
    greedily: a new page starts when adding the next record would exceed
    `page_size` bytes, and a page always holds at least one record.
22. **§9: order of entry bodies in the file, and of the central directory in
    an unpaged archive.** Not constrained. *Chosen:* non-pinned entries in
    input order, then `__vz__/sources`, then pinned entries, then
    `__vz__/index`. The central directory is in UTF-8 order with the format
    records last, paged or not.
23. **§3.1 DEFLATE level** is unspecified (fine). *Chosen:* zlib level 9.

## C. Clarity issues (no interoperability effect found, but they slowed implementation)

24. **§3.4 step 1 rationale** ("would take its comment length from the real
    record's disk-number field") took a while to verify. It holds only
    because a real 22-byte comment puts the real EOCD at `size − 44`, so
    `size − 60 + 20` lands on its "number of this disk" field. A one-line
    diagram would help.
25. **§4.3: "Extra field blocks may appear in any order."** This sentence sits
    in the middle of the method-0 paragraph, where it reads as if it were
    about reference entries only. It applies to all records (§4.1).
26. **§8.4 order of checks for `raw`.** Step 2 (hidden → missing) does not
    apply to raw, and step 3 for the format entries comes from the comment,
    not from a lookup. Both follow from §8.2 but aren't in the list.
    *Chosen:* raw(`__vz__/sources`) and, in a paged archive, raw(`__vz__/index`)
    return the inflated bodies located by the comment. raw(`__vz__/index`) in
    an unpaged archive is `missing`. That is the only possibility, because
    such a record is an archive error at open.
27. **§6, `key` source "is a format entry".** In a paged archive the format
    entries' records sit outside all pages, so a lookup would simply say
    "missing". In an unpaged archive the lookup finds a real record. Both
    give a resolution error, so the outcome is the same, but checking the
    name first is simpler and avoids a CD read. This deserves a sentence.
28. **§5.1: field numbers.** "Field number 0, or above 2^29 − 1" doesn't say
    whether a tag varint whose value exceeds 2^32 (but not 2^64) is checked
    as a field number above 2^29 − 1 or rejected as an oversized tag. The
    answer is "malformed" either way, so this is fine.
29. **§3.3 vs JSON:** a key that is "not valid UTF-8" can only reach the
    harness writer as a JSON string with a lone surrogate escape
    (`"\ud800"`). It is worth saying so in HARNESS.md, because many JSON
    parsers reject or replace lone surrogates before the writer sees them.
30. **§3.1 rule 7 / §9.1:** "less than 0xFFFFFFFF" and "0xFFFFFFFF or more"
    agree. Note that a stored entry of exactly 0xFFFFFFFE bytes is legal.
31. **§4.3 65519 limit:** this checks out (65519 + 4 + 12 = 65535). A worked
    example of the largest literal that fits (a single `Range` with
    `data` of 65515 bytes, i.e. tag 1 + 3-byte length + 65515) would help
    writers test the boundary. The implementation tests exactly this.

## D. Things that were unnatural or unexpectedly hard

- **Strict RFC 3986 validation and resolution** had to be written by hand.
  Python's `urllib.parse` accepts invalid characters and its `urljoin` is
  not RFC 3986 strict (scheme-specific behaviour, no `remove_dot_segments`
  on references with a scheme). Most languages will have the same problem.
  Consider an appendix with test vectors (validation and resolution).
- **ZIP without `zipfile`:** easy. Rule 4 (no local extra field) makes ZIP64
  local headers impossible, which is clean. Testing archives over 4 GiB was
  impractical, so the ZIP64 `0x0001` path in the writer is covered only by
  reading hand-built archives (reader side).
- **"Exactly fills" parsing of pages:** a page that cuts a record in half is
  only detected when the page is read. That is correct per spec, but it means
  a paged archive with a corrupt central directory opens successfully.
- **Error-order bookkeeping** (§8.4) needed several passes, particularly for
  `key` sources, where a target's entry error or body error must become a
  *resolution* error, and for the request-size limit (item 7).

## E. HARNESS.md

- **H1.** Exit status for an invalid query object (unknown `op`, missing
  `key`, a `range` with two forms, negative numbers). "The queries file was
  unreadable or invalid" covers it. *Chosen:* exit 2 with a stderr message,
  printing no JSON (the per-query rule "one failed query must not prevent
  the others" applies only to spec errors).
- **H2.** `get_raw` with a `range` member is described as "(no `range`)".
  Is a `range` member ignored or an invalid query? *Chosen:* ignored.
- **H3.** `"modified_not_after"` is an `int64` in the schema, but the harness
  says all numbers are non-negative. *Chosen:* the writer accepts any int64.
- **H4.** Unknown members are ignored, but a range with `data` *and* an
  unknown member is fine while `data` + `source` is invalid. That is clear,
  but a range with both `data` and `"length": 0` is also invalid, which may
  surprise.
- **H5.** "The runner never passes an `<out-path>` that already exists":
  the implementation still refuses to overwrite (it writes to a temporary
  file, then hard-links it into place).
- **H6.** It is not said whether a `pinned` entry must also appear in a page.
  Per spec §7.1 all non-format entries are body records, so pinned entries
  appear in pages too. This implementation does that.
- **H7.** `get` with a range on a missing key with `start > end` is a request
  error (spec §8.4 order). HARNESS doesn't say so, but it follows from the
  spec.
