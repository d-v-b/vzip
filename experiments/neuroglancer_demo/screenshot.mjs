// Render a vzip dataset in the vzip-enabled Neuroglancer fork and take a
// screenshot, logging the HTTP requests the viewer makes.
//
// Usage (from the neuroglancer/ directory, after `npm run build`):
//   node ../experiments/neuroglancer_demo/screenshot.mjs <data dir> <vzip name> <out.png> [scheme]
//
// With "scheme", the layer source is the bare `vzip://<archive-url>` form, so
// Neuroglancer must also auto-detect the Zarr array inside the archive.

import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

// Packages come from neuroglancer's node_modules (the working directory).
const require = createRequire(path.resolve("package.json"));
const { createServer } = require("http-server");
const { chromium } = require("playwright");

const [dataDir, vzipName, outPng, form] = process.argv.slice(2);
const stage = fs.mkdtempSync("/tmp/ng-vzip-");
fs.cpSync("dist/client", stage, { recursive: true });
fs.cpSync(dataDir, path.join(stage, "data"), { recursive: true });

const server = createServer({ root: stage, cache: -1, cors: true }).server;
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${server.address().port}`;

const state = {
  layers: [
    {
      type: "image",
      source:
        form === "scheme"
          ? `vzip://${base}/data/${vzipName}`
          : `${base}/data/${vzipName}|vzip:|zarr3:`,
      name: "HDF5 via vzip",
    },
  ],
  layout: "xy",
  crossSectionScale: 2.2,
};
const url = `${base}/#!${encodeURIComponent(JSON.stringify(state))}`;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 900, height: 700 } });
const requests = [];
page.on("request", (r) => {
  if (r.url().includes("/data/")) {
    requests.push(`${r.method()} ${r.url().replace(base, "")} ${r.headers()["range"] ?? ""}`);
  }
});
const errors = [];
page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
page.on("pageerror", (e) => errors.push(String(e)));

await page.goto(url);
// Wait until every chunk of the .nc file has been requested and rendered.
await page.waitForFunction(
  () => document.querySelector(".neuroglancer-layer-panel") !== null,
  null,
  { timeout: 30000 },
);
await page.waitForTimeout(5000);
await page.screenshot({ path: outPng });
await browser.close();
server.close();

console.log(`source: ${state.layers[0].source}`);
console.log(`screenshot: ${outPng}`);
console.log(`requests to /data/ (${requests.length}):`);
for (const r of requests) console.log("  " + r);
console.log(`console errors (${errors.length}):`);
for (const e of errors) console.log("  " + e.slice(0, 300));
