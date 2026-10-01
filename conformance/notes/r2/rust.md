# Notes on the vzip spec (draft, revision 2), from a Rust implementation

Each item gives the section, the question, and what this implementation chose and why.
Items are roughly ordered by how much they could affect interoperability. Items marked
**[interop]** are places where two careful implementations could plausibly give
different observable results for the same input.

## A. Error classification gaps (affect which error class / whether open fails)

1. **[interop] §8.1 / §8.4: what counts as "bad end records"?** The term is not defined.
   Candidates: non-zero disk numbers, entries-on-disk != total entries, central directory
   (CD) extending past the EOCD / zip64 EOCD, a zip64 locator whose target lacks the
   zip64 EOCD signature, a CD whose record count disagrees with the end record.
   *Chosen:* all of these are archive errors. The count check is only done for unpaged
   archives, where the whole CD is read anyway. A reader that checks less opens archives
   that this one rejects.

2. **[interop] §8.1 / §8.4: structural CD errors in an unpaged archive.** §8.1 says opening
   "reads and checks ... the whole central directory" for unpaged archives, but the
   archive-error examples in §8.4 don't mention CD records with a bad signature or
   truncated records. Only the paged case is covered ("surface as entry errors").
   *Chosen:* archive error at open for unpaged archives. In paged archives, an
   unparseable page is an entry error for every lookup that lands in that page.

3. **[interop] §3.4 / §6 / §7: the source table or index can't be read.** Possible causes:
   `sources_offset + sources_size` past EOF, invalid DEFLATE data, or `sources_size = 0`.
   §8.4 lists "malformed source table", which is about protobuf, not the container.
   *Chosen:* archive error. The spec should say so explicitly, and should also say
   whether an empty (0-byte) DEFLATE body is acceptable. Formally it is not a valid raw
   DEFLATE stream.

4. **[interop] §4.2 / §8.4: a corrupt bytes body.** A bytes entry whose DEFLATE stream is
   invalid, whose body extends past EOF, or that inflates to more than its declared
   uncompressed size: which error class is that? None of the five classes covers it.
   *Chosen:* entry error, reported by get/raw only. classify still says `bytes`, because
   classify looks only at the record. When the same entry is reached through a `key`
   source, the failure becomes a resolution error.

5. **[interop] §3.3 / §8: records whose name is empty or not valid UTF-8.** §3.3 says keys
   MUST be non-empty and valid UTF-8, but neither §8.4 (error classes) nor §8.6
   (violations readers need not detect) covers a record that breaks this.
   *Chosen:* such records are silently ignored. They can't be named by any query and
   aren't listed. Another reader could make this an archive error (unpaged) or an entry
   error.

6. **[interop] §4.1 / §3.2: ZIP64 extra field missing or too short.** A record whose local
   header offset is 0xFFFFFFFF but which has no `0x0001` block, or one that is too short,
   cannot be located. It isn't listed as an entry error. *Chosen:* entry error.

7. **§7.1 / §8.4: page bounds.** If a `Page` extends past `cd_size`, or `offset + length`
   overflows: is that a "malformed page index" (archive error) or a §8.6 "CD doesn't match
   the index" situation (unspecified)? *Chosen:* archive error at open. It is cheap to
   check and needs no extra I/O. Unsorted, overlapping or non-contiguous pages are not
   checked (§8.6).

8. **§7.2: pinned `method` other than 0/8.** A `Pinned` stands in for the record, so the
   record checks (bit 0, method) can't be applied. *Chosen:* a pinned method other than
   0 or 8 is an entry error for that key. The spec should say whether pinned values are
   trusted blindly.

9. **§8.2: order of checks.** Which comes first when several errors apply: request
   error, hidden key, or entry error? E.g. `get("__vz__/x", range(5,2))`, or `classify`
   on a hidden key whose record has an entry error. *Chosen:* request error first (the
   spec says "whether or not the key is present"), then the hidden check (returns
   missing without looking anything up), then lookup and entry errors. Stating the
   precedence would remove a source of divergence.

10. **§8.2 `list` in a paged archive when a page can't be parsed.** `list` must read every
    page. "Keys with entry errors are listed" covers per-record problems but not a page
    that doesn't parse at all, where the keys are unknown. *Chosen:* `list` fails with an
    entry error.

## B. URL / base URI resolution (§6)

11. **[interop] The exact base URI string isn't defined.** Is it `file:///abs/path` (empty
    authority) or `file:/abs/path` (no authority)? Which characters are percent-encoded?
    Results agree for almost every reference, but not quite all (e.g. references starting
    with `//`). *Chosen:* `file://` + path, with every byte outside the RFC 3986
    unreserved set and `/` percent-encoded (uppercase hex).

12. **[interop] "Relative paths are made absolute against the current directory" combined
    with "Symbolic links are not resolved".** `getcwd()` returns the physical path (on
    macOS `/tmp/x` becomes `/private/tmp/x`). `$PWD` may hold a logical path that contains
    symlinks. With a `..` reference that crosses a symlinked directory, the two give
    different files. *Chosen:* `std::env::current_dir()` (physical) followed by lexical
    normalization. The spec should name the source of "the current directory".

13. **The `file:` rules leave several details open.**
    - Does an empty query (`x.bin?`) count as "a query"? *Chosen:* yes, so it is an error.
    - Are the scheme and `localhost` matched case-insensitively? *Chosen:* yes (RFC 3986
      says schemes and hosts are case-insensitive).
    - What does `%2F` decode to? It becomes `/` after decoding and so silently changes
      the path structure.
    - `%00` produces a path that cannot be opened. That becomes a resolution error here.
    - Windows drive letters and UNC paths are not addressed.

14. **What counts as "a valid URI reference"?** RFC 3986's grammar is detailed: IPv6
    literals, ports, `path-noscheme`, and so on. Readers whose validators differ in
    strictness can split on inputs such as non-ASCII characters (an IRI), `[` in a path,
    or `1a:b`. *Chosen:* a fairly strict hand-written validator. Any non-ASCII character
    is invalid. Percent triplets must be well formed. A colon in the first segment of a
    relative path is invalid. IP literals are only loosely checked. A list of
    must-reject and must-accept examples would help.

15. **An empty `url` is an archive error, yet `""` is a valid same-document reference.**
    Meanwhile `"#x"` (also a same-document reference) is accepted and resolves to the
    archive file itself. That is harmless but inconsistent. Consider also rejecting
    references that resolve to the archive, or explaining why `""` alone is special.

16. **HTTP is a SHOULD.** This implementation doesn't support http(s). It reports
    "unsupported scheme" as a resolution error, as the spec allows. A conformance suite
    that tests http URLs would see a difference.

## C. ZIP container details

17. **[interop] §3.4 locating the EOCD.** Suppose the check at `file_size − 60` matches
    the signature and a comment length of 38, but the comment doesn't start with
    `vzip/1`. Should the reader still try `file_size − 44`? The steps read as "stop at
    the first structural match". *Chosen:* no fallback, so it is an archive error. A
    22-byte-comment archive whose bytes at −60 happen to look like an EOCD with length
    38 would then be rejected. That is unlikely, but the two readings disagree.

18. **§3.2 zip64 EOCD.**
    - When a zip64 locator is present, does the reader take *all* values from the zip64
      record or only those that are all-ones in the EOCD? *Chosen:* all (the spec says
      "use the zip64 end of central directory record").
    - "version 1, no extensible data" is ambiguous. It presumably means the APPNOTE
      "Version 1" layout (size-of-record = 44). The *version made by* / *version needed*
      values for that record are not given. *Chosen:* 45 / 45, total disks = 1 in the
      locator.

19. **§4.3 vs §3.2: the extra-field limit depends on layout.** A record needs a 12-byte
    ZIP64 block only if its local header offset is ≥ 4 GiB. So the maximum payload is
    65531 or 65519 bytes, depending on where the entry lands. A writer can't validate an
    entry in isolation. The reject-or-accept outcome depends on the sizes of earlier
    entries and can change with entry order. *Chosen:* reject payloads > 65531 up front.
    Re-check the full extra field length when each record is built, which is still
    before any output is written. The 4 GiB path is not exercised by tests here because
    the writer builds the archive in memory.

20. **§3.1 rule 7 vs §9.1.** Rule 7 limits *both* compressed and uncompressed size, but
    §9.1 only lists "a bytes entry's size". DEFLATE can expand incompressible data. If an
    entry is slightly under 4 GiB, its compressed form can exceed the limit. *Chosen:*
    reject either case.

21. **§3.1 rule 5: bit 3 and other flags.** §8.6 excludes rule 5 except bit 0, so a set
    bit 3 is unspecified. Readers here ignore it. Bit 11 isn't checked by readers either.

22. **§4.1: zero-length reference blocks.** A `0x7A76` block with `data_size = 0` decodes
    to `Range{source:0, offset:0, length:0}`, which is the canonical encoding of a valid
    zero-length source range. It is valid if there is ≥ 1 source, and a payload error
    otherwise. That seems intended, but it is surprising enough to deserve an example.
    Similarly, a `0x7A77` block of size 0 is a valid empty Concat.

## D. Messages (§5)

23. **§5.1 "Fields may appear in any order ... last one wins".** This is fine for scalars.
    The spec could say explicitly that it is not proto's merge semantics for embedded
    messages, though no singular message field exists today. Packed encoding is
    irrelevant because there are no repeated scalars. It is worth stating that no
    packed fields exist.

24. **§5.1 canonical encoding is only partly determined.** It fixes field order and
    defaults, but not:
    - the order of `pinned` entries,
    - page boundaries (writer's choice),
    - the order of CD records in an unpaged archive.

    So "compare encodings byte for byte" works per message but not per archive.
    *Chosen:* pinned are sorted in UTF-8 key order. The unpaged CD is in file order.

25. **§5.2 "offset + length MUST NOT exceed 2^64 − 1".** This means the sum must fit in a
    u64. That is unambiguous, but "the end offset fits in u64" would be clearer.

## E. Data model and keys

26. **Unicode normalization.** Keys are compared as UTF-8 bytes, so NFC and NFD forms of
    the same name are different keys. That is probably intended, but it is not stated.

27. **§8.2 hidden keys in a source `key`.** These are allowed, and the format entries are
    excluded. Pinned keys are resolved through their `Pinned` record. This was clear
    enough.

28. **§7.2 pinned precedence.** "A pinned key is a present `bytes` entry" means a pinned
    entry wins over whatever the page says. Duplicate pinned keys are a writer error, but
    reader behaviour is not in §8.6. *Chosen:* first wins.

29. **§7.1 page sizing.** HARNESS says "a page SHOULD hold about `page_size` bytes". The
    spec itself says nothing about the unit or how close "about" is. *Chosen:* close a
    page once it reaches ≥ `page_size` bytes, so a page can exceed it by up to one
    record. A single record larger than `page_size` gets its own page.

## F. Things that were unnatural or hard to implement

- §4.3 / §3.2: the payload limit depends on layout (item 19). It forces a validate-after-
  layout step in a writer that otherwise validates first.
- §6 / RFC 3986: there is no URI crate in the dependency budget, so the RFC 3986 parser,
  validator and §5.2.2 resolver were hand-written (and tested against the RFC §5.4
  examples). This is the largest source of likely divergence between independent
  implementations (items 11–15).
- §7: the paged reader still has to read *all* pages to answer `list("")`. That is
  expected, but it means page parse errors leak into `list` (item 10).
- §3.4: the reader cannot bound DEFLATE output for the format entries because the comment
  gives only the compressed size. A fixed cap (4 GiB) is used.

## G. HARNESS.md

- **Defaults for missing top-level members** `page_size`, `sources`, `entries` are not
  stated. Only `compress`, `pinned` and `mirror` have defaults. *Chosen:* null, [] and [].
- **Wrong JSON types** (`"compress": "yes"`, `"mirror": null`) are not covered. *Chosen:*
  invalid description. Exit non-zero.
- **Pinning hidden entries.** HARNESS forbids it ("not hidden"). Spec §7.1 / §9.1 only
  forbid non-bytes entries and format entries, so a hidden bytes entry may be pinned
  according to the spec. This implementation follows HARNESS for writing. Its reader
  accepts pinned hidden keys (they still classify as missing).
- **Range `{}` with no sources.** By "missing means 0", `{}` is `source 0`, so it is
  invalid when there are no sources. Is that intended? (It is a natural consequence,
  but surprising.)
- **`range` query objects.** Behaviour with extra members (`{"start":1,"end":2,"suffix":3}`)
  or `"range": null` is not stated. *Chosen:* mixed forms make the queries file invalid,
  so the CLI exits non-zero. `null` means whole.
- **Archive file that can't be opened at all** (ENOENT, EACCES). Is that an archive error
  reported in JSON, or a CLI failure? *Chosen:* archive error, exit 0.
- **Hex strings:** uppercase hex in a description is treated as invalid ("Hex strings are
  lowercase"). That is strict, but the text reads as a constraint rather than a
  convention.
- **JSON keys with lone surrogates** (`"\ud800"`) can't become UTF-8. serde_json rejects
  the document, so the description is invalid. This is consistent with §9.1 ("not valid
  UTF-8"), but HARNESS doesn't say.
- The CLI ignores unknown members everywhere, including in sources, ranges and queries.
  HARNESS says so only for the description in general.
