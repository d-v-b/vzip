# Mapping image formats to Zarr: techniques

vzip maps eleven source formats to Zarr v3 without copying their data:
- seven file formats: TIFF, NDPI, ND2, DICOM, NIfTI, IMS and CZI;
- three store formats: N5, Zarr v2 and OME-Zarr 0.4;
- Sentinel-2 SAFE products, a directory or a zip file of JPEG 2000 bands
  and XML metadata.

Doing the same thing eleven times shows which problems recur, and which
techniques solve them for any format. This document collects those
techniques. It then proposes, from them, how the conventions should
treat a source's metadata. It is a design note: the conventions in this
directory are normative, and this document is not.

## 1. Three kinds of information

Every source mixes three kinds of information, and the mapping treats
each kind differently.

1. **Data**: the pixels, and other arrays of values.
   - Data becomes Zarr chunks that reference the source's bytes in place.
2. **Layout**: where the bytes are and how to decode them.
   - Examples:
     - offsets and byte counts (TIFF `TileOffsets`, the ND2 chunk map,
       DICOM offset tables, HDF5 B-trees);
     - compression tables (`JPEGTables`, restart intervals);
     - padding, block headers, and the container's own versioning.
   - Layout is *consumed*: it determines the Zarr array metadata (shape,
     chunk grid, codecs) and the chunk references. It is not recorded
     anywhere else, because it means nothing on its own, and its JSON form
     is what makes metadata large (an ND2 file's 25–86 MB root documents
     were almost entirely per-frame binary blobs).
3. **Semantic metadata**: what the data means.
   - Examples: units, acquisition settings, channel names, instrument,
     patient or study, timestamps, positions.
   - It has a natural JSON form: text, numbers, and trees of them. It goes
     into attributes, translated by fixed rules so that independent
     producers agree.
   - When semantic metadata is a *stream of numbers* (one value per frame,
     a lookup table), it is data again, and becomes an array (§4).

The test for which kind a value is: would a scientist reading the
hierarchy want it, and can it be written as JSON without encoding bytes?
An ICC profile is meaningful but opaque. Its bytes stay in the source.

## 2. Chunks from the source's own units

A chunk is one unit that the source already encodes independently: a
tile, a strip, a frame, a block, or a chunk object. The Zarr chunk grid is
the source's grid. Most of the work is making each unit decode on its
own with a standard codec. The same few techniques recur:

| problem | technique | where |
|---|---|---|
| the unit omits shared headers (JPEG tables, color transform) | prepend a shared byte string, held once in the archive (a data source), to every reference | TIFF/SVS JPEG tiles, DICOM JPEG frames, NDPI, SAFE (a JPEG 2000 main header) |
| the unit is one tile of a codestream (a JPEG 2000 tile-part) | a standalone codestream per tile: a literal SIZ that places the tile where the file does on the reference grid, the shared main header, a literal SOT, and the tile-part's body | SAFE |
| the unit is smaller than the chunk at the edge | pad the codestream with empty tiles on the same reference grid (each a tile-part of empty packets, one per layer, component, resolution and precinct), so that it decodes to the chunk shape with any decoder | SAFE |
| the unit is too large to be a chunk (one JPEG strip per level) | cut it at the format's own resynchronization points (restart markers), and rewrite each piece's header as literal bytes | NDPI |
| rows have padding | one byte range per row, gathered into one chunk | ND2 |
| units carry a per-unit header | a codec that parses it, rather than layout tricks | N5 (`n5_default`) |
| units carry a few header bytes before a standard stream | a range that starts after them; the header's one meaningful bit becomes a codec | CZI (the Zstd1 header, whose hi-lo flag is `numcodecs.shuffle`) |
| the unit's bytes are a transform of the pixels a standard codec undoes (16-bit values split into low and high bytes) | that codec in the chain, before the compressor | CZI (`bytes`, `numcodecs.shuffle`, `zstd`) |
| the last column or row of units is narrower (a region clipped by the scan) | the zarr-extensions `rectilinear` chunk grid, a run of full chunks and one short one per axis | CZI pyramid levels |
| units lie at arbitrary positions, overlapping (stage tile scans) | one array per position, holding all its planes, and recording where it lies; repeated planes at one position in further arrays | CZI tiles |
| samples are interleaved, or the order is Fortran | a `transpose` codec | TIFF, ND2, NIfTI RGB, Zarr v2 `order: F` |
| byte order | the `bytes` codec's `endian` | everywhere |
| edge units are truncated or padded | a codec that tolerates it (N5), or stores matching Zarr's whole-edge rule (HDF5); chunks wholly outside the image are dropped | N5, IMS |
| units are missing | no entry: the chunk reads as the fill value | ND2 missing frames, HDF5 unallocated chunks, sparse Zarr |
| the source's key scheme | a chunk key encoding that reproduces it | Zarr v2 and N5 via the `v2` encoding |

A format whose units can't be made to decode independently (LZW strips,
predictors, multi-file series, TILED_SPARSE) is rejected rather than
approximated.

## 3. Axes, grids and coordinates

- **Find the logical grid, then map frames onto it.** Sources number their
  planes in their own order, and the mapping recovers the grid:
  - ND2 flattens a tree of loops;
  - OME-TIFF uses `TiffData` with `DimensionOrder`;
  - DICOM uses the Frame Increment Pointer;
  - Imaris uses `ResolutionLevel` / `TimePoint` / `Channel` groups.

  Positions that don't share a grid become separate images (the
  bioformats2raw layout).
- **Find the lattice, not just the grid.** Some sources hold tiles by
  position rather than by index (CZI). Tiles that sit on one lattice
  (equal tiles, without overlap, the last column and row possibly
  clipped) are one image level; tiles that overlap or sit off the lattice
  cannot be stitched without choosing a blend, so each position stays an
  array of its own, in the same pixel frame, and the choice is left to the
  reader.
- **Axis type matters as much as size.** Cine frames are `t`, not `z`. A
  NIfTI `dim` beyond 5 has no axis. An unknown loop type rejects the input.
- **Coordinates need care at three points.**
  - Scale and translation must be representable: an affine that rotates or
    flips is not, so it is recorded but not applied.
  - Positions given for the center of an image or a voxel need an offset:
    a stage center becomes a corner; a downsampled voxel is centered
    `(f − 1) / 2` source voxels in.
  - Index order may run against physical order, as with a negative z step.
- **Defaults are not data.** A unit a format assumes but the file never
  wrote (TIFF's ResolutionUnit, a 72-dpi resolution) is not a calibration.

## 4. Numeric metadata as arrays

Some semantic metadata is a column of numbers: frame times, stage
positions per frame, exposure per plane, lookup tables, per-frame focus
offsets. As JSON these are large and awkward. As Zarr arrays they are
compact, typed and sliceable, and they can be **aligned with the image**:
an array whose dimensions are the image's non-spatial axes, `(t, c, z)`,
holds one value per plane, so a reader slices it with the same indices
as the pixels.

Three sources of such arrays, by how the values are stored:

1. **A contiguous binary stream** in a layout the format defines (ND2
   `CustomData|AcqTimesCache!` and the X/Y/Z stage arrays as float64,
   long numeric TIFF tags, DICOM `OF`/`OD`/`OL`/`OV` elements): a virtual
   array whose chunk references the stream in place.
2. **Values scattered one per frame** (the timestamp at the head of each
   ND2 frame, one value per IFD): a reference can gather them, but reading
   it then costs one request per frame. Copying them into an inline,
   rechunked entry costs a few bytes per frame and one request. Use inline
   bytes, in a fixed encoding, so the output stays repeatable.
3. **Values derived from structured metadata** (z positions from OME-XML
   `Plane` elements, DICOM per-frame Image Position (Patient)): nothing to
   reference, so these are inline arrays computed by rules the convention
   states.

A frame-indexed stream is reshaped to the frame grid of §3. ND2's frame
`f` is row-major over its loops, so its `AcqTimesCache` becomes a
`(t, p, z)` array, aligned with the images.

## 5. Metadata that scales with the data

Metadata that grows with the number of frames, planes or channels
doesn't belong in the root's `zarr.json`, which every reader fetches
before any pixel. Examples:
- DICOM's Per-frame Functional Groups (one item per frame);
- an OME-TIFF's one IFD per plane;
- Imaris's per-channel groups;
- ND2's event list and per-frame metadata.

Those collections go on a separate node: a child group of the image,
which only a reader who wants them opens, holding the §4 arrays as well.
The root keeps the file-level description, which is small.

A source that is itself a tree (HDF5) is best mirrored: one Zarr group per
group, one array per dataset, attributes as JSON on the node they belong to.
No document then grows with the file, a reader opens only the part it
wants, and the mapping is easy to state and to invert. The one exception
is data the image already holds (Imaris's `Data` datasets): the mirror
names it instead of referencing its chunks twice.

## 6. Repeatable translation

The machinery that made eleven formats agree across two implementations:

- **One rule per value type.** Integers beyond 2^53 become strings. NaN
  and the infinities become strings. Text is UTF-8 if valid, else a tagged
  ISO 8859-1 reading.
- **One reading per format.** A generic walker over the format's own
  structure handles every case alike: TIFF tags, ND2 lite-variant trees,
  DICOM datasets, HDF5 attributes, NIfTI fields. It replaces a curated
  list of fields.
- **Budgets** bound the output. They must be counted in emitted bytes,
  and kept small once layout and streams are gone.
- **Source metadata never rejects.** What the layout needs is checked and
  rejects the input. What only the metadata reads falls back to absent
  or `null`, by a stated rule.
- **Verification on three axes:**
  - two implementations compared on every fixture, public corpus file and
    seeded mutant;
  - each compared with an independent reader of the format (tifffile,
    `nd2`, pydicom, nibabel, h5py);
  - schemas for every declaring node.

## 7. Proposal: the conventions' source metadata

From §1, §4 and §5:

| format | root `S` (small, file-level) | metadata node `S` | arrays |
|---|---|---|---|
| TIFF, NDPI | byte order, BigTIFF | each IFD's semantic tags: text tags (ImageDescription, Software, DateTime, Make, private text), numeric tags up to N values; not layout tags (offsets, counts, SubIFDs, EXIF/GPS pointers, JPEGTables) and not opaque BYTE/UNDEFINED values (ICC), except XMP (700) as text | numeric tags of more than N values (ColorMap, TransferFunction, private tables) |
| ND2 | the file-level lite-variant chunks: attributes, experiment, text info, calibration, picture metadata of frame 0 | events, the per-frame `ImageMetadataSeqLV\|n!` | the known `CustomData` streams (frame times, X/Y/Z, PFS, exposure), reshaped to the frame grid; frame timestamps inline when `AcqTimesCache` is absent; other binary chunks omitted |
| DICOM | the dataset without Per-frame Functional Groups and without binary-VR values | Per-frame Functional Groups | `OF`/`OD`/`OL`/`OV` values; optionally derived per-frame position and time arrays |
| NIfTI | the header; text extensions (comment, AFNI and CIFTI XML, JSON) as text; a derived orientation (which affine, whether it is applied) | none | none |
| IMS | none | the whole HDF5 tree, mirrored under `vzip_source/hdf5`: each group's attributes, links and named datatypes on its own group | every dataset but the image's (histograms, thumbnail, `DataSetTimes`, `Scene8` tables), by reference, typed when its elements are numbers; large attributes, copied |
| N5 | each node's `attributes.json` without its layout members (`dimensions`, `blockSize`, `dataType`, `compression`, `n5`) | none | none |
| Zarr v2, OME-Zarr | each node's `.zattrs` | none | none |
| CZI | the file header (version, GUIDs) | the attachment directory's entries (name, type, GUID, the form of their data), within a 64 KiB budget | the directory as copied typed columns, one per field and dimension; subblock metadata, attachments and unplaced data as families; the metadata XML, attachments (time stamps and focus positions typed, event lists as columns), unreferenced segments and the tail by reference |
| SAFE | the product's identity (a fixed set of fields, a derived convenience); per band, the values that interpret it (band id, wavelengths, gains, irradiance, quantification and offsets), recorded and not applied; GeoZarr `proj` and `spatial` on the resolution groups | the XML documents as text within a 64 KiB budget, and the keys of the rest | the XML documents past the budget, and each band file's JP2 header, as arrays of bytes; every other file kept whole |

## 8. Open questions

1. **The metadata node's name and place.**
   - Proposal: a child group `vzip_source` of the root.
   - It declares the convention, with the collections as its `S`, and
     holds the arrays.
   - OME-NGFF readers ignore children they don't know.
2. **Array naming:** names from the source (`AcqTimesCache`,
   `ifd/3/320`), or from meaning (`time`, `stage_x`)? Source names are
   repeatable without a dictionary; meaningful names need a table per
   format.
3. **Where an inline array is too large** (a gathered or derived array of
   millions of frames), the same rules apply, but the archive grows. Is
   there a limit?
4. **N**, the count above which a numeric tag becomes an array (proposed:
   64).
5. **Index order vs physical order** (ND2's negative z step):
   - option 1: flip the index so that z increases with position;
   - option 2: record the sign and keep the source order.
