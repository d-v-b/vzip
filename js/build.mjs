// Builds dist/: the service worker (vzip-sw.js, a classic script so it works
// in every browser with service workers), the demo page, and the Rust core's
// wasm32 build (vzip_ir.wasm, which `#irwasm` fetches from beside the script;
// `just js::wasm` builds it).

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
fs.rmSync(dist, { recursive: true, force: true });
const common = { bundle: true, target: "es2022", sourcemap: true, logLevel: "info" };
await esbuild.build({ ...common, entryPoints: [path.join(here, "src/sw.ts")], outfile: path.join(dist, "vzip-sw.js"), format: "iife" });
await esbuild.build({ ...common, entryPoints: [path.join(here, "demo/demo.ts")], outfile: path.join(dist, "demo.js"), format: "esm" });
fs.copyFileSync(path.join(here, "demo/index.html"), path.join(dist, "index.html"));
fs.copyFileSync(wasm, path.join(dist, "vzip_ir.wasm"));
