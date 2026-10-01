// Open a Neuroglancer state in the vzip-enabled fork and take a screenshot,
// logging the data requests the viewer makes.
//
// Usage (after `npm run build` in the Neuroglancer fork):
//   node experiments/neuroglancer_demo/screenshot.mjs <data dir> <state.json> <out.png> [wait ms]
//
// The fork (https://github.com/d-v-b/neuroglancer, branch vzip) is read from
// $NEUROGLANCER, by default a clone next to this repository (../neuroglancer).
//
// The built client is served together with <data dir> (at /data/), and
// "{base}" in the state file is replaced by the server's origin. Examples are
// in states/.

import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

const here = path.dirname(new URL(import.meta.url).pathname);
const neuroglancer = process.env.NEUROGLANCER ?? path.join(here, "../../../neuroglancer");
// Packages come from the fork's node_modules.
const require = createRequire(path.join(neuroglancer, "package.json"));
const { createServer } = require("http-server");
const { chromium } = require("playwright");

const [dataDir, stateFile, outPng, waitMs = "5000"] = process.argv.slice(2);
const stage = fs.mkdtempSync("/tmp/ng-vzip-");
fs.cpSync(path.join(neuroglancer, "dist/client"), stage, { recursive: true });
fs.cpSync(dataDir, path.join(stage, "data"), { recursive: true });

const server = createServer({ root: stage, cache: -1, cors: true }).server;
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${server.address().port}`;

const state = JSON.parse(
  fs.readFileSync(stateFile, "utf8").replaceAll("{base}", base),
);
const url = `${base}/#!${encodeURIComponent(JSON.stringify(state))}`;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 900, height: 700 } });
const requests = [];
page.on("request", (r) => {
  const u = r.url();
  if (u.startsWith(`${base}/data/`) || !u.startsWith(base)) {
    requests.push(`${r.method()} ${u.replace(base, "")} ${r.headers()["range"] ?? ""}`);
  }
});
const failed = [];
page.on("requestfailed", (r) => failed.push(`${r.url()}: ${r.failure()?.errorText}`));
const errors = [];
page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
page.on("pageerror", (e) => errors.push(String(e)));

await page.goto(url);
await page.waitForFunction(
  () => document.querySelector(".neuroglancer-layer-panel") !== null,
  null,
  { timeout: 30000 },
);
await page.waitForTimeout(Number(waitMs));
await page.screenshot({ path: outPng });
const dimensions = await page.evaluate(() =>
  JSON.stringify(window.viewer?.state.toJSON().dimensions),
);
if (process.env.PRINT_STATE) {
  console.log(await page.evaluate(() => JSON.stringify(window.viewer?.state.toJSON(), null, 1)));
}
await browser.close();
server.close();

console.log(`sources: ${state.layers.map((l) => l.source.url ?? l.source).join(", ")}`);
console.log(`dimensions: ${dimensions}`);
console.log(`screenshot: ${outPng}`);
const shown = requests.length > 40 ? [...requests.slice(0, 20), "...", ...requests.slice(-5)] : requests;
console.log(`data requests (${requests.length}):`);
for (const r of shown) console.log("  " + r);
console.log(`failed requests (${failed.length}):`);
for (const r of failed.slice(0, 10)) console.log("  " + r);
console.log(`console errors (${errors.length}):`);
for (const e of errors.slice(0, 10)) console.log("  " + e.slice(0, 300));
