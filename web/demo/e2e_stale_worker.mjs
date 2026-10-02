// Checks the demo in a browser that already has a service worker with a
// broader scope, as left by the demo site's first deploy, which served the
// demo (and its worker) at the site root before it moved to tiff-to-zarr/ (now image-to-zarr/).
//
// The server mimics the GitHub Pages layout:
//   /vzip-demo/              the first deploy's demo and worker (vzip-sw.js),
//                            or, with "retired", the retired worker from
//                            web/demo/retired-sw.js; with "upgrade", the old
//                            worker is installed first and then replaced by
//                            the retired one, as on the live site
//   /vzip-demo/image-to-zarr/ the current demo (web/dist)
//   /data/fixture.tif        a TIFF fixture
//
// Usage (after `node web/build.mjs`):
//   node web/demo/e2e_stale_worker.mjs [old|retired|upgrade]

import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { chromium } from "playwright";

const here = path.dirname(new URL(import.meta.url).pathname);
const dist = path.join(here, "../dist");
const mode = process.argv[2] ?? "old";
let retired = mode === "retired";
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".tif": "image/tiff" };

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://x");
  let file;
  if (url.pathname === "/data/fixture.tif") {
    file = path.join(here, "../test/fixtures/rgb_planar_jpeg2000_bigtiff_be.ome.tif");
  } else if (url.pathname.startsWith("/vzip-demo/image-to-zarr/")) {
    file = path.join(dist, url.pathname.slice("/vzip-demo/image-to-zarr/".length) || "index.html");
  } else if (url.pathname === "/vzip-demo/vzip-sw.js" && retired) {
    file = path.join(here, "retired-sw.js");
  } else if (url.pathname.startsWith("/vzip-demo/")) {
    file = path.join(dist, url.pathname.slice("/vzip-demo/".length) || "index.html");
  }
  if (!file || !fs.existsSync(file)) return res.writeHead(404).end("not found");
  const body = fs.readFileSync(file);
  const range = req.headers.range?.match(/^bytes=(\d+)-(\d+)$/);
  const headers = { "Content-Type": TYPES[path.extname(file)] ?? "application/octet-stream", "Accept-Ranges": "bytes" };
  if (!range) headers["Content-Length"] = String(body.length);
  if (range) {
    const [a, b] = [Number(range[1]), Math.min(Number(range[2]), body.length - 1)];
    res.writeHead(206, { ...headers, "Content-Range": `bytes ${a}-${b}/${body.length}` });
    return res.end(body.subarray(a, b + 1));
  }
  res.writeHead(200, headers).end(req.method === "HEAD" ? undefined : body);
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const base = `http://127.0.0.1:${server.address().port}`;

const browser = await chromium.launch();
const context = await browser.newContext();
const page = await context.newPage();

// 1. The first deploy: its worker takes the whole site's scope. (With
// "retired", the page at the root still registers it, as old tabs would.)
// The old demo registered its worker when the example was clicked.
await page.goto(`${base}/vzip-demo/`);
await page.evaluate(async () => {
  await navigator.serviceWorker.register("vzip-sw.js");
  await navigator.serviceWorker.ready;
});
const before = await page.evaluate(async () =>
  (await navigator.serviceWorker.getRegistrations()).map((r) => r.scope),
);
console.log("registrations after visiting the old root:", before);

if (mode === "upgrade") {
  // The site now serves the retired worker at the old script URL. A visit in
  // the old worker's scope makes the browser check it for updates.
  retired = true;
  await page.goto(`${base}/vzip-demo/`);
  await page.evaluate(async () => {
    const r = await navigator.serviceWorker.getRegistration("/vzip-demo/");
    await r?.update();
  });
  const left = await page.evaluate(async () => {
    for (let i = 0; i < 50; i++) {
      const scopes = (await navigator.serviceWorker.getRegistrations()).map((r) => r.scope);
      if (scopes.length === 0) return scopes;
      await new Promise((r) => setTimeout(r, 200));
    }
    return (await navigator.serviceWorker.getRegistrations()).map((r) => r.scope);
  });
  console.log("registrations after the update check:", left);
  if (left.length !== 0) throw new Error("the retired worker did not unregister the old one");
}

// 2. The current demo, opened in the same browser.
const tiff = `${base}/data/fixture.tif`;
await page.goto(`${base}/vzip-demo/image-to-zarr/?url=${encodeURIComponent(tiff)}`);
await page.waitForFunction(
  () => /Ready|error|\d{3}:|not answered/i.test(document.querySelector("#status")?.textContent ?? ""),
  null,
  { timeout: 30000 },
);
const status = (await page.textContent("#status")).slice(0, 160);
const ok = await page.isVisible("#result");
console.log("status:", status);
console.log("result shown:", ok);
console.log("controller:", await page.evaluate(() => navigator.serviceWorker.controller?.scriptURL));

// 3. Clicking the example (here: submitting another URL) records it in the page URL.
await page.goto(`${base}/vzip-demo/image-to-zarr/`);
await page.fill("#url", tiff);
await page.click("button[type=submit]");
await page.waitForSelector("#result:not([hidden])", { timeout: 30000 });
const recorded = new URL(page.url()).searchParams.get("url");
console.log("page URL records the TIFF:", recorded === tiff);

const after = await page.evaluate(async () =>
  (await navigator.serviceWorker.getRegistrations()).map((r) => r.scope),
);
console.log("registrations at the end:", after);
await browser.close();
server.close();
process.exitCode = ok && recorded === tiff ? 0 : 1;
