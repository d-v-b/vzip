# Spec notes from the TypeScript implementation (vzip v1, draft rev. 2)

These are the places where SPEC.md (and HARNESS.md) left me to guess, plus
things that were surprising or harder than expected. Each item gives the
section, the question, what I chose, and why. Items marked **[interop]**
could plausibly make two conforming implementations disagree on the same
archive or description. The rest are about clarity, or pitfalls that are
specific to an implementation.

The ranked summary is at the end.

---

## §3 ZIP container

### 3.1 §3.4: EOCD probe (fall-through after a magic mismatch)
- **Question:** Step 1 probes `file_size − 60`. In an archive with a 22-byte
  comment, that offset falls 16 bytes before the real EOCD. I first worried
  that a crafted key could fake an EOCD there. It cannot, for valid archives:
  the fake "comment length" field at probe+20 is the real EOCD's
  "number of this disk" field, which is 0, never 38. The spec could state
  this reasoning, because it is not obvious. What remains unclear is the
  case where step 1 matches but the comment does not start with `vzip/1`
  (possible only in invalid files). Is that an error, or does the reader
  fall through to step 2?
- **Chosen:** I pick the location from the signature and length only (60
  first, then 44), then check the magic. A mismatch is an archive error
  with no fall-through.

### 3.2 [interop] §3.2 / §8.4: what counts as "bad end records"
- **Question:** §8.4 lists "bad end records" as an archive error but never
  says which checks a reader makes. The open cases include: disk numbers
  other than 0, "entries on this disk" ≠ "total entries", a CD that extends
  past the EOCD or zip64 EOCD, a zip64 locator whose target lacks the
  signature, and an EOCD field that is all ones with no zip64 locator.
  §8.1 forbids errors at open that are not listed, so a strict reader and a
  lenient reader disagree on whether these archives open at all.
- **Chosen:** Each of these is an archive error. An all-ones EOCD field
  without a locator is an error rather than being read as a literal value.
  I also check that `cd_offset + cd_size` does not pass the start of the
  zip64 EOCD (or the EOCD), and, for unpaged archives, that the number of
  records parsed equals the EOCD count and that the records exactly fill
  `cd_size`.
- **Suggestion:** List the checks in the spec.

### 3.3 §3.2: "version 1" of the zip64 EOCD, and version-needed values
- **Question:** "zip64 end of central directory record (version 1, no
  extensible data)" could mean APPNOTE's v1 record layout (4.3.14.1, as
  opposed to v2 with CD encryption fields), or a version field set to 1.
  The "version made by" and "version needed" fields of that record are not
  given. §9.2 says version needed is 45 "for records with a ZIP64 extra
  field and 20 otherwise". Local headers never have a ZIP64 extra (rule 4),
  so the local header says 20 while its CD record says 45. That is legal but
  odd.
- **Chosen:** I use the v1 layout with size-of-record 44 and version
  made by / needed 45. The locator's total-disks field is 1. The local
  header uses 20 and the CD record uses 45 when it carries a ZIP64 extra.
  On read, I accept a total-disks value of 0 or 1.

### 3.4 §3.2: order of extra blocks
- **Question:** When a reference record also needs a 0x0001 block (local
  header offset ≥ 4 GiB), the spec does not say which block comes first.
- **Chosen:** 0x0001 first, then 0x7A76/0x7A77. Readers must accept either
  order, which mine does.

### 3.5 §3.1 rule 7 / §9.1: compressed size of 4 GiB or more
- **Question:** §9.1 tells the writer to reject a bytes entry whose *size* is
  ≥ 0xFFFFFFFF. Incompressible data can deflate to a *compressed* size of
  ≥ 0xFFFFFFFF even when the uncompressed size is below it. Rule 7 forbids
  this, but §9.1 does not list it.
- **Chosen:** The writer also rejects a compressed size ≥ 0xFFFFFFFF.

### 3.6 [interop] §3.3 / §8: records whose names are invalid UTF-8 or empty
- **Question:** Keys MUST be non-empty valid UTF-8, but neither §8.4 nor §8.6
  says what a reader does with a record that breaks this. Is it an entry
  error, an archive error, or unspecified? Should `list` show it?
- **Chosen:** Such a record is invisible. It cannot be addressed by a
  (valid) key, it is not listed, and it is not an error.
- **Suggestion:** Add it to §8.6 (unspecified), or make it an entry error
  and say that it is not listed.

### 3.7 §3.4: format entries reached through the comment have no uncompressed size
- **Question:** The comment gives only the offset and the *compressed* size.
  A reader that follows §3.4 ("need not consult those entries' central
  directory records") cannot bound or verify the inflated size, and cannot
  tell whether the DEFLATE stream ends before `sources_size` bytes, leaving
  trailing garbage.
- **Chosen:** I inflate with a fixed memory cap (1 GiB) and ignore trailing
  bytes after the end of the DEFLATE stream (Node's zlib does this). The
  same applies to `raw("__vz__/sources")`.

## §4 Entries

### 4.1 §4.1: a needed ZIP64 extra block that is missing or short
- **Question:** A record whose local-header-offset field is 0xFFFFFFFF but
  that has no 0x0001 block, or one too short to hold the offset, is not
  classified by any rule.
- **Chosen:** Entry error. The extra field "parses" in the §4.1 sense, but
  the body offset cannot be computed. I also honour 0xFFFFFFFF in the size
  fields in APPNOTE order (usize, csize, offset), even though §3.2 says sizes
  never need it.

### 4.2 [interop] §4.3 / §5.2: a zero-byte single-range payload depends on the source table
- **Question:** The canonical encoding of the Range `{source 0, offset 0,
  length 0}` is empty. A 0x7A76 block with `data_size = 0` is therefore a
  well-formed *source* range that needs `source (0) < number of sources`. In
  an archive with no sources, that payload is **malformed** (a payload
  error), even though it describes the empty value and needs no I/O. The
  harness range `{}` produces exactly this.
- **Chosen:** I follow the spec literally. The payload is malformed when
  there are no sources, and the writer rejects `{}` when `sources` is empty.
  This is surprising. Consider exempting zero-length source ranges from the
  `source` bound, or telling writers to use a 0x7A77 empty Concat or an
  empty literal for empty values.

### 4.3 §4.3 / §8.2: raw view of entries with entry errors
- **Question:** §8.2 calls `raw` "the view of a ZIP tool that knows nothing
  about vzip", but §8.4 says entry errors make `raw` fail. An entry with two
  0x7A76 blocks is perfectly readable by such a tool.
- **Chosen:** I follow §8.4: `raw` fails with an entry error.

### 4.4 Hidden reference entries are allowed but unreachable
- §2 and the harness allow reference entries under `__vz__/`. `get` treats
  hidden keys as missing, and a `key` source cannot point at a reference
  entry. Such entries can therefore only be seen through `raw`. I accept
  them, but the spec could say whether writers should reject them.

## §5 Messages

### 5.1 §5.1: is a reserved field number "in the schema"?
- **Question:** `reserved 2` in `Range`. The decoding rule is "skip fields
  whose field number is not in the schema". It is arguable whether a
  reserved number is "in the schema", and if it is, which wire type is
  "the one its type uses".
- **Chosen:** Treat field 2 as unknown and skip it with any allowed wire
  type.

### 5.2 §5.1: repeated message elements and the "default value" rule
- The encoding rule talks only about scalar fields, `oneof` and `optional`.
  An element of `repeated Range parts` whose own encoding is empty must
  still be emitted (`0a 00`), or the part disappears. This seems obvious,
  but nothing states it. I always emit repeated message elements.

### 5.3 §5.1: what "valid UTF-8" means, and a BOM pitfall
- The spec should cite RFC 3629, which rules out surrogates (CESU-8) and
  overlong forms. I use `TextDecoder("utf-8", {fatal: true})`.
  **Pitfall:** `TextDecoder` *strips a leading U+FEFF by default*. Without
  `ignoreBOM: true`, the key `"﻿bom"` silently decodes as `"bom"`. The
  same pitfall exists in other languages' "UTF-8 with BOM" decoders. A
  conformance test with a BOM-prefixed key would catch it.

### 5.4 §5.2: whether a payload is malformed depends on context
- Whether a Range is malformed depends on the number of sources, which lives
  in another entry. A payload cannot be validated on its own. That is fine,
  but it should be called out, because it is easy to validate payloads in
  the protobuf layer without the source count.

### 5.5 64-bit values in JavaScript
- `uint64` offsets, lengths and the 2^64 − 1 checks need `BigInt` in
  JS/TS, which spreads through the whole range arithmetic. This is not a
  spec problem, but it was the most invasive implementation cost. The
  harness limits its numbers to below 2^53. Archives can still carry larger
  values.

## §6 Source table

### 6.1 [interop] How to percent-encode the base `file:` URI
- **Question:** The base URI is "the `file:` URI of the absolute form of that
  path", but which characters get percent-encoded is not specified, nor is
  whether it is `file:///p` or `file:/p`, nor how non-UTF-8 path bytes and
  Windows paths (drive letters, backslashes) are handled. Resolution
  results only agree if every implementation maps the same file name to the
  same path again. A name containing `%`, `?` or `#` *must* be encoded.
- **Chosen:** `file://` plus an empty authority plus the UTF-8 bytes of
  `path.resolve(p)`. Every byte outside unreserved / sub-delims / `:` / `@` /
  `/` is percent-encoded as `%XX` in uppercase.
- **Suggestion:** Give the exact encoding set and show `%`, `#`, `?` and a
  space in an example.

### 6.2 [interop] Which URI references are "valid"
- **Question:** "Not a valid URI reference" is a resolution error. Strict
  RFC 3986 rejects spaces, non-ASCII characters (IRIs), malformed `%`
  escapes, `\`, and so on. Many URL libraries, such as WHATWG `new URL` in
  JS and Python's `urllib`, accept and repair these silently, so an
  implementation built on them reads data that a strict one rejects.
- **Chosen:** A hand-written RFC 3986 grammar check (scheme, authority with
  userinfo / IP-literal / reg-name / port, and character classes for path,
  query and fragment), then hand-written §5.2.2 strict resolution and
  §5.2.4 `remove_dot_segments`. `"a b.bin"` and `"é.bin"` are resolution
  errors. I deliberately avoided `new URL`.
- **Suggestion:** Warn implementers about WHATWG URL parsers. Say whether
  writers must validate `url` syntax: §9.1 only rejects an empty `url`, and
  my writer accepts any non-empty string.

### 6.3 `file:` details not covered
- Is the scheme `file` matched case-insensitively? Is `localhost`? I chose
  both, following RFC 3986 §3.1 and §3.2.2.
- After percent-decoding, `%2F` becomes `/` and `%00` becomes NUL. I decode
  everything, so NUL makes the open fail, which is a resolution error.
  Should `%2F` be forbidden?
- If the target is a directory or not a regular file, I treat it as "cannot
  be read", which is a resolution error.
- Self-reference: `url: "#"` or the archive's own file name resolves to the
  archive itself. Nothing forbids this, and it works. Only an empty `url`
  is rejected.

### 6.4 HTTP behaviour
- The spec does not say whether a server that ignores `Range` (status 200)
  is acceptable, how to detect that the object changed between requests,
  or whether the fragment is stripped. I accept 200 and slice the body,
  treat 416 or a short body as "source value is shorter" (a resolution
  error), strip the fragment, and do not check ETag or Last-Modified.

### 6.5 Empty `key` source
- An empty `url` is an archive error at open, but an empty `key` is only a
  resolution error later (the key is missing). The asymmetry is harmless
  but surprising. The writer rejects both, because an empty key is "absent".

### 6.6 Key sources that are DEFLATE entries
- A `key` source whose target is a method-8 entry forces the whole target to
  be inflated for any slice of it, which conflicts with the "fetch only the
  bytes in step 3" SHOULD. Consider recommending STORED for `key`-source
  targets. I read STORED targets by window and cache inflated ones.

## §7 Page index

### 7.1 [interop] What "malformed page index" covers
- **Question:** Is a page whose `[offset, offset+length)` extends past
  `cd_size` a malformed page index (an archive error at open), or a §8.6
  "does not match the central directory" case (unspecified, or an entry
  error when the page is read)? The same question applies to pages that
  overlap, are out of order, or are empty, and to duplicate pinned keys.
- **Chosen:** Out-of-bounds pages are an archive error at open. I do not
  check anything else. Duplicate pinned keys: the first one wins.

### 7.2 §7.2: "the last page whose first_key ≤ k"
- "Last" is only unambiguous if pages are sorted. I implement it literally
  as the last such page in wire order, which equals a binary search for
  valid archives.

### 7.3 [interop] `list` in a paged archive
- **Question:** The spec does not say how `list` interacts with pages. Must
  pinned keys be listed even if no page holds them? (They are "present" per
  §7.2.) Should a record that sits on the "wrong" page (one that §7.2 lookup
  would not select) be listed? If a page cannot be parsed, does `list` fail,
  and with which error class? "Keys with entry errors are listed" does not
  help when a page cannot be enumerated.
- **Chosen:** `list` reads every page and includes a record only if §7.2
  lookup would find it on that page, plus all pinned keys. A page that
  cannot be parsed makes `list` fail with an entry error.

### 7.4 Pinned entries
- The spec allows pinned hidden (non-format) entries, but the harness
  forbids them. That is an inconsistency. My library allows them and the
  CLI rejects them.
- A `Pinned` with `method` other than 0 or 8: I report an entry error. The
  spec does not say.
- A pinned key wins over the page lookup. The record on the page is not
  consulted, so a pinned key whose real record is a reference shows up as
  `bytes`. That is unspecified per §8.6, but worth saying explicitly.
- Pinned entries also appear in the pages (§7.1 says pages partition all
  body records). I include them.

### 7.5 §9.1: "`__vz__/index` MUST follow every entry … it indexes or pins"
- "Follow" means "be placed after in the file". That can be confused with
  the CD order in §7.1, where the format records come *after* the body
  records. Saying "its local header offset is greater than that of every
  body entry" would be clearer. The `__vz__/sources` entry may sit anywhere,
  because nothing in the index refers to it.

## §8 Reading

### 8.1 [interop] Error class for corrupt bodies of bytes entries
- **Question:** §8.4 has no class for "the DEFLATE body of a bytes entry is
  corrupt", "the body extends past end of file", or "the inflated size does
  not match the record". These are not covered by §8.6 either; §8.6 covers
  CRC checks only.
- **Chosen:** An entry error for that key.

### 8.2 [interop] Error class for requests larger than the reader's memory bound
- §10 says readers SHOULD bound memory, but no error class fits a request
  that exceeds the bound. I use a request error (default 1 GiB).

### 8.3 Error precedence
- When several errors apply, the spec does not say which one is reported:
  `start > end` on a key with an entry error, `start > end` with a malformed
  payload, or two failing ranges. I check the request first, then the
  entry, then the payload, then ranges in order. Only the class matters for
  the harness, so precedence matters too.

### 8.4 Unpaged central directory corruption
- In an unpaged archive the whole CD is read at open. A record with a bad
  signature or a truncated record makes the rest unparseable. I treat that
  as an archive error ("bad end records" or central directory). The spec
  only says that in *paged* archives such problems become entry errors.

### 8.5 Size of a STORED bytes value
- For method 0, is the value size `csize` or `usize`? They are equal in
  valid archives. I use `csize`, which matches the bytes actually present.
  For method 8 I check that the inflated length equals `usize`, and report
  an entry error otherwise.

### 8.6 UTF-8 ordering pitfall
- JavaScript's default `Array.prototype.sort` and `<` compare UTF-16 code
  units, which disagrees with UTF-8 byte order for characters above U+FFFF
  versus U+E000–U+FFFF (for example U+FF5E against U+1F600). Writers that
  sort the CD with the language default produce a broken page index. A
  conformance test with such keys would catch it. Mine are tested.

## §9 Writing

### 9.1 Validity of a description depends on layout
- The extra-field limit includes the ZIP64 block, so a reference with a
  65 520–65 531-byte payload is valid or invalid depending on whether the
  writer places it below or above 4 GiB. My writer checks this during
  layout. A description-level rule ("payload ≤ 65 519 bytes") would be
  easier to reason about.

### 9.2 Layout freedom and reproducibility
- §5.1 promises byte-identical *messages*. Archive bytes still differ
  between writers: entry order, CD order in unpaged archives, page grouping,
  extra-block order, and external attributes are all free.
- My writer: entries in description order, but with pinned entries moved
  after `__vz__/sources`. Then `__vz__/index`. The CD is sorted in UTF-8
  order in both paged and unpaged archives, followed by `sources` and then
  `index`. A page is closed when adding the next record would exceed
  `page_size`, and every page holds at least one record. DOS date
  1980-01-01 00:00. Version made by is 20 with host 0 (MS-DOS). External
  attributes are 0.

### 9.3 §9.1 does not require validating `url` syntax
- See 6.2. A writer can produce archives whose URLs every reader rejects.

---

## HARNESS.md

- **`page_size`, `sources`, `entries` absent:** The defaults are listed only
  for `compress`, `pinned` and `mirror`. I treat a missing `page_size` as
  `null` and missing arrays as empty.
- **Hex case:** "Hex strings are lowercase with even length" could be a
  promise about runner input or a rule to enforce. I reject uppercase hex as
  an invalid description.
- **Mixed range with explicit zeros:** Is `{"data": "00", "source": 0}` "a
  mix"? I reject any presence of `source`, `offset` or `length` next to
  `data`.
- **`{}` range:** This means `{source 0, offset 0, length 0}` and therefore
  needs at least one source (see 4.2). With `sources: []` my writer rejects
  it.
- **`compress: false` / `pinned: false` on reference entries:** I accept
  them; only `true` is invalid.
- **Pinned hidden entries:** The harness forbids them, but the spec allows
  them (see 7.4).
- **Invalid queries file:** "invalid" is not defined. I exit non-zero for a
  non-array, an unknown `op`, a non-string `key` or `prefix`, a `range`
  with zero or several forms, or a non-integer number. `range: null` is
  treated as *whole*.
- **Error class in output:** Results carry only a free-form message. If the
  runner wants to check the error *class* (entry vs payload vs resolution),
  it needs a field for it. My messages start with `"<class> error: "`.
- **Non-UTF-8 / lone-surrogate keys in JSON:** A description key like
  `"a\ud800"` parses in JSON but is not valid Unicode. I reject it as
  "not valid UTF-8".

---

## Ranked summary (most interoperability impact first)

1. **§6 URL validity and base-URI encoding (6.1, 6.2).** Strict RFC 3986
   versus WHATWG-style parsers will disagree on everyday inputs such as
   spaces and non-ASCII names, and the base `file:` URI encoding is not
   pinned down.
2. **§4.3/§5.2 empty single-range payload with zero sources (4.2).** The
   harness's own `{}` range yields a payload that is malformed or valid
   depending on whether the archive has any sources.
3. **Unclassified errors (8.1, 8.2, 3.6).** Corrupt DEFLATE bodies, size
   mismatches, the memory bound, and invalid-UTF-8 names have no error class
   or defined visibility.
4. **"Bad end records" and "malformed page index" are not enumerated (3.2,
   7.1).** Because §8.1 forbids unlisted open errors, readers will disagree
   on which slightly-off archives open at all.
5. **`list` in paged archives (7.3).** Pinned-only keys, records on the
   wrong page, and unparseable pages.
6. **Spec vs harness on pinned hidden entries (7.4).** Minor, but a direct
   contradiction.
7. **UTF-8 pitfalls (5.3 BOM stripping, 8.6 UTF-16 sort order).** These are
   not ambiguities, but they are likely sources of real-world bugs worth a
   note and a conformance test.
8. **Minor:** the EOCD probe fall-through (3.1), zip64 EOCD "version 1"
   wording (3.3), and the error precedence order (8.3).
