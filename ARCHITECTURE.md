# vzip architecture: review and proposal

Status: proposal, 2026-10-08. This document collects an adversarial design
review of vzip as a whole (five independent reviews: the data model, the
archive format, resource bounds, the two implementations, and remote I/O)
and proposes one structural change, an intermediate representation, that
addresses most of what they found. It changes no normative text; VIRTUALIZE.md,
SPEC.md and the conventions stay authoritative until a revision adopts parts
of this.

## 1. Verdict

vzip works: eleven formats virtualize into Zarr v3, two implementations agree
on 667 fixtures and the public corpora, and independent readers confirm the
pixels. Four rounds of per-format review made every format lossless and
bounded on the inputs reviewers could think of.

But the per-format rounds kept finding the same kinds of problem in each
format in turn, and each was fixed locally. The five architectural reviews
agree on why: vzip has no shared model of the source, no shared model of
resources, and no shared model of I/O. Each profile parses, translates,
budgets, spills, escapes and reads in its own way, twice. That is where the
recurring bugs come from, and it is what makes a twelfth format expensive.

## 2. What the reviews found

### 2.1 Two incompatible ideas of what the record is

Six formats (TIFF, NDPI, ND2, DICOM, NIfTI, IMS) treat translated JSON as the
record of the source. Making JSON lossless forced a growing vocabulary of
about twenty tagged forms (`{"latin1"}`, `{"utf16"}`, `{"int"}`,
`{"float"}`, `{"bits"}`, `[name, value]` pairs, `\0`+base64 names, `named`,
`Gathered`/`Structures`, hex fills, base64 data forms, ...), several of which
collide with user keys in copied attributes. Even so, some information is
still lost: ND2's LV record types (so LV chunks cannot be rebuilt, as ND2
§5.1 admits), ND2 XML attributes and text, DICOM number spelling, IMS object
comments, and every colliding name under the shared §6 rule.

SAFE and CZI took the other view: the source bytes are the record, kept by
reference, and JSON is a derived convenience. That view is simpler and loses
nothing.

Consequences: reconstruction depends on eleven hand-written inverses that no
one has written down as code and no test exercises (only SAFE and CZI rebuild
files in conformance); "layout" means three different things across
conventions; there are about twelve budget numbers, five spill-to-text forms,
two path roots, four escaping schemes, and one reserved name with two meanings
(`vzip_source/objects`).

### 2.2 No resource model

About 95 per-site constants bound intermediate quantities. Nothing bounds
total output, bytes read, requests, wall time or browser memory. The class
that matters most is reproduced in three formats: references multiply when a
logical index maps onto a shared physical structure (an OME-TIFF whose 3000
planes name one IFD gives 12 M references from 176 KB and exhausts the
browser; HDF5 hard-linked time points; a server that lies about the size).
Each instance was patched separately ("share repeated tables", "scale the IMS
object cap") and the class remains open.

### 2.3 No I/O model

Each profile walks its format one round trip at a time. Measured: an ND2
frame-header prefetch that misses on every real frame (names are padded to
4072 bytes), DICOM fragment walks of 912 serial 64 KiB reads over one 57 MB
run, SAFE tile-part chains of 1849 12-byte reads per band, an IDR plate of
10 k sequential document reads. Python and the browser differ in
concurrency, retry, caching and request counts; the browser has no retry, no
timeout and unbounded fan-out, and downloads a whole object per block when a
server ignores `Range`.

### 2.4 The archive format

- Readers resolve any scheme an archive names, with ambient credentials: a
  remote archive can read `file:///etc/hosts` or private buckets, and one
  chunk can fan out to about 10,000 requests. SPEC §10 says SHOULD; no reader
  does.
- No staleness detection: the profiles forbid pins, so a changed
  uncompressed source silently yields wrong pixels.
- About 115 bytes of ZIP framing per chunk (40x kerchunk Parquet); a store
  archive repeats a full URL per chunk and decodes it all at open; payloads
  are mirrored into entry bodies (up to 49% of an archive); the 65,519-byte
  payload cap rejects real padded ND2 frames.
- Little interoperability without vzip code: `zlib` and the imagecodecs
  codecs are not registered, rectilinear grids are experimental, and the
  browser path needs the service worker.

### 2.5 Two implementations that no longer prove the spec

The TypeScript code is a function-by-function port of the Python code. Spec-
only implementation rounds stopped at revision 4; nine profiles have never
been built from the spec alone. So agreement between the twins shows a
faithful port, not a complete spec, while every fix costs two edits.
`compare.py` cannot see the spec's own exact-integer rule (it compares
numbers as binary64), changed reference bodies, extra hidden entries, or
duplicate members. There is no CI. The spec has absorbed ECMAScript artifacts:
budgets defined as `JSON.stringify` output length, −0 allowed to become 0,
member order declared insignificant because JavaScript reorders keys.

### 2.6 Versioning is off

Every convention still says version 1 after about thirty breaking changes;
the `virtualize-*` tags do not exist, so every `schema_url` and `spec_url` in
every archive is a 404. An archive from revision 14 and one from revision 18
are indistinguishable.

## 3. Proposal: an intermediate representation

### 3.1 Two stages instead of one

Today each profile goes from bytes to Zarr in one step. Split it:

1. A **parser** reads the source into an IR: a typed tree of the format's own
   elements, each with its extent in the source.
2. **Projections** turn an IR into a Zarr hierarchy. A projection never reads
   the source.

The same TIFF IR can then feed an OME-NGFF image, a raw per-IFD view, a
GeoZarr layout or a sharded layout. Shared machinery (budgets, tags, spill,
escaping, output bounds) lives in the projection layer, once.

### 3.2 The IR

A tree of elements, emitted as a stream of records with parent ids so that a
million-frame source never sits in memory. Names come from the format's own
specification: TIFF tag numbers and IFD paths, DICOM `(gggg,eeee)` and item
indexes, HDF5 object paths, ND2 chunk names, CZI segment ids, JPEG 2000
marker names, ZIP entry names.

| Kind | Meaning | Carries |
|---|---|---|
| `struct` | A container: IFD, group, segment, item, box | Children |
| `value` | A field the layout or a reader needs | Its extent, its declared type, its decoded value (small) |
| `data` | A decodable unit: tile, strip, frame, subblock, codestream tile | Geometry (shape, dtype), codec, and a recipe: source ranges, literals and shared data sources |
| `derived` | Content that exists only after a transform: an inflated LV chunk, a deflated XML entry | The transform, its input extent, and the derived bytes' own sub-tree |
| `alias` | A second name for an element already described: two planes naming one IFD, a hard link | A reference to the first |
| `gap` | Bytes no element claims: padding, dead space, unparsed regions | Its extent |

The framing repairs vzip already does (JPEG tables and restart segments,
J2K headers and empty packets, padded rows) are recipes of `data` elements:
they belong to the parse, not the projection.

### 3.3 Invariants

- **Coverage.** The extents of the leaves (`value`, `data` sources ranges,
  `derived` inputs, `gap`) partition `[0, size)` of each source. Checked by
  one shared function on every IR.
- **Injectivity.** No byte is claimed twice. Sharing is explicit, as `alias`.
- **Bounded parse.** The IR's record count and the bytes the parser read are
  charged to one budget; see §3.5.

These invariants give, for every format at once:

- **A reconstruction contract.** Concatenate the leaves in extent order: the
  source rebuilds byte for byte with no format knowledge. "Complete
  reconstruction minus layout" stops being a per-format judgment; layout is
  in the IR as `gap` or `value` elements, kept by reference.
- **The end of the amplification class.** A projection that emits references
  can be checked against the IR: every pixel byte referenced at most once
  unless the IR says `alias`, and the projection decides what an alias
  becomes (one array, or a link), instead of silently multiplying.

### 3.4 Projections

- **Image** (OME-NGFF 0.5): from `data` elements and the `value` elements
  that give geometry, scale and channels. Today's main output.
- **Geo** (GeoZarr): for SAFE.
- **Source mirror**: one generic projection that writes the IR itself under
  `vzip_source`, with one vocabulary: `value` elements as JSON with one tag
  namespace that cannot collide with user keys (`{"$vz": kind, ...}`),
  everything large as arrays or families by the existing §7 rules, one budget
  and one spill form, paths relative to `vzip_source`, one escape rule. It
  replaces the eleven bespoke source-metadata translators and their tags.
- Variants: sharded layouts (SPEC already has virtual shards), alternate
  chunkings, a raw view next to a stitched one.

JSON in the mirror becomes a view of the IR, not the record: it need not be
lossless, because the record is the IR's extents. The tagged-form arms race
ends at "readable".

### 3.5 Resources and I/O

- One **budget** per run, threaded through the parser's reader and the
  projection's output: bytes read, requests, IR records, output entries,
  document bytes, work for loops over declared counts. Limits split into
  format-validity rejections (in the profiles) and resource limits (one
  table, stated as "MUST support at least X, MAY fail above"), generated into
  both languages from one file.
- One **read planner**: parsers declare the ranges of each step
  (`plan.add(ranges)`, then `await plan.run()`), never single reads in loops.
  The planner coalesces with a gap cost model calibrated from the first
  requests (PR #28 already has one), limits concurrency per host (6 in
  browsers on HTTP/1.1), retries with backoff and `Retry-After`, detects
  sequential walks and reads ahead, and charges the budget. Estimated
  effect: ND2 18 k rounds to about 560, DICOM 912 to about 15, SAFE L1C 1700
  to 1–130, the IDR plate 10 k to about 160.

### 3.6 One core, and the spec

The IR makes the twin question tractable. Parsers stay format-specific;
projections, budgets, the planner and the mirror are shared code and a small
shared spec. Options for delivering both Python and the browser:

| Option | Cost | Notes |
|---|---|---|
| Keep two hand-written implementations | Ongoing: every change twice | What we have |
| Pyodide in a browser worker, running the Python core | Low; drops about 14 k lines of TypeScript | Python is stdlib-only and synchronous; worker XHR allows sync reads. Needs a cold-start measurement (6–10 MB runtime) |
| Rust core, WASM and PyO3 | High: a rewrite | Fastest; `impls/virtualize/rust` is a seed |
| TypeScript core called from Python | Low | Python users then need Node |

Recommendation: measure Pyodide's cold start on the CZI and ND2 corpus; if
acceptable, make Python the single implementation and the browser run it.
Spec completeness is then proven the way it was meant to be, by spec-only
implementation rounds per profile, gating each profile out of draft.

The spec reorganizes along the same line: per format, a **source model** (the
IR element schema, normative, with canonical names); shared, the **IR
invariants**, the **projections**, the **budget table** and the **read
planner**.

## 4. Do now, independent of the IR

These are small and right under any architecture:

1. **Reader security policy** (SPEC): a source whose scheme differs from the
   archive's is refused unless the application allows it; readers take a URL
   prefix allowlist; caps on sources and requests per value; bounded
   inflation of format entries.
2. **Staleness**: let profiles pin `size` (always known) and `etag` when
   visible; add an optional per-range `crc32c` that readers check after the
   fetch, which works across CORS.
3. **Conformance**: make §1.1 compare integer literals exactly (adopt
   `same()`); run SPEC's strict reader on every archive inside `compare.py`;
   fail the TypeScript reader when `JSON.parse` source text is unavailable;
   add CI (pytest, node tests, sharded fixture compare) and a nightly corpus
   and mutant run.
4. **Versioning**: write `version: 0` and the revision until release; at
   release, tag and bump on every breaking change; pin source sizes.
5. **Bugs found on the way**: the ND2 frame-header prefetch misses on every
   real frame (read `min(name_length, 64)` or prefetch 16 + 4096); the
   browser ND2 path reads headers one at a time; Python reads multi-block
   spans one block at a time; the Python LRU is shared across threads
   without a lock.
6. **Fixtures**: pin timestamps (h5py `track_times=False`, gzip `mtime=0`),
   add generators for the two hand-committed fixtures, pin corpus URLs to
   commits and record their hashes.
7. **Rebase** the work onto `origin/main`, which already has concurrent store
   document reads (#37). Done: merged, the reads ahead going through the
   store's per-thread kept-alive connections, which each reading thread
   closes as it ends.
8. **Registry**: register `zlib` and the imagecodecs codecs, or prefer core
   codecs where the source allows.

## 5. How to test the proposal

Before touching nine formats, prototype on two:

- **TIFF** (simplest): parser to IR; the image projection and the mirror.
- **CZI or ND2** (hardest: derived content, sparse indexes, shared
  structures).

Success means: the image projection reproduces today's outputs (equivalent by
§1.1); the mirror projection rebuilds every corpus file byte for byte from
the IR's extents with no format code; the amplification probes (shared IFDs,
hard links) are refused or aliased by the shared check, not by format code;
and the IR plus projections are smaller than today's profile.

## 6. Open questions

1. Is the IR normative (part of each convention, versioned) or an
   implementation detail with only the projections normative? A normative IR
   gives independent implementations a checkpoint to compare; it also makes
   the spec larger.
2. Should the mirror projection be on by default, or opt-in? It costs one
   reference per gap, about 20 bytes per frame for ND2 and DICOM.
3. Single core: measure Pyodide first, or decide on principle?
4. Do the stores (N5, Zarr v2, OME-Zarr) fit the IR, or stay as they are?
   Their source is a set of objects rather than one byte stream; an IR of
   objects, each referenced whole, is the natural form and matches what the
   store conventions already do for other objects.
