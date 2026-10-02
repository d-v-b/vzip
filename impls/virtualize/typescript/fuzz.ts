// Scratch fuzz test: mutates fixtures in memory and checks the virtualizer
// either succeeds or rejects (never throws anything else).
import { readFileSync, readdirSync } from "node:fs";
import { Output, Reject, Source, reject } from "./lib.ts";
import { virtualizeTiff } from "./tiff.ts";
import { virtualizeNd2 } from "./nd2.ts";

async function run(data: Uint8Array): Promise<string> {
  const src = Source.fromBytes("http://x/f", data);
  const out = new Output(src.size);
  try {
    if (src.size < 4) reject("short");
    const h = await src.read(0, 4);
    if (h[0] === 0x49 || h[0] === 0x4d) {
      if (!((h[0] === 0x49 && h[1] === 0x49 && h[3] === 0 && (h[2] === 42 || h[2] === 43)) || (h[0] === 0x4d && h[1] === 0x4d && h[2] === 0 && (h[3] === 42 || h[3] === 43)))) reject("sig");
      await virtualizeTiff(src, out);
    } else if (h[0] === 0xda && h[1] === 0xce && h[2] === 0xbe && h[3] === 0x0a) await virtualizeNd2(src, out);
    else reject("sig");
    return "ok";
  } catch (e) {
    if (e instanceof Reject) return "reject";
    return "CRASH " + ((e as Error).stack ?? String(e)).split("\n").slice(0, 3).join(" | ");
  }
}

let seed = Number(process.argv[2] ?? 1);
const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2147483648), seed / 2147483648);
const dir = "../../../web/test/fixtures/";
const iters = Number(process.argv[3] ?? 200);
const counts: Record<string, number> = {};
for (const f of readdirSync(dir).filter((x) => /\.(tif|nd2)$/.test(x))) {
  const orig = new Uint8Array(readFileSync(dir + f));
  for (let i = 0; i < iters; i++) {
    let d = orig.slice();
    const mode = rnd();
    if (mode < 0.2) d = d.slice(0, Math.floor(rnd() * d.length));
    else {
      const n = 1 + Math.floor(rnd() * 4);
      for (let k = 0; k < n; k++) {
        // bias to headers/metadata: first 6KB and last 600 bytes
        const r = rnd();
        const pos = r < 0.6 ? Math.floor(rnd() * Math.min(6000, d.length)) : r < 0.8 ? d.length - 1 - Math.floor(rnd() * Math.min(600, d.length)) : Math.floor(rnd() * d.length);
        const v = rnd();
        d[pos] = v < 0.3 ? 0 : v < 0.5 ? 0xff : v < 0.7 ? d[pos] ^ (1 << Math.floor(rnd() * 8)) : Math.floor(rnd() * 256);
      }
    }
    const t0 = Date.now();
    const r = await run(d);
    const dt = Date.now() - t0;
    const key = r.startsWith("CRASH") ? r : r;
    counts[r.startsWith("CRASH") ? "crash" : r] = (counts[r.startsWith("CRASH") ? "crash" : r] ?? 0) + 1;
    if (r.startsWith("CRASH") || dt > 5000) console.log(f, i, dt + "ms", key);
  }
}
console.log(counts);
