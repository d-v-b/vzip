# Virtualizing NIfTI files

The NIfTI profile of [VIRTUALIZE.md](../VIRTUALIZE.md) (revision 16), numbered
as its §7. §1 and §2 are in VIRTUALIZE.md and apply here.

Profile version: 1 · Convention: [conventions/nifti](../conventions/nifti/README.md),
version 1. The output has the convention's layout for the input
([VIRTUALIZE.md §2](../VIRTUALIZE.md#2-the-zarr-layout)); this profile says
which inputs are accepted and how each chunk references the file. "The
convention §n" below is a section of conventions/nifti/README.md.

## 7. NIfTI profile

### 7.1 Header

The input is a NIfTI-1 or NIfTI-2 single file (`.nii`), which §1.2 chose by
its first bytes. Its header is the first `L` bytes of the file
([the convention §2](../conventions/nifti/README.md#2-the-header)); a file
shorter than `L` bytes is rejected. The file is **little-endian** if bytes
0–3, read as a little-endian 32-bit integer, are `L`, and **big-endian**
otherwise (§1.2 has checked that one of the two holds). Every field is read
in that byte order, at the offset and with the type that the convention §2
gives it.

**Checks.** The input is rejected unless all of these hold:

- **Dimensions:** `n = dim[0]` is from 1 to 7, and `dim[i]` is from 1 to
  2^53 − 1 for every `1 ≤ i ≤ n`. The values `dim[i]` for `i > n` are not
  checked. If `n ≥ 6`, `dim[6]` MUST be 1, and if `n = 7`, `dim[7]` MUST be
  1 (the layout has no axis for dimensions 6 and 7, so CIFTI-2 files, whose
  data runs along them, are rejected).
- **Data type:** `datatype` is in the table of
  [the convention §3](../conventions/nifti/README.md#3-data-types), and
  `bitpix` is 8 times its bytes per voxel `b`.
- **Color:** a color data type has `n ≤ 4`.
- **Offset:** `vox_offset` is an integer `v` with `L + 4 ≤ v ≤ 2^53 − 1`.
  For NIfTI-1, `vox_offset` is a binary32 number, which MUST be finite and
  have an integral value. (The voxel data never starts inside the header or
  its 4-byte extender. The NIfTI-1 standard's advice that `vox_offset` be a
  multiple of 16 is not checked, and a `vox_offset` of 0, which some readers
  take to mean 352, is rejected.)
- **Data size:** with `X`, `Y`, `Z`, `T`, `C` the sizes of the convention
  §2, the size of the voxel data, `B = X × Y × Z × T × C × b` computed
  exactly, is at most 2^53 − 1, and `v + B` is at most the file's size.

Every check applies whatever the other fields hold. No other field is
checked: the values the layout uses (the convention §4) only change the
output as the convention says, and never reject the input (except by §1.3,
in the convention §4.4).

**Extensions.** The extender's first byte, at offset `L`, and, when it is
not 0, the extension chain of the
[convention §5](../conventions/nifti/README.md#5-source-metadata) are read
to make the source metadata. They never reject the input, and never change
where the voxel data starts: it starts at `v` whatever they hold. A
header-and-image pair (magic `ni1` or `ni2`) and a gzipped file are rejected
by §1.2.

### 7.2 Chunks

The voxel data is `B` contiguous bytes at offset `v`, `x` varying fastest:
the voxel with indices `(x, y, z, t, k)` along dimensions 1 to 5 starts at
byte `v + ((((k × T + t) × Z + z) × Y + y) × X + x) × b`, and a slice
`(z, t, k)` is `Y × R` contiguous bytes, where `R = X × b`.

Every chunk of the convention's array is present. The chunk that holds
block `j` of slice `(z, t, k)`
([the convention §4.1](../conventions/nifti/README.md#41-the-array)), at
`0/c/<coords>`, is the single range
`(0, v + (((k × T + t) × Z + z) × Y + j × h) × R, h × R)`. It lies within
the file (by the data size check), and its payload (§1.2) is far below the
limit. The archive has no other entries than the root's and the array's
`zarr.json`.

### 7.3 Summary

This section is informative. Both implementations print a one-line JSON
summary: `version` (1 or 2), `byteOrder` (`"little"` or `"big"`), `sizes`
(each axis's size), `dataType`, `color`, `rowBlock` (`h`), `chunks`,
`scaling` (`{"slope": s, "inter": i}` when the scaling is nontrivial, else
`null`), `affine` (`"sform"`, `"qform"` or `null`), `translation` and
`extensions` (whether the extender's first byte is nonzero).
