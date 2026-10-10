# Notes on VIRTUALIZE.md (profiles version 0, revision 1)

These notes come from writing an independent Python implementation
(`impls/virtualize/python/`, standard library only) using only VIRTUALIZE.md,
HARNESS.md and SPEC.md. Each item gives the section, the question, what this
implementation decided, and why. Items marked **[trouble]** are rules that the
author thinks are wrong or likely to make implementations disagree.

## General (§1)

1. **How is the profile chosen? (§1, §3.1, §4.1)** Nothing says how a
   virtualizer decides whether an input is a TIFF or an ND2. File extensions
   are unreliable (`.ome.tiff`, `.tif`, `.svs`). *Decision:* the first bytes
   decide. `II*\0`, `MM\0*`, `II+\0` and `MM\0+` mean TIFF. The ND2 chunk magic
   `DA CE BE 0A` means ND2. The 12-byte JPEG 2000 signature box means a legacy
   ND2, which is rejected. Anything else is rejected. *Suggest:* say this in
   §1.2.

2. **Does a malformed file count as "rejected"? (§1.2)** The spec lists only
   specific rejections. It says nothing about reads past end of file, a bad
   chunk magic, a truncated LV record, an unknown TIFF field type or an
   invalid zlib stream in a type-76 record. HARNESS says "any other exit
   status means the implementation failed", so this decides status 3 versus a
   crash. *Decision:* every structural error is a rejection (status 3).
   Network errors are failures. *Suggest:* "An input that is not well formed
   as described here is rejected."

3. **Non-finite numbers (§1.3).** `float("NaN")` and `float("inf")` parse in
   most languages, and an ND2 `dCalibration` or `dPeriod` can be a NaN or
   infinite binary64. JSON cannot hold either value. *Decision:* OME-XML
   decimals must match decimal syntax
   (`[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?`) and must not overflow, or the
   input is rejected. Any non-finite scale is rejected. Zero and negative
   scales are not checked, although NGFF does not allow them (see 47).

## Common output (§2)

4. **`transpose` with JPEG 2000 (§2.1, §3.5).** §2.1 says a JPEG 2000 chunk
   decodes "to the chunk's `[y, x]` or `[y, x, c]`". §3.5 says "When `spp > 1`
   and PlanarConfiguration is 1 (interleaved), `transpose` comes first", for
   any compression. *Decision:* an interleaved JPEG 2000 image gets
   `[transpose, imagecodecs_jpeg2k]`, with chunk `c` equal to `spp`. This
   looks intended, but no fixture tests it (the JPEG 2000 fixture and the
   public IDR file are both planar). An explicit sentence would help.

5. **`OME/METADATA.ome.xml` without `OME/zarr.json` (§3.6).** For TIFF, the
   key `OME/METADATA.ome.xml` sits under `OME/`, but there is no
   `OME/zarr.json` (unlike ND2, §4.6). This is harmless but inconsistent with
   the bioformats2raw convention the ND2 profile follows.

## TIFF profile (§3)

6. **What counts as an IFD cycle, and what counts toward the 100000 limit?
   (§3.1)** *Decision:* one visited set of IFD offsets is shared by the main
   chain and all SubIFDs. Seeing any offset twice is a "cycle", and the
   100000 limit counts main-chain IFDs and SubIFDs together. An IFD that is
   both a SubIFD and in the main chain is therefore rejected. Another reading
   allows that sharing.

7. **SubIFD chains (§3.1).** "for every IFD in the chain the IFDs listed in
   its `SubIFDs` tag". *Decision:* only the listed offsets are read. A
   SubIFD's own next-IFD pointer and nested SubIFDs are ignored. It would be
   better to say so.

8. **SampleFormat with several values (§3.1).** "All BitsPerSample values of
   an image MUST be equal" says nothing about SampleFormat, which also has one
   value per sample. *Decision:* differing values are rejected; otherwise the
   first value is used. A BitsPerSample count that differs from
   SamplesPerPixel is ignored.

9. **"Tiled" against "required" tags (§3.1).** An image is tiled "if it has
   TileWidth and TileOffsets". The table marks TileLength and TileByteCounts
   "required (the image is tiled)". *Decision:* a tiled image (TileWidth plus
   TileOffsets) with no TileLength or no TileByteCounts is rejected.

10. **PlanarConfiguration in the format tuple when `spp = 1` (§3.1).** "only
    read when SamplesPerPixel > 1". *Decision:* the tuple uses 1 when
    `spp = 1`, whatever the tag says. Two single-sample IFDs whose stray
    PlanarConfiguration differs then have the same format. The spec implies
    this but does not state it.

11. **`<OME` detection (§3.2).** "If the first IFD's ImageDescription
    contains `<OME`": is that checked before or after cutting at the first
    NUL? *Decision:* after. Invalid UTF-8 is rejected (unspecified). An
    ImageDescription stored as UNDEFINED (type 7) instead of ASCII is accepted
    as bytes.

12. **XML parsing strictness (§3.2).** The spec describes a tolerant scan
    ("Element names may carry a namespace prefix. Attribute values have the
    five XML predefined entities and numeric character references decoded")
    but does not say what happens with malformed XML, DTD-declared entities,
    comments or CDATA. A conforming XML parser rejects things a regex scanner
    accepts. *Decision:* a tolerant tokenizer skips comments, CDATA,
    processing instructions and `<!...>` declarations, matches local element
    names, compares attribute names exactly (no prefix stripping), decodes
    only the five entities and `&#..;`/`&#x..;`, and leaves any other
    `&name;` as is. Malformed XML is not rejected. **[trouble]**: a
    browser-based implementation that uses DOMParser fails or diverges here.

13. **Which Pixels, and which TiffData? (§3.2)** "the attributes of the first
    `Pixels` element" means the first in the document, which might not be
    inside the first `Image`. "the `TiffData` elements inside it" could mean
    children or descendants. *Decision:* the first Pixels in the document,
    and every TiffData descendant of it. The `UUID` is a child of the
    TiffData.

14. **Attribute value syntax (§3.2, §3.3).** Integers (`SizeZ`, `IFD`,
    `PlaneCount`, ...): leading or trailing whitespace is allowed, and a
    non-integer is rejected. `SizeZ`, `SizeC` or `SizeT` less than 1 is
    rejected. A missing `Pixels` element means all defaults. None of this is
    specified.

15. **`DimensionOrder` validity (§3.3).** *Decision:* the value must be `XY`
    followed by a permutation of `ZCT`, or the input is rejected.

16. **Stepping through C when `spp > 1` (§3.3).** "Successive planes step
    through positions in `DimensionOrder`". When channels are samples there
    is one plane per (z, t), so what size does C have while stepping, `SizeC`
    or 1? *Decision:* `Cp` (that is, 1), so `FirstC` must be 0. **[trouble]**:
    a reader who uses `SizeC` maps planes to the wrong IFDs for multi-Z or
    multi-T RGB OME-TIFFs.

17. **TiffData positions out of range (§3.3).** Nothing says what happens
    when (`FirstZ`, `FirstC`, `FirstT`) is out of range, or when `PlaneCount`
    steps past the last position. This happens easily: a sole `TiffData` with
    `FirstZ="1"` and no `IFD` defaults `PlaneCount` to "all planes".
    *Decision:* a starting position out of range is rejected, and steps past
    the last position are ignored.

18. **A plane mapped twice (§3.3).** *Decision:* the later `TiffData` in
    document order wins. Unspecified.

19. **"consecutive IFDs" (§3.3).** *Decision:* these are indices in the main
    chain (0-based), and `IFD` is a main-chain index. This is implied by
    "mapped to main-chain IFDs".

20. **OME-XML disagreeing with the TIFF tags.** The public IDR file declares
    `Interleaved="true"` in OME-XML, but its TIFF PlanarConfiguration is 2.
    The spec (correctly) uses only the TIFF tag. It could say so explicitly
    to warn implementers.

21. **Number of SubIFD levels (§3.4).** "level `k ≥ 1` is, for every plane,
    its IFD's `k`-th SubIFD": how many levels are there? *Decision:* as many
    as the first IFD has SubIFDs. A plane IFD with fewer is rejected, and
    extra SubIFDs are ignored.

22. **"the first IFD" when it is not a plane (§3.4).** With OME-XML, the
    first plane need not be IFD 0 (`TiffData IFD="1"`). The rules "If the
    first IFD has SubIFDs" and "have the first IFD's format" then refer to
    an IFD that may not be a plane. *Decision:* followed literally (IFD 0).
    *Suggest:* "the first plane's IFD".

23. **SVS level discovery (§3.4).** "every later main-chain IFD that is tiled,
    has the first IFD's format, and is smaller in both width and length than
    the previous level". *Decision:* strictly smaller in both dimensions.
    IFDs that fail the test (for example strip thumbnails) are skipped
    without ending the search. This matches the svs fixture.

24. **Channel count for the `c` axis (§3.6).** "`c` if the channel count
    (`SizeC`, or `spp`)": *Decision:* `SizeC` after the §3.3 normalization
    with OME-XML, and `spp` without it.

25. **Units when the unit symbol is unknown (§3.6, §2.3).** When
    `PhysicalSizeX` is present but `PhysicalSizeXUnit` is not in the table,
    the axis gets no unit, but the scale still uses the value. That is
    surprising (a scale in an unknown unit), but it follows the text.

26. **Tile counts (§3.6).** "The IFD's tile count MUST be `T` times the
    number of sample planes": is that the count of TileOffsets, of
    TileByteCounts, or of both? *Decision:* both must equal it.

27. **The image name (§3.6).** "Name: the OME `Image` name, if any". An empty
    `Name=""` is written as `"name": ""`. *Suggest:* say whether an empty
    name counts.

28. **TileOffsets of 0 with a byte count above 0.** These are written as is.
    The spec only skips `n = 0`.

## ND2 profile (§4)

29. **Version string (§4.1).** "its data starts with `VerM.m`": *Decision:*
    `M` is the run of decimal digits after `Ver` and must be followed by `.`.
    The minor version is ignored, and a malformed string is rejected.

30. **Chunk validation (§4.1).** The spec does not say to check the magic of
    the chunks it reads (metadata, frames, file map), or their names.
    *Decision:* the magic is checked for every chunk read, and a mismatch is
    rejected. The file-map chunk's name (NUL-stripped) is checked. Frame and
    metadata chunk names are not checked. A file smaller than 40 bytes is
    rejected. In the map, a duplicate name keeps the last offset, and the
    record `size` is unused.

31. **Type 76 placement and scope (§4.2) [trouble].** "the rest of the data is
    a zlib stream ... of an LV structure, which replaces this one": the rest
    of the chunk data, or of the enclosing level? Which is "this one", the
    record or the whole structure? *Decision:* type 76 is accepted only as
    the first record of a chunk's top-level structure. The rest of the chunk
    data (after the 10 skipped bytes) is inflated and parsed as the top-level
    structure. Bytes after the end of the zlib stream are ignored, and a
    truncated stream is rejected. A type 76 anywhere else is rejected. The
    `nd2_compressed_positions` fixture uses it at the top level.

32. **Level length `L` against sequential parsing (§4.2).** There are two ways
    to find the end of a level's records: parse `c` records, or use
    `start of record + L`. *Decision:* the next record starts at
    `record start + L + 8c`, where "record start" is the type byte. On every
    fixture and both public ND2 files, sequential parsing ends at exactly
    `record start + L`, so this reading of "from the start of this record" is
    probably right. The spec does not say what to do when they disagree.

33. **Strings (§4.2).** "UTF-16LE code units up to and excluding the first
    NUL unit": the spec gives the value, not how many bytes the record uses.
    *Decision:* the record uses the units through the NUL. Unpaired
    surrogates are kept (surrogatepass), so a channel label could contain
    one, and JSON then holds a `\udXXX` escape. Unspecified.

34. **Record names (§4.2).** The name is cut at the first NUL. One public file
    has a member named `uiCon20(L`, so names in real files are not always
    clean. This is harmless, but implementations must not validate names.

35. **Unknown record types (§4.2).** Types 0, 10, 12 and so on are rejected.
    The spec does not say so.

36. **Lists against objects (§4.2).** "A level whose records all have empty
    names is a list": an empty level (`c = 0`) is both. That is harmless for
    the paths used. A repeated name "keeps its last value", but at which
    position among "the members ... in order"? *Decision:* the position of
    the first occurrence (insertion order) holding the last value. This
    matters for `ppNextLevelEx`, `Points` and `pPeriod` if names repeat.
    *Suggest:* specify it.

37. **Unused attribute (§4.3).** `uiSequenceCount` is listed but never used.
    It could check `N` (it equals the frame count in all three test ND2s),
    or it could be removed from the list.

38. **Missing required members (§4.3).** A missing `uiWidth`, `uiHeight`,
    `uiWidthBytes`, `uiComp`, `uiBpcInMemory` or `uiBpcSignificant` is
    rejected. `uiTileWidth` and `uiTileHeight` default to 0. An
    `eCompression` other than 0, 1 or 2 is rejected (unspecified).
    `uiComp`, `uiWidth` or `uiHeight` of 0 is rejected. A 32-bit
    `uiBpcInMemory` always means `float32`, as written, even though a
    `uint32` ND2 can exist in principle.

39. **When is `eType` checked? (§4.3)** "Any other `eType` is rejected": is
    it checked for nodes that are skipped (no `uLoopPars`, count 0) and for
    their unvisited children? *Decision:* every visited node is checked,
    including nodes skipped for having no `uLoopPars` or a count of 0. The
    children of a skipped node are not visited, so they are not checked. A
    missing `eType` is rejected.

40. **Missing loop parameters (§4.3).** A fallback is given only for spectral
    (`else 0`). *Decision:* a missing `uiCount` (types 1 and 4), `pPeriod`
    (type 8) or `Points` (type 2) is rejected, as is a missing
    `uiCount` in a `pPeriod` member. Type 4 treats a missing `dZStep` as 0;
    a missing `dZHigh` or `dZLow` gives step 0. Type 1 or 8 without
    `dPeriod` has no period.

41. **Type 8 validity (§4.3).** "whose entry in `uLoopPars/pPeriodValid` is
    nonzero": *Decision:* a missing `pPeriodValid` means every member is
    valid. A member whose index is past the end of `pPeriodValid` is
    invalid. Type 2 treats `pItemValid` shorter than `Points` the same way
    (missing entries are invalid). The type-2 count ignores
    `uLoopPars/uiCount` (they agree in the public file).

42. **Order of the flattening bullets (§4.3) [trouble].** The bullets are
    "without `uLoopPars`, or with a count of 0: the node and its children
    are skipped" and then "spectral: the node is skipped and its children
    are visited". A spectral node whose count is 0 (no `uiCount` and no
    `pPlanes/uiCount`) therefore hides its children. *Decision:* the
    bullets apply in order, so its children are skipped. This looks
    unintended: a spectral node is never a loop of the output, and its
    count does not matter.

43. **Children of dropped or replacing loops (§4.3).** "Either way its
    children are visited": the children of a dropped node are visited at
    `depth + 1`, so they can append loops after a sibling's loop. That
    follows the text. It is surprising, and it is worth an example in the
    spec. In the public 25-position file the second position node is
    dropped (equal count), and its spectral child adds nothing.

44. **Picture planes (§4.3, §4.5).**
    - A missing `sPlaneNew/a<i>` for `i < uiCount` makes the plane list
      unusable, so the generic labels `C<k>` are used (unspecified).
    - The spec says what happens for planes with 1 or 3 components only.
      *Decision:* any other `uiCompCount` (2, 4, ...) also gives the generic
      labels, even when the counts add up to `uiComp`. **[trouble]**: one
      could also give such planes `<desc> 0`, `<desc> 1`, ... labels.
    - A missing `sDescription` becomes `""` (labels such as `" R"`). A
      missing `uiColor` becomes `FFFFFF`.
    - `bCalibrated` true without `dCalibration` is treated as uncalibrated.

45. **The first and last frame (§4.4).** "MUST read the headers of the first
    and the last frame": frames 0 and `N − 1`, which may be missing, or the
    lowest- and highest-numbered frames present? *Decision:* the lowest and
    highest `f < N` present in the chunk map. With no frame present, nothing
    is read.

46. **No frame-size checks (§4.4) [trouble].**
    - Nothing checks that an uncompressed frame chunk's `d` is at least
      `8 + uiHeight × uiWidthBytes`, so references can run past the chunk.
    - `uiWidthBytes < R` gives overlapping row ranges, and nothing rejects
      it.
    - A compressed frame with `d < 8` gives a negative length.

    *Decision:* none of these are checked, following the text. *Suggest:*
    reject `uiWidthBytes < R`, `d < 8`, and an uncompressed `d` that is too
    small.

47. **x and y scales (§4.6).** "`x` and `y` are `micrometer` with scales
    `dCalibration` and `dCalibration × dAspect`" is read "respectively":
    `x = dCalibration`, `y = dCalibration × dAspect`. Writing it out per
    axis would be clearer. A zero or negative `dCalibration` is used as is,
    although NGFF does not allow it.

48. **`omero` when there is no `c` axis (§4.6).** "the image's `M` has
    `omero`" means always, so a single-component image gets a one-channel
    `omero`. *Decision:* always written. `V` is written as a JSON integer.

## Other things needed that the spec does not give

- **Reading strategy.** HARNESS's server refuses a `GET` without `Range`.
  This implementation reads in 64 KiB blocks and merges adjacent missing
  blocks into one request. The OME-TIFF needed 5 requests, and the ND2s
  needed 8 and 10.
- **Interpreter version.** The task says Python 3.12, but `python3` on this
  machine is 3.9. The wrapper runs `python3`, so the code is kept compatible
  with 3.9 or later. It gives byte-identical outputs under 3.9.6 and 3.12.13.
- **Section §5** describes two other implementations and says the reference
  decodes LV "with the `nd2` package". That means the LV rules in §4.2 were
  written to match that package. Any behaviour of the package that the text
  does not state (items 31–36) is a likely source of disagreement.
