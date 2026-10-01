// Builds dist/: the service worker (vzip-sw.js, a classic script so it works
// in every browser with service workers) and the demo page.
// Uses the esbuild installed for the Neuroglancer fork.

import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

const here = path.dirname(new URL(import.meta.url).pathname);
const require = createRequire(path.join(here, "../neuroglancer/package.json"));
const esbuild = require("esbuild");

const dist = path.join(here, "dist");
fs.rmSync(dist, { recursive: true, force: true });
const common = { bundle: true, target: "es2022", sourcemap: true, logLevel: "info" };
await esbuild.build({ ...common, entryPoints: [path.join(here, "src/sw.ts")], outfile: path.join(dist, "vzip-sw.js"), format: "iife" });
await esbuild.build({ ...common, entryPoints: [path.join(here, "demo/demo.ts")], outfile: path.join(dist, "demo.js"), format: "esm" });
fs.copyFileSync(path.join(here, "demo/index.html"), path.join(dist, "index.html"));
