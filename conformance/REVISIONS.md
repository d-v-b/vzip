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

## Round 2

*(pending)*
