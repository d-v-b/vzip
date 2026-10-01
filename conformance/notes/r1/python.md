# vzip spec notes (Python implementation, round 1)

Each item gives the section, the question, and what this implementation chose and why.
Items are grouped roughly by how much they can change observable behaviour between two
conforming implementations. "Interop" means the item can make two conforming readers or
writers disagree.

## A. High interop impact

### A1. §3.1 vs §8.2: MAY reject vs MUST report
- §3.1 says "a reader MAY reject archives that violate [rules 1-8]". §8.2 says
  "violations of §3 ... MUST be reported as errors". These contradict each other. A
  lenient reader and a strict reader are both arguably conforming, and they give different
  results on the same file.
- **Choice:** strict. Every §3 rule that can be checked cheaply is checked:
  - local headers are read and compared with the central directory at open;
  - CRC and size are checked when a body is read;
  - a reference body that is present is compared with its payload at open.
- **Suggest:** pick one. If readers may skip checks that need extra I/O (local headers,
  CRCs, mirrored bodies), say so explicitly, and list which violations a reader MUST
  detect.

### A2. §8.2: error at open or at the operation
- The spec lets a reader report an error either when the archive is opened or in the
  operation that first hits it. Under the harness this changes the output: one reader
  prints `open.ok=false`, another prints `open.ok=true` and fails some queries. A test runner
  can only compare results by accepting both.
- **Choice:**
  - Reported at open: structural errors, meaning ZIP structure, §3 rules, §4.1
    classification, source table decode and §6 validity, and §7 index layout.
  - Reported by the operation:
    - CRC or inflate failures of individual bytes entries;
    - reference payload decode failures (§5.1), and literal ranges with non-zero fields;
    - a `source` index out of bounds;
    - anything that happens while resolving a source.
- **Suggest:** state which class each error in §8.2 belongs to. At least say that
  per-reference errors (payload decode, source bounds, literal fields) MUST NOT fail the
  open, so that one bad reference does not hide the rest of the archive. That would match
  the lazy-source philosophy of §6.

### A3. §5.2 / §8.1: when is a part "resolved"?
- The spec says only resolving a range touches its source. It does not say whether these
  are checked for every part of a reference or only for the parts that overlap the request:
  - the decode of the payload;
  - `source < len(sources)`;
  - literal-field checks;
  - `key` existence and kind.
- Examples:
  - A whole `get` of a reference whose zero-length part points at a missing key: error or
    not?
  - `get(range(0,1))` of a reference whose second part has `source = 99`: error or not?
- **Choice:**
  - Checked whenever a reference is read with any request (the payload is a unit):
    payload decode, literal-field checks, and `source` index bounds.
  - Checked only for parts whose overlap with the request is non-empty: source access, which
    means `key` lookup and kind, URL fetch, and end-of-source checks. Zero-length parts never
    touch their source.
- **Suggest:** define this explicitly. It is directly visible in conformance tests.

### A4. §5.2: out-of-bounds ranges, "MUST ... if past the end, MAY otherwise"
- Take a part of length 10 over a 5-byte source:
  - `range(0,3)` may succeed or fail;
  - a whole `get` must fail.
- Two conforming readers can differ on partial requests. For `data` and `key` sources the
  size is known for free, so a reader could always detect the problem.
- **Choice:** error only when a byte we would return is past the end. This is the lazy
  option and needs no size discovery.
- **Suggest:** either require the lazy behaviour, or require an error whenever the source
  size is known (`data`/`key`). Test runners must otherwise accept both outcomes.

### A5. §3.4: non-vzip comment, "MUST reject, or MAY treat as all-bytes"
- Two readers can return completely different results (open failure vs. a full listing)
  for a plain ZIP file.
- **Choice:** reject.
- Also unspecified:
  - a comment that has the right length but a different magic, e.g. `vzip/2`;
  - whether a future version may extend the comment.

  Both are rejected here.
- **Suggest:** pick one behaviour. Add a versioning rule, e.g. "readers MUST reject any
  magic other than `vzip/1`".

### A6. §6: relative URLs, base URI and percent-encoding
- **Symlinks in the base path.** "The `file:` URI of the absolute path" does not say whether
  symlinks are resolved. On macOS `/tmp` is `/private/tmp`, and a relative reference
  containing `..` resolves differently depending on the choice.
  - **Choice:** `os.path.abspath`, which does not resolve symlinks. `Path.as_uri()`
    percent-encodes the result as `file:///abs/path`.
- **Percent-encoding.** Is `url` stored percent-encoded? RFC 3986 says yes: a file named
  `e x/a.bin` must be referenced as `e%20x/a.bin`. The harness example shows only plain
  ASCII. A writer that stores raw names with spaces or non-ASCII characters, and a reader
  that does not percent-decode, will disagree.
  - **Choice:** store the string verbatim and percent-decode the path when reading `file:`
    URLs (RFC 8089). Strings that are not strictly valid URI references, such as raw
    spaces, are accepted leniently.
- **Resolution mode.** Strict or non-strict resolution (RFC 3986 §5.2.2) is not specified.
  `file:ext/a.bin` is a scheme-qualified, rootless reference. Strict resolution does not
  resolve it against the base. A non-strict resolver would.
  - **Choice:** strict, and a `file:` URI whose path is not absolute is an error. Using the
    process cwd would give results that depend on the caller.
- **Other `file:` URL parts.** Host, query and fragment are not specified.
  - **Choice:** the host must be empty or `localhost`, a query is an error, and the fragment
    is ignored.
- **Suggest:**
  - require strict RFC 3986 resolution;
  - require percent-encoded URI references, and say what readers do with invalid ones;
  - say whether symlinks are resolved;
  - give a worked example.

## B. Medium impact / real implementation constraints

### B1. §3.1 rule 3 (no local extra field) makes entries of 4 GiB or more impossible
- APPNOTE 4.5.3 requires a ZIP64 extra field *in the local header* when the sizes do not
  fit. Rule 3 forbids local extra fields. Rule 4 forbids data descriptors. An entry whose
  size is 0xFFFFFFFF or more therefore cannot be written conformantly.
- Large `bytes` entries are plausible, for example inlined chunks.
- **Choice:**
  - The writer rejects such entries.
  - The reader requires the local CRC and sizes to equal the central directory values, so
    it rejects such entries too.
- **Suggest:** either allow a local ZIP64 extra block (and adjust the body-offset formula
  to `30 + n + m`), or state the 4 GiB limit explicitly.

### B2. §4.3: the 65531-byte payload limit ignores the ZIP64 extra block
- The central directory extra field as a whole is limited to 65535 bytes. A reference entry
  whose local header offset is 4 GiB or more also needs a `0x0001` block of 12 bytes. A
  65531-byte payload then no longer fits.
- **Choice:** the writer rejects the payload if the *total* central directory extra field
  would exceed 65535 bytes, as well as rejecting payloads over 65531 bytes.
- **Suggest:** state the limit as "the whole extra field ≤ 65535", or lower it to 65519.

### B3. §3.1: what counts as "does not fit" for ZIP64
- 0xFFFF and 0xFFFFFFFF are the APPNOTE "look in ZIP64" sentinels, so a value equal to the
  sentinel does not really fit.
- **Choice:**
  - The writer uses ZIP64 for values ≥ 0xFFFF entries or ≥ 0xFFFFFFFF bytes. In the end of
    central directory record it sets only the overflowing fields to the sentinel. In the
    `0x0001` block it includes only the overflowing fields, in APPNOTE order.
  - The reader rejects these, under "MUST NOT use ZIP64 otherwise":
    - ZIP64 end records when no value reaches a sentinel;
    - a `0x0001` block when no central directory field is a sentinel;
    - a `0x0001` block of the wrong size.
  - The reader tolerates an entry count of exactly 0xFFFF with no ZIP64 locator.
- **Suggest:** say "≥ the all-ones value", and say which fields go in the `0x0001` block.
  Say whether a reader must check the "MUST NOT use ZIP64 otherwise" rule.

### B4. §4.3: the mirrored body vs "MUST NOT require the body"
- A reader that never reads the body cannot detect a body that differs from the payload,
  yet §8.2 says that violation MUST be reported.
- **Choice:** check at open, which is cheap for local files. This includes the body's
  CRC-32.

### B5. §5.1: unspecified decoder corner cases
- **Field numbers 0 and above 2^29−1** (invalid in protobuf).
  - **Choice:** reject.
- **A 10-byte varint above 2^64−1** (10th byte > 1). The spec rejects only varints *longer*
  than 10 bytes.
  - **Choice:** reject.
- **`uint32` fields (`source`, `method`) with values ≥ 2^32.** Protobuf truncates these.
  Truncation could turn an out-of-range source into an in-range one.
  - **Choice:** reject as malformed.
- **Non-minimal varints**, e.g. `0x80 0x00`. Readers accept them. The "unique encoding"
  claim also needs encoders to use minimal varints, which is implied but not stated.
- **Field 2 of `Range`** is unused. It is presumably reserved. Add `reserved 2;` to the
  schema so nobody reuses it.

### B6. §7.1 / §3.4: "has a page index"
- Is the trigger the 38-byte comment, the `__vz__/index` entry, or both?
- **Choice:** both must agree.
  - A 38-byte comment without the entry is an error.
  - The entry with a 22-byte comment is an error.

### B7. §7: page index validation details
- **Unspecified:**
  - whether pages must appear on the wire in ascending offset order. They are implied by
    "page *i*". **Choice:** required.
  - whether pinned entries may be hidden (`__vz__/foo`). **Choice:** allowed.
  - whether pinned entries may repeat, and whether their order matters. **Choice:** both
    allowed.
  - whether a pinned key absent from the body records is an error. **Choice:** error.
- **Missing checks:**
  - `Pinned` carries no CRC. A reader that serves a pinned entry without the central
    directory record cannot verify rule 6.
  - A reader that uses only pages and pinned entries cannot check the "MUST equal" rule
    without reading the central directory.
- **Choice:** the reader parses the whole central directory, which is cheap for local files.
  It validates the whole index at open, and still performs lookups through the pages, as in
  §7.2.

### B8. §7 page grouping and `page_size`
- The harness says only "about `page_size` bytes". Values of 0 and negative values are not
  covered.
- **Choice:**
  - Greedy grouping: start a new page when adding the next record would exceed `page_size`
    and the current page is not empty.
  - `page_size: 0` gives one record per page.
  - Negative values are rejected.
  - A record larger than `page_size` gets its own page.

### B9. Layout circularity, which the spec does not mention
- Page offsets depend on the body records, and the body records contain local header
  offsets. If `__vz__/index` were written before body entries, its size would change their
  offsets, which is circular.
- The recommended layout in §9 avoids this. The normative text does not require it.
- **Suggest:** say that `__vz__/index` MUST follow every body entry, or note the
  dependency.

## C. Lower impact / clarifications

### C1. §6: `key` sources naming `__vz__/sources` or `__vz__/index`
- These are present `bytes` entries and may be hidden, so they appear to be allowed. This is
  odd and probably unintended.
- **Choice:** allowed, both by the reader and by the writer when the entry will exist.
- **Suggest:** forbid it.

### C2. §6: empty `url`
- An empty `url` is forbidden. Under RFC 3986 it would resolve to the archive itself.
- **Choice:**
  - The reader rejects it at open, as a malformed source table, not as an "unreachable URL".
  - The writer rejects it.
- **Tension:** "an archive whose source table names an unreachable URL is fully readable".
  Is a malformed source entry fatal for the whole archive?

### C3. §6 vs §5.1: the oneof
- "Exactly one member set" and "last member on the wire wins" mean the same thing only for
  the decoded value. A `Source` with two members on the wire is valid.
- **Choice:** last member wins.

### C4. §3.3: problematic keys for ZIP tools
- The spec allows keys that ZIP tools treat badly:
  - keys ending in `/` look like directories to ZIP tools;
  - keys that contain `..`, start with `/` or contain `\` are extraction hazards for
    `unzip`;
  - NUL is allowed;
  - keys longer than 65535 UTF-8 bytes cannot be stored.
- **Choice:** accept everything except keys over 65535 bytes, which the writer rejects.

### C5. §3.4: the binary comment
- The comment is binary. `unzip -l` and `unzip -t` print it raw to the terminal, which shows
  as `vzip/1` followed by garbage.
- Our test suite failed at first on a UTF-8 decode error caused by this.
- The comment can also contain the bytes `PK\x05\x06`. The usual backward search for the end
  of central directory record copes with this only because the comment length must match.
- **Suggest:** note this, or recommend locating the end record by trying comment lengths 38
  and 22 exactly.

### C6. §3.1: fields the spec leaves open
- The spec does not specify:
  - timestamps;
  - version made by and version needed;
  - external attributes;
  - whether bytes may appear before the first entry or between entries.
- **Choice:**
  - DOS date 1980-01-01 00:00, so output is reproducible;
  - version 20, or 45 with ZIP64;
  - UNIX mode 0644;
  - the reader requires the central directory to end exactly where the end records start,
    and does not check for gaps.

### C7. §4.1: reference kind on the reserved entries
- `__vz__/sources` and `__vz__/index` carrying a reference extra field would make them
  reference entries, which conflicts with "method MUST be 8". The spec only implies this is
  invalid.
- **Choice:** error at open.

### C8. §5.2: no upper bound on value size
- A reference value can be up to 2^64 bytes per range. The sum of several ranges can exceed
  u64. Implementations in fixed-width languages need a rule: reject, or saturate.

### C9. §5.2: source index 0
- "Writers SHOULD give index 0 to the most used source". Under the harness the writer cannot
  reorder sources, because indices are given.
- **Choice:** keep the order as given.

### C10. §8.1: request and prefix corner cases
- `start > end` gives an error even for missing or hidden keys. **Choice:** the request is
  validated first.
- Negative or non-integer bounds give an error.
- `suffix(0)` returns an empty value.
- `list` with a prefix that cannot be encoded as UTF-8 (a lone surrogate from JSON) returns
  `[]`.
- `classify` or `get` with such a key returns `missing`.

### C11. §8.1: classify on a bad payload
- `classify` of a reference whose payload is malformed returns `reference`. Classification
  uses the central directory alone. Only `get` fails.

### C12. §8.1: the raw view of the reserved entries
- `get_raw("__vz__/sources")` returns the inflated SourceTable bytes. The spec implies this
  but does not say so.

## D. HARNESS.md

### D1. `compress: true` on a reference entry
- This contradicts §4.3, which requires method 0.
- **Choice:** ignore it. Rejecting could fail runner cases that set it on every entry.

### D2. Range elements with both `data` and `source`/`offset`/`length`
- **Choice:** reject.
- Unknown members in source and range objects are also rejected. Unknown members of entries
  and of the top level are ignored. The harness does not say which applies.

### D3. "Do not leave a file at `<out-path>`"
- It is unclear whether a file that existed before the call must be deleted.
- **Choice:** the archive is written to a temporary file, then atomically renamed. On error
  any existing `<out-path>` is removed.

### D4. Optional fields
- `sources`, `entries` and `page_size` are not marked optional or required.
- **Choice:** they default to `[]`, `[]` and `null`.

### D5. Number limits
- Integers in JSON beyond u64, or beyond u32 for `source`, are rejected.
- Hex input is accepted in either case. Output is lowercase.

### D6. `get_raw` with a `range`
- This is undefined. **Choice:** the range is ignored.

### D7. Malformed queries
- Malformed queries (unknown `op`, a missing key, a bad `range` shape) produce
  `{"ok": false}` results. The CLI does not crash.
- An unreadable or non-array queries file is treated as a CLI failure, with a non-zero exit.
  The harness does not say how to handle it.

### D8. Error message format
- The format is unspecified. **Choice:** `"<ExceptionType>: <message>"`.
