// Virtualizes every TIFF listed in <list> (one file name per line, relative to
// <base url>) with the browser code in js/src, over HTTP, as the demo's
// service worker does. Writes <out dir>/<n>.vzip and one JSON line per file.
//
// Usage: node design/experiments/idr0096_survey/virtualize_all.ts <base url> <list> <out dir> [concurrency]

import fs from "node:fs";
import path from "node:path";
import { virtualizeIr } from "../../../js/src/virtualize/ir/run.ts";
import { openHttpSource } from "../../../js/src/virtualize/ir/source.ts";
import { writeVzip } from "../../../js/src/writer.ts";

const [base, list, outDir, concurrency = "6"] = process.argv.slice(2);
const names = fs.readFileSync(list, "utf8").split("\n").filter((l) => l.endsWith(".tiff") || l.endsWith(".tif"));
fs.mkdirSync(outDir, { recursive: true });

async function one(i: number, name: string) {
  const url = new URL(name, base).href;
  const t0 = performance.now();
  let source: Awaited<ReturnType<typeof openHttpSource>> | undefined;
  const requests = () => source?.requests ?? 0;
  try {
    source = await openHttpSource(url);
    const virtual = await virtualizeIr("tiff", url, source);
    const bytes = await writeVzip(virtual);
    fs.writeFileSync(path.join(outDir, `${i}.vzip`), bytes);
    return { i, name, url, ok: true, size: source.size, archive: bytes.length, requests: requests(), ms: Math.round(performance.now() - t0), summary: virtual.summary };
  } catch (e) {
    return { i, name, url, ok: false, error: `${(e as Error).constructor.name}: ${(e as Error).message}`, requests: requests(), ms: Math.round(performance.now() - t0) };
  }
}

let next = 0;
const results: unknown[] = [];
await Promise.all(
  Array.from({ length: Number(concurrency) }, async () => {
    while (next < names.length) {
      const i = next++;
      const r = await one(i, names[i]);
      results[i] = r;
      process.stdout.write(JSON.stringify(r) + "\n");
    }
  }),
);
