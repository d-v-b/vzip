# Notes on VIRTUALIZE.md (profiles version 0, revision 1)

These notes come from an independent Rust implementation (`impls/virtualize/rust/`).
It was written from VIRTUALIZE.md, HARNESS.md and SPEC.md only. Each item gives
the section, the question, the decision made and the reason. Items marked
**[risk]** are rules that the author thinks are wrong or are likely to cause
implementations to disagree.

## General

1. **Profile selection is not specified (§1, §3, §4).** The document never says
   how a virtualizer decides whether an input is a TIFF or an ND2. *Decision:*
   choose by magic bytes. `II*\0`, `MM\0*`, `II+\0` or `MM\0+` selects TIFF.
   ND2 chunk magic `DA CE BE 0A` or a JPEG 2000 signature box (`00 00 00 0C`…)
   selects ND2, so that the legacy-file rejection in §4.1 applies. Anything else
   is rejected. The URL extension is ignored.

2. **What counts as a "rejection" (§1.2).** "MUST reject an input the profile
   does not accept". Malformed input is not covered: truncated files,
   pointers past EOF, LV data that runs off the end, invalid UTF-8 and so on.
   *Decision:* every structural error in the input is a rejection (exit 3).
   Only network and I/O failures exit 1. The spec should say that a malformed
   input is "not accepted".

3. **"A virtualizer reads only the file's structure, never its pixel data"
   (§1.2)** conflicts with HARNESS.md recommending block caching. On a small
   file one 64 KiB block contains the pixels. *Decision:* block reads that
   happen to cover pixel bytes are allowed. Nothing is decoded from them. Suggest
   rewording to "does not depend on pixel data".

4. **Units table (§2.3).** `Å` is listed. It is not stated whether this is
   U+00C5 or U+212B (ANGSTROM SIGN), and OME-XML schemas allow both. *Decision:*
   only U+00C5, as typed in the spec. Suggest listing both explicitly.

5. **The `bytes` codec for 1-byte types vs §4.6.** §2.1: `{"name": "bytes"}` for
   1-byte data types. §4.6: "bytes (little-endian)". For `uint8` ND2 files these
   conflict. *Decision:* §2.1 wins (no `configuration`), on the reading that §4.6
   only names the byte order for larger types. **[risk]** The literal reading of
   §4.6 gives a different JSON value, so §4.6 should say "bytes (§2.1, little
   endian when larger than 1 byte)".

## TIFF profile

6. **§3.1 SubIFDs: depth and chains.** "for every IFD in the chain the IFDs
   listed in its `SubIFDs` tag". This does not say whether (a) a SubIFD's own
   next-IFD pointer is followed, or (b) SubIFDs of SubIFDs are read.
   *Decision:* neither. Only the listed IFDs are read.

7. **§3.1 "An IFD cycle, or more than 100000 IFDs, is rejected".** Two questions.
   Does the limit count SubIFDs? Is a SubIFD offset that equals a main-chain IFD,
   or two IFDs sharing a SubIFD, a "cycle"? *Decision:* the limit counts main-chain
   IFDs and SubIFDs together. A cycle is a revisit within the main chain only,
   because SubIFD chains are not followed (item 6). Shared or aliased SubIFDs are
   accepted.

8. **§3.1 tag reading details not covered by "as TIFF 6.0 and BigTIFF define
   them".** These cases are not covered:
   - duplicate tags in one IFD (*decision:* first occurrence wins);
   - integer tags stored with a float or rational type (*decision:* reject);
   - signed types with negative values (*decision:* reject);
   - an empty value array (count 0) for a required tag (*decision:* reject).

9. **§3.1 per-sample tags.** "All BitsPerSample values of an image MUST be
   equal". Nothing is said about SampleFormat, which is also per-sample, or
   about which value the "format" tuple uses. *Decision:* use the first
   SampleFormat value and do not check the others. **[risk]** Another
   implementation could reject differing values, or compare whole arrays.

10. **§3.1 PlanarConfiguration values other than 1 and 2.** These are not
    mentioned. *Decision:* reject when spp > 1.

11. **§3.1 "required" tags of unused IFDs.** The table says ImageWidth etc. are
    "required". Is an IFD that the output never uses (an SVS label stored as
    strips, or one with missing tags) a reason to reject? *Decision:* no. Only
    IFDs that are levels/planes and the first IFD are validated. During the
    no-OME pyramid scan (§3.4), an IFD whose tags cannot be read is skipped
    rather than rejected.

12. **§3.2 detecting OME-XML.** Three questions about "If the first IFD's
    ImageDescription contains `<OME`, it is the OME-XML `X` (the tag's bytes up
    to the first NUL, as UTF-8)":
    - Is containment tested before or after NUL truncation? *Decision:* after,
      on the truncated bytes.
    - **[risk]** §3.2 also says "Element names may carry a namespace prefix".
      A document whose root is `<ome:OME …>` does **not** contain `<OME`, so it
      is treated as having no OME-XML. That is a different output, not a
      rejection. Conversely, any description that merely contains `<OMEGA` is
      treated as OME-XML. The test should probably be on the root element's
      local name.
    - The bytes may not be valid UTF-8. *Decision:* reject. (Lossy decoding would
      change the bytes of `OME/METADATA.ome.xml`.)

13. **§3.2 "the first `Image` element", "the first `Pixels` element".** These are
    independent "first" lookups. In a document where the first `Pixels` is not
    inside the first `Image`, the result is odd. *Decision:* implemented
    literally, as two independent document-order searches. Multi-image
    OME-TIFFs (several `Image` elements) are silently virtualized as the first
    image only. The spec might want to say so explicitly, or reject them.

14. **§3.2 "the `TiffData` elements inside it".** It is not stated whether these
    are children or descendants. *Decision:* descendants of the first `Pixels`.
    The `UUID` child is likewise any descendant of the `TiffData`, using the
    first `UUID` that has a `FileName`.

15. **§3.2 XML parsing scope.** Attribute names: are prefixed attributes (e.g.
    `ome:Name`) matched? *Decision:* no, only the exact unprefixed name. Comments,
    CDATA, processing instructions and DOCTYPE are skipped. Unknown entity
    references (`&foo;`) are kept verbatim. Invalid numeric references are kept
    verbatim. Whitespace around integer attribute values is trimmed.

16. **§3.3 invalid sizes.** `SizeZ`/`SizeC`/`SizeT` may be 0 or non-numeric.
    *Decision:* reject either.

17. **§3.3 `PlaneCount` defaulting to "all planes".** "`PlaneCount` defaults to
    all planes when it is the only `TiffData` and has no `IFD`". When that
    `TiffData` also has `FirstZ`/`FirstC`/`FirstT`, "all planes" from a non-zero
    start runs past the end. More generally, nothing says what happens when
    `start + PlaneCount` exceeds the plane count. *Decision:* positions past the
    last plane are ignored (no rejection).

18. **§3.3 out-of-range start position.** `FirstZ ≥ SizeZ`, or `FirstC ≥ Cp`
    (note: with spp > 1, `Cp = 1`, so `FirstC` must be 0). *Decision:* reject.

19. **§3.3 overlapping `TiffData`.** Two `TiffData` may map the same plane. Is
    this an error, or does the first or the last win? *Decision:* the later one
    (document order) wins, silently.

20. **§3.3 `DimensionOrder` validation.** The value may not be `XY` followed by a
    permutation of `ZCT`. *Decision:* reject. Also, "the letters after `XY` are
    fastest first" is clear. "Successive planes step through positions in
    `DimensionOrder`" assumes the position sizes are `(SizeZ, Cp, SizeT)`. With
    spp > 1 the C dimension has size 1 in this stepping. That is what was
    implemented, but the spec could say it explicitly.

21. **§3.4 number of SubIFD levels.** "level `k ≥ 1` is, for every plane, its
    IFD's `k`-th SubIFD". This does not say how many levels there are, or what
    happens when planes have different numbers of SubIFDs. *Decision:* the level
    count is 1 + the number of SubIFDs of the **first IFD** (the one whose
    SubIFDs trigger this rule). A plane whose IFD has fewer SubIFDs is rejected.
    Extra SubIFDs on other planes are ignored. Also, "k-th" is read as 1-based
    (level 1 = the first SubIFD).

22. **§3.4 "the first IFD's format" when the first IFD is not a plane.** If
    `TiffData` maps plane 0 to IFD 3, the reference format is still IFD 0's, and
    IFD 0 must still have the required tags. Implemented literally.

23. **§3.4 SVS-style scan.** "every later main-chain IFD that is tiled, has the
    first IFD's format, and is smaller in both width and length than the
    previous level". This was read as strictly smaller than the most recently
    accepted level, scanning IFDs 1, 2, … in order. Fine as written. The case of
    OME-XML without SubIFDs (one level only, even if later IFDs look like a
    pyramid) is clear.

24. **§3.6 PhysicalSize parsing.** A `PhysicalSize` that is present but
    unparseable, or `inf`/`NaN`, is not covered. Zero and negative values are
    not covered either. *Decision:* reject a non-numeric or non-finite value.
    Accept zero and negative values literally (they give a zero or negative
    scale). Also, "Decimal strings are converted to binary64 by correct rounding"
    (§1.3) does not define which decimal syntaxes are accepted. *Decision:*
    `[+-]digits[.digits][e[+-]digits]`, as Rust parses it.

25. **§3.6 empty image name.** `Name=""`. *Decision:* `"name": ""` is emitted
    (the attribute is present).

26. **§3.6 tile count.** "The IFD's tile count MUST be `T` times the number of
    sample planes". It is not stated whether this is the count of TileOffsets,
    TileByteCounts or both. *Decision:* both must equal it.

27. **§3.6 references are not bounds-checked.** The spec does not say whether
    a tile whose `offset + count` exceeds the file size is rejected.
    *Decision:* not checked (it would only show up when reading).

28. **§3.6 `c` coordinate when interleaved** ("0 when interleaved") and the
    chunk shape `spp` are clear. Note, though, that with spp > 1 and OME
    `SizeC` = k·spp (several RGB channels) the input is rejected by §3.3. That
    may be intended, but it rejects valid OME-TIFFs.

## ND2 profile

29. **§4.1 signature chunk.** "(`n = 32`, `d = 64`)". Is this a requirement
    (reject otherwise) or a description? *Decision:* required. Also, "its data
    starts with `VerM.m`". *Decision:* `Ver`, one or more digits, `.`, at least
    one digit. Major parsed as decimal.

30. **§4.1 chunk map details.**
    - It is not stated whether the map chunk's header magic and name are
      checked. *Decision:* both are checked; reject on mismatch.
    - Duplicate names in the map. *Decision:* the last record wins.
    - The record's `u64` size is unused by the profile. Say so, so that nobody
      validates it against the chunk header.
    - It is not stated whether the chunk header found at a mapped offset must
      have the magic, or a name equal to the map's name. *Decision:* the magic is
      checked for every chunk header read. The name is not compared.
    - A map that runs out before the terminating record. *Decision:* reject.

31. **§4.2 type 76 placement.** "the rest of the data is a zlib stream (RFC 1950)
    of an LV structure, which replaces this one". "The rest of the data" and
    "this one" are unclear when a type-76 record appears inside a level, or after
    other records. *Decision:* type 76 is only valid in a top-level structure
    (including inside a decompressed one). It replaces the whole structure,
    discarding any records parsed before it. Inside a level it is rejected. In
    the fixtures it only occurs at offset 0 of a chunk.

32. **§4.2 level length `L`.** Both a count `c` and a length `L` are given, so
    they can disagree. *Decision:* records are parsed sequentially. The input
    is rejected if they do not end exactly at `start + L`. Then `8c` bytes are
    skipped. This never triggered on the fixtures or the public files, so the
    two are consistent in practice. The spec should still say which one is
    authoritative.

33. **§4.2 record type 10 and unknown types.** The table skips 10 (and 0,
    12–75, …). *Decision:* any unlisted type is rejected. Suggest stating this.

34. **§4.2 repeated names.** "a repeated name keeps its last value". It is not
    stated whether the member then sits at its first or its last position. This
    matters because "the members of" a level are "its values in order" and are
    used for `ppNextLevelEx`, `pPeriod` and `Points`. *Decision:* the first
    position (dictionary-update semantics). **[risk]**

35. **§4.2 empty levels.** A level with 0 records: "A level whose records all
    have empty names is a list" is vacuously true. *Decision:* it is treated as
    an (empty) object. This is not observable in the output, but it matters for
    any type check. Real files contain many such levels (`pCoeff`,
    `nameAngIn`, and a spectral node's `uLoopPars` in the 4.6 GB public file).

36. **§4.2 name decoding.** `k` includes the terminating NUL. *Decision:* the name
    is the code units before the first NUL. Unpaired surrogates are decoded
    lossily. A name with `k = 0` is empty.

37. **§4.3 attributes: required or not.** The list contains `uiSequenceCount`,
    which nothing in the profile uses. Required-ness of any attribute is not
    stated. *Decision:* reject when `uiWidth`, `uiHeight`, `uiWidthBytes`,
    `uiComp`, `uiBpcInMemory` or `uiBpcSignificant` is missing.
    `uiSequenceCount` is not read. `eCompression` defaults to 2. Tile sizes are
    optional. Values of `eCompression` other than 0/1/2 are rejected (not
    covered by the spec). Zero `uiWidth`/`uiHeight`/`uiComp` is rejected (not
    covered).

38. **[risk] §4.3 `uiBpcInMemory` 32 → `float32`.** The attributes also carry
    `ePixelType` (1 in every file seen). A 32-bit integer ND2 would be labeled
    `float32`. The spec should either key on `ePixelType` or reject non-float
    32-bit data.

39. **§4.3 experiment: order of checks.** "Any other `eType` is rejected" and
    "without `uLoopPars` … the node and its children are skipped". Is an unknown
    `eType` rejected when the node lacks `uLoopPars`? The children of a skipped
    node are never visited, so their types are never checked. *Decision:* the
    eType of every *visited* node is checked first. A missing `eType` is
    rejected.

40. **§4.3 missing counts.** A type 1 or type 4 node may have `uLoopPars` but no
    `uiCount`. *Decision:* reject. (Only type 6 has an explicit "else 0".)
    Negative counts are treated as 0. For type 8, a `pPeriod` member without
    `uiCount` is rejected.

41. **§4.3 type 8 without `pPeriodValid`, and length mismatch.** "whose entry in
    `uLoopPars/pPeriodValid` is nonzero". Nothing is said when `pPeriodValid` is
    absent or shorter than `pPeriod`. *Decision:* a missing entry counts as
    invalid (so a missing `pPeriodValid` gives count 0 and the node is
    skipped). **[risk]** The type 2 rule says "when `pItemValid` is present",
    which implies "absent = all valid" there. Type 8 has no such clause. The two
    rules should be made parallel.

42. **§4.3 type 2 `pItemValid` shorter than `Points`.** *Decision:* points
    without an entry are not counted. Note that real files also carry
    `uLoopPars/uiCount` for type 2 (25 in the public 4.6 GB file), which the
    spec ignores in favour of counting `Points`. That is fine, but worth
    stating.

43. **§4.3 type 4 missing `dZStep`/`dZHigh`/`dZLow`.** *Decision:* a missing value
    is 0.

44. **§4.3 time loop without `dPeriod`.** *Decision:* the period is 0, giving no
    unit and scale 1.

45. **§4.3 spectral node with count 0 hides its children.** The first rule
    ("without `uLoopPars`, or with a count of 0: the node and its children are
    skipped") applies before the spectral rule. So a spectral node whose count
    resolves to 0 (no `uiCount` and no `pPlanes/uiCount`, as in the public
    4.6 GB file, where `uLoopPars` is an empty level) hides any loops nested
    under it. Implemented literally. **[risk]** This is probably unintended: the
    spectral count is never used for anything else.

46. **§4.3 replacement rule wording.** "If the last loop has the same depth and
    type (`eType`) and a smaller count, the node's loop replaces it." The subject
    is "the last loop", so the replacement happens when `last.count <
    node.count` (the larger loop wins). That reading is grammatical, but "a
    smaller count" invites the opposite reading. Suggest "and its count is less
    than the node's".

47. **§4.3 picture metadata gaps.** These cases are not covered:
    - `sPlaneNew/a<i>` missing for some `i < uiCount`. *Decision:* treat as no
      planes, which falls back to `C<k>` labels.
    - Missing `sDescription`. *Decision:* the empty string.
    - Missing `uiColor`. *Decision:* `0xFFFFFF`, giving `FFFFFF`.
    - `bCalibrated` true but no `dCalibration`. *Decision:* uncalibrated.
    - A non-positive `dCalibration`. *Decision:* used as is.
    - `uiColor` stored as a signed type. *Decision:* the low 32 bits.

48. **§4.4 "the first and the last frame".** Is this frames `0` and `N−1`, or the
    first and last frames *present* in the chunk map? Frame 0 may be missing.
    *Decision:* the first and last present frames.

49. **§4.4 frame data length not validated.** Uncompressed frames: nothing
    checks that `d ≥ 8 + uiHeight × uiWidthBytes`, or that frames other than
    the first and last have the same name length. The latter is presumably
    deliberate, to avoid reading every header. *Decision:* not checked. For
    compressed frames with `d < 8`: *decision:* reject.

50. **[risk] §4.4 per-row ranges vs SPEC.md §4.3.** With `uiWidthBytes ≠ R`, an
    uncompressed frame becomes `uiHeight` ranges. A reference payload MUST NOT
    exceed 65519 bytes (SPEC.md §4.3), and each Range costs about 10–14
    encoded bytes. So a padded frame with more than roughly 5000 rows cannot be
    written to a vzip archive. The profile should reject (or otherwise handle)
    such inputs. Otherwise a virtualizer "accepts" an input it cannot write.
    This implementation outputs JSON, so it does not hit the limit.

51. **§4.4 `uiSequenceCount` vs `N`.** Frames beyond `N` are ignored, and missing
    frames are holes. A mismatch with `uiSequenceCount` is never checked. That
    is fine, but say whether it is intended.

52. **§4.5 planes with 2 or 4+ components.** Only 1- and 3-component planes are
    defined. When `uiCompCount` values sum to `uiComp` but some plane has 2 or 4
    components, the rule gives no labels. *Decision:* fall back to `C<k>` /
    `FFFFFF` for all channels. **[risk]**

53. **§4.6 `window` integers.** `V = 2^uiBpcSignificant − 1` is written as a JSON
    integer. A large `uiBpcSignificant` (≥ 54) is not exactly representable as
    binary64, which §1.1 uses for comparison. *Decision:* exact integer.
    `uiBpcSignificant > 126` is rejected (not covered).

54. **§4.6 scales.** "`x` and `y` are `micrometer` with scales `dCalibration`
    and `dCalibration × dAspect`". The order of the two pairs is ambiguous, so
    it was read as x = dCalibration and y = dCalibration × dAspect, in that
    order. Suggest "`y` is `dCalibration × dAspect` and `x` is `dCalibration`".

## Harness

55. Exit status for network errors is not 0 or 3, so it is "failed". This
    implementation uses 1. HARNESS.md is clear enough.

56. HARNESS.md's server returns `Connection` keep-alive responses with
    `Content-Length`. The client also supports chunked encoding and
    connection-close bodies. No issues were found.

## Things the implementation needed that the spec did not say

- How to choose the profile (item 1).
- What happens to malformed or truncated input (item 2).
- Required vs optional for every ND2 attribute and experiment field (items
  37, 40–44, 47).
- How many SubIFD levels there are (item 21).
