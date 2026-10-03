# Virtual Zarr formats compared: kerchunk JSON, kerchunk Parquet, Icechunk and vzip

This document measures four ways of storing a virtual Zarr dataset (chunk
references into existing netCDF4/HDF5 files) on the same data, and reports
what was measured.

It is not a ranking. Each format makes different trade-offs, and which one
fits depends on the use case. vzip is this repository's format. We think it
has properties that suit some use cases, listed in
[Where each format fits](#where-each-format-fits), and these measurements
show what those properties cost and where vzip does worse.

The code is in [`comparison/`](comparison/README.md). Every number below can
be regenerated with `just compare-formats`.

## Measured facts in brief

All numbers are from the large run (259,200 references), except where noted.
Every value read, in every run and access pattern, matched the source files.

- **Size per reference:**
  - kerchunk Parquet: 3.0–3.2 bytes;
  - kerchunk JSON: 8.7 bytes with templates and gzip, 105 bytes plain;
  - Icechunk: 18–19 bytes, of which 6 are transaction history;
  - vzip: 125 bytes.
- **Opening the dataset:**
  - Every format opened in 0.05–0.17 s, except those that load every
    reference at open.
  - The formats that load every reference at open took 0.46–0.62 s and
    fetched 2.2–27 MB: kerchunk JSON, and vzip without a page index.
  - vzip with a page index made the fewest requests (2, 115 kB). kerchunk
    Parquet fetched the fewest bytes (4 requests, 13 kB).
- **One value or one map:** 0.02–0.14 s in every format.
- **A time series at one grid point (3,600 chunks):**
  - kerchunk JSON, kerchunk Parquet and default Icechunk: 7.4–7.6 s.
  - vzip without a page index: 9.3 s.
  - Icechunk with split manifests: 10.2 s. It reads 120 manifests.
  - vzip with a page index: 10.8 s. It reads 145 pages, 9.5 MB.
- **Everything (small run, 2,880 chunks):**
  - kerchunk: 6.1 s;
  - Icechunk: 6.7–6.8 s;
  - vzip: 7.5 s.

  vzip's reader made the same requests as kerchunk JSON's, and fetched the
  same bytes, so the extra time is spent in the reader.
- **Writing:**
  - Icechunk: 0.2–0.5 s;
  - kerchunk JSON: 0.8–1.0 s;
  - kerchunk Parquet: 1.2–1.3 s;
  - vzip: 1.4–1.5 s.
- **Updates:**
  - Only Icechunk supports updates in place: commits, history, branches and
    appends.
  - The other three are rewritten to change.
  - Updates were not measured.

## The formats

| | kerchunk JSON | kerchunk Parquet | Icechunk | vzip |
|---|---|---|---|---|
| what is written | one JSON file, optionally compressed | a directory: `.zmetadata` and Parquet files of references | a repository: config, branches, snapshots, manifests, transaction logs | one ZIP file |
| how a reader finds a reference | loads the whole file | loads the Parquet file holding it | loads the manifest holding it | reads the whole central directory at open, or with a page index, the 64 KiB page holding it |
| Zarr version presented | 2 (as written by VirtualiZarr) | 2 | 3 | 3 |
| changing it | rewrite | rewrite | commits, with history, branches and tags | rewrite |
| checks that referenced files haven't changed | none | none | optional: refuses chunks modified after a given time | optional: size, ETag or modification time per source (SPEC.md §6.1) |
| readers | fsspec's reference filesystem (Python) | fsspec's reference filesystem (Python) | the Icechunk library (Rust, with Python bindings) | this repository's Python store; independent Rust, TypeScript and Python readers; a browser reader; any ZIP tool for entries stored as bytes |
| written here with | `vds.vz.to_kerchunk(format="json")` | `vds.vz.to_kerchunk(format="parquet")` | `vds.vz.to_icechunk(session.store)` | `vzip.convert.write_vzip(vds, path)` |

The kerchunk files are written by VirtualiZarr's kerchunk writer, not by
kerchunk's own `SingleHdf5ToZarr` and `MultiZarrToZarr`. With chunks
of 1.2–38 kB, those tools would inline nothing at their default threshold
(500 bytes), so the references would be the same.

## Method

1. **Source files.**
   - netCDF4 files written with xarray and h5netcdf, one file per 30 days.
   - Each day holds float32 variables on a lat/lon grid, compressed with zlib
     and shuffle, and split into chunks.
   - The coordinates are `time`, `lat` and `lon`.
   - The data is seeded, so every run writes the same files.
2. **One virtual dataset.**
   - VirtualiZarr's HDF parser reads every file, with the coordinates loaded
     into memory.
   - The per-file datasets are concatenated along `time`.
   - References point to the source files by `http://` URL.
3. **Write** that dataset in each format twice: with the library's defaults,
   and with one setting changed for this workload:

   | format | default | tuned |
   |---|---|---|
   | kerchunk JSON | plain JSON | one URL template for the common prefix, gzip |
   | kerchunk Parquet | `record_size=100,000` | `record_size=10,000` |
   | Icechunk | default repository config | manifests split every 30 time steps (one per source file); readers make one request per object instead of splitting reads into ranges |
   | vzip | no page index | page index, 64 KiB pages |

   For the formats with an index of references, the tuned setting is that
   index's granularity, aimed at tens of kilobytes per index read. kerchunk
   JSON has no granularity to set, so its tuned setting is templates and
   compression.
4. **Serve** the source files and the written formats from one local HTTP
   server, which logs every request.
   - It honors Range requests.
   - It holds each response for 20 ms plus its size at 100 MB/s, a stand-in
     for an object store.
   - It runs in its own process and times the wait by watching the clock,
     because on macOS `time.sleep(0.02)` can last 160 ms.
5. **Read** each variant in each access pattern, each time from a cold open,
   five times:
   - **open:** `xr.open_zarr(store, chunks=None)`, with xarray's default
     consolidation;
   - **point:** one element of `tas`;
   - **time series:** every time step of `tas` at the middle grid point;
   - **map:** the whole grid of `tas` at the middle time step;
   - **everything:** every variable in full, small run only.
6. **Check** every value read against the same values read from the source
   files.

Openers: kerchunk through fsspec's reference filesystem, created
asynchronously and passed to `zarr.storage.FsspecStore`; Icechunk through
`Repository.open(icechunk.http_storage(url))`; vzip through
`vzip.store.VZipStore(url)`.

`chunks=None` matters for kerchunk. With xarray's default dask chunking,
xarray tokenizes the store by pickling it, and fsspec's reference filesystem
unpickles by downloading its references again. That tripled kerchunk JSON's
open cost in an earlier version of this comparison.

Library versions: vzip 0.1.0 (this repository), VirtualiZarr 2.7.3, kerchunk
0.2.10, fsspec 2026.9.0, Icechunk 2.2.2, zarr-python 3.4.0, xarray 2026.9.0;
Python 3.12 on macOS (Apple silicon). `uv.lock` pins the exact environment.

### How this comparison changed

The first version of this comparison measured vzip unfavorably at open, and
we changed vzip's reader and converter in response:
- consolidated metadata (#23);
- a listing that seeks through the page index (#22);
- coordinate chunks stored with the metadata (#22).

An adversarial review then pointed out four problems:
- the other formats still ran on their defaults;
- kerchunk was opened through a sync wrapper;
- dask's pickling tripled kerchunk's open cost;
- the comparison measured only one access pattern.

This version gives every format a default and a tuned variant, opens all of
them the same way, and adds access patterns, write times, bandwidth and
repeated runs.

## Results

Cells under "Requests" read **requests (of which to the format's own files)
· bytes of the format's own files fetched**. The source data fetched is the
same for every format and is not shown. Times are the median of five cold
runs, with the range in parentheses. Raw results, with every repeat, are in
[`comparison/results/`](comparison/results/).

### Large run: 259,200 references

120 files, 90 × 180 grid in 6 × 6 chunks, 2 variables.

**Size and write time**

| format | setting | size | objects | write |
|---|---|---:|---:|---:|
| kerchunk JSON | default | 27.21 MB | 1 | 0.75 s |
| kerchunk JSON | tuned | 2.24 MB | 1 | 1.01 s |
| kerchunk Parquet | default | 768.3 kB | 8 | 1.31 s |
| kerchunk Parquet | tuned | 833.2 kB | 30 | 1.22 s |
| Icechunk | default | 4.90 MB (of which history 1.52 MB) | 13 | 0.21 s |
| Icechunk | tuned | 4.74 MB (of which history 1.52 MB) | 254 | 0.54 s |
| vzip | default | 32.39 MB | 1 | 1.36 s |
| vzip | tuned | 32.39 MB | 1 | 1.50 s |

**Requests (of which to the format's own files) · bytes of the format's own files**

| format | setting | open | point | time series | map |
|---|---|---|---|---|---|
| kerchunk JSON | default | 2 (2) · 27.21 MB | 1 (0) · 0 | 3,600 (0) · 0 | 36 (0) · 0 |
| kerchunk JSON | tuned | 2 (2) · 2.24 MB | 1 (0) · 0 | 3,600 (0) · 0 | 36 (0) · 0 |
| kerchunk Parquet | default | 4 (4) · 12.9 kB | 2 (1) · 269.0 kB | 3,602 (2) · 353.4 kB | 37 (1) · 269.0 kB |
| kerchunk Parquet | tuned | 4 (4) · 12.6 kB | 2 (1) · 30.1 kB | 3,613 (13) · 391.2 kB | 37 (1) · 30.1 kB |
| Icechunk | default | 11 (11) · 11.7 kB | 6 (5) · 1.66 MB | 3,605 (5) · 1.66 MB | 41 (5) · 1.66 MB |
| Icechunk | tuned | 13 (13) · 20.1 kB | 2 (1) · 13.5 kB | 3,720 (120) · 1.58 MB | 37 (1) · 13.5 kB |
| vzip | default | 2 (2) · 18.81 MB | 1 (0) · 0 | 3,600 (0) · 0 | 36 (0) · 0 |
| vzip | tuned | 2 (2) · 114.7 kB | 2 (1) · 65.5 kB | 3,745 (145) · 9.46 MB | 37 (1) · 65.5 kB |

**Seconds, median (min–max)**

| format | setting | open | point | time series | map |
|---|---|---|---|---|---|
| kerchunk JSON | default | 0.62 (0.47–0.70) | 0.02 (0.02–0.02) | 7.44 (7.44–7.68) | 0.09 (0.09–0.09) |
| kerchunk JSON | tuned | 0.46 (0.38–0.52) | 0.02 (0.02–0.02) | 7.60 (7.44–7.64) | 0.09 (0.09–0.09) |
| kerchunk Parquet | default | 0.09 (0.09–0.09) | 0.05 (0.05–0.05) | 7.52 (7.50–7.61) | 0.11 (0.11–0.11) |
| kerchunk Parquet | tuned | 0.09 (0.09–0.09) | 0.04 (0.04–0.04) | 7.60 (7.59–7.69) | 0.11 (0.11–0.11) |
| Icechunk | default | 0.13 (0.13–0.13) | 0.06 (0.06–0.06) | 7.60 (7.58–7.68) | 0.14 (0.12–0.14) |
| Icechunk | tuned | 0.17 (0.17–0.17) | 0.04 (0.04–0.04) | 10.22 (10.14–10.32) | 0.13 (0.11–0.13) |
| vzip | default | 0.62 (0.50–0.69) | 0.02 (0.02–0.03) | 9.28 (9.17–9.34) | 0.11 (0.11–0.11) |
| vzip | tuned | 0.05 (0.05–0.05) | 0.05 (0.05–0.05) | 10.83 (10.61–11.06) | 0.13 (0.13–0.14) |

Values: every value read matched the source files.

### Small run: 2,880 references

12 files, 180 × 360 grid in 2 × 2 chunks, 2 variables. This run includes
reading everything.

**Size and write time**

| format | setting | size | objects | write |
|---|---|---:|---:|---:|
| kerchunk JSON | default | 314.2 kB | 1 | 0.01 s |
| kerchunk JSON | tuned | 33.5 kB | 1 | 0.01 s |
| kerchunk Parquet | default | 21.8 kB | 6 | 0.08 s |
| kerchunk Parquet | tuned | 21.5 kB | 6 | 0.02 s |
| Icechunk | default | 62.7 kB (of which history 12.8 kB) | 13 | 0.02 s |
| Icechunk | tuned | 67.1 kB (of which history 12.8 kB) | 35 | 0.01 s |
| vzip | default | 379.9 kB | 1 | 0.02 s |
| vzip | tuned | 380.3 kB | 1 | 0.04 s |

**Requests (of which to the format's own files) · bytes of the format's own files**

| format | setting | open | point | time series | map | everything |
|---|---|---|---|---|---|---|
| kerchunk JSON | default | 2 (2) · 314.2 kB | 1 (0) · 0 | 360 (0) · 0 | 4 (0) · 0 | 2,880 (0) · 0 |
| kerchunk JSON | tuned | 2 (2) · 33.5 kB | 1 (0) · 0 | 360 (0) · 0 | 4 (0) · 0 | 2,880 (0) · 0 |
| kerchunk Parquet | default | 4 (4) · 6.7 kB | 2 (1) · 6.8 kB | 361 (1) · 6.8 kB | 5 (1) · 6.8 kB | 2,882 (2) · 15.0 kB |
| kerchunk Parquet | tuned | 4 (4) · 6.6 kB | 2 (1) · 6.8 kB | 361 (1) · 6.8 kB | 5 (1) · 6.8 kB | 2,882 (2) · 14.9 kB |
| Icechunk | default | 10 (10) · 8.5 kB | 10 (5) · 20.0 kB | 1,805 (5) · 20.0 kB | 25 (5) · 20.0 kB | 14,410 (10) · 41.3 kB |
| Icechunk | tuned | 10 (10) · 9.5 kB | 2 (1) · 1.8 kB | 372 (12) · 21.8 kB | 5 (1) · 1.8 kB | 2,904 (24) · 44.7 kB |
| vzip | default | 2 (2) · 227.8 kB | 1 (0) · 0 | 360 (0) · 0 | 4 (0) · 0 | 2,880 (0) · 0 |
| vzip | tuned | 2 (2) · 82.8 kB | 2 (1) · 65.6 kB | 363 (3) · 145.2 kB | 5 (1) · 65.6 kB | 2,884 (4) · 210.8 kB |

**Seconds, median (min–max)**

| format | setting | open | point | time series | map | everything |
|---|---|---|---|---|---|---|
| kerchunk JSON | default | 0.05 (0.05–0.16) | 0.02 (0.02–0.02) | 0.77 (0.77–0.78) | 0.02 (0.02–0.02) | 6.12 (6.11–6.18) |
| kerchunk JSON | tuned | 0.05 (0.05–0.06) | 0.02 (0.02–0.02) | 0.77 (0.77–0.78) | 0.02 (0.02–0.02) | 6.11 (6.10–6.12) |
| kerchunk Parquet | default | 0.09 (0.09–0.09) | 0.04 (0.04–0.04) | 0.79 (0.79–0.81) | 0.04 (0.04–0.04) | 6.16 (6.15–6.18) |
| kerchunk Parquet | tuned | 0.09 (0.09–0.09) | 0.04 (0.04–0.04) | 0.79 (0.79–0.81) | 0.04 (0.04–0.04) | 6.15 (6.15–6.17) |
| Icechunk | default | 0.13 (0.13–0.13) | 0.04 (0.04–0.04) | 0.92 (0.90–0.93) | 0.10 (0.08–0.10) | 6.78 (6.73–6.80) |
| Icechunk | tuned | 0.13 (0.13–0.13) | 0.04 (0.04–0.04) | 1.03 (1.03–1.03) | 0.04 (0.04–0.04) | 6.68 (6.67–6.71) |
| vzip | default | 0.05 (0.05–0.05) | 0.02 (0.02–0.03) | 0.93 (0.91–0.97) | 0.03 (0.03–0.03) | 7.48 (7.41–7.49) |
| vzip | tuned | 0.04 (0.04–0.04) | 0.05 (0.05–0.05) | 0.96 (0.95–1.01) | 0.05 (0.05–0.06) | 7.54 (7.50–7.61) |

Values: every value read matched the source files.

### Many variables: 57,600 references

12 files, 90 × 180 grid in 2 × 2 chunks, 40 variables.

**Size and write time**

| format | setting | size | objects | write |
|---|---|---:|---:|---:|
| kerchunk JSON | default | 6.21 MB | 1 | 0.14 s |
| kerchunk JSON | tuned | 575.0 kB | 1 | 0.24 s |
| kerchunk Parquet | default | 306.5 kB | 44 | 0.57 s |
| kerchunk Parquet | tuned | 304.4 kB | 44 | 0.32 s |
| Icechunk | default | 954.9 kB (of which history 159.4 kB) | 51 | 0.13 s |
| Icechunk | tuned | 1.04 MB (of which history 160.4 kB) | 491 | 0.12 s |
| vzip | default | 7.40 MB | 1 | 0.32 s |
| vzip | tuned | 7.41 MB | 1 | 0.33 s |

**Requests (of which to the format's own files) · bytes of the format's own files**

| format | setting | open | point | time series | map |
|---|---|---|---|---|---|
| kerchunk JSON | default | 2 (2) · 6.21 MB | 1 (0) · 0 | 360 (0) · 0 | 4 (0) · 0 |
| kerchunk JSON | tuned | 2 (2) · 575.0 kB | 1 (0) · 0 | 360 (0) · 0 | 4 (0) · 0 |
| kerchunk Parquet | default | 4 (4) · 20.1 kB | 2 (1) · 6.7 kB | 361 (1) · 6.7 kB | 5 (1) · 6.7 kB |
| kerchunk Parquet | tuned | 4 (4) · 19.9 kB | 2 (1) · 6.7 kB | 361 (1) · 6.7 kB | 5 (1) · 6.7 kB |
| Icechunk | default | 10 (10) · 9.4 kB | 8 (5) · 20.0 kB | 1,085 (5) · 20.0 kB | 17 (5) · 20.0 kB |
| Icechunk | tuned | 10 (10) · 21.8 kB | 2 (1) · 1.8 kB | 372 (12) · 21.6 kB | 5 (1) · 1.8 kB |
| vzip | default | 2 (2) · 4.33 MB | 1 (0) · 0 | 360 (0) · 0 | 4 (0) · 0 |
| vzip | tuned | 2 (2) · 162.5 kB | 2 (1) · 65.6 kB | 363 (3) · 196.7 kB | 5 (1) · 65.6 kB |

**Seconds, median (min–max)**

| format | setting | open | point | time series | map |
|---|---|---|---|---|---|
| kerchunk JSON | default | 0.17 (0.14–0.28) | 0.02 (0.02–0.02) | 0.77 (0.75–0.79) | 0.02 (0.02–0.02) |
| kerchunk JSON | tuned | 0.12 (0.11–0.13) | 0.02 (0.02–0.02) | 0.76 (0.75–0.76) | 0.02 (0.02–0.02) |
| kerchunk Parquet | default | 0.09 (0.09–0.11) | 0.04 (0.04–0.04) | 0.78 (0.77–0.78) | 0.04 (0.04–0.04) |
| kerchunk Parquet | tuned | 0.09 (0.09–0.10) | 0.04 (0.04–0.04) | 0.78 (0.77–0.78) | 0.04 (0.04–0.04) |
| Icechunk | default | 0.13 (0.13–0.13) | 0.04 (0.04–0.04) | 0.86 (0.84–0.86) | 0.07 (0.06–0.08) |
| Icechunk | tuned | 0.20 (0.20–0.20) | 0.04 (0.04–0.04) | 1.02 (1.01–1.03) | 0.04 (0.04–0.04) |
| vzip | default | 0.17 (0.16–0.17) | 0.02 (0.02–0.02) | 0.94 (0.92–0.96) | 0.03 (0.03–0.03) |
| vzip | tuned | 0.05 (0.05–0.05) | 0.05 (0.05–0.05) | 0.98 (0.96–1.03) | 0.05 (0.05–0.05) |

Values: every value read matched the source files.

## What the numbers show

### Size

| format | bytes per reference (large run) |
|---|---:|
| kerchunk Parquet | 3.0 (default), 3.2 (tuned) |
| kerchunk JSON | 105 (plain), 8.7 (templates and gzip) |
| Icechunk | 18.9 (default), 18.3 (tuned); 13.0 and 12.4 without transaction history |
| vzip | 125 |

- **kerchunk Parquet** is a columnar, compressed table of highly regular
  values: few URLs and increasing offsets.
- **Icechunk's** size includes a transaction log that records what each
  commit changed. That log is not read to serve data.
- **vzip** stores one ZIP entry per chunk: a 30-byte local header, a 46-byte
  central directory record, the key twice, and the reference payload twice.
  - The second copy of the payload is the entry's body, so that ZIP tools
    show it (SPEC.md §4.3); writers can leave it out.
  - vzip has no compression of the references, and no setting changes its
    size much.

### Writing

All four writers took between 0.2 and 1.5 s for 259,200 references. Icechunk
was fastest and vzip slowest. vzip's writer is the reference Python
implementation in this repository.

### Opening

- **Formats that load every reference at open pay for it at open.**
  - kerchunk JSON fetched 27 MB plain, or 2.2 MB with templates and gzip.
  - vzip without a page index fetched its 18.8 MB central directory.
  - Both took about 0.5–0.6 s with 100 MB/s per response.
- **Formats with an index load little at open:**
  - kerchunk Parquet: 4 requests, 13 kB;
  - Icechunk: 11–13 requests, 12–20 kB;
  - vzip with a page index: 2 requests, 115 kB.
  - Their opens took 0.05–0.17 s.
- **With 40 variables**, the ordering is the same:
  - kerchunk Parquet: 4 requests, 20 kB;
  - Icechunk: 10 requests, 9–22 kB;
  - vzip with a page index: 2 requests, 163 kB;
  - kerchunk JSON and vzip without a page index: 0.6–6.2 MB.

### One value, one map

Every format read a single value in 0.02–0.06 s and a map (36 chunks) in
0.09–0.14 s.

The first read of an indexed format also fetches the index piece that holds
the reference. In the large run that was:

| format | default | tuned |
|---|---:|---:|
| kerchunk Parquet | 269 kB | 30 kB |
| Icechunk | 1.66 MB | 13.5 kB |
| vzip | – (no index) | 65.5 kB (one page) |

At 100 MB/s these differences are a few milliseconds to a few tens of
milliseconds.

### A time series at one grid point

This pattern touches one chunk in each of 3,600 time steps.

- kerchunk JSON and Parquet and default Icechunk took 7.4–7.6 s.
- The finer-grained indexes did worse on it, because the time series crosses
  every piece of the index:
  - Icechunk with one manifest per file read 120 manifests (1.6 MB) and took
    10.2 s.
  - vzip with a page index read 145 pages (9.5 MB) and took 10.8 s, because
    its keys are sorted by variable and then chunk index, and one grid
    point's chunks are spread over every page of `tas`.
- vzip without a page index fetched nothing beyond the chunks, as kerchunk
  JSON did, but took 9.3 s against 7.4–7.6 s. The difference is in the
  reader.

So the setting that helps a point read can hurt a time series. The right
granularity depends on the access pattern.

### Everything

In the small run, every format read 2,880 chunks:
- kerchunk: 6.1 s;
- Icechunk: 6.7–6.8 s;
- vzip: 7.5 s.

Default Icechunk made 14,410 requests, because by default its HTTP store
splits each read into five ranges. With one request per object it made
2,904. vzip and kerchunk JSON made the same requests, so vzip's extra 1.4 s
is in its reader.

## Where each format fits

This section describes properties, not scores. Several of them were not
measured here.

- **kerchunk JSON** is the simplest: one text file that any JSON tool can
  read and edit. With templates and gzip it is small, but every reader loads
  all of it.
- **kerchunk Parquet** is the most compact here, and opens with little data.
  It is a directory of files, read in Python through fsspec.
- **Icechunk** is the only one of the four with transactions, history,
  branches and appends. It is the natural choice for a dataset that changes
  over time, or that several writers update. It is a repository of many
  objects, read through the Icechunk library.
- **vzip**'s properties may suit some cases:
  - **One file.** One object to copy, upload, attach or delete.
  - **A plain ZIP file.** Any ZIP tool lists its keys and reads its bytes
    entries, such as `zarr.json` documents.
  - **Bytes and references in one key space.** An archive can hold real
    chunks (for example, edited ones) and references side by side.
  - **Sources pinned by size, ETag or modification time.** A reader can
    refuse data that changed. Icechunk has a similar check, by modification
    time.
  - **Independent readers checked by one conformance suite:** Rust,
    TypeScript and Python, and a reader for the browser.

  Its costs, as measured here:
  - It is the largest of the four: 1.2× plain kerchunk JSON, about 7×
    Icechunk, 14× compressed kerchunk JSON and 40× kerchunk Parquet.
  - It is the slowest to write.
  - Its Python reader is 20–25% slower than kerchunk's on the same requests.

  Other costs follow from its design:
  - Without a page index, it loads every reference at open.
  - With one, a read that crosses every page, such as a time series, fetches
    the whole index piece by piece.
  - It has no way to update an archive except rewriting the file.

## Limits

- **One local server.** Real object stores have variable latency,
  per-request costs and limits on concurrent requests. The 20 ms and
  100 MB/s per response are assumptions.
- **Generated data.** The sources are synthetic but real compressed netCDF4.
  `--input` runs the same comparison on any netCDF4/HDF5 files. Many small
  files, non-HDF sources, deep hierarchies and tens of millions of
  references were not tested.
- **Python readers only.** vzip's other readers, and Icechunk's or kerchunk's
  readers in other languages, were not measured.
- **One tuned setting per format.** Others exist, and could change results:
  Icechunk's manifest preloading, Parquet's categorical threshold, and vzip's
  virtual shards (`vzip.shards`), for example.
- **Not measured at all:**
  - updates and appends;
  - concurrent writers;
  - warm caches;
  - repeated opens in one process;
  - memory use.
- **One machine.** Times vary by a few percent between repeats; the ranges
  are in the tables. Request counts and bytes are the same in every repeat.

## Reproduce

See [`comparison/README.md`](comparison/README.md):

```bash
just compare-formats
just compare-formats --files 120 --days 30 --grid 90 180 --split 6 --no-full-read
just compare-formats --files 12 --days 30 --grid 90 180 --variables 40 --no-full-read
```
