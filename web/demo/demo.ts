import {
  archiveDownloadUrl,
  archiveZarrUrl,
  imageZarrUrl,
  registerVzipWorker,
} from "../src/client.ts";
import { WORKER_HEADER } from "../src/server.ts";

const EXAMPLE =
  "https://ftp.ebi.ac.uk/pub/databases/IDR/idr0096-tratwal-marrowquant/20210609-ftp-ome-tiffs/4000_d11_m5_LT_2%20(20x_01).ome.tiff";
// A Nikon ND2 time-lapse (BioImage Archive S-BIAD3015, 4.6 GB).
const ND2_EXAMPLE =
  "https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/015/S-BIAD3015/Files/1-SR_1_9_6hPre-C_MC1.nd2";
// A Nikon ND2 z-stack with five channels (BioImage Archive S-BIAD2077, 263 MB).
const ND2_ZSTACK_EXAMPLE =
  "https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/077/S-BIAD2077/Files/373_230614_A1_Blk_Reg2_40x.nd2";

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

/**
 * A Neuroglancer state for an OME-Zarr image.
 *
 * When every chunk holds all channels (ND2 frames, interleaved TIFF), the
 * image is one layer whose channel axis is a Neuroglancer channel dimension
 * (`c^`): the shader composites the channels, each with a checkbox, a colour
 * and a contrast range from the omero metadata, and there is no channel axis
 * to scroll. Otherwise (planar TIFF, one channel per chunk) each channel is a
 * layer of its own.
 */
const SECONDS: Record<string, number> = { second: 1, millisecond: 1e-3, minute: 60, hour: 3600 };
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
  chunks: number[],
  dtype: string,
  omero: OmeroChannel[] = [],
) {
  const source = `${zarrUrl}|zarr3:`;
  const n = shape.length;
  // The coordinate space, declared up front so that displayDimensions can
  // name x and y before the layers load (otherwise Neuroglancer may display a
  // non-spatial axis such as t).
  const dimensions: Record<string, [number, string]> = {};
  axes.forEach((a, i) => {
    if (a.name === "c") return;
    const unit = a.unit ?? "";
    if (unit in METERS) dimensions[a.name] = [scale[i] * METERS[unit], "m"];
    else if (unit in SECONDS) dimensions[a.name] = [scale[i] * SECONDS[unit], "s"];
    else dimensions[a.name] = [scale[i], ""];
  });
  // Start at the first time point (Neuroglancer would pick the middle one),
  // the middle z slice, and the centre of the image.
  const position = axes.flatMap((a, i) =>
    a.name === "c" ? [] : [a.name === "t" ? 0 : a.name === "z" ? Math.floor(shape[i] / 2) : shape[i] / 2]);
  const view = {
    dimensions,
    position,
    displayDimensions: ["x", "y"],
    // Fit the whole image: with the coordinate space declared, this counts
    // full-resolution voxels per screen pixel.
    crossSectionScale: Math.max(shape[n - 2] / 600, shape[n - 1] / 850),
    layout: "xy",
  };
  const c = axes.findIndex((a) => a.name === "c");
  if (c >= 0 && shape[c] > 1 && shape[c] <= 16 && chunks[c] === shape[c]) {
    const outputDimensions = Object.fromEntries(
      axes.map((a) => (a.name === "c" ? ["c^", [1, ""]] : [a.name, dimensions[a.name]])),
    );
    const rgb = shape[c] === 3 && dtype === "uint8" && omero.length === 0;
    return {
      layers: [{
        type: "image",
        source: { url: source, transform: { outputDimensions } },
        name: rgb ? "image" : "channels",
        opacity: 1,
        shader: rgb ? RGB_SHADER : channelShader(shape[c], omero),
      }],
      crossSectionBackgroundColor: "#000000", ...view,
    };
  }
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

const RGB_SHADER = `void main() {
  emitRGB(vec3(toNormalized(getDataValue(0)), toNormalized(getDataValue(1)), toNormalized(getDataValue(2))));
}
`;

/** A shader compositing `n` channels, with a control per channel. */
function channelShader(n: number, omero: OmeroChannel[]): string {
  // Control names are shown as labels in the layer panel; Neuroglancer
  // accepts identifiers starting with a lowercase letter.
  const used = new Set<string>();
  const ident = (label: string, k: number) => {
    let id = label.toLowerCase().replace(/[^a-z0-9_]/g, "_").replace(/^(?=[^a-z])/, "c");
    if (id === "" || id.length > 40 || used.has(id)) id = `channel${k}`;
    used.add(id);
    return id;
  };
  const controls: string[] = [];
  const sum: string[] = [];
  for (let k = 0; k < n; k++) {
    const ch = omero[k] ?? {};
    const id = ident(ch.label ?? `channel${k}`, k);
    const range = ch.window ? `, range=[${ch.window.start}, ${ch.window.end}]` : "";
    controls.push(
      `#uicontrol bool ${id}_on checkbox(default=true)`,
      `#uicontrol vec3 ${id}_color color(default="#${(ch.color ?? "FFFFFF").toLowerCase()}")`,
      `#uicontrol invlerp ${id}(channel=${k}${range})`,
    );
    sum.push(`  if (${id}_on) rgb += ${id}_color * ${id}();`);
  }
  return `${controls.join("\n")}
void main() {
  vec3 rgb = vec3(0.0);
${sum.join("\n")}
  emitRGB(rgb);
}
`;
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
  const zarrUrl = isArchive ? archiveZarrUrl(p, url) : imageZarrUrl(p, url);
  setStatus(isArchive ? "Opening the archive…" : "Reading the file's structure…");
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
  const state = neuroglancerState(
    imageUrl, ms0.axes, scale, level0.shape, level0.chunk_grid.configuration.chunk_shape, level0.data_type, omero,
  );
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
for (const [id, url] of [["example", EXAMPLE], ["example-nd2", ND2_EXAMPLE], ["example-nd2-zstack", ND2_ZSTACK_EXAMPLE]]) {
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
