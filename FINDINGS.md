# vzip: virtual zarr as a ZIP overlay — findings

Exploratory prototype. Everything below was measured with the code in this
repo unless marked *untested*.

## The model

A virtual zarr store is an overlay of two key spaces with a decidable
classifier:

| key space   | value                                                       |
|-------------|-------------------------------------------------------------|
| bytes       | literal bytes (metadata documents, small/loaded chunks)     |
| reference   | a `Range` (the default) or a `Concat` of several ranges     |
| (neither)   | missing → zarr fill value                                   |

The schema is a few small protobuf messages ([proto/vzip.proto](proto/vzip.proto)).

- The archive has one **source table**: a deduplicated list of things bytes
  can come from. It is deflated, and the open request already fetches it.
  Each `Source` is one of:
  - `url`: an external object, absolute or relative to the archive
  - `key`: another entry of the same archive (an *internal reference*, i.e. a
    byte-range symlink), possibly deflated
  - `data`: literal bytes kept in the table itself, e.g. a decoding header
    shared by many chunks
- A **`Range`** is the default reference: `(source index, offset, length)`,
  or literal `data` for bytes unique to that one range. It matches what
  virtualizarr, kerchunk and icechunk store for a chunk.
- A **`Concat`** is a list of `Range`s whose value is their concatenation.
  Uses:
  - Virtual shards (`[file] ++ [shard index]`).
  - Chunks that need extra bytes in front before a codec can decode them. For
    example, JPEG-compressed TIFF tiles are stored without the JPEG tables
    that every tile shares. With the tables stored once as a `data` source,
    each tile is a `Concat` of `[source 7, 0..n] ++ [tile range]`, about 3
    bytes more than a plain `Range`, and the codec sees a complete stream
    (the mechanism is tested on synthetic headers; a real JPEG/TIFF file is
    *untested*).

Every kind of source costs the same per reference: a small integer. The writer
gives the most-used URL index 0, which costs zero bytes. The common case is a
bare `Range` of **6–9 bytes** on the wire, e.g. `18 cf 52 20 85 09`. The
codec is ~150 lines of hand-written Python ([pb.py](src/refstore/pb.py)), and a
test checks it byte-for-byte against the official protobuf runtime.

## Realization as a ZIP file

- **Bytes keys** are normal STORED zip entries.
- **Reference keys** are zip entries whose *central-directory* record has an
  extra field holding the reference: `0x7a76` for a `Range`, `0x7a77` for a
  `Concat`. The writer uses `Concat` only when there is more than one range.
  The entry body holds the same bytes, so naive tools see a well-formed file;
  to decode the body, look at which extra-field ID the entry carries.
- **Classifier**: `kind(key)` = `ref` if the CD record has `0x7a76` or
  `0x7a77`, else `bytes`; if there's no record, neither. It's decidable from
  the central directory alone.
- **Reserved `__vz__/` prefix** (zarr v3 reserves names starting with `__`)
  holds:
  - the source table (DEFLATE)
  - optional shard indexes
  - an optional CD page index
- **Archive comment** `vzip/1 + offsets` locates the source table and page index,
  so a reader needs at most two range requests to open any archive.
- **Metadata-late layout**: `zarr.json` documents are written between the
  source table and the central directory. The open request already contains all
  metadata, which gives you consolidated metadata with no consolidation step.
- **Valid zip**: verified with Python `zipfile`, Info-ZIP `unzip -t`/`zipinfo`,
  and `zarr.storage.ZipStore`.

### Does the "naive vs wise reader" idea work?

Yes. On the 3-file netCDF4 (HDF5, shuffle+zlib) fixture:

- `VZipStore(resolve=True)` → `xr.open_zarr` is `identical` to opening the
  netCDF files directly, both for a single file and for a 3-file `xr.concat`.
  That includes broadcast variables, where several keys alias one source chunk.
- `VZipStore(resolve=False)`, `zipfile` and `unzip` see the metadata as real
  JSON and see each chunk key as a small protobuf blob.
- **Caveat:** a naive *zarr* reader (`zarr.storage.ZipStore`) will try to
  decode those blobs as chunk data.
  - Here it failed loudly (`zlib: incorrect header check`), but only because
    the array was compressed.
  - For an uncompressed array it could return garbage of the wrong size.
  - If naive zarr readers must fail safely, mark references with an
    unregistered zip compression method instead. Naive unzip would then
    refuse those entries ("unsupported method") rather than return the
    payload. The trade-off is that `unzip -t` stops passing.

## Numbers

Synthetic array: N chunks spread over N/1000 files (1000 chunks per file),
compressed chunk lengths 50–500 kB, small gaps between chunks, ~95-character
URLs. Each run is a cold open plus reading one chunk.

### Size on disk (bytes per reference)

| format                               | 10⁴   | 10⁵   | 10⁶   |
|--------------------------------------|-------|-------|-------|
| kerchunk JSON                        | 134   | 135   | 136   |
| **vzip, one entry per chunk**        | 122   | 124   | 128   |
| vzip, one entry per chunk, no mirror | 111   | 113   | 116   |
| icechunk 2.2                         | 22    | 22    | 22    |
| **vzip, virtual shards**             | 8.2   | 8.0   | 8.0   |
| kerchunk parquet                     | 6.6   | 6.4   | 6.4   |

### Over HTTP: cold open + one chunk, 20 ms injected per request

Every request was logged by a local range server. All formats reference
`http://` targets.

| format (10⁶ refs)                    | requests | index bytes fetched |
|--------------------------------------|----------|---------------------|
| **vzip, per-chunk, paged CD**        | 4        | 0.15 MB             |
| **vzip, virtual shards**             | 4        | 0.16 MB             |
| kerchunk parquet                     | 4        | 0.64 MB             |
| icechunk                             | 11       | 16.7 MB             |
| vzip, per-chunk, unpaged             | 3        | 73.8 MB             |
| kerchunk JSON                        | 3        | 109 MB              |

Notes on the other formats:

- **icechunk**:
  - Its 11 requests include two 404 probes, a duplicate fetch of the repo
    file, and the manifest fetched as 5 parallel ranges.
  - The whole array has one manifest here. Manifest splitting would shrink
    it, but this benchmark uses the defaults.
- **kerchunk parquet** fetches `.zmetadata` twice, then one 100k-ref
  partition.

At 10⁴ refs, the virtual-shard archive needs **2 requests in total**: one tail
read (which contains everything) plus the chunk itself.

Raw data is in `experiments/results/`. Wall-clock times there are dominated by
Python overhead; don't read much into them.

## What I learned

1. **One zip entry per reference has a hard floor of ~118 B/ref.**
   - Each entry costs a 46 B CD header, a 30 B local header, the key written
     twice, and the extra field.
   - None of it can be compressed: zip compresses entry bodies, not
     headers.
   - On disk it is about the same size as kerchunk JSON.
2. **Read cost can still be fixed with a page index.**
   - Keep the CD sorted, and write a sparse index of (first key, CD offset)
     per ~64 KB page plus "pinned" metadata entries.
   - Opening is then 2 requests, and each cold key costs one ~64 KB page
     read, regardless of N.
   - It is still a 100% standard zip, and every key is still a visible entry.
   - Listing everything still reads the whole CD.
3. **Virtual shards are the best result** ([shards.py](src/refstore/shards.py)).
   - When every chunk in a block of the chunk grid comes from one file, store
     *one* reference per block: `[file bytes 0..end] ++ [zarr shard index]`.
   - The array metadata says `sharding_indexed`. zarr-python's own sharding
     codec then does the per-chunk lookup, and the store just maps its range
     requests onto the source file.
   - The per-chunk manifest becomes a **standard zarr shard index** (16 B per
     chunk, ~8 B after DEFLATE), stored in a hidden entry the reference points
     to through an internal (`key`) source.
   - Both `index_location` values work. "end" gives `[file] ++ [index]`;
     "start" gives `[index] ++ [file]`, with every chunk offset in the index
     shifted by the index size (16 B × chunks per shard; no checksum codec).
   - Size matches parquet; read cost matches the best of the others.
   - Works on real HDF5 with shuffle+zlib.
   - Limitations:
     - The shard grid must be regular, and each shard must come from exactly
       one file. Ragged inputs (e.g. months with different day counts) don't
       fit. zarr 3.4 now has rectilinear chunk grids, which could relax this
       (*untested*).
     - **Over-read on full-shard reads.** When a selection covers a whole
       shard, zarr-python fetches the whole shard value. Here that is the
       source file from byte 0, including other variables' data: 87 kB
       fetched vs 56 kB needed on the fixture, and worse for files with many
       variables. Partial-shard reads are exact, and zarr coalesces adjacent
       inner chunks. The fix belongs upstream: read the index first even
       for full-shard reads.
     - The shard index size is fixed by the zarr spec (no compression codecs
       on the index), so the DEFLATE happens at the zip layer.
4. **Internal references (`key` sources, intra-archive symlinks) are worth
   keeping in the model.**
   - It gives indirection without a new mechanism: shard indexes,
     deduplicated blobs, compressed side tables.
   - It also makes "a reference into the archive itself" expressible, so a
     bytes value and a reference value can share storage.
5. **Relative URLs** (resolved against the archive's URL) let an archive move
   together with its data files. That's cheap, and none of the existing
   formats make it the default.

## Comparison

|                          | kerchunk JSON   | kerchunk parquet          | icechunk                          | vzip                              |
|--------------------------|-----------------|---------------------------|-----------------------------------|-----------------------------------|
| objects                  | 1               | dir of N                  | dir of N (+ refs, snapshots)      | 1                                 |
| reader needs             | JSON            | parquet + fsspec layout   | the icechunk Rust lib             | zip CD parse + ~150 lines protobuf |
| lazy at 10⁶ refs         | no              | yes (partitions)          | per-manifest                      | yes (paged CD or shards)          |
| bytes/ref                | ~135            | ~6                        | ~22                               | ~130 per-chunk, ~8 sharded        |
| versioning/transactions  | no              | no                        | yes                               | no; immutable, write-once         |
| naive tooling            | text editor     | pandas                    | none                              | `unzip`, any zip lib              |
| write model              | rewrite         | rewrite                   | append/commit                     | stream once; rewrite to change    |

Where vzip is clearly worse:

- **No updates.** Appending means rewriting the archive. A small "base +
  delta" design, i.e. a stack of archives where later ones shadow earlier
  ones, would fit the overlay model (*untested*).
- **No transactions or history.** That is icechunk's real value
  proposition.

## Other ideas from brainstorming (untested)

- **Storage transformer instead of a container.** Zarr v3 has
  `storage_transformers` in array metadata. A "references" transformer would
  declare the classifier *in metadata*, e.g. "chunk keys of this array resolve
  through manifest key X". The overlay then works on any store: zip, S3
  prefix, local dir. This looks like the most portable and spec-aligned home
  for the idea, and it resembles the chunk-manifest transformer that has been
  floated in zarr-specs discussions. vzip's virtual shards could be one
  backend for it.
- **PMTiles-style archive.** PMTiles is a single-file, HTTP-range-optimized
  KV store:
  - a header, a root directory, and leaf directories
  - varint delta-encoded, compressed entries with run-lengths
  - ~1–2 B per entry for dense keys

  Replacing "offset into this file" with "(source index, offset, length)" gives a
  very compact, simple reference store. It has no naive-zip story, though.
- **The manifest as a zarr array.** Store `source:u32`, `offset:u64` and
  `length:u64` as zarr arrays shaped like the chunk grid (compressed,
  chunked). Every zarr implementation can then read the manifest with no new
  container format. Virtual shards are a specialization of this that zarr
  already understands.
- **SQLite.** One file, B-tree, readable over HTTP with page-range VFS
  readers. Ubiquitous, but page-at-a-time IO means many sequential requests
  per lookup.
- **Plain object-store overlay.** Concrete keys as objects, references as
  objects flagged by object metadata or content-type. It's trivially simple,
  but it costs one request per reference and isn't portable across stores.
  Fine for 10², bad for 10⁶.
- **Zip symlinks.** A zip entry with unix mode `S_IFLNK` and body
  `s3://bucket/file.nc#bytes=100-200` would make `unzip` extract references as
  symlinks. It's a cute naive view, but the target has to be text, not
  protobuf.

## Suggested next steps

- Run on a real large VirtualiZarr workload (e.g. a CMIP or NWM collection) to
  get a realistic URL/offset entropy and file count.
- Decide what the naive zarr reader should see when it hits a reference
  (failing silently vs refusing).
- Prototype the storage-transformer framing, with vzip shards as one backend,
  and see whether a spec text stays under a page.
- Raise the full-shard over-read with zarr-python (read the index first even
  for total-shard reads).

## Layout / reproduce

```
proto/vzip.proto          wire schema
src/refstore/pb.py        protobuf codec (no protoc)
src/refstore/archive.py   zip writer + CD/EOCD parsing, paged CD index
src/refstore/store.py     zarr Store: overlay resolver (obstore IO, request stats)
src/refstore/convert.py   virtualizarr Dataset -> vzip (per-chunk refs)
src/refstore/shards.py    virtualizarr Dataset -> vzip (virtual shards)
experiments/naive_view.py what naive tools see
experiments/bench.py      local size/latency comparison
experiments/http_bench.py request-level comparison over HTTP
```

```bash
uv run pytest tests
uv run python experiments/naive_view.py
RUST_LOG=error uv run python experiments/bench.py 10000 100000 1000000
RUST_LOG=error uv run python experiments/http_bench.py 10000 1000000
```
