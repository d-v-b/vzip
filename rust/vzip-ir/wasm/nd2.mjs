// Runs the ND2 parser built for wasm32 (rust/vzip-ir, `just ir-wasm`) on a local
// file from Node: the host reads the ranges each step asks for and feeds them.
// Usage: node rust/vzip-ir/wasm/nd2.mjs <file.nd2> [vzip_ir.wasm]
import { openSync, readSync, fstatSync, readFileSync } from "node:fs";

const [file, wasmPath = new URL("../target/web/vzip_ir.wasm", import.meta.url)] =
  process.argv.slice(2);
const { instance } = await WebAssembly.instantiate(readFileSync(wasmPath), {});
const w = instance.exports;
const fd = openSync(file, "r");
const size = fstatSync(fd).size;
const out = (n) => new Uint8Array(w.memory.buffer, w.vz_out(), n);
const text = (n) => new TextDecoder().decode(out(n));

const t0 = performance.now();
const p = w.nd2_new(BigInt(size));
let rounds = 0, ranges = 0, bytes = 0;
for (;;) {
  const r = Number(w.nd2_step(p));
  if (r < 0) { console.log(JSON.stringify({ rejected: text(-r) })); process.exit(3); }
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
    w.nd2_feed(p, o, ptr, m);
    ranges++; bytes += m;
  }
}
const n = Number(w.nd2_finish(p));
if (n < 0) { console.log(JSON.stringify({ rejected: text(-n) })); process.exit(3); }
const facts = JSON.parse(text(n));
console.log(JSON.stringify({ elements: facts.elements, runs: facts.runs, check: facts.check, frames: facts.frames,
  positions: facts.positions, loops: facts.loops.map((l) => `${l.kind}${l.count}`), decoded: facts.decoded.length,
  rounds, ranges, bytes, ms: Math.round(performance.now() - t0) }));
