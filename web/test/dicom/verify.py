"""Checks the browser DICOM virtualizer's output against pydicom.

Each web/test/fixtures/dicom/*.dcm (or, given as arguments, other DICOM files
or http(s) URLs) is served over local HTTP and virtualized by
web/conformance/virtualize.ts (the browser code, run under Node). The whole
array is then read through the reference reader (src/vzip) and zarr-python,
and must equal what pydicom reads from the file: its pixel_array for native
pixel data, and for encapsulated pixel data its frames (pydicom's
generate_frames) decoded by imagecodecs, with the color space that the
Photometric Interpretation names. Whole-slide tiles are assembled into the
total pixel matrix. `dicom_reject_*` files must be rejected.

Usage: uv run python web/test/dicom/verify.py [<file or URL> ...]
"""

from __future__ import annotations

import base64
import io
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import imagecodecs
import numpy as np
import pydicom
import zarr
from pydicom.encaps import generate_frames

import vzip.codecs  # noqa: F401  (registers imagecodecs_jpeg and imagecodecs_jpeg2k)
from vzip.store import VZipStore

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "conformance"))
from http_server import Server  # noqa: E402

WHOLE_SLIDE = "1.2.840.10008.5.1.4.1.1.77.1.6"
NATIVE = {"1.2.840.10008.1.2", "1.2.840.10008.1.2.1", "1.2.840.10008.1.2.2"}


def expected(ds: pydicom.Dataset) -> np.ndarray:
    """The image as the profile lays it out: [c,] [z,] y, x."""
    n = int(ds.get("NumberOfFrames", 1) or 1)
    rows, columns, spp = ds.Rows, ds.Columns, ds.SamplesPerPixel
    dtype = np.dtype(f"{'u' if ds.PixelRepresentation == 0 else ''}int{ds.BitsAllocated}")
    if ds.file_meta.TransferSyntaxUID in NATIVE:
        frames = np.asarray(ds.pixel_array).reshape(n, rows, columns, *([spp] if spp > 1 else []))
    else:
        offsets = None
        if "ExtendedOffsetTable" in ds:
            offsets = (ds.ExtendedOffsetTable, ds.ExtendedOffsetTableLengths)
        decoded = []
        for frame in generate_frames(ds.PixelData, number_of_frames=n, extended_offsets=offsets):
            if ds.file_meta.TransferSyntaxUID == "1.2.840.10008.1.2.4.50":
                colorspace = None
                if spp == 3:
                    colorspace = "RGB" if ds.PhotometricInterpretation == "RGB" else "YCBCR"
                decoded.append(imagecodecs.jpeg8_decode(frame, colorspace=colorspace, outcolorspace="RGB" if spp == 3 else None))
            else:
                decoded.append(imagecodecs.jpeg2k_decode(frame))
        frames = np.stack(decoded).astype(dtype)
    frames = frames.astype(dtype)
    if ds.SOPClassUID == WHOLE_SLIDE:
        width, height = ds.TotalPixelMatrixColumns, ds.TotalPixelMatrixRows
        across = -(-width // columns)
        down = -(-height // rows)
        full = np.zeros((down * rows, across * columns, *frames.shape[3:]), dtype)
        for f in range(n):
            r, c = divmod(f, across)
            full[r * rows : (r + 1) * rows, c * columns : (c + 1) * columns] = frames[f]
        image = full[:height, :width]
    else:
        image = frames if n > 1 else frames[0]
    return np.moveaxis(image, -1, 0) if spp == 3 else image


def header_problems(source: dict, data: bytes) -> list[str]:
    """The source metadata against pydicom: in explicit VR, its DICOM JSON Model
    (without group lengths and Pixel Data); in implicit VR, each top-level
    element's raw bytes as UN (conventions/dicom/README.md §5)."""
    ds = pydicom.dcmread(io.BytesIO(data))
    strip = lambda d: {k: v for k, v in d.items() if not k.endswith("0000") and k != "7FE00010"}  # noqa: E731
    problems = []
    meta = ds.file_meta.to_json_dict(bulk_data_threshold=1 << 30)
    if source["meta"] != strip(meta):
        problems.append("meta differs from pydicom's")
    ours = strip(source["dataset"])
    if not ds.file_meta.TransferSyntaxUID.is_implicit_VR:
        if ours != strip(ds.to_json_dict(bulk_data_threshold=1 << 30)):
            problems.append("dataset differs from pydicom's")
        return problems
    if set(ours) != set(strip({f"{t:08X}": None for t in ds.keys()})):
        problems.append("dataset tags differ from pydicom's")
    for k, v in ours.items():
        raw = ds.get_item(int(k, 16)).value
        if v["vr"] == "UN" and isinstance(raw, bytes) and base64.b64decode(v.get("InlineBinary", "")) != raw:
            problems.append(f"{k}: bytes differ")
    return problems


def check(name: str, url: str, data: bytes, tmp: Path) -> tuple[bool, str]:
    out = tmp / "out.vzip"
    out.unlink(missing_ok=True)
    p = subprocess.run(["node", str(ROOT / "web" / "conformance" / "virtualize.ts"), url, str(out)],
                       capture_output=True, text=True)
    if name.startswith("dicom_reject"):
        ok = p.returncode == 3 and not out.exists()
        return ok, ("rejected: " + p.stderr.strip()[-90:]) if ok else "NOT REJECTED " + p.stderr[-200:]
    if p.returncode:
        return False, f"FAILED: {p.stderr.strip()[-300:]}"
    root = zarr.open_group(VZipStore(str(out)), mode="r", zarr_format=3)
    header = header_problems(root.attrs["vzip_virtualized"]["dicom"], data)
    if header:
        return False, f"HEADER: {header[:3]}"
    arr = root["0"]
    got = arr[...]
    want = expected(pydicom.dcmread(io.BytesIO(data)))
    if got.dtype != want.dtype or got.shape != want.shape:
        return False, f"{got.dtype} {got.shape} != {want.dtype} {want.shape}"
    if not np.array_equal(got, want):
        return False, f"pixels differ in {np.count_nonzero(got != want)} of {got.size} values"
    return True, f"ok {list(arr.metadata.dimension_names)} {got.shape} {got.dtype}"


def main(argv: list[str]) -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        if argv:
            for target in argv:
                if target.startswith(("http://", "https://")):
                    req = urllib.request.Request(target, headers={"User-Agent": "vzip-verify"})
                    data = urllib.request.urlopen(req, timeout=600).read()
                    url = target
                else:
                    data = Path(target).read_bytes()
                    server = Server(Path(target).resolve().parent)
                    url = server.base + Path(target).name
                ok, message = check(Path(target).stem, url, data, Path(tmp))
                failures += not ok
                print(f"{Path(target).name[:60]:60s} {message}")
        else:
            fixtures = HERE.parent / "fixtures" / "dicom"
            server = Server(fixtures)
            for path in sorted(fixtures.glob("*.dcm")):
                ok, message = check(path.stem, server.base + path.name, path.read_bytes(), Path(tmp))
                failures += not ok
                print(f"{path.name:44s} {message}")
    print(f"\n{failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
