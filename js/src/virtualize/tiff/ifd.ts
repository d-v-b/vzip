// Reading the structure of a TIFF or BigTIFF file (both byte orders) through
// range reads: its image file directories (IFDs) and their SubIFDs, nested.
// Pixel data is never read.

import type { ByteReader } from "../common.ts";
import type { Entry, Found } from "./tags.ts";

export class TiffError extends Error {}

export const Tag = {
  ImageWidth: 256,
  ImageLength: 257,
  BitsPerSample: 258,
  Compression: 259,
  PhotometricInterpretation: 262,
  FillOrder: 266,
  ImageDescription: 270,
  SamplesPerPixel: 277,
  PlanarConfiguration: 284,
  Predictor: 317,
  TileWidth: 322,
  TileLength: 323,
  TileOffsets: 324,
  TileByteCounts: 325,
  SubIFDs: 330,
  SampleFormat: 339,
  JPEGTables: 347,
  XResolution: 282,
  YResolution: 283,
  ResolutionUnit: 296,
  YCbCrSubsampling: 530,
} as const;

/** The tags of the convention's table (spec/virtualize/tiff.md §2). */
export const WANTED = new Set<number>(Object.values(Tag));
/** The deepest nesting of SubIFDs (and of pointer targets): main-chain IFDs are at depth 0. */
export const MAX_DEPTH = 4;

// Tags with one used value (spec/virtualize/tiff/profile.md §3.1); the others are arrays.
const SCALARS = new Set<number>([256, 257, 259, 262, 266, 277, 282, 283, 284, 296, 317, 322, 323]);
// XResolution and YResolution: RATIONAL, kept as [numerator, denominator].
const RATIONAL_TAGS = new Set<number>([282, 283]);
// Field types allowed for every tag but ImageDescription: unsigned integers.
const INTEGER_TYPES = new Set([1, 3, 4, 13, 16, 18]);
const TILES = new Set<number>([Tag.TileOffsets, Tag.TileByteCounts]);
// The tags a used IFD reads (spec/virtualize/tiff/profile.md §3.1): all of the table's but
// ImageDescription (IFD 0's only) and SubIFDs (read where they are followed).
const USED = new Set([...WANTED].filter((t) => t !== Tag.ImageDescription && t !== Tag.SubIFDs));

// Byte size of each field type (TIFF 6.0, BigTIFF).
const TYPE_SIZE: Record<number, number> = {
  1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8,
  13: 4, 16: 8, 17: 8, 18: 8,
};

/** A checked entry of a tag of the table: its value is `inline` (the value field's
 * bytes) or at `offset`, within the file. */
export interface Field {
  type: number;
  count: number;
  inline?: Uint8Array;
  offset?: number;
}

export interface Ifd {
  offset: number;
  /** The table's tags (of duplicates, the first), checked but not read. */
  fields: Map<number, Field>;
  /** The values read (load): the first of a scalar, all of an array. */
  tags: Map<number, number[] | Uint8Array>;
  /** Field type of each tag of the table. */
  types: Map<number, number>;
  subIfds: Ifd[];
  /** Every entry, for the source metadata. */
  entries: Entry[];
}

export interface Tiff {
  littleEndian: boolean;
  bigTiff: boolean;
  /** The main IFD chain, each with its SubIFDs. */
  ifds: Ifd[];
  /** Reads the values of an IFD that the layout uses (spec/virtualize/tiff/profile.md §3.1). */
  load: (ifd: Ifd, options?: { tiles?: boolean; description?: boolean }) => Promise<void>;
}

function u64(view: DataView, at: number, le: boolean): number {
  const v = view.getBigUint64(at, le);
  if (v > BigInt(Number.MAX_SAFE_INTEGER)) throw new TiffError("offset too large");
  return Number(v);
}

/** `n` integers of `type`: a value above 2^53 - 1 rejects. */
function integers(bytes: Uint8Array, type: number, n: number, le: boolean, tag: number): number[] {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const out: number[] = [];
  for (let i = 0; i < n; i++) {
    switch (type) {
      case 1: out.push(view.getUint8(i)); break;
      case 3: out.push(view.getUint16(2 * i, le)); break;
      case 4: case 13: out.push(view.getUint32(4 * i, le)); break;
      default: {
        const v = view.getBigUint64(8 * i, le);
        if (v > BigInt(Number.MAX_SAFE_INTEGER)) throw new TiffError(`a value of tag ${tag} is more than 2^53 - 1`);
        out.push(Number(v));
      }
    }
  }
  return out;
}

export async function readTiff(read: ByteReader, fileSize: number): Promise<Tiff> {
  const header = await read(0, Math.min(16, fileSize));
  if (header.length < 8) throw new TiffError("file too short for a TIFF header");
  const order = String.fromCharCode(header[0], header[1]);
  if (order !== "II" && order !== "MM") throw new TiffError("not a TIFF file");
  const le = order === "II";
  const hview = new DataView(header.buffer, header.byteOffset, header.byteLength);
  const magic = hview.getUint16(2, le);
  let bigTiff: boolean;
  let first: number;
  if (magic === 42) {
    bigTiff = false;
    first = hview.getUint32(4, le);
  } else if (magic === 43) {
    if (header.length < 16 || hview.getUint16(4, le) !== 8 || hview.getUint16(6, le) !== 0) {
      throw new TiffError("invalid BigTIFF header");
    }
    bigTiff = true;
    first = u64(hview, 8, le);
  } else {
    throw new TiffError("not a TIFF file");
  }
  const countSize = bigTiff ? 8 : 2;
  const entrySize = bigTiff ? 20 : 12;
  const fieldSize = bigTiff ? 8 : 4;
  const seen = new Set<number>();
  // (tag kind, type, count, offset) -> values, for shared tables.
  const decoded = new Map<string, number[] | Uint8Array>();

  /** The values of a field that the layout uses: bytes, the first value of a scalar
   * (a pair for a RATIONAL), or every value of an array. */
  async function values(tag: number, f: Field): Promise<number[] | Uint8Array> {
    const kind = tag === Tag.ImageDescription || tag === Tag.JPEGTables ? "bytes"
      : RATIONAL_TAGS.has(tag) ? "pair" : SCALARS.has(tag) ? "one" : "all";
    const n = kind === "bytes" || kind === "all" ? f.count : 1;
    const key = `${kind},${f.type},${n},${f.offset}`;
    if (f.offset !== undefined) {
      const known = decoded.get(key);
      if (known !== undefined) return known;
    }
    const nbytes = n * TYPE_SIZE[f.type];
    const data = f.inline !== undefined ? f.inline.subarray(0, nbytes) : await read(f.offset!, nbytes);
    let out: number[] | Uint8Array;
    if (kind === "bytes") out = data.slice();
    else if (kind === "pair") {
      const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
      out = [view.getUint32(0, le), view.getUint32(4, le)];
    } else out = integers(data, f.type, n, le, tag);
    if (f.offset !== undefined) decoded.set(key, out);
    return out;
  }

  async function load(ifd: Ifd, options: { tiles?: boolean; description?: boolean } = {}): Promise<void> {
    for (const [tag, f] of ifd.fields) {
      const used = (USED.has(tag) && (options.tiles || !TILES.has(tag))) ||
        (tag === Tag.ImageDescription && options.description && f.type === 2);
      if (used && !ifd.tags.has(tag)) ifd.tags.set(tag, await values(tag, f));
    }
  }

  async function readIfd(offset: number): Promise<{ ifd: Ifd; next: number }> {
    if (offset < (bigTiff ? 16 : 8)) throw new TiffError(`IFD offset ${offset} is inside the header`);
    if (seen.has(offset)) throw new TiffError(`IFD offset ${offset} read twice`);
    if (seen.size >= 100000) throw new TiffError("too many IFDs");
    seen.add(offset);
    const cbytes = await read(offset, countSize);
    const cview = new DataView(cbytes.buffer, cbytes.byteOffset, cbytes.byteLength);
    const count = bigTiff ? u64(cview, 0, le) : cview.getUint16(0, le);
    const body = await read(offset + countSize, count * entrySize + fieldSize);
    const view = new DataView(body.buffer, body.byteOffset, body.byteLength);
    const fields = new Map<number, Field>();
    const types = new Map<number, number>();
    const entries: Entry[] = [];
    for (let i = 0; i < count; i++) {
      const at = i * entrySize;
      const tag = view.getUint16(at, le);
      const type = view.getUint16(at + 2, le);
      const n = bigTiff ? view.getBigUint64(at + 4, le) : BigInt(view.getUint32(at + 4, le));
      const valueAt = at + 4 + fieldSize;
      const size = TYPE_SIZE[type];
      const field = body.slice(valueAt, valueAt + fieldSize);
      const inline = size !== undefined && BigInt(size) * n <= BigInt(fieldSize);
      const where = inline ? undefined : bigTiff ? view.getBigUint64(valueAt, le) : BigInt(view.getUint32(valueAt, le));
      if (inline) entries.push({ tag, type, count: n, inline: field, field });
      else {
        entries.push({
          tag, type, count: n, offset: where! <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(where) : undefined, field,
        });
      }
      if (!WANTED.has(tag) || fields.has(tag)) continue; // unused, or a duplicate (the first is used)
      const allowed = tag === Tag.ImageDescription ? size !== undefined
        : tag === Tag.JPEGTables ? type === 1 || type === 7
        : RATIONAL_TAGS.has(tag) ? type === 5 : INTEGER_TYPES.has(type);
      if (!allowed) throw new TiffError(`tag ${tag} has field type ${type}`);
      if (SCALARS.has(tag) && n === 0n) throw new TiffError(`tag ${tag} has no value`);
      if (!inline && where! + BigInt(size) * n > BigInt(fileSize)) {
        throw new TiffError(`the value of tag ${tag} is outside the file`);
      }
      fields.set(tag, inline ? { type, count: Number(n), inline: field } : { type, count: Number(n), offset: Number(where) });
      types.set(tag, type);
    }
    // The next-IFD field is read with the IFD; its value is checked where it
    // is used (the main chain), not for SubIFDs.
    const nextAt = count * entrySize;
    const next = bigTiff ? Number(view.getBigUint64(nextAt, le)) : view.getUint32(nextAt, le);
    return { ifd: { offset, fields, tags: new Map(), types, subIfds: [], entries }, next };
  }

  const ifds: Ifd[] = [];
  for (let offset = first; offset !== 0; ) {
    if (offset > Number.MAX_SAFE_INTEGER) throw new TiffError("IFD offset too large");
    const { ifd, next } = await readIfd(offset);
    ifds.push(ifd);
    offset = next;
  }
  // The SubIFDs of each IFD, and theirs, down to MAX_DEPTH, depth first.
  async function readSubs(ifd: Ifd, depth: number): Promise<void> {
    const f = ifd.fields.get(Tag.SubIFDs);
    if (f === undefined || depth >= MAX_DEPTH) return;
    for (const o of await values(Tag.SubIFDs, f) as number[]) ifd.subIfds.push((await readIfd(o)).ifd);
    for (const sub of ifd.subIfds) await readSubs(sub, depth + 1);
  }
  for (const ifd of ifds) await readSubs(ifd, 0);
  return { littleEndian: le, bigTiff, ifds, load };
}

export function num(ifd: Ifd, tag: number, fallback?: number): number {
  const v = ifd.tags.get(tag);
  if (Array.isArray(v) && v.length > 0) return v[0];
  if (fallback !== undefined) return fallback;
  throw new TiffError(`IFD at ${ifd.offset} has no tag ${tag}`);
}

export function nums(ifd: Ifd, tag: number): number[] {
  const v = ifd.tags.get(tag);
  if (!Array.isArray(v)) throw new TiffError(`IFD at ${ifd.offset} has no tag ${tag}`);
  return v;
}

/** The bytes an IFD of `count` entries occupies: its entry count, entries and
 * next-IFD offset (spec/virtualize/tiff.md §5). */
export function extent(count: number, bigTiff: boolean): number {
  return bigTiff ? 16 + 20 * count : 6 + 12 * count;
}

/** A function that finds the IFD at an offset for a pointer tag (it never rejects), or
 * undefined if its entry count and entries do not lie within the file. The entries are
 * read only when the function it returns is called. */
export function entriesReader(read: ByteReader, size: number, bigTiff: boolean, le: boolean) {
  const countSize = bigTiff ? 8 : 2;
  const entrySize = bigTiff ? 20 : 12;
  const fieldSize = bigTiff ? 8 : 4;
  const entries = async (offset: number, count: number): Promise<Entry[]> => {
    const body = await read(offset + countSize, count * entrySize);
    const view = new DataView(body.buffer, body.byteOffset, body.byteLength);
    const out: Entry[] = [];
    for (let i = 0; i < count; i++) {
      const at = i * entrySize;
      const tag = view.getUint16(at, le);
      const type = view.getUint16(at + 2, le);
      const n = bigTiff ? view.getBigUint64(at + 4, le) : BigInt(view.getUint32(at + 4, le));
      const valueAt = at + 4 + fieldSize;
      const sz = TYPE_SIZE[type];
      const field = body.slice(valueAt, valueAt + fieldSize);
      if (sz !== undefined && BigInt(sz) * n <= BigInt(fieldSize)) {
        out.push({ tag, type, count: n, inline: field, field });
      } else {
        const where = bigTiff ? view.getBigUint64(valueAt, le) : BigInt(view.getUint32(valueAt, le));
        out.push({ tag, type, count: n, offset: where <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(where) : undefined, field });
      }
    }
    return out;
  };
  return async (offset: number): Promise<Found | undefined> => {
    if (offset < (bigTiff ? 16 : 8) || offset + countSize > size) return undefined;
    const c = await read(offset, countSize);
    const cv = new DataView(c.buffer, c.byteOffset, c.byteLength);
    const count = bigTiff ? Number(cv.getBigUint64(0, le)) : cv.getUint16(0, le);
    if (offset + countSize + count * entrySize > size) return undefined;
    return [count, offset + extent(count, bigTiff), () => entries(offset, count)];
  };
}
