# VIRTUALIZE.md revisions

VIRTUALIZE.md is refined in rounds. In each round, independent agents
implement the current revision in Python, TypeScript and Rust from the
document alone (plus HARNESS.md and the test inputs). They may not see any
other implementation. Each one writes notes on what was unclear. Their
outputs are compared with the maintained implementations by
`compare.py --impl`, and the document is revised from the differences and
the notes. Each round's document is kept in `spec_history/`, its notes in
`notes/r<N>/`, and its implementations in `impls/virtualize/`, which holds
only the latest round; earlier rounds are in git history.

## Round 1 (revision 1)

**Result.** On 240 inputs (24 synthetic files, the 205 OME-TIFFs of IDR
idr0096 and 11 public ND2 files), all three implementations matched the
Python reference: 231 equivalent and 9 rejected. The corpus was too regular
to show the document's gaps. The notes (`notes/r1/`, about 180 items) found
them instead: places where an independent reader had to guess, and would
very likely guess differently on other inputs.

**Revision 2** made these changes:

- **General (§1).**
  - The profile is chosen by magic bytes, and legacy (JPEG 2000) ND2 is
    rejected.
  - Any malformed input is rejected; network errors are failures, not
    rejections.
  - The output doesn't depend on pixel data, and every range lies within the
    file.
  - A reference payload larger than 65519 bytes rejects the input, and so
    does a non-finite number.
  - Both Å code points (U+00C5, U+212B) are angstrom.
- **TIFF (§3).**
  - IFD reading:
    - the SubIFD traversal and the IFD limit (100000, counting all IFDs) are
      fixed;
    - the BigTIFF reserved word must be 0;
    - of duplicate tags, the first wins;
    - "required" applies only to IFDs that are used.
  - Sample checks: SampleFormat values must be equal, and
    PlanarConfiguration must be 1 or 2.
  - OME-XML:
    - detected from the ASCII type, valid UTF-8 and an `OME` start tag;
    - read as a tag scan that skips comments, CDATA, PIs and declarations;
    - integer syntax, sizes of at least 1, `First*` bounds and
      DimensionOrder are validated;
    - PhysicalSize uses a decimal grammar and must be positive, otherwise it
      counts as absent;
    - an empty Name is omitted.
  - A multi-file dataset (UUIDs naming more than one file) is rejected; a
    self-reference is accepted.
  - The PlaneCount defaults, excess planes and the later-mapping-wins rule
    are stated.
  - Pyramids:
    - the SubIFD level count comes from IFD 0;
    - an SVS level candidate needs to be tiled and to have BitsPerSample.
  - Interleaved JPEG 2000 gets transpose.
  - Tile arrays must have exactly the expected length, and tiles must lie
    within the file.
  - `OME/METADATA.ome.xml` holds the raw bytes of the description.
- **ND2 (§4).**
  - Version syntax, the minimum file size, the chunk magic on every read,
    and chunk-map names (up to the first `!`, last duplicate wins).
  - LV:
    - an unknown type rejects the input;
    - level records must end at the level's length;
    - type 76 is allowed only as a whole chunk, with an exact zlib end and
      no nesting;
    - unpaired surrogates become U+FFFD;
    - object member order and value are first position, last value;
    - the list rule is stated;
    - member types are checked for numbers and flags;
    - defaults are listed for every member.
  - Attributes:
    - which are required;
    - width, height and components must be at least 1;
    - only `eCompression` 0 and 2 are accepted.
  - Experiment:
    - `eType` is checked first;
    - counts default to 0;
    - the validity arrays are stated (absent means all valid, missing
      entries are invalid);
    - the flattening rule has a worked example.
  - Planes:
    - the defaults are listed;
    - a component count other than 1 or 3 gives generic labels;
    - the calibration requires both the flag and a positive value;
    - `dAspect` ≤ 0 counts as 1.
  - Frames:
    - `uiWidthBytes` ≥ R;
    - the lengths of the first and last uncompressed frames are checked;
    - a compressed frame needs `d` > 8.
  - Output:
    - the display window's bit rule is stated;
    - per-axis scales are stated;
    - `omero` is always present;
    - the `t` axis is present even for a count of 1.

New synthetic inputs exercise these rules: `web/test/tiff_edge_fixtures.py`
(`edge_*.tif`) and the `nd2_edge_*` and new `nd2_reject_*` files of
`web/test/nd2_fixtures.py`. Both maintained implementations agree on all 70
synthetic files and on the 240-input corpus.
