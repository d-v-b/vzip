// The Zarr v2 profile (profiles/zarr2.md, §10): a Zarr v2 hierarchy as a
// Zarr v3 one, each chunk a whole-object reference under its own key.

import { bloscCodec } from "../n5/virtualize.ts";
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
} from "../store.ts";

export class Zarr2Error extends Error {}

const reject = (message: string): never => {
  throw new Zarr2Error(message);
};

const TYPES: Record<string, string> = {
  b1: "bool", i1: "int8", u1: "uint8", i2: "int16", u2: "uint16", f2: "float16", i4: "int32", u4: "uint32",
  f4: "float32", i8: "int64", u8: "uint64", f8: "float64",
};
const FLOAT_MAX: Record<number, number> = { 2: 65504, 4: 3.4028234663852886e38, 8: Infinity };
const SHUFFLES = new Map([[0, "noshuffle"], [1, "shuffle"], [2, "bitshuffle"], [-1, "auto"]]);

function dataType(dtype: Json): [string, number, string] {
  const m = typeof dtype === "string" ? dtype.match(/^([<>|])([biuf])([1248])$/) : null;
  if (m === null || !has(TYPES, m[2] + m[3])) reject(`dtype ${show(dtype).slice(0, 60)} is not supported`);
  const [, order, kind, size] = m!;
  const b = Number(size);
  if (b > 1 && order === "|") reject(`dtype ${dtype} has no byte order`);
  return [TYPES[kind + size], b, order];
}

function fillValue(v: Json, dt: string, b: number): Json {
  if (v === null) return dt === "bool" ? false : 0;
  if (dt === "bool") {
    if (typeof v === "boolean") return v;
  } else if (dt.startsWith("int") || dt.startsWith("uint")) {
    const bits = 8 * b;
    const [lo, hi] = dt.startsWith("u") ? [0, 2 ** bits - 1] : [-(2 ** (bits - 1)), 2 ** (bits - 1) - 1];
    const i = asInt(v, lo, hi);
    if (i !== undefined) return i;
  } else {
    if (v === "NaN" || v === "Infinity" || v === "-Infinity") return v;
    if (isNumber(v) && Math.abs(v) <= FLOAT_MAX[b]) return v;
  }
  return reject(`fill_value ${show(v).slice(0, 40)} is not a ${dt}`);
}

function compressor(c: Json, b: number): Json | undefined {
  if (c === null) return undefined;
  if (!isObject(c) || typeof c.id !== "string") reject("compressor is not null or an object with a string id");
  const id = (c as { id: string }).id;
  if (id === "zlib") return { name: "zlib", configuration: { level: 1 } };
  if (id === "gzip") return { name: "gzip", configuration: { level: 1 } };
  if (id === "zstd") return { name: "zstd", configuration: { level: 0, checksum: false } };
  if (id === "blosc") return bloscCodec(c as { [k: string]: Json }, b, SHUFFLES, reject);
  return reject(`compressor ${JSON.stringify(id)} is not supported`);
}

export interface ArrayDoc {
  [k: string]: Json;
  shape: number[];
  chunk_grid: { name: string; configuration: { chunk_shape: number[] } };
  attributes: { [k: string]: Json };
}

const get = (o: { [k: string]: Json }, k: string): Json => (has(o, k) ? o[k] : null);

/** An array's zarr.json and its dimension separator (§10.2). */
function array(path: string, z: Json, attrs: { [k: string]: Json }): [ArrayDoc, string] {
  if (!isObject(z)) reject(`${path}/.zarray is not a JSON object`);
  const zz = z as { [k: string]: Json };
  if (asInt(zz.zarr_format) !== 2) reject(`${path}/.zarray: zarr_format is not 2`);
  const shape = zz.shape;
  const chunks = zz.chunks;
  if (!Array.isArray(shape) || shape.length > 32 || shape.some((s) => asInt(s, 0) === undefined)) {
    reject(`${path}: shape ${show(shape).slice(0, 80)} is not up to 32 sizes`);
  }
  const n = (shape as Json[]).length;
  if (!Array.isArray(chunks) || chunks.length !== n || chunks.some((c) => asInt(c, 1) === undefined)) {
    reject(`${path}: chunks ${show(chunks).slice(0, 80)} is not ${n} chunk sizes`);
  }
  const [dt, b, byteorder] = dataType(get(zz, "dtype"));
  const order = zz.order;
  if (order !== "C" && order !== "F") reject(`${path}: order ${show(order).slice(0, 20)} is not C or F`);
  const filters = get(zz, "filters");
  if (filters !== null && !(Array.isArray(filters) && filters.length === 0)) reject(`${path}: filters are not supported`);
  let sep = get(zz, "dimension_separator");
  if (sep === null) sep = ".";
  if (sep !== "." && sep !== "/") reject(`${path}: dimension_separator ${show(sep).slice(0, 20)} is not . or /`);
  const fill = fillValue(get(zz, "fill_value"), dt, b);
  const codecs: Json[] = [];
  if (order === "F" && n >= 2) {
    codecs.push({ name: "transpose", configuration: { order: Array.from({ length: n }, (_, i) => n - 1 - i) } });
  }
  codecs.push(b === 1 ? { name: "bytes" } : { name: "bytes", configuration: { endian: byteorder === "<" ? "little" : "big" } });
  const c = compressor(get(zz, "compressor"), b);
  if (c !== undefined) codecs.push(c);
  return [{
    zarr_format: 3,
    node_type: "array",
    shape: (shape as number[]).slice(),
    data_type: dt,
    chunk_grid: { name: "regular", configuration: { chunk_shape: (chunks as number[]).slice() } },
    chunk_key_encoding: { name: "v2", configuration: { separator: sep as string } },
    fill_value: fill,
    codecs,
    attributes: attrs,
  }, sep as string];
}

/** A Zarr v2 hierarchy read by §10.1–§10.2. */
export interface Hierarchy {
  arrays: Map<string, ArrayDoc>;
  seps: Map<string, string>;
  /** Each explicit group's attributes. */
  groups: Map<string, { [k: string]: Json }>;
  implicit: Set<string>;
}

/** The nodes of a Zarr v2 store (§10.1), each document read and checked. */
export async function readHierarchy(store: Store): Promise<Hierarchy> {
  const objects = store.objects;
  const candidates = new Map<string, string>();
  for (const key of objects.keys()) {
    const i = key.lastIndexOf("/");
    const head = i < 0 ? "" : key.slice(0, i);
    const name = key.slice(i + 1);
    if (name === ".zarray" || name === ".zgroup") {
      const kind = name === ".zarray" ? "array" : "group";
      if ((candidates.get(head) ?? kind) !== kind) reject(`${head || "/"} has both .zarray and .zgroup`);
      candidates.set(head, kind);
    }
  }
  const nodes = new Map<string, string>();
  const arrayPaths = new Set<string>();
  for (const path of byDepth(candidates.keys())) {
    if (insideArray(path, arrayPaths)) continue;
    nodes.set(path, candidates.get(path)!);
    if (candidates.get(path) === "array") arrayPaths.add(path);
  }
  const implicit = implicitGroups(nodes.keys());

  const attributes = async (path: string): Promise<{ [k: string]: Json }> => {
    const key = join(path, ".zattrs");
    if (!objects.has(key)) return {};
    const a = await readDocument(store, key);
    if (!isObject(a)) reject(`${key} is not a JSON object`);
    return a as { [k: string]: Json };
  };

  const h: Hierarchy = { arrays: new Map(), seps: new Map(), groups: new Map(), implicit };
  for (const path of [...nodes.keys()].sort(compareKeys)) {
    if (nodes.get(path) === "array") {
      const [doc, sep] = array(path || "/", await readDocument(store, join(path, ".zarray")), await attributes(path));
      h.arrays.set(path, doc);
      h.seps.set(path, sep);
    } else {
      const g = await readDocument(store, join(path, ".zgroup"));
      if (!isObject(g) || asInt(g.zarr_format) !== 2) reject(`${join(path, ".zgroup")}: zarr_format is not 2`);
      h.groups.set(path, await attributes(path));
    }
  }
  return h;
}

/** Every chunk object of every array (§10.2), sizes 0 included, in key order. */
export function chunkObjects(store: Store, h: Hierarchy): [string, number][] {
  const tests = new Map<string, (rest: string) => boolean>();
  for (const [path, a] of h.arrays) {
    const g = grid(a.shape, a.chunk_grid.configuration.chunk_shape);
    const sep = h.seps.get(path)!;
    tests.set(path, (rest) => {
      if (g.length === 0) return rest === "0";
      if (sep === "." && rest.includes("/")) return false;
      const parts = rest.split(sep);
      return parts.length === g.length && parts.every((s, i) => canonicalIndex(s, g[i]));
    });
  }
  return findChunks(store.objects, tests);
}

/** The output of a hierarchy: a zarr.json per node and an entry per nonempty
 * chunk object, plus the whole objects `extra` (§1.4). Also returns every chunk object. */
export function hierarchyOutput(
  store: Store,
  h: Hierarchy,
  groups: Map<string, { [k: string]: Json }>,
  arrays: Map<string, Json>,
  extra: [string, number][] = [],
): [StoreResult, [string, number][]] {
  const docs = new Map<string, Json>();
  for (const path of h.implicit) docs.set(docKey(path), GROUP_IMPLICIT());
  for (const [path, attributes] of groups) docs.set(docKey(path), { zarr_format: 3, node_type: "group", attributes });
  for (const [path, doc] of arrays) docs.set(docKey(path), doc);
  const all = chunkObjects(store, h);
  const chunks = [...all, ...extra].filter(([, n]) => n > 0).sort((a, b) => compareKeys(a[0], b[0]));
  return [{ docs, chunks }, all];
}

export async function virtualizeZarr2(store: Store): Promise<StoreResult & { summary: object }> {
  const h = await readHierarchy(store);
  const [result, all] = hierarchyOutput(store, h, h.groups, h.arrays as unknown as Map<string, Json>);
  const chunks = all.filter(([, n]) => n > 0).length;
  return {
    ...result,
    summary: {
      groups: h.groups.size + h.implicit.size, arrays: h.arrays.size, chunks,
      emptyChunks: all.length - chunks, objects: store.objects.size, listingRequests: store.requests,
    },
  };
}
