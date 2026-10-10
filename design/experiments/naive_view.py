"""Show what tools that know nothing about references see in a vzip archive."""
import sys, tempfile, zipfile
from pathlib import Path

import numpy as np
import xarray as xr
import zarr

sys.path.insert(0, str(Path(__file__).parents[2] / "python" / "tests"))
from conftest import make_netcdf, virtualize  # noqa: E402

from vzip.convert import write_vzip
from vzip.pb import Range
from vzip.store import VZipStore

d = Path(tempfile.mkdtemp())
make_netcdf(d / "a.nc", 0)
write_vzip(virtualize(d / "a.nc"), d / "a.vzip")
print("archive:", d / "a.vzip", (d / "a.vzip").stat().st_size, "bytes\n")

with zipfile.ZipFile(d / "a.vzip") as zf:
    print("zipfile.testzip ->", zf.testzip())
    for i in zf.infolist()[:6]:
        print(f"  {i.filename:20s} {i.file_size:6d}  extra={i.extra.hex()}")
    raw = zf.read("temp/c/0/0/0")
    print("\nnaive body of temp/c/0/0/0:", raw.hex(), "->", Range.decode(raw))

print("\nzarr.storage.ZipStore (naive):")
g = zarr.open_group(zarr.storage.ZipStore(d / "a.vzip", mode="r"), mode="r")
print("  members:", sorted(g.array_keys()))
try:
    g["temp"][:]
except Exception as e:
    print("  reading temp ->", type(e).__name__, str(e)[:100])

print("\nVZipStore(resolve=True):")
s = VZipStore(str(d / "a.vzip"))
ds = xr.open_zarr(s, consolidated=False, zarr_format=3)
ref = xr.open_dataset(d / "a.nc", engine="h5netcdf")
print("  identical:", ds.load().identical(ref.load()))
print("  stats:", s.stats.archive_requests, "archive reqs,", s.stats.external_requests, "external reqs")
