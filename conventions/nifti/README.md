# The NIfTI convention

The Zarr layout of a NIfTI-1 or NIfTI-2 single file (`.nii`), and the
translation of its header into JSON. What all of vzip's conventions share is
in [conventions/README.md](../README.md), cited here as "conventions §n".
How vzip produces this layout as a virtual store, and which files it
accepts, is the NIfTI profile, [profiles/nifti.md](../../profiles/nifti.md).

Convention version: 1 · UUID: `06e5809d-4d54-4b72-afd0-6bf61a7b4c85` ·
Schema: [schema.json](schema.json)

## 1. Declaration

The root declares the convention by [conventions §2](../README.md#2-attributes),
with `"profile": "nifti"`, `"version": 1`, the file's URL as `source.url`,
and the source metadata of §5 as the member `"nifti"`. Its CMO is:

```json
{
  "uuid": "06e5809d-4d54-4b72-afd0-6bf61a7b4c85",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/tags/virtualize-nifti-v1/conventions/nifti/schema.json",
  "spec_url": "https://github.com/d-v-b/vzip/blob/virtualize-nifti-v1/conventions/nifti/README.md",
  "name": "vzip_virtualized",
  "description": "The Zarr layout of a NIfTI source virtualized by vzip, and the source's metadata"
}
```

No other node declares it: the array has no source metadata.

## 2. The header

The file's **header** is its first `L` bytes, `L = 348` for NIfTI-1 and
`L = 540` for NIfTI-2, in the file's byte order (the one in which
`sizeof_hdr` reads as `L`). Its fields, in the order of the file, are those
of the NIfTI-1 and NIfTI-2 standards (`nifti1.h`, `nifti2.h`), at these byte
offsets. `i16`, `i32` and `i64` are two's complement integers, `u8` an
unsigned byte, `f32` and `f64` IEEE 754 binary32 and binary64 numbers (an
`f32` value is converted to binary64 exactly), and `char[k]` `k` bytes of
text. A field `T[k]` is `k` consecutive values, indexed from 0.

NIfTI-1:

| field | offset | type |
|---|---|---|
| `sizeof_hdr` | 0 | `i32` |
| `data_type` | 4 | `char[10]` |
| `db_name` | 14 | `char[18]` |
| `extents` | 32 | `i32` |
| `session_error` | 36 | `i16` |
| `regular` | 38 | `char[1]` |
| `dim_info` | 39 | `u8` |
| `dim` | 40 | `i16[8]` |
| `intent_p1` | 56 | `f32` |
| `intent_p2` | 60 | `f32` |
| `intent_p3` | 64 | `f32` |
| `intent_code` | 68 | `i16` |
| `datatype` | 70 | `i16` |
| `bitpix` | 72 | `i16` |
| `slice_start` | 74 | `i16` |
| `pixdim` | 76 | `f32[8]` |
| `vox_offset` | 108 | `f32` |
| `scl_slope` | 112 | `f32` |
| `scl_inter` | 116 | `f32` |
| `slice_end` | 120 | `i16` |
| `slice_code` | 122 | `u8` |
| `xyzt_units` | 123 | `u8` |
| `cal_max` | 124 | `f32` |
| `cal_min` | 128 | `f32` |
| `slice_duration` | 132 | `f32` |
| `toffset` | 136 | `f32` |
| `glmax` | 140 | `i32` |
| `glmin` | 144 | `i32` |
| `descrip` | 148 | `char[80]` |
| `aux_file` | 228 | `char[24]` |
| `qform_code` | 252 | `i16` |
| `sform_code` | 254 | `i16` |
| `quatern_b` | 256 | `f32` |
| `quatern_c` | 260 | `f32` |
| `quatern_d` | 264 | `f32` |
| `qoffset_x` | 268 | `f32` |
| `qoffset_y` | 272 | `f32` |
| `qoffset_z` | 276 | `f32` |
| `srow_x` | 280 | `f32[4]` |
| `srow_y` | 296 | `f32[4]` |
| `srow_z` | 312 | `f32[4]` |
| `intent_name` | 328 | `char[16]` |
| `magic` | 344 | `char[4]` |
NIfTI-2:

| field | offset | type |
|---|---|---|
| `sizeof_hdr` | 0 | `i32` |
| `magic` | 4 | `char[8]` |
| `datatype` | 12 | `i16` |
| `bitpix` | 14 | `i16` |
| `dim` | 16 | `i64[8]` |
| `intent_p1` | 80 | `f64` |
| `intent_p2` | 88 | `f64` |
| `intent_p3` | 96 | `f64` |
| `pixdim` | 104 | `f64[8]` |
| `vox_offset` | 168 | `i64` |
| `scl_slope` | 176 | `f64` |
| `scl_inter` | 184 | `f64` |
| `cal_max` | 192 | `f64` |
| `cal_min` | 200 | `f64` |
| `slice_duration` | 208 | `f64` |
| `toffset` | 216 | `f64` |
| `slice_start` | 224 | `i64` |
| `slice_end` | 232 | `i64` |
| `descrip` | 240 | `char[80]` |
| `aux_file` | 320 | `char[24]` |
| `qform_code` | 344 | `i32` |
| `sform_code` | 348 | `i32` |
| `quatern_b` | 352 | `f64` |
| `quatern_c` | 360 | `f64` |
| `quatern_d` | 368 | `f64` |
| `qoffset_x` | 376 | `f64` |
| `qoffset_y` | 384 | `f64` |
| `qoffset_z` | 392 | `f64` |
| `srow_x` | 400 | `f64[4]` |
| `srow_y` | 432 | `f64[4]` |
| `srow_z` | 464 | `f64[4]` |
| `slice_code` | 496 | `i32` |
| `xyzt_units` | 500 | `i32` |
| `intent_code` | 504 | `i32` |
| `intent_name` | 508 | `char[16]` |
| `dim_info` | 524 | `u8` |
| `unused_str` | 525 | `char[15]` |
`n = dim[0]` is the number of dimensions. The **sizes** `X`, `Y`, `Z`, `T`,
`C` are `dim[1]` to `dim[5]`, each 1 when its index is more than `n`;
dimensions 6 and 7 have size 1. `v` is `vox_offset` as an integer.

## 3. Data types

| `datatype` | name | Zarr data type | bytes per voxel `b` | samples per voxel |
|---|---|---|---|---|
| 2 | UINT8 | `uint8` | 1 | 1 |
| 4 | INT16 | `int16` | 2 | 1 |
| 8 | INT32 | `int32` | 4 | 1 |
| 16 | FLOAT32 | `float32` | 4 | 1 |
| 64 | FLOAT64 | `float64` | 8 | 1 |
| 256 | INT8 | `int8` | 1 | 1 |
| 512 | UINT16 | `uint16` | 2 | 1 |
| 768 | UINT32 | `uint32` | 4 | 1 |
| 1024 | INT64 | `int64` | 8 | 1 |
| 1280 | UINT64 | `uint64` | 8 | 1 |
| 128 | RGB24 | `uint8` | 3 | 3 |
| 2304 | RGBA32 | `uint8` | 4 | 4 |

RGB24 and RGBA32 are the **color** data types: each voxel is its samples
(red, green, blue and, for RGBA32, alpha) in consecutive bytes. The file's
voxel data is `X × Y × Z × T × C × b` bytes at offset `v`, `x` varying
fastest. A **slice** is the `Y × X` voxels of one `(z, t, k)`, `k` the
index along dimension 5, and a **row** is `R = X × b` bytes.

## 4. The image

The hierarchy is one image at the root ([conventions §4](../README.md#4-images)),
without a name, with one array at path `"0"`: the root `zarr.json` is
`{"zarr_format": 3, "node_type": "group", "attributes": A}`, where `A` has
`ome` (`M`, below), `zarr_conventions` and `vzip_virtualized` (§1).

### 4.1 The array

- **Axes:** `t` if `n ≥ 4`; `c` if `n ≥ 5` or the data type is a color
  type; `z` if `n ≥ 3`; then `y` and `x`. (The axes follow `n`, so a
  dimension of size 1 up to `n` still has its axis.)
- **Shape:** `T`, `C` (the samples per voxel for a color type), `Z`, `Y`
  and `X`, each only for the axes present.
- **Data type:** from §3.
- **Row blocks.** Each slice is split into blocks of `h` rows, where `h` is
  the largest divisor of `Y` with `h × R ≤ 131072` (2^17 bytes), or 1 if
  there is none (`R > 131072`). A slice of at most 128 KiB is a single
  block, and a larger one is cut into equal blocks of at most 128 KiB as
  far as the divisors of `Y` allow.
- **Chunk shape:** 1 for `t` and `z`; 1 for `c`, or the samples per voxel
  for a color type; `h` for `y`; `X` for `x`.
- **Codecs** ([conventions §3](../README.md#3-arrays)): `transpose` for a
  color type (its samples are interleaved), then `bytes`, with `"endian"`
  (`"little"` or `"big"`: the file's byte order) when the data type is
  larger than 1 byte. There is no compressor.
- **Chunks:** every chunk is present. The chunk with coords `t`, `k` (0 for
  a color type), `z` (each only for the axes present), `j` and 0 is block
  `j` of slice `(z, t, k)`: rows `j × h` to `(j + 1) × h − 1` of it, as the
  file stores them.

The array holds the voxels' raw values. NIfTI's value scaling is not
applied; it is recorded in the source metadata (§5).

### 4.2 Value scaling

For a data type that is not a color type, the **scaling** applies when
`scl_slope` is finite and not 0; its slope `s` is `scl_slope`, and its
intercept `i` is `scl_inter` if that is finite, else 0. (A color type, or
a `scl_slope` of 0, ±∞ or NaN, has no scaling.) The scaling is
**nontrivial** when `s ≠ 1` or `i ≠ 0`. The scaled value of a voxel is
`s × raw + i`.

### 4.3 Scales, units and position

**Units.** The spatial unit is given by `xyzt_units` modulo 8 (its bits
0–2): 1 `meter`, 2 `millimeter`, 3 `micrometer`; any other value gives no
unit. The time unit is given by `xyzt_units` bitwise-and 56 (its bits 3–5):
8 `second`, 16 `millisecond`, 24 `microsecond`; any other value (including
Hz, ppm and rad/s) gives no unit. For NIfTI-2 the bits are those of the
`i32` in two's complement.

**Affine.** The image's **affine** is the sform if `sform_code > 0`, else
the qform if `qform_code > 0`, else there is none. (A code of 0 or less is
unset. A set sform is used even when it is not representable; the qform is
then not considered.) OME-NGFF 0.5 places an image only by a scale and a
translation per axis, so an affine is used only when it is
**representable**: an axis-aligned map with a positive diagonal.

- The sform is representable when its 12 values are finite, the six values
  `srow_x[1]`, `srow_x[2]`, `srow_y[0]`, `srow_y[2]`, `srow_z[0]` and
  `srow_z[1]` are 0, and `srow_x[0]`, `srow_y[1]` and `srow_z[2]` are
  positive. Its diagonal is `(srow_x[0], srow_y[1], srow_z[2])` and its
  offset `(srow_x[3], srow_y[3], srow_z[3])`.
- The qform is representable when `quatern_b`, `quatern_c` and `quatern_d`
  are all 0 (the rotation is the identity), `pixdim[0]` is not negative
  (`qfac` is 1; a NaN `pixdim[0]` is not negative), `pixdim[1]`,
  `pixdim[2]` and `pixdim[3]` are finite and positive, and `qoffset_x`,
  `qoffset_y` and `qoffset_z` are finite. Its diagonal is `(pixdim[1],
  pixdim[2], pixdim[3])` and its offset `(qoffset_x, qoffset_y, qoffset_z)`.

An affine that flips or rotates an axis, such as the common radiological
sform with a negative `srow_x[0]`, is not representable: the image then has
no translation, and its scales come from `pixdim`. The affine itself is
always in the source metadata (§5).

**Scales.** A number is **valid** if it is finite and positive.

- `x`, `y`, `z`: with a representable affine, the first, second and third
  values of its diagonal; otherwise `pixdim[1]`, `pixdim[2]` and
  `pixdim[3]` where valid, and 1 where not. The spatial unit, if any, is
  the unit of each of these axes whose scale is a value of the diagonal or
  a valid `pixdim`; an axis with scale 1 for an invalid `pixdim` has no
  unit.
- `t`: `pixdim[4]` with the time unit, if it is valid; else 1 with no unit.
- `c`: 1, with no unit.

**Translation.** With a representable affine, the image has a translation
([conventions §5](../README.md#5-units)): the first, second and third values
of its offset for `x`, `y` and `z`, and 0 for `t` and `c` (each only for the
axes present). Otherwise it has none.

### 4.4 Channels

`M` has an `"omero"` member in these two cases, and in no other:

- **Color:** `{"channels": [...]}` with one object per sample,
  `{"label": l, "color": k, "active": true, "window": {"min": 0, "max": 255,
  "start": 0, "end": 255}}`, where `(l, k)` are `("R", "FF0000")`,
  `("G", "00FF00")`, `("B", "0000FF")` and, for RGBA32, `("A", "FFFFFF")`.
- **Display range:** another data type, with `cal_min` and `cal_max`
  finite and `cal_max > cal_min`. The window in raw values is
  `lo = cal_min` and `hi = cal_max`; when the scaling applies (§4.2), `lo`
  and `hi` are instead the smaller and the larger of `(cal_min − i) / s`
  and `(cal_max − i) / s`. `M` has `{"channels": [...]}` with one object
  per index `k < C` of dimension 5 (one object when `n < 5`):
  `{"label": "C<k>", "color": "FFFFFF", "active": true, "window": {"min":
  lo, "max": hi, "start": lo, "end": hi}}`, with `k` in decimal.

## 5. Source metadata

The root's source metadata `S` ([conventions §2](../README.md#2-attributes))
is an object with these members, in this order, translated by
[conventions §6](../README.md#6-source-metadata-as-json):

- `nifti_version`: 1 or 2.
- `byte_order`: `"little"` or `"big"`.
- `header`: an object with one member per field of §2 for the file's
  version, in the order of §2 and named as there. A `char[k]` field is
  text; every other field is a number, or an array of `k` numbers for
  `T[k]`. (For example, `magic` is `"n+1"` or `"n+2"`, the bytes before
  the first NUL.)
- `extensions`: present when the first byte of the 4-byte extender at
  offset `L` is not 0. An array of the header extensions, in file order,
  each `{"ecode": c, "edata": d}`, where `c` is the extension's `ecode`
  and `d` its `esize − 8` bytes of data as base64. The extensions form a
  chain: the first starts at `q = L + 4`; while `q + 8 ≤ v`, the extension
  at `q` has `esize` (`i32`) at `q` and `ecode` (`i32`) at `q + 4`, and the
  next starts at `q + esize`. The chain stops, without the extension at
  `q`, when `esize < 8`, when `q + esize > v`, or when its data and that of
  the extensions before it would exceed 2^24 bytes (16 MiB); in these
  three cases `extensions_truncated` is present. (The standard's rule that
  `esize` is a multiple of 16 is not checked.)
- `extensions_truncated`: `true`, only as above.
- `scaling`: `{"slope": s, "inter": i}`, present when the scaling is
  nontrivial (§4.2).

`header` holds every field as stored, including those the layout does not
use, so that a reader who needs, say, the slice timing (`slice_code`,
`slice_duration`), the intent, or the full affine of a rotated image finds
it here. `scaling` is derived from `header`; it is there so that a reader
need not repeat the rules of §4.2.

## 6. Example

A big-endian NIfTI-1 file at `https://example.org/brain.nii` holds an int16
`4 × 3 × 2 × 5` volume, a radiological sform (so the image has no
translation), and a slope of 0.5 with an intercept of −20. Its root
`zarr.json` has these attributes (`M` elided):

```json
{
  "ome": M,
  "zarr_conventions": [{
    "uuid": "06e5809d-4d54-4b72-afd0-6bf61a7b4c85",
    "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/tags/virtualize-nifti-v1/conventions/nifti/schema.json",
    "spec_url": "https://github.com/d-v-b/vzip/blob/virtualize-nifti-v1/conventions/nifti/README.md",
    "name": "vzip_virtualized",
    "description": "The Zarr layout of a NIfTI source virtualized by vzip, and the source's metadata"
  }],
  "vzip_virtualized": {
    "profile": "nifti",
    "version": 1,
    "source": {"url": "https://example.org/brain.nii"},
    "nifti": {
      "nifti_version": 1,
      "byte_order": "big",
      "header": {
        "sizeof_hdr": 348, "data_type": "", "db_name": "", "extents": 0,
        "session_error": 0, "regular": "", "dim_info": 0,
        "dim": [4, 4, 3, 2, 5, 1, 1, 1],
        "intent_p1": 0.0, "intent_p2": 0.0, "intent_p3": 0.0, "intent_code": 0,
        "datatype": 4, "bitpix": 16, "slice_start": 0,
        "pixdim": [-1.0, 1.5, 1.5, 3.0, 2.5, 1.0, 1.0, 1.0],
        "vox_offset": 352.0, "scl_slope": 0.5, "scl_inter": -20.0,
        "slice_end": 0, "slice_code": 0, "xyzt_units": 10,
        "cal_max": 10.0, "cal_min": -30.0, "slice_duration": 0.0, "toffset": 0.0,
        "glmax": 0, "glmin": 0, "descrip": "", "aux_file": "",
        "qform_code": 0, "sform_code": 1,
        "quatern_b": 0.0, "quatern_c": 0.0, "quatern_d": 0.0,
        "qoffset_x": 0.0, "qoffset_y": 0.0, "qoffset_z": 0.0,
        "srow_x": [-1.5, 0.0, 0.0, 90.0],
        "srow_y": [0.0, 1.5, 0.0, -126.0],
        "srow_z": [0.0, 0.0, 3.0, -72.0],
        "intent_name": "", "magic": "n+1"
      },
      "scaling": {"slope": 0.5, "inter": -20.0}
    }
  }
}
```

A NIfTI-2 file with one comment extension (`ecode` 6) has, after `header`:

```json
"extensions": [{"ecode": 6, "edata": "d3JpdHRlbiBieSB3cml0ZV9maXh0dXJlcy5weQAAAAAAAAAAAAAAAA=="}]
```
