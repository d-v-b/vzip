# Notes on VIRTUALIZE.md (revision 2), from an independent Python implementation

Implementation: `impls/virtualize/python/virtualize.py` (standard library only),
run as `impls/virtualize/python/virtualize <url> <out.json>`. It was written from
VIRTUALIZE.md, HARNESS.md and SPEC.md alone.

Issues are ordered roughly by how likely they are to make two careful
implementations disagree. "Decided" says what this implementation does.

---

## A. Substantive: two careful readers can produce different output or a different accept/reject

### A1. §4.2 / §4.3 / §4.5: when is a member "used"? (lazy or eager type checking)

§4.2: "A member used as a number MUST have type 2–6, and as a flag type 1–5
... a missing member without [a default] rejects the input." Whether a
wrongly typed member rejects therefore depends on whether it is *used*, and
the document does not say when evaluation is conditional. Examples:

- `dCalibration` is a string while `bCalibrated` is false. Is `dCalibration`
  used? ("calibrated if `bCalibrated` is true and `dCalibration` ... is
  present and positive").
- A picture plane's `uiColor` or `sDescription` is wrongly typed, but §4.5's
  condition fails (counts do not add up to `uiComp`), so the planes are not
  used for labels.
- `uiColor` of a 3-component plane (which is colored `FF0000`...) is wrongly
  typed.
- `dZHigh`/`dZLow` are wrongly typed but `dZStep` is nonzero.
- `uLoopPars/dPeriod` of a node whose loop is later dropped by rule 4, or the
  loop parameters of a node whose count turns out to be 0.
- `pPeriod` members after an invalid one.

Decided: lazy, short-circuit evaluation in the order the text is written:
`dCalibration` is only examined when `bCalibrated` is true; plane
`uiCompCount`s are examined only if every plane exists; `sDescription` and
`uiColor` only when the §4.5 condition holds, and `uiColor` only for
1-component planes; `dZHigh`/`dZLow` only when the step is 0 and the count is
more than 1; `dAspect` is always examined when picture metadata is present.
Suggest the spec states either "every member named in this section that is
present is type-checked" (eager, simplest to test) or lists the conditions.

### A2. §3.1: which TIFF field types are acceptable for the integer tags, and what a count of 0 or >1 means

"Values are read as TIFF 6.0 and BigTIFF define them, for the field types
they define." This says how to decode, not which types are acceptable for
ImageWidth, TileOffsets, Compression etc. Open questions:

- ImageWidth stored as RATIONAL, FLOAT, DOUBLE, ASCII or UNDEFINED: reject,
  or convert?
- Signed types (SSHORT, SLONG, SLONG8) holding negative values.
- A scalar tag (ImageWidth, Compression, Predictor, PlanarConfiguration...)
  with count 0: default, or reject? (The spec only says "A `SubIFDs` tag with
  no values means no SubIFDs", which by contrast suggests other empty tags
  are something else.) BitsPerSample/SampleFormat "counts are not checked" -
  does that include count 0?
- A scalar tag with count > 1 (ImageWidth with two values): first value, or
  reject?

Decided: integer types only (BYTE, SHORT, LONG, SBYTE, SSHORT, SLONG, IFD,
LONG8, SLONG8, IFD8); any other known type on a used integer tag rejects;
negative values reject; count 0 rejects (except SubIFDs); count > 1 uses the
first value. Suggest an explicit table of allowed types and an explicit
rule for counts.

### A3. §3.1/§3.4: zero sizes in TIFF

Nothing says ImageWidth, ImageLength, TileWidth or TileLength must be at
least 1 (§4.3 says so for ND2). TileWidth 0 makes `ceil(W/TileWidth)` a
division by zero; ImageWidth 0 gives `T = 0` and a scale `W0 / WL` that is
infinite or NaN. Decided: reject any of the four being 0 on a plane/level IFD.

### A4. §3.4: the SVS level scan and malformed candidate IFDs

The scan looks at every later main-chain IFD and decides "tiled, has
BitsPerSample, has IFD 0's format, strictly smaller". For a candidate that
does *not* become a level:

- a missing ImageWidth/ImageLength (cannot compare sizes): skip or reject?
- PlanarConfiguration 3 (with SamplesPerPixel > 1): "then it MUST be 1 or 2"
  - reject, or simply "not IFD 0's format" and skip?
- BitsPerSample (or SampleFormat) with unequal values: which value is "the"
  BitsPerSample of the format tuple? The equality requirement is only for IFDs
  "that become planes or levels".
- An unknown field type on one of its format tags (a "used tag"?).

Decided: missing width/length skips; PlanarConfiguration outside {1,2}
rejects; unequal BitsPerSample/SampleFormat use the first value for the
comparison (and are then rejected by the equality check if the IFD becomes a
level); unknown field types reject. Each of these could reasonably go the
other way. Suggest: "a candidate whose format cannot be determined is
skipped", or say that every used tag of every main-chain IFD is validated.

### A5. §3.1: are values of unused tags read at all?

Values are presumably read lazily, but the text does not say so. An eager
reader that resolves every entry's value offset rejects files whose unused
tags (e.g. a garbage StripOffsets in a thumbnail IFD, or a vendor tag with a
bad offset) point outside the file; a lazy reader accepts them. Only "a used
tag with an unknown field type" is mentioned. Decided: lazy (only tags this
document uses are decoded; IFD entry tables themselves are always read and
must lie within the file). Suggest stating this explicitly.

### A6. §3.3: IFD 0 when it is not a plane

With OME-XML the planes need not include IFD 0 (`<TiffData IFD="1" .../>`),
yet `spp`, "IFD 0's format", and "IFD 0 has SubIFDs / IFD 0's SubIFD count"
all come from IFD 0. "Required" tags are only required for IFDs "that become
planes or levels", so is a missing BitsPerSample on IFD 0 a rejection when IFD
0 is not a plane? Decided: IFD 0's format must be complete (BitsPerSample
required) because the format comparison needs it. Suggest saying so, or
taking the reference format/spp/SubIFD count from the IFD of plane (0,0,0).

### A7. §3.2: what counts as "a start tag whose name ... is `OME`"

The detection is described as a text property ("its text contains ... `<OME`
or `<prefix:OME`, followed by whitespace, `/` or `>`"), while the rest of
§3.2 is a tag scan that skips comments/CDATA. Questions:

- `<OME ...>` appearing only inside a comment, CDATA section or attribute
  value: is `D` OME-XML? (Then `OME/METADATA.ome.xml` is written and the
  scan finds no `Pixels`.)
- What characters may the prefix contain? (`<a.b-c:OME>`, `<:OME>`,
  `<a:b:OME>`.)
- "whitespace": XML whitespace (space, tab, CR, LF) or Unicode whitespace
  (e.g. U+00A0, U+3000)? The same question applies to "decimal digits,
  optionally surrounded by whitespace" in the integer attribute rule.

Decided: plain text search with regex `<([^\s<>/:!?="']+:)?OME[ \t\r\n/>]`
(so an `<OME` in a comment counts); XML whitespace only, everywhere.
Suggest: detection = "the tag scan finds a start tag with local name `OME`",
and define whitespace as the four XML characters.

### A8. §3.2: OME-XML without a `Pixels` start tag; self-closing `Pixels`

- If `X` has no `Pixels` start tag, do all Size* default (one plane, SizeC =
  spp, implicit TiffData) or is the input rejected? Decided: defaults.
- If the first `Pixels` start tag is self-closing (`<Pixels .../>`), "the
  `TiffData` start tags between that `Pixels` start tag and its end tag
  (`</Pixels>`, or the end of `X`)" could mean "none" or "everything up to
  the next `</Pixels>`/end of X". Decided: none (the implicit TiffData is
  used).

### A9. §3.2: attribute parsing details

The scanner rules don't say:

- which of duplicate attributes wins (`<Pixels SizeZ="2" SizeZ="3">`).
  Decided: the first.
- whether unquoted values (`SizeZ=2`) are accepted. Decided: no (the
  attribute is ignored).
- how a start tag's end is found when an attribute value contains `>`.
  Decided: `>` inside a quoted value does not end the tag.
- which numeric character references are decoded: `&#0;`, `&#xD800;`,
  `&#x110000;`, `&#X41;` (uppercase X, invalid in XML), `&#65` (no `;`).
  Decided: only `&#[0-9]+;` and `&#x[0-9A-Fa-f]+;` whose code point is a
  valid non-NUL, non-surrogate scalar value ≤ U+10FFFF; everything else is
  left as written.

### A10. §3.3: UUID identity for the multi-file rule

"if the `TiffData` elements' `UUID`s name more than one distinct file (by
`FileName`, or by UUID text when there is no `FileName`)". Open questions:

- Is the UUID text entity-decoded? Whitespace-trimmed? (`<UUID>urn:uuid:1
  </UUID>` vs `<UUID>urn:uuid:1</UUID>`.) The decoding rules in §3.2 cover
  attribute values only.
- Is a UUID with `FileName="a.tif"` and text `T` the same file as a UUID
  with no FileName and text `T`? Literally they are compared in different
  name spaces, so the input is rejected as multi-file although both UUIDs are
  identical.
- Is `FileName=""` a FileName?
- Which `UUID` is used if a TiffData contains two?

Decided: text is entity-decoded and trimmed of XML whitespace; identity is
the pair (kind, value) with kind "FileName" or "text", so the mixed case above
is rejected; an empty FileName is a FileName; the first UUID start tag is
used. Suggest: identify a file by its UUID text when every UUID has text, and
specify trimming.

### A11. §3.2: PhysicalSize whitespace

Integer attributes may be "surrounded by whitespace", but a PhysicalSize
"counts as present only if it matches `[+-]?(...)`". Is the match a full
match of the raw value (so `" 0.5"` is absent), or a search? Decided: full
match, no whitespace. The asymmetry with integer attributes is surprising;
suggest the same whitespace rule for both. Similarly `DimensionOrder`
(`" XYZCT"`) and the unit attributes are compared exactly.

### A12. §4.3: "uiCount, else pPlanes/uiCount, else 0" (spectral)

"else" can mean "when absent" or "when absent or 0". The fixture
`nd2_edge_spectral.nd2` has a spectral node with `uiCount` = 0 present and no
`pPlanes`, which gives 0 either way, so the fixtures don't distinguish.
Decided: "when absent". Suggest: "`uLoopPars/uiCount` if present, else ...".

### A13. §4.3: eType 8 members without `uiCount`

eType 1 says `uiCount` "(default 0)"; for eType 8, "the sum of `uiCount` over
the members `p` of `uLoopPars/pPeriod`" gives no default, so by §4.2 a valid
`pPeriod` member without `uiCount` rejects the input. Is that intended?
Decided: reject (required). Same for each valid member being a level at all
(a `pPeriod` member that is an integer).

### A14. §4.2: following a path through a non-level value

`uLoopPars/uiCount` where `uLoopPars` is an `i32`; `sPicturePlanes/uiCount`
where `sPicturePlanes` is a string; `sPlaneNew/a0` where `sPlaneNew` is a
list (its members have empty names); `ppNextLevelEx` being an integer; a child
of `ppNextLevelEx` that is not a level (where `eType` is required);
`pItemValid` that is a scalar. Is the member missing (default applies), or is
it "a value of the wrong type" (reject)? Decided: descending into a
non-level scalar rejects; looking up a name in a list gives "missing";
"members of" a scalar rejects; "members of" a byte array are its bytes. A
§4.5 plane `a<i>` that is a scalar therefore rejects (rather than "does not
exist"). Suggest stating one rule.

### A15. §4.2: numbers that must be integers

Counts, sizes and colors may be type 6 (binary64) per "A member used as a
number MUST have type 2–6". What does `uiWidth = 4.5`, `uiCount = 2.5`, or
`uiColor = 1e300` mean? Decided: a binary64 value used where an integer is
needed must be integral (else reject) and is converted exactly. Also,
negative values: a negative `uiCount` (i32) — decided: reject immediately when
the node's count is computed (even if the loop would later be dropped);
negative `sPicturePlanes/uiCount` — decided: no planes; negative or
out-of-u32 `uiColor` — decided: masked as two's complement bytes.
`uiTileWidth` negative is accepted ("positive and differs" is the only
rejection), as written.

### A16. §4.2: elements of byte arrays have no LV type

"A byte array is a list of its bytes" and `pItemValid` is normally a byte
array, but the flag rule says "as a flag type 1–5". A byte has no LV type.
Decided: bytes count as numbers/flags. Suggest saying "a byte array's
members are `u8` values usable as numbers and flags".

### A17. §4.2: compressed record name

"the type byte and a name-length byte, 10 bytes to skip, then a zlib stream".
Is the name-length byte ignored (always 10 bytes skipped), or are `2k` name
bytes skipped and then something else? Decided: the name-length byte is
ignored and exactly 10 bytes are skipped (data[12:] is the stream). If real
files always have k = 0 or the 10 bytes include the name, say so.

### A18. §4.3: chunk present but member absent

- `ImageMetadataLV!` present without `SLxExperiment`: "if the chunk is
  absent there are no loops" — is a present chunk without the member a
  rejection (missing member without default) or "no loops"? Decided: reject.
- `ImageMetadataSeqLV|0!` present without `SLxPictureMetadata` ("optional"):
  decided: treated as absent picture metadata.
- `ImageMetadataSeqLV|0!` present but its LV is malformed: reject (decided),
  although the data is "optional".
- The asymmetry is surprising; suggest the same rule for both.

### A19. §4.3 rule 4: what "the node's loop replaces it" carries

When a later sibling with a larger count replaces the last loop, does the
replacement carry the new node's period / z step too? Decided: yes, the
whole loop (count, period, step) is replaced. Worth one sentence.

### A20. §4.1: the chunk map terminator record

"ending with the record named `ND2 CHUNK MAP SIGNATURE 0000001!`". Must the
terminator be followed by its `u64` offset and `u64` size (16 bytes) within
the data? Must it be the last thing in the data, or is trailing data
ignored? Decided: scanning stops at the terminator name; its offset/size are
not required and bytes after it are ignored. A stricter implementation
rejects files the lenient one accepts.

### A21. §4.4: frame chunk names

"Frame `f` is the chunk named `ImageDataSeq|<f>!` (decimal)". A map name
like `ImageDataSeq|01!` is not the name of any frame under a strict reading;
an implementation that parses the number with `int()` maps it to frame 1.
Decided: only the canonical decimal form (no leading zeros, no sign).
Suggest saying so explicitly.

### A22. §3.3: large plane counts / work bounds

There is a limit of 100000 IFDs, but none on `SizeZ × Cp × SizeT` or on
`PlaneCount`. Since several planes may map to the same IFD (nothing forbids
it), a 100-byte OME-XML can ask for 10^12 planes, and a conforming
virtualizer must then either iterate or prove they cannot all be mapped.
Decided: if the sum over TiffData of `min(PlaneCount, plane count)` is less
than the plane count, reject early; otherwise iterate. Suggest a limit on the
plane count (e.g. ≤ the number of main-chain IFDs, or ≤ 100000), or forbid
two planes sharing an IFD (which would also prevent duplicate references).

---

## B. Rules that may be wrong or cause trouble

### B1. §1.2 + §4.4: the 65519-byte payload limit rejects ordinary padded ND2 files

Uncompressed frames with `uiWidthBytes > R` become `uiHeight` ranges in one
Concat. Each part costs about 10–12 bytes (tag, length, offset varint of 4–5
bytes, length varint), so any padded frame taller than roughly 5500–6500
rows is rejected (`nd2_reject_payload.nd2` exercises this). Odd-width RGB
cameras at high resolution, or stitched large images, hit it. That is a
conformance-consistent but probably unwanted rejection; consider allowing a
strided representation or chunking a frame into row blocks (several Zarr
chunks along `y`).

### B2. §3.1: "reading the same offset twice (anywhere) is a cycle"

Two main-chain IFDs whose SubIFDs share a SubIFD (or a SubIFD offset equal to
a main-chain IFD) are rejected as a cycle although no cycle exists. Probably
acceptable, but it is not really a "cycle"; say "every IFD offset must be
distinct". Also: is a SubIFD offset of 0 read as an IFD at offset 0 (the
header)? Decided: yes, read literally (it will normally fail or parse
garbage).

### B3. §3.1: the limit wording

"Every IFD read counts toward a limit of 100000": is the 100000th IFD allowed?
Decided: up to 100000 accepted, the 100001st rejects.

### B4. §3.3 Coverage: First* checked even for planes past the end

"each MUST be less than SizeZ, Cp, SizeT respectively, else the input is
rejected" means `FirstC="1"` on an RGB file (Cp = 1) with OME `SizeC="3"`
rejects. Some writers emit `FirstC` per channel for RGB OME-TIFFs (one
TiffData per sample). That may be intended, but it's worth noting.

### B5. §4.3: two time loops of different eTypes in sibling branches

Rule 4 compares "type (`eType`)", so siblings with eType 1 and 8 are never
merged; the second is dropped (same depth, different type), not rejected.
Two time loops at different depths are rejected. Probably intended; noting
that "type" here means eType and "kind" (final check) means time/position/z.

### B6. §4.4: uncompressed name length taken from two frames only

Only the lowest and highest frames' headers are read; every other frame's
pixel start uses their name length. A file with a differently named middle
frame silently gets wrong references. This is a deliberate "structure only /
few reads" choice, but it contradicts §1.2's "a structure that is
truncated or inconsistent" rejects; say explicitly that the other headers are
not read or checked.

---

## C. Things I needed that the document did not say (minor)

- §1.2: a file shorter than 4 bytes: "first bytes decide" — decided:
  rejected (not a known signature).
- §1.2/§1.3: "an offset or length above 2^53 − 1" — applied to IFD offsets,
  tag value offsets, TileOffsets/TileByteCounts values, chunk offsets from the
  ND2 map (only those that are used) and chunk data lengths read.
- §2.3: units are matched by exact code points (no Unicode normalization),
  so U+00C5 and U+212B are both listed and both map to `angstrom`; any other
  spelling (e.g. `A` + combining ring) gives no unit.
- §3.2: UTF-8 validity: decided strict (surrogates and overlong forms are
  invalid), via Python's strict decoder.
- §3.6 scales: integral binary64 results (e.g. `H0/HL = 2.0`) are written as
  JSON integers; this is harmless because comparison is by binary64 value.
- §4.1: the chunk map chunk's name is compared after stripping trailing NULs
  only ("after removing NUL padding"); other chunk names are not checked.
- §4.1: map record names are compared as raw bytes.
- §4.2: names: unpaired surrogates in record names are replaced by U+FFFD as
  for strings (the text says so only for type 8).
- §4.2: the top-level LV structure of a chunk is treated as an object even if
  all its names are empty (the list rule is stated for "a level").
- §4.5: `uiColor` is read as an integer and masked (`v & 0xFF` red, etc.).
- HARNESS: the wrapper runs `python3`; on this machine that is CPython 3.9,
  so the code avoids 3.10+ syntax. It gives byte-identical outputs on all
  fixtures under CPython 3.13 (3.12 itself is not installed here).
