# Notes on VIRTUALIZE.md (profiles version 0, revision 2)

These notes come from a TypeScript implementation (`impls/virtualize/typescript/`)
written only from VIRTUALIZE.md, HARNESS.md and SPEC.md. Each item gives the
section, the question, what this implementation does, and why. The items are
ordered by how likely they are to make two careful implementations produce
different output (or accept/reject differently). Items marked **[divergence]**
are ones where the rival readings are both natural.

All fixtures behave as expected: every `unsupported_*`, `edge_reject_*` and
`nd2_reject_*` file is rejected (status 3), and every other file is accepted.
The three public inputs are accepted.

---

## A. Issues most likely to cause different outputs

### A1. §4.3: where `pItemValid` lives **[divergence]**

> The `i`-th member of `pPeriod` (`Points`) is **valid** if
> `uLoopPars/pPeriodValid` (the node's `pItemValid`) is absent, ...

The parenthesis is meant to be read in parallel: `pPeriodValid` sits under
`uLoopPars` and `pItemValid` sits on the **node**, as a sibling of `uLoopPars`.
My first reading took `(the node's pItemValid)` as `uLoopPars/pItemValid`. That
reading ignored validity completely and gave 4 positions instead of 3 for
`nd2_compressed_positions` and 3 instead of 1 for `nd2_edge_validity`. Only
the fixtures caught it. Suggested wording: "for `Points`, the node's own
member `pItemValid` (not under `uLoopPars`); for `pPeriod`, `uLoopPars/pPeriodValid`".
Write the full paths in the table.

### A2. §4.2: bytes of a byte array used as flags or numbers **[divergence]**

> A member used as a number MUST have type 2–6, and as a flag type 1–5

`pItemValid` and `pPeriodValid` are byte arrays (type 9) in the fixtures, and
"A byte array is a list of its bytes". A byte inside it has no record type, so
read literally, `pItemValid[i]` "used as a flag" fails the type 1–5 rule and the
input is rejected. I treat a byte of a byte array as an unsigned integer that
may be used as a number or a flag. The spec should say this.

### A3. §4.3: spectral count, "else" on missing vs. zero **[divergence]**

> 6 | (spectral) | `uLoopPars/uiCount`, else `uLoopPars/pPlanes/uiCount`, else 0

Does "else" apply when `uiCount` is **absent** or when it is **0**? With
`uiCount = 0` and `pPlanes/uiCount = 2`, the count is 0 (subtree skipped) or 2
(children visited). I use presence: a present `uiCount` (even 0) is the count.
`nd2_edge_spectral`'s second branch has `uiCount 0` and no `pPlanes`, so it does
not tell the two readings apart.

### A4. §3.3: "Every plane MUST be mapped to an existing IFD", checked when? **[divergence]**

A `TiffData` can map planes to IFD indices past the end of the main chain, and
a later `TiffData` can then remap those planes ("A plane mapped twice takes the
later mapping"). I check only the **final** mapping. So an out-of-range mapping
that a later one overrides is accepted. An implementation that checks each
mapping as it is made would reject. Please say which one applies.

### A5. §3.4: malformed candidates in the SVS level scan **[divergence]**

> One becomes the next level if it is tiled, has BitsPerSample, has IFD 0's
> format, and is strictly smaller ...; others are skipped.

To decide "has IFD 0's format" you must read the candidate's BitsPerSample,
SamplesPerPixel, SampleFormat, PlanarConfiguration, Compression and Predictor.
The spec does not say what happens when doing so fails. Examples:
BitsPerSample values that differ, PlanarConfiguration 3, a used tag with an
unknown field type, a value offset outside the file. It also does not say what
happens when a candidate whose format matches lacks ImageWidth or ImageLength.
"Required" and the BitsPerSample/SampleFormat equality rules apply only to
"IFDs that become planes or levels". A candidate is not one yet. Both
"reject" and "skip" are defensible. I **reject**: I compute the format strictly
and evaluate the conditions in the order written, short-circuiting.
Recommend: say that any IFD whose format is evaluated must be well formed, or
that an ill-formed candidate is skipped.

### A6. §3.1: which field types are acceptable for integer tags **[divergence]**

> **Values** are read as TIFF 6.0 and BigTIFF define them, for the field types
> they define. A used tag with an unknown field type rejects the input.

This does not say:
- whether a **known but non-integer** type is acceptable for an integer tag,
  for example ImageWidth as RATIONAL, FLOAT or ASCII. I reject ("a value of the
  wrong type").
- whether **signed** types (SBYTE, SSHORT, SLONG, SLONG8) are acceptable. I
  accept them, but reject a negative value.
- whether type 13 (IFD), which TIFF 6.0 does not define (it comes from a
  supplement), and the BigTIFF types 16–18 **in a classic TIFF** are known. I
  accept 13 and 16–18 in both variants.
- what a **count of 0** means for a scalar tag such as ImageWidth or
  Compression. The spec says it only for SubIFDs ("no values means no
  SubIFDs"). I reject. An implementation could instead treat the tag as
  absent and use the default.
- what a **count above 1** means for a scalar tag such as ImageWidth. I use the
  first value.

### A7. §3.1: when the "unknown field type" check happens **[divergence]**

"A used tag" can mean a tag listed in the table, in **any** IFD read, or a tag
whose value the algorithm actually reads. I check lazily: only when the value
is needed. A non-plane IFD in an OME-TIFF with Compression of field type 99 is
therefore accepted. An eager implementation would reject it. The same goes for
value offsets outside the file in tags that are never needed.

ImageDescription in IFD 0 is always "used". I check its field type (unknown
rejects), but read its value only when the type is ASCII.

### A8. §4.2: path steps through non-objects **[divergence]**

> Paths name members, for example `SLxImageAttributes/uiWidth` ... a missing
> member takes the default given

What if an intermediate step is not an object? Examples: `uLoopPars` is an i32
or a pointer; `sPlaneNew` is a **list** because all its records have empty
names; the path goes through a byte array. "Missing" (use the default) and
"wrong type" (reject) are both readings. I **reject** whenever a path step
needs an object and finds anything else. A related case: "without
`uLoopPars`" in §4.3 rule 2. I reject when `uLoopPars` is present but is not a
level, rather than treating it as absent.

### A9. §4.2 and §4.3: non-integer, negative or oversized numbers **[divergence]**

Type 6 (binary64) is allowed wherever a number is used, and types 2 and 4 are
signed. The spec never says what happens to a value that must be an integer,
or must be non-negative, but is not. My choices:
- `uiWidth`, `uiHeight`, `uiWidthBytes`, `uiComp`, `uiBpcInMemory`, every
  `uiCount`, and `uiCompCount` must be non-negative integers (safe integers),
  else reject. For example `uiCount = -1` rejects; it does not count as 0.
- A type 4/5 value above 2^53 − 1 rejects whenever it is used as a number.
- `uiBpcSignificant`: a value that is not an integer counts as "not between 1
  and uiBpcInMemory", so `uiBpcInMemory` is used.
- `uiColor`: a value that is not an integer rejects. A negative value (an
  `i32`) is taken modulo 2^32, so `0xAABBGGRR` stored as `-16776961` works.
- `eType`, `eCompression`, `uiTileWidth/Height`: compared as numbers, so
  `eType = 1.0` (type 6) is time.

### A10. §4.3 and §4.5: evaluation order decides rejection

Several rules read members conditionally. Whether a wrong-typed member rejects
the input then depends on whether it is read at all. The spec gives no order.
Mine:
- **Channels:** first check that every `a<i>` exists (stop at the first one
  missing). Then read every plane's `uiCompCount` and check that each is 1 or 3
  and that they sum to `uiComp`. Only then read `sDescription` and `uiColor`.
  So a bad `uiColor` rejects only when the planes are used for labels.
- **Calibration:** `dCalibration` is read only if `bCalibrated` is true.
  `dAspect` is read only if the image is calibrated.
- **z step and period:** `dZStep`, `dZHigh`, `dZLow` and `dPeriod` are read
  only for loops that end up in the final list. A dropped or replaced node's
  values are never checked. In contrast, every visited node's **count** is
  computed, so a bad count member in a node that is later dropped still
  rejects.
- **eType 8:** `uiCount` of each **valid** `pPeriod` member is required (no
  default is given). Invalid members are not read.

### A11. §4.2 and §4.5: type of string members

`sDescription` "(default empty)". The spec gives no type rule for strings. I
reject when `sDescription` is present but is not type 8.

### A12. §1.3 and §4.6: NaN and infinite values read from the file

> A number that would be infinite or NaN rejects the input.

This is clear for computed numbers. Values **read** as NaN or ±inf are less
clear. `abs(dZStep)` with `dZStep = NaN` is "computed". I reject, because I
apply `finite()` to every computed scale and step. `dCalibration = NaN` is
simply "not positive", so the image is uncalibrated and nothing rejects.
`dPeriod = +inf` is "positive", and `period / 1000 = inf` rejects. Say
explicitly whether read values pass through the NaN rule.

### A13. §4.1: chunk map details

- **The terminating record.** "ending with the record named
  `ND2 CHUNK MAP SIGNATURE 0000001!`". Does that record also carry its `u64`
  offset and `u64` size? Every record is described as "name, then a `u64`
  offset and a `u64` size", so I require the 16 bytes after the terminator's
  name. An implementation that stops at the name accepts a map truncated
  right after it.
- **Offsets above 2^53 − 1.** I reject such an offset for **every** record,
  even records whose chunks are never read. A lazy implementation rejects only
  for the chunks it uses. The unused `size` is not checked.
- **"after removing NUL padding".** Does this strip trailing NULs, or cut at
  the first NUL? I cut at the first NUL. They differ only for names with
  embedded NULs.
- **Names of records** are bytes up to and including the first `!`. I decode
  them as Latin-1 for comparison. A non-ASCII name cannot match a
  needed name either way.
- **No terminator.** A map that runs out of data before the terminating
  record is rejected.

### A14. §4.2: the compressed record's name

> type 76: the type byte and a name-length byte, 10 bytes to skip, then a zlib
> stream

The name-length byte is read but the name is not skipped: the stream always
starts at byte 12. I follow the text literally. If real files can have `k > 0`
here, this is wrong. If they cannot, say "the name-length byte (ignored)".

### A15. §3.2: OME detection uses the tag scan or a raw substring?

> If `D` is valid UTF-8 and its text contains a start tag whose name, without a
> namespace prefix, is `OME` (`<OME` or `<prefix:OME`, followed by whitespace,
> `/` or `>`)

Is `<OME ` inside a comment, CDATA section or attribute value a "start tag"?
I run the §3.2 tag scanner and look for a start tag whose local name is `OME`,
so text inside comments and CDATA does not count. A substring search
(`/<([^\s/>:]+:)?OME[\s/>]/`) would count it. The bullet list of skipping rules
comes after this sentence, so a reader may apply it only to the later uses.

### A16. §3.2: tag-scanner details the spec leaves open

"malformed XML is not detected; the scan uses the tags it finds" does not say
how to find tags in malformed input. My choices:
- **Element name:** the characters after `<` up to whitespace (space, tab,
  CR, LF), `/` or `>`. The local name is the part after the **last** `:`, so
  `<a:b:OME>` is `OME`. A `<` followed by whitespace, or by nothing, is not a
  tag.
- **Duplicate attributes:** the **first** occurrence wins (XML forbids
  duplicates, so the spec should pick one).
- **Unquoted values** (`a=b`) are read up to whitespace or `>`. An attribute
  without `=` has the value "". A missing closing quote extends the value to
  the end of the text.
- **A `>` inside a quoted attribute value** does not end the tag.
- **Numeric character references** to invalid code points (`&#0;`,
  surrogates, above U+10FFFF) are left as they are. `&#X41;` (capital X) is
  not decoded, because XML allows only `x`. Leading zeros are allowed.
- **Whitespace** in "decimal digits, optionally surrounded by whitespace" is
  XML whitespace (space, tab, CR, LF), not Unicode whitespace.
- **End tags** are matched by local name too (`</ome:Pixels>` ends
  `<ome:Pixels>`). The spec writes the end tag as `</Pixels>`.
- **A self-closing `<Pixels .../>`** has no `TiffData`. (The spec says "between
  that `Pixels` start tag and its end tag (`</Pixels>`, or the end of `X`)". A
  literal reader would scan to the end of `X`.)
- **A `TiffData` that is not self-closing and has no `</TiffData>`:** its
  `UUID` search stops at the next `TiffData` tag or at the end of `Pixels`.
- **Several `UUID`s in one `TiffData`:** the first is used.

### A17. §3.3: identity of UUIDs (multi-file check)

> by `FileName`, or by UUID text when there is no `FileName`

- **Is the UUID text entity-decoded or trimmed?** Entity decoding is specified
  only for attribute values. I decode entities in the text the same way and do
  not trim it. The text is everything between `<UUID ...>` and the next `<`, so
  a comment inside it ends it.
- **`FileName=""`** counts as present (the identity is the empty file name).
- **Namespaces.** I keep FileName identities and UUID-text identities apart:
  `FileName="x"` and a FileName-less UUID with text `x` are two files. Saying
  "two `TiffData`s name the same file if they have equal `FileName`s, or both
  lack `FileName` and have equal texts" would settle this.
- **A `TiffData` without a `UUID`** contributes no identity. Is it "this
  file"? If it were, a dataset with one UUID-less `TiffData` and one `TiffData`
  naming another file would be multi-file. I accept it.

---

## B. Underspecified, but probably harmless

- **§1.2: files shorter than 4 bytes** are rejected (they match no row of the
  table).
- **§1.2: "an offset or length above 2^53 − 1".** I apply this to every 64-bit
  value I read and use: BigTIFF offsets, counts and tile offsets, and ND2 chunk
  data lengths and map offsets.
- **§3.1: IFD offset 0.** A header whose first IFD offset is 0 is rejected
  (there is no IFD 0). A **SubIFD** offset of 0 is read literally as an IFD at
  offset 0 (it normally rejects as garbage). Real files have neither. Say "an
  offset of 0 in SubIFDs rejects" or "is ignored".
- **§3.1: the IFD limit.** "a limit of 100000": I allow exactly 100000 IFDs and
  reject the 100001st.
- **§3.1: a SubIFD's next-IFD offset is "ignored".** I still read its bytes,
  as part of the IFD's structure, so a SubIFD that ends exactly at the end of
  the file without them is rejected. An implementation that reads only the
  entries would accept it.
- **§3.1: the cycle rule** covers the main chain pointing into a SubIFD and the
  other way round ("anywhere"). I keep one set of offsets for all IFDs.
- **§3.3: work bounds.** `SizeZ × Cp × SizeT` can be astronomically large while
  the input is still small. I reject early when the plane count exceeds the
  number of (plane, IFD) mappings the `TiffData` elements could possibly
  provide (the sum of `min(PlaneCount, chainLength − IFD)`). That is equivalent
  to the spec's rule, but implementations without such a bound may run out of
  memory instead of rejecting. Consider stating a bound.
- **§3.4: SubIFD levels larger than level 0.** These are not forbidden, and the
  scale is then below `PX`. Fine, but an SVS-style "strictly smaller" check is
  not applied to SubIFD levels.
- **§3.4: SubIFDs of IFD 0 when IFD 0 is not a plane** (OME maps planes to
  later IFDs). `s` still comes from IFD 0. This follows the text but is
  surprising.
- **§3.5: Predictor with JPEG 2000** is "any", but Predictor is part of the
  format, so all levels and planes must still agree on it. A Predictor with an
  unknown field type rejects even for JPEG 2000.
- **§3.6 / §3.2: units.** The `PhysicalSize*Unit` value is compared exactly
  (no trimming). An empty unit gives no unit.
- **§4.1: signature data.** "starts with `Ver`, decimal digits ..., and `.`".
  I require at least one digit. A very long digit string is compared as a
  number and only has to be at least 3.
- **§4.1: names of metadata and frame chunks** are not checked against the map
  name ("names are not checked except as stated").
- **§4.2: the type 76 record** appearing anywhere other than as the whole of a
  chunk's data (for example as a second record) is "any other type" and
  rejects.
- **§4.2: level length `L`.** I also reject when `L` runs past the enclosing
  data, before parsing the children.
- **§4.2: empty chunk data** is an empty object, so a required member is then
  missing.
- **§4.3: `ImageMetadataLV!` present but without `SLxExperiment`.** "if the
  chunk is absent there are no loops" says nothing about a missing member. I
  reject, because the member has no default.
- **§4.3: `ImageMetadataSeqLV|0!` present but without `SLxPictureMetadata`.**
  I treat "optional" as covering the member too: no planes, uncalibrated.
- **§4.3: the experiment tree.** Each member of `ppNextLevelEx` must be a level
  (a node). Otherwise I reject. `ppNextLevelEx` of a skipped node (rule 2) is
  never examined.
- **§4.4: frame chunk names** must be canonical decimal: `ImageDataSeq|07!` is
  not frame 7 (I look names up by constructing them).
- **§4.4: uncompressed frames other than the lowest and highest** are never
  read, so their chunk magic is not checked. Only "References stay in the
  file" protects them. The lowest and highest frames' `d` is not checked
  against the file size either, only `d ≥ 8 + uiHeight × uiWidthBytes`.
- **§4.4: compressed frames.** "reads every present frame's header" includes
  the magic check (from §4.1). I fetch these headers in parallel. A 1-frame
  read per frame is unavoidable here.
- **§4.5: an empty `sDescription`** with three components gives labels `" R"`,
  `" G"`, `" B"` (leading space). This follows the text, but is it intended?
- **§4.6: `uiBpcSignificant` is required** even for float32, where it is not
  used.
- **§4.6: positions with no frames** still get an image group and an array.

---

## C. Rules that look wrong or may cause trouble

1. **§1.2 payload limit, uncompressed ND2 with row padding.** Each padded row
   is a separate range. With roughly 10 bytes per range, an image taller than
   about 6000 rows with padded rows exceeds 65519 bytes and the whole file is
   rejected (`nd2_reject_payload`). This is by design, but large padded ND2s
   (for example 3-component, odd-width RGB scans) will be unsupported.
2. **§3.2: OME-XML `Interleaved` is ignored.** The public IDR file declares
   `Interleaved="true"` but its TIFF has PlanarConfiguration 2. The spec's "the
   TIFF tags decide" gives the right answer. Keep it.
3. **§3.3: `SizeC ≠ spp` rejects when `spp > 1`.** OME-TIFFs that store RGB as
   separate planes per channel group (`SizeC = 6`, `spp = 3`) are rejected.
   This is documented, but common in brightfield datasets.
4. **§4.3: two loops of the same kind** are rejected, but a time loop of eType
   1 and one of eType 8 at the same depth are "different types" for rule 4.
   The second is then dropped (same depth, not appended), so rejection only
   happens at different depths. This is consistent, but it is not obvious.
5. **§4.6: the `float32` mapping for `uiBpcInMemory = 32`** assumes 32-bit ND2
   data is always float. Some ND2s carry 32-bit integer data
   (`ePixelType`). Those would be silently mislabeled.
6. **§5 references implementation paths.** Reference implementations
   (`src/vzip/virtualize/`, `web/src/...`) are named in a normative document.
   Independent implementers are told not to look at them, which is fine, but
   the spec should not depend on them.

---

## D. Things I needed that the spec did not say

- The encoded size of a reference payload, to apply the 65519-byte limit. I
  had to derive it from SPEC.md §5: a single range is a `Range` (source 0
  omitted, offset and length as varints with 1-byte tags, zero fields
  omitted). Several ranges are a `Concat` whose parts are each
  `0x0a, varint(len), Range`. A worked formula in VIRTUALIZE.md would help,
  because this is the only place where SPEC.md's encoding affects which inputs
  are accepted.
- How a reader of `IFD` and `PlaneCount` handles `PlaneCount` larger than the
  remaining positions. It is answered ("Planes past the last position are
  ignored"), but whether the IFD indices for those ignored planes must exist is
  not. I do not require them to.
- The meaning of "first" `Image` when it has no `Name` but a later `Image`
  does: no name (I take the first `Image` tag only).
- Whether the time-loop `period` for eType 8 can come from a member whose
  `uiCount` is 0. I take the first valid member regardless of its count.
