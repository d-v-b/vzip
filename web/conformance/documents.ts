// Virtualizes local image files and directories (as stores) with the browser
// code, under Node, and prints every output's zarr.json documents:
//   node web/conformance/documents.ts < inputs.json
// The input is a JSON array of [file or directory, source url] pairs (for a
// directory, the url is the store URL it is served from, ending in "/"). The
// output is a JSON array with, for each input in order, {"format", "docs"}
// (docs maps each zarr.json key to its document) or {"rejected": message}.
// Used by tests/test_virtualize_conventions.py.

import fs from "node:fs";
import { blockReader } from "../src/virtualize/common.ts";
import { virtualizeImage, virtualizeStore } from "../src/virtualize/index.ts";
import { directoryStore } from "./directory_store.ts";

const REJECTIONS = /^(TiffError|Nd2Error|LVError|DicomError|NiftiError|ImsError|ImageError|StoreError|N5Error|Zarr2Error|OmeZarrError)$/;
const inputs: [string, string][] = JSON.parse(fs.readFileSync(0, "utf8"));
const decoder = new TextDecoder();
const results: unknown[] = [];
for (const [path, url] of inputs) {
  try {
    let virtual;
    if (fs.statSync(path).isDirectory()) {
      virtual = await virtualizeStore(directoryStore(path, url));
    } else {
      const bytes = new Uint8Array(fs.readFileSync(path));
      virtual = await virtualizeImage(url, blockReader(async (o, n) => bytes.subarray(o, o + n), bytes.length), bytes.length);
    }
    const docs: { [k: string]: unknown } = {};
    for (const e of virtual.entries) {
      if (e.key.endsWith("zarr.json") && "bytes" in e) docs[e.key] = JSON.parse(decoder.decode(e.bytes));
    }
    results.push({ format: virtual.format, docs });
  } catch (e) {
    if (!(e instanceof Error) || !REJECTIONS.test(e.constructor.name)) throw e;
    results.push({ rejected: `${e.constructor.name}: ${e.message}` });
  }
}
process.stdout.write(JSON.stringify(results));
