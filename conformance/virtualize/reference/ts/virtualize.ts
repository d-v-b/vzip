// Virtualizes an image file, or a store (a URL ending in "/"), at an http(s)
// URL with the browser code as it was before the Rust core: the frozen TIFF, ND2
// and CZI virtualizers of this directory, and the shipped code for the other
// profiles. For comparisons with js/conformance/virtualize.ts:
//   node conformance/virtualize/reference/ts/virtualize.ts <url> <out.vzip> [--checksums] [--allow-private-hosts]
// (--allow-private-hosts is accepted for the same command line, and has no effect:
// these readers do not apply the reader policy.)
// Source 0 (or each store object's source) pins its size, and a file's its ETag
// when every response gave the same strong one; --checksums also records the
// CRC-32C of every range of a url source (spec/archive.md §5.2), reading its bytes.
// Prints a JSON summary; exits 3 (writing nothing) if the input is rejected.
// A store over the browser limit (§14) is a failure (exit 1), not a rejection.

import fs from "node:fs";
import { openHttpFile } from "../../../../js/src/http.ts";
import { blockReader } from "../../../../js/src/virtualize/common.ts";
import { openHttpStore } from "../../../../js/src/virtualize/store.ts";
import { writeVzip } from "../../../../js/src/writer.ts";
import { isStoreUrl, virtualizeImage, virtualizeStore } from "./index.ts";

const args = process.argv.slice(2);
const checksums = args.includes("--checksums");
const [url, out] = args.filter((a) => !a.startsWith("--"));
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
    const file = await retry(() => openHttpFile(url));
    const read = blockReader((o, n) => { requests++; return retry(() => file.read(o, n)); }, file.size);
    const exact = (o: number, n: number) => { requests++; return retry(() => file.read(o, n)); };
    virtual = await virtualizeImage(url, read, file.size, exact, { etag: file.etag, checksums });
  }
  fs.writeFileSync(out, await writeVzip(virtual));
  console.log(JSON.stringify({ format: virtual.format, requests, ms: Math.round(performance.now() - t0), ...virtual.summary }));
} catch (e) {
  if (!(e instanceof Error) || !REJECTIONS.test(e.constructor.name)) throw e;
  console.error(`rejected: ${e.constructor.name}: ${e.message}`);
  process.exitCode = 3;
}
