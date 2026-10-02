# SPEC_NOTES: VIRTUALIZE.md revision 3, TypeScript implementation

This implementation was written from VIRTUALIZE.md and HARNESS.md alone. It
accepts or rejects every fixture as expected, and it accepts all three
public inputs. The notes cover only places where two careful implementers
could disagree on output or on accepting/rejecting a file. For each one: the
question, what this implementation does, and how likely it is to matter on
real files. The most important item is first.

## 1. §4.3 Experiment: are skipped subtrees checked? (HIGH relevance)

§4.3 opens with: "Every member listed in this section is read and checked
(with the kind given, §4.2) wherever it is present in the places described,
whether or not its value ends up in the output." §1.2 adds: "every listed
check applies whether or not its value ends up in the output."

The flattening procedure then says: "For each node visited, in this order:
1. its `eType` is checked; 2. without `uLoopPars`, or with a count of 0
(...): the node and its children are skipped (not visited)".

Question: are the descendants of a skipped node checked? This covers
`eType` being an integer in {1,2,4,6,8}, `uLoopPars` being an object,
`pItemValid` flags, `ppNextLevelEx` members being objects, and the loop
members (`uiCount`, `dPeriod`, ...). The opening sentence ("wherever it is
present") and "SLxExperiment ... is a tree of objects (any other value
rejects)" suggest that the whole tree is checked. Step 2 ("not visited") and
step 1 being part of the visit suggest that only visited nodes are checked.

Decision: only visited nodes are checked. A node that step 2 skips still has
all its own members checked, because computing its count reads them. Its
children are never looked at. I read "the places described" as the visited
nodes.

Likelihood: moderate. Real ND2 files often contain disabled loops (count 0)
and loop types that do not appear elsewhere. One vendor-specific `eType`
(for example 7) under a count-0 node rejects the file under one reading
and not the other. The spec should say explicitly whether step 2 also
exempts descendants from the checks.

## 2. §4.3: which "listed members" are checked when the text makes reading conditional (LOW–MODERATE)

The blanket rule ("every member listed ... is read and checked wherever it
is present") conflicts with three places where the text reads a member only
under a condition:

- **Spectral `pPlanes/uiCount`:** "integer `uiCount`; if it is absent,
  integer `pPlanes/uiCount`". When `uiCount` is present, is
  `pPlanes/uiCount` still checked, and must `pPlanes` then be an object (a
  path step through a non-object rejects)? Decision: yes, whenever `pPlanes`
  is present. The spec makes an explicit exception for eType 8 ("`uiCount`
  and `dPeriod` are read only from valid members"), which implies that the
  blanket rule applies everywhere else.
- **eType 8 `dPeriod`:** "The period is number `p/dPeriod` ... of the first
  valid `p`", and "`dPeriod` [is] read only from valid members". Is
  `dPeriod` checked on every valid member or only on the first? Decision:
  every valid member.
- **`pItemValid` on non-position nodes:** it is listed as a member of every
  node ("`pItemValid` (list, default absent), a member of the node itself"),
  but it is only *used* as the validity list of `Points`. "Every member of a
  validity list is a flag": is it a validity list on a time or z node?
  Decision: it is checked (list, all members flags) on every visited node.
  Real files store these as byte arrays, which always pass, so the risk is
  low.

## 3. §3.2 "`Size*`" also covers `SizeX`/`SizeY`? (LOW)

"**Integer attributes** (`Size*`, `First*`, `IFD`, `PlaneCount`): the whole
value MUST be one or more digits ..." The "uses" list above it names only
`SizeZ`, `SizeC`, `SizeT`. Read literally, `Size*` includes `SizeX` and
`SizeY`, so `SizeX=" 64x"` would reject. Decision: only `SizeZ/C/T` are
checked, because only used attributes are validated. Real files almost
always have valid `SizeX/Y`, but the wildcard should be replaced by the
explicit list.

## 4. §3.2 UUID without `FileName` and self-closing (LOW)

"a `TiffData` with a `UUID` names the file identified by the `UUID`'s
`FileName` if it has one, else by its text", and "its **text**: if it is not
self-closing, ...". A self-closing `<UUID/>` without `FileName` has no text.
Does it name a file (with what identifier), or no file? Decision: it names
the file "" (empty identifier), so `<UUID/>` in one `TiffData` and
`<UUID>urn:x</UUID>` in another reject as two files. The other reading
(names no file) accepts. This is rare in real files.

Related: an empty `FileName=""` counts as "has one". It then identifies the
file "" instead of falling back to the text. The spec's "if it has one"
supports that reading, but saying so would help.

## 5. §3.2 UUID text: CDATA content is dropped (LOW, surprising)

"the characters of `X` from the end of the `UUID` tag to the start of the
next tag, with skipped sections removed". CDATA sections are skipped
sections, so `<UUID><![CDATA[urn:uuid:1]]></UUID>` has the empty text "". In
XML that content *is* the text. This implementation follows the spec. The
order of operations is "removed, decoded, trimmed", taken as listed. This
matters for `&#32;x`, which is decoded to " x" and then trimmed to "x". The
spec should state the order explicitly, if it is intended.

## 6. §3.2 Skipped-section end search: overlap with the start marker (VERY LOW)

"comments, `<!--` to the next `-->`". Is `<!-->` a complete comment? The
`-->` overlaps the opening `<!--`. Likewise `<?>` and "`<?` to the next
`?>`". Decision: the end marker is searched for after the whole start
marker (from `<!--`+4, `<![CDATA[`+9, `<?`+2, `<!`+2), so `<!-->` is not
closed by its own `>`. This could differ only on adversarial inputs.

## 7. §3.1 SubIFD details (LOW)

- **A SubIFD offset of 0:** "every IFD offset read (main chain or SubIFD)
  MUST be at least 8", so a `SubIFDs` value of 0 rejects. This
  implementation rejects. Some writers might use 0 as a placeholder, and an
  implementer could treat it as "no SubIFD" by analogy with the
  main-chain terminator. Worth stating explicitly.
- **SubIFD next-IFD field:** "a SubIFD's own next-IFD offset and SubIFDs are
  ignored". Must the 4/8-byte next-offset field after a SubIFD's entries
  still lie within the file? Decision: it is not read, so it is not checked.
  This matters only for a file truncated inside a SubIFD's last 4–8 bytes.
- **Empty main chain:** a header whose first IFD offset is 0 gives an empty
  chain. No sentence says that this rejects, but IFD 0 is required for
  everything. Decision: reject.

## 8. §3.6 Range check of unused tile offsets above 2^53 − 1 (VERY LOW)

Tiles with `TileByteCounts` 0 produce no entry, and their offsets are "not
otherwise checked". §1.2 rejects "an offset or length above 2^53 − 1".
Decision: every value of `TileOffsets`/`TileByteCounts` read from a LONG8
or IFD8 field is required to be at most 2^53 − 1, including those of
empty tiles. Another implementer could check only the offsets that are used.

## 9. §4.3 Overflows the spec does not address (VERY LOW)

- The eType 8 count is "the sum of integer `p/uiCount`". Each term is at most
  2^53 − 1, but the sum can exceed it. Decision: a sum above 2^53 − 1
  rejects (it becomes a shape).
- `N`, the product of loop counts, can exceed 2^53. Decision: `f < N` is
  compared exactly (BigInt), as "Integers are exact" implies.

## 10. §4.1 Signature data bounds (VERY LOW)

"with `n = 32` and `d = 64`. Its data starts with `Ver`, decimal digits ...,
and `.`". Must all 64 bytes of data be in the file, or only the bytes that
are parsed? Decision: all 64 bytes are read, so a file shorter than 112
bytes rejects. If the digits run to the end of the 64 bytes without a `.`,
the file is rejected.

## 11. Things that are correct but surprising (no ambiguity)

- §4.4: the chunk shape of a padded ND2 depends on the file's byte offsets.
  `h` is chosen from the varint sizes of the row offsets, so inserting a
  metadata chunk can change `chunk_shape`. Without present frames, `h`
  equals `uiHeight` (vacuously). This is deterministic, but it couples the
  array metadata to the byte layout.
- §4.2: a byte array is a list, so `ppNextLevelEx` stored as a byte array
  rejects ("each of which MUST be an object"), while `pItemValid` stored the
  same way is fine. The behavior is consistent, but it shows that kinds are
  enforced on unusual encodings.

## Implementation choices that were not spec questions

- Integers read from LV `i64`/`u64` are converted with `Number(bigint)`,
  which rounds to nearest-even. This implements "converted to binary64
  (rounding to nearest)".
- "Ends exactly at the end of the chunk's data" for the zlib stream is
  checked with Node's `inflateSync(..., {info: true}).engine.bytesWritten`
  (input consumed) equal to the input length. Node otherwise ignores
  trailing bytes silently, and an implementer who does not know this would
  accept `nd2_reject_zlib_trailing`. A note in the spec may help.
- UTF-16 decoding uses `TextDecoder("utf-16le")` (non-fatal), which replaces
  unpaired surrogates with U+FFFD as §4.2 requires.
