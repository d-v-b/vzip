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
