"""Writes synthetic NIfTI files to web/test/fixtures/nifti/, covering the rules of
the NIfTI profile (profiles/nifti.md §7): NIfTI-1 and NIfTI-2 in both byte
orders, 1 to 7 dimensions, every data type, colour, units, value scaling,
representable and oblique affines, extensions, row blocks, and the inputs the
profile rejects (`nifti_reject_*`).

Most files are packed field by field here, so that every header value is
exactly what the case needs; a few are written by nibabel, as real files are.
web/test/nifti/verify.py compares each accepted file's virtualization with
nibabel's reading of it.

Usage: uv run python web/test/nifti/write_fixtures.py
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

import nibabel as nib
import numpy as np

OUT = Path(__file__).parents[1] / "fixtures" / "nifti"

# Offset and struct format of the header fields (profiles/nifti.md §7.1).
LAYOUTS = {
    1: {
        "length": 348, "magic": (344, "4s"), "dim": (40, "8h"), "datatype": (70, "h"), "bitpix": (72, "h"),
        "pixdim": (76, "8f"), "vox_offset": (108, "f"), "scl_slope": (112, "f"), "scl_inter": (116, "f"),
        "xyzt_units": (123, "B"), "cal_max": (124, "f"), "cal_min": (128, "f"), "qform_code": (252, "h"),
        "sform_code": (254, "h"), "quatern": (256, "3f"), "qoffset": (268, "3f"), "srow": (280, "12f"),
    },
    2: {
        "length": 540, "magic": (4, "8s"), "dim": (16, "8q"), "datatype": (12, "h"), "bitpix": (14, "h"),
        "pixdim": (104, "8d"), "vox_offset": (168, "q"), "scl_slope": (176, "d"), "scl_inter": (184, "d"),
        "xyzt_units": (500, "i"), "cal_max": (192, "d"), "cal_min": (200, "d"), "qform_code": (344, "i"),
        "sform_code": (348, "i"), "quatern": (352, "3d"), "qoffset": (376, "3d"), "srow": (400, "12d"),
    },
}
MAGIC = {1: b"n+1\0", 2: b"n+2\0\r\n\x1a\n"}
# datatype: (numpy type, bitpix)
TYPES = {
    2: ("u1", 8), 4: ("i2", 16), 8: ("i4", 32), 16: ("f4", 32), 64: ("f8", 64), 256: ("i1", 8),
    512: ("u2", 16), 768: ("u4", 32), 1024: ("i8", 64), 1280: ("u8", 64), 128: ("u1", 24), 2304: ("u1", 32),
}
AXIS_ALIGNED = [2.0, 0, 0, -10.5, 0, 3.0, 0, 20.25, 0, 0, 4.0, 7.0]


def voxels(shape, datatype: int, order: str, seed: int) -> bytes:
    """Distinct values for every voxel, x fastest, in the file's byte order."""
    np_type, bitpix = TYPES[datatype]
    samples = bitpix // 8 if datatype in (128, 2304) else 1
    count = math.prod(shape) * samples
    values = (np.arange(count, dtype=np.int64) * 7 + seed) % 251 - (0 if np_type[0] == "u" else 100)
    return np.asarray(values).astype(order + np_type).tobytes()


def nifti(name: str, dims, datatype: int = 2, *, version: int = 1, order: str = "<", bitpix: int | None = None,
          vox_offset: float | None = None, data: bytes | None = None, truncate: int = 0, extension: bytes = b"",
          seed: int = 0, **fields) -> None:
    """Packs a single-file NIfTI: header, extender (and extensions), then the voxels."""
    layout = LAYOUTS[version]
    length = layout["length"]
    header = bytearray(length)
    values = {
        "magic": MAGIC[version], "dim": list(dims) + [1] * (8 - len(dims)), "datatype": datatype,
        "bitpix": TYPES.get(datatype, ("u1", 8))[1] if bitpix is None else bitpix,
        "pixdim": [1.0] * 8, "scl_slope": 1.0, "scl_inter": 0.0, "xyzt_units": 2, "cal_max": 0.0, "cal_min": 0.0,
        "qform_code": 0, "sform_code": 0, "quatern": [0.0] * 3, "qoffset": [0.0] * 3, "srow": [0.0] * 12,
    }
    values.update(fields)
    ext = b""
    if extension:
        # One extension: esize (a multiple of 16), ecode 6 (comment), then the text.
        esize = 8 + len(extension) + (-(8 + len(extension)) % 16)
        ext = struct.pack(order + "ii", esize, 6) + extension.ljust(esize - 8, b"\0")
    values["vox_offset"] = length + 4 + len(ext) if vox_offset is None else vox_offset
    struct.pack_into(order + "i", header, 0, length)
    for key, value in values.items():
        offset, fmt = layout[key]
        struct.pack_into(order + fmt, header, offset, *(value if isinstance(value, list) else [value]))
    if data is None:
        n = values["dim"][0]
        data = voxels(values["dim"][1 : n + 1] if 1 <= n <= 7 else [1], datatype if datatype in TYPES else 2,
                      order, seed)
    extender = bytes([1 if ext else 0, 0, 0, 0])
    body = bytes(header) + extender + ext
    body = body.ljust(int(values["vox_offset"]) if math.isfinite(values["vox_offset"]) else len(body), b"\0")
    content = body + data
    (OUT / f"{name}.nii").write_bytes(content[: len(content) - truncate])


def quaternion_b(radians: float) -> list[float]:
    """A rotation about x: quatern_b, c, d."""
    return [math.sin(radians / 2), 0.0, 0.0]


def by_nibabel() -> None:
    """Files as nibabel writes them."""
    rng = np.random.default_rng(1)
    affine = np.diag([0.5, 0.75, 1.25, 1.0])
    affine[:3, 3] = [-12.0, 3.5, 40.0]
    img = nib.Nifti1Image(rng.integers(0, 255, (6, 5, 4), dtype=np.uint8), affine)
    img.header.set_xyzt_units("mm")
    img.to_filename(OUT / "nifti_nibabel_n1_uint8.nii")
    # NIfTI-2, 5D (a vector per voxel, as in displacement fields), with a comment extension.
    data = rng.standard_normal((5, 4, 3, 2, 3)).astype(np.float32)
    img = nib.Nifti2Image(data, np.diag([2.0, 2.0, 2.0, 1.0]))
    img.header.set_xyzt_units("micron", "msec")
    img.header["pixdim"][4] = 250
    img.header.extensions.append(nib.nifti1.Nifti1Extension("comment", b"written by write_fixtures.py"))
    img.to_filename(OUT / "nifti_nibabel_n2_vector_ext.nii")
    # RGB24, which nibabel stores from a structured array.
    rgb = np.zeros((4, 3, 2), dtype=[("R", "u1"), ("G", "u1"), ("B", "u1")])
    for k, c in enumerate("RGB"):
        rgb[c] = rng.integers(0, 255, rgb.shape)
    nib.Nifti1Image(rgb, np.eye(4)).to_filename(OUT / "nifti_nibabel_rgb24.nii")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for p in OUT.glob("*.nii"):
        p.unlink()
    by_nibabel()

    # NIfTI-1 and NIfTI-2, both byte orders, 3D/4D/5D, units, affines, scaling.
    nifti("nifti_n1_le_int16_3d_sform", [3, 5, 4, 3], 4, pixdim=[1, 2.0, 3.0, 4.0, 1, 1, 1, 1],
          sform_code=2, srow=AXIS_ALIGNED, qform_code=1, quatern=quaternion_b(0.3))
    nifti("nifti_n1_be_int16_4d_scaled", [4, 4, 3, 2, 5], 4, order=">", xyzt_units=2 | 8,
          pixdim=[-1, 1.5, 1.5, 3.0, 2.5, 1, 1, 1], scl_slope=0.5, scl_inter=-20.0, cal_min=-30.0, cal_max=10.0,
          sform_code=1, srow=[-1.5, 0, 0, 90, 0, 1.5, 0, -126, 0, 0, 3, -72])
    nifti("nifti_n1_le_qform_identity", [3, 4, 3, 2], 512, xyzt_units=3, pixdim=[1, 0.25, 0.5, 2.0, 1, 1, 1, 1],
          qform_code=1, qoffset=[100.0, -50.5, 0.125])
    nifti("nifti_n1_be_qform_oblique", [3, 4, 3, 2], 512, order=">", qform_code=1, quatern=quaternion_b(0.1),
          qoffset=[1.0, 2.0, 3.0], pixdim=[1, 1.1, 1.2, 1.3, 1, 1, 1, 1])
    nifti("nifti_n1_le_qfac_negative", [3, 4, 3, 2], 2, qform_code=1, qoffset=[1.0, 2.0, 3.0],
          pixdim=[-1, 1, 1, 1, 1, 1, 1, 1])
    nifti("nifti_n2_le_float32_4d", [4, 3, 4, 2, 3], 16, version=2, xyzt_units=2 | 16,
          pixdim=[1, 0.9, 0.9, 1.1, 750.0, 1, 1, 1], sform_code=4, srow=AXIS_ALIGNED, cal_min=0.0, cal_max=200.0)
    nifti("nifti_n2_be_float64_5d", [5, 3, 2, 2, 2, 3], 64, version=2, order=">", xyzt_units=1 | 24,
          pixdim=[1, 0.001, 0.001, 0.002, 0.5, 1, 1, 1], cal_min=-5.0, cal_max=5.0)
    nifti("nifti_n2_be_rgba32", [3, 3, 2, 2], 2304, version=2, order=">")
    nifti("nifti_n1_be_rgb24_4d", [4, 2, 3, 2, 2], 128, order=">", cal_min=0.0, cal_max=100.0, scl_slope=3.0)
    nifti("nifti_n1_le_rgba32_2d", [2, 3, 2], 2304, extension=b"an extension before the voxels")

    # Every other data type.
    for datatype, name in [(256, "int8"), (8, "int32"), (768, "uint32"), (1024, "int64"), (1280, "uint64"),
                           (64, "float64")]:
        nifti(f"nifti_dtype_{name}", [3, 3, 2, 2], datatype, seed=datatype)
    for datatype, name in [(512, "uint16"), (16, "float32"), (4, "int16")]:
        nifti(f"nifti_dtype_{name}_be", [3, 3, 2, 2], datatype, order=">", seed=datatype)

    # Dimensions.
    nifti("nifti_dims_1d", [1, 7], 2)
    nifti("nifti_dims_2d", [2, 5, 3], 4)
    nifti("nifti_dims_4d_t1", [4, 3, 2, 2, 1], 2)
    nifti("nifti_dims_7d_ones", [7, 3, 2, 2, 2, 2, 1, 1], 2)
    nifti("nifti_dims_unread_beyond_n", [3, 3, 2, 2, 0, -5, 9, 9], 2)

    # Header values that change the output but are never rejected.
    nan, inf = float("nan"), float("inf")
    nifti("nifti_edge_pixdim_invalid", [4, 3, 2, 2, 2], 2, xyzt_units=2 | 8, pixdim=[1, nan, -2.0, 0.0, inf, 1, 1, 1])
    nifti("nifti_edge_units_unknown", [4, 3, 2, 2, 2], 2, xyzt_units=5 | 32, pixdim=[1, 2, 2, 2, 2, 1, 1, 1])
    nifti("nifti_edge_n2_units_negative", [4, 3, 2, 2, 2], 2, version=2, xyzt_units=-1)
    nifti("nifti_edge_slope_zero", [3, 3, 2, 2], 4, scl_slope=0.0, scl_inter=5.0, cal_min=1.0, cal_max=2.0)
    nifti("nifti_edge_slope_nan", [3, 3, 2, 2], 4, scl_slope=nan, scl_inter=5.0)
    nifti("nifti_edge_inter_inf", [3, 3, 2, 2], 4, scl_slope=2.0, scl_inter=inf, cal_min=-4.0, cal_max=8.0)
    nifti("nifti_edge_slope_negative", [3, 3, 2, 2], 4, scl_slope=-2.0, scl_inter=1.0, cal_min=-4.0, cal_max=8.0)
    nifti("nifti_edge_slope_trivial", [3, 3, 2, 2], 4, scl_slope=1.0, scl_inter=0.0)
    nifti("nifti_edge_cal_reversed", [3, 3, 2, 2], 4, cal_min=8.0, cal_max=-4.0)
    nifti("nifti_edge_sform_set_not_representable", [3, 3, 2, 2], 2, sform_code=1,
          srow=[1, 0.01, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0], qform_code=1, qoffset=[5.0, 6.0, 7.0])
    nifti("nifti_edge_sform_nan", [3, 3, 2, 2], 2, sform_code=1, srow=[1, 0, 0, nan, 0, 1, 0, 0, 0, 0, 1, 0])
    nifti("nifti_edge_sform_code_negative", [3, 3, 2, 2], 2, sform_code=-1, srow=AXIS_ALIGNED,
          qform_code=2, qoffset=[1.5, 2.5, 3.5])
    nifti("nifti_edge_qform_2d", [2, 4, 3], 2, qform_code=1, qoffset=[1.0, 2.0, 3.0])
    nifti("nifti_edge_vox_offset_padded", [3, 3, 2, 2], 4, vox_offset=480.0)

    # Row blocks: slices of 140000 bytes split into 2 blocks of 350 rows.
    nifti("nifti_rowblock_split", [3, 200, 700, 1], 2)

    # Rejected, one per rule of §7.1 and §7.2 (and the profile choice of §1.2).
    nifti("nifti_reject_dim0", [0, 3, 2, 2], 2)
    nifti("nifti_reject_dim0_8", [8, 3, 2, 2, 1, 1, 1, 1], 2, data=b"\0" * 12)
    nifti("nifti_reject_dim_zero", [3, 3, 0, 2], 2, data=b"\0" * 12)
    nifti("nifti_reject_dim_negative", [3, 3, -2, 2], 2, data=b"\0" * 12)
    nifti("nifti_reject_n2_dim_huge", [3, 2**53, 1, 1], 2, version=2, data=b"\0" * 12)
    nifti("nifti_reject_dim6", [6, 3, 2, 1, 1, 1, 2], 2)
    nifti("nifti_reject_dim7", [7, 3, 2, 1, 1, 1, 1, 2], 2)
    nifti("nifti_reject_complex64", [3, 3, 2, 2], 32, bitpix=64, data=b"\0" * 96)
    nifti("nifti_reject_float128", [3, 3, 2, 2], 1536, bitpix=128, data=b"\0" * 192)
    nifti("nifti_reject_binary", [3, 8, 2, 2], 1, bitpix=1, data=b"\0" * 4)
    nifti("nifti_reject_datatype_unknown", [3, 3, 2, 2], 3, bitpix=8, data=b"\0" * 12)
    nifti("nifti_reject_bitpix", [3, 3, 2, 2], 4, bitpix=8)
    nifti("nifti_reject_rgb_5d", [5, 3, 2, 1, 1, 2], 128)
    nifti("nifti_reject_vox_offset_fraction", [3, 3, 2, 2], 2, vox_offset=352.5)
    nifti("nifti_reject_vox_offset_nan", [3, 3, 2, 2], 2, vox_offset=float("nan"))
    nifti("nifti_reject_vox_offset_zero", [3, 3, 2, 2], 2, vox_offset=0.0)
    nifti("nifti_reject_vox_offset_in_extender", [3, 3, 2, 2], 2, vox_offset=348.0)
    nifti("nifti_reject_n2_vox_offset_low", [3, 3, 2, 2], 2, version=2, vox_offset=540)
    nifti("nifti_reject_truncated", [3, 3, 2, 2], 4, truncate=1)
    nifti("nifti_reject_n2_size_overflow", [3, 2**30, 2**30, 2**30], 2, version=2, data=b"\0" * 12)
    nifti("nifti_reject_n2_short_header", [3, 3, 2, 2], 2, version=2, data=b"", truncate=540 + 4 - 100)
    nifti("nifti_reject_pair_magic", [3, 3, 2, 2], 2, magic=b"ni1\0")


if __name__ == "__main__":
    main()
