// The N5 profile (profiles/n5.md, §9): an N5 container as a Zarr v3
// hierarchy, each block a whole-object chunk read by the n5_default codec.

import {
  asInt,
  byDepth,
  canonicalIndex,
  compareKeys,
  docKey,
  findChunks,
  GROUP_IMPLICIT,
  grid,
  has,
  implicitGroups,
  insideArray,
  isNumber,
  isObject,
  join,
  type Json,
  readDocument,
  type Store,
  show,
  type StoreResult,
  without,
} from "../store.ts";

export class N5Error extends Error {}

const reject = (message: string): never => {
  throw new N5Error(message);
};

const DATA_TYPES: Record<string, number> = {
  uint8: 1, int8: 1, uint16: 2, int16: 2, uint32: 4, int32: 4, float32: 4, uint64: 8, int64: 8, float64: 8,
};
const ARRAY_KEYS = ["dimensions", "blockSize", "dataType", "compression", "compressionType", "n5"];
const BLOSC_NAMES = ["blosclz", "lz4", "lz4hc", "snappy", "zlib", "zstd"];
const MAX_BLOCK = 2 ** 31 - 1;

// §2.3's units, and the unit names they map to.
const UNITS: Record<string, string> = {
  "µm": "micrometer", "μm": "micrometer", um: "micrometer", nm: "nanometer", mm: "millimeter", cm: "centimeter",
  m: "meter", "Å": "angstrom", "Å": "angstrom", pm: "picometer", in: "inch", ft: "foot", s: "second",
  ms: "millisecond", min: "minute", h: "hour",
};
const UNIT_NAMES = new Set(Object.values(UNITS));

/** The Zarr v3 blosc codec of an n5-blosc or numcodecs Blosc configuration (§9.2, §10.3). */
export function bloscCodec(
  c: { [k: string]: Json },
  typesize: number,
  shuffles: Map<number, string>,
  fail: (m: string) => never,
): Json {
  const cname = c.cname;
  const clevel = asInt(c.clevel, 0, 9);
  const shuffle = asInt(c.shuffle);
  const blocksize = asInt(has(c, "blocksize") ? c.blocksize : 0, 0, MAX_BLOCK);
  if (typeof cname !== "string" || !BLOSC_NAMES.includes(cname)) fail(`blosc cname ${JSON.stringify(cname)} is not supported`);
  if (clevel === undefined) fail(`blosc clevel ${JSON.stringify(c.clevel)} is not an integer from 0 to 9`);
  if (shuffle === undefined || !shuffles.has(shuffle)) fail(`blosc shuffle ${JSON.stringify(c.shuffle)} is not supported`);
  if (blocksize === undefined) fail(`blosc blocksize ${JSON.stringify(c.blocksize)} is not supported`);
  let s = shuffles.get(shuffle!)!;
  if (s === "auto") s = typesize === 1 ? "bitshuffle" : "shuffle";
  return { name: "blosc", configuration: { cname: cname as string, clevel: clevel!, shuffle: s, typesize, blocksize: blocksize! } };
}

const SHUFFLES = new Map([[0, "noshuffle"], [1, "shuffle"], [2, "bitshuffle"]]);

function compressor(compression: { [k: string]: Json }, b: number): Json | undefined {
  const t = compression.type as string;
  if (t === "raw") return undefined;
  if (t === "gzip") {
    const z = has(compression, "useZlib") ? compression.useZlib : false;
    if (typeof z !== "boolean") reject(`gzip useZlib ${JSON.stringify(z)} is not a boolean`);
    return { name: z ? "zlib" : "gzip", configuration: { level: 1 } };
  }
  if (t === "zstd") return { name: "zstd", configuration: { level: 0, checksum: false } };
  if (t === "blosc") return bloscCodec(compression, b, SHUFFLES, reject);
  return reject(`N5 compression ${JSON.stringify(t)} is not supported`);
}

interface ArrayDoc {
  [k: string]: Json;
  shape: number[];
  chunk_grid: { name: string; configuration: { chunk_shape: number[] } };
  attributes: { [k: string]: Json };
}

/** A dataset's zarr.json (§9.2). */
function dataset(path: string, doc: { [k: string]: Json }): ArrayDoc {
  const dims = doc.dimensions;
  const block = doc.blockSize;
  const dtype = doc.dataType;
  if (!Array.isArray(dims) || dims.length < 1 || dims.length > 32 || dims.some((d) => asInt(d, 0) === undefined)) {
    reject(`${path}: dimensions ${show(dims).slice(0, 80)} is not 1 to 32 sizes`);
  }
  const n = (dims as Json[]).length;
  if (!Array.isArray(block) || block.length !== n || block.some((x) => asInt(x, 1, MAX_BLOCK) === undefined)) {
    reject(`${path}: blockSize ${show(block).slice(0, 80)} is not ${n} block sizes`);
  }
  if (typeof dtype !== "string" || !has(DATA_TYPES, dtype)) {
    reject(`${path}: dataType ${show(dtype).slice(0, 40)} is not supported`);
  }
  let compression: { [k: string]: Json };
  if (has(doc, "compression")) {
    const c = doc.compression;
    if (!isObject(c) || typeof c.type !== "string") reject(`${path}: compression is not an object with a string type`);
    compression = c as { [k: string]: Json };
  } else if (typeof doc.compressionType === "string") {
    compression = { type: doc.compressionType };
  } else {
    return reject(`${path}: no compression`);
  }
  const b = DATA_TYPES[dtype as string];
  const inner: Json[] = [
    { name: "transpose", configuration: { order: Array.from({ length: n }, (_, i) => n - 1 - i) } },
    b > 1 ? { name: "bytes", configuration: { endian: "big" } } : { name: "bytes" },
  ];
  const c = compressor(compression, b);
  if (c !== undefined) inner.push(c);
  return {
    zarr_format: 3,
    node_type: "array",
    shape: (dims as number[]).slice(),
    data_type: dtype as string,
    chunk_grid: { name: "regular", configuration: { chunk_shape: (block as number[]).slice() } },
    chunk_key_encoding: { name: "v2", configuration: { separator: "/" } },
    fill_value: 0,
    codecs: [{ name: "n5_default", configuration: { codecs: inner } }],
    attributes: without(doc, ARRAY_KEYS),
  };
}

// ---------------------------------------------------------------- multiscales (§9.5)

const AXIS_TYPES: Record<string, string> = { x: "space", y: "space", z: "space", t: "time", c: "channel" };

export function levelPath(p: unknown): p is string {
  return typeof p === "string" && p !== "" && p.split("/").every((s) => s !== "" && s !== "." && s !== "..");
}

function validAxes(names: string[]): boolean {
  if (names.some((a) => a === "") || new Set(names).size !== names.length) return false;
  const kinds = names.map((a) => (has(AXIS_TYPES, a) ? AXIS_TYPES[a] : "custom"));
  let i = 0;
  if (i < kinds.length && kinds[i] === "time") i++;
  if (i < kinds.length && (kinds[i] === "channel" || kinds[i] === "custom")) i++;
  const rest = kinds.slice(i);
  return rest.length >= 2 && rest.length <= 3 && rest.every((k) => k === "space");
}

function unitOf(u: unknown): string | null {
  if (typeof u !== "string") return null;
  if (UNIT_NAMES.has(u)) return u;
  return has(UNITS, u) ? UNITS[u] : null;
}

function omeMultiscale(
  name: string | null, axes: string[], units: (string | null)[], paths: string[], scales: number[][],
  translations: number[][] | null,
): Json {
  const ms: { [k: string]: Json } = {};
  if (name !== null) ms.name = name;
  ms.axes = axes.map((a, i) => ({
    name: a,
    ...(has(AXIS_TYPES, a) ? { type: AXIS_TYPES[a] } : {}),
    ...(units[i] !== null ? { unit: units[i] } : {}),
  }));
  ms.datasets = paths.map((p, i) => ({
    path: p,
    coordinateTransformations: [
      { type: "scale", scale: scales[i] } as Json,
      ...(translations !== null ? [{ type: "translation", translation: translations[i] } as Json] : []),
    ],
  }));
  return { version: "0.5", multiscales: [ms] };
}

function numbers(v: unknown, n: number, positive = false): number[] | undefined {
  if (!Array.isArray(v) || v.length !== n || !v.every(isNumber)) return undefined;
  if (positive && !v.every((x) => x > 0)) return undefined;
  return v as number[];
}

function strings(v: unknown, n: number): v is string[] {
  return Array.isArray(v) && v.length === n && v.every((x) => typeof x === "string");
}

type Found = [Json, string[], string[]]; // ome, level array paths, axis names

function cosem(g: string, attrs: { [k: string]: Json }, ndim: Map<string, number>, docs: Map<string, { [k: string]: Json }>): Found | undefined {
  const ms = attrs.multiscales;
  if (!Array.isArray(ms) || ms.length === 0 || !isObject(ms[0])) return undefined;
  const m = ms[0];
  const ds = m.datasets;
  if (!Array.isArray(ds) || ds.length === 0 || !ds.every(isObject)) return undefined;
  const levels: [string, string[], number[], number[], (string | null)[]][] = [];
  for (const d of ds as { [k: string]: Json }[]) {
    const p = d.path;
    if (!levelPath(p) || !ndim.has(join(g, p))) return undefined;
    const n = ndim.get(join(g, p))!;
    const t = has(d, "transform") ? d.transform : docs.get(join(g, p))!.transform;
    if (!isObject(t)) return undefined;
    let axes = t.axes;
    let scale = numbers(t.scale, n, true);
    let translate = has(t, "translate") ? numbers(t.translate, n) : new Array(n).fill(0);
    let units: (string | null)[] | Json = has(t, "units") ? t.units : new Array(n).fill(null);
    const order = has(t, "order") ? t.order : "C";
    if (!strings(axes, n) || scale === undefined || translate === undefined || (order !== "C" && order !== "F")) {
      return undefined;
    }
    if (has(t, "units") && !strings(units, n)) return undefined;
    let ax = axes as string[];
    let un = units as (string | null)[];
    if (order === "C") {
      ax = ax.slice().reverse();
      scale = scale.slice().reverse();
      translate = translate.slice().reverse();
      un = un.slice().reverse();
    }
    axes = ax;
    units = un;
    levels.push([p, ax, scale, translate, un]);
  }
  const key = (x: unknown) => JSON.stringify(x);
  if (levels.some((lv) => key(lv[1]) !== key(levels[0][1]) || key(lv[4]) !== key(levels[0][4]))) return undefined;
  const axes = levels[0][1];
  if (!validAxes(axes)) return undefined;
  const name = typeof m.name === "string" ? m.name : null;
  return [
    omeMultiscale(name, axes, levels[0][4].map(unitOf), levels.map((lv) => lv[0]), levels.map((lv) => lv[2]),
      levels.map((lv) => lv[3])),
    levels.map((lv) => join(g, lv[0])),
    axes,
  ];
}

function n5Viewer(g: string, attrs: { [k: string]: Json }, ndim: Map<string, number>, docs: Map<string, { [k: string]: Json }>): Found | undefined {
  const s0 = join(g, "s0");
  if (!ndim.has(s0)) return undefined;
  const n = ndim.get(s0)!;
  let levels: string[];
  let factors: number[][];
  if (has(attrs, "scales")) {
    const scales = attrs.scales;
    if (!Array.isArray(scales) || scales.length === 0) return undefined;
    const fs = scales.map((f) => numbers(f, n, true));
    if (fs.some((f) => f === undefined)) return undefined;
    factors = fs as number[][];
    levels = factors.map((_, i) => join(g, `s${i}`));
    if (levels.some((lv) => ndim.get(lv) !== n)) return undefined;
  } else {
    if (!ndim.has(join(g, "s1"))) return undefined;
    levels = [];
    while (ndim.has(join(g, `s${levels.length}`))) levels.push(join(g, `s${levels.length}`));
    factors = [];
    for (const [i, lv] of levels.entries()) {
      if (ndim.get(lv) !== n) return undefined;
      const doc = docs.get(lv)!;
      if (i === 0 && !has(doc, "downsamplingFactors")) {
        factors.push(new Array(n).fill(1));
        continue;
      }
      const f = numbers(doc.downsamplingFactors, n, true);
      if (f === undefined) return undefined;
      factors.push(f);
    }
  }
  const resDoc = has(attrs, "pixelResolution") ? attrs : has(docs.get(s0)!, "pixelResolution") ? docs.get(s0)! : undefined;
  let unit: string | null = null;
  let r: number[] | undefined;
  if (resDoc === undefined) {
    r = new Array(n).fill(1);
  } else {
    const pr = resDoc.pixelResolution;
    if (isObject(pr)) {
      r = numbers(pr.dimensions, n, true);
      if (has(pr, "unit")) {
        if (typeof pr.unit !== "string") return undefined;
        unit = pr.unit;
      }
    } else {
      r = numbers(pr, n, true);
    }
    if (r === undefined) return undefined;
  }
  let axes: string[];
  if (has(attrs, "axes")) {
    if (!strings(attrs.axes, n)) return undefined;
    axes = attrs.axes as string[];
  } else if (n === 2 || n === 3) {
    axes = ["x", "y", "z"].slice(0, n);
  } else {
    return undefined;
  }
  if (!validAxes(axes)) return undefined;
  const u = unitOf(unit);
  const units = axes.map((a) => (AXIS_TYPES[a] === "space" && has(AXIS_TYPES, a) ? u : null));
  const scales = factors.map((f) => f.map((x, j) => r![j] * x));
  if (scales.some((s) => s.some((v) => !Number.isFinite(v)))) reject("a scale is not finite");
  const paths = levels.map((lv) => (g === "" ? lv : lv.slice(g.length + 1)));
  return [omeMultiscale(null, axes, units, paths, scales, null), levels, axes];
}

// ---------------------------------------------------------------- the profile

export async function virtualizeN5(store: Store): Promise<StoreResult & { summary: object }> {
  const objects = store.objects;
  const candidates: string[] = [];
  for (const key of objects.keys()) {
    if (key === "attributes.json") candidates.push("");
    else if (key.endsWith("/attributes.json")) candidates.push(key.slice(0, -"/attributes.json".length));
  }
  // §9.1: classify from the root down; a candidate's kind needs its document.
  const docs = new Map<string, { [k: string]: Json }>();
  const datasets = new Set<string>();
  const groupPaths: string[] = [];
  for (const path of byDepth(candidates)) {
    if (insideArray(path, datasets)) continue;
    const doc = await readDocument(store, join(path, "attributes.json"));
    if (!isObject(doc)) reject(`${join(path, "attributes.json")} is not a JSON object`);
    docs.set(path, doc as { [k: string]: Json });
    if (has(doc as object, "dimensions")) datasets.add(path);
    else groupPaths.push(path);
  }
  const implicit = implicitGroups(docs.keys());

  const arrays = new Map<string, ArrayDoc>();
  for (const path of [...datasets].sort()) arrays.set(path, dataset(path || "/", docs.get(path)!));
  const ndim = new Map([...arrays].map(([p, a]) => [p, a.shape.length]));

  const groups = new Map<string, { [k: string]: Json }>();
  for (const path of groupPaths) {
    groups.set(path, { zarr_format: 3, node_type: "group", attributes: without(docs.get(path)!, ["n5"]) });
  }
  const images: Json[] = [];
  const named = new Map<string, string[]>();
  const order = [...groups.keys()].sort(compareKeys);
  for (const g of order) {
    const attrs = docs.get(g)!;
    if (has(attrs, "ome")) continue;
    let found = cosem(g, attrs, ndim, docs);
    let convention = "cosem";
    if (found === undefined) {
      found = n5Viewer(g, attrs, ndim, docs);
      convention = "n5-viewer";
    }
    if (found === undefined) continue;
    const [ome, levels, axes] = found;
    (groups.get(g)!.attributes as { [k: string]: Json }).ome = ome;
    images.push({ path: g, convention });
    for (const lv of levels) if (!named.has(lv)) named.set(lv, axes.slice());
  }
  for (const [path, names] of named) arrays.get(path)!.dimension_names = names;

  const out = new Map<string, Json>();
  for (const path of implicit) out.set(docKey(path), GROUP_IMPLICIT());
  for (const [path, doc] of groups) out.set(docKey(path), doc);
  for (const [path, doc] of arrays) out.set(docKey(path), doc as unknown as Json);

  const tests = new Map<string, (rest: string) => boolean>();
  for (const [path, a] of arrays) {
    const g = grid(a.shape, a.chunk_grid.configuration.chunk_shape);
    tests.set(path, (rest) => {
      const parts = rest.split("/");
      return parts.length === g.length && parts.every((s, i) => canonicalIndex(s, g[i]));
    });
  }
  const all = findChunks(objects, tests);
  const chunks = all.filter(([, n]) => n > 0);
  return {
    docs: out,
    chunks,
    summary: {
      groups: groups.size + implicit.size, arrays: arrays.size, chunks: chunks.length,
      emptyChunks: all.length - chunks.length, objects: objects.size, images, listingRequests: store.requests,
    },
  };
}
