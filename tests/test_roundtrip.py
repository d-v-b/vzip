import itertools

import xarray as xr
import zarr

from vzip.convert import write_vzip
from vzip.store import VZipStore

from conftest import virtualize


def test_roundtrip_single_and_combined(netcdf_files, tmp_path):
    paths, expected = netcdf_files
    vdss = [virtualize(p) for p in paths]
    combined = xr.concat(vdss, dim="time", coords="minimal", compat="override")
    cases = [
        ("one.vzip", vdss[0], xr.open_dataset(paths[0], engine="h5netcdf")),
        ("all.vzip", combined, expected),
    ]
    for (name, vds, exp), page_size in itertools.product(cases, [None, 256]):
        out = tmp_path / f"{page_size}{name}"
        write_vzip(vds, out, page_size=page_size)
        ds = xr.open_zarr(VZipStore(str(out)), consolidated=False, zarr_format=3)
        xr.testing.assert_identical(ds.load(), exp.load())


def test_naive_view_is_a_plain_zip(netcdf_files, tmp_path):
    import zipfile

    from vzip.pb import Range

    paths, _ = netcdf_files
    out = tmp_path / "one.vzip"
    write_vzip(virtualize(paths[0]), out)
    with zipfile.ZipFile(out) as zf:
        assert zf.testzip() is None
        ref = Range.decode(zf.read("temp/c/0/0/0"))
    store = VZipStore(str(out), resolve=False)
    zarr.open_group(store, mode="r")  # metadata is plain bytes, readable naively
    assert VZipStore(str(out), resolve=False) is not None
    assert ref.length > 0
