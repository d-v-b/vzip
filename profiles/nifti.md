# Virtualizing NIfTI files

The NIfTI profile of [VIRTUALIZE.md](../VIRTUALIZE.md) (revision 11), numbered
as its §7. §1 and §2 are in VIRTUALIZE.md and apply here.

## 7. NIfTI profile

### 7.1 Header

The input is a NIfTI-1 or NIfTI-2 single file (`.nii`), which §1.2 chose by
its first bytes. Its **header** is the first `L` bytes of the file, `L = 348`
for NIfTI-1 and `L = 540` for NIfTI-2; a file shorter than `L` bytes is
rejected. The file is **little-endian** if bytes 0–3, read as a little-endian
32-bit integer, are `L`, and **big-endian** otherwise (§1.2 has checked that
one of the two holds). Every field below is read in that byte order.

These fields are read, at these byte offsets, and no others (intent, slice
timing, `dim_info`, `toffset`, `glmin`, `glmax`, `descrip`, `aux_file` and the
like are ignored). `i16`, `i32` and `i64` are two's complement integers, `u8`
an unsigned byte, and `f32` and `f64` IEEE 754 binary32 and binary64 numbers;
an `f32` value is converted to binary64 exactly. A field of `k` values is `k`
consecutive values, indexed from 0.

| field | NIfTI-1 | NIfTI-2 |
|---|---|---|
| `dim` (8 values) | 40, `i16` | 16, `i64` |
| `datatype` | 70, `i16` | 12, `i16` |
| `bitpix` | 72, `i16` | 14, `i16` |
| `pixdim` (8 values) | 76, `f32` | 104, `f64` |
| `vox_offset` | 108, `f32` | 168, `i64` |
| `scl_slope`, `scl_inter` | 112, 116, `f32` | 176, 184, `f64` |
| `xyzt_units` | 123, `u8` | 500, `i32` |
| `cal_max`, `cal_min` | 124, 128, `f32` | 192, 200, `f64` |
| `qform_code`, `sform_code` | 252, 254, `i16` | 344, 348, `i32` |
| `quatern_b`, `quatern_c`, `quatern_d` | 256, 260, 264, `f32` | 352, 360, 368, `f64` |
| `qoffset_x`, `qoffset_y`, `qoffset_z` | 268, 272, 276, `f32` | 376, 384, 392, `f64` |
| `srow_x`, `srow_y`, `srow_z` (4 values each) | 280, 296, 312, `f32` | 400, 432, 464, `f64` |

**Checks.** The input is rejected unless all of these hold:

- **Dimensions:** `n = dim[0]` is from 1 to 7, and `dim[i]` is from 1 to
  2^53 − 1 for every `1 ≤ i ≤ n`. The values `dim[i]` for `i > n` are not
  read: the size of every dimension `i > n` is 1. If `n ≥ 6`, `dim[6]` MUST
  be 1, and if `n = 7`, `dim[7]` MUST be 1 (the output has no axis for
  dimensions 6 and 7, so CIFTI-2 files, whose data runs along them, are
  rejected).
- **Data type:** `datatype` is in the table of §7.2, and `bitpix` equals
  the table's value for it.
- **Colour:** a colour data type (§7.2) has `n ≤ 4`.
- **Offset:** `vox_offset` is an integer `v` with `L + 4 ≤ v ≤ 2^53 − 1`.
  For NIfTI-1, `vox_offset` is a binary32 number, which MUST be finite and
  have an integral value. (The voxel data never starts inside the header or
  its 4-byte extender. The NIfTI-1 standard's advice that `vox_offset` be a
  multiple of 16 is not checked, and a `vox_offset` of 0, which some readers
  take to mean 352, is rejected.)
- **Data size:** with `X`, `Y`, `Z`, `T`, `C` the sizes of dimensions 1 to
  5 (`X = dim[1]`, and each is 1 when its index is more than `n`) and `b`
  the bytes per voxel of §7.2, the size of the voxel data,
  `B = X × Y × Z × T × C × b` computed exactly, is at most 2^53 − 1, and
  `v + B` is at most the file's size.

Every check applies whatever the other fields hold. No other field is
checked: values that §7.4–§7.6 use only change the output as those sections
say, and never reject the input (except by §1.3, in §7.6).

**Extensions.** The 4 bytes at offset `L` (the extender) and any extensions
after them are neither read nor checked, so their presence never changes
the output: the voxel data starts at `v` whatever they hold. (An
implementation MAY report the extender's first byte, which is nonzero when
extensions are present, in an informative summary.) A header-and-image pair
(magic `ni1` or `ni2`) and a gzipped file are rejected by §1.2.

### 7.2 Data types

| `datatype` | name | `bitpix` | Zarr data type | bytes per voxel `b` | samples per voxel |
|---|---|---|---|---|---|
| 2 | UINT8 | 8 | `uint8` | 1 | 1 |
| 4 | INT16 | 16 | `int16` | 2 | 1 |
| 8 | INT32 | 32 | `int32` | 4 | 1 |
| 16 | FLOAT32 | 32 | `float32` | 4 | 1 |
| 64 | FLOAT64 | 64 | `float64` | 8 | 1 |
| 256 | INT8 | 8 | `int8` | 1 | 1 |
| 512 | UINT16 | 16 | `uint16` | 2 | 1 |
| 768 | UINT32 | 32 | `uint32` | 4 | 1 |
| 1024 | INT64 | 64 | `int64` | 8 | 1 |
| 1280 | UINT64 | 64 | `uint64` | 8 | 1 |
| 128 | RGB24 | 24 | `uint8` | 3 | 3 |
| 2304 | RGBA32 | 32 | `uint8` | 4 | 4 |

RGB24 and RGBA32 are the **colour** data types: each voxel is its samples
(red, green, blue and, for RGBA32, alpha) in consecutive bytes. Every other
`datatype` is rejected, among them BINARY (1), COMPLEX64 (32), FLOAT128
(1536), COMPLEX128 (1792), COMPLEX256 (2048) and 0 (unknown).

### 7.3 Layout

The voxel data is `B` contiguous bytes at offset `v`, `x` varying fastest:
the voxel with indices `(x, y, z, t, k)` along dimensions 1 to 5 starts at
byte `v + ((((k × T + t) × Z + z) × Y + y) × X + x) × b`. A **slice** is the
`Y × X` voxels of one `(z, t, k)`: `Y × R` contiguous bytes, where
`R = X × b` is the size of a row.

**Row blocks.** Each slice is split into blocks of `h` rows, where `h` is
the largest divisor of `Y` with `h × R ≤ 131072` (2^17 bytes), or 1 if
there is none (`R > 131072`). A slice of at most 128 KiB is therefore a
single block, and a larger one is cut into equal blocks of at most 128 KiB
as far as `Y`'s divisors allow. (When `Y` is prime the blocks of a large
slice are single rows.)

Block `j` (`0 ≤ j < Y / h`) of slice `(z, t, k)` is the single range
`(0, v + (((k × T + t) × Z + z) × Y + j × h) × R, h × R)`. It lies within
the file (by the data size check), and its payload (§1.2) is far below the
limit.

### 7.4 Value scaling

The output stores the voxels' raw values: the scaling NIfTI defines is not
applied. For a data type that is not a colour type, the **scaling** applies
when `scl_slope` is finite and not 0; its slope `s` is `scl_slope`, and its
intercept `i` is `scl_inter` if that is finite, else 0. (A colour type, or
a `scl_slope` of 0, ±∞ or NaN, has no scaling, and `scl_inter` is then not
used.) The scaling is **nontrivial** when `s ≠ 1` or `i ≠ 0`. A nontrivial
scaling is recorded in the image's attributes (§7.7), so that a reader can
compute the scaled values `s × raw + i`.

### 7.5 Scales, units and position

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
no translation, and its scales come from `pixdim`.

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
(§2.2): the first, second and third values of its offset for `x`, `y` and
`z`, and 0 for `t` and `c` (each only for the axes present). Otherwise it
has none.

### 7.6 Channels

The image's `M` (§2.2) has an `"omero"` member in these two cases, and in
no other:

- **Colour:** `{"channels": [...]}` with one object per sample,
  `{"label": l, "color": k, "active": true, "window": {"min": 0, "max": 255,
  "start": 0, "end": 255}}`, where `(l, k)` are `("R", "FF0000")`,
  `("G", "00FF00")`, `("B", "0000FF")` and, for RGBA32, `("A", "FFFFFF")`.
- **Display range:** another data type, with `cal_min` and `cal_max`
  finite and `cal_max > cal_min`. The window in raw values is
  `lo = cal_min` and `hi = cal_max`; when the scaling applies (§7.4), `lo`
  and `hi` are instead the smaller and the larger of `(cal_min − i) / s`
  and `(cal_max − i) / s`. `M` has `{"channels": [...]}` with one object
  per index `k < C` of dimension 5 (one object when `n < 5`):
  `{"label": "C<k>", "color": "FFFFFF", "active": true, "window": {"min":
  lo, "max": hi, "start": lo, "end": hi}}`, with `k` in decimal.

### 7.7 Output

The output is one image at the archive root (§2.2), without a name, with
one array at path `"0"`. The root `zarr.json` is
`{"zarr_format": 3, "node_type": "group", "attributes": A}`, where `A` is
`{"ome": M}`, plus, when the scaling is nontrivial (§7.4), the member
`"nifti": {"scl_slope": s, "scl_inter": i}`.

The array `"0"`:

- **Axes:** `t` if `n ≥ 4`; `c` if `n ≥ 5` or the data type is a colour
  type; `z` if `n ≥ 3`; then `y` and `x`. (The axes follow `dim[0]`, so a
  dimension of size 1 up to `n` still has its axis.)
- **Shape:** `T`, `C` (the samples per voxel for a colour type), `Z`, `Y`
  and `X`, each only for the axes present.
- **Data type:** from §7.2.
- **Chunk shape:** 1 for `t` and `z`; 1 for `c`, or the samples per voxel
  for a colour type; `h` (§7.3) for `y`; `X` for `x`.
- **Codecs** (§2.1): `transpose` for a colour type (its samples are
  interleaved), then `bytes`, with `"endian"` (`"little"` or `"big"`: the
  file's byte order) when the data type is larger than 1 byte.
- **Scales and translation:** §7.5, with the image's one level.
- **Chunks:** every block is present. Block `j` of slice `(z, t, k)` is the
  entry `0/c/<coords>` with its range (§7.3), where the coords are `t`,
  `k` (0 for a colour type), `z` (each only for the axes present), then `j`
  and 0.

### 7.8 Summary

This section is informative. Both implementations print a one-line JSON
summary: `version` (1 or 2), `byteOrder` (`"little"` or `"big"`), `sizes`
(each axis's size), `dataType`, `colour`, `rowBlock` (`h`), `chunks`,
`scaling` (`{"slope": s, "inter": i}` when the scaling is nontrivial, else
`null`), `affine` (`"sform"`, `"qform"` or `null`), `translation` and
`extensions` (whether the extender's first byte is nonzero).
