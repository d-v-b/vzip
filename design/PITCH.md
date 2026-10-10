# Why vzip

## The problem

Petabytes of scientific data sit in HDF5, netCDF, GRIB and TIFF files on
object storage. [kerchunk](https://github.com/fsspec/kerchunk) showed that
Zarr readers can read those files in place, without copying them, if someone
writes down where each chunk lives: *this chunk is bytes 4,096–135,000 of
`s3://bucket/file.nc`*. This is "virtual Zarr".
[VirtualiZarr](https://github.com/zarr-developers/VirtualiZarr) grew out of
kerchunk and makes virtual datasets easy to build and combine with xarray.
vzip exists because of both projects; see
[Where the ideas come from](#where-the-ideas-come-from).

The hard part is storing those references. A virtual dataset is a Zarr store
in which some keys hold real bytes (metadata, small coordinate arrays) and
others hold "go read these bytes over there". Today there are three ways to
store that, and each one costs something:

- **kerchunk JSON**:
  - ~135 bytes per reference;
  - the whole document has to be downloaded and parsed before anything can be
    read: 109 MB for a million chunks.
- **kerchunk Parquet**:
  - compact, but a directory of files in an fsspec-specific layout;
  - reading it needs a Parquet library, plus the conventions of one Python
    ecosystem.
- **Icechunk**:
  - versioned, transactional and fast;
  - but a reader is a Rust library;
  - its formats are defined by its implementation;
  - it is maintained by one company.

  That's the right trade if you need commits and branches. It's a lot to
  depend on if you only need to publish a set of references.

There is no small, boring format for this: one you could read from a
specification in an afternoon, in any language, with tools you already have.

## Status

Format version 0 (`vzip/0`) is **provisional**, as of
[spec revision 8](../spec/archive.md). The format is believed complete and
implementers may rely on it, but it can still change in response to
feedback. Any incompatible change will be announced in the
[changelog](../spec/history/archive/REVISIONS.md). Feedback is welcome.

## The idea

vzip stores a virtual dataset as **one ordinary ZIP file**:

- Bytes keys (metadata, small arrays) are normal ZIP entries.
- Reference keys are ZIP entries whose directory record carries a small
  extra field. In it, a few bytes of protobuf say "bytes `[offset, offset +
  length)` of source *n*".
- Sources (external URLs, inline header bytes, or other entries of the same
  archive) are listed once in a table and referred to by number.

A program that has never heard of vzip still sees a valid archive:

- `unzip -l` lists every key;
- `unzip -p` prints the metadata;
- reference keys show up as small files holding their encoded reference.

A vzip reader sees the virtual dataset.

## Where the ideas come from

vzip is a container for ideas that kerchunk and VirtualiZarr worked out.
Very little in its data model is new.

- **[kerchunk](https://github.com/fsspec/kerchunk)** (Martin Durant and
  contributors, part of the fsspec project) introduced virtual Zarr. It scans
  HDF5, netCDF, GRIB, TIFF and FITS files for the locations of their chunks,
  and fsspec's `ReferenceFileSystem` serves those references so that Zarr
  reads the files as if they were Zarr. vzip stores the mapping that kerchunk's
  [reference specification](https://fsspec.github.io/kerchunk/spec.html)
  defines:
  - **The data model:** a key's value is either inline data or
    `[url, offset, length]`. These are vzip's bytes entries and `Range`
    references.
  - **Inline binary data:** values prefixed `base64:` carry binary data
    inline. These are vzip's literal ranges and `data` sources.
  - **Shared URLs:** `templates` let many references share one URL. In vzip,
    references name an entry of the source table by number.
  - **Binary storage:** the version 1 specification already anticipated
    "future possible binary storage" of references. vzip is one such encoding.

  kerchunk's Parquet layout showed how compact references can be:
  dictionary-encoded paths, columns of offsets and lengths, read lazily in
  pieces. It is the bar vzip's size and request counts are measured against
  [below](#what-it-costs-measured).
- **[VirtualiZarr](https://github.com/zarr-developers/VirtualiZarr)**
  (started by Tom Nicholas, now a zarr-developers project) recast virtual Zarr
  in Zarr's own terms. It grew out of discussions on kerchunk
  ([fsspec/kerchunk#377](https://github.com/fsspec/kerchunk/issues/377)).
  - **Chunk manifests:** VirtualiZarr's chunk manifests (a path, offset and
    length for every chunk of an array) are what vzip's references record.
  - **The code:** vzip's Python converters
    ([convert.py](../python/src/vzip/convert.py), [shards.py](../python/src/vzip/shards.py)) take
    VirtualiZarr's `ManifestArray` datasets as input and reuse helpers from
    its Icechunk writer. The benchmarks build their datasets with its
    `ChunkManifest`.
  - **The open problem:** its roadmap names a Zarr-native on-disk chunk
    manifest format as future work
    ([zarr-specs#287](https://github.com/zarr-developers/zarr-specs/issues/287),
    Joe Hamman's manifest storage transformer proposal). vzip explores one
    container for such manifests.
  - **Virtual shards:** a virtual shard stores a source file's manifest as a
    Zarr shard index, so the manifest idea works inside Zarr's own sharding
    codec.
- **[Icechunk](https://icechunk.io/)** (Earthmover) checks virtual chunks
  against the ETag or modification time of the object they point into. vzip's
  source pins are the same idea.

What vzip adds is narrow: a single-file container (ZIP) and a binary encoding,
defined by one document with a conformance suite, so that references can be
read without fsspec, Python, or a particular storage engine. It is meant to
sit alongside these tools, not replace them. VirtualiZarr could write vzip as
one more output format, as it writes kerchunk references and Icechunk today.

## Why ZIP

- **It is already cloud-optimized.** The index (the central directory) is at
  the end of the file. One range request for the tail finds everything else.
  ZIP is supported almost everywhere: every language, every OS, every archive
  tool.
- **It is one object.** Copy it, cache it, checksum it, put it behind a CDN,
  attach it to a DOI. There is no directory layout to keep consistent.
- **The overlay comes for free.** Whether a key is bytes or a reference is
  decided from its own directory record. No second index has to agree with
  the first.
- **Nobody owns it.** ZIP is documented by PKWARE's APPNOTE, and protobuf's
  wire format by a public spec. The vzip rules on top are [one
  document](../spec/archive.md). Every archive records its format version (`vzip/0`
  today), so a reader knows whether it can read an archive before it tries.

## What it costs, measured

Measured on a synthetic million-chunk dataset, opening it and reading one
chunk over HTTP with 20 ms of latency per request:

| format | requests | index bytes fetched | size on disk |
|---|---|---|---|
| vzip, virtual shards | 4 | 0.17 MB | 8 B / ref |
| vzip, one entry per chunk + page index | 4 | 0.15 MB | 128 B / ref |
| kerchunk Parquet | 4 | 0.64 MB | 6 B / ref |
| Icechunk | 11 | 16.7 MB | 22 B / ref |
| kerchunk JSON | 3 | 109 MB | 136 B / ref |

Opening a small dataset, with all metadata included, takes **one request**:
the writer places the metadata documents next to the directory at the end of
the file.

"Virtual shards" are not a separate feature. They are a usage pattern: one
reference per source file, made of the file's bytes followed by a standard
Zarr shard index. Zarr's own sharding codec then does the per-chunk lookup.

## Simple enough to implement from the spec

To check the "afternoon" claim, agents implemented vzip from
[spec/archive.md](../spec/archive.md) alone, in Rust, TypeScript and Python, with no access to
any existing code. This was done in seven rounds, with fresh agents each time.
After each round, their notes and every disagreement between them went back
into the spec ([spec/history/archive/REVISIONS.md](../spec/history/archive/REVISIONS.md)).

- **Size:** each implementation, reader plus writer, is about 1,500–2,100
  lines.
- **Round 1:** every implementation passed the conformance suite and read the
  others' archives. But on 11 queries they gave different answers that the
  spec allowed.
- **Rounds 2–7:** fresh agents again passed everything, and on every
  tested query all of them, plus the reference, gave the same answer. That
  includes error classes.

Rounds 5–7 added HTTP, which all three implementations support. The
round-7 implementations are in [conformance/impls/](../conformance/impls/).

## What vzip is not

- **Not a version-control system.** It has no commits, branches or
  transactions. To change a vzip, write a new one. If you need history,
  concurrent writers or time travel, use Icechunk.
- **Not tied to Zarr.** Keys are strings and values are bytes. Zarr is the
  motivating case, not a dependency.
- **Not a replacement for real Zarr data.** It references bytes that live in
  other formats, with their chunking and compression.

## Who it's for

- **Data producers** who want to publish an analysis-ready "view" of an
  archive of netCDF/HDF5 files as one file next to the data.
- **Tool authors** who want to read virtual datasets without taking on a
  large dependency.
- **Anyone archiving a virtual dataset** for the long term, who wants a format
  a future reader can rebuild from a short document.

## Open questions

- **Stale references.** A reference points at bytes the archive doesn't
  control. If a producer regenerates a file in place, a reference written
  earlier silently returns the new file's values; this was measured with
  netCDF files whose layout didn't change. Icechunk guards against this by
  pinning each reference to the object's ETag or last-modified time, and
  checking with a conditional request. vzip took the same idea as source pins
  ([spec/archive.md §6.1](../spec/archive.md), since revision 3), per source rather than per
  chunk. What remains open is that browsers often cannot check pins: servers
  must expose the headers to cross-origin pages
  ([open feedback](../spec/history/archive/REVISIONS.md)).
- **Updates.** Appending a day of data means rewriting the archive, or
  stacking archives where later ones shadow earlier ones (not yet designed).
- **Ragged sources.** Virtual shards need source files that tile a regular
  grid. Zarr's newer rectilinear chunk grids may lift that restriction.
