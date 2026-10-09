# Notes on the vzip specification (draft, revision 3)

Written while implementing a Rust reader and writer (`src/`) from `SPEC.md` and
`HARNESS.md` alone. Each item gives the section, the question, and the choice
made. Items are grouped by how much they could affect interoperability. The
most important ones come first.

## A. Likely to cause implementations to disagree

1. **§8.1 "inflate cleanly", §8.4 "does not inflate": these are not defined.**
   Is it an error if the DEFLATE stream (a) ends before its final block,
   (b) reaches the final block with bytes of the compressed size left over,
   or (c) for the format entries, has no uncompressed size to check against?
   Common libraries differ: Go's `compress/flate` and Python's
   `zlib.decompressobj` silently ignore trailing data, and some streaming
   decoders return a short result on truncated input with no error.
   *Choice:* strict. The stream must reach its final block and use exactly
   `csize` bytes. Truncation, trailing bytes and corrupt data are all errors:
   archive errors for format entries, body errors otherwise. The spec should
   say which of (a) and (b) are errors.

2. **§8.2 `list`: the error class is unspecified.** "`list` … fails if one of
   [the pages] cannot be parsed" does not say which class. HARNESS.md
   requires one. *Choice:* `entry`, because §8.4 lists "the page that holds
   the key's record cannot be parsed" as an entry error.

3. **§3.2 zip64 use: take the whole zip64 EOCD, or only the all-ones
   fields?** The spec says a reader "MUST use the zip64 end of central
   directory record" when a locator is present. APPNOTE's convention is to
   consult the zip64 value only for fields that are all ones. These differ
   when the two records disagree (for example, a hand-made test archive).
   *Choice:* when a locator is present, take entry count, CD size and CD
   offset from the zip64 record, whatever the 32-bit fields hold.
   Also unspecified:
   - whether the zip64 EOCD's size field (44) is checked;
   - whether the zip64 EOCD must lie before the locator, or only "within the
     file" (I check that its 56 fixed bytes are within the file);
   - whether the locator's "total number of disks" is checked (I don't).

4. **§3.2 "A field of all ones without a locator is an archive error": which
   fields?** The EOCD has disk-number fields, which readers MUST ignore, plus
   "entries on this disk", "total entries", "CD size" and "CD offset".
   *Choice:* the last four. Disk-number fields are ignored even when they are
   0xFFFF.

5. **§3.2 / §4.1 / §8.4: a CD record whose local header offset is
   0xFFFFFFFF but which has no ZIP64 extra field (or one that is too
   short).** §8.4's list of entry-error causes doesn't include this. §8.1
   says "a problem confined to a single record's contents is not an archive
   error … they are entry errors", but §8.6 also says §3.2 violations are
   unspecified. *Choice:* entry error. For the reader I parse the 0x0001
   block per APPNOTE: uncompressed size, then compressed size, then offset,
   each present only if the 32-bit field is all ones. The writer emits only
   the offset, as §3.2 requires.

6. **§8.4 / §10: resource-limit request errors conflict with the check
   order.** "Each operation checks in this order: 1. the request …" but a
   request can only be known to exceed a size limit after the lookup (bytes
   entry: its size; reference: after decoding the payload). It is also
   unclear whether a 10-byte range of a 2 GiB DEFLATE entry "exceeds a
   resource limit", since the whole body must be inflated.
   *Choice:* limit 1 GiB (`MAX_REQUEST_BYTES`). It applies to the returned
   window for STORED entries and references, and to the whole body for
   DEFLATE entries. It is checked at the step where the size becomes known,
   and reported as `request`.
   Format entries: §8.1 says a reader "MUST NOT report an error at open that
   this section does not list", yet a bounded reader has to refuse a
   decompression bomb in `__vz__/sources`. I bound it at 1 GiB and report an
   archive error. The spec should allow this.

7. **§6 etag: "not a quoted string" vs RFC 9110 `entity-tag`.** "A value
   that is not a quoted string, or is a weak tag" admits two readings. One
   is "starts and ends with `"`". The other is RFC 9110 `opaque-tag`, where
   `etagc` excludes space, `"`, DEL and control characters but allows
   obs-text (0x80–0xFF). The empty tag `""` is valid under both. *Choice:*
   the RFC 9110 grammar exactly. Non-ASCII UTF-8 bytes are accepted as
   obs-text and `""` is accepted. `"a b"` and `"a"b"` are rejected, both by
   the writer and as an archive error.

8. **§8.4 STORED entries whose compressed and uncompressed sizes differ.**
   Is that a body error ("inflates to a size other than its record's"), or a
   §3.1 rule 6 violation with unspecified results? *Choice:* body error. The
   body is treated as `csize` bytes, which "inflate" to themselves.

9. **§7.2 / §8.4: what does "the page cannot be parsed" cover?** If a page's
   first record is fine and a later one is corrupt, does looking up the first
   key succeed? *Choice:* a page parses only if it is a sequence of records
   with correct signatures that fills `length` exactly. Any failure makes
   every lookup that selects that page an entry error, and makes `list`
   fail.

10. **§6 `file:` URIs with no authority.** "Its authority MUST be empty or
    `localhost`" doesn't say whether an absent authority (`file:/x`, valid
    per RFC 8089) counts as empty. *Choice:* accepted. Not decoded: a
    percent-encoded host (`loc%61lhost`) is rejected. Rejected: userinfo or
    a port (`localhost:80`).

11. **§6 base URI normalisation.** "Make the path absolute and normalise it.
    `.` and `..` are removed lexically" leaves several questions open. Are
    repeated slashes collapsed? Is a trailing slash kept? Does `..` above the
    root stay at the root? *Choice:* Rust `Path::components()` semantics.
    Repeated slashes are collapsed, a trailing slash is dropped, `/..` is
    `/`, and `.` is dropped. This only affects archives with relative URLs
    opened through odd paths. On macOS, `getcwd` returns `/private/tmp/...`
    for `/tmp/...`. The spec's choice of `getcwd` over `$PWD` makes that
    deterministic, which is good.

## B. Underspecified, but my choice probably matches others

12. **§3.4 / §8.1: the EOCD entry counts are never used.** With a page index
    the reader never reads the whole CD, so it cannot check them. Without
    one, it is not stated whether a count that doesn't match the parsed
    records is an archive error. The "MUST NOT report an error … not
    listed" rule implies it is not. *Choice:* counts are ignored.

13. **§8.1 "the central directory lies within the file".** Must it end at or
    before the zip64 EOCD or EOCD, or only before EOF? *Choice:* only
    `cd_offset + cd_size <= file_size`.

14. **§3.1 rule 4 / §9.2: "version needed" in local headers of entries that
    need ZIP64 in their CD record.** The local header has no ZIP64 field
    (rule 4), so by §9.2's wording ("records with a ZIP64 extra field") it
    gets 20, while its CD record gets 45. *Choice:* 20 in the local header
    and 45 in the CD record. For the zip64 EOCD, §3.2 fixes "version needed"
    at 45 but not "version made by". §9.2 recommends "version made by" 20,
    which gives "made by 2.0, needs 4.5". *Choice:* made by 20, needed 45.

15. **§9.2: the DOS date for 1980-01-01 00:00.** Give the raw values: date
    `0x0021`, time `0x0000`. These are what the writer emits.

16. **§6.1 `modified_not_after`, "truncated to whole seconds", for times
    before the epoch.** Truncate toward zero or floor? *Choice:* floor, so
    "≤ pin" is never satisfied early. HARNESS.md limits pins to
    non-negative values anyway.

17. **§6 / §8.1: a source `url` that is not a valid URI reference is not an
    open-time error.** Only "empty" is checked at open. A syntax error is a
    resolution error when a range is read, and a writer must reject it.
    This is consistent, but surprising next to `etag`, which is validated
    at open. *Choice:* as specified.

18. **§6.1: etag pins on `file:` sources are always resolution errors.** A
    writer may legitimately put an etag pin on a relative URL. Opened over
    HTTP the archive works, but opened from disk every range of that source
    fails. This is consistent with "fail closed", but worth a sentence (or a
    SHOULD NOT) for writers.

19. **§6 `file:` resolution of non-regular files.** If the URL names a
    directory, is that "cannot be read"? *Choice:* a resolution error for
    anything that is not a regular file.

20. **§8.2 `raw` of the format entries in a paged archive.** Their records
    are not in any page, so `raw` takes the body from the comment's
    offset/size and inflates it. No uncompressed size is available to
    verify against.

21. **§8.3 `key` sources with DEFLATE bodies.** The whole entry must be
    inflated and its size checked before any byte is returned, because
    body errors make the source fail. The spec implies this but doesn't
    say it.

22. **§3.3 / §3.1 rule 5: a record with bit 11 clear.** The key is still
    decoded as UTF-8, not CP437. That follows from §3.3 and §8.6, but it is
    worth saying explicitly, since many ZIP libraries switch encodings on
    this bit.

23. **§7.2 `first_key = ""`.** Keys are non-empty, but an empty `first_key`
    is not listed as malformed. *Choice:* accepted. It has no effect on
    lookup.

24. **§4.3: the sentence "Extra field blocks may appear in any order"** is
    in the middle of the method paragraph. It probably belongs in §4.1.

25. **§8.2 `list` with a page index: when must a page be read?** The
    requirement is clear, but the page-selection predicate is left to the
    reader. Page `i` covers `[first_key_i, first_key_{i+1})`, and it can
    hold a key with prefix `p` iff `first_key_{i+1} > p` (or it is the last
    page) and (`first_key_i < p` or `first_key_i` starts with `p`). An
    informative formula would help.

## C. Unnatural or harder than expected to implement

26. **Protobuf presence tracking (§5.1, §5.2, §6).** `optional` fields
    (`Range.data`, the pins) carry meaning through presence: an empty
    literal versus a source range, and a pin of 0 versus no pin. Hand-written
    and some generated decoders must track presence explicitly. The
    canonical-encoding rule ("a set optional field is always emitted, even
    when empty") is clear.

27. **DEFLATE streaming with `flate2`.** Calling `Decompress` repeatedly with
    `FlushDecompress::Finish` and a growing buffer fails with a "deflate
    decompression error" on larger inputs. `FlushDecompress::None` plus an
    explicit check for "no progress" works. This is a library pitfall, not a
    spec issue. It mattered because the spec makes a format entry's inflate
    failure fatal.

28. **The archive comment is binary.** `unzip -t`/`zipinfo` print it as
    garbage (`vzip/1\x..`). This is harmless, but a ZIP tool user will see
    it.

29. **ZIP64 testing.** Body offsets of 4 GiB or more cannot be exercised
    without multi-GiB files. Only the entry-count trigger (≥ 65535 entries)
    was tested end to end. The record-level ZIP64 extra field is covered by
    unit tests on both the writer and the reader side.

## D. HARNESS.md

30. **`compress: false` on a reference entry.** Only `compress: true` is
    called invalid. *Choice:* `false` is accepted.
31. **`null` for members other than `page_size`** (for example
    `"mirror": null`, `"sources": null`). *Choice:* invalid (type error).
    Only `page_size` accepts null.
32. **Malformed queries** (a `range` with `start` but no `end`, an unknown
    `op`). Not covered. *Choice:* the queries file is treated as invalid, and
    the CLI exits non-zero without output. That fails every query, not just
    the bad one.
33. **Order of entries in the written archive.** Not covered. The writer
    stores non-pinned entries in description order, then
    `__vz__/sources`, then pinned entries, then `__vz__/index`. CD records
    are always sorted by key (body records first, then sources, then index),
    with or without a page index.
34. **Page grouping.** "About `page_size` bytes" is implemented as: start a
    new page when adding the next record would push the current page over
    `page_size`. Every page holds at least one record.
35. **HTTP** is not implemented. `http:` and `https:` URLs give resolution
    errors ("unsupported scheme"). The spec says readers SHOULD support
    them.
36. **Request size limit.** It is not exposed through the harness. A request
    over 1 GiB reports `request`.
