// What every profile of VIRTUALIZE.md shares: reading the input through range
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
