# IR prototype: notes

A prototype of ARCHITECTURE.md §3 for TIFF and CZI, in Python only. Nothing
outside `src/vzip/ir/` and `tests/ir/` changes; the existing profiles are the
reference the projections reproduce.

## 1. The core (`model.py`, `types.py`, `check.py`, `emit.py`, `mirror.py`)

**Records.** An `IR` is a list of records kept as columns (`array` columns for
kind, parent, alias target and extents; lists for names and types; sparse
dicts for decoded values and for the geometry, codec and recipe of `data`). A
parser emits records in order, a parent before its children
(`ir.struct(...)`, `ir.value(...)`, `ir.data(...)`, `ir.derived(...)`,
`ir.alias(...)`, `ir.gap(...)`), then calls `ir.finish()`.

**Types.** A `value` carries a declared type from a small grammar
(`types.py`): numbers with an endianness and dimensions (`<u2[3]`,
`>u4[1,2]`), `ascii[n]`, `cstr[n]` (to the first NUL), `bytes[n]`, `guid`,
and records `{name:type,...}` (with dimensions: arrays of records). A value's
decoded value is always `decode(type, its bytes)`: one decoder, no format
code. Records were needed (see the report): a TIFF entry header or a CZI
directory entry as one record value keeps the record count near one per
field group instead of one per field.

**Invariants and `finish()`.** Parsers do not have to know whether bytes are
already claimed. `finish()` sorts the leaves' extents by offset (then by
emission), turns every leaf that overlaps an earlier claim into an `alias` of
the first claimant (it keeps its own extents, type, value and recipe: an alias
may name the same bytes with another meaning, or overlap them only in part),
and makes every byte nobody claims a `gap` (`gaps/<offset>` under the root).
A parser may also name its gaps itself (CZI: spare bytes, unused allocation).
`check()` verifies, independently, that the leaves partition `[0, size)`,
that parents precede children and that aliases name an element, and names the
first violation precisely ("bytes [6, 10) are claimed by element 1 (value 'a')
and element 2 (value 'b')").

**Rebuild.** `check.rebuild(ir, read, write)` writes the leaves' bytes in
extent order: by coverage and injectivity, that is the source. No format code.

**Budget.** One `Budget` per run, charged by the parser's reader (bytes and
calls), by every record, by every output reference, and by every *repeated*
reference (`emit.Refs`): a reference to bytes some reference already used,
which the IR can only reach through an alias. Limits are resource limits,
stated once for every format: records ≤ 2^20 + size, bytes read ≤ 2^24 + 2 ×
size, read calls ≤ 2^20 + size / 64, references ≤ 2^22 + 4 × size, repeated
references ≤ 2^20, mirror documents ≤ 2^24 bytes.

**References.** A projection never writes a range itself: `Refs.chunk(key,
element)` writes the element's recipe (source ranges, literals, shared data
sources) and charges the budget.

## 2. How the IR is stored (the mirror)

The mirror projection writes the IR under `vzip_source`, so a consumer can
rebuild the source from the archive alone (`mirror.rebuild_from_archive`,
which uses only the archive and the source URL's bytes through the archive's
references):

| path under `vzip_source` | form |
|---|---|
| `ir/kind` | uint8 [n]: 0 struct, 1 value, 2 data, 3 derived, 4 alias, 5 gap |
| `ir/parent`, `ir/target` | int64 [n] (−1: none) |
| `ir/extent_index` | int64 [n + 1]: element i's extents are rows `[x[i], x[i+1])` of `ir/extents` |
| `ir/extents` | int64 [m, 2]: (offset, length) |
| `ir/name`, `ir/type`, `ir/info` | families (conventions §7 offsets + data): UTF-8 names, types, and JSON of geometry, codec, recipe, transform |
| `ir/shared` | family: the shared data sources recipes name |
| `bytes` | uint8 [size], the source's bytes in order (conventions §7 bytes), referenced in place |
| `tree/...` | the view: a group per struct that holds structs; its attributes are its values |

The table arrays are copied (cut as contiguous values, 1 MiB chunks). The
`bytes` array is what the extents index: since the leaves partition the
source, "the leaves' bytes, by reference" and "the source in order, by
reference" are the same references, and the second costs one reference per
16 MiB instead of one per leaf. Rebuilding reads the table, checks the
invariants on it, and concatenates `bytes[o : o + n]` for each leaf in offset
order.

**The view** (`tree/`) is one vocabulary for every format: values are JSON
where JSON holds them; otherwise `{"$vz": kind, ...}` (`int` past 2^53,
`float` for NaN and infinities, `bytes` up to 64 bytes, `array` for a large
value written as a conventions §7 array referenced in place, `alias`, `group`,
and the one spill form `{"$vz": "element", "id": i}`, "read element i in the
table"). The one escape rule: a key that starts with `$` gets another `$`; a
path segment is percent-encoded. A struct gets a group when it holds structs
or more than 64 children; otherwise it is an object nested in its parent's
document. Each document holds at most 2^16 bytes (what does not fit is
spilled), and all of them at most the budget's 2^24 bytes (past it, the view
stops; the table has everything).

The table stores each `data` element's geometry, codec, transform and recipe
as a **form** (`ir/form` int32 [n], `ir/forms` a family of JSON), the recipe
relative to the element's first extent, so that the 20,000 tiles of a slide
share one form. Rebuilding needs only kind, extents and `bytes`.

## 3. TIFF (`tiff/parse.py`, `tiff/ome.py`, `tiff/image.py`)

The parser emits: `header` (byte order, magic, BigTIFF's offset size and
reserved word, first IFD offset); the main chain `ifds/<i>`, each IFD's
`subifds/<j>` to depth 4, and the IFDs other pointer tags lead to (`exif`,
`gps`, `interoperability`, `ifd_<tag>`) under today's rules (depth, 10,000
tries, no overlap with an IFD read); per IFD its `entry_count`, each entry as
`tags/<tag>` (duplicates `tags/<tag>~<k>`) with its `entry` record (tag, type,
count, and the offset when out of line) and its `value` (in line: the bytes
of the value field it uses, the rest of the field is a gap; out of line: at
its offset), and `next_ifd`; strips and tiles as `data` (`tiles/<k>`,
`strips/<k>`) with geometry, codec and recipe, JPEG tiles framed with the
Adobe marker and JPEGTables as a shared data source in the recipe; for an
OME-TIFF, `ome/planes/<p>` as aliases of the main-chain IFDs (OME-TIFF's
TiffData mapping). An IFD whose TileOffsets and TileByteCounts are the same
values as an earlier IFD's (and whose codec and framing agree) names its
`tiles` by alias. The parser rejects only a file whose header is not TIFF.

The image projection is today's convention logic reading the IR: every check
of profiles/tiff.md §3.1 (field types, counts and places of the table's tags
in every IFD read, IFDs read twice, IFD offsets in the header, values past
2^53) is a check of IR values and of which elements exist.

## 4. CZI (`czi/parse.py`, `czi/xml.py`, `czi/image.py`)

The parser emits each segment by its role, each with its 32-byte `header`:
`file_header` (its `fields` record), `directory` (`entry_count`,
`entries/<i>`, `dimensions/<i>`), `subblocks/<i>` (`sizes`, its copy of its
`entry` and `dimensions`, `metadata`, `data`, `zstd1_header`, `trailing`,
`attachment`), `metadata` (`sizes`, `xml`, `attachment`),
`attachment_directory` (`entry_count`, `entries/<k>`), `attachments/<k>`
(`data_size`, `entry`, and `data` by form: TimeStamps and FocusPositions as
`size`, `count`, `values`; an EventList as `size`, `count` and `events` (one
array of records when no event has a description, else `events/<e>` and
`descriptions/<e>`); Zip-Comp and ZIP as a `derived` gzip element, not
inflated; anything else bytes), the walk's other segments `segments/<k>`
(`header`, `data`), and `tail`. A subblock is a `data` element when its copy
agrees and its codec header gives a coded size (the parser reads the codec
header with today's `coding.coded_size`): geometry is the coded size, codec
the chain, recipe the pixels (after the Zstd1 header, or the first W×H×q
bytes of uncompressed data). Two directory or attachment entries with one
FilePosition: the second segment is an alias.

The image projection: today's series, layers, levels and tiles
(`layout.py` reused), reading the IR; the metadata XML values read in one
streaming pass (`xml.py`, no tree: linear, memory bounded by nesting).

Two deliberate differences from today, on hostile files only: an entry whose
subblock is an alias is not placed again (today: a copy, a tile array each);
and row bands have a floor (`image.bands`): when today's divisor band holds
less than 1 MiB, bands are the most rows within 2^24 bytes with a shorter
last band per tile row (a rectilinear grid).

## 5. Results (2026-10-08)

Equivalence is `compare.differences()` (§1.1) of the archives, without the
entries under `vzip_source`, against `python -m vzip.virtualize`.

| inputs | equivalent | both reject | rebuilt from the mirror archive |
|---|---|---|---|
| TIFF fixtures (73) | 33 | 40 | 33 of 33 accepted (all 73 from the IR in memory) |
| SVS corpus (4) | 4 | 0 | IR checked (coverage, injectivity) |
| IDR idr0096 (205) | 205 | 0 | IR checked |
| CZI fixtures (71) | 32 | 39 | 32 of 32 (all 71 from the IR in memory) |
| CZI corpus (24) | 24 | 0 | IR checked |

No difference in any hierarchy. The corpus files are not rebuilt (that reads
every byte; the proxy cache holds only the structure), but their IRs pass the
invariant checker.

**Largest files** (cached proxy, `/usr/bin/time -l`): CMU-1.svs (178 MB):
today 0.6 s, 102 MB RSS, 3.87 MB archive; IR 1.1 s, 122 MB, 4.17 MB.
axioscan_background_subtraction.czi (2.1 GB): today 0.5 s, 73 MB, 270 KB; IR
0.6 s, 112 MB, 316 KB.

**Mirror size.** The record (table plus `bytes`) costs about 12 bytes per
element after compression, mostly `ir/extents`. Against today's `vzip_source`
the archives are from 0.5× (small CZI files, whose JSON today repeats
per-subblock columns) to 1.5× (lsm980_xzt, 3575 subblocks: 809 KB against
557 KB), and +300 KB on CMU-1.svs (25,000 elements, each tile's extent, which
today's `vzip_source` does not hold at all). It is bounded by the record
budget (2^20 + size / 8 records); on the hostile `walk0.czi` (2 million empty
segments) the IR archive is 27.7 MB against today's 153 KB, since every
segment is an element.

**Amplification probes (TIFF).** OME-TIFF planes naming IFD 0, tiles naming
one 256-byte range (`arch-bounds/amp_tiff.py`): 100×100 and 1000×1024 are
accepted as today (tile aliases 99 and 1023, plane aliases 100 and 1000);
3000×4096 is refused by the shared `repeats` budget in 3.5 s and 350 MB (today
accepts it with 12.3 M references in 24.6 s and 3.5 GB). IFDs sharing one
tile table (10,000 IFDs × 4096 tiles): the tables are aliases, the tile
arrays an alias each; nothing is repeated.

**CZI review probes** (`review-czi/hostile/`):

| probe | today | IR |
|---|---|---|
| `dup.czi`: 100,000 entries, one FilePosition | 100,000 tile arrays, 69 MB archive | 99,999 subblock aliases, placed once: 2 chunks, 2.4 MB archive, 1.6 s, 297 MB |
| `evl16.czi`: 16 attachment entries, one 1 M-event list | 185 MB archive | 15 aliases, one array of records: 12.7 KB, 0.5 s |
| `bands1.czi`: prime height, 1-byte rows | 16.8 M chunks, 11.6 GB RSS (review) | 2 chunks, rectilinear `[16777216, 43]`, 0.3 s |
| `bands64.czi`: prime height, 64-byte rows | 262,147 chunks | 2 chunks |
| `deep40000.czi`: 40,000 unmatched end tags | 33.4 s | 0.4 s (streaming XML, no tree) |
| `flat.czi`: 16 MB of empty tags | 4.4 s, 1.76 GB | 2.8 s, 122 MB |
| `xt.czi` over HTTP: 100,000 subblocks of 4 KiB | 561 requests, 447 MB, 14.8 s, 14.3 GB RSS | 6,831 requests, 447 MB, 413 s, 1.5 GB RSS |
| `walk0.czi`: 2 M empty segments | 1.2 s, 718 MB | 6.8 s, 2.9 GB (4 M records) |

The first four are structural: aliases from injectivity, a band floor in the
projection, a streaming reader of the XML. `xt.czi` is not solved: the
parser requests only 38 MB (8.5%), but the transport underneath reads in
64 KiB blocks, one at a time, so the whole file crosses; it no longer holds
it (1.5 GB is the IR's records). A read planner (ARCHITECTURE.md §3.5) would
fetch the 38 MB as coalesced exact ranges. `walk0.czi` shows the cost of an
element per segment in Python: about 700 bytes of memory per record.

## 6. Answers

**Vocabulary.** The six kinds were enough, with three additions to what an
element carries:
- record types (`{name:type,...}`, arrays of them) in the value type grammar:
  without them a TIFF entry or CZI directory entry is 5–8 records;
- an alias keeps its own extents and fields: `finish()` makes aliases of
  partial overlaps (a tile that starts inside another), and of the same bytes
  read with another meaning (a shared TileOffsets read as a second IFD's), so
  an alias is "these bytes are claimed elsewhere", not only "this is that
  element";
- forms (shared geometry, codec, recipe), for the mirror's size.
`derived` was hardly needed: only CZI gzip attachments, not inflated.

**Where coverage fought the format.** TIFF claims arrive out of file order,
and only the whole IR says who claimed first, so injectivity is resolved
once, at the end, by sorting (a parser cannot decide aliases as it goes
without an interval index). The first claimant is defined by offset, then
emission, not by meaning. CZI is easy: segments partition the file, and dead
space (spare bytes, unused allocation, padding) is gaps. Both formats needed
`gap` only for dead space; the parsers name no gap themselves.

**Mirror size.** Bounded by the record budget, about 12 bytes per element.
Smaller than today for small CZI files, up to 1.5× larger on dense CZI files
and a few hundred KB larger on slides, because it holds every leaf's extent,
layout included. It replaces both bespoke translators (`tiff/tags.py`, 382
lines; `czi/source.py`, 186) with one projection.

**Should the IR be normative?** The prototype says: normative as a source
model per format (element names, kinds, types, and which elements a parser
emits), but not the record encoding nor `finish()`'s alias choice. The
checkpoint it gives is real: every accepted input of both formats compared
equal through it, and the invariants are checkable on any archive. But the
parsers here are permissive by design and every rejection moved to the
projection; that duplicates the format's structure in two places (the
parser emits what exists, the projection checks what must), which is the
cost of the split.

## 7. Size

Lines (physical / non-blank non-comment):

| | today | IR prototype |
|---|---|---|
| shared core | (common.py, 645, shared by eleven profiles) | 1008 / 822: model, types, check, emit, mirror, CLI |
| TIFF | 1083 / 947 (`virtualize/tiff`) | 732 / 649, plus 140 reused unchanged (OME-XML scanner) |
| CZI | 1373 / 1180 (`virtualize/czi`) | 707 / 656, plus about 470 reused unchanged (`coding.py`, `layout.py`, omero and array helpers, XML value parsers) |

Per format the IR code is smaller (TIFF 872 against 1083, CZI about 1180
against 1373, counting reused code), because the source-metadata translators
are gone; with the core counted against two formats only, the total is
larger (3057 against 2456). The core is format-free and would serve all
eleven.

# Round 2: ND2, a Rust core, a read planner, a schema

Round 2 takes the hardest format left (ND2: compressed LV chunks, XML
variants, sparse frame indexes, budgets that depend on map order) through a
Rust core, and addresses round 1's three open problems: per-element cost, the
missing read planner, and rejections living in the projections. Nothing in
`src/vzip/virtualize/` or the specs changes; today's ND2 profile is the
reference.

## 8. What was built

**The crate** (`rust/vzip-ir/`, `just ir-build`, `just ir-test`, `just ir-wasm`):

| file | what |
|---|---|
| `ir.rs` | the compact IR: dense columns (kind u8, parent u32, name u32, name index u64, type u32, space u32, start u64, len u64) and sparse tables sorted by element (value bytes, form, alias target, runs); names, types and forms interned; `fold`, `unfold`, rollback marks; the budget |
| `check.rs` | `finish` (aliases, gaps, gap runs, unfolding), `check` (coverage, injectivity, parents, aliases, derived spaces), `leaves` (the rebuild's ranges) |
| `types.rs` | round 1's type grammar and its one generic decoder, plus `utf16[n]` and the `xml:<runtype>` text type |
| `lv.rs`, `xml.rs` | the LV decoder (positions kept, exactness checked, room spent) and the XML variant scan (conventions/tiff §3 tag scan, ported) |
| `schema.rs`, `expr.rs`, `schema/nd2.json` | the ND2 source model as data, and its checker |
| `nd2.rs` | the sans-IO ND2 parser |
| `czi.rs` | round 1's CZI segment layer, sans-IO, for the comparison on `walk0.czi` |
| `py.rs` | PyO3 bindings (feature `python`): `Nd2Parser`, `CziParser` (step/feed/finish), `Ir` (columns, tables, per-element reads, `check`, `leaves`, `from_table`) |
| `wasm.rs`, `wasm/nd2.mjs` | a C-ABI surface for wasm32 hosts, and a Node driver |

Python: `planner.py` (the read planner and its transports), `nd2/parse.py`
(drives the parser), `nd2/image.py` (the image projection), `cmirror.py` (the
mirror of a compact IR, and the rebuild from an archive). Round 1's CZI parser
gained batch declarations (`Parser.declare`) so it can use the planner.

**Sans-IO.** A parser is `step() -> ranges | done`, `feed(offset, bytes)`,
`finish() -> (IR, facts)`. It never reads and holds fed bytes only until the
step that uses them (the source metadata's chunks are dropped one by one as
they are decoded and emitted). The ND2 parser takes six or seven rounds
whatever the file: (1) the signature and the tail; (2) the map's header (and
its name, when longer than 64 bytes); (3) the map's data; (4) every chunk's
16-byte header, one batch; (5) the three chunks the convention reads and
`CustomDataVar|CustomDataV2_0!`; then the schema is checked; (6) the chunks the
source metadata decodes, one batch (a lower bound of the budget it will spend
picks them, so no chunk it tries is missing).

**What it emits.** `signature` (header, name, data), `map` (header, name,
`records/<i>` as `{name:bytes[k],offset:<u8,size:<u8}`, `end`, `rest`),
`tail`, `chunks/<name>` for every chunk of the map but frames (header, name,
then `lv`, `xml` or `data`), `frames/<f>` for every `ImageDataSeq|<f>!`
(placed frames: header, name, `timestamp`, `pixels` as a `data` element whose
recipe is the whole range or `rows` for padded rows, `trailing`; others:
`data`), and gaps. An uncompressed LV chunk's records are elements in the
source (a scalar is one value of a record type,
`{lv:u1,k:u1,name:bytes[2k],v:<type>}`, named by the record's exact name; a
level is a struct of its `header`, its records and its `table`); a compressed
one is a `derived` element (transform `nd2-lv-zlib`, its inflated size in its
form) whose records are elements in its own space. An XML variant is a
`derived` element (transform `xml-variant`) whose elements are structs and
whose value attributes are values of type `xml:<runtype>`. Unplaced frames,
repeated names (map records replaced by a later one are gaps; two names whose
text reads alike are two elements), chunks whose names make bad paths, and
chunks that do not decode are all just elements: none is a special case.

**Facts.** `finish()` also returns the schema's derived quantities (the
attributes read, the flattened loops, N, P, the picture metadata's scales and
translations, the decoded chunks in map order with a lower bound of their
JSON size). The projection reads facts and elements only.

## 9. Runs

A run is a subtree that stands for `count` identically shaped subtrees,
member `j` shifted by `j × stride` with its root's name index increased by
`j`. Parsers do not build runs: they emit subtrees one by one and call
`fold(prev, new)`, which folds the subtree just emitted into its previous
sibling when every row matches (kind, name, type, length, value bytes, form,
parent offsets), the extents shift by one constant, and the members do not
overlap. A run costs one subtree; runs do not nest and hold no derived
content.

**How the invariants handle runs** (`check.rs`):

- A run's leaves are a *family*: member 0's leaves, repeated. A family whose
  member-0 leaves overlap one another, or span more than the stride, is
  invalid (`check` names it; `finish` unfolds it).
- Families of one stride whose member-0 leaves lie in one window
  `[a, a + stride)` form a *tile*. A tile is **complete** when its member-0
  leaves partition the window, with count `c` for a prefix `[0, t)` of it and
  `c` or `c − 1` for the rest: its members then partition exactly
  `[a, a + (c − 1)·stride + t)`, one leaf for the sweep (a **block**).
- `finish` fills the holes of an incomplete tile that nothing else reaches with
  **gap runs** (count `c` before the window's last leaf, `c − 1` after it), so
  frames separated by padding stay one run. A tile that something else
  intersects is expanded member by member (within the record budget). A run
  member that overlaps an earlier claim makes `finish` **unfold** the run
  (member 0 stays, members 1… are appended as plain subtrees) and start over;
  a plain leaf that overlaps a run member becomes an alias of it.
- `check` is round 1's sweep over blocks and the other leaves (expanded tiles
  member by member). `leaves` is the same sweep, a block one range: the
  rebuild reads `(c − 1)·stride + t` bytes in one range where round 1 read `c`
  members.
- Each `derived` element whose form gives its size is checked separately: its
  space's leaves partition `[0, size)`.

Tests: `rust/vzip-ir/tests/core.rs` (one test over contiguous runs, runs with
holes, a single after, inside and before a run; one per violation).

**Do runs fit the invariants cleanly?** Yes, with one rule (tiles) and two
fallbacks (expand, unfold) whose cost is bounded by the budget. The rule
covers what parsers produce: contiguous frames and padded frames (one block),
frames separated by holes (gap runs). The fallbacks only trigger on hostile
layouts. Two limits showed up. Folding needs *identical* value bytes, so
frames fold (their 16-byte headers are identical; timestamps and names are
not read) but map records, CZI subblock headers and LV records with distinct
values do not. And it needs a *constant* stride: in the BIAD 3015 files
NIS-Elements interleaves per-frame metadata chunks of varying size, so
8,800 frames fold into about 1,200 runs, not one. A run of *distinct* values
(timestamps, positions) would need a column of values per run: values
reached by index into an array element. That is the natural next step, and
it is what a frame-time stream already is.

## 10. Results (2026-10-08)

**Equivalence** is `compare.differences()` of the archives without the
entries under `vzip_source` (the fixture and fuzz runs compare the `Output`s
directly, documents and references), against `python -m vzip.virtualize`.

| inputs | equivalent | both reject | rebuilt from the mirror archive alone |
|---|---|---|---|
| ND2 fixtures (76) | 33 | 43 | 33 of 33 |
| review-4 fuzz files (`review4-nd2/fz`, 300: LV fuzz, names, XML) | 300 | 0 | 300 of 300 |
| review-4 probes (9) | 6 | 3 | — |
| round-2 probes (8) | 7 | 1 | — |
| ND2 corpus (17, cached proxy) | 17 | 0 | IR checked (17 of 17) |
| ND2 corpus files copied whole from EBI (263 MB, 1.16 GB, 4.57 GB) | 3 | 0 | 3 of 3, byte for byte (sha-256) |
| the 27 GB file, as a sparse local copy (below) | 1 | 0 | — |

No difference in any hierarchy, and every rejection matches today's (the
messages are the schema's). The wasm32 build, driven from Node, produces the
same IR (elements, runs, frames, decoded chunks, or the same rejection) as
the native build on all 384 test inputs.

**Corpus** (cached proxy, `/usr/bin/time -l`; the IR path includes the mirror):

| file | today | IR | archive today | archive IR | elements (runs) |
|---|---|---|---|---|---|
| biad3015 1-SR MC1 | 0.9 s, 117 MB | 0.8 s, 78 MB | 327 KB | 314 KB | 26,368 (200) |
| biad3015 3-SR MC1 (27 GB) | 1.6 s, 249 MB | 1.7 s, 119 MB | 1.58 MB | 1.05 MB | 104,275 (1,197) |
| biad3015 3-SR MC9 | 2.2 s, 260 MB | 2.4 s, 149 MB | 2.37 MB | 1.52 MB | 152,458 (1,792) |
| biad3015 3-SR MC10 | 1.1 s, 243 MB | 2.6 s, 141 MB | 3.19 MB | 0.80 MB | 121,820 (720) |
| biad2077 373_A1 | 0.4 s, 67 MB | 0.5 s, 68 MB | 64 KB | 88 KB | 2,425 (1) |
| biad1573 Bb54 | 0.5 s, 72 MB | 0.5 s, 69 MB | 123 KB | 183 KB | 5,190 (1) |
| pollen 169 | 0.5 s, 76 MB | 0.5 s, 74 MB | 1.04 MB | 1.07 MB | 1,373 (2) |
| zenodo vpa002 | 0.4 s, 66 MB | 0.4 s, 68 MB | 38 KB | 103 KB | 4,890 (0) |

Over the 13 BIAD 3015 files: time within 0.3 s of today's (MC10: 2.6 s against
1.1 s), peak memory about half, archives smaller on all 13 (0.25 to 0.96 of
today's; 0.65 to 0.74 for most). The hierarchy outside `vzip_source` is identical, so the archive
difference is the mirror against today's `vzip_source`: the mirror's table
(about 6 bytes per element compressed) and its view (the decoded chunks'
JSON, inlined up to 8 KiB per subtree) against today's JSON, per-frame arrays
and per-chunk byte arrays. On small files with large decoded metadata
(vpa002: 4,890 elements, almost all LV records) the mirror is 65 KB larger.

**Hostile probes** (local files; `positions_no_frames` and the review-4
probes are equivalent and cost the same as today, within 10%):

| probe | today | IR |
|---|---|---|
| `p4`: 36 MB of padded frames | 6.6 s, 813 MB | 4.8 s, 816 MB, equivalent |
| `p5`: 16 MB XML variant of declared tags | 1.4 s, 126 MB | 0.9 s, 128 MB, equivalent |
| `p1b`: frames at f = k × 8192 of N = 2^31 − 1 | 0.5 s, 2.86 MB archive | 0.8 s, 1.48 MB archive |
| `p2`, `p2b`: an LV bomb past 2^20 records | rejected, 353/433 MB | rejected, 183/222 MB |
| `p6`: 100,000 positions | rejected | rejected |
| `p6_20000`: 20,000 positions, no frames | 0.7 s, 19.2 MB | 0.9 s, 19.3 MB |
| `bomb_records_300`: 300 compressed chunks of 65,000 records | 5.7 s, 128 MB, 149 KB | 8.7 s, 1.3 GB, 1.65 MB |
| `bomb_bytes_custom`: a 64 MiB byte array in a CustomData chunk | 0.4 s | 0.4 s |
| `failed_decodes`, `repeated_names`, `sparse_families` | equivalent | equivalent |
| `frame_times_sparse`: 2^26 frames, 256 placed | 81 KB | 60 KB |

`bomb_records_300` is the one where the IR costs more: the source metadata
may decode up to 2^26 bytes of LV, which is 6 million records here, and the
IR holds each as an element (about 60 bytes, against today's shared Python
objects for 65,000 identical bools). The first version rejected it (more than
2^22 records); the parser now declares the records its decode limits allow
(2^20 + 2^26 / 3), and the mirror keeps derived subtrees in its table only up
to 2^20 rows (derived content is not needed to rebuild the source), which
took the archive's write from 30 s to 6 s. Folding derived content (runs in
derived spaces) would make this file small; it is not done.

## 11. Per-element cost

`walk0.czi` (2,000,000 empty segments after a subblock; 64 MB):

| | elements | time | peak RSS | archive |
|---|---|---|---|---|
| round 1, Python IR (parse only) | 4,000,020 | 6.1 s | 2,152 MB | 27.7 MB (round 1) |
| round 2, Rust core, folded | 15 (1 run) | 0.7 s | 274 MB | 12.8 KB |
| today's CZI profile | — | 1.2 s (round 1) | 718 MB (round 1) | 153 KB |

The same stream without folding (`vzip_ir.synthetic(2_000_000, fold=False)`:
4,000,002 rows, a struct and a 32-byte header value per segment) costs 73
bytes per element in the core (the columns, 41 bytes; the value index, 16; the
value bytes, 32 per header, half the rows) and 125 bytes per element of peak
RSS including `finish()`'s sort buffers, built in 0.15 s. Round 1 cost about
520 bytes per element (2,152 MB for 4 M). So the compact core is about 4×
smaller per element without runs, and runs remove the per-element cost of
repeated structure altogether. Most of the 274 MB of the folded walk is the
interpreter, numpy and the walk's 16 MiB read windows.

## 12. The read planner

`planner.py`. A parser declares each step's ranges as one batch (a sans-IO
parser's `step()`, or `read.prefetch()` from a Python parser); the planner:

- coalesces two ranges when the gap costs less than a request: gap <
  latency × bandwidth / concurrency (the median time of small requests and the
  median rate of large ones, recalibrated every 8 requests); 512 bytes where
  the server packs ranges;
- caps read amplification at 16× the bytes asked (plus 64 KiB), merging the
  smallest gaps first;
- **packs ranges into multi-range requests** (`multipart/byteranges`) where the
  server answers them: probed once with two ranges, a 200 closed before its
  body is read; packs as small as keep every connection busy, at most 100
  ranges;
- runs at most 16 requests at a time per host (each thread keeps its
  connection alive);
- retries 429, 5xx and connection errors with exponential backoff and jitter,
  waiting at least what `Retry-After` says;
- reads ahead on a walk (a one-range batch starting where the last ended),
  doubling a window from 64 KiB to 8 MiB;
- holds bytes only until they are handed over (a merged response is cut into
  its ranges and dropped; the read-ahead window is the only buffer kept);
- charges every request and byte to the budget.

Tests (`tests/ir/test_nd2.py`): the cost model and the cap, retries honoring
`Retry-After`, read-ahead on a walk, multi-range packing and the fallback when
a server answers 200.

**`xt.czi`** (100,000 subblocks of 4 KiB, 447 MB; the parser asks for 38 MB).
Round 1 measured it through the conformance proxy, which reads the whole
local file for every request (its 413 s is mostly that). Here a local range
server (`pread` per range, optional per-request latency, optionally
multipart):

| server | today | round 1 IR | round 2 IR with the planner |
|---|---|---|---|
| single ranges, 0 ms | 561 requests, 447 MB, 1.1 s | 6,831 requests (round 1, proxy) | 29 requests, 447 MB, 4.4 s |
| single ranges, 20 ms per request | 561 requests, 447 MB, 23.9 s | — | 29 requests, 447 MB, 10.6 s |
| multipart, 20 ms per request | 561 requests, 447 MB, 22.8 s | — | 1,006 requests (100,007 ranges), **38.1 MB**, 15.8 s |

On a server that answers single ranges, the cost model merges the 4 KiB gaps
(a request costs more than 4 KiB at any realistic latency), so the planner
reads everything, in 29 requests instead of 561 and with at most 18 MB held.
On a server that packs ranges it fetches exactly what the parser asked: 8.5%
of the file. The remaining time is round 1's Python CZI IR (100,000 subblocks
of records, 1 GB RSS), not I/O. The first version of the planner, with a 4×
amplification cap and no packing, made 173,693 requests: a cap that ignores
the cost model forbids exactly the merges the model wants.

**The 27 GB ND2 file** (`3-SR_1_9_6hPre-C_PlainM9_TS_MC1.nd2`, 9,010 chunks):
the parser asks for 9,010 ranges (1.75 MB) in 7 rounds. Frame headers are
1.4 MB apart, so nothing coalesces; the gains are rounds and, on Apache
(EBI serves `multipart/byteranges`), packing.

Measuring against EBI directly was not repeatable: today's profile was
refused (`Connection refused`, twice, under its 32-connection prefetch) and
then EBI refused every connection from this machine for about 80 minutes.
The one direct run that completed, the IR at 16 connections with packing:
92 s, 110 requests, 1.8 MB, 126 MB RSS. EBI spends about 100 ms per range of a
multipart request (300 scattered ranges in 3 requests took 34 s), so on EBI
packing saves requests, not time.

So the measurements below use a **sparse local copy** and a local range
server (`experiments/ir_round2/rangeserver.py`; the round-2 probes are
generated by `experiments/ir_round2/gen_nd2_probes.py`): the union of the byte
ranges today's profile and the IR read (30,332 ranges, 16.6 MB; 38 MB after
rounding to 4 KiB), taken at 64 KiB granularity (229 MB, 3,495 blocks) from
the caching proxy's blocks, which were fetched once from EBI, written at
their offsets into a file of the full 26,753,216,512 bytes. Both
implementations give the same archive from it as from the remote (every
entry, `vzip_source` included), and the IR's archive equals the one it wrote
from EBI directly. A local range server serves it with no delay, or with
EBI's measured profile: 65 ms to the first byte and 2.5 MB/s per connection
(the delays are kept by a timer thread: this machine stretches short sleeps
of background processes, 26 ms into 170 ms). Per-range server cost inside a
multipart request is not modeled.

| server | today | IR, 16 connections | IR, 6 connections (a browser) |
|---|---|---|---|
| no delay, multipart | 1.7 s, 9,552 requests, 425 MB, 251 MB RSS | 0.6 s, 111 requests (91 packed), 1.8 MB, 104 MB RSS | — |
| 65 ms + 2.5 MB/s, multipart | 599 s, 9,552 requests, 425 MB | **2.2 s**, 111 requests, 1.8 MB | 2.8 s, 106 requests |
| 65 ms + 2.5 MB/s, single ranges | 600 s, 9,552 requests, 425 MB | **36 s**, 8,457 requests, 4.2 MB | 94 s, 8,456 requests |

Today's profile reads each chunk header the source metadata checks through
its 64 KiB block cache, one block at a time (425 MB for 1.75 MB of headers);
at 2.5 MB/s per connection that is 10 minutes. (The 97 s quoted for it was
measured against EBI, whose per-connection bandwidth is higher than this
model's.) The IR asks for each range once, in seven batches, and the planner
runs them 16 at a time or packed. The other copied files, on the EBI-profile
server (multipart):

| file | today | IR |
|---|---|---|
| biad2077 373_A1 (263 MB) | 3.6 s, 58 requests, 2.0 MB | 1.3 s, 40 requests (95 ranges), 192 KB |
| biad1573 Bb54 (1.16 GB) | 7.7 s, 146 requests, 5.0 MB | 1.2 s, 36 requests (113 ranges), 253 KB |
| biad3015 1-SR MC1 (4.57 GB) | 56.4 s, 1,123 requests, 39 MB | 1.5 s, 38 requests (1,565 ranges), 646 KB |

## 13. Schema-driven validation

`rust/vzip-ir/schema/nd2.json` is ND2's source model as data: the element
vocabulary the parser emits; the limits (budgets, depths, counts); the three
objects the convention reads with each member's kind (number, integer, color,
flag, string, object, list, members), `required` (with its message),
`default`, `in`, indexed members (`sPlaneNew/a<i>` below `uiCount`) and
validity-filtered members (`pPeriod` by `pPeriodValid`, `Points` by the node's
`pItemValid`); the experiment node as a recursive type with its loop per
`eType` (members, then `count`, `scale`, `flip`, `stage` and constraints as
expressions); the derived quantities (`R`, `P`, `N`, the calibration, the
scales); and the constraints with the spec's messages (17, from "uiWidthBytes
is less than a row" to "a stage position is not finite"). The checker
(`schema.rs` with `expr.rs`, a small expression language) reads it while
parsing, after round 5, and rejects there. The projection (`nd2/image.py`)
checks nothing: it is 175 lines against today's 431 of `virtualize.py`, and it
reads facts the schema derived.

**Does schema-driven validation remove the duplication?** For the semantic
layer, yes: every member check, derived quantity and constraint of
conventions/nd2 §3 and profiles/nd2.md §5.3 is stated once and checked once;
round 1's split (a permissive parser, a projection that re-derives what must
hold) is gone, and the rejections are the same set as today's on 43 fixtures
and every probe. Three things stay code: the binary layer (chunk headers, the
map's record grammar, the LV and XML grammars, the source metadata's decode
budget and its map-order rules), which the schema only documents; the
flattening of loops (rules 1–3) and the stage translations, named builtins;
and the checker itself, about 950 lines of Rust, more than the checks it
replaces. The schema pays off only if it is shared: by a second
implementation, or by a second format with the same kind of metadata.

**Could the schema be the normative text** (ARCHITECTURE.md §6 Q1)? Its
`objects`, `types` and `constraints` sections are conventions/nd2 §3 in
tabular form, unambiguous where the prose needed sentences like "read only
from the valid members, and only from those" and "checked on every node,
whether or not the flattening visits it". They could replace §3's member
lists and its MUSTs, with the prose kept for what the schema names but does
not define (the flattening, the validity rule, the source metadata's budget).
The element vocabulary (`elements`) is the source model ARCHITECTURE.md §3.6
asks for, but as documentation; making it checkable (the parser's output
validated against it) is the next step and was not done. Recommendation:
normative member tables and constraints in the schema, normative algorithms
in prose, both versioned with the convention.

## 14. Code size

Lines (physical / non-blank non-comment), against today's ND2 profile:

| | today | round 2 |
|---|---|---|
| ND2 parser and checks | Python 1,388 / 1,194 (`virtualize/nd2`); TypeScript twin 1,493 | Rust: `nd2.rs` 1,008 / 936, `lv.rs` 570 / 516, `xml.rs` 478 / 436; schema 283 (`nd2.json`), checker `schema.rs` 684 / 636 |
| ND2 projection | (in the above) | Python `nd2/image.py` 203 / 175 and `parse.py` 23 / 17, plus about 135 reused from today (JSON sizes and tags, `Rows`, XML values) |
| shared core | `common.py` | Rust `ir.rs` 514, `check.rs` 448, `types.rs` 279, `expr.rs` 310, `py.rs` 395, `wasm.rs` 55; Python `planner.py` 414, `cmirror.py` 244 |

ND2-specific code is larger than today's Python: about 2,700 Rust lines and a
283-line schema against 1,194 Python lines, because Rust is more verbose, the
parser builds the IR (positions, record types, emission), and the checker is
an interpreter. Against today's *two* implementations (Python and
TypeScript, 2,690 lines) it is about even, and the core, the planner and the
mirror are format-free.

## 15. The wasm32 build

`cargo build --target wasm32-unknown-unknown` compiles the core (IR, checker,
types, LV, XML, schema, ND2 and CZI parsers) without the PyO3 layer: a 552 KB
module, nothing blocking. `serde_json` and `miniz_oxide` are pure Rust; the
schema is embedded with `include_str!`. `wasm.rs` exposes the ND2 parser's
step/feed/finish over linear memory (u64 offsets as BigInt), and
`wasm/nd2.mjs` drives it from Node with file reads: on all 384 test inputs it
gives the native build's IR.

**Should the parser core be the single implementation, with wasm32 replacing
the TypeScript twin?** For parsers, the checker and the core: yes, on this
evidence. One parser gives identical results in Python and in a JS host;
sans-IO means the browser's `fetch` (and its 6 connections, and multi-range
requests: 2.8 s for the 27 GB file at 6 connections on the EBI-profile
server) drives it the same way the Python planner does; and it replaces
both parsers, which today must be kept equal by hand. What is not settled:
the projections are Python, so a browser would still need them, either
ported to Rust (the mirror is format-free and small; each image projection is
about 200 lines) or kept in TypeScript over the IR; the planner's policy is
Python (it is small and would be written once more in TypeScript, or moved
into Rust as a pure function from batch and calibration to requests); and the
module adds 552 KB to the page before compression. Pyodide (ARCHITECTURE.md
§3.6) is no longer needed for the parsers.

## 16. Commits

On `feat/virtualize-conventions-v2-work`, after round 1's 3042265:

- d57f3cd feat(ir): add a compact Rust IR core with runs, a sans-IO ND2 parser and a source model schema
- 5bbaaae feat(ir): read ND2 through a read planner and the Rust parser, and project today's ND2 hierarchy
- 6cbd523 feat(ir): pack ranges into multi-range requests, coalesce by a concurrency-aware cost, and batch round 1's CZI subblock reads
- 764d458 feat(ir): decode and emit ND2 chunks one at a time, bound the mirror's derived rows, and run the parser from wasm32
- 3ebadfa fix(ir): close a planner connection before dropping it on a retry
- docs(ir): record round 2's results and answers, and add its range server and probe generator (this section)

# Round 3: TIFF and CZI on the Rust core

Round 3 moves TIFF and CZI onto round 2's Rust core: sans-IO parsers, a
source-model schema per format that holds every semantic check, Python
projections that check nothing, the read planner for both formats, and the
wasm32 build. It closes round 1's CZI review findings, each with a probe, and
rebuilds round 1's TIFF amplification probes. Nothing in `src/vzip/virtualize/`,
`web/` or the specs changes; today's profiles are the reference.

## 17. What was built

| file | what |
|---|---|
| `tiff.rs` | the sans-IO TIFF parser: classic and BigTIFF, both byte orders, the main chain, SubIFDs to depth 4, the IFDs other pointer tags lead to, each entry and its value (in line, or out of line: read when the profile reads it, or when it is at most 64 KiB, within a 16 MiB budget of such reads), strips and tiles as `data` elements (runs where they fold; an IFD whose tile tables and codec are an earlier IFD's names its tiles by alias), the OME-XML planes as aliases; then the layout (planes, levels, format, codecs, pixel sizes, translation) as facts |
| `czi.rs` | the sans-IO CZI parser: the file header; the directory, its metadata segment and attachment directory; the directory's entries in windows of up to 16 MiB; the subblocks in groups of 16,384 (their headers, then their codec headers, growing within the first 2^16 bytes of their data until each gives its coded size); the metadata XML, streamed; the attachments by form; the walk over the segments nothing references; then the layout (placement, series, layers, levels, tiles, row bands, scales, translations) as facts |
| `coding.rs`, `cxml.rs` | CZI's codec headers (Zstd0, Zstd1, JPEG, JPEG XR) and the metadata XML's values, one streaming pass with no tree (round 1's `czi/xml.py`, ported) |
| `rules.rs`, `schema/tiff.json`, `schema/czi.json` | the role checker and the two source models (below) |
| `fed.rs`, `xml.rs` | the fed bytes a parser holds; the tag scan of conventions/tiff §3 as an iterator that holds nothing but its position (round 2's scan built a list of every tag) |
| `lib.rs`, `wasm.rs`, `wasm/run.mjs` | a `Parser` trait for the three parsers; the C-ABI drives any of them (`vz_new(format, size)`), and `run.mjs` runs them from Node |
| `ir.rs`, `types.rs`, `expr.rs` | shared data sources (a JPEG prefix, once), aliases emitted by parsers, the data members of an element with runs expanded, a digest of the rows; `cstr[n]` that keeps the bytes after its NUL; list and string literals, `in`, `distinct`, `first`, `at`, `min`, `max`, `present` |

Python: `tiff/parse.py` and `czi/parse.py` drive the parsers through the
planner; `tiff/image.py` (52 lines) and `czi/image.py` (63) write today's
hierarchy from the facts and the IR's elements; `crefs.py` charges every
reference, and every repeated one, to the budget (format-free); `cmirror.py`
mirrors any compact IR. **Round 1's Python TIFF and CZI parsers and
projections are deleted** (`tiff/parse.py`, `tiff/image.py`, `tiff/ome.py`,
`czi/parse.py`, `czi/image.py`, `czi/xml.py` of round 1): today's profiles are
the reference every result here is compared against, so an oracle of round 1
would only repeat them. Round 1's Python core (`model.py`, `check.py`,
`emit.py`, `mirror.py`, `types.py`) stays, for its tests and for the helpers
the compact mirror reuses.

## 18. Schemas of roles

ND2's schema (round 2) describes metadata objects a checker reads by name.
TIFF's and CZI's semantic checks are about binary structures met in an order:
an IFD read, a field of the table, the IFDs a level uses, a directory entry, a
dimension, a subblock, an image's extents. So their schemas are made of
**roles** (`rules.rs`): each role names what the parser gives for it
(`given`), derives quantities (`derive`), and lists constraints with the
message a file that fails is rejected with. The parser's code reads the binary
layer and decides which role each thing plays, at the point today's profile
checks it; the schema decides whether it is valid. The schema's `tables`
(TIFF's tags with their kinds, scalar and used flags; CZI's pixel types and
compressions with the types each admits; units and lengths) and `limits` are
names every expression can use.

| | TIFF | CZI |
|---|---|---|
| roles | 22: header, ifd_offset, field, values, required, format, predictor, integer_attribute, ome_pixels, ome_planes, tiff_data, plane_map, subifd_levels, size, samples, level_ifd, pyramid, image, plane, tile, images, translation | 15: segment, within, file_header, entry_count, entry, dimension, entry_dimensions, subblock, metadata_sizes, attachment_entry, attachment, image, images, extent, translation |
| constraints | 47 | 32 |
| schema lines | 409 | 313 |

Every rejection of today's profiles is one of these constraints: the 40 TIFF
and 39 CZI fixtures today rejects are rejected here, each with its role's
message (`rust/vzip-ir/tests/parsers.rs`: one test per fixture), and the
rejections match today's on every input below. The projections check nothing:
`tiff/image.py` and `czi/image.py` read facts and elements, and the only checks
left in Python are the budget's (references and repeated references, in
`crefs.py`, the same for every format) and `image_ome`'s finite translation,
which today's shared helper still makes and which the schemas' `translation`
roles now reject first.

**Where the schema language reached its limits.**

- *Iteration and roles are code.* A constraint is about one thing; which
  things play a role (which IFDs a level uses, which subblocks a level places,
  each dimension of each entry) is decided by the parser, in today's order.
  The schema has no quantifier over a list (it would need one for "every
  tile's bytes are in the file" to be a schema statement and not a role the
  parser applies 25,000 times), and no way to say *where* a role applies; that
  is prose (`about_builtins`), as round 2's flattening was.
- *Order-dependent validation stays in the caller.* OME-XML's Plane selection
  reads TheZ, TheC, TheT as integers in order until one is not 0 or one Plane
  is at (0, 0, 0), and the first non-integer rejects: the integer rule is a
  role (`integer_attribute`), but which attributes are read, and when the scan
  stops, is code.
- *Expressions grew a little.* List and string literals, `in`, `distinct`,
  `first`, `at` (a table by key), `min`, `max`, `present` and booleans were
  added; a path after a call (`at(t, k).field`) is not part of the grammar, so
  derivations chain (`spec = at(compressions, compression)`, then
  `spec.pixel_types`).
- *The binary layer is code, and some of it is semantics.* Whether an IFD's
  entry count and entries lie in the file, whether a CZI id equals one of
  §2.1's (all 16 bytes, NUL-padded), a subblock's coded size from its codec
  header: the schema checks the verdicts (`count_within`, `id == expected`),
  the code computes them.
- *Cost.* The parsers evaluate a role per tile, per subblock and per
  dimension: CMU-1.svs's 25,000 tiles cost about 20 ms, and xt.czi's 100,000
  subblocks parse in 1.7 s, roles included. A role's environment is a short
  ordered list; with a map built per call (and the data forms rebuilt per
  subblock) the same parse took 2.8 s.

**Does schema-driven validation remove the duplication?** For TIFF and CZI as
for ND2: every semantic check is stated once, in the schema, with its
message, and today's two implementations (Python and TypeScript) state each
twice. It does not remove the order: a schema of roles is checked in today's
order by code that knows the format, so the schema is the *what* and the
parser the *where*. A second implementation (or the wasm32 build in a
browser) checks the same schema.

## 19. Runs: how TIFF tiles and CZI subblocks fold

| input | IR rows | runs | data elements | in runs | bytes per row (IR memory) |
|---|---|---|---|---|---|
| idr-000.ome.tiff (JPEG 2000) | 3,682 | 1,163 | 4,383 | 2,460 (56%) | 87 |
| idr-100.ome.tiff | 4,493 | 0 | 3,897 | 0 | 74 |
| idr-204.ome.tiff | 3,291 | 945 | 3,861 | 2,046 (53%) | 86 |
| svs-CMU-1.svs (JPEG) | 25,154 | 161 | 24,953 | 322 (1.3%) | 58 |
| svs-CMU-1-JP2K-33005.svs | 28,638 | 101 | 28,424 | 226 (0.8%) | 58 |
| lsm980_xzt.czi (3,575 subblocks) | 39,378 | 0 | 3,575 | 0 | 101 |
| zen395_offline.czi (JPEG XR) | 14,354 | 0 | 1,272 | 0 | 96 |
| axioscan_bgr24_test.czi | 24,226 | 0 | 2,157 | 0 | 98 |
| xt.czi (100,000 subblocks) | 900,009 | 0 | 100,000 | 0 | 93 |
| walk0.czi (2,000,000 segments) | 22 | 1 | 1 | 0 (2,000,000 segments in one run) | 170 |

- **TIFF tiles** fold where consecutive tiles have one length at one stride:
  uncompressed tiles, and compressed tiles that compress to the same size.
  On two of the three IDR inputs (JPEG 2000) more than half the tiles are in
  runs; on the SVS slides about 1% (blank JPEG tiles of equal size). A run of
  *distinct* lengths (a TileByteCounts column) does not fold.
- **CZI subblocks never fold.** A subblock's data element sits under its
  segment's struct, whose copy of its entry differs from its neighbor's (its
  T, C, Z or M start), and runs fold sibling subtrees whose values are equal.
  xt.czi's 100,000 one-line subblocks are 900,009 rows. Folding them needs
  runs of distinct values (a column per run for the fields that step, as
  round 2 proposed for frame times), not done.
- **CZI segments the walk meets fold** (walk0.czi's 2,000,000 empty segments
  are one run, 22 rows), as in round 2.
- Bytes per row of the compact IR: 58 to 101 on the corpora (CZI rows carry
  each entry's 32 + 20d value bytes twice, the directory's and the
  subblock's copy); round 1's Python IR cost about 520.

## 20. The read planner for both formats

Both parsers are sans-IO and run through round 2's planner (`drive`), so every
read of a TIFF or a CZI is a declared batch. Changes:

- **A hard cap on read amplification: 4** (it was 16): a batch fetches at
  most 4 times the bytes it asks for, plus 64 KiB, whatever the cost model
  wants, from a server or a local file. With 16, xt.czi's subblock headers
  (38 MB asked) were read as the whole 447 MB file on a single-range server;
  with 4, they cannot be. `VZIP_AMPLIFICATION` overrides it, for experiments.
  A local file's requests are served in the calling thread, without
  calibration.
- **Calibration before planning.** A batch of more than 4 × concurrency
  ranges, planned before the cost model has samples, first sends
  `concurrency` exact requests and recalibrates (latency, bandwidth), so that
  the first large batch is not planned on the defaults.
- **Whole responses.** The TIFF and CZI parsers are fed the responses as
  fetched (merged ranges are not cut), and find each range inside them: a copy
  and a call per range spared (3.3 s of feeding on xt.czi; a quadratic count
  of the bytes held, 1.9 s more, is gone too). ND2 keeps exact ranges (its
  parser takes each chunk's buffer by its offset).
- **TIFF:** the chain is one IFD a round (each names the next), read from a
  window that grows while the chain keeps to the bytes just after the last
  window (a chain written in order is a few requests: 10,000 IFDs in 13);
  SubIFDs a depth a round; out-of-line values in one batch per set of IFDs;
  the values the layout needs that were not read (tile tables larger than
  64 KiB), as one batch per level.
- **CZI:** subblocks in groups of 16,384, one batch each: an uncompressed
  subblock's 320 bytes, a compressed one's 2 KiB (its header, its metadata
  and usually its codec header, as today's profile reads them); the codec
  headers those 2 KiB do not hold in one more batch (32 bytes for Zstd, 1 KiB
  for JPEG and JPEG XR, doubled for those that need more, within 2^16);
  attachments' headers in one batch, their data (where the IR describes it by
  form) in one; the walk in windows that double from 64 KiB to 16 MiB.
  zen395_offline.czi (1,272 JPEG XR subblocks) is 1,289 ranges in 7 rounds.

## 21. Round 1's findings, closed

The probes are written by `experiments/ir_round3/gen_probes.py` (round 2's
style; `--small` writes the tests' sizes, which `tests/ir` generate at test
time). The measurements are `experiments/ir_round3/`'s scripts (`corpus.py`
through the caching proxy, `measure.py` on local files, `sparse.py` and
`remote.py` over the range server, `parity.py`, `rebuild.py`, `fuzz.py`,
`runs.py`), with `VZIP_R3_WORK` naming their work directory (the sparse
copies, the saved idr0096 listing, round 1's tree from `git archive 3042265`). Times are wall clock of one process under `/usr/bin/time -l` on local
files (round 3 starts 0.3 s slower than today: it imports the extension and
numpy); "r1" is round 1's code (3042265) run the same way.

| finding | fix | probe | today | round 1 | round 3 |
|---|---|---|---|---|---|
| row bands without a floor | `czi.rs` `bands`: divisor bands when they hold at least 1 MiB, else bands of the most rows within 2^24 bytes, the last of each tile row shorter (a rectilinear grid); the layout is the parser's now, so the floor is too | `czi_bands1.czi`: one Gray8 subblock 1 × 16,777,259 (a prime height) | 124 s, 14.6 GB, 2.09 GB archive (16.8 M chunks) | 1.1 s | 0.8 s, 64 MB, 15 KB: two chunks, `[16777216, 43]` |
| quadratic XML | `cxml.rs`: one streaming pass, no tree; an end tag closes back to the innermost open element of its name, found by a count per name | `czi_deep40000.czi`: 40,000 opens, then 40,000 unmatched end tags | 42.6 s | 0.5 s | 0.8 s, equivalent |
| XML scan memory | `xml.rs`'s scan is an iterator holding its position; the values pass keeps a path only for elements on the paths the layout reads | `czi_flat.czi`: 16 MB of `<a/>` (4 M tags) | 5.5 s, 1.76 GB | 4.0 s, 138 MB | 1.4 s, 110 MB, equivalent |
| shared FilePosition amplification | **aliasing**, per the injectivity invariant: a segment two entries name is read and checked once; the second name is an alias, whose entry is checked but not placed again | `czi_dup.czi`: 100,000 entries naming one subblock | 3.7 s, 317 MB, 68.6 MB archive (100,000 tile arrays) | 5.8 s, 2.4 MB | 2.7 s, 186 MB, 0.17 MB: 99,999 aliases, one chunk |
| coalesced prefetch reading the whole file | the planner's cap: at most 4 × the bytes asked (+64 KiB) a batch; batched subblock headers (16,384 a round) and codec heads | `czi_xt.czi`: 100,000 one-line subblocks of 4 KiB (447 MB) | reads all 447 MB (561 requests of up to 1 MiB) | — (round 2's planner: all 447 MB on a single-range server) | asks 38 MB; reads 138 MB locally, on the EBI profile with multi-range requests 41 MB in 1,006 requests (100,006 ranges), 25.4 s (today: 447 MB, 24.5 s at 32 connections); with single ranges 138 MB in 76,274 requests (§24) |
| unchecked tile extent | a tile array's y and x (its coded size) go through the `extent` role, as an image's do | `czi_jxrbig.czi`: a JPEG XR subblock whose header says 2^32 − 1 × 2^32 − 1 | accepted: a tile array of shape [4294967295, 4294967295] | accepted | rejected: "an array dimension of 4294967295, more than 2^31" |
| attachment-name bytes after the NUL | `cstr[n]` decodes to its text up to the first NUL, or to `{text, after}` when the bytes after it are not all NUL (`types.rs`), in the IR and the mirror's view; an attachment's kind is still its ContentFileType up to the NUL (conventions/czi §5.6); a segment id is compared by all 16 bytes (the fuzz found a subblock whose id had a byte after its NUL, which today rejects) | `czi_attname.czi`: names `Label\0hidden name`, `Thumb\0…x`, type `CZTIMS\0x` | the bytes after the NUL are not kept in `vzip_source` | the same | kept; hierarchy equivalent |

Three of these change the hierarchy from today's, on these probes only, by
design: `czi_bands1` (two chunks instead of 16.8 million), `czi_dup` (one image
instead of 100,000 tile arrays) and `czi_jxrbig` (rejected). No input of the
corpora, the fixtures or the mutants meets them.

Two more costs round 1 measured:

| probe | today | round 1 | round 3 |
|---|---|---|---|
| `walk0.czi`: 2,000,000 empty segments (64 MB) | 2.6 s, 718 MB, 153 KB | 105.6 s, 2.94 GB, 27.7 MB archive (4 M records) | 1.4 s, 202 MB, 15.6 KB: 22 rows (one run) |
| `evl16.czi`: 16 attachment entries naming one 1 M-event list | 14.0 s, 756 MB, 185 MB archive | 1.1 s, 202 MB | 0.9 s, 159 MB, 16 KB |

**TIFF amplification probes** (round 1's, rebuilt, and IFD loops and huge counts):

| probe | today | round 1 | round 3 |
|---|---|---|---|
| `tiff_amp_100_100`: 100 OME planes naming IFD 0, 100 tiles naming one 256 bytes | 1.1 s | 1.6 s | 0.9 s, equivalent (10,000 references, 9,999 repeated) |
| `tiff_amp_1000_1024` | 5.1 s, 915 MB | 11.4 s | 9.0 s, 935 MB, equivalent (1 M references) |
| `tiff_amp_3000_4096` | accepted: 78.5 s, 11.3 GB, 1.43 GB archive (12.3 M references) | rejected | rejected by the shared `repeats` budget (2^20): 3.7 s, 348 MB |
| `tiff_shared_10000_4096`: 10,000 IFDs naming one table of 4,096 tiles | 1.9 s, 158 MB, 6.2 MB | 10.3 s, 302 MB | 2.6 s, 163 MB, 1.1 MB, equivalent: the tile tables are read once, the IFDs' tiles are aliases (404,101 rows, 13 requests) |
| `tiff_loop_chain`, `tiff_loop_self`, `tiff_loop_sub` (a chain that loops, an IFD that is its own next, a SubIFD naming its IFD) | rejected | rejected | rejected ("read twice", the `ifd_offset` role) |
| `tiff_loop_pointers`: EXIF and GPS pointers to IFD 0 and to themselves | accepted | accepted | accepted, equivalent: aliases |
| `tiff_huge_entry_count` (a BigTIFF IFD of 2^40 entries), `tiff_huge_table_count` (TileOffsets of 2^32 − 1 values), `tiff_huge_bigtiff_value` (an offset past 2^53) | rejected | rejected | rejected |
| `tiff_huge_other_count`: a tag the profile does not read, 2^32 − 1 bytes long | accepted | accepted | accepted, equivalent; the value is not read |
| `tiff_many_ifds`: 100,001 IFDs | rejected | rejected | rejected ("too many IFDs"), 1.6 s |

The shared checks handle all of them: aliases for shared offsets (tiles,
tables, planes, pointer IFDs), the `ifd_offset` role for loops and counts, and
the reference budget for what aliases cannot prevent (12.3 million references
to one tile).

## 22. wasm32

`wasm.rs` exposes `vz_new(format, size)`, `vz_step`, `vz_feed` and
`vz_finish` for the three parsers (round 2's `nd2_*` names are kept), and
`wasm/run.mjs` drives them from Node, printing each IR's element and run
counts, the checker's verdict, a 64-bit digest of every row (`Ir::digest`:
kinds, parents, names, indexes, types, spaces, extents, runs, aliases, values,
forms, shared sources) and the facts. `parity.py` runs each input
through the native build (PyO3, fed from memory) and the wasm32 build:

| inputs | native and wasm32 agree |
|---|---|
| TIFF, CZI and ND2 fixtures (73, 71, 76) | 220 of 220 |
| round-3 probes, both sizes (18, 22) | 40 of 40 |
| round 1's CZI review probes (13, `xt.czi` and `walk0.czi` among them) | 13 of 13 |
| mutants (1,716 new; round 1's 876 TIFF and 840 CZI; the CZI review's 248) | 3,680 of 3,680 |
| all | 3,953 of 3,953 (the same digest, element and run counts, verdict and facts, or the same rejection) |

The module is 1.1 MB (552 KB in round 2): the TIFF and CZI parsers, the two
schemas (embedded), the role checker and the XML scan.

## 23. Results (2026-10-08)

**Equivalence** is `compare.differences()` of the archives without the entries
under `vzip_source` (corpora, through the caching proxy, one process each), or
the `Output`s compared directly (fixtures, mutants, probes: documents,
references and data sources), against `python -m vzip.virtualize`.
**Rebuild** is the source rebuilt from the IR archive alone
(`cmirror.rebuild_from_archive`: the mirror's table, checked by the Rust
checker, and `vzip_source/bytes`), compared byte for byte (sha-256 for the
corpus copies).

| inputs | equivalent | both reject | divergent | rebuilt from the mirror archive alone |
|---|---|---|---|---|
| TIFF fixtures (73) | 33 | 40 | 0 | 33 of 33 |
| CZI fixtures (71) | 32 | 39 | 0 | 32 of 32 |
| TIFF mutants (1,752: round 1's 876 and 876 new, `mutate.py`, seed 33) | 619 | 1,133 | 0 | 619 of 619 |
| CZI mutants (1,928: round 1's 840 and the CZI review's 248, and 840 new) | 731 | 1,197 | 0 | 731 of 731 |
| SVS (4) | 4 | 0 | 0 | 4 of 4 (copies) |
| IDR idr0096 (205) | 205 | 0 | 0 | 3 of 3 copies (idr-000, -100, -204); every IR checked |
| CZI corpus (24) | 24 | 0 | 0 | 24 of 24 (copies) |
| TIFF probes (14) | 6 | 7 | 1 by design (`tiff_amp_3000_4096`: the repeats budget) | — |
| CZI probes (9, with round 1's `walk0`, `xt`, `evl16`) | 6 | 0 | 3 by design (`bands1`, `dup`, `jxrbig`: §21) | — |

Rejections match today's on every input (the same inputs rejected), but the
four probes above, by design. One divergence was found and fixed on the way: a
CZI mutant whose subblock id had a byte after its NUL (`ZISRAWSUBBLOCK\0\xdf`),
which today rejects and the first version accepted (it compared the id up to
its NUL). No hierarchy differs on any accepted input of the corpora, fixtures
or mutants.

**Fuzz.** 286,000 further random mutations of the 144 fixtures (`mutate.py`'s
edits, one to three per input, seed 11) through both Rust parsers in memory:
75,252 accepted, 210,748 rejected, no panic, and every IR returned passes the
checker.

**EBI.** Not contacted: the IDR inputs were read through the caching proxy,
whose blocks were all cached (0 misses in every run; the listing of idr0096
was fetched once and saved). The remote measurements (§24) serve sparse local
copies built from the proxy's blocks.

## 24. Measurements

**Corpora through the caching proxy** (single ranges, no delay; one process
each, `/usr/bin/time -l`; round 3 at 6 connections; round 1's numbers are
from its own run, `src/vzip/ir/NOTES.md` §5, and its result files):

| corpus | | time (sum; median) | peak RSS (median; max) | requests | bytes read | archives |
|---|---|---|---|---|---|---|
| CZI (24) | today | 11.6 s; 0.47 s | 72; 83 MB | 8,135 | 42.4 MB | 6.17 MB |
| | round 1 | 13.6 s; 0.44 s | 78; 209 MB | — | — | 6.87 MB |
| | round 3 | 10.9 s; 0.42 s | 68; 145 MB | 7,803 | 52.0 MB | 6.22 MB |
| SVS (4) | today | 3.2 s; 0.81 s | 80; 103 MB | 32 | 2.0 MB | 7.83 MB |
| | round 1 | 3.1 s; 0.81 s | 93; 122 MB | — | — | 8.54 MB |
| | round 3 | 3.6 s; 0.81 s | 85; 108 MB | 63 | 0.9 MB | 8.23 MB |
| IDR (205) | today | 140.6 s; 0.57 s | 70; 99 MB | 1,307 | 79.3 MB | 151.9 MB |
| | round 1 | 171.2 s; 0.78 s | 75; 140 MB | — | — | 188.2 MB |
| | round 3 | 146.8 s; 0.58 s | 72; 110 MB | 1,461 | 79.0 MB | 168.6 MB |

Elements (IR rows): CZI corpus 143,690 (median 1,542), SVS 59,351, IDR
1,288,993 (median 4,099). Bytes per row of the IR in memory: 58 to 101 (§19);
round 1: about 520. The archives are within a few percent of today's for CZI
(0.39 to 1.13 × each), 1.04 to 1.37 × for SVS and 1.08 to 1.47 × for IDR: the
mirror's table holds every leaf's extent (13 bytes per element more than
today's `vzip_source` on IDR, 7 on SVS, 0.4 on CZI).

**Over the range server** (`experiments/ir_round2/rangeserver.py`, serving
the sparse copies; "fast": no delay, multi-range; "EBI": 65 ms to the first
byte and 2.5 MB/s per connection, multi-range; "EBI 1": the same with single
ranges only). Round 3 runs at 6 connections (a browser's; "c16": 16, the
planner's remote default); today's reader prefetches at 32.

| file | today, fast | round 3, fast | today, EBI | round 3, EBI | today, EBI 1 | round 3, EBI 1 | round 3, EBI 1, c16 |
|---|---|---|---|---|---|---|---|
| CMU-1.svs (178 MB) | 1.8 s, 11 req, 0.61 MB | 2.0 s, 14 req (18 ranges), 0.29 MB | 2.6 s | 2.8 s | 2.7 s | 3.0 s, 19 req, 0.38 MB | 2.2 s, 16 req |
| idr-000.ome.tiff (588 MB) | 1.2 s, 7 req, 0.34 MB | 1.2 s, 9 req (10 ranges), 0.35 MB | 1.7 s | 1.9 s | 1.8 s | 2.0 s, 7 req, 0.31 MB | 1.6 s |
| zen395_offline.czi (1.4 GB) | 1.4 s, 1,291 req, 3.34 MB | 1.4 s, 27 req (1,287 ranges), 3.32 MB | 5.5 s | **2.5 s** | 5.4 s | 16.7 s, 1,198 req, 11.0 MB | 7.0 s, 1,249 req, 4.52 MB |
| axioscan_bgr24_test.czi (141 MB) | 1.6 s, 1,887 req, 7.40 MB | 1.5 s, 37 req (2,101 ranges), 5.24 MB | 7.2 s | **3.0 s** | 7.1 s | 22.6 s, 1,678 req, 11.9 MB | 10.2 s, 1,982 req, 5.93 MB |
| lsm980_xzt.czi (5.9 MB) | 1.4 s, 32 req, 5.95 MB | 1.4 s, 44 req (3,584 ranges), 2.62 MB | 4.2 s | 3.0 s | 4.1 s | 4.3 s, 13 req, 5.93 MB | 4.1 s |
| axioscan_background_subtraction.czi (2.1 GB) | 1.4 s, 520 req, 1.61 MB | 1.2 s, 18 req (518 ranges), 0.72 MB | 3.4 s | 1.9 s | 3.2 s | 7.9 s, 509 req, 1.17 MB | 3.7 s, 513 req |
| walk0.czi (64 MB) | 6.8 s, 979 req, 64 MB | 2.0 s, 13 req, 64 MB | 99.8 s | **28.5 s** | 99.5 s | 28.4 s | 27.9 s |
| xt.czi (447 MB) | 4.4 s, 562 req, 447 MB | 7.8 s, 1,006 req (100,006 ranges), **41 MB** | 24.5 s, 447 MB | 25.4 s, 41 MB | 24.8 s, 447 MB | 922.7 s, 76,274 req, 138 MB | 332.4 s, 76,276 req, 138 MB |

- **With multi-range requests** (EBI's Apache answers them) round 3 is as fast
  as today or faster on every file at 6 connections against today's 32, and
  reads less: xt.czi's 41 MB of headers instead of its 447 MB, zen395 in 27
  requests instead of 1,291, walk0 3.5 times faster (13 requests: windows
  doubling to 16 MiB, against today's 64 KiB blocks one at a time).
- **With single ranges only**, files of many small subblocks cost round 3 a
  request per subblock or so: at 6 connections it is 2 to 4 times slower than
  today's 32 (zen395 16.7 s against 5.4 s), at 16 about even (7.0 s). xt.czi
  is the cap's price: 76,274 requests, 923 s at 6 connections and 332 s at 16,
  where today reads the whole file in 24.8 s. With the cap raised to 16
  (`VZIP_AMPLIFICATION=16`) round 3 reads the whole file too: 103.6 s, 29
  requests, 448 MB.
- The TIFF files are a few requests either way (their IFDs are a chain; the
  tile tables come in one batch per level).
- Round 1 read xt.czi through the proxy in 413 s (its 64 KiB blocks one at a
  time); round 2's planner read all 447 MB in 29 requests on a single-range
  server, and 38 MB on a multi-range one.

## 25. Code size

Non-blank, non-comment lines, against today's Python and its TypeScript twin:

| | today: Python | today: TypeScript | round 1 (Python) | round 3 |
|---|---|---|---|---|
| TIFF | 878 (`virtualize/tiff`; 299 of them `tags.py`, the source-metadata translator) | 1,216 (`web/src/virtualize/tiff`) | 618, plus 140 reused | Rust `tiff.rs` 1,712; schema 409; Python 65 (`tiff/image.py` 52, `tiff/parse.py` 13) |
| CZI | 1,129 (`virtualize/czi`; 147 of them `source.py`) | 1,447 (`web/src/virtualize/czi`) | 616, plus about 470 reused | Rust `czi.rs` 1,344, `coding.rs` 219, `cxml.rs` 250; schema 313; Python 76 (`czi/image.py` 63, `czi/parse.py` 13) |
| shared, new in round 3 | | | | Rust `rules.rs` 139, `fed.rs` 56; Python `crefs.py` 32; and additions to `ir.rs`, `expr.rs`, `xml.rs`, `types.rs`, `py.rs`, `wasm.rs` (about 450) |

Per format, round 3's code (Rust, schema and Python) is about today's two
implementations together: TIFF 2,186 against 2,094, CZI 2,202 against 2,576;
and about twice today's Python alone. The Rust is longer than the Python it
ports (the parsers build the IR, positions and record types included, and the
role calls spell out what each role is given); the projections are a tenth of
round 1's (52 and 63 lines against round 1's 303 and 267), since the layout
moved into the parser with the checks. The source-metadata translators
(`tags.py`, `source.py`) have no counterpart: the mirror is format-free.

## 26. Open

- **Runs of distinct values.** CZI subblocks and directory entries, and TIFF
  tiles of distinct lengths, do not fold; a run with a column of values per
  stepping field would fold them (xt.czi: 900,009 rows to a few dozen).
- **The cap's trade-off on single-range servers.** With amplification 4, a
  file of many small subblocks on a server without multi-range requests costs
  a request per few subblocks (xt.czi on the EBI profile: §24). That is the
  price of never reading the whole file; with multi-range requests (EBI,
  Apache) it does not arise.
- **The mirror's write.** The table's chunks are now deflated at level 1 by
  the mirror; the archive's documents are still deflated at level 9 by the
  writer. The mirror is larger than today's `vzip_source` on slides (+4% to
  +37%) and smaller on small CZI files.
- **The planner's first batches.** Through the caching proxy (single ranges,
  no delay) round 3 reads 52 MB of the CZI corpus where today reads 42 MB:
  until the cost model has samples, a batch's gaps are judged at the remote
  defaults (50 ms, 20 MB/s), so more of them are merged (within the cap).
  Bandwidth is calibrated only from requests of 256 KiB or more, which a
  parser that asks for headers rarely makes.
- **The Python core of round 1** (`model.py`, `check.py`, `emit.py`,
  `mirror.py`) is kept for its tests and the mirror's helpers; nothing
  parses with it any more.
- **Pointer IFDs** (EXIF, GPS, private IFD tags) are described for the IR only;
  they are read after the layout, so they add rounds (one per depth) but no
  checks.

## 27. Tests

`just ir-test` runs them all (Rust, then Python):

- `rust/vzip-ir/tests/rules.rs` (9): the expression language over valid
  combinations, and one test per error (an unterminated string, an unclosed
  list, trailing tokens); roles derive then check in order, and one test per
  error (an unknown role, a constraint without a message, a bad expression);
  both schemas load.
- `rust/vzip-ir/tests/parsers.rs` (88): every accepted TIFF and CZI fixture is
  a valid IR whose leaves rebuild it; one test per rejected fixture (79, each
  asserting its role's message); the TIFF probes built in Rust (one test over
  accepted probes, one per rejection: a chain that loops, an IFD that is its
  own next, a SubIFD naming its IFD, a BigTIFF entry count past the file, a
  tile table past the file, 100,001 IFDs); a CZI segment id with a byte after
  its NUL.
- `tests/ir/test_tiff.py`, `tests/ir/test_czi.py`: each fixture projects as
  today and rebuilds from its archive; the probes (generated at test time at
  `--small` sizes) decide as today; aliases for shared tiles, tables and
  subblocks; the references budget; the streaming XML values against today's
  tree; the band floor (one test over valid combinations); the tile extent;
  attachment names after their NUL; the walk's run; the planner reading
  headers, not the file, over a single-range server.

## 28. Commits

On `feat/virtualize-conventions-v2-work`, after round 2's ad3b714:

- e0363ae feat(ir): parse TIFF and CZI in Rust against source model schemas, and project today's hierarchies from them
- 17f416f feat(ir): cap the planner's read amplification at 4, drive every parser from wasm32, and compare segment ids by all 16 bytes
- ee8545a fix(ir): read a TIFF chain written in order from one growing window, and check every tile through the schema
- ceb267d perf(ir): deflate the mirror's table chunks at level 1, feed the TIFF and CZI parsers whole responses, and give roles small ordered environments
- beb60ae refactor(ir): stop comparing an aliased CZI subblock's copy with its entry
- 78b5b99 perf(ir): hold local files to the read cap too, serve them without threads, and count fed bytes as they come
- 004b44d perf(ir): read a compressed CZI subblock's header and codec header in one range
- docs(ir): record round 3's results and answers, and add its measurement scripts (this section)

# Adoption: the IR becomes the production path for TIFF, ND2 and CZI

The adoption makes the Rust core the only implementation of TIFF (but NDPI), ND2
and CZI in both `vzip.virtualize` (Python) and `web/src/virtualize` (browser and
Node): it parses, plans the reads, projects the convention's hierarchy and writes
the mirror; each host performs the I/O and writes the archive. Today's Python and
TypeScript profiles for the three formats are kept, unchanged but for their import
paths, as frozen reference oracles out of the shipped trees.

## 29. The read cap, per server

`rust/vzip-ir/src/plan.rs` (the policy, sans-IO) and `src/vzip/ir/planner.py` /
`web/src/virtualize/ir/source.ts` (the I/O). The cost model: a request costs a
latency `L`, a byte `1 / B` per connection; merging two ranges pays when the gap
`g < L × B`. What the planner learns about the server sets the threshold and the
amplification cap (a batch fetches at most cap × the bytes it asks, plus 64 KiB,
smallest gaps first):

| the server | gap threshold | cap |
|---|---|---|
| a local file | `L × B / concurrency` (fixed costs), at least 4 KiB | 4 |
| answers multi-range requests (`multipart/byteranges`, probed once with two ranges) | 512 B (ranges are packed, up to 100 a request) | 4 |
| single ranges, not yet calibrated (fewer than 4 latency samples, or no rate) | 4 KiB | 4 |
| single ranges, calibrated, and the batch's requests cost less than half a second (`n × L / concurrency < 0.5 s`) | `min(L × B, 4 KiB)` | 4 |
| single ranges, calibrated, and the batch's requests cost at least half a second | `L × B`, from 4 KiB to 8 MiB | **16** (the ceiling) |

**The ceiling and why.** On a single-range server the cost-optimal amplification
can be the whole file: xt.czi's subblock headers (380 B every 4.5 KB) are best
read by reading everything (a 4 KB gap costs 1.6 ms at 2.5 MB/s, a request 65 ms).
The hard ceiling of 16 guarantees that a batch fetches a whole file only when it
asks for at least a sixteenth of it, so header reads of real image files (a few
hundred bytes per tile, frame or subblock of tens of kilobytes or more, 1/100 to
1/10,000 of the file) never fetch the file, whatever the latency estimate says;
and `L × B` is clamped at 8 MiB, so one slow sample cannot merge everything. A
batch whose requests cost little (the caching proxy, a fast server, a small batch)
is read exactly: merging would save little time and read more bytes.

**The first batches.** The request that opens a remote source (one GET of the
first 64 KiB: size, ETag, the first bytes) is a sample for the model and seeds the
read-ahead, so the parser's first batches cost nothing. Until the model has the
server's latency (4 small requests) and rate (one request of 64 KiB or more), it
plans conservatively (4 KiB, cap 4), and a large batch first sends `concurrency`
exact requests to calibrate it (the first one 256 KiB long when no request has
measured the rate and the batch's requests would cost at least half a second).
Through the caching proxy the CZI corpus now reads **38.0 MB where today reads
42.4 MB** (round 3: 52.0 MB), in 8,368 requests against 7,995, in the same time.

Merged spans are cut so that a large read keeps every connection busy (joins stop
at about the batch's bytes over the concurrency, 1 to 32 MiB; a range asked is
never cut). A multi-range request is tried once: a server that answers it otherwise
(its body is closed unread) or fails on it gets single ranges for the rest of the
run. Python reads a remote source 32 requests at a time (the width of the reader it
replaces), the browser 6.

**Measurements** (the committed range server, `experiments/ir_round2/rangeserver.py`,
serving sparse local copies on EBI's profile: 65 ms to the first byte, 2.5 MB/s per
connection; `experiments/ir_adopt/remote.py`; "today" is the frozen reference,
which prefetches 32 at a time; wall clock of one process):

| file | server | today (32) | IR, 6 connections | IR, 16 | IR, 32 |
|---|---|---|---|---|---|
| xt.czi (447 MB, 100,000 subblocks) | multi-range | 12.7 s, 431 req, 446.7 MB | 20.9 s, 1,005 req, 41.3 MB | 12.3 s, 1,005 req, 41.3 MB | 9.8 s, 1,020 req, 41.3 MB |
| | single ranges | 12.7 s, 431 req, 446.7 MB | 35.5 s, 51 req, 447.5 MB | 17.5 s, 123 req, 447.2 MB | 12.1 s, 235 req, 446.7 MB |
| zen395_offline.czi (1.4 GB) | multi-range | 3.9 s, 1,285 req, 3.3 MB | 1.5 s, 24 req, 3.3 MB | 1.2 s, 27 req, 3.3 MB | 1.2 s, 43 req, 3.3 MB |
| | single ranges | 3.9 s, 1,285 req, 3.3 MB | 14.7 s, 1,182 req, 13.6 MB | 6.2 s, 1,183 req, 13.8 MB | 3.7 s, 1,188 req, 13.8 MB |
| walk0.czi (64 MB, 2 M segments) | multi-range | 92.0 s, 979 req, 64.0 MB | 27.1 s, 12 req, 64.1 MB | 27.1 s, 12 req, 64.1 MB | — |
| | single ranges | 92.1 s, 979 req, 64.0 MB | 27.1 s, 12 req, 64.1 MB | 27.1 s, 12 req, 64.1 MB | — |
| 27 GB ND2 (sparse copy) | multi-range | 103.2 s, 4,102 req, 81.1 MB | 2.7 s, 105 req, 1.8 MB | 2.0 s, 110 req, 1.8 MB | 1.8 s, 110 req, 1.8 MB |
| | single ranges | 101.5 s, 4,102 req, 81.1 MB | 93.8 s, 8,459 req, 4.1 MB | 36.2 s, 8,460 req, 4.0 MB | 18.9 s, 8,460 req, 4.0 MB |
| CZI corpus (23 more, sum) | multi-range | 41.9 s, 6,734 req, 39.1 MB | 29.1 s, 379 req, 30.3 MB | 28.5 s, 537 req, 30.3 MB | 28.8 s, 772 req, 30.3 MB |
| | single ranges | 41.6 s, 6,734 req, 39.1 MB | 79.4 s, 4,013 req, 167.9 MB | 48.2 s, 4,043 req, 167.8 MB | 39.2 s, 4,091 req, 167.8 MB |

- xt.czi on a single-range server: **923 s in round 3, 35.5 s now** at 6
  connections, 12.1 s at 32 (today 12.7 s): its batches ask for 1/12 of the file,
  inside the ceiling, so the planner reads it whole, cut across the connections.
- With multi-range requests the IR is as fast as today or faster at 16 and 32
  connections everywhere, and at 6 everywhere but xt.czi (20.9 s against 12.7 s:
  1,005 packs of 100 ranges, 6 at a time).
- With single ranges, at today's 32 connections the IR is as fast or faster on
  every file but two small ones of the corpus, which cost 2 or 3 more round trips
  (image1_all_dims 0.98 s against 0.78 s, zenblack_3d 1.65 s against 1.37 s: the
  parsers' rounds, which the first 64 KiB do not cover). At 6 and 16 connections,
  request-bound files are slower than today's 32: zen395's 1,182 headers are 1 MB
  apart, so nothing pays to merge, and 1,182 × 65 ms / 6 is 12.8 s whatever the
  plan. Bytes are higher on single ranges (the corpus: 168 MB against 39 MB):
  where requests dominate, the planner trades bytes for requests within the
  ceiling, which is what the cost model asks for.
- Measured on the committed range server only; EBI was not contacted.

## 30. The mirror: column runs, and its size

`rust/vzip-ir/src/mirror.rs` (documented there). The mirror's table (version 2)
stores the IR's rows depth-first and folds consecutive siblings of one shape (kinds,
names, spaces, parents; one to four sibling subtrees a member, so a CZI directory's
alternating entries and dimensions fold too) into **column runs**: member 0 is
stored; every field that differs between members (name index, type, start, start
relative to the member's first row, length, form, alias target) is a column, stored
as an arithmetic progression, as differences from the previous member (deflated),
as entries of an unsigned array the source holds (a TIFF's TileOffsets and
TileByteCounts: zero bytes in the archive), or, for a gap's index, as its start.
Coverage and injectivity stay checkable: loading expands the column runs (within the
record budget) and the round-2 checker and the rebuild run on the expanded IR;
`tests/mirror.rs` has one test over every fixture (written, read back, equal to the
IR, checked, rebuilt byte for byte) and one per malformed table. A column run is a
form of the table, not of the IR in memory: the facts and the projections address
the parser's rows (folding CZI subblocks in memory would renumber the elements the
facts name).

To get under today's size, also: TIFF tiles are no longer folded while parsing
(the parse-time runs split the column runs; the table folds them), the TIFF parser
keeps the tile tables it reads as values (the columns read from them), the table is
two arrays and an attribute (`ir/rows` [8, m], `ir/tables` [k, 7], the interned
strings in `vzip_source`'s attributes), small tables are deflated at level 9, the
leaves index the archive's source 0 (no `vzip_source/bytes`: one reference per
16 MiB, 1,600 on the 27 GB ND2), and a large value spills to the table instead of
being an array of the view.

**Fold rates** (`experiments/ir_adopt/folds.py`: rows the table stores of the IR's rows):

| inputs | IR rows | stored | column runs |
|---|---|---|---|
| SVS (5) | 59,843 | 230 (0.4%) | 54 |
| IDR (3 copies) | 13,864 | 340 (2.5%) | 40 |
| CZI corpus (24) | 143,690 | 13,239 (9.2%) | 574 |
| xt.czi and walk0.czi | 900,031 | 34 | 4 |
| ND2 copies (5) | 242,533 | 92,404 (38%) | 30 |
| TIFF fixtures (32 accepted) | 75,249 | 61,459 (82%: `edge_pointer_limit.tif`, 60,014 of its 70,055 rows; the other 31 fixtures 28%) | 144 |
| CZI fixtures (32) | 8,316 | 745 (9.0%) | 69 |
| ND2 fixtures (33) | 61,091 | 38,645 (63%: `nd2_source_names_spill.nd2`, 34,039 of 44,040, distinct names) | 33 |

ND2's LV records with distinct names do not fold (names are part of the shape).

**Archive size against today** (whole archives, the hierarchy being equal; through
the caching proxy, offline):

| corpus | equivalent | today | IR | IR / today | above today |
|---|---|---|---|---|---|
| SVS (4) | 4 | 7,829,413 B | 7,822,023 B | 0.999 | 0 |
| IDR idr0096 (205) | 205 | 151,888,438 B | 151,665,352 B | 0.9985 | 0 |
| CZI corpus (24) | 24 | 6,136,612 B | 5,409,053 B | 0.88 | 0 |
| ND2 (6 through the proxy; the 4 local copies) | 10 | 6,048,507 B (the 6) | 2,341,630 B (the 6) | 0.39 | 3 of the 6 and 2 of the copies, small files (+4.6 to +34 KB: their decoded metadata in the view; no goal was set for ND2) |

Round 3: SVS 1.04–1.37×, IDR 1.08–1.47× of today.

## 31. What moved to Rust

| | before (4a4e08e) | after |
|---|---|---|
| TIFF, ND2, CZI profiles, Python | 3,121 (`vzip/virtualize/{tiff,nd2,czi}`) | 299 shipped (`tiff/tags.py`, for NDPI); 3,199 frozen in `conformance/virtualize/reference` |
| the same, TypeScript | 3,883 | 627 shipped (`tiff/{ifd,tags}.ts`, for NDPI); 4,030 frozen in `web/conformance/reference` |
| Python IR host (planner, mirror, projections, refs, parse drivers) | 1,020 | 341 (`planner.py` 237: the I/O; `cmirror.py`, `output.py`, `__init__`, `__main__`) |
| TypeScript IR host | — | 523 (`web/src/virtualize/ir`: wasm loading, range sources, the driver) |
| Rust core | 8,735 | 12,004: + `plan.rs`/`run.rs` 608, `project/` and `refs.rs` 984, `mirror.rs` 1,027, `out.rs` 408, `wasm.rs` 202 (C-ABI), `py.rs` 594 |

(Non-blank, non-comment lines.) The planner's policy, the three image projections
(with the helpers of today's profiles they used: ND2's JSON sizes of JavaScript,
CZI's omero and codecs, TIFF's codecs), the reference budget and the mirror are now
Rust, behind PyO3 (`vzip_ir.Run`: `poll`, `complete`, `output`) and the wasm C-ABI
(`vz_run_new`, `vz_run_poll`, `vz_run_complete`, `vz_run_output`, …; an output is
one buffer, `Out::encode`). The archive writer stays per language: Python's
`Output.write` and the browser's `writeVzip` already exist, are tested by the
conformance suite against SPEC.md, and differ in what each host needs (paging,
streaming), so a Rust writer would be a third one to keep equal; the core hands
over entries, references and data sources, and each host writes them.

## 32. The switch-over

- `vzip.virtualize.virtualize` opens a file through the IR's transport (under the
  reader policy for http(s): `Policy()`, private hosts and proxies refused; the CLI
  takes `--allow-private-hosts`), sniffs its first bytes, and sends TIFF (but NDPI),
  ND2 and CZI to `vzip.ir.virtualize`; the other profiles keep their code and
  readers. `web/src/virtualize/index.ts` does the same with the wasm32 build
  (`virtualizeSource`, and `virtualizeImage` for callers with byte readers); its
  http source honors the TypeScript reader policy, through `#net`'s checked fetch
  under Node.
- The frozen oracles: `conformance/virtualize/reference/vzip_reference` (Python,
  with `cli.py` and the old tests in `tests/reference`) and
  `web/conformance/reference` (TypeScript, with CLIs; the old Node tests import it).
  They are not deleted.
- `compare.py`'s implementations are now `ref` (the frozen Python, the reference),
  `py` and `web`. For TIFF, ND2 and CZI, `ref` is compared with `py` and `web`
  outside `vzip_source`, and `py` with `web` entirely. HARNESS.md says what this
  proves: that the IR path writes what an independent implementation writes and
  rejects what it rejects, and that the two hosts drive one core alike. It no longer
  proves that two independent implementations of these conventions agree (the
  parsers, checks and projections exist once, and the frozen reference stops
  changing); the mirror has no second implementation, and its check is the
  byte-exact rebuild.
- **The conventions' §5.** conventions/{tiff,nd2,czi}/README.md §5 and their
  schema.json describe the source metadata the frozen reference writes under
  `vzip_source`. The IR writes its mirror there instead, which does not declare the
  convention and names itself (`vzip_ir`); `tests/test_virtualize_conventions.py`
  checks exactly that for these profiles. The specs were not changed: a revision
  that makes the mirror normative (or keeps §5 and ports the translators) is the
  user's decision.

**The browser bundle.** The module is a sibling file (`dist/vzip_ir.wasm`, fetched
beside the script, compiled while it streams; under Node read from the cargo build),
not inlined (base64 would add 2.1 MB to the service worker):

| | before | after |
|---|---|---|
| `vzip-sw.js` | 634,713 B (171,479 gzip) | 507,272 B (137,982 gzip) |
| `demo.js` | 65,775 B (23,294 gzip) | 62,803 B (22,116 gzip) |
| `vzip_ir.wasm` | — | 1,579,717 B (498,119 gzip) |

So a page that virtualizes loads about 463 KB more (gzip). Startup under Node:
instantiating the module 2.3 ms cold; the first virtualization of a small TIFF
10.6 ms (the frozen TypeScript 1.9 ms), the second 0.9 ms; all 220 fixtures 285 ms
(TypeScript: 682 ms). In Chromium through the service worker, the first TIFF was
ready in 51 ms, the module's fetch and compilation included.

## 33. Validation

| check | result |
|---|---|
| fixtures, 4 shards of `compare.py` (`ref`, `py`, `web`) | 667 inputs: 293 equivalent, 374 rejected by all, 0 divergent; `py` and `web` identical, `vzip_source` included |
| mutants (`just compare-mutants`: 10 per fixture, seed 0) | 5,290 inputs: 1,895 equivalent, 3,395 rejected by all, 0 divergent |
| SVS (4), IDR idr0096 (205, the saved listing), CZI corpus (24), through the caching proxy offline (`experiments/ir_adopt/corpus.py`), `ref` against `python -m vzip.virtualize`, and against the browser code (`web/conformance/virtualize.ts`) | 233 of 233 equivalent, both |
| ND2 corpus through the proxy (both hosts) | 6 equivalent; 11 not comparable offline (the frozen reference reads blocks the cache lacks, 2,378 of them; the IR none; EBI was not contacted) |
| ND2 local copies (3 whole files from EBI, the 27 GB sparse copy) | 4 of 4 equivalent, rebuilt byte for byte (sha-256) |
| byte-exact rebuild from the mirror | every accepted fixture (`tests/mirror.rs`, tests/ir), 33 of 33 accepted probes, the 4 ND2 copies, SVS/IDR/CZI sparse copies |
| hostile probes (round 2's ND2, round 3's, the CZI review's; 46) | 27 equivalent, 10 rejected by both, 9 different by design (round 3's §21: the band floor, aliased subblocks, the JPEG XR extent, the repeats budget) |
| native and wasm32 parity | parsers: 1,971 of 1,971 inputs (fixtures, probes, mutants) give the same IR digest; whole outputs: `py` and `web` identical on the 667 fixtures and 5,290 mutants |
| `just test --ignore=tests/ir`, `just ir-test`, `just web::test` | green |
| verify scripts (`just verify`, CZI, SAFE; private hosts opted in) | 0 failures (TIFF/ND2/CZI archives of the IR check their mirror's rebuild) |
| `conformance/run.py --impl ref="python -m vzip.cli --allow-private-hosts"` | 5,125/5,125 reads, 11/11 writes, 45/45 rejects, 4,973/4,973 cross-reads, 1,582/1,582 http |
| `node build.mjs`, the service worker in Chromium | builds; virtualizes a TIFF, an ND2 and a CZI |
| `just fixtures-check` (last, alone) | passes: two runs agree, and with the committed fixtures |

## 34. Open

- **The conventions' §5** (§32): the archives' `vzip_source` no longer follows the
  three conventions as written.
- **Delete the frozen twins?** They are the only independent check of the IR's
  hierarchy; deleting them leaves the mirror's rebuild and the fixtures' expected
  pixels (the verify scripts) as the checks.
- **Small files on single-range servers** pay 2 or 3 more round trips than today
  (§29); reading more than 64 KiB on open, or reading small files whole, would
  remove them.
- **Bytes against time.** Where requests dominate on single-range servers the IR
  reads up to 16× what it asks (the CZI corpus: 4× today's bytes for the same time
  at 32 connections). A cost for bytes (egress) would change the ceiling.
- **Multi-range packs** are 100 ranges (half of Apache's default MaxRanges); at 6
  connections xt.czi needs 1,005 of them.
- **wasm size**: 1.58 MB (498 KB gzip), built at opt-level 3 without LTO or
  wasm-opt.

## 35. Commits

On `feat/virtualize-conventions-v2-work`, after 4a4e08e:

- 128f2dc feat(ir)!: plan reads in Rust, with an amplification cap set by what the server does
- 314a873 feat(ir)!: project and mirror in Rust, and fold the mirror's table into column runs
- fd53c9d feat(virtualize)!: virtualize TIFF, ND2 and CZI through the Rust IR, and freeze today's profiles as the reference
- ae404af feat(web)!: virtualize TIFF, ND2 and CZI with the Rust core as wasm32, and freeze today's twins as the reference
- da09521 ci: build the Rust core natively and for wasm32 wherever the virtualizers run
- 46e3173 fix(ir): never retry a response other than 429 or 5xx, and read a remote source 32 requests at a time
- 5539739 fix(ir): fall back to single ranges when a server fails on a multi-range request, and check the mirror in the verify scripts
- docs(ir): record the adoption (this section)

# Adoption, follow-ups (2026-10-09)

## 36. The mirror is normative (VIRTUALIZE revision 21)

[conventions/README.md §8](../../../conventions/README.md#8-the-ir-mirror)
specifies the mirror format-free: elements, runs and invariants (§8.1), the
table and `vzip_source`'s source metadata `{"ir": {...}}` (§8.2), column runs
and their four encodings (§8.3), the checks and the byte-exact rebuild (§8.4),
forms and recipes (§8.5), types (§8.6), the view (§8.7). Each format's §5 is
its root's source metadata (unchanged) and its source model, the elements of
its IR: [TIFF §5](../../../conventions/tiff/README.md#5-source-metadata),
[ND2 §5](../../../conventions/nd2/README.md#5-source-metadata) (the root keeps
its decoded chunks as before), [CZI §5](../../../conventions/czi/README.md#5-source-metadata).
`vzip_source` declares the convention with `{"<p>": {"ir": {...}}}`, and so
does each view group with its document; `vzip_ir` is gone. VIRTUALIZE.md
§1.1 compares these profiles outside `vzip_source` and a mirror by its
validity and what it rebuilds (two producers may fold and encode one IR
differently); `compare.py` loads `py`'s and `web`'s mirrors and checks them
(and still requires `py` and `web` to agree entirely). The frozen references
stay unchanged, as the independent check, until the user decides otherwise
(HARNESS.md); they now record revision 21 with the previous source metadata.

## 37. Connections, the cost of bytes, small files

- **Python reads a remote source 8 requests at a time** (`connections=`,
  `--connections`); the browser keeps 6.
- **The planner charges for bytes** (`plan.rs`, `byte_cost`, settable by the
  host: `byte_cost=`, `--byte-cost` in s per MB, `RangeSource.planner` in the
  browser). A batch costs `(requests × L + bytes / B) / c` of wall time plus
  `byte_cost` per byte, so a gap pays when `g < L / (1 / B + c × byte_cost)`.
  The default, 0.1 s per MB (a megabyte read beyond what is asked must save a
  tenth of a second), stands for metered or polite access; 0 gives the
  time-optimal plan. The 16× ceiling is unchanged.
- **Small sources are read whole**: a remote source of at most 1 MiB
  (`whole_below`) is read in the request that opens it and one more for the
  rest; every batch is served from it. Larger sources, and the ceiling's
  guarantee for them, are unchanged.

EBI profile on the range server (65 ms, 2.5 MB/s a connection; "before" is
the first adoption's planner, bytes free; "free" is this planner with
`--byte-cost 0`; "asked" the parser's bytes):

| file | server | today (32) | before, 32 | 8, free | **8 (default)** | 32, free | 32 |
|---|---|---|---|---|---|---|---|
| xt.czi (asked 41.2 MB) | multi-range | 12.7 s, 431 req, 446.7 MB | 9.8 s, 1,020 req, 41.3 MB | 18.4 s, 1,005 req, 41.3 MB | 17.3 s, 1,005 req, 41.3 MB | 10.9 s, 1,020 req, 41.3 MB | 9.9 s, 1,020 req, 41.3 MB |
| | single | 12.7 s, 431 req, 446.7 MB | 12.1 s, 235 req, 446.7 MB | 28.7 s, 67 req, 447.4 MB | 28.2 s, 67 req, 447.4 MB | 12.6 s, 235 req, 446.7 MB | 12.2 s, 235 req, 446.7 MB |
| zen395_offline.czi (asked 3.3 MB) | multi-range | 4.0 s, 1,285 req, 3.3 MB | 1.2 s, 43 req, 3.3 MB | 1.5 s, 24 req | 1.4 s, 24 req, 3.3 MB | 1.4 s, 43 req | 1.2 s, 43 req, 3.3 MB |
| | single | 3.9 s, 1,285 req, 3.3 MB | 3.7 s, 1,188 req, 13.8 MB | 11.5 s, 1,182 req, 13.6 MB | 11.4 s, 1,249 req, **4.4 MB** | 3.8 s, 1,191 req, 13.3 MB | 3.9 s, 1,284 req, **3.3 MB** |
| 27 GB ND2 (asked 1.8 MB) | multi-range | 102.5 s, 4,102 req, 81.1 MB | 1.8 s, 110 req, 1.8 MB | 2.5 s, 105 req | 2.5 s, 105 req, 1.8 MB | 2.0 s, 110 req | 1.9 s, 110 req, 1.8 MB |
| | single | 101.6 s, 4,102 req, 81.1 MB | 18.9 s, 8,460 req, 4.0 MB | 70.8 s, 8,459 req, 4.1 MB | 71.0 s, 8,459 req, 4.1 MB | 19.1 s, 8,460 req | 19.1 s, 8,460 req, 4.0 MB |
| CZI corpus, 23 files (asked 29.0 MB) | multi-range | 44.4 s, 6,734 req, 39.1 MB | 28.8 s, 772 req, 30.3 MB | 30.7 s, 409 req, 30.3 MB | 30.6 s, 409 req, 30.3 MB | 30.6 s, 772 req | 30.7 s, 772 req, 30.3 MB |
| | single | 43.7 s, 6,734 req, 39.1 MB | 39.2 s, 4,091 req, 167.8 MB | 68.9 s, 4,019 req, 167.9 MB | 72.8 s, 4,870 req, **108.0 MB** | 41.1 s, 4,091 req, 167.8 MB | 44.0 s, 6,735 req, **39.8 MB** |

- The cost of bytes removes the over-reading where it saved little: on the
  single-range corpus at 32 connections 167.8 MB becomes 39.8 MB (today 39.1,
  asked 29.0) for 2.9 s more (44.0 s against 41.1, today 43.7); zen395's
  13.3 MB becomes 3.3 MB at the same time. It keeps it where it saves much:
  xt.czi still reads the whole file (its 4 KB gaps cost nothing next to a
  request). At 8 connections the threshold is higher (`c × byte_cost` is
  smaller), so 108 MB.
- At the default 8 connections the IR is faster than today wherever the server
  packs ranges, and slower where requests dominate on a single-range server
  (xt.czi 28 s, the 27 GB ND2 71 s, zen395 11 s, against today's 32-connection
  13, 102 and 4 s); `--connections 32` gives today's times or better.

Small files (single-range and multi-range alike; "before": `whole_below` 0):

| file (size) | server | today (32) | before (8) | **after (8)** |
|---|---|---|---|---|
| czi_many_attachments.czi (315 KB) | multi | 2.4 s, 705 req | 1.0 s, 5 req | 0.7 s, 2 req |
| | single | 2.4 s, 705 req | 1.1 s, 14 req | 0.7 s, 2 req |
| tczyx_uint16_deflate.ome.tif (222 KB) | multi | 0.9 s, 5 req | 1.2 s, 20 req | 0.7 s, 2 req |
| | single | 0.9 s, 5 req | 1.3 s, 30 req | 0.7 s, 2 req |
| lsm980_xt.czi (1.07 MB, above 1 MiB) | multi | 1.4 s, 8 req | 1.3 s, 14 req | 1.3 s, 14 req (unchanged) |
| svs-CMU-1-Small-Region.svs (1.9 MB) | multi | 1.0 s, 6 req | 1.1 s, 12 req | 1.1 s, 12 req (unchanged) |

Files under 1 MiB now take two requests, faster than today; files above it
are planned as before. The corpus files that paid round trips in §29
(image1_all_dims, 7.4 MB; zenblack_3d, 12.8 MB) are well above the threshold
and keep their extra rounds: reading 7 MB whole costs more at 2.5 MB/s.

## 38. The wasm module

`rust/vzip-ir/wasm/build.sh`: cargo's `wasm` profile (LTO, one codegen unit,
opt-level 2, panic=abort), then binaryen's `wasm-opt -O3` when installed
(installed here with Homebrew; CI installs it with apt), into
`rust/vzip-ir/target/web/vzip_ir.wasm`. Measured with
`experiments/ir_adopt/wasm_bench.ts` under Node (compile and instantiate cold;
the 220 fixtures through the shipped driver twice; four files from disk):

| build | raw | gzip | compile | 220 fixtures, cold / warm | xt.czi | CMU-1.svs | lsm980_xzt | 27 GB ND2 |
|---|---|---|---|---|---|---|---|---|
| before (release, opt-level 3) | 1,579,717 | 496,392 | 1.3 ms | 240 / 230 ms | 1,621 ms | 30 ms | 54 ms | 120 ms |
| **after** (wasm profile + `wasm-opt -O3`) | **1,154,187** | **436,344** | 1.2 ms | 232 / 225 ms | 1,646 ms | 31 ms | 54 ms | 117 ms |
| after, without wasm-opt | 1,372,751 | 465,533 | 1.2 ms | 236 / 227 ms | 1,736–1,792 ms | 28–34 ms | 55 ms | 126 ms |
| opt-level `s` + `-O3` (not chosen) | 1,033,549 | 401,765 | 1.1 ms | 282 / 274 ms | 1,924 ms | 34 ms | 65 ms | 139 ms |
| opt-level `z` + `-Oz` (not chosen) | 848,966 | 348,046 | 1.0 ms | 353 / 345 ms | 2,712 ms | 49 ms | 87 ms | 205 ms |

Instantiation is 0.05 ms in every build (V8 compiles lazily). The chosen build
is 27% smaller (12% gzipped) at the same speed; opt-level `s` or `z` would
save 9–20% more for 20–50% slower parsing. `wasm-opt -Oz` is no smaller than
`-O3` at opt-level 2 (1,175,444 against 1,179,971 at opt-level 3), so `-O3`.
`dist/vzip_ir.wasm` is the chosen build. Native and wasm32 parity: 1,971 of
1,971 parser digests (fixtures, probes, mutants), and `py` and `web` give the
same archives on every fixture and mutant.

## 39. Validation (after these changes)

| check | result |
|---|---|
| `just test --ignore=tests/ir`, `just ir-test`, `just web::test`, `just web::build` | green (622, 250 + Rust, 433) |
| fixtures, 4 shards of `compare.py` (`ref`, `py`, `web`, with the mirror checks) | 667 inputs: 293 equivalent, 374 rejected by all, 0 divergent |
| mutants (`just compare-mutants`) | 5,290: 1,895 equivalent, 3,395 rejected by all, 0 divergent |
| corpora through the offline proxy against the frozen reference | SVS 4/4, IDR 205/205, CZI 24/24 equivalent; ND2 6 equivalent, 11 not comparable offline (the reference needs uncached blocks) |
| archive size against today | CZI 0.884, SVS 0.999, IDR 0.999 (27 IDR files up to 450 bytes above: each view group now carries the convention's metadata object) |
| schemas | 250 IR archives of the corpora, 6,390 declaring nodes, 0 invalid; every fixture (`tests/test_virtualize_conventions.py`) |
| hostile probes and copies (50) | 31 equivalent, 10 rejected by both, the 9 by-design differences of §21; 37 of 37 accepted rebuilt byte for byte |
| native and wasm32 parity | 1,971 of 1,971 |
| verify scripts, CZI and SAFE verifiers | 0 failures |
| `conformance/run.py` | all pass |
| actionlint | clean |
| `just fixtures-check` (last, alone) | passes |

## 40. Commits

- ae3fa1e feat(ir): charge the planner for bytes, read small sources whole, and default Python to 8 connections
- 6c88012 build(wasm): build the wasm32 module with LTO, one codegen unit and wasm-opt -O3, 27% smaller
- 0f39bd5 feat(conventions)!: make the IR mirror the source metadata of TIFF, ND2 and CZI (VIRTUALIZE revision 21)
- docs(ir): record the follow-ups (this section)

## 41. The canonical mirror (VIRTUALIZE revision 22)

[conventions/README.md §8.8](../../../conventions/README.md#88-canonical-form)
specifies how a producer folds the IR into the mirror's table, one way only,
and §8.7 makes the view a function of the IR too. In short:

- **Runs** are expanded; the table has no row of code 0.
- **Order**: depth first; siblings by name group (the siblings of one name,
  the groups in order of their least first byte, then by name), then name
  index (none last), then first byte. A first attempt ordered siblings by
  first byte alone: a TIFF's tiles then sorted by offset, their indexes,
  offsets and byte counts became stored columns, and IDR archives grew 5%.
  Name groups keep layout order between differently named siblings and index
  order within a family. `finish` uses the same order for equal starts, and
  unfolds a run whose member overlaps a single element (it used to alias the
  single element).
- **Derived spaces** are kept in that order while their rows total at most
  2^20; **strings** sorted by bytes; **shared sources** numbered by first use,
  forms rewritten and sorted.
- **Column runs**: outermost first, left to right, the period (1 to 4) that
  folds the most siblings, the smaller on a tie. **Columns**: none when
  constant, else encoding 3, 0, 2, 1, the first that holds. Encoding 2 is
  no longer a search over every array the IR holds: it applies only to the
  tiles or strips of a TIFF IFD (16 or more), read from that IFD's
  TileOffsets/TileByteCounts (StripOffsets/StripByteCounts).
- **Chunks** are raw (codec `bytes` alone): the archive deflates its entries.
- **View**: values shown by type (number, record, GUID, XML) and size (at
  most 1024 bytes), whatever a parser read; the ND2 frames' timestamps are
  never shown. The TIFF parser now holds every numeric value of at most 1024
  bytes. Sizes are JSON.stringify's; `$form` and `$partial` replace members a
  child could shadow; no `"$vz": "run"`.
- **Readers accept, validators flag.** `load` takes any valid table;
  `canonical_problem` (Rust, and `vzip_ir.canonical_problem`) refolds a stored
  mirror from what it loads (the arrays of encoding 2 read from the source)
  and names the first difference. `compare.py` runs it on every archive in
  addition to comparing `py` and `web` entry for entry; VIRTUALIZE §1.1 now
  compares mirrors entry for entry.
- **The frozen reference** records revision 20 through its own constant
  (`vzip_reference/revision.py`, `web/conformance/reference/revision.ts`);
  `compare.py`, the IR tests, the corpus and probe scripts and the CZI
  verifier read its root at the current revision.

Archive sizes, offline corpus, this round against round 2 (and against the
frozen reference): SVS 0.999 (0.999), CZI 0.999 (0.884), ND2 0.991 (0.386,
the 6 comparable files), IDR 1.000 (0.999). Per file 0.947 to 1.025 of round
2; no IDR file grows by more than 26 bytes.

**Kept as they are** (the user's decisions): the planner's defaults, 0.1 s
per MB and 8 Python connections; the convention declared on every view group
(about 450 bytes on the largest IDR files); the frozen twins, until CI has
run on the IR for a while.

## 42. Validation (after these changes)

| check | result |
|---|---|
| `just test --ignore=tests/ir`, `just ir-test`, `just web::test`, `just web::build` | green (627; 250 + Rust 132; 433) |
| fixtures, 4 shards of `compare.py` (`ref`, `py`, `web`, mirror checks with the canonical one) | 667 inputs: 293 equivalent, 374 rejected by all, 0 divergent |
| mutants (`just compare-mutants`) | 5,290: 1,895 equivalent, 3,395 rejected by all, 0 divergent |
| corpora through the offline proxy against the frozen reference | SVS 4/4, IDR 205/205, CZI 24/24 equivalent; ND2 6 equivalent, 11 not comparable offline (as before) |
| the corpora's 250 IR archives | all load, check, pin source 0's size and are canonical |
| schemas | 250 IR archives, 6,347 declaring nodes, 0 invalid; every fixture |
| hostile probes and copies (50) | 31 equivalent, 10 rejected by both, the 9 by-design differences of §21; 37 of 37 accepted rebuilt byte for byte |
| native and wasm32 parity | 1,826 of 1,826 parser digests (fixtures, mutants, probes) |
| `just verify`, CZI and SAFE verifiers | 0 failures |
| `conformance/run.py` | all pass |
| actionlint | clean |
| `just fixtures-check` (last, alone) | passes |

## 43. Commits

- f6412da test(conformance): pin the frozen reference twins to revision 20
- 7aab4e2 feat(ir)!: make the IR mirror canonical (VIRTUALIZE revision 22)
- a6c6077 fix(ir): order siblings by name group, so families keep their indexes
- 1f95fde test: read the frozen reference's root at the current revision in the probes and the CZI verifier
- docs(ir): record the canonical mirror (this section)

## 44. Revision 23: text in the view, identical siblings, named array rules, the budget, the view check

The user's answers to §41's open questions:

- **Text in the view** (conventions §8.7): `ascii`, `cstr` and `utf16` values
  of at most 1024 bytes are shown by §6's rule (up to the first NUL; a string
  when UTF-8, else `{"latin1": ...}`; `utf16` a string, else its `$vz` form);
  `bytes` is not text. A run reads, after its parser, every shown value the
  parser did not keep (`run::unread_shown`); the TIFF parser keeps its short
  ASCII tags, the CZI parser its metadata XML and subblock metadata. Reading
  the subblock metadata in a batch of its own cost 23% more requests on the
  CZI corpus; read with the subblock's header (1108070), the requests are
  back to revision 22's (8,364 against 8,368) for 6% more bytes.
- **Identical siblings** (§8.8): an alias whose target lies in a sibling
  identical in every field to an earlier one names the earliest
  (`retarget`, found by digest within each block of tied siblings).
- **Named array rules** (§8.8): encoding 2 only under a profile's rule, in a
  table: TIFF's "layout tables"; ND2 and CZI none.
- **The budget** (§8.3, VIRTUALIZE §1.1): "budget: the mirror would have more
  than N rows", N = 2^22 + floor(size / 4), raised before expanding when the
  rows outside derived spaces, runs expanded, already exceed N. No source the
  parsers accept comes near it (fewer than one element per four bytes); the
  tests craft an IR (Rust) and a table (Python) past it.
- **The view check** (`view_problem`, compare.py): the view rebuilt from the
  table and the source (LV inflated, XML references decoded), compared group
  by group and member by member.

Archive size, offline corpus, against revision 22 (and the frozen
reference): SVS 1.000 (0.999), CZI 1.000 (0.884), ND2 1.000 (0.386), IDR
1.000 (0.999); per file 0.992 to 1.029 of revision 22 (at most +438 bytes).
The validator on the corpus's 250 IR archives (local cache): load and check
8.3 s, canonical 9.5 s, view 14.9 s (41,154 reads of source ranges).

| check | result |
|---|---|
| `just test --ignore=tests/ir`, `just ir-test`, `just web::test`, `just web::build` | green (628; 251 + Rust 138; 433) |
| fixtures, 4 shards of `compare.py` (with the canonical and view checks) | 667 inputs: 293 equivalent, 374 rejected by all, 0 divergent |
| mutants | 5,290: 1,895 equivalent, 3,395 rejected by all, 0 divergent |
| offline corpus against the frozen reference | SVS 4/4, IDR 205/205, CZI 24/24 equivalent; ND2 6 equivalent, 11 not comparable offline |
| the corpus's 250 IR archives | all load, check, pin source 0's size, are canonical and have the view their table gives |
| schemas | 250 IR archives, 6,347 declaring nodes, 0 invalid |
| probes and copies (50) | as §42 |
| native and wasm32 parity | 1,826 of 1,826 |
| `just verify`, CZI and SAFE verifiers, `conformance/run.py`, actionlint | pass |
| `just fixtures-check` (last, alone) | passes |

## 45. Revision 23's follow-ups

- **The browser's budget test.** The wasm module's test-only hook
  `vz_test_mirror` (cargo feature `test-hooks`) mirrors a crafted IR;
  `wasm/build.sh test` builds it into `target/web-test` in its own target
  directory, and `just web::test` builds and uses it
  (`web/test/ir/budget.test.ts`: the exact message, as an ImageError, the
  browser's rejection; and no `vz_test_` export in the shipped module). The
  shipped `vzip_ir.wasm` is byte-identical with and without the hook's code
  (1,181,746 bytes, the same 24 exports).
- **Derived transforms** (`transform.rs`): every transform a parser emits is a
  variant of `Transform` (`ALL`, kept complete by `index`), parsers write
  their forms from it, and each says whether it may hold shown values and how
  a validator derives it again. A test fails when one may hold shown values
  and cannot be derived, or when a fixture's derived element names an unknown
  transform or a space that may hold none holds a shown value; `mirror`
  refuses such a space. §8.8 makes it the rule.
- **The fill batch** is counted in the planner's statistics (`fill`: ranges,
  requests, bytes). The Rust fixture test and the Python probe test require
  it empty. On the offline corpus (SVS, CZI, ND2, IDR: 250 runs) it is empty
  everywhere: 0 ranges, 0 requests, 0 bytes.
- **Text with bytes after its NUL** is `{text, after}` in the view, as a
  record's `cstr` field. No fixture, probe or corpus archive changes, so no
  new revision (REVISIONS.md, revision 23's amendment).

Validation: the bar of §44, with the same results (fixtures 667: 293
equivalent, 374 rejected by all; mutants 5,290: 0 divergent; the offline
corpus as before, its 250 IR archives identical to revision 23's, all
canonical and with the view their table gives, the view check 14.8 s; schemas
0 invalid; probes as §42; parity 1,826 of 1,826; verify scripts,
conformance/run.py and actionlint pass); `just web::test` 435, `just ir-test`
252 + Rust 140, the Python suite 628; `just fixtures-check` last.

Commits: 9627410 test(web) (the hook and the browser's budget test), 17a6aae
feat(ir) (text after its NUL, the transform registry, the fill counts), and
this one.

## 46. Main's #35, #36 and #37

`origin/main` squash-merged the earlier stack as #33 (9e07bfa, the tree of
4f41a32), then added #35 (per-format user-story READMEs), #36 (the demo opens
inside a translated image's data) and #37 (store metadata documents read
concurrently). They were applied as a 3-way merge with 4f41a32 as the base.

- **#37 and the reader.** The read-ahead (`Store.prefetch`, 16 at a time by
  default, `--workers N`; `concurrency` in `openHttpStore`) reads through
  `HttpStore.read`, so through the per-thread kept-alive connections; each
  reading thread now closes its own as it ends, and `Store.close` the
  caller's. Store inputs are not under the reader policy, before or after
  the merge: `virtualize` checks the policy for file inputs only (its
  docstring), and the browser lists and reads stores with `fetchStore`.
- **#35's pages** described revision 16; they now describe the IR, CZI,
  SAFE, `vzip_source` and revision 23. Outputs, sizes and timings measured
  on public files were not measured again offline and are labeled as
  revision 16's.

| check | result |
|---|---|
| `just test --ignore=tests/ir`, `just ir-test`, `just web::test`, `just web::build` | green (800, with #37's 171 and one kept-alive test; 252 + Rust 140; 606, with #37's prefetch tests) |
| fixtures, 4 shards of `compare.py` | 667 inputs: 293 equivalent, 374 rejected by all, 0 divergent |
| mutants | 5,290: 1,895 equivalent, 3,395 rejected by all, 0 divergent |
| `just verify`, CZI and SAFE verifiers | 0 failures |
| `conformance/run.py --impl ref=...` | 5,125/5,125 reads, 11/11 writes, 45/45 rejects, 4,973/4,973 cross-reads, 1,582/1,582 http |
| actionlint | pass |
| `just fixtures-check` (last, alone) | passes |
