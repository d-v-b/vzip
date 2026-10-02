# Notes on VIRTUALIZE.md (revision 4) from the TypeScript implementation

This implementation was written from VIRTUALIZE.md and HARNESS.md alone. It
accepts or rejects all 95 fixtures as their names require, and it succeeds on
the three public inputs. Revision 4 is precise. The points below are the only
places where two careful implementers could plausibly produce different
output or a different accept/reject decision. All of them concern malformed or
unusual files, so none is likely to matter on real files from instruments.

## 1. §4.2 Nesting limit: a level record at depth 100

> "Levels MUST NOT be nested more than 100 deep: a chunk's top-level records
> are at depth 0, and the records of a level at depth `d` are at depth `d + 1`."

The text defines the depth of *records*, but the limit is on *levels*. One
reading allows records at depths 0–100 and rejects any record at depth 101.
The other counts levels and allows level records only at depths 0–99, which
makes the deepest level the 100th. The two fixtures (`nd2_edge_nesting_100`
has a scalar at depth 100, and `nd2_reject_nesting_101` has a scalar at depth
101) do not tell these apart. They differ on one input: an **empty** level
record (count 0) at depth 100. The first reading accepts it, because no record
reaches depth 101. The second rejects it.

**Decision:** a level record at depth 100 rejects, even when it is empty. I
read it as the 101st nested level. **Suggestion:** say "a record at depth
greater than 100 rejects the input" (or "a level record at depth 100 or
more"). **Likelihood:** negligible on real files, but trivial to diverge on
in a fuzzed corpus.

## 2. §4.4 Uncompressed frames: what reading a frame's "header" covers

> "the virtualizer MUST read the headers of the present frames with the
> lowest and the highest numbers … It reads no other frame's header"

and in §4.1:

> "Every chunk the virtualizer reads MUST start with the magic"

together with §1.2's "a read outside the file … rejects". It is unclear
whether "reading the header" of the first and last frame:

- (a) reads only the 16-byte header,
- (b) also reads the `n` name bytes, or
- (c) amounts to "reading the chunk", so that its `d` data bytes must lie in
  the file.

The emitted row ranges end at `start + (H−1)·W_bytes + R`. That can be less
than `o + 16 + n + d`: padding after the last row is not referenced, and `d`
may exceed `8 + H·W_bytes`. So a file truncated inside the last frame's
trailing padding is accepted under (a) and rejected under (c). (b) differs
from (a) only when the name runs past the end of the file, which the range
check usually catches anyway.

**Decision:** (a). Only the 16 header bytes must be in the file. Everything
else is covered by the requirement that every range lies within the file.
**Suggestion:** state which bytes are read. **Likelihood:** low (truncated
uncompressed ND2 files whose missing tail is only row padding).

## 3. §3.2 UUID text: CDATA content is removed

> "its **text**: … the characters of `X` from the end of the `UUID` tag to the
> start of the next tag, with skipped sections removed"

CDATA sections are skipped sections, so read literally,
`<UUID><![CDATA[urn:uuid:1]]></UUID>` has the empty text. An implementer
thinking in XML terms would keep the CDATA content as text. This changes the
multi-file decision. Two `TiffData` elements with UUIDs `<![CDATA[a]]>` and
`<![CDATA[b]]>` (no `FileName`) both have the identifier "" under the literal
reading, so the input is accepted. Under the XML reading they name two files,
so the input is rejected.

**Decision:** literal. The whole CDATA section, content included, is
removed. **Suggestion:** say explicitly "(including CDATA sections and their
content)". **Likelihood:** very low. Real OME-TIFF writers do not put CDATA in
UUID elements.

## 4. §4.3 Checks on members that are only conditionally meaningful

The section says "every member listed … is read and checked … wherever it is
present", and the table gives conditions ("`pPlanes` is read only then"). Two
cases are not stated as clearly:

- **`uLoopPars/pPeriodValid` on an eType 8 node without `pPeriod`.** The
  validity list exists only to qualify `pPeriod`'s members. It is unclear
  whether it is checked (it must be a list of flags) when there is no
  `pPeriod`. **Decision:** it is checked on every eType 8 node that has
  `uLoopPars`, whether or not `pPeriod` is present. It is not checked on
  other eTypes. This parallels the explicit rule for `pItemValid`.
- **The computed z step of a node that is never visited.** For eType 4, the
  step `abs(dZHigh − dZLow) / (count − 1)` can overflow to infinity (for
  example, `dZHigh = 1e308` and `dZLow = −1e308`), and §1.3 says an infinite
  computed number rejects. It is unclear whether this applies to nodes that
  flattening skips or drops, whose step never reaches the output.
  **Decision:** the step is computed, and an infinite result rejects, for
  every eType 4 node with `uLoopPars`, visited or not. This follows "every
  node of the tree … is checked … whether or not the flattening below visits
  it". **Suggestion:** state it. **Likelihood:** negligible.

## 5. §3.1 Duplicate tags: whether the ignored duplicates are still checked

> "Tags not in the table below are ignored: neither their field types nor
> their values are read or checked. Of duplicate tags in an IFD, the first is
> used and the others are ignored."

The parenthetical "neither … read or checked" is attached only to unknown
tags. A duplicate table tag (a second `Compression` with field type 99, or
with a value outside the file) could be "ignored" for its value but still
type-checked under "Checks on every IFD read … for every tag of the table
present in an IFD".

**Decision:** duplicates are not checked at all. **Suggestion:** repeat
"(not read or checked)" for duplicates. **Likelihood:** low. Duplicate tags
are rare, and they would have to be malformed as well.

## Rules that look surprising or may cause trouble (no ambiguity)

- **§4.3 `uiBpcInMemory` 32 always gives `float32`.** `ePixelType` is
  explicitly not read, so a 32-bit *integer* ND2 (rare, but Nikon can write
  them) would be presented as float32 and its pixels reinterpreted. This is
  clear, but it may be wrong.
- **§4.4 One name length for all uncompressed frames.** Only the first and
  last frame headers are read, and the first one's `n` is used for every
  frame. A file whose middle frames have a different name length gets offsets
  that are silently wrong, not a rejection. This is deliberate in the spec,
  and it matches how real files pad chunk names, but it is the one place where
  an accepted output can point at the wrong bytes.
- **§3.4 Candidate scan.** As the spec itself says, any later tiled IFD with
  IFD 0's format that is strictly smaller is taken as a level, even when it is
  not part of the pyramid.

No contradictions were found. Everything else (the tag grammar, the
reference decoding, TiffData stepping, the coverage defaults, flattening rule
3, validity lists, row-block height, payload sizes, colors, windows) was
unambiguous enough to implement directly. The fixtures matched the
implementation's readings everywhere they exercise these rules.
