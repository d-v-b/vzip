# SPEC_NOTES: vzip v1 draft rev. 4, Python implementation

Layout: `vzip` (shell entry point), `vzip_impl/` (`pb.py` protobuf, `uri.py` RFC 3986,
`reader.py`, `writer.py`, `cli.py`), `tests/` (run with
`cd tests && uv run --no-project --python 3.12 python -m unittest discover -p 'test_*.py'`).

Each item gives the section, the question, and what this implementation chose.
Items are roughly ranked by how much they could hurt interoperability.

## A. Issues likely to cause implementations to disagree

1. **§6.1 contradicts itself on rounding `modified_not_after`.** The pin table says the
   modification time is "rounded down, i.e. towards negative infinity". The `file:` check
   column says "truncated to whole seconds". These differ for mtimes before 1970, such as
   `-0.5 s`, which floors to -1 and truncates to 0. *Chose floor* (`st_mtime_ns // 10**9`).
   Suggest saying "rounded down (floor)" in both places.

2. **§8.2 `list` on a paged archive: what is a page's "key range"?** The text says list
   "reads every page whose key range can contain a key with that prefix", but the key range
   of a page is never defined. *Chose* `[first_key_i, first_key_{i+1})`, with the last page
   unbounded. The prefix range is `[p, succ(p))`, where `succ` strips trailing 0xFF bytes
   and increments the last byte, and the empty prefix or an all-0xFF prefix is unbounded.
   A page is read iff the two half-open ranges intersect. Computing `succ(p)` is fiddly and
   easy to get wrong at boundaries, for example when the next page's `first_key` equals `p`
   exactly. That decides whether a corrupt page fails `list`, so it shows up as a visible
   difference (entry error versus success). Suggest defining both ranges and the
   intersection test normatively.
   - Related: should list read pages whose range can only hold *hidden* keys? With prefix
     `""`, a corrupt page that holds only `__vz__/...` records makes `list("")` fail in my
     implementation, even though no key from it would be listed. *Chose: yes* (the literal
     reading). An implementation that skips the hidden range would return success.

3. **§8.4 step 4: does "body lies outside the file" for a STORED entry depend on the window?**
   "A STORED body is read only within the window" suggests only the window is checked.
   "a bytes entry's body lies outside the file" suggests the whole `[body_offset,
   body_offset+csize)` is checked. For a truncated archive and a small window (or
   `suffix(0)`), these give different answers. *Chose:* always check the full extent (no I/O
   is needed), so the result is a body error regardless of the window.

4. **§3.2 ZIP64 extra field parsing.** The spec says the block contains "only that value"
   (the offset), but a general APPNOTE parser reads the usize, csize and offset fields in
   order, for each header field that is 0xFFFFFFFF. In a non-conforming record whose size
   field is also 0xFFFFFFFF, readers following the two models read different offsets.
   It is also unspecified what happens if the 0x0001 block is present but shorter than 8
   bytes, or appears twice. *Chose:* APPNOTE order (usize, csize, offset, each only if its
   field is all ones). A block too short to hold a needed value is an entry error. The first
   0x0001 block wins. Suggest stating that only the offset is read from the first 8 bytes,
   and that a short block is an entry error.

5. **§8.1 "lies within the file" for the central directory and the zip64 record.** Neither
   is required to lie *before* the end records. A CD whose declared extent overlaps the
   zip64 record, locator or EOCD passes the check. *Chose:* bound by the file size only
   (literal reading). A reader that bounds by `eocd_offset` (or by the locator offset for the
   zip64 record) reports an archive error on such a file. Suggest saying which bound is meant.

6. **§3.2 / §8.1: the EOCD and zip64 entry counts are never checked.** §8.1 lists no check
   against the number of parsed records, and says a reader "MUST NOT report an error at open
   that this section does not list". So a count mismatch must be silently accepted. That is
   probably intended, but many ZIP libraries check it, so a reader built on one would report
   an error the spec forbids. Worth saying explicitly ("entry counts are not used").

7. **§6 / §8.1 / §9.1: URL syntax is checked at different times by readers and writers.**
   Writers must reject a `url` that is not a URI-reference. Readers do *not* check it at open
   (it is not in the §6 open-time list). It surfaces only as a resolution error when a range
   that uses the source is resolved (§6, "It is a resolution error if the reference is not a
   valid URI reference"). *Implemented that way.* An implementer might well put it into the
   open checks next to "a `url` is empty". Suggest calling the asymmetry out explicitly.

8. **§8.1: the format entries' uncompressed size.** "For a bytes entry it must also inflate
   to the record's uncompressed size" does not say whether that applies to the format entries.
   They are located through the comment, which has no uncompressed size, and readers "need
   not consult those entries' central directory records". *Chose:* no size check for format
   entries; they must only inflate cleanly. I applied a 4 GiB inflation cap, beyond which
   opening fails. A reader that cross-checks the CD record's usize gives a different result
   on such an archive. §8.6 makes it unspecified, but only implicitly.

9. **§8.4 / §7: pinned STORED entries whose `size != csize`.** The pinned values replace the
   record. The spec does not say whether a STORED pinned entry with `size != csize` is a body
   error (like a STORED record) or an open-time malformed index. *Chose:* body error at
   `get`/`raw` time (it is not on the §7.2 malformed list).

10. **§5.1 "valid UTF-8" is not defined.** Encoded surrogates (`ED A0 80`), overlong forms and
    code points above U+10FFFF are rejected by strict decoders but accepted by some lenient
    ones (for example WTF-8 in some runtimes). *Chose:* Python's strict `utf-8` (RFC 3629).
    Suggest citing RFC 3629. The same question applies to keys (§3.3).

11. **§6 base URI from a non-UTF-8 POSIX path.** "Form `file://` followed by the path's UTF-8
    bytes" assumes the path *is* UTF-8. *Chose:* raw `os.fsencode` bytes, percent-encoded.

## B. Underspecified, but my choice is probably what everyone does

12. **§3.2 zip64 EOCD record fields.** "version needed to extract" is fixed at 45, but
    "version made by" is not given (§9.2 only talks about CD records). *Wrote 45.* Readers
    ignore it anyway.
13. **§3.1 local header "version needed to extract"** is not specified (§9.2 recommends values
    only for records). *Wrote 20*: local headers never carry ZIP64 data.
14. **§9.1 / §7.1: the CD order of an unpaged archive** is unspecified. *Chose:* the same
    sorted order as paged archives (body records in UTF-8 order, then format entries).
15. **§7.1 page grouping.** HARNESS says a page "SHOULD hold about `page_size` bytes". *Chose:*
    a page is closed as soon as its size is at least `page_size`, so every page except the last
    is at least `page_size` bytes, and `page_size: 1` means one record per page.
16. **§9.2 placement.** *Chose:* non-pinned entries in description order, then
    `__vz__/sources`, then pinned entries, then `__vz__/index`, then the CD. The index's
    own CD record comes last, so it cannot shift the page offsets.
17. **§3.2: what "within the file" means for the zip64 locator** when the EOCD is at offset
    < 20. Treated as "locator missing" (archive error).
18. **§8.4: duplicate names** (§8.6, unspecified). The first record wins for lookup, and
    `list` deduplicates.
19. **§6: `key` sources naming a pinned entry, or a key in an unparseable page.** Lookup follows
    §7.2, so pinned entries work as sources. An entry error during the lookup becomes a
    resolution error. The spec lists "has an entry error", but does not mention an unparseable
    page explicitly (it is an entry error for the key per §8.4, so this follows).
20. **§8.2 raw of format entries in a paged archive.** The format records are not in any
    page, so `raw("__vz__/sources")` and `raw("__vz__/index")` are served from the bodies the
    comment locates. That seemed to be the intent ("The format entries are known from the
    comment"). In an unpaged archive, `raw("__vz__/index")` is *missing*.
21. **§10 resource limit.** Documented limit: a single `get` returns at most 1 GiB (larger
    windows are a request error). DEFLATE bodies are inflated with an output cap of the
    record's uncompressed size, plus 1 byte.
22. **§6 `file:` mapping and literal empty segments.** `file:///a//b` keeps the empty segment.
    POSIX treats it as `/a/b`. The spec forbids decoded `/` and dot segments but says nothing
    about empty ones (unlike the base-URI construction, which removes them).
23. **§6 schemes.** Only `file:` is implemented. `http:`/`https:` (SHOULD) return a
    resolution error, "unsupported scheme".

## C. Surprising or hard to implement

24. **Strict RFC 3986 (§6).** Python's `urllib.parse` neither validates `URI-reference` nor
    implements strict §5.2.2 resolution (`urljoin` has scheme-specific, non-strict behaviour,
    for example `urljoin("http://a/b", "http:g")`). I had to hand-write the full ABNF as a
    regex (including IPv6 literals) and the resolver. The spec could include a few test
    vectors: RFC 3986 §5.4 plus the `file:` cases (`%2E%2E`, `%2F`, `?`, `file://host/`).
25. **"Inflates cleanly" (§8.1)** needs `zlib.decompressobj(-15)`, then checks of `eof`,
    `unused_data` and `unconsumed_tail` and the output length. A plain
    `zlib.decompress(..., -15)` silently ignores trailing bytes. The spec warns about this,
    which helped.
26. **§3.4 two-step EOCD search.** Step 1 needs `file_size >= 60`, and step 2
    `file_size >= 44`. That is obvious, but the spec does not state it.
27. **§8.4 check order with request errors.** The `start > end` check comes before
    everything, including for keys a client cannot even encode. In the CLI, JSON strings
    holding lone surrogates are treated as keys that are never present, after the request
    check.
28. **§3.2 testing.** ZIP64 offsets (>= 4 GiB) cannot be exercised cheaply. My tests cover
    the zip64 EOCD path through the entry count (>= 65535 entries). The offset path
    (0x0001 block in CD records) is tested only on the reader side, with a hand-patched record.
29. **§4.3 payload limit 65519.** It is correct (65535 − 4 − 12), but a writer has to compute
    the *encoded* payload size, so a single literal of 65515 bytes is the largest that fits
    (tag + 3-byte length + data). A worked example would help.
30. **§5.1 "a known field whose wire type differs"** also applies to the *reserved* field 2
    of `Range`? The text says it is "skipped like any unknown field", so any of wire types
    0/1/2/5 is accepted. *Implemented that way.*

## D. HARNESS.md

31. `compress: true` on a reference entry is invalid. Is an explicit `compress: false` on a
    reference entry valid? *Chose: valid* (the sentence only forbids `true`).
32. A `range` member on `classify`/`get_raw`/`list` queries: *chose* invalid queries file
    (non-zero exit). Unknown members in query objects are ignored. HARNESS says what is
    malformed only by example.
33. "All numbers ... are non-negative integers below 2^53" conflicts with
    `modified_not_after` being `int64` (it can be negative). *Chose:* accept any integer in
    int64 range for that member. Other numbers must be non-negative integers.
34. Keys in JSON may contain lone surrogates (`"\ud800"`), which cannot be UTF-8. The writer
    rejects them (invalid key). The reader treats them as never present (classify `missing`,
    get `null`, list `[]`).
35. A source element with pins on a `key`/`data` source is rejected (spec §9.1). HARNESS only
    says "A `url` element may also have pins". Probably intended.
36. "A file that cannot be opened or read at all" includes a directory path: reported as
    `open.ok = false`, class `archive`.
37. The writer writes to `<out>.tmp-vzip` and renames, so a failed write never leaves
    `<out-path>`. HARNESS does not say whether sibling temporary files are acceptable.
