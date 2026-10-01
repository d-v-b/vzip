# Notes on the vzip specification (format version 0, revision 5)

These notes come from a TypeScript implementation (`src/`) written from SPEC.md and
HARNESS.md alone. Each item gives the section, the question, and the choice made.
They are roughly ordered by how much they could affect interoperability. Items marked
**[interop]** are places where two conforming readers or writers could give different
results on the same input.

## A. Places where conforming implementations can disagree

1. **§6.2 Redirects are optional ("MAY follow up to 5").** [interop]
   A reader that follows a 302 returns bytes, and one that does not returns a
   resolution error (a 3xx is "any other status"). Both conform, so the result of `get`
   is not determined by the archive and the server. Related details the spec leaves out:
   - whether `Range`, `If-Match` and `If-Unmodified-Since` are re-sent on the redirected
     request (they have to be, or the pins aren't checked at the final response);
   - how a relative `Location` is resolved (I resolve it against the current request URL
     with RFC 3986 §5.2.2);
   - whether a redirect may change the scheme (http to https or back), or go to a
     non-HTTP scheme;
   - which statuses count as redirects (I use 301, 302, 303, 307 and 308, and only when a
     `Location` header is present);
   - whether 6 redirects is an error. I treat it as a resolution error.

   *Choice:* follow up to 5 redirects among http/https, re-sending every header.
   *Suggestion:* make redirect handling a MUST, or a MUST NOT.

2. **§6.2 Inconsistent 206 responses aren't covered.** [interop] The table covers
   "returned range is not the one requested", but not:
   - a 206 whose body length differs from `z − a + 1`;
   - a 206 whose `total` is smaller than `z + 1`;
   - a `multipart/byteranges` 206, which has no top-level `Content-Range`;
   - a 206 with no `Content-Range` at all;
   - `Content-Range` syntax variants (case, extra whitespace).

   *Choice:* every one of these is a resolution error. I parse `Content-Range` with
   `bytes <a>-<z>/<total|*>`, case-insensitively, and allow surrounding whitespace.

3. **§6.1/§6.2 Pins over a `200` response can fail open.** If the server ignores
   `If-Match` or `If-Unmodified-Since` and returns 200 (or 206), the pin is treated as
   passed. The spec says nothing about checking the response's own `ETag` or
   `Last-Modified` headers. This contradicts the §6.1 statement that pins "fail closed".
   *Choice:* the reader does not inspect response `ETag` or `Last-Modified` headers.
   This matches the table: only a 412 fails these pins. *Suggestion:* say this
   explicitly, or require the reader to compare the response's `ETag` when it is
   present.

4. **§6.2 `Content-Encoding` values.** The spec says "a `Content-Encoding` other than
   `identity`". It doesn't say whether the header may be absent (I allow that), whether
   the comparison is case-insensitive (mine is), or how a list such as
   `identity, identity` is handled (I reject it). It also doesn't say whether the rule
   applies to 412 and 416 responses. That one doesn't matter, since both are errors
   either way.

5. **§10 / §8.4 Interaction between resource limits and error classes.** [interop, but
   declared untested] A reader with a memory bound may report a request error where
   another reader reports a resolution error, for example a whole `get` of a 2^40-byte
   range from a 3-byte `data` source. *Choice:* the limit is 1 GiB per request and per
   inflated body. It is checked per piece, after that piece's own resolution checks, so
   a short source still gives a resolution error. Limits hit while opening, such as a
   source table that inflates past 1 GiB or an unpaged central directory over 1 GiB, are
   reported as archive errors. §8.1 says a reader "MUST NOT report an error at open that
   this section does not list", so this technically breaks that rule. The spec should
   allow resource limits at open.

6. **§8.1 "the central directory (offset and size) lies within the file".** It isn't
   clear whether "within the file" means `≤ file_size`, or before the end-of-central-
   directory record (or the zip64 records). *Choice:* `cd_offset + cd_size ≤ file_size`.
   An overlap with the EOCD then usually shows up as a parse error in unpaged archives,
   and isn't detected at all in paged ones. The same question applies to the zip64 end
   record. I only check that its 56 bytes lie in the file, not that it comes before the
   locator.

7. **§3.2 ZIP64 extra with sizes set to all ones.** The entry-error rules only cover a
   record whose *offset* is `0xFFFFFFFF`. If the compressed or uncompressed size is
   `0xFFFFFFFF` (a §3.1 rule 7 violation, so unspecified per §8.6), I still read the
   sizes from the `0x0001` block in APPNOTE order when exactly one block is present and
   long enough. Otherwise I use the 32-bit values, which normally gives a body error.
   Two `0x0001` blocks are an entry error only when the offset is all ones, as the text
   says. It is odd that a record with a non-all-ones offset may have duplicate ZIP64
   blocks.

8. **§8.2 `raw` of format entries.** `raw("__vz__/sources")` and `raw("__vz__/index")`
   are visible, but the spec doesn't say whether the body comes from the comment's
   offset and size or from the central directory record. In a paged archive the format
   records are never parsed, so the comment is the only practical choice. *Choice:* always
   read through the comment, inflated. Its uncompressed size is not checked, because
   §8.1 says it isn't. A reader that goes through the CD record could report a body
   error that mine doesn't.

## B. Underspecified, with a natural choice

9. **§3.4 Magic with a non-digit version (`vzip/x`).** It isn't clear whether this is
   "not a vzip archive" or "unsupported version". Both are archive errors, so it makes
   no observable difference. Only the message changes.

10. **§3.4 / §8.1 Format entries' central directory records.** In an unpaged archive, it
    isn't clear whether the CD must contain an `__vz__/sources` record at all, since
    readers read it through the comment. *Choice:* not checked (§8.6 seems to allow
    this).

11. **§6 Base URI normalisation at the root.** The spec says "`..` segments are removed
    lexically together with the segment before them", but not what happens to `..` at
    `/`. *Choice:* Node's `path.resolve` behaviour, so `/..` becomes `/`, which matches
    POSIX.

12. **§6 `file:` authority.** "MUST be absent, empty or `localhost`". It isn't clear
    whether `localhost:` (empty port), `localhost:80` or `user@localhost` are allowed.
    *Choice:* only the exact strings `""` and `localhost` (case-insensitive) are allowed.

13. **§6 Percent-decoded `file:` paths that aren't UTF-8.** They are allowed and passed
    to the OS as raw bytes. The spec only forbids `%2F` and NUL.

14. **§6 Checking order for a `file:` source.** For example, an etag pin on a missing
    file, or a size pin that fails on a file that is also too short. All of these are
    resolution errors, so the order is unobservable. Mine is: syntax, then scheme, then
    `file:` mapping, then the etag pin, then open, then size, then mtime, then length.

15. **§6.1 File mtime with sub-second precision.** I use `floor(mtime_ns / 1e9)`,
    rounding towards negative infinity, using the nanosecond `stat` value. The spec is
    clear here. I mention it because a float `mtimeMs` would round wrongly near
    boundaries.

16. **§6.2 Request timeouts.** None are specified. I use a 30 s timeout, after which the
    request is a resolution error.

17. **§5.1 Canonical encoding.**
    - It isn't stated that elements of a repeated message field are always emitted, even
      when the element encodes to zero bytes. For example, an all-default source range
      inside a `Concat` is `0a 00`. This follows from protobuf semantics, but the rule
      "not emit a scalar field equal to its default value" could be misread as applying
      here.
    - The encoding of negative `int64` (`modified_not_after`) isn't stated. I use the
      standard protobuf form, a 10-byte two's-complement varint.

    Both matter because the spec says encodings can be "compared byte for byte".

18. **§5.2 Literal ranges with explicit zero fields.** A literal range that encodes
    `source = 0` explicitly is not malformed. Mine accepts it, since only the decoded
    value is checked. It's worth stating.

19. **§7.1 Are pinned entries also in pages?** The definition of body records ("every
    entry except the two format entries") says yes, and I include them. A note in §7.1
    item 3 would help, because "pinned" suggests "taken out of the directory".

20. **§7.1 Page grouping.** HARNESS says a page SHOULD hold "about `page_size` bytes".
    *Choice:* records are added to the current page until its length is at least
    `page_size`, then a new page starts. Every page except the last therefore holds
    `page_size` bytes or a little more.

21. **§9.1 "`__vz__/index` MUST follow every entry whose record it indexes or pins".**
    Page offsets are relative to the start of the CD, so only *pinned* entries actually
    depend on the index's size. The rule as written forces the index after *every* body
    entry. I follow it. My layout is: entries, `__vz__/sources`, pinned entries,
    `__vz__/index`, CD. The rationale given in the text only covers pins.

22. **§9.2 "version needed to extract" in local headers.** Local headers never have a
    ZIP64 extra (rule 4). The spec says 45 is for "records with a ZIP64 extra field" but
    doesn't say what the *local header* of such an entry uses. *Choice:* 45 when the
    entry's offset needs ZIP64, otherwise 20. Readers ignore it anyway.

23. **§3.2 Zip64 end record "version made by".** It isn't specified. I write 45, the
    same as "version needed to extract".

24. **§8.2 Operations on keys that can't exist.** This covers the empty key, and query
    strings that aren't valid Unicode, such as a JSON `\ud800`. *Choice:* `missing` for
    classify, get and raw. `list` with such a prefix uses its UTF-8 encoding, with
    replacement characters.

25. **§8.2 `list` page selection.** "Reads every page whose range includes at least one
    key starting with `prefix`." I compute this as the intersection of
    `[first_key_i, first_key_{i+1})` with `[prefix, succ(prefix))` in byte order, where
    `succ` increments the last byte that isn't 0xFF. The lower end of a non-empty
    intersection is always a valid key with the prefix, so byte-level and string-level
    reasoning agree. Implementers still have to work this out. A sentence or a worked
    example would help.

26. **§8.1 "inflates cleanly".** Node's `zlib.inflateRawSync` silently ignores trailing
    bytes. The spec warns about this, which helped. I detect trailing bytes with
    `{info: true}` and `engine.bytesWritten`. Truncated streams and streams with no
    final block throw "unexpected end of file".

27. **§1.3** links `conformance/REVISIONS.md`, which wasn't provided. That's harmless.

28. **§6 / HARNESS Opening an archive from a URL.** The spec defines the base URI for
    this case, but the harness never does it. My library only opens local paths.

## C. Things that were surprising or hard to implement

29. **§3.4 EOCD discovery** depends on the comment length being 22 or 38. The trick that
    "a record found at −60 takes its comment length from the real record's disk-number
    field" is clever, but needed a careful test (`test/reader-errors.test.ts`, "no
    fallback from 60 to 44").

30. **§3.3 and §5.1 BOM handling.** `TextDecoder` strips a leading BOM by default.
    Without `ignoreBOM: true`, keys and strings starting with U+FEFF would silently
    change. The spec's warning was useful.

31. **§3.3 / §8.2 Sort order.** JavaScript's default string sort uses UTF-16 order. All
    sorting and comparison is done on UTF-8 `Buffer`s with `Buffer.compare`. The 😀 / ～
    example in the spec is in the round-trip tests.

32. **§5.1 uint64.** JavaScript numbers can't hold uint64, so every protobuf integer and
    every reference size and offset is a `bigint`. This is natural, but it is a source of
    bugs that the spec can't prevent.

33. **§6 RFC 3986 `URI-reference` validation.** This needs a full grammar, including
    IPv6 literals and the `path-noscheme` rule (no `:` in the first segment of a
    scheme-less reference). WHATWG `URL` parsing, which most JS code would reach for, is
    *not* RFC 3986 and gives the wrong answer for many inputs. One example is
    `new URL("a b", base)`, which succeeds. A short list of valid and invalid examples in
    the spec, or in conformance tests, would help.

34. **JSON integers.** HARNESS says "`1.0` is invalid". `JSON.parse` can't tell `1` from
    `1.0`, so I wrote a small JSON parser that keeps number text. I also treat exponent
    forms such as `1e2` as non-integers. HARNESS doesn't say how they should be treated.

## D. HARNESS.md

35. **Negative numbers in queries.** "Only `modified_not_after` may be negative." It
    isn't clear whether a negative `start`, `end`, `offset` or `suffix` is an invalid
    queries file (non-zero exit) or a request error. *Choice:* invalid queries file.

36. **`get_raw` with a `range`, or `list` with a `key`.** It isn't clear whether these
    are invalid. *Choice:* a `range` on any op other than `get` is an invalid query.
    Extra unknown members are ignored.

37. **`{"start": S}` without `end`, or `{"end": E}` alone.** I treat both as invalid
    queries files. The text only names "a `range` with several forms".

38. **Exponent-form numbers** (`1e0`) in descriptions and queries are not mentioned. I
    reject them as non-integers.

39. **Lone surrogates in JSON strings** (`"\ud800"`). A key or URL containing one can't
    be encoded as UTF-8. The writer rejects it under the "key not valid UTF-8" rule of
    §9.1. That seems to be the only way that rule can be triggered from JSON.

40. **`size` pin out of range.** A negative `size` is rejected as invalid, since the
    field is `uint64`. It isn't stated.

41. **Unknown members with `null`** are ignored, and so are unknown members of nested
    objects (sources, entries, ranges). The text says this only at the top level ("Unknown
    members are ignored"), and I applied it at every level.

42. **Error message for unreadable archive files.** It is clear that this is an archive
    error. It isn't clear whether a *directory* path counts too. I treat it as one ("not
    a regular file").

43. **`write` with an `<out-path>` in a missing directory.** The CLI exits with status 2,
    and the message is printed to stderr. This isn't covered by the harness text.
