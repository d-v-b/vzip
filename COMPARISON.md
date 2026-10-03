# Virtual Zarr formats compared: kerchunk JSON, kerchunk Parquet, Icechunk and vzip

This document compares four ways of storing a virtual Zarr dataset: chunk
references into existing netCDF4/HDF5 files. All four stored the same
references. Each was read back over HTTP from a cold start, and every value
was checked against the source files. The code is in
[`comparison/`](comparison/README.md), and the numbers below can be
regenerated with `just compare-formats`.

## Summary

- **All four formats returned identical values.** Every format reads the same
  chunk bytes. They differ only in what they fetch of their own files.
- **Size.** kerchunk Parquet is the smallest (3 B per reference), then
  Icechunk (19 B), kerchunk JSON (102 B) and vzip (125 B, with one ZIP entry
  per chunk).
- **Opening a large dataset:**
  - kerchunk Parquet and Icechunk read a few kilobytes in 6 to 11 requests.
  - kerchunk JSON downloads its whole file, three times.
  - vzip's Python reader made 126 to 413 requests and fetched 18.8 MB. That is
    the worst of the four, and it comes from the reader and converter, not
    from the format (see [vzip at open](#vzip-at-open)).
- **The first value:**
  - After opening, vzip already holds every reference, so the first value
    costs one data request.
  - kerchunk Parquet first fetches a 269 kB partition.
  - Icechunk first fetches a 1.7 MB manifest.
- **Full reads** took about the same time in every format (about 48 s for
  2,880 chunks). That time mostly measures the Python readers' concurrency.

## The formats

| | kerchunk JSON | kerchunk Parquet | Icechunk | vzip |
|---|---|---|---|---|
| what is written | one JSON file | a directory: `.zmetadata` and Parquet files of references | a repository: metadata, snapshots and manifests | one ZIP file |
| objects (large run) | 1 | 8 | 13 | 1 |
| where references live | a JSON object, `[url, offset, length]` per key | columns of Parquet partitions, 100,000 references each | manifests | one ZIP entry per key, the reference in its central directory record |
| written here with | `vds.vz.to_kerchunk(format="json")` | `vds.vz.to_kerchunk(format="parquet")` | `vds.vz.to_icechunk(session.store)` | `vzip.convert.write_vzip(vds, path)` |
| read here with | fsspec's reference filesystem | fsspec's reference filesystem | the Icechunk library | `vzip.store.VZipStore` |
| changing it | rewrite | rewrite | commits, with history and transactions | rewrite |

## Method

1. **Source files.**
   - netCDF4 files written with xarray and h5netcdf, one file per 30 days.
   - Each day holds two float32 variables, `tas` and `pr`, on a lat/lon grid.
     They are compressed with zlib and shuffle, and each day is split into
     chunks.
   - The coordinates are `time`, `lat` and `lon`.
2. **One virtual dataset.**
   - VirtualiZarr's HDF parser reads every file, with the coordinates loaded
     into memory.
   - The per-file datasets are concatenated along `time`.
   - The references point to the source files by `http://` URL.
3. **Write** that dataset in each format with the format's own library, as in
   the table above. vzip is written twice: without and with a page index
   (SPEC.md §7).
4. **Serve** the source files and the written formats from one local HTTP
   server. It honours Range requests, waits 20 ms before answering each
   request (a stand-in for an object store round trip), and logs every
   request.
5. **Read** each format from a cold start, measuring three steps separately:
   - **open:** `xr.open_zarr(store)` with xarray's defaults, so it uses
     consolidated metadata where the store has it and lists the store where it
     doesn't. This step reads the hierarchy's metadata and loads the
     coordinates;
   - **first value:** one element of `tas`;
   - **everything:** `ds.load()`, every variable in full.
6. **Check** every value of every variable and coordinate against the source
   files, read directly with xarray. In the large run, only the first value
   is read and checked.

The two runs below differ only in their parameters:

| run | files | chunks | references | source data |
|---|---:|---|---:|---:|
| small | 12 | 180 × 360 grid, each day in 2 × 2 chunks | 2,880 | 107 MB |
| large | 120 | 90 × 180 grid, each day in 6 × 6 chunks | 259,200 | 313 MB |

Library versions: vzip 0.1.0 (this repository), VirtualiZarr 2.7.3, kerchunk
0.2.10, fsspec 2026.9.0, Icechunk 2.2.2, zarr-python 3.4.0, xarray 2026.9.0.
Python 3.12 on macOS.

## Results

Each step's cell reads **requests (of which to the format's own files) ·
bytes of the format's own files fetched · seconds**. Bytes of the source
files are the same for every format and are not shown.

**Small run:** 2,880 references.

| format | size | objects | open | first value | everything | values |
|---|---:|---:|---|---|---|---|
| kerchunk JSON | 305.5 kB | 1 | 6 (6) · 916.6 kB · 1.15 s | 1 (0) · 0 · 0.16 s | 2,880 (0) · 0 · 47.79 s | identical |
| kerchunk Parquet | 21.8 kB | 6 | 6 (6) · 10.0 kB · 0.71 s | 2 (1) · 6.8 kB · 0.33 s | 2,890 (10) · 82.0 kB · 47.84 s | identical |
| Icechunk | 62.5 kB | 13 | 10 (10) · 8.6 kB · 1.02 s | 10 (5) · 19.8 kB · 0.35 s | 14,405 (5) · 21.3 kB · 48.38 s | identical |
| vzip | 374.8 kB | 1 | 18 (18) · 222.6 kB · 1.32 s | 1 (0) · 0 · 0.17 s | 2,880 (0) · 0 · 49.34 s | identical |
| vzip, paged | 375.1 kB | 1 | 22 (22) · 288.3 kB · 1.55 s | 1 (0) · 0 · 0.18 s | 2,880 (0) · 0 · 48.90 s | identical |

**Large run:** 259,200 references; full reads skipped.

| format | size | objects | open | first value | values |
|---|---:|---:|---|---|---|
| kerchunk JSON | 26.4 MB | 1 | 6 (6) · 79.28 MB · 1.50 s | 1 (0) · 0 · 0.24 s | identical |
| kerchunk Parquet | 768.3 kB | 8 | 6 (6) · 16.2 kB · 0.65 s | 2 (1) · 269.0 kB · 0.36 s | identical |
| Icechunk | 4.9 MB | 13 | 11 (11) · 11.7 kB · 1.01 s | 6 (5) · 1.66 MB · 0.36 s | identical |
| vzip | 32.4 MB | 1 | 126 (126) · 18.80 MB · 4.25 s | 1 (0) · 0 · 0.19 s | identical |
| vzip, paged | 32.4 MB | 1 | 413 (413) · 18.87 MB · 4.92 s | 1 (0) · 0 · 0.23 s | identical |

The raw results, with every count, are in
[`comparison/results/`](comparison/results/).

## What the numbers say

### Size

Per reference in the large run:

| format | bytes per reference |
|---|---:|
| kerchunk Parquet | 3.0 |
| Icechunk | 19 |
| kerchunk JSON | 102 |
| vzip | 125 |

- **kerchunk Parquet** is a compressed columnar file, and its references are
  highly regular (a few URLs, increasing offsets).
- **vzip** writes one ZIP entry per chunk: a local header, a central directory
  record, and the reference payload twice. The body mirrors the payload so
  that plain ZIP tools see it (SPEC.md §4.3).
- vzip's virtual-shard writer (`vzip.shards`) packs a shard's references into
  one entry, and earlier measurements in [FINDINGS.md](FINDINGS.md) put it at
  about 8 bytes per reference. It isn't used here, because it needs a
  chunks-per-shard choice for each array.

### Opening

- **kerchunk JSON** downloads the whole file. fsspec fetched it three times
  (79.3 MB for a 26.4 MB file), so opening costs grow with the number of
  references.
- **kerchunk Parquet** reads its consolidated `.zmetadata`: 6 requests, 16 kB.
- **Icechunk** reads its own small metadata files: 11 requests, 12 kB.
- **vzip** is the worst at scale. The causes are covered in the next section.

### vzip at open

The format lets a reader open an archive in two range requests: the tail,
then the region from the source table to the end (SPEC.md §9.2). The Python
reader and converter used here do much more than that, for three reasons:

- **Listing reads the whole central directory.**
  - To find the arrays, xarray lists the store. `VZipStore` answers a listing
    of the root by reading every central directory record: one 18 MB read
    without a page index, or every 64 KiB page in turn with one (about 290
    requests).
  - The keys are sorted when there is a page index (SPEC.md §7.1). A lister
    could use the pages' first keys to jump past each array's chunk keys, and
    read a handful of pages.
- **Coordinate chunks are fetched one at a time.**
  - `write_vzip` places the coordinate chunks (here 120 `time` chunks, one per
    source file) near the start of the archive.
  - The reader then fetches each with its own request, one after another.
  - Placed with the metadata at the end, they would come with the tail read.
- **No consolidated metadata.** The other formats answer "which arrays exist"
  from consolidated metadata. vzip archives don't carry Zarr's consolidated
  metadata, though the converter could add it to the root `zarr.json`.

All three are changes to the reference reader and converter. None of them
needs a change to the format.

### The first value

- **vzip** has every reference in memory after opening. The first value costs
  one request, for the chunk itself.
- **kerchunk Parquet** first fetches the Parquet partition that holds the
  reference: 269 kB in the large run, for a partition of 100,000 references.
- **Icechunk** first fetches the array's manifest (1.66 MB in the large run),
  as five parallel range requests.
  - Its HTTP store splits larger chunk reads the same way. The small run's
    38 kB chunks took five requests each, while the large run's 1.2 kB chunks
    took one.
  - Splitting helps throughput against an object store. Here it multiplied the
    small run's request count by about five.

### Reading everything

All five took about 48 s to read 2,880 chunks, so this step mostly measures
the Python readers rather than the formats.
- **kerchunk** (fsspec's reference filesystem) and **vzip** (`VZipStore`)
  issued their reads one at a time. kerchunk's documented
  `xr.open_dataset(..., engine="kerchunk")` path behaved the same.
- **Icechunk** read concurrently, but issued five requests per chunk.

## Limits

- **One local server.** Real object stores have variable latency,
  per-request costs and bandwidth limits.
- **Python readers only.** vzip also has Rust, TypeScript and Python
  implementations in [`impls/`](impls/) and a browser reader in
  [`web/`](web/). Only the reference `VZipStore` is measured here.
- **Writes and updates are not compared.** Icechunk's commits, history and
  transactions have no counterpart in the other three formats, which are
  rewritten to change.
- **Cold reads only.** No warm caches, repeated opens or concurrent readers.
- **Generated data.** The source files are synthetic, though they are real
  compressed netCDF4. `--input` runs the same comparison on any
  netCDF4/HDF5 files.
- **One run on one machine.** Times move by tens of milliseconds between
  runs. Request counts and bytes are deterministic for these library
  versions.

## Reproduce

See [`comparison/README.md`](comparison/README.md):

```bash
just compare-formats
just compare-formats --files 120 --days 30 --grid 90 180 --split 6 --no-full-read
```
