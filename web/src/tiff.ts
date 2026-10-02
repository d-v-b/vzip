// Reading the structure of a TIFF or BigTIFF file (both byte orders) through
// range reads: its image file directories (IFDs) and their SubIFDs. Pixel
// data is never read.

export class TiffError extends Error {}

/** Reads `length` bytes at `offset`; must return exactly that many. */
export type ByteReader = (offset: number, length: number) => Promise<Uint8Array>;

export const Tag = {
  ImageWidth: 256,
  ImageLength: 257,
  BitsPerSample: 258,
  Compression: 259,
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
} as const;

const WANTED = new Set<number>(Object.values(Tag));

// Tags with one used value (VIRTUALIZE.md §3.1); the others are arrays.
const SCALARS = new Set<number>([256, 257, 259, 277, 284, 317, 322, 323]);
// Field types allowed for every tag but ImageDescription: unsigned integers.
const INTEGER_TYPES = new Set([1, 3, 4, 13, 16, 18]);

// Byte size of each field type (TIFF 6.0, BigTIFF).
const TYPE_SIZE: Record<number, number> = {
  1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8,
  13: 4, 16: 8, 17: 8, 18: 8,
};

export interface Ifd {
  offset: number;
  tags: Map<number, number[] | Uint8Array>;
  /** Field type of each tag. */
  types: Map<number, number>;
  subIfds: Ifd[];
}

export interface Tiff {
  littleEndian: boolean;
  bigTiff: boolean;
  /** The main IFD chain, each with its SubIFDs. */
  ifds: Ifd[];
}

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
      throw new TiffError(`read of [${offset}, ${offset + length}) outside the ${fileSize}-byte file`);
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

function u64(view: DataView, at: number, le: boolean): number {
  const v = view.getBigUint64(at, le);
  if (v > BigInt(Number.MAX_SAFE_INTEGER)) throw new TiffError("offset too large");
  return Number(v);
}

function values(
  bytes: Uint8Array,
  type: number,
  count: number,
  le: boolean,
): number[] | Uint8Array {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (type === 2) return bytes.slice(); // ASCII: kept as bytes
  const out: number[] = [];
  for (let i = 0; i < count; i++) {
    switch (type) {
      case 1: case 7: out.push(view.getUint8(i)); break;
      case 6: out.push(view.getInt8(i)); break;
      case 3: out.push(view.getUint16(2 * i, le)); break;
      case 8: out.push(view.getInt16(2 * i, le)); break;
      case 4: case 13: out.push(view.getUint32(4 * i, le)); break;
      case 9: out.push(view.getInt32(4 * i, le)); break;
      case 16: case 18: out.push(u64(view, 8 * i, le)); break;
      case 17: {
        const v = view.getBigInt64(8 * i, le);
        if (v > BigInt(Number.MAX_SAFE_INTEGER) || v < -BigInt(Number.MAX_SAFE_INTEGER)) {
          throw new TiffError("a tag value is more than 2^53 - 1");
        }
        out.push(Number(v));
        break;
      }
      case 11: out.push(view.getFloat32(4 * i, le)); break;
      case 12: out.push(view.getFloat64(8 * i, le)); break;
      case 5: out.push(view.getUint32(8 * i, le) / view.getUint32(8 * i + 4, le)); break;
      case 10: out.push(view.getInt32(8 * i, le) / view.getInt32(8 * i + 4, le)); break;
      default: throw new TiffError(`unknown field type ${type}`);
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
    const tags = new Map<number, number[] | Uint8Array>();
    const types = new Map<number, number>();
    const pending: Promise<void>[] = [];
    for (let i = 0; i < count; i++) {
      const at = i * entrySize;
      const tag = view.getUint16(at, le);
      if (!WANTED.has(tag) || types.has(tag)) continue; // unused, or a duplicate (the first is used)
      const type = view.getUint16(at + 2, le);
      const n = bigTiff ? u64(view, at + 4, le) : view.getUint32(at + 4, le);
      const size = TYPE_SIZE[type];
      if (tag === Tag.ImageDescription ? size === undefined : !INTEGER_TYPES.has(type)) {
        throw new TiffError(`tag ${tag} has field type ${type}`);
      }
      if (SCALARS.has(tag) && n === 0) throw new TiffError(`tag ${tag} has no value`);
      const valueAt = at + 4 + (bigTiff ? 8 : 4);
      if (n * size <= fieldSize) {
        types.set(tag, type);
        tags.set(tag, values(body.subarray(valueAt, valueAt + n * size), type, n, le));
      } else {
        const where = bigTiff ? u64(view, valueAt, le) : view.getUint32(valueAt, le);
        types.set(tag, type);
        pending.push(
          read(where, n * size).then((b) => {
            tags.set(tag, values(b, type, n, le));
          }),
        );
      }
    }
    await Promise.all(pending);
    const nextAt = count * entrySize;
    const next = bigTiff ? u64(view, nextAt, le) : view.getUint32(nextAt, le);
    return { ifd: { offset, tags, types, subIfds: [] }, next };
  }

  const ifds: Ifd[] = [];
  for (let offset = first; offset !== 0; ) {
    const { ifd, next } = await readIfd(offset);
    ifds.push(ifd);
    offset = next;
  }
  await Promise.all(
    ifds.map(async (ifd) => {
      const subs = ifd.tags.get(Tag.SubIFDs);
      if (Array.isArray(subs)) {
        ifd.subIfds = await Promise.all(subs.map(async (o) => (await readIfd(o)).ifd));
      }
    }),
  );
  return { littleEndian: le, bigTiff, ifds };
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
