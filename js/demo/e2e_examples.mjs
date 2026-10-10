// Runs every example of the demo page (the links with a data-url) in headless
// Chromium, one fresh browser context each: the input is virtualized by the
// service worker until the page reports Ready, and the viewer it offers
// (Neuroglancer, or the map for a Sentinel-2 product) is opened. For each
// example this records the time to Ready, the requests made, the viewer's
// console errors, the value under the center of the view, and screenshots.
//
// Usage (after `node js/build.mjs` and building the Neuroglancer fork; see
// serve.mjs):
//   node js/demo/e2e_examples.mjs <out dir> [example id or input URL …]
// With BASE=<url>, tests that deployment instead of serving the local build.
// WAIT_MS (default 25000) is how long a viewer is given to load its chunks.

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

const here = path.dirname(new URL(import.meta.url).pathname);
const [outDir, ...only] = process.argv.slice(2);
const waitMs = Number(process.env.WAIT_MS ?? 25000);
const readyTimeout = Number(process.env.READY_TIMEOUT_MS ?? 600000);
fs.mkdirSync(outDir, { recursive: true });

const server = process.env.BASE ? undefined : spawn("node", [path.join(here, "serve.mjs"), "0"]);
const base = process.env.BASE ?? await new Promise((resolve) =>
  server.stdout.on("data", (d) => resolve(String(d).trim())),
);

const browser = await chromium.launch();
try {
  const listing = await browser.newPage();
  await listing.goto(base);
  let examples = await listing.$$eval("a[data-url]", (links) =>
    links.map((a) => ({ id: a.id, url: a.dataset.url, caption: a.textContent.trim() })));
  await listing.close();
  if (only.length > 0) {
    const urls = only.filter((a) => /^https?:/.test(a));
    examples = [
      ...examples.filter((e) => only.includes(e.id)),
      ...urls.map((url, i) => ({ id: `url-${i}`, url, caption: url })),
    ];
  }

  const results = [];
  for (const example of examples) {
    console.log(`=== ${example.id}: ${example.caption}`);
    const result = { ...example };
    results.push(result);
    const context = await browser.newContext({ viewport: { width: 1000, height: 760 } });
    const requests = { source: 0, zarr: 0, hosts: {} };
    let phase = "virtualize";
    const sourceByPhase = { virtualize: 0, viewer: 0 };
    context.on("request", (r) => {
      const u = new URL(r.url());
      if (u.href.startsWith(base)) {
        if (u.pathname.includes("/vz/")) requests.zarr++;
        return;
      }
      requests.source++;
      sourceByPhase[phase]++;
      requests.hosts[u.host] = (requests.hosts[u.host] ?? 0) + 1;
    });
    const errors = [];
    context.on("weberror", (e) => errors.push(String(e.error())));
    const watch = (page, where) =>
      page.on("console", (m) => m.type() === "error" && errors.push(`${where}: ${m.text()}`));
    try {
      const page = await context.newPage();
      watch(page, "demo");
      const t0 = Date.now();
      await page.goto(`${base}?url=${encodeURIComponent(example.url)}`);
      await page.waitForFunction(() => {
        const s = document.getElementById("status");
        return s.classList.contains("error") || s.textContent.startsWith("Ready");
      }, null, { timeout: readyTimeout, polling: 500 });
      result.readyMs = Date.now() - t0;
      result.status = await page.textContent("#status");
      result.requestsToReady = sourceByPhase.virtualize;
      console.log(`  ${result.status} (${result.readyMs} ms wall, ${result.requestsToReady} requests)`);
      await page.screenshot({ path: path.join(outDir, `${example.id}-demo.png`), fullPage: true });
      if (await page.$eval("#status", (s) => s.classList.contains("error"))) throw new Error(result.status);
      result.levels = await page.$$eval("#levels tr", (rows) => rows.map((r) => r.textContent));
      phase = "viewer";
      const mapLink = await page.$("#open-map:not([hidden])");
      const viewer = await context.newPage();
      if (mapLink) {
        result.viewer = "map";
        watch(viewer, "map");
        await viewer.goto(await mapLink.getAttribute("href"));
        await viewer.waitForFunction(() => {
          const s = document.getElementById("status");
          return s.classList.contains("error") || s.textContent.startsWith("Ready");
        }, null, { timeout: readyTimeout, polling: 500 });
        result.viewerStatus = await viewer.textContent("#status");
        await viewer.waitForTimeout(waitMs);
        result.value = await viewer.evaluate(() => {
          const { map, getLayer } = window.vzipMap;
          const [w, h] = map.getSize();
          const data = getLayer().getData([w / 2, h / 2]);
          return { coordinate: map.getCoordinateFromPixel([w / 2, h / 2]), resolution: map.getView().getResolution(),
            data: data && Array.from(data) };
        });
      } else {
        result.viewer = "neuroglancer";
        watch(viewer, "neuroglancer");
        await viewer.goto(await page.getAttribute("#open-ng", "href"));
        await viewer.waitForTimeout(waitMs);
        await viewer.screenshot({ path: path.join(outDir, `${example.id}-neuroglancer.png`) });
        // The value under the center of the view, zoomed in to full
        // resolution (Neuroglancer shows the value of the scale it draws).
        await viewer.evaluate(() => { window.viewer.navigationState.zoomFactor.value = 0.25; });
        await viewer.waitForTimeout(Math.min(waitMs, 10000));
        const panel = await viewer.$(".neuroglancer-rendered-data-panel");
        const box = await panel?.boundingBox();
        if (box) {
          await viewer.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
          await viewer.waitForTimeout(1000);
        }
        result.value = await viewer.evaluate(() => {
          const v = window.viewer;
          return {
            names: v.coordinateSpace.value.names,
            position: Array.from(v.mouseState.position),
            values: [...document.querySelectorAll(".neuroglancer-layer-item")].map((e) => [
              e.querySelector(".neuroglancer-layer-item-label")?.textContent,
              e.querySelector(".neuroglancer-layer-item-value")?.textContent,
            ]),
          };
        });
        result.controlled = await viewer.evaluate(() => navigator.serviceWorker.controller !== null);
      }
      console.log(`  ${result.viewer}: ${JSON.stringify(result.value).slice(0, 400)}`);
      await viewer.screenshot({ path: path.join(outDir, `${example.id}-${result.viewer}${result.viewer === "map" ? "" : "-zoomed"}.png`) });
    } catch (e) {
      result.failure = String(e.message ?? e).slice(0, 500);
      console.log(`  FAILED: ${result.failure}`);
    } finally {
      result.requests = requests;
      result.errors = errors;
      if (errors.length) console.log(`  errors (${errors.length}): ${errors.slice(0, 5).join(" | ").slice(0, 800)}`);
      await context.close();
    }
    fs.writeFileSync(path.join(outDir, "results.json"), JSON.stringify(results, null, 2));
  }
} finally {
  await browser.close();
  server?.kill();
}
