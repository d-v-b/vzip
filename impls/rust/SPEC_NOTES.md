# SPEC_NOTES: vzip draft revision 4, Rust implementation

These notes list each place where the spec (SPEC.md) or the harness
(HARNESS.md) was ambiguous, underspecified, inconsistent or surprising,
and the choice this implementation made. Items are grouped by section and
ranked at the end by how much they are likely to affect interoperability.

Implementation layout: `src/proto.rs` (hand-written protobuf),
`src/uri.rs` (RFC 3986 validation and resolution, `file:` mapping),
`src/reader.rs`, `src/writer.rs`, `src/main.rs` (harness CLI). HTTP is not
implemented; `http:`/`https:` URLs give a resolution error ("unsupported
scheme").

---

## Ranked by interoperability impact

1. **The entry-error list is split across §8.1 and §8.4, and they differ.**
   §8.1 says "a local header offset of 0xFFFFFFFF without a ZIP64 extra
   block holding the offset" is an entry error. The §8.4 table, which is
   supposed to be the list of causes, leaves it out. It is also unclear
   whether this error makes **classify** fail. Classify "looks only at the
   central directory record (§4.1)", and §4.1 is only about the extra-field
   blocks. But the §8.4 effect column says entry errors make classify fail.
   - Choice: every entry-error cause (unparseable extra field, R>1, method
     not 0/8, bit 0, reference entry with method 8, offset 0xFFFFFFFF with
     no usable ZIP64 value) makes classify, get and raw fail, including for
     reference entries that never need the body offset.
   - Suggestion: put one complete list in §8.4 and say outright that
     classify checks all of it.

2. **ZIP64 extra block layout on the reader side (§3.2, §8.1).** Writers
   put only the offset in the 0x0001 block. APPNOTE puts fields in a fixed
   order (uncompressed size, compressed size, offset, disk) and includes
   each one only when its header field is all ones. The spec doesn't say
   what a reader does with a record whose 32-bit size is 0xFFFFFFFF, which
   §3.1 rule 7 forbids but §8.6 lets go undetected. That record shifts
   where the offset sits in the block. It also doesn't say what happens
   when the block is shorter than needed, or when 0x0001 appears twice.
   - Choice: follow APPNOTE order. A block too short for a field that
     needs it is an entry error. If there are duplicate 0x0001 blocks, the
     first one is used.

3. **Which bytes a `raw` read of a format entry returns (§3.4, §8.2).**
   Readers "MUST read" the format entries through the comment, and raw
   shows "every present entry ... including format entries". In an unpaged
   archive the CD also has a `__vz__/sources` record, which may disagree
   with the comment (§8.6 makes that unspecified).
   - Choice: raw of `__vz__/sources` and `__vz__/index` always goes through
     the comment and is inflated with the clean-inflate rule. No
     uncompressed-size check is possible, because the comment has no
     uncompressed size.
   - Related gap: §8.1 says "for a bytes entry it must also inflate to the
     record's uncompressed size", but format entries are read without their
     record. So their uncompressed size is never checked. The spec should
     say this.

4. **Body errors on reference entries in the raw view (§8.4).** The body
   error row says "a **bytes** entry's body lies outside the file" and "a
   STORED entry's sizes differ". raw on a reference entry reads its body,
   but the spec never says what happens when that body is out of bounds or
   has mismatched sizes.
   - Choice: the same body errors apply to raw on reference entries.

5. **Duplicate central directory names (§3.1 rule 2, §8.6).** These are
   "unspecified", but readers still have to pick something, and list
   output depends on it.
   - Choice: the first record wins in unpaged archives, and list
     de-duplicates.
   - In paged archives, lookup uses the first record with that name in the
     selected page.

6. **`list` in paged archives (§8.2).** The phrase "every page whose key
   range can contain a key with that prefix" needs a definition of a
   page's key range.
   - Choice: page `i` covers `[first_key_i, first_key_{i+1})`, and the last
     page is open-ended. Keys with prefix `P` form the interval
     `[P, succ(P))`. Here `succ(P)` is `P` with trailing 0xFF bytes
     stripped and its last byte incremented, or unbounded if nothing is
     left.
   - A record is listed only if `select_page(name) == i`. Pinned keys are
     always listed. The result is de-duplicated, because a pinned key is
     usually also in a page.
   - It would help to spell this computation out in the spec, because
     getting it wrong makes list silently lose keys in exactly the
     edge-case archives a conformance suite will build.

7. **Pinned keys that are not valid keys (§7.2).** A `Pinned.key` may be
   the empty string, and `first_key` may be `""`, because §7.2's
   malformed list doesn't forbid either. With an empty pinned key,
   `classify("")` would report `bytes` in a paged archive and `missing` in
   an unpaged one. `""` can never be a valid key.
   - Choice: accepted as written (not malformed). Suggestion: add "a pinned
     key is empty" to the malformed list.

8. **The EOCD entry counts are never checked (§3.2, §8.1).** §8.1 says "MUST
   NOT report an error at open that this section does not list". It doesn't
   list a mismatch between the EOCD (or zip64) entry counts and the number
   of records, or between "entries on this disk" and "total entries".
   - Choice: both counts are ignored, except for the all-ones check that
     triggers ZIP64.
   - Also: the zip64 record only has to "lie within the file". The spec
     doesn't say whether it must come before the locator, or not overlap
     the CD. Only the bounds are checked.

9. **Where the CD may lie (§8.1).** The spec says "lies within the file". It
   doesn't say the CD must end before the EOCD (or the zip64 records).
   - Choice: only `cd_offset + cd_size <= file_size` is checked. A CD that
     overlaps the EOCD fails the unpaged record parse anyway.

10. **The `modified_not_after` rounding wording is inconsistent (§6.1).** The
    pin table says "rounded down, i.e. towards negative infinity". The
    `file:` column says "truncated to whole seconds". For pre-1970 mtimes
    with a fractional part, these differ.
    - Choice: `st_mtime`, which floors.

11. **Resource limit (§8.4, §10).** The reader documents a limit of
    **1 GiB** (`MAX_REQUEST_BYTES`). It applies to the bytes one get/raw
    returns and to the inflated size of a DEFLATE body, and exceeding it is
    a request error.
    - The check is lazy: ranges are resolved in order, and the limit trips
      only when real bytes have been gathered. So a huge range on a
      missing source still reports the resolution error a reader without
      a limit would report.
    - Because §8.4 lets a limit be reported "at whatever step", the same
      archive can give different classes in different conforming readers.
      Test suites must avoid that case.

## §3 ZIP container

- §3.1 rule 4 / §3.2: the "version needed to extract" of local headers is
  never stated. A local header never has a ZIP64 extra, so it uses 20.
- §3.4: Info-ZIP `unzip -t` prints the binary vzip comment (`vzip/1` plus
  16 or 32 binary bytes) to the terminal. That's harmless but surprising
  for a format that advertises transparency to ZIP tools.
- §3.4: if the file is shorter than 44 or 60 bytes, that step just
  doesn't match. The reader implements it that way, though the spec could
  say so.
- §3.2: the 16- and 32-bit EOCD fields that are *not* all ones are ignored
  once ZIP64 is triggered ("all four values are taken from the zip64
  record"). Clear, but disagreements between the two sets are silently
  accepted.

## §4 Entries

- §4.1: whether a zero-length 0x7A76 block counts is unclear. It does
  count: an empty Range is a valid source range, `{source 0, offset 0,
  length 0}`. This decodes to a payload error if there are no sources.
- §4.3: "Extra field blocks may appear in any order" sits in the middle of
  a paragraph about methods and seems misplaced. The writer puts the ZIP64
  block (when needed) first, then the reference block.

## §5 Messages

- §5.1 encoding: "not emit a scalar field equal to its default value"
  doesn't cover **repeated message elements whose encoding is empty**, for
  example a Concat part `{source 0, offset 0, length 0}`, which encodes to
  zero bytes. Every repeated element is always emitted (tag + length 0);
  otherwise the part would vanish. The spec should say so explicitly.
- §5.1: `int64 modified_not_after` uses plain VARINT (two's complement, 10
  bytes for negatives), not zigzag. That follows from the `int64` type,
  but it's worth an explicit note for hand-written codecs.
- §5.2: "In a literal range, `source`, `offset` and `length` MUST all be 0"
  is checked on the decoded values. So an explicitly encoded 0 (a
  non-canonical encoding) is accepted.
- §5.3: Concat size overflow (sum > 2^64−1) is a payload error, and it is
  checked while decoding.

## §6 Source table

- §6: URL syntax is checked only when the source is resolved (resolution
  error), not at open. That follows the spec (§6 lists the open-time
  checks), but it's asymmetric with `etag`, which is checked at open. A
  reader could reasonably validate at open, and that would turn a
  resolution error into an archive error. Worth a sentence confirming the
  intent.
- §6 file-URI mapping:
  - Empty segments in the resolved path (`/a//b`) aren't addressed. They
    are passed through to the OS.
  - A trailing `/` makes the path a directory. Reading it fails, giving a
    resolution error.
- §6: a URL that is only a query (`?x`) resolves to the archive's path
  plus a query, which the `file:` rules reject. That's fine, but worth
  noting next to the "fragment-only resolves to the archive itself"
  remark.
- §6 base URI: the base is built from `getcwd` for relative paths. On
  macOS `getcwd` returns the resolved `/private/tmp/...`, while an
  absolute argument `/tmp/...` stays unresolved. The two give different
  base URIs for the same file. Only absolute (`file:///...`) URLs would
  notice, so this is harmless in practice.
- §6.1: the order of pin checks relative to each other and to the "source
  value is shorter" check isn't specified. All of them are resolution
  errors, so the class doesn't change. The order used: size, etag,
  modified_not_after, then length.
- §6.1: "A reader that checks a pin once per source SHOULD do so per
  opening". This implementation re-stats the file on every read, which is
  stricter.

## §7 Page index

- §7.1 rule 2 ("the last page ends where the format entries' records
  begin") isn't in the §7.2 malformed list and can't be checked without
  parsing the CD. It is not checked.
- §7.2: lookup checks pinned keys first, then pages. If a key is both
  pinned and in a page with different values, the pinned values win.
  §8.6 covers this.
- Page grouping: "about `page_size` bytes". The writer starts a new page
  when adding the next record would push the current page over
  `page_size`. A record bigger than `page_size` gets a page of its own.

## §8 Reading

- §8.2 `get` on a bytes entry: the window uses the record's uncompressed
  size `n`. For a DEFLATE body, the whole body is inflated and checked
  against `n` before slicing (§8.4 step 4).
- §8.3 for `key` sources: the "value shorter than offset + j" check uses
  the record's uncompressed size before inflating. A corrupt body is
  reported as "shorter" rather than "body error inside a resolution
  error". Both are class `resolution`.
- §8.4: when a `key` source's lookup hits an unparseable page, that is an
  entry error for the key, which becomes a resolution error for the
  reference. It is treated as "has an entry error".

## §9 Writing

- §9.1's reject list is incomplete relative to "MUST produce a valid
  archive". It doesn't mention:
  - a source range with `offset + length > 2^64−1`;
  - a Concat whose total size overflows.
  Both would produce malformed payloads, so the writer rejects them too.
- §9.1: whether a `key` source may name a **hidden** bytes entry is clear
  (yes, §6). A `key` source may also name a pinned entry. That's allowed.
- §9.2 layout is followed: unpinned entries in description order, then
  `__vz__/sources`, then pinned entries, then `__vz__/index`, then the CD.
  In paged archives the CD is sorted; in unpaged archives it's in write
  order.

## HARNESS.md

- "`null` is allowed only for `page_size`. Any other member that is `null`
  makes the description invalid" conflicts with "Unknown members are
  ignored" when an *unknown* member is `null`. Choice: any `null` member
  other than top-level `page_size` is rejected, known or not.
- A `get_raw` query that carries a `range`: the table says "(no `range`)"
  but doesn't say whether one is invalid. Choice: invalid (exit non-zero).
- A `list` query without `prefix`: only "missing `key`" is named as
  malformed. Choice: invalid.
- A range object with a partial form (`{"start": 1}` without `end`) or with
  none of the forms: choice: invalid.
- A literal range with an explicit zero source (`{"data": "00", "source":
  0}`): "never a mix" taken literally, so it's invalid.
- `modified_not_after`: "All numbers ... are non-negative", but the field is
  int64 and pre-1970 times are meaningful. Negative values are accepted.
- `source` values ≥ 2^32 in a range are rejected as "out of bounds", which
  they are anyway.
- Archive errors vs. invalid queries: the queries file is parsed and
  validated *before* the archive is opened. So an invalid queries file
  exits non-zero even when the archive is also bad.
