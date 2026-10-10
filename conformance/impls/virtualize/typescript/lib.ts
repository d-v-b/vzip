// Shared pieces: rejection, the HTTP range reader, payload sizes, output.

export class Reject extends Error {}

export function reject(msg: string): never {
  throw new Reject(msg);
}

export const MAX_SAFE = Number.MAX_SAFE_INTEGER; // 2^53 - 1
export const MAX_SAFE_BIG = BigInt(MAX_SAFE);

/** Converts a bigint offset/length to a number, rejecting values above 2^53 - 1. */
export function safe(v: bigint, what: string): number {
  if (v < 0n || v > MAX_SAFE_BIG) reject(`${what} out of range: ${v}`);
  return Number(v);
}

const BLOCK = 1 << 16;

/** Reads byte ranges of a URL over HTTP, caching 64 KiB blocks. */
export class Source {
  url: string;
  size: number;
  blocks: Map<number, Uint8Array> = new Map();

  constructor(url: string, size: number) {
    this.url = url;
    this.size = size;
  }

  static async open(url: string): Promise<Source> {
    const r = await fetch(url, { method: "HEAD" });
    if (r.status !== 200) throw new Error(`HEAD ${url}: status ${r.status}`);
    const cl = r.headers.get("content-length");
    if (cl === null || !/^[0-9]+$/.test(cl)) throw new Error(`HEAD ${url}: no Content-Length`);
    return new Source(url, Number(cl));
  }

  /** A source over in-memory bytes (used by the fuzz test). */
  static fromBytes(url: string, data: Uint8Array): Source {
    const s = new Source(url, data.length);
    s.local = data;
    return s;
  }
  local: Uint8Array | undefined;

  private async fetchRange(a: number, b: number): Promise<Uint8Array> {
    // [a, b)
    if (this.local !== undefined) return this.local.slice(a, b);
    for (let attempt = 0; ; attempt++) {
      try {
        const r = await fetch(this.url, { headers: { Range: `bytes=${a}-${b - 1}`, "Accept-Encoding": "identity" } });
        if (r.status !== 206) throw new Error(`GET ${this.url} ${a}-${b - 1}: status ${r.status}`);
        const body = new Uint8Array(await r.arrayBuffer());
        if (body.length !== b - a) throw new Error(`GET ${this.url}: short body`);
        return body;
      } catch (e) {
        if (attempt >= 3) throw e;
        await new Promise((res) => setTimeout(res, 500 * (attempt + 1)));
      }
    }
  }

  /** Reads `len` bytes at `off`; a read outside the file rejects the input. */
  async read(off: number, len: number): Promise<Uint8Array> {
    if (!Number.isSafeInteger(off) || !Number.isSafeInteger(len) || off < 0 || len < 0) reject(`bad read ${off}+${len}`);
    if (off + len > this.size) reject(`read outside the file: ${off}+${len} > ${this.size}`);
    const out = new Uint8Array(len);
    if (len === 0) return out;
    const b0 = Math.floor(off / BLOCK);
    const b1 = Math.floor((off + len - 1) / BLOCK);
    // fetch missing runs of blocks
    let i = b0;
    while (i <= b1) {
      if (this.blocks.has(i)) {
        i++;
        continue;
      }
      let j = i;
      while (j + 1 <= b1 && !this.blocks.has(j + 1)) j++;
      const a = i * BLOCK;
      const b = Math.min(this.size, (j + 1) * BLOCK);
      const data = await this.fetchRange(a, b);
      for (let k = i; k <= j; k++) {
        this.blocks.set(k, data.subarray((k - i) * BLOCK, Math.min(data.length, (k - i + 1) * BLOCK)));
      }
      i = j + 1;
    }
    for (let k = b0; k <= b1; k++) {
      const blk = this.blocks.get(k)!;
      const bs = k * BLOCK;
      const s = Math.max(off, bs);
      const e = Math.min(off + len, bs + blk.length);
      out.set(blk.subarray(s - bs, e - bs), s - off);
    }
    return out;
  }
}

export function varintLen(v: number): number {
  let n = 1;
  while (v >= 128) {
    v = Math.floor(v / 128);
    n++;
  }
  return n;
}

export function rangeMsgLen(o: number, n: number): number {
  return (o > 0 ? 1 + varintLen(o) : 0) + (n > 0 ? 1 + varintLen(n) : 0);
}

/** Payload size (spec/virtualize.md §1.2) of a list of ranges of source 0. */
export function payloadLen(ranges: [number, number, number][]): number {
  if (ranges.length === 1) return rangeMsgLen(ranges[0][1], ranges[0][2]);
  let t = 0;
  for (const r of ranges) {
    const l = rangeMsgLen(r[1], r[2]);
    t += 1 + varintLen(l) + l;
  }
  return t;
}

export type Entry = { ranges: [number, number, number][] } | { json: unknown } | { base64: string };

export class Output {
  entries: Map<string, Entry> = new Map();
  fileSize: number;
  constructor(fileSize: number) {
    this.fileSize = fileSize;
  }
  json(key: string, v: unknown) {
    this.entries.set(key, { json: v });
  }
  bytes(key: string, b: Uint8Array) {
    this.entries.set(key, { base64: Buffer.from(b).toString("base64") });
  }
  ref(key: string, ranges: [number, number, number][]) {
    for (const [, o, n] of ranges) {
      if (!Number.isSafeInteger(o) || !Number.isSafeInteger(n) || o < 0 || n < 0 || o + n > this.fileSize)
        reject(`range (${o}, ${n}) of ${key} is outside the file`);
    }
    if (payloadLen(ranges) > 65519) reject(`payload of ${key} exceeds 65519 bytes`);
    this.entries.set(key, { ranges });
  }
}

export function finite(x: number, what: string): number {
  if (!Number.isFinite(x)) reject(`${what} is not finite`);
  return x;
}

export function arrayJson(o: {
  shape: number[];
  dataType: string;
  chunkShape: number[];
  codecs: unknown[];
  dims: string[];
}): unknown {
  return {
    zarr_format: 3,
    node_type: "array",
    shape: o.shape,
    data_type: o.dataType,
    chunk_grid: { name: "regular", configuration: { chunk_shape: o.chunkShape } },
    chunk_key_encoding: { name: "default", configuration: { separator: "/" } },
    fill_value: 0,
    codecs: o.codecs,
    dimension_names: o.dims,
    attributes: {},
  };
}

export function transposeCodec(dims: string[]): unknown {
  const order: number[] = [];
  dims.forEach((d, i) => {
    if (d !== "c") order.push(i);
  });
  order.push(dims.indexOf("c"));
  return { name: "transpose", configuration: { order } };
}

export function bytesCodec(itemSize: number, endian: "little" | "big"): unknown {
  return itemSize === 1 ? { name: "bytes" } : { name: "bytes", configuration: { endian } };
}

export const AXIS_TYPE: Record<string, string> = { t: "time", c: "channel", z: "space", y: "space", x: "space" };
