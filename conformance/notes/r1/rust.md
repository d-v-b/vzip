# SPEC_NOTES: vzip v1 draft rev 1, Rust implementation

These are the places where the spec was ambiguous, contradictory, surprising or hard to
implement. Each entry gives the section, the question, and the choice made here. The
entries are ranked within each group, with the ones most likely to break interoperability
first.

## A. Highest interoperability impact

### A1. §3.1 vs §8.2: MAY reject or MUST report?
§3.1 says "a reader MAY reject archives that violate [rules 1-8]". §8.2 says "violations of
§3, §4.1, §5.1, §6, §7" MUST be reported as errors. These conflict. A conformance runner
cannot tell whether a lenient reader is conforming.
**Choice:** strict wherever the check needs no extra I/O beyond what the operation already
does. At open the reader checks: EOCD/comment, ZIP64 consistency, every CD record (flags,
method, UTF-8, extra-field parse, kind classification, duplicate names, reference
method/size rules), the sources/index entries against the comment, and the full page index
layout. When it reads a body it checks the local header, the CRC and the sizes. It does
**not** detect orphan or extra local headers, gaps, or overlapping bodies (rule 1).

### A2. §8.2: when errors surface (at open or in the operation)
"The archive itself may be rejected when it is opened, or the error may be raised by the
operation that first encounters it." The harness reports these two cases differently
(`open.ok=false` versus a failed query), so two conforming readers will give different
harness output for the same broken archive.
**Choice:** structural problems (ZIP, comment, CD, source table, page index) fail at open.
A reference payload is decoded and checked (wire format, literal rules, source index bound)
only by `get`, so `classify` still returns `reference` for a key whose payload is garbage.
Resolution errors (missing key source, out of bounds, I/O, unsupported scheme) come from
`get`. **Suggestion:** the spec should say which errors are open-time and which are
per-key, or the runner should accept either.

### A3. §3.1 rule 3 vs APPNOTE ZIP64 local headers
Rule 3 requires a local extra length of 0. APPNOTE 4.5.3 requires the ZIP64 extended
information extra field in the **local** header when the local size fields are 0xFFFFFFFF.
So an entry of 4 GiB or more cannot satisfy both.
**Choice:** the local header saturates csize/usize to 0xFFFFFFFF and has no extra field.
The true values are only in the CD. On read, a local 0xFFFFFFFF matches any CD value of
0xFFFFFFFF or more. Info-ZIP and similar tools may reject such entries. Not tested, because
it needs a body of 4 GiB or more.

### A4. §4.3/§4.1: the maximum payload of 65531 is not reachable together with ZIP64
The CD extra field holds at most 65535 bytes in total. If the record also needs a ZIP64
`0x0001` block (an entry offset of 4 GiB or more is enough), the payload limit drops to
65531 − 4 − 8·k. §9 does not list this as a writer rejection.
**Choice:** the writer rejects a record whose combined extra field exceeds 65535 bytes,
with an error.

### A5. §3.1 ZIP64 threshold: "does not fit in its 32-bit field"
Literally, 0xFFFFFFFF (and 0xFFFF for counts) fits. But APPNOTE treats those values as the
"see ZIP64" sentinel. The spec also does not say which EOCD fields to saturate once ZIP64 is
used (only the overflowing ones, or all of them).
**Choice:** use ZIP64 when value ≥ 0xFFFFFFFF (≥ 0xFFFF for entry counts) and saturate only
the overflowing fields. The reader accepts a ZIP64 locator even when it isn't needed, and
requires non-saturated EOCD fields to agree with the ZIP64 record. "MUST NOT use ZIP64
otherwise" is not enforced on read.

### A6. §4.3 + §8.2: checking the reference body
The reader "MUST NOT require the body", but the body "MUST be either empty or
byte-identical to the payload", and §8.2 says violations of §4 MUST be reported. A reader
that never reads the body cannot detect a bad one.
**Choice:** at open, the CD alone must show method 0, csize == usize and
csize ∈ {0, len(payload)}. `get` on a reference does not read the body. `get_raw` reads it
and checks the CRC and that it equals the payload (or is empty). So a corrupted mirror
shows up only through `get_raw`.

### A7. §5.2 / §8.1: which parts of a reference are validated and resolved
- The static checks (literal fields zero, `source` < table length) are run on **every**
  part on each `get`, including parts the request does not overlap.
- A part is resolved (its source touched) only if it overlaps the request in at least one
  byte. So zero-length parts, and parts outside the request, never touch their source: a
  `key` source that is missing, or a `url` that is unreachable, goes unnoticed for them.
  The spec says to resolve "only the parts of the ranges that overlap", but does not say
  whether a zero-length range "resolves" its source.
- Out of bounds: once any part of a source range is touched, the whole
  `[offset, offset+length)` must lie inside the source value (the "MAY report" option),
  even if the requested bytes are in bounds. This is deterministic, but another reader may
  return the in-bounds bytes instead. **Suggestion:** pick one, or make the runner accept
  both.
- `offset + length` overflowing u64, or the sum of part sizes overflowing u64, is reported
  as an error. The spec does not mention it.

### A8. §5.1: decoder edge cases the spec leaves open
- **uint32 overflow:** a varint above 2^32−1 in `Range.source` or `Pinned.method`. Proto3
  practice truncates silently. **Chose to reject** as malformed. Interop risk: a
  truncating reader would see `source = 2^32 + 1` as `source = 1`.
- **10-byte varint whose 10th byte is above 1** (overflows 64 bits): rejected. The spec only
  forbids more than 10 bytes.
- **Field number 0, or above 2^29−1:** rejected. These are invalid protobuf, but the spec
  says to "skip unknown field numbers".
- **Non-canonical input** (explicit zero fields, out-of-order fields, non-minimal varints):
  accepted. The spec makes encoders canonical but does not say whether a decoder may reject
  non-canonical input.
- **Canonical uniqueness** also needs minimal varints and length prefixes, which the spec
  does not state.
- **Embedded messages with an empty encoding** (a `Range` with every field at its default,
  inside `Concat.parts`) must be emitted as a zero-length LEN field. The "MUST NOT emit
  defaults" rule covers only scalars. Worth saying explicitly.

### A9. §3.4: locating the EOCD; non-vzip ZIP files
The spec does not say how to find the EOCD. A generic backwards scan for `PK\x05\x06` can
match inside the binary comment, whose offsets are arbitrary bytes. Because the comment is
exactly 22 or 38 bytes, the EOCD is at one of two fixed positions.
**Choice:** try `len-60` (38-byte comment) and then `len-44` (22-byte comment). Each
candidate needs the signature, a matching comment-length field, and the magic. In theory
both can match; the 38-byte form wins. Trailing bytes after the comment are not allowed.
For a ZIP without a vzip comment, "MUST reject, or MAY treat as all-bytes" leaves the
runner with two possible answers. **Choice:** reject.

## B. Medium impact

### B1. §7 vs §8.2: page index validation defeats the page index
§7.2 is meant to let a reader read one page. But §8.2 requires every §7 violation (unsorted
body, misaligned pages, wrong `first_key`, pinned mismatch) to be reported, which takes the
whole CD. **Choice:** this reader reads the whole CD at open and validates everything:
- sorted body records, then trailer records = {sources, index};
- pages contiguous, non-empty, on record boundaries, ending at the trailer, with matching
  `first_key`;
- no pages if there are no body records;
- every pinned key present, a body record, of kind bytes, with matching
  offset/size/csize/method.

Lookups use an in-memory map, not the index.

### B2. §7 / §3.4: an `__vz__/index` entry with a 22-byte comment
The 38-byte comment is required iff the archive "has a page index", but it is not stated
whether an `__vz__/index` entry alone means it has one. **Choice:** reject the
mismatch either way. A 38-byte comment with no `__vz__/index` entry is also rejected.

### B3. §7.3 Pinned details left unspecified
- Duplicate pinned keys: accepted.
- Pinning `__vz__/sources` or `__vz__/index`: rejected, because "pinned entries also appear
  in the body records".
- Pinning hidden keys: allowed.
- Order of the `pinned` list: unspecified. The writer emits it sorted by key.

### B4. §4.1 / §6: reserved entries as references
Nothing forbids `__vz__/sources` or `__vz__/index` from carrying 0x7A76/0x7A77, but §6 also
calls for an inflated SourceTable body. **Choice:** reject (they must be kind bytes,
method 8). Other `__vz__/...` hidden entries may be references; a `key` source naming one
fails at resolve time.

### B5. §6: base URI and URL details
- **Base URI of a local path:** absolute or canonical (symlinks resolved)? Chose
  `std::path::absolute` (lexical, without resolving symlinks). The path is percent-encoded
  except for unreserved characters and `/`. Two readers that disagree here resolve `../x`
  differently when symlinks are involved.
- **`url` values that are not strictly valid URIs** (spaces, non-ASCII, a stray `%`): the
  spec says "URI reference" but neither rejects these nor says how to treat them. This
  reader treats them literally (no validation) and percent-decodes the resolved `file:`
  path. A bad `%xx` is an error at resolve time. So `"a%20b.bin"` names the file `a b.bin`.
  Writers and harness descriptions should be told to percent-encode.
- **`file:` URIs** with a host other than empty or `localhost`: error. Query and fragment
  are ignored.
- **`http(s)`:** not implemented (SHOULD); it is reported as an unsupported scheme at
  resolve time.
- **Empty `url`:** rejected at open, because the source table is decoded at open. Another
  reader may only fail the ranges that use it.
- **Security:** a reader that MUST support `file:` will read any local file an archive
  names, including absolute paths and `../` paths. The spec has no security considerations
  section. It should have one (a sandbox or allow-list option for readers).

### B6. §6: `key` source edge cases
- A `key` source naming `__vz__/sources` or `__vz__/index` is a bytes entry, so the reader
  allows it.
- The writer can't produce one, because the harness forbids those keys in `entries` and
  §9 says to reject a key "absent from the archive".
- An empty `key` string is not forbidden. It is simply missing at resolve time.

### B7. §8.1: `classify`, `get_raw`, hidden keys
- `classify` does not decode the payload (see A2).
- `get` with `start > end` on a hidden key is an error, the same as for a missing key.
- `get_raw` on a hidden key returns its body, and on a missing key returns null. The CLI
  also accepts a `range` on `get_raw`.
- `list("__vz__/")` returns an empty list.
- The empty key `""` classifies as missing; it is not an error.

## C. Lower impact / writer freedom

- **§3.1 ZIP fields not specified:** timestamps, version made by/needed, external
  attributes. The writer uses DOS date 1980-01-01 00:00, version 20 (45 when ZIP64), and
  attributes 0, so output is deterministic. A byte-for-byte comparison across writers
  would need these to be specified.
- **§3.2:** "Writers SHOULD sort" in unpaged archives. All records are sorted, including
  `__vz__/sources`.
- **§9 layout and body order:**
  - Body order is description order for non-pinned entries, then `__vz__/sources`, then
    the pinned entries (the only "metadata" signal the harness gives), then
    `__vz__/index`, then the CD.
  - In unpaged archives no entries are pinned, so the "metadata after sources" layout
    cannot be expressed.
- **§5.2 "Writers SHOULD give index 0 to the most used source":** the harness fixes the
  source indices, so the writer keeps them as given.
- **§4.3 zero-range references:** the payload is an empty 0x7A77 block, so `mirror` makes no
  difference. Note also that an empty **0x7A76** payload is a valid zero-length range of
  source 0, and it is invalid if the source table is empty. That is surprising.
- **§7.1 page grouping:**
  - The writer is greedy: a record is added to the current page while
    `page.length + record_len <= page_size`, otherwise it starts a new page.
  - A record bigger than `page_size` gets a page of its own.
  - `page_size: 0` means one record per page.
  - With no body records there are no pages, and the 38-byte comment is still used.
- **§3.1 rule 1:** the reader does not detect duplicate or orphan local headers.

## D. HARNESS.md

1. **"Do not leave a file at `<out-path>`":** the writer writes to a temporary sibling and
   renames it. On failure it also **deletes a pre-existing file** at `<out-path>`. It is
   unclear whether that is wanted.
2. **Unreadable or invalid queries JSON:** not specified. The CLI prints to stderr and
   exits 2, since there is nothing to report under `open`.
3. **Range object with several forms** (e.g. both `start`/`end` and `suffix`): not
   specified. Precedence is start/end, then offset, then suffix. `start` without `end` is
   a query error. Negative or non-integer values are query errors.
4. **A range element with no `source`:** the harness only says that missing
   `offset`/`length` mean 0. A missing `source` is also taken as 0.
5. **A range element with both `data` and any of `source`/`offset`/`length`:** rejected.
6. **`compress` on a reference entry:** ignored (§4.3 forces method 0). It could be read as
   an error. **`pinned: true` on a reference:** rejected.
7. **A source object with more than one of `url`/`key`/`data`, or none:** rejected.
8. **Non-UTF-8 keys:** these can't be expressed in JSON, except as lone surrogates, which
   serde_json rejects as invalid JSON (exit 1). So the §9 "non-UTF-8 key" rejection can't
   really be tested.
9. **Hex input:** uppercase is accepted, odd length is rejected.
10. **JSON integers above 2^53:** handled exactly (serde_json u64). Runners or
    implementations in other languages may lose precision.
11. **Error messages:** free-form; the runner presumably only looks at `ok`.
