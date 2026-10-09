// Runs the parsers built for wasm32 (rust/vzip-ir, `just ir-wasm`) on local files
// from Node: the host reads the ranges each step asks for and feeds them. Prints
// one JSON line per file: the IR's element and run counts, the checker's verdict,
// its digest and the facts (or the rejection), and the rounds, ranges and bytes read.
// Usage: node rust/vzip-ir/wasm/run.mjs [--wasm <vzip_ir.wasm>] <file>...
import { openSync, readSync, fstatSync, readFileSync, closeSync } from "node:fs";

const args = process.argv.slice(2);
let wasmPath = new URL("../target/web/vzip_ir.wasm", import.meta.url);
if (args[0] === "--wasm") { wasmPath = args[1]; args.splice(0, 2); }
const { instance } = await WebAssembly.instantiate(readFileSync(wasmPath), {});
const w = instance.exports;
const text = (n) => new TextDecoder().decode(new Uint8Array(w.memory.buffer, w.vz_out(), n));

function format(fd) {
  const head = new Uint8Array(16);
  readSync(fd, head, 0, 16, 0);
  const s = String.fromCharCode(...head);
  if (head[0] === 0xda && head[1] === 0xce && head[2] === 0xbe && head[3] === 0x0a) return 0;
  if (s.startsWith("II*\0") || s.startsWith("MM\0*") || s.startsWith("II+\0") || s.startsWith("MM\0+")) return 1;
  return 2;
}

for (const file of args) {
  const fd = openSync(file, "r");
  const size = fstatSync(fd).size;
  const t0 = performance.now();
  const p = w.vz_new(format(fd), BigInt(size));
  let rounds = 0, ranges = 0, bytes = 0, out = null;
  for (;;) {
    const r = Number(w.vz_step(p));
    if (r < 0) { out = { rejected: text(-r) }; break; }
    if (r === 0) break;
    const n = r - 1;
    rounds++;
    const view = new DataView(w.memory.buffer, w.vz_out(), n);
    const batch = [];
    for (let k = 0; k < n; k += 16) batch.push([view.getBigUint64(k, true), view.getBigUint64(k + 8, true)]);
    for (const [o, len] of batch) {
      const m = Number(len);
      const ptr = w.vz_alloc(m);
      readSync(fd, new Uint8Array(w.memory.buffer, ptr, m), 0, m, Number(o));
      w.vz_feed(p, o, ptr, m);
      ranges++; bytes += m;
    }
  }
  if (out === null) {
    const n = Number(w.vz_finish(p));
    out = n < 0 ? { rejected: text(-n) } : JSON.parse(text(n));
  }
  closeSync(fd);
  console.log(JSON.stringify({ file, ...out, rounds, ranges, bytes, ms: Math.round(performance.now() - t0) }));
}
