# Notes on VIRTUALIZE.md (profiles version 0, revision 2)

From a Rust implementation written using only VIRTUALIZE.md, HARNESS.md and
the parts of SPEC.md it cites. Each item gives the section, the question, what
this implementation does, and why. Items are ordered roughly by how likely two
careful implementers are to produce different output (or different
accept/reject decisions). "Literal" means I followed the text even where it
seemed odd.

All 64 fixtures behave as their names say (all `unsupported_*`,
`edge_reject_*` and `nd2_reject_*` are rejected, all others accepted). So none
of the items below is settled by the fixtures, unless noted.

---

## A. Substantive: two careful implementers could differ

### A1. §3.1: lazy or eager value reading ("a used tag", "a read outside the file")

> A used tag with an unknown field type rejects the input.

> (§1.2) That includes every input that is not well formed …: a read outside the file …

It is not said **which tags of which IFDs are "used"**, or whether a tag's
out-of-line value has to be read (and so lie inside the file) when nothing
needs it. Examples where outputs differ:

- IFD 3 (a stripped SVS label, skipped) has tag 256 with field type 99, or a
  TileOffsets value offset past the end of the file.
- IFD 0 has a `Software` tag (305, not in the table) whose value points
  outside the file.
- A non-tiled IFD being scanned for SVS levels (§3.4) has ImageWidth stored as
  type 99: is it "used"?

**Chosen:** tags are read lazily, only when the algorithm needs their value.
Tags not in the table are never read. In the §3.4 scan I read, in order:
TileWidth and TileOffsets **presence** (not values), then BitsPerSample,
SampleFormat, SamplesPerPixel, PlanarConfiguration, Compression and Predictor,
then ImageWidth and ImageLength. A bad type or out-of-file value on a tag read
before the IFD is skipped rejects; on a tag read later it doesn't. SubIFDs (330)
is read in **every** main-chain IFD, because §3.1 reads every main-chain IFD's
SubIFDs. **Suggest:** list exactly which (IFD, tag) pairs are read, in which
order, or say "every tag in the table, in every IFD that is read, MUST have a
known field type, and its value MUST lie in the file". (The second is simpler
and makes the result independent of evaluation order.)

### A2. §3.1: which field types count as integers; count-0 values

The table lists integer tags but does not say which field types they may have.

- **Chosen:** BYTE (1), SHORT (3), LONG (4), IFD (13), LONG8 (16), IFD8 (18) are
  integers. Any other *known* type on an integer tag (SBYTE, SSHORT, SLONG,
  SLONG8, RATIONAL, FLOAT, DOUBLE, ASCII, UNDEFINED) rejects as "a value of
  the wrong type". Another implementer could accept signed types holding
  non-negative values, or reject 13 or 16–18 in a classic TIFF.
- Type 13 (IFD) is not a TIFF 6.0 type (it comes from the Adobe PageMaker
  technical notes), and 16–18 are BigTIFF types. "for the field types they
  define" leaves open whether 13 is known at all, and whether 16–18 are valid
  in a classic (magic 42) file. **Chosen:** known in both variants. A SubIFDs
  tag of type 13 is common, so rejecting it would be harmful.
- A single-valued tag (ImageWidth, Compression, …) with count 0. **Chosen:**
  reject ("value missing"). For multi-valued tags with count > 1 (for example
  ImageWidth with 2 values), I use the first value. The spec says counts are
  not checked only for BitsPerSample and SampleFormat.

### A3. §3.1/§3.4: a SubIFD offset shared between IFDs, or equal to a main IFD

> reading the same offset twice (anywhere) is a cycle

So two main-chain IFDs whose SubIFDs list the same offset reject the file, as
does a SubIFD offset equal to a main-chain IFD's offset, even though neither is
a cycle in the graph sense. It also applies to SubIFDs of main-chain IFDs that
are never used as planes. **Chosen:** literal (reject). This could reject
real files that share a thumbnail SubIFD. Please confirm it is intended.
SubIFD offset 0 is read as an IFD at offset 0 (it usually rejects as garbage).
The spec doesn't say whether 0 means "no SubIFD" here.

### A4. §3.1/§3.4: IFD 0's format when IFD 0 is not a plane

> "Required" applies to the IFDs that become planes or levels

> Every image of every level MUST be tiled and have IFD 0's format.

With OME-XML, IFD 0 need not be a plane (TiffData `IFD="1"` …). IFD 0's format
(and `spp`) is still needed. **Chosen:** IFD 0's format is always computed
strictly (missing BitsPerSample or unequal BitsPerSample/SampleFormat values
in IFD 0 rejects), even when IFD 0 is not a plane. Its width, length and tile
tags are not required unless it is a plane (or the §3.4 scan needs its
width/length as the first "last level"). **Suggest:** say so.

### A5. §3.4: SVS-scan candidates that are malformed

The scan takes an IFD as the next level if it "is tiled, has BitsPerSample,
has IFD 0's format, and is strictly smaller". It does not say what happens
when a candidate:

- has unequal BitsPerSample values or unequal SampleFormat values.
  **Chosen:** it does not "have IFD 0's format", so it is skipped (the
  equal-values MUST is scoped to plane/level IFDs);
- has PlanarConfiguration 3 with spp > 1. **Chosen:** reject, because the
  table rule "then it MUST be 1 or 2" applies whenever the tag is read. That
  is inconsistent with the previous point, and another implementer could
  skip;
- lacks ImageWidth or ImageLength. **Chosen:** skip (it cannot be "strictly
  smaller"). Rejecting is also a defensible reading;
- has TileWidth/TileOffsets but no TileLength/TileByteCounts. **Chosen:** it
  becomes a level (if it matches) and is then rejected by the level checks.

**Suggest:** "a candidate whose format cannot be determined is skipped", and
say whether tag-level MUSTs (PlanarConfiguration) apply to candidates.

### A6. §3.2: the UUID "text"

> the `UUID` start tag … with its `FileName` attribute and its text.

The text is compared to detect multi-file datasets, but its extraction is not
defined:

- **Entity decoding.** **Chosen:** decoded like attribute values (the five
  entities and numeric references).
- **Whitespace.** **Chosen:** not trimmed, so `<UUID>urn:uuid:1</UUID>` and
  `<UUID> urn:uuid:1 </UUID>` name different files.
- **Comments/CDATA inside UUID.** **Chosen:** they are skipped like everywhere
  else, so CDATA content is *not* part of the text.
- **Which UUID.** **Chosen:** the first `UUID` start tag after the TiffData
  start tag, stopping at `</TiffData>`, `</Pixels>` or the next `TiffData`
  start tag.
- **TiffData without a UUID** does not name a file. So one TiffData naming
  `other.tif` plus several without UUID is accepted (only one distinct file is
  named). That is surprising: the UUID-less TiffData implicitly refers to
  *this* file. **Suggest:** state it.
- A `UUID` with `FileName=""` names the file `""`, which differs from a UUID
  with no FileName and text `""`. I keep FileName and text in separate
  namespaces, so `FileName="x"` and text `x` (no FileName) are two files.

### A7. §3.2: what is "whitespace", what is "decimal digits", how large

> Integer attributes … MUST be decimal digits, optionally surrounded by whitespace

**Chosen:** XML whitespace (space, TAB, CR, LF). Unicode whitespace (NBSP)
rejects. Leading zeros are accepted (`"007"` = 7). Values that do not fit in a
u64 reject. Another implementer could use Python `str.strip()` (Unicode) or
`int()` (which accepts `_` and non-ASCII digits).

### A8. §3.3: plane count bounds and resource limits

`SizeZ × Cp × SizeT` can be astronomically large (`SizeZ="1000000000000"`).
The spec says the input is rejected only when a plane ends up unmapped, which
needs per-plane bookkeeping. **Chosen:** overflow of the product rejects. If
the plane count exceeds (number of TiffData elements) × (main-chain length),
some plane must be unmapped (each TiffData maps at most that many distinct
existing IFDs), so I reject early. That is a valid shortcut, not a different
rule. Other implementers may run out of memory instead. **Suggest:** a
normative plane-count limit (like the 100000-IFD limit).

### A9. §3.3: a plane mapped to a missing IFD and then remapped

> Every plane MUST be mapped to an existing main-chain IFD … A plane mapped twice takes the later mapping.

**Chosen:** the check applies to the *final* mapping. A TiffData that steps
past the end of the chain is not an error if a later TiffData remaps those
planes. An implementer who checks each mapping as it is made would reject.
(This also covers a TiffData whose `IFD` attribute is beyond the chain but
whose planes are all overridden.)

### A10. §3.2: OME detection, and self-closing `Pixels`

- "its text contains a start tag whose name … is `OME`". **Chosen:** detected
  with the same tag scanner, so `<OME>` inside a comment, CDATA or processing
  instruction does *not* count (the `edge_ome_prefixed` fixture has
  `<Pixels…>` inside a comment and CDATA, but its real `<ome:OME` is outside
  them). A plain substring search would detect `<!-- <OME> -->`. Also,
  `<OME` at the very end of `D` (followed by nothing) does not count.
- `<Pixels …/>` (self-closing). "the `TiffData` start tags between that
  `Pixels` start tag and its end tag (`</Pixels>`, or the end of `X`)".
  **Chosen:** a self-closing Pixels has no TiffData. The literal reading
  ("until `</Pixels>` or the end of X") would pick up later TiffData elements
  (for example those of a second Image).
- Prefix stripping: a name like `a:b:OME` → I strip up to the *first* colon
  (`b:OME`, so no match).
- Numeric character references to non-characters (`&#0;`, `&#xD800;`,
  `&#x110000;`): **Chosen:** `&#0;` decodes to U+0000 (any Unicode scalar value
  decodes), surrogates and out-of-range values are left as they are.
- Unquoted attribute values and attributes without `=` are accepted
  leniently ("malformed XML is not detected"). The scanner's behavior on
  malformed input is inherently implementation-specific. Only well-formed
  input can be expected to agree.

### A11. §3.5/§3.6: zero sizes

Nothing forbids `ImageWidth = 0`, `TileWidth = 0` or `SamplesPerPixel = 0`.

- TileWidth/TileLength 0: **Chosen:** reject (ceil division by zero).
- SamplesPerPixel 0: **Chosen:** reject (no sensible `SizeC` default or `Cp`).
- ImageWidth/Length 0 at level 0: **Chosen:** accepted literally (shape 0, no
  tiles, `T` = 0 so TileOffsets must have 0 values). At a level ≥ 1 it gives
  an infinite scale and rejects by §1.3.

**Suggest:** require all of these to be ≥ 1.

### A12. §4.2: the compressed record's name

> type 76: the type byte and a name-length byte, 10 bytes to skip, then a zlib stream

It is unclear whether a name of `2k` bytes follows the name-length byte (as for
every other record) before the 10 skipped bytes. **Chosen:** literal: the zlib
stream always starts at offset 12, whatever the name-length byte says. If real
files have k ≠ 0 here, the two readings differ. **Suggest:** say "whatever its
value" or give the layout with the name.

Also: the inflated structure "MUST NOT itself be compressed". I reject when the
inflated data starts with type byte 76. A 76 later in it rejects anyway as an
unknown type. Empty inflated data is accepted as an empty object.

### A13. §4.2: numeric types used as integers

> A member used as a number MUST have type 2–6

Counts, widths and colors are integers, but type 6 (binary64) is allowed as a
"number". **Chosen:** where an integer is needed (uiWidth, uiCount, uiComp,
uiColor, eType, uiCompCount, uiBpc*, eCompression, uiTile*), a binary64 value
must be finite, integral, non-negative and ≤ 2^53−1, else reject. Negative
type-2/4 values reject where an integer is needed (for example `uiCount = -1`,
`eType = -1`, `uiColor = -1` as i32). Another implementer might truncate,
reinterpret `-1` as `0xFFFFFFFF` (likely for `uiColor`, which is often written
as i32), or reject every type-6 integer. **Suggest:** say which members are
integers and how to treat negative/float values. `uiColor` as i32 −1 is the
most likely real-world case.

### A14. §4.2: byte-array elements as flags/numbers

> A byte array is a list of its bytes. … as a flag type 1–5

`pItemValid`/`pPeriodValid` are byte arrays in the fixtures, so their elements
are bytes, which have no LV type. **Chosen:** a byte-array element is accepted
both as a flag (nonzero = true) and as a number. **Suggest:** say "a byte counts
as type 3 (u32)" or similar.

### A15. §4.2: paths through non-levels, and `a<i>` on lists

- `uLoopPars/uiCount` where `uLoopPars` is, say, an i32. **Chosen:** reject
  ("wrong type"). Treating it as "missing" (and taking the default) is
  equally plausible.
- `sPicturePlanes/sPlaneNew/a0` where `sPlaneNew` is a *list* (all names
  empty). **Chosen:** lists have no named members, so `a0` is missing (falls
  back to `C<k>` channels). A different reader might index the list.
- "every plane exists": **Chosen:** `a<i>` must be present; if present but not
  a level, reject (wrong type) rather than "does not exist".
- "The members of" a scalar (for example `ppNextLevelEx` is an i32):
  **Chosen:** reject. A child that is not a level: reject.

### A16. §4.3: `pItemValid` location

> The `i`-th member of `pPeriod` (`Points`) is **valid** if `uLoopPars/pPeriodValid` (the node's `pItemValid`) is absent …

The parenthetical is easy to read as `uLoopPars/pItemValid` by analogy.
**Chosen:** `pItemValid` is a member of the **node** (sibling of `uLoopPars`).
The fixtures `nd2_compressed_positions` and `nd2_edge_validity` put it there, and
the result differs between readings (3 vs 4 positions in
`nd2_compressed_positions`). **Suggest:** write the path out:
"`pItemValid` (a member of the node itself, not of `uLoopPars`)".

### A17. §4.3: missing `uiCount` in a `pPeriod` member; first *valid* member

> eType 8: the sum of `uiCount` over the members `p` of `uLoopPars/pPeriod` … that are valid

No default is given for `p/uiCount`, so by §4.2 a valid member without it
rejects. **Chosen:** literal (reject). Invalid members' `uiCount` is not read
(no rejection). A `pPeriod` member that is not a level: reject. "period: the
first valid member's `dPeriod` (default 0)": the default applies when that
member lacks `dPeriod`, not when there is no valid member (then the period is 0
too, so the outputs agree).

### A18. §4.3: spectral count "else"

> `uLoopPars/uiCount`, else `uLoopPars/pPlanes/uiCount`, else 0

**Chosen:** "else" means "if absent". `uiCount = 0` present gives 0 (skip with
children) even when `pPlanes/uiCount` is 2. Reading "else" as "if absent or 0"
gives a different loop list. (`nd2_edge_spectral`'s second spectral node has
`uiCount = 0` and no `pPlanes`, so both readings agree there.)

### A19. §4.3 rule 4: comparing only with the *last* loop

> If the last loop has the same depth and type (`eType`), and its count is less than the node's count, the node's loop replaces it. Otherwise the node's loop is dropped.

When the first sibling branch appended a deeper loop (time d0 → position d1 →
z d2), the second sibling position (d1) is compared with the *last* loop (z,
d2): depth 2 > 1, so it is dropped even if it has more points. **Chosen:**
literal. As a result, replacement can only happen when the earlier sibling
appended no descendants (otherwise a descendant is the last loop). This is
consistent but hard to see. An example with grandchildren would help.

"Two loops of the same kind": **Chosen:** kinds are time (eType 1 and 8),
position, z. So eType 1 and eType 8 at different depths reject, but eType 1
and 8 at the same depth do not merge under rule 4 (rule 4 compares `eType`).
At the same depth, the later one is dropped (it is not deeper and not the same
type). Please confirm.

### A20. §4.3: `SLxExperiment` / `SLxPictureMetadata` absent from a present chunk

> **Experiment** (chunk `ImageMetadataLV!`, member `SLxExperiment`; if the chunk is absent there are no loops)

> **Picture metadata** (chunk `ImageMetadataSeqLV|0!`, member `SLxPictureMetadata`; optional)

**Chosen:** chunk present, `SLxExperiment` missing → reject (a missing member
without a default). Chunk present, `SLxPictureMetadata` missing → treated as
absent (the whole thing is "optional"). Either could go the other way.

### A21. §4.3/§4.5: what is read lazily in picture metadata

If the plane check fails (a plane missing, a compCount not 1/3, or a sum
≠ `uiComp`), channels fall back to `C<k>`. **Chosen:** `uiCompCount` of every
plane is read (type errors reject). `sDescription` and `uiColor` are read only
when the plane labels are used, so a bad `uiColor` type is ignored on fallback.
`sDescription` of a non-string type: reject. Same issue as A1: say whether
every listed member is validated.

### A22. §4.4: frame chunk names

> Frame `f` is the chunk named `ImageDataSeq|<f>!` (decimal)

**Chosen:** only the canonical decimal (no leading zeros, no sign) matches.
`ImageDataSeq|01!` is not frame 1 and is ignored. Values ≥ 2^64 are ignored.
`N` is computed without overflow (u128, saturating).

### A23. §4.4: uncompressed frames other than the lowest/highest

Only the lowest- and highest-numbered present frames' headers are read. The
others' map offsets are used with the first frame's name length, unchecked
(the magic is not verified), apart from the range-in-file check. This is
literal, but it means a corrupt map entry for a middle frame yields a bogus
reference instead of a rejection. Fine if intended, but it should be said
outright that middle frames are not validated.

### A24. §4.1: "after removing NUL padding" and map details

- Chunk-map chunk name: **Chosen:** strip *trailing* NULs and compare. (A name
  like `ND2 FILEMAP SIGNATURE NAME 0001!\0junk` fails. Truncating at the first
  NUL would accept it.)
- Bytes after the terminating `ND2 CHUNK MAP SIGNATURE 0000001!` record in
  the map data: ignored.
- A map record whose 16 offset/size bytes are truncated: reject.
- Map record offsets are checked (≤ 2^53−1, magic) only when the chunk is
  used. §1.2's "an offset … above 2^53 − 1" rejects could be read as covering
  every offset in the map.
- Signature: the order of checks (file ≥ 40 bytes vs signature) does not
  matter, since both reject.

### A25. §1.3/§4.6: NaN/inf read from the file (not computed)

> A number that would be infinite or NaN rejects the input.

Stored binary64 values can be NaN/inf. **Chosen:**
- `dZStep = NaN` → `abs` is NaN → reject. `dZStep = inf` → reject.
- `dPeriod = NaN` → "the period is positive" is false → no unit, scale 1 (no
  computation happens), *not* a rejection. `dPeriod = inf` → `inf/1000` → reject.
- `dCalibration = NaN` → not positive → uncalibrated. `dAspect = NaN` → "not
  positive" → 1.

These are inconsistent but follow the text. **Suggest:** "a NaN or infinite
binary64 member rejects the input", or "counts as absent".

---

## B. Clarity: probably agreed, but worth a sentence

- **§1.3 / HARNESS "integers are written as integers".** Scales that happen to
  be integral (1, 2, 300) are computed numbers. I write them as `1.0`, `300.0`
  (Rust shortest round-trip), and integers (shape, chunk shape, fill value,
  window, `bioformats2raw.layout`, transpose order, `level`) as integers.
  Under §1.1 they compare equal anyway. Say whether scales must be written
  `1` or may be `1.0`.
- **§3.1 IFD reading order and the limit.** The main chain is read fully
  first, then SubIFDs in main-chain order. The 100000 limit counts SubIFDs
  too. Clear enough.
- **§3.2 ImageDescription value out of file.** The tag is "used" and read, so
  this rejects (even when it would not be OME). A non-ASCII type (for example
  UNDEFINED in `edge_ome_undefined_type`) still has its value read, so an
  unknown *type* on IFD 0's ImageDescription rejects.
- **§3.2 PhysicalSize regex and conversion.** Correct rounding from the
  matched string. `PhysicalSizeX="1,5"` → absent (fixture). A unit attribute
  given with an absent size → no unit. Units are matched exactly (no trimming,
  case-sensitive).
- **§3.3 PlaneCount default.** "defaults to the plane count when it is the
  only `TiffData` and has no `IFD` attribute": `First*` attributes don't
  matter. With `FirstZ = 1` the stepping just stops at the last position.
- **§3.6 Units/axes interplay.** The z unit is only emitted when the z axis
  exists (SizeZ > 1). The unit is dropped silently if `PhysicalSizeZ` is
  present but SizeZ = 1. Fine, but worth an example.
- **§3.6 Chunks, planar data with `c` absent.** Impossible (spp > 1 implies a
  c axis), but a sentence would help readers.
- **§4.2 top level.** The chunk's top-level record sequence is treated as an
  object/list by the same rule as a level (so a top level whose records all
  have empty names is a list and has no `SLxImageAttributes`). Say so.
- **§4.2 level `8c` skip running past the data.** Rejects (truncation).
- **§4.2 type 8 strings without a terminating NUL before the data end.**
  Reject (truncated).
- **§4.2 record names.** The name is the units up to the first NUL, *decoded
  how*? I decode with replacement of unpaired surrogates (same as type 8).
  This matters only for matching names, which are ASCII in practice.
- **§4.3 `bCalibrated` absent.** Treated as false (no default given; a missing
  member without a default would *reject*, which is surely not intended). Same
  for `dCalibration` absent ("present and positive" already handles it).
  Please give `bCalibrated` an explicit default.
- **§4.3 the 'step' when count ≤ 1.** `abs(dZStep)` only, so a single-plane z
  loop with dZStep 0 has no unit.
- **§4.5 uiColor high byte.** `AA` is ignored.
- **§4.6 omero window.** `b` up to `uiBpcInMemory` ≤ 16 for integer types, so
  `V` fits easily. Note that `uiBpcSignificant` is *required* (§4.3) even though
  only the window uses it, and it is unused for float32.
- **§5.** The conformance section names files implementers are told not to
  read. Not a problem for the spec, but implementers can't check them.

---

## C. Rules that may cause trouble

1. **A3 (shared SubIFD offset = cycle).** May reject valid files.
2. **A23 (middle frames unchecked).** Silent bogus references on a damaged
   map. Consider reading every frame header (as compressed files already
   require) or say explicitly that it is not done.
3. **A1 (lazy reading).** Without an explicit list, acceptance depends on
   evaluation order. This is the largest source of reject/accept divergence on
   damaged files I can see.
4. **A13 (`uiColor` as signed i32).** If writers store `uiColor` as type 2,
   colors with the high bit set (`AA` ≥ 0x80) reject under my reading.
5. **The 65519-byte payload limit** (§1.2) rejects ND2 files with padded rows
   and more than ~7000 rows (each row is a separate range of about 9 bytes).
   That is by design, but large padded images are plausible in practice
   (`nd2_reject_payload` exercises it).

## D. Things I needed that were not stated

- HTTP: what to do with `200` (full body) instead of `206`. I treat anything
  but `206` as a failure (not a rejection), with retries on network errors
  and 5xx.
- Exit status for failures: HARNESS says only "any other status". I use 2.
- Whether a virtualizer must validate `U` (§1.2 "MUST be a valid absolute
  URI"). I don't. Rejecting would be a third outcome that isn't listed.
