// Shared helpers: rejection, HTTP range reader with a block cache, output entries.

export const MAX_SAFE = 2 ** 53 - 1;

export class Reject extends Error {}

export function reject(msg: string): never {
  throw new Reject(msg);
}

/** Convert a u64 BigInt to a number, rejecting values above 2^53 - 1. */
export function big2num(v: bigint, what: string): number {
  if (v > BigInt(MAX_SAFE)) reject(`${what} above 2^53-1: ${v}`);
  return Number(v);
}

/** A number computed by §1.3 must be finite. */
export function fin(x: number, what: string): number {
  if (!Number.isFinite(x)) reject(`${what} is not finite`);
  return x;
}

const BLOCK = 65536;

export class Source {
  url: string;
  size: number;
  private blocks = new Map<number, Promise<Uint8Array>>();

  constructor(url: string, size: number) {
    this.url = url;
    this.size = size;
  }

  static async open(url: string): Promise<Source> {
    const r = await fetch(url, { method: "HEAD" });
    if (r.status !== 200) throw new Error(`HEAD ${url}: status ${r.status}`);
    const cl = r.headers.get("content-length");
    if (cl === null || !/^[0-9]+$/.test(cl)) throw new Error("HEAD: no Content-Length");
    const size = Number(cl);
    if (!Number.isSafeInteger(size)) throw new Error("HEAD: bad Content-Length");
    return new Source(url, size);
  }

  private async fetchRange(a: number, b: number): Promise<Uint8Array> {
    let lastErr: unknown;
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        const r = await fetch(this.url, { headers: { Range: `bytes=${a}-${b - 1}` } });
        if (r.status !== 206) throw new Error(`GET ${a}-${b - 1}: status ${r.status}`);
        const buf = new Uint8Array(await r.arrayBuffer());
        if (buf.length !== b - a) throw new Error(`GET ${a}-${b - 1}: got ${buf.length} bytes`);
        return buf;
      } catch (e) {
        lastErr = e;
        await new Promise((res) => setTimeout(res, 200 * (attempt + 1)));
      }
    }
    throw lastErr;
  }

  private block(i: number): Promise<Uint8Array> {
    let p = this.blocks.get(i);
    if (!p) {
      const a = i * BLOCK;
      const b = Math.min(this.size, a + BLOCK);
      p = this.fetchRange(a, b);
      this.blocks.set(i, p);
    }
    return p;
  }

  /** Read `len` bytes at `off`; a read outside the file rejects the input. */
  async read(off: number, len: number): Promise<Uint8Array> {
    if (!Number.isSafeInteger(off) || !Number.isSafeInteger(len) || off < 0 || len < 0 || off > this.size - len) {
      reject(`read outside the file: ${off}+${len} > ${this.size}`);
    }
    if (len === 0) return new Uint8Array(0);
    if (len > 16 * BLOCK) return this.fetchRange(off, off + len);
    const first = Math.floor(off / BLOCK);
    const last = Math.floor((off + len - 1) / BLOCK);
    const parts = await Promise.all(Array.from({ length: last - first + 1 }, (_, k) => this.block(first + k)));
    if (parts.length === 1) {
      const s = off - first * BLOCK;
      return parts[0].subarray(s, s + len);
    }
    const out = new Uint8Array(len);
    let w = 0;
    for (let k = 0; k < parts.length; k++) {
      const base = (first + k) * BLOCK;
      const s = Math.max(off, base) - base;
      const e = Math.min(off + len, base + parts[k].length) - base;
      out.set(parts[k].subarray(s, e), w);
      w += e - s;
    }
    return out;
  }
}

// ---- output ----

export type Range = [number, number]; // (offset, length) in source 0

function varintLen(n: number): number {
  let l = 1;
  while (n >= 128) {
    n = Math.floor(n / 128);
    l++;
  }
  return l;
}

function rangeMsgLen(o: number, n: number): number {
  return (o > 0 ? 1 + varintLen(o) : 0) + (n > 0 ? 1 + varintLen(n) : 0);
}

/** Payload size of a reference entry (§1.2). */
export function payloadLen(ranges: Range[]): number {
  if (ranges.length === 1) return rangeMsgLen(ranges[0][0], ranges[0][1]);
  let t = 0;
  for (const [o, n] of ranges) {
    const r = rangeMsgLen(o, n);
    t += 1 + varintLen(r) + r;
  }
  return t;
}

export class Output {
  entries: Record<string, unknown> = {};
  fileSize: number;
  constructor(fileSize: number) {
    this.fileSize = fileSize;
  }
  json(key: string, value: unknown) {
    this.entries[key] = { json: value };
  }
  bytes(key: string, b: Uint8Array) {
    this.entries[key] = { base64: Buffer.from(b).toString("base64") };
  }
  ref(key: string, ranges: Range[]) {
    for (const [o, n] of ranges) {
      if (!Number.isSafeInteger(o) || !Number.isSafeInteger(n) || o < 0 || n < 0 || o > this.fileSize - n) {
        reject(`reference ${o}+${n} outside the file (size ${this.fileSize}) for ${key}`);
      }
    }
    if (payloadLen(ranges) > 65519) reject(`payload of ${key} above 65519 bytes`);
    this.entries[key] = { ranges: ranges.map(([o, n]) => [0, o, n]) };
  }
}

// ---- common zarr documents (§2) ----

export function groupDoc(attributes: unknown) {
  return { zarr_format: 3, node_type: "group", attributes };
}

export function arrayDoc(shape: number[], dataType: string, chunkShape: number[], codecs: unknown[], dims: string[]) {
  return {
    zarr_format: 3,
    node_type: "array",
    shape,
    data_type: dataType,
    chunk_grid: { name: "regular", configuration: { chunk_shape: chunkShape } },
    chunk_key_encoding: { name: "default", configuration: { separator: "/" } },
    fill_value: 0,
    codecs,
    dimension_names: dims,
    attributes: {},
  };
}

export function transposeCodec(dims: string[]) {
  const order: number[] = [];
  dims.forEach((d, i) => {
    if (d !== "c") order.push(i);
  });
  order.push(dims.indexOf("c"));
  return { name: "transpose", configuration: { order } };
}

export function bytesCodec(itemSize: number, little: boolean) {
  return itemSize === 1 ? { name: "bytes" } : { name: "bytes", configuration: { endian: little ? "little" : "big" } };
}

export const AXIS_TYPE: Record<string, string> = { t: "time", c: "channel", z: "space", y: "space", x: "space" };

export function axisObj(name: string, unit: string | undefined) {
  const a: Record<string, string> = { name, type: AXIS_TYPE[name] };
  if (unit !== undefined) a.unit = unit;
  return a;
}
