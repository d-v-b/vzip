// The Zarr v2 profile (spec/virtualize.md, §10): a Zarr v2 hierarchy as a
// Zarr v3 one, each chunk a whole-object reference under its own key.

import type { Profile } from "../common.ts";
import { bloscCodec } from "../n5/virtualize.ts";
import {
  asInt,
  byDepth,
  canonicalIndex,
  compareKeys,
  declareNodes,
  docKey,
  findChunks,
  GROUP_IMPLICIT,
  grid,
  has,
  implicitGroups,
  insideArray,
  isNumber,
  isObject,
  isRawInteger,
  join,
  type Json,
  keepObjects,
  PathTrie,
  num,
  prefetchDocuments,
  readDocument,
  sourceMetadata,
  type Store,
  show,
  type StoreResult,
  without,
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

// The members of .zarray that §3 reads; the others are kept in M (spec/virtualize/zarr2.md §4).
const ZARRAY_READ = ["zarr_format", "shape", "chunks", "dtype", "order", "compressor", "filters", "fill_value",
  "dimension_separator"];
const NEG_ZERO: Record<number, string> = { 2: "0x8000", 4: "0x80000000", 8: "0x8000000000000000" };
// The levels a compressor may have (zlib's -1, its default, is level 6, which the Zarr v3
// codecs take), and the levels numcodecs uses when the member is absent.
const LEVELS: Record<string, [number, number, number]> = { zlib: [-1, 9, 1], gzip: [-1, 9, 1], zstd: [-131072, 22, 0] };

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
    if (isNumber(v) && Math.abs(num(v)) <= FLOAT_MAX[b]) {
      // -0.0, which JSON writers do not all keep (JSON.stringify writes 0).
      return Object.is(num(v), -0) ? NEG_ZERO[b] : num(v);
    }
  }
  return reject(`fill_value ${show(v).slice(0, 40)} is not a ${dt}`);
}

/** The level of a zlib, gzip or zstd compressor (spec/virtualize/zarr2.md §3.1). */
function level(id: string, c: { [k: string]: Json }): number {
  const [lo, hi, dflt] = LEVELS[id];
  if (!has(c, "level")) return dflt;
  const v = asInt(c.level, lo, hi);
  if (v === undefined) return reject(`${id} level ${show(c.level).slice(0, 20)} is not an integer from ${lo} to ${hi}`);
  return v === -1 && id !== "zstd" ? 6 : v;
}

/** The compressor's codec, and its members the codec does not carry. */
function compressor(c: Json, b: number): [Json | undefined, { [k: string]: Json }] {
  if (c === null) return [undefined, {}];
  if (!isObject(c) || typeof c.id !== "string") reject("compressor is not null or an object with a string id");
  const cc = c as { [k: string]: Json };
  const id = cc.id as string;
  let codec: Json;
  let carried: string[];
  if (id === "zlib" || id === "gzip") {
    codec = { name: id, configuration: { level: level(id, cc) } };
    carried = ["id", "level"];
  } else if (id === "zstd") {
    const checksum = has(cc, "checksum") ? cc.checksum : false;
    if (typeof checksum !== "boolean") reject(`zstd checksum ${show(checksum).slice(0, 20)} is not a boolean`);
    codec = { name: "zstd", configuration: { level: level(id, cc), checksum } };
    carried = ["id", "level", "checksum"];
  } else if (id === "blosc") {
    codec = bloscCodec(cc, b, SHUFFLES, reject);
    carried = ["id", "cname", "clevel", "shuffle", "blocksize"];
    if (asInt(cc.shuffle) === -1) carried = carried.filter((k) => k !== "shuffle"); // -1 (automatic) is resolved: keep it
  } else {
    return reject(`compressor ${JSON.stringify(id)} is not supported`);
  }
  return [codec, without(cc, carried)];
}

export interface ArrayDoc {
  [k: string]: Json;
  shape: number[];
  chunk_grid: { name: string; configuration: { chunk_shape: number[] } };
  attributes: { [k: string]: Json };
}

const get = (o: { [k: string]: Json }, k: string): Json => (has(o, k) ? o[k] : null);

/** An array's zarr.json, its dimension separator, and the members of its .zarray that
 * the zarr.json does not reproduce (spec/virtualize/zarr2.md §3, §4). */
function array(path: string, z: Json, attrs: { [k: string]: Json }): [ArrayDoc, string, { [k: string]: Json }] {
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
  const [c, extra] = compressor(get(zz, "compressor"), b);
  if (c !== undefined) codecs.push(c);
  const meta = without(zz, ZARRAY_READ);
  const fv = get(zz, "fill_value");
  if (fv === null) meta.fill_value = null;
  // An integer literal beyond 2^53 - 1: F is its binary64 value, so keep the digits.
  // (The reader keeps -0.0 raw too: an integer literal kept raw is beyond 2^53 - 1.)
  else if (dt.startsWith("float") && isRawInteger(fv) && Math.abs(num(fv)) > Number.MAX_SAFE_INTEGER) meta.fill_value = fv;
  if (Object.keys(extra).length) meta.compressor = extra;
  return [{
    zarr_format: 3,
    node_type: "array",
    shape: (shape as unknown[]).map((v) => asInt(v)!),
    data_type: dt,
    chunk_grid: { name: "regular", configuration: { chunk_shape: (chunks as unknown[]).map((v) => asInt(v)!) } },
    chunk_key_encoding: { name: "v2", configuration: { separator: sep as string } },
    fill_value: fill,
    codecs,
    attributes: attrs,
  }, sep as string, meta];
}

/** A Zarr v2 hierarchy read by spec/virtualize/zarr2.md §2–§3. */
export interface Hierarchy {
  arrays: Map<string, ArrayDoc>;
  seps: Map<string, string>;
  /** Each explicit group's attributes. */
  groups: Map<string, { [k: string]: Json }>;
  implicit: Set<string>;
  /** Each node's metadata M (spec/virtualize/zarr2.md §4). */
  meta: Map<string, { [k: string]: Json }>;
  /** The node documents read. */
  documents: Set<string>;
}

function nodesOf(objects: Map<string, number>): Map<string, string> {
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
  const arrayPaths = new PathTrie<true>();
  for (const path of byDepth(candidates.keys())) {
    if (insideArray(path, arrayPaths)) continue;
    nodes.set(path, candidates.get(path)!);
    if (candidates.get(path) === "array") arrayPaths.add(path, true);
  }
  return nodes;
}

/** Starts reading, concurrently, exactly the documents readHierarchy reads and in its
 * order: for each node in path order, its .zarray or .zgroup, then its .zattrs if listed. */
export function prefetch(store: Store) {
  let nodes: Map<string, string>;
  try {
    nodes = nodesOf(store.objects);
  } catch (e) {
    if (e instanceof Zarr2Error) return; // readHierarchy rejects before it reads a document
    throw e;
  }
  const keys: string[] = [];
  for (const path of [...nodes.keys()].sort(compareKeys)) {
    keys.push(join(path, nodes.get(path) === "array" ? ".zarray" : ".zgroup"));
    if (store.objects.has(join(path, ".zattrs"))) keys.push(join(path, ".zattrs"));
  }
  prefetchDocuments(store, keys);
}

/** The nodes of a Zarr v2 store (spec/virtualize/zarr2.md §2), each document read and checked. */
export async function readHierarchy(store: Store): Promise<Hierarchy> {
  const objects = store.objects;
  const nodes = nodesOf(objects);
  const implicit = implicitGroups(nodes.keys());
  prefetch(store);

  const h: Hierarchy = { arrays: new Map(), seps: new Map(), groups: new Map(), implicit, meta: new Map(), documents: new Set() };
  const attributes = async (path: string): Promise<{ [k: string]: Json }> => {
    const key = join(path, ".zattrs");
    if (!objects.has(key)) return {};
    h.documents.add(key);
    const a = await readDocument(store, key);
    if (!isObject(a)) reject(`${key} is not a JSON object`);
    return a as { [k: string]: Json };
  };

  for (const path of [...nodes.keys()].sort(compareKeys)) {
    if (nodes.get(path) === "array") {
      h.documents.add(join(path, ".zarray"));
      const [doc, sep, meta] = array(path || "/", await readDocument(store, join(path, ".zarray")), await attributes(path));
      h.arrays.set(path, doc);
      h.seps.set(path, sep);
      h.meta.set(path, meta);
    } else {
      h.documents.add(join(path, ".zgroup"));
      const g = await readDocument(store, join(path, ".zgroup"));
      if (!isObject(g) || asInt(g.zarr_format) !== 2) reject(`${join(path, ".zgroup")}: zarr_format is not 2`);
      h.groups.set(path, await attributes(path));
      h.meta.set(path, without(g as { [k: string]: Json }, ["zarr_format"]));
    }
  }
  return h;
}

/** Every chunk object of every array (spec/virtualize/zarr2.md §3), sizes 0 included, in key order. */
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

/** The output of a hierarchy: a zarr.json per node (explicit groups with the attributes
 * `groups` gives, arrays as `arrays` gives, their `attributes` the attributes A to keep),
 * an entry per nonempty chunk object, the whole objects `extra` under their own keys, and
 * every other object under `vzip_source/objects/` (§1.4, spec/virtualize/zarr2.md §5).
 * Each node's source metadata is A and its metadata M, under `profile`'s convention, and
 * the groups `omes` names get the member `ome` it gives (conventions §2), and the members
 * `unversioned` gives (spec/virtualize/ome-zarr.md §8). Also returns
 * every chunk object and the number of other objects. */
export function hierarchyOutput(
  store: Store,
  h: Hierarchy,
  groups: Map<string, { [k: string]: Json }>,
  arrays: Map<string, Json>,
  profile: Profile,
  extra: [string, number][] = [],
  omes: Map<string, Json> = new Map(),
  unversioned: Map<string, string[]> = new Map(),
): [StoreResult, [string, number][], number] {
  const docs = new Map<string, Json>();
  for (const path of h.implicit) docs.set(docKey(path), GROUP_IMPLICIT());
  for (const [path, attributes] of groups) {
    docs.set(docKey(path), { zarr_format: 3, node_type: "group", attributes: sourceMetadata(attributes, h.meta.get(path)!, unversioned.get(path)) });
  }
  for (const [path, doc] of arrays) {
    const d = doc as { [k: string]: Json };
    docs.set(docKey(path), { ...d, attributes: sourceMetadata(d.attributes as { [k: string]: Json }, h.meta.get(path)!) });
  }
  declareNodes(docs, profile, store.url, omes);
  const all = chunkObjects(store, h);
  const chunks = [...all, ...extra].filter(([, n]) => n > 0).sort((a, b) => compareKeys(a[0], b[0]));
  const result: StoreResult = { docs, chunks };
  // An empty object in `extra` is not in the hierarchy: its key is listed with the other empty ones.
  // An empty chunk object has no entry either: its key is listed with the empty ones (§5).
  const used = new Set([...h.documents, ...all.filter(([, n]) => n > 0).map(([k]) => k), ...extra.filter(([, n]) => n > 0).map(([k]) => k)]);
  const others = keepObjects(result, store.objects, used, [...h.arrays.keys(), ...h.groups.keys(), ...h.implicit], reject, store.ignored);
  return [result, all, others];
}

export async function virtualizeZarr2(store: Store): Promise<StoreResult & { summary: object }> {
  const h = await readHierarchy(store);
  const [result, all, others] = hierarchyOutput(store, h, h.groups, h.arrays as unknown as Map<string, Json>, "zarr2");
  const chunks = all.filter(([, n]) => n > 0).length;
  return {
    ...result,
    summary: {
      groups: h.groups.size + h.implicit.size, arrays: h.arrays.size, chunks,
      emptyChunks: all.length - chunks, objects: store.objects.size, otherObjects: others, listingRequests: store.requests,
    },
  };
}
