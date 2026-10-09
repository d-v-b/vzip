// The wasm32 build of the core under Node: the module's size (raw, gzip), cold
// compilation and instantiation, and the throughput of the shipped driver over
// every TIFF, ND2 and CZI fixture (two passes, the second warm), and the time of
// each file given (read from disk through a block reader; the better of two runs).
// Usage: VZIP_IR_WASM=<module> node experiments/ir_adopt/wasm_bench.ts [<file>...]
import fs from "node:fs";
import zlib from "node:zlib";
import { blockReader } from "../../web/src/virtualize/common.ts";
import { virtualizeImage } from "../../web/src/virtualize/index.ts";

const path = process.env.VZIP_IR_WASM!;
const bytes = fs.readFileSync(path);
const t0 = performance.now();
const module = await WebAssembly.compile(bytes);
const t1 = performance.now();
await WebAssembly.instantiate(module, {});
const t2 = performance.now();
const files: string[] = [];
for (const d of ["tiff", "nd2", "czi"]) {
  const dir = new URL(`../../web/test/fixtures/${d}/`, import.meta.url);
  for (const f of fs.readdirSync(dir).sort()) if (/\.(tif|nd2|czi)$/.test(f)) files.push(new URL(f, dir).pathname);
}
const pass = async () => {
  const s = performance.now();
  for (const f of files) {
    const b = new Uint8Array(fs.readFileSync(f));
    try {
      await virtualizeImage("https://data.test/x", blockReader(async (o, n) => b.subarray(o, o + n), b.length), b.length);
    } catch {
      // rejections count too
    }
  }
  return performance.now() - s;
};
const big: Record<string, number> = {};
for (const f of process.argv.slice(2)) {
  const fd = fs.openSync(f, "r");
  const size = fs.fstatSync(fd).size;
  const read = async (o: number, n: number) => {
    const b = new Uint8Array(n);
    fs.readSync(fd, b, 0, n, o);
    return b;
  };
  let best = Infinity;
  for (let k = 0; k < 2; k++) {
    const s = performance.now();
    await virtualizeImage("https://data.test/x", blockReader(read, size), size, read);
    best = Math.min(best, performance.now() - s);
  }
  big[f.split("/").pop()!] = Math.round(best);
  fs.closeSync(fd);
}
const first = await pass();
const second = await pass();
console.log(JSON.stringify({ raw: bytes.length, gzip: zlib.gzipSync(bytes, { level: 9 }).length,
  compile_ms: +(t1 - t0).toFixed(1), instantiate_ms: +(t2 - t1).toFixed(2), files: files.length,
  first_pass_ms: Math.round(first), second_pass_ms: Math.round(second), files_ms: big }));
