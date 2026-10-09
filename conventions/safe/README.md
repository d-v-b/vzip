# The SAFE convention

The Zarr v3 layout of a Sentinel-2 Level-1C or Level-2A product in the SAFE
format (a `.SAFE` directory, or the `.SAFE.zip` archive of one), and the
translation of its metadata into JSON. What all of vzip's conventions share
is in [conventions/README.md](../README.md), cited here as "conventions §n".
How vzip produces this layout as a virtual store is the SAFE profile,
[profiles/safe.md](../../profiles/safe.md).

Convention version: 0 (until release, README §1) · UUID: `ef81346c-19e8-42ad-93b0-a279ccaf44c1` ·
Schema: [schema.json](schema.json)

This convention gives a layout only to the products that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a product that fails has no layout under this
convention, and the profile rejects it.

A SAFE product is a tree of files. Its pixels are JPEG 2000 files (JP2),
one per band and resolution, each a grid of independently coded tiles. Its
metadata is XML: the product metadata `MTD_MSIL1C.xml` or `MTD_MSIL2A.xml`,
the tile metadata `GRANULE/<g>/MTD_TL.xml`, the datastrip metadata, quality
reports and the SAFE manifest. The output is a GeoZarr hierarchy: one group
per resolution (`r10m`, `r20m`, `r60m`), one array per band, and one chunk
per JP2 tile. Each chunk is a standalone JPEG 2000 codestream that is
rebuilt from byte ranges of the band's file, plus a header that is shared
by the band's chunks. The groups declare their coordinate reference system
and affine transform with the zarr-conventions `proj` and `spatial`
conventions (§5). The source's values are recorded but never applied: the
radiometric offsets and the quantification values are metadata of each band
(§6.2). Every file of the product is kept: the XML files as text or as
arrays of their bytes, and the other files whole (§6.3).

Unlike the other conventions, the hierarchy has no OME-NGFF metadata. It is
geospatial data, and its target formats are Zarr v3 and the GeoZarr
conventions of §5. This section extends conventions §2 ("Nothing else"): the
attributes of this convention's nodes may hold, besides `zarr_conventions`
and `vzip_virtualized`, the members that §5 gives them (`proj:code`,
`proj:wkt2`, `spatial:*` and `multiscales`), and only on the groups that §5
names.

## 1. Declaration

The root declares the convention by [conventions §2](../README.md#2-attributes),
with `"profile": "safe"`, `"version": 0`, `"revision": 24` (README §1), the input's URL as `source.url`
(the store URL, ending in `/`, of a directory, or the URL of a zip file),
and the root's source metadata (§6.1) as the member `"safe"`. Each band
array and `vzip_source` declare it with their own source metadata (§6.2,
§6.3). The arrays under `vzip_source` have none. Its CMO is:

```json
{
  "uuid": "ef81346c-19e8-42ad-93b0-a279ccaf44c1",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/heads/main/conventions/safe/schema.json",
  "spec_url": "https://github.com/d-v-b/vzip/blob/main/conventions/safe/README.md",
  "name": "vzip_virtualized",
  "description": "The Zarr layout of a Sentinel-2 SAFE source virtualized by vzip, and the source's metadata"
}
```

A node's `zarr_conventions` lists the CMOs of the conventions it uses, in
this order: this convention's, then `multiscales`, `proj` and `spatial`
(§5), each only when the node uses it.

## 2. The source

### 2.1 Objects

The product is a set of **objects**, each with a **key** (a relative path
whose segments are separated by `/`) and a **size**. The profile says how
it finds them: by listing a store, or from the central directory of a zip
file. Keys compare as UTF-8 byte strings. The objects are classified as
follows:

- the **band files**: the objects that the product metadata lists as
  images in `IMG_DATA` (§2.3);
- the **XML documents**: every other object whose key is `manifest.safe`,
  or ends in `.xml` or `.xsd`;
- the **other objects**: every other object. Among them are the quality
  masks (GML files before processing baseline 04.00, JP2 files from 04.00),
  the preview image (`QI_DATA/*_PVI.jp2`), the L2A cloud and snow
  probabilities (`QI_DATA/MSK_CLDPRB_*.jp2`, `MSK_SNWPRB_*.jp2`, and before
  processing baseline 02.07 `QI_DATA/*_CLD_*.jp2`, `*_SNW_*.jp2`), the
  auxiliary data (`AUX_DATA/*`), the `HTML` folder and the quicklook images
  that some mirrors add (the Google Cloud mirror's `<product>-ql.jpg`). In
  this version the masks, the probabilities and the preview image are kept
  whole (§6.3), not presented as arrays.

An object of size 0 is an **empty object**. It is none of the three kinds,
and only its key is kept (§6.3).

A **folder marker** is an object whose key is a nonempty `d` followed by
`_$folder$`. Mirrors that emulate directories in a flat bucket write them:
the Google Cloud mirror writes one of 6 bytes (`folder`) for every
directory, including the empty ones (such as the root's `AUX_DATA` of a
Level-2A product of processing baseline 02.06 or 02.07). A folder marker is
layout of the mirror, as a zip file's directory entries are layout of the
zip: it is none of the kinds above (not an empty object either), it is not
read, and nothing of it is kept but the directory it names. The kinds above
are those of the other objects.

The **named directories** are the `d` of every folder marker, and those
that the profile names otherwise (a zip file's directory entries, a
listing's empty objects whose keys end in `/`). A named directory is
**empty** when no other key, of an object, of a folder marker or of a named
directory, starts with `d/`. The empty directories are kept (§6.3), so that
the rebuilt product has them (§6.4); the others are the directories of its
objects.

**Reading XML.** The product metadata and the tile metadata (§2.2, §2.4)
are read as the subset of XML 1.0 of
[VIRTUALIZE.md §1.5](../../VIRTUALIZE.md#15-listing-a-store). They MUST be
at most 2^24 bytes, and the input is rejected if they are not in that
subset. In this convention, an element is named by its **local name**: its
name after the last `:` (so `n1:General_Info` is `General_Info`). A name
below also matches its alias in the product specification (PSD) 14.2, which
the Level-2A products of processing baselines 02.04–02.06 use:

| name | PSD 14.2 alias |
|---|---|
| `Product_Info`, `Product_Organisation`, `Product_Image_Characteristics` | `L2A_` followed by the name |
| `IMAGE_FILE`, `PRODUCT_URI`, `TILE_ID` | the name followed by `_2A` |
| `QUANTIFICATION_VALUES_LIST` | `L1C_L2A_Quantification_Values_List` |
| `BOA_QUANTIFICATION_VALUE`, `AOT_QUANTIFICATION_VALUE`, `WVP_QUANTIFICATION_VALUE` | `L2A_` followed by the name |
| `Scene_Classification_List`, `Scene_Classification_ID`, `SCENE_CLASSIFICATION_TEXT`, `SCENE_CLASSIFICATION_INDEX` | `L2A_` followed by the name | A **path**
`A/B/C` names the elements `C` that are children of a `B` that is a child of
an `A`. An element's **text** is
[§1.5's](../../VIRTUALIZE.md#15-listing-a-store), with leading and trailing
whitespace (§1.3) removed. XML documents that this convention does not read
are not parsed.

### 2.2 The product metadata

The root MUST have exactly one of the objects `MTD_MSIL1C.xml` (the product
is **Level-1C**) and `MTD_MSIL2A.xml` (**Level-2A**), and the object
`manifest.safe`. (A product in the format used before December 2016, whose
product metadata is named `S2A_OPER_MTD_SAFL1C_….xml` and which may hold
several granules, has neither, and is rejected: this version does not
support that format.) The product metadata's
root element MUST be named `Level-1C_User_Product` or
`Level-2A_User_Product`, as the level requires.

The **granules** are the elements `General_Info/Product_Info/Product_Organisation/Granule_List/Granule`
under the root. Their `IMAGE_FILE` children, in document order, are the
**image files**, of which there MUST be at least one. (A product has one
granule. Processing baselines 02.04–02.07 list it three times, one
`Granule_List` per resolution, each with the image files of that
resolution: their image files are concatenated. That they name one granule
is checked by §2.3.)

### 2.3 Image files

Each image file's text `F` MUST have the form `GRANULE/<g>/<d>/<rest>`,
where `<g>` and `<d>` are segments, `<g>` the same for every image file,
and `<rest>` is one or more segments, and the image files MUST all differ.
The image files with `<d>` = `IMG_DATA` are the bands, of which there MUST
be at least one. For each, the key `F` followed by `.jp2` MUST be an object
of the product, the image file's **band file**, whose **basename** `b` is
the last segment of `F`. The others (PSD 14.2 lists its DEM under
`AUX_DATA`, which the products do not hold, and its cloud and snow
probabilities under `QI_DATA`) are not bands: their objects, if any, are
other objects (§2.1).

`<g>` names the granule's directory. The **tile metadata** is the object
`GRANULE/<g>/MTD_TL.xml`, which MUST exist, and whose root element MUST be
named `Level-1C_Tile_ID` or `Level-2A_Tile_ID`, as the level requires.

### 2.4 The tile geocoding

The element `Geometric_Info/Tile_Geocoding` under the tile metadata's root
MUST be present once. It MUST have:

- one child `HORIZONTAL_CS_CODE`, whose text is `EPSG:` followed by one or
  more digits: the **CRS code**;
- one or more children `Size`, each with an attribute `resolution` whose
  value is one or more digits without a leading zero, at most 2^32 − 1,
  the resolution `r` in metres, and with one child `NROWS` and one child `NCOLS`, each of whose
  texts is a positive decimal integer (digits, without a leading zero), at
  most 2^32 − 1. No two have the same `r`;
- for each `Size` of resolution `r`, exactly one child `Geoposition` whose
  attribute `resolution` is the same text, with one child each of `ULX`,
  `ULY`, `XDIM` and `YDIM`, whose texts are decimal numbers (§6.1), finite
  as binary64 values (VIRTUALIZE.md §1.3), with `XDIM` and `YDIM` not zero.
  (`ULX` and `ULY` are the coordinates of the outer corner of the top-left
  pixel; `XDIM` and `YDIM` are the pixel's width and height, `YDIM`
  negative for a north-up grid.)

Other children of `Tile_Geocoding` (`HORIZONTAL_CS_NAME`) are not checked.

### 2.5 Band files

Each band file is a JP2 file whose codestream the profile reads and checks
([profiles/safe.md §12.3](../../profiles/safe.md#123-band-files)). It gives:

- its **image size** `W × H` (the SIZ marker's `Xsiz` and `Ysiz`), its
  **tile size** `T × U` (`XTsiz` and `YTsiz`), and its number of tiles,
  `nx = ceil(W / T)` across and `ny = ceil(H / U)` down;
- its number of **components** `C`, which MUST be 1 (a band) or 3 (a
  true-color image), and their **precision** `p`, the same for every
  component, from 1 to 16, unsigned and not subsampled;
- one **tile-part** per tile, in raster order.

The band file's **resolution** is the `r` of the one `Size` (§2.4) whose
`NCOLS` is `W` and whose `NROWS` is `H`; if none or several match, the input
is rejected. Its **band name** is the basename `b`, with the suffix `_<r>m`
removed if `b` ends with it, then the part of it after its last `_` (all of
it if it has no `_`). The band name MUST be one or more ASCII letters and
digits (`B02`, `B8A`, `TCI`, `AOT`, `WVP`, `SCL`). No two band files with the
same resolution may have the same band name.

So in a Level-1C product `T32TNS_20240103T101329_B02` is the band `B02` at
10 m, and in a Level-2A product `T32TNS_20240103T101329_B02_20m` is the band
`B02` at 20 m.

## 3. The hierarchy

The hierarchy is:

- the root, a group (§5.3);
- for each resolution `r` of a band file, in ascending order, the group
  `r<r>m` (for example `r10m`), a child of the root (§5.2), and in it one
  array per band file of that resolution, named by its band name (§4);
- the group `vzip_source`, a child of the root, holding the source's other
  metadata (§6.3).

A product whose bands have the resolutions 10, 20 and 60 m, as every
product does, has the groups `r10m`, `r20m` and `r60m`. A Level-1C product
has each band at its own resolution (`r10m` has `B02`, `B03`, `B04`, `B08`
and `TCI`; `r20m` has `B05`, `B06`, `B07`, `B8A`, `B11` and `B12`; `r60m`
has `B01`, `B09` and `B10`). A Level-2A product has most bands at several
resolutions, and adds `AOT`, `WVP` and `SCL`.

Every group's `zarr.json` is
`{"zarr_format": 3, "node_type": "group", "attributes": A}`, with `A` as
this document gives it, or `{}` for a group it gives no attributes.

## 4. Arrays

A band file of `C` components, precision `p`, image size `W × H` and tile
size `T × U` is an array whose `zarr.json` is as in
[conventions §3](../README.md#3-arrays), with:

- `shape` `[H, W]` and `chunk_shape` `[U, T]` when `C = 1`, and `[3, H, W]`
  and `[3, U, T]` when `C = 3`;
- `data_type` `uint8` when `p ≤ 8`, and `uint16` otherwise. (A Sentinel-2
  reflectance band has `p = 15`: its values fill 15 bits of a `uint16`. The
  true-color image `TCI` and the scene classification `SCL` have `p = 8`.)
- `dimension_names` `["y", "x"]`, or `["c", "y", "x"]`;
- `codecs` `[{"name": "imagecodecs_jpeg2k"}]` when `C = 1`, and
  `[{"name": "transpose", "configuration": {"order": [1, 2, 0]}}, {"name": "imagecodecs_jpeg2k"}]`
  when `C = 3` (the codestream decodes to `[y, x, c]`);
- `fill_value` 0;
- `attributes`: the declaration of §1 with the band's source metadata
  (§6.2).

**Chunks.** The chunk at `<group>/<band>/c/<v>/<u>` (`<group>/<band>/c/0/<v>/<u>`
when `C = 3`), for `0 ≤ u < nx` and `0 ≤ v < ny`, is the tile `t = v × nx + u`
of the band file. It holds one JPEG 2000 codestream (ISO/IEC 15444-1),
which decodes to `U` rows of `T` samples per component. Its top-left
`h × w` samples are the tile's samples, where `w = min(T, W − u × T)` and
`h = min(U, H − v × U)`. The other samples are outside the array at its
right and bottom edges, and are unspecified. Every chunk is present.

The codestream is the band file's tile, coded as the file codes it, placed
on the JPEG 2000 reference grid where the file places it, and it is the
only tile with data:

- its main header is the band file's, with its own SIZ marker. The image
  area is `[x0, x0 + T) × [y0, y0 + U)`, where `x0 = u × T` and
  `y0 = v × U`. The tiles are `w × h`, with their origin at `(x0, y0)`.
- its tile 0 is the band file's tile `t`. Its area, `[x0, x0 + w) × [y0, y0 + h)`,
  is the area that tile has in the file, so the wavelet decomposition, the
  precincts and the code-blocks are the file's, and so are its packets.
- each of its other tiles (when `w < T` or `h < U`) lies outside the array,
  and holds only empty packets. They decode to the value `2^(p − 1)` (the DC
  level shift) and are never read.

The profile says how these codestreams are made of byte ranges of the band
file and a few bytes of their own
([profiles/safe.md §12.6](../../profiles/safe.md#126-chunks)).

The band file's other bytes are kept in the source metadata: the JP2 boxes
before the codestream (§6.3), and the original SIZ marker segment (§6.2).
The rest of the main header, its coding style, quantization and comments,
is in every chunk.

## 5. Georeferencing (GeoZarr)

The groups declare their coordinate reference system and their grid with
two conventions of the [zarr-conventions](https://github.com/zarr-conventions)
organization, which the GeoZarr specification builds on. Both are at their
tag `v0.1`, whose members are prefixed keys at the top level of
`attributes`:

- [`proj`](https://github.com/zarr-conventions/proj/blob/v0.1/README.md), with
  the CMO
  ```json
  {"uuid": "f17cb550-5864-4468-aeb7-f3180cfb622f",
   "schema_url": "https://raw.githubusercontent.com/zarr-conventions/proj/refs/tags/v0.1/schema.json",
   "spec_url": "https://github.com/zarr-conventions/proj/blob/v0.1/README.md",
   "name": "proj",
   "description": "Coordinate reference system information for geospatial data"}
  ```
- [`spatial`](https://github.com/zarr-conventions/spatial/blob/v0.1/README.md),
  with the CMO
  ```json
  {"uuid": "689b58e2-cf7b-45e0-9fff-9cfc0883d6b4",
   "schema_url": "https://raw.githubusercontent.com/zarr-conventions/spatial/refs/tags/v0.1/schema.json",
   "spec_url": "https://github.com/zarr-conventions/spatial/blob/v0.1/README.md",
   "name": "spatial",
   "description": "Spatial coordinate information"}
  ```

A Level-2A root also uses
[`multiscales`](https://github.com/zarr-conventions/multiscales/blob/v0.1/README.md)
(§5.3), with the CMO
```json
{"uuid": "d35379db-88df-4056-af3a-620245f8e347",
 "schema_url": "https://raw.githubusercontent.com/zarr-conventions/multiscales/refs/tags/v0.1/schema.json",
 "spec_url": "https://github.com/zarr-conventions/multiscales/blob/v0.1/README.md",
 "name": "multiscales",
 "description": "Multiscale layout of zarr datasets"}
```

Each CMO is written exactly as above, with these five members.

### 5.1 The grid of a resolution

For the resolution `r`, with the `Size` `NROWS`, `NCOLS` and the
`Geoposition` `ULX`, `ULY`, `XDIM`, `YDIM` of §2.4 (as binary64 values,
VIRTUALIZE.md §1.3):

- the **transform** is `[XDIM, 0, ULX, 0, YDIM, ULY]`: the affine
  coefficients `[a, b, c, d, e, f]` of the spatial convention, which maps the
  index `(col, row)` of a pixel's top-left corner to `(a × col + b × row + c,
  d × col + e × row + f)`. (This is GDAL's geotransform `[ULX, XDIM, 0, ULY,
  0, YDIM]` in the spatial convention's order.)
- the **shape** is `[NROWS, NCOLS]`.
- the **bounding box** is `[min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]`,
  where `x0 = ULX`, `x1 = ULX + XDIM × NCOLS`, `y0 = ULY` and
  `y1 = ULY + YDIM × NROWS` (the product first).

The grid's position is pixel registration: `ULX` and `ULY` are a corner, not
a pixel center (the GML in each band file gives the center of the top-left
pixel, half a pixel in from it).

### 5.2 Resolution groups

The attributes of the group `r<r>m` are:

```json
{
  "zarr_conventions": [P, Q],
  "proj:code": "EPSG:32632",
  "proj:wkt2": "PROJCRS[\"WGS 84 / UTM zone 32N\",BASEGEOGCRS[…]]",
  "spatial:dimensions": ["y", "x"],
  "spatial:transform": [10.0, 0.0, 499980.0, 0.0, -10.0, 5200020.0],
  "spatial:shape": [10980, 10980],
  "spatial:bbox": [499980.0, 5090220.0, 609780.0, 5200020.0],
  "spatial:registration": "pixel"
}
```

where `P` and `Q` are the CMOs of `proj` and `spatial`, `proj:code` is the
CRS code (§2.4) as written, `proj:wkt2` is as below (shortened here), and
the transform, shape and bounding box are
those of resolution `r` (§5.1). The proj convention applies a group's CRS
to its direct child arrays, and the spatial convention its properties, so
the band arrays carry neither. Every band array's `shape` ends with the
group's `spatial:shape` (§2.5), and its dimension names include `y` and
`x`.

**`proj:wkt2`.** When the CRS code is `EPSG:` followed by one of the
numbers 32601 to 32660 or 32701 to 32760, written without a leading zero
(the 120 WGS 84 / UTM zones, which are every CRS Sentinel-2 products use),
the group also has the member `proj:wkt2`, right after `proj:code`. Its
value is the template below, with `<c>` the number, `<z>` the zone
(`c − 32600` or `c − 32700`, without a leading zero), `<h>` `N` for
32601–32660 and `S` for 32701–32760, `<lon>` the decimal integer
`6 × z − 183` (with a `-` when negative), and `<fn>` `0` for `N` and
`10000000` for `S`, all on one line, with no whitespace but the spaces
shown inside the quoted names:

```
PROJCRS["WGS 84 / UTM zone <z><h>",BASEGEOGCRS["WGS 84",ENSEMBLE["World Geodetic System 1984 ensemble",
MEMBER["World Geodetic System 1984 (Transit)"],MEMBER["World Geodetic System 1984 (G730)"],
MEMBER["World Geodetic System 1984 (G873)"],MEMBER["World Geodetic System 1984 (G1150)"],
MEMBER["World Geodetic System 1984 (G1674)"],MEMBER["World Geodetic System 1984 (G1762)"],
MEMBER["World Geodetic System 1984 (G2139)"],MEMBER["World Geodetic System 1984 (G2296)"],
ELLIPSOID["WGS 84",6378137,298.257223563,LENGTHUNIT["metre",1]],ENSEMBLEACCURACY[2.0]],
PRIMEM["Greenwich",0,ANGLEUNIT["degree",0.0174532925199433]],ID["EPSG",4326]],
CONVERSION["UTM zone <z><h>",METHOD["Transverse Mercator",ID["EPSG",9807]],
PARAMETER["Latitude of natural origin",0,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8801]],
PARAMETER["Longitude of natural origin",<lon>,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8802]],
PARAMETER["Scale factor at natural origin",0.9996,SCALEUNIT["unity",1],ID["EPSG",8805]],
PARAMETER["False easting",500000,LENGTHUNIT["metre",1],ID["EPSG",8806]],
PARAMETER["False northing",<fn>,LENGTHUNIT["metre",1],ID["EPSG",8807]]],
CS[Cartesian,2],AXIS["(E)",east,ORDER[1],LENGTHUNIT["metre",1]],
AXIS["(N)",north,ORDER[2],LENGTHUNIT["metre",1]],ID["EPSG",<c>]]
```

(The line breaks above are for reading: the value has none.) This is the
WKT2 (ISO 19162:2019) that PROJ 9.8 writes for these codes from the EPSG
database, without its `USAGE` (the scope and area of use, which is not part
of the definition). It is fixed here, rather than taken from a CRS database
at run time, so that every producer writes the same string whatever its
database's version. For any other code the group has no `proj:wkt2`: the
EPSG code is what the product gives.

### 5.3 The root

The root's attributes are the declaration of §1 and:

- `proj:code`, the CRS code, `proj:wkt2` as §5.2 gives it, and
  `spatial:bbox`, the bounding box of the first resolution group (§5.1), so
  that a reader finds where the product is without opening a group. The
  root's `zarr_conventions` then lists the CMOs of `proj` and `spatial`.
- for a Level-2A product, `multiscales`, with `spatial:dimensions`
  `["y", "x"]` and `spatial:registration` `"pixel"`, and the multiscales CMO
  in `zarr_conventions`. `multiscales` is
  ```json
  {"layout": [
    {"asset": "r10m", "spatial:shape": [10980, 10980], "spatial:transform": [10.0, 0.0, 499980.0, 0.0, -10.0, 5200020.0]},
    {"asset": "r20m", "spatial:shape": [5490, 5490], "spatial:transform": [20.0, 0.0, 499980.0, 0.0, -20.0, 5200020.0]},
    {"asset": "r60m", "spatial:shape": [1830, 1830], "spatial:transform": [60.0, 0.0, 499980.0, 0.0, -60.0, 5200020.0]}
  ]}
  ```
  with one layout item per resolution group, in the order of §3, whose
  `asset` is the group's name and whose `spatial:shape` and
  `spatial:transform` are the group's.

A Level-2A product presents most of its bands at 10, 20 and 60 m. Its
coarser groups are resampled versions of the finer ones, made by the
processor, and a reader that wants a band at lower resolution can choose
among them. The layout items have no `derived_from` and no `transform`.
The product metadata does not say how the coarser bands were made, or from
which resolution, so the hierarchy does not claim it. A Level-1C product
has each band at only one resolution, so its groups are not levels of one
image, and its root has no `multiscales`.

In this version, the JPEG 2000 resolution levels inside each band file (4
wavelet decompositions) are not presented as multiscales levels, and the
`proj` and `spatial` members are on the groups only: the band arrays carry
neither, since the conventions apply a group's members to its child
arrays.

## 6. Source metadata

### 6.1 Values

The values this section copies from the XML are translated as follows (in
place of [conventions §6](../README.md#6-source-metadata-as-json)'s rules for
binary fields, which do not apply):

- An element's **value** is its text (§2.1). If it is a **decimal number**
  (an optional `-` or `+`, one or more digits, optionally `.` and one or more
  digits, and optionally `e` or `E`, an optional sign and one or more
  digits), it is a JSON number: an integer when it has neither a fraction
  nor an exponent (conventions §6: the string of its digits beyond
  2^53 − 1), and otherwise its binary64 value. A `+` and leading zeros are
  not kept. Otherwise the value is the JSON string of the text.
- A **measure** is an element with an attribute `unit`: the object
  `{"value": V, "unit": T}`, where `V` is its value and `T` the attribute's
  value as a string. Without the attribute, it is its value alone.
- A field that is missing, or found more than once where one is named, is
  absent. **Source metadata never rejects**: only what §2 reads for the
  layout is checked.

The XML documents themselves are kept whole (§6.3), and they are the
record of the product's metadata. What follows is a **derived
convenience**: a fixed set of fields copied from them into JSON, not a
translation of the XML. It holds what a reader needs to
interpret a band's values, kept on the band's array, and the product's
identity, kept on the root. TECHNIQUES.md's criteria decide what goes in
it. Each value is small, typed and semantic, and is wanted by nearly every
reader of the band, who would otherwise have to fetch and parse the
product metadata. The rest (the angle grids, the datastrip's
per-detector tables, the quality reports) is large, or a column of
numbers, or rarely wanted. It stays in the XML, which is kept whole and
read only by a reader who wants it. A general translation of XML into JSON
is not attempted: the product metadata alone holds spectral response curves
of hundreds of numbers per band, and the datastrip metadata is 15–25 MB.

### 6.2 Band arrays

Let `X` be the product metadata's element
`General_Info/Product_Image_Characteristics` under its root. A band whose
band name is `B` followed by two digits `dd`, or `B8A`, is a **spectral
band**, whose physical band name `PB` is `B` followed by `dd` without a
leading `0` (`B02` is `B2`), or `B8A`. Its **band id** `i` is the attribute
`bandId` of the element `X/Spectral_Information_List/Spectral_Information`
whose attribute `physicalBand` is `PB`, read as an integer, when exactly one
such element has a `bandId` of one to nine digits. Below, an element "whose
`bandId` (or `band_id`) is `i`" is one whose attribute of that name is one
to nine digits of the value `i`.

A band's source metadata `S` is an object with these members, in this
order, each present only when its value is:

- `IMAGE_FILE`: the image file's text (§2.3);
- for a spectral band with band id `i`:
  - `bandId`: `i`; `physicalBand`: `PB`;
  - `RESOLUTION`: the value of `RESOLUTION` under its `Spectral_Information`;
  - `Wavelength`: an object of the measures `MIN`, `MAX` and `CENTRAL` under
    its `Spectral_Information/Wavelength`, those present;
  - `PHYSICAL_GAINS`: the value of the element `X/PHYSICAL_GAINS` whose
    `bandId` is `i`;
  - `SOLAR_IRRADIANCE`: the measure
    `X/Reflectance_Conversion/Solar_Irradiance_List/SOLAR_IRRADIANCE` whose
    `bandId` is `i`;
  - Level-1C: `QUANTIFICATION_VALUE`, the measure `X/QUANTIFICATION_VALUE`;
    and `RADIO_ADD_OFFSET`, the value of
    `X/Radiometric_Offset_List/RADIO_ADD_OFFSET` whose `band_id` is `i`;
  - Level-2A: `BOA_QUANTIFICATION_VALUE`, the measure
    `X/QUANTIFICATION_VALUES_LIST/BOA_QUANTIFICATION_VALUE`; and
    `BOA_ADD_OFFSET`, the value of `X/BOA_ADD_OFFSET_VALUES_LIST/BOA_ADD_OFFSET`
    whose `band_id` is `i`;
- for the band `AOT` (Level-2A): `AOT_QUANTIFICATION_VALUE`, the measure
  `X/QUANTIFICATION_VALUES_LIST/AOT_QUANTIFICATION_VALUE`;
- for the band `WVP` (Level-2A): `WVP_QUANTIFICATION_VALUE`, the measure
  `X/QUANTIFICATION_VALUES_LIST/WVP_QUANTIFICATION_VALUE`;
- for the band `SCL` (Level-2A): `Scene_Classification_List`, an array with
  one object per element `X/Scene_Classification_List/Scene_Classification_ID`,
  in order, with the values of its children `SCENE_CLASSIFICATION_TEXT` and
  `SCENE_CLASSIFICATION_INDEX`, those present (and absent when there are more
  than 64 such elements; a product has 12);
- `siz`: the base64 (conventions §6) of the band file's SIZ marker segment,
  from its `FF 51` marker to its end, which the chunks replace with their
  own.

So the band `B04` of a Level-2A product of processing baseline 05.10 has:

```json
{
  "IMAGE_FILE": "GRANULE/L2A_T32TNS_A035655_20240103T101328/IMG_DATA/R10m/T32TNS_20240103T101329_B04_10m",
  "bandId": 3,
  "physicalBand": "B4",
  "RESOLUTION": 10,
  "Wavelength": {"MIN": {"value": 646, "unit": "nm"}, "MAX": {"value": 685, "unit": "nm"}, "CENTRAL": {"value": 665, "unit": "nm"}},
  "PHYSICAL_GAINS": 4.76278246,
  "SOLAR_IRRADIANCE": {"value": 1512.79, "unit": "W/m²/µm"},
  "BOA_QUANTIFICATION_VALUE": {"value": 10000, "unit": "none"},
  "BOA_ADD_OFFSET": -1000,
  "siz": "/1EAKQAAAAAq5AAAKuQAAAAAAAAAAAAABAAAAAQAAAAAAAAAAAAAAQ4BAQ=="
}
```

**The values are not applied.** The arrays hold the digital numbers the
band files hold. Since processing baseline 04.00, a Level-1C reflectance is
`(DN + RADIO_ADD_OFFSET) / QUANTIFICATION_VALUE`, and a Level-2A reflectance
is `(DN + BOA_ADD_OFFSET) / BOA_QUANTIFICATION_VALUE`. Earlier baselines have
no offset, and their products have no offset member. The convention writes
no `scale_factor`, `add_offset` or `_FillValue` attributes, which a reader
would apply.

### 6.3 The source metadata node

The root's source metadata `S` is the object

```json
{
  "PRODUCT_URI": "S2B_MSIL2A_20240103T101329_N0510_R022_T32TNS_20240103T113848.SAFE",
  "PROCESSING_LEVEL": "Level-2A",
  "PRODUCT_TYPE": "S2MSI2A",
  "PROCESSING_BASELINE": "05.10",
  "Special_Values": [{"SPECIAL_VALUE_TEXT": "NODATA", "SPECIAL_VALUE_INDEX": 0}, {"SPECIAL_VALUE_TEXT": "SATURATED", "SPECIAL_VALUE_INDEX": 65535}],
  "U": 1.03421885250175,
  "TILE_ID": "S2B_OPER_MSI_L2A_TL_2BPS_20240103T113848_A035655_T32TNS_N05.10",
  "SENSING_TIME": "2024-01-03T10:18:03.604815Z",
  "HORIZONTAL_CS_NAME": "WGS84 / UTM zone 32N",
  "HORIZONTAL_CS_CODE": "EPSG:32632"
}
```

with the members, in this order, each present only when its value is:

- `PRODUCT_URI`, `PROCESSING_LEVEL`, `PRODUCT_TYPE`, `PROCESSING_BASELINE`:
  the values of those children of `General_Info/Product_Info` in the product
  metadata. (`PROCESSING_BASELINE` is the string `"05.10"`, not a number:
  it is a version, but by §6.1 its text is a decimal number. As an
  exception, these four are always strings.)
- `Special_Values`: an array with one object per element
  `X/Special_Values`, in order, with the values of its children
  `SPECIAL_VALUE_TEXT` and `SPECIAL_VALUE_INDEX`, those present (and absent
  when there are more than 64 such elements; a product has 2);
- `U`: the value of `X/Reflectance_Conversion/U`, the Earth–Sun distance
  correction;
- `TILE_ID` and `SENSING_TIME`: the texts of those children of
  `General_Info` in the tile metadata, as strings;
- `HORIZONTAL_CS_NAME` and `HORIZONTAL_CS_CODE`: the texts of those
  children of `Tile_Geocoding`, as strings.

**`vzip_source`.** The group `vzip_source` (the source metadata node of
[conventions §2](../README.md#2-attributes)) is always present. Its source
metadata `S` is the object

```json
{"xml": {...}, "xml_arrays": [...], "empty": [...], "empty_dirs": [...], "ignored": [...]}
```

where each member is present only when it is not empty:

- `xml`: one member per XML document kept as **text** (below), in ascending
  order of keys: its key, and its **text value** (conventions §6: the JSON
  string of its bytes when they are UTF-8, else `{"latin1": T}`).
- `xml_arrays`: the keys of the other XML documents, in ascending order of
  keys. The `i`-th (from 0) is the array `vzip_source/xml/<i>`: its bytes as
  an array of bytes ([conventions §7](../README.md#7-source-values-as-arrays),
  **Bytes**), with no attributes. (Such are the tile metadata,
  190–630 KB, and the datastrip metadata `MTD_DS.xml`, 15–25 MB.)
- `empty`: the keys of the empty objects, ascending;
- `empty_dirs`: the empty directories (§2.1), ascending, each without a
  final `/`;
- `ignored`: the keys of the objects that the listing recorded but did
  not use (VIRTUALIZE.md §1.4, and for a zip file its directory entries
  that are recorded likewise, [profiles/safe.md §12.4](../../profiles/safe.md#124-the-zip-form)),
  ascending.

**Text or array.** An XML document of more than 65536 bytes is an array.
The others, the **candidates**, are taken in ascending order of size, and
of keys for equal sizes, starting from the state in which every XML
document is an array. A candidate becomes text when `S`, with it as text
and with the candidates after it still arrays, is at most 65536 bytes of
JSON; otherwise it stays an array, and the candidates after it are still
taken. The **size** of `S`'s JSON is measured as the ND2 convention
measures its budgets: the length in bytes of the UTF-8 encoding of the
text that ECMAScript's `JSON.stringify` writes for it, without whitespace,
with its string escapes (`\"`, `\\`, `\b`, `\f`, `\n`, `\r`, `\t`, and
`\u00XX` with lowercase hex digits for the other code points below
U+0020). Moving a document from `xml_arrays` to `xml` only makes `S` larger,
so the result does not depend on how a producer computes it (nor on
whether it reads a candidate that cannot fit, profiles/safe.md §12.2).
`empty`, `empty_dirs` and `ignored` count in the size but never move: a
product whose listing records many keys may keep every XML document as an
array. (So the budget bounds
`vzip_source/zarr.json` to about 64 KiB, its declaration included, whenever
the product's listing is ordinary. The order of sizes keeps as many
documents as text as the budget allows: the XML schemas and quality reports
of a few kB each, before the 45–70 KB product metadata and manifest, which
then usually stay arrays.)

It also holds:

- `vzip_source/jp2/<group>/<band>`, for every band array `<group>/<band>`:
  the bytes of its band file before the codestream (the JP2 signature, file
  type and header boxes, the GML-in-JP2 georeferencing, any other boxes,
  and the header of the contiguous codestream box `jp2c`), as an array of
  bytes (conventions §7, **Bytes**), with no attributes. The groups
  `vzip_source/jp2` and `vzip_source/jp2/<group>` have no attributes.
- every other object, kept whole as the
  [Zarr v2 convention §5](../zarr2/README.md#5-other-objects) keeps one: the
  archive's key `vzip_source/objects/<k>`, with `k` the object's key
  escaped as it says, holds the object's bytes. The directories between
  `vzip_source` and the objects are not nodes.

The groups `vzip_source/xml` and `vzip_source/jp2` exist only when they
have arrays.

### 6.4 Reconstruction

The product can be rebuilt from the hierarchy:

- each XML document and each other object, from `vzip_source` (its text
  value is its bytes; an array holds its bytes; an object is kept whole);
- each empty directory, from `empty_dirs` (every other directory is one of
  an object's);
- each band file: the bytes of `vzip_source/jp2/<group>/<band>`, then the
  codestream. The codestream is `FF 4F`, then the segment `siz`, then the
  main header after the SIZ segment of any chunk, then, for each tile `t` in
  raster order, the tile-part of the chunk's tile 0 with its `Isot` set to
  `t`, and then `FF D9`.

The band file's `jp2c` box ends at its end (the profile requires it), and
each tile has one tile-part with `TPsot = 0` and `TNsot = 1`, so nothing
else is needed. What is not kept is layout. That is the order of a
directory listing, the ZIP container (its headers, the timestamps of its
entries and its directory entries), the mirror's own metadata (generations
and storage class) and its folder markers (§2.1), and, for an XML document
of a zip file that was deflate-compressed, its compressed bytes. (A folder
marker's 6 bytes are not kept, but its directory is.)

## 7. Example

The resolution group `r20m` of the Level-2A product
`https://storage.googleapis.com/gcp-public-data-sentinel-2/L2/tiles/32/T/NS/S2B_MSIL2A_20240103T101329_N0510_R022_T32TNS_20240103T113848.SAFE/`:

```json
{
  "zarr_format": 3,
  "node_type": "group",
  "attributes": {
    "zarr_conventions": [
      {"uuid": "f17cb550-5864-4468-aeb7-f3180cfb622f", "schema_url": "https://raw.githubusercontent.com/zarr-conventions/proj/refs/tags/v0.1/schema.json", "spec_url": "https://github.com/zarr-conventions/proj/blob/v0.1/README.md", "name": "proj", "description": "Coordinate reference system information for geospatial data"},
      {"uuid": "689b58e2-cf7b-45e0-9fff-9cfc0883d6b4", "schema_url": "https://raw.githubusercontent.com/zarr-conventions/spatial/refs/tags/v0.1/schema.json", "spec_url": "https://github.com/zarr-conventions/spatial/blob/v0.1/README.md", "name": "spatial", "description": "Spatial coordinate information"}
    ],
    "proj:code": "EPSG:32632",
    "proj:wkt2": "PROJCRS[\"WGS 84 / UTM zone 32N\",BASEGEOGCRS[…]]",
    "spatial:dimensions": ["y", "x"],
    "spatial:transform": [20.0, 0.0, 499980.0, 0.0, -20.0, 5200020.0],
    "spatial:shape": [5490, 5490],
    "spatial:bbox": [499980.0, 5090220.0, 609780.0, 5200020.0],
    "spatial:registration": "pixel"
  }
}
```

(with `proj:wkt2` shortened). Its array `B05`, from `T32TNS_20240103T101329_B05_20m.jp2` (5490 × 5490,
15-bit, tiles of 640 × 640: 9 × 9 tiles, the last row and column 370
pixels):

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": [5490, 5490],
  "data_type": "uint16",
  "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [640, 640]}},
  "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
  "fill_value": 0,
  "codecs": [{"name": "imagecodecs_jpeg2k"}],
  "dimension_names": ["y", "x"],
  "attributes": {
    "zarr_conventions": [{"uuid": "ef81346c-19e8-42ad-93b0-a279ccaf44c1", "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/heads/main/conventions/safe/schema.json", "spec_url": "https://github.com/d-v-b/vzip/blob/main/conventions/safe/README.md", "name": "vzip_virtualized", "description": "The Zarr layout of a Sentinel-2 SAFE source virtualized by vzip, and the source's metadata"}],
    "vzip_virtualized": {"safe": {
      "IMAGE_FILE": "GRANULE/L2A_T32TNS_A035655_20240103T101328/IMG_DATA/R20m/T32TNS_20240103T101329_B05_20m",
      "bandId": 4, "physicalBand": "B5", "RESOLUTION": 20,
      "Wavelength": {"MIN": {"value": 694, "unit": "nm"}, "MAX": {"value": 714, "unit": "nm"}, "CENTRAL": {"value": 703.8, "unit": "nm"}},
      "PHYSICAL_GAINS": 5.17211547,
      "SOLAR_IRRADIANCE": {"value": 1425.78, "unit": "W/m²/µm"},
      "BOA_QUANTIFICATION_VALUE": {"value": 10000, "unit": "none"},
      "BOA_ADD_OFFSET": -1000,
      "siz": "/1EAKQAAAAAVcgAAFXIAAAAAAAAAAAAAAoAAAAKAAAAAAAAAAAAAAQ4BAQ=="
    }}
  }
}
```

The chunk `r20m/B05/c/8/8` is the codestream of tile 80, whose area is
`[5120, 5490) × [5120, 5490)`. Its image area is `[5120, 5760)²`, its tiles
are 370 × 370, and its tiles 1, 2 and 3 hold empty packets.
