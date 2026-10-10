// Reading a CZI file's segments (spec/virtualize/czi.md §2, spec/virtualize.md §13.2).

import { batched, type ByteReader, Prefetched } from "../../../../../js/src/virtualize/common.ts";
import { COMPRESSIONS, PIXEL_TYPES } from "./coding.ts";

/** The input is not accepted by the CZI profile (spec/virtualize.md §1.2). */
export class CziError extends Error {}

export const reject = (message: string): never => {
  throw new CziError(message);
};

export const MAX_SAFE = Number.MAX_SAFE_INTEGER;
const MAX_ENTRIES = 2 ** 21; // directory entries (spec/virtualize.md §13.3)
const MAX_ATTACHMENTS = 2 ** 16; // attachment entries
const MAX_WALK = 2 ** 23; // segments the walk visits
export const LETTERS = "XYZCTRSIHVBM";

export const dv = (b: Uint8Array) => new DataView(b.buffer, b.byteOffset, b.byteLength);
/** A signed 64-bit field, as the nearest number: any value past 2^53 - 1 stays past it. */
export const i64 = (v: DataView, at: number) => Number(v.getBigInt64(at, true));

export function segId(name: string): Uint8Array {
  const out = new Uint8Array(16);
  for (let i = 0; i < name.length; i++) out[i] = name.charCodeAt(i);
  return out;
}

export const FILE = segId("ZISRAWFILE");
export const DIRECTORY = segId("ZISRAWDIRECTORY");
export const SUBBLOCK = segId("ZISRAWSUBBLOCK");
export const METADATA = segId("ZISRAWMETADATA");
export const ATTDIR = segId("ZISRAWATTDIR");
export const ATTACH = segId("ZISRAWATTACH");

export function equal(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

const name = (id: Uint8Array) => String.fromCharCode(...id).replace(/\0+$/, "");

function offset(v: number, what: string): number {
  if (!(v >= 0 && v <= MAX_SAFE)) reject(`${what} ${v} is not an offset from 0 to 2^53 - 1`);
  return v;
}

function size(v: number, what: string): number {
  if (!(v >= 0 && v <= MAX_SAFE)) reject(`${what} ${v} is not a size from 0 to 2^53 - 1`);
  return v;
}

function within(o: number, n: number, fileSize: number, what: string): void {
  if (o + n > fileSize) reject(`${what} [${o}, ${o + n}) is outside the ${fileSize}-byte file`);
}

/** [AllocatedSize, UsedSize] of the segment header `h` read at `o`, which must have the id `expected`. */
function checkHeader(h: Uint8Array, o: number, expected: Uint8Array, what: string): [number, number] {
  if (!equal(h.subarray(0, 16), expected)) reject(`${what} at ${o} is not a ${name(expected)} segment`);
  const v = dv(h);
  return [size(i64(v, 16), `${what}'s AllocatedSize`), size(i64(v, 24), `${what}'s UsedSize`)];
}

async function segmentHeader(
  read: ByteReader, fileSize: number, o: number, expected: Uint8Array, what: string,
): Promise<[number, number]> {
  offset(o, `${what}'s position`);
  within(o, 32, fileSize, `${what}'s segment header`);
  return checkHeader(await read(o, 32), o, expected, what);
}

/** A GUID as text (spec/virtualize/czi.md §2.2). */
export function guid(b: Uint8Array): string {
  const v = dv(b);
  const h = (x: number, n: number) => x.toString(16).padStart(n, "0");
  const bytes = (s: Uint8Array) => Array.from(s, (x) => h(x, 2)).join("");
  return `${h(v.getUint32(0, true), 8)}-${h(v.getUint16(4, true), 4)}-${h(v.getUint16(6, true), 4)}-` +
    `${bytes(b.subarray(8, 10))}-${bytes(b.subarray(10, 16))}`;
}

export interface FileHeader {
  major: number;
  minor: number;
  primaryFileGuid: Uint8Array;
  fileGuid: Uint8Array;
  filePart: number;
  directory: number;
  metadata: number;
  updatePending: number;
  attachments: number;
  allocated: number;
}

export async function readFileHeader(read: ByteReader, fileSize: number): Promise<FileHeader> {
  const [allocated] = await segmentHeader(read, fileSize, 0, FILE, "the file header");
  within(32, 80, fileSize, "the file header");
  const d = await read(32, 80);
  const v = dv(d);
  const major = v.getInt32(0, true), minor = v.getInt32(4, true);
  const filePart = v.getInt32(48, true);
  if (major !== 1) reject(`CZI file version ${major}.${minor} (Major MUST be 1)`);
  if (filePart !== 0) reject(`FilePart ${filePart}: a CZI split over several files is not supported`);
  return {
    major, minor, primaryFileGuid: d.slice(16, 32), fileGuid: d.slice(32, 48), filePart,
    directory: i64(v, 52), metadata: i64(v, 60), updatePending: v.getInt32(68, true), attachments: i64(v, 72),
    allocated,
  };
}

/** A dimension letter's directory columns. */
export interface Column {
  start: Int32Array;
  size: Int32Array;
  stored: Int32Array;
  coordinate: Uint32Array; // the float32's bits
  position: Int8Array;
}

/** The subblock directory as columns: entry i's fields at index i. */
export interface Directory {
  offset: number;
  allocated: number;
  count: number;
  pixelType: Int32Array;
  compression: Int32Array;
  pyramidType: Uint8Array;
  spare: Uint8Array; // 5 bytes per entry: bytes 23-27
  filePosition: Float64Array;
  raw: Uint8Array; // the entries, back to back
  starts: Float64Array; // entry i is raw[starts[i]:starts[i + 1]]
  columns: Map<string, Column>;
}

export function entry(d: Directory, i: number): Uint8Array {
  return d.raw.subarray(d.starts[i], d.starts[i + 1]);
}

/** [Start, Size, StoredSize] of entry i's dimension `letter`, or undefined. */
export function dim(d: Directory, i: number, letter: string): [number, number, number] | undefined {
  const c = d.columns.get(letter);
  if (c === undefined || c.position[i] < 0) return undefined;
  return [c.start[i], c.size[i], c.stored[i]];
}

export async function readDirectory(
  read: ByteReader, fileSize: number, o: number, exact: ByteReader = read,
): Promise<Directory> {
  let [allocated, used] = await segmentHeader(read, fileSize, o, DIRECTORY, "the subblock directory");
  used = used || allocated;
  within(o + 32, 4, fileSize, "the directory's EntryCount");
  const count = dv(await read(o + 32, 4)).getInt32(0, true);
  if (!(count >= 0 && count <= MAX_ENTRIES)) {
    reject(`the directory's EntryCount ${count} is not from 0 to ${MAX_ENTRIES}`);
  }
  const end = o + 32 + used;
  const d: Directory = {
    offset: o, allocated, count, pixelType: new Int32Array(count), compression: new Int32Array(count),
    pyramidType: new Uint8Array(count), spare: new Uint8Array(5 * count), filePosition: new Float64Array(count),
    raw: new Uint8Array(), starts: new Float64Array(count + 1), columns: new Map(),
  };
  let raw = new Uint8Array(Math.min(1 << 16, 272 * count));
  let rawLength = 0;
  let pos = o + 160;
  let window: Uint8Array = new Uint8Array(), wstart = pos; // a window of the entries' bytes, read in steps of up to 1 MiB
  let i = 0;
  const take = async (at: number, n: number): Promise<Uint8Array> => {
    if (at + n > end) reject(`directory entry ${i} does not end within the directory's ${used} used bytes`);
    if (at < wstart || at + n > wstart + window.length) {
      within(at, n, fileSize, "a directory entry");
      wstart = at;
      window = await exact(at, Math.max(n, Math.min(1 << 20, end - at, fileSize - at)));
    }
    return window.subarray(at - wstart, at - wstart + n);
  };
  for (; i < count; i++) {
    const head = await take(pos, 32);
    if (head[0] !== 0x44 || head[1] !== 0x56) {
      reject(`directory entry ${i} has schema ${JSON.stringify(String.fromCharCode(head[0], head[1]))}, not DV`);
    }
    const hv = dv(head);
    const pixelType = hv.getInt32(2, true), filePosition = i64(hv, 6), filePart = hv.getInt32(14, true);
    const compression = hv.getInt32(18, true);
    const dims = hv.getInt32(28, true);
    if (filePart !== 0) reject(`directory entry ${i} is in FilePart ${filePart}`);
    if (!Object.hasOwn(PIXEL_TYPES, pixelType)) reject(`directory entry ${i} has an unsupported PixelType ${pixelType}`);
    if (!Object.hasOwn(COMPRESSIONS, compression)) {
      reject(`directory entry ${i} has an unsupported Compression ${compression}`);
    }
    if (!COMPRESSIONS[compression][1].has(pixelType)) {
      reject(`directory entry ${i}: Compression ${compression} with PixelType ${pixelType}`);
    }
    if (!(dims >= 2 && dims <= 12)) reject(`directory entry ${i} has DimensionCount ${dims}, not 2 to 12`);
    const headCopy = head.slice();
    const body = (await take(pos + 32, 20 * dims)).slice();
    const bv = dv(body);
    d.pixelType[i] = pixelType;
    d.compression[i] = compression;
    d.filePosition[i] = filePosition;
    d.pyramidType[i] = headCopy[22];
    d.spare.set(headCopy.subarray(23, 28), 5 * i);
    const seen = new Set<string>();
    for (let k = 0; k < dims; k++) {
      const id = body.subarray(20 * k, 20 * k + 4);
      const letter = id[1] === 0 && id[2] === 0 && id[3] === 0 && id[0] >= 0x41 && id[0] <= 0x5a
        ? String.fromCharCode(id[0])
        : undefined;
      if (letter === undefined || !LETTERS.includes(letter)) {
        reject(`directory entry ${i} has a dimension ${JSON.stringify(String.fromCharCode(...id))}`);
      }
      const l = letter!;
      if (seen.has(l)) reject(`directory entry ${i} has the dimension ${l} twice`);
      seen.add(l);
      const start = bv.getInt32(20 * k + 4, true), sz = bv.getInt32(20 * k + 8, true);
      const coordinate = bv.getUint32(20 * k + 12, true), stored = bv.getInt32(20 * k + 16, true);
      if (l === "X" || l === "Y") {
        if (sz < 1 || stored < 1) reject(`directory entry ${i}'s ${l} Size and StoredSize must be at least 1`);
      } else if (l !== "M" && (sz !== 1 || stored !== 1)) {
        reject(`directory entry ${i}'s ${l} Size and StoredSize must be 1`);
      }
      let c = d.columns.get(l);
      if (c === undefined) {
        c = {
          start: new Int32Array(count), size: new Int32Array(count), stored: new Int32Array(count),
          coordinate: new Uint32Array(count), position: new Int8Array(count).fill(-1),
        };
        d.columns.set(l, c);
      }
      c.start[i] = start;
      c.size[i] = sz;
      c.stored[i] = stored;
      c.coordinate[i] = coordinate;
      c.position[i] = k;
    }
    if (!seen.has("X") || !seen.has("Y")) reject(`directory entry ${i} lacks the X or Y dimension`);
    const n = 32 + 20 * dims;
    if (rawLength + n > raw.length) {
      const grown = new Uint8Array(Math.max(2 * raw.length, rawLength + n));
      grown.set(raw.subarray(0, rawLength));
      raw = grown;
    }
    raw.set(headCopy, rawLength);
    raw.set(body, rawLength + 32);
    rawLength += n;
    pos += n;
    d.starts[i + 1] = rawLength;
  }
  d.raw = raw.slice(0, rawLength);
  return d;
}

/** Each subblock's header: its segment's AllocatedSize, header length L, and part sizes. */
export interface Subblocks {
  allocated: Float64Array;
  length: Int32Array;
  metadata: Int32Array;
  attachment: Int32Array;
  data: Float64Array;
  agrees: Uint8Array; // 1 where its copy of its entry agrees with the directory
  copyLength: Int32Array; // the length of its copy of its entry, 32 + 20 d'
}

const FIRST = 32 + 288; // the segment header, and L for up to 12 dimensions
const PREFETCH_HEAD = 2048; // the bytes read at a subblock's segment
export const PREFETCH_DATA = 1024; // the bytes of a subblock's data read for its codec header
const MAX_GAP = 2 ** 14; // prefetched ranges this close are read as one
const MAX_MERGED = 2 ** 20; // up to this many bytes

/** Sorted [offset, length] ranges, those less than 16 KiB apart merged into reads of up
 * to 1 MiB: a file of many small subblocks then costs few requests. */
export function coalesce(ranges: [number, number][]): [number, number][] {
  const out: [number, number][] = [];
  for (const [o, n] of [...ranges].sort((a, b) => a[0] - b[0] || a[1] - b[1])) {
    const last = out[out.length - 1];
    if (last !== undefined && o - (last[0] + last[1]) <= MAX_GAP && o + n - last[0] <= MAX_MERGED) {
      last[1] = Math.max(last[1], o + n - last[0]);
    } else {
      out.push([o, n]);
    }
  }
  return out;
}

/** The subblocks' headers, read with the bytes after each (which usually hold its
 * metadata and codec header) through `exact`, coalesced; and the ranges read. */
export async function readSubblocks(
  read: ByteReader, fileSize: number, d: Directory, exact: ByteReader = read,
): Promise<[Subblocks, Prefetched]> {
  const n = d.count;
  const s: Subblocks = {
    allocated: new Float64Array(n), length: new Int32Array(n), metadata: new Int32Array(n),
    attachment: new Int32Array(n), data: new Float64Array(n), agrees: new Uint8Array(n), copyLength: new Int32Array(n),
  };
  const ahead = new Prefetched(exact, fileSize);
  const positions = [...d.filePosition].filter((o) => o >= 0 && o <= MAX_SAFE);
  await ahead.fetch(coalesce(positions.map((o): [number, number] => [o, PREFETCH_HEAD])));
  for (let i = 0; i < n; i++) {
    const o = offset(d.filePosition[i], `subblock ${i}'s FilePosition`);
    within(o, 32 + 256, fileSize, `subblock ${i}'s header`);
    let h = await ahead.read(o, Math.min(FIRST, fileSize - o));
    s.allocated[i] = checkHeader(h, o, SUBBLOCK, `subblock ${i}`)[0];
    const v = dv(h);
    const m = v.getInt32(32, true), a = v.getInt32(36, true), data = i64(v, 40);
    if (m < 0 || a < 0 || !(data >= 0 && data <= MAX_SAFE)) {
      reject(`subblock ${i} has a negative MetadataSize, AttachmentSize or DataSize`);
    }
    if (h[48] !== 0x44 || h[49] !== 0x56) reject(`subblock ${i}'s copy of its entry has schema other than DV`);
    const dims = v.getInt32(32 + 16 + 28, true);
    if (!(dims >= 0 && dims <= 40)) reject(`subblock ${i}'s copy of its entry has DimensionCount ${dims}, not 0 to 40`);
    const length = Math.max(256, 48 + 20 * dims);
    within(o, 32 + length, fileSize, `subblock ${i}'s header`);
    if (32 + length > h.length) h = await ahead.read(o, 32 + length);
    within(o + 32 + length, m + data + a, fileSize, `subblock ${i}'s parts`);
    s.length[i] = length;
    s.metadata[i] = m;
    s.attachment[i] = a;
    s.data[i] = data;
    s.copyLength[i] = 32 + 20 * dims;
    s.agrees[i] = equal(h.subarray(48, 80 + 20 * dims), entry(d, i)) ? 1 : 0;
  }
  // The codec headers the first reads do not hold.
  const heads: [number, number][] = [];
  for (let i = 0; i < n; i++) {
    if (!s.agrees[i] || d.compression[i] === 0 || !s.data[i]) continue;
    const h: [number, number] = [d.filePosition[i] + 32 + s.length[i] + s.metadata[i], Math.min(s.data[i], PREFETCH_DATA)];
    if (!ahead.covers(...h)) heads.push(h);
  }
  await ahead.fetch(coalesce(heads));
  return [s, ahead];
}

export interface MetadataSegment {
  offset: number;
  allocated: number;
  xml: number; // XmlSize
  attachment: number; // AttachmentSize
}

export async function readMetadataSegment(read: ByteReader, fileSize: number, o: number): Promise<MetadataSegment> {
  const [allocated] = await segmentHeader(read, fileSize, o, METADATA, "the metadata segment");
  within(o + 32, 8, fileSize, "the metadata segment's sizes");
  const v = dv(await read(o + 32, 8));
  const x = v.getInt32(0, true), b = v.getInt32(4, true);
  if (x < 0 || b < 0) reject("the metadata segment has a negative XmlSize or AttachmentSize");
  within(o + 32 + 256, x + b, fileSize, "the metadata segment's parts");
  return { offset: o, allocated, xml: x, attachment: b };
}

/** Attachment entry k: its 128 bytes, and for an A1 entry its segment. */
export interface Attachment {
  entry: Uint8Array;
  a1: boolean;
  offset: number; // its segment's offset, for an A1 entry
  allocated: number;
  dataSize: number;
  segmentEntry: Uint8Array; // the segment's copy of the entry
}

export const dataOffset = (a: Attachment) => a.offset + 32 + 256;

/** [the directory's AllocatedSize, its entries]. */
export async function readAttachments(
  read: ByteReader, fileSize: number, o: number, exact: ByteReader = read,
): Promise<[number, Attachment[]]> {
  const [allocated] = await segmentHeader(read, fileSize, o, ATTDIR, "the attachment directory");
  within(o + 32, 4, fileSize, "the attachment directory's EntryCount");
  const count = dv(await read(o + 32, 4)).getInt32(0, true);
  if (!(count >= 0 && count <= MAX_ATTACHMENTS)) {
    reject(`the attachment directory's EntryCount ${count} is not from 0 to ${MAX_ATTACHMENTS}`);
  }
  within(o + 32 + 256, 128 * count, fileSize, "the attachment entries");
  const raw = await read(o + 32 + 256, 128 * count);
  const out: Attachment[] = Array.from({ length: count }, (_, k) => {
    const e = raw.slice(128 * k, 128 * k + 128);
    return { entry: e, a1: e[0] === 0x41 && e[1] === 0x31, offset: -1, allocated: 0, dataSize: 0, segmentEntry: new Uint8Array() };
  });
  const position = (a: Attachment) => i64(dv(a.entry), 12);
  const readable = out.filter((a) => {
    const p = position(a);
    return a.a1 && p >= 0 && p <= MAX_SAFE && p + 288 <= fileSize;
  });
  const headers = new Map<Attachment, Uint8Array>();
  for (const [k, h] of (await batched(readable, (a) => exact(position(a), 32 + 256))).entries()) {
    headers.set(readable[k], h);
  }
  for (const [k, a] of out.entries()) {
    if (!a.a1) continue;
    const filePart = dv(a.entry).getInt32(20, true);
    if (filePart !== 0) reject(`attachment entry ${k} is in FilePart ${filePart}`);
    a.offset = offset(position(a), `attachment ${k}'s FilePosition`);
    within(a.offset, 32 + 256, fileSize, `attachment ${k}'s header`);
    const h = headers.get(a)!;
    a.allocated = checkHeader(h, a.offset, ATTACH, `attachment ${k}`)[0];
    a.dataSize = size(i64(dv(h), 32), `attachment ${k}'s DataSize`);
    a.segmentEntry = h.slice(48, 176);
    within(dataOffset(a), a.dataSize, fileSize, `attachment ${k}'s data`);
  }
  return [allocated, out];
}

/** A segment the walk visits: [offset, id, AllocatedSize, UsedSize]. */
export type Segment = [number, Uint8Array, number, number];

/** A segment id: 1 to 16 of A-Z, 0-9 and _, then NULs. */
function isSegmentId(id: Uint8Array): boolean {
  let k = 0;
  while (k < 16 && ((id[k] >= 0x41 && id[k] <= 0x5a) || (id[k] >= 0x30 && id[k] <= 0x39) || id[k] === 0x5f)) k++;
  if (k === 0) return false;
  for (; k < 16; k++) if (id[k] !== 0) return false;
  return true;
}

/** The walk (spec/virtualize.md §13.2 step 6): the segments in file order, and
 * where the tail starts. `known` holds the headers already read, by offset. */
export async function walk(
  read: ByteReader, fileSize: number, known: Map<number, [Uint8Array, number, number]>,
): Promise<[Segment[], number]> {
  const out: Segment[] = [];
  let o = 0;
  while (o < fileSize && out.length < MAX_WALK) {
    let id: Uint8Array, allocated: number, used: number;
    const k = known.get(o);
    if (k !== undefined) {
      [id, allocated, used] = k;
    } else {
      if (o + 32 > fileSize) break;
      const h = await read(o, 32);
      id = h.slice(0, 16);
      if (!isSegmentId(id)) break;
      const v = dv(h);
      allocated = i64(v, 16);
      used = i64(v, 24);
      if (allocated < 0 || used < 0) break;
    }
    if (o + 32 + allocated > fileSize) break;
    out.push([o, id, allocated, used]);
    o += 32 + allocated;
  }
  return [out, o];
}
