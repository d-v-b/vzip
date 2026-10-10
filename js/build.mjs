// Builds dist/: the service worker (vzip-sw.js, a classic script so it works
// in every browser with service workers), the demo page, the map page for
// Sentinel-2 products (map.html, with OpenLayers, and the JPEG 2000 decoder
// and its worker), and the Rust core's wasm32 build (vzip_ir.wasm, which
// `#irwasm` fetches from beside the script; `just js::wasm` builds it).
//
// The JPEG 2000 and JPEG XR decoders (jpeg2000_decoder.wasm: hayro-jpeg2000,
// Apache-2.0 OR MIT; jpegxr.wasm: jxrlib, BSD-2-Clause) are the Neuroglancer
// fork's (https://github.com/d-v-b/neuroglancer, branch vzip,
// src/sliceview/jpeg2000/ and src/sliceview/jpegxr/), read from
// $NEUROGLANCER, by default a clone next to this repository (../neuroglancer),
// as serve.mjs and pages.sh read the fork. Without them the map page cannot
// decode bands, and the demo cannot sample contrast from such chunks.

import fs from "node:fs";
import path from "node:path";
import * as esbuild from "esbuild";

const here = path.dirname(new URL(import.meta.url).pathname);

const dist = path.join(here, "dist");
const wasm = process.env.VZIP_IR_WASM || path.join(here, "../rust/vzip-ir/target/web/vzip_ir.wasm");
if (!fs.existsSync(wasm)) {
  console.error(`${wasm} is missing: build it with \`just js::wasm\``);
  process.exit(1);
}
const neuroglancer = process.env.NEUROGLANCER ?? path.join(here, "../../neuroglancer");
const decoders = [
  path.join(neuroglancer, "src/sliceview/jpeg2000/jpeg2000_decoder.wasm"),
  path.join(neuroglancer, "src/sliceview/jpegxr/jpegxr.wasm"),
];
fs.rmSync(dist, { recursive: true, force: true });
const common = { bundle: true, target: "es2022", sourcemap: true, logLevel: "info" };
const build = (entry, outfile, options = {}) =>
  esbuild.build({ ...common, entryPoints: [path.join(here, entry)], outfile: path.join(dist, outfile), ...options });
await build("src/sw.ts", "vzip-sw.js", { format: "iife" });
await build("demo/demo.ts", "demo.js", { format: "esm" });
// zarrita imports its blosc, lz4 and zstd codecs (numcodecs, 1.4 MB with their
// wasm inline) dynamically: split them out, to be loaded only when an array
// uses them.
await esbuild.build({
  ...common, entryPoints: { map: path.join(here, "demo/map/map.ts") }, outdir: dist,
  format: "esm", minify: true, splitting: true, chunkNames: "map-chunks/[name]-[hash]",
});
await build("demo/map/jpeg2k_worker.ts", "jpeg2k_worker.js", { format: "iife", minify: true });
fs.copyFileSync(path.join(here, "demo/index.html"), path.join(dist, "index.html"));
fs.copyFileSync(path.join(here, "demo/map/map.html"), path.join(dist, "map.html"));
fs.copyFileSync(wasm, path.join(dist, "vzip_ir.wasm"));
for (const decoder of decoders) {
  if (fs.existsSync(decoder)) {
    fs.copyFileSync(decoder, path.join(dist, path.basename(decoder)));
  } else {
    console.warn(`${decoder} is missing: the pages will not decode its codec (set NEUROGLANCER)`);
  }
}
