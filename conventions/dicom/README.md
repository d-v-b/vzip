# The DICOM convention

The Zarr layout of a DICOM Part 10 file (PS3.10) with native or JPEG or
JPEG 2000 encapsulated pixel data, including whole-slide images, and the
translation of its File Meta Information and dataset into the DICOM JSON
Model. What all of vzip's conventions share is in
[conventions/README.md](../README.md), cited here as "conventions §n". How
vzip produces this layout as a virtual store is the DICOM profile,
[profiles/dicom.md](../../profiles/dicom.md).

Convention version: 1 · UUID: `acf17198-e5a5-48d3-8187-22ec4bb40ea5` ·
Schema: [schema.json](schema.json)

This convention gives a layout only to the files that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a file that fails has no layout under this
convention, and the profile rejects it.

## 1. Declaration

The root declares the convention by [conventions §2](../README.md#2-attributes),
with `"profile": "dicom"`, `"version": 1`, the file's URL as `source.url`,
and the source metadata of §5 as the member `"dicom"`. Its CMO is:

```json
{
  "uuid": "acf17198-e5a5-48d3-8187-22ec4bb40ea5",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/tags/virtualize-dicom-v1/conventions/dicom/schema.json",
  "spec_url": "https://github.com/d-v-b/vzip/blob/virtualize-dicom-v1/conventions/dicom/README.md",
  "name": "vzip_virtualized",
  "description": "The Zarr layout of a DICOM source virtualized by vzip, and the source's metadata"
}
```

No other node declares it: the array has no source metadata.

## 2. The source

The input is one DICOM Part 10 file (PS3.10): a 128-byte preamble, `DICM`,
the File Meta Information, then a dataset whose last element read is Pixel
Data. One file is one image with one pyramid level; a whole-slide image's
other levels are other files.


### 2.1 Transfer syntax

The File Meta Information is the file's group `0002` elements, from offset
132; the top-level dataset follows it and runs until Pixel Data
`(7FE0,0010)`. ([profiles/dicom.md §6.2](../../profiles/dicom.md#62-file)
says how they are read.)

- **Transfer syntax:** the meta element `(0002,0010)` (Transfer Syntax UID)
  is required, and its VR MUST be `UI`. Its value, a string (§2.2), selects
  the dataset's encoding and the pixel data's form, and MUST be one of:

  | UID | name | encoding | pixel data |
  |---|---|---|---|
  | `1.2.840.10008.1.2` | Implicit VR Little Endian | implicit, little | native |
  | `1.2.840.10008.1.2.1` | Explicit VR Little Endian | explicit, little | native |
  | `1.2.840.10008.1.2.2` | Explicit VR Big Endian (retired) | explicit, big | native |
  | `1.2.840.10008.1.2.4.50` | JPEG Baseline (Process 1) | explicit, little | encapsulated, `jpeg` |
  | `1.2.840.10008.1.2.4.90` | JPEG 2000 (lossless only) | explicit, little | encapsulated, `jpeg2k` |
  | `1.2.840.10008.1.2.4.91` | JPEG 2000 | explicit, little | encapsulated, `jpeg2k` |

  Any other value, including Deflated Explicit VR Little Endian, RLE
  Lossless, the other JPEG processes, JPEG-LS and HTJ2K, rejects the input:
  their frames are not a single stream that a codec of [conventions §3](../README.md#3-arrays) decodes.

### 2.2 Attributes

The attributes read are these. Those marked **(FG)** are read from two
places: the first item of `(0028,9110)` (Pixel Measures Sequence) in the
first item of `(5200,9229)` (Shared Functional Groups Sequence) of the
top-level dataset, and the top-level dataset itself. All others are read
only from the top-level dataset. An element with length 0 counts as absent.
Attributes elsewhere (in other items, or deeper) are not read.

| tag | name | VR | kind |
|---|---|---|---|
| `(0008,0016)` | SOP Class UID | UI | string |
| `(0018,0088)` | Spacing Between Slices (FG) | DS | decimals |
| `(0020,9311)` | Dimension Organization Type | CS | string |
| `(0028,0002)` | Samples per Pixel | US | integer |
| `(0028,0004)` | Photometric Interpretation | CS | string |
| `(0028,0006)` | Planar Configuration | US | integer |
| `(0028,0008)` | Number of Frames | IS | integer string |
| `(0028,0010)`, `(0028,0011)` | Rows, Columns | US | integer |
| `(0028,0030)` | Pixel Spacing (FG) | DS | decimals |
| `(0028,0100)` | Bits Allocated | US | integer |
| `(0028,0101)` | Bits Stored | US | integer |
| `(0028,0102)` | High Bit | US | integer |
| `(0028,0103)` | Pixel Representation | US | integer |
| `(0028,1050)`, `(0028,1051)` | Window Center, Window Width | DS | decimals |
| `(0028,1052)`, `(0028,1053)` | Rescale Intercept, Rescale Slope | DS | decimals |
| `(0048,0006)`, `(0048,0007)` | Total Pixel Matrix Columns, Rows | UL | integer |
| `(0048,0302)` | Number of Optical Paths | UL | integer |
| `(0048,0303)` | Total Pixel Matrix Focal Planes | UL | integer |
| `(7FE0,0001)` | Extended Offset Table | OV | 64-bit integers |
| `(7FE0,0002)` | Extended Offset Table Lengths | OV | 64-bit integers |
| `(7FE0,0010)` | Pixel Data | OB or OW | [profiles/dicom.md §6.5](../../profiles/dicom.md#65-frames) |

Every attribute of the table present where it is read is checked as below,
whether or not its value ends up in the output.

- **VR:** in explicit VR, the element's VR MUST be the table's, else the
  input is rejected. (In implicit VR there is none to check.)
- **Length:** an attribute of the table other than Pixel Data MUST NOT have
  an undefined length (in implicit VR, [profiles/dicom.md §6.3](../../profiles/dicom.md#63-datasets-and-sequences) walks such an element as a
  sequence; it is then rejected here).
- **integer:** the value is `u16` (US) or `u32` (UL) values in the dataset's
  byte order; its length MUST be a multiple of 2 (4), and the first value
  is used.
- **64-bit integers:** `u64` values in the dataset's byte order; the length
  MUST be a multiple of 8, and every value MUST be at most 2^53 − 1.
- **string:** the value's bytes, split at each `\` (byte `5C`) into string
  values, each with its leading and trailing bytes `20` (space) and `00`
  removed. The first is used, as bytes (compared byte for byte with the
  ASCII text given here).
- **integer string:** the first string value MUST be one or more digits
  ([VIRTUALIZE.md §1.3](../../VIRTUALIZE.md#13-arithmetic)), optionally preceded by `+` or `-`, and is read as a decimal
  integer, which for Number of Frames MUST be from 1 to 2^53 − 1. Otherwise
  the input is rejected.
- **decimals:** the string values. A value is **valid** if it matches
  `[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?` and its binary64
  value ([VIRTUALIZE.md §1.3](../../VIRTUALIZE.md#13-arithmetic)) is finite. Invalid values never reject the input; they make
  the attribute unusable where §4 says so.

An **(FG)** attribute is taken from the Pixel Measures item if it is present
there, else from the top-level dataset. (The choice is made by presence: an
unusable value in the item does not fall back to the top level.)

## 3. The image

- **Rows**, **Columns**, **Samples per Pixel**, **Photometric
  Interpretation**, **Bits Allocated**, **Bits Stored** and **Pixel
  Representation** are required. Rows and Columns MUST be at least 1.
- **Samples per Pixel** `S` MUST be 1 or 3. Planar Configuration is required
  when `S` is 3, and MUST then be 0 (interleaved) or 1 (planar); when `S` is
  1 it is not used (but is checked as §2.2 says).
- **Number of Frames** `N` defaults to 1.
- **Bits:** Bits Stored MUST be from 1 to Bits Allocated. High Bit, when
  present, MUST be Bits Stored − 1. Pixel Representation MUST be 0
  (unsigned) or 1 (signed).
- **Data type:** `uint` (Pixel Representation 0) or `int` (1), then Bits
  Allocated. Bits Allocated MUST be 8, 16 or 32 for native pixel data, 8
  for `jpeg`, and 8 or 16 for `jpeg2k`. For `jpeg`, Bits Stored MUST be 8 and
  Pixel Representation 0.
- **Cells:** native pixels are read as whole cells of Bits Allocated bits.
  When Bits Stored is less, the cells are not masked or sign-extended: the
  output equals the stored values only when the unused high bits are 0
  (unsigned) or copies of the sign bit (signed), as writers produce them.
- **Photometric Interpretation** (a string) and `S` MUST be one of:

  | pixel data | `S` = 1 | `S` = 3 |
  |---|---|---|
  | native | `MONOCHROME1`, `MONOCHROME2` | `RGB` |
  | `jpeg` | `MONOCHROME1`, `MONOCHROME2` | `RGB`, `YBR_FULL`, `YBR_FULL_422` |
  | `jpeg2k` | `MONOCHROME1`, `MONOCHROME2` | `RGB`, `YBR_ICT`, `YBR_RCT` |

  Anything else is rejected: `PALETTE COLOR` (its values are indices into a
  lookup table that no codec of [conventions §3](../README.md#3-arrays) applies), native `YBR_*` (YCbCr, or
  subsampled, as stored), and the combinations a JPEG or JPEG 2000 decoder
  does not turn into RGB. Every accepted three-sample image decodes to RGB.

**Whole-slide images.** The file is a **whole-slide image** if its SOP Class
UID is `1.2.840.10008.5.1.4.1.1.77.1.6` (VL Whole Slide Microscopy Image
Storage). Then:

- Dimension Organization Type MUST be `TILED_FULL` (`TILED_SPARSE`, whose
  frames are placed by per-frame positions, and an absent value are
  rejected);
- Total Pixel Matrix Columns `W` and Rows `H` are required and MUST be at
  least 1;
- Number of Optical Paths and Total Pixel Matrix Focal Planes default to 1
  and MUST be 1;
- the frames are tiles of `Rows × Columns` in row-major order: with
  `TC = ceil(W / Columns)` and `TR = ceil(H / Rows)`, `N` MUST equal
  `TR × TC`, and frame `f` is tile row `f div TC`, column `f mod TC`.

Otherwise the image is `Rows × Columns`, and frame `f` is plane `z = f`.

## 4. Output

One image at the archive root ([conventions §4](../README.md#4-images)), with no `name` and one level, the
array `"0"`.

- **Axes:** `c` if `S` = 3; `z` if the image is not a whole-slide image and
  `N > 1`; then `y`, `x`.
- **Shape:** 3 for `c`, `N` for `z`; then `H`, `W` for a whole-slide image,
  else Rows, Columns.
- **Chunk shape:** for `c`, 1 if the pixel data is native with Planar
  Configuration 1, else 3; 1 for `z`; then Rows, Columns.
- **Codecs** ([conventions §3](../README.md#3-arrays)): `transpose` when `c` is present and its chunk size is
  3 (interleaved, as every decoded JPEG and JPEG 2000 frame is); then
  `bytes` (with `endian` `little` or `big` by the encoding when Bits
  Allocated is above 8), `imagecodecs_jpeg` or `imagecodecs_jpeg2k`.
- **Scale and units:**
  - `y`, `x`: if Pixel Spacing (FG, §2.2) is **usable**, the scales are its
    first and second values (the spacing between rows, then between
    columns), with unit `millimeter`. It is usable if it has exactly two
    string values, both valid decimals and positive. Otherwise the scales
    are 1 with no unit.
  - `z`: if Spacing Between Slices (FG) has a first value that is a valid,
    positive decimal, that value with unit `millimeter`; otherwise 1 with
    no unit.
  - `c`: scale 1, no unit.
- **Translation:** none. A whole-slide image's origin is given in the slide
  coordinate system, whose axes are rotated or mirrored against the image's
  by Image Orientation (Slide); a cross-sectional image's position is in the
  patient's frame, oriented by Image Orientation (Patient). Neither is a
  translation along the image's own axes in general.
- **omero:** `M` has `"omero": {"channels": [...]}`, one object per channel
  (one when `S` = 1, even though there is no `c` axis):
  `{"label": ..., "color": ..., "active": true, "window": {"min": A, "max": Z, "start": a, "end": e}}`.
  - Labels and colors: `S` = 1 gives `gray`, `FFFFFF`; `S` = 3 gives `R`,
    `G`, `B` with `FF0000`, `00FF00`, `0000FF`. For `MONOCHROME1` the
    channel also has `"inverted": true`.
  - `A` and `Z` are the stored values' range for Bits Stored `b`: 0 and
    `2^b − 1` when unsigned, `−2^(b−1)` and `2^(b−1) − 1` when signed.
  - `a` and `e` are `A` and `Z`, except when `S` = 1 and there is a **VOI
    window**: Window Center and Window Width are present with valid first
    values `c` and `w`, `w ≥ 1`, and Rescale Slope `m` and Rescale Intercept
    `k` are each absent (taken as 1 and 0) or have a valid first value, with
    `m ≠ 0`. The window, in rescaled units, is converted to stored values:
    `a = (c − w / 2 − k) / m` and `e = (c + w / 2 − k) / m` (`w / 2` first,
    then left to right), swapped if `m < 0`.
- **Chunks:** frame `f` (or sample `s` of it) is the chunk at
  `0/c/<coords>`. Coords are: for `c`, the sample `s` when planar, else 0;
  for `z`, `f`; then the frame's tile row and column for a whole-slide
  image, else 0 and 0. Every chunk is present. A chunk holds the frame's
  pixel data: for native pixel data, its `Rows × Columns × S` cells (or one
  sample's, when planar); for encapsulated pixel data, the frame's
  fragments' data, concatenated, which is one JPEG or JPEG 2000 stream (a
  JPEG stream with three samples made explicit about its color transform,
  as [profiles/dicom.md §6.5](../../profiles/dicom.md#65-frames) says).

## 5. Source metadata

The root's source metadata `S` ([conventions §2](../README.md#2-attributes))
holds every attribute of the File Meta Information and of the dataset up
to Pixel Data, at every depth, including private attributes, so that the
patient, study, series, equipment, acquisition and per-frame information
the file records is kept. It is an object with two members, each a
dataset in the DICOM JSON Model (PS3.18 §F.2):

- `meta`: the File Meta Information;
- `dataset`: the top-level dataset, up to and including Pixel Data.

A dataset is a JSON object with one member per attribute, named by its tag
as eight uppercase hexadecimal digits (`"00280010"`), in ascending order of
tag. Of elements with the same tag in a dataset, the first is used; group
length elements (`gggg,0000`) are omitted, as PS3.18 §F.2 asks. An
attribute is `{"vr": VR}` with, when it has a value, `"Value"` or
`"InlineBinary"`:

- **Explicit VR.** `VR` is the element's. Its value is translated by VR:
  - `AE AS CS DA DS DT IS LO PN SH TM UC UI`: the value's bytes are split
    at each `\` (byte `5C`); `LT ST UT UR` are one value. Each value has
    its trailing bytes `20` and `00` removed and, except for `LT ST UT UC
    UR`, its leading bytes `20`; it is then text by
    [conventions §6](../README.md#6-source-metadata-as-json) (UTF-8 if
    valid, else ISO 8859-1; Specific Character Set is not applied, and is
    in the dataset for a reader who needs it). An empty value is `null`.
    A `DS` value that is a valid decimal (by the pattern of §2.2) with a
    finite binary64 value is that number, and an `IS` value of the form
    `[+-]?[0-9]+` is that integer (conventions §6); any other `DS` or `IS`
    value stays text. A `PN` value is split at its first two `=` into the
    `Alphabetic`, `Ideographic` and `Phonetic` groups, of which the
    non-empty ones are members of an object (`null` if none is).
  - `AT`: each 4 bytes, a `u16` group and a `u16` element in the dataset's
    byte order, as eight uppercase hexadecimal digits.
  - `FL FD SL SS UL US SV UV`: the values, numbers by conventions §6.
    Trailing bytes short of a whole value are ignored, as for `AT`.
  - `OB OD OF OL OV OW UN`: `"InlineBinary"`, the bytes in base64, as
    stored (in the dataset's byte order).
  - `SQ`: `"Value"`, an array of its items' datasets.
- **Implicit VR.** An element of defined length is `{"vr": "UN",
  "InlineBinary": ...}`, its bytes in base64: the file does not state its
  VR, and the data dictionary is not applied (a reader such as pydicom
  applies it to `UN` values). An element of undefined length is a
  sequence, `SQ`.
- An element of length 0 has no `"Value"`.
- A sequence of undefined length is `SQ` (also when its VR is `UN`, whose
  items are then in implicit VR little endian).
- **Pixel Data.** The top-level Pixel Data is `{"vr": VR}` (`"OW"` in
  implicit VR), without a value: its value is the chunks. Pixel Data
  nested in an item (an icon image) is `{"vr": VR}` (`"OB"` in implicit
  VR) too. Elements after the top-level Pixel Data are not read.

**Sequences that the profile does not walk.** The profile checks the
structure of every element it walks, which is every element of the
top-level dataset, of sequences of undefined length, and of the two
sequences of §2.2 ([profiles/dicom.md §6.3](../../profiles/dicom.md#63-datasets-and-sequences)).
The source metadata also walks the other sequences of defined length, by
the same rules. When one of them breaks a rule (an element runs past its
container, an unknown VR, items nested more than 64 deep, a tag other
than an item where one is expected), it is `{"vr": "SQ"}` without a value,
and it never rejects the input.

**Budget.** The values are taken in the order of the file (the File Meta
Information, then the dataset, each sequence's items where they occur),
and a value is read only when its length, added to the lengths of the
values read before it, is at most 2^26 bytes (64 MiB); otherwise the
attribute is `{"vr": VR}` without a value. A sequence without a value
(above) uses none of the budget, whatever its items held.

## 6. Example

The root of an explicit VR little endian secondary capture image with a
referenced image sequence and shared functional groups
(`https://example.org/a.dcm`):

```json
"vzip_virtualized": {
  "profile": "dicom",
  "version": 1,
  "source": {
    "url": "https://example.org/a.dcm"
  },
  "dicom": {
    "meta": {
      "00020001": {
        "vr": "OB",
        "InlineBinary": "AAE="
      },
      "00020002": {
        "vr": "UI",
        "Value": [
          "1.2.840.10008.5.1.4.1.1.7"
        ]
      },
      "00020003": {
        "vr": "UI",
        "Value": [
          "1.2.826.0.1.3680043.8.498.54895542129125825857839641277016018109"
        ]
      },
      "00020010": {
        "vr": "UI",
        "Value": [
          "1.2.840.10008.1.2.1"
        ]
      },
      "00020012": {
        "vr": "UI",
        "Value": [
          "1.2.826.0.1.3680043.8.498.1"
        ]
      },
      "00020013": {
        "vr": "SH",
        "Value": [
          "PYDICOM 3.0.2"
        ]
      }
    },
    "dataset": {
      "00080016": {
        "vr": "UI",
        "Value": [
          "1.2.840.10008.5.1.4.1.1.7"
        ]
      },
      "00080018": {
        "vr": "UI",
        "Value": [
          "1.2.826.0.1.3680043.8.498.54895542129125825857839641277016018109"
        ]
      },
      "00080060": {
        "vr": "CS",
        "Value": [
          "OT"
        ]
      },
      "00081110": {
        "vr": "SQ",
        "Value": [
          {}
        ]
      },
      "00081140": {
        "vr": "SQ",
        "Value": []
      },
      "00082112": {
        "vr": "SQ",
        "Value": [
          {
            "00081150": {
              "vr": "UI",
              "Value": [
                "1.2.840.10008.5.1.4.1.1.7"
              ]
            },
            "00081155": {
              "vr": "UI",
              "Value": [
                "1.2.826.0.1.3680043.8.498.72746325067103167969493458029025673857"
              ]
            },
            "0040A170": {
              "vr": "SQ",
              "Value": [
                {
                  "00080100": {
                    "vr": "SH",
                    "Value": [
                      "121327"
                    ]
                  },
                  "00080102": {
                    "vr": "SH",
                    "Value": [
                      "DCM"
                    ]
                  },
                  "00080104": {
                    "vr": "LO",
                    "Value": [
                      "Full fidelity image"
                    ]
                  }
                }
              ]
            }
          },
          {}
        ]
      },
      "00280002": {
        "vr": "US",
        "Value": [
          1
        ]
      },
      "00280004": {
        "vr": "CS",
        "Value": [
          "MONOCHROME2"
        ]
      },
      "00280010": {
        "vr": "US",
        "Value": [
          5
        ]
      },
      "00280011": {
        "vr": "US",
        "Value": [
          3
        ]
      },
      "00280030": {
        "vr": "DS",
        "Value": [
          9.0,
          9.0
        ]
      },
      "00280100": {
        "vr": "US",
        "Value": [
          8
        ]
      },
      "00280101": {
        "vr": "US",
        "Value": [
          8
        ]
      },
      "00280102": {
        "vr": "US",
        "Value": [
          7
        ]
      },
      "00280103": {
        "vr": "US",
        "Value": [
          0
        ]
      },
      "52009229": {
        "vr": "SQ",
        "Value": [
          {
            "00289110": {
              "vr": "SQ",
              "Value": [
                {
                  "00280030": {
                    "vr": "DS",
                    "Value": [
                      0.2,
                      0.3
                    ]
                  }
                }
              ]
            }
          }
        ]
      },
      "7FE00010": {
        "vr": "OB"
      }
    }
  }
}
```
