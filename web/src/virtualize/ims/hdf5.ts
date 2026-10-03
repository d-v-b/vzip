// Reading the structure of an HDF5 file through range reads (§8.1–§8.6): the
// superblock, object headers and their messages, the links of groups,
// attributes, and the chunk index of a chunked dataset. Pixel data is never
// read, and checksums are not checked.

import type { ByteReader } from "../common.ts";

export class ImsError extends Error {}

const reject = (message: string): never => {
  throw new ImsError(message);
};

const SIGNATURE = [0x89, 0x48, 0x44, 0x46, 0x0d, 0x0a, 0x1a, 0x0a];
const UNDEFINED = (1n << 64n) - 1n;
const MAX_SAFE = BigInt(Number.MAX_SAFE_INTEGER);
const MAX_BLOCKS = 1000; // blocks of one object header (§8.2)
const MAX_BTREE2_DEPTH = 16; // §8.4
// The record size of each type of version 2 B-tree read: huge fractal heap
// objects, link names and attribute names (§8.4).
const RECORD_SIZES: Record<number, number> = { 1: 24, 5: 11, 8: 17 };

// Message types (§8.2).
const DATASPACE = 0x01, LINK_INFO = 0x02, DATATYPE = 0x03, FILL_VALUE = 0x05, LINK = 0x06, LAYOUT = 0x08;
const FILTERS = 0x0b, ATTRIBUTE = 0x0c, CONTINUATION = 0x10, SYMBOL_TABLE = 0x11, ATTRIBUTE_INFO = 0x15;

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
function address(v: bigint): number | undefined {
  if (v === UNDEFINED) return undefined;
  if (v > MAX_SAFE) reject(`HDF5 address ${v} is too large`);
  return Number(v);
}

function length(v: bigint): number {
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
}

export interface Datatype {
  cls: number; // 0 fixed-point, 1 floating-point, 3 string, ...
  size: number;
  bits: number; // the class bit fields
  props: Uint8Array;
}

interface Dataspace {
  dims: number[] | null; // null for a null dataspace; [] for a scalar
  maxdims: bigint[] | null;
}

export interface Dataset {
  dims: number[];
  maxdims: bigint[] | null;
  datatype: Datatype;
  chunk: number[]; // chunk dimensions, without the element size
  grid: number[]; // chunks per dimension
  filters: number[]; // filter identifiers, in pipeline order
  index: "btree1" | "single" | "farray";
  indexAddress: number | undefined;
  singleSize: number | undefined; // single chunk index: the filtered chunk's size
}

/** A link's target: an object header address (a hard link), a path (a soft
 * link), or null (any other kind of link). */
export type Target = { hard: number } | { soft: string } | null;

/** A group's links by name. Names and paths are kept as latin1 text of their bytes. */
export type Links = Map<string, Target>;

function parseDatatype(d: Uint8Array): Datatype {
  const cv = u(d, 0, 1);
  if (cv >> 4 < 1 || cv >> 4 > 3) reject(`unsupported datatype message version ${cv >> 4}`);
  return { cls: cv & 15, size: u(d, 4, 4), bits: u(d, 1, 3), props: d.subarray(8) };
}

function parseDataspace(d: Uint8Array): Dataspace {
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

  private constructor(read: ByteReader, size: number) {
    this.read = read;
    this.size = size;
  }

  static async open(read: ByteReader, size: number): Promise<Hdf5> {
    const f = new Hdf5(read, size);
    const head = await read(0, 9);
    if (!SIGNATURE.every((b, i) => head[i] === b)) reject("not an HDF5 file");
    const version = head[8];
    let sb: Uint8Array, baseAt: number, sizes: number[], rootAt: number;
    if (version === 0 || version === 1) {
      sb = await read(0, version === 1 ? 76 : 72);
      baseAt = version === 1 ? 28 : 24;
      [sizes, rootAt] = [[sb[13], sb[14]], baseAt + 40];
    } else if (version === 2 || version === 3) {
      sb = await read(0, 48);
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
          messages.push({ type, flags: mflags, data: mdata });
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
    if (target === null || target === undefined) return reject(`link ${JSON.stringify(name)} is neither hard nor soft`);
    if ("hard" in target) return target.hard;
    const path = target.soft;
    if (!path.startsWith("/")) reject(`soft link ${JSON.stringify(name)} to a relative path`);
    let at = this.root;
    for (const part of path === "/" ? [] : path.slice(1).split("/")) {
      const step = (await this.links(at)).get(part);
      if (step === null || step === undefined || !("hard" in step)) {
        return reject(`soft link ${JSON.stringify(name)} to ${JSON.stringify(path)} does not resolve through hard links`);
      }
      at = step.hard;
    }
    return at;
  }

  private async localHeap(at: number): Promise<Uint8Array> {
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
        out.push(...await this.btree1(child, nodeType, keySize, seen, nodeLevel - 1));
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

  /** The records of the version 2 B-tree at `at`, which MUST have type `type`. */
  async btree2(at: number, type: number): Promise<Uint8Array[]> {
    const h = await this.read(at, 38);
    if (latin1(h.subarray(0, 4)) !== "BTHD" || h[4] !== 0 || h[5] !== type) {
      reject(`no version 2 B-tree of type ${type} at ${at}`);
    }
    const nodeSize = u(h, 6, 4), recordSize = u(h, 10, 2), depth = u(h, 12, 2);
    const root = address(le(h, 16, 8));
    const rootCount = u(h, 24, 2);
    if (depth > MAX_BTREE2_DEPTH) reject(`version 2 B-tree depth ${depth}`);
    if (recordSize !== RECORD_SIZES[type] || nodeSize < 10 + recordSize) {
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

  /** An object's attributes: name -> attribute message. */
  async attributes(at: number): Promise<Map<string, Uint8Array>> {
    const messages = await this.header(at);
    const out = new Map<string, Uint8Array>();
    const add = (data: Uint8Array) => {
      const name = attributeName(data);
      if (out.has(name)) reject(`two attributes named ${JSON.stringify(name)}`);
      out.set(name, data);
    };
    for (const m of messages) {
      if (m.type !== ATTRIBUTE) continue;
      if (m.flags & 2) reject("shared attribute message");
      add(m.data);
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
          add(await fh.get(record.subarray(0, 8)));
        }
      }
    }
    return out;
  }

  // ---- datasets (§8.5)

  async dataset(at: number): Promise<Dataset> {
    const messages = await this.header(at);
    const space = parseDataspace(Hdf5.message(messages, DATASPACE, true));
    const datatype = parseDatatype(Hdf5.message(messages, DATATYPE, true));
    const fill = Hdf5.message(messages, FILL_VALUE, false);
    const filters = parseFilters(Hdf5.message(messages, FILTERS, false));
    const layout = Hdf5.message(messages, LAYOUT, true);
    if (fill !== undefined && !fillIsZero(fill)) reject("the fill value is not zero");
    if (space.dims === null) return reject("dataset with a null dataspace");
    const rank = space.dims.length;
    const version = u(layout, 0, 1), cls = u(layout, 1, 1);
    if (version < 3 || version > 5) reject(`unsupported layout message version ${version}`);
    if (cls !== 2) reject(`dataset layout class ${cls} is not chunked`);
    let singleSize: number | undefined;
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
          if (le(layout, p + 8, 4) !== 0n) reject("a chunk's filter mask is not 0");
          p += 12;
        }
      } else if (itype === 3) {
        index = "farray";
        le(layout, p, 1); // page bits: the fixed array header's are used
        p += 1;
      } else {
        return reject(`unsupported chunk index type ${itype}`);
      }
      indexAddress = address(le(layout, p, 8));
      if (filters.length && flags & 1) reject("partial edge chunks are not filtered");
      if (filters.length && index === "single" && !(flags & 2)) reject("single filtered chunk without its size");
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
    };
  }

  /** The allocated chunks: grid coordinates (joined by "/") -> [address, size] (§8.5). */
  async chunks(ds: Dataset): Promise<Map<string, [number, number]>> {
    const out = new Map<string, [number, number]>();
    const nbytes = ds.chunk.reduce((n, c) => n * BigInt(c), BigInt(ds.datatype.size));
    const filtered = ds.filters.length > 0;
    const add = (coords: number[], at: number, size: bigint) => {
      const key = coords.join("/");
      if (out.has(key)) reject(`chunk ${key} indexed twice`);
      if (!filtered && size !== nbytes) reject(`unfiltered chunk ${key} of ${size} bytes, not ${nbytes}`);
      if (size < 1n) reject(`chunk ${key} is empty`);
      if (BigInt(at) + size > BigInt(this.size)) reject(`chunk ${key} lies outside the file`);
      out.set(key, [at, Number(size)]);
    };
    const at = ds.indexAddress;
    if (at === undefined) return out;
    const rank = ds.dims.length;
    if (ds.index === "btree1") {
      for (const [key, child] of await this.btree1(at, 1, 8 + 8 * (rank + 1))) {
        const size = le(key, 0, 4), mask = le(key, 4, 4);
        const offsets = Array.from({ length: rank + 1 }, (_, i) => le(key, 8 + 8 * i, 8));
        if (mask) reject("a chunk's filter mask is not 0");
        const bad = offsets[rank] !== 0n ||
          ds.chunk.some((c, i) => offsets[i] % BigInt(c) !== 0n || offsets[i] >= BigInt(ds.dims[i]));
        if (bad) reject(`invalid chunk offset ${offsets}`);
        add(ds.chunk.map((c, i) => Number(offsets[i]) / c), child, size);
      }
    } else if (ds.index === "single") {
      if (ds.grid.some((g) => g !== 1)) reject("single chunk index for more than one chunk");
      add(new Array(rank).fill(0), at, filtered ? BigInt(ds.singleSize!) : nbytes);
    } else {
      await this.fixedArray(ds, at, filtered, nbytes, add);
    }
    return out;
  }

  private async fixedArray(
    ds: Dataset,
    at: number,
    filtered: boolean,
    nbytes: bigint,
    add: (coords: number[], at: number, size: bigint) => void,
  ): Promise<void> {
    if (ds.maxdims !== null && ds.maxdims.some((m, i) => m !== BigInt(ds.dims[i]))) {
      reject("fixed array index with maximum dimensions other than the dimensions");
    }
    const h = await this.read(at, 28);
    if (latin1(h.subarray(0, 4)) !== "FAHD" || h[4] !== 0) reject(`no fixed array header at ${at}`);
    const client = h[5], entry = h[6], pageBits = h[7], count = le(h, 8, 8);
    const total = ds.grid.reduce((n, g) => n * g, 1);
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
        const chunkAt = address(le(data, k * entry, 8));
        if (chunkAt === undefined) continue;
        let size = nbytes;
        if (filtered) {
          size = BigInt(length(le(data, k * entry + 8, entry - 12)));
          if (le(data, k * entry + entry - 4, 4) !== 0n) reject("a chunk's filter mask is not 0");
        }
        let rest = first + k;
        const coords: number[] = [];
        for (let i = ds.grid.length - 1; i >= 0; i--) {
          coords.unshift(rest % ds.grid[i]);
          rest = Math.floor(rest / ds.grid[i]);
        }
        add(coords, chunkAt, size);
      }
    }
  }
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
    return this.f.read(block + Number(offset - start), Number(n));
  }

  /** The huge object with this key, from the huge objects' B-tree. */
  private async huge(key: bigint): Promise<Uint8Array> {
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
    return this.f.read(...object);
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
  if (kind !== 0) return [name, null];
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
  const [flags, nameSize, typeSize, spaceSize, start] = attributeLayout(d);
  if (flags & 3) reject("attribute with a shared datatype or dataspace");
  const pad = u(d, 0, 1) === 1 ? pad8 : (n: number) => n;
  let p = start + pad(nameSize);
  if (p + typeSize > d.length) reject("truncated attribute message");
  const datatype = parseDatatype(d.subarray(p, p + typeSize));
  p += pad(typeSize);
  if (p + spaceSize > d.length) reject("truncated attribute message");
  const space = parseDataspace(d.subarray(p, p + spaceSize));
  p += pad(spaceSize);
  if (space.dims === null) return reject("attribute with a null dataspace");
  const n = space.dims.reduce((k, x) => k * BigInt(x), 1n) * BigInt(datatype.size);
  if (BigInt(p) + n > BigInt(d.length)) reject("truncated attribute data");
  return [datatype, d.subarray(p, p + Number(n))];
}

/** Whether a fill value message gives no fill value, or zero (§8.5). */
function fillIsZero(d: Uint8Array): boolean {
  const version = u(d, 0, 1);
  let size: number, p: number;
  if (version === 1 || version === 2) {
    if (version === 2 && !u(d, 3, 1)) return true;
    [size, p] = [u(d, 4, 4), 8];
  } else if (version === 3) {
    if (!(u(d, 1, 1) & 0x20)) return true;
    [size, p] = [u(d, 2, 4), 6];
  } else {
    return reject(`unsupported fill value message version ${version}`);
  }
  if (p + size > d.length) reject("truncated fill value message");
  return d.subarray(p, p + size).every((b) => b === 0);
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
