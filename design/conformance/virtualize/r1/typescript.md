# Notes on VIRTUALIZE.md (profiles version 0, revision 1)

These notes come from an independent TypeScript implementation (`main.ts`,
`util.ts`, `xml.ts`, `tiff.ts`, `lv.ts`, `nd2.ts` in this directory). The
only inputs used were VIRTUALIZE.md, conformance/virtualize/HARNESS.md,
SPEC.md, the fixtures and the three public files. Each item gives the
section, the text, the question, what this implementation does, and why.
Items marked **[risk]** are rules that I think are wrong or likely to cause
trouble.

## General (§1, §2)

1. **How to tell which profile applies.** §1 says the virtualizer reads "an
   image file (TIFF or Nikon ND2)", but nothing says how to choose the
   profile: by magic bytes, by extension, or by trying both. *Decision:* sniff
   the first bytes. `II`/`MM` means TIFF. The ND2 chunk magic `DA CE BE 0A`
   or the 12-byte JPEG 2000 signature box (`00 00 00 0C 6A 50 20 20 0D 0A 87
   0A`) means ND2. Anything else is rejected with status 3. A sentence giving
   the dispatch rule would remove this question.

2. **Malformed input versus rejection.** §1.2 says "It MUST reject an input
   the profile does not accept", but the profiles only list some rejection
   reasons. Many are missing: reads past EOF, offsets pointing nowhere, bad
   chunk magic at a chunk-map offset, truncated LV data, an unknown LV record
   type, a missing required attribute. *Decision:* every structural failure
   rejects with status 3. Only network or internal errors give another
   status. The spec should state that any input that cannot be parsed as
   described is rejected.

3. **Integer values above 2^53.** §1.3 says "Integers are exact", but `u64`
   offsets, LV `i64`/`u64` values and JSON numbers are binary64 in practice.
   *Decision:* an offset above 2^53−1 is rejected. LV 64-bit values are
   converted to Number, so they may lose precision; this does not matter for
   any value the profiles use.

4. **§2.1, `bytes` codec for 1-byte types:** clear. One wording point: §2.1
   says the JPEG 2000 codestream decodes "to the chunk's `[y, x]` or
   `[y, x, c]`". It does not say explicitly that with interleaved JPEG 2000
   the `transpose` codec comes before `imagecodecs_jpeg2k`. §3.5 implies it
   ("When spp > 1 and PlanarConfiguration is 1 (interleaved), transpose comes
   first"), so that is what I do.

## TIFF profile (§3)

5. **§3.1 SubIFD traversal.** "for every IFD in the chain the IFDs listed in
   its SubIFDs tag (330), if any."
   - Are a SubIFD's own next-IFD pointers or its own SubIFDs followed?
     *Decision:* no. Only the listed offsets are read.
   - What counts as an "IFD cycle"? A main-chain IFD revisited is clearly a
     cycle. A SubIFD offset that equals a main-chain IFD, or a repeated SubIFD
     offset, is not clearly one. *Decision:* only main-chain revisits are
     rejected.
   - Do SubIFDs count toward "more than 100000 IFDs"? *Decision:* yes; every
     IFD read counts.
   - Are SubIFDs of IFDs that no plane uses read, so that a broken one
     rejects the file? *Decision:* yes. All SubIFDs are read eagerly, because
     §3.1 says the virtualizer "reads ... for every IFD in the chain the IFDs
     listed". Another implementation could read them lazily and accept such
     files.

6. **§3.1 tag reading details not covered by "as TIFF 6.0 and BigTIFF define
   them":** duplicate tags (*decision:* the first one wins), unknown field
   types (*decision:* ignored unless the tag is used, then rejected), and
   BigTIFF headers with an offset size other than 8 (*decision:* rejected).

7. **§3.1 SampleFormat per sample.** "All BitsPerSample values of an image
   MUST be equal" says nothing about SampleFormat, which also has one value
   per sample. It also says nothing about a BitsPerSample count that differs
   from SamplesPerPixel. *Decision:* the first SampleFormat value is used and
   counts are not checked. Suggest: "All BitsPerSample values, and all
   SampleFormat values, MUST be equal".

8. **§3.1 required tags.** The table marks TileLength, TileByteCounts and
   BitsPerSample "required", but "An image is tiled if it has TileWidth and
   TileOffsets". *Decision:* a level image that is tiled but lacks
   TileLength, TileByteCounts or BitsPerSample is rejected. In the SVS
   pyramid scan (§3.4), an IFD that lacks required tags is skipped, not
   rejected, because it is just "not a level". The spec should say whether
   "required" applies to every IFD or only to the images that are used.

9. **§3.1 PlanarConfiguration values other than 1 and 2.** These are not
   mentioned. *Decision:* rejected when spp > 1.

10. **§3.2 OME-XML detection.** "If the first IFD's ImageDescription contains
    `<OME`".
    - **[risk]** "Element names may carry a namespace prefix", but a document
      whose root is `<ome:OME ...>` does not contain the string `<OME`, so it
      is not treated as OME-XML. This contradicts the prefix allowance.
      *Decision:* literal substring test, as written.
    - Is the test made on the raw tag bytes or on the bytes up to the first
      NUL? *Decision:* on the decoded text up to the first NUL, which is `X`.
    - `<OME` also matches text inside comments, `<OMEGA`, and so on. This is
      accepted literally.

11. **§3.2 / §3.6 the bytes of `OME/METADATA.ome.xml`.** "X (the tag's bytes
    up to the first NUL, as UTF-8)" and "the entry `OME/METADATA.ome.xml`
    holds `X` as UTF-8."
    - Is the entry the original bytes, or the decoded string re-encoded? The
      two differ when the bytes are not valid UTF-8 (replacement characters)
      or start with a BOM (many decoders strip it). *Decision:* decode with
      U+FFFD replacement and keep the BOM, then re-encode. Valid UTF-8 is
      therefore preserved byte for byte. Suggest: "the entry holds the tag's
      bytes up to the first NUL, unchanged", and say whether invalid UTF-8 is
      rejected.
    - Not said: what if the ImageDescription has type BYTE or UNDEFINED
      rather than ASCII? *Decision:* the raw bytes are used regardless of
      type.

12. **§3.2 which elements.** "the attributes of the first `Pixels` element"
    means the first in the whole document or the `Pixels` of the first
    `Image`? *Decision:* the first in document order (pre-order). "the
    `TiffData` elements inside it" means children or descendants? *Decision:*
    descendants of that `Pixels`, in document order. "the `FileName` of a
    `UUID` child" means a direct child of `TiffData` (*decision:* yes).

13. **§3.2 XML decoding.**
    - "Attribute values have the five XML predefined entities and numeric
      character references decoded." Not covered: XML attribute-value
      normalization (tab, CR and LF become spaces), and entities from an
      internal DTD subset. *Decision:* no normalization; other entities are
      left as is.
    - Are attribute names compared with or without a prefix? *Decision:*
      exact match on the unprefixed names (`IFD`, `SizeZ`, ...).
    - Malformed XML (mismatched tags) is not covered. *Decision:* rejected.

14. **§3.3 numeric attribute syntax.** How are `SizeZ="abc"`, `SizeZ=" 3 "`,
    `SizeZ="0"`, `PhysicalSizeX="NaN"` or `PhysicalSizeX=""` treated? *Decision:*
    - integer attributes (`Size*`, `First*`, `IFD`, `PlaneCount`) must match
      `^\+?[0-9]+$` after trimming, else the input is rejected;
    - a size of 0 is rejected;
    - a `PhysicalSize*` that is not a decimal float literal is treated as
      absent (no unit, scale 1).

    All of these choices are mine.

15. **§3.3 DimensionOrder validation.** An invalid value (`XYZZT`, `ZCTXY`)
    is not addressed. *Decision:* anything other than the six `XY` + a
    permutation of `ZCT` is rejected.

16. **§3.3 position space when spp > 1.** "Successive planes step through
    positions in DimensionOrder" does not say how large the C dimension of
    the position space is when channels are samples. *Decision:* C has size
    `Cp` = 1, so planes are numbered over (z, t) only and `FirstC` must be 0.
    Please state this.

17. **§3.3 out-of-range and overlapping `TiffData`.**
    - A `FirstZ`/`FirstC`/`FirstT` at or above its size: *decision:* rejected.
    - `PlaneCount` stepping past the last position: *decision:* the excess
      planes are ignored, not rejected.
    - Two `TiffData` that map the same plane: *decision:* the later one wins.
    - "PlaneCount defaults to all planes": *decision:* this means the total
      plane count, even if the start position is not 0, with the excess
      ignored as above.

    None of these is specified, and implementations will differ.

18. **§3.3 [risk] UUID FileName rejection.** "A `TiffData` with a `UUID`
    `FileName` is rejected (multi-file datasets)." Many single-file
    OME-TIFFs written by Bio-Formats carry
    `<UUID FileName="itself.ome.tif">`. Read literally, the rule rejects
    them. *Decision:* implemented literally. Consider rejecting only when the
    `FileName` (or the UUID) differs from the file's own, or when the
    `TiffData` elements reference more than one UUID.

19. **§3.4 SubIFD level count.** "level `k ≥ 1` is, for every plane, its
    IFD's `k`-th SubIFD" does not say how many levels there are when planes
    have different SubIFD counts. *Decision:* the number of levels is
    1 + the SubIFD count of the first IFD, and a plane whose IFD lacks the
    `k`-th SubIFD rejects the input. Taking the minimum over planes would
    also be reasonable.

20. **§3.4 "the first IFD".** The first IFD of the main chain may not be a
    plane at all (for example `TiffData IFD="1"`). The spec uses it both for
    "If the first IFD has SubIFDs" and for "the first IFD's format". It also
    says (§3.3) "Let `spp` be the first IFD's SamplesPerPixel".
    *Decision:* main-chain IFD 0 in every case. Also, an empty SubIFDs tag
    (count 0) is treated as "has SubIFDs" with no extra levels.

21. **§3.4 [risk] the SVS pyramid scan.** "every later main-chain IFD that is
    tiled, has the first IFD's format, and is smaller in both width and
    length than the previous level" means the last accepted level
    (*decision:* yes). A tiled thumbnail with the same compression (rare,
    but possible) would be taken as a level. It would then block real,
    larger levels after it, because each level must be smaller than the
    previous one. This is implemented as written.

22. **§3.6 PhysicalSize present but zero or negative.**
    `y = (PhysicalSizeY or 1) × ...` reads as "if absent". *Decision:* a
    parsed value of 0 is used as is, giving scale 0, which OME-NGFF
    validators may reject. Suggest: "positive PhysicalSize, else 1 and no
    unit".

23. **§3.6 empty Image `Name`.** "Name: the OME `Image` name, if any."
    *Decision:* `Name=""` is present, so `"name": ""` is written.

24. **§3.6 TileByteCounts count.** "The IFD's tile count MUST be `T` times
    the number of sample planes" does not mention TileByteCounts.
    *Decision:* its length must equal TileOffsets' length, else the input is
    rejected.

25. **§3.6 units.** "(default `µm`) when that `PhysicalSize` is present"
    applies only with OME-XML (*decision:* without OME-XML there are no
    units). An unknown unit symbol gives no unit, but the scale still uses
    the value (§2.3 "Any other symbol gives no unit").

26. **§3.6 chunk shape per level.** The chunk shape is per level (TileLength
    and TileWidth of that level), so levels can have different chunk shapes.
    The public IDR file has a 1024×1019 tile on level 5. This is clear, but
    it is surprising enough to mention.

## ND2 profile (§4)

27. **§4.1 legacy detection.** "Files that start with a JPEG 2000 signature
    (version 1, "legacy") are rejected." Which bytes? *Decision:* the 12-byte
    JP2 signature box `00 00 00 0C 6A 50 20 20 0D 0A 87 0A`.

28. **§4.1 chunk map.**
    - "each a name ending in `!`, then `u64` offset and `u64` size": what is
      "size" (the data length, or the whole chunk)? It is unused here.
    - Names are parsed up to the first `!`, so a name with an embedded `!`
      would break parsing. The spec should say the name runs to the first
      `!`.
    - Duplicate names: *decision:* the last one wins.
    - Should the chunk at a map offset be checked (magic, and name equal to
      the map's name)? *Decision:* the magic is checked (else reject); the
      name is checked only for the file-map chunk itself.

29. **§4.2 type 76 (compressed).** "the name-length byte is followed by no
    name and 10 bytes to skip; the rest of the data is a zlib stream (RFC
    1950) of an LV structure, which replaces this one."
    - Is "the rest of the data" the rest of the chunk, or of the enclosing
      level's extent?
    - Does "replaces this one" mean the record, or the whole structure being
      parsed (including records already read before it)?

    *Decision:* when a type-76 record appears, the rest of the current
    region (the chunk, or the level's records extent) is inflated and parsed.
    Its records replace everything parsed so far in that sequence. No sample
    file contained type 76, so this was not exercised.

30. **§4.2 type 11 (level).** `L` and `c` are redundant. *Decision:* parse
    exactly `c` records. If they do not end at `start + L`, reject. Then
    skip `8c` bytes after `start + L`. The spec does not say what to do on a
    mismatch, or whether the records are bounded by `L`.

31. **§4.2 unknown record types** (0, 10, 12, ...) and trailing bytes after
    the top-level records are not covered. *Decision:* reject. The top-level
    sequence runs to the end of the chunk data. No public or fixture file
    had padding, but if real files do, this rejects them.

32. **§4.2 "a repeated name keeps its last value".** For "the members of" an
    object, which position does a repeated name take: first or last
    occurrence? *Decision:* first occurrence's position, last value. The
    public Z-stack file repeats `dZLow` inside `uLoopPars`. A level with
    *some* empty names (mixed) is an object whose empty-named records
    collapse into one member `""`. The spec should say what "members" means
    here. An empty level (`c = 0`) is ambiguous between object and list, but
    harmless.

33. **§4.2 booleans.** `bCalibrated` "is true" assumes type 1. *Decision:* a
    numeric 1 is also accepted as true.

34. **§4.3 [risk] 32-bit data.** "`uiBpcInMemory` 8, 16, 32 gives `uint8`,
    `uint16`, `float32`". 32-bit integer ND2 data (if it exists; ND2 has
    `ePixelType`, which is 1 in all samples seen) would be labeled float32.
    This is implemented as written. Consider checking `ePixelType`.

35. **§4.3 missing attributes and other compression values.** Missing
    `uiWidth`, `uiHeight`, `uiWidthBytes`, `uiComp`, `uiBpcInMemory` or
    `uiBpcSignificant` is not covered (*decision:* reject). An
    `eCompression` other than 0, 1 or 2 is not covered (*decision:*
    reject).

36. **§4.3 order of the experiment checks.** "Any other `eType` is
    rejected", but nodes "without `uLoopPars`, or with a count of 0: the node
    and its children are skipped". Is an unknown eType rejected even in a
    node that is skipped, or in a subtree that is never visited?
    *Decision:* the eType is checked on every node that is visited, before
    the skip test. Children of skipped nodes are not visited, so their
    eTypes are not checked. A missing `eType` is rejected.

37. **§4.3 the replacement rule.** "If the last loop has the same depth and
    type (`eType`) and a smaller count, the node's loop replaces it."
    Grammatically "a smaller count" belongs to the last loop, so the larger
    loop wins. A quick reading can take it the other way. I first
    implemented it backwards. The `nd2_compressed_positions` fixture
    (sibling position loops of 3 valid and 2 points, frames 0–8 present)
    agrees with "the larger wins". Suggest: "and its count is smaller than
    the node's count".

38. **§4.3 eType 8 without `pPeriodValid`.** This is not covered.
    *Decision:* all periods count as valid. When `pPeriodValid` (or
    `pItemValid` for eType 2) is shorter than the list, the missing entries
    count as 0 (invalid).

39. **§4.3 children of a dropped node.** "Otherwise the node's loop is
    dropped. Either way its children are visited." They are visited at depth
    `node + 1`, even though the node contributes no loop. This is
    implemented as written; it is a little surprising.

40. **§4.3 picture planes.**
    - A missing `sPicturePlanes/sPlaneNew/a<i>` for some `i < uiCount`:
      *decision:* the labeling falls back to `C<k>`/`FFFFFF`.
    - A missing `uiColor`: *decision:* 0, giving `000000`.
    - A missing `sDescription`: *decision:* label `""`.

41. **§4.3 / §4.6 calibration edge cases.** "`dCalibration` (µm per pixel)
    when `bCalibrated` is true": if `dCalibration` is absent, *decision:*
    treated as uncalibrated. If it is 0 or negative, it is used as is
    (scale ≤ 0). Suggest "when `bCalibrated` is true and `dCalibration` is
    positive".

42. **§4.4 [risk] `uiSequenceCount` is ignored.** `N` comes only from the
    experiment loops. A file without `SLxExperiment` but with many frames
    (for example a broken or stripped experiment) silently virtualizes only
    frame 0. Suggest rejecting when `N ≠ uiSequenceCount`, or at least
    stating that this is intended.

43. **§4.4 "the first and the last frame".** Frames 0 and `N−1` may be
    missing from the chunk map. *Decision:* the lowest- and highest-numbered
    frames below `N` that are present. If no frame is present, no header is
    read and the output has no chunk entries.

44. **§4.4 frame size not checked.** The spec does not require the frame
    chunk to be large enough (`d ≥ 8 + uiHeight × uiWidthBytes`
    uncompressed). Nor does it say what `uiWidthBytes < R` means: the row
    ranges would overlap. *Decision:* neither is checked. The formulas are
    applied literally. For compressed frames with `d < 8`: *decision:*
    reject.

45. **§4.5 planes whose component count is not 1 or 3.** "If the planes'
    `uiCompCount` values add up to `uiComp`, the components are labeled
    plane by plane", but only 1 and 3 components are defined. *Decision:* if
    any plane has another count, the whole image falls back to
    `C<k>`/`FFFFFF`.

46. **§4.6 which calibration goes to which axis.** "`x` and `y` are
    `micrometer` with scales `dCalibration` and `dCalibration × dAspect`"
    reads as "respectively". *Decision:* x = dCalibration, y = dCalibration ×
    dAspect. All samples have `dAspect = 1`, so this was not exercised;
    please state it per axis.

47. **§4.6 time axis with a count of 1.** A time loop with `uiCount = 1`
    still produces a `t` axis of length 1 (the axis exists "if there is a
    time loop"). The TIFF profile instead drops axes of size 1. This is
    consistent with the text but asymmetric between profiles.

48. **§4.6 `omero` placement.** "the image's `M` has `"omero"`" means it sits
    next to `multiscales` inside `attributes.ome` (*decision:* yes). The
    window uses `uiBpcSignificant` even for uint8/uint16 (for example 12
    bits, giving 4095), as written.

## Things I needed that the spec did not say

- How to choose the profile (item 1) and what counts as a rejection versus a
  failure (item 2).
- The JP2 signature bytes (item 27).
- What "members" means for objects with repeated names (item 32).
- That `transpose` precedes `imagecodecs_jpeg2k` (item 4); this is implied.
- §5 refers to implementations and files (`compare.py`, `corpus_nd2.txt`)
  that independent implementers may not read. The spec text alone was enough
  apart from the items above.
