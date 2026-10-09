// Virtualizes an image file, or a store (a URL ending in "/"), at an http(s)
// URL with the browser code, under Node, by VIRTUALIZE.md:
//   node web/conformance/virtualize.ts <url> <out.vzip> [--checksums] [--allow-private-hosts]
// A file is read under the reader policy (SPEC.md §8.7), which refuses private
// and special hosts unless --allow-private-hosts (UNSAFE; the harness serves its
// inputs from 127.0.0.1); TIFF, ND2 and CZI are virtualized by the Rust core
// (rust/vzip-ir, as WebAssembly), which plans the requests.
// Source 0 (or each store object's source) pins its size, and a file's its ETag
// when every response gave the same strong one; --checksums also records the
// CRC-32C of every range of a url source (SPEC.md §5.2), reading its bytes.
// Prints a JSON summary; exits 3 (writing nothing) if the input is rejected.
// A store over the browser limit (§14) is a failure (exit 1), not a rejection.

import fs from "node:fs";
import { isStoreUrl, virtualizeSource, virtualizeStore } from "../src/virtualize/index.ts";
import { openHttpSource } from "../src/virtualize/ir/source.ts";
import { openHttpStore } from "../src/virtualize/store.ts";
import { writeVzip } from "../src/writer.ts";

const args = process.argv.slice(2);
const checksums = args.includes("--checksums");
const allowPrivateHosts = args.includes("--allow-private-hosts");
const [url, out] = args.filter((a) => !a.startsWith("--"));
const UA = "vzip-virtualize (https://github.com/d-v-b/vzip)";
// Some servers (Zenodo) refuse Node's default User-Agent ("node"); browsers
// send their own.
const nodeFetch = globalThis.fetch;
globalThis.fetch = (input, init) => {
  const headers = new Headers(init?.headers);
  if (!headers.has("User-Agent")) headers.set("User-Agent", UA);
  return nodeFetch(input, { ...init, headers });
};

// Transient statuses are retried too; what remains is the response.
async function retryingFetch(u: string, init?: RequestInit): Promise<Response> {
  for (let attempt = 0; ; attempt++) {
    try {
      const r = await fetch(u, init);
      if ((r.status === 429 || r.status >= 500) && attempt < 4) {
        await new Promise((res) => setTimeout(res, 1000 * 2 ** attempt));
        continue;
      }
      return r;
    } catch (e) {
      if (attempt >= 4) throw e;
      await new Promise((res) => setTimeout(res, 1000 * 2 ** attempt));
    }
  }
}

const REJECTIONS = /^(TiffError|Nd2Error|LVError|DicomError|NiftiError|ImsError|ImageError|StoreError|N5Error|Zarr2Error|OmeZarrError|SafeError|CziError)$/;
let requests = 0;
const t0 = performance.now();
try {
  let virtual;
  if (isStoreUrl(url)) {
    const store = await openHttpStore(url, { fetch: (u, i) => { requests++; return retryingFetch(u, i); } });
    virtual = await virtualizeStore(store, { checksums });
  } else {
    // retries (429, 5xx, network errors) are the source's
    const source = await openHttpSource(url, { policy: { allowPrivateHosts }, headers: { "User-Agent": UA } });
    virtual = await virtualizeSource(url, source, { checksums });
    requests = source.requests;
  }
  fs.writeFileSync(out, await writeVzip(virtual));
  console.log(JSON.stringify({ format: virtual.format, requests, ms: Math.round(performance.now() - t0), ...virtual.summary }));
} catch (e) {
  if (!(e instanceof Error) || !REJECTIONS.test(e.constructor.name)) throw e;
  console.error(`rejected: ${e.constructor.name}: ${e.message}`);
  process.exitCode = 3;
}
