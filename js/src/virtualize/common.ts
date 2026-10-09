// What every profile of spec/virtualize.md shares: reading the input through range
// reads, rejecting it, and sizing reference payloads.

import type { Range, Source } from "../protobuf.ts";

/** The input is rejected for a reason no single profile owns (§1.2). */
export class ImageError extends Error {}

/** Reads `length` bytes at `offset`; must return exactly that many. */
export type ByteReader = (offset: number, length: number) => Promise<Uint8Array>;

/** Caches reads in aligned blocks, so nearby small reads share a request. */
export function blockReader(
  read: ByteReader,
  fileSize: number,
  blockSize = 1 << 16,
): ByteReader {
  const blocks = new Map<number, Promise<Uint8Array>>();
  const block = (i: number) => {
    let b = blocks.get(i);
    if (b === undefined) {
      const start = i * blockSize;
      b = read(start, Math.min(blockSize, fileSize - start));
      blocks.set(i, b);
    }
    return b;
  };
  return async (offset, length) => {
    if (offset < 0 || offset + length > fileSize) {
      throw new ImageError(`read of [${offset}, ${offset + length}) outside the ${fileSize}-byte file`);
    }
    const out = new Uint8Array(length);
    const first = Math.floor(offset / blockSize);
    const last = Math.floor((offset + Math.max(length, 1) - 1) / blockSize);
    const parts = await Promise.all(
      Array.from({ length: last - first + 1 }, (_, k) => block(first + k)),
    );
    for (const [k, data] of parts.entries()) {
      const start = (first + k) * blockSize;
      const a = Math.max(offset, start);
      const b = Math.min(offset + length, start + data.length);
      if (a < b) out.set(data.subarray(a - start, b - start), a - offset);
    }
    return out;
  };
}

const BATCH = 256; // scattered small reads in flight at once

/** Runs `f` over `items` in parallel batches of `size`, keeping the order of the results. */
export async function batched<T, R>(items: T[], f: (x: T) => Promise<R>, size = BATCH): Promise<R[]> {
  const out: R[] = [];
  for (let k = 0; k < items.length; k += size) out.push(...(await Promise.all(items.slice(k, k + size).map(f))));
  return out;
}

/** Ranges read ahead through `exact`, which later reads inside one of them are served from. */
export class Prefetched {
  private starts: number[] = [];
  private data: Uint8Array[] = [];
  private readonly exact: ByteReader;
  private readonly fileSize: number;
  private readonly batch: number;

  /** `batch`: the reads in flight at once. */
  constructor(exact: ByteReader, fileSize: number, batch = BATCH) {
    this.exact = exact;
    this.fileSize = fileSize;
    this.batch = batch;
  }

  /** Reads `ranges` (clipped to the file) in parallel batches. */
  async fetch(ranges: [number, number][]): Promise<void> {
    const todo = ranges.filter(([o, n]) => o >= 0 && o < this.fileSize && n > 0)
      .map(([o, n]): [number, number] => [o, Math.min(n, this.fileSize - o)]);
    const got = await batched(todo, ([o, n]) => this.exact(o, n), this.batch);
    const all = [...this.starts.map((o, k): [number, Uint8Array] => [o, this.data[k]]),
      ...todo.map(([o], k): [number, Uint8Array] => [o, got[k]])].sort((a, b) => a[0] - b[0]);
    this.starts = all.map(([o]) => o);
    this.data = all.map(([, d]) => d);
  }

  /** The fetched range holding [o, o + n) whole, as [start, length], or undefined. */
  private find(o: number, n: number): number | undefined {
    let lo = 0, hi = this.starts.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.starts[mid] <= o) lo = mid + 1;
      else hi = mid;
    }
    const j = lo - 1;
    return j >= 0 && o + n <= this.starts[j] + this.data[j].length ? j : undefined;
  }

  covers(o: number, n: number): boolean {
    return this.find(o, n) !== undefined;
  }

  /** A reader serving from the fetched ranges, else through `exact`. */
  readonly read: ByteReader = async (o, n) => {
    const j = this.find(o, n);
    if (j === undefined) return this.exact(o, n);
    return this.data[j].subarray(o - this.starts[j], o - this.starts[j] + n);
  };
}

// ---- reference payloads (§1.2)

/** The largest reference payload a vzip entry can carry. */
export const MAX_PAYLOAD = 65519;

function varintSize(v: number): number {
  let n = 1;
  while (v >= 128) {
    v = Math.floor(v / 128);
    n++;
  }
  return n;
}

/** A range of an output: [offset, length] of source 0, [source, offset,
 * length] of any source, or literal bytes. */
export type Part = [number, number] | [number, number, number] | Uint8Array;

function rangeSize(r: Part): number {
  if (r instanceof Uint8Array) return 1 + varintSize(r.length) + r.length;
  const [source, offset, length] = r.length === 3 ? r : [0, ...r];
  return [source, offset, length].reduce((n, v) => n + (v ? 1 + varintSize(v) : 0), 0);
}

/** The encoded size of a reference to `ranges` (§1.2). */
export function payloadSize(ranges: Part[]): number {
  if (ranges.length === 1) return rangeSize(ranges[0]);
  return ranges.reduce((n, range) => {
    const r = rangeSize(range);
    return n + 1 + varintSize(r) + r;
  }, 0);
}

/** The data sources of a file input's output (§1.2): byte strings shared by
 * many references, numbered from 1 (after the url source 0) in order of
 * first use. */
export class DataSources {
  readonly sources: Uint8Array[] = [];
  private readonly index = new Map<string, number>();

  /** A range of all of `value`, adding it as a source the first time it is used. */
  range(value: Uint8Array): [number, number, number] {
    const key = Array.from(value, (b) => String.fromCharCode(b)).join("");
    let i = this.index.get(key);
    if (i === undefined) {
      this.sources.push(value.slice());
      i = this.sources.length;
      this.index.set(key, i);
    }
    return [i, 0, value.length];
  }

  /** The output's source table: `url`, then the data sources. */
  table(url: string): Source[] {
    return [{ url }, ...this.sources.map((data) => ({ data }))];
  }
}

/** A Part as an archive Range. */
export function toRange(p: Part): Range {
  if (p instanceof Uint8Array) return { data: p };
  const [source, offset, length] = p.length === 3 ? p : [0, ...p];
  return { source, offset: BigInt(offset), length: BigInt(length) };
}

// ---- the virtualization conventions (conventions §2)

/** The key of the virtualization convention's property (conventions §2). */
export const CONVENTION_KEY = "vzip_virtualized";

export type Profile = "tiff" | "ndpi" | "nd2" | "dicom" | "nifti" | "ims" | "n5" | "zarr2" | "ome-zarr" | "safe" | "czi";

/** Each profile's convention: its fixed UUID, its current version and its name in the description. */
/** The revision of spec/virtualize.md this implementation follows. Until the release, every
 * convention is at version 0 and the root property records it (conventions §1, §2). */
export const REVISION = 24;
export const PROFILES: Record<Profile, [uuid: string, version: number, title: string]> = {
  tiff: ["48e9ac4e-1156-4a62-955e-20467d9c2700", 0, "TIFF"],
  ndpi: ["6cac71ef-dbb2-4acd-b60c-00389aa4238a", 0, "NDPI"],
  nd2: ["59612f14-e314-4207-ba00-8f422ba71490", 0, "ND2"],
  dicom: ["acf17198-e5a5-48d3-8187-22ec4bb40ea5", 0, "DICOM"],
  nifti: ["06e5809d-4d54-4b72-afd0-6bf61a7b4c85", 0, "NIfTI"],
  ims: ["5067a535-8261-4b25-a93c-1985ed333bde", 0, "IMS"],
  n5: ["ad5d4c39-c69e-48f7-a3ef-4cc8c607d416", 0, "N5"],
  zarr2: ["8e792619-d671-4687-ab51-752885dd3ee6", 0, "Zarr v2"],
  "ome-zarr": ["b74ea302-65bb-49ae-b81f-f9bb52cd4eed", 0, "OME-Zarr"],
  safe: ["ef81346c-19e8-42ad-93b0-a279ccaf44c1", 0, "Sentinel-2 SAFE"],
  czi: ["7a0733c7-d4be-4482-a64f-6904d9354ea5", 0, "CZI"],
};
export const UUIDS = new Set(Object.values(PROFILES).map(([uuid]) => uuid));

/** The Convention Metadata Object of a profile's convention. */
export function convention(profile: Profile): { [k: string]: string } {
  const [uuid, version, title] = PROFILES[profile];
  // Version 0 has no tag: its URLs name the development branch (conventions §1).
  const tag = `virtualize-${profile}-v${version}`;
  const [ref, blob] = version === 0 ? ["heads/main", "main"] : [`tags/${tag}`, tag];
  return {
    uuid,
    schema_url: `https://raw.githubusercontent.com/d-v-b/vzip/refs/${ref}/spec/virtualize/${profile}/schema.json`,
    spec_url: `https://github.com/d-v-b/vzip/blob/${blob}/spec/virtualize/${profile}.md`,
    name: CONVENTION_KEY,
    description: `The Zarr layout of a ${title} source virtualized by vzip, and the source's metadata`,
  };
}

/** The root's property without its source metadata (conventions §2): the profile, its
 * convention's version, the revision while that version is 0 (`revision`: the one the
 * implementation follows; the frozen reference records its own), and the source URL. */
export function rootProperty(profile: Profile, url: string, revision = REVISION): { [k: string]: unknown } {
  const version = PROFILES[profile][1];
  return { profile, version, ...(version === 0 ? { revision } : {}), source: { url } };
}

/** A node's attributes (conventions §2): `attributes`, the members the target formats
 * define (such as `ome`), and the profile's convention when the node is the
 * root (`url`, the source URL, is given) or has source-specific metadata
 * (`own` is a nonempty object): its metadata object in `zarr_conventions`,
 * and the property `vzip_virtualized`, which holds `own` as its member named
 * after the profile. */
export function declare(
  attributes: { [k: string]: unknown },
  profile: Profile,
  url: string | undefined,
  own?: object,
  revision = REVISION,
): { [k: string]: unknown } {
  const value: { [k: string]: unknown } = url === undefined
    ? {}
    : rootProperty(profile, url, revision);
  if (own !== undefined && Object.keys(own).length > 0) value[profile] = own;
  if (Object.keys(value).length === 0) return { ...attributes };
  return { ...attributes, zarr_conventions: [convention(profile)], [CONVENTION_KEY]: value };
}

/** Sets a member of a JSON object, even one named `__proto__`. */
export function setMember(o: Record<string, unknown>, key: string, value: unknown): void {
  Object.defineProperty(o, key, { value, enumerable: true, writable: true, configurable: true });
}

/** A number as source metadata (spec/conventions.md §6). */
export function jsonNumber(v: number | bigint): number | string {
  const max = BigInt(Number.MAX_SAFE_INTEGER);
  if (typeof v === "bigint") return v <= max && v >= -max ? Number(v) : v.toString();
  if (Number.isNaN(v)) return "NaN";
  if (!Number.isFinite(v)) return v > 0 ? "Infinity" : "-Infinity";
  return v;
}

/** Bytes as text: UTF-8 if valid, else ISO 8859-1 (spec/conventions.md §6). */
export function decodeText(b: Uint8Array): string {
  try {
    return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(b);
  } catch {
    let s = "";
    for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode(...b.subarray(i, i + 0x8000));
    return s;
  }
}

/** A text value (spec/conventions.md §6): UTF-8 if valid, else {latin1: ...},
 * so that the bytes can always be recovered. */
export function textJson(b: Uint8Array): string | { latin1: string } {
  try {
    return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(b);
  } catch {
    return { latin1: latin1(b) };
  }
}

/** Each byte as the code point of the same value (ISO 8859-1, not windows-1252). */
export function latin1(b: Uint8Array): string {
  let s = "";
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode(...b.subarray(i, i + 0x8000));
  return s;
}

/** A fixed-size character field: its bytes up to the first NUL, as a text value. */
export function jsonText(b: Uint8Array): string | { latin1: string } {
  const nul = b.indexOf(0);
  return textJson(nul < 0 ? b : b.subarray(0, nul));
}

/**
 * The JSON text of an output document (spec/virtualize.md §1.1). `JSON.stringify`
 * writes a binary64 value that is an integer beyond 2^53 − 1 and below 10^21
 * as digits padded with zeros (2^64 as `18446744073709552000`), which reads as
 * an integer literal of another value; this writes its exact digits instead.
 */
export function stringifyJson(v: unknown): string {
  const raw = JSON as unknown as { rawJSON(text: string): object };
  return JSON.stringify(v, (_k, x) =>
    typeof x === "number" && Number.isInteger(x) && Math.abs(x) > Number.MAX_SAFE_INTEGER && Math.abs(x) < 1e21
      ? raw.rawJSON(BigInt(x).toString())
      : x,
  );
}

export function base64(b: Uint8Array): string {
  let s = "";
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode(...b.subarray(i, i + 0x8000));
  return btoa(s);
}

// ---- the source metadata node (spec/conventions.md §2)

export const SOURCE_NODE = "vzip_source";
export const MAX_PAYLOAD_BYTES = 65519;
const MAX_CHUNK = 2 ** 24; // the largest chunk of a metadata array of bytes

/** An array of the source metadata node. */
export interface Plan {
  path: string; // under vzip_source
  dataType: string;
  shape: number[];
  chunkShape: number[];
  dims: string[] | undefined; // undefined: no dimension names
  /** coords joined by "/" -> the ranges that hold the chunk, or its bytes. */
  chunks: Map<string, Part[] | Uint8Array>;
  fill?: unknown;
  attributes?: Record<string, unknown>;
  endian?: "little" | "big";
  compressor?: Record<string, unknown>; // a codec after `bytes`
}

/** The entries of a metadata array: its zarr.json and its chunks. */
export function planEntries(prefix: string, p: Plan): (
  { key: string; bytes: Uint8Array; compress?: boolean } | { key: string; ranges: Range[] })[] {
  const oneByte = p.dataType === "uint8" || p.dataType === "int8";
  const doc = {
    zarr_format: 3,
    node_type: "array",
    shape: p.shape,
    data_type: p.dataType,
    chunk_grid: { name: "regular", configuration: { chunk_shape: p.chunkShape } },
    chunk_key_encoding: { name: "default", configuration: { separator: "/" } },
    fill_value: p.fill ?? 0,
    codecs: [oneByte ? { name: "bytes" } : { name: "bytes", configuration: { endian: p.endian ?? "little" } },
      ...(p.compressor ? [p.compressor] : [])],
    ...(p.dims !== undefined ? { dimension_names: p.dims } : {}),
    attributes: p.attributes ?? {},
  };
  const out: ({ key: string; bytes: Uint8Array; compress?: boolean } | { key: string; ranges: Range[] })[] = [
    { key: `${prefix}/${p.path}/zarr.json`, bytes: new TextEncoder().encode(stringifyJson(doc)), compress: true },
  ];
  for (const [coords, c] of p.chunks) {
    const key = coords === "" ? `${prefix}/${p.path}/c` : `${prefix}/${p.path}/c/${coords}`;
    out.push(c instanceof Uint8Array ? { key, bytes: c, compress: true } : { key, ranges: c.map(toRange) });
  }
  return out;
}

/** A run of the source's bytes (length at least 1) as a 1-D uint8 array: its
 * chunk size and chunks, of equal size ceil(length / k), k = ceil(length / 2^24),
 * the last padded with fewer than k zero bytes. */
export function blobChunks(offset: number, length: number): [number, Map<string, Part[]>] {
  const k = Math.ceil(length / MAX_CHUNK);
  const size = Math.ceil(length / k);
  const chunks = new Map<string, Part[]>();
  for (let i = 0; i < k; i++) {
    const n = Math.min(size, length - i * size);
    chunks.set(String(i), n < size ? [[offset + i * size, n], new Uint8Array(size - n)] : [[offset + i * size, n]]);
  }
  return [size, chunks];
}

type Entry = { key: string; bytes: Uint8Array; compress?: boolean } | { key: string; ranges: Range[] };

/** The source metadata node, its groups and its arrays. */
export function emitPlans(plans: Plan[], nodeAttributes: Record<string, unknown>): Entry[] {
  const utf8 = new TextEncoder();
  const group = (attributes: unknown) => utf8.encode(stringifyJson({ zarr_format: 3, node_type: "group", attributes }));
  const out: Entry[] = [{ key: `${SOURCE_NODE}/zarr.json`, bytes: group(nodeAttributes), compress: true }];
  const groups = new Set<string>();
  for (const a of plans) {
    const parts = a.path.split("/");
    for (let k = 1; k < parts.length; k++) groups.add(parts.slice(0, k).join("/"));
  }
  for (const g of [...groups].sort()) out.push({ key: `${SOURCE_NODE}/${g}/zarr.json`, bytes: group({}), compress: true });
  for (const a of plans) out.push(...planEntries(SOURCE_NODE, a));
  return out;
}

/** The C-order array of `shape`, of `item`-byte elements contiguous at
 * `offset`, cut as spec/conventions.md §7 cuts contiguous values: its chunk
 * shape, and its chunks (coords joined by "/" -> the ranges that hold it). The
 * cut axis `a` is the first whose index holds at most 2^24 bytes (`s`); along
 * it, k = ceil(n / floor(2^24 / s)) chunks of c = ceil(n / k) indices, the
 * grid's ceil(n / c), the last padded with zero bytes. `large` lists the
 * chunks whose padding does not fit in a payload, to be copied. */
const MAX_PADDING = 2 ** 16 - 2 ** 10; // the most zero bytes an edge chunk's reference holds
const SMALL_PADDING = 2 ** 10; // edge padding small enough to take the fewest chunks
const MAX_TOTAL_PADDING = 2 ** 20; // the most zero bytes all edge chunks of a cut hold

export function planGrid(
  offset: number, shape: number[], item: number, deepen = true, limit = MAX_CHUNK,
): [number[], Map<string, Part[]>, string[]] {
  if (item > limit) throw new Error(`an element of more than ${limit} bytes`);
  const chunks = new Map<string, Part[]>();
  const large: string[] = [];
  if (shape.includes(0)) return [shape.map((v) => Math.max(1, v)), chunks, large];
  if (shape.length === 0) return [[], new Map([["", [[offset, item]]]]), large];
  let a = 0, slab = shape.slice(1).reduce((p, v) => p * v, item);
  while (slab > limit) slab /= shape[++a];
  let n: number, k: number, c: number;
  for (;;) {
    n = shape[a];
    k = Math.ceil(n / Math.floor(limit / slab));
    c = Math.ceil(n / k);
    let bestPad = Infinity, bestC = c; // the first count padding at most SMALL_PADDING, else the least
    for (let q = k; q <= Math.min(2 * k, n); q++) {
      const cq = Math.ceil(n / q);
      const pad = (Math.ceil(n / cq) * cq - n) * slab;
      if (pad < bestPad) [bestPad, bestC] = [pad, cq];
      if (pad <= SMALL_PADDING) break;
    }
    const outer = shape.slice(0, a).reduce((p, v) => p * v, 1);
    const total = shape.reduce((p, v) => p * v, item);
    // All edge chunks' padding: at most 1 MiB, or 1/64 of the values when they are larger.
    const fits = bestPad <= MAX_PADDING && bestPad * outer <= Math.max(MAX_TOTAL_PADDING, Math.floor(total / 64));
    if (fits) c = bestC;
    if (fits || !deepen || a === shape.length - 1) break;
    slab /= shape[++a];
  }
  const tail = "/0".repeat(shape.length - a - 1);
  const outerCount = shape.slice(0, a).reduce((p, v) => p * v, 1);
  for (let base = 0; base < outerCount; base++) {
    const coords: number[] = [];
    for (let j = a - 1, rest = base; j >= 0; j--) {
      coords.unshift(rest % shape[j]);
      rest = Math.floor(rest / shape[j]);
    }
    for (let q = 0; q < Math.ceil(n / c); q++) {
      const m = Math.min(c, n - q * c);
      const parts: Part[] = [[offset + (base * n + q * c) * slab, m * slab]];
      const key = [...coords, q].join("/") + tail;
      if (m < c) {
        parts.push(new Uint8Array((c - m) * slab));
        if (payloadSize(parts) > MAX_PAYLOAD_BYTES) large.push(key);
      }
      chunks.set(key, parts);
    }
  }
  return [[...new Array(a).fill(1), c, ...shape.slice(a + 1)], chunks, large];
}

/** planGrid's chunks, each whose padding does not fit in a payload copied through `read`. */
export async function gridChunks(
  offset: number, shape: number[], item: number, read: ByteReader, limit = MAX_CHUNK,
): Promise<[number[], Map<string, Part[] | Uint8Array>]> {
  const [chunkShape, chunks, large] = planGrid(offset, shape, item, true, limit);
  const out = new Map<string, Part[] | Uint8Array>(chunks);
  for (const key of large) {
    const [[at, n], pad] = chunks.get(key)! as [[number, number], Uint8Array];
    const copy = new Uint8Array(n + pad.length);
    copy.set(await read(at, n));
    out.set(key, copy);
  }
  return [chunkShape, out];
}

/** `rows` values of `rowBytes` bytes each (at most 2^24), contiguous at
 * `offset`, as the chunks of an array along its first axis: planGrid of the
 * bytes [rows, rowBytes], each chunk's coords followed by `rowCoords`. */
export function rowChunks(offset: number, rows: number, rowBytes: number, rowCoords = ""): [number, Map<string, Part[]>] {
  if (rowBytes > MAX_CHUNK) throw new Error("a row of more than 2^24 bytes");
  const [shape, chunks, large] = planGrid(offset, [rows, rowBytes], 1, false);
  if (large.length) throw new Error("an edge chunk whose padding does not fit in a payload");
  return [shape[0], new Map([...chunks].map(([k, v]) => [`${k.split("/")[0]}${rowCoords}`, v]))];
}

const RAGGED_CHUNK = 2 ** 20;

/** A family of byte values [index, offset, length], as spec/conventions.md §7 says. */
export async function familyPlans(
  path: string, members: [number, number, number][], read: ByteReader, count?: number, ragged = false,
): Promise<Plan[]> {
  count ??= members.reduce((m, [i]) => Math.max(m, i), -1) + 1;
  const lengths = new Set(members.map(([, , d]) => d));
  if (lengths.size === 1 && !lengths.has(0) && !ragged) {
    const [length] = lengths;
    const ordered = [...members].sort((x, y) => x[0] - y[0] || x[1] - y[1] || x[2] - y[2]);
    if (ordered.length === count && ordered.every(([i, o], j) => i === j && o === ordered[0][1] + j * length)) {
      // Adjacent in the source, in index order: contiguous values.
      const [chunkShape, chunks] = await gridChunks(ordered[0][1], [count, length], 1, read);
      return [{ path, dataType: "uint8", shape: [count, length], chunkShape, dims: ["index", "byte"], chunks }];
    }
    if (length <= MAX_CHUNK) {
      return [{
        path, dataType: "uint8", shape: [count, length], chunkShape: [1, length], dims: ["index", "byte"],
        chunks: new Map(members.map(([i, o, d]) => [`${i}/0`, [[o, d]] as Part[]])),
      }];
    }
  }
  const byIndex = new Map<number, [number, number]>();
  for (const [i, o, d] of members) if (!byIndex.has(i)) byIndex.set(i, [o, d]);
  const starts: number[] = [];
  const pieces: [number, number, number][] = [];
  let total = 0;
  for (let i = 0; i < count; i++) {
    starts.push(total);
    const m = byIndex.get(i);
    if (m !== undefined) {
      pieces.push([total, m[0], m[1]]);
      total += m[1];
    }
  }
  starts.push(total);
  const offsets = new Uint8Array(8 * (count + 1));
  starts.forEach((x, i) => new DataView(offsets.buffer).setBigInt64(8 * i, BigInt(x), true));
  // The offsets are copied, and cut as contiguous values (spec/conventions.md §7).
  const [offsetShape, cut] = await gridChunks(0, [count + 1], 8, async (o, n) => offsets.slice(o, o + n));
  const offsetChunks = new Map<string, Uint8Array>();
  for (const [k, v] of cut) {
    if (v instanceof Uint8Array) {
      offsetChunks.set(k, v);
      continue;
    }
    const parts = v.map((r) => (r instanceof Uint8Array ? r : offsets.subarray(r[0], r[0] + r[1])));
    const chunk = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
    let at = 0;
    for (const p of parts) { chunk.set(p, at); at += p.length; }
    offsetChunks.set(k, chunk);
  }
  const plans: Plan[] = [{
    path: `${path}/offsets`, dataType: "int64", shape: [count + 1], chunkShape: offsetShape, dims: ["index"],
    chunks: offsetChunks,
  }];
  if (total > 0) {
    const sizeC = Math.ceil(total / Math.ceil(total / RAGGED_CHUNK)); // balanced: k = ceil(total / 2^20) chunks
    const dataChunks = new Map<string, Part[] | Uint8Array>();
    let first = 0; // the first piece that may reach the chunk: one sweep over the pieces
    for (let c = 0; c < Math.ceil(total / sizeC); c++) {
      const lo = c * sizeC;
      const hi = Math.min(total, (c + 1) * sizeC);
      while (first < pieces.length && pieces[first][0] + pieces[first][2] <= lo) first++;
      const ranges: Part[] = [];
      for (let p = first; p < pieces.length && pieces[p][0] < hi; p++) {
        const [at, o, d] = pieces[p];
        if (at + d <= lo) continue;
        const start = o + Math.max(lo, at) - at;
        const n = Math.min(hi, at + d) - Math.max(lo, at);
        const last = ranges[ranges.length - 1] as [number, number] | undefined;
        if (last !== undefined && last[0] + last[1] === start) last[1] += n; // adjacent in the source: one range
        else ranges.push([start, n]);
      }
      if (hi - lo < sizeC) ranges.push(new Uint8Array(sizeC - (hi - lo)));
      if (payloadSize(ranges) > MAX_PAYLOAD_BYTES) {
        const parts = await Promise.all(ranges.map((r) => (r instanceof Uint8Array ? r : read(r[0], r[1]))));
        const copy = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
        let at = 0;
        for (const p of parts) { copy.set(p, at); at += p.length; }
        dataChunks.set(String(c), copy);
      } else {
        dataChunks.set(String(c), ranges);
      }
    }
    plans.push({ path: `${path}/data`, dataType: "uint8", shape: [total], chunkShape: [sizeC], dims: ["byte"], chunks: dataChunks });
  }
  return plans;
}
