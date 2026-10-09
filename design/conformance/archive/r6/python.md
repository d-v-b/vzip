# SPEC_NOTES: vzip v0 rev 6, Python implementation

This file lists every place where the spec (SPEC.md) or the harness (HARNESS.md)
was ambiguous, underspecified, surprising or hard to implement. Each note gives
the section, the question, what this implementation chose, and why. Items marked
**[interop]** are the ones most likely to make two conforming implementations
give different results.

## A. ZIP container (§3)

1. **§3.4 / §8.1: what "the central directory lies within the file" means.** **[interop]**
   The CD could be required to end before the zip64 end records or the EOCD,
   or only to end before EOF. *Chosen:* `cd_offset + cd_size <= file_size`, so
   overlap with the end records is not checked. *Why:* the text says "within
   the file" and §8.1 forbids extra open errors. A CD that overlaps the EOCD
   would normally fail record parsing anyway. A crafted archive where the
   overlap still parses would get different results from a stricter reader.

2. **§3.2: where the zip64 EOCD record may be, and which locator fields are checked.**
   The spec requires the locator to sit immediately before the EOCD, and the
   record to lie within the file, carry the right signature and have size field
   44. It does not say:
   - whether the record must sit immediately before the locator;
   - whether the locator's "total number of disks" (which the writer must set
     to 1) is checked.

   *Chosen:* neither is checked. The 56-byte record must fit in the file.

3. **§3.2: ZIP64 extra block when the size fields are also all ones.** "Too
   short for the values it must hold" implies that the 64-bit sizes come first,
   in APPNOTE order, when `usize`/`csize` are 0xFFFFFFFF. Such records violate
   rule 7, though, and §8.6 makes them unspecified. *Chosen:* when the offset is
   all ones, the block must hold 8 bytes for each all-ones field among
   (usize, csize, offset), and those 64-bit values are used. When only a size is
   all ones (and the offset is not), the block is not required and the 32-bit
   value is used as it is. It would help if the spec either said
   "only the offset is ever read from the block" or defined this case.

4. **§3.4: magic `vzip/` followed by a non-digit (`vzip/x`).** Is that "not a
   vzip archive" or "unsupported version"? Both are archive errors, so it only
   affects the message. *Chosen:* "unsupported version" for anything other than
   `0` after `vzip/`.

5. **§3.1 rule 2 / §8.6: duplicate names.** Results are unspecified, but a
   reader has to pick something. *Chosen:* the first record in CD order (or in
   page order) wins. **[interop]** A conformance suite should not test this,
   because implementations will differ (many ZIP libraries use the last).

6. **§3.3: a record with an empty or non-UTF-8 name.** The spec says it is
   ignored. *Chosen:* it still has to parse structurally, both for §8.1 CD
   parsing and for page parsing, and is then dropped. Python's strict UTF-8
   codec rejects surrogates and overlong forms, which matches "valid UTF-8".

## B. Entries and messages (§4, §5)

7. **§5.2: literal range with an explicitly encoded zero `source`/`offset`/`length`.**
   "MUST all be 0" is read as "have the value 0", so `08 00` (source = 0 encoded
   explicitly) next to `data` is accepted. A reader that tests field presence
   would call it malformed. It would help if the spec said "value 0".

8. **§5.1: encoding of an all-default `Range` inside a `Concat`.** The rules
   produce `0a 00` (an empty embedded message). That is consistent, but worth
   stating, because "do not emit default values" could be misread as dropping
   the element.

9. **§4.3 / §9.1: 0x7A77 payload with exactly one part.** Writers MUST NOT
   produce it, and readers accept it. This is not in the §8.6 list of
   undetected violations, but readers clearly must not reject it (§4.3: "any
   number of parts"). It could be added to §8.6 for completeness.

## C. Sources and URLs (§6)

10. **§6: `key` source with an empty key (`key: ""`).** An empty `url` is an
    archive error at open, but nothing is said about an empty `key`. *Chosen:*
    it is accepted at open and gives a resolution error when used (no key is
    empty, so it is "missing"). The writer rejects it as "absent". This is
    asymmetric with `url`; consider making it an archive error too.

11. **§6: base URI when the path is not valid UTF-8.** "Form `file://` followed
    by the path's UTF-8 bytes" assumes the path can be represented in UTF-8.
    *Chosen:* the raw OS path bytes (`os.fsencode`) are percent-encoded, so an
    undecodable path still gives a deterministic URI.

12. **§6: lexical `..` versus symlinks.** Normalising `..` lexically while not
    resolving symlinks means `../x` relative to an archive in a symlinked
    directory can name a different file than the OS would. This follows the spec;
    it is noted because it is surprising. On macOS, `getcwd()` returns
    `/private/tmp/...` where `$PWD` says `/tmp/...`. The spec handles this
    explicitly, which is good.

13. **§6: URI-reference validation is not available in the standard library.**
    `urllib.parse` is lenient: it accepts spaces, non-ASCII and bad `%`
    escapes. A strict RFC 3986 validator had to be written by hand, including
    IP-literal/IPv6/IPvFuture hosts. *Choices:* IPv6 is validated with
    `ipaddress.IPv6Address`, and zone IDs (`%25`) are rejected because
    RFC 3986 has no zone IDs. Reasonable implementations will differ on edge
    cases such as `[v1.x]` or IPv4-embedded IPv6. **[interop]** for writer
    rejection tests that use exotic hosts.

14. **§6 `file:` mapping, authority `localhost:port` or with userinfo.** Only
    absent, empty or `localhost` are allowed, so `localhost:80` and
    `u@localhost` are rejected. That is unambiguous but strict, and worth
    stating explicitly.

15. **§6 `file:` mapping, query on the base URI.** Base URIs built from paths
    never have queries. A relative reference `?q` resolves to the archive's own
    path with a query, which is then rejected ("no query component").
    `#frag` resolves to the archive itself. Both are consistent with the spec.

16. **§6: `http:` URLs with userinfo or percent-encoded hosts.** Not specified.
    *Chosen:* userinfo is ignored, so no credentials are sent, and a
    percent-encoded reg-name host is decoded before connecting.

17. **§6.1: pin checks per open versus per request.** *Chosen:* every read
    re-checks the pins (stat for `file:`, headers for HTTP). That is allowed;
    the SHOULD only covers caching.

## D. HTTP (§6.2)

18. **206 without a `Content-Range` header** (for example a
    `multipart/byteranges` reply, or a broken server). Not listed in the
    response table. *Chosen:* resolution error. **[interop]**

19. **206 whose `Content-Range` is internally inconsistent**, such as
    `total <= last` or a body length that differs from `last - first + 1`.
    *Chosen:* resolution error. The table only covers "the returned range is not
    the one requested".

20. **Requested range past the end of the object.** Servers usually answer a
    request for `[a, b)` with `b > size` with a 206 for `a-(size-1)`. The spec
    makes that a resolution error ("not the one requested"), which matches
    "source value shorter than offset + j". The two rules agree, but it might
    be worth saying so explicitly.

21. **`Content-Encoding` parsing.** "A `Content-Encoding` other than `identity`"
    does not say how to treat a list (`identity, identity`), an empty value, or
    case. *Chosen:* comma-split, case-insensitive; every token must be `identity`
    or empty. Note that `identity` is not a registered content-coding in
    RFC 9110, so servers should not send it at all.

22. **Last-Modified: "valid HTTP-date".** **[interop]** RFC 9110 requires
    recipients to accept IMF-fixdate, RFC 850 and asctime formats.
    *Chosen:* all three are accepted. Open issues:
    - The RFC 850 two-digit year needs a "50 years in the future" rule, which
      makes the result depend on the current date. This implementation uses
      the RFC 9110 rule.
    - The day name is not checked against the date.
    - Impossible dates (Feb 31, second = 60) are "unparseable".

    The spec should either restrict to IMF-fixdate or name these rules.

23. **Redirects:**
    - An invalid `Location` (spaces, non-ASCII, which are common in practice),
      or no `Location` at all, gives a resolution error under this
      implementation's choice.
    - The fragment of the `Location` is ignored.
    - https→http downgrades are followed, because the spec allows any
      http(s) target. That may deserve a security note.
    - 300/304/305 count as "any other status", so they are errors.

24. **§6.2: is the Content-Encoding / pin check applied to redirect responses?**
    *Chosen:* no, only the final 200/206 response is checked ("Pins and the
    size apply to the final response").

25. **ETag comparison.** The header value is trimmed of whitespace and compared
    byte for byte with the pin. A weak response ETag `W/"v1"` never equals a
    strong pin. Multiple `ETag` headers: the first is used (unspecified).

## E. Page index (§7)

26. **§7.2/§8.2: `list(prefix)` page selection.** "Every page whose range includes
    at least one key starting with prefix" needs a small algebra. Page `[lo, hi)`
    intersects the set of strings with prefix `P` iff
    `(lo <= P and P < hi) or lo.startswith(P)`. This was the most error-prone
    piece to get right. **[interop]** It matters because an unparseable page
    makes `list` fail with an entry error only if that page is selected. A
    worked example in the spec would help (for example, prefix `b` with a page
    `["a", "b")` is not read, but a page `["a", "b\x00")` is).

27. **§7.2: a pinned key that also has a record in a page.** Lookup finds the
    pinned entry first, so the page record is shadowed. For `list`, the key
    appears once. Not stated explicitly, but it follows from the lookup order.

28. **§7.1 writer page grouping.** "About `page_size` bytes": *chosen* to start a
    new page when adding the next record would exceed `page_size`, so a record
    larger than `page_size` gets its own page. `page_size = 1` therefore gives
    one record per page.

29. **§7.2 check "a page lies outside the central directory".** Checked against
    `cd_size`. Whether the last page ends exactly where the format records
    begin is not checked (§8.6).

## F. Reading operations and errors (§8)

30. **§8.2 `raw` for format entries: located through the comment or the CD
    record?** **[interop]** §3.4 says readers MUST read the two format entries
    through the comment, and §7.2 says (for paged archives) "the format entries
    are known from the comment". For `raw` in an unpaged archive the text is
    silent. *Chosen:* `raw("__vz__/sources")` and `raw("__vz__/index")` always
    return the body located by the comment, which was already inflated and
    checked at open. A reader that uses the CD record instead can give different
    bytes or a different error (for example an entry error) on a tampered
    archive. The spec should say which.

31. **§8.2: negative values in `range`/`offset`/`suffix`.** The spec has
    `start <= end` but no sign constraint. HARNESS says only
    `modified_not_after` may be negative, so this probably never arises.
    *Chosen:* request error.

32. **§8.4: order of the entry-error checks within one record** (extra field,
    method, bit 0, reference + method 8, ZIP64). Irrelevant to the class, but
    messages differ. No issue.

33. **§8.4: `key` source naming a STORED entry whose sizes differ, or whose body
    is outside the file.** The spec says this is a resolution error (from the
    body error). The sizes check happens before the "shorter than offset + j"
    check. Both are resolution errors, so this has no visible effect.

34. **§8.4 resource limits.** This implementation documents these limits:
    - a request whose window is over 1 GiB is a request error;
    - inflating a DEFLATE bytes entry whose declared size is over 1 GiB is a
      request error;
    - format entries that inflate to more than 256 MiB make opening fail with
      an archive error.

    The spec says only "request error", but a limit hit while opening cannot be
    a request error. It may be worth saying that a resource limit at open is an
    archive error.

35. **§8.1: a DEFLATE body that produces more output than `usize`.** Inflation
    is capped at `usize + 1` bytes and anything larger is reported as a body
    error, which avoids unbounded memory. This gives the same result as
    "inflates to the record's uncompressed size", but implementations that
    inflate fully first may run into a resource limit instead.

36. **§8.3: should a zero-length window on a reference still decode the
    payload?** Yes (step 1 "whatever the request"). Done; noted because it is
    easy to miss.

## G. Writing (§9)

37. **§9.1 does not reject `url` values that are valid URI-references but can
    never resolve** (for example `file://host/x`, or `?q`). The writer accepts
    them, as the spec requires. Noted because it is surprising: a writer could
    easily catch these.

38. **§3.2: "version made by" for the zip64 EOCD record.** Not specified (only
    "version needed 45"). *Chosen:* 45.

39. **§9.1: `__vz__/index` must follow every entry it indexes or pins.** This
    forces a two-phase layout: write the bodies, collect the CD records, compute
    the pages, then write the index. That is easy once noticed. The recommended
    layout (§9.2) puts pinned entries after `__vz__/sources`, which this writer
    follows.

40. **§9.1: limit on the number of pinned entries or the index size.** None is
    given. The index is an ordinary DEFLATE bytes entry, so it is bounded only
    by the 4 GiB rule.

## H. HARNESS.md

41. **Query validation versus archive open.** If the queries file is invalid
    *and* the archive cannot be opened, which wins? *Chosen:* the queries are
    validated first, giving a non-zero exit.

42. **`get_raw` with a `range` member.** The harness says "(no `range`)" but not
    whether its presence makes the file invalid. *Chosen:* invalid (exit
    non-zero). Unknown extra members in a `range` object are ignored.

43. **Keys in queries that cannot be UTF-8 encoded** (JSON `"\ud800"`). These
    are treated as never present (missing / matching nothing). The harness does
    not say.

44. **Output JSON for non-ASCII keys.** Printed with `\u` escapes
    (`ensure_ascii`). The JSON is valid either way, but a byte-comparing runner
    would see a difference. Presumably the runner parses the JSON.

45. **Description: duplicate JSON object members.** Not specified. Python's
    `json` keeps the last one.

46. **Description: `{"data": ...}` source with pins.** Invalid per spec §9.1;
    rejected.

47. **Description: what makes a range a "mix".** A range object with `data` and
    any of `source`/`offset`/`length` is rejected, even when the value is 0.

48. **Description: numbers must be integers.** `1.0`, `NaN` and `Infinity` are
    rejected (via `parse_float`/`parse_constant`). `true` is not accepted where
    an integer is expected (Python `bool` is a subclass of `int`, so this needed
    an explicit check).

49. **Write atomicity.** "Do not create a file at `<out-path>`" on invalid input:
    the description is validated fully before anything is written. The archive
    is then written to a temporary file in the same directory and renamed, so
    an I/O error part-way never leaves a partial file at `<out-path>`.
