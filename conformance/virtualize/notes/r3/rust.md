# Notes on VIRTUALIZE.md (profiles version 0, revision 3)

These notes come from a Rust implementation written from VIRTUALIZE.md and
HARNESS.md alone. They cover only places where two careful implementers could
produce different output or a different accept/reject decision. Each item
gives the section, the question, what this implementation does and why, and
how likely the difference is to show up on real files.

Results: all 86 fixtures behave as expected (every `unsupported_*`,
`edge_reject_*` and `nd2_reject_*` file exits 3, every other file exits 0).
The three public inputs succeed. About 3,900 corrupted variants of the
fixtures (truncated, bit flips, random bytes in headers and trailers) gave
exit 0 or 3 and never crashed or hung.

## A. Conflicts and real ambiguities (could change accept/reject)

### A1. §4.3: are the subtrees of skipped experiment nodes checked?

The opening sentence of §4.3 says: "Every member listed in this section is
read and checked ... wherever it is present in the places described, whether
or not its value ends up in the output." §1.2 says the same in general:
"every listed check applies whether or not its value ends up in the output".
Rule 2 of the flattening, however, says: "without `uLoopPars`, or with a
count of 0 ...: the node and its children are skipped (**not visited**)", and
rule 1 says "its `eType` is checked" as a step of visiting.

Take a node with count 0 whose child has `eType` 7, has no `eType`, or has a
`uLoopPars` that is a scalar. Under the visiting reading the input is
accepted, because the child is never visited. Under the check-everything
reading it is rejected.

- **Decision:** every node of the tree is checked, visited or not: `eType`
  (required, in {1, 2, 4, 6, 8}), `uLoopPars` (an object), `pItemValid`
  (a list of flags), `ppNextLevelEx` (an object or a list of objects), and,
  when `uLoopPars` is present, all the loop parameters for that `eType`.
  Visiting decides only which loops are appended. The global rule and §1.2
  are explicit, and "not visited" reads as "not appended".
- **Suggestion:** say that "skipped" affects only the loop list, and that
  checks apply to the whole tree (or say the opposite).
- **Likelihood:** low on real files, where skipped subtrees seem to be well
  formed. A synthetic fixture would separate the two readings at once; none
  of the current fixtures does.

### A2. §4.3, eType 8: which `dPeriod` values are read and checked?

"The period is number `p/dPeriod` (default 0) of the first valid `p`" and
"For eType 8, `uiCount` and `dPeriod` are read only from valid members."
`uiCount` is clearly summed over every valid `p`. For `dPeriod`, one reading
takes it only from the first valid `p`; the other reads and checks it in
every valid `p` (because of the "every member listed is read and checked"
rule).

- **Decision:** `dPeriod` is read and checked as a number in every valid
  `p`, and the first one is used. So a NaN or non-numeric `dPeriod` in a
  second valid period rejects the input.
- **Likelihood:** low to medium. NaN periods are plausible in real files
  (there is a `nd2_reject_nan_period` fixture), and NaN in a later
  `pPeriod` entry is what would split implementations.

### A3. §4.3, eType 6: is `pPlanes/uiCount` read when `uiCount` is present?

"integer `uiCount`; if it is absent, integer `pPlanes/uiCount`; if that is
absent, 0". This reads as conditional, but the global "every member listed
... is read and checked wherever it is present" suggests that
`pPlanes/uiCount` (and `pPlanes` being an object) should be checked even
when `uiCount` is present.

- **Decision:** `pPlanes` and `pPlanes/uiCount` are read only when
  `uiCount` is absent, following the "if it is absent" wording.
- **Likelihood:** low.

### A4. §4.3: is `pItemValid` checked on nodes that are not position loops?

`pItemValid` is listed as a member of every node ("`pItemValid` (list,
default absent), a member of the node itself"). "Every member of a validity
list is a flag" is in the Validity paragraph, which uses `pItemValid` only
for `Points`. So is a time or z node's `pItemValid` checked to be a list,
and are its members checked to be flags?

- **Decision:** on every node, `pItemValid` must be a list and all its
  members must be flags. It is used only for eType 2.
- **Likelihood:** low. Real files carry `pItemValid` on several node kinds,
  but as byte arrays, which pass either reading.

### A5. §3.2/§3.3: a self-closing `UUID` without `FileName`

"a `TiffData` with a `UUID` names the file identified by the `UUID`'s
`FileName` if it has one, else by its text". The text is defined only "if it
is not self-closing". For `<UUID/>` there is no file name and no text. Does
that `TiffData` name the empty-string file, or no file at all?

- **Decision:** it names the file `""`. So `<UUID/>` in one `TiffData` and
  `<UUID FileName="x.tif"/>` in another make two files, and the input is
  rejected. An empty non-self-closing `<UUID></UUID>` also names `""`.
- **Likelihood:** very low.

### A6. §3.2: where the search for a skipped section's end starts

"comments, `<!--` to the next `-->`" and "processing instructions, `<?` to
the next `?>`". Does `<!-->` close itself (the `-->` overlaps the opener)?
Does `<?>` close itself?

- **Decision:** the search for the end starts after the whole opener, so
  `<!-->` and `<?>` do not close and run on to the next real terminator.
- **Likelihood:** very low. Say "the next `-->` after the `<!--`".

### A7. §3.2: UUID text, removing skipped sections and decoding references

"with skipped sections removed, references decoded as in attribute values,
and leading and trailing whitespace removed". The order matters for input
like `&am<!---->p;`.

- **Decision:** skipped sections are removed first, then references are
  decoded, then whitespace is trimmed, in the order written.
- **Likelihood:** negligible.

## B. Underspecified points where the obvious choice was taken

### B1. §3.1: a TIFF whose header gives first-IFD offset 0

The chain is then empty and there is no IFD 0. The document never says this
rejects, but §3.1 needs IFD 0 "always". **Decision:** reject. Likelihood:
negligible.

### B2. §3.6: TileOffsets/TileByteCounts count

"Both ... MUST have `T` times the number of sample planes values." This is
implemented as exact equality, so extra values reject. Some readers accept
"at least". Likelihood: low (writers produce exact counts).

### B3. §4.4: row-block height when no frame is present

`h` is "the largest divisor of `uiHeight` such that every block of every
present frame" fits. With no present frames, the condition holds vacuously
and `h = uiHeight`. This changes `chunk_shape` in `zarr.json` even though
there are no chunks. **Decision:** `h = uiHeight`. Likelihood: negligible.

### B4. §4.3, eType 8: a time count above 2^53 − 1

The sum of valid `uiCount`s can exceed 2^53 − 1 although each term is an
integer. **Decision:** reject (a shape that cannot be written as an exact
JSON integer). Likelihood: negligible.

### B5. §4.2: depth of LV nesting

No limit is stated. A chunk can nest levels many thousands deep (14 bytes
per level), which overflows a native stack in recursive parsers. This
implementation runs on a 1 GiB stack. The specification should either set a
depth limit (rejecting deeper input) or say that implementations must handle
any depth. Otherwise a crafted file gives a crash in one implementation and
output in another. Likelihood on real files: none. It matters for fuzzing
and conformance.

### B6. §4.3: picture planes and huge `uiCount`

Plane `i` exists for `i < uiCount` with `uiCount` up to 2^53 − 1. An
implementation that loops `for i in 0..uiCount` hangs. This one iterates over
the `a<i>` members instead. In §4.5 a large `uiCount` with missing planes
falls back to `C<k>`. Not ambiguous, but a note would help implementers.

## C. Rules that may cause trouble

- **§4.4, payload-driven row blocks.** `h` depends on the varint lengths of
  the frame offsets. Two files with identical dimensions can therefore get
  different chunk shapes depending on where in the file the frames lie, and
  so can the same file rewritten with different padding. This is
  deterministic but surprising. It is also expensive to evaluate naively
  (frames × rows × divisors), and an implementer might be tempted to bound
  it per frame instead of over "every block of every present frame".
- **§4.4, only the lowest and highest uncompressed frames are checked.**
  This is clear, but a corrupt middle frame yields references to garbage
  instead of a rejection. That is by design, but worth a sentence of
  rationale in the document.
- **§3.3, `SizeC` with `spp > 1`.** Any `SizeC` other than 1 or `spp`
  rejects. Multi-channel RGB OME-TIFFs (for example, `SizeC=6`, `spp=3`) are
  rejected rather than mapped. This is stated, so it is not ambiguous, but
  it will reject some real files.

## D. Checked and found clear (no action needed)

- The tag-scan grammar (§3.2) is deterministic with greedy matching, because
  an `attr` can never start where the closing `[ws] ["/"] ">"` could.
- A level's `8c` skip bytes come after `L`, and the skip must lie within the
  data (implemented as a read, so a truncated skip rejects).
- §1.2 payload sizes: a one-row block is a single `Range` message, not a
  `Concat` of one. The text says this, and it affects the 65519 check only
  for `h = 1`, which always passes.
