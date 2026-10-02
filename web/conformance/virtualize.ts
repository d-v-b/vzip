// Virtualizes a TIFF or ND2 file at an http(s) URL with the browser code,
// under Node, by VIRTUALIZE.md:
//   node web/conformance/virtualize.ts <url> <out.vzip>
// Prints a JSON summary; exits 3 (writing nothing) if the input is rejected.

import fs from "node:fs";
import { openHttpFile } from "../src/http.ts";
import { blockReader } from "../src/virtualize/common.ts";
import { virtualizeImage } from "../src/virtualize/index.ts";
import { writeVzip } from "../src/writer.ts";

const [url, out] = process.argv.slice(2);
// Some servers (Zenodo) refuse Node's default User-Agent ("node"); browsers
// send their own.
const nodeFetch = globalThis.fetch;
globalThis.fetch = (input, init) => {
  const headers = new Headers(init?.headers);
  if (!headers.has("User-Agent")) headers.set("User-Agent", "vzip-virtualize (https://github.com/d-v-b/vzip)");
  return nodeFetch(input, { ...init, headers });
};

// Transient network errors are retried with backoff.
async function retry<T>(f: () => Promise<T>): Promise<T> {
  for (let attempt = 0; ; attempt++) {
    try {
      return await f();
    } catch (e) {
      if (attempt >= 4 || !/fetch failed|ECONN|socket|HTTP 5\d\d|HTTP 429/.test(String((e as Error).message) + String((e as { cause?: unknown }).cause ?? ""))) throw e;
      await new Promise((r) => setTimeout(r, 1000 * 2 ** attempt));
    }
  }
}
const file = await retry(() => openHttpFile(url));
let requests = 0;
const read = blockReader((o, n) => { requests++; return retry(() => file.read(o, n)); }, file.size);
const t0 = performance.now();
try {
  const virtual = await virtualizeImage(url, read, file.size);
  fs.writeFileSync(out, await writeVzip(virtual));
  console.log(JSON.stringify({ format: virtual.format, requests, ms: Math.round(performance.now() - t0), ...virtual.summary }));
} catch (e) {
  if (!(e instanceof Error) || !/^(TiffError|Nd2Error|LVError|DicomError|NiftiError|ImsError|ImageError)$/.test(e.constructor.name)) throw e;
  console.error(`rejected: ${e.constructor.name}: ${e.message}`);
  process.exitCode = 3;
}
