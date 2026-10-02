# Virtualization harness

For [VIRTUALIZE.md](../../VIRTUALIZE.md) (profiles version 0, draft). This
document says how an implementation is run and checked. It adds no rules to
the specification.

## The command

An implementation is a command:

```
virtualize <url> <out.json>
```

- `<url>` is an `http://` URL of a TIFF or ND2 file. Use it, exactly as given,
  as the input URL `U` of the specification (§1.2).
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

## The output file

A JSON object:

```json
{
  "sources": ["<url>"],
  "entries": {
    "0/c/0/0/0": {"ranges": [[0, 4096, 1000]]},
    "0/zarr.json": {"json": {"zarr_format": 3, "...": "..."}},
    "OME/METADATA.ome.xml": {"base64": "PE9NRS..."}
  }
}
```

- `sources`: the source table's URLs, in order (one, `<url>`).
- `entries`: one member per key. The value is one of:
  - `{"ranges": [[source, offset, length], ...]}` for a reference entry;
  - `{"json": value}` for a key ending in `zarr.json` (the document as a JSON
    value, not as a string);
  - `{"base64": "..."}` for any other bytes entry.

Numbers in JSON documents are written so that they parse back to the same
binary64 value (any shortest round-trip formatting does this).

## How outputs are compared

Two outputs are equivalent (§1.1) if they have the same `sources`, the same
keys, and for every key: equal range lists, equal JSON values (numbers
compared as binary64, so `1` equals `1.0`), or equal bytes.

## Test inputs

The synthetic files in `web/test/fixtures/` (`*.tif`, `*.nd2`) are good
first inputs. Their names say what they exercise, and `unsupported_*` and
`nd2_reject_*` files must be rejected. To serve them the way the harness
does, run:

```
python conformance/virtualize/proxy.py web/test/fixtures /tmp/vzip-proxy-cache 8765
```

Then `http://127.0.0.1:8765/f/<file name>` is a fixture. The same server also
serves remote files at `http://127.0.0.1:8765/u/<id>/<name>`, where `<id>` is
the base64url encoding (no padding) of an `https://` URL. Public test inputs
include:

- `https://ftp.ebi.ac.uk/pub/databases/IDR/idr0096-tratwal-marrowquant/20210609-ftp-ome-tiffs/4000_d11_m5_LT_2%20(20x_01).ome.tiff`
  (OME-TIFF, 487 MB);
- `https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/015/S-BIAD3015/Files/1-SR_1_9_6hPre-C_MC1.nd2`
  (ND2, 4.6 GB);
- `https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/077/S-BIAD2077/Files/373_230614_A1_Blk_Reg2_40x.nd2`
  (ND2 Z-stack).
