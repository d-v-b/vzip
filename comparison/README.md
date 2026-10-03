# kerchunk JSON, kerchunk Parquet, Icechunk and vzip on the same data

[`compare.py`](compare.py) stores one virtual dataset in each format, reads
it back with xarray over HTTP in several access patterns, counts every
request, and checks every value read against the source files. The results
are written up in [COMPARISON.md](../COMPARISON.md).

Each format's code is one writer and one opener of a few lines, under
`# formats` in the script. It also shows how each is written and opened.

```bash
just compare-formats                               # 2,880 references
just compare-formats --files 120 --days 30 --grid 90 180 --split 6 --no-full-read
just compare-formats --files 12 --days 30 --grid 90 180 --variables 40 --no-full-read
just compare-formats --input 'path/to/*.nc' --concat-dim time   # your own files
uv run pytest tests/test_comparison.py             # a tiny run, no latency
```

## What it does

1. **Sources.** The script writes netCDF4 files with xarray and h5netcdf,
   one file per `--days` days. Each day holds `--variables` variables
   (`tas`, `pr`, then more like `tas`) on a lat/lon grid, compressed with
   zlib and shuffle, and split into `--split` × `--split` chunks. The data is
   seeded, so every run writes the same files. `--input` uses your own
   netCDF4/HDF5 files instead.
2. **Virtualize once.** VirtualiZarr's HDF parser reads the files, and the
   per-file datasets are concatenated. Every format stores this same virtual
   dataset, with references to the sources as `http://` URLs.
3. **Write** each format twice: once with its library's defaults, and once
   with one setting changed for this workload. The settings are listed in
   COMPARISON.md and in `VARIANTS` in the script.
4. **Serve** the sources and the written formats from a local HTTP server,
   which logs every request. It honors Range requests, and holds each
   response for `--latency` seconds (default 20 ms) plus its size divided by
   `--bandwidth` (default 100 MB/s per response).
   - This stands in for an object store.
   - The server runs in its own process and times the wait by watching the
     clock, because macOS timer coalescing can stretch a 20 ms `time.sleep`
     to 160 ms.
5. **Read** each format in each access pattern, each from a cold open,
   `--repeats` times (default 5):
   - **open:** `xr.open_zarr(store, chunks=None)`, with xarray's default
     consolidation (consolidated metadata if the store has it, else a
     listing);
   - **point:** one element of the first variable;
   - **time series:** every time step at the middle grid point;
   - **map:** the whole grid at the middle time step;
   - **everything:** every variable in full (skipped with `--no-full-read`).

   `chunks=None` keeps xarray from wrapping the variables in dask. With dask,
   xarray tokenizes the store by pickling it. fsspec's reference filesystem
   pickles as its constructor arguments, so it downloads its references
   again each time.
6. **Check** every value read against the same values read from the source
   files with xarray.

Results go to `comparison/results/*.json`. They include every repeat's
request counts, bytes and times, and the library versions.
