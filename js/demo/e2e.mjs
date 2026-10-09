// End-to-end check in headless Chromium: the demo page virtualizes a TIFF in
// its service worker, Neuroglancer (same origin) opens the resulting Zarr URL
// as plain zarr3 over HTTP, and the archive is downloaded.
//
// Usage (after `node js/build.mjs` and building the Neuroglancer fork; see
// serve.mjs):
//   node js/demo/e2e.mjs <tiff url> <out dir> [wait ms]
// With BASE=<url> (e.g. the GitHub Pages site), tests that deployment instead
// of serving the local build.

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

const here = path.dirname(new URL(import.meta.url).pathname);

const [tiffUrl, outDir, waitMs = "20000"] = process.argv.slice(2);
fs.mkdirSync(outDir, { recursive: true });

const server = process.env.BASE ? undefined : spawn("node", [path.join(here, "serve.mjs"), "0"]);
const base = process.env.BASE ?? await new Promise((resolve) =>
  server.stdout.on("data", (d) => resolve(String(d).trim())),
);

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 900, height: 700 }, acceptDownloads: true });
const log = [];
context.on("request", (r) => {
  const u = r.url();
  if (!u.startsWith(base) || u.includes("/vz/")) log.push(`${r.method()} ${u.replace(base, "")} ${r.headers()["range"] ?? ""}`);
});
const errors = [];
context.on("weberror", (e) => errors.push(String(e.error())));

const page = await context.newPage();
page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
await page.goto(`${base}?url=${encodeURIComponent(tiffUrl)}`);
await page.waitForSelector("#result:not([hidden])", { timeout: 60000 });
console.log("demo:", await page.textContent("#status"));
console.log("zarr url:", await page.textContent("#zarr-url"));
console.log("levels:", (await page.$$eval("#levels tr", (rows) => rows.map((r) => r.textContent))).join(" | "));
await page.screenshot({ path: path.join(outDir, "demo.png"), fullPage: true });

const download = page.waitForEvent("download");
await page.click("#download");
const file = path.join(outDir, (await download).suggestedFilename());
await (await download).saveAs(file);
console.log("downloaded:", file, fs.statSync(file).size, "bytes");

const viewer = await context.newPage();
viewer.on("console", (m) => m.type() === "error" && errors.push(m.text()));
await viewer.goto(await page.getAttribute("#open-ng", "href"));
await viewer.waitForTimeout(Number(waitMs));
await viewer.screenshot({ path: path.join(outDir, "neuroglancer.png") });
const controlled = await viewer.evaluate(() => navigator.serviceWorker.controller !== null);
console.log("neuroglancer page controlled by the service worker:", controlled);

const zarrRequests = log.filter((l) => l.split(" ")[1].startsWith("vz/"));
const tiffRequests = log.filter((l) => !l.split(" ")[1].startsWith("vz/"));
console.log(`requests to the Zarr URL: ${zarrRequests.length}, e.g.`);
for (const l of zarrRequests.slice(0, 6)) console.log("  " + l.slice(0, 160));
console.log(`requests to the TIFF (from the service worker): ${tiffRequests.length}`);
for (const l of tiffRequests.slice(0, 4)) console.log("  " + l.slice(0, 200));
console.log(`errors (${errors.length}):`);
for (const e of errors.slice(0, 10)) console.log("  " + e.slice(0, 300));

await browser.close();
server?.kill();
