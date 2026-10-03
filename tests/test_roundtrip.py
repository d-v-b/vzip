import itertools

import xarray as xr
import zarr
from zarr.core.sync import sync

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
    for (name, vds, exp), page_size, consolidated in itertools.product(
        cases, [None, 256], [False, True]
    ):
        out = tmp_path / f"{page_size}{name}"
        write_vzip(vds, out, page_size=page_size)
        store = VZipStore(str(out))
        if consolidated:
            # the root zarr.json lists every array, so opening never lists the archive
            store.list_dir = store.list_prefix = store.list = _no_listing
        ds = xr.open_zarr(store, consolidated=consolidated, zarr_format=3)
        xr.testing.assert_identical(ds.load(), exp.load())


def _no_listing(*args):
    raise AssertionError("listed the archive")


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


def test_what_xarray_reads_at_open_is_written_late(netcdf_files, tmp_path):
    from vzip.convert import read_at_open

    paths, _ = netcdf_files
    vds = xr.concat([virtualize(p) for p in paths], dim="time", coords="minimal",
                    compat="override")
    assert {k: read_at_open(k, vds) for k in [
        "zarr.json", "temp/zarr.json", "time/c/0", "lat/c/0", "temp/c/0/0/0", "mask/c/0/0",
    ]} == {"zarr.json": True, "temp/zarr.json": True, "time/c/0": True, "lat/c/0": True,
           "temp/c/0/0/0": False, "mask/c/0/0": False}
    for page_size in [None, 256]:
        out = tmp_path / f"{page_size}.vzip"
        write_vzip(vds, out, page_size=page_size)
        s = VZipStore(str(out), resolve=False)
        sources = sync(s.kind("__vz__/sources")) and s.entry("__vz__/sources").data_offset
        for k in ["zarr.json", "time/zarr.json", "time/c/0", "lat/c/0", "temp/c/0/0/0"]:
            assert sync(s.kind(k)) == "bytes"
            assert (s.entry(k).data_offset > sources) == read_at_open(k, vds), (page_size, k)
