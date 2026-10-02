// Debug: dump the LV metadata chunks of a local ND2 fixture (scratch tool).
import { readFileSync } from "node:fs";
import { parseChunk, MAX_DEPTH, type LV } from "./lv.ts";
const b = readFileSync(process.argv[2]);
const dv = new DataView(b.buffer, b.byteOffset, b.byteLength);
const want = process.argv[3];
let o = 0;
function show(v: LV, ind: string, depth: number, maxd: number[]): void {
  maxd[0] = Math.max(maxd[0], depth);
  if (v.k === "object") for (const [n, x] of v.m) { if (process.argv[4] !== "q") console.log(ind + n + ":" + (x.k === "scalar" ? ` (${x.type}) ${x.v}` : x.k === "string" ? ` "${x.v}"` : x.k === "bytes" ? ` bytes[${x.v.length}] ${Array.from(x.v.subarray(0,16))}` : ` ${x.k}`)); show(x, ind + "  ", depth + 1, maxd); }
  else if (v.k === "list") v.a.forEach((x, i) => { if (process.argv[4] !== "q") console.log(ind + "[" + i + "]" + (x.k === "scalar" ? ` (${x.type}) ${x.v}` : ` ${x.k}`)); show(x, ind + "  ", depth + 1, maxd); });
}
while (o + 16 <= b.length) {
  if (dv.getUint32(o, true) !== 0x0abeceda) { o++; continue; }
  const n = dv.getUint32(o + 4, true);
  const d = Number(dv.getBigUint64(o + 8, true));
  const name = b.subarray(o + 16, o + 16 + n).toString("latin1").replace(/\0+$/, "");
  if (name.includes("LV") && (!want || name.includes(want))) {
    console.log("== " + name + " @" + o + " d=" + d);
    try { const md=[0]; show(parseChunk(b.subarray(o + 16 + n, o + 16 + n + d)), "  ", 0, md); console.log("max value depth", md[0]); }
    catch (e) { console.log("  ERR", (e as Error).message); }
  } else console.log("-- " + name + " @" + o + " n=" + n + " d=" + d);
  o += 16 + n + d;
}
