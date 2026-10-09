// Decodes every <name>.j2k in <tile dir> with the browser JPEG 2000 decoder
// of the Neuroglancer fork (src/sliceview/jpeg2000/jpeg2000_decoder.wasm) and
// compares it with <name>.raw, the imagecodecs (OpenJPEG) decode written by
// survey.py.
//
// Usage: node design/experiments/idr0096_survey/wasm_check.mjs <tile dir>
// The fork is read from $NEUROGLANCER, by default a clone next to this repository.

import fs from "node:fs";
import path from "node:path";

const here = path.dirname(new URL(import.meta.url).pathname);
const neuroglancer = process.env.NEUROGLANCER ?? path.join(here, "../../../../neuroglancer");
const wasm = fs.readFileSync(path.join(neuroglancer, "src/sliceview/jpeg2000/jpeg2000_decoder.wasm"));
const { instance } = await WebAssembly.instantiate(wasm, {});
const e = instance.exports;

function decode(buf, bytesPerSample, signed) {
  const p = e.malloc(buf.length);
  new Uint8Array(e.memory.buffer).set(buf, p);
  const r = e.decode(p, buf.length, bytesPerSample, signed ? 1 : 0);
  const [width, height, components, n] = new Uint32Array(e.memory.buffer.slice(r, r + 16));
  const body = new Uint8Array(e.memory.buffer, r + 16, n).slice();
  e.free(p, buf.length);
  e.free(r, 16 + n);
  if (width === 0) throw new Error(new TextDecoder().decode(body));
  return { width, height, components, data: body };
}

const dir = process.argv[2];
const names = fs.readdirSync(dir).filter((f) => f.endsWith(".j2k")).sort();
const failures = [];
let worst = { ms: 0 };
for (const f of names) {
  const stem = f.slice(0, -4);
  const info = JSON.parse(fs.readFileSync(path.join(dir, `${stem}.json`), "utf8"));
  const expected = fs.readFileSync(path.join(dir, `${stem}.raw`));
  const t = performance.now();
  try {
    const got = decode(fs.readFileSync(path.join(dir, f)), info.dtype.endsWith("16") ? 2 : 1, info.dtype.startsWith("int"));
    const ms = performance.now() - t;
    if (ms > worst.ms) worst = { ms, stem };
    const [h, w, c = 1] = info.shape;
    if (got.height !== h || got.width !== w || got.components !== c) {
      failures.push(`${stem}: ${got.height}x${got.width}x${got.components}, imagecodecs ${info.shape}`);
    } else if (Buffer.compare(Buffer.from(got.data), expected) !== 0) {
      let diff = 0;
      for (let i = 0; i < expected.length; i++) diff += got.data[i] !== expected[i];
      failures.push(`${stem}: ${diff} of ${expected.length} samples differ`);
    }
  } catch (err) {
    failures.push(`${stem}: ${err.message}`);
  }
}
console.log(`${names.length} tiles, ${names.length - failures.length} identical to imagecodecs, ${failures.length} not`);
console.log(`slowest: ${worst.stem} ${worst.ms.toFixed(0)} ms`);
for (const f of failures) console.log("  " + f);
