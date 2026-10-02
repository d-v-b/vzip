# Spec revision log

Each entry records what changed in SPEC.md, and what exposed the problem.
Snapshots of the spec given to each round's implementers are in
`spec_history/`.

## Before round 1 (found while building the conformance kit)

| § | problem | fix | found by |
|---|---|---|---|
| 5.1/5.2 | An empty literal `data` was indistinguishable from "no data": the encoding rules say empty defaults are omitted | `Range.data` is `optional bytes` (explicit presence); encoders emit set optional fields even when empty | writing the model |
| 5.1 | Behaviour for a known field arriving with the wrong wire type was unspecified | Malformed; strings must be valid UTF-8 | writing the reference decoder |
| 5.2/8.2 | "Source shorter than offset+length is an error" was impossible to enforce for partial reads of external objects without extra I/O | Define "out of bounds"; MUST error only if a returned byte lies past the end, MAY error otherwise | writing the model |
| 8.1 | Was `start > end` on a missing key an error or "missing"? | Error, whether or not the key is present | writing the model |
| 7.2 | The lookup rule made `__vz__/sources` and `__vz__/index` (trailer records, in no page) look missing | Readers know both entries from the archive comment | conformance self-test of the reference implementation |

## Round 1 (spec r1 → r2)

**Result:** all three implementations (Rust, TypeScript, Python) passed every
graded check on their first attempt:
- 3570/3570 read queries;
- 6/6 valid write cases;
- 11/11 rejections;
- 14120/14120 cross-read queries.

The spec was implementable and interoperable for well-formed archives. The
divergence report found **11 queries** where they gave different answers, each
allowed by the spec. All three agents' notes put "conforming readers can
disagree" first. Revision 2 removes that latitude.

| § (r1) | problem | r2 fix | evidence |
|---|---|---|---|
| 3.1, 8.2 | "MAY reject" vs "MUST report"; errors "at open or later" | One error model: §8.4 defines five classes (archive, entry, payload, resolution, request), each with the operations it fails. §8.6 lists the writer violations readers need not detect. | All 3 notes ranked this #1. Divergence: truncated payload / wire-type mismatch / long varint failed at open in ref, per query in the others. |
| 5.2 | Out-of-bounds: "MUST if a returned byte is past the end, MAY otherwise" | Exact rule: a resolved read fails iff the source is shorter than the bytes needed (§8.3). | Divergence: `r/short` range(1,3) gave bytes from ref/ts/py and an error from rust. |
| 5.2, 6, 8 | Are parts outside the request, or zero-length parts, validated? | Payload errors (decode, source index, literal fields) apply to every get. Resolution errors only for ranges overlapping the window. Zero-length ranges are never resolved. | All 3 notes (TS #3, Py A3, Rust A7). |
| 3.1 rule 3 | Contradiction: no local extra field, but ZIP64 sizes need one | Entry sizes must be < 0xFFFFFFFF; local ZIP64 is never needed. | All 3 notes (the planted flaw was found by all three). |
| 3.1 | ZIP64 threshold ("does not fit") and which end-record fields get sentinels | ZIP64 iff a value is ≥ the all-ones value; only overflowing fields get sentinels; readers use the zip64 record whenever the locator is present. | All 3 notes. |
| 4.3 | 65531-byte payload limit ignores a co-resident ZIP64 block | The whole extra field must be ≤ 65535. | All 3 notes. |
| 4.3, 8.2 | Reference body: check it or not? | Readers never check it; raw view returns it as stored. | Divergence: body ≠ payload, open failed in rust/py, not in ts/ref. |
| 4.3 | DEFLATE reference entry: open error or per key? | Entry error. | Divergence: open failed in rust/ts/py, not in ref. |
| 3.4 | Non-vzip ZIP: reject, or read as all-bytes | Reject (archive error); also any magic other than `vzip/1`. | Divergence: ref read as bytes; rust/ts/py rejected. |
| 3.4 | Locating the EOCD with a binary comment (backward scan can hit `PK\5\6` inside it) | Fixed positions: size−60 (38-byte comment) or size−44 (22-byte). | Rust A9, TS #21. |
| 3.4 | Cross-check comment vs central directory? | Readers use the comment; mismatch is in §8.6 (unspecified). | TS #11. |
| 5.1 | uint32 overflow, field numbers 0 / > 2^29−1, 10-byte varints > 2^64−1, non-minimal varints, field order | The first three are malformed. Non-minimal varints and any field order are accepted. Encoders use minimal varints. | Divergence: `source = 2^32` was truncated by ref, rejected by others. |
| 6 | Empty `url`: open error or resolution error | Archive error at open. | Divergence: open failed in rust/ts/py, not in ref. |
| 6 | URL details: percent-encoding, strict vs non-strict resolution, symlinks in base, `file:` host/query/relative path | `url` is a percent-encoded URI reference with strict RFC 3986 §5.2.2 resolution. The base is the lexically-normalised absolute path, symlinks not resolved. `file:` must have an empty or `localhost` authority, an absolute path and no query; the fragment is ignored. Anything else is a resolution error. | All 3 notes. The relative `file:` path rule was found while writing r2 test vectors. |
| 6 | `key` source naming `__vz__/sources` / `__vz__/index` | Writer must reject; reader resolution error. | Py C1, TS #18. |
| 7 | Validating §7.1 needs the whole CD, which defeats the index | Lookup algorithm normative (§7.2); index/CD consistency in §8.6. | Rust B1, TS #13. |
| 7 | `__vz__/index` with a 22-byte comment; pinned rules (duplicates, hidden, format entries) | Archive error; pinned: bytes entries only, not format entries, no duplicates. | All 3 notes. |
| 7, 9 | Layout circularity (index before indexed entries) | `__vz__/index` MUST follow every entry it indexes. | Py B9. |
| 9 | "Informative except where marked", nothing marked | §9.1 normative requirements, §9.2 informative layout. | TS #24. |
| — | No security considerations | §10: `file:` access, keys are not paths, size bounds. | Rust B5. |
| HARNESS | missing `source` default; `compress` on references; mixed range forms; `page_size` 0; pre-existing output; JSON number range | All specified. | All 3 notes. |

## Round 2 (spec r2 → r3)

**Result:** all three fresh implementations passed every graded check:
- 3829/3829 read queries;
- 6/6 write cases;
- 18/18 rejections;
- 14984/14984 cross-read queries.

The divergence report was **empty**: on every tested query, all four
implementations gave the same answer (round 1: 11 divergences). The notes
moved from "readers may disagree" to the edges: corrupt containers, the base
URI string, the order of checks.

r3 also adds **source pins** (§6.1), from the study of icechunk's
`etag_checksum` / `last_updated_at_checksum` ([proposals/source-pins.md](proposals/source-pins.md)).

| § (r2) | problem | r3 fix | evidence |
|---|---|---|---|
| 8.4 | "In a paged archive, a record's problems surface as entry errors" implied archive errors in unpaged ones, contradicting the entry-error row | A record's own problems are entry errors in every archive. §8.1 lists exactly what open checks. | Py #1 (rust and py both chose entry errors anyway) |
| 8.1, 8.4 | "Bad end records" / "malformed page index" undefined; §8.1 forbids unlisted open errors | Both enumerated: end-record checks, format bodies within the file, CD structure for unpaged; page index decode, contiguity, bounds, increasing `first_key`, pinned rules | All 3 notes |
| 8.4 | Corrupt DEFLATE, body past EOF, inflated-size mismatch had no class | New class: **body error** (get/raw fail; a `key` source naming it is a resolution error) | All 3 notes |
| 8.4 | Order of checks (hidden + entry error, hidden + `start > end`) | Normative order: request, hidden, lookup/entry, body, payload, ranges | Rust #6, TS #8 |
| 3.3 | Records with empty / non-UTF-8 names unclassified | Ignored: they name no key | All 3 notes |
| 3.3 | UTF-16 vs UTF-8 sort order; `TextDecoder` stripping a BOM | Keys are byte strings, with examples; conformance keys 😀 / ～ / U+FFFF / BOM | TS #7 (both pitfalls hit during implementation) |
| 3.4 | Fall back from the −60 probe to −44 when the magic is wrong? | No fallback. A note shows the −60 probe can't match a fake record in a valid archive. | Rust #7, TS (proof in its notes) |
| 3.2 | zip64 EOCD "version 1"; disk numbers, versions | Exact record size/version; readers ignore disk numbers, versions, times, attributes | Py, TS minor |
| 4.3 | Payload limit depended on where the entry lands (ZIP64 block) | Fixed limit of 65519 bytes | All 3 notes |
| 3.1, 9.1 | Rule 7 limits compressed size too; §9.1 listed only uncompressed | Both listed | Rust #9 |
| 6 | Base URI string (encoding, `file:///` vs `file:/`), `getcwd` vs `$PWD` | Exact construction: `getcwd`, lexical normalisation, `file://` + percent-encoding of everything except unreserved/sub-delims/`:@/` | Rust #3, Py #2, TS #1 |
| 6 | What is a valid URI reference (non-ASCII, `1a:b`, `%zz`); writers not required to validate | Exactly RFC 3986 `URI-reference`, ASCII only; writers must reject others | All 3 notes |
| 6 | `file:` edge cases: empty `?`, `%2F`, case of scheme/`localhost` | Specified | Rust #4 |
| 5.1 | Library-based implementations vs the strict rules (groups, uint32, UTF-8); reserved field 2; message merging | Notes added; field 2 is skipped; no singular message fields, so merging never applies | Py #4 |
| 7 | Pinned hidden entries: spec allowed, harness forbade | Allowed in both | Rust, TS |
| 8.4, 10 | A reader's memory bound had no class | Request error for a documented limit | TS #3 |
| HARNESS | Error class not reported; defaults; JSON types; `{}` range with no sources | Errors carry `class`, and the runner checks it; defaults and strict types specified | All 3 notes |

The round-2 implementations were also run against the r3 suite (in
`rounds/r2/results_vs_r3`); see the round 3 entry.

## Round 3 (spec r3 → r4)

**Result:** all three fresh implementations passed every graded check:
- 5031/5031 read queries;
- 9/9 write cases;
- 28/28 rejections;
- 19676/19676 cross-read queries.

The divergence report was empty again. That includes the new pins, and the
stale-source use case from the icechunk study.

**The round found a bug in the test kit, not the spec.** The Python agent
resolved `data/blob.bin?` (an empty query) to a URL that has a query, and so
reported a resolution error. That is what RFC 3986 §5.2.2 requires. The
reference implementation and the model both used Python's
`urllib.parse.urljoin`, which silently drops an empty query. The reference
now has its own RFC 3986 resolver, which passes all §5.4 examples, and the
model was fixed. The Rust and TypeScript agents had it right too.

The notes reached deep edge cases. Two were real defects:

| § (r3) | problem | r4 fix | evidence |
|---|---|---|---|
| 3.2 | **False zip64 locator.** "Use the zip64 record whenever a locator precedes the EOCD" misreads a valid unpaged archive whose last record's name contains `PK\x06\x07` at that position | Zip64 records are used iff an EOCD count/size/offset is all ones (APPNOTE's convention); otherwise the preceding bytes are not examined. All four values come from the zip64 record. | TS #1. Against r4, **all three r3 implementations reject the valid archive** (`crafted/false_zip64_locator_signature`). |
| 6 | **Encoded dot segments.** `%2E%2E` survives dot-segment removal and becomes `..` after decoding: path traversal | A decoded `.` or `..` segment is a resolution error | TS #9. Against r4, **all three r3 implementations follow the traversal** (`r/encoded_dotdot`). |
| 8.1, 8.4 | "Inflate cleanly" undefined (trailing bytes, truncation); corruption outside a small window | Defined: one complete stream filling the body exactly. DEFLATE bodies are always inflated in full. | All 3 notes. The r3 implementations already agreed. |
| 8.4 | STORED entry with csize ≠ usize; offset 0xFFFFFFFF without a ZIP64 block | Body error; entry error | Rust #4, TS #7/#8, Py #6 |
| 8.4 | A resource limit vs the check order | A limit may be reported at any step | All 3 notes |
| 8.2, 7.2 | `list`: records outside their page's range, pinned keys, class of a page failure, "cannot be parsed" | Specified (lookup semantics; entry error; parse definition) | All 3 notes |
| 5.2, 8.4 | A zero-length range with an out-of-bounds source: payload error? | Yes: payload checks cover every range | TS #6 |
| 6, 6.1 | `file:/x` (absent authority); `//` in the base path; strong ETag grammar; pre-epoch rounding | Absent authority accepted; empty segments removed; ETag = `DQUOTE *etagc DQUOTE`, ASCII; times round down | Rust #6/#9, Py #10, TS #9 |
| HARNESS | `null` members; malformed query objects | `null` only for `page_size`; a malformed query invalidates the file (exit non-zero) | TS #10, Py |

Before round 4, the r3 implementations were run against the r4 suite. They
failed exactly the two defect fixes above and nothing else, so every other r4
change made explicit what the implementations already did.

## Convergence

| round | spec | implementations passing everything | divergent queries | what the notes were about |
|---|---|---|---|---|
| 1 | r1 | 3/3 | 11 | "conforming readers can disagree"; one contradiction (planted) |
| 2 | r2 | 3/3 | 0 | edges of the error model, base URI, check order |
| 3 | r3 | 3/3 | 0 | deep edges; two real defects (zip64 locator, encoded dot segments) |
| 4 | r4 | 3/3 | 0 | internal consistency of the text; HTTP and ZIP64 corner cases |

## Round 4 (spec r4 → r4.1)

**Result:** all three fresh implementations passed every graded check:
- 5129/5129 read queries;
- 9/9 write cases;
- 32/32 rejections;
- 20000/20000 cross-read queries.

The divergence report was empty. All three got both r4 fixes right: they
reject encoded dot segments and ignore a false zip64 locator.

r4.1 is one editorial fix. §6.1 said "rounded down" in the pin table but
"truncated" in the `file:` column; these differ before 1970, and Python #1
and TypeScript #4 both noticed.

**Open issues** reported in round 4, deferred: none produced a divergence on
the tested surface.

- **Text consistency:**
  - §8.1 lists "offset 0xFFFFFFFF without a ZIP64 block" as an entry error,
    but the §8.4 table doesn't.
  - Does `classify` fail on entry errors outside §4.1 (method, bit 0)?
    All three said yes.
- **ZIP64 extra-block parsing:** field order when a size is also all ones,
  blocks that are too short, duplicate `0x0001` blocks.
- **`list` with a page index:** define a page's key range exactly as
  `[first_key_i, first_key_{i+1})`, and say whether hidden-only pages are
  read.
- **Empty strings:** forbid an empty `first_key` and an empty pinned `key`.
- **Body errors:**
  - Do they apply to `raw` of reference entries?
  - STORED body partly outside the file: is it checked against the window or
    the whole extent?
  - Format entries' uncompressed size is never checked.
- **Resource limits:** "may be reported at any step" makes the error class
  implementation-dependent. Limits should be kept out of conformance.
- **Writer rejections in §9.1:** add `offset + length` overflow, Concat size
  overflow, keys over 65535 bytes, and lone surrogates.
- **HTTP pins:** a 200 that ignores Range, `Content-Range: bytes …/*`, 416,
  and pin times not representable as an HTTP-date.
- **HARNESS:**
  - `null` in unknown members;
  - a negative `modified_not_after` vs "all numbers are non-negative";
  - `compress: false` on reference entries.
- **Test-kit gap:** no HTTP cases. The reference reader had a
  double-percent-encoding bug for `http(s)` URLs that only showed up against a
  real server (see FINDINGS.md, "Real data").


## Revision 5: versioning and HTTP (requested)

Format version 0, specification revision 5.

**Versioning (§1.3).** vzip now has an integer **format version**, carried in
the archive's magic as `vzip/<N>`. This document specifies version **0**, so
the magic changes from `vzip/1` to `vzip/0` and the protobuf package from
`vzip.v1` to `vzip.v0`.

- **Revisions vs versions:** the document has revisions. A revision may
  clarify, resolve an ambiguity, fix a contradiction or add tests, but must
  not change the result of any operation that was already fully determined.
  Anything else is a new format version.
- **Why new fields mean a new version:** readers skip unknown protobuf
  fields, so a field added within a version (a new pin, say) would be
  silently ignored by older readers, and "fail closed" would become "fail
  open".
- **Readers** reject versions they don't implement, as an archive error.
- **Writers** should write the lowest version that can express the archive.
- **Version 0 is a draft:** until it is declared final, revisions may break
  the rule above, and this log says when they do. **Revision 5 does**: the
  magic changed.

**HTTP (§6.2).** Reading over HTTP is now specified:

- **Requests:** a GET with `Range` and `Accept-Encoding: identity`, and no
  HEAD or whole-object requests. Pins ride on those requests. Nearby ranges
  may be combined into one request.
- **Responses:** 206 with a total size; 206 with `*` (the size is unknown, so
  a `size` pin fails); 200 (the server ignored Range, so take the bytes from
  the body); 412 (a pin failed); 416 (the object is too short); any other
  status, or a non-identity `Content-Encoding` (resolution error).
- **Redirects and timestamps:** redirects may be followed, up to 5. A pin time
  that cannot be written as an HTTP-date is a resolution error.

**Round-4 open issues resolved:**
- the entry-error list now agrees between §8.1 and §8.4;
- ZIP64 extra-block parsing is specified (APPNOTE order; too short or
  duplicated → entry error);
- a page's key range is defined, along with which pages `list` reads;
- an empty `first_key` or pinned key makes the page index malformed;
- body errors apply to the whole extent of a body, and to `raw` of reference
  entries;
- format entries' uncompressed sizes are not checked;
- resource limits are kept out of conformance;
- §9.1 gains offset/size overflow and keys over 65535 bytes;
- readers ignore entry counts;
- URL syntax is checked at resolution;
- HARNESS: unknown members are ignored even if `null`, negative
  `modified_not_after` is allowed, and `compress: false` is allowed on
  references.

**Conformance kit:**
- **HTTP profile, optional, reported separately.**
  [`http_server.py`](http_server.py) is a local range server with strong
  ETags, Last-Modified, conditional requests, a request log and three quirk
  modes (`/norange/`, `/nototal/`, `/gzip/`). The `http_basic` and
  `http_paged` cases cover:
  - plain, percent-encoded and non-ASCII names, and a 404;
  - each pin passing and failing;
  - the quirks;
  - a clipped range and a 416.
- **Request accounting** checks, from the server's log:
  - at most one GET per resolved range, and none for `classify`;
  - no HEAD;
  - `Range` and `Accept-Encoding: identity` on every request;
  - `If-Match` and `If-Unmodified-Since` on pinned reads.
- **The HTTP cases immediately caught two bugs in the reference.** Its
  unpinned HTTP reads went through obstore, which rejects
  `Content-Range: …/*` and sends no `Accept-Encoding`. The reference now uses
  a small §6.2 reader for every `http(s)` read.
- `proto/vzip.proto` is now generated from the spec's Appendix A.

## Round 5 (spec r5 → r6)

**Result:** all three fresh implementations passed every graded check,
including the new HTTP profile:
- 5129/5129 read queries;
- 11/11 write cases;
- 32/32 rejections;
- 20000/20000 cross-reads;
- 1876/1876 HTTP checks, including request accounting.

The divergence report was empty.

**All three agents independently found the same design flaw in §6.2: pins
failed open.** The only failure signal for `etag` and `modified_not_after`
was a 412 response. A server or proxy that ignores conditional headers
answers 200 or 206, and the pin was never checked. The Python agent
demonstrated this against Python's stock `http.server`.

| § (r5) | problem | r6 fix | evidence |
|---|---|---|---|
| 6.2 | Pins fail open when a server ignores `If-Match`/`If-Unmodified-Since` | Readers check every successful response's `ETag` and `Last-Modified` against the pins; a missing or unparseable header is a resolution error | All 3 notes (Py #1, Rust #2, TS #3). Against r6, the r5 implementations fail exactly `h/nocond_bad_etag`, `h/nocond_bad_mtime` and `h/noetag_pinned`, and nothing else. |
| 6.2 | Redirects were "MAY follow": readers could disagree | MUST follow 301/302/303/307/308, up to 5; `Location` resolved per RFC 3986; `http(s)` only; headers re-sent; pins apply to the final response | TS #1, Py #2, Rust #7 |
| 3.4 | The note that the step-1 probe can't match a fake record "relies on disk numbers, which readers ignore" | The note now says it holds for valid (single-disk) archives, and that an invalid one is rejected, as intended | Py (built such a file) |

**Kit:** the HTTP server gained three quirk modes: `/nocond/` (ignores
conditional headers), `/noetag/` (no `ETag` or `Last-Modified`) and
`/redirect/N/`. There are seven new HTTP cases.

**A bug in the kit itself:** in the first run, `/noetag/` crashed the server
whenever a request carried `If-Match`, so `h/noetag_pinned` passed vacuously
for every reader. It was fixed before the results above.


**Open issues** from round 5, deferred:
- **HTTP response edge cases:**
  - a 206 without `Content-Range`, or with a multipart body;
  - a body whose length doesn't match its `Content-Range`;
  - chunked transfer encoding (all three implementations already treat the
    first two as resolution errors).
- **`list` on paged archives:** state the prefix-successor computation
  explicitly. Also, `classify` of an absent key whose page is broken: missing
  or entry error? (Rust chose entry error.)
- **ZIP64:** all-ones size fields in invalid archives.
- **"Lies within the file":** whether regions must also come before the end
  records.
- **`raw` of the format entries:** read through the comment or the CD record?
  All three use the comment.
- **§9.1:** whether writers must reject `key`/`data` source ranges past the
  end of a known value.
- **§5.1:** say explicitly that empty repeated message elements are emitted,
  and give the encoding of a negative `int64`.
- **§9.1:** the stated reason for putting the index last really only applies
  to pinned entries.
- **HARNESS:** whether `list` requires `prefix`; `range` on operations other
  than `get`; duplicate JSON members.

## Round 6 (spec r6)

**Result:** all three fresh implementations passed every graded check,
including the HTTP profile:
- 5129/5129 read queries;
- 11/11 write cases;
- 32/32 rejections;
- 20000/20000 cross-reads;
- 2506/2506 HTTP checks.

The divergence report was empty. All three implemented the r6 pin rules
correctly from the spec alone, without being told about the flaw: pins stay
closed when a server ignores conditional headers or sends no `ETag`, and
redirects are followed up to 5, `http(s)` only.

## Convergence (updated)

| round | spec | passing everything | divergent queries | what the notes were about |
|---|---|---|---|---|
| 1 | r1 | 3/3 | 11 | "conforming readers can disagree"; one contradiction (planted) |
| 2 | r2 | 3/3 | 0 | edges of the error model, base URI, check order |
| 3 | r3 | 3/3 | 0 | deep edges; two real defects (zip64 locator, encoded dot segments) |
| 4 | r4 | 3/3 | 0 | internal consistency; HTTP and ZIP64 corner cases |
| 5 | r5 + HTTP | 3/3 | 0 | one design flaw: pins failed open over HTTP |
| 6 | r6 + HTTP | 3/3 | 0 | details: HTTP-date formats, malformed 206s, `raw` of format entries |

**Open issues** from round 6, deferred. Several were raised by all three
agents, and none caused a divergence:

- **Format entries in the raw view.** Does `raw("__vz__/sources")` read through
  the comment or through the central directory record? All three chose the
  comment. Codify that.
- **What counts as a valid HTTP-date.** RFC 9110 requires accepting two
  obsolete formats. In one of them (RFC 850) a two-digit year is resolved
  against the reader's clock, so results can depend on when the reader runs.
  Rust and TypeScript flagged this as their #1 or #3 issue. Proposal: accept
  IMF-fixdate only, and treat anything else as uncheckable.
- **ZIP64 blocks.** How to read one when size fields are also all ones, and
  what to do with duplicate blocks when the offset isn't all ones.
- **Missing keys on a broken page.** A missing key whose page can't be parsed:
  `missing`, or an entry error? Rust chose entry error.
- **Paged `list`.** Give the exact interval test for which pages
  `list(prefix)` reads, with a worked example. All three asked for this.
- **Oversized payloads.** A payload between 65520 and 65531 bytes fits in the
  extra field but isn't classified; TypeScript accepts it.
- **Unhandled HTTP cases:**
  - a 206 whose body length or total is inconsistent, or which is multipart;
  - a redirect without a `Location` header;
  - a `Content-Encoding` list such as `identity, identity`;
  - URLs with userinfo or an empty host;
  - `localhost` with a port.
- **Literal ranges.** One with an explicitly encoded zero `source`: is that
  checked by value or by field presence? Python checks the value.
- **Empty `key` source.** An empty `url` is an open error, but an empty `key`
  isn't classified at open.
- **Memory.** Accepting a 200 response means downloading the whole object, so
  the limit needs guidance.
- **HARNESS:**
  - `get_raw` with a `range`;
  - unknown members inside queries;
  - `range: null`;
  - the overflow rejections, which the CLI's JSON number limit makes
    unreachable.

## Revision 7: the open issues from rounds 5 and 6

Format version 0, specification revision 7.

**§1.3 note: this revision breaks the revision rule.** Several of its choices
are ones none of the round-6 implementations used (listed below). That is
allowed only because version 0 is still a draft. After version 0 is final,
changes like these would need version 1.

| issue | r7 decision | already matched r6 implementations? |
|---|---|---|
| HTTP-date formats for `Last-Modified` (RFC 850 two-digit years depend on the reader's clock) | IMF-fixdate only, correct weekday; anything else → the pin can't be checked (resolution error) | **No**: all three accepted the obsolete formats (`h/oldate_pinned`) |
| 206 with no `Content-Range`, multipart, an inconsistent body length or total | resolution error | yes |
| Redirect without a `Location`, or with an invalid one | resolution error | yes |
| A `Content-Encoding` list such as `identity, identity` | resolution error (only an absent header or exactly `identity`) | TS yes; **Rust and Python no** |
| Chunked transfer coding | readers MUST accept it | yes |
| Userinfo or an empty host in `http(s)` URLs | resolution error | **No** (`h/userinfo`) |
| `file:` authority `localhost:80`, `user@localhost`, encoded `localhost` | not allowed: exactly `localhost` | (not tested) |
| ZIP64: a size field of 0xFFFFFFFF; duplicate `0x0001` blocks when the offset isn't all ones | entry errors | **No** |
| Payload of 65520–65531 bytes (fits the extra field, over the limit) | payload error | **No** |
| Empty `key` source | archive error at open, like an empty `url`; writers reject it | **No** |
| Missing key whose lookup page cannot be parsed | entry error | yes |
| Pages read by paged `list` | exact interval test, with a worked example | yes |
| `raw` of the format entries | through the comment; `raw("__vz__/index")` is missing in unpaged archives | yes (all chose the comment) |
| Literal range with an explicitly encoded zero `source` | checked by value: accepted | yes |
| `key`/`data` source range past the end of a value the writer knows | writers MUST reject | **No** (no writer checked) |
| "Lies within the file" | inside `[0, file size]`; overlaps aren't checked | yes |
| §5.1: empty repeated elements; negative `int64` | emitted (`0a 00`); 10-byte two's complement | yes |
| §9.1: reason for putting the index last | `__vz__/index` must follow the *pinned* entries; page offsets are relative to the CD | yes (editorial) |
| Memory for 200 responses | a documented limit → request error | (not tested) |
| HARNESS query rules | `range` only on `get`; `prefix` required; `range: null`, partial or negative ranges invalid; duplicate JSON members invalid; unknown members ignored | partly: TS accepted `range` on `get_raw`; Python accepted a negative range; **none** rejected duplicate members |

**Kit:**
- **Five new HTTP server modes:** `/oldate/`, `/multipart/`, `/badlen/`,
  `/nolocation/`, `/enclist/`. There are eight new HTTP cases, including a
  userinfo URL.
- **Seven new crafted cases:** all-ones size, duplicate ZIP64 block, a
  65520-byte payload, an explicit zero `source`, a missing key on a broken
  page, an empty `key` source, and reading a `data` range past the end. The
  last moved out of the basic description, because writers must now reject
  it.
- **Three new writer rejections and six new invalid query files.**
- **Each new HTTP case was checked to fail or pass for its intended reason.**
  This guards against the vacuous pass found in round 5.

**Result:** the reference passes everything:
- 5120/5120 read queries;
- 11/11 write cases;
- 41/41 rejections;
- 4973/4973 cross-reads;
- 1258/1258 HTTP checks.

The round-6 implementations, run against r7, fail exactly the rows marked
**No** above and nothing else.

## Round 7 (spec r7)

**Result:** all three fresh implementations passed every graded check:
- 5120/5120 read queries;
- 11/11 write cases;
- 41/41 rejections;
- 19892/19892 cross-reads;
- 3136/3136 HTTP checks.

The divergence report was empty. Every rule r7 newly chose was implemented
correctly from the text alone, including the ones the round-6
implementations had failed. Those are: IMF-fixdate-only, userinfo URLs,
`Content-Encoding` lists, ZIP64 size fields and duplicate blocks, oversized
payloads, empty `key` sources, the writer range checks, and the strict
harness query rules.

**Process note:** while deleting a temporary directory it had created one
level up, the TypeScript agent ran `ls` once on the parent scratchpad
directory. It read none of the files listed. File names don't reveal the
reference implementation or the test vectors, so the result stands.

| round | spec | passing everything | divergent queries | what the notes were about |
|---|---|---|---|---|
| 7 | r7 + HTTP | 3/3 | 0 | HTTP header-parsing minutiae; a few editorial gaps |

**Open issues** from round 7, deferred. None caused a divergence, and most
concern malformed HTTP responses or invalid archives:

- **§9.1:** does a zero-length range at an offset past the end of a `key` or
  `data` source "extend past the end"? Rust and TS reject it
  (`offset + length > len`).
- **§5.1:** when a non-repeated or `oneof` field appears twice, does an
  invalid *earlier* occurrence (uint32 overflow, bad UTF-8) make the message
  malformed? Rust: yes.
- **§6.2, header parsing:**
  - Does the `Content-Encoding` rule cover redirect, 412 and 416 responses?
  - Repeated `Content-Encoding`, `ETag`, `Last-Modified`, `Location` or
    `Content-Range` lines.
  - Case of the `bytes` unit, and whitespace.
  - Do the range and body-length checks apply to `bytes a-z/*` too? Python:
    yes.
  - A 200 shorter than the requested range.
  - A leap second (`:60`) in an IMF-fixdate.
- **§6.2, redirects:** how to count them ("a sixth is an error"), and whether
  the userinfo and empty-host rules apply to redirect targets.
- **URLs:** `http:/x` (no authority); empty or out-of-range ports.
- **§3.2:** a ZIP64 offset block longer than 8 bytes. All three use the
  first 8 bytes.
- **§8.1:** a resource limit hit while inflating a format entry at open. All
  three report an archive error. Say so.
- **§6:** base URI paths that aren't valid UTF-8, and `..` at the root.
- **§6.1:** a pin explicitly encoded as 0 still counts as a pin. Note it for
  protobuf-library implementations.
- **§8.1:** the same central-directory corruption is an archive error when
  unpaged but invisible when paged. This is by design; say so.
- **HARNESS:**
  - unknown members inside `range`;
  - a form member set to `null`;
  - non-string `key` or `prefix`;
  - numbers of 2^53 or more;
  - lone-surrogate strings;
  - an error class for internal bugs.

## Revision 8: version 0 becomes provisional

Format version 0, specification revision 8. **Status: provisional.**

§1.3 now defines three states for a format version:

- **draft:** anything may change.
- **provisional:** the format is believed complete, and implementers may
  rely on it. Revisions may still change it in response to outside feedback,
  but:
  - every change is logged here with its reason;
  - an incompatible change, meaning one that changes the result of an
    operation on an archive that was valid before, must be announced as
    incompatible, with the conformance suite updated;
  - incompatible changes are avoided whenever a compatible one would do.
- **final:** no incompatible change, ever. Changes of that kind become the
  next format version.

Version 0 becomes final once it has gone through outside review with no
incompatible change needed. Feedback is welcome as issues or pull requests.

**Revision 8 resolves the round-7 open issues:**

| issue | r8 decision | already matched r7 implementations? |
|---|---|---|
| Zero-length range at an offset past the end of a `key`/`data` source | "past the end" means `offset + length > size`, so writers reject it | yes (Rust, TS) |
| Invalid *earlier* occurrence of an overridden field | every occurrence must be valid. "Valid UTF-8" per RFC 3629. | yes: all three reject the corrected case |
| ZIP64 block longer than 8 bytes | the offset is the first 8 bytes; the rest is ignored | yes |
| Resource limit hit at open | archive error | yes |
| Paged vs unpaged corruption asymmetry | stated as intentional | (editorial) |
| A pin explicitly encoded as 0 | still a pin (presence); a note for protobuf-library users | (not tested) |
| `Content-Range` unit case | case-insensitive, one space (RFC 9110 §14.4) | **TS no** (required lowercase) |
| `bytes a-z/*` | same range and body-length checks as `/total` | yes |
| A 200 shorter than the requested range | resolution error | yes |
| Repeated `Content-Range`, `ETag`, `Last-Modified`, `Location` fields | resolution error | **No**: all three accepted a duplicate `ETag` on an unpinned read |
| Repeated `Content-Encoding` fields; which responses are checked | combined into one list; only the final 200/206 is checked | yes |
| Redirect counting | at most 5 redirects, so at most 6 requests; tested at exactly 5 (succeeds) and 6 (fails) | yes |
| URL rules on redirect targets; `http:/x`; ports | apply to every URL requested; an absent or empty host is an error; port > 65535 is an error; an empty port means the default | yes |
| Leap second; case of day and month names in IMF-fixdate | `:60` is accepted as the following second; names are case-sensitive | (not tested) |
| Base URI for non-UTF-8 paths; `..` at the root; `file:` path bytes | raw bytes are percent-encoded; `..` at the root is dropped; decoded bytes are used as they are | (not tested) |
| HARNESS: non-string or lone-surrogate `key`/`prefix`; numbers ≥ 2^53; unknown members in `range`; internal errors | malformed (exit non-zero); malformed; ignored; `class: "internal"`, always a failure | partly: **TS and Py** accepted lone surrogates and 2^53 |

**Kit:**
- **Five new HTTP server modes:** `/upperbytes/`, `/dupetag/`, `/dupenc/`,
  `/short200/`, `/redirectuser/`.
- **New HTTP cases:**
  - the exact redirect boundary: 5 redirects succeed, 6 fail;
  - a port above 65535.
- **Three new crafted cases:** a ZIP64 block longer than 8 bytes, and an
  invalid earlier occurrence in a payload and in the source table.
- **New rejections:** a zero-length range past the end, and three new
  malformed query files.

**Two bugs found in the kit itself while adding these:**
- **The redirect boundary.** The first version expected `/redirect/5/` to
  succeed, but that path makes 6 redirects (the `/redirect/0/` hop redirects
  too), and the reference correctly refused it. The cases now test 5 and 6
  exactly.
- **A crafted source table** had a wrong length byte. It failed for a
  different reason (a truncated field) than intended. Fixed and re-checked.

**Result:** the reference passes everything:
- 5125/5125 read queries;
- 11/11 write cases;
- 45/45 rejections;
- the whole HTTP profile.

Against r8, the round-7 implementations fail exactly the rows marked **No**
or partly above (`h/dupetag`, TS `h/upperbytes`, and two query-file
rejections), and nothing else.

## Revision 8.1: credit for prior work (editorial)

Format version 0, specification revision 8.1. **Status: provisional.**
Editorial only: no rule changes, so the revision 8 conformance suite applies
unchanged.

The new informative §1.4 credits the work vzip builds on:
- **kerchunk:** its reference specification defines vzip's data model. §1.4
  maps each kerchunk construct (inline data, `[url, offset, length]`,
  `base64:` values, `templates`) to its vzip counterpart.
- **VirtualiZarr:** its chunk manifests are what vzip references record, and
  the call for a Zarr-native on-disk manifest format (zarr-specs#287)
  motivated this work.
- **Icechunk:** its virtual chunk checks are the origin of source pins
  (§6.1).

PITCH.md gains a matching section, "Where the ideas come from".

## Revision 8.2: coalescing reads

Format version 0, specification revision 8.2. **Status: provisional.** No
result changes, so the revision 8 conformance suite applies unchanged.

**What prompted it.** Virtualizing Nikon ND2 files whose image rows are
padded (`widthBytes` larger than a row) gives one Concat per frame, with one
range per row. Every reader in this project fetched each range separately:
877 requests for one frame, which Zenodo answered with HTTP 429.

**The conflict.** §6.2 said a reader MAY combine reads of nearby ranges of
the same object into one request, but §8.3 said readers SHOULD fetch only
the bytes of the ranges, which a combined read with gaps does not.

**The change.**
- **§8.3:** readers SHOULD coalesce. Nearby reads of the same source become
  one read covering the gaps between them, and the gap bytes are never
  returned. "Nearby" is up to the reader; this project's readers use 64 KiB.
  The section also notes that coalescing cannot change a result: a combined
  read ends where one of its ranges ends, so it fails only where that range
  would.
- **§6.2:** the MAY becomes a SHOULD, pointing to §8.3.

**Implementations.** All three readers now coalesce: the reference reader
(`src/vzip/store.py`), the browser reader (`web/src/archive.ts`) and the
Neuroglancer driver (d-v-b/neuroglancer, `vzip` branch). An ND2 frame of 877
padded rows is now one request.

## Open feedback: browser readers (from the Neuroglancer driver)

The Neuroglancer driver
([`src/kvstore/vzip/`](https://github.com/d-v-b/neuroglancer/tree/vzip/src/kvstore/vzip)
in the fork d-v-b/neuroglancer) is the first
reader to run inside a web browser. Its demo reads the IDR OME-TIFF from
`ftp.ebi.ac.uk`, and that turned up §6.2 rules a browser cannot follow:

- **`Accept-Encoding` cannot be set.** It is a forbidden request header.
  Browsers send `identity` for range requests on their own, and the driver
  still rejects encoded responses.
- **Redirects are not under the reader's control.** `fetch` follows up to 20
  redirects itself, so the 5-redirect limit cannot be enforced.
- **Headers are hidden cross-origin.** `ETag`, `Last-Modified` and
  `Content-Range` are visible only if the server lists them in
  `Access-Control-Expose-Headers`. `If-Match` and `If-Unmodified-Since` also
  need the server to allow them in the CORS preflight. EBI allows `Range`,
  but none of these.
  - **Pins** therefore cannot be checked against such a server, and pinned
    reads fail closed. The demo uses an archive written without pins.
  - **Every 206 response** lacks a visible `Content-Range`, so §6.2 as written
    rejects all reads. The driver accepts a cross-origin 206 with no visible
    `Content-Range` if the body has exactly the requested length. The size is
    then unknown, so a `size` pin fails.

A future revision could define a "browser profile" that permits these
relaxations explicitly. It could also say what a writer can do to keep pins
checkable in browsers, such as recommending servers that expose the headers.

## Revision 9: the ZIP64 end records are always present (incompatible)

Format version 0, specification revision 9. **Status: provisional.**

**This revision is incompatible**, in the sense of §1.3. An archive that was
valid under revision 8 and has no zip64 end records is no longer valid, and
opening it is now an archive error. That is every revision-8 archive with
fewer than 65,535 entries and a central directory that ends below 4 GiB. To
convert one, replace its end of central directory record with a zip64 end
record, a locator and an all-ones end record; nothing before the end of the
central directory changes.

**Why.** Outside review
([issue 4](https://github.com/d-v-b/vzip/issues/4)) proposed making ZIP64
unconditional, and asked whether "exactly where a value is too large, and
nowhere else" (r8 §3.2) was a deliberate trade-off. It was not: it was the
default carried over from revision 1. The part of the proposal that concerns
the end records ("option A") is adopted, for these reasons:

- **The conditional rule had a path that small archives never took.** An
  archive that crossed 65,535 entries, or a central directory offset or size
  of 4 GiB, changed shape at its tail. The suite exercised the entry-count
  threshold with one 70,000-entry archive and never the other two.
- **It had already produced a defect.** Round 3's rule ("use the zip64 record
  whenever a locator precedes the end record") misread a valid archive whose
  last file name contained a locator signature, and all three round-3
  implementations rejected it. Revision 4 fixed that with a second
  conditional rule. With the locator always present, the bytes before the
  end record are never file-name bytes, and both rules go away.
- **It costs 76 bytes per archive** and nothing per entry.

| § | r8 | r9 |
|---|---|---|
| 3.2, writers | zip64 end record and locator if and only if a count, size or offset is too large; only the overflowing end-record fields are all ones | zip64 end record and locator in every archive, directly after the central directory; the end record's two counts, size and offset are all ones in every archive |
| 3.2, readers | use the zip64 record if and only if an end-record count, size or offset is all ones; otherwise do not examine the bytes before the end record | always take the directory's size and offset from the zip64 record; a missing locator is an archive error; the end record's counts, size and offset are ignored, whatever they hold |
| 3.4 | step 1 cannot match a fake record because the real record's disk number is 0 | the same argument, restated for a tail that now ends in a locator |
| 8.1 | "the zip64 records are used when, and only when, §3.2 says so" | the locator and the record it names are checked in every archive |
| 8.6 | readers need not detect ZIP64 used where not needed | the same for a record's ZIP64 block, for end-record fields that are not all ones, and for a zip64 record that is not adjacent to the directory and the locator |
| 9.2 | zip64 end records "if needed" | always |

**What did not change:**

- **Central directory records.** A record still carries a ZIP64 block only
  when a value does not fit its field (its local header offset, or, since
  large entries were added below, its sizes). Putting the block on
  every record ("option B") would add 12 bytes to each record, about 16% of
  the central directory of a per-chunk archive, and remove no reader rule.
- **The magic** stays `vzip/0`.

**Large entries (added to this revision after review of
[PR #5](https://github.com/d-v-b/vzip/pull/5)).** Revision 8 limited every
entry to less than 4 GiB, so that sizes never needed ZIP64 and no local header
had an extra field. A Zarr shard can be 4 GiB or more, and a vzip archive
holding shards as bytes entries could not store it. The limit is lifted with
the standard ZIP64 layout, applied only to the entries that need it:

| § | r8 | r9 |
|---|---|---|
| 3.1 rule 4 | every local header has an empty extra field | the same, except that a large entry's has exactly the 20-byte ZIP64 field (sizes only); its body offset is 20 bytes further on |
| 3.1 rule 7 | sizes are less than 0xFFFFFFFF | an entry of 0xFFFFFFFF bytes or more (compressed or not) is **large**: both size fields are all ones in both headers, and the sizes are in the ZIP64 field. A large entry MUST be STORED (method 8 is an entry error), and the format entries are never large |
| 3.2 | a record's ZIP64 block holds only the offset; a size field of 0xFFFFFFFF is an entry error | the block holds the sizes of a large entry, then an offset that needs it, in APPNOTE order; exactly one all-ones size field, a block too short for what the record needs, or two blocks are entry errors |
| 4.3 | — | a large reference entry is an entry error (payloads are at most 65,519 bytes, so it is never needed) |
| 9.1 | writers reject bytes entries of 0xFFFFFFFF bytes or more | removed |

Why this layout and not the alternatives considered when the end records
were changed:

- **The body offset still comes from the central directory record alone.**
  Whether an entry is large is decided by its two size fields, and the local
  extra field's length is then fixed (20 bytes), so a reader never reads a
  local header to find a body.
- **It costs nothing for entries under 4 GiB.** A ZIP64 field in every local
  header ("option C" in the issue) would cost 20 bytes per entry, which in a
  per-chunk archive is mostly reference entries that can never be large.
- **Large entries are STORED.** A DEFLATE body is inflated in full even for
  a small window (§8.4), which for an entry of 4 GiB or more defeats range
  reads; [issue 15](https://github.com/d-v-b/vzip/issues/15) points out this
  is the cost SOZip exists to remove. Shards are compressed inside, and OME-Zarr
  RFC 9 asks for ZIP-level compression to be off, so nothing is lost.
- **It is what ZIP64 tools expect.** APPNOTE requires the local ZIP64 field
  when the local size fields are all ones; tools that read local headers (and
  `unzip -t`, which checks them) find the sizes where they look for them.

This part of the revision is **compatible**: every revision-8 archive is
still valid. A revision-8 reader gives an entry error for a large entry and
reads the rest of the archive normally.

The reference writer gains `add_file`, which streams a file into a STORED
entry (one pass for the CRC-32, one to copy), so a shard of any size is never
held in memory.

**Checked by hand on the large-entry crafted archive** (8 GiB, sparse):
Python's `zipfile` reads every size, offset and body; `bsdtar -tv` lists
every size. macOS's UnZip 6.00 reads the large entries but reports "extra
field (type: 0x0001) corrupt" for the entry whose central directory record
follows the large ones and holds only an offset in its ZIP64 block, and
fails to find its local header; the bytes are as APPNOTE describes, and the
same layout would come from any ZIP64 writer (the entry needs the offset
block because it lies past 4 GiB). UnZip appears to carry state over from
the previous entry when it decodes a ZIP64 block.

**Compatibility:**

| archive | reader | result |
|---|---|---|
| revision 8, without zip64 end records | revision 9 | archive error |
| revision 8, with zip64 end records but end-record fields that are not all ones | revision 9 | read normally (those fields are ignored); the archive is not valid, §8.6 |
| revision 9 | the three revision-8 implementations | read normally: the all-ones fields send them to the zip64 record |
| revision 9 | a reader following the revision-8 text strictly | archive error: r8 §8.1 uses the zip64 records "when, and only when" §3.2 needs them, which it does not for a small archive |
| revision 9 | a ZIP tool that supports ZIP64 | read normally |

The third row was checked: the three round-7 implementations, before any
change, read nine archives written by the revision-9 reference writer and
gave the reference's answer on all 4,973 queries. The fourth row is about
the text, not the implementations: they do not enforce r8 §8.1's "only
when", but the text allows a reader that does. For the fourth, the
validator runs `unzip -t` on every written archive, and Python's `zipfile`,
`zipinfo`, `bsdtar` and macOS `ditto` were checked by hand on the converted
5,050-entry archive in `experiments/out/`.
A tool without ZIP64 support cannot read a vzip archive any more.

**Kit:**

- **The validator** requires the zip64 record directly after the central
  directory and the locator directly after it, a size field of 44, "version
  needed" 45, single-disk fields, and an all-ones end record.
- **Retired:** `false_zip64_locator_signature` and
  `zip64_sentinel_without_locator`, which tested the two conditional rules.
- **New crafted cases that must fail to open:** no zip64 records; a
  revision-8 archive, unpaged and paged; a locator that does not immediately
  precede the end record; a zip64 record with a wrong signature, with a size
  field of 45, or outside the file; a central directory outside the file.
- **Large entries:** the validator requires the 20-byte local ZIP64 field
  and all-ones size fields in both headers of a large entry, and an empty
  local extra field and equal local sizes on every other entry. New crafted
  cases: `large_entries` (an entry of exactly 0xFFFFFFFF bytes, one of
  2^32 + 3, and entries after them past 8 GiB, read by range from sparse
  files), and entry errors for a ZIP64 block too short for the sizes, one
  holding the sizes but not the offset, no block at all, a large
  reference entry, and a large entry with method 8. `size_field_all_ones` now tests "exactly one size field
  is all ones".
- **New crafted cases that must read normally:** end-record fields of zero,
  of garbage (unpaged and paged), and of the actual values; wrong entry
  counts in the zip64 record; a last file name full of end-record
  signatures.
- **`experiments/out/idr0096_4000_d11_m5_LT_2.vzip`** was converted in place
  (its tail rewritten as above), not regenerated from the remote TIFF.

**Implementations.** The reference and the three round-7 implementations in
`impls/` were patched directly: a small change to how each writer and each
reader handles the end records, plus their own tests. This was not a round
of fresh implementations from the text, so revision 9's wording of §3.2 has
not been tested the way revisions 1–7 were.

**Result:** the reference and all three implementations pass everything
revision 9 changed:

- 5169/5169 read queries, over 79 crafted archives (61 before; 6 of the 18
  new ones test large entries);
- 11/11 write cases, each valid under the new validator;
- 19892/19892 cross-reads.

The only failures left are the four revision-8 rules the round-7
implementations already failed (`h/dupetag`, TS `h/upperbytes`, and two
query-file rejections in TS and Python). The same run on the commit before
this revision fails exactly the same checks.
