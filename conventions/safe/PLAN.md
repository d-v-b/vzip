# SAFE: implementation plan

The plan for implementing [the SAFE convention](README.md) and
[profile](../../profiles/safe.md). The design is settled in those two
documents; this one says how to build, test and verify it, what the
research behind the design found, and what is still open.

## 1. Findings that shaped the design

All of these were checked against real products in October 2026, with the
scripts kept in the scratch directory of the design session (`jp2parse.py`,
`tiles.py`, `pltcheck.py`, `zipcd.py`, `corpus_check.py`). §6 repeats them as
tasks.

- **Tiling.** Every band file examined is written by Kakadu v7.4, and has
  the same structure:
  - one tile-part per tile (`TPsot = 0`, `TNsot = 1`), in raster order,
    each with a PLT marker;
  - no TLM, PLM or PPM in the main header, and no SOP or EPH markers;
  - `Scod = 1`, 1 layer, LRCP, 4 decomposition levels, reversible 5/3;
  - main header: SOC, SIZ, COD, QCD and two COMs (`Kakadu-v7.4` and
    `Kdu-Layer-Info`), 193 bytes, or 199 for 3 components.

  This holds for processing baselines 02.07, 02.12, 03.01, 05.00, 05.09,
  05.10 and 05.11, for L1C and L2A, and on the GCS mirror and in ESA's zips.
  The tile sizes vary, so they are read from SIZ and never assumed:

  | file | size | tile | tiles |
  |---|---|---|---|
  | 10 m bands (L1C, L2A), AOT, WVP | 10980 | 1024 | 11 × 11 |
  | 20 m bands, AOT, WVP, SCL | 5490 | 640 | 9 × 9 |
  | 60 m bands, SCL | 1830 | 192 | 10 × 10 |
  | L1C TCI (10 m) | 10980 | 256 | 43 × 43 |
  | L2A TCI 10 m / 20 m / 60 m | 10980 / 5490 / 1830 | 1024 / 2048 / 256 | 11² / 3² / 8² |
  | QI masks (`MSK_*.jp2`, PB ≥ 04.00) | 1830 | 256 | 8 × 8 |

  Reflectance bands, AOT and WVP are 15-bit unsigned. TCI, SCL and the
  masks are 8-bit. TCI and `MSK_CLASSI` have 3 components and `MSK_QUALIT`
  has 8.
- **Standalone tiles.** The design keeps a tile at its place on the
  reference grid (image origin `(x0, y0)`, tile origin `(x0, y0)`) instead
  of moving it to the origin. The wavelet decomposition, precincts and
  code-blocks are then the file's by construction, whatever the alignment.
  It was verified by decoding every tile of 9 real band files and 2
  synthetic ones with imagecodecs (OpenJPEG 2.5.4), with no mismatch
  against the matching window of the whole file decoded by rasterio's
  JP2OpenJPEG driver. The real files were B01 60 m, B02 10 m (L2A, 121
  tiles), SCL 20 m, MSK_CLASSI, MSK_QUALIT (8 components) and MSK_DETFOO,
  plus one band file taken by range from inside an ESA zip. Every edge tile
  of these is included. The synthetic files had 32 × 32 tiles on a 110 × 110
  image, and 40 × 40 RGB tiles on 101 × 101, whose tile origins are not
  multiples of `2^N`.
- **Edge tiles.** An edge tile is smaller than the chunk. vzip's
  `imagecodecs_jpeg2k` codec (`src/vzip/codecs.py`) reshapes the decoded
  image to the chunk shape, and conventions §3 says a codestream decodes to
  the chunk's shape. So the chunk codestream fills the chunk with empty
  tiles. OpenJPEG accepts a wrong count of empty packets without
  complaint, so decoding cannot validate the count. Instead,
  `pltcheck.py` compared the formula of profile §12.6 with the number of
  packet lengths in the PLT markers of every tile of 7 files: there were 0
  mismatches, edge tiles included.
- **Listing.** `storage.googleapis.com` answers S3 `ListObjectsV2`
  (`?list-type=2&prefix=…`) anonymously for the bucket
  `gcp-public-data-sentinel-2`, path-style. vzip's existing
  `store.py` lists a product unchanged: 69 objects in 1 request for an L1C.
  Objects serve range requests (206, `accept-ranges: bytes`). The mirror
  differs from ESA's product:
  - it adds a quicklook `<product>-ql.jpg` at the root;
  - it adds Hadoop-style folder markers `<dir>_$folder$` (6 bytes,
    `folder`), whose keys do not end in `/`, so they are other objects;
  - it lacks `HTML/` for most products before about 2025.
- **Other mirrors.**
  - AWS `sentinel-s2-l1c` / `sentinel-s2-l2a` answer `ListObjectsV2`, but
    their layout is the per-tile layout (`tiles/32/T/NS/2024/1/3/0/B01.jp2`,
    `metadata.xml`), not SAFE, and the buckets are documented as requester
    pays.
  - The Copernicus Data Space Ecosystem's `eodata` S3 needs credentials (403
    anonymously).
  - The Microsoft Planetary Computer serves SAFE directories from Azure Blob
    Storage. Its listing is not S3 (`?restype=container&comp=list`, 409
    without a SAS token), and a SAS token is a query string, which §1.4
    rejects.
  - So the directory form's public corpus is the GCS mirror, and the zip
    form's is the ESA zips that people have published.
- **Zips.** ESA's SAFE zips **store** every entry (method 0, no data
  descriptors, no ZIP64, one top-level `<name>.SAFE/` directory). Checked on
  the central directories of the two Mendeley zips below: 146 and 115
  entries, all method 0. Local headers have extra fields that differ from
  the central directory's (28 bytes), so a local header must be read to find
  an entry's data.
- **XML.** The product, tile and manifest XML use only what the §1.5 XML
  subset allows. They have an XML declaration, namespace prefixes (`n1:`)
  and comments (in `manifest.safe`), and no DOCTYPE, CDATA or processing
  instructions. The element paths of the convention §6 are the same in PB
  02.12 and 05.10. Offsets (`RADIO_ADD_OFFSET`, `BOA_ADD_OFFSET`) exist only
  from PB 04.00. The L2A R20m group has no B01 before PB 04.00 (35 image
  files instead of 36).
- **Georeferencing.** `MTD_TL.xml` gives `ULX`/`ULY` of the outer corner
  (499980, 5200020 for T32TNS). GDAL's transform of each band file (from
  its GML-in-JP2) equals `[XDIM, 0, ULX, 0, YDIM, ULY]`. The GML's origin
  is the center of the top-left pixel (500010, 5199990 at 60 m).
- **GeoZarr conventions.** The CRS convention was renamed from `geo-proj`
  to `proj` (CMO `name` `"proj"`, the UUID unchanged). `proj:transform`,
  `proj:shape`, `proj:bbox` and `proj:epsg` moved to `spatial:*` or were
  dropped. `proj`, `spatial` and `multiscales` are at tag `v0.1` ("pilot";
  breaking changes are expected before `v1`), read at commits `5ca5b2f`,
  `54d81b7` and `9b78efa` (2026-06-12). The framework's README examples use
  the stale names, and the GeoZarr spec repository now only points to these
  conventions. EOPF-Explorer's Sentinel-2 GeoZarr conversion uses the same
  three CMOs (through `zarr-cm` 0.5), `r10m`/`r20m`/`r60m` groups, and
  `multiscales` on the group that holds them.

## 2. Modules

Python, `src/vzip/virtualize/safe/`:

| module | holds |
|---|---|
| `__init__.py` | `virtualize_safe_store(store)`, `virtualize_safe_zip(url, read, size)`, `is_zip(head)` |
| `xml.py` | the §1.5 XML subset parser, generalized from `store.py`'s `_Xml` (which it then replaces there): elements with attributes, local names, paths, text |
| `zipdir.py` | §12.4: EOCD, ZIP64, central directory, local headers, raw inflate with CRC-32 (`zlib.decompressobj(-15)`) |
| `jp2.py` | §12.3: boxes, SIZ/COD/COC/QCD parsing, the main header rest, the SOT walk; `chunk_ranges(tile)`: §12.6 pieces 1–5, with the empty-packet count |
| `product.py` | the convention §2: objects and their kinds, product and tile metadata, image files, band names, resolutions, the tile geocoding |
| `metadata.py` | the convention §5 and §6: GeoZarr attributes, the band, root and `vzip_source` source metadata, the §6.1 value rules |
| `virtualize.py` | assembles the output: documents, chunk references, data sources, `vzip_source` arrays and objects, the summary |

A product's objects come through one small interface with two
implementations. It lists `(key, size)`, reads a range of an object, and
gives where an object's bytes are (`(source, offset)`). The store
implementation wraps `store.Store`. The zip implementation wraps
`zipdir`, its objects all in source 0 at their data start, with deflated
XML documents held in memory. Everything above `product.py` sees only this
interface, so the two forms share every line but their source tables.

**The output.** Neither `common.Output` (one url source plus data sources)
nor `store.StoreOutput` (one url source per entry, whole objects) fits the
directory form. Add a `SafeOutput` (or generalize `Output`) with:

- a list of url sources;
- data sources numbered after them by first use, in ascending entry-key
  order (profile §12.8; `Output.shared` assumes source 0 is the only url
  source);
- references of `(source, offset, length)` and literal ranges;
- bytes entries.

Its writer emits the url sources, then the data sources, then the entries.
The zip form is the case of one url source.

**Shared code.**

- `common.metadata_array` / `Plan` and the §7 **Bytes** chunking for the XML
  and JP2 header arrays;
- `store.object_key` for the escaping of `vzip_source/objects/`;
- `common.text_json` / `json_base64` / `json_number` for §6 values;
- `common.http_reader` for range reads of band files in the directory form.
  It has a 64 KiB block cache, so the SOT walk costs one request per tile
  of more than 64 KiB, which is fine.

The SOT walks of a product's band files are independent, so run them
concurrently. A thread pool in Python, and `Promise.all` with a
concurrency limit of about 16 in TypeScript, keep a product at about
10–30 s.

**Dispatch.**

- `vzip/virtualize/__init__.py`: in `virtualize_store`, after `choose_profile`
  returns `"safe"` (`store.choose_profile` gains the `manifest.safe` row);
  in `virtualize`, `head[:4] == b"PK\x03\x04"` → `virtualize_safe_zip`
  (before the final rejection).
- `common.PROFILES` gains `"safe": ("ef81346c-19e8-42ad-93b0-a279ccaf44c1",
  1, "Sentinel-2 SAFE")`.
- The CMOs of `proj`, `spatial` and `multiscales` are constants in
  `metadata.py`, copied from the convention §5.

TypeScript, `web/src/virtualize/safe/`: the same modules (`xml.ts`,
`zipdir.ts`, `jp2.ts`, `product.ts`, `metadata.ts`, `virtualize.ts`), with
`index.ts` dispatching as above and `store.ts`'s `chooseProfile` gaining the
row.

- Raw inflate: `DecompressionStream("deflate-raw")` (Node ≥ 18, every
  current browser); CRC-32 by a 256-entry table.
- Decimal-to-binary64 parsing is `Number(text)` after the §6.1 grammar
  check (correctly rounded in V8).
- Integers beyond 2^53 − 1 are strings (§6), detected by digit count and
  comparison before conversion.
- `web/conformance/virtualize.ts` needs no change beyond `index.ts`: it
  already runs file and store inputs.

Shared documents to change when this lands (the design does not edit them):

- `VIRTUALIZE.md`:
  - the profiles table gains §12 (and Conformance becomes §13);
  - §1.2's table gains the ZIP row;
  - §1.4's table gains the `manifest.safe` row;
  - §1.4's source table and "no data sources" rules gain the SAFE
    exception (profile §12.8), and §1.4's "reads an object whole" gains the
    exception for band files (§12.2);
  - §13 lists the SAFE implementations, fixtures and corpus;
  - the revision becomes 17.
- `conventions/README.md`:
  - the table gains `safe`, and §1 gains `T` = `Sentinel-2 SAFE`;
  - §2 "Nothing else" gains the GeoZarr members (`proj:*`, `spatial:*`,
    `multiscales`) as target-format metadata, where a convention places
    them.
- `conventions/generate_schemas.py` gains the `safe` schema: the root,
  band and `vzip_source` source metadata. It should also validate the
  `proj`, `spatial` and `multiscales` members against their own v0.1
  schemas in `tests/test_virtualize_conventions.py`.
- `src/vzip/codecs.py`: no change. `imagecodecs_jpeg2k` already decodes
  each chunk, and every chunk decodes to the full chunk shape.
- `conventions/TECHNIQUES.md` §2's table gains the technique "the unit is
  smaller than the chunk at the edge → pad the codestream with empty tiles
  on the same reference grid", and §7's table gains SAFE.

## 3. Tests

Following the user's rule (one test of the expected outputs over
reasonable inputs, and one test per error case).

**Python** (`tests/test_virtualize_safe.py`):

- `test_outputs`: for every accepting fixture, the shapes, data types,
  chunk shapes, codecs, group names, GeoZarr attributes and summary, as a
  table like `test_virtualize_zarr2.CASES`;
- `test_chunk_bytes`: for one fixture, the exact bytes of an interior chunk
  and an edge chunk (SIZ', SOT, empty tiles, EOC). These are golden values,
  checked once by hand against the profile;
- one `test_reject_<rule>` per rejection fixture, each asserting `Rejected`
  with the rule's message;
- `tests/test_virtualize_conventions.py` picks the fixtures up through its
  fixture list (schemas, declarations).

**Unit tests** of `jp2.empty_packets` against the PLT counts of the
fixtures' own tiles (the check `pltcheck.py` made), and of `zipdir` on
zips written by Python's `zipfile` (stored, deflated, ZIP64 forced by
`force_zip64=True`).

**TypeScript** (`web/test/safe/safe.test.ts`): the same output table and
rejections, run under `node --test`.

## 4. Fixtures

`web/test/safe/write_fixtures.py` writes small synthetic products to
`web/test/fixtures/safe/<name>/` (directories), and `<name>.SAFE.zip`
beside them for the zip cases.

**Band files** are written with rasterio's JP2OpenJPEG driver (GDAL 3.12,
OpenJPEG 2.5.4, from the `rasterio` wheel; checked to install with `uv` in
a scratch venv), which gives tiles, precincts, PLT, 15-bit precision and
GML-in-JP2. The creation options that mimic Kakadu's files are
`tiled=True, blockxsize=T, blockysize=T, QUALITY=100, REVERSIBLE=YES,
RESOLUTIONS=5, NBITS=15, PRECINCTS={256,256}×5, PLT=YES, CODEBLOCK_WIDTH=64,
CODEBLOCK_HEIGHT=64, GMLJP2=YES, GeoJP2=NO, YCC=NO`, plus
`BLOCKSIZE_STRICT=YES`. Uppercase `BLOCKXSIZE` is ignored by rasterio; the
lowercase keys work.

- imagecodecs cannot write tiles ("writing tiles not implemented yet").
- glymur installs, but finds no `libopenjp2` on this machine (it needs a
  system OpenJPEG, such as `brew install openjpeg`), so it is not used.
- OpenJPEG writes a COM of its own and `Rsiz = 2`. The fixture writer then
  rewrites the main header to Kakadu's form, so that the fixtures exercise
  the bytes real files have: it replaces the COM with the two Kakadu COMs,
  and sets `Rsiz` to 0. Rewriting a main header segment never moves the
  tile-parts' contents. Only the codestream and `jp2c` lengths change, and
  the writer adjusts both.

**XML** is generated from templates trimmed from the real
`MTD_MSIL1C.xml`, `MTD_MSIL2A.xml`, `MTD_TL.xml` and `manifest.safe` of
the corpus products (with sizes, geopositions, file names and band lists
filled in). A filler XML file brings the tile metadata over 65536 bytes, so
that it becomes an array.

Images are scaled down: "10 m" 110 × 110 in tiles of 32 (an edge of 14),
"20 m" 55 × 55 in tiles of 20 (an edge of 15, not a multiple of 2^N), "60 m"
19 × 19 in tiles of 6 (an edge of 1, so 6 × 6 tiles of 1 pixel fill the
corner chunk). The geocoding uses the same 1:1 / 1:2 / 1:6 ratios with
`ULX`/`ULY` of T32TNS. Pixel values are seeded random 15-bit numbers, so
that a misplaced tile cannot pass.

Accepting:

| name | covers |
|---|---|
| `safe_l1c` | L1C at PB 05.10: 13 bands and TCI (3 components, 8-bit) at their native resolutions, JP2 masks and PVI, AUX_DATA, HTML, rep_info, a `_$folder$` marker, an empty object |
| `safe_l2a` | L2A at PB 05.10: bands at 10/20/60 m, AOT, WVP, SCL (8-bit), TCI at three tile sizes (one larger than half the image), multiscales |
| `safe_l1c_pb0207` | L1C at PB 02.07: GML masks, no offsets |
| `safe_l2a_pb0212` | L2A at PB 02.12: no B01 at 20 m, no offsets |
| `safe_l1c.SAFE.zip` | `safe_l1c` as ESA zips it (stored, a directory entry per directory) |
| `safe_l2a_deflated_xml.SAFE.zip` | XML documents deflated (copied), band files stored |
| `safe_zip64.SAFE.zip` | ZIP64 records (forced) |
| `safe_latin1_xml` | an XML document of the product that is not UTF-8 (`{"latin1": …}` text) |
| `safe_metadata_gaps` | missing `Spectral_Information`, a non-numeric offset, a duplicate `SOLAR_IRRADIANCE`: absent members, no rejection |

Rejecting (`safe_reject_*`, one per rule, each the smallest product that
breaks it):

- `old_format` (no MTD_MSIL*);
- `two_mtd`, `no_manifest`, `two_granules`, `no_image_file`;
- `missing_band_file`, `mixed_granule_dirs`, `no_tile_metadata`;
- `bad_cs_code`, `no_geoposition`, `zero_xdim`, `duplicate_resolution`;
- `size_matches_no_resolution`, `duplicate_band_name`, `bad_band_name`;
- `not_jp2` (a raw `.j2k`), `jp2c_not_last`, `box_beyond_file`;
- `tlm`, `ppm`, `sop`, `eph`;
- `two_tile_parts`, `tile_parts_out_of_order`, `psot_zero`, `no_eoc`,
  `bytes_after_eoc`;
- `tile_origin`, `signed`, `subsampled`, `precision_17`, `two_components`,
  `mixed_precision`;
- `xml_doctype`, `xml_not_utf8_metadata` (the product metadata itself);
- zip: `zip_encrypted`, `zip_deflated_band`, `zip_deflated_mask`,
  `zip_bzip2`, `zip_bad_crc`, `zip_two_roots`, `zip_not_safe_root`,
  `zip_duplicate_name`, `zip_dotdot`, `zip_truncated_cd`, `zip_no_eocd`.

## 5. Verifier

`web/test/safe/verify.py`, like the other profiles' verifiers. It
virtualizes each fixture (served by the harness proxy with its S3 listing,
and the zips as files) with the browser code
(`web/conformance/virtualize.ts`) and the Python one, opens each archive
through VZipStore and zarr-python (with `vzip.codecs` registered), and
checks:

- **every band array**, read whole, equals the band file decoded by an
  independent reader:
  - rasterio's JP2OpenJPEG driver on the band file;
  - and for the real products, GDAL's SENTINEL2 driver on the product
    metadata, whose subdatasets group the bands by resolution
    (`SENTINEL2_L2A:/vsicurl/<…>/MTD_MSIL2A.xml:20m:EPSG_32632`, checked to
    open remotely on the GCS mirror). Its band metadata (`BOA_ADD_OFFSET`,
    `SOLAR_IRRADIANCE`, wavelengths) is also compared with the convention
    §6.2 members.
  - Both use OpenJPEG to decode. What they check independently is the
    layout: the tiles, their places and the georeferencing, not the
    decoder.

  With Kakadu (`kdu_expand`) available, a third pass decodes every chunk's
  codestream with it. Kakadu is stricter about packet counts than OpenJPEG,
  so this pass would catch a wrong count of empty packets.
- **the group attributes**: `spatial:transform` equals rasterio's
  `transform` of each band file in the group, `spatial:shape` its shape,
  and `proj:code` its CRS;
- **the source metadata**: every XML document's text or array equals the
  object's bytes, every `vzip_source/objects/` entry equals its object,
  every JP2 header array equals the file's first bytes, and the band files
  are rebuilt from the hierarchy (the convention §6.4) and compared with the
  originals, byte for byte;
- **rejections**: every `safe_reject_*` is rejected by both
  implementations.

`--remote <url> [<band> …]` virtualizes a corpus product and compares the
listed bands (default: the 60 m bands, about 1–3 MB each), so a corpus run
does not download gigabytes.

`conformance/virtualize/compare.py` gains `corpus_safe.txt` (prefix
`safe-`) and the zip fixtures. The proxy needs no change for the directory
form: it already serves fixtures with the S3 listing.

## 6. Corpus

`conformance/virtualize/corpus_safe.txt`. Every directory URL below was
listed with `ListObjectsV2` through vzip's own `store.py` (1 request each),
had all its image files present, served range requests, and had every tile
of its B01 (60 m) band rebuilt as a standalone codestream and compared with
rasterio's decoding of the whole file (0 mismatches). The two zip URLs were
read by range (redirect from `data.mendeley.com` to S3, 206), their central
directories parsed (all entries stored), and one band file taken from
inside a zip by range passed the same tile check.

```
# url|name of public Sentinel-2 SAFE products. Directory form: the Google Cloud public
# mirror gcp-public-data-sentinel-2 (Copernicus Sentinel data, free and open licence),
# listed anonymously with S3 ListObjectsV2 at storage.googleapis.com. Zip form: ESA zips
# published on Mendeley Data (doi:10.17632/ckcxh6jskz.1, CC BY 4.0).
https://storage.googleapis.com/gcp-public-data-sentinel-2/tiles/32/T/NS/S2B_MSIL1C_20240103T101329_N0510_R022_T32TNS_20240103T110304.SAFE/|l1c-t32tns-pb0510
https://storage.googleapis.com/gcp-public-data-sentinel-2/L2/tiles/32/T/NS/S2B_MSIL2A_20240103T101329_N0510_R022_T32TNS_20240103T113848.SAFE/|l2a-t32tns-pb0510
https://storage.googleapis.com/gcp-public-data-sentinel-2/tiles/32/T/NS/S2C_MSIL1C_20260107T101421_N0511_R022_T32TNS_20260107T121047.SAFE/|l1c-t32tns-pb0511-s2c
https://storage.googleapis.com/gcp-public-data-sentinel-2/L2/tiles/32/T/NS/S2C_MSIL2A_20260107T101421_N0511_R022_T32TNS_20260107T140910.SAFE/|l2a-t32tns-pb0511-s2c
https://storage.googleapis.com/gcp-public-data-sentinel-2/tiles/32/T/NS/S2B_MSIL1C_20190701T102029_N0207_R065_T32TNS_20190701T123353.SAFE/|l1c-t32tns-pb0207-gml
https://storage.googleapis.com/gcp-public-data-sentinel-2/L2/tiles/32/T/NS/S2B_MSIL2A_20190701T102029_N0212_R065_T32TNS_20190701T134657.SAFE/|l2a-t32tns-pb0212-gml
https://storage.googleapis.com/gcp-public-data-sentinel-2/L2/tiles/32/T/NS/S2A_MSIL2A_20220121T102331_N0301_R065_T32TNS_20220121T131536.SAFE/|l2a-t32tns-pb0301
https://storage.googleapis.com/gcp-public-data-sentinel-2/L2/tiles/56/H/LH/S2A_MSIL2A_20230603T000231_N0509_R030_T56HLH_20230603T040000.SAFE/|l2a-t56hlh-south-utm56s
https://storage.googleapis.com/gcp-public-data-sentinel-2/tiles/18/T/WL/S2B_MSIL1C_20230606T153819_N0509_R011_T18TWL_20230606T173807.SAFE/|l1c-t18twl-utm18n
https://storage.googleapis.com/gcp-public-data-sentinel-2/tiles/32/T/NS/S2A_MSIL1C_20150704T101006_N0500_R022_T32TNS_20231011T134419.SAFE/|l1c-t32tns-2015-reprocessed-pb0500-html
https://data.mendeley.com/public-files/datasets/ckcxh6jskz/files/e97b9543-b8d8-436e-b967-7e64fe7be62c/file_downloaded|zip-l2a-t32upu-pb0212
https://data.mendeley.com/public-files/datasets/ckcxh6jskz/files/52abe583-c322-4ef1-8825-883fbfefe495/file_downloaded|zip-l1c-t33uwp-pb0208
```

Notes:

- What the products cover:
  - L1C and L2A;
  - processing baselines 02.07 to 05.11, and Sentinel-2A, 2B and 2C;
  - GML masks (before PB 04.00) and JP2 masks;
  - UTM zones 18N, 32N and 56S;
  - a reprocessed 2015 acquisition with `HTML/`;
  - both forms.
- `l2a-t32tns-pb0510` is 78% NODATA. Its edge of swath makes many tiles
  nearly empty (78-byte tile-parts), which is a useful case.
- The Mendeley file names end in `.zip`, not `.SAFE.zip`. Detection uses
  the zip magic and the `.SAFE/` root, not the name. The zips are 845 and
  945 MB, but only their central directories, local headers, XML and SOT
  headers are read.
- A rejection to check: the 2015 product in the format used before
  December 2016,
  `https://storage.googleapis.com/gcp-public-data-sentinel-2/tiles/32/T/NS/S2A_MSIL1C_20150627T102531_N0202_R062_T32TNS_20160607T154053.SAFE/`.
  It has no `MTD_MSIL1C.xml` (its product metadata is
  `S2A_OPER_MTD_SAFL1C_…xml`), and is rejected.
- Products near the GCS mirror's limits (the mirror's coverage after
  2026, a product with every tile empty) can be added as they are found.

## 7. Order of work

1. `xml.py` (and `store.py` switched to it), `zipdir.py` and `jp2.py`, each
   with its unit tests; the Python fixture writer for band files, checked
   with `pltcheck`-style assertions.
2. `product.py`, `metadata.py` and `virtualize.py`, together with the output
   class and the dispatch. Then the fixtures and `test_virtualize_safe.py`.
3. The verifier on the fixtures, then on the corpus with `--remote`.
4. The TypeScript twin, module by module, compared with Python by
   `compare.py` on fixtures and corpus.
5. The shared documents (§2), the schema, `REVISIONS.md` (revision 17), and
   an independent implementation round from the documents alone
   (`impls/virtualize/`), as the other profiles had.

## 8. Open questions, and their decisions

The questions below were decided before implementation. The convention
and the profile state the decisions:

1. Edge tiles: padded with empty tiles, as designed.
2. Multiscales: a Level-2A root declares `multiscales` without
   `derived_from`; a Level-1C root declares none. The JPEG 2000 resolution
   levels are not exposed in version 1.
3. XML: a document stays text up to 65536 bytes, and `vzip_source`'s `S`
   has a budget of 65536 bytes of JSON (measured as ND2's budgets are). Past
   it, documents become byte arrays referencing the object, the candidates
   taken in ascending order of size, then of keys (the convention §6.3).
4. The parsed fields: the fixed set, labeled in the convention as a derived
   convenience. The full XML is kept anyway.
5. CF attributes: none. Values are recorded, never applied.
6. Masks, probabilities and the preview image: kept whole, as objects, in
   version 1.
7. Products in the format used before December 2016: rejected.
8. `proj:wkt2`: written from a fixed template for the WGS 84 / UTM zones
   (EPSG 32601–32660 and 32701–32760), identically in both
   implementations, and tested against pyproj.
9. `spatial` and `proj`: on groups only.
10. (Version stability: unchanged, the convention follows `v1` when it
    comes.)
11. The GCS mirror's extras: its `_$folder$` markers are ignored, as layout
    of the mirror (not even their keys are kept); its `-ql.jpg` quicklook
    is an other object.

The questions as they were asked:

1. **Edge tiles.**
   - The design pads an edge chunk's codestream with empty tiles, so that
     every chunk decodes to the chunk shape with any JPEG 2000 decoder, and
     conventions §3 is unchanged.
   - The alternative is a codec rule: "a codestream may decode to a smaller
     `[h, w]` at the array's edge, which the codec pads". It is simpler (no
     packet counting), but it changes what `imagecodecs_jpeg2k` means.
     vzip's codec, the Neuroglancer fork, and any third-party reader would
     all have to pad.
   - A third option is a `rectilinear` chunk grid. zarr-python's support
     for it is new, and viewers lack it.
   - Is the padding approach acceptable, given that only PLT counts (not a
     strict decoder) validated the packet formula?
2. **Multiscales.**
   - The design declares `multiscales` only for L2A, without `derived_from`
     or `transform`, since the product does not say how its coarser bands
     were made.
   - Should L1C also declare it (its groups hold different bands)? Should
     L2A claim `derived_from: "r10m"` with scales 2 and 6, as EOPF's
     GeoZarr does?
   - The JPEG 2000 resolution levels are not exposed. They could be: with 1
     layer and LRCP or RPCL order, a tile's coarser resolutions are a
     prefix of its packets, whose lengths the PLT markers give. A chunk of
     level `k` would be that prefix with COD's `N` and QCD's subbands cut to
     `N − k`, and SIZ scaled by `2^k`. That gives 4 more levels per band
     (687 px at 10 m), but chunks shrink with the level (64 px tiles at
     level 4) unless several tiles share a codestream. It is a future
     version, if wanted.
3. **XML as text.**
   - XML documents of at most 65536 bytes are text in
     `vzip_source/zarr.json`; larger ones are byte arrays. The threshold is
     a choice. At 65536, the L1C manifest (49 KB) is text, and the L2A
     manifest (69 KB) and the tile metadata (190–630 KB) are arrays.
   - Should all XML be arrays (no copying, one rule), or should all XML of
     at most 2^20 bytes be text?
4. **The parsed subset** (the convention §6.2–§6.3) is a fixed list of
   fields, not a generic XML-to-JSON reading, so it breaks the "one reading
   per format" principle of TECHNIQUES.md §6. That was chosen for size and
   simplicity. A generic reading of `MTD_MSIL*.xml` (45–55 KB, about 1500
   elements, with arrays for repeated elements and attributes as members)
   is possible if wanted. `MTD_TL.xml` and `MTD_DS.xml` would need their
   numeric `VALUES` lists as arrays (TECHNIQUES.md §4).
5. **CF attributes.** Should the band arrays also carry
   `scale_factor = 1 / Q` and `add_offset = O / Q` (and `_FillValue` from
   NODATA), so that xarray decodes reflectances? The design writes none,
   since the user asked that values be recorded, not applied. CF
   attributes are applied by default by xarray.
6. **Masks and probabilities as arrays.**
   - The JP2 masks (`MSK_CLASSI`, `MSK_QUALIT` with 8 bit-plane components,
     `MSK_DETFOO`, L2A `MSK_CLDPRB`/`MSK_SNWPRB`) and the PVI are kept whole
     as objects, as the user asked.
   - The same tile technique virtualizes them; it was verified on
     `MSK_CLASSI` and `MSK_QUALIT`.
   - Should a later version present them as arrays (for example
     `quality/r60m/MSK_QUALIT_B01`, with `C` up to 8)?
7. **Old-format products** (before December 2016: one product with several
   granules, `S2A_OPER_…` names) are rejected. Many are still on the GCS
   mirror, though ESA reprocessed much of the archive to the compact format
   (PB 05.00). Supporting them means one group set per granule, and a
   different metadata layout.
8. **`proj:wkt2`.** The proj convention asks for it ("SHOULD always
   include"). The design writes only `proj:code`, since WKT2 would come
   from a CRS database. An option is a fixed table of WKT2 strings for the
   120 UTM/WGS84 codes Sentinel-2 uses, generated once and frozen into the
   spec.
9. **Group- vs array-level `spatial`.** The design puts `proj:*` and
   `spatial:*` on the resolution groups only, which the conventions' rules
   apply to their direct child arrays (EOPF does the same). The spatial
   schema requires `spatial:dimensions` on an array that is validated
   against it. Some readers may look only at arrays. Should every band
   array repeat them (about 300 bytes each)?
10. **Version stability.** `proj`, `spatial` and `multiscales` are at `v0.1`
    ("pilot"). When they reach `v1`, the convention's next version follows
    them.
11. **The GCS mirror's extras.** The `_$folder$` markers and `-ql.jpg` are
    kept as other objects, so the same product gives different
    `vzip_source` contents on GCS and from an ESA zip. They could be
    ignored instead (recorded in `ignored`), at the cost of a
    mirror-specific rule.
