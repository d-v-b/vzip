# SPEC_NOTES: vzip v1 draft rev 3, from a TypeScript implementation

This file lists every point where SPEC.md or HARNESS.md was ambiguous,
underspecified, surprising or hard to implement, and the choice this
implementation made for each. Items are grouped by section. The ones most
likely to cause disagreement between implementations are marked
**[interop]**.

Implementation layout: `src/proto.ts` (protobuf), `src/uri.ts` (RFC 3986),
`src/reader.ts`, `src/writer.ts`, `src/cli.ts`. Tests are in `test/`.

---

## §3 ZIP container

### 3.2-a [interop] A zip64 locator can be found where there isn't one
*Question:* "A reader MUST use the zip64 end of central directory record
whenever a zip64 locator immediately precedes the end of central directory
record". The 20 bytes before the EOCD of a non-ZIP64 archive are the tail of
the last central directory record. If that record's name or extra field holds
`PK\x06\x07` exactly 20 bytes from its end, a reader sees a "locator" in a
valid archive, follows a garbage offset, and reports an archive error. In a
paged archive the last record is a format entry with a fixed name, so this
can't happen there. In an unpaged archive the CD order is unconstrained, so a
body record can be last, and its key is chosen by the user.
*Choice:* I followed the text literally: signature at `eocd-20` means a
locator. My writer always puts the format records last, so it never produces
this case.
*Suggestion:* require the format entries' records to be last in every
archive (the paged layout already does), or tell readers to use the locator
only when the EOCD has an all-ones field, or to check that
`cd_offset + cd_size` lands on the zip64 record or the locator.

### 3.2-b Which zip64 EOCD fields does a reader take?
*Question:* When a locator is present, does the reader take every value from
the zip64 record, or only the values whose EOCD field is all ones (the
APPNOTE reading)? The two differ if a writer puts real values in the EOCD
that disagree with the zip64 record.
*Choice:* I take CD size and CD offset from the zip64 record. Entry counts
are never used.

### 3.2-c What "zip64 records lie within the file" means
*Choice:* I check that the 56 fixed bytes at the locator's offset are inside
the file and start with the right signature. I don't check that the record
ends before the locator, and I don't check its `size` field (44) or the
locator's disk count. The spec doesn't say whether readers must check these.

### 3.2-d Version made by of the zip64 EOCD record
§3.2 fixes "version needed to extract" (45) but not "version made by", and
§9.2's reproducibility advice (made-by 20) covers only the per-entry
records. I write 45. This affects byte-for-byte reproducibility between
writers.

### 3.4-a [interop] What "inflate cleanly" means: trailing data, unknown size
*Question:* The comment gives only the compressed size of a format entry, so
a reader that follows "read them through the comment" can't check the
uncompressed size or the CRC. Is a body whose DEFLATE stream ends before the
`sources_size`/`csize` bytes run out (trailing garbage) clean or not? The
same question applies to bytes entries ("does not inflate").
*Choice:* Strict. The DEFLATE stream must consume exactly `csize` bytes.
Otherwise it's an archive error for format entries and a body error for
bytes entries. A truncated stream is also an error. Implementations built on
zlib/`inflateRaw` without checking consumed input will accept trailing
garbage silently, so this needs to be pinned down.

### 3.4-b Files between 44 and 59 bytes long
Step 1 needs `file_size >= 60`. I skip step 1 for shorter files and go on to
step 2. That seems to be the intent, but the spec doesn't say so.

### 3.3-a What "valid UTF-8" means
I use RFC 3629, which rejects overlongs, encoded surrogates (CESU-8) and
values above U+10FFFF, via WHATWG `TextDecoder({fatal:true, ignoreBOM:true})`.
`ignoreBOM` is essential: the default TextDecoder strips a leading BOM,
exactly as §3.3 warns.

### 3.3-b Keys that can't be encoded at all
JSON query keys can contain lone surrogates (`"\ud800"`), which have no
UTF-8 encoding. The spec says a key is a Unicode string but says nothing
about requests for a non-scalar-value string. *Choice:* classify returns
`missing`, get returns *missing*, raw returns *missing*, and list returns
`[]`. The writer rejects such keys as "not valid UTF-8".

---

## §4 Entries

### 4.1-a [interop] ZIP64 extra field problems in a CD record
*Question:* A record whose local header offset is 0xFFFFFFFF but has no
`0x0001` block, or one too short to hold the value, can't be read. Is that an
entry error (it's "confined to a single record") or a body error? §8.4's list
of entry error causes doesn't mention it.
*Choice:* Entry error, raised by classify, get and raw. Also, when
`csize`/`usize` are 0xFFFFFFFF (forbidden by rule 7, but a reader may still
meet them), I parse the 0x0001 block per APPNOTE: values in the order usize,
csize, offset, and only for the fields that are all ones.

### 4.1-b Multiple 0x0001 blocks
Not addressed. I use the first one.

### 4.2-a [interop] A STORED bytes entry with `csize != usize`
§8.4 defines a body error as "inflates to a size other than its record's".
For method 0 nothing is inflated. *Choice:* a stored entry whose compressed
and uncompressed sizes differ is a body error. A reader that ignores `usize`
for stored entries returns `csize` bytes instead.

### 4.3-a 0x7A76 with an empty payload
An empty `Range` payload decodes to the source range (0, 0, 0). It is
malformed when the archive has no sources, even though its size is 0. That's
consistent with §5.2, but surprising. The empty value has to be written as
0x7A77 with an empty Concat, which is what §4.3 mandates anyway.

---

## §5 Messages

### 5.1-a Decoder rules I had to add explicitly
These are all covered by the text, but a hand-written decoder needs care
with each:
- the 10th varint byte may only be 0 or 1;
- the tag varint can exceed 2^32, giving field numbers above 2^29−1;
- `int64 modified_not_after` is a 64-bit two's complement varint. Negative
  values take 10 bytes. The encoder must emit it even when it is 0 (it's
  `optional`).

### 5.1-b A known field with the wrong wire type, where the field is reserved
Field 2 of `Range` is reserved and "skipped like any unknown field", so any
wire type except groups is accepted there. I implemented that.

### 5.2-a [interop] Zero-length ranges vs. payload validation (§5.2 vs §8.3 step 4)
§8.3 step 4 says zero-length ranges "are not resolved and cause no error",
but §5.2 makes a source range with `source >= number of sources` malformed.
I read §5.2 as winning (step 1, decoding, comes first). So a zero-length
range with an out-of-bounds source is a payload error. The wording of step 4
("no error ... even if their source is missing") could be read the other
way. Worth one sentence of clarification.

---

## §6 Source table

### 6-a URL syntax checked at open or at resolution?
At open, only an *empty* `url` is an archive error. A non-empty url that
isn't a URI-reference is a resolution error (§6, "It is a resolution error if
the reference is not a valid URI reference"). Writers must reject both. I
implemented that asymmetry, but it is surprising: one bad url and an empty
url are classified differently.

### 6-b [interop] `%2E%2E` path segments escape `remove_dot_segments`
RFC 3986 §5.2.4 removes only literal `.` and `..`. A url like
`%2E%2E/%2E%2E/etc/passwd` survives resolution. After percent-decoding, the
OS sees `../../etc/passwd`. The `file:` rules forbid a decoded `%2F` and NUL
but not decoded dot segments. *Choice:* I follow the text: decode and open
whatever results, so the OS interprets `..`. Implementations that normalise
percent-encoded unreserved characters before resolution (RFC 3986 §6.2.2.2)
will compute different paths. This is also a security note for §10.

### 6-c `file:` URIs with no authority
"its authority MUST be empty or `localhost`": `file:/abs/path` has no
authority at all (undefined, as opposed to empty). *Choice:* treated as
empty, so it is accepted.

### 6-d `file:` authority comparisons
Is `localhost:` (empty port) or `user@localhost` acceptable? *Choice:* no.
The authority must be exactly empty or case-insensitively `localhost`.

### 6-e Base URI normalisation details
- "`.` and `..` segments are removed lexically". Does normalisation also
  collapse `//` and drop a trailing `/`? I used Node's `path.resolve`, which
  does both.
- "the path's UTF-8 bytes": POSIX paths are byte strings and may not be
  UTF-8. In Node, `process.argv` is already decoded as UTF-8 (lossily), so a
  non-UTF-8 archive path can't be represented. Not fixable from the spec
  side, but worth noting.
- `process.cwd()` in Node uses `getcwd`, which matches the spec.

### 6-f Fragment-only references and empty references
`#x` resolves to the archive itself, but `""` (which RFC 3986 also resolves
to the base) is forbidden as "empty url". Consistent, but it would read
better if the spec said so explicitly.

### 6-g Query on `file:` URIs
`d.bin?` (empty query) is forbidden. A url with a query relative to a
`file:` base is also forbidden. Note that resolving `?q` against
`file:///x/a.vzip` gives `file:///x/a.vzip?q`, which is a resolution error,
while `#q` is fine.

### 6.1-a [interop] `modified_not_after` and file mtimes before 1970
"Truncated to whole seconds": truncation toward zero and floor differ for
negative mtimes. *Choice:* truncation toward zero (`mtimeNs / 1e9n` with
BigInt division).

### 6.1-b What counts as a strong etag
§6.1 says "a quoted string". I implemented RFC 9110 `entity-tag` without
`W/`: `DQUOTE *etagc DQUOTE`, where etagc is `%x21 / %x23-7E / obs-text`. I
check it on the UTF-8 bytes, so any non-ASCII character is allowed as
obs-text. `""` (empty opaque tag) is accepted. Implementations that use a
simpler "starts and ends with `"`" test will accept `"a"b"`. Please spell
out the grammar.

### 6.1-c HTTP size pin with a 200 response
If the server ignores `Range` and returns 200, there is no `Content-Range`.
*Choice:* the total size is the body length, and the reader slices the body.
The spec only describes the 206 case.

### 6.1-d Pins on a range that doesn't overlap the window
Pins are checked only when bytes are actually read from the source, per §6.1
"Before returning any byte read from a pinned source". No I/O, no check. I
check pins on every read (no caching), which is allowed.

---

## §7 Page index

### 7.1-a How `page_size` maps to pages
HARNESS: "a page SHOULD hold about `page_size` bytes". *Choice:* records are
grouped greedily. A new page starts when adding the next record would push
the page over `page_size`, so a single record larger than `page_size` gets a
page to itself.

### 7.2-a [interop] Is a record in the "wrong" page present?
A page can hold a record whose name sorts outside
`[first_key_i, first_key_{i+1})` (an invalid archive that readers needn't
detect). §7.2 lookup never finds it. For `list`, "present is as defined by
§7.2", so *choice:* such records are not listed. A reader that lists every
record of each page it reads would list them.

### 7.2-b [interop] Class of a `list` failure
§8.2: "`list` ... fails if one of [the pages] cannot be parsed". No class is
given. *Choice:* `entry`, matching §8.4's "the page that holds the key's
record cannot be parsed".

### 7.2-c Which pages `list(prefix)` must read
"every page whose key range can contain a key with that prefix". I compute
page `i`'s range as `[first_key_i, first_key_{i+1})` and read it iff the
range intersects the set of strings with that prefix. A reader that reads
more pages may fail where I succeed (an unparseable page outside the prefix
range). The spec implies that failing there is not allowed, but it's subtle.

### 7.2-d What "page cannot be parsed" means
Not defined. *Choice:* the same rule as the unpaged CD in §8.1: a sequence
of records with correct signatures and variable-length fields inside the
page, ending exactly at the page end. Invalid UTF-8 names inside a page are
not parse failures.

### 7.2-e Page index checks not possible at open
"the last page ends where the format entries' records begin" can't be
checked without parsing the CD. It isn't in the open checklist, so I don't
check it. A `first_key` that doesn't match the page's first record is also
unchecked.

### 7.2-f Pinned entries vs. page entries
A pinned key wins over whatever its page says, so a pinned key is never an
entry error. Pinned keys are listed by `list` even if they don't appear in
any page.

---

## §8 Reading

### 8.1-a Unpaged duplicates and the EOCD entry count
Duplicate names (unspecified): the *first* record wins. A mismatch between
the EOCD entry count and the number of parsed records isn't an error,
because "MUST NOT report an error at open that this section does not list".

### 8.2-a `raw` on format entries
`raw("__vz__/sources")` inflates the comment-located body, without a size
check, since no size is known. `raw("__vz__/index")` in an unpaged archive
is *missing*.

### 8.2-b Body errors outside the requested window
For a bytes entry, `get(key, range)` on a DEFLATE body inflates the whole
body, so a corruption after the window still gives a body error. For a STORED
body I read only the window, but I still require the *whole* body to lie
within the file. Readers that stream-inflate only up to the window would
return bytes where I report a body error. The same applies to `key` sources.
The spec doesn't say whether body errors are checked for the whole body or
only for the bytes needed. **[interop]**

### 8.4-a [interop] Where resource-limit request errors go in the check order
"Each operation checks in this order ... 1. the request (request error)". A
resource-limit request error can only be detected once the value size is
known: after lookup for bytes entries, and after decoding the payload for
references. *Choice:* checks run in this order:
1. `start > end`
2. hidden
3. lookup / entry error
4. for bytes entries, body outside the file → body error, then window or inflated size over the limit → request error, then inflate/size → body error
5. for references, payload error, then window size over the limit → request error, then resolution

### 8.4-b The resource limit itself
§10 says readers SHOULD bound memory and document the bound. Mine is 1 GiB
(`MAX_REQUEST_BYTES` in `src/util.ts`) per request and per inflated body.
A conformance suite that fetches a large whole value (for example a 2^40-byte
reference to a missing file) can't expect a particular class: a reader with
no limit reports `resolution`, mine reports `request`.

### 8.4-c Errors that fit no class
An inflated format entry over the limit at open is reported as an archive
error. An unexpected internal exception in the CLI is reported as class
`internal` (not in the HARNESS list). It should never happen.

---

## §9 Writing

### 9.1-a Local entry order and CD order
The spec doesn't constrain local order except "`__vz__/index` MUST follow
every entry it indexes or pins". Mine is: non-pinned entries in description
order, `__vz__/sources`, pinned entries, `__vz__/index`, then CD (body
records sorted, then sources, then index). The CD is sorted in unpaged
archives too.

### 9.1-b A writer can't check every "valid archive" property cheaply
For example, a `key` source naming a hidden bytes entry is allowed, and
nothing requires the writer to check that the named entry's bytes cover the
ranges used. Ranges past the end of a `key` or `data` source are accepted by
the writer and fail at read time. That's consistent with the spec, but
writers could easily reject out-of-bounds ranges on `data`/`key` sources.
Should they?

### 9.1-c A value of 0 vs. absent
`compress: false` on a reference entry is allowed. Only `true` is invalid.

---

## HARNESS.md

### H-a `null` for optional members
Only `page_size` is documented as nullable. *Choice:* `null` for any other
member (`compress`, `pinned`, `mirror`, pins, `sources`, `entries`) is
invalid. In queries, `"range": null` is treated as absent (whole value).

### H-b Integer syntax
"numbers are JSON integers (`1.0` is invalid)". *Choice:* only
`-?(0|[1-9][0-9]*)` source text counts as an integer. `1e3` and `1.0` are
invalid. This needs JSON source access (`JSON.parse` reviver `context.source`
in Node ≥ 21), which many JSON libraries don't offer, so some
implementations may accept `1.0`. Integers are parsed exactly as BigInt, so
values ≥ 2^53 work even though the harness promises not to send them.
Negative numbers are invalid everywhere except `modified_not_after` (int64).

### H-c Malformed queries
A range object with an unknown form (for example only `start`), a missing
`key`, or an unknown `op` makes the queries file "invalid", so the CLI exits
non-zero and no results are printed. The alternative would be to fail only
that query. The harness only implies the exit-code behaviour.

### H-d Pins with `null` and pin validation in the writer
`etag` is validated as a strong tag. `size` and `modified_not_after` must be
integers.

### H-e Base URI for `read`
"its base URI is the `file:` URI of its absolute path". This implementation
uses `getcwd` + lexical normalisation, as in §6. If the runner passes a path
through a symlinked directory, implementations that `realpath` will differ.
The spec says not to resolve symlinks, and I don't.

### H-f The output directory
"do not create a file at `<out-path>`": I validate fully before opening the
file, open it with `O_EXCL` (`wx`), and delete it if a write fails.

---

## Things that were unnatural or unexpectedly hard

- **Distinguishing `1` from `1.0` in JSON** (H-b): this needs the
  source-text reviver.
- **BOM handling**: the default `TextDecoder` strips a leading U+FEFF. You
  must pass `ignoreBOM: true`.
- **UTF-8 order**: JS string comparison is UTF-16 order. Every comparison
  goes through `Buffer.compare` on UTF-8 bytes.
- **u64 arithmetic**: offsets and sizes are BigInt throughout, which makes
  the code noisy but avoids silent precision loss above 2^53.
- **RFC 3986 URI-reference validation**: this needs a full grammar
  (including IPv6 literals). There's no ready-made strict validator in Node,
  and `new URL()` is WHATWG, which is not strict RFC 3986 (it normalises,
  accepts spaces, and so on).
- **Strict end-of-stream detection for DEFLATE**: this needs
  `inflateRawSync(..., {info: true})` and `engine.bytesWritten` to see how
  much input was consumed.
- **Testing ZIP64 offsets ≥ 4 GiB**: I did it with a test-only writer option
  that inserts a sparse 4 GiB gap after the first entry (`testGap`). The
  spec allows gaps between entries; `unzip -t` accepts them.
