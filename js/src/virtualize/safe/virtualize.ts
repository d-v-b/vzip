// The SAFE profile (spec/virtualize/safe/profile.md, §12): the output of a Sentinel-2 product, in
// the directory form (a store input) or the zip form (a file input).

import {
  base64, type ByteReader, blobChunks, CONVENTION_KEY, convention, declare, MAX_PAYLOAD, type Part, payloadSize,
  PROFILES, rootProperty, SOURCE_NODE, textJson, toRange, stringifyJson,
} from "../common.ts";
import { compareKeys, objectKey, objectUrl, type Store } from "../store.ts";
import type { ArchiveDesc, EntryDesc } from "../../writer.ts";
import { chunkParts, type Codestream, grid, readBand, tailLength } from "./jp2.ts";
import { bandMetadata, chooseTexts, groupAttributes, rootGeozarr, rootMetadata } from "./metadata.ts";
import { bandName, folders, type Image, isXml, readProduct, resolution, SafeError } from "./product.ts";
import { centralDirectory, type Entry, inflate, locate, MAX_DEFLATED, MAX_INFLATED, productEntries } from "./zipdir.ts";

export { SafeError };

const PROFILE = "safe";
const WORKERS = 16;
/** The browser implementation's limit on a zip file's entries (§14), as on a store's listing. */
const MAX_ENTRIES = 100000;

const reject = (m: string): never => {
  throw new SafeError(m);
};

/** `f` over `items`, at most `n` at a time, results in order. */
async function pool<T, R>(items: T[], n: number, f: (x: T) => Promise<R>): Promise<R[]> {
  const out: R[] = new Array(items.length);
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
    while (next < items.length) {
      const i = next++;
      out[i] = await f(items[i]);
    }
  }));
  return out;
}

// ---------------------------------------------------------------- the product's objects (§12.2, §12.4)

/** A source of a range: an object's key (its own url source) or 0 (the zip file). */
type Src = string | 0;

interface Objects {
  form: "store" | "zip";
  sizes: Map<string, number>;
  ignored: string[];
  /** Keys ending in `/`: a zip file's directory entries, a listing's empty `d/` objects. */
  directories: string[];
  listingRequests: number;
  reads: [number, number];
  whole(key: string): Promise<Uint8Array>;
  /** [read(offset, length) of a source, the object's offset in it], for range reads. */
  raw(key: string): [(o: number, n: number) => Promise<Uint8Array>, number];
  where(key: string): [Src, number];
  copied(key: string): Promise<Uint8Array | undefined>;
}

/** `f`, remembering each key's result; `peek(key)` is the remembered one, if any. */
function cached(f: (key: string) => Promise<Uint8Array>) {
  const memo = new Map<string, Promise<Uint8Array>>();
  const get = (key: string) => {
    let p = memo.get(key);
    if (p === undefined) memo.set(key, (p = f(key)));
    return p;
  };
  return Object.assign(get, { peek: (key: string) => memo.get(key) });
}

function storeObjects(store: Store): Objects {
  const reads: [number, number] = [0, 0];
  if (store.readRange === undefined) throw new Error("the store has no range reads");
  return {
    form: "store", sizes: new Map(store.objects), ignored: [...(store.ignored ?? [])],
    directories: [...(store.folders ?? [])], listingRequests: store.requests, reads,
    whole: cached(async (key) => {
      reads[0]++;
      reads[1] += store.objects.get(key)!;
      return store.read(key);
    }),
    raw: (key) => [async (o, n) => {
      reads[0]++;
      reads[1] += n;
      return store.readRange!(key, o, n);
    }, 0],
    where: (key) => [key, 0],
    copied: async () => undefined,
  };
}

async function zipObjects(read: ByteReader, size: number, exact: ByteReader): Promise<Objects> {
  const d = await centralDirectory(read, size);
  if (d.entries.length > MAX_ENTRIES) throw new Error(`the zip file has more than ${MAX_ENTRIES} entries`);
  const [, entries, ignored, directories] = productEntries(d);
  const deflated = new Set<string>();
  let inflatedTotal = 0;
  for (const [k, e] of entries) {
    if (!e.us) continue;
    if (e.method === 0) {
      if (e.cs !== e.us) reject(`the stored zip entry ${k.slice(0, 200)} has a compressed size other than its size`);
    } else if (e.method === 8) {
      if (!isXml(k)) reject(`the zip entry ${k.slice(0, 200)} is compressed: only XML documents may be`);
      if (e.us > MAX_DEFLATED) reject(`the deflated XML document ${k.slice(0, 200)} is larger than 2^26 bytes`);
      deflated.add(k);
      inflatedTotal += e.us;
    } else {
      reject(`the zip entry ${k.slice(0, 200)} has the compression method ${e.method}`);
    }
  }
  if (inflatedTotal > MAX_INFLATED) reject("the deflated XML documents are larger than 2^27 bytes together");
  await locate(read, d, entries);
  const e = (k: string) => entries.get(k) as Entry;
  // A deflated entry's bytes, inflated when they are needed, one entry at a time.
  const inflated = async (k: string) => inflate(await read(e(k).ds, e(k).cs), e(k));
  const whole = cached(async (key) => (deflated.has(key) ? inflated(key) : read(e(key).ds, e(key).us)));
  return {
    form: "zip", sizes: new Map([...entries].map(([k, x]) => [k, x.us])), ignored, directories, listingRequests: 0, reads: [0, 0],
    whole: (key) => whole(key),
    raw: (key) => [exact, e(key).ds],
    where: (key) => [0, e(key).ds],
    copied: async (key) => (deflated.has(key) ? (whole.peek(key) ?? inflated(key)) : undefined),
  };
}

// ---------------------------------------------------------------- the output (§12.8)

/** A range: literal bytes, [source, offset, length], or a shared byte string (a data source). */
type Ref = Uint8Array | [Src, number, number] | { shared: Uint8Array };

const latin1Key = (b: Uint8Array) => {
  let s = "";
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode(...b.subarray(i, i + 0x8000));
  return s;
};

function group(attributes: Record<string, unknown> = {}) {
  return { zarr_format: 3, node_type: "group", attributes };
}

function bytesArray(n: number, size: number) {
  return {
    zarr_format: 3, node_type: "array", shape: [n], data_type: "uint8",
    chunk_grid: { name: "regular", configuration: { chunk_shape: [size] } },
    chunk_key_encoding: { name: "default", configuration: { separator: "/" } },
    fill_value: 0, codecs: [{ name: "bytes" }], dimension_names: ["byte"], attributes: {},
  };
}

interface Band {
  image: Image;
  cs: Codestream;
  resolution: number;
  name: string;
}

async function virtualizeProduct(url: string, objects: Objects): Promise<ArchiveDesc & { summary: object }> {
  const [markers, emptyDirs] = folders(objects.sizes.keys(), objects.directories);
  const sizes = new Map([...objects.sizes].filter(([k]) => !markers.has(k)));
  const product = await readProduct(sizes, (k) => objects.whole(k));
  const streams = await pool(product.images, WORKERS, async (image) => {
    const [raw, base] = objects.raw(image.key);
    return (await readBand(raw, base, sizes.get(image.key)!))[0];
  });
  const bands: Band[] = [];
  const names = new Set<string>();
  for (const [i, image] of product.images.entries()) {
    const cs = streams[i];
    const r = resolution(product.geocoding, cs.width, cs.height, image.key);
    const name = bandName(image.basename, r);
    if (names.has(`${r}/${name}`)) reject(`two band files at ${r} m have the band name ${name}`);
    names.add(`${r}/${name}`);
    bands.push({ image, cs, resolution: r, name });
  }
  const bandKeys = new Set(bands.map((b) => b.image.key));
  const keys = [...sizes.keys()].sort(compareKeys);
  const empty = keys.filter((k) => sizes.get(k) === 0);
  const xmlKeys = keys.filter((k) => sizes.get(k) && !bandKeys.has(k) && isXml(k));
  const others = keys.filter((k) => sizes.get(k) && !bandKeys.has(k) && !isXml(k));
  const ignored = [...objects.ignored].sort(compareKeys);
  const texts = await chooseTexts(new Map(xmlKeys.map((k) => [k, sizes.get(k)!])),
    (batch) => pool(batch, WORKERS, async (k) => textJson(await objects.whole(k))),
    [["empty", empty], ["empty_dirs", emptyDirs], ["ignored", ignored]], compareKeys);
  const inText = new Set(texts.keys());
  const arrays = xmlKeys.filter((k) => !inText.has(k));

  const docs = new Map<string, unknown>();
  const refs = new Map<string, Ref[]>();
  const bytesEntries = new Map<string, Uint8Array>();
  const resolutions = [...new Set(bands.map((b) => b.resolution))].sort((a, b) => a - b);
  const [cmos, geo] = rootGeozarr(product, resolutions);
  docs.set("zarr.json", group({
    zarr_conventions: [convention(PROFILE), ...cmos],
    [CONVENTION_KEY]: { ...rootProperty(PROFILE, url), [PROFILE]: rootMetadata(product) },
    ...geo,
  }));
  for (const r of resolutions) docs.set(`r${r}m/zarr.json`, group(groupAttributes(product.geocoding, r)));

  let chunks = 0, edge = 0, tileParts = 0;
  for (const b of bands) {
    const cs = b.cs, path = `r${b.resolution}m/${b.name}`, three = cs.components === 3;
    const s = bandMetadata(product, b.image.text, b.name, base64(cs.siz));
    docs.set(`${path}/zarr.json`, {
      zarr_format: 3, node_type: "array", shape: three ? [3, cs.height, cs.width] : [cs.height, cs.width],
      data_type: cs.precision <= 8 ? "uint8" : "uint16",
      chunk_grid: { name: "regular", configuration: { chunk_shape: three ? [3, cs.tileH, cs.tileW] : [cs.tileH, cs.tileW] } },
      chunk_key_encoding: { name: "default", configuration: { separator: "/" } },
      fill_value: 0,
      codecs: three
        ? [{ name: "transpose", configuration: { order: [1, 2, 0] } }, { name: "imagecodecs_jpeg2k" }]
        : [{ name: "imagecodecs_jpeg2k" }],
      dimension_names: three ? ["c", "y", "x"] : ["y", "x"],
      attributes: declare({}, PROFILE, undefined, s),
    });
    const [src, base] = objects.where(b.image.key);
    const rest = { shared: cs.rest };
    const [nx] = grid(cs);
    // The empty tiles are measured before they are made: all of a band's, at most its file's size.
    let total = 0;
    for (let t = 0; t < cs.tiles.length; t++) total += tailLength(cs, t);
    if (total > cs.n) {
      reject(`the empty tiles of ${b.image.key.slice(0, 200)}'s chunks would take ${total} bytes, more than the band file's ${cs.n}`);
    }
    for (let t = 0; t < cs.tiles.length; t++) {
      const [head, sot, [o, n], tail, padded] = chunkParts(cs, t);
      refs.set(`${path}/c/${three ? "0/" : ""}${Math.floor(t / nx)}/${t % nx}`,
        [head, rest, sot, [src, base + o, n], padded ? { shared: tail } : tail]);
      chunks++;
      if (padded) edge++;
    }
    tileParts += cs.tiles.length;
  }

  const referenced = (path: string, key: string, start: number, n: number) => {
    const [src, base] = objects.where(key);
    const [size, cut] = blobChunks(base + start, n);
    docs.set(`${path}/zarr.json`, bytesArray(n, size));
    for (const [i, parts] of cut) {
      refs.set(`${path}/c/${i}`, parts.map((p) => (p instanceof Uint8Array ? p : [src, p[0], p[1]] as [Src, number, number])));
    }
  };
  const S: Record<string, unknown> = {};
  if (inText.size) S.xml = Object.fromEntries([...inText].sort(compareKeys).map((k) => [k, texts.get(k)]));
  if (arrays.length) S.xml_arrays = arrays;
  if (empty.length) S.empty = empty;
  if (emptyDirs.length) S.empty_dirs = emptyDirs;
  if (ignored.length) S.ignored = ignored;
  docs.set(`${SOURCE_NODE}/zarr.json`, group(declare({}, PROFILE, undefined, S)));
  if (arrays.length) {
    docs.set(`${SOURCE_NODE}/xml/zarr.json`, group());
    for (const [i, k] of arrays.entries()) {
      const data = await objects.copied(k);
      if (data === undefined) {
        referenced(`${SOURCE_NODE}/xml/${i}`, k, 0, sizes.get(k)!);
        continue;
      }
      const [size, cut] = blobChunks(0, data.length);
      docs.set(`${SOURCE_NODE}/xml/${i}/zarr.json`, bytesArray(data.length, size));
      for (const j of cut.keys()) {
        const chunk = new Uint8Array(size);
        chunk.set(data.subarray(Number(j) * size, (Number(j) + 1) * size));
        bytesEntries.set(`${SOURCE_NODE}/xml/${i}/c/${j}`, chunk);
      }
    }
  }
  docs.set(`${SOURCE_NODE}/jp2/zarr.json`, group());
  for (const r of resolutions) docs.set(`${SOURCE_NODE}/jp2/r${r}m/zarr.json`, group());
  for (const b of bands) referenced(`${SOURCE_NODE}/jp2/r${b.resolution}m/${b.name}`, b.image.key, 0, b.cs.c0);
  for (const k of others) {
    const [src, base] = objects.where(k);
    refs.set(objectKey(k), [[src, base, sizes.get(k)!]]);
  }

  const utf8 = new TextEncoder();
  for (const key of [...docs.keys(), ...refs.keys(), ...bytesEntries.keys()]) {
    if (utf8.encode(key).length > 65535) reject("an output key is longer than 65535 bytes");
    if (key.startsWith("__vz__/")) reject(`output key ${JSON.stringify(key.slice(0, 200))} is in the reserved __vz__/ space`);
  }
  // The source table (§12.8): url sources, then data sources by first use in key order.
  const refKeys = [...refs.keys()].sort(compareKeys);
  let urls: string[];
  const index = new Map<Src, number>();
  if (objects.form === "zip") {
    urls = [url];
    index.set(0, 0);
  } else {
    const objs = [...new Set(refKeys.flatMap((k) =>
      refs.get(k)!.filter((r): r is [Src, number, number] => Array.isArray(r)).map((r) => r[0] as string)))]
      .sort(compareKeys);
    urls = objs.map((o) => objectUrl(url, o));
    objs.forEach((o, i) => index.set(o, i));
  }
  const data = new Map<string, [number, Uint8Array]>();
  const entries: EntryDesc[] = [];
  for (const k of refKeys) {
    const ranges: Part[] = refs.get(k)!.map((r) => {
      if (r instanceof Uint8Array) return r;
      if (Array.isArray(r)) return [index.get(r[0])!, r[1], r[2]];
      const key = latin1Key(r.shared);
      let d = data.get(key);
      if (d === undefined) data.set(key, (d = [urls.length + data.size, r.shared]));
      return [d[0], 0, r.shared.length];
    });
    if (payloadSize(ranges) > MAX_PAYLOAD) reject(`the reference payload of ${k.slice(0, 200)} is over ${MAX_PAYLOAD} bytes`);
    entries.push({ key: k, ranges: ranges.map(toRange) });
  }
  for (const [k, v] of bytesEntries) entries.push({ key: k, bytes: v, compress: true });
  for (const [k, v] of docs) {
    entries.push({ key: k, bytes: utf8.encode(stringifyJson(v)), ...(k.startsWith(SOURCE_NODE + "/") ? { compress: true } : {}) });
  }
  const summary = {
    level: product.level, form: objects.form, groups: resolutions.length, bands: bands.length, chunks, edgeChunks: edge,
    dataSources: data.size, objects: objects.sizes.size, folderMarkers: markers.size, emptyDirs: emptyDirs.length, xmlText: inText.size,
    xmlArrays: arrays.length, otherObjects: others.length, emptyObjects: empty.length, tileParts,
    listingRequests: objects.listingRequests, readRequests: objects.reads[0], readBytes: objects.reads[1],
  };
  // The objects' url sources pin their listed sizes (§1.4); the zip file's is pinned by virtualizeImage (§1.2).
  const sized = new Map(objects.form === "zip" ? [] : [...objects.sizes].map(([k, n]) => [objectUrl(url, k), n]));
  const source = (u: string) => (sized.has(u) ? { url: u, size: BigInt(sized.get(u)!) } : { url: u });
  return { sources: [...urls.map(source), ...[...data.values()].map(([, d]) => ({ data: d }))], entries, summary };
}

/** The directory form (§12): a listed `.SAFE` directory. */
export function virtualizeSafeStore(store: Store): Promise<ArchiveDesc & { summary: object }> {
  return virtualizeProduct(store.url, storeObjects(store));
}

/** The zip form (§12.4): a `.SAFE.zip` file, `read` its (cached) reader, and `exact` one
 * that reads exactly the ranges asked for (the tile-part headers). */
export async function virtualizeSafeZip(
  url: string, read: ByteReader, size: number, exact: ByteReader = read,
): Promise<ArchiveDesc & { summary: object }> {
  const reads: [number, number] = [0, 0];
  const counted: ByteReader = (o, n) => {
    reads[0]++;
    reads[1] += n;
    return exact(o, n);
  };
  const objects = await zipObjects(read, size, counted);
  objects.reads = reads;
  return virtualizeProduct(url, objects);
}

/** A ZIP local file header's signature (§12.1). */
export const isZip = (head: Uint8Array) => head[0] === 0x50 && head[1] === 0x4b && head[2] === 0x03 && head[3] === 0x04;
