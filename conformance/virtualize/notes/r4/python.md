# Notes on VIRTUALIZE.md (revision 4) from the Python stdlib implementation

Implementation: `impls/virtualize/python/virtualize.py` (wrapper `virtualize`).
It was written from VIRTUALIZE.md and HARNESS.md alone. All 95 fixtures give the
expected status (the 65 `unsupported_*`/`edge_reject_*`/`nd2_reject_*` files are
rejected, the 30 others succeed), and the three public inputs succeed. About
22,000 in-process mutations of the fixtures (truncation, random bytes, 8-byte
splats) gave no crash: each mutated input was either rejected or virtualized.

Overall the document was precise enough to implement without guessing on any
of the fixtures. The points below are the places where two careful
implementers could still differ. Most of them only matter on malformed or
synthetic inputs.

## Points where the text allows more than one reading

### 1. §3.1: is a SubIFD's next-IFD field read, and is a SubIFD's own `SubIFDs` tag checked?

> "Only the listed offsets are read: a SubIFD's own next-IFD offset and SubIFDs are ignored."
> "**Checks on every IFD read.** For every tag of the table present in an IFD read ..."

There are two questions here:

- **The next-IFD field.** Do the 4 or 8 bytes after a SubIFD's entries have to lie
  inside the file, and does the value have to be ≤ 2^53 − 1? *Decision:* no. The
  field is not read, because "ignored" here means "not read". An implementation
  that reads each IFD as one block (count, entries, next offset) and then
  ignores the value would reject a SubIFD that ends exactly at EOF, or one
  whose BigTIFF next offset is above 2^53 − 1.
- **The SubIFD's own tag 330.** *Decision:* its field type, its value location
  and the 2^53 − 1 limit are still checked, because it is a tag of the table
  in an IFD read. The offsets it lists are not followed. Someone could read
  "SubIFDs are ignored" as "the tag is ignored", and then skip these checks.

Likelihood on real files: very low. Suggestion: say "the next-IFD offset field
of a SubIFD is not read", and "a SubIFD's `SubIFDs` tag is checked like any
other tag, but not followed".

### 2. §3.2: skipped sections and tags in one scan, or in two passes?

> "1. Skipped sections: scanning from the start, the first of these to begin at each point is skipped ... 2. Tags: outside skipped sections, a tag is a match ... Scanning continues after the end of each tag."

*Decision:* one left-to-right scan. At each `<`, a skipped-section start is
tried first, then the tag grammar. A tag that matches consumes its attribute
values, so `<Image Name="a <!-- b">` is a tag and does not start a comment.

The other reading is a first pass that finds skipped sections, then a tag
match only outside them. Under that reading the `<!--` inside the attribute
starts a comment that runs to the end of `X`. `edge_xml_scan.tif` tells the
two readings apart: with one scan it has an image name and SizeZ 2, with two
passes it has neither. The sentence "Scanning continues after the end of each
tag" points to one scan, but step 1 on its own reads like a separate pass.

Likelihood: low on real files, but the fixture shows the difference is real.
Suggestion: say explicitly that steps 1 and 2 are one scan, tried in that
order at each `<`.

Two smaller points in the same step:

- "the first of these to begin at each point". *Decision:* priority follows
  list order at the same position, so `<!--` wins over `<!`. "First to begin"
  could also be read as "earliest position", but that gives the same result.
- Under the one-scan reading, a tag cannot span a skipped section, and a
  `<!--` inside a quoted attribute value has no effect.

### 3. §4.2: what "nested more than 100 deep" counts

> "Levels MUST NOT be nested more than 100 deep: a chunk's top-level records are at depth 0, and the records of a level at depth `d` are at depth `d + 1`."

*Decision:* a record at depth > 100 rejects. That means a level record at
depth 100 with `c = 0` is accepted, and one with `c ≥ 1` is rejected. The
fixtures agree: `nd2_edge_nesting_100` has records down to depth 100 and is
accepted, and `nd2_reject_nesting_101` has a record at depth 101.

The rule as written gives depths to records, but limits "levels". Another
implementer could reject a *level record* at depth 100 even when it is empty,
or could count levels starting from 1. Likelihood: very low (only
synthetic files). Suggestion: "a record at depth greater than 100 rejects the
input".

### 4. §4.3: when is the z step's arithmetic checked for overflow?

> "step `abs(dZStep)`, or if that is 0 and the count is more than 1, `abs(dZHigh − dZLow) / (count − 1)`" together with §1.3 "A number that would be infinite or NaN rejects the input."

`dZHigh − dZLow` can overflow to infinity, for example with ±1e308.
*Decision:* the step is computed, and so can reject, for every eType 4 node
that has `uLoopPars`. That includes nodes the flattening skips, because §4.3
says every node is checked. An implementation that computes the step only for
loops that end up in the list would accept such a file.

The same question applies to `dCalibration × dAspect`. *Decision:* it is
computed only when the image is calibrated, because only then does the text
compute it. Likelihood: negligible. Suggestion: say whether the step is part
of "checked on every node".

### 5. §4.3: is `pPeriodValid` checked when `pPeriod` is absent?

> "the list `uLoopPars/pPeriodValid` ... Every member of a validity list is a flag."

*Decision:* on an eType 8 node with `uLoopPars`, `pPeriodValid` is read and
checked when it is present, whether or not `pPeriod` exists. It is not read on
other eTypes. ("Every member listed in this section is read and checked
wherever it is present in the places described".) Another implementer might
read it only while iterating `pPeriod`. Likelihood: very low.

### 6. §4.4: does a frame chunk that runs past EOF reject the file?

> "reject the file if ... either's data length `d` is less than `8 + uiHeight × uiWidthBytes`" ... "(Other frames' chunks are not checked, except that their ranges MUST lie within the file, §1.2.)"

*Decision:* a frame's header may claim a data length `d` that extends past the
end of the file. The input is still accepted as long as every emitted range
(the pixel rows) lies within the file. Nothing in the text says the whole
chunk `o + 16 + n + d` must lie within the file for the lowest and highest
frames. An implementer who checks "chunk fits in file" when reading a header
would reject, for example, a last frame with trailing padding cut off. (For
compressed frames the range runs to `o + 16 + n + d`, so the two readings
agree there.)

Likelihood: low. A truncated ND2 file usually also loses its chunk map, which
is at the end of the file.

## Rules that are surprising (but implementable)

- **§4.4 row-block height depends on byte offsets.** `h` is chosen so that the
  *encoded payload* fits in 65519 bytes, and the payload size depends on the
  varint length of the file offsets. So the same image layout placed at a
  different offset in the file can get a different chunk shape. When no frame
  is present, the condition is vacuous, so `h = uiHeight`. *Decision:* follow
  the rule literally. Because the per-row payload never decreases as the
  offset grows, only the last block of the frame with the largest start needs
  to be checked. Since every row costs at least 6 payload bytes, block heights above 10919 never need to be tried.
- **§3.2 UUID text drops CDATA content** ("with skipped sections removed"),
  which real XML would keep as text. Followed literally.
- **§3.2 / §1.3 infinite `PhysicalSize`.** `1e400` makes `PhysicalSize` *absent*
  (§3.2), whereas the general rule in §1.3 would *reject*. The specific rule
  wins. It is clear enough, but worth a cross-reference.
- **§3.1 LONG8/IFD8 (16, 18) in a classic TIFF are accepted** ("in either TIFF
  variant"). This is deliberate according to the text, but other TIFF readers
  reject these types in classic TIFF.

## Things checked and found unambiguous

The following are clear enough that two implementers should not differ:

- TiffData stepping, PlaneCount defaults, later mappings overriding, and the
  multi-file rule (including a self-closing UUID naming the file `""`).
- The SVS candidate scan.
- The SubIFD pyramid.
- Chunk numbering for planar and interleaved data.
- ND2 chunk map parsing.
- The LV list/object rule and duplicate names.
- The validity lists.
- Flattening rules 1–3 (`nd2_edge_kind_merge`, `nd2_edge_spectral` and
  `nd2_edge_validity` came out as worked out by hand).
- Channel labels and colors.
- The omero window.
- The uncompressed and compressed frame layouts.

No rule looks wrong.
