# SPEC_NOTES: vzip format version 0, revision 7 (Python implementation)

These notes cover every place where the spec (SPEC.md) or the harness (HARNESS.md) was
ambiguous, underspecified, surprising, or awkward to implement. Each item gives the
section, the question, what this implementation chose, and why. Items are ranked
roughly by how much they could affect interoperability. The highest-impact items come first.

Layout: `vzip_impl/` (pb.py protobuf, uri.py RFC 3986, httpfetch.py, reader.py,
writer.py, cli.py), `./vzip` launcher, `tests/` (run with
`uv run --no-project --python 3.12 python -m unittest discover -s tests -t .`).

Documented resource limit (§10): a single request may use at most **1 GiB**. That
covers a get window, an inflated DEFLATE body, and an HTTP response body. Beyond it the
reader reports a `request` error. For an HTTP 200 the limit applies to the whole body. For
format entries it becomes an archive error at open, because every error at open is one.
The limit is checked only when the reader is about to allocate. The cheap
"source too short" checks run first, so a huge request on a short source still
reports `resolution`.

---

## A. Likely to cause disagreement between implementations

1. **§6.2: which responses the `Content-Encoding` rule applies to, and how repeated
   header lines combine.** "A response with a `Content-Encoding` header ... is a resolution
   error". Does that include 3xx redirect responses and 412/416 responses? The latter fail
   anyway, but a redirect carrying `Content-Encoding: gzip` (with an empty or irrelevant
   body) would be a hard failure under a literal reading.
   *Choice:* the check runs only on the final 200/206 response. Two separate
   `Content-Encoding: identity` header lines are joined as `identity, identity` (RFC 9110
   field-line combination) and rejected. *Suggest:* say "a 200 or 206 response" and state
   the rule for repeated field lines.

2. **§6.2: checks on the `bytes a-z/*` form.** The 206 `/total` row lists three checks:
   the returned range equals the requested one, `z < total`, and the body length is
   `z − a + 1`. The `/*` row says only "the requested bytes". *Choice:* both forms get the
   range-equality check and the body-length check. *Suggest:* state that explicitly.

3. **§6.2: parsing `Content-Range`, `ETag` and `Last-Modified`.** The spec doesn't say:
   - whether the range unit `bytes` is case-insensitive (RFC 9110 says range units are);
   - whether surrounding whitespace is trimmed;
   - what happens with several `ETag` or `Last-Modified` lines.

   *Choice:* `bytes` matches case-insensitively and OWS is trimmed. Anything other than
   exactly one `Content-Range`, `ETag` or `Last-Modified` line counts as "missing or
   unparseable", which is a resolution error. A weak `ETag` in the response fails a strong
   pin.

4. **§6.2: IMF-fixdate details.** These are not specified:
   - second `60`: RFC 9110 inherits `second = 2DIGIT`, and RFC 5322 allows leap seconds.
     *Choice:* accepted and treated as :59 + 1 s.
   - case sensitivity of day and month names. *Choice:* case-sensitive, per RFC 9110's
     "HTTP-date is case sensitive".
   - year `0000`. *Choice:* rejected as not a valid date.
   - invalid calendar dates such as `30 Feb`. *Choice:* unparseable.

   Different readers could split on any of these.

5. **§6.2: a 200 response shorter than the requested range.** The table says "the reader
   takes the requested bytes from the body" but doesn't say what happens when the body is
   too short. *Choice:* resolution error ("source value shorter than offset + j", §8.3).
   *Suggest:* say so in the 200 row.

6. **§6.2: counting redirects.** "up to 5 in a row; a sixth is a resolution error". My
   reading: up to 6 requests in total; a redirect status on the 6th response is an error.
   Off-by-one disagreements are likely. *Suggest:* say "at most 6 requests", or give an
   example.

7. **§6.2: the HTTP URL rules on redirect targets.** "An http: URL with userinfo or an
   empty host is a resolution error". Does this apply to redirect `Location` targets?
   *Choice:* yes, because the check runs for every request. Unspecified:
   - an empty port (`http://h:/`). *Choice:* default port.
   - port > 65535. *Choice:* resolution error.
   - a percent-encoded reg-name host. *Choice:* decoded.
   - the IPv6 zone ID. *Choice:* not accepted, because RFC 3986 has no syntax for it.

8. **§3.2: a `0x0001` block longer than 8 bytes on a record whose offset is all ones.**
   The spec says it is an error when the block is *shorter* than 8 bytes, and that in a valid
   archive the block is *exactly* 8 bytes. A longer block is not classified.
   *Choice:* accepted; the first 8 bytes are the offset (APPNOTE order, since the sizes
   can never be all ones). *Suggest:* classify it either way.

9. **§3.3 / §5.1: what "valid UTF-8" means.** Are encoded surrogates (CESU-8, `ED A0 80`)
   and overlong forms invalid? *Choice:* invalid (Python's strict decoder follows
   RFC 3629). This affects §3.3 (records ignored), §5.1 (malformed strings) and the writer.
   *Suggest:* cite RFC 3629 / Unicode "well-formed UTF-8".

10. **§6: building the base URI from a path.**
    - `..` at the root (`/../x`): there is no "segment before" to remove.
      *Choice:* the `..` is dropped, giving `/x`.
    - Non-UTF-8 path bytes: "the path's UTF-8 bytes" assumes a UTF-8 path.
      *Choice:* the raw OS bytes (`os.fsencode`) are percent-encoded.
    - A trailing `/`: the archive path is a file, so it doesn't arise.

11. **§6 file mapping: invalid UTF-8 after percent-decoding.** Is `%FF` in a `file:`
    path allowed? *Choice:* yes, because POSIX paths are bytes.
    *Suggest:* say so explicitly.

12. **§6 / §6.1: whether an explicit default value counts as a pin.** A `size` field
    encoded as 0 on a `data` source is "a pin". *Choice:* the decoder tracks presence of
    `optional` fields, so any present pin field counts, whatever its value. This follows
    from proto3 `optional`, but a reader built on a library that drops presence would
    differ. *Suggest:* say "present, even if 0".

13. **§8.1 vs §7: little is checked at open in a paged archive.** A paged archive's central
    directory is not parsed at open. Corruption outside the pages, or between the last
    page and the format records, is never detected. That seems intended, but it means
    the same corruption is an archive error in an unpaged archive and invisible in a
    paged one. Readers that "read more" (allowed by §8.1) must still not report it, so a
    careful reader must deliberately *not* parse the whole directory. *Suggest:* point
    this out.

14. **§3.4: magic `vzip/` followed by a non-digit (e.g. `vzip/x`).** Is it "not vzip"
    or "unsupported version"? Both are archive errors, so it has no effect on results;
    only the message differs. *Choice:* unsupported version.

## B. Underspecified but not likely to cause disagreement

15. **§3.2: what "lies within the file" means for the zip64 EOCD record.** The record
    is 56 bytes (12 + 44). *Choice:* `[off, off+56)` must be inside the file. The text
    could say "56 bytes".

16. **§3.2: the writer's "version made by" for the zip64 end record.** It is not
    specified (§9.2 gives 20 for records). *Choice:* 45/45.

17. **§7.1: page boundaries.** `page_size` is "about" N bytes. *Choice:* greedy. A new
    page starts when adding a record would exceed `page_size`, and a record bigger
    than `page_size` gets a page of its own. Readers don't care.

18. **§9.2: where pinned entries go.** Pinned entries are written after
    `__vz__/sources` and before `__vz__/index`, as the recommended layout shows. In the
    unpaged layout the central directory is in file order; in the paged layout body
    records are sorted, then sources, then index.

19. **§8.6: duplicate names.** The result is unspecified. *Choice:* the first record wins.

20. **§9.1: what the writer checks on URLs.** It checks only `URI-reference` syntax, not
    the `file:` mapping rules. For example, `file://otherhost/x` is accepted when
    written and fails at resolution. This matches "writers check it [syntax] when
    writing", but it could be stated.

21. **§6.1: `file:` sources and non-regular files.** The spec doesn't cover these. *Choice:* a
    directory is a resolution error. Other non-regular files are read as streams.

22. **§8.4: memory for DEFLATE `key` sources.** A `key` source that names a large DEFLATE
    entry must be inflated in full for every range read. *Choice:* a small per-archive cache
    of inflated values (64 entries). This is a performance trap the spec could mention.

23. **§4.3 / §8.4: payload length versus extra-field framing.** A payload longer than
    65519 bytes can only exist when the record has no other extra blocks. The rule is
    clear; it is just hard to build a test for it.

24. **§5.1: error precedence between "unknown field" and "malformed".** The text says
    unknown fields with wire types 0/1/2/5 are skipped, and that wire types 3/4/6/7 make
    the message malformed even for unknown fields. Clear, and implemented as stated.

## C. Things that were surprising or unnatural to implement

25. **§3.4: two fixed EOCD locations.** The two-location EOCD search with "no fallback" is
    simple, but unusual next to ordinary ZIP readers that scan backwards.

26. **§8.2 list on paged archives.** The `lo <= prefix or lo.startswith(prefix)` page
    selection rule, plus filtering records to `[lo, hi)`, is subtle but precisely
    specified. The worked example helped.

27. **§8.4: a key outside a broken page is still an entry error.** A key that is not in
    an unparseable page still gets an entry error, not *missing*. The rule is explicit
    but counter-intuitive. Tested.

28. **§5.1: deterministic encoding.** "One encoding per message" needs care with
    `optional` and `oneof` presence (always emit when set, even when empty). The examples
    (`0a 00`, the −1 encoding) were useful.

29. **Python specifics:**
    - `json` accepts duplicate members and `NaN` by default.
    - `bool` is an `int` subclass, so `true` would pass as an integer.
    - `zlib.decompressobj` silently ignores trailing bytes; `eof`, `unused_data` and
      `unconsumed_tail` must be checked.
    - `re` `$` matches before a trailing newline, so `fullmatch` is used everywhere.
    - `http.client` adds `Accept-Encoding: identity` itself unless the header is supplied.

## D. HARNESS.md

30. **Unknown members inside a `range` object** (e.g. `{"offset": 1, "foo": 2}`). The text
    says unknown members *of a query* are ignored; it doesn't cover the range object.
    *Choice:* ignored.

31. **A non-string `key` or `prefix`** (number, null). This is not listed as malformed.
    *Choice:* malformed, so the CLI exits non-zero. A lone surrogate (`"\ud800"`) in a
    query key is accepted; it can never match, so the key is `missing`.

32. **Order of checks in `read`.** The queries file is validated fully *before* the
    archive is opened, so an invalid queries file always exits non-zero, even when the
    archive also fails to open.

33. **Classes for unexpected internal errors.** None are defined. *Choice:* report
    `{"ok": false, "class": "internal"}` for that query and carry on, so one bug does not
    lose all results.

34. **Lone surrogates in description keys** (`"\ud800"`). These count as "not valid UTF-8"
    and are rejected.

35. **Negative `size` pin and pin ranges.** "Only `modified_not_after` may be negative" is
    taken to mean a negative `size`, `source`, `offset` or `length` is invalid.
    `modified_not_after` must fit in int64.

36. **The write output file.** It is written to a temporary name and renamed, so no file
    exists at `<out-path>` on failure.
