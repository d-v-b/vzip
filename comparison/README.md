# kerchunk JSON, kerchunk Parquet, Icechunk and vzip on the same data

[`compare.py`](compare.py) stores one virtual dataset in each format, reads
each back with xarray over HTTP, counts every request, and checks every value
against the source files. The write-up is [COMPARISON.md](../COMPARISON.md). Each format's code is one writer and one opener of
a few lines, under `# formats` in the script, so it doubles as a side-by-side
example of how the four are written and opened.

```bash
just compare-formats                               # 2,880 references, ~4 minutes
just compare-formats --files 120 --days 30 --grid 90 180 --split 6 --no-full-read
just compare-formats --input 'path/to/*.nc' --concat-dim time   # your own files
uv run pytest tests/test_comparison.py             # a tiny run, no latency
```

## What it does

1. **Sources.** The script writes netCDF4 files with xarray and h5netcdf, one
   file per 30 days. Each day holds two variables (`tas`, `pr`) on a lat/lon
   grid, compressed with zlib and shuffle and split into chunks. `--input`
   uses your own netCDF4/HDF5 files instead.
2. **Virtualize once.** VirtualiZarr's HDF parser reads the files, and the
   per-file datasets are concatenated. Every format stores this same virtual
   dataset, with references to the sources as `http://` URLs.
3. **Write** each format with its own library:
   - kerchunk JSON and Parquet with `vds.vz.to_kerchunk`;
   - Icechunk with `vds.vz.to_icechunk` into a local repository with an HTTP
     virtual chunk container;
   - vzip with `vzip.convert.write_vzip`, without and with a page index.
4. **Serve** the sources and the written formats from a local HTTP server. It
   honours Range requests, waits 20 ms before answering each request (a
   stand-in for an object store round trip; `--latency`), and logs every
   request.
5. **Read** each format from a cold start. Each step is measured on its own:
   - **open:** `xr.open_zarr(store)`, which reads the hierarchy's metadata and
     loads the coordinates;
   - **first value:** one element of `tas`;
   - **everything:** `ds.load()`, every variable in full.
6. **Check** every value of every variable and coordinate against the source
   files, read with xarray. With `--no-full-read`, only the probed value is
   checked.

Results are written to `comparison/results/*.json`, including the library
versions. The results, and what they say about each format, are written up
in [COMPARISON.md](../COMPARISON.md).
