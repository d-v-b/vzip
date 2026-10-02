# Notes on VIRTUALIZE.md (revision 3) from the Python implementation

Implementation: `impls/virtualize/python/virtualize.py` (Python 3.12, stdlib
only). It was written from VIRTUALIZE.md, HARNESS.md and SPEC.md only.

Results: every fixture in `web/test/fixtures/` gives the expected status (27
accepted, 59 rejected), and all three public inputs are accepted. About 42,000
truncated or byte-mutated variants of the fixtures were run in-process. None
crashed; each was either accepted or rejected.

Revision 3 is close to complete. The fixtures and public files never forced a
guess. The issues below are the places where a careful second implementer
could still differ. They are ordered by how likely they are to matter.

---

## A. Issues that can change accept/reject or output

### A1. §4.3: are nodes that are "skipped (not visited)" still checked? (moderate)

The text pulls two ways:

- The preamble says: "Every member listed in this section is read and checked
  (with the kind given, §4.2) **wherever it is present in the places
  described, whether or not its value ends up in the output**." The
  experiment is also "a tree of objects (any other value rejects)".
- The flattening steps say: "For each node **visited**, in this order: 1. its
  `eType` is checked; 2. without `uLoopPars`, or with a count of 0 …: the node
  and its children are skipped (**not visited**)".

Read the second way, a child of a count-0 node (or of a node without
`uLoopPars`) is never checked. Such a child could have an unknown `eType`
(for example 7), a non-object `uLoopPars`, a NaN `dPeriod`, or a non-flag
`pItemValid` member, and the file would still be accepted. Read the first
way, the file is rejected. A dropped loop (rule 4, "Otherwise the node's loop
is dropped") raises the same question for its own members.

**Decision:** I check the whole tree. That means every node's `eType`,
`uLoopPars`, `pItemValid` and `ppNextLevelEx`, plus the loop members of every
node that has `uLoopPars`, for its own `eType`, whether or not the node is
visited, skipped or dropped. I based this on the preamble and on §1.2
("every listed check applies whether or not its value ends up in the
output").

**Likelihood:** moderate on real files. Real experiments often contain empty
(count-0) loops, and an unknown `eType` inside one is plausible. No fixture
tells the two readings apart: `nd2_reject_etype_without_pars` puts the bad
`eType` on the root, which is visited in both readings. Suggested fix: say
"every node of the tree" explicitly, and add a fixture with a bad child under
a count-0 node.

### A2. §4.3 eType 8 and eType 6: which conditional members are checked? (low–moderate)

- eType 8: "The period is number `p/dPeriod` (default 0) of the first valid
  `p`" and "For eType 8, `uiCount` and `dPeriod` are read only from valid
  members." It is unclear whether `dPeriod` is read (and so type- and
  finiteness-checked) on every valid member or only on the first one.
  **Decision:** on every valid member, following the preamble. A NaN
  `dPeriod` in the second valid period therefore rejects the file.
- eType 6: "integer `uiCount`; if it is absent, integer `pPlanes/uiCount`".
  When `uiCount` is present, is `pPlanes/uiCount` still checked (and is a
  scalar `pPlanes` a "path through a non-object" rejection)?
  **Decision:** it is not read when `uiCount` is present, because the text is
  conditional.
- `pPeriodValid` on a node whose `eType` is not 8, and `pItemValid` on a node
  whose `eType` is not 2. **Decision:** `pItemValid` is checked on every node
  (it is listed as a member of every node, and "Every member of a validity
  list is a flag"). `pPeriodValid` is checked only for eType 8, the only
  place it is described.

**Likelihood:** low. Real validity lists are byte arrays, whose members are
always flags. NaN periods do occur, which is why `nd2_reject_nan_period`
exists. Suggested fix: a sentence of the form "for each eType, exactly these
members are read: …".

### A3. §3.2 tag scan: one pass or two, when `<!--`, `<?` or `<!` is inside a quoted attribute value (low)

"Skipped sections: scanning from the start, the first of these to begin at
each point is skipped" and "outside skipped sections, a tag is a match of
this grammar". This can be read as a first pass that finds the skipped
sections, followed by a tag pass. In that reading,
`<Image Name="a <!-- b">` starts a comment inside the attribute value, and
the comment runs to the next `-->` (or to the end of `X`), hiding `Pixels`
and `TiffData`. In the one-pass reading, the tag matches first and consumes
its quoted value.

**Decision:** one pass. At each `<`, test for a skipped-section opener; if
there is none, try the tag grammar; continue after whatever matched. The
`edge_xml_scan.tif` fixture has exactly this case (`Name="a <!-- b …"`), and
the one-pass reading gives the image name `a <!-- b &#0; &#xD800; 😀`.

**Likelihood:** low, because a raw `<` in an attribute value is not
well-formed XML. Because a fixture depends on it, the text should say
"scanning left to right, at each `<` …" so that only one reading is possible.

Related, minor: where does the search for the end of a section start? I
searched after the full opener, so `<!-->` does not close itself and starts a
comment that runs to the next `-->`. The same applies to `<?>`. XML agrees
for comments. The spec says only "to the next `-->`".

### A4. §3.2/§3.3: a self-closing `<UUID/>` without `FileName` (low)

"its **text**: if it is not self-closing, …", and a `TiffData` with a `UUID`
"names the file identified by the `UUID`'s `FileName` if it has one, else by
its text". A self-closing `UUID` without `FileName` has no defined text.

**Decision:** the identifier is the empty string. A `<UUID/>` therefore names
the file `""`, and a second `TiffData` with `<UUID>urn:…</UUID>` makes the
input multi-file (rejected). The other choice, "names no file", would accept
that input. **Likelihood:** low.

### A5. §3.2 "Integer attributes (`Size*`, …)": does `Size*` include `SizeX`/`SizeY`? (low)

The wildcard `Size*` literally covers `SizeX` and `SizeY`. The list of
attributes the virtualizer uses, however, is "`SizeZ`, `SizeC`, `SizeT` and
`DimensionOrder`", and the "at least 1" rule names only Z, C and T.
**Decision:** only `SizeZ`, `SizeC` and `SizeT` are validated. An OME-XML
with `SizeX="abc"` is accepted. Suggested fix: write
"`SizeZ`/`SizeC`/`SizeT`" instead of `Size*`.

### A6. §3.1/§1.2: values above 2^53 − 1 in tags that are read but not used (very low)

§1.2 says that "an offset or length above 2^53 − 1" rejects. §3.1 checks only
"field type, value within file, count" on every IFD read. It is unclear
whether a LONG8 `ImageWidth` or `TileOffsets` value above 2^53 − 1 rejects
when it sits in an IFD that is read but not used, or in a tile whose byte
count is 0.

**Decision:** it rejects only when the value is used: a scalar of a
plane/level/candidate/IFD 0, a SubIFD offset (always used), or a tile with
`n > 0`. **Likelihood:** very low (it needs a corrupt BigTIFF).

### A7. §1.3: infinite intermediates in numbers that do not reach the output (very low)

"A number that would be infinite or NaN rejects the input." It is unclear
whether this applies to the z step `abs(dZHigh − dZLow) / (count − 1)` of a
loop that is later dropped or skipped. **Decision:** yes. I compute the step
for every node that has `uLoopPars`, consistent with A1. **Likelihood:** very
low.

---

## B. Rules that are clear but may cause trouble on real files

### B1. §3.2: OME-XML that is not valid UTF-8 is silently treated as plain TIFF

"If `D` is valid UTF-8 and the tag scan … finds a start tag named `OME`".
Older OME-TIFF writers sometimes stored `µ` in Latin-1 (byte `0xB5`), with or
without `encoding="ISO-8859-1"`. Such a file is not rejected. Instead it
becomes a single-plane image of IFD 0, followed by an SVS-style pyramid scan
over the remaining IFDs. A multi-plane OME-TIFF can therefore come out as a
plausible but wrong image, for example with the later Z planes taken as
pyramid levels if they are tiled and smaller (usually they are not smaller,
so they are only dropped). All implementers will agree on this, but the
output silently loses planes. Consider rejecting when `D` contains `<OME` but
is not valid UTF-8, or decoding Latin-1 when the XML declaration says so.
Likelihood: low to moderate for older files.

### B2. §4.3: unknown `eType` rejects the whole file

"Any other `eType` is rejected." Combined with A1 (whole-tree checking), one
unsupported or custom loop anywhere in the experiment rejects the file, even
in a count-0 branch that contributes nothing. If the intent is
conservative, this is fine, but it should be a deliberate choice.

### B3. §4.4: frame pixel ranges not covered by the two header reads

For uncompressed files, only the lowest- and highest-numbered present frames'
headers are read. A middle frame whose map offset points at garbage is
accepted, and its ranges reference whatever bytes are there. The spec says
this explicitly ("Other frames' chunks are not checked"). It is consistent,
but it is the one place where a corrupt file produces a silently wrong
archive instead of a rejection.

---

## C. Things I needed that the spec does not say (no output difference expected)

- **Nesting depth.** Neither LV levels nor the experiment tree have a depth
  limit. A 20,000-deep LV nest is a valid input, and it overflows a naive
  recursive parser (Python's default limit is 1000). I raised the recursion
  limit and run on a large-stack thread. A stated limit, for example "depth
  above 1000 rejects", would keep implementations from crashing or failing
  differently.
- **Decompression size.** The compressed LV record (type 76) has no inflated
  size limit, so a zlib bomb is an accepted input.
- **Padded rows with no present frames.** "`h` is the largest divisor of
  `uiHeight` such that every block of every present frame …" is vacuously
  true for every divisor when no frame is present, so `h = uiHeight`. The
  chunk shape depends on this. It is implied, but it is easy to get wrong.
- **Payload maximum (implementation hint).** Per-row cost grows with the
  offset, so the largest block payload is the last block of the frame with
  the highest start offset. Only that block needs checking. Divisors can be
  enumerated in O(√H), and any `h` with `6h > 65519` can be skipped. Without
  this, a tall image makes the search quadratic. This could be an
  informative note.
- **Header IFD offset 0** (empty main chain): rejected, because "IFD 0" is
  required. This is implied but not stated.
- **§4.2 level offset table.** "then `8c` bytes to skip": those bytes must
  also lie within the data that contains the record. I reject when they do
  not, which is implied by "a read outside … rejects".

## D. Decisions where I believe the text is unambiguous (recorded for comparison)

- Duplicate TIFF tags: the later duplicates are not type-, count- or
  bounds-checked ("the others are ignored").
- UUID text: CDATA content is removed along with comments ("with skipped
  sections removed"), unlike XML. This is surprising but stated.
- `&#X…;` (uppercase X) is not a reference (the spec writes `&#x`).
- Color: "a color is an integer from −2^31 to 2^32 − 1", which I read as
  overriding the 0…2^53 − 1 range of "integer". A type-6 double with an
  integral value is accepted.
- Map names are compared as raw bytes, including the trailing `!`. Frame
  names must be `ImageDataSeq|<f>!` with no leading zeros. Other spellings
  are ignored.
- Planes `sPlaneNew/a<i>` with `i ≥ uiCount` are not checked.
