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

## Revision 7: JPEG tiles (Aperio SVS)

Revision 7 adds a rule; it does not come from a spec round. Tiled TIFFs with
JPEG tiles (Compression 7) are virtualized, with a new `imagecodecs_jpeg`
codec. Supported layouts: 8-bit, greyscale, or 3 interleaved samples in RGB
or YCbCr. This covers Aperio SVS, the common whole-slide format.

**Why the reference adds literal bytes.** A JPEG-in-TIFF tile is not a
stand-alone JPEG. Its tables are in the IFD's JPEGTables, and Aperio stores
RGB samples (component IDs 0, 1, 2), which decoders take for YCbCr.

**The rebuilt tile.** Each tile's reference is a concatenation:
- a literal prefix: SOI, an Adobe colour marker from
  PhotometricInterpretation, and the tables;
- the tile's bytes, without its SOI.

The result is a complete standard JPEG, with no pixel data copied.

**Other changes:**
- §1.1 now compares literal ranges;
- §1.2 gives their payload size, and its tag bytes for source ranges are
  corrected (fields 3 and 4: `0x18`, `0x20`);
- HARNESS.md writes a literal range as `{"data": base64}`.

**Tests.**
- Synthetic files:
  - `jpeg_aperio_rgb` (abbreviated RGB tiles with shared tables);
  - `jpeg_ycbcr` and `jpeg_gray` (written by tifffile);
  - five rejections.
- Pixels: both implementations match tifffile pixel for pixel.
- Real slides: OpenSlide's Aperio samples (CC0) are now in the comparison
  corpus (`corpus_tiff.txt`). Every pyramid level of CMU-1 and
  CMU-1-Small-Region matches tifffile.

## Revision 8: NDPI

Revision 8 adds a rule; it does not come from a spec round. It adds §3.7,
Hamamatsu NDPI, a variant of the TIFF profile.

**Reading.** NDPI's 64-bit extension is the 8-byte header offset, 8-byte
next-IFD offsets, and a high word per IFD entry. NDPI is detected by probing
the 8-byte header offset for an IFD with tag 65420, a test that cannot
mistake a classic TIFF for NDPI. File extensions are not used.

**Chunks.** Each level is one JPEG strip with restart markers. A chunk is up
to 1024 × 1024 px of restart intervals, rebuilt as a JPEG stream from ranges
of the file:
- the strip's header, with a literal frame header for the chunk's size;
- the intervals, each followed by a literal restart marker numbered for the
  new stream;
- EOI.

Chunks at the right and bottom edges repeat the last intervals; those pixels
fall outside the array. Pixel size comes from XResolution/YResolution.

**Tests.**
- Synthetic files (`web/test/ndpi_fixtures.py`) are cut from CMU-1.ndpi's
  restart intervals: a 3-level file whose level 0 has padded edge chunks, a
  single-strip level and a skipped macro image, plus four rejections.
- Pixels: both implementations match tifffile pixel for pixel on the
  synthetic file, and on every level checked of three OpenSlide NDPI slides
  (CC0), including the edges.
- Real slides: CMU-1, Hamamatsu-1 (6.9 GB, offsets above 4 GiB) and
  Hamamatsu-2 are in the comparison corpus.
- Neuroglancer: the fork renders the rebuilt streams to within 0.5 grey
  levels of tifffile, across a chunk boundary.

## Revision 9: spatial metadata

Revision 9 adds rules; it does not come from a spec round. Every format now
carries the pixel size and position it declares, as OME-NGFF `scale` and
`translation`.

| format | pixel size | position |
|---|---|---|
| OME-TIFF | `PhysicalSize*` (as before) | the `Plane` at z = c = t = 0: `PositionX/Y`, converted to the axes' units; taken as the image's centre |
| Aperio SVS | `MPP` from the ImageDescription's fields | `Left`/`Top` (mm): the scanned area's top-left |
| other TIFF | XResolution/YResolution with ResolutionUnit inch or cm | none |
| NDPI | resolution tags (as before) | X/YOffsetFromSlideCenter (nm): the image's centre |
| ND2 | `dCalibration` (as before) | stage positions, now also for a single image (`dXPos`/`dYPos`) |

§2.3 now defines length conversion and how a centre becomes a translation.
A translation is the same at every level.

**Tests.** New synthetic files check each rule against a hand calculation:
- Aperio fields, including a duplicate `MPP`, where the first wins;
- resolution tags in centimetres and inches, and an unusable unit;
- an OME `Plane` position in nm on µm axes, where a non-zero-z `Plane`
  comes first;
- NDPI slide offsets;
- an ND2 single image under a 180° camera.

Both maintained implementations agree on all 120 synthetic files and on the
343-input corpus.

**Assumptions not verified against image content:**
- an OME `Plane` position is the image's centre (as ND2 and NDPI stage
  positions are);
- Aperio `Left`/`Top` are the scanned area's top-left, in slide
  coordinates with x right and y down.

**Not covered:** z positions (ND2 `dZLow`/`dZPos`, NDPI
ZOffsetFromSlideCenter, OME `PositionZ`).

## Revision 10: one document per profile

Revision 10 changes no rules. VIRTUALIZE.md keeps what every profile shares
(§1, §2) and the conformance notes (§6), and each profile is now a document
of its own in `profiles/`: TIFF (`tiff.md`, §3), NDPI (`ndpi.md`, §4,
formerly §3.7) and ND2 (`nd2.md`, §5, formerly §4). The conformance section
moved from §5 to §6. Revision 9, the last single-file revision, is
`spec_history/VIRTUALIZE.r9.md`.

The implementations are organized the same way: `src/vzip/virtualize/` and
`web/src/virtualize/` have one directory per profile (`tiff/`, `ndpi/`,
`nd2/`) next to what they share (`common`), and the synthetic inputs are in
`web/test/fixtures/<profile>/`, written by `web/test/<profile>/`.

## Revision 11: DICOM, NIfTI and IMS

Revision 11 adds three profiles; they do not come from a spec round. §1.2's
table now chooses among six profiles from the file's first 552 bytes. It
tests for `DICM` at byte 128 first, so DICOM files whose preamble holds a TIFF
header (as four of pydicom's sample files do) are read as DICOM. It then
tests the full four-byte TIFF magic, which the implementations had shortened
to the byte-order mark. §2.2 lets a profile add one attribute member of its
own, beside `ome`, for what OME-NGFF cannot express. Conformance moved from
§6 to §9.

| profile | inputs | rejected |
|---|---|---|
| DICOM (§6) | Part 10 files: native pixel data (implicit/explicit VR, either byte order), JPEG Baseline and JPEG 2000 frames over one or more fragments; multi-frame along `z`; TILED_FULL whole-slide levels | other transfer syntaxes, palette colour, native YBR, TILED_SPARSE, several focal planes or optical paths |
| NIfTI (§7) | NIfTI-1 and NIfTI-2 single files, either byte order, up to 5 dimensions, integer, float and RGB(A) voxels; slices split into row blocks above 128 KiB; intensity scaling recorded as a `nifti` attribute | gzip, header-and-image pairs, complex types, dimensions 6–7 (CIFTI) |
| IMS (§8) | Imaris 5.5–10 files: a hand-read HDF5 subset (superblock 0–3, object headers v1/v2, symbol-table, compact and dense groups and attributes, v1 B-tree, single-chunk and fixed-array chunk indexes, absolute soft links), uncompressed or deflate chunks | LZ4, shuffle and other filters, contiguous layouts, other chunk indexes, shared messages |

**Tests.** Each profile has synthetic files in `web/test/fixtures/<profile>/`
(43 DICOM, 64 NIfTI, 27 IMS, about 580 KB in all), checked pixel by pixel
against pydicom, nibabel and h5py. Both maintained implementations agree on
all 254 synthetic files of the six profiles, on mutants of the new ones
(420 DICOM, 1920 NIfTI, 1350 IMS) and on public files: 17 DICOM (pydicom
samples and NCI Imaging Data Commons whole-slide levels), 12 NIfTI and 21
IMS.

**Not yet done.** No independent implementation round has read the new
profiles.

**A first independent reading of IMS.** A standard-library implementation
written from §1, §2, §8 and HARNESS.md alone agreed with both maintained
implementations on all 27 IMS fixtures, 270 mutants and the 21 public files.
Its notes found one real bug, in the profile and both implementations: with
one z plane at level 0, the z axis was dropped even when chunks held several
z planes, so a chunk's bytes did not decode to its Zarr chunk. The z axis
now stays when any level's `cz` is more than 1 (new fixture
`ims_2d_deep_chunks`). Three wordings were also fixed: where a v2 object
header's optional fields start, how the time step's Δ is computed in
binary64, and a wrong cross-reference to §2.3's centre rule.

Points the notes raised that are not yet settled, for the next round: which
fields count as lengths for the 2^53 − 1 check; whether a v2 B-tree node's
record count is checked; whether a fractal heap direct block must lie wholly
within the file; whether fixed-array checks apply to unallocated entries; the
padding of v1 attribute messages; and whether a datatype or dataspace must
lie within its declared size or only within the message.

## Revision 12: store inputs, N5 and Zarr v2

Revision 12 adds the first inputs that are not single files. A URL whose
path ends in `/` (or a local directory) is a **store input** (§1.4): the
virtualizer lists the store with S3 ListObjectsV2 (§1.5), chooses the profile
from the root keys (`.zarray` or `.zgroup`: Zarr v2, §10; else
`attributes.json`: N5, §9), reads only the metadata documents, and writes one
`url` source per non-empty chunk object, in UTF-8 order of its key, each
chunk entry `(i, 0, size)` under the object's own key. §1.1 compares source
tables as lists, §1.2's payload rule now covers sources other than 0, and
§1.6 fixes one strict JSON reading for both implementations (RFC 8259, no
BOM, last duplicate member wins, binary64 numbers, integers up to 2^53 − 1,
256 levels of nesting). Conformance moved from §9 to §11.

| profile | inputs | rejected |
|---|---|---|
| N5 (§9) | groups (explicit and implicit) and datasets of 1–32 dimensions, unsigned and signed integers and floats; raw, gzip, zlib (`useZlib`), zstd and blosc blocks through the `n5_default` codec, dimensions not reversed; COSEM and n5-viewer multiscales as OME-NGFF 0.5 | other data types; lz4, xz, bzip2, jpeg and unknown compressions; blosc configurations that are not exactly expressible |
| Zarr v2 (§10) | groups and arrays of 0–32 dimensions, bool, integer and float types in either byte order, C and F order, `.` and `/` separators, zlib, gzip, zstd and blosc; OME-NGFF 0.4 as 0.5 | filters; other compressors; strings, objects, structured, complex and date-time types; fill values the data type cannot hold |

**Tests.** 40 synthetic N5 stores (29 rejected) and 40 Zarr v2 stores (34
rejected), about 58 KB of objects, are checked against an independent N5
block reader and zarr-n5, and against zarr-python's Zarr v2 reader. Both
implementations agree on all 335 synthetic inputs of the eight profiles, on
2800 store mutants (400 N5 + 400 Zarr v2 with seed 0, 1000 + 1000 with seed 1), and on
12 OpenOrganelle stores (6 N5, 6 Zarr v2) through the proxy; one array of
each format was compared pixel by pixel with an independent reader over HTTP.

**Scale.** The Python reference virtualized OpenOrganelle's
`jrc_hela-2.n5/em/fibsem-uint16/s0` (12000 × 1600 × 6368 in 64³ gzip
blocks; 382288 blocks exist) in 328 s, almost all of it the 383 sequential
listing requests (19 s of CPU), with a peak resident set of 460 MiB. The
archive is 43.7 MB with 382291 entries (its source table is 40.1 MB of URLs,
0.98 MB deflated) and opens in 1.2 s. The browser implementation fails, by
§11's limit, on stores of more than 100000 objects.

**Not yet done.** No independent implementation round has read §1.4–§1.6, §9
or §10.

## Revision 13: OME-Zarr 0.4 to 0.5

Revision 13 adds the OME-Zarr profile (§11, `profiles/ome-zarr.md`), a
different use of vzip: **format migration without copying data**. The input
is already Zarr, an OME-Zarr 0.4 hierarchy on Zarr v2 storage; the output is
the same data as OME-Zarr 0.5 on Zarr v3. Every chunk is referenced in place,
unchanged (the Zarr v3 `v2` chunk key encoding keeps Zarr v2's keys, and
OME-Zarr 0.5 allows any Zarr v3 chunk key encoding); only the metadata is
rewritten. Conformance moved from §11 to §12.

**Profile choice (§1.4).** A store whose root has `.zarray` or `.zgroup` is
read by §11 when it declares OME-NGFF 0.4, and by §10 otherwise. The test
reads at most three documents, in order: the root's `.zattrs` (0.4
multiscales, or a `plate` or `well` of version 0.4), and, for a
`bioformats2raw.layout` root, the first image's (through the plate's first
well's first field, or `OME`'s `series[0]`, or `0`).

**What moved out of §10.** §10.4 (0.4 multiscales and omero moved under
`ome`, `dimension_names` on levels, with nothing checked) is gone: §10 now
copies every attribute unchanged, and its summary lost `images`. Its
hierarchy reader is shared with §11, whose arrays and chunks are exactly
§10's.

**§11.** The input is checked against the OME-NGFF 0.4 requirements that
0.5 also has (axes, datasets, coordinate transformations, scale order,
omero, labels and image-label, plates, wells, acquisitions, bioformats2raw
numbering and series; §11.3 lists each rule), and rejected otherwise; 0.4's
SHOULDs are not checked. OME groups get their 0.4 members under
`{"ome": {"version": "0.5", ...}}`, without the per-entry `version`s;
other attributes stay outside `ome`, and nodes that are not OME are §10's.
Image levels get `dimension_names`. A bioformats2raw collection's
`OME/METADATA.ome.xml` is referenced whole, like a chunk.

**Label levels (L7).** OME-NGFF 0.4 and 0.5 both require a label image to
have as many levels as its image. omero-zarr, which wrote the IDR's OME-Zarr
0.4 data, gives every label image one level more: all eight labelled IDR
samples in the corpus have it, so the strict rule rejected every one. A
label image with more levels than its image now keeps only the first `N`
datasets in its 0.5 `multiscales` (each unchanged); the dropped levels'
arrays stay in the hierarchy as plain arrays, without `dimension_names`.
Fewer levels than the image is still rejected. The summary reports
`droppedLabelLevels`.

**Ambiguities in the OME-NGFF text**, and the reading taken:

- 0.5's prose says a plate "MUST contain a version key", but its examples
  and JSON schema have none, and `ome-zarr-models` only warns: the plate's
  `version` is dropped like the others.
- `omero`'s own `version` (in 0.4's example) is dropped, as 0.5's example
  does; neither version specifies it.
- "Ordered from largest (i.e. highest resolution) to smallest" is read as
  scales that never decrease from one level to the next (what
  `ome-zarr-models` checks), not as shapes.
- The omero MUSTs (6-digit color, window min/max/start/end) are on the
  current 0.4 page but came with 0.5.1 (PR-191); they are checked.
- "Only integer values are supported" for labels is a comment in 0.4's
  layout tree, a MUST in 0.5; it is checked on the kept label levels.
- 0.4 allows one axis that is "channel or a null / custom type"; a channel
  axis and a custom axis together are rejected (`ome-zarr-models` allows one
  of each).
- Transformations given by `path` (binary data in an undefined format) are
  allowed by 0.4 but rejected as unsupported.

**Tests.** 61 synthetic stores (10 accepted, 51 `ome_zarr_reject_*`, one per
rule; 158 KB) are written by `web/test/ome-zarr/write_fixtures.py`.
`web/test/ome-zarr/verify.py` validates every output group with
`ome-zarr-models` 0.5 and every accepted input with 0.4 (of the rejected
inputs, the 0.4 models reject 34 and do not cover the other 17 rules), and
reads every array through vzip against zarr-python's Zarr v2 reader. Both
implementations agree on all 396 synthetic inputs of every profile (145 equivalent, 251
rejected) and on 4230 mutants of the OME-Zarr stores (seeds 0, 1 and 2). The corpus
(`corpus_ome_zarr.txt`) has 14 IDR stores through the proxy (images, eight
images with extra label levels, an HCS plate of 1440 fields and a
bioformats2raw plate with OME-XML) and one py-only plate of 102114 objects:
both implementations produce equivalent
outputs for all 14. Every output validates with `ome-zarr-models` 0.5
(1513 groups for the plate alone; the only warnings are 0.5's missing plate
`version`, above), and arrays of an image, of four images' kept and dropped
label levels, and of a field of each plate equal zarr-python's Zarr v2
reading over HTTP. (zarr-python cannot read idr0073's `>u1` arrays as Zarr
v2, so that image was not pixel-checked.)

**Not yet done.** No independent implementation round has read §11.

## Revision 14: shared JPEG headers in data sources

Revision 14 changes the TIFF and NDPI outputs; it does not come from a spec
round.

**The problem.** An NDPI chunk is a JPEG stream rebuilt from five pieces,
two of which, the strip's header before and after SOF0, were ranges of the
file. They are the same in every chunk of a level, but they sit at the start
of the strip, beyond the readers' 64 KiB merge gap from most intervals, so
reading a chunk cost a second HTTP request just for them. The TIFF profile
had the opposite trade: its JPEG prefix `P` (SOI, the Adobe marker and the
JPEGTables) was a literal in every tile's reference, costing no request but
about 300 bytes of reference payload per tile, stored twice (local header
and central directory).

**The rule (§1.2).** A file input's source table is the `url` source 0, then
its **data sources** ([SPEC.md §6](../../SPEC.md#6-source-table)): the distinct byte strings that the
profile names as shared, numbered from 1 in order of first use (references
in the profile's listed order, ranges in order). A range of a shared string
is the whole data source `(i, 0, len)`. The TIFF profile shares `P`, and the
NDPI profile the header before and after SOF0; both state the order in
which they list their references. Store inputs have no data sources. The
headers are JPEG markers and tables, which §1.2's **Structure only** now
says explicitly are not pixel data. §1.1 compares data sources by their
bytes; HARNESS.md writes one as `{"data": "<base64>"}` in `sources`, and
`compare.py` compares them.

**Results.** Pixels are unchanged: `web/test/ndpi/verify.py` and
`web/test/tiff/verify.py` pass, and every chunk of the NDPI fixture and of
CMU-1.ndpi levels 1–3 reads back byte for byte as before. Both
implementations agree on all 396 synthetic inputs (145 equivalent, 251
rejected) and on the NDPI and SVS corpus (7 equivalent). With the Python
reader through the caching proxy:

| input | read | requests before → after |
|---|---|---|
| CMU-1.ndpi | level 0, 4 × 4 chunks | 32 → 16 |
| CMU-1.ndpi | level 1, all 130 chunks | 247 → 130 |
| CMU-1.ndpi | level 1, 4 × 4 chunks | 32 → 16 |
| CMU-1.ndpi | level 2, all 12 chunks | 20 → 12 |
| `ndpi_levels.ndpi` | level 0, all 4 chunks | 6 → 4 |

CMU-1.ndpi gets four data sources (its levels share the tables before SOF0;
the part after SOF0 differs by restart interval), and its archive is 3 KB
smaller (5.52 MB). CMU-1.svs's archive shrinks from 18.4 MB to 3.4 MB (three data
sources for 24813 tiles), and reading a 4 × 4 tile region fetches 66 KB of
central directory instead of 263 KB. DICOM's 18-byte prefix stays a literal:
a data source would save 16 bytes of payload per frame, too little to matter.

**Not changed.** The readers already resolved `data` sources without I/O
(vzip's Python and browser readers, and the Neuroglancer fork's
`readSource`), so no reader changed. The round implementations in
`impls/virtualize/` predate the rule and now differ on JPEG TIFFs and NDPI.

**Review fix (§9.5).** A dataset that is a level of two recognized N5 groups
whose axis names differ used to keep the first group's names, so the second
image's `ome` axes contradicted its arrays' `dimension_names` (not valid
OME-Zarr 0.5). Now the later group is not recognized and stays plain (new
fixture `n5_shared_levels`).

## Revision 15: a Zarr convention per profile

Revision 15 changes every profile's output, at the root node only; it does
not come from a spec round.

**The problem.** An output said nothing about how it was made: a reader of
a virtualized hierarchy could not tell which profile, in which revision,
produced it, or from which file or store, without the archive's source
table (which a store input spreads over one source per chunk).

**The rule (§2.4).** Each profile is a Zarr convention
([zarr-conventions-spec](https://github.com/zarr-conventions/zarr-conventions-spec))
with a fixed UUID, a version (all 1 now) and a JSON Schema,
`profiles/<p>.schema.json`. The root node of every output (the image group,
ND2's bioformats2raw collection, or a store's root group or array) appends
the profile's Convention Metadata Object to `zarr_conventions` and gets the
property `vzip_virtualized`: `{"profile", "version", "source": {"url"}}`,
with the input URL `U` as given. It holds no pins (store inputs reference
many objects), no time and nothing about the implementation, so outputs
stay equivalent (§1.1). The NIfTI profile's scaling moves from its own
`nifti` member into the property, so §2.2 no longer allows a profile-named
member. A store root that already declares a vzip virtualization
convention, has `vzip_virtualized`, or has a `zarr_conventions` that is not
an array is rejected (new fixtures `zarr2_conventions` and
`zarr2_reject_conventions_*`).

**Versions and tags.** A profile's version increases when a revision changes
its output for some input; a revision that does not leaves it. The
convention URLs name the tag `virtualize-<p>-v<N>` (`spec_url` the profile's
document, `schema_url` its schema). After a revision that changes a
version is merged, run `just tag-conventions` on `main`: it refuses to run
on any other branch, creates the missing `virtualize-<p>-v<N>` tags at HEAD
for the current versions (keeping existing ones), and prints the push
command. Revision 15 creates all nine `virtualize-<p>-v1` tags.

**Results.** Only root documents changed; references and pixels did not. Both
implementations agree on all 401 synthetic inputs (147 equivalent, 254
rejected, `compare.py --fixtures web/test/fixtures`). The new
`tests/test_virtualize_conventions.py` runs every fixture through both
implementations and validates the root of each of the 147 accepted outputs
against its profile's schema (no other node declares a convention). All nine
`web/test/<profile>/verify.py` pass, and ome-zarr-models 0.5 accepts the
OME-Zarr roots with the extra attributes next to `ome`.

## Revision 16: the conventions specify the Zarr layout and the source's metadata

This revision did not come from a spec round.

**The problem.** Revision 15's conventions only recorded where an output
came from. Two goals ask for more. First, a virtualized hierarchy should
lose as little of the source's scientific information as possible: a
reader who needs what the ND2 or DICOM header says should find it in the
hierarchy. Second, making the hierarchy should be repeatable, so the
translation of that information must be specified exactly.

**The split.** Each format's convention, `conventions/<p>/README.md`, now
specifies the whole Zarr view. That is the hierarchy, each array's
metadata, the OME-NGFF metadata, and what each chunk holds in terms of the
source. It also specifies the source metadata: the translation of the
source's header into JSON, under `vzip_virtualized.<p>` on the node it
belongs to. `conventions/README.md` holds what the conventions share,
moved from VIRTUALIZE.md §2.1–§2.4, plus a new §6 on how source values
become JSON. VIRTUALIZE.md §2 now only states the obligation to produce the
convention's layout. The profiles keep how the input is read, which inputs
are rejected, and how each chunk references the input's bytes. Where a
convention says "the input is rejected", a source that fails has no layout
under it, and the profile rejects it.

**Source metadata.**

| format | source metadata |
|---|---|
| TIFF, NDPI | every tag of every IFD and SubIFD, by tag number, with its type, count and value; offsets and byte counts by type and count only. This replaces revision 15's inlined OME-XML, which is tag 270. |
| ND2 | every chunk of the chunk map but the frames: lite-variant chunks decoded, others in base64 (such as the frame times in `CustomData\|AcqTimesCache!`) |
| DICOM | the File Meta Information and the dataset up to Pixel Data in the DICOM JSON Model (PS3.18 §F.2); implicit-VR elements as `UN` |
| NIfTI | every header field, the extensions, and the derived scaling |
| IMS | the attributes of the root group, of each `DataSetInfo` group, and of each channel group |
| N5 | each node's `attributes.json`, whole (revision 15 dropped the dataset members and `n5`) |
| Zarr v2, OME-Zarr | each node's `.zattrs` (for an OME group, without its OME members) |

Every file format bounds its translation with a 64 MiB budget, taken in
document order. The source metadata never rejects an input: what cannot be
read is recorded as absent or `null`, as each convention says. The
conventions stay at version 1: no version had been tagged.

**Results.**
- **Implementations:** both agree on all 398 synthetic inputs (147
  equivalent, 251 rejected) and on the public corpora of every format.
- **Independent readers:** the verifiers now compare the source metadata
  with independent readers:
  - TIFF and NDPI tags with tifffile;
  - ND2 chunks with the `nd2` package;
  - DICOM with pydicom's DICOM JSON Model, which matches exactly for
    explicit VR;
  - NIfTI with nibabel;
  - IMS attributes with h5py.

  All nine report 0 failures.
- **Size:** the root `zarr.json` of CMU-1.svs is 210 KB, most of it the ICC
  profiles of its six IFDs.

## Revision 17: the Sentinel-2 SAFE profile

This revision did not come from a spec round.

**The profile.** VIRTUALIZE.md gains §12, the SAFE profile
([profiles/safe.md](../../profiles/safe.md)), with its convention
([conventions/safe/README.md](../../conventions/safe/README.md)), and
Conformance moves to §13. A Sentinel-2 Level-1C or Level-2A product is read
as a `.SAFE` directory (a store input, chosen by `manifest.safe` at its root)
or as a `.SAFE.zip` file (a file input, chosen by the ZIP local header
signature). It is the first profile that reads a store's objects in ranges,
and the first whose store output has data sources, so §1.4 states both
exceptions. The hierarchy is GeoZarr rather than OME-NGFF: one group per
resolution with the zarr-conventions `proj` and `spatial` members, one array
per band file, one chunk per JPEG 2000 tile.

**Chunks.** Each chunk is a standalone JPEG 2000 codestream: a literal SIZ
that keeps the tile where the file places it on the reference grid, the
file's main header after SIZ (a data source), a literal SOT, and the
tile-part's body, referenced. An edge tile is padded with tiles of empty
packets up to the chunk shape. The count of empty packets was checked
against the PLT markers of every tile of the synthetic band files (and, in
the design, of 7 real ones), with no mismatch.

**Decisions** on the design's open questions: edge tiles padded; Level-2A
declares `multiscales` without `derived_from`, Level-1C none, and the JPEG
2000 resolution levels are not exposed; XML documents are text up to 65536
bytes, within a 65536-byte budget for `vzip_source`'s source metadata,
smallest first, and byte arrays past it; the parsed fields are a fixed set,
labeled a derived convenience; no CF attributes; masks, probabilities and
the preview kept whole; products before December 2016 rejected; `proj:wkt2`
from a fixed template for the 120 WGS 84 / UTM zones, equal to pyproj's
definitions; `proj` and `spatial` on groups only; the Google Cloud mirror's
`_$folder$` markers dropped as layout, its quicklook kept.

**Results.**
- **Implementations:** both agree on all 62 synthetic inputs (9 products
  accepted, 53 rejected, one per rule) and on the 13 public products of
  `corpus_safe.txt` (12 equivalent, the 2015 product in the old format
  rejected by both): 75 inputs, 0 divergences.
- **Independent reader:** `web/test/safe/verify.py` decodes every band
  array of every synthetic product through zarr-python and compares it with
  GDAL's JP2OpenJPEG driver, checks each group's transform, shape and CRS
  against GDAL's, and rebuilds every file of each product from its
  hierarchy, byte for byte. On the public products it does the same for
  the 60 m bands, and compares them with GDAL's SENTINEL2 driver.
- **Cost:** on the public products, a directory-form product takes 857–3126
  range requests and 6–22 MB of reads, a zip 2358–3141; the archives are
  1.0–1.13 MB, the root `zarr.json` 3.2–4.0 KB and the largest document
  (`vzip_source/zarr.json`) at most 64.4 KB. The browser implementation
  takes 4–83 s per directory-form product, the Python one 10–154 s; the
  slowest are the Level-1C products, whose true-color image has 1849 tiles
  of about 17 KB, read one tile-part header after another.

The SAFE profile has not yet been through an independent implementation
round.

## Revision 18: the Zeiss CZI profile

This revision did not come from a spec round.

**The profile.** VIRTUALIZE.md gains §13, the CZI profile
([profiles/czi.md](../../profiles/czi.md)), with its convention
([conventions/czi/README.md](../../conventions/czi/README.md)), and
Conformance moves to §14. A file whose first 16 bytes are `ZISRAWFILE` and
six NULs is read as a CZI file (version 1, single file): its file header,
subblock directory, subblock headers (one coalesced read each), metadata
segment, attachment directory, and a walk over the segment headers. It is
the first profile whose pixels are placed by position rather than by index:
conforming subblocks of one series and pyramid layer that sit on a lattice
form an image level, and every other placed subblock is a tile.

**Decisions** on the design's open questions:
- clipped levels (the last column or row narrower) use the zarr-extensions
  `rectilinear` chunk grid, `{"kind": "inline", "chunk_shapes": [...]}`
  with `[[H, r − 1], H']` for a clipped axis, which zarr-python 3.4 reads
  with `array.rectilinear_chunks` set;
- JPEG XR and Zstd1 hi-lo subblocks are accepted, with
  `{"name": "imagecodecs_jpegxr"}` and `bytes`,
  `{"name": "numcodecs.shuffle", "configuration": {"elementsize": 2}}`,
  `zstd`; their registration and Neuroglancer support are a follow-up;
- tiles are one array per tile position (series, X and Y Start, logical,
  stored and coded size, codec form) holding all its planes, a repeated
  plane starting a further copy, each array recording its position;
- translations in the global pixel frame; no typed per-subblock columns,
  no JSON subset of the metadata XML, embedded CZIs kept as bytes;
  unsupported compressions rejected; the directory as copied typed
  columns; no time step from TimeStamps; omero windows from the display
  settings' `Low` and `High`.

**Results.**
- **Implementations:** both agree on the 71 synthetic files (32 accepted,
  39 rejected, one per rule; the row-band file and the 2^16 + 1 series
  rejection, too large to commit, are written at test time) and on the 24
  public files of `corpus_czi.txt`, all accepted: 95 inputs, 0
  divergences. The browser implementation makes the same requests within a
  few (498 for RecognizedCode-27, 1290 for the ZEN 3.9 slide).
- **Independent readers:** `web/test/czi/verify.py` finds every subblock
  of every synthetic file in the archive exactly once, decodes it through
  zarr-python (rectilinear grids included) and by hand, and compares it
  with czifile; compares each full-resolution level with libCZI's
  composite (pylibCZIrw); and rebuilds every directory entry, subblock
  part, the metadata XML, every attachment, the unreferenced segments and
  the tail from the archive, against the file's bytes. 0 failures.
  pylibCZIrw 6.1.0 on arm64 leaves all but the first 16 bytes of each row
  of a Bgr48 hi-lo subblock at 0 (its C code, czifile and the archive
  agree), and its jxrlib reads neither 24bppRGB nor 32bppBGRA, so those
  composites are not compared.
- **Cost** (Python reference, through the harness proxy): about one range
  request per subblock (498 for RecognizedCode-27's 481 subblocks, 1290 for
  a 1.4 GB ZEN 3.9 slide of 1272), or a few for files of many small
  subblocks (31 for the 3575 subblocks of `xzt-scan-lsm980.czi`); 0.1–7.6
  MB read per file of 1 MB to 2.1 GB. Archives are 29 KB to 1.7 MB, the
  root `zarr.json` 752–873 bytes, and the largest document at most 1.9 KB
  (an image's `zarr.json`); the Axioscan Bgr24 files, whose
  minimal-coverage pyramids are almost all tiles, have about 2150 tile
  arrays of about 0.8 KB each.

The CZI profile has not yet been through an independent implementation
round.

## Revision 19: pins, checksums, and version 0 for the conventions

**What prompted it.** An architecture review (ARCHITECTURE.md §2.4, §2.6,
§4) found that the profiles wrote no pins, so a changed uncompressed
source silently gave wrong pixels; and that every convention still said
version 1 after about thirty breaking changes, with `schema_url` and
`spec_url` naming `virtualize-*` tags that do not exist, so that archives of
revision 14 and of revision 18 could not be told apart. SPEC.md revision 9
added the reader side: range checksums, and readers that check them.

**The changes.**
- **§1.2, §1.4: pins.** Source 0 of a file input pins the file's size, and
  its ETag when every response for the file (to `HEAD` and to range
  requests) had the same strong one; a response without one, or a weak one,
  means no `etag` pin, and two different strong ones mean the file changed
  while it was read, a failure. A virtualizer that read a local file served
  at `U` pins no ETag. A store input's url sources, and the SAFE directory
  form's, pin their objects' listed sizes.
- **§1.2: checksums.** A virtualizer may, when asked to, record the
  CRC-32C of every range of a url source (SPEC.md §5.2). It reads every
  referenced byte, so it is off by default, and it is the one exception to
  "Structure only".
- **§1.1:** pins and checksums are not part of the compared output.
- **Conventions §1, §2: version 0.** Until vzip's first release every
  convention is version 0, the root property records the revision of this
  document (`"revision": 19`), and the CMO's URLs name the `main` branch.
  At the release each convention becomes version 1, tagged
  `virtualize-<p>-v1`, and from then on each breaking change increments its
  version and is tagged; the schemas reject other versions.

**Implementations.** Both implementations write the pins and, with
`--checksums`, the checksums; `tests/test_virtualize_pins.py` and
`web/test/pins.test.ts` check them. The conventions' schemas were
regenerated. Every synthetic input gives equivalent outputs in both, as
before.

## Revision 20: integers compare exactly

**What prompted it.** §1.1 compared every number as its binary64 value,
while §1.6 copies a number written as an integer with every digit, and
`compare.py` compared two integers exactly but an integer and a float by
binary64. So `9007199254740993` and `9007199254740993.0` (binary64
9007199254740992) were equivalent, and a virtualizer that lost the digit
§1.6 requires it to keep would conform.

**The changes.**
- **§1.1:** a number written as an integer (no fraction, no exponent)
  compares as its exact value, any other number as its binary64 value, and
  two numbers are equal when their values are mathematically equal. An
  integer equals a binary64 value only if that value is exactly the
  integer, not if the integer merely rounds to it. A writer must write a
  binary64 integer beyond 2^53 − 1 with its exact digits or with a fraction
  or an exponent.
- **§1.6** points to §1.1 for the comparison.
- **Conventions:** the root property records `"revision": 20`.

**Implementations.** `same()` (used by `compare.py`) compares an integer
and a float by their exact values. The TypeScript virtualizers wrote such
binary64 values with `JSON.stringify`, which pads them with zeros (the
`zarr2` fill value -2^64 became `-18446744073709552000`); they now write
the exact digits (`stringifyJson`). The Python ones write floats with an
exponent or a fraction already. The conventions' schemas were regenerated.

## Revision 21: the IR mirror is the source metadata of TIFF, ND2 and CZI

**What prompted it.** vzip now virtualizes TIFF, ND2 and CZI through one
Rust core that parses each file into an IR (intermediate representation)
whose leaves partition the file, and writes that IR under `vzip_source` as a
format-free table (the **mirror**) from which the file is rebuilt byte for
byte. The conventions still described the per-format source metadata that
the previous implementations wrote, which the archives no longer held.

**The changes.**
- **conventions/README.md §8 (new): the IR mirror.** Elements, kinds, names,
  extents and runs; the invariants (coverage, injectivity, parents, aliases,
  derived spaces); the table (`vzip_source/ir/rows` [8, m],
  `vzip_source/ir/tables` [k, 7], `vzip_source/ir/shared`, and the
  description `{"ir": {...}}` that is `vzip_source`'s source metadata); column
  runs and their four encodings (arithmetic, stored differences, entries of
  an unsigned array the source holds, an index equal to the start); the
  record budget (`2^22 + size / 4` expanded rows); the checks and the
  byte-exact rebuild from source 0; forms and recipes; the type grammar; the
  view under `vzip_source/tree`, each of whose groups declares the
  convention with its view document as source metadata.
- **TIFF, ND2 and CZI conventions §5:** the root's source metadata is
  unchanged (ND2's root keeps the decoded chunks of at most 16384 bytes of
  JSON, within 65536 bytes, as before); `vzip_source` is the mirror, and each
  convention lists its **source model**, the elements of the format's IR.
  Nothing is left out: layout and dead space are elements (gaps), so the old
  "not kept" lists are gone. `vzip_source` declares the convention.
- **§1.1:** for these three profiles, outputs are equivalent when their
  entries outside `vzip_source/` are, and each mirror is valid and rebuilds
  the same source; a mirror's table may be folded and encoded in more than
  one valid way, so its entries are not compared otherwise.
- **Conventions:** the root property records `"revision": 21`; the
  schemas were regenerated (the TIFF, ND2 and CZI ones describe the mirror).

**Implementations.** `python -m vzip.virtualize` and the browser code write
the mirror (rust/vzip-ir). The frozen reference implementations in
`conformance/virtualize/reference` and `web/conformance/reference` still
write the previous source metadata (under the current revision number); `compare.py` compares them with the IR
path outside `vzip_source`, as §1.1 now says.

## Revision 22: the IR mirror is canonical

**What prompted it.** Revision 21 let a producer fold and encode the mirror's
table in more than one valid way, so mirrors could only be compared by what
they rebuild, not entry for entry like the rest of an output, and two
producers could write different archives for one source.

**The changes.**
- **conventions/README.md §8.8 (new): canonical form.** How a producer
  derives the one table from the IR: every run expanded (no row of code 0);
  the elements in canonical order (depth first; siblings by name group, the
  groups in order of their first byte, then by name index, none last, then
  by first byte); the derived
  spaces kept in that order within 2^20 rows; names and types sorted by
  bytes; shared sources numbered by first use and forms sorted; column runs
  found outermost first, left to right, at the period (1 to 4) that folds
  the most siblings, the smaller on a tie; each column at the first encoding
  that holds it (none when constant, then 3, 0, 2, 1), encoding 2 only for
  the tiles or strips of a TIFF IFD with at least 16 members; the rows and
  tables in a fixed order; chunks uncompressed (codec `bytes` alone). A
  reader still accepts any valid table (runs, `zlib`); a validator flags one
  that is not canonical, and §8.8 says why.
- **§8.7, the view:** from the canonical order; sizes as `JSON.stringify`
  writes them; a value is shown when it is a number, a record, a GUID or XML
  of at most 1024 bytes (not top-level text or bytes, which parsers need not
  read; not an ND2 frame's timestamp), so the view no longer depends on what
  a parser happened to read; the budgets as an exact procedure; no
  `"$vz": "run"` documents (runs are expanded); a derived element's
  document has `"$vz": "derived"` and `"$form"`, and a cut document
  `"$partial": true` (no longer `"$vz": "partial"`, which a derived
  document's `$vz` could hide).
- **§1.1:** mirrors are compared entry for entry, like the rest of the
  output; the validity and rebuild checks stay, as additional checks.
- **TIFF, ND2 and CZI §5.4** say so; the TIFF example shows the sorted
  strings and the view's spilled text values.
- **Conventions:** the root property records `"revision": 22`; the schemas
  were regenerated (the view's vocabulary).

**Implementations.** The Rust core writes the canonical mirror and checks it
(`canonical_problem`); `compare.py` flags a non-canonical mirror. The TIFF
parser holds every numeric value of at most 1024 bytes, which the view
shows. The frozen reference implementations record revision 20, the one they
implement, through their own constant; `compare.py` compares their roots as
if they recorded the current revision (HARNESS.md).

## Revision 23: text in the view, aliases of identical siblings, named array rules, the mirror's budget

**What prompted it.** The user's answers to revision 22's open questions:
the view should show short text, the canonical form should settle aliases of
identical siblings, encoding 2 should be a per-format rule rather than a
TIFF special case, the mirror's row budget should be a specified rejection,
and validators should check the view as well as the table.

**The changes.**
- **conventions/README.md §8.7, the view:** text values (`ascii`, `cstr`,
  `utf16`; not `bytes`) of at most 1024 bytes are shown, by §6's rule: up to
  the first NUL, a string when UTF-8, else `{"latin1": ...}`; `utf16` a
  string when UTF-16, else `{"$vz": "utf16", ...}`. A producer reads every
  shown value. So a TIFF's byte order, ImageDescription and Software, and a
  CZI's metadata XML and subblock metadata of at most 1024 bytes, are shown.
- **§8.8, identical siblings:** an alias whose target lies in a sibling
  identical in every field to an earlier one names the earliest.
- **§8.8, named array rules:** encoding 2 applies only under a profile's
  named rule, listed in a table (TIFF: layout tables; ND2 and CZI: none),
  and the section says how a format adds one.
- **§8.3 and VIRTUALIZE.md §1.1, the budget:** a source whose canonical
  mirror would have more than `2^22 + floor(size / 4)` rows is rejected with
  `budget: the mirror would have more than N rows`.
- **§8.8, validators** also rebuild the view from the table and the source
  (re-deriving LV and XML spaces) and compare it member by member.
- **Conventions:** the root property records `"revision": 23`; the TIFF
  example shows its text values; the schemas were regenerated.

**Implementations.** The Rust core: a run reads, after its parser, the shown
values the parser did not; `canonical` retargets aliases; `table` takes the
profile's array rules; `mirror` rejects past the budget before expanding;
`view_problem` (also in Python) is the validator's view check, which
`compare.py` runs on every archive. The frozen reference still records 20.

**Amended within revision 23** (no new revision: no output changed). Three
clarifications and one rule that changes stored output only for inputs that
no fixture, probe or corpus file has:
- §8.7: a shown text value with bytes after its NUL that are not all zero
  is `{"text": T, "after": {"$vz": "bytes", "b64": A}}`, as a record's `cstr`
  field is, so the view is lossless. Every fixture, probe and corpus archive
  is byte-identical with and without the change: none has such a value
  among the values the view shows (the CZI attachment names that do are
  record fields, which already had this form). A producer that wrote the
  old form for such an input wrote what revision 23 left unspecified.
- §8.8: a derived transform may hold shown values only if validators can
  derive it again (`nd2-lv-zlib` and `xml-variant` can; `gzip` holds none).
- The planner's statistics count the batch that reads the shown values a
  parser did not keep (`fill`); it is empty on every fixture, probe and
  corpus file, and the tests require it.
