"""Use case: a referenced file is regenerated in place after the archive was written.

Operational products (forecast cycles, reprocessed L2/L3 granules) are often
re-published under the same name. A vzip archive written before the rewrite
still points at the old byte offsets. This script shows what a reader gets
today, for an uncompressed and a compressed variable.

Usage: uv run python experiments/stale_source.py
"""

from __future__ import annotations

import sys
import tempfile
import warnings
from pathlib import Path

import numpy as np
import xarray as xr

sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))
from conftest import virtualize  # noqa: E402

from vzip.convert import write_vzip  # noqa: E402
from vzip.store import VZipStore  # noqa: E402

warnings.filterwarnings("ignore")


def make(path: Path, seed: int) -> xr.Dataset:
    rng = np.random.default_rng(seed)
    ds = xr.Dataset(
        {
            "raw": (("y", "x"), rng.standard_normal((20, 20)).astype("f4")),
            "packed": (("y", "x"), rng.standard_normal((20, 20)).astype("f4")),
        },
        coords={"y": np.arange(20), "x": np.arange(20)},
    )
    ds.to_netcdf(path, engine="h5netcdf", encoding={
        "raw": {"chunksizes": (10, 10)},
        "packed": {"chunksizes": (10, 10), "zlib": True, "complevel": 4},
    })
    return ds


def main() -> None:
    d = Path(tempfile.mkdtemp())
    nc = d / "forecast.nc"
    v1 = make(nc, seed=1)
    write_vzip(virtualize(nc), d / "forecast.vzip")
    v2 = make(nc, seed=2)  # the producer re-publishes the file under the same name

    for var in ["raw", "packed"]:
        ds = xr.open_zarr(VZipStore(str(d / "forecast.vzip")), consolidated=False, zarr_format=3)
        try:
            got = ds[var].values
        except Exception as e:  # noqa: BLE001
            print(f"{var:7s}: read failed: {type(e).__name__}: {str(e)[:90]}")
            continue
        print(f"{var:7s}: equals the data the archive was made from: "
              f"{np.array_equal(got, v1[var].values)}; equals the new file: "
              f"{np.array_equal(got, v2[var].values)}")


if __name__ == "__main__":
    main()
