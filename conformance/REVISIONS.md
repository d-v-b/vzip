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

Revision 6 has not been through a fresh round of agents.

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
