// What every profile of VIRTUALIZE.md shares: reading the input through range
// reads, rejecting it, and sizing reference payloads.

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

function rangeSize(r: [number, number] | Uint8Array): number {
  if (r instanceof Uint8Array) return 1 + varintSize(r.length) + r.length;
  const [offset, length] = r;
  return (offset ? 1 + varintSize(offset) : 0) + (length ? 1 + varintSize(length) : 0);
}

/** The encoded size of a reference to `ranges`: [offset, length] pairs or literal bytes (§1.2). */
export function payloadSize(ranges: ([number, number] | Uint8Array)[]): number {
  if (ranges.length === 1) return rangeSize(ranges[0]);
  return ranges.reduce((n, range) => {
    const r = rangeSize(range);
    return n + 1 + varintSize(r) + r;
  }, 0);
}
