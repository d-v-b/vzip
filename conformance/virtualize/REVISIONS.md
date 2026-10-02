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

## Round 2 (revision 2)

**Result.** Fresh agents implemented revision 2. All five implementations
(the two maintained ones and the three round-2 ones) agreed on all 286
inputs, and on 640 corrupted copies of the synthetic files
(`mutate.py`): 165 equivalent and 475 rejected by all. The notes
(`notes/r2/`) list about 25 points where careful readers could still
disagree. No input reached them. The main ones:

- **Evaluation order:** whether a malformed tag or LV member rejects the
  input depended on whether, and in which order, an implementation read it.
- **TIFF field types:** which types count as integers, and counts of 0.
- **The XML tag scan:** its grammar, Unicode vs XML whitespace, duplicate
  attributes, numeric references, and the text of `UUID` elements.
- **LV types:** pointers (type 7) and bytes as numbers, binary64 or negative
  integers, and paths through values that are not objects.
- **Validity lists:** where `pItemValid` lives.
- **Sibling loops:** time loops of eType 1 and 8 at the same depth were
  dropped instead of merged.
- **Tall padded ND2 frames:** these were rejected, because one reference per
  row exceeds 65519 bytes after about 6000 rows.

**Revision 3** settles these:

- **Order independence (§1.2):** every listed check applies whether or not
  its value ends up in the output.
- **Reference payloads:** §1.2 gives the payload formula.
- **Whitespace and digits:** both are defined.
- **TIFF:**
  - every table tag of every IFD read is checked (field type, value in the
    file, count), while other tags are ignored;
  - integer tags take unsigned integer types only;
  - IFD offsets must be distinct and past the header;
  - formats and sizes are checked for IFD 0, planes, levels and level-scan
    candidates, and sizes are at least 1;
  - at most 100000 planes.
- **XML:**
  - one left-to-right scan with an ASCII grammar;
  - the first of duplicate attributes wins;
  - references are decoded only to scalar values other than U+0000;
  - UUID text is defined, and file identity is FileName, else text;
  - mapping is checked after all `TiffData`.
- **LV:** members are read as number, integer, color, flag, string, object
  or list, with the types each allows:
  - a byte counts as type 3, and pointers are not numbers;
  - paths through non-objects reject;
  - NaN and infinity reject;
  - numbers are used as binary64.
- **Experiment:**
  - full paths are given (`pItemValid` belongs to the node);
  - every member of every visited node is checked;
  - rule 4 merges by kind and carries the period or step.
- **Picture metadata:**
  - every plane member is checked;
  - a missing `SLxExperiment` or `SLxPictureMetadata` counts as absent.
- **Frames:**
  - frame names must be canonical;
  - other frames' headers are explicitly not read;
  - padded frames are split into **row blocks** (the largest divisor of the
    height whose references fit), so they are never rejected for their size.

New synthetic files exercise each rule: 13 TIFFs (`edge_xml_scan`,
`edge_field_types`, `edge_reject_shared_subifd`, ...) and 9 ND2 files
(`nd2_edge_kind_merge`, `nd2_edge_tall_padded`, `nd2_reject_pointer_number`,
...). Both maintained implementations agree on all 86 synthetic files and on
860 corrupted copies.

## Round 3 (revision 3)

**Result.** On the 317-input corpus (95 synthetic files, 205 IDR TIFFs and
17 ND2 files), the round-3 implementations agree with both maintained ones
everywhere except on the files added for revision 4. The same holds for 950
corrupted copies (`mutate.py`, seed 1). The corrupted copies also found one
bug shared by both maintained implementations: a file truncated inside the
8-byte TIFF header crashed them instead of being rejected.

The notes (`notes/r3/`) agree on a short list. Probe files showed which
points split the implementations:

| question | split |
|---|---|
| Are nodes under a skipped (count 0) node checked? | Python and Rust yes; TypeScript and both maintained implementations no |
| Is `pItemValid` a list of flags on nodes other than positions? | all three round-3 implementations yes; maintained no |
| Is `pPlanes/uiCount` read when a spectral node has `uiCount`? | TypeScript yes; the rest no |
| A LONG8 tile offset above 2^53 on a tile with no bytes | TypeScript and the browser implementation reject; the rest accept |
| LV levels nested 2000 deep | the Python reference crashed; the rest accept |

**Revision 4** settles these:

- **Experiment tree:**
  - every node is checked, whether or not the flattening visits it;
  - `pItemValid` holds flags on every node;
  - `pPlanes` is read only when `uiCount` is absent;
  - the eType 8 count sum and the frame count `N` are at most 2^53 − 1.
- **TIFF tags:** integer values of table tags are at most 2^53 − 1 in
  magnitude.
- **LV nesting:** at most 100 levels deep.
- **Smaller points, now stated:**
  - a TIFF needs at least one IFD;
  - only `SizeZ`, `SizeC` and `SizeT` are validated;
  - the end of a skipped section is searched for after its whole start
    marker;
  - a self-closing `UUID` has empty text.

New synthetic files cover each rule (`nd2_reject_skipped_bad_etype`,
`nd2_reject_itemvalid_on_time`, `nd2_edge_spectral_unread_pplanes`,
`nd2_edge_nesting_100`, `nd2_reject_nesting_101`, `edge_reject_big_offset`,
`edge_xml_comment_marker`, `edge_reject_uuid_self_closing`,
`edge_reject_no_ifds`).

## Round 4 (revision 4): converged

**Result.** The round-4 implementations agree with both maintained ones on
every input, with no difference:
- the 317-input corpus: 252 accepted and 65 rejected by all five;
- 950 corrupted copies with mutation seed 1;
- 950 corrupted copies with mutation seed 2.

All three sets of notes (`notes/r4/`) call the document precise for real
files. The points they still raise concern only malformed input, and none
splits the implementations on the corpus. They led to two fixes in the
maintained implementations, which they had missed:
- an overflowing z step on a node that the flattening skips is now
  rejected;
- the Python one now rejects an ND2 chunk length above 2^53 − 1.

**Revision 5** states the remaining points explicitly:
- a level record at depth 100 rejects even if empty;
- reading a chunk header means its 16 bytes and its name;
- a SubIFD's next-IFD field lies within the file but its value is not used;
- the XML scan is a single left-to-right pass;
- UUID text drops CDATA content;
- frame chunks need not end within the file, only their ranges;
- `uiComp` is at most 1024 (a frameless file could otherwise demand an
  unbounded `omero` channel list).

New synthetic files: `nd2_reject_too_many_components`,
`nd2_reject_z_step_overflow` and `edge_subifd_next_ignored`. The round-4
implementations differ only on the first, which is the one new rule.

## Revision 6: stage positions

Not from a spec round: revision 6 adds a rule. Each image of a multi-position
ND2 now has an OME-NGFF `translation` that places it where the stage was:
- the position's `dPosX`/`dPosY` (the centre of its field of view);
- mapped into image coordinates by the inverse of the picture metadata's
  stage-to-camera matrix (`dStgLgCT11`…`22`);
- minus half the field of view.

There is no translation when the image is uncalibrated, when a position
lacks coordinates, or when the matrix is singular.

The corpus shows that the matrix matters. S-BIAD3015's camera is rotated by
180° (the matrix is about −I), so its stage axes run opposite to the image
axes. No corpus file has overlapping fields, so the orientation convention
has not been checked against image content.

New synthetic files cover the rule: `nd2_edge_stage_positions` (a 90° camera
and an invalid point), `nd2_edge_stage_singular` and
`nd2_edge_stage_incomplete`. Both maintained implementations agree on all 101
synthetic files and on the 323-input corpus, whose 14 multi-position ND2
files now carry translations.
