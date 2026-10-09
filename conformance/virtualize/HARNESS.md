# Virtualization harness

For [VIRTUALIZE.md](../../VIRTUALIZE.md) and its profiles in
[profiles/](../../profiles/) (profiles version 0, draft). This
document says how an implementation is run and checked. It adds no rules to
the specification.

## The command

An implementation is a command:

```
virtualize <url> <out.json>
```

- `<url>` is an `http://` URL of an image file (any profile, a SAFE zip
  included), or of a store (N5, Zarr v2, OME-Zarr 0.4 or a SAFE directory),
  whose URL ends in `/` (§1.4). Use it, exactly as given, as the input
  URL `U` of the specification (§1.2).
- On success, write the output (§1.1) to `<out.json>` as described below, and
  exit with status 0. Printing a one-line summary to stdout is optional.
- If the specification rejects the input, exit with status 3, write nothing,
  and print the reason to stderr.
- If reading fails (a network error, or a response other than those below),
  exit with any other status (for example 1). Any status other than 0 and 3
  means the implementation failed.

## Reading the input

The harness serves every input over plain HTTP/1.1 (no TLS) from a local
server that:

- answers `HEAD` with `Content-Length` (the file's size);
- answers `GET` with a single `Range: bytes=a-b` header with 206 and
  `Content-Range: bytes a-b/size`;
- refuses `GET` without `Range` (403).

Read only the file's structure (headers, directories, metadata), never its
pixel data. Files can be tens of gigabytes. Caching reads in blocks is
recommended.

A store is listed as §1.5 says. Store URLs from the harness are path-style
(`http://127.0.0.1:<port>/f/...` and `http://127.0.0.1:<port>/u/...`, with
the buckets `f` and `u`), and the server answers
`GET /<bucket>/?list-type=2&prefix=<P>[&continuation-token=<T>]` with an S3
`ListBucketResult` of at most 100 keys per page. A store's objects are served
like files (`HEAD`, and `GET` with a single `Range`), at the store URL
followed by the key (§1.4).

## The output file

A JSON object:

```json
{
  "sources": ["<url>", {"data": "/9j/2wBDAA..."}],
  "entries": {
    "0/c/0/0/0": {"ranges": [[0, 4096, 1000]]},
    "0/zarr.json": {"json": {"zarr_format": 3, "...": "..."}},
    "OME/METADATA.ome.xml": {"base64": "PE9NRS..."}
  }
}
```

- `sources`: the source table, in order. A `url` source is its URL, a
  string; a `data` source is `{"data": "<base64>"}`. A file's table is
  `<url>`, then its data sources if its profile has any (§1.2); a store's is
  one URL per chunk entry, in key order (§1.4).
- `entries`: one member per key. The value is one of:
  - `{"ranges": [[source, offset, length], ...]}` for a reference entry,
    where a literal range is `{"data": "<base64>"}` instead of a triple;
  - `{"json": value}` for a key ending in `zarr.json` (the document as a JSON
    value, not as a string);
  - `{"base64": "..."}` for any other bytes entry.

Numbers in JSON documents are written so that they parse back to the same
binary64 value (any shortest round-trip formatting does this).

## How outputs are compared

Base64 is the standard alphabet with padding (RFC 4648 §4), so equal bytes
have equal strings.

Two outputs are equivalent (§1.1) if they have the same `sources` (equal
URLs, or data sources with equal bytes), the same
keys, and for every key: equal range lists, equal JSON values, or equal
bytes. In JSON values, two integers compare exactly (§1.6 copies an integer
literal with every digit, so `9007199254740993` differs from
`9007199254740992`), and a number against a non-integer by its binary64
value (so `1` equals `1.0`); booleans are not numbers.

`compare.py` also reads every vzip archive by SPEC.md before it compares it:
the independent reader in `impls/python` (written from SPEC.md alone) must
open it and see the same entries, and the archive must meet SPEC.md's writer
requirements as `conformance/validate.py` checks them (canonical payloads,
reference bodies, no duplicate names or extra `__vz__/` entries, CRC-32s,
local headers, the page index). A JSON document with a duplicate member is
unreadable too. An archive that fails any of these counts as a crash of the
implementation that wrote it.

## Test inputs

The synthetic files in `web/test/fixtures/`, one directory per format
(`tiff/`, `ndpi/`, `nd2/`, `dicom/`, `nifti/`, `ims/`), and the synthetic
stores in `web/test/fixtures/n5/`, `web/test/fixtures/zarr2/` and
`web/test/fixtures/ome-zarr/` (one directory per store), are good first inputs. Their names say what they
exercise, and `unsupported_*`, `edge_reject_*` and `<profile>_reject_*`
inputs must be rejected. To serve them the way the harness does, run:

```
python conformance/virtualize/proxy.py web/test/fixtures /tmp/vzip-proxy-cache 8765
```

Then `http://127.0.0.1:8765/f/<format>/<file name>` is a fixture file, and
`http://127.0.0.1:8765/f/<format>/<store name>/` a fixture store. The same
server also serves remote files at `http://127.0.0.1:8765/u/<id>/<name>`, and
remote stores at `http://127.0.0.1:8765/u/<id>/`, where `<id>` is the
base64url encoding (no padding) of an `https://` URL (a store's ending in
`/`; the server lists it with its own S3 endpoint, §1.5). Public test inputs
include:

- `https://ftp.ebi.ac.uk/pub/databases/IDR/idr0096-tratwal-marrowquant/20210609-ftp-ome-tiffs/4000_d11_m5_LT_2%20(20x_01).ome.tiff`
  (OME-TIFF, 487 MB);
- `https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/015/S-BIAD3015/Files/1-SR_1_9_6hPre-C_MC1.nd2`
  (ND2, 4.6 GB);
- `https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/077/S-BIAD2077/Files/373_230614_A1_Blk_Reg2_40x.nd2`
  (ND2 Z-stack);
- `https://janelia-cosem-datasets.s3.amazonaws.com/jrc_hela-2/jrc_hela-2.n5/labels/gt/`
  (an N5 COSEM multiscale group, 831 objects);
- `https://janelia-cosem-datasets.s3.amazonaws.com/jrc_hela-2/jrc_hela-2.zarr/recon-1/labels/groundtruth/crop1/ves/`
  (a Zarr v2 OME-NGFF 0.4 multiscale group, read by the OME-Zarr profile);
- `https://uk1s3.embassy.ebi.ac.uk/idr/zarr/v0.4/idr0072B/9512.zarr/`
  (an OME-Zarr 0.4 plate from IDR, 24634 objects, path-style listing).

`compare.py --fixtures <dir>` takes the store fixtures from `<dir>/n5/`,
`<dir>/zarr2/` and `<dir>/ome-zarr/` (as `mutate.py` writes them), and `--large` adds the corpus
inputs marked `py-only`, which every implementation but the browser's runs.

## The built-in implementations, and what a comparison proves

`compare.py` runs three implementations of its own, and compares each output
with the first's:

- `ref`, the reference: `conformance/virtualize/reference/cli.py`. For TIFF,
  ND2 and CZI it runs the frozen Python profiles in
  `conformance/virtualize/reference/vzip_reference/` (what `vzip.virtualize`
  shipped before these formats moved to the IR, unchanged but for their import
  paths); for every other profile, the shipped code.
- `py`: `python -m vzip.virtualize --allow-private-hosts`.
- `web`: the browser code under Node, `web/conformance/virtualize.ts
  --allow-private-hosts`.

For TIFF, ND2 and CZI, `py` and `web` are one implementation: the Rust core
`rust/vzip-ir` (its parsers, schemas, read planner, projections and mirror),
built natively for Python and as wasm32 for the browser, each with a thin host
(the I/O, and the archive writer). Their `vzip_source` is the IR's mirror,
which the reference does not write. So, for those profiles:

- `ref` against `py` and `web`, outside `vzip_source`: proves that the IR path
  writes the hierarchy, references and data sources an independent
  implementation (the frozen one) writes, and rejects the same inputs. It is
  the same check as before the switch-over, with one of its two sides now the
  core.
- `py` against `web`, entirely (`vzip_source` included): proves that the two
  hosts drive the core alike and write the same archive contents (the native
  and wasm32 builds agree, and so do the Python and browser drivers and
  writers). It no longer proves that two independent implementations of the
  convention agree: the parsers, checks and projections exist once. That
  independence now rests on the frozen reference, which stops at what it does
  today: the mirror (`vzip_source`) has no second implementation, and its
  checks are the byte-exact rebuild of the source from it and its canonical
  form (below).

For every other profile, `py` and `web` are still two hand-written
implementations, and `ref` runs the same code as `py`.

Since revision 21, `vzip_source` of these three profiles is the IR mirror of
conventions §8; since revision 22 the mirror is canonical (conventions §8.8)
and VIRTUALIZE.md §1.1 compares it entry for entry, as `py` against `web`
does. `compare.py` also checks each archive's mirror on its own, in addition
to that comparison: it loads the table, checks its invariants (conventions
§8.4), checks that it describes a source of the size source 0 pins, and
checks that it is canonical (folded again from what it loads, it is the same
table, entry for entry); a mirror that fails is a crash of the
implementation that wrote it.

**The frozen reference records revision 20.** The frozen TIFF, ND2 and CZI
profiles have their own revision constant
(`conformance/virtualize/reference/vzip_reference/revision.py`,
`web/conformance/reference/revision.ts`), 20, the last revision before the
mirror, which their roots record with their old `vzip_source`; nothing else
in them changed (an edit made so that a reference archive does not claim a
revision it does not implement). `compare.py` compares a reference
root that records 20 as if it recorded the current revision, so the revision
number is the one difference it does not report; every other profile's
reference is the shipped code, which records the current revision.

**The frozen reference stays.** The frozen implementations
(`conformance/virtualize/reference`, `web/conformance/reference`) are kept
unchanged as the independent check of the IR path's hierarchy, at least until
the conventions have been revised around the IR and CI has run on the IR path
for a while. Whether and when to delete them is the user's decision.
