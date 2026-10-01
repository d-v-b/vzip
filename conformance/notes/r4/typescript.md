# SPEC_NOTES: vzip v1 draft rev. 4, TypeScript implementation

These notes cover every place where SPEC.md or HARNESS.md was ambiguous,
underspecified, contradictory or surprising, and every place where I had to
choose. Each entry gives the section, the question, my choice, and the reason.
Entries in part A are ordered by how likely they are to make two conforming
implementations disagree. Parts B and C hold the smaller issues and the
implementation difficulties.

---

## A. Issues that affect interoperability

### A1. §8.4 / §10: the resource-limit exception makes error classes depend on the implementation
*Question.* A limit "may be reported at whatever step the reader reaches it".
Take a reference whose single range is `length = 2^40` over a 10-byte file.
`get(whole)` could fail with a **request** error (a reader that checks the
window size first) or a **resolution** error (a reader that checks the
source's length first). Both readers conform. The harness compares classes,
so the two will disagree. A bytes entry whose record declares a 5 GiB
uncompressed size has the same problem: it can be a request error or a body
error.
*Choice.* My limit defaults to 256 MiB and can be changed with
`maxRequestBytes` or `$VZIP_MAX_REQUEST_BYTES`. I check it as late as
possible: for each range, in order, the reader resolves the source, checks
the pins and checks that the source is long enough, and only then checks
`accumulated + overlap` against the limit. Resolution errors therefore win
whenever they exist. For DEFLATE bodies, the limit is checked against the
recorded `usize`/`csize` before inflating.
*Suggestion.* Fix the order, for example "limits are checked after every
other check of the same range", or say that conformance tests must not
exercise limits.

### A2. §8.1 / §10: a resource limit at open has no error class
*Question.* §8.1 says a reader "MUST NOT report an error at open that this
section does not list". A central directory or source table that is too
large for memory is not listed. A request error needs a request, and there
is none at open.
*Choice.* I report an **archive** error when the source table, the page index
or an unpaged central directory exceeds the limit.

### A3. §8.4 body errors: is a STORED body that extends past EOF an error when the window lies inside the file?
*Question.* "A STORED body is read only within the window", but "a bytes
entry's body lies outside the file" is a body error. A reader that checks
only the bytes it reads succeeds on `range(0,2)` of a STORED body whose tail
lies past EOF. A reader that checks the declared extent fails.
*Choice.* I check the full extent `bodyOffset + csize <= fileSize` on every
get and raw. This costs no I/O and makes the result independent of the
window. I also check `csize == usize` for STORED entries in every case,
including raw of reference entries.

### A4. §6.1: "truncated" contradicts "rounded down, towards negative infinity"
*Question.* For `modified_not_after`, the pin table defines the time as
"rounded down, i.e. towards negative infinity". The `file:` check column
says "truncated to whole seconds". The two differ for mtimes before 1970
(for example −0.5 s becomes −1 or 0).
*Choice.* Floor (towards −∞), following the normative definition. I compute
it from `mtimeNs` as a BigInt.

### A5. §8.1 "inflates cleanly": is the uncompressed size of the format entries checked?
*Question.* "For a bytes entry it must also inflate to the record's
uncompressed size." The format entries are bytes entries, but readers are
told to locate them through the comment and need not consult their records.
In a paged archive the reader cannot even find those records without parsing
the tail of the central directory. Must the reader check the size?
*Choice.* No. The format entries must only be a single complete raw DEFLATE
stream that fills `[offset, offset+size)` exactly. A reader that does check
would report an archive error where mine opens the archive.
*Suggestion.* Say explicitly whether the format entries' uncompressed sizes
are checked.

### A6. §7.2 / §8.2: which pages does `list(prefix)` read?
*Question.* "list reads every page whose key range can contain a key with
that prefix." The spec never defines a page's key range.
*Choice.* Page *i* covers `[first_key_i, first_key_{i+1})`, and the last page
covers `[first_key_last, +∞)`. Page *i* is read if
`first_key_i < upper(p)` and (`i` is the last page or
`first_key_{i+1} > p`). Here `upper(p)` is `p` with trailing 0xFF bytes
removed and the last byte incremented, or +∞ if nothing remains. A record
found in page *i* is listed only if `first_key_i <= name < first_key_{i+1}`,
which is the record lookup would find. Readers that pick a different (still
"can contain") set of pages report entry errors differently when an
unrelated page is corrupt, for example a reader that always reads every page.
*Suggestion.* Define the key range of a page and say whether the reader
MUST NOT read pages outside the prefix range. Otherwise the result of a
`list` over an archive with one corrupt page varies.

### A7. §4.1 / §8.1: how the ZIP64 extra block is interpreted
*Questions.*
(a) If the local header offset is 0xFFFFFFFF and the ZIP64 block holds more
than 8 bytes, which 8 bytes are the offset? APPNOTE says fields appear in
the fixed order usize, csize, offset, disk, and only for header fields that
are all ones. A reader that takes "the first 8 bytes" and one that follows
APPNOTE disagree only when sizes are also 0xFFFFFFFF, which breaks rule 7
(need-not-detect).
(b) If several 0x0001 blocks are present, which one counts?
(c) Is a 0x0001 block that is too short to hold the offset an entry error?
The spec only says "without a ZIP64 extra block holding the offset".
*Choice.* I follow APPNOTE ordering: the sizes are read from the block too
when they are 0xFFFFFFFF. The first 0x0001 block counts. A block too short
for the needed field is an entry error.

### A8. §6 vs §9.1: an empty url is an archive error, an invalid URI only a resolution error
*Question.* At open, an empty `url` is an archive error, but a `url` that
does not match `URI-reference` (for example `é.bin` or `a b`) is only a
resolution error when used. That asymmetry is surprising. Was it intended?
*Choice.* I followed the text: empty is an archive error, an invalid
reference is a resolution error on use.

### A9. §6: what counts as "the object cannot be read"
*Question.* Is a directory, FIFO or device a resolution error? Should
symlinks be followed?
*Choice.* I follow symlinks (fs.open). Anything that is not a regular file
after open is a resolution error.

### A10. §6.1 over HTTP: several cases are unspecified
- A server that ignores `Range` and answers **200**. *Choice:* accept it,
  check `size` against the body length, and slice.
- A **206** response with `Content-Range: bytes a-b/*` (unknown total) and a
  `size` pin. *Choice:* resolution error, because the pin cannot be checked
  and pins fail closed.
- A **206** response whose range differs from the one requested (the server
  clipped it because the object is short). *Choice:* resolution error ("the
  source value is shorter than offset + j").
- **416**. *Choice:* resolution error (object too short).
- `modified_not_after` outside the years HTTP-date can express (int64 allows
  values before year 1 or after 9999). *Choice:* resolution error, because
  the pin cannot be checked.
- Any status other than 200, 206, 412 or 416: resolution error.

### A11. HARNESS: does `null` in an unknown member make the description invalid?
*Question.* "Unknown members are ignored" contradicts "Any other member that
is null makes the description invalid" when an unknown member is `null`.
*Choice.* Invalid. I reject `null` anywhere except at top-level `page_size`,
recursively, including inside unknown members. Another implementation may
ignore them.

### A12. HARNESS: `compress: false` and `pinned: false` on reference entries
*Question.* The text says "`compress: true` ... on a reference entry the
description is invalid". Is an explicit `false` allowed?
*Choice.* Allowed, because only `true` is forbidden. The same holds for
`pinned: false`.

### A13. HARNESS: negative `modified_not_after`
*Question.* The harness says "All numbers ... are non-negative integers", but
`modified_not_after` is an `int64` and a negative pin is meaningful.
*Choice.* I accept any integer in int64 range for `modified_not_after` and
require non-negative integers everywhere else.

---

## B. Smaller ambiguities and surprising rules

**B1. §3.2: what "lies within the file" means for the zip64 record.** I
require `[recOff, recOff+56)` to lie within the file. I do not require it to
end before the locator, or the central directory to end before the end
records. The central directory check is only `cdOffset + cdSize <=
fileSize`, exactly as §8.1 says. An archive whose CD overlaps its EOCD
therefore passes the open checks.

**B2. §3.2: the locator's "total number of disks".** Is it a "disk-number
field" that readers ignore? I ignore it, and I write 1.

**B3. §8.1: the EOCD entry counts are never checked.** In an unpaged archive
the count can disagree with the number of records, and §8.1 forbids
reporting it. That is surprising, but I followed it. The counts matter only
as the trigger for ZIP64.

**B4. §3.1 rule 9 is neither checked nor waived.** The rule forbids CD
encryption, digital signatures and the archive extra data record. It is
absent from §8.6, but §8.1 lists no check for it either. In an unpaged
archive a digital-signature record (0x05054b50) breaks "parses as a sequence
of records", which is an archive error. In a paged archive it goes
unnoticed. I did nothing special.

**B5. §3.3 / §8.6: ignored records.** §3.3 points to §8.6 for records with
empty or non-UTF-8 names, but §8.6 does not mention them. I ignore such
records everywhere: lookup, list and the duplicate check. "Valid UTF-8" is
taken as RFC 3629, so encoded surrogates and overlong forms are invalid
(`TextDecoder` with `fatal: true`).

**B6. §3.1 rule 2 / §8.6: duplicate names.** Results are unspecified. I use
the first record in an unpaged archive and the first match inside the page
in a paged one.

**B7. §8.2 raw of the format entries.** Should raw read through the comment
or through the CD record? In an unpaged archive the two may disagree
(need-not-detect). I return the bytes found through the comment, inflated
at open. `raw("__vz__/index")` on an unpaged archive is *missing*.

**B8. §8.2 raw of a pinned entry.** I use the `Pinned` values, consistent
with §7.2, rather than the CD record.

**B9. §7.2: the precedence of a pinned key.** The text implies "pinned wins"
over a record with the same name in a page. I implemented that. `list`
de-duplicates.

**B10. §7.2: an empty `first_key` or pinned `key`.** These are not listed as
malformed. An empty pinned key can never be looked up, because keys are
non-empty. `list` ignores it.

**B11. §7.2: what "a page lies outside the central directory" covers.** I
check `offset + length <= cd_size`, where `cd_size` includes the
format-entry records. Nothing checks that the last page ends where the format
records begin.

**B12. §8.4: ordering for `get` on a key whose page cannot be parsed while
the key is hidden.** The hidden check comes before lookup, so the result is
*missing*, not an entry error. Raw has no hidden step, so raw gives an entry
error. I implemented both as described. I note it because the two results
are asymmetric.

**B13. §6 `key` sources: how the classes translate.** An entry error, body
error or missing key on the referenced key becomes a **resolution** error
(the §8.4 table says this only for body errors, and §6 says it for entry
errors). Request errors from limits pass through unchanged.

**B14. §6: base URI for a path that is not valid UTF-8.** POSIX paths are
bytes, but the spec says "the path's UTF-8 bytes". Node gives me a string,
so I encode that string as UTF-8. A non-UTF-8 path cannot be represented
faithfully.

**B15. §6 `file:` authority.** I accept only exactly `localhost`
(case-insensitive) or an empty or absent authority. `localhost:`, `@localhost`
and `LOCALHOST:80` are rejected. The spec does not mention port or userinfo.

**B16. §6: fragment-only references and pins.** `#x` resolves to the archive
itself, and pins on that source are checked against the archive file. This
follows from the text, but I state it because readers may special-case the
fragment.

**B17. §5.1: "The schema has no singular message fields".** That is true,
and Concat parts and pages are repeated. I still note that a `Range`
embedded in a `Concat` is decoded independently, so a malformed part makes
the whole payload malformed.

**B18. §5.2: zero-valued source fields on a literal range.** A literal range
that carries an explicitly encoded `source = 0` (`08 00 2a 00`) is valid on
decode, because the value is 0. Canonical encoders would not emit it. Byte
comparison of payloads therefore needs re-encoding, not raw-byte equality.

**B19. §9.1: rejections the list leaves out.** A writer "MUST produce a
valid archive", but the list does not name:
- a key longer than 65535 UTF-8 bytes (the ZIP name length is u16). I reject
  it.
- a Concat whose total size exceeds 2^64−1, or a range with
  `offset + length > 2^64−1`. I reject both.
- a JSON string with a lone surrogate, which is not valid UTF-8. I reject it.

**B20. §9.2: version fields.** "Version needed to extract" is 45 for records
with a ZIP64 extra field. The local header of the same entry has no ZIP64
field, so I write 20 there. For the zip64 EOCD's "version made by", I chose
45, since the spec gives only "version needed".

**B21. §4.3: odd placement of "Extra field blocks may appear in any order."**
The sentence sits in the middle of the reference-entry paragraph, but
presumably applies to all records (§4.1). I write the ZIP64 block first, then
the vzip block.

**B22. HARNESS queries: forms not covered.** The harness does not say what
to do with `list` without `prefix`, a `range` with only `start`, or
`get_raw` with a `range`. I treat all three as malformed queries (non-zero
exit). `start > end` is a request error inside the results, as the spec
requires.

**B23. HARNESS: class for unexpected internal errors.** There is no class
for them. I emit `"class": "internal"` so they are visible. None occurs in
my tests.

**B24. HARNESS `page_size`: grouping rule.** I start a new page once the
current page holds at least `page_size` bytes. Every page therefore has at
least one record and is at most one record larger than `page_size`.

---

## C. Things that were unnatural or hard to implement (TypeScript/Node)

1. **Detecting trailing bytes after DEFLATE.** `zlib.inflateRawSync` silently
   ignores data after the final block. The only way I found to detect it
   was the poorly documented `{info: true}` option and
   `engine.bytesWritten !== input.length`. A hand-written inflater would be
   the alternative.
2. **Distinguishing `1` from `1.0` in JSON (HARNESS).** `JSON.parse` loses
   this. I used the reviver's `context.source` (JSON.parse source text
   access, a recent V8 feature). Older runtimes would need a custom JSON
   parser.
3. **RFC 3986 validation and strict resolution.** The WHATWG `URL` class is
   not RFC 3986: it normalises, accepts invalid input and percent-encodes
   silently. I hand-wrote the URI-reference validator (including IPv6 and
   IPvFuture literals), the §5.2.2 resolver and §5.2.4 `remove_dot_segments`.
   This was the largest single piece of non-ZIP work, and the most likely
   place for implementations to diverge in edge cases, for example
   `http://[::1.2.3.4]/` or `a:b:c`.
4. **UTF-8 order everywhere.** JS string comparison uses UTF-16 order, so
   every sort, page search and prefix test compares encoded `Buffer`s.
   `TextDecoder` strips a leading BOM unless given `ignoreBOM: true`. The
   spec's warnings about both were helpful.
5. **64-bit integers.** Every protobuf integer and ZIP64 value is a `BigInt`.
   File offsets become `number`s only after bounds checks.
6. **Protobuf decoding rules.** These were easy to hand-write: the wire types,
   field number range, 10-byte varints, values above 2^64−1, uint32
   overflow, and last-wins for repeated fields and oneofs.
7. **Making the error order of §8.4 match.** This required threading the
   class through `key` sources, so that inner entry and body errors become
   resolution errors while request errors pass through.

---

## D. Decisions summary (quick reference)

| topic | decision |
|---|---|
| memory bound | 256 MiB per operation (`$VZIP_MAX_REQUEST_BYTES`), checked last |
| STORED body outside file | body error even if the window lies inside the file |
| format entry usize | not checked |
| list page set | pages whose `[first_key_i, first_key_{i+1})` intersects the prefix range |
| ZIP64 extra | APPNOTE field order, first 0x0001 block, too short means entry error |
| mtime pin | floor towards −∞ |
| raw of format entries | via comment |
| null in unknown description member | invalid |
| http | supported (fetch); 200 accepted; `/*` total with a size pin fails closed |
| URL allow-list (§10) | `allowedUrlPrefixes` option; default allows everything |
