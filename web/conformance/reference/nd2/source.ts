// An ND2 file's metadata chunks as JSON, and its numeric streams as arrays:
// the source metadata of the ND2 convention (conventions/nd2/README.md §5).

import {
  blobChunks, type ByteReader, declare, decodeText, familyPlans, gridChunks, latin1, type Part, type Plan, setMember,
  textJson, stringifyJson,
} from "../../../src/virtualize/common.ts";
import { DECIMAL, scan } from "../tiff/virtualize.ts";
import { decodeLV, type LV, LVError, RECORDS } from "./lv.ts";

// Decoded JSON keeps its objects as Maps, in the order of their members (a
// plain object would move integer-like names first), until `plain` turns it
// into the output's plain objects.

const CHUNK_MAGIC = 0x0abeceda;
const MAX_DATA_BYTES = 2 ** 26; // the most chunk data, inflated, read per file
const MAX_ROOT_JSON = 2 ** 14; // a decoded chunk with more JSON than this is on vzip_source
const MAX_ROOT_TOTAL = 2 ** 16; // the most JSON of the root's chunks
const MAX_NODE_TOTAL = 2 ** 16; // the most JSON of vzip_source's source metadata
const NODE_RESERVE = 128; // of it, what its chunks leave for its braces, keys, and other and empty as paths
const LISTS: Record<string, string> = { other: "other/names", empty: "other/empty" };
const MAX_COPIED = 2 ** 16; // the chunk limit of an array of copied values (conventions/README.md §7)
const MAX_FAMILY = 2 ** 24; // a family member's index is less than this
const MAX_STAMPS = 2 ** 20; // the frame times' written chunks hold at most this many bytes, or 512 per placed frame
const MAX_TAG_JSON = 2 ** 14; // the most JSON of a declared stream's tag (conventions/nd2/README.md §5.2)
const DOCUMENTS = new Set(["zarr.json", ".zarray", ".zgroup"]); // names a path segment cannot have
const MAX_CHUNK = 2 ** 24; // the largest chunk of contiguous values (conventions/README.md §7)
const MAX_PADDING = 2n ** 16n - 2n ** 10n; // the most zero bytes an edge chunk's reference holds
const SMALL_PADDING = 2n ** 10n; // edge padding small enough to take the fewest chunks
const MAX_TOTAL_PADDING = 2n ** 20n; // the most zero bytes all edge chunks of a cut hold
const TAGS = new Set(["utf16", "int", "float"]); // the names of the tags (conventions/nd2/README.md §5.1)
const MAX_SAFE = BigInt(Number.MAX_SAFE_INTEGER);
// Streams that every ND2 writer uses without declaring them: one value per frame.
export const STREAMS: Record<string, string> = {
  AcqTimesCache: "float64", AcqTimes2Cache: "float64", X: "float64", Y: "float64",
  Z: "float64", Z1: "float64", Z2: "float64", AcqFramesCache: "int32",
};
const DECLARED: Record<number, string> = { 2: "int32", 3: "float64" }; // CustomTagDescription Type
export const SIZES: Record<string, number> = { int32: 4, float64: 8 };
const INTS = new Set(["", "u"].flatMap((s) => [8, 16, 32, 64].map((b) => `lx_${s}int${b}`)));
const FRAME = /^ImageDataSeq\|(0|[1-9][0-9]*)!$/;
const FAMILY = /^CustomDataSeq\|(.+)\|(0|[1-9][0-9]*)!$/s;
const DECLARATIONS = "CustomDataVar|CustomDataV2_0!";
const INDEX = /^(0|[1-9][0-9]*)$/; // a name that is an array index: a plain object would order it first
const OTHER = "other"; // the reserved path of the chunks that have no path of their own

// ---- JSON (conventions/nd2/README.md §5.1)

const pairLike = (j: unknown) => Array.isArray(j) && j.length === 2 && typeof j[0] === "string";

/** Named values as an object (a Map), or as [name, value] pairs when a name repeats,
 * a name is an array index (0, 1, ..., without leading zeros), or the object would
 * read as a tag (one member named utf16, int or float). */
function pairsOrObject(records: [string, unknown][]): unknown {
  if (new Set(records.map(([n]) => n)).size < records.length || (records.length === 1 && TAGS.has(records[0][0]))
    || records.some(([n]) => INDEX.test(n))) {
    return records.map(([n, v]) => [n, v]);
  }
  return new Map(records);
}

/** Decoded JSON with its Maps as plain objects, for the output. */
export function plain(v: unknown): unknown {
  if (v instanceof Map) {
    const o: Record<string, unknown> = {};
    for (const [k, x] of v) setMember(o, k, plain(x));
    return o;
  }
  return Array.isArray(v) ? v.map(plain) : v;
}

/** A number as JSON: itself, or a tag when JSON cannot hold it exactly (conventions/nd2/README.md §5.1). */
function numberJson(v: number | bigint): unknown {
  if (typeof v === "bigint") return v <= MAX_SAFE && v >= -MAX_SAFE ? Number(v) : { int: v.toString() };
  if (Number.isNaN(v)) return { float: "NaN" };
  if (!Number.isFinite(v)) return { float: v > 0 ? "Infinity" : "-Infinity" };
  if (Object.is(v, -0)) return { float: "-0" }; // JSON.stringify writes -0 as 0
  return v;
}

/** An LV value as JSON (objects as Maps): objects (or pairs), lists, and scalars by their type. */
function lvTree(v: LV): unknown {
  if (v instanceof Uint8Array) return Array.from(v);
  if (Array.isArray(v)) {
    const items = v.map(lvTree);
    // A list that would read as pairs is written as pairs with empty names.
    return items.length > 0 && items.every(pairLike) ? items.map((j) => ["", j]) : items;
  }
  if (v instanceof Map) return pairsOrObject((RECORDS.get(v) ?? [...v]).map(([k, x]) => [k, lvTree(x)]));
  if (v.type === 8) return v.exact ?? v.value;
  if (v.type === 1) return v.value;
  return numberJson(v.value as number | bigint);
}

/** An LV value as JSON (conventions/nd2/README.md §5.1). */
export const lvJson = (v: LV): unknown => plain(lvTree(v));

const utf8 = new TextEncoder();
/** The length in UTF-8 bytes of `v` as JSON.stringify writes it (conventions/nd2/README.md §5.1). */
export const jsonSize = (v: unknown) => utf8.encode(JSON.stringify(v)).length;

// ---- XML variants (conventions/nd2/README.md §5.1)

function scalar(runtype: string | undefined, value: string): unknown {
  if (runtype !== undefined && INTS.has(runtype) && /^[+-]?[0-9]+$/.test(value)) {
    const sign = value[0] === "-" ? "-" : "";
    const digits = value.replace(/^[+-]/, "").replace(/^0+/, "") || "0";
    return digits.length <= 20 ? numberJson(BigInt(sign + digits)) : { int: sign + digits };
  }
  if ((runtype === "double" || runtype === "float") && DECIMAL.test(value)) return numberJson(Number(value));
  if (runtype === "bool" && (value === "true" || value === "false")) return value === "true";
  return value;
}

const NO_VALUE = Symbol("no value");
const MAX_XML_DEPTH = 100; // the variant element is at depth 0

/** An XML variant document (<variant> of elements with runtype and value) as JSON, or undefined. */
export function variantJson(data: Uint8Array): unknown {
  const tree = variantTree(data);
  return tree === undefined ? undefined : plain(tree);
}

/** variantJson with its objects as Maps. */
function variantTree(data: Uint8Array): unknown {
  let xml: string;
  try {
    xml = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(data);
  } catch {
    return undefined;
  }
  const stack: [string, unknown, [string, unknown][]][] = []; // name, value attribute or NO_VALUE, children
  let root: unknown;
  for (const tag of scan(xml).tags) {
    if (root !== undefined) return undefined; // content after the document element
    const own = (k: string) => Object.hasOwn(tag.attrs, k);
    let value: unknown;
    let children: [string, unknown][] = [];
    if (tag.closing) {
      if (stack.length === 0 || stack[stack.length - 1][0] !== tag.name) return undefined;
      [, value, children] = stack.pop()!;
    } else {
      if (stack.length > MAX_XML_DEPTH) return undefined; // nested too deep
      value = own("value") ? scalar(own("runtype") ? tag.attrs.runtype : undefined, tag.attrs.value) : NO_VALUE;
      if (!tag.selfClosing) {
        stack.push([tag.name, value, children]);
        continue;
      }
    }
    const isElement = value === NO_VALUE;
    if (isElement) value = pairsOrObject(children);
    if (stack.length > 0) {
      stack[stack.length - 1][2].push([tag.name, value]); // (the children of an element with a value are not kept)
    } else if (tag.name === "variant" && isElement) {
      root = value;
    } else {
      return undefined;
    }
  }
  return stack.length === 0 ? root : undefined;
}

/** A JSON object (decoded: a Map, or a tag), or pairs read as an object (first
 * position, last value), as a Map in member order; undefined otherwise. */
function asObject(v: unknown): Map<string, unknown> | undefined {
  if (Array.isArray(v)) {
    if (v.length === 0 || !v.every(pairLike)) return undefined;
    const o = new Map<string, unknown>();
    for (const [n, x] of v as [string, unknown][]) o.set(n, x);
    return o;
  }
  if (v instanceof Map) return v as Map<string, unknown>;
  return typeof v === "object" && v !== null ? new Map(Object.entries(v)) : undefined;
}

/** ID -> [data type, CustomTagDescription member, its index among the members]
 * of the streams CustomDataV2_0 declares (§5.2). */
function declaredStreams(doc: unknown): Map<string, [string, unknown, number]> {
  const out = new Map<string, [string, unknown, number]>();
  const tags = asObject(asObject(doc)?.get("CustomTagDescription_v1.0"));
  let i = 0;
  for (const tag of tags?.values() ?? []) {
    const index = i++;
    const t = asObject(tag);
    if (t === undefined) continue;
    const id = t.get("ID");
    const type = t.get("Type");
    if (typeof id === "string" && (type === 2 || type === 3) && !out.has(id)) {
      out.set(id, [DECLARED[type], tag, index]);
    }
  }
  return out;
}

// ---- paths (conventions/nd2/README.md §5.3)

const segment = (s: string) =>
  s.replace(/\./g, "") !== "" && !s.startsWith("__") && !s.includes("/") && !s.includes("\0") && !DOCUMENTS.has(s);

/** A chunk name's path under vzip_source: `A|B!` is `A/B` (undefined if not a valid path). */
export function nodePath(name: string): string | undefined {
  const parts = (name.endsWith("!") ? name.slice(0, -1) : name).split("|");
  return parts.every(segment) ? parts.join("/") : undefined;
}

/** The paths taken under vzip_source, each a leaf. A path is free when it is valid,
 * not taken, and neither an ancestor nor a descendant of one taken. `other` is taken
 * from the start. */
class Paths {
  leaves = new Set([OTHER]);
  ancestors = new Set<string>();
  free(path: string | undefined): path is string {
    if (path === undefined || this.leaves.has(path) || this.ancestors.has(path)) return false;
    const parts = path.split("/");
    for (let k = 1; k < parts.length; k++) if (this.leaves.has(parts.slice(0, k).join("/"))) return false;
    return true;
  }
  take(path: string) {
    this.leaves.add(path);
    const parts = path.split("/");
    for (let k = 1; k < parts.length; k++) this.ancestors.add(parts.slice(0, k).join("/"));
  }
}

// ---- the chunks

export interface Source {
  root: Record<string, unknown>;
  node: Record<string, unknown>;
  arrays: Plan[];
}

/** What the image takes from the frames (profiles/nd2.md §5.3). */
export interface Frames {
  count: number; // N
  placed: Map<number, number>; // each placed frame's chunk offset
  stamps: Map<number, number>; // each placed frame's timestamp offset
  nameLength?: number; // uncompressed: the name length of every frame
  pixels?: number; // uncompressed: the bytes of pixels after the timestamp
}

/** Chunks that grow with the frames: the events, and per-frame metadata after frame 0. */
function frameScaled(name: string): boolean {
  if (name === "ImageEventsLV!" || name === "CustomData|ExperimentEventsV1_0!") return true;
  const m = /\|([0-9]+)!$/.exec(name);
  return m !== null && name.startsWith("ImageMetadataSeqLV|") && /[1-9]/.test(m[1]); // n >= 1
}

interface GridLoop { kind: string; count: number; flip?: boolean }

/** floor(a / b) and a mod b, exact for integers up to 2^53 - 1. */
function divmod(a: number, b: number): [number, number] {
  let q = Math.floor(a / b);
  if (q * b > a) q--;
  else if ((q + 1) * b <= a) q++;
  return [q, a - q * b];
}

/** Frame f's index in the frame grid, row-major over the loops, with a flipped
 * z index when the z loop flips (conventions/nd2/README.md §4.3). (The flip is
 * its own inverse: the frame at grid index g is gridIndex(g).) */
export function gridIndex(loops: GridLoop[], f: number): number {
  const coords: number[] = [];
  let rest = f;
  for (let i = loops.length - 1; i >= 0; i--) {
    const [q, c] = divmod(rest, loops[i].count);
    coords[i] = loops[i].flip ? loops[i].count - 1 - c : c;
    rest = q;
  }
  let g = 0;
  for (let i = 0; i < loops.length; i++) g = g * loops[i].count + coords[i];
  return g;
}

/** The cut axis `a` and the chunk length `c` along it that conventions/README.md §7
 * gives contiguous values of `shape` with the chunk limit `limit` (as common's
 * planGrid cuts them), without listing the chunks: of the counts with the same
 * `c` (and so the same padding), only the first is tried. Exact, in bigints. */
export function gridCut(shape: number[], item: number, limit = MAX_CHUNK): [number, number] {
  const s = shape.map(BigInt);
  const cdiv = (x: bigint, y: bigint) => (x + y - 1n) / y;
  const total = s.reduce((p, v) => p * v, BigInt(item));
  let a = 0;
  let slab = s.slice(1).reduce((p, v) => p * v, BigInt(item));
  while (slab > BigInt(limit)) slab /= s[++a];
  for (;;) {
    const n = s[a];
    const outer = s.slice(0, a).reduce((p, v) => p * v, 1n); // the edge chunks
    const k = cdiv(n, BigInt(limit) / slab);
    let best: [bigint, bigint] | undefined; // padding, c: the first count padding at most SMALL_PADDING, else the least
    for (let q = k; q <= (2n * k < n ? 2n * k : n);) {
      const c = cdiv(n, q);
      const pad = (cdiv(n, c) * c - n) * slab;
      if (best === undefined || pad < best[0]) best = [pad, c];
      if (pad <= SMALL_PADDING || c === 1n) break;
      q = cdiv(n, c - 1n); // the first count with a smaller c
    }
    const most = total / 64n > MAX_TOTAL_PADDING ? total / 64n : MAX_TOTAL_PADDING;
    if (best![0] <= MAX_PADDING && best![0] * outer <= most) return [a, Number(best![1])];
    if (a === shape.length - 1) return [a, Number(cdiv(n, k))];
    slab /= s[++a];
  }
}

/** The chunk of grid index `g` under the cut [a, c]: its coords joined by "/", the
 * element's index within it, and the elements of the chunk that lie within the array. */
function gridChunk(shape: number[], a: number, c: number, g: number): [string, number, number] {
  const inner = shape.slice(a + 1).reduce((p, v) => p * v, 1);
  const n = shape[a];
  let [outer, within] = divmod(g, n * inner);
  const [i, rest] = divmod(within, inner);
  const coords: number[] = [];
  for (let j = a - 1; j >= 0; j--) {
    const [q, x] = divmod(outer, shape[j]);
    coords.unshift(x);
    outer = q;
  }
  const q = Math.floor(i / c);
  return [[...coords, q, ...new Array(shape.length - a - 1).fill(0)].join("/"), (i - q * c) * inner + rest,
    Math.min(c, n - q * c) * inner];
}

/** A chunk whose name says it holds LV data (conventions/nd2/README.md §5.1). */
const lvNamed = (name: string) => name.endsWith("LV!") || name.includes("LV|");

const bytesOf = (latin: string) => Uint8Array.from(latin, (c) => c.charCodeAt(0));
/** Decimal digits as a bigint, or undefined when there are more than 20 (at least 10^20). */
const smallInt = (digits: string) => (digits.length <= 20 ? BigInt(digits) : undefined);

type Kept = { kind: "bytes"; name: string; start: number; d: number } | { kind: "family"; key: string };
type Member = [number, number, number, string]; // index, offset, length, chunk name

/** A declared stream's source metadata: its member, or past 16 KiB of JSON its
 * index among the members (conventions/nd2/README.md §5.2). */
function tagOf([, tag, index]: [string, unknown, number]): Record<string, unknown> {
  const value = plain(tag);
  return jsonSize(value) <= MAX_TAG_JSON ? { tag: value } : { tag_index: index };
}

/** The chunk map's chunks (conventions/nd2/README.md §5), in map order, sharing one budget. */
export async function sourceMetadata(
  read: ByteReader,
  size: number,
  chunks: Map<string, number>,
  loops: GridLoop[],
  frames: Frames,
): Promise<Source> {
  const out: Source = { root: {}, node: {}, arrays: [] };
  const n = frames.count;
  const grid = loops.length > 0 ? loops.map((l) => l.count) : [1];
  const dims = loops.length > 0 ? loops.map((l) => l.kind) : ["frame"];
  const flipped = loops.some((l) => l.flip);
  const position = new Map([...chunks.keys()].map((name, i) => [name, i])); // map order

  const dataOf = async (offset: number): Promise<[number, number] | undefined> => {
    if (offset + 16 > size) return undefined;
    const head = await read(offset, 16);
    const view = new DataView(head.buffer, head.byteOffset, head.byteLength);
    if (view.getUint32(0, true) !== CHUNK_MAGIC) return undefined;
    const start = offset + 16 + view.getUint32(4, true);
    const big = view.getBigUint64(8, true);
    return BigInt(start) + big > BigInt(size) ? undefined : [start, Number(big)];
  };

  let used = 0;
  /** A chunk decoded within the budget (its JSON with Maps), or undefined. Every attempt is charged. */
  const decode = async (name: string, start: number, d: number): Promise<unknown> => {
    if (d === 0 || used + d > MAX_DATA_BYTES) return undefined;
    const data = await read(start, d);
    const inflated: number[] = [];
    let value: unknown;
    if (name.startsWith("CustomDataVar|")) {
      value = variantTree(data);
    } else {
      try {
        // A CustomData chunk holds LV data only by guess: it decodes only when its JSON keeps every byte.
        // LV data of more records and array bytes than vzip_source's budget has JSON too large for it.
        value = lvTree(await decodeLV(data, MAX_DATA_BYTES - used - d, inflated, !lvNamed(name), MAX_NODE_TOTAL));
      } catch (e) {
        if (!(e instanceof LVError)) throw e;
      }
    }
    used += d + inflated.reduce((a, b) => a + b, 0);
    return value;
  };

  // The declarations first (§5.2): they say which chunks are streams.
  const decoded = new Map<string, unknown>();
  const declarationsAt = chunks.has(DECLARATIONS) ? await dataOf(chunks.get(DECLARATIONS)!) : undefined;
  if (declarationsAt !== undefined) decoded.set(DECLARATIONS, await decode(DECLARATIONS, ...declarationsAt));
  const declared = declaredStreams(decoded.get(DECLARATIONS));
  const streamIds = [...Object.keys(STREAMS), ...[...declared.keys()].filter((i) => !Object.hasOwn(STREAMS, i))];
  const streamNames = new Map(streamIds.map((sid) => [latin1(utf8.encode(`CustomData|${sid}!`)), sid]));

  const empty: string[] = [];
  const other: [string, number, number][] = []; // chunk name, data offset, length
  const beyond: Member[] = []; // frames f >= N: f - N, offset, length, chunk name
  const families = new Map<string, Member[]>();
  const kept: Kept[] = [];
  const streams = new Map<string, [string, number, number]>();
  const texts = new Set<string>();
  const chunkJson = new Map<string, unknown>(); // in map order, as plain JSON
  const chunkAt = new Map<string, [number, number]>(); // a decoded chunk's data offset and length
  for (const [name, offset] of chunks) {
    const frame = FRAME.exec(name);
    const f = frame ? smallInt(frame[1]) : undefined;
    if (f !== undefined && f < BigInt(n)) continue; // a placed frame: the image; its timestamp and trailing bytes below
    const at = await dataOf(offset);
    if (at === undefined) continue;
    const [start, d] = at;
    const isFrame = name.startsWith("ImageDataSeq|");
    const text = decodeText(bytesOf(name));
    const fm = FAMILY.exec(name);
    if (d === 0) {
      empty.push(name);
    } else if (isFrame) {
      if (f !== undefined && f - BigInt(n) < BigInt(MAX_FAMILY)) beyond.push([Number(f - BigInt(n)), start, d, name]);
      else other.push([name, start, d]);
    } else if (texts.has(text)) {
      other.push([name, start, d]); // a name that reads like an earlier one
    } else if (fm !== null) {
      if (fm[2].length <= 8 && Number(fm[2]) < MAX_FAMILY) {
        if (!families.has(fm[1])) {
          families.set(fm[1], []);
          kept.push({ kind: "family", key: fm[1] });
        }
        families.get(fm[1])!.push([Number(fm[2]), start, d, name]);
      } else {
        other.push([name, start, d]);
      }
    } else if (streamNames.has(name)) {
      streams.set(streamNames.get(name)!, [name, start, d]);
    } else {
      let value: unknown;
      if (lvNamed(name) || name.startsWith("CustomDataVar|") || name.startsWith("CustomData|")) {
        value = decoded.has(name) ? decoded.get(name) : await decode(name, start, d);
      }
      if (value !== undefined) {
        chunkJson.set(name, plain(value));
        chunkAt.set(name, [start, d]);
      } else {
        kept.push({ kind: "bytes", name, start, d });
      }
    }
    if (!isFrame) texts.add(text);
  }

  // Decoded chunks: on vzip_source when they grow with the frames or are large;
  // then, while the root's chunks have more than 64 KiB of JSON, the largest
  // of them (the first in map order of equal sizes) moves too.
  const sizes = new Map([...chunkJson].map(([name, value]) => [name, jsonSize(value)]));
  const onNode = new Set([...chunkJson.keys()].filter((name) => frameScaled(name) || sizes.get(name)! > MAX_ROOT_JSON));
  const members = new Map([...chunkJson.keys()].filter((name) => !onNode.has(name))
    .map((name) => [name, jsonSize(decodeText(bytesOf(name))) + 1 + sizes.get(name)!]));
  let total = [...members.values()].reduce((a, b) => a + b, 0);
  let count = members.size;
  const largest = [...members.keys()].sort((x, y) => members.get(y)! - members.get(x)! || position.get(x)! - position.get(y)!);
  for (const name of largest) {
    if (2 + total + Math.max(0, count - 1) <= MAX_ROOT_TOTAL) break;
    onNode.add(name);
    total -= members.get(name)!;
    count--;
  }
  // vzip_source's chunks, in map order, each while they fit in 64 KiB of JSON;
  // a chunk that does not is kept as bytes.
  let room = MAX_NODE_TOTAL - NODE_RESERVE - 2;
  for (const [name, value] of chunkJson) {
    if (!onNode.has(name)) {
      setMember(out.root, decodeText(bytesOf(name)), value);
      continue;
    }
    const has = Object.hasOwn(out.node, "chunks");
    const cost = jsonSize(decodeText(bytesOf(name))) + 1 + sizes.get(name)! + (has ? 1 : 0);
    if (cost <= room) {
      room -= cost;
      if (!has) out.node.chunks = {};
      setMember(out.node.chunks as Record<string, unknown>, decodeText(bytesOf(name)), value);
    } else {
      const [start, d] = chunkAt.get(name)!;
      kept.push({ kind: "bytes", name, start, d });
    }
  }

  const paths = new Paths();
  /** A family (conventions §7) of at most 16 members per member and 1024 more: a
   * member whose index is not less than that goes to other. */
  const family = async (path: string, all: Member[]) => {
    const cap = 16 * all.length + 1024;
    for (const [i, o, d, name] of all) if (i >= cap) other.push([name, o, d]);
    const members = all.filter(([i]) => i < cap).map(([i, o, d]): [number, number, number] => [i, o, d]);
    if (members.length > 0) {
      paths.take(path);
      out.arrays.push(...await familyPlans(path, members, read));
    }
  };
  const bytesPlan = (path: string, start: number, d: number): Plan => {
    const [chunkSize, cs] = blobChunks(start, d);
    return { path, dataType: "uint8", shape: [d], chunkShape: [chunkSize], dims: ["byte"], chunks: cs };
  };
  /** The cut of an array of copied values (chunks of at most 64 KiB). */
  const cut = (item: number): [number, number, number[], number] => {
    const [a, c] = gridCut(grid, item, MAX_COPIED);
    const chunkShape = [...new Array(a).fill(1), c, ...grid.slice(a + 1)];
    return [a, c, chunkShape, chunkShape.reduce((p, v) => p * v, 1)];
  };

  // The frames' timestamps: the binary64 at the start of each frame, copied,
  // since they are scattered. The array is cut as contiguous values would be
  // (conventions §7), and only the chunks that hold a placed frame are
  // written, NaN where a frame is missing (and zero past the array's edge).
  // The chunk limit halves from 64 KiB while those chunks would hold more
  // than max(1 MiB, 512 bytes per placed frame).
  if (frames.stamps.size > 0) {
    paths.take("ImageDataSeq");
    const bound = Math.max(MAX_STAMPS, 512 * frames.stamps.size);
    const indexes = [...frames.stamps.keys()].map((f) => gridIndex(loops, f));
    let a: number, c: number, chunkShape: number[], elements: number;
    for (let limit = MAX_COPIED; ; limit /= 2) {
      [a, c] = gridCut(grid, 8, limit);
      chunkShape = [...new Array(a).fill(1), c, ...grid.slice(a + 1)];
      elements = chunkShape.reduce((p, v) => p * v, 1);
      const keys = new Set<string>();
      for (const g of indexes) {
        keys.add(gridChunk(grid, a, c, g)[0]);
        if (8 * elements * keys.size > bound) break;
      }
      if (8 * elements * keys.size <= bound) break; // at 8 bytes, a chunk per placed frame: 8 bytes each
    }
    const stamps = new Map<string, Uint8Array>();
    let j = 0;
    for (const at of frames.stamps.values()) {
      const [key, i, m] = gridChunk(grid, a, c, indexes[j++]);
      let chunk = stamps.get(key);
      if (chunk === undefined) {
        chunk = new Uint8Array(8 * elements);
        const view = new DataView(chunk.buffer);
        for (let j = 0; j < m; j++) view.setFloat64(8 * j, NaN, true);
        stamps.set(key, chunk);
      }
      chunk.set(await read(at, 8), 8 * i);
    }
    out.arrays.push({ path: "ImageDataSeq", dataType: "float64", shape: grid, chunkShape, dims, chunks: stamps, fill: "NaN" });
  }
  // The frames the image does not place, whole, and the bytes after a placed frame's pixels.
  if (beyond.length > 0) await family("ImageDataSeq.beyond", beyond);
  if (frames.pixels !== undefined) {
    const trailing: Member[] = [];
    for (const f of [...frames.placed.keys()].sort((a, b) => a - b)) {
      const o = frames.placed.get(f)!;
      const nameLength = frames.nameLength!;
      const head = await read(o + 8, 8); // the profile has checked the header
      const d = new DataView(head.buffer, head.byteOffset, head.byteLength).getBigUint64(0, true);
      if (BigInt(o + 16 + nameLength) + d > BigInt(size) || d <= BigInt(8 + frames.pixels)) continue;
      const start = o + 16 + nameLength + 8 + frames.pixels;
      const length = Number(d) - 8 - frames.pixels;
      if (f < MAX_FAMILY) trailing.push([f, start, length, `ImageDataSeq|${f}!`]);
      else other.push([`ImageDataSeq|${f}!`, start, length]);
    }
    if (trailing.length > 0) await family("ImageDataSeq.trailing", trailing);
  }

  // Streams: one value per frame, typed (the undeclared ones, then those that
  // CustomDataV2_0 declares), shaped to the frame grid and cut as contiguous values.
  for (const sid of streamIds) {
    const s = streams.get(sid);
    if (s === undefined) continue;
    const [name, start, d] = s;
    const dataType = Object.hasOwn(STREAMS, sid) ? STREAMS[sid] : declared.get(sid)![0];
    const k = SIZES[dataType];
    const path = nodePath(decodeText(bytesOf(name)));
    const rest = d - k * n;
    if (rest < 0 || !paths.free(path) || (rest > 0 && !paths.free(`${path}.rest`))) {
      kept.push({ kind: "bytes", name, start, d });
      continue;
    }
    paths.take(path);
    let chunkShape: number[];
    let cs: Map<string, Part[] | Uint8Array>;
    if (flipped) { // in the grid's order: copies, padded with zero bytes
      const values = await read(start, k * n);
      const [a, c, shape, elements] = cut(k);
      chunkShape = shape;
      cs = new Map();
      for (let g = 0; g < n; g++) {
        const [key, i] = gridChunk(grid, a, c, g);
        let chunk = cs.get(key) as Uint8Array | undefined;
        if (chunk === undefined) cs.set(key, chunk = new Uint8Array(k * elements));
        const f = gridIndex(loops, g);
        chunk.set(values.subarray(k * f, k * (f + 1)), k * i);
      }
    } else {
      [chunkShape, cs] = await gridChunks(start, grid, k, read);
    }
    out.arrays.push({
      path, dataType, shape: grid, chunkShape, dims, chunks: cs,
      attributes: declared.has(sid) ? declare({}, "nd2", undefined, tagOf(declared.get(sid)!)) : undefined,
    });
    if (rest > 0) { // the bytes after the first N values
      paths.take(`${path}.rest`);
      out.arrays.push(bytesPlan(`${path}.rest`, start + k * n, rest));
    }
  }

  // Every other chunk, as its bytes, at its path, in map order (a family at its first member's place).
  const mapPosition = (item: Kept) =>
    position.get(item.kind === "bytes" ? item.name : families.get(item.key)![0][3])!;
  for (const item of [...kept].sort((a, b) => mapPosition(a) - mapPosition(b))) {
    if (item.kind === "bytes") {
      const path = nodePath(decodeText(bytesOf(item.name)));
      if (paths.free(path)) {
        paths.take(path);
        out.arrays.push(bytesPlan(path, item.start, item.d));
      } else {
        other.push([item.name, item.start, item.d]);
      }
    } else {
      const members = families.get(item.key)!;
      const path = nodePath(`CustomDataSeq|${decodeText(bytesOf(item.key))}!`);
      if (paths.free(path)) await family(path, members);
      else other.push(...members.map(([, o, d, name]): [string, number, number] => [name, o, d]));
    }
  }

  // The chunks without a path of their own, numbered in map order.
  other.sort((a, b) => position.get(a[0])! - position.get(b[0])!);
  other.forEach(([, start, d], i) => out.arrays.push(bytesPlan(`${OTHER}/${i}`, start, d)));
  // The names in other and empty, in S while it stays within 64 KiB, else as
  // an array of their JSON text (copied), at other/names and other/empty.
  for (const [key, names] of [["other", other.map(([name]) => name)], ["empty", empty]] as [string, string[]][]) {
    if (names.length === 0) continue;
    const value = names.map((name) => textJson(bytesOf(name)));
    if (jsonSize({ ...out.node, [key]: value }) <= MAX_NODE_TOTAL) {
      out.node[key] = value;
    } else {
      out.node[key] = LISTS[key];
      out.arrays.push(copiedBytes(LISTS[key], utf8.encode(stringifyJson(value))));
    }
  }
  return out;
}

/** Bytes held in memory as a 1-D uint8 array (conventions §7), each chunk copied. */
function copiedBytes(path: string, data: Uint8Array): Plan {
  const k = Math.ceil(data.length / MAX_CHUNK);
  const size = Math.ceil(data.length / k);
  const chunks = new Map<string, Uint8Array>();
  for (let i = 0; i < k; i++) {
    const chunk = new Uint8Array(size);
    chunk.set(data.subarray(i * size, (i + 1) * size));
    chunks.set(String(i), chunk);
  }
  return { path, dataType: "uint8", shape: [data.length], chunkShape: [size], dims: ["byte"], chunks };
}
