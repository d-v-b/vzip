# kerchunk JSON, kerchunk Parquet, Icechunk and vzip on the same data

[`compare.py`](compare.py) stores one virtual dataset in each format, reads
each back with xarray over HTTP, counts every request, and checks every value
against the source files. Each format's code is one writer and one opener of
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
versions.

## Results

vzip 0.1.0 (this repository), VirtualiZarr 2.7.3, kerchunk 0.2.10, fsspec
2026.9.0, Icechunk 2.2.2, zarr-python 3.4.0, xarray 2026.9.0; macOS, one run
each. Each step's cell reads **requests (of which to the format's own files)
· bytes of the format's own files fetched · seconds**.

**Small:** 2,880 chunk references into 12 files (107 MB); 20 ms per request.

| format | size | objects | open | first value | everything | values |
|---|---:|---:|---|---|---|---|
| kerchunk JSON | 305.5 kB | 1 | 6 (6) · 916.6 kB · 1.14 s | 1 (0) · 0.0 kB · 0.15 s | 2880 (0) · 0.0 kB · 47.35 s | identical |
| kerchunk Parquet | 21.8 kB | 6 | 5 (5) · 8.4 kB · 0.71 s | 2 (1) · 6.8 kB · 0.34 s | 2890 (10) · 82.0 kB · 47.58 s | identical |
| Icechunk | 62.8 kB | 13 | 10 (10) · 8.5 kB · 1.03 s | 10 (5) · 20.1 kB · 0.34 s | 14405 (5) · 21.3 kB · 47.91 s | identical |
| vzip | 374.8 kB | 1 | 18 (18) · 222.6 kB · 1.37 s | 1 (0) · 0.0 kB · 0.16 s | 2880 (0) · 0.0 kB · 49.10 s | identical |
| vzip, paged | 375.1 kB | 1 | 22 (22) · 288.3 kB · 1.35 s | 1 (0) · 0.0 kB · 0.16 s | 2880 (0) · 0.0 kB · 49.07 s | identical |

**Large:** 259,200 chunk references into 120 files (313 MB); 20 ms per
request; `--no-full-read`.

| format | size | objects | open | first value | values |
|---|---:|---:|---|---|---|
| kerchunk JSON | 26,427.9 kB | 1 | 6 (6) · 79,283.6 kB · 1.50 s | 1 (0) · 0.0 kB · 0.25 s | identical |
| kerchunk Parquet | 768.3 kB | 8 | 5 (5) · 14.6 kB · 0.69 s | 2 (1) · 269.0 kB · 0.38 s | identical |
| Icechunk | 4,902.3 kB | 13 | 11 (11) · 11.7 kB · 1.00 s | 6 (5) · 1,657.9 kB · 0.37 s | identical |
| vzip | 32,385.5 kB | 1 | 126 (126) · 18,804.1 kB · 4.23 s | 1 (0) · 0.0 kB · 0.33 s | identical |
| vzip, paged | 32,388.7 kB | 1 | 413 (413) · 18,872.6 kB · 5.26 s | 1 (0) · 0.0 kB · 0.19 s | identical |

## Reading the results

**Every format returned identical values.** The data requests and bytes are
the same for all of them (Icechunk splits them differently, see below); what
differs is the cost of the format's own files.

**Size.** Per reference, the large run's sizes are:
- kerchunk Parquet: 3.0 B;
- Icechunk: 19 B;
- kerchunk JSON: 102 B;
- vzip: 125 B.

vzip stores one ZIP entry per chunk: a local header, a central directory
record, and the reference payload twice (§4.3 lets the body mirror it so that
plain ZIP tools see it). Its virtual-shard writer (`vzip.shards`, measured in
[FINDINGS.md](../FINDINGS.md) at about 8 B per reference) is not used here,
because it needs a chunks-per-shard choice for each array.

**Open.**
- **kerchunk JSON** downloads the whole file; fsspec fetches it three times
  (79 MB for a 26 MB file).
- **kerchunk Parquet** reads its consolidated `.zmetadata` and nothing else.
- **Icechunk** reads its own small metadata files (11 requests, 12 kB).
- **vzip, here, is the worst at scale, and the cause is the Python store and
  converter, not the format:**
  - **Listing.** xarray lists the store to find the arrays. `VZipStore`
    answers a listing of the root by reading the whole central directory: one
    18 MB read unpaged, or every 64 KiB page in turn when paged (about 290
    requests).
    The keys are sorted, so a lister could use the page index to jump past
    each array's chunk keys and read a handful of pages. The store doesn't do
    this yet.
  - **Coordinates.** `write_vzip` puts the coordinate chunks (here 120 `time`
    chunks, one per source file) near the start of the archive, and the store
    fetches each with its own request, one at a time. Placing them with the
    metadata at the end, inside the tail that is read first (§9.2), would make
    them free.
  - **No consolidated metadata.** The other three formats answer "which
    arrays exist" from consolidated metadata. vzip archives don't carry
    Zarr's consolidated metadata, although a converter could add it to the
    root `zarr.json`.

**First value.**
- **vzip** already holds every reference after open, so the first value costs
  one data request.
- **kerchunk Parquet** fetches the partition holding the reference (269 kB in
  the large run).
- **Icechunk** fetches the array's manifest (1.7 MB in the large run), as
  five parallel range requests. It splits larger chunk reads the same way: the
  small run's 38 kB chunks take five requests each, and the large run's
  1.2 kB chunks take one. That splitting is the default for its HTTP store. It
  helps throughput against an object store, and here it multiplies the
  small run's request count by about five.

**Everything.** All five take about 48 s, with the time dominated by
2,880 reads through a Python HTTP server.
- **kerchunk** (fsspec's reference filesystem, used through a sync wrapper)
  and **vzip** (`VZipStore`) issue their reads one at a time. The documented
  `xr.open_dataset(..., engine="kerchunk")` path behaves the same here.
- **Icechunk** reads concurrently, but issues five requests per chunk.

These times say more about the Python readers than about the formats.

## What this does not measure

- **Real object stores.** Their latency varies, and they have per-request
  costs and bandwidth limits. Everything here is a single local server.
- **Updates.** Icechunk's transactions, history and appends have no
  counterpart in the other three. kerchunk and vzip rewrite the file to
  change it.
- **Other readers.** vzip has Rust, TypeScript and Python readers in
  `impls/` and a browser reader in `web/`; only the reference `VZipStore` is
  used here.
- **Warm caches, repeated opens, and concurrent users.**
- **Variance.** One run on one machine; the times move by tens of
  milliseconds between runs. The request counts and bytes are deterministic
  for a given set of library versions.
