import itertools

import pytest
import xarray as xr

from vzip.shards import write_vzip_sharded
from zarr.core.sync import sync

from vzip.store import VZipStore

from conftest import virtualize


async def _list(s):
    return [k async for k in s.list()]


def test_virtual_shards_roundtrip(netcdf_files, tmp_path):
    paths, expected = netcdf_files
    vds = xr.concat([virtualize(p) for p in paths], dim="time", coords="minimal",
                    compat="override", data_vars="all")
    cases = itertools.product([False, True], [None, 128], ["end", "start"])
    for inline, page_size, loc in cases:
        out = tmp_path / f"s{inline}{page_size}{loc}.vzip"
        # temp: one shard per file (4 time steps x the full 2x2 spatial chunk grid)
        write_vzip_sharded(vds, out, {"temp": (4, 2, 2), "mask": (4, 3, 4)},
                           inline_index=inline, page_size=page_size, index_location=loc)
        s = VZipStore(str(out))
        ds = xr.open_zarr(s, consolidated=False, zarr_format=3)
        xr.testing.assert_identical(ds.load(), expected.load())
        refs = [k for k in sync(_list(s)) if s.classify(k) == "ref"]
        assert s.entry("temp/c/0/0/0").ref.parts[0 if loc == "start" else 1].size == 16 * 16
        assert sorted(refs) == [f"{v}/c/{i}/0/0" for v in ("mask", "temp") for i in range(3)]


def test_shard_spanning_files_rejected(netcdf_files, tmp_path):
    paths, _ = netcdf_files
    vds = xr.concat([virtualize(p) for p in paths], dim="time", coords="minimal",
                    compat="override", data_vars="all")
    with pytest.raises(ValueError, match="spans 2 files"):
        write_vzip_sharded(vds, tmp_path / "x.vzip", {"temp": (8, 2, 2), "mask": (4, 3, 4)})
