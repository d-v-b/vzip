"""Checks the browser NIfTI virtualizer's output against nibabel.

Each web/test/fixtures/nifti/*.nii is served over local HTTP and virtualized
by web/conformance/virtualize.ts (the browser code, run under Node). The
array is read through the reference reader and zarr-python and must equal
nibabel's unscaled data (`img.dataobj.get_unscaled()`), with nibabel's
(x, y, z, t, dimension 5) axes put in the output's order (t, c, z, y, x), and
a colour type's samples as the channel axis. A nontrivial scaling must be
recorded as nibabel reads it. `nifti_reject_*` files must be rejected.

Usage: uv run python web/test/nifti/verify.py [<dir of .nii files>]
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import nibabel as nib
import numpy as np
import zarr

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "conformance"))
from http_server import Server  # noqa: E402

from vzip.store import VZipStore  # noqa: E402


def expected(img) -> tuple[np.ndarray, list[str]]:
    """nibabel's raw voxels as a (t, c, z, y, x) array, and the axes present."""
    n = int(img.header["dim"][0])
    data = np.asarray(img.dataobj.get_unscaled())
    colour = data.dtype.names is not None
    if colour:
        data = np.stack([data[name] for name in data.dtype.names], axis=-1)
    else:
        data = data[..., np.newaxis]  # the samples
    sizes = list(data.shape[:-1]) + [1] * (7 - data.ndim + 1)
    data = data.reshape(sizes[:5] + [data.shape[-1]])  # x, y, z, t, k, samples
    # Colour samples are the channel axis; dimension 5 is otherwise.
    data = data[..., 0, :] if colour else data[..., 0]
    axes = (["t"] if n >= 4 else []) + (["c"] if n >= 5 or colour else []) + (["z"] if n >= 3 else []) + ["y", "x"]
    full = data.transpose(3, 4, 2, 1, 0)  # t, c, z, y, x
    drop = tuple(i for i, a in enumerate("tczyx") if a not in axes)
    return full.squeeze(axis=drop), axes


def load(nii: Path, tmp: str):
    """nibabel's image. nibabel refuses a valid scl_slope with a non-finite
    scl_inter, which the profile (like nifti1_io) takes as 0; such a file is
    loaded from a copy with scl_inter set to 0."""
    try:
        return nib.load(nii)
    except nib.spatialimages.HeaderDataError as e:
        if "invalid intercept" not in str(e):
            raise
    data = nii.read_bytes()
    header_class = nib.Nifti2Header if data[4:7] == b"n+2" else nib.Nifti1Header
    hdr = header_class.from_fileobj(open(nii, "rb"), check=False)
    hdr["scl_inter"] = 0
    copy = Path(tmp) / nii.name
    copy.write_bytes(hdr.binaryblock + data[len(hdr.binaryblock):])
    return nib.load(copy)


def main(fixtures: Path) -> int:
    server = Server(fixtures)
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for nii in sorted(fixtures.glob("*.nii")):
            out = Path(tmp) / f"{nii.stem}.vzip"
            p = subprocess.run(["node", str(ROOT / "web" / "conformance" / "virtualize.ts"),
                                server.base + nii.name, str(out)], capture_output=True, text=True)
            if nii.stem.startswith("nifti_reject"):
                ok = p.returncode == 3 and not out.exists()
                failures += not ok
                print(f"{nii.name:44s} {'rejected: ' + p.stderr.strip()[-80:] if ok else 'NOT REJECTED ' + p.stderr[-200:]}")
                continue
            if p.returncode:
                failures += 1
                print(f"{nii.name:44s} FAILED: {p.stderr.strip()[-300:]}")
                continue
            img = load(nii, tmp)
            want, axes = expected(img)
            root = zarr.open_group(VZipStore(str(out)), mode="r", zarr_format=3)
            arr = root["0"]
            got = arr[...]
            problems = []
            if list(arr.metadata.dimension_names) != axes:
                problems.append(f"axes {arr.metadata.dimension_names} != {axes}")
            if got.dtype != want.dtype.newbyteorder("="):
                problems.append(f"dtype {got.dtype} != {want.dtype}")
            if got.shape != want.shape or not np.array_equal(got, want, equal_nan=got.dtype.kind == "f"):
                problems.append(f"voxels differ ({got.shape} vs {want.shape})")
            slope, inter = img.dataobj.slope, img.dataobj.inter  # the header's are reset on load
            recorded = root.attrs.get("nifti")
            # nibabel keeps a colour type's scl_slope but cannot apply it; NIfTI does not scale colour.
            if img.get_data_dtype().names is None and (slope, inter) != (1, 0):
                if recorded != {"scl_slope": slope, "scl_inter": inter}:
                    problems.append(f"scaling {recorded} != nibabel's {(slope, inter)}")
            elif recorded is not None:
                problems.append(f"scaling {recorded} recorded, nibabel has none")
            failures += bool(problems)
            print(f"{nii.name:44s} {'ok' if not problems else problems} ({arr.nchunks} chunks, axes {axes})")
    print(f"\n{failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "fixtures" / "nifti"))
