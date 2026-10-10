// The map page: shows a virtualized Sentinel-2 SAFE product
// (spec/virtualize/safe.md) on a map, with OpenLayers' GeoZarr source reading
// the service worker's Zarr URL. The page is in the worker's scope, so its
// requests to that URL are answered by the worker, like Neuroglancer's.
//
// Usage: map.html?url=<the SAFE product's URL, or a .vzip archive of one>

import "ol/ol.css";
import OlMap from "ol/Map.js";
import TileLayer from "ol/layer/Tile.js";
import WebGLTileLayer from "ol/layer/WebGLTile.js";
import { register } from "ol/proj/proj4.js";
import { toLonLat } from "ol/proj.js";
import GeoZarr from "ol/source/GeoZarr.js";
import OSM from "ol/source/OSM.js";
import proj4 from "proj4";
import { archiveZarrUrl, imageZarrUrl, registerVzipWorker } from "../../src/client.ts";
import { WORKER_HEADER } from "../../src/server.ts";
import { registerJpeg2k } from "./jpeg2k.ts";

registerJpeg2k();

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const status = $("status");
const setStatus = (text: string, error = false) => {
  status.textContent = text;
  status.classList.toggle("error", error);
};

async function getJson(url: string) {
  const r = await fetch(url);
  if (r.headers.get(WORKER_HEADER) === null) {
    throw new Error("the vzip service worker did not answer; reload the page and try again");
  }
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json();
}

/** How a band's digital numbers become reflectance (§6.2): `(DN + offset) / quantification`. */
interface Radiometry {
  offset: number;
  quantification: number;
}

type Style = NonNullable<ConstructorParameters<typeof WebGLTileLayer>[0]>["style"];

/** One way to show the product: a GeoZarr source and its style. */
interface Rendering {
  label: string;
  source: () => GeoZarr;
  style: Style;
  /** The bands read, for the value readout. */
  bands: string[];
}

const BRIGHTNESS = "brightness";

/**
 * Reflectance as the display value, times the brightness (1 shows
 * reflectance 0.3 as white), with nodata (DN 0, the SAFE's NODATA special
 * value) transparent.
 */
function reflectanceStyle(radiometry: Radiometry[]): Style {
  const channel = (i: number) => [
    "clamp",
    [
      "*",
      ["/", ["+", ["band", i + 1], radiometry[i].offset], radiometry[i].quantification * 0.3],
      ["var", BRIGHTNESS],
    ],
    0,
    1,
  ];
  return {
    variables: { [BRIGHTNESS]: 1 },
    color: ["array", channel(0), channel(1), channel(2), ["case", ["==", ["band", 1], 0], 0, 1]],
  } as Style;
}

/** The 8-bit true-color image, with black (its nodata) transparent. */
function tciStyle(): Style {
  const channel = (i: number) => ["clamp", ["*", ["/", ["band", i], 255], ["var", BRIGHTNESS]], 0, 1];
  const black = ["all", ["==", ["band", 1], 0], ["==", ["band", 2], 0], ["==", ["band", 3], 0]];
  return {
    variables: { [BRIGHTNESS]: 1 },
    color: ["array", channel(1), channel(2), channel(3), ["case", black, 0, 1]],
  } as Style;
}

async function main() {
  const url = new URLSearchParams(location.search).get("url");
  if (!url) throw new Error("no product: open this page as map.html?url=<SAFE product URL>");
  $<HTMLAnchorElement>("back").href = `./?url=${encodeURIComponent(url)}`;
  setStatus("Starting the service worker…");
  const prefix = await registerVzipWorker(new URL("vzip-sw.js", location.href));
  const zarrUrl = /\.vzip(?:[?#]|$)/i.test(url) ? archiveZarrUrl(prefix, url) : imageZarrUrl(prefix, url);
  setStatus("Reading the product (a few minutes for a large SAFE zip)…");
  const t0 = performance.now();
  const root = await getJson(`${zarrUrl}zarr.json`);
  const attrs = root.attributes ?? {};
  const decl = attrs.vzip_virtualized;
  if (decl?.profile !== "safe") throw new Error("not a virtualized Sentinel-2 SAFE product");
  const safe = decl.safe ?? {};
  $("name").textContent = safe.PRODUCT_URI ?? url;
  $("meta").textContent = [safe.PROCESSING_LEVEL, safe.SENSING_TIME, attrs["proj:code"]]
    .filter(Boolean).join(" · ");

  // OpenLayers looks the CRS up by its code (proj:code), and does not fall
  // back to proj:wkt2; define the code from the WKT2 the product carries.
  const code: string = attrs["proj:code"];
  if (code && attrs["proj:wkt2"] && !proj4.defs(code)) proj4.defs(code, attrs["proj:wkt2"]);
  register(proj4);

  // A Level-2A product presents its bands at 10, 20 and 60 m as the levels of
  // one multiscale (the root's multiscales layout); a Level-1C product has
  // each band at one resolution only, and no multiscales.
  const multiscale = Array.isArray(attrs.multiscales?.layout);
  const radiometry = async (band: string): Promise<Radiometry> => {
    const s = (await getJson(`${zarrUrl}r10m/${band}/zarr.json`)).attributes?.vzip_virtualized?.safe ?? {};
    // Offsets exist from processing baseline 04.00; they are not applied to
    // the arrays (§6.2), so the style applies them.
    const q = s.BOA_QUANTIFICATION_VALUE ?? s.QUANTIFICATION_VALUE;
    return {
      offset: Number(s.BOA_ADD_OFFSET ?? s.RADIO_ADD_OFFSET ?? 0),
      quantification: Number(q?.value ?? q ?? 10000),
    };
  };
  const rgb = ["B04", "B03", "B02"];
  const rad = await Promise.all(rgb.map(radiometry));
  const renderings: Rendering[] = [
    {
      label: "Natural color (B04, B03, B02 reflectance)",
      source: () => multiscale
        ? new GeoZarr({ url: zarrUrl, bands: rgb })
        : new GeoZarr({ url: zarrUrl, bands: rgb.map((name) => ({ name, group: "r10m" })) }),
      style: reflectanceStyle(rad),
      bands: rgb,
    },
    {
      label: "True-color image (TCI, 8-bit)",
      source: () => new GeoZarr({
        url: multiscale ? zarrUrl : `${zarrUrl}r10m`, variable: "TCI", dimensions: { c: [0, 1, 2] },
      }),
      style: tciStyle(),
      bands: ["TCI red", "TCI green", "TCI blue"],
    },
  ];

  const map = new OlMap({ target: "map", layers: [new TileLayer({ source: new OSM() })] });
  let layer: WebGLTileLayer | undefined;
  let shown: Rendering | undefined;
  const brightness = $<HTMLInputElement>("brightness");
  const select = $<HTMLSelectElement>("rendering");
  select.replaceChildren(...renderings.map((r, i) => new Option(r.label, String(i))));

  const show = (r: Rendering, fit: boolean) => {
    const source = r.source();
    if (layer) map.removeLayer(layer);
    layer = new WebGLTileLayer({ source, style: r.style });
    layer.updateStyleVariables({ [BRIGHTNESS]: Number(brightness.value) });
    map.addLayer(layer);
    shown = r;
    if (fit) map.setView(source.getView() as never);
    source.on("change", () => {
      if (source.getState() === "error") {
        setStatus(String((source as unknown as { error_?: Error }).error_?.message ?? "the source failed"), true);
      }
    });
    return source;
  };
  select.addEventListener("change", () => show(renderings[Number(select.value)], false));
  brightness.addEventListener("input", () => layer?.updateStyleVariables({ [BRIGHTNESS]: Number(brightness.value) }));

  const source = show(renderings[0], true);
  await new Promise<void>((resolve, reject) => {
    const check = () => {
      if (source.getState() === "ready") resolve();
      if (source.getState() === "error") reject((source as unknown as { error_?: Error }).error_ ?? new Error("the source failed"));
    };
    source.on("change", check);
    check();
  });
  setStatus(`Ready in ${Math.round(performance.now() - t0)} ms. Hover for values.`);

  const readout = $("readout");
  map.on("pointermove", (event) => {
    const data = layer?.getData(event.pixel) as ArrayLike<number> | null | undefined;
    const [lon, lat] = toLonLat(event.coordinate, map.getView().getProjection());
    const where = `${lat.toFixed(4)}°, ${lon.toFixed(4)}°`;
    readout.textContent = data && shown
      ? `${where}: ${shown.bands.map((b, i) => `${b} ${Math.round(data[i])}`).join(", ")}`
      : where;
  });
  // For the end-to-end test.
  (window as unknown as { vzipMap: unknown }).vzipMap = { map, getLayer: () => layer, zarrUrl };
}

main().catch((e) => setStatus(String(e.message ?? e), true));
