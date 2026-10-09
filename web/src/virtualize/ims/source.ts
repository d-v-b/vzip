// The HDF5 tree of an Imaris file as the source metadata node of the IMS
// convention (conventions/ims/README.md §5): every group, dataset, attribute,
// link and named datatype, with each dataset's data as an array.

import {
  base64, type ByteReader, declare, decodeText, familyPlans, gridChunks, jsonNumber, jsonText, type Part, type Plan,
  planEntries, planGrid, setMember, SOURCE_NODE, textJson, stringifyJson,
} from "../common.ts";
import {
  address, attributeFull, attributeSharedType, DATASPACE, DATATYPE, type Dataset, EXTERNAL_FILES, FILL_VALUE,
  fillValue, Hdf5, ImsError, LAYOUT, le, LINK_INFO, type Message, parseDataspace, parseSelection, ravel, showCoords,
  SYMBOL_TABLE, u, unravel,
} from "./hdf5.ts";

const MAX_VALUE_BYTES = 2 ** 26; // the most attribute data read per file
const MAX_OBJECTS = 100000; // objects walked per file, besides PER_IMAGE for each Data dataset of the image
const PER_IMAGE = 5; // its channel group, Data, Histogram, Histogram1024, and a time point group (§5.1)
const MAX_ELEMENTS = 2 ** 20; // elements of a dataset whose datatype holds addresses (peak memory: under 1 KB each)
const MAX_DEPTH = 16; // nesting of datatypes
const INLINE = 64; // the most numbers of an attribute held as JSON
const MAX_TEXT = 2 ** 20; // the longest text of an attribute held as JSON
const MAX_RAW = 4096; // the most bytes of an attribute held as base64
const BUDGET = 2 ** 16; // the cost of the entries one document holds (§5.6)
const MAX_HEAP = 2 ** 16; // the most bytes of variable-length elements of an attribute held as JSON
const MAX_COPY = 2 ** 24; // the most bytes of a dataset decoded and copied
const MAX_SELECTIONS = 2 ** 22; // the most bytes of region references' selections read per file
const MAX_CHUNK = 2 ** 24; // conventions §7
const VLEN_REASON = "variable-length data of references or variable-length data";
const RAGGED_CHUNK = 2 ** 20; // conventions §7
const PADDING = ["null-terminated", "null-padded", "space-padded"];
const CHARSETS = ["ascii", "utf-8"];
// IEEE 754 binary16, 32 and 64: [sign location, exponent location, exponent
// size, exponent bias, mantissa location, mantissa size].
const IEEE: Record<number, number[]> = {
  2: [15, 10, 5, 15, 0, 10], 4: [31, 23, 8, 127, 0, 23], 8: [63, 52, 11, 1023, 0, 52],
};
const ZLIB = { name: "zlib", configuration: { level: 1 } };

const reject = (message: string): never => {
  throw new ImsError(message);
};

const bytesOf = (latin1: string) => Uint8Array.from(latin1, (c) => c.charCodeAt(0));
const product = (dims: number[]) => dims.reduce((n, v) => n * v, 1);

type Json = unknown;
type Type = Record<string, any>;

// ---- datatypes (conventions/ims/README.md §5.2)

const choice = (names: string[], i: number) => (i < names.length ? names[i] : i);

function memberName(d: Uint8Array, q: number, version: number): [string, number] {
  const end = d.indexOf(0, q);
  if (end < 0) reject("truncated datatype");
  return [decodeText(d.subarray(q, end)), version < 3 ? q + Math.floor((end - q + 8) / 8) * 8 : end + 1];
}

function bounds(d: Uint8Array, q: number, n: number): void {
  if (q < 0 || q + n > d.length) reject("truncated HDF5 structure");
}

/** The JSON description of the datatype message at `p`, and where it ends. */
export function describeType(d: Uint8Array, p = 0, depth = 0): [Type, number] {
  if (depth > MAX_DEPTH) reject("datatypes nested too deeply");
  const cv = u(d, p, 1);
  const cls = cv & 15, version = cv >> 4;
  if (version < 1 || version > 5) reject(`unsupported datatype message version ${version}`);
  const bits = u(d, p + 1, 3), size = u(d, p + 4, 4);
  let q = p + 8;
  const order = bits & 1 ? "big" : "little";
  if (cls === 0 || cls === 4) {
    const offset = u(d, q, 2), precision = u(d, q + 2, 2);
    const t: Type = { class: cls === 0 ? "integer" : "bitfield", size, order };
    if (cls === 0) t.signed = Boolean(bits & 8);
    if (offset || precision !== 8 * size || bits & 6) {
      Object.assign(t, { offset, precision, padding: [(bits >> 1) & 1, (bits >> 2) & 1] });
    }
    return [t, q + 4];
  }
  if (cls === 1) {
    const offset = u(d, q, 2), precision = u(d, q + 2, 2);
    const eloc = u(d, q + 4, 1), esize = u(d, q + 5, 1), mloc = u(d, q + 6, 1), msize = u(d, q + 7, 1);
    const bias = u(d, q + 8, 4);
    const sign = (bits >> 8) & 0xff, norm = (bits >> 4) & 3;
    const t: Type = { class: "float", size, order: bits & 0x40 ? "vax" : order };
    const ieee = IEEE[size];
    if (bits & 0x4e || norm !== 2 || offset || precision !== 8 * size || ieee === undefined
        || [sign, eloc, esize, bias, mloc, msize].some((v, i) => v !== ieee[i])) {
      t.layout = {
        offset, precision, sign, exponent: [eloc, esize, bias], mantissa: [mloc, msize, norm],
        padding: [(bits >> 1) & 1, (bits >> 2) & 1, (bits >> 3) & 1],
      };
    }
    return [t, q + 12];
  }
  if (cls === 2) return [{ class: "time", size, order, precision: u(d, q, 2) }, q + 2];
  if (cls === 3) {
    return [{ class: "string", size, padding: choice(PADDING, bits & 15), charset: choice(CHARSETS, (bits >> 4) & 15) }, q];
  }
  if (cls === 5) {
    const n = bits & 0xff;
    if (n) bounds(d, q, n);
    return [{ class: "opaque", size, tag: jsonText(d.subarray(q, q + n)) }, q + n];
  }
  if (cls === 6) {
    const members: Type[] = [];
    for (let k = 0; k < (bits & 0xffff); k++) {
      let name: string, offset: number, member: Type;
      [name, q] = memberName(d, q, version);
      if (version >= 3) {
        const w = size ? Math.ceil(size.toString(2).length / 8) : 1;
        offset = u(d, q, w);
        [member, q] = describeType(d, q + w, depth + 1);
      } else if (version === 2) {
        offset = u(d, q, 4);
        [member, q] = describeType(d, q + 4, depth + 1);
      } else {
        offset = u(d, q, 4);
        const rank = u(d, q + 4, 1);
        if (rank > 4) reject("a compound member of more than 4 dimensions");
        const dims = Array.from({ length: rank }, (_, i) => u(d, q + 16 + 4 * i, 4));
        [member, q] = describeType(d, q + 32, depth + 1);
        if (rank) member = { class: "array", size: member.size * product(dims), shape: dims, base: member };
      }
      members.push({ name, offset, type: member });
    }
    return [{ class: "compound", size, members }, q];
  }
  if (cls === 7) return [{ class: "reference", size, kind: bits & 15 }, q];
  if (cls === 8) {
    let base: Type;
    [base, q] = describeType(d, q, depth + 1);
    if (base.class !== "integer") reject("an enumeration of a datatype that is not an integer");
    const names: string[] = [];
    for (let k = 0; k < (bits & 0xffff); k++) {
      let name: string;
      [name, q] = memberName(d, q, version);
      names.push(name);
    }
    const n = base.size;
    if (names.length && n) bounds(d, q, names.length * n);
    const values: Record<string, Json> = {};
    names.forEach((name, i) => {
      if (!Object.hasOwn(values, name)) setMember(values, name, integer(base, d.subarray(q + i * n, q + (i + 1) * n)));
    });
    return [{ class: "enum", size, base, members: values }, q + names.length * n];
  }
  if (cls === 9) {
    let base: Type;
    [base, q] = describeType(d, q, depth + 1);
    const t: Type = { class: "variable-length", size, base };
    if ((bits & 15) === 1) {
      Object.assign(t, { string: true, padding: choice(PADDING, (bits >> 4) & 15), charset: choice(CHARSETS, (bits >> 8) & 15) });
    }
    return [t, q];
  }
  if (cls === 10) {
    const rank = u(d, q, 1);
    let dims: number[];
    if (version < 3) {
      dims = Array.from({ length: rank }, (_, i) => u(d, q + 4 + 4 * i, 4));
      q += 4 + 8 * rank;
    } else {
      dims = Array.from({ length: rank }, (_, i) => u(d, q + 1 + 4 * i, 4));
      q += 1 + 4 * rank;
    }
    let base: Type;
    [base, q] = describeType(d, q, depth + 1);
    return [{ class: "array", size, shape: dims, base }, q];
  }
  return reject(`unknown datatype class ${cls}`);
}

/** An integer of datatype `t`, as JSON. */
function integer(t: Type, b: Uint8Array): Json {
  let v = 0n;
  const bytes = t.order === "big" ? [...b] : [...b].reverse();
  for (const x of bytes) v = (v << 8n) | BigInt(x);
  if (t.signed && b.length && v >= 1n << BigInt(8 * b.length - 1)) v -= 1n << BigInt(8 * b.length);
  return jsonNumber(v);
}

/** The Zarr data type of an integer or IEEE float datatype, else undefined. */
function numeric(t: Type): string | undefined {
  if (t.order !== "little" && t.order !== "big") return undefined;
  if (t.class === "integer" && !("offset" in t) && [1, 2, 4, 8].includes(t.size)) {
    return `${t.signed ? "int" : "uint"}${8 * t.size}`;
  }
  if (t.class === "float" && !("layout" in t) && [2, 4, 8].includes(t.size)) return `float${8 * t.size}`;
  return undefined;
}

/** The number datatype and the extra dimensions of a dataset's elements of
 * datatype `t`: its own for a number, its base's for an enumeration, and
 * those of its base, after its own dimensions, for an array; else undefined. */
function element(t: Type): [Type, number[]] | undefined {
  if (t.class === "enum") return element(t.base);
  if (t.class === "array") {
    const inner = element(t.base);
    return inner !== undefined ? [inner[0], [...t.shape, ...inner[1]]] : undefined;
  }
  return numeric(t) !== undefined ? [t, []] : undefined;
}

function half(v: number): number {
  const sign = v & 0x8000 ? -1 : 1, exp = (v >> 10) & 0x1f, frac = v & 0x3ff;
  if (exp === 0) return sign * 2 ** -14 * (frac / 1024);
  if (exp === 31) return frac ? NaN : sign * Infinity;
  return sign * 2 ** (exp - 15) * (1 + frac / 1024);
}

function numbers(t: Type, count: number, data: Uint8Array): Json[] | undefined {
  const type = numeric(t);
  if (type === undefined) return undefined;
  const little = t.order === "little";
  const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
  const out: Json[] = [];
  for (let i = 0; i < count; i++) {
    const at = i * t.size;
    switch (type) {
      case "int8": out.push(view.getInt8(at)); break;
      case "uint8": out.push(view.getUint8(at)); break;
      case "int16": out.push(view.getInt16(at, little)); break;
      case "uint16": out.push(view.getUint16(at, little)); break;
      case "int32": out.push(view.getInt32(at, little)); break;
      case "uint32": out.push(view.getUint32(at, little)); break;
      case "int64": out.push(jsonNumber(view.getBigInt64(at, little))); break;
      case "uint64": out.push(jsonNumber(view.getBigUint64(at, little))); break;
      case "float16": out.push(jsonNumber(half(view.getUint16(at, little)))); break;
      case "float32": out.push(jsonNumber(view.getFloat32(at, little))); break;
      default: out.push(jsonNumber(view.getFloat64(at, little)));
    }
  }
  return out;
}

/** Whether elements of datatype `t` hold file addresses: references and
 * variable-length data, at any depth. */
function holdsAddresses(t: Type): boolean {
  if (t.class === "reference" || t.class === "variable-length") return true;
  if (t.class === "compound") return t.members.some((m: Type) => holdsAddresses(m.type));
  return t.class === "array" && holdsAddresses(t.base);
}

/** Whether a member's name can name a Zarr node of the hierarchy (§5.1). */
function nodeName(key: string): boolean {
  return key !== "" && key !== "." && key !== ".." && key !== "zarr.json" && !key.startsWith("__") && !key.includes("/");
}

/** A fixed-size string element's text: its bytes without the trailing padding
 * (spaces for a space-padded string, else NUL), if no NUL is left. */
function elementText(e: Uint8Array, padding: unknown): Uint8Array | undefined {
  const pad = padding === "space-padded" ? 0x20 : 0;
  let end = e.length;
  while (end > 0 && e[end - 1] === pad) end--;
  const text = e.subarray(0, end);
  return text.includes(0) ? undefined : text;
}

/** The value form: one value for a scalar, the list for one dimension, else
 * the list and the shape. */
function put(entry: Record<string, Json>, dims: number[], values: Json[]): Record<string, Json> {
  if (dims.length === 0) {
    entry.value = values[0];
  } else {
    if (dims.length !== 1) entry.shape = dims;
    entry.value = values;
  }
  return entry;
}

const CANONICAL_NAN: Record<number, bigint> = { 2: 0x7e00n, 4: 0x7fc00000n, 8: 0x7ff8000000000000n };

/** The Zarr fill value that is exactly the dataset's fill value (§5.4), or
 * undefined: the elements' number when they are all the same bytes (an
 * integer as it is, NaN only as the canonical quiet NaN, never -0), and the
 * byte for an array of bytes when they are all equal. */
function exactFill(base: Type | undefined, fill: Uint8Array): Json | undefined {
  const size = base !== undefined ? base.size : 1;
  for (let i = size; i < fill.length; i += size) {
    for (let j = 0; j < size; j++) if (fill[i + j] !== fill[j]) return undefined;
  }
  if (base === undefined) return fill[0];
  const first = fill.subarray(0, size);
  const big = base.order === "big";
  let bits = 0n;
  for (const x of big ? [...first] : [...first].reverse()) bits = (bits << 8n) | BigInt(x);
  if (base.class === "integer") {
    if (base.signed && bits >= 1n << BigInt(8 * size - 1)) bits -= 1n << BigInt(8 * size);
    return Number.isSafeInteger(Number(bits)) && BigInt(Number(bits)) === bits ? Number(bits)
      : (JSON as unknown as { rawJSON: (s: string) => Json }).rawJSON(bits.toString());
  }
  const v = numbers(base, 1, first)![0];
  if (v === "NaN") return bits === CANONICAL_NAN[size] ? "NaN" : undefined;
  if (v === 0 && bits >> BigInt(8 * size - 1)) return undefined;
  return v;
}

/** Inflates a whole zlib stream, with nothing after it, of `size` bytes. */
async function inflate(data: Uint8Array, size: number): Promise<Uint8Array> {
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    const stream = new Blob([data as BlobPart]).stream().pipeThrough(new DecompressionStream("deflate"));
    const reader = stream.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.length;
      if (total > size) {
        await reader.cancel();
        break;
      }
      chunks.push(value);
    }
  } catch {
    total = -1;
  }
  if (total !== size) reject("a chunk that does not decode to its size");
  const out = new Uint8Array(size);
  let p = 0;
  for (const c of chunks) {
    out.set(c, p);
    p += c.length;
  }
  return out;
}

/** Copies a chunk into the C-order array `out`, clipped to `dims`. */
function scatter(out: Uint8Array, dims: number[], e: number, chunk: Uint8Array, chunkShape: number[], origin: number[]): void {
  const rank = dims.length;
  if (rank === 0) {
    out.set(chunk.subarray(0, e));
    return;
  }
  const run = Math.min(chunkShape[rank - 1], dims[rank - 1] - origin[rank - 1]) * e;
  const index = new Array(rank - 1).fill(0);
  for (;;) {
    if (index.every((v, i) => origin[i] + v < dims[i])) {
      let src = 0, dst = 0;
      for (let i = 0; i < rank - 1; i++) {
        src = src * chunkShape[i] + index[i];
        dst = dst * dims[i] + origin[i] + index[i];
      }
      src = src * chunkShape[rank - 1] * e;
      dst = (dst * dims[rank - 1] + origin[rank - 1]) * e;
      out.set(chunk.subarray(src, src + run), dst);
    }
    let i = rank - 2;
    for (; i >= 0; i--) {
      index[i]++;
      if (index[i] < chunkShape[i]) break;
      index[i] = 0;
    }
    if (i < 0) return;
  }
}

const joinKey = (coords: (string | number)[]) => coords.join("/");
const splitKey = (key: string) => (key === "" ? [] : key.split("/").map(Number));

/** The numbers of datatype `t`, as JSON, when JSON holds them exactly:
 * undefined when one is a NaN other than the canonical quiet NaN or a negative zero. */
function exactNumbers(t: Type, count: number, data: Uint8Array): Json[] | undefined {
  const values = numbers(t, count, data);
  if (values === undefined || t.class !== "float") return values;
  const size: number = t.size;
  const nan = CANONICAL_NAN[size], sign = t.order === "big" ? 0 : size - 1;
  for (let i = 0; i < values.length; i++) {
    const b = data.subarray(i * size, (i + 1) * size);
    let bits = 0n;
    for (const x of t.order === "big" ? [...b] : [...b].reverse()) bits = (bits << 8n) | BigInt(x);
    if ((values[i] === "NaN" && bits !== nan) || (values[i] === 0 && b[sign] & 0x80)) return undefined;
  }
  return values;
}

// ---- JSON texts and their sizes (conventions/ims/README.md §5.6)

/** Orders strings by their UTF-8 bytes (by code point). */
function byCodePoint(a: string, b: string): number {
  const x = [...a], y = [...b];
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    const d = x[i].codePointAt(0)! - y[i].codePointAt(0)!;
    if (d) return d;
  }
  return x.length - y.length;
}

/** The JSON text of a value (§5.6): no whitespace, object members in ascending
 * order of their keys' UTF-8 bytes, characters other than those JSON escapes as
 * themselves, and numbers as ECMAScript writes them. */
export function canonical(v: Json): string {
  if (v === null || typeof v !== "object") return JSON.stringify(v);
  if (Array.isArray(v)) return `[${v.map(canonical).join(",")}]`;
  const o = v as Record<string, Json>;
  return `{${Object.keys(o).sort(byCodePoint).map((k) => `${JSON.stringify(k)}:${canonical(o[k])}`).join(",")}}`;
}

const utf8 = new TextEncoder();

/** The size of a value: the UTF-8 length of its JSON text. */
const size = (v: Json) => utf8.encode(canonical(v)).length;

/** How a dataset's elements of datatype `t`, which holds addresses, are kept
 * (§5.4): a family of byte values, indexes into the object table, or a family of
 * region references; undefined when they are not. */
function column(t: Type): "family" | "paths" | "regions" | undefined {
  if (t.class === "variable-length" && !holdsAddresses(t.base)) return "family";
  if (t.class === "reference" && t.kind === 0 && t.size === 8) return "paths";
  if (t.class === "reference" && t.kind === 1 && t.size === 12) return "regions";
  return undefined;
}

/** The object header address of an object reference, or undefined (§8.9). */
function refAddress(b: Uint8Array): number | undefined {
  const v = le(b, 0, 8);
  return v > 0n && v <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(v) : undefined;
}

/** Undoes HDF5's shuffle filter: byte j of element i is at j * n + i. */
function unshuffle(data: Uint8Array, item: number): Uint8Array {
  const n = Math.floor(data.length / item);
  if (item <= 1 || n <= 1) return data;
  const out = new Uint8Array(data.length);
  for (let j = 0; j < item; j++) for (let i = 0; i < n; i++) out[i * item + j] = data[j * n + i];
  out.set(data.subarray(n * item), n * item);
  return out;
}

function shapeWith(entry: Record<string, Json>, dims: number[], more: Record<string, Json>): Record<string, Json> {
  if (dims.length !== 1) entry.shape = dims;
  return Object.assign(entry, more);
}

const KEYED = ["attributes", "names", "links", "images", "datatypes", "unsupported"];
const LISTS = ["attribute_collisions", "collisions"];

/** Adds an entry to a source metadata object (§5.6). */
function merge(s: Record<string, Json>, entry: Record<string, Json>): void {
  for (const [k, v] of Object.entries(entry)) {
    if (KEYED.includes(k) && v !== null) {
      const into = (s[k] ??= {}) as Record<string, Json>;
      for (const [key, value] of Object.entries(v as Record<string, Json>)) setMember(into, key, value);
    } else if (LISTS.includes(k)) {
      ((s[k] ??= []) as Json[]).push(...(v as Json[]));
    } else {
      s[k] = v;
    }
  }
}

const SHUFFLED = ["2", "2,1", "2,3", "2,1,3"]; // pipelines with shuffle, decoded and copied (§5.4)
const REFERENCED = ["", "1", "3", "1,3"]; // pipelines whose chunks are referenced
const ALLOWED = [...REFERENCED, ...SHUFFLED];

// ---- the tree (conventions/ims/README.md §5.1)

type Item = [key: string, name: Json | undefined, raw: Uint8Array, clash: boolean, at: number];
type Entry = ReturnType<typeof planEntries>[number];
type DocEntry =
  | ["type", Type, number | undefined]
  | ["set", Record<string, Json>]
  | ["keyed", string, string, Json]
  | ["list", string, Json]
  | ["attrs", Item[] | null]
  | ["member", string, string, () => Record<string, Json>, Item[] | null];
type Region = [number | undefined, Record<string, unknown>] | null;
type Job =
  | ["index", string, (number | undefined)[], number[], Record<string, Json> | undefined, boolean]
  | ["regions", string, Region[], Record<string, Json> | undefined]
  | ["family", string, [number, number][], number]
  | ["data", string, string, number[], number, number, "little" | "big"];

/** A document of the source metadata node: its source metadata `s`, which
 * settling fills, and its entries in order (§5.6). */
class Doc {
  s: Record<string, Json>;
  entries: DocEntry[] = [];
  constructor(s: Record<string, Json>) {
    this.s = s;
  }
}

/** A link of a type other than hard or soft (§5.1): an external link's file
 * and path, else the type and its value in base64. */
function otherLink(kind: number, value: Uint8Array | null): Json {
  if (value === null) return { user: { type: kind, value: null } };
  if (kind === 64 && value.length && value[0] === 0) {
    const fileEnd = value.indexOf(0, 1);
    const pathEnd = fileEnd > 0 ? value.indexOf(0, fileEnd + 1) : -1;
    if (pathEnd === value.length - 1) {
      return { external: { file: textJson(value.subarray(1, fileEnd)), path: textJson(value.subarray(fileEnd + 1, pathEnd)) } };
    }
  }
  return { user: { type: kind, value: base64(value) } };
}

/** A family of byte values held in memory (conventions §7), each chunk copied. */
export function bytesFamily(path: string, values: Uint8Array[]): Plan[] {
  const count = values.length;
  const lengths = new Set(values.map((v) => v.length));
  if (lengths.size === 1 && !lengths.has(0) && Math.max(...lengths) <= MAX_COPY) {
    const [length] = lengths;
    return [{
      path, dataType: "uint8", shape: [count, length], chunkShape: [1, length], dims: ["index", "byte"],
      chunks: new Map(values.map((v, i) => [`${i}/0`, v])),
    }];
  }
  const offsets = new Uint8Array(8 * (count + 1));
  const view = new DataView(offsets.buffer);
  let total = 0;
  values.forEach((v, i) => {
    view.setBigInt64(8 * i, BigInt(total), true);
    total += v.length;
  });
  view.setBigInt64(8 * count, BigInt(total), true);
  const plans: Plan[] = [{
    path: `${path}/offsets`, dataType: "int64", shape: [count + 1], chunkShape: [count + 1], dims: ["index"],
    chunks: new Map([["0", offsets]]),
  }];
  if (total > 0) {
    const blob = new Uint8Array(total);
    let at = 0;
    for (const v of values) {
      blob.set(v, at);
      at += v.length;
    }
    const size = Math.ceil(total / Math.ceil(total / RAGGED_CHUNK)); // balanced: ceil(total / 2^20) chunks
    const chunks = new Map<string, Part[] | Uint8Array>();
    for (let c = 0; c < Math.ceil(total / size); c++) {
      const chunk = new Uint8Array(size);
      chunk.set(blob.subarray(c * size, (c + 1) * size));
      chunks.set(String(c), chunk);
    }
    plans.push({ path: `${path}/data`, dataType: "uint8", shape: [total], chunkShape: [size], dims: ["byte"], chunks });
  }
  return plans;
}

/** Walks the HDF5 tree from the root group; a failure to read an object records
 * it, and never rejects. The documents are settled after the walk, when every
 * path is known. */
export class Tree {
  private used = 0;
  private selections = 0; // bytes of selections read for region references (MAX_SELECTIONS)
  private regions = new Map<string, [number | undefined, Record<string, unknown>]>(); // "collection index" -> [object header, selection]
  private objects = 0;
  private seen = new Map<number, string>(); // object header -> the path where the walk met it
  private order = new Map<number, number>(); // object header -> its place in the walk, its index in the object table
  private parents: number[] = []; // the object table: each object's parent's index (-1 for the root)
  private names: Uint8Array[] = []; // and its key
  groups: [string, Record<string, Json>][] = []; // [path under the node, source metadata]
  plans: Plan[] = [];
  attributePlans: Plan[] = [];
  attributeGroups: string[] = [];
  private docs: Doc[] = [];
  private jobs: Job[] = []; // arrays built once the object table is known
  private arrays = 0; // attributes/<i> numbered so far
  private spills = 0; // spilled/<i> numbered so far

  private f: Hdf5;
  private read: ByteReader;
  private images: Map<number, Record<string, Json>>;

  constructor(f: Hdf5, read: ByteReader, images: Map<number, Record<string, Json>>) {
    this.f = f;
    this.read = read;
    this.images = images;
  }

  /** Numbers an object the walk meets, in the object table (§5.7). */
  private meet(at: number, path: string, parent: number | undefined, key: string): void {
    this.order.set(at, this.order.size);
    this.seen.set(at, path);
    this.parents.push(parent === undefined ? -1 : this.order.get(parent)!);
    this.names.push(utf8.encode(key));
  }

  /** The index in the object table of the object at `at`, or null. */
  private indexOf(at: number | undefined): number | null {
    return at !== undefined ? this.order.get(at) ?? null : null;
  }

  private doc(s: Record<string, Json>): Doc {
    const d = new Doc(s);
    this.docs.push(d);
    return d;
  }

  /** A datatype's members (§5.2): its description, or, when it is named and the
   * walk met the committed datatype, its path alone. */
  private typePart(t: Type, named: number | undefined): Record<string, Json> {
    if (named === undefined) return { datatype: t };
    const index = this.order.get(named);
    return index !== undefined ? { named: index } : { named: null, datatype: t };
  }

  // ---- attributes (§5.3)

  /** An object's attributes, in ascending byte order of their names: [key, name
   * as a text value when it must be given, message, collision, the message's
   * address], or null when they cannot be read. */
  private async attributeItems(at: number): Promise<Item[] | null> {
    let raw: Map<string, Uint8Array>;
    const where = new Map<string, number>();
    try {
      raw = await this.f.attributes(at, where);
    } catch (e) {
      if (e instanceof ImsError) return null;
      throw e;
    }
    const items: Item[] = [];
    const keys = new Set<string>();
    for (const name of [...raw.keys()].sort()) {
      const bytes = bytesOf(name);
      const key = decodeText(bytes), text = textJson(bytes);
      const clash = keys.has(key);
      keys.add(key);
      items.push([key, clash || typeof text !== "string" ? text : undefined, raw.get(name)!, clash, where.get(name)!]);
    }
    return items;
  }

  /** An attribute's value and its cost, held as JSON only within `room` (§5.6). */
  private async attributeEntry(item: Item, room: number): Promise<[Json, number]> {
    const [key, name, raw, clash, at] = item;
    const cost = (v: Json) => size(v) + (clash ? 0 : size(key));
    const value = await this.attribute(raw, name, (v) => cost(v) <= room, at);
    return [value, cost(value)];
  }

  /** An attribute's value; held as JSON when not large and `fits` allows it.
   * `at` is the message's address, so that the array form references its data. */
  private async attribute(raw: Uint8Array, name: Json | undefined, fits: (v: Json) => boolean, at: number): Promise<Json> {
    let typeMessage: Uint8Array, dims: number[] | null, data: Uint8Array, named: number | undefined, t: Type, start: number;
    try {
      const shared = attributeSharedType(raw);
      let committed: Uint8Array | undefined;
      if (shared !== undefined) [committed, named] = await this.f.committed(shared);
      [typeMessage, dims, data, start] = attributeFull(raw, committed);
      [t] = describeType(typeMessage);
    } catch (e) {
      if (!(e instanceof ImsError)) throw e;
      return { reason: e.message, ...(name !== undefined ? { name } : {}) };
    }
    const entry = this.typePart(t, named);
    if (name !== undefined) entry.name = name;
    if (dims === null) {
      entry.shape = null;
      return entry;
    }
    if (this.used + data.length > MAX_VALUE_BYTES) return shapeWith(entry, dims, { reason: "over the budget of attribute data" });
    this.used += data.length;
    const count = product(dims);
    if (t.size === 0 && count > MAX_TEXT) return shapeWith(entry, dims, { reason: "more than 2^20 elements of no bytes" });
    let held: Record<string, Json> | ["bare", Json] | undefined, large: boolean, array: (path: string) => Record<string, Json>;
    const selections = this.selections;
    try {
      [held, large, array] = await this.forms(t, dims, data, count, name === undefined && named === undefined, at + start);
    } catch (e) {
      if (!(e instanceof ImsError)) throw e;
      this.selections = selections; // what an object that fails read is not counted
      return shapeWith(entry, dims, { reason: e.message });
    }
    if (held !== undefined && !large) {
      const value = Array.isArray(held) ? held[1] : { ...entry, ...held };
      if (fits(value)) return value;
    }
    const path = `attributes/${this.arrays++}`;
    return Object.assign(entry, array(path));
  }

  /** [the attribute's value as JSON, or undefined; whether it is large; the
   * array form, a function of its path giving its members] (§5.3). The file
   * holds the data at `at`, where the array form references it. */
  private async forms(t: Type, dims: number[], data: Uint8Array, count: number, plain: boolean, at: number): Promise<[
    Record<string, Json> | ["bare", Json] | undefined, boolean, (path: string) => Record<string, Json>,
  ]> {
    const cls = t.class;
    if (cls === "string") {
      const array = (path: string) => {
        this.jobs.push(["data", path, "uint8", [data.length], 1, at, "little"]);
        return { array: path, ...(dims.length === 1 && dims[0] === data.length ? {} : { shape: dims }) };
      };
      if (data.length > MAX_TEXT) return [undefined, true, array];
      let large = false;
      if (dims.length === 1 && t.size === 1 && !data.includes(0)) {
        const text = textJson(data);
        if (plain && t.size === 1 && t.padding === "null-terminated" && t.charset === "ascii") return [["bare", text], large, array];
        return [{ value: text }, large, array];
      }
      const texts: (Uint8Array | undefined)[] = [];
      if (t.size === 0) {
        for (let i = 0; i < count; i++) texts.push(new Uint8Array(0)); // a string of no bytes is empty
      } else {
        for (let i = 0; i < data.length; i += t.size) texts.push(elementText(data.subarray(i, i + t.size), t.padding));
      }
      if (!texts.includes(undefined)) return [put({}, dims, texts.map((b) => textJson(b!))), large, array];
      large ||= data.length > MAX_RAW;
      return [dataForm(dims, data), large, array];
    }
    if (numeric(t) !== undefined) {
      const array = (path: string) => {
        this.jobs.push(["data", path, numeric(t)!, dims, t.size, at, t.order]);
        return shapeWith({}, dims, { array: path });
      };
      if (count > INLINE) return [undefined, true, array];
      const values = exactNumbers(t, count, data);
      if (values === undefined) return [dataForm(dims, data), data.length > MAX_RAW, array];
      return [put({}, dims, values), false, array];
    }
    if (cls === "variable-length" && !holdsAddresses(t.base)) {
      const heap: [number, number][] = [];
      for (let i = 0; i < count; i++) heap.push(await this.heapObject(t, data.subarray(16 * i, 16 * i + 16)));
      const array = (path: string) => {
        this.jobs.push(["family", path, heap, count]);
        return shapeWith({}, dims, { array: path });
      };
      if (heap.reduce((n, [, k]) => n + k, 0) > MAX_HEAP) return [undefined, true, array];
      const values: Json[] = [];
      for (const [where, n] of heap) values.push(vlenValue(t, await this.heapBytes(where, n, true)));
      return [put({}, dims, values), false, array];
    }
    if (column(t) === "paths") {
      const refs = Array.from({ length: count }, (_, i) => refAddress(data.subarray(8 * i, 8 * i + 8)));
      const array = (path: string) => {
        this.jobs.push(["index", path, refs, dims, undefined, false]);
        return shapeWith({}, dims, { array: path });
      };
      return [put({}, dims, refs.map((a) => this.indexOf(a))), count > INLINE, array];
    }
    if (holdsAddresses(t)) {
      const n: number = t.size;
      const values: Json[] = [];
      for (let i = 0; i < count; i++) values.push(await this.structured(t, data.subarray(i * n, (i + 1) * n), true));
      const array = (path: string) => {
        const text = utf8.encode(canonical(dims.length ? values : values[0]));
        this.attributeArray(path, "uint8", [text.length], 1, text, "little");
        return { json: path, ...(dims.length > 1 ? { shape: dims } : {}) };
      };
      return [put({}, dims, values), false, array];
    }
    const array = (path: string) => {
      this.jobs.push(["data", path, "uint8", [...dims, t.size], 1, at, "little"]);
      return { array: path, shape: dims };
    };
    if (data.length > MAX_RAW) return [undefined, true, array];
    return [dataForm(dims, data), false, array];
  }

  /** [address, size] of a variable-length element's bytes. */
  private async heapObject(t: Type, ref: Uint8Array): Promise<[number, number]> {
    const n = u(ref, 0, 4), at = address(le(ref, 4, 8));
    const size = t.string ? n : n * t.base.size;
    if (!n) return [0, 0];
    if (at === undefined) return reject("a variable-length element at an undefined address");
    const [where, stored] = await this.f.globalObject(at, u(ref, 12, 4));
    if (stored < size) reject("a variable-length element longer than its heap object");
    return [where, size];
  }

  /** Bytes of the global heap; `counted` reads them within the budget of attribute data. */
  private async heapBytes(where: number, n: number, counted: boolean): Promise<Uint8Array> {
    if (counted) {
      if (this.used + n > MAX_VALUE_BYTES) reject("over the budget of attribute data");
      this.used += n;
    }
    return n ? this.read(where, n) : new Uint8Array(0);
  }

  /** An element of a datatype that holds addresses, as JSON (§5.3). */
  private async structured(t: Type, b: Uint8Array, counted: boolean): Promise<Json> {
    if (!holdsAddresses(t)) {
      if (numeric(t) !== undefined) {
        const v = exactNumbers(t, 1, b);
        if (v !== undefined) return v[0];
      }
      return base64(b);
    }
    if (t.class === "reference") {
      const kind = column(t);
      if (kind === "paths") return this.indexOf(refAddress(b));
      if (kind === "regions") return this.regionValue(await this.region(b));
      return reject("a reference that is neither an object nor a region reference");
    }
    if (t.class === "variable-length") {
      const data = await this.heapBytes(...(await this.heapObject(t, b)), counted);
      if (!holdsAddresses(t.base)) return vlenValue(t, data);
      const n: number = t.base.size, out: Json[] = [];
      for (let i = 0; i < u(b, 0, 4); i++) out.push(await this.structured(t.base, data.subarray(i * n, (i + 1) * n), counted));
      return out;
    }
    if (t.class === "compound") {
      const out: Record<string, Json> = {};
      for (const m of t.members as Type[]) {
        const mt = m.type, o: number = m.offset;
        if (o + mt.size > b.length) reject("a compound member outside its compound");
        if (Object.hasOwn(out, m.name)) reject("compound members of the same name");
        setMember(out, m.name, await this.structured(mt, b.subarray(o, o + mt.size), counted));
      }
      return out;
    }
    const n: number = t.base.size, count = product(t.shape);
    if (n * count > b.length) reject("an array larger than its datatype");
    const out: Json[] = [];
    for (let i = 0; i < count; i++) out.push(await this.structured(t.base, b.subarray(i * n, (i + 1) * n), counted));
    return out;
  }

  /** A region reference's [object header address, selection], or null for the
   * null reference (§8.9). Each reference counts the size of its global heap
   * object against the budget of selections; each object is read once. */
  private async region(b: Uint8Array): Promise<Region> {
    const at = address(le(b, 0, 8));
    if (at === undefined || at === 0) return null;
    const index = u(b, 8, 4), key = `${at} ${index}`;
    const [where, n] = await this.f.globalObject(at, index);
    if (this.selections + n > MAX_SELECTIONS) reject("over the budget of region selections");
    this.selections += n;
    let found = this.regions.get(key);
    if (found === undefined) {
      const d = n ? await this.read(where, n) : new Uint8Array(0);
      found = [refAddress(d), parseSelection(d, 8)[0]];
      this.regions.set(key, found);
    }
    return found;
  }

  /** A region reference as JSON (§5.3): null, or its object's index and its selection. */
  private regionValue(found: Region): Json {
    return found === null ? null : { object: this.indexOf(found[0]), selection: found[1] as Json };
  }

  /** A copy of in-memory data as an array (conventions §7). */
  private attributeArray(
    path: string, dataType: string, shape: number[], item: number, data: Uint8Array, endian: string,
    attributes?: Record<string, Json>, plans?: Plan[],
  ): void {
    const [chunkShape, ranges] = planGrid(0, shape, item);
    const chunks = new Map<string, Part[] | Uint8Array>();
    for (const [k, parts] of ranges) {
      const pieces = parts.map((p) => (p instanceof Uint8Array ? p : data.subarray(p[0], p[0] + p[1])));
      const c = new Uint8Array(pieces.reduce((n, p) => n + p.length, 0));
      let at = 0;
      for (const p of pieces) {
        c.set(p, at);
        at += p.length;
      }
      chunks.set(k, c);
    }
    (plans ?? this.attributePlans).push({
      path, dataType, shape, chunkShape, dims: undefined, chunks, fill: 0, endian: endian as "little" | "big", attributes,
    });
  }

  private attributeFamily(path: string, plans: Plan[]): void {
    if (plans[0].path !== path) this.attributeGroups.push(path);
    this.attributePlans.push(...plans);
  }

  // ---- settling the documents (§5.6)

  private async settle(doc: Doc): Promise<void> {
    let kept = 0, spilling = false;
    const spilled: Record<string, Json>[] = [];
    const offer = (entry: Record<string, Json>, cost: number) => {
      if (!spilling && kept + cost <= BUDGET) {
        merge(doc.s, entry);
        kept += cost;
      } else {
        spilling = true;
        spilled.push(entry);
      }
    };
    // Spilled entries are data, not the document: their attributes are JSON unless large.
    const room = () => (spilling ? Number.MAX_SAFE_INTEGER : BUDGET - kept);
    const keyed = (section: string, key: string, value: Json) => {
      const inner: Record<string, Json> = {};
      setMember(inner, key, value);
      return { [section]: inner };
    };
    for (const e of doc.entries) {
      if (e[0] === "type") {
        const part = this.typePart(e[1], e[2]);
        offer(part, size(part));
      } else if (e[0] === "set") {
        offer(e[1], size(e[1]));
      } else if (e[0] === "keyed") {
        offer(keyed(e[1], e[2], e[3]), size(e[2]) + size(e[3]));
      } else if (e[0] === "list") {
        offer({ [e[1]]: [e[2]] }, size(e[2]));
      } else if (e[0] === "attrs") {
        if (e[1] === null) offer({ attributes: null }, size(null));
        for (const item of e[1] ?? []) {
          const [value, cost] = await this.attributeEntry(item, room());
          offer(item[3] ? { attribute_collisions: [value] } : keyed("attributes", item[0], value), cost);
        }
      } else { // "member": an entry of images, datatypes or unsupported, with the member's attributes
        const [, section, key, make, items] = e;
        const value = make();
        let cost = size(key) + size(value);
        if (items === null) {
          value.attributes = null;
          cost += size(null);
        }
        const held: Record<string, Json> = {};
        const collided: Json[] = [];
        for (const item of items ?? []) {
          const [v, c] = await this.attributeEntry(item, room() - cost);
          cost += c;
          if (item[3]) collided.push(v);
          else setMember(held, item[0], v);
        }
        if (Object.keys(held).length) value.attributes = held;
        if (collided.length) value.attribute_collisions = collided;
        offer(keyed(section, key, value), cost);
      }
    }
    if (spilled.length) {
      const path = `spilled/${this.spills++}`;
      doc.s.spilled = path;
      this.attributeFamily(path, bytesFamily(path, spilled.map((e) => utf8.encode(canonical(e)))));
    }
  }

  /** Settles every document, then builds the object table and the arrays of indexes into it. */
  private async finish(): Promise<void> {
    for (const d of this.docs) await this.settle(d);
    const parents = new Uint8Array(4 * this.parents.length);
    this.parents.forEach((p, i) => new DataView(parents.buffer).setInt32(4 * i, p, true));
    this.attributeArray("objects/parent", "int32", [this.parents.length], 4, parents, "little");
    this.attributeFamily("objects/name", bytesFamily("objects/name", this.names));
    for (const job of this.jobs) {
      if (job[0] === "index") {
        const [, path, refs, dims, s, dataset] = job;
        const data = new Uint8Array(4 * refs.length);
        const view = new DataView(data.buffer);
        refs.forEach((a, i) => view.setInt32(4 * i, a !== undefined ? this.order.get(a) ?? -1 : -1, true));
        this.attributeArray(path, "int32", dims, 4, data, "little", s, dataset ? this.plans : undefined);
      } else if (job[0] === "regions") {
        const [, path, regions, s] = job;
        const texts = regions.map((r) => utf8.encode(canonical(this.regionValue(r))));
        this.family(path, bytesFamily(path, texts), s);
      } else if (job[0] === "data") {
        const [, path, dataType, shape, item, at, endian] = job;
        const [chunkShape, chunks] = await gridChunks(at, shape, item, this.read);
        this.attributePlans.push({ path, dataType, shape, chunkShape, dims: undefined, chunks, fill: 0, endian });
      } else {
        const [, path, heap, count] = job;
        this.attributeFamily(path, await familyPlans(path, heap.map(([w, n], i) => [i, w, n]), this.read, count));
      }
    }
  }

  // ---- datasets (§5.4)

  /** A dataset's array, or its group: a family, a compound of several, or a null dataspace. */
  async dataset(at: number, path: string): Promise<void> {
    const f = this.f;
    if ((await f.header(at)).some((m) => m.type === EXTERNAL_FILES)) reject("its data is in external files");
    const ds = await f.dataset(at, true);
    const [t] = describeType(ds.typeMessage);
    const s: Record<string, Json> = {};
    const doc = this.doc(s);
    doc.entries.push(["type", t, ds.named]);
    const attributes: DocEntry = ["attrs", await this.attributeItems(at)];
    if (ds.dims === null) {
      doc.entries.push(["set", { shape: null }]);
      if (ds.fill !== undefined && !holdsAddresses(t)) doc.entries.push(["set", { fill: base64(ds.fill) }]);
      doc.entries.push(attributes);
      this.groups.push([path, s]);
      return;
    }
    const count = product(ds.dims);
    if (holdsAddresses(t)) {
      const kind = column(t);
      if (kind === undefined && t.class !== "compound") {
        reject(t.class === "variable-length" ? VLEN_REASON
          : t.class === "reference" ? "a reference that is neither an object nor a region reference"
          : "a datatype that holds references or variable-length data");
      }
      if (count > MAX_ELEMENTS) reject(`more than ${MAX_ELEMENTS} elements that hold addresses`);
      if (kind !== undefined) {
        if (kind !== "paths") doc.entries.push(["set", { shape: ds.dims }]);
        doc.entries.push(attributes);
        const n: number = t.size;
        await this.column(path, t, kind, await this.stored(ds, n * count), n, 0, count, ds.dims, s);
        return;
      }
      await this.compound(ds, t, path, count, s);
      doc.entries.push(attributes);
      return;
    }
    const e = element(t);
    let base: Type | undefined, extra: number[], dataType: string, endian: "little" | "big", item: number;
    if (e !== undefined) {
      [base, extra] = e;
      [dataType, endian, item] = [numeric(base)!, base.order, base.size];
    } else {
      [base, extra, dataType, endian, item] = [undefined, [t.size], "uint8", "little", 1];
    }
    let fill: Json = 0;
    if (ds.fill !== undefined) {
      const exact = exactFill(base, ds.fill);
      if (exact === undefined) doc.entries.push(["set", { fill: base64(ds.fill) }]);
      else fill = exact;
    }
    doc.entries.push(attributes);
    const shape = [...ds.dims, ...extra];
    const size = count * t.size;
    const zeros = (n: number) => new Array(n).fill(0);
    let compressor: Record<string, unknown> | undefined;
    let chunkShape: number[];
    let chunks = new Map<string, Part[] | Uint8Array>();
    if (ds.layout === "chunked") {
      const pipeline = ds.filters.join(",");
      if (!ALLOWED.includes(pipeline)) reject(`unsupported HDF5 filters [${ds.filters.join(", ")}]`);
      chunkShape = [...ds.chunk, ...extra];
      const nbytes = product(ds.chunk) * t.size;
      const implicit = ds.index === "implicit" ? await this.implicit(ds, shape, item, t.size) : undefined;
      if (implicit !== undefined) {
        [chunkShape, chunks] = implicit;
      } else {
        const masks = new Map<string, number>();
        const found = [...(await f.chunks(ds, masks))].map(([k, ref]) => [splitKey(k), ref, masks.get(k) ?? 0] as [number[], [number, number], number]);
        found.sort(([a], [b]) => {
          for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] - b[i];
          return 0;
        });
        if (REFERENCED.includes(pipeline) && masks.size === 0) {
          compressor = ds.filters.includes(1) ? ZLIB : undefined;
          for (const [k, [where, n0]] of found) {
            const n = n0 - (ds.filters.includes(3) ? 4 : 0);
            if (n < 1 || (!ds.filters.includes(1) && n !== nbytes)) reject(`chunk ${showCoords(k)} of ${n} bytes, not ${nbytes}`);
            chunks.set(joinKey([...k, ...zeros(extra.length)]), [[where, n]]);
          }
        } else {
          if (found.length * nbytes > MAX_COPY) reject("data to decode of more than 2^24 bytes");
          for (const [k, [where, n], mask] of found) {
            chunks.set(joinKey([...k, ...zeros(extra.length)]), await this.decodeChunk(ds, await this.read(where, n), nbytes, mask));
          }
        }
      }
    } else if (ds.layout === "contiguous") {
      if (ds.dataAddress !== undefined && ds.dataAddress + size > f.size) reject("data outside the file");
      if (size === 0 || ds.dataAddress === undefined) {
        chunkShape = shape.map((v) => Math.max(1, v));
      } else {
        [chunkShape, chunks] = await gridChunks(ds.dataAddress, shape, item, this.read);
      }
    } else {
      if (ds.compact.length < size) reject("truncated compact dataset");
      chunkShape = shape.map((v) => Math.max(1, v));
      if (size) chunks.set(joinKey(zeros(shape.length)), ds.compact.slice(0, size));
    }
    this.plans.push({ path, dataType, shape, chunkShape, dims: undefined, chunks, fill, attributes: s, endian, compressor });
  }

  /** [chunk shape, chunks] of an implicitly indexed dataset (§5.4) whose chunks
   * are its elements in row-major order: cut as contiguous values; or whose
   * chunk shape is 1 but along its last dimension: a chunk per row, its chunks'
   * adjacent runs one range. Undefined for any other. */
  private async implicit(ds: Dataset, shape: number[], item: number, size: number): Promise<[number[], Map<string, Part[] | Uint8Array>] | undefined> {
    const dims = ds.dims!, rank = dims.length;
    if (ds.indexAddress === undefined || dims.includes(0)) return undefined;
    const maxgrid = this.f.implicit(ds);
    const nbytes = product(ds.chunk) * size;
    if (rank === 0 || (maxgrid.slice(1).every((m, i) => m === ds.grid[i + 1]) && rowMajor(ds))) {
      return gridChunks(ds.indexAddress, shape, item, this.read);
    }
    const n = dims[rank - 1] * size;
    if (ds.chunk.slice(0, -1).some((c) => c !== 1) || ds.grid[rank - 1] < 2 || n > MAX_CHUNK) return undefined;
    const chunks = new Map<string, Part[] | Uint8Array>();
    const tail = new Array(shape.length - rank).fill(0);
    const outer = ds.grid.slice(0, -1);
    for (let i = 0; i < product(outer); i++) {
      const coords = unravel(i, outer);
      chunks.set(joinKey([...coords, 0, ...tail]), [[ds.indexAddress + ravel([...coords, 0], maxgrid) * nbytes, n]]);
    }
    return [[...new Array(rank - 1).fill(1), dims[rank - 1], ...shape.slice(rank)], chunks];
  }

  /** Elements that hold addresses as an array (§5.4): a family of their
   * variable-length values, the indexes of the objects they refer to in the
   * object table, or a family of region references. Element `i` of the
   * `count` is the bytes of `data` at `i * stride + offset`, read in place
   * (no copy or view of each is kept: memory per element is its result's). */
  private async column(
    path: string, t: Type, kind: string, data: Uint8Array, stride: number, offset: number, count: number,
    dims: number[], s: Record<string, Json> | undefined,
  ): Promise<void> {
    const n: number = t.size;
    const element = (i: number) => data.subarray(i * stride + offset, i * stride + offset + n);
    if (kind === "family") {
      const members: [number, number, number][] = [];
      for (let i = 0; i < count; i++) members.push([i, ...(await this.heapObject(t, element(i)))]);
      this.family(path, await familyPlans(path, members, this.read, count), s);
    } else if (kind === "paths") {
      this.jobs.push(["index", path, Array.from({ length: count }, (_, i) => refAddress(element(i))), dims, s, true]);
    } else {
      const regions: Region[] = [];
      for (let i = 0; i < count; i++) regions.push(await this.region(element(i)));
      this.jobs.push(["regions", path, regions, s]);
    }
  }

  /** A compound dataset with members that hold addresses (§5.4): the group of its
   * bytes, those members' zeroed, and of each such member's array. */
  private async compound(ds: Dataset, t: Type, path: string, count: number, s: Record<string, Json>): Promise<void> {
    const members: [number, number, Type, string][] = [];
    (t.members as Type[]).forEach((m, i) => {
      if (!holdsAddresses(m.type)) return;
      const kind = column(m.type);
      if (kind === undefined || m.offset + m.type.size > t.size) {
        reject("a compound member that holds addresses other than as variable-length data or a reference");
      }
      members.push([i, m.offset, m.type, kind!]);
    });
    const n: number = t.size;
    if (count * n > MAX_COPY) reject("a compound of more than 2^24 bytes");
    const data = (await this.stored(ds, count * n)).slice();
    for (const [i, o, mt, kind] of members) await this.column(`${path}/${i}`, mt, kind, data, n, o, count, ds.dims!, undefined);
    for (const [, o, mt] of members) for (let k = 0; k < count; k++) data.fill(0, k * n + o, k * n + o + mt.size);
    this.attributeArray(`${path}/data`, "uint8", [...ds.dims!, n], 1, data, "little", undefined, this.plans);
    this.groups.push([path, s]);
  }

  /** A family (conventions §7) with its source metadata: on its 2-D array, or on its group. */
  private family(path: string, plans: Plan[], s: Record<string, Json> | undefined): void {
    if (plans.length && plans[0].path === path) plans[0].attributes = s;
    else this.groups.push([path, s ?? {}]);
    this.plans.push(...plans);
  }

  /** A chunk's bytes, its filters undone: Fletcher32's checksum removed, deflate
   * inflated (one zlib stream of the chunk's size) and the shuffle undone, but
   * for the filters its filter mask skips. */
  private async decodeChunk(ds: Dataset, data: Uint8Array, nbytes: number, mask: number): Promise<Uint8Array> {
    if (!ALLOWED.includes(ds.filters.join(","))) reject(`unsupported HDF5 filters [${ds.filters.join(", ")}]`);
    const filters = ds.filters.filter((_, i) => !(i < 32 && (mask >>> i) & 1));
    if (filters.includes(3)) data = data.length > 4 ? data.subarray(0, data.length - 4) : new Uint8Array(0);
    if (filters.includes(1)) data = await inflate(data, nbytes);
    if (data.length !== nbytes) reject("a chunk that does not decode to its size");
    return filters.includes(2) ? unshuffle(data, ds.datatype.size) : data;
  }

  /** The whole data of a dataset of `size` bytes, read and decoded. */
  async stored(ds: Dataset, size: number): Promise<Uint8Array> {
    const f = this.f;
    if (ds.layout === "compact") {
      if (ds.compact.length < size) reject("truncated compact dataset");
      return ds.compact.subarray(0, size);
    }
    if (ds.layout === "contiguous") {
      if (ds.dataAddress === undefined) return new Uint8Array(size);
      if (ds.dataAddress + size > f.size) reject("data outside the file");
      return this.read(ds.dataAddress, size);
    }
    if (!ALLOWED.includes(ds.filters.join(","))) reject(`unsupported HDF5 filters [${ds.filters.join(", ")}]`);
    const e = ds.datatype.size;
    const chunkBytes = product(ds.chunk) * e;
    if (chunkBytes > MAX_COPY) reject("a chunk of more than 2^24 bytes to decode");
    const out = new Uint8Array(size);
    const masks = new Map<string, number>();
    const found = [...(await f.chunks(ds, masks))].map(([k, ref]) => [splitKey(k), ref, masks.get(k) ?? 0] as [number[], [number, number], number]);
    found.sort(([a], [b]) => {
      for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] - b[i];
      return 0;
    });
    for (const [key, [at, n], mask] of found) {
      const data = await this.decodeChunk(ds, await this.read(at, n), chunkBytes, mask);
      scatter(out, ds.dims!, e, data, ds.chunk, key.map((c, i) => c * ds.chunk[i]));
    }
    return out;
  }

  // ---- unsupported objects (§5.5)

  /** What can be read of an object that is not mapped: its datatype, shape,
   * fill value, external files or virtual mappings, and attributes. */
  async unsupported(at: number, reason: string): Promise<[() => Record<string, Json>, Item[] | null]> {
    const f = this.f;
    const record: Record<string, Json> = {};
    let messages: Message[];
    try {
      messages = await f.header(at);
    } catch (e) {
      if (e instanceof ImsError) return [() => ({ reason }), []];
      throw e;
    }
    const types = new Set(messages.map((m) => m.type));
    let typed: [Type, number | undefined] | undefined;
    const attempt = async (step: () => Promise<void> | void) => {
      try {
        await step();
      } catch (e) {
        if (!(e instanceof ImsError)) throw e;
      }
    };
    if (types.has(LAYOUT) || types.has(DATATYPE)) {
      await attempt(async () => {
        const [typeMessage, named] = await f.datatype(messages);
        typed = [describeType(typeMessage)[0], named];
      });
    }
    if (types.has(LAYOUT)) {
      await attempt(() => {
        record.shape = parseDataspace(Hdf5.message(messages, DATASPACE, true)).dims;
      });
      await attempt(() => {
        const message = Hdf5.message(messages, FILL_VALUE, false);
        const fill = message !== undefined ? fillValue(message) : new Uint8Array(0);
        if (fill.length && typed !== undefined && !holdsAddresses(typed[0])) record.fill = base64(fill);
      });
      await attempt(async () => {
        const efl = Hdf5.message(messages, EXTERNAL_FILES, false);
        if (efl !== undefined) record.external = await this.externalFiles(efl);
      });
      await attempt(async () => {
        const layout = Hdf5.message(messages, LAYOUT, true);
        if (u(layout, 1, 1) === 3) record.virtual = (await f.virtualMapping(layout)) as Json;
      });
    }
    const make = () => ({ reason, ...(typed !== undefined ? this.typePart(...typed) : {}), ...record });
    return [make, await this.attributeItems(at)];
  }

  /** The files of an external data files message (§8.9). */
  private async externalFiles(d: Uint8Array): Promise<Json[]> {
    if (u(d, 0, 1) !== 1) reject("unsupported external data files message version");
    const used = u(d, 6, 2), heap = address(le(d, 8, 8));
    if (heap === undefined) return reject("external data files without a local heap");
    const names = await this.f.localHeap(heap);
    const out: Json[] = [];
    for (let i = 0; i < used; i++) {
      const p = 16 + 24 * i;
      const offset = le(d, p, 8);
      const end = offset < BigInt(names.length) ? names.indexOf(0, Number(offset)) : -1;
      if (end < 0) reject("an external file name outside the local heap");
      out.push({
        file: textJson(names.subarray(Number(offset), end)), offset: jsonNumber(le(d, p + 8, 8)),
        size: jsonNumber(le(d, p + 16, 8)),
      });
    }
    return out;
  }

  // ---- groups (§5.1)

  async kind(at: number): Promise<string> {
    const types = new Set((await this.f.header(at)).map((m) => m.type));
    if (types.has(LAYOUT)) return "dataset";
    if (types.has(SYMBOL_TABLE) || types.has(LINK_INFO)) return "group";
    if (types.has(DATATYPE)) return "datatype";
    return reject("an object that is not a group, a dataset or a named datatype");
  }

  async walk(): Promise<void> {
    const f = this.f;
    this.meet(f.root, "/", undefined, "");
    this.objects = 1; // the root group is the first object
    const stack: [number, string, string][] = [[f.root, "/", "hdf5"]];
    while (stack.length) {
      const [at, path, zpath] = stack.pop()!;
      const s: Record<string, Json> = {};
      const doc = this.doc(s);
      doc.entries.push(["attrs", await this.attributeItems(at)]);
      const links = await f.links(at);
      const keys = new Set<string>();
      let overflow = 0;
      const children: [number, string, string][] = [];
      for (const name of [...links.keys()].sort()) {
        const bytes = bytesOf(name);
        const key = decodeText(bytes), text = textJson(bytes);
        if (keys.has(key)) {
          doc.entries.push(["list", "collisions", text]); // reads like an earlier name
          continue;
        }
        keys.add(key);
        if (typeof text !== "string") doc.entries.push(["keyed", "names", key, text]);
        const target = links.get(name)!;
        const child = (path !== "/" ? path : "") + "/" + key, zchild = `${zpath}/${key}`;
        if ("soft" in target) {
          doc.entries.push(["keyed", "links", key, { soft: textJson(bytesOf(target.soft)) }]);
        } else if ("other" in target) {
          doc.entries.push(["keyed", "links", key, otherLink(target.other, target.value)]);
        } else if (this.seen.has(target.hard)) {
          doc.entries.push(["keyed", "links", key, { hard: this.order.get(target.hard)! }]);
        } else if (this.objects >= MAX_OBJECTS + PER_IMAGE * this.images.size) {
          overflow++;
        } else {
          this.objects++;
          this.meet(target.hard, child, at, key);
          try {
            await this.member(target.hard, child, zchild, key, doc, children);
          } catch (e) {
            if (!(e instanceof ImsError)) throw e;
            doc.entries.push(["member", "unsupported", key, ...(await this.unsupported(target.hard, e.message))]);
          }
        }
      }
      if (overflow) s.overflow = overflow;
      this.groups.push([zpath, s]);
      for (const child of children.reverse()) stack.push(child);
    }
    await this.finish();
  }

  /** A group's member: a named datatype, an image dataset, or (named as a Zarr
   * node) a group (walked later) or a dataset. */
  async member(at: number, path: string, zpath: string, key: string, doc: Doc, children: [number, string, string][]): Promise<void> {
    const kind = await this.kind(at);
    if (kind === "datatype") {
      const t = describeType(Hdf5.message(await this.f.header(at), DATATYPE, true))[0];
      doc.entries.push(["member", "datatypes", key, () => ({ datatype: t }), await this.attributeItems(at)]);
      return;
    }
    const image = this.images.get(at);
    if (image !== undefined) {
      doc.entries.push(["member", "images", key, () => ({ ...image }), await this.attributeItems(at)]);
      return;
    }
    if (!nodeName(key)) reject("the name is not a Zarr node name");
    if (kind === "group") {
      await this.f.links(at);
      children.push([at, path, zpath]);
      return;
    }
    const mark = [this.plans.length, this.groups.length, this.docs.length, this.jobs.length, this.attributePlans.length,
      this.attributeGroups.length, this.selections];
    try {
      await this.dataset(at, zpath);
    } catch (e) {
      // Undo what the dataset that failed added, and the selections it read.
      this.selections = mark[6];
      this.plans.length = mark[0];
      this.groups.length = mark[1];
      this.docs.length = mark[2];
      this.jobs.length = mark[3];
      this.attributePlans.length = mark[4];
      this.attributeGroups.length = mark[5];
      throw e;
    }
  }

  /** The source metadata node's entries. */
  entries(): Entry[] {
    const group = (key: string, attributes: Record<string, unknown>): Entry =>
      ({ key: `${key}/zarr.json`, bytes: utf8.encode(stringifyJson({ zarr_format: 3, node_type: "group", attributes })), compress: true });
    const out: Entry[] = [group(SOURCE_NODE, {})];
    for (const [path, s] of this.groups) {
      out.push(group(`${SOURCE_NODE}/${path}`, Object.keys(s).length ? declare({}, "ims", undefined, s) : {}));
    }
    if (this.arrays) out.push(group(`${SOURCE_NODE}/attributes`, {}));
    if (this.spills) out.push(group(`${SOURCE_NODE}/spilled`, {}));
    out.push(group(`${SOURCE_NODE}/objects`, {}));
    for (const path of this.attributeGroups) out.push(group(`${SOURCE_NODE}/${path}`, {}));
    for (const a of [...this.plans, ...this.attributePlans]) {
      const entries = planEntries(SOURCE_NODE, {
        ...a, attributes: a.attributes && Object.keys(a.attributes).length ? declare({}, "ims", undefined, a.attributes) : undefined,
      });
      for (const e of entries) out.push(e); // one at a time: an array may have more chunks than the stack holds arguments
    }
    return out;
  }
}

/** Whether chunks of `ds` in row-major order hold its elements in row-major
 * order: a chunk shape of 1 before some dimension `a` and of the whole dimension
 * after it, and `a`'s size a multiple of its chunk shape unless the dimensions
 * before it are all 1 (the maximum grid aside, §5.4). */
function rowMajor(ds: Dataset): boolean {
  const dims = ds.dims!;
  let a = 0;
  dims.forEach((n, i) => {
    if (ds.chunk[i] !== n) a = i;
  });
  if (ds.chunk.slice(0, a).some((c) => c !== 1)) return false;
  return dims[a] % ds.chunk[a] === 0 || product(dims.slice(0, a)) === 1;
}

/** A variable-length element whose base holds no addresses, as JSON. */
function vlenValue(t: Type, data: Uint8Array): Json {
  if (t.string) return textJson(data);
  const values = exactNumbers(t.base, t.base.size ? Math.floor(data.length / t.base.size) : 0, data);
  return values !== undefined ? values : base64(data);
}

function dataForm(dims: number[], data: Uint8Array): Record<string, Json> {
  return { data: base64(data), ...(dims.length !== 1 ? { shape: dims } : {}) };
}

export async function sourceTree(f: Hdf5, read: ByteReader, images: Map<number, Record<string, Json>>): Promise<Tree> {
  const tree = new Tree(f, read, images);
  await tree.walk();
  return tree;
}
