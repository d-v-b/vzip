import {
  archiveDownloadUrl,
  archiveZarrUrl,
  registerVzipWorker,
  tiffZarrUrl,
} from "../src/client.ts";
import { WORKER_HEADER } from "../src/server.ts";

const EXAMPLE =
  "https://ftp.ebi.ac.uk/pub/databases/IDR/idr0096-tratwal-marrowquant/20210609-ftp-ome-tiffs/4000_d11_m5_LT_2%20(20x_01).ome.tiff";
// A virtualized Nikon ND2 time-lapse (BioImage Archive S-BIAD3015, 4.6 GB),
// made by experiments/nd2_to_vzip.py.
const ND2_EXAMPLE =
  "https://raw.githubusercontent.com/d-v-b/vzip/main/experiments/out/nd2/biad3015_1-SR_1_9_6hPre-C_MC1.vzip";

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const input = $<HTMLInputElement>("url");
const status = $("status");

const setStatus = (text: string, error = false) => {
  status.textContent = text;
  status.classList.toggle("error", error);
};

async function getJson(url: string) {
  const r = await fetch(url);
  if (r.headers.get(WORKER_HEADER) === null) {
    throw new Error(
      `the vzip service worker did not answer this request (HTTP ${r.status} from the network); reload the page and try again`,
    );
  }
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json();
}

/** A Neuroglancer state for an OME-Zarr image; RGB as three additive layers. */
const METERS: Record<string, number> = {
  meter: 1, millimeter: 1e-3, micrometer: 1e-6, nanometer: 1e-9, picometer: 1e-12, angstrom: 1e-10,
  centimeter: 1e-2, inch: 0.0254, foot: 0.3048,
};

interface OmeroChannel {
  label?: string;
  color?: string;
  window?: { start: number; end: number };
}

function neuroglancerState(
  zarrUrl: string,
  axes: { name: string; unit?: string }[],
  scale: number[],
  shape: number[],
  dtype: string,
  omero: OmeroChannel[] = [],
) {
  const source = `${zarrUrl}|zarr3:`;
  // Fit the whole image in the view: crossSectionScale is in meters per
  // screen pixel (or voxels per pixel if the axes have no unit).
  const n = shape.length;
  const extent = (i: number) => shape[i] * scale[i] * (METERS[axes[i].unit ?? ""] ?? 1);
  const view = {
    crossSectionScale: Math.max(extent(n - 2) / 600, extent(n - 1) / 850),
    layout: "xy",
    // Without this, Neuroglancer may display a non-spatial axis such as t.
    displayDimensions: ["x", "y"],
  };
  const c = axes.findIndex((a) => a.name === "c");
  if (c >= 0 && shape[c] === 3 && dtype === "uint8") {
    const colors = ["v, 0.0, 0.0", "0.0, v, 0.0", "0.0, 0.0, v"];
    return {
      layers: colors.map((rgb, i) => ({
        type: "image", source, name: ["red", "green", "blue"][i], opacity: 1, blend: "additive",
        localDimensions: { "c'": [1, ""] }, localPosition: [i],
        shader: `void main() {\n  float v = toNormalized(getDataValue());\n  emitRGB(vec3(${rgb}));\n}\n`,
      })),
      crossSectionBackgroundColor: "#000000", ...view,
    };
  }
  // Other multi-channel images: one layer per channel, with the colours and
  // contrast windows from OME's omero metadata when it has them.
  if (c >= 0 && shape[c] > 1 && shape[c] <= 8) {
    const hex = (s: string) => [0, 2, 4].map((i) => (parseInt(s.slice(i, i + 2), 16) / 255).toFixed(3));
    return {
      layers: Array.from({ length: shape[c] }, (_, i) => {
        const ch = omero[i] ?? {};
        const [r, g, b] = hex(ch.color ?? "FFFFFF");
        const range = ch.window ? `(range=[${ch.window.start}, ${ch.window.end}])` : "";
        return {
          type: "image", source, name: ch.label ?? `channel ${i}`, opacity: 1, blend: "additive",
          localDimensions: { "c'": [1, ""] }, localPosition: [i],
          shader: `#uicontrol invlerp contrast${range}\nvoid main() {\n  emitRGB(vec3(${r}, ${g}, ${b}) * contrast());\n}\n`,
        };
      }),
      crossSectionBackgroundColor: "#000000", ...view,
    };
  }
  return { layers: [{ type: "image", source, name: "image" }], ...view };
}

let prefix: Promise<string> | undefined;

async function virtualize(url: string) {
  $("result").hidden = true;
  // Record the URL in the address bar, so the page can be shared or reloaded.
  const here = new URL(location.href);
  here.searchParams.set("url", url);
  history.replaceState(null, "", here);
  setStatus("Starting the service worker…");
  prefix ??= registerVzipWorker(new URL("vzip-sw.js", location.href));
  const p = await prefix;
  const isArchive = /\.vzip(?:[?#]|$)/i.test(url);
  const zarrUrl = isArchive ? archiveZarrUrl(p, url) : tiffZarrUrl(p, url);
  setStatus(isArchive ? "Opening the archive…" : "Reading the TIFF's directories…");
  const t0 = performance.now();
  const group = await getJson(`${zarrUrl}zarr.json`);
  const ms = Math.round(performance.now() - t0);
  // The image: the root itself, or one series of a bioformats2raw layout
  // (e.g. one stage position of an ND2), chosen with ?series=.
  let imageUrl = zarrUrl;
  let ms0 = group.attributes?.ome?.multiscales?.[0];
  const seriesRow = $("series-row");
  seriesRow.hidden = true;
  if (ms0 === undefined && group.attributes?.ome?.["bioformats2raw.layout"] !== undefined) {
    const series: string[] = (await getJson(`${zarrUrl}OME/zarr.json`)).attributes?.ome?.series ?? [];
    const chosen = here.searchParams.get("series") ?? series[0];
    if (!series.includes(chosen)) throw new Error(`no series ${JSON.stringify(chosen)}`);
    imageUrl = `${zarrUrl}${chosen}/`;
    ms0 = (await getJson(`${imageUrl}zarr.json`)).attributes?.ome?.multiscales?.[0];
    const select = $<HTMLSelectElement>("series");
    select.replaceChildren(
      ...series.map((s) => Object.assign(document.createElement("option"), { value: s, textContent: s, selected: s === chosen })),
    );
    $("series-count").textContent = `of ${series.length}`;
    seriesRow.hidden = false;
  }
  if (ms0 === undefined) throw new Error("not an OME-Zarr multiscale image");
  const rows = await Promise.all(
    ms0.datasets.map(async (d: { path: string }) => [d.path, await getJson(`${imageUrl}${d.path}/zarr.json`)]),
  );
  const tbody = $("levels");
  tbody.replaceChildren(
    ...rows.map(([path, a]) => {
      const tr = document.createElement("tr");
      for (const v of [
        path,
        a.shape.join(" × "),
        a.chunk_grid.configuration.chunk_shape.join(" × "),
        a.data_type,
        a.codecs.map((c: { name: string }) => c.name).join(", "),
      ]) {
        const td = document.createElement("td");
        td.textContent = v;
        tr.append(td);
      }
      return tr;
    }),
  );
  $("name").textContent = ms0.name ?? url.split("/").pop();
  $("zarr-url").textContent = imageUrl;
  const [, level0] = rows[0];
  const scale = ms0.datasets[0].coordinateTransformations?.find(
    (t: { type: string }) => t.type === "scale",
  )?.scale ?? level0.shape.map(() => 1);
  const omero = (await getJson(`${imageUrl}zarr.json`)).attributes?.ome?.omero?.channels ?? [];
  const state = neuroglancerState(imageUrl, ms0.axes, scale, level0.shape, level0.data_type, omero);
  $<HTMLAnchorElement>("open-ng").href = new URL(
    `neuroglancer/#!${encodeURIComponent(JSON.stringify(state))}`,
    location.href,
  ).href;
  $<HTMLAnchorElement>("download").href = archiveDownloadUrl(zarrUrl);
  $("result").hidden = false;
  setStatus(`Ready in ${ms} ms.`);
}

$("series").addEventListener("change", () => {
  const here = new URL(location.href);
  here.searchParams.set("series", $<HTMLSelectElement>("series").value);
  history.replaceState(null, "", here);
  virtualize(input.value.trim()).catch((e) => setStatus(String(e.message ?? e), true));
});
$("form").addEventListener("submit", (event) => {
  event.preventDefault();
  // A new URL starts from its first series.
  const here = new URL(location.href);
  if (here.searchParams.get("url") !== input.value.trim()) {
    here.searchParams.delete("series");
    history.replaceState(null, "", here);
  }
  virtualize(input.value.trim()).catch((e) => setStatus(String(e.message ?? e), true));
});
for (const [id, url] of [["example", EXAMPLE], ["example-nd2", ND2_EXAMPLE]]) {
  $(id).addEventListener("click", (event) => {
    event.preventDefault();
    input.value = url;
    $<HTMLFormElement>("form").requestSubmit();
  });
}
const fromQuery = new URLSearchParams(location.search).get("url");
if (fromQuery) {
  input.value = fromQuery;
  $<HTMLFormElement>("form").requestSubmit();
}
