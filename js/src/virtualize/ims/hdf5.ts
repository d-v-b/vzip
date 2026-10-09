// Reading the structure of an HDF5 file through range reads (§8.1–§8.6): the
// superblock, object headers and their messages, the links of groups,
// attributes, and the chunk index of a chunked dataset. Pixel data is never
// read, and checksums are not checked.

import { type ByteReader, jsonNumber, textJson } from "../common.ts";

export class ImsError extends Error {}

const reject = (message: string): never => {
  throw new ImsError(message);
};

const SIGNATURE = [0x89, 0x48, 0x44, 0x46, 0x0d, 0x0a, 0x1a, 0x0a];
const UNDEFINED = (1n << 64n) - 1n;
const MAX_SAFE = BigInt(Number.MAX_SAFE_INTEGER);
const MAX_BLOCKS = 1000; // blocks of one object header (§8.2)
const MAX_HEADERS = 4096; // object headers held in memory at once
const MAX_BTREE2_DEPTH = 16; // §8.4
// The record size of each type of version 2 B-tree read: huge fractal heap
// objects, link names and attribute names (§8.4); chunk records (types 10 and
// 11) have the size the dataset gives them (§8.5).
const RECORD_SIZES: Record<number, number> = { 1: 24, 5: 11, 8: 17 };

// Message types (§8.2).
export const DATASPACE = 0x01, LINK_INFO = 0x02, DATATYPE = 0x03, FILL_VALUE = 0x05, LINK = 0x06, LAYOUT = 0x08;
export const FILTERS = 0x0b, ATTRIBUTE = 0x0c, CONTINUATION = 0x10, SYMBOL_TABLE = 0x11, ATTRIBUTE_INFO = 0x15;
export const EXTERNAL_FILES = 0x07;

/** The little-endian unsigned integer of `n` bytes at `at`, which MUST lie within `data`. */
export function le(data: Uint8Array, at: number, n: number): bigint {
  if (at < 0 || at + n > data.length) reject("truncated HDF5 structure");
  let v = 0n;
  for (let i = n - 1; i >= 0; i--) v = (v << 8n) | BigInt(data[at + i]);
  return v;
}

/** A small little-endian field (at most 6 bytes), as a number. */
export function u(data: Uint8Array, at: number, n: number): number {
  return Number(le(data, at, n));
}

/** An address: undefined if undefined (all bits set), else at most 2^53 - 1. */
export function address(v: bigint): number | undefined {
  if (v === UNDEFINED) return undefined;
  if (v > MAX_SAFE) reject(`HDF5 address ${v} is too large`);
  return Number(v);
}

export function length(v: bigint): number {
  if (v > MAX_SAFE) reject(`HDF5 length ${v} is too large`);
  return Number(v);
}

/** floor(log2(v)) for v >= 1. */
function log2(v: bigint | number): number {
  return BigInt(v).toString(2).length - 1;
}

/** Bytes as text, one character per byte: how names are kept. */
export function latin1(b: Uint8Array): string {
  let s = "";
  for (const x of b) s += String.fromCharCode(x);
  return s;
}

/** The bytes up to the first NUL (all of them if there is none). */
export function untilNul(b: Uint8Array): Uint8Array {
  const end = b.indexOf(0);
  return end < 0 ? b : b.subarray(0, end);
}

export interface Message {
  type: number;
  flags: number;
  data: Uint8Array;
  offset: number; // where the file holds the data
}

export interface Datatype {
  cls: number; // 0 fixed-point, 1 floating-point, 3 string, ...
  size: number;
  bits: number; // the class bit fields
  props: Uint8Array;
}

export interface Dataspace {
  dims: number[] | null; // null for a null dataspace; [] for a scalar
  maxdims: bigint[] | null;
}

export interface Dataset {
  dims: number[] | null; // null for a null dataspace (source metadata only)
  maxdims: bigint[] | null;
  datatype: Datatype;
  chunk: number[]; // chunk dimensions, without the element size
  grid: number[]; // chunks per dimension
  filters: number[]; // filter identifiers, in pipeline order
  index: "btree1" | "single" | "implicit" | "farray" | "earray" | "btree2";
  indexAddress: number | undefined;
  singleSize: number | undefined; // single chunk index: the filtered chunk's size
  layout: "compact" | "contiguous" | "chunked" | "null";
  compact: Uint8Array; // a compact dataset's data
  dataAddress: number | undefined; // a contiguous dataset's data, or undefined if unallocated
  fill: Uint8Array | undefined; // the fill value, or undefined for the default (zero bytes)
  typeMessage: Uint8Array; // the datatype message
  named?: number; // the committed datatype's object header, for a shared datatype
  singleMask?: number; // single chunk index: the filtered chunk's filter mask
  rawEdges?: boolean; // partial edge chunks are stored unfiltered (layout flag 1, §8.5)
}

/** A link's target: an object header address (a hard link), a path (a soft
 * link), or the type and value of any other type of link (§8.9). */
export type Target = { hard: number } | { soft: string } | { other: number; value: Uint8Array | null };

/** A group's links by name. Names and paths are kept as latin1 text of their bytes. */
export type Links = Map<string, Target>;

function parseDatatype(d: Uint8Array): Datatype {
  const cv = u(d, 0, 1);
  if (cv >> 4 < 1 || cv >> 4 > 5) reject(`unsupported datatype message version ${cv >> 4}`);
  return { cls: cv & 15, size: u(d, 4, 4), bits: u(d, 1, 3), props: d.subarray(8) };
}

export function parseDataspace(d: Uint8Array): Dataspace {
  const version = u(d, 0, 1), rank = u(d, 1, 1), flags = u(d, 2, 1);
  let kind: number, p: number;
  if (version === 1) {
    [kind, p] = [rank ? 1 : 0, 8];
  } else if (version === 2) {
    [kind, p] = [u(d, 3, 1), 4];
    if (kind > 2 || (kind !== 1 && rank !== 0)) reject(`invalid dataspace type ${kind}`);
  } else {
    return reject(`unsupported dataspace message version ${version}`);
  }
  const dims = Array.from({ length: rank }, (_, i) => length(le(d, p + 8 * i, 8)));
  const maxdims = flags & 1 ? Array.from({ length: rank }, (_, i) => le(d, p + 8 * (rank + i), 8)) : null;
  return { dims: kind === 2 ? null : dims, maxdims };
}

/** An HDF5 file read through `read` (§8.1). */
export class Hdf5 {
  read: ByteReader;
  size: number;
  root = 0;
  private headers = new Map<number, Promise<Message[]>>();
  private heaps = new Map<number, Promise<FractalHeap>>();
  // Per global heap collection: each object's address and size, as numbers (no array per object).
  private collections = new Map<number, Promise<[Map<number, number>, Map<number, number>]>>();

  private constructor(read: ByteReader, size: number) {
    this.read = read;
    this.size = size;
  }

  static async open(read: ByteReader, size: number): Promise<Hdf5> {
    // Every read MUST lie within the file (§8.1).
    const f = new Hdf5(async (offset, n) => {
      if (offset < 0 || offset + n > size) reject(`read of [${offset}, ${offset + n}) outside the ${size}-byte file`);
      return read(offset, n);
    }, size);
    const head = await f.read(0, 9);
    if (!SIGNATURE.every((b, i) => head[i] === b)) reject("not an HDF5 file");
    const version = head[8];
    let sb: Uint8Array, baseAt: number, sizes: number[], rootAt: number;
    if (version === 0 || version === 1) {
      sb = await f.read(0, version === 1 ? 76 : 72);
      baseAt = version === 1 ? 28 : 24;
      [sizes, rootAt] = [[sb[13], sb[14]], baseAt + 40];
    } else if (version === 2 || version === 3) {
      sb = await f.read(0, 48);
      [baseAt, sizes, rootAt] = [12, [sb[9], sb[10]], 36];
    } else {
      return reject(`unsupported HDF5 superblock version ${version}`);
    }
    if (sizes[0] !== 8 || sizes[1] !== 8) {
      reject(`HDF5 offsets and lengths of ${sizes[0]} and ${sizes[1]} bytes are not supported`);
    }
    if (le(sb, baseAt, 8) !== 0n) reject("HDF5 base address is not 0");
    const root = address(le(sb, rootAt, 8));
    if (root === undefined) return reject("no HDF5 root group");
    f.root = root;
    return f;
  }

  // ---- object headers (§8.2)

  header(at: number): Promise<Message[]> {
    let h = this.headers.get(at);
    if (h === undefined) {
      if (this.headers.size >= MAX_HEADERS) this.headers.clear(); // a cache: memory bounded, whatever the number of objects
      h = this.readHeader(at);
      this.headers.set(at, h);
    }
    return h;
  }

  private async readHeader(at: number): Promise<Message[]> {
    const read = this.read;
    let blocks: [number, number][], v2: boolean, messageHeader: number;
    if (latin1(await read(at, 4)) === "OHDR") {
      const prefix = await read(at, 6);
      if (prefix[4] !== 2) reject(`unsupported object header version ${prefix[4]}`);
      const flags = prefix[5];
      const p = at + 6 + (flags & 0x20 ? 16 : 0) + (flags & 0x10 ? 4 : 0);
      const n = 1 << (flags & 3);
      const first = length(le(await read(p, n), 0, n));
      await read(p + n, first + 4); // the block and its checksum
      [blocks, v2, messageHeader] = [[[p + n, first]], true, flags & 4 ? 6 : 4];
    } else {
      const prefix = await read(at, 16);
      if (prefix[0] !== 1) reject(`no object header at ${at}`);
      [blocks, v2, messageHeader] = [[[at + 16, u(prefix, 8, 4)]], false, 8];
    }
    const messages: Message[] = [];
    const seen = new Set([blocks[0][0]]);
    for (let i = 0; i < blocks.length; i++) {
      const [start, n] = blocks[i];
      const data = await read(start, n);
      let pos = 0;
      while (pos < n) {
        if (n - pos < messageHeader) {
          if (v2) break; // a gap
          reject("truncated object header message");
        }
        const [type, size, mflags] = v2
          ? [data[pos], u(data, pos + 1, 2), data[pos + 3]]
          : [u(data, pos, 2), u(data, pos + 2, 2), data[pos + 4]];
        const body = pos + messageHeader;
        if (body + size > n) reject("object header message runs past its block");
        pos = body + size;
        const mdata = data.subarray(body, pos);
        if (type !== CONTINUATION) {
          messages.push({ type, flags: mflags, data: mdata, offset: start + body });
          continue;
        }
        if (mflags & 2) reject("shared continuation message");
        const to = address(le(mdata, 0, 8));
        const ln = length(le(mdata, 8, 8));
        if (to === undefined) return reject("continuation to an undefined address");
        let block: [number, number];
        if (v2) {
          if (ln < 8 || latin1(await read(to, 4)) !== "OCHK") reject(`no continuation block at ${to}`);
          await read(to, ln);
          block = [to + 4, ln - 8];
        } else {
          block = [to, ln];
        }
        if (seen.has(block[0])) reject(`object header block at ${to} read twice`);
        if (blocks.length >= MAX_BLOCKS) reject("too many object header blocks");
        seen.add(block[0]);
        blocks.push(block);
      }
    }
    return messages;
  }

  /** The data of the one message of type `type` (§8.2). */
  static message(messages: Message[], type: number, required: true): Uint8Array;
  static message(messages: Message[], type: number, required: false): Uint8Array | undefined;
  static message(messages: Message[], type: number, required: boolean): Uint8Array | undefined {
    const found = messages.filter((m) => m.type === type);
    if (found.length > 1) reject(`object header has ${found.length} messages of type ${type}`);
    if (found.length === 0) return required ? reject(`object header has no message of type ${type}`) : undefined;
    if (found[0].flags & 2) reject(`shared message of type ${type}`);
    return found[0].data;
  }

  /** The datatype message of the committed datatype that a shared datatype
   * message leads to, and its object header (§8.9). */
  async committed(shared: Uint8Array): Promise<[Uint8Array, number]> {
    const version = u(shared, 0, 1);
    let at: number | undefined;
    if (version === 1) at = address(le(shared, 8, 8));
    else if (version === 2 || (version === 3 && u(shared, 1, 1) === 2)) at = address(le(shared, 2, 8));
    else return reject("a datatype shared other than as a committed datatype");
    if (at === undefined) return reject("a shared datatype at an undefined address");
    return [Hdf5.message(await this.header(at), DATATYPE, true), at];
  }

  /** The datatype message of an object header, through a committed datatype
   * when it is shared, and that datatype's object header (§8.9). */
  async datatype(messages: Message[]): Promise<[Uint8Array, number | undefined]> {
    const found = messages.filter((m) => m.type === DATATYPE);
    if (found.length !== 1) reject(`object header has ${found.length} messages of type ${DATATYPE}`);
    if (found[0].flags & 2) return this.committed(found[0].data);
    return [found[0].data, undefined];
  }

  // ---- groups (§8.3)

  /** A group's links. */
  async links(at: number): Promise<Links> {
    const messages = await this.header(at);
    const table = Hdf5.message(messages, SYMBOL_TABLE, false);
    const info = Hdf5.message(messages, LINK_INFO, false);
    if (table !== undefined && info !== undefined) reject("group has both a symbol table and link info");
    const links: Links = new Map();
    const add = ([name, target]: [string, Target]) => {
      if (links.has(name)) reject(`group has two links named ${JSON.stringify(name)}`);
      links.set(name, target);
    };
    if (table !== undefined) {
      const btree = address(le(table, 0, 8));
      const heap = address(le(table, 8, 8));
      if (btree === undefined || heap === undefined) return reject("symbol table with an undefined address");
      const names = await this.localHeap(heap);
      for (const [, snod] of await this.btree1(btree, 0, 8)) {
        const head = await this.read(snod, 8);
        if (latin1(head.subarray(0, 4)) !== "SNOD" || head[4] !== 1) reject(`no symbol table node at ${snod}`);
        const count = u(head, 6, 2);
        const entries = await this.read(snod + 8, 40 * count);
        for (let k = 0; k < count; k++) {
          const offset = le(entries, 40 * k, 8);
          const end = offset < BigInt(names.length) ? names.indexOf(0, Number(offset)) : -1;
          if (end < 0) reject("link name outside the local heap");
          const target = address(le(entries, 40 * k + 8, 8));
          if (target === undefined) return reject("symbol table entry with an undefined address");
          add([latin1(names.subarray(Number(offset), end)), { hard: target }]);
        }
      }
    } else if (info !== undefined) {
      if (u(info, 0, 1) !== 0) reject("unsupported link info message version");
      const p = 2 + (u(info, 1, 1) & 1 ? 8 : 0);
      const heap = address(le(info, p, 8));
      const btree = address(le(info, p + 8, 8));
      if (heap === undefined) {
        for (const m of messages) {
          if (m.type !== LINK) continue;
          if (m.flags & 2) reject("shared link message");
          add(parseLink(m.data));
        }
      } else {
        if (btree === undefined) return reject("dense links without a name index");
        const fh = await this.fractalHeap(heap);
        for (const record of await this.btree2(btree, 5)) add(parseLink(await fh.get(record.subarray(4))));
      }
    } else {
      reject(`object at ${at} is not a group`);
    }
    return links;
  }

  /** The object header that the link `name` of a group leads to (§8.3). */
  async follow(links: Links, name: string): Promise<number> {
    const target = links.get(name);
    if (target === undefined || "other" in target) return reject(`link ${JSON.stringify(name)} is neither hard nor soft`);
    if ("hard" in target) return target.hard;
    const path = target.soft;
    if (!path.startsWith("/")) reject(`soft link ${JSON.stringify(name)} to a relative path`);
    let at = this.root;
    for (const part of path === "/" ? [] : path.slice(1).split("/")) {
      const step = (await this.links(at)).get(part);
      if (step === undefined || !("hard" in step)) {
        return reject(`soft link ${JSON.stringify(name)} to ${JSON.stringify(path)} does not resolve through hard links`);
      }
      at = step.hard;
    }
    return at;
  }

  async localHeap(at: number): Promise<Uint8Array> {
    const head = await this.read(at, 32);
    if (latin1(head.subarray(0, 4)) !== "HEAP" || head[4] !== 0) reject(`no local heap at ${at}`);
    const data = address(le(head, 24, 8));
    if (data === undefined) return reject("local heap without a data segment");
    return this.read(data, length(le(head, 8, 8)));
  }

  /** The children of the leaves of the version 1 B-tree at `at`, each with
   * its key (the key before it) (§8.3, §8.5). */
  private async btree1(
    at: number,
    nodeType: number,
    keySize: number,
    seen = new Set<number>(),
    level?: number,
  ): Promise<[Uint8Array, number][]> {
    if (seen.has(at)) reject(`B-tree node at ${at} read twice`);
    seen.add(at);
    const head = await this.read(at, 24);
    if (latin1(head.subarray(0, 4)) !== "TREE" || head[4] !== nodeType) {
      reject(`no B-tree node of type ${nodeType} at ${at}`);
    }
    const nodeLevel = head[5], entries = u(head, 6, 2);
    if (level !== undefined && nodeLevel !== level) reject(`B-tree node at ${at} has level ${nodeLevel}, not ${level}`);
    const step = keySize + 8;
    const body = await this.read(at + 24, entries * step + keySize);
    const out: [Uint8Array, number][] = [];
    for (let i = 0; i < entries; i++) {
      const child = address(le(body, i * step + keySize, 8));
      if (child === undefined) return reject("B-tree child with an undefined address");
      if (nodeLevel > 0) {
        for (const found of await this.btree1(child, nodeType, keySize, seen, nodeLevel - 1)) out.push(found);
        continue;
      }
      if (nodeType === 0) {
        if (seen.has(child)) reject(`symbol table node at ${child} read twice`);
        seen.add(child);
      }
      out.push([body.subarray(i * step, i * step + keySize), child]);
    }
    return out;
  }

  // ---- version 2 B-trees and fractal heaps (§8.4)

  /** The records of the version 2 B-tree at `at`, which MUST have type `type`
   * and a record size in `sizes` (by default the type's, RECORD_SIZES). */
  async btree2(at: number, type: number, sizes?: number[]): Promise<Uint8Array[]> {
    const h = await this.read(at, 38);
    if (latin1(h.subarray(0, 4)) !== "BTHD" || h[4] !== 0 || h[5] !== type) {
      reject(`no version 2 B-tree of type ${type} at ${at}`);
    }
    const nodeSize = u(h, 6, 4), recordSize = u(h, 10, 2), depth = u(h, 12, 2);
    const root = address(le(h, 16, 8));
    const rootCount = u(h, 24, 2);
    if (depth > MAX_BTREE2_DEPTH) reject(`version 2 B-tree depth ${depth}`);
    if (!(sizes ?? [RECORD_SIZES[type]]).includes(recordSize) || nodeSize < 10 + recordSize) {
      reject("invalid version 2 B-tree node or record size");
    }
    const leafMax = Math.floor((nodeSize - 10) / recordSize);
    const countSize = Math.floor(log2(leafMax) / 8) + 1;
    // pointer[d]: the size of a child pointer in a node of depth d.
    const cum: bigint[] = [BigInt(leafMax)], cumSize = [0], pointer = [0];
    for (let d = 1; d <= depth; d++) {
      pointer.push(8 + countSize + (d > 1 ? cumSize[d - 1] : 0));
      const most = Math.floor((nodeSize - 10 - pointer[d]) / (recordSize + pointer[d]));
      if (most < 1) reject("version 2 B-tree nodes too small");
      cum.push(BigInt(most + 1) * cum[d - 1] + BigInt(most));
      cumSize.push(Math.floor(log2(cum[d]) / 8) + 1);
    }
    const records: Uint8Array[] = [];
    const seen = new Set<number>();
    const node = async (nodeAt: number, count: number, d: number): Promise<void> => {
      if (seen.has(nodeAt)) reject(`B-tree node at ${nodeAt} read twice`);
      seen.add(nodeAt);
      const data = await this.read(nodeAt, 6 + count * recordSize + (d ? (count + 1) * pointer[d] : 0));
      if (latin1(data.subarray(0, 4)) !== (d ? "BTIN" : "BTLF") || data[4] !== 0 || data[5] !== type) {
        reject(`no version 2 B-tree node at ${nodeAt}`);
      }
      for (let k = 0; k < count; k++) records.push(data.subarray(6 + k * recordSize, 6 + (k + 1) * recordSize));
      let p = 6 + count * recordSize;
      for (let k = 0; k < (d ? count + 1 : 0); k++) {
        const child = address(le(data, p, 8));
        if (child === undefined) return reject("B-tree child with an undefined address");
        await node(child, u(data, p + 8, countSize), d - 1);
        p += pointer[d];
      }
    };
    if (root !== undefined) await node(root, rootCount, depth);
    return records;
  }

  fractalHeap(at: number): Promise<FractalHeap> {
    let h = this.heaps.get(at);
    if (h === undefined) {
      h = FractalHeap.open(this, at);
      this.heaps.set(at, h);
    }
    return h;
  }

  // ---- attributes (§8.6)

  /** An object's attributes: name -> attribute message; `where` receives name ->
   * the message's address in the file. */
  async attributes(at: number, where?: Map<string, number>): Promise<Map<string, Uint8Array>> {
    const messages = await this.header(at);
    const out = new Map<string, Uint8Array>();
    const add = (data: Uint8Array, offset: number) => {
      const name = attributeName(data);
      if (out.has(name)) reject(`two attributes named ${JSON.stringify(name)}`);
      out.set(name, data);
      where?.set(name, offset);
    };
    for (const m of messages) {
      if (m.type !== ATTRIBUTE) continue;
      if (m.flags & 2) reject("shared attribute message");
      add(m.data, m.offset);
    }
    const info = Hdf5.message(messages, ATTRIBUTE_INFO, false);
    if (info !== undefined) {
      if (u(info, 0, 1) !== 0) reject("unsupported attribute info message version");
      const p = 2 + (u(info, 1, 1) & 1 ? 2 : 0);
      const heap = address(le(info, p, 8));
      const btree = address(le(info, p + 8, 8));
      if (heap !== undefined) {
        if (btree === undefined) return reject("dense attributes without a name index");
        const fh = await this.fractalHeap(heap);
        for (const record of await this.btree2(btree, 8)) {
          if (record[8] & 2) reject("shared dense attribute");
          const [objectAt, n] = await fh.where(record.subarray(0, 8));
          add(await this.read(objectAt, n), objectAt);
        }
      }
    }
    return out;
  }

  // ---- datasets (§8.5)

  /** A dataset: a chunked one with a zero fill value (§8.5), or, with
   * `anyLayout`, one of any layout and fill value (the source metadata). */
  async dataset(at: number, anyLayout = false): Promise<Dataset> {
    const messages = await this.header(at);
    const space = parseDataspace(Hdf5.message(messages, DATASPACE, true));
    const [typeMessage, named] = anyLayout ? await this.datatype(messages)
      : [Hdf5.message(messages, DATATYPE, true), undefined];
    const datatype = parseDatatype(typeMessage);
    const fillMessage = Hdf5.message(messages, FILL_VALUE, false);
    const filters = parseFilters(Hdf5.message(messages, FILTERS, false));
    const layout = Hdf5.message(messages, LAYOUT, true);
    let fill = fillMessage !== undefined ? fillValue(fillMessage) : undefined;
    if (anyLayout && fill !== undefined && fill.length !== 0 && fill.length !== datatype.size) {
      reject("the fill value is not of the datatype's size");
    }
    if (fill !== undefined && fill.length === 0) fill = undefined;
    if (!anyLayout && fill !== undefined && fill.some((b) => b !== 0)) reject("the fill value is not zero");
    if (space.dims === null) {
      if (!anyLayout) return reject("dataset with a null dataspace");
      return {
        dims: null, maxdims: null, datatype, chunk: [], grid: [], filters, index: "btree1", indexAddress: undefined,
        singleSize: undefined, layout: "null", compact: new Uint8Array(0), dataAddress: undefined, fill, typeMessage,
        named,
      };
    }
    const rank = space.dims.length;
    const version = u(layout, 0, 1), cls = u(layout, 1, 1);
    if (version < 3 || version > 5) reject(`unsupported layout message version ${version}`);
    if (cls === 3) reject("a virtual dataset, whose data is in other datasets");
    if (cls !== 2 && anyLayout && (cls === 0 || cls === 1)) {
      const ds: Dataset = {
        dims: space.dims, maxdims: space.maxdims, datatype, chunk: [], grid: [], filters, index: "btree1",
        indexAddress: undefined, singleSize: undefined, layout: cls === 0 ? "compact" : "contiguous",
        compact: new Uint8Array(0), dataAddress: undefined, fill, typeMessage, named,
      };
      if (cls === 0) {
        const n = u(layout, 2, 2);
        if (4 + n > layout.length) reject("truncated compact dataset");
        ds.compact = layout.subarray(4, 4 + n);
      } else {
        ds.dataAddress = address(le(layout, 2, 8));
        length(le(layout, 10, 8));
      }
      return ds;
    }
    if (cls !== 2) reject(`dataset layout class ${cls} is not chunked`);
    let singleSize: number | undefined, singleMask = 0, rawEdges = false;
    let index: Dataset["index"], indexAddress: number | undefined, ndims: number, dims: bigint[];
    if (version === 3) {
      ndims = u(layout, 2, 1);
      [index, indexAddress] = ["btree1", address(le(layout, 3, 8))];
      dims = Array.from({ length: ndims }, (_, i) => le(layout, 11 + 4 * i, 4));
    } else {
      const flags = u(layout, 2, 1), enc = u(layout, 4, 1);
      ndims = u(layout, 3, 1);
      if (enc < 1 || enc > 8) reject(`invalid chunk dimension size length ${enc}`);
      dims = Array.from({ length: ndims }, (_, i) => le(layout, 5 + enc * i, enc));
      let p = 5 + enc * ndims;
      const itype = u(layout, p, 1);
      p += 1;
      if (itype === 1) {
        index = "single";
        if (flags & 2) {
          singleSize = length(le(layout, p, 8));
          singleMask = u(layout, p + 8, 4);
          p += 12;
        }
      } else if (itype === 2) {
        index = "implicit";
      } else if (itype === 3) {
        index = "farray";
        le(layout, p, 1); // page bits: the fixed array header's are used
        p += 1;
      } else if (itype === 4) {
        index = "earray";
        le(layout, p, 5); // the extensible array header's parameters are used
        p += 5;
      } else if (itype === 5) {
        index = "btree2";
        le(layout, p, 6); // node size, split and merge percents: the B-tree header's are used
        p += 6;
      } else {
        return reject(`unsupported chunk index type ${itype}`);
      }
      indexAddress = address(le(layout, p, 8));
      rawEdges = filters.length > 0 && (flags & 1) !== 0;
      if (filters.length && index === "single" && !(flags & 2)) reject("single filtered chunk without its size");
      if (filters.length && index === "implicit") reject("implicit chunk index with filters");
    }
    if (ndims !== rank + 1 || dims[ndims - 1] !== BigInt(datatype.size)) {
      reject("chunk dimensions do not match the dataspace and datatype");
    }
    const chunkDims = dims.slice(0, -1);
    if (chunkDims.some((c) => c < 1n || c > MAX_SAFE)) reject("a chunk dimension is not from 1 to 2^53 - 1");
    const chunk = chunkDims.map(Number);
    const grid: number[] = [];
    let total = 1;
    for (const [i, n] of space.dims.entries()) {
      grid.push(Math.ceil(n / chunk[i]));
      total *= grid[i];
      if (total > Number.MAX_SAFE_INTEGER) reject("more than 2^53 - 1 chunks");
    }
    return {
      dims: space.dims, maxdims: space.maxdims, datatype, chunk, grid, filters, index, indexAddress, singleSize,
      layout: "chunked", compact: new Uint8Array(0), dataAddress: undefined, fill, typeMessage, named, singleMask,
      rawEdges,
    };
  }

  // ---- the global heap (§8.9)

  /** The [address, size] of the object `index` of the global heap collection at `at`. */
  async globalObject(at: number, index: number): Promise<[number, number]> {
    let objects = this.collections.get(at);
    if (objects === undefined) {
      objects = (async () => {
        const head = await this.read(at, 16);
        if (latin1(head.subarray(0, 4)) !== "GCOL" || head[4] !== 1) reject(`no global heap collection at ${at}`);
        const size = length(le(head, 8, 8));
        if (size < 16 || at + size > this.size) reject("a global heap collection outside the file");
        const data = await this.read(at, size);
        const where = new Map<number, number>(), sizes = new Map<number, number>();
        let p = 16;
        while (p + 16 <= size) {
          const i = u(data, p, 2), n = length(le(data, p + 8, 8));
          if (i === 0) break; // free space
          if (p + 16 + n > size) reject("a global heap object outside its collection");
          if (!where.has(i)) {
            where.set(i, at + p + 16);
            sizes.set(i, n);
          }
          p += 16 + pad8(n);
        }
        return [where, sizes] as [Map<number, number>, Map<number, number>];
      })();
      this.collections.set(at, objects);
    }
    const [where, sizes] = await objects;
    const found = where.get(index);
    if (found === undefined) return reject(`no global heap object ${index} at ${at}`);
    return [found, sizes.get(index)!];
  }

  /** The allocated chunks: grid coordinates (joined by "/") -> [address, size] (§8.5). A
   * chunk's filter mask MUST be 0, unless `masks` receives the chunks' that are not (§8.9); a
   * partial edge chunk stored unfiltered has every filter's bit set. */
  async chunks(ds: Dataset, masks?: Map<string, number>): Promise<Map<string, [number, number]>> {
    const out = new Map<string, [number, number]>();
    const nbytes = ds.chunk.reduce((n, c) => n * BigInt(c), BigInt(ds.datatype.size));
    const filtered = ds.filters.length > 0;
    const add = (coords: number[], at: number, size: bigint, mask = 0) => {
      const key = coords.join("/");
      if (ds.rawEdges && coords.some((c, i) => (c + 1) * ds.chunk[i] > ds.dims![i])) {
        mask = 2 ** ds.filters.length - 1; // a partial edge chunk, stored unfiltered: every filter skipped
      }
      if (mask) {
        if (masks === undefined) reject("a chunk's filter mask is not 0");
        masks!.set(key, mask);
      }
      if (out.has(key)) reject(`chunk ${showCoords(coords)} indexed twice`);
      if (!filtered && size !== nbytes) reject(`unfiltered chunk ${showCoords(coords)} of ${size} bytes, not ${nbytes}`);
      if (size < 1n) reject(`chunk ${showCoords(coords)} is empty`);
      if (BigInt(at) + size > BigInt(this.size)) reject(`chunk ${showCoords(coords)} lies outside the file`);
      out.set(key, [at, Number(size)]);
    };
    const at = ds.indexAddress;
    if (at === undefined) return out;
    const rank = ds.dims!.length;
    if (ds.index === "btree1") {
      for (const [key, child] of await this.btree1(at, 1, 8 + 8 * (rank + 1))) {
        const size = le(key, 0, 4), mask = u(key, 4, 4);
        const offsets = Array.from({ length: rank + 1 }, (_, i) => le(key, 8 + 8 * i, 8));
        const bad = offsets[rank] !== 0n ||
          ds.chunk.some((c, i) => offsets[i] % BigInt(c) !== 0n || offsets[i] >= BigInt(ds.dims![i]));
        if (bad) reject(`invalid chunk offset ${showCoords(offsets)}`);
        add(ds.chunk.map((c, i) => Number(offsets[i]) / c), child, size, mask);
      }
    } else if (ds.index === "single") {
      if (ds.grid.some((g) => g !== 1)) reject("single chunk index for more than one chunk");
      add(new Array(rank).fill(0), at, filtered ? BigInt(ds.singleSize!) : nbytes, filtered ? ds.singleMask ?? 0 : 0);
    } else if (ds.index === "implicit") {
      const maxgrid = this.implicit(ds);
      const total = product(ds.grid);
      for (let i = 0; i < total; i++) {
        const coords = unravel(i, ds.grid);
        add(coords, at + ravel(coords, maxgrid) * Number(nbytes), nbytes);
      }
    } else if (ds.index === "farray") {
      await this.fixedArray(ds, at, filtered, nbytes, add);
    } else if (ds.index === "earray") {
      await this.extensibleArray(ds, at, filtered, nbytes, add);
    } else {
      await this.btree2Chunks(ds, at, filtered, nbytes, add);
    }
    return out;
  }

  /** The maximum grid of an implicit chunk index, whose last chunk MUST lie within the file (§8.5). */
  implicit(ds: Dataset): number[] {
    const maxgrid = maxGrid(ds, false) as number[];
    const nbytes = ds.chunk.reduce((n, c) => n * BigInt(c), BigInt(ds.datatype.size));
    // The last chunk is the furthest: when it lies within the file, so do the others.
    if (product(ds.grid) && BigInt(ds.indexAddress!) + BigInt(ravel(ds.grid.map((g) => g - 1), maxgrid) + 1) * nbytes > BigInt(this.size)) {
      reject("an implicit chunk index past the end of the file");
    }
    return maxgrid;
  }

  /** An array index's entry: [the chunk's address or undefined, its size, its filter mask] (§8.5). */
  private entry(data: Uint8Array, k: number, entry: number, filtered: boolean, nbytes: bigint): [number | undefined, bigint, number] {
    const chunkAt = address(le(data, k * entry, 8));
    if (chunkAt === undefined || !filtered) return [chunkAt, nbytes, 0];
    return [chunkAt, BigInt(length(le(data, k * entry + 8, entry - 12))), u(data, k * entry + entry - 4, 4)];
  }

  private async btree2Chunks(
    ds: Dataset, at: number, filtered: boolean, nbytes: bigint, add: (coords: number[], at: number, size: bigint, mask: number) => void,
  ): Promise<void> {
    const rank = ds.dims!.length;
    const sizes = filtered ? Array.from({ length: 8 }, (_, i) => 13 + 8 * rank + i) : [8 + 8 * rank];
    for (const record of await this.btree2(at, filtered ? 11 : 10, sizes)) {
      const chunkAt = address(le(record, 0, 8));
      if (chunkAt === undefined) return reject("a chunk record with an undefined address");
      let p = 8, size = nbytes, mask = 0;
      if (filtered) {
        const n = record.length - 12 - 8 * rank;
        size = BigInt(length(le(record, 8, n)));
        mask = u(record, 8 + n, 4);
        p += n + 4;
      }
      const coords = Array.from({ length: rank }, (_, i) => le(record, p + 8 * i, 8));
      if (coords.some((c, i) => c >= BigInt(ds.grid[i]))) reject(`invalid chunk coordinates ${showCoords(coords)}`);
      add(coords.map(Number), chunkAt, size, mask);
    }
  }

  private async extensibleArray(
    ds: Dataset, at: number, filtered: boolean, nbytes: bigint, add: (coords: number[], at: number, size: bigint, mask: number) => void,
  ): Promise<void> {
    const maxgrid = maxGrid(ds, true);
    const unlimited = maxgrid.flatMap((m, i) => (m === undefined ? [i] : []));
    if (unlimited.length !== 1) reject("an extensible array index without exactly one unlimited dimension");
    const u = unlimited[0];
    const others = maxgrid.flatMap((_, i) => (i === u ? [] : [i]));
    const otherGrid = others.map((i) => maxgrid[i]!);
    const down = product(otherGrid);
    const limit = ds.grid[u] * down; // no index at or beyond it is in the grid
    const h = await this.read(at, 68);
    if (latin1(h.subarray(0, 4)) !== "EAHD" || h[4] !== 0) reject(`no extensible array header at ${at}`);
    const [client, entry, bits, ibCount, dbMin, sbMin, pageBits] = [h[5], h[6], h[7], h[8], h[9], h[10], h[11]];
    if (client !== Number(filtered) || (filtered ? entry < 13 || entry > 20 : entry !== 8)) {
      reject("the extensible array does not match the dataset");
    }
    const power = (v: number) => v >= 1 && (v & (v - 1)) === 0;
    if (!(power(dbMin) && power(sbMin) && log2(dbMin) <= bits && bits <= 64 && pageBits <= 64)) {
      reject("invalid extensible array parameters");
    }
    const iblock = address(le(h, 60, 8));
    if (iblock === undefined) return;
    const offsetSize = Math.floor((bits + 7) / 8);
    const page = 2 ** pageBits;
    const nsblks = 1 + bits - log2(dbMin);
    const ibSblks = 2 * log2(sbMin); // the super blocks whose data blocks the index block holds
    const ibDblks = 2 * (sbMin - 1);
    if (ibSblks > nsblks) reject("invalid extensible array parameters");
    const found = (first: number, data: Uint8Array, n: number) => {
      for (let k = 0; k < n; k++) {
        const [chunkAt, size, mask] = this.entry(data, k, entry, filtered, nbytes);
        if (chunkAt === undefined) continue;
        const rest = first + k;
        const coords = new Array(maxgrid.length).fill(0);
        coords[u] = Math.floor(rest / down);
        unravel(rest % down, otherGrid).forEach((c, j) => { coords[others[j]] = c; });
        if (coords.every((c, i) => c < ds.grid[i])) add(coords, chunkAt, size, mask);
      }
    };
    const dataBlock = async (blockAt: number, first: number, n: number, pages: Uint8Array | undefined, d: number) => {
      const prefix = 14 + offsetSize;
      const head = await this.read(blockAt, prefix);
      if (latin1(head.subarray(0, 4)) !== "EADB" || head[4] !== 0 || head[5] !== client) {
        reject(`no extensible array data block at ${blockAt}`);
      }
      const want = Math.min(n, limit - first);
      if (n <= page) {
        found(first, await this.read(blockAt + prefix, want * entry), want);
        return;
      }
      if (pages === undefined) reject("a paged extensible array data block in the index block");
      const npages = n / page;
      for (let j = 0; j < npages && j * page < want; j++) {
        const bit = d * npages + j;
        if (pages![Math.floor(bit / 8)] & (0x80 >> (bit % 8))) {
          const m = Math.min(page, want - j * page);
          found(first + j * page, await this.read(blockAt + prefix + 4 + j * (page * entry + 4), m * entry), m);
        }
      }
    };
    const head = await this.read(iblock, 14);
    if (latin1(head.subarray(0, 4)) !== "EAIB" || head[4] !== 0 || head[5] !== client) {
      reject(`no extensible array index block at ${iblock}`);
    }
    const body = await this.read(iblock + 14, ibCount * entry + 8 * ibDblks + 8 * (nsblks - ibSblks));
    found(0, body, Math.min(ibCount, limit));
    const dblks = body.subarray(ibCount * entry, ibCount * entry + 8 * ibDblks);
    const sblks = body.subarray(ibCount * entry + 8 * ibDblks);
    let start = ibCount, k = 0;
    for (let s = 0; s < nsblks; s++) {
      const count = 2 ** Math.floor(s / 2), n = 2 ** Math.floor((s + 1) / 2) * dbMin;
      if (start >= limit) break;
      const needed = Math.min(count, Math.ceil((limit - start) / n)); // the data blocks that hold indexes below the limit
      if (s < ibSblks) {
        for (let d = 0; d < needed; d++) {
          const blockAt = address(le(dblks, 8 * (k + d), 8));
          if (blockAt !== undefined) await dataBlock(blockAt, start + d * n, n, undefined, d);
        }
        k += count;
      } else {
        const sblock = address(le(sblks, 8 * (s - ibSblks), 8));
        if (sblock !== undefined) {
          const npages = n > page ? n / page : 0;
          const bitmap = count * Math.ceil(npages / 8);
          const prefix = 14 + offsetSize;
          const shead = await this.read(sblock, prefix + bitmap + 8 * count);
          if (latin1(shead.subarray(0, 4)) !== "EASB" || shead[4] !== 0 || shead[5] !== client) {
            reject(`no extensible array super block at ${sblock}`);
          }
          const pages = shead.subarray(prefix, prefix + bitmap);
          for (let d = 0; d < needed; d++) {
            const blockAt = address(le(shead, prefix + bitmap + 8 * d, 8));
            if (blockAt !== undefined) await dataBlock(blockAt, start + d * n, n, pages, d);
          }
        }
      }
      start += count * n;
    }
  }

  /** The mappings of a virtual dataset's layout message, from the global heap (§8.9). */
  async virtualMapping(layout: Uint8Array): Promise<unknown[]> {
    if (![4, 5].includes(u(layout, 0, 1)) || u(layout, 1, 1) !== 3) reject("not a virtual dataset layout");
    const at = address(le(layout, 2, 8));
    if (at === undefined) return [];
    const [where, n] = await this.globalObject(at, u(layout, 10, 4));
    const d = await this.read(where, n);
    if (u(d, 0, 1) !== 0) reject("unsupported virtual dataset mapping version");
    const count = le(d, 1, 8);
    let p = 9;
    const out: unknown[] = [];
    for (let i = 0n; i < count; i++) {
      const names: unknown[] = [];
      for (let j = 0; j < 2; j++) {
        const end = d.indexOf(0, p);
        if (end < 0) reject("truncated virtual dataset mapping");
        names.push(textJson(d.subarray(p, end)));
        p = end + 1;
      }
      let source: Record<string, unknown>, selection: Record<string, unknown>;
      [source, p] = parseSelection(d, p);
      [selection, p] = parseSelection(d, p);
      out.push({ file: names[0], dataset: names[1], source, selection });
    }
    return out;
  }

  private async fixedArray(
    ds: Dataset,
    at: number,
    filtered: boolean,
    nbytes: bigint,
    add: (coords: number[], at: number, size: bigint, mask: number) => void,
  ): Promise<void> {
    const maxgrid = maxGrid(ds, false) as number[];
    const h = await this.read(at, 28);
    if (latin1(h.subarray(0, 4)) !== "FAHD" || h[4] !== 0) reject(`no fixed array header at ${at}`);
    const client = h[5], entry = h[6], pageBits = h[7], count = le(h, 8, 8);
    const total = product(maxgrid);
    if (client !== Number(filtered) || count !== BigInt(total)) reject("the fixed array does not match the dataset");
    if (filtered ? entry < 13 || entry > 20 : entry !== 8) reject(`invalid fixed array entry size ${entry}`);
    const block = address(le(h, 16, 8));
    if (block === undefined) return;
    const prefix = await this.read(block, 14);
    if (latin1(prefix.subarray(0, 4)) !== "FADB" || prefix[4] !== 0 || prefix[5] !== client) {
      reject(`no fixed array data block at ${block}`);
    }
    const pages: [number, number, number][] = []; // [first entry, entries, address]
    if (BigInt(total) > 1n << BigInt(pageBits)) {
      const page = 2 ** pageBits;
      const npages = Math.ceil(total / page);
      const bitmap = await this.read(block + 14, Math.ceil(npages / 8));
      const start = block + 14 + bitmap.length + 4;
      for (let j = 0; j < npages; j++) {
        if (bitmap[j >> 3] & (0x80 >> (j % 8))) {
          pages.push([j * page, Math.min(page, total - j * page), start + j * (page * entry + 4)]);
        }
      }
    } else {
      pages.push([0, total, block + 14]);
    }
    for (const [first, n, pageAt] of pages) {
      const data = await this.read(pageAt, n * entry);
      for (let k = 0; k < n; k++) {
        const [chunkAt, size, mask] = this.entry(data, k, entry, filtered, nbytes);
        if (chunkAt === undefined) continue;
        const coords = unravel(first + k, maxgrid);
        if (coords.every((c, i) => c < ds.grid[i])) add(coords, chunkAt, size, mask);
      }
    }
  }
}

const product = (values: number[]) => values.reduce((n, v) => n * v, 1);

/** Coordinates as reasons give them: `[1, 2]`. */
export const showCoords = (values: (number | bigint)[]) => `[${values.join(", ")}]`;

/** The coordinates of index `i` in row-major order over `grid`. */
export function unravel(i: number, grid: number[]): number[] {
  const coords = new Array(grid.length).fill(0);
  for (let k = grid.length - 1; k >= 0; k--) {
    coords[k] = i % grid[k];
    i = Math.floor(i / grid[k]);
  }
  return coords;
}

export function ravel(coords: number[], grid: number[]): number {
  return coords.reduce((i, c, k) => i * grid[k] + c, 0);
}

/** The number of chunks along each dimension at the maximum dimensions (§8.5):
 * undefined for an unlimited one, which only `unlimited` allows. */
function maxGrid(ds: Dataset, unlimited: boolean): (number | undefined)[] {
  if (ds.maxdims === null) return [...ds.grid];
  const out = ds.maxdims.map((m, i) => {
    if (m === UNDEFINED && unlimited) return undefined;
    if (m === UNDEFINED || m > MAX_SAFE || m < BigInt(ds.dims![i])) {
      return reject("a maximum dimension that the chunk index does not allow");
    }
    return Math.ceil(Number(m) / ds.chunk[i]);
  });
  if (out.reduce((n: bigint, g) => n * BigInt(g ?? 1), 1n) > MAX_SAFE) reject("more than 2^53 - 1 chunks");
  return out;
}

/** The managed and huge objects of a fractal heap (§8.4). Heap offsets and
 * block sizes are bigints: many rows make them exceed 2^53. */
class FractalHeap {
  private f: Hdf5;
  private idLength = 0;
  private width = 0n;
  private start = 0n;
  private root: number | undefined;
  private rootRows = 0;
  private offsetSize = 0;
  private lengthSize = 0;
  private directRows = 0;
  private hugeTree: number | undefined;
  private hugeObjects: Map<bigint, [number, number]> | undefined;
  private blocks = new Set<string>();

  private constructor(f: Hdf5) {
    this.f = f;
  }

  static async open(f: Hdf5, at: number): Promise<FractalHeap> {
    const heap = new FractalHeap(f);
    const h = await f.read(at, 146);
    if (latin1(h.subarray(0, 4)) !== "FRHP" || h[4] !== 0) reject(`no fractal heap at ${at}`);
    heap.idLength = u(h, 5, 2);
    const filters = u(h, 7, 2), maxManaged = u(h, 10, 4);
    heap.width = le(h, 110, 2);
    heap.start = BigInt(length(le(h, 112, 8)));
    const maxDirect = BigInt(length(le(h, 120, 8)));
    const heapBits = u(h, 128, 2);
    heap.root = address(le(h, 132, 8));
    heap.rootRows = u(h, 140, 2);
    if (filters) reject("filtered fractal heaps are not supported");
    const power = (v: bigint) => v >= 1n && (v & (v - 1n)) === 0n;
    if (!(power(heap.width) && power(heap.start) && power(maxDirect) && maxDirect >= heap.start &&
      heapBits >= 1 && heapBits <= 64 && maxManaged >= 1)) {
      reject("invalid fractal heap parameters");
    }
    heap.offsetSize = Math.floor((heapBits + 7) / 8);
    heap.lengthSize = Math.min(Math.floor((log2(maxDirect) + 7) / 8), Math.floor(log2(maxManaged) / 8) + 1);
    heap.directRows = log2(maxDirect) - log2(heap.start) + 2;
    heap.hugeTree = address(le(h, 22, 8));
    return heap;
  }

  /** The object with this heap ID. */
  async get(id: Uint8Array): Promise<Uint8Array> {
    return this.f.read(...(await this.where(id)));
  }

  /** The [address, size] of the object with this heap ID: one run of the file. */
  async where(id: Uint8Array): Promise<[number, number]> {
    if (id.length !== this.idLength) reject("invalid fractal heap ID");
    const kind = id[0] >> 4;
    if (kind === 1) return this.huge(le(id, 1, Math.min(this.idLength - 1, 8)));
    if (kind !== 0 || id.length < 1 + this.offsetSize + this.lengthSize) {
      reject("only managed and huge fractal heap objects are supported");
    }
    const offset = le(id, 1, this.offsetSize);
    const n = le(id, 1 + this.offsetSize, this.lengthSize);
    if (this.root === undefined) return reject("empty fractal heap");
    const [block, start, size] = this.rootRows === 0
      ? [this.root, 0n, this.start]
      : await this.locate(this.root, this.rootRows, 0n, offset);
    if (!this.blocks.has(`${block} ${start}`)) {
      const head = await this.f.read(block, 13 + this.offsetSize);
      if (latin1(head.subarray(0, 4)) !== "FHDB" || head[4] !== 0 || le(head, 13, this.offsetSize) !== start) {
        reject(`no fractal heap direct block at ${block}`);
      }
      this.blocks.add(`${block} ${start}`);
    }
    if (offset < start || offset + n > start + size) reject("fractal heap object outside its block");
    return [block + Number(offset - start), Number(n)];
  }

  /** The huge object with this key, from the huge objects' B-tree. */
  private async huge(key: bigint): Promise<[number, number]> {
    if (this.hugeObjects === undefined) {
      if (this.hugeTree === undefined) return reject("huge fractal heap object without a B-tree");
      const objects = new Map<bigint, [number, number]>();
      for (const record of await this.f.btree2(this.hugeTree, 1)) {
        const at = address(le(record, 0, 8)), n = length(le(record, 8, 8)), k = le(record, 16, 8);
        if (at === undefined || objects.has(k)) return reject("invalid huge object record");
        objects.set(k, [at, n]);
      }
      this.hugeObjects = objects;
    }
    const object = this.hugeObjects.get(key);
    if (object === undefined) return reject(`no huge fractal heap object ${key}`);
    return object;
  }

  /** The direct block [address, heap offset, size] holding `offset`, under the
   * indirect block at `at` with `rows` rows starting at `start`. */
  private async locate(at: number, rows: number, start: bigint, offset: bigint): Promise<[number, bigint, bigint]> {
    const head = await this.f.read(at, 13 + this.offsetSize);
    if (latin1(head.subarray(0, 4)) !== "FHIB" || head[4] !== 0 || le(head, 13, this.offsetSize) !== start) {
      reject(`no fractal heap indirect block at ${at}`);
    }
    let pos = start;
    for (let r = 0; r < rows; r++) {
      const size = r === 0 ? this.start : this.start << BigInt(r - 1);
      if (offset < pos + size * this.width) {
        const col = (offset - pos) / size;
        const entryAt = BigInt(at + 13 + this.offsetSize) + 8n * (BigInt(r) * this.width + col);
        if (entryAt + 8n > BigInt(this.f.size)) reject("fractal heap block entry outside the file");
        const child = address(le(await this.f.read(Number(entryAt), 8), 0, 8));
        if (child === undefined) return reject("fractal heap object in an unallocated block");
        if (r < this.directRows) return [child, pos + col * size, size];
        const childRows = log2(size) - log2(this.start * this.width) + 1;
        if (childRows < 1) reject("invalid fractal heap indirect block");
        return this.locate(child, childRows, pos + col * size, offset);
      }
      pos += size * this.width;
    }
    return reject("fractal heap offset outside the heap");
  }
}

// ---- message bodies

/** A link message: [name, target] (§8.3). */
function parseLink(d: Uint8Array): [string, Target] {
  if (u(d, 0, 1) !== 1) reject("unsupported link message version");
  const flags = u(d, 1, 1);
  let p = 2, kind = 0;
  if (flags & 8) kind = u(d, p++, 1);
  p += (flags & 4 ? 8 : 0) + (flags & 16 ? 1 : 0);
  const n = 1 << (flags & 3);
  const nameLength = le(d, p, n);
  p += n;
  if (BigInt(p) + nameLength > BigInt(d.length)) reject("truncated link message");
  const end = p + Number(nameLength);
  const name = latin1(d.subarray(p, end));
  if (kind === 1) {
    const m = u(d, end, 2);
    if (end + 2 + m > d.length) reject("truncated soft link");
    return [name, { soft: latin1(d.subarray(end + 2, end + 2 + m)) }];
  }
  if (kind !== 0) {
    // Any other type: its value, or null when it does not lie within the message (§8.9).
    const m = end + 2 <= d.length ? u(d, end, 2) : undefined;
    return [name, { other: kind, value: m !== undefined && end + 2 + m <= d.length ? d.subarray(end + 2, end + 2 + m) : null }];
  }
  const target = address(le(d, end, 8));
  if (target === undefined) return reject("hard link to an undefined address");
  return [name, { hard: target }];
}

/** [flags, name size, datatype size, dataspace size, name start] of an attribute message. */
function attributeLayout(d: Uint8Array): [number, number, number, number, number] {
  const version = u(d, 0, 1);
  if (version < 1 || version > 3) reject(`unsupported attribute message version ${version}`);
  return [version > 1 ? u(d, 1, 1) : 0, u(d, 2, 2), u(d, 4, 2), u(d, 6, 2), version === 3 ? 9 : 8];
}

function attributeName(d: Uint8Array): string {
  const [, nameSize, , , p] = attributeLayout(d);
  if (p + nameSize > d.length) reject("truncated attribute message");
  return latin1(untilNul(d.subarray(p, p + nameSize)));
}

const pad8 = (n: number) => Math.ceil(n / 8) * 8;

/** An attribute's datatype and data (§8.6). */
export function attributeValue(d: Uint8Array): [Datatype, Uint8Array] {
  const [typeMessage, , data] = attributeParts(d);
  return [parseDatatype(typeMessage), data];
}

/** An attribute's [datatype message, dimensions, data] (§8.6). */
export function attributeParts(d: Uint8Array): [Uint8Array, number[], Uint8Array] {
  const [flags, nameSize, typeSize, spaceSize, start] = attributeLayout(d);
  if (flags & 3) reject("attribute with a shared datatype or dataspace");
  const pad = u(d, 0, 1) === 1 ? pad8 : (n: number) => n;
  let p = start + pad(nameSize);
  if (p + typeSize > d.length) reject("truncated attribute message");
  const typeMessage = d.subarray(p, p + typeSize);
  const datatype = parseDatatype(typeMessage);
  p += pad(typeSize);
  if (p + spaceSize > d.length) reject("truncated attribute message");
  const space = parseDataspace(d.subarray(p, p + spaceSize));
  p += pad(spaceSize);
  if (space.dims === null) return reject("attribute with a null dataspace");
  const n = space.dims.reduce((k, x) => k * BigInt(x), 1n) * BigInt(datatype.size);
  if (BigInt(p) + n > BigInt(d.length)) reject("truncated attribute data");
  return [typeMessage, space.dims, d.subarray(p, p + Number(n))];
}

/** The shared datatype message of an attribute whose datatype is shared, else undefined. */
export function attributeSharedType(d: Uint8Array): Uint8Array | undefined {
  const [flags, nameSize, typeSize, , start] = attributeLayout(d);
  if (!(flags & 1)) return undefined;
  const pad = u(d, 0, 1) === 1 ? pad8 : (n: number) => n;
  const p = start + pad(nameSize);
  if (p + typeSize > d.length) reject("truncated attribute message");
  return d.subarray(p, p + typeSize);
}

/** An attribute's [datatype message, dimensions or null for a null dataspace,
 * data], with `committed` the datatype message of its shared datatype (§8.9). */
export function attributeFull(d: Uint8Array, committed?: Uint8Array): [Uint8Array, number[] | null, Uint8Array, number] {
  const [flags, nameSize, typeSize, spaceSize, start] = attributeLayout(d);
  if (flags & 2) reject("an attribute with a shared dataspace");
  const pad = u(d, 0, 1) === 1 ? pad8 : (n: number) => n;
  let p = start + pad(nameSize);
  if (p + typeSize > d.length) reject("truncated attribute message");
  let typeMessage = d.subarray(p, p + typeSize);
  if (flags & 1) {
    if (committed === undefined) return reject("an attribute with a shared datatype");
    typeMessage = committed;
  }
  const datatype = parseDatatype(typeMessage);
  p += pad(typeSize);
  if (p + spaceSize > d.length) reject("truncated attribute message");
  const space = parseDataspace(d.subarray(p, p + spaceSize));
  p += pad(spaceSize);
  if (space.dims === null) return [typeMessage, null, new Uint8Array(0), p];
  const n = space.dims.reduce((k, x) => k * BigInt(x), 1n) * BigInt(datatype.size);
  if (BigInt(p) + n > BigInt(d.length)) reject("truncated attribute data");
  return [typeMessage, space.dims, d.subarray(p, p + Number(n)), p];
}

/** The fill value of a fill value message, or no bytes when it gives none (§8.5). */
export function fillValue(d: Uint8Array): Uint8Array {
  const version = u(d, 0, 1);
  let size: number, p: number;
  if (version === 1 || version === 2) {
    if (version === 2 && !u(d, 3, 1)) return new Uint8Array(0);
    [size, p] = [u(d, 4, 4), 8];
  } else if (version === 3) {
    if (!(u(d, 1, 1) & 0x20)) return new Uint8Array(0);
    [size, p] = [u(d, 2, 4), 6];
  } else {
    return reject(`unsupported fill value message version ${version}`);
  }
  if (p + size > d.length) reject("truncated fill value message");
  return d.subarray(p, p + size);
}

function parseFilters(d: Uint8Array | undefined): number[] {
  if (d === undefined) return [];
  const version = u(d, 0, 1), count = u(d, 1, 1);
  if (version !== 1 && version !== 2) reject(`unsupported filter pipeline message version ${version}`);
  let p = version === 1 ? 8 : 2;
  const ids: number[] = [];
  for (let i = 0; i < count; i++) {
    const id = u(d, p, 2);
    let nameLength = 0;
    if (version === 1 || id >= 256) {
      nameLength = u(d, p + 2, 2);
      p += 4;
    } else {
      p += 2;
    }
    const values = u(d, p + 2, 2);
    p += 4;
    p += version === 1 ? pad8(nameLength) : nameLength;
    p += 4 * values + (version === 1 && values % 2 ? 4 : 0);
    if (p > d.length) reject("truncated filter pipeline message");
    ids.push(id);
  }
  return ids;
}

/** The serialized dataspace selection at `p`: its description
 * (spec/virtualize/ims.md §5.3) and where it ends (§8.9). */
export function parseSelection(d: Uint8Array, p: number): [Record<string, unknown>, number] {
  const kind = u(d, p, 4), version = u(d, p + 4, 4);
  p += 8;
  let flags = 0, enc: number;
  if (kind === 0 || kind === 3) {
    if (version !== 1) reject(`unsupported selection version ${version}`);
    le(d, p, 8);
    return [{ select: kind === 0 ? "none" : "all" }, p + 8];
  }
  if (kind === 1) {
    if (version === 1) [enc, p] = [4, p + 8];
    else if (version === 2) [enc, p] = [u(d, p, 1), p + 1];
    else return reject(`unsupported selection version ${version}`);
  } else if (kind === 2) {
    if (version === 1) [enc, p] = [4, p + 8];
    else if (version === 2) [flags, enc, p] = [u(d, p, 1), 8, p + 5];
    else if (version === 3) [flags, enc, p] = [u(d, p, 1), u(d, p + 1, 1), p + 2];
    else return reject(`unsupported selection version ${version}`);
    if (flags & ~1) reject("unknown selection flags");
  } else {
    return reject(`unknown selection type ${kind}`);
  }
  if (![2, 4, 8].includes(enc)) reject(`invalid selection encoding size ${enc}`);
  const rank = u(d, p, 4);
  p += 4;
  const ones = (1n << BigInt(8 * enc)) - 1n;
  if (!(flags & 1) && rank === 0) reject("a selection of rank 0");
  const values = (q: number, n: number) => Array.from({ length: n }, (_, i) => jsonNumber(le(d, q + enc * i, enc)));
  if (flags & 1) { // a regular hyperslab: start, stride, count and block of each dimension
    if (4 * rank * enc > d.length - p) reject("truncated selection");
    const names = ["start", "stride", "count", "block"];
    const out: Record<string, unknown> = { select: "hyperslab", rank };
    const lists: unknown[][] = names.map(() => []);
    for (let i = 0; i < rank; i++) {
      for (let j = 0; j < 4; j++) {
        const v = le(d, p + enc * (4 * i + j), enc);
        lists[j].push(j >= 2 && v === ones ? "unlimited" : jsonNumber(v));
      }
    }
    names.forEach((name, j) => { out[name] = lists[j]; });
    return [out, p + 4 * rank * enc];
  }
  const n = le(d, p, enc);
  p += enc;
  const width = (kind === 2 ? 2 : 1) * rank * enc; // a block's start and end, or a point
  if (n > BigInt(d.length - p) || n * BigInt(width) > BigInt(d.length - p)) reject("truncated selection");
  const count = Number(n);
  if (kind === 1) {
    const points = Array.from({ length: count }, (_, i) => values(p + width * i, rank));
    return [{ select: "points", rank, points }, p + count * width];
  }
  const blocks = Array.from({ length: count }, (_, i) => [values(p + width * i, rank), values(p + width * i + rank * enc, rank)]);
  return [{ select: "hyperslab", rank, blocks }, p + count * width];
}
