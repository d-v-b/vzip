// CZI pixel types, compressions and codec headers (spec/virtualize/czi.md
// §3.1–§3.2, spec/virtualize.md §13.4).

import type { ByteReader } from "../../../../../js/src/virtualize/common.ts";

/** PixelType -> [name, data type, samples p, bytes per pixel q]. */
export const PIXEL_TYPES: Record<number, [string, string, number, number]> = {
  0: ["Gray8", "uint8", 1, 1],
  1: ["Gray16", "uint16", 1, 2],
  2: ["Gray32Float", "float32", 1, 4],
  3: ["Bgr24", "uint8", 3, 3],
  4: ["Bgr48", "uint16", 3, 6],
  8: ["Bgr96Float", "float32", 3, 12],
  9: ["Bgra32", "uint8", 4, 4],
  10: ["Gray64ComplexFloat", "complex64", 1, 8],
  11: ["Bgr192ComplexFloat", "complex64", 3, 24],
  12: ["Gray32", "int32", 1, 4],
  13: ["Gray64Float", "float64", 1, 8],
};
const ALL = new Set(Object.keys(PIXEL_TYPES).map(Number));
/** Compression -> [name, the pixel types it admits]. */
export const COMPRESSIONS: Record<number, [string, Set<number>]> = {
  0: ["uncompressed", ALL],
  1: ["JpgFile", new Set([0, 3])],
  4: ["JpgXr", new Set([0, 1, 2, 3, 4, 8, 9])],
  5: ["Zstd0", ALL],
  6: ["Zstd1", ALL],
};
export const UNCOMPRESSED = 0, JPEG = 1, JPEGXR = 4, ZSTD0 = 5, ZSTD1 = 6;
const HILO_TYPES = new Set([1, 4]); // Gray16, Bgr48: the only types hi-lo packing is on
export const MAX_HEADER = 2 ** 16; // codec headers are scanned within this many bytes of the data
const ZSTD = { name: "zstd", configuration: { level: 0, checksum: false } };
const SHUFFLE = { name: "numcodecs.shuffle", configuration: { elementsize: 2 } };
// JPEG XR pixel format GUIDs (as stored) each pixel type admits (spec/virtualize.md §13.4).
const WIC = "24C3DD6F034EFE4BB1853D77768DC9".toLowerCase();
const JXR_FORMATS: Record<number, string[]> = {
  0: [WIC + "08"],
  1: [WIC + "0b"],
  2: [WIC + "11"],
  3: [WIC + "0c", WIC + "0d"],
  4: [WIC + "15"],
  9: [WIC + "0f"],
  8: ["8fd7fee3dbe8cf4a84c1e97f6136b327"],
};
const SAMPLES: Record<number, string[]> = { 1: [""], 3: ["B", "G", "R"], 4: ["B", "G", "R", "A"] };
const RGB_SAMPLES: Record<number, string[]> = { 1: [""], 3: ["R", "G", "B"], 4: ["R", "G", "B", "A"] };

/** (offset, length) within the data, or null past the bound. */
export type Head = (offset: number, length: number) => Promise<Uint8Array | null>;

export const hex = (b: Uint8Array) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");

export function bytesCodec(dataType: string): Record<string, unknown> {
  return dataType === "uint8" || dataType === "int8"
    ? { name: "bytes" }
    : { name: "bytes", configuration: { endian: "little" } };
}

/** The codecs after `transpose` (spec/virtualize/czi.md §3.1). */
export function codecChain(pixelType: number, compression: number, hilo: boolean): Record<string, unknown>[] {
  const dataType = PIXEL_TYPES[pixelType][1];
  if (compression === JPEG) return [{ name: "imagecodecs_jpeg" }];
  if (compression === JPEGXR) return [{ name: "imagecodecs_jpegxr" }];
  if (compression === ZSTD0 || compression === ZSTD1) {
    return [bytesCodec(dataType), ...(hilo ? [SHUFFLE] : []), ZSTD];
  }
  return [bytesCodec(dataType)];
}

export function sampleLetters(pixelType: number, compression: number): string[] {
  const p = PIXEL_TYPES[pixelType][2];
  return (compression === JPEG || compression === JPEGXR ? RGB_SAMPLES : SAMPLES)[p];
}

/** [header length, hi-lo flag] of a Zstd1 subblock's data, or null. */
export async function zstd1Header(head: Head): Promise<[number, boolean] | null> {
  let b = await head(0, 1);
  if (b === null) return null;
  if (b[0] === 1) return [1, false];
  b = await head(0, 3);
  if (b === null || b[0] !== 3 || b[1] !== 1) return null;
  return [3, (b[2] & 1) !== 0];
}

const le = (b: Uint8Array): bigint => {
  let v = 0n;
  for (let i = b.length - 1; i >= 0; i--) v = (v << 8n) | BigInt(b[i]);
  return v;
};

/** The content size a zstd frame header at `at` declares, or null. */
export async function zstdContentSize(head: Head, at: number): Promise<bigint | null> {
  const b = await head(at, 5);
  if (b === null || b[0] !== 0x28 || b[1] !== 0xb5 || b[2] !== 0x2f || b[3] !== 0xfd) return null;
  const h = b[4];
  if (h & 0x08 || h & 0x03) return null;
  const single = (h & 0x20) !== 0;
  const n = [single ? 1 : 0, 2, 4, 8][h >> 6];
  if (n === 0) return null;
  const f = await head(at + 5 + (single ? 0 : 1), n);
  if (f === null) return null;
  const v = le(f);
  return n === 2 ? v + 256n : v;
}

/** [width, height] of a JPEG stream's frame header, or null. */
export async function jpegFrame(head: Head, p: number): Promise<[number, number] | null> {
  let b = await head(0, 2);
  if (b === null || b[0] !== 0xff || b[1] !== 0xd8) return null;
  let pos = 2;
  for (;;) {
    b = await head(pos, 1);
    if (b === null || b[0] !== 0xff) return null;
    while (b !== null && b[0] === 0xff) {
      pos += 1;
      b = await head(pos, 1);
    }
    if (b === null) return null;
    const m = b[0];
    pos += 1;
    if ((m >= 0xd0 && m <= 0xd7) || m === 0x01) continue;
    if (m === 0xda || m === 0xd9) return null;
    const lb = await head(pos, 2);
    if (lb === null) return null;
    const length = (lb[0] << 8) | lb[1];
    if (length < 2) return null;
    if (m >= 0xc0 && m <= 0xcf && m !== 0xc4 && m !== 0xc8 && m !== 0xcc) {
      const f = await head(pos + 2, 6);
      if (f === null) return null;
      const precision = f[0], height = (f[1] << 8) | f[2], width = (f[3] << 8) | f[4], count = f[5];
      if ((m === 0xc0 || m === 0xc1 || m === 0xc2) && precision === 8 && height >= 1 && width >= 1 && count === p) {
        return [width, height];
      }
      return null;
    }
    pos += length;
  }
}

const dv = (b: Uint8Array) => new DataView(b.buffer, b.byteOffset, b.byteLength);

/** [ImageWidth, ImageHeight] of a JPEG XR file whose pixel format the pixel type admits, or null. */
export async function jpegxrSize(head: Head, pixelType: number): Promise<[number, number] | null> {
  const b = await head(0, 8);
  if (b === null || b[0] !== 0x49 || b[1] !== 0x49 || b[2] !== 0xbc || b[3] !== 0x01) return null;
  const ifd = dv(b).getUint32(4, true);
  const c = await head(ifd, 2);
  if (c === null) return null;
  const count = dv(c).getUint16(0, true);
  const found = new Map<number, Uint8Array>();
  for (let k = 0; k < count; k++) {
    const e = await head(ifd + 2 + 12 * k, 12);
    if (e === null) return null;
    const tag = dv(e).getUint16(0, true);
    if ((tag === 0xbc01 || tag === 0xbc80 || tag === 0xbc81) && !found.has(tag)) {
      found.set(tag, e);
      if (found.size === 3) break;
    }
  }
  if (found.size < 3) return null;
  const pf = dv(found.get(0xbc01)!);
  if (pf.getUint16(2, true) !== 1 || pf.getUint32(4, true) !== 16) return null;
  const guid = await head(pf.getUint32(8, true), 16);
  if (guid === null || !(JXR_FORMATS[pixelType] ?? []).includes(hex(guid))) return null;
  const size: number[] = [];
  for (const t of [0xbc80, 0xbc81]) {
    const e = dv(found.get(t)!);
    const typ = e.getUint16(2, true), n = e.getUint32(4, true);
    if (n !== 1 || (typ !== 3 && typ !== 4)) return null;
    const v = typ === 3 ? e.getUint16(8, true) : e.getUint32(8, true);
    if (v < 1) return null;
    size.push(v);
  }
  return [size[0], size[1]];
}

/** [coded width, coded height, hi-lo flag, header length] of a subblock whose
 * stored size is width x height and whose data is n bytes, or null when it has
 * no coded size (spec/virtualize/czi.md §3.2). */
export async function codedSize(
  pixelType: number, compression: number, width: number, height: number, n: number, head: Head,
): Promise<[number, number, boolean, number] | null> {
  const q = PIXEL_TYPES[pixelType][3];
  const pixels = BigInt(width) * BigInt(height) * BigInt(q);
  if (compression === UNCOMPRESSED) return BigInt(n) >= pixels ? [width, height, false, 0] : null;
  if (compression === ZSTD0) return (await zstdContentSize(head, 0)) === pixels ? [width, height, false, 0] : null;
  if (compression === ZSTD1) {
    const h = await zstd1Header(head);
    if (h === null || n <= h[0] || (h[1] && !HILO_TYPES.has(pixelType))) return null;
    return (await zstdContentSize(head, h[0])) === pixels ? [width, height, h[1], h[0]] : null;
  }
  if (compression === JPEG) {
    const f = await jpegFrame(head, PIXEL_TYPES[pixelType][2]);
    return f === null ? null : [f[0], f[1], false, 0];
  }
  const f = await jpegxrSize(head, pixelType);
  return f === null ? null : [f[0], f[1], false, 0];
}

/** Reads within the first min(n, 2^16) bytes of the data at `start` (the
 * first of them may be given), in growing windows so that a header costs few reads. */
export function boundedHead(read: ByteReader, start: number, n: number, first?: Uint8Array): Head {
  const bound = Math.min(n, MAX_HEADER);
  let cache = (first ?? new Uint8Array()).subarray(0, bound);
  return async (offset, length) => {
    if (offset < 0 || offset + length > bound) return null;
    if (offset + length > cache.length) {
      const want = Math.min(bound, Math.max(offset + length, 2 * cache.length, 64));
      cache = await read(start, want);
    }
    return cache.subarray(offset, offset + length);
  };
}
