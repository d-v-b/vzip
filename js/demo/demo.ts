import { Archive } from "../src/archive.ts";
import {
  archiveDownloadUrl,
  archiveZarrUrl,
  imageZarrUrl,
  registerVzipWorker,
} from "../src/client.ts";
import { WORKER_HEADER } from "../src/server.ts";

// The examples are the links with a data-url attribute in index.html, where
// their sources and licenses are noted.

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

/** One image to show: its Zarr URL, a name, and its translation (one number per axis, in the axes' units). */
interface ShownImage {
  url: string;
  name: string;
  translation?: number[];
}

/**
 * A Neuroglancer state for OME-Zarr images that share axes, scale and shape:
 * one image, or every stage position of a multi-position file, each placed by
 * its translation.
 *
 * When every chunk holds all channels (ND2 frames, interleaved TIFF), an
 * image is one layer whose channel axis is a Neuroglancer channel dimension
 * (`c^`): the shader composites the channels, each with a checkbox, a color
 * and a contrast range from the omero metadata, and there is no channel axis
 * to scroll. Otherwise (planar TIFF, one channel per chunk) each channel is a
 * layer of its own.
 */
function imageState(
  images: ShownImage[],
  axes: { name: string; unit?: string }[],
  scale: number[],
  shape: number[],
  chunks: number[],
  dtype: string,
  omero: OmeroChannel[] = [],
  labels: { name: string; url: string }[] = [],
) {
  // The two displayed axes: x and y, or else the last two.
  let xi = axes.findIndex((a) => a.name === "x");
  let yi = axes.findIndex((a) => a.name === "y");
  if (xi < 0 || yi < 0) [yi, xi] = [axes.length - 2, axes.length - 1];
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
  // Where each image's voxel (0, 0) lands, in full-resolution voxels of the
  // first image (Neuroglancer's coordinates with these dimensions).
  const offset = (img: ShownImage, i: number) => (img.translation?.[i] ?? 0) / scale[i];
  // Start at the first time point (Neuroglancer would pick the middle one),
  // the middle z slice, and the centre of the first image, fitted to the
  // view, each where the image's translation places it (an image translated
  // in z or t would otherwise open outside its data). Zooming out shows the
  // other images where they are; starting with all of them in view would load
  // every one at full resolution.
  const position = axes.flatMap((a, i) => {
    if (a.name === "c") return [];
    const at = offset(images[0], i);
    if (a.name === "t") return [at];
    return [at + (a.name === "z" ? Math.floor(shape[i] / 2) : shape[i] / 2)];
  });
  const view = {
    dimensions,
    position,
    displayDimensions: [axes[xi].name, axes[yi].name],
    // With the coordinate space declared, this counts full-resolution voxels
    // per screen pixel.
    crossSectionScale: Math.max(shape[yi] / 600, shape[xi] / 850),
    layout: "xy",
  };
  const many = images.length > 1;
  const c = axes.findIndex((a) => a.name === "c");
  if (c >= 0 && shape[c] > 1 && shape[c] <= 16 && chunks[c] === shape[c]) {
    const outputDimensions = Object.fromEntries(
      axes.map((a) => (a.name === "c" ? ["c^", [1, ""]] : [a.name, dimensions[a.name]])),
    );
    const rgb = shape[c] === 3 && dtype === "uint8" && omero.length === 0;
    const shader = rgb ? RGB_SHADER : channelShader(shape[c], omero);
    return {
      layers: images.map((img) => ({
        type: "image",
        source: { url: `${img.url}|zarr3:`, transform: { outputDimensions } },
        name: many ? img.name : rgb ? "image" : "channels",
        opacity: 1,
        shader,
      })),
      crossSectionBackgroundColor: "#000000", ...view,
    };
  }
  // Planar channels: a layer per channel of each image.
  const perChannel = (label: string, i: number, shader: string) =>
    images.map((img) => ({
      type: "image", source: `${img.url}|zarr3:`, name: many ? `${img.name} ${label}` : label,
      opacity: 1, blend: "additive", localDimensions: { "c'": [1, ""] }, localPosition: [i], shader,
    }));
  if (c >= 0 && shape[c] === 3 && dtype === "uint8") {
    const colors = ["v, 0.0, 0.0", "0.0, v, 0.0", "0.0, 0.0, v"];
    return {
      layers: colors.flatMap((rgb, i) => perChannel(["red", "green", "blue"][i], i,
        `void main() {\n  float v = toNormalized(getDataValue());\n  emitRGB(vec3(${rgb}));\n}\n`)),
      crossSectionBackgroundColor: "#000000", ...view,
    };
  }
  // Other multi-channel images: one layer per channel, with the colors and
  // contrast windows from OME's omero metadata when it has them.
  if (c >= 0 && shape[c] > 1 && shape[c] <= 8) {
    const hex = (s: string) => [0, 2, 4].map((i) => (parseInt(s.slice(i, i + 2), 16) / 255).toFixed(3));
    return {
      layers: Array.from({ length: shape[c] }, (_, i) => {
        const ch = omero[i] ?? {};
        const [r, g, b] = hex(ch.color ?? "FFFFFF");
        const range = ch.window ? `(range=[${ch.window.start}, ${ch.window.end}])` : "";
        return perChannel(ch.label ?? `channel ${i}`, i,
          `#uicontrol invlerp contrast${range}\nvoid main() {\n  emitRGB(vec3(${r}, ${g}, ${b}) * contrast());\n}\n`);
      }).flat(),
      crossSectionBackgroundColor: "#000000", ...view,
    };
  }
  return {
    layers: images.map((img) => ({ type: "image", source: `${img.url}|zarr3:`, name: many ? img.name : "image" })),
    ...view,
  };
}

/** {@link imageState}, with a segmentation layer for each OME-Zarr label image. */
function neuroglancerState(...args: Parameters<typeof imageState>) {
  const state = imageState(...args);
  const labels = args[7] ?? [];
  state.layers.push(...labels.map((l) => ({
    type: "segmentation", source: `${l.url}|zarr3:`, name: `labels ${l.name}`,
  })) as never[]);
  return state;
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

/** The translation of a multiscale image's first level, if it has one. */
function translationOf(ms: { datasets?: { coordinateTransformations?: { type: string; translation?: number[] }[] }[] } | undefined) {
  return ms?.datasets?.[0]?.coordinateTransformations?.find((t) => t.type === "translation")?.translation;
}

type ArrayMeta = any;

/**
 * The chunk shape of a Zarr array: that of a regular chunk grid, or for a
 * rectilinear one (clipped CZI levels) the largest chunk length per axis.
 */
function chunkShapeOf(a: ArrayMeta): number[] {
  const { name, configuration } = a.chunk_grid;
  if (name !== "rectilinear") return configuration.chunk_shape;
  return configuration.chunk_shapes.map((lengths: number | (number | [number, number])[]) =>
    typeof lengths === "number" ? lengths : Math.max(...lengths.map((l) => (Array.isArray(l) ? l[0] : l))),
  );
}

/** One level of an image: its path (for the table), URL and metadata. */
interface Level {
  path: string;
  /** The array's URL, ending in "/". */
  url: string;
  meta: ArrayMeta;
}

/**
 * What can be opened in Neuroglancer as one view: an OME-Zarr image, every
 * series of a file together, one series, or a plain array.
 */
interface View {
  label: string;
  name: string;
  /** The URL shown as the Zarr URL. */
  url: string;
  images: ShownImage[];
  axes: { name: string; unit?: string }[];
  scale: number[];
  levels: Level[];
  omero: OmeroChannel[];
  labels: { name: string; url: string }[];
  note?: string;
}

type Multiscale = any;

/** The view of the OME-Zarr multiscale image at `url` (a group URL). */
async function omeView(url: string, ms: Multiscale, label: string, withLabels = false): Promise<View> {
  const levels = await Promise.all(
    ms.datasets.map(async (d: { path: string }) => ({
      path: d.path, url: `${url}${d.path}/`, meta: await getJson(`${url}${d.path}/zarr.json`),
    })),
  );
  const scale = ms.datasets[0].coordinateTransformations?.find(
    (t: { type: string }) => t.type === "scale",
  )?.scale ?? levels[0].meta.shape.map(() => 1);
  const group = await getJson(`${url}zarr.json`);
  // An OME-Zarr store's label images, as segmentation layers. (Only an
  // OME-Zarr input has them; looking for them elsewhere would be a 404.)
  let labels: { name: string; url: string }[] = [];
  if (withLabels) {
    try {
      const names: string[] = (await getJson(`${url}labels/zarr.json`)).attributes?.ome?.labels ?? [];
      labels = names.map((n) => ({ name: n, url: `${url}labels/${n}/` }));
    } catch {
      // No labels.
    }
  }
  return {
    label, name: ms.name ?? label, url, images: [{ url, name: ms.name ?? label, translation: translationOf(ms) }],
    axes: ms.axes, scale, levels, omero: group.attributes?.ome?.omero?.channels ?? [], labels,
  };
}

/** The view of a plain Zarr array (no OME-Zarr metadata) at `url`. */
function arrayView(url: string, path: string, meta: ArrayMeta): View {
  // Neuroglancer names unnamed zarr v3 dimensions dim_0, dim_1, …
  const axes = meta.shape.map((_: number, i: number) => ({ name: meta.dimension_names?.[i] ?? `dim_${i}` }));
  const label = path === "" ? "the array" : path;
  return {
    label, name: label, url, images: [{ url, name: path || "array" }], axes,
    scale: axes.map(() => 1), levels: [{ path: path || "/", url, meta }], omero: [], labels: [],
  };
}

/** The views of a bioformats2raw layout: each series, and all of them together when they can be. */
async function seriesViews(zarrUrl: string): Promise<View[]> {
  const series: string[] = (await getJson(`${zarrUrl}OME/zarr.json`)).attributes?.ome?.series ?? [];
  const groups = await Promise.all(series.map((s) => getJson(`${zarrUrl}${s}/zarr.json`)));
  const views = await Promise.all(series.map((s, i) => {
    const ms = groups[i].attributes?.ome?.multiscales?.[0];
    return omeView(`${zarrUrl}${s}/`, ms, `series ${s}${ms?.name ? ` (${ms.name})` : ""}`);
  }));
  if (views.length < 2) return views;
  // Series are shown together, each placed by its translation, when they have
  // the same axes, data type and channels (the stage positions of an ND2 or a
  // CZI); otherwise (CZI scenes of other types or axes) one at a time.
  const key = (v: View) => {
    const m = v.levels[0].meta;
    const c = v.axes.findIndex((a) => a.name === "c");
    return JSON.stringify([v.axes.map((a) => a.name), m.data_type, c < 0 ? 0 : m.shape[c], c < 0 ? 0 : chunkShapeOf(m)[c]]);
  };
  if (views.some((v) => key(v) !== key(views[0]))) {
    views[0].note = `${views.length} series of different axes, data types or channels: pick one.`;
    return views;
  }
  const placed = views.every((v) => v.images[0].translation !== undefined);
  const all: View = {
    ...views[0],
    label: `all ${views.length} series`,
    url: views[0].url,
    images: views.map((v) => v.images[0]),
    labels: [],
    note: `${views.length} images (series ${series[0]}–${series[series.length - 1]}), ` +
      (placed ? "placed at their stage positions; zoom out in Neuroglancer to see them all." :
        "without stage positions, so they overlap."),
  };
  return [all, ...views];
}

/**
 * The arrays of the hierarchy at `zarrUrl`, outside `vzip_source`, from the
 * keys of its archive (which the worker holds in memory): a Zarr hierarchy
 * cannot otherwise be listed.
 */
async function listArrays(zarrUrl: string): Promise<{ path: string; meta: ArrayMeta }[]> {
  const r = await fetch(archiveDownloadUrl(zarrUrl));
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  const archive = await Archive.open(new Uint8Array(await r.arrayBuffer()), zarrUrl, () => {
    throw new Error("not read");
  });
  const paths = archive.keys()
    .filter((k) => (k === "zarr.json" || k.endsWith("/zarr.json")) && !k.startsWith("vzip_source/"))
    .map((k) => k.slice(0, -"zarr.json".length).replace(/\/$/, ""));
  const nodes = await Promise.all(paths.map(async (path) => ({
    path, meta: await getJson(`${zarrUrl}${path === "" ? "" : `${path}/`}zarr.json`),
  })));
  return nodes.filter((n) => n.meta.node_type === "array");
}

let prefix: Promise<string> | undefined;
let views: View[] = [];

function showLevels(rows: Omit<Level, "url">[]) {
  $("levels").replaceChildren(
    ...rows.map(({ path, meta: a }) => {
      const tr = document.createElement("tr");
      for (const v of [
        path,
        a.shape.join(" × "),
        chunkShapeOf(a).join(" × ") + (a.chunk_grid.name === "rectilinear" ? " (clipped)" : ""),
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
}

/**
 * A contrast range for an image without one: the 1st and 99th percentiles of
 * a chunk of its coarsest level where the view opens, when it is uncompressed or
 * gzip or zlib compressed (as NIfTI, DICOM and IMS chunks are), else
 * undefined. Neuroglancer would otherwise map the whole range of the data
 * type, which leaves most 16-bit images uniformly gray.
 */
async function sampleRange(level: Level, axes: { name: string }[]): Promise<[number, number] | undefined> {
  const a = level.meta;
  const [first, ...rest] = a.codecs.map((c: { name: string }) => c.name);
  const TYPES: Record<string, [number, (v: DataView, i: number, le: boolean) => number]> = {
    uint8: [1, (v, i) => v.getUint8(i)], int8: [1, (v, i) => v.getInt8(i)],
    uint16: [2, (v, i, le) => v.getUint16(i, le)], int16: [2, (v, i, le) => v.getInt16(i, le)],
    uint32: [4, (v, i, le) => v.getUint32(i, le)], int32: [4, (v, i, le) => v.getInt32(i, le)],
    float32: [4, (v, i, le) => v.getFloat32(i, le)], float64: [8, (v, i, le) => v.getFloat64(i, le)],
  };
  const type = TYPES[a.data_type];
  if (first !== "bytes" || rest.some((c: string) => c !== "gzip" && c !== "zlib") || !type || a.chunk_grid.name !== "regular") {
    return undefined;
  }
  const chunks = chunkShapeOf(a);
  // The chunk the view opens on: the first time point, the middle elsewhere.
  const index = a.shape.map((n: number, i: number) => axes[i]?.name === "t" ? 0 : Math.floor(Math.floor(n / 2) / chunks[i]));
  const enc = a.chunk_key_encoding ?? { name: "default" };
  const sep = enc.configuration?.separator ?? (enc.name === "v2" ? "." : "/");
  const key = enc.name === "v2" ? index.join(sep) || "0" : ["c", ...index].join(sep);
  const r = await fetch(`${level.url}${key}`);
  if (!r.ok) return undefined;
  let bytes = new Uint8Array(await r.arrayBuffer());
  for (const c of [...rest].reverse()) {
    const inflate = new DecompressionStream(c === "gzip" ? "gzip" : "deflate") as TransformStream<Uint8Array, Uint8Array>;
    bytes = new Uint8Array(await new Response(new Blob([bytes]).stream().pipeThrough(inflate)).arrayBuffer());
  }
  const [size, get] = type;
  const le = a.codecs[0].configuration?.endian !== "big";
  const view = new DataView(bytes.buffer);
  const n = Math.floor(bytes.length / size);
  const step = Math.max(1, Math.floor(n / 100000));
  const values: number[] = [];
  for (let i = 0; i < n; i += step) values.push(get(view, i * size, le));
  values.sort((x, y) => x - y);
  const lo = values[Math.floor(values.length * 0.01)], hi = values[Math.floor(values.length * 0.99)];
  return lo < hi ? [lo, hi] : undefined;
}

let shown: View | undefined;

/** Shows `view`: its levels, and a Neuroglancer link. */
async function showView(view: View) {
  shown = view;
  $("name").textContent = view.name;
  $("zarr-url").textContent = view.url;
  $("view-note").textContent = view.note ?? "";
  showLevels(view.levels);
  const level0 = view.levels[0].meta;
  // A window of integers narrower than one value maps every value to black
  // or white (a CZI's display setting can be such a window): leave it out.
  const omero = view.omero.map((ch) =>
    ch.window && /int/.test(level0.data_type) && ch.window.end - ch.window.start < 1 ? { ...ch, window: undefined } : ch
  );
  const state = neuroglancerState(
    view.images, view.axes, view.scale, level0.shape, chunkShapeOf(level0), level0.data_type, omero, view.labels,
  );
  const ng = $<HTMLAnchorElement>("open-ng");
  ng.hidden = false;
  const link = () => {
    ng.href = new URL(`neuroglancer/#!${encodeURIComponent(JSON.stringify(state))}`, location.href).href;
  };
  link();
  // An image shown with Neuroglancer's default shader, without a contrast
  // range, gets one from its data.
  const plain = state.layers.filter((l) => l.type === "image" && !("shader" in l));
  if (plain.length > 0 && level0.data_type !== "uint8") {
    const range = await sampleRange(view.levels[view.levels.length - 1], view.axes).catch(() => undefined);
    if (range === undefined || shown !== view) return;
    for (const l of plain) {
      Object.assign(l, { shader: `#uicontrol invlerp normalized(range=[${range[0]}, ${range[1]}])\nvoid main() {\n  emitGrayscale(normalized());\n}\n` });
    }
    link();
  }
}

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
  const root = await getJson(`${zarrUrl}zarr.json`);
  const ms = Math.round(performance.now() - t0);
  const attrs = root.attributes ?? {};
  const map = $<HTMLAnchorElement>("open-map");
  const picker = $<HTMLSelectElement>("view");
  map.hidden = true;
  $("view-row").hidden = true;
  if (attrs.vzip_virtualized?.profile === "safe") {
    // A Sentinel-2 product: GeoZarr groups of JPEG 2000 bands, for a map
    // viewer (map.html) rather than Neuroglancer.
    const safe = attrs.vzip_virtualized.safe ?? {};
    views = [];
    $("name").textContent = `${safe.PRODUCT_URI ?? url.split("/").pop()} (${safe.PROCESSING_LEVEL ?? "Sentinel-2"})`;
    $("zarr-url").textContent = zarrUrl;
    $("view-note").textContent = `${safe.SENSING_TIME ?? ""} ${attrs["proj:code"] ?? ""}`;
    showLevels(await listArrays(zarrUrl));
    $("open-ng").hidden = true;
    map.hidden = false;
    map.href = new URL(`map.html?url=${encodeURIComponent(url)}`, location.href).href;
  } else {
    const ms0 = attrs.ome?.multiscales?.[0];
    if (root.node_type === "array") {
      views = [arrayView(zarrUrl, "", root)];
    } else if (ms0 !== undefined) {
      views = [await omeView(zarrUrl, ms0, ms0.name ?? "image", attrs.vzip_virtualized?.profile === "ome-zarr")];
    } else if (attrs.ome?.["bioformats2raw.layout"] !== undefined) {
      views = await seriesViews(zarrUrl);
    } else {
      // No OME-Zarr image (a Zarr v2 or N5 hierarchy without one): its arrays.
      views = (await listArrays(zarrUrl)).map(({ path, meta }) => arrayView(`${zarrUrl}${path}/`, path, meta));
      if (views.length === 0) throw new Error("no arrays to show");
    }
    if (views.length > 1) {
      picker.replaceChildren(...views.map((v, i) => new Option(v.label, String(i))));
      $("view-row").hidden = false;
    }
    await showView(views[0]);
  }
  $<HTMLAnchorElement>("download").href = archiveDownloadUrl(zarrUrl);
  $("result").hidden = false;
  setStatus(`Ready in ${ms} ms.`);
}

$("view").addEventListener("change", () => {
  showView(views[Number($<HTMLSelectElement>("view").value)]).catch((e) => setStatus(String(e.message ?? e), true));
});
$("form").addEventListener("submit", (event) => {
  event.preventDefault();
  virtualize(input.value.trim()).catch((e) => setStatus(String(e.message ?? e), true));
});
for (const link of document.querySelectorAll<HTMLAnchorElement>("a[data-url]")) {
  link.addEventListener("click", (event) => {
    event.preventDefault();
    input.value = link.dataset.url!;
    $<HTMLFormElement>("form").requestSubmit();
  });
}
const fromQuery = new URLSearchParams(location.search).get("url");
if (fromQuery) {
  input.value = fromQuery;
  $<HTMLFormElement>("form").requestSubmit();
}
