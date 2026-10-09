# Proposal: source pins (for spec revision 3)

## What icechunk does

The icechunk 2.2.2 `VirtualChunkSpec` holds `location, offset, length`, plus
one optional checksum:

- `etag_checksum: str`: "refuse to serve data from this chunk if the etag has
  changed";
- `last_updated_at_checksum: datetime`: "refuse to serve data if [the object]
  has been modified after this time".

VirtualiZarr exposes the second as `to_icechunk(..., last_updated_at=...)`,
one timestamp for every reference written in a session.

Observed on the wire ([design/experiments/icechunk_checksums.py](../experiments/icechunk_checksums.py)),
against an HTTP server that logs headers and honours conditional requests:

| reference | header added to the ranged GET | object unchanged | rewritten 2 s later |
|---|---|---|---|
| no checksum | none | ok | **new bytes returned silently** |
| `last_updated_at` = now | `If-Unmodified-Since: <HTTP-date>` | ok | error (412) |
| ETag | `If-Match: <etag>` | ok | error (412) |
| `last_updated_at` = 2 h ago (before the object's mtime) | `If-Unmodified-Since` | **error (false positive)** | error |

Points about how this works:

- **No extra requests.** The check rides on the chunk's own range request,
  and the server does the comparison.
- **ETags are sent unquoted.** For example, icechunk sent `If-Match: 0c84…`
  where RFC 9110 requires `"0c84…"`. A strict server answered 412 for an
  unchanged object. S3 tolerates this; other servers may not.
- **One-second resolution.** HTTP dates have one-second resolution. When the
  object was rewritten within the same second as `last_updated_at`, the stale
  reference returned the new bytes with no error.
- **The pin is per chunk reference**, although it describes the containing
  object. Every chunk of a file repeats the same value.

## Why it's needed: the use case

A virtual reference names bytes the archive doesn't control. Producers
regularly re-publish files under the same name: forecast cycles, reprocessed
granules, files that get appended to. HDF5 often lays out a regenerated file
identically.

[design/experiments/stale_source.py](../experiments/stale_source.py) writes a vzip
for a netCDF file, regenerates the file with different values, and reads the
archive again. For both an uncompressed and a zlib-compressed variable, the
reader **silently returns the new file's values**. There is no error and no
decode failure, just wrong data. vzip r2 has no defence against this.

## Proposal

The version of an object is a property of the object, not of each range read
from it. So pins belong on the `url` source, where vzip already lists each
object once:

```proto
message Source {
  oneof kind { string url = 1; string key = 2; bytes data = 3; }
  // Pins: assertions about the object a `url` source names. Allowed only
  // with `url`. A reader MUST check every pin present before returning bytes
  // from the source; a failed or uncheckable pin is a resolution error.
  optional uint64 size = 4;                // total size of the object in bytes
  optional string etag = 5;                // strong entity tag, with quotes, e.g. "\"abc\""
  optional int64  modified_not_after = 6;  // seconds since the Unix epoch
}
```

Checking, per scheme:

| pin | `http(s):` (no extra request) | `file:` |
|---|---|---|
| `size` | total length from the 206 response's `Content-Range` | `stat` size |
| `etag` | send `If-Match: <etag>`; 412 → error. Weak tags (`W/`) are not allowed. | uncheckable → resolution error |
| `modified_not_after` | send `If-Unmodified-Since: <HTTP-date>`; 412 → error | `stat` mtime, truncated to seconds, must be ≤ the pin |

Writer guidance:

- **Take `modified_not_after` from the object, not the clock.** Use the
  object's own `Last-Modified` value (from the HEAD or GET the scan already
  did), not the writer's clock. That avoids both the false positive in row 4
  above and clock skew between writer and server.
- **Pair it with `size`.** The one-second window can't be closed, but `size`
  catches appends and most rewrites, and costs nothing to check.
- **Prefer `etag`** where the store has strong ETags. It is exact.
- **Know what pins do to relocation.** Copying the data elsewhere changes
  `Last-Modified`, and may change the ETag (for example S3 multipart uploads).
  Those pins then fail by design. Only `size` survives a copy. Archives meant
  to travel with their data (relative URLs) should pin `size` only.

Cost: one pin set per source file, not per chunk. A million chunks in 1,000
files means 1,000 pins.

## Conformance cases to add

These are written as model/runner cases.

- A **read vector** with five sources over the same `data/blob.bin`:
  1. unpinned;
  2. `size` correct;
  3. `size` wrong;
  4. `modified_not_after` = the file's mtime;
  5. `modified_not_after` = mtime − 1 s.

  Expected results: ok, ok, resolution error, ok, resolution error.
- **The same vector after the runner rewrites the file** with different bytes
  of the same size and a newer mtime. Expected: source 1 returns the *new*
  bytes, which documents the hazard; sources 2–5 are as expected for the new
  file.
- **An `etag` pin on a `file:` source** → resolution error (fail closed).
- **Pins are resolution errors, not archive errors.** Opening succeeds;
  unpinned and correctly pinned sources stay readable, and so do ranges that
  don't touch a failing source.
- **Writer rejects** a pin on a `key` or `data` source, a weak ETag, or an
  ETag without quotes.
- **HTTP** (needs an HTTP server in the kit, as in the experiment):
  - `etag` correct, wrong, and changed after writing;
  - `modified_not_after` across a rewrite;
  - a check that the reader sends conditional headers rather than extra HEAD
    requests (the request log shows one request per resolved range).
