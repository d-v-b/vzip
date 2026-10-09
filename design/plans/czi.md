# Implementation plan: the CZI profile

This plan implements [README.md](../../spec/virtualize/czi.md) (the convention) and
[spec/virtualize/czi.md](../../spec/virtualize/czi.md#part-2-the-profile) (the profile) in both
implementations, with fixtures, a verifier and a public corpus. It is a
design note, not normative.

## 1. Research summary

Sources read: ZEISS libCZI (`Src/libCZI/CziStructs.h`, `CziParse.cpp`,
`CziSubBlockDirectory.cpp`, `decoder_zstd.cpp`, `utilities.cpp`,
`CziMetadataDocumentInfo.cpp`, `libCZI_Pixels.h`, `docs/`), czifile
2026.8.16 (Gohlke, BSD-3), pylibCZIrw 6.1.0, jxrlib's pixel-format GUIDs,
the zarr-extensions registry, the Neuroglancer fork's codec list
(`d-v-b/neuroglancer@vzip`), and 30 public files surveyed over HTTP range
requests with `scratchpad/czi-design/survey.py` and `classify.py`.

Findings that shaped the design:

- Every surveyed file is Major 1.0, single file, all-`DV` directory, with
  the subblock's copy of its entry identical to the directory's (FilePosition
  included). The segment walk reaches the end of every file checked;
  `DELETED` segments are common (3 of 5 small files walked).
- Compression in the corpus: uncompressed (confocal, LSM, artificial test
  files), JpgXr (every Axioscan slide; Gray16 with `16bppGray`, Bgr24 with
  `24bppBGR`), Zstd0/Zstd1 (ZEN 3.x; pylibCZIrw writes both). Zstd1 with
  hi-lo packing is exactly numcodecs' byte shuffle of element size 2 after
  zstd: checked by decoding a pylibCZIrw file with `numcodecs.Zstd` +
  `numcodecs.Shuffle(2)` and comparing with czifile.
- Layer-0 tile scans overlap (stage positions with ~10 % overlap): tiles.
  ZEN's pyramid layers lie on a lattice with the last column or row narrower
  (Axioscan `RecognizedCode-27`: 5 pyramid levels, all clipped) unless the
  scene uses "minimal coverage" pyramids (`TEST 2023_10_10__1046`), whose
  tiles are clipped around a polygon and stay tiles.
- Attachments seen: EventList, TimeStamps, LookupTables, Thumbnail (JPG),
  Label and SlidePreview (embedded CZI), Profile (Zip-Comp), SLM (PNG).
  TimeStamps' and EventList's first `i32` is not always the data size (12
  for a 16-byte TimeStamps), so it is kept. Event sizes are always
  `20 + description size`.
- Subblock metadata is 40–600 bytes of XML per subblock (stage X/Y, focus,
  acquisition time, `OriginalBounds`); ZEN 3.9 subblocks carry 62 KB
  `CHUNKCONTAINER` attachments (valid-pixel masks) of equal size.
- pylibCZIrw installs with `uv` on Python 3.12 (wheels; the project's
  version), not on 3.14; it writes Gray8/16/32Float and Bgr24/48 tiles,
  uncompressed, Zstd0 and Zstd1 (with hi-lo), at any position and plane, but
  no pyramids, JPEG, JPEG XR, attachments or deleted segments. czifile reads
  everything above, including JPEG XR (imagecodecs) and zstd1.

## 2. Modules

Python, `python/src/vzip/virtualize/czi/`, and its TypeScript twin,
`js/src/virtualize/czi/`, with the same module names and functions, as
for ND2:

| module | functions | convention / profile |
|---|---|---|
| `__init__.py` / `index` entry | `is_czi(head)`, `virtualize_czi(read, size, url) -> Output` | profile §13.1 |
| `segments.py` / `segments.ts` | `read_file_header`, `read_directory` (entries as a struct-of-arrays: one `Int32Array` per field and per dimension letter, so memory is linear and typed), `read_subblock_header(i)`, `read_metadata_segment`, `read_attachment_directory`, `read_attachment`, `walk(size, known_headers)` | conv. §2, profile §13.2 |
| `coding.py` / `coding.ts` | `PIXEL_TYPES`, `codec_chain(pixel_type, compression, hilo)`, `zstd1_header(D)`, `zstd_content_size(D)`, `jpeg_frame(D)`, `jpegxr_size(D, pixel_type)`, `coded_size(subblock, read)` | conv. §3.1–3.2, profile §13.4 |
| `xml.py` / `xml.ts` | `elements(text)` (nesting over the tag scan), `path(root, "A/B/C[k]")`, `text(el)`, `read_xml_values(bytes) -> XmlValues` (PX, PY, PZ, INC, BITS, channels, scenes) | conv. §2.7 |
| `layout.py` / `layout.ts` | `layer(Wl, Hl, W, H)`, `series_key(entry)`, `classify(B) -> Level \| None`, `image_axes(levels)`, `level_array(level, axes, values)`, `chunk_refs(level)`, `tile_array(i)`, `row_band(H, H2, W, q)` | conv. §3.3–4.4, profile §13.5 |
| `source.py` / `source.ts` | `directory_plans`, `subblock_families`, `metadata_plans`, `attachment_plans` (forms, budget), `segment_plans`, `tail_plan` | conv. §5 |
| `virtualize.py` / `virtualize.ts` | orchestration: reads, rejections, then documents and references | all |

Shared changes:

- **Tag scan.** Move `scan` and `_decode` from `tiff/virtualize.py` (and the
  TS twin) to a shared `xmlscan` module used by TIFF and CZI; no change to
  TIFF output.
- `common.PROFILES` (both): `"czi": ("7a0733c7-d4be-4482-a64f-6904d9354ea5",
  1, "CZI")`; `virtualize()` dispatch on the `ZISRAWFILE` magic;
  `NOT_SUPPORTED` text.
- `common.metadata_array`: accept a `chunk_grid` override (rectilinear) and a
  complex fill value; add a `rectilinear(shape, edges)` helper.
- `Output.write`: treat `tiles/*/zarr.json` like `vzip_source/` documents
  (lazy), so opening an archive does not read one document per tile array.
- `python/src/vzip/codecs.py`: register `imagecodecs_jpegxr` (decode with
  `imagecodecs.jpegxr_decode`, reshape to the chunk) like
  `imagecodecs_jpeg2k`. `numcodecs.shuffle` and `zstd` are zarr-python's.
  Readers with rectilinear levels need
  `zarr.config.set({"array.rectilinear_chunks": True})` (zarr-python 3.4);
  the verifier and `vzip.store` helpers set it.
- `spec/virtualize.md`: the detection row (profile §13.1), the profile table
  row, §13 = CZI and §14 = Conformance (update the `#13-conformance`
  anchors), revision 18 in `REVISIONS.md`.
- `spec/conventions.md`: the table row and the title list in §1
  (`CZI`); `spec/generate_schemas.py`: `SOURCE_METADATA["czi"]`
  (root header, image `dimensions`, tile record, `vzip_source`
  attachments, the attachment arrays' `size`), then regenerate
  `spec/virtualize/czi/schema.json`.

Order of work:

1. `segments` + `source` + `virtualize` with every placed subblock as a
   tile (no images): exercises reading, rejection, the walk and all of §5.
2. `coding`: coded sizes and unplaced subblocks.
3. `layout`: series, layers, regular levels, images, rectilinear grids,
   row bands.
4. `xml`: scales, names, colors, windows.
5. The TypeScript twin, module by module, compared by
   `conformance/virtualize/compare.py` after each.
6. Corpus run, verifier, schema, docs.

## 3. Tests

`python/tests/reference/test_virtualize_czi.py`, following the house rule (one test for the
accepted combinations, one per error):

- `test_virtualize_czi_outputs`: parametrized over the accepted fixtures,
  asserting each output's documents and references against expectations
  (axes, shapes, chunk grids, codecs, tiles, unplaced lists, families,
  attachment forms) and that both implementations agree.
- One test per rejection: `bad_magic`, `major_2`, `file_part`,
  `entry_file_part`, `attachment_file_part`, `no_directory`,
  `directory_id`, `entry_count_negative`, `entry_count_over_limit`,
  `directory_overrun`, `de_schema`, `unknown_schema`, `dimension_letter`,
  `dimension_padding`, `duplicate_dimension`, `missing_x`, `zero_size`,
  `plane_size_not_1`, `subblock_id`, `subblock_outside_file`,
  `subblock_copy_schema`, `subblock_copy_count`, `negative_sizes`,
  `pixel_type_unknown`, `compression_lzw`, `compression_chunked`,
  `compression_raw_camera`, `jpeg_gray16`, `metadata_id`,
  `metadata_outside_file`, `attdir_id`, `attdir_count_over_limit`,
  `attachment_id`, `too_many_series`.
- Unit tests of `layer` (table edges), `classify` (each failing condition
  of conv. §3.5 gives tiles), `row_band`, the codec header parsers (each
  failure gives no coded size), and the GUID text form.
- `python/tests/test_virtualize_conventions.py` picks up the new fixtures and
  schema automatically once the profile is registered.

## 4. Fixtures

`fixtures/generators/czi/write_fixtures.py` writes `fixtures/czi/<name>.czi`
and `<name>.npz` (one array per expected chunk key, as for ND2) with a small
**struct-based writer** (`segment(id, data, allocated)`, `dv_entry(...)`,
`subblock(entry, metadata, data, attachment)`, `attachment(...)`,
`write(segments, header)`), because no writer covers pyramids, JPEG XR,
attachments, deleted segments or broken files. Pixels are random
(seeded); JPEG via `imagecodecs.jpeg8_encode`, JPEG XR via
`imagecodecs.jpegxr_encode` (checking the container's PixelFormat byte, and
patching it to `24bppBGR` for one Bgr24 case), zstd via `imagecodecs`, hi-lo
via `numcodecs.Shuffle(2)`. Every accepted fixture is checked once, at
generation, to open in czifile, and (when Python 3.12 has pylibCZIrw) in
libCZI.

Accepted:

| fixture | covers |
|---|---|
| `czi_gray8_single` | one subblock, minimal XML with scaling and a channel name |
| `czi_gray16_tczyx` | T 2, C 3, Z 4, one tile per plane; subblock metadata; TimeStamps, EventList (2 events), JPG thumbnail |
| `czi_bgr24_raw`, `czi_bgra32_raw`, `czi_bgr48_raw` | samples, transpose, B/G/R(/A) labels |
| `czi_types` | Gray32Float, Gray32, Gray64Float, Gray64ComplexFloat, Bgr96Float as separate scenes (complex fill value) |
| `czi_zstd0_gray8`, `czi_zstd1_plain_gray16`, `czi_zstd1_hilo_gray16`, `czi_zstd1_hilo_bgr48` | zstd forms |
| `czi_jpeg_bgr24_grid` | 2 × 2 JPEG grid, R/G/B order |
| `czi_jxr_pyramid` | Gray16 JPEG XR: overlapping layer 0 (tiles), layers 1–3 regular with a clipped last column and row (rectilinear) |
| `czi_regular_holes` | aligned grid with missing cells |
| `czi_clipped_raw` | uncompressed clipped level |
| `czi_mosaic_overlap` | overlapping M tiles in two channels: tiles with dimension records |
| `czi_multiscene` | S 0–2 with names; an H 0/1 split; B, I, R, V present |
| `czi_mixed_types` | two channels of different pixel types: tiles |
| `czi_subsampled` | layer-0 stored at half size (Airyscan-like): dataset 0 has factor 2 |
| `czi_line_scan` | X 512 × Y 1 per T (as LSM 980 xt scans) |
| `czi_deleted` | `DELETED` segments, an orphan subblock, an unknown id, a tail of garbage |
| `czi_entry_mismatch` | a subblock whose copy differs: unplaced, `subblocks/entry` |
| `czi_pyramid_edge_bug` | a JPEG pyramid tile coded one row short: a non-conforming tile |
| `czi_unplaced` | zstd content size wrong, short uncompressed data, JPEG XR with a wrong PixelFormat, bad Zstd1 header, hi-lo on Gray8 |
| `czi_trailing` | uncompressed data longer than the pixels |
| `czi_subblock_attachments` | `CHUNKCONTAINER` masks of equal length ([1, L] family) and of unequal length |
| `czi_attachments` | FocusPositions, LUT, embedded CZI, Zip-Comp, non-`A1` entry, a segment copy that differs, an empty attachment, a TimeStamps of the wrong size (bytes form) |
| `czi_metadata_attachment` | metadata segment with a binary part; XML with BOM; XML not UTF-8 (no values read) |
| `czi_many_attachments` | enough entries to pass `vzip_source`'s budget (`attachments/index`) |
| `czi_empty_directory` | `N = 0` |

The row-band rule needs a tile over 16 MiB; that fixture
(`czi_big_plane`, 4100 × 2050 Gray16) is generated at test time into a
temporary directory, not committed.

Rejected: one fixture per rejection test of §3, named `czi_reject_<case>`.

## 5. Verifier

`js/test/czi/verify.py` serves the fixtures over local HTTP, runs the
browser virtualizer under Node, opens every archive with `VZipStore` and
zarr-python (with `vzip.codecs` and rectilinear enabled), and checks:

1. **Pixels, per subblock**, against **czifile** (independent, pure Python,
   BSD-3): for every subblock, `CziSubBlockSegmentData.data()` (stored
   resolution) equals the subblock's region of its level array, or its tile
   array, with the sample order reversed for uncompressed and zstd data
   (czifile always returns RGB) and Bgra32's alpha compared as stored
   (czifile forces 255; compare `data(raw=True)` for that type).
2. **Stitching**, against czifile's `CziImage` of the scene and level:
   the level array equals czifile's composite over the level's bounding
   box (fill 0) for regular levels, which have no overlap to blend; and
   against **pylibCZIrw** (libCZI) `read(roi, plane, zoom)` for layer 0
   where installed (Python 3.12).
3. **Reconstruction** (criterion 1): a rebuild script reads only the archive
   and reconstructs every directory entry (columns), every subblock's
   metadata, data (from chunks, tiles or `subblocks/data`) and attachment,
   the XML, every attachment's data (decoding forms back to bytes), every
   unreferenced segment and the tail, and compares each with czifile's
   parse of the file (`czi.subblocks()`, `metadata_segment`,
   `attachments()`, `segments(...)`) and the raw file bytes.
4. Every `czi_reject_*` fixture is rejected by both implementations.

`experiments/verify_czi_vzip.py` runs 1–3 on the public corpus (pixels on a
sample of subblocks per file, to bound downloads).

## 6. Corpus

`conformance/virtualize/corpus_czi.txt`, `url|name`. Every URL below was
checked on 2026-10-08 to answer a `Range: bytes=0-15` request with `206` and
the `ZISRAWFILE` magic. Licenses: CC-BY-4.0 unless noted.

```
# url|name of the public CZI files in the corpus
https://downloads.openmicroscopy.org/images/Zeiss-CZI/idr0011/Plate1-Blue-A_TS-Stinger/Plate1-Blue-A-02-Scene-1-P2-E1-01.czi|idr0011_A02_S1
https://downloads.openmicroscopy.org/images/Zeiss-CZI/idr0011/Plate1-Blue-A_TS-Stinger/Plate1-Blue-A-03-Scene-1-P1-D1-01.czi|idr0011_A03_S1
https://downloads.openmicroscopy.org/images/Zeiss-CZI/zenodo-10577186/2023_11_30__RecognizedCode-27.czi|axioscan_recognizedcode27
https://downloads.openmicroscopy.org/images/Zeiss-CZI/zenodo-10577186/2023_11_30__RecognizedCode-27-Background%20subtraction-08.czi|axioscan_background_subtraction
https://downloads.openmicroscopy.org/images/Zeiss-CZI/zenodo-10577186/2023_11_30__RecognizedCode-27-Deconvolution%20%28defaults%29-11.czi|axioscan_deconvolution
https://downloads.openmicroscopy.org/images/Zeiss-CZI/zenodo-14968770/2025_01_27__0007_offline_Zen_3_9_5.czi|zen395_offline
https://zenodo.org/api/records/19047136/files/xt-scan-lsm980.czi/content|lsm980_xt
https://zenodo.org/api/records/19047136/files/xz-scan-lsm980.czi/content|lsm980_xz
https://zenodo.org/api/records/19047136/files/xzt-scan-lsm980.czi/content|lsm980_xzt
https://zenodo.org/api/records/19047136/files/Image_1_2023_08_18__14_32_31_964.czi/content|image1_all_dims
https://zenodo.org/api/records/19047136/files/test_gray.czi/content|test_gray_tiles
https://zenodo.org/api/records/19047136/files/ZeissLLS7Demo-Max.czi/content|lls7_demo_max
https://zenodo.org/api/records/7015307/files/S%3D2_2x2_Z%3D4_CH%3D1.czi/content|testcam_S2_2x2_Z4
https://zenodo.org/api/records/7015307/files/T%3D3_Z%3D5_CH%3D2.czi/content|testcam_T3_Z5_C2
https://zenodo.org/api/records/7015307/files/S%3D3_1Pos_2Mosaic_T%3D2%3DZ%3D3_CH%3D2.czi/content|testcam_S3_mosaic
https://zenodo.org/api/records/7015307/files/W96_B2%2BB4_S%3D2_T%3D1%3DZ%3D1_C%3D1_Tile%3D5x9.czi/content|testcam_W96_tiles
https://zenodo.org/api/records/7015307/files/S%3D2_3x3_T%3D3_CH%3D2.czi/content|testcam_S2_3x3_T3
https://zenodo.org/api/records/7015307/files/S%3D1_3x3_T%3D3_Z%3D4_CH%3D2.czi/content|testcam_S1_3x3_T3_Z4
https://zenodo.org/api/records/10708864/files/ztacktimeposition16bit.czi/content|bioformats_ztackt
https://zenodo.org/api/records/8321543/files/3Dexample.czi/content|zenblack_3d
https://zenodo.org/api/records/10724935/files/TEST%202023_10_10__1046.czi/content|axioscan_bgr24_test
https://zenodo.org/api/records/10724935/files/TEST%202023_10_10__1046_merged.czi/content|axioscan_bgr24_merged
https://zenodo.org/api/records/8358320/files/Import%20issue.czi/content|import_issue
https://zenodo.org/api/records/5823010/files/mouse_01_05.czi/content|inflammasome_mouse_01_05
```

Sources: OME sample images (idr0011 subset, (c) Ledesma-Fernández et al.;
zenodo-10577186, (c) Stephan Wagner-Conrad; zenodo-14968770, (c) Jürgen
Bohl), Zenodo records 19047136 ("CZI file examples"), 7015307 (artificial
test-camera CZIs, 25 files), 10708864 (Bio-Formats samples), 8321543 (ZEN
black), 10724935 (Axioscan Bgr24 JPEG XR), 8358320, and 5823010 (CC0).
Sizes run from 1 MB to 2.1 GB; the corpus has uncompressed Gray8/Gray16,
Bgr24 and Gray16 JPEG XR, layer-0 overlapping mosaics, regular and
clipped pyramids, minimal-coverage pyramids, line scans, every dimension
letter, `DELETED` segments, embedded CZIs and LUTs. It lacks Zstd (ZEN 3.x
light-microscopy files; pylibCZIrw-written fixtures cover it) and JPEG
files: finding a public Zstd CZI (for example in the BioImage Archive) is
a follow-up. Larger candidates not listed: Zenodo 19047136's
`MouseBrain_*` (0.5–3 GB, light sheet with I and V) and `ZeissLLS7Demo`
(15 GB), 10708864's `40_Dual.czi` (0.7 GB), and the H&E/Trichrome slide
records (2–6 GB each, 10234790 and siblings).

Expected classification of the corpus, from `classify.py` under the
convention's rule: the LSM, idr0011, ZEN-black, Bio-Formats and single-image
files are one level per series; the test-camera mosaics give tiles at layer
0 and a regular level at layer 1; RecognizedCode-27 gives 264 tiles and 5
clipped levels; the Bgr24 Axioscan test files give tiles almost everywhere.

## 7. Decisions on the open questions

1. **Clipped edges:** a lattice whose last column or row is narrower is a
   level, with the zarr-extensions `rectilinear` chunk grid in its exact
   inline form (convention §4.3). zarr-python 3.4 reads it with
   `array.rectilinear_chunks` set; the verifier reads clipped levels that
   way and also chunk by chunk. Strict equality would have left every ZEN
   slide pyramid as tiles (RecognizedCode-27: 481 arrays, no image).
2. **JPEG XR and hi-lo packing:** accepted, with `imagecodecs_jpegxr` (no
   configuration) and `bytes`, `numcodecs.shuffle`
   (`{"elementsize": 2}`), `zstd` (convention §3.1). Follow-up: register
   both in zarr-extensions (or move hi-lo to a registry `shuffle`), and add
   a jxrlib WASM decoder and the shuffle to the Neuroglancer fork.
3. **Irregular tiles:** one array per tile position (series, X and Y Start,
   logical, stored and coded size, codec form), holding all its planes;
   repeated planes at one position go to further copies; equal positions
   with different sizes or codec forms are different arrays; each array
   records its position (convention §4.4).
4. **Translations** are in the global pixel frame, scaled by the pixel size.
5. **Typed per-subblock columns** (stage X/Y, focus, time) are not in
   version 1; `subblocks/metadata` keeps them.
6. **No JSON subset** of the metadata XML: it is kept as referenced bytes.
7. **Embedded Label and SlidePreview CZIs** are attachment bytes, not
   virtualized recursively.
8. **Unsupported compressions** (LZW, lossless JPEG, chunked, camera and
   system raw) reject the file, as in the other profiles.
9. **The directory** stays copied typed columns.
10. **Time scale:** none is derived from TimeStamps when
    `Interval/Increment` is absent.
11. **omero windows** use the display settings' `Low` and `High`, scaled by
    the pixel type's range, when present.
12. **Section numbering:** SAFE took spec/virtualize.md §12; CZI is §13,
    Conformance §14, and the CZI profile is revision 18.
