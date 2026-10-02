// Shared pieces: rejection error, HTTP range reader with a block cache,
// unit table and array/codec JSON builders (VIRTUALIZE.md §1, §2).

export class Reject extends Error {}

export function reject(msg: string): never {
  throw new Reject(msg);
}

const BLOCK = 1 << 16;
const MAX_INFLIGHT = 16;

export class Reader {
  url: string;
  size = 0;
  blocks = new Map<number, Promise<Uint8Array>>();
  inflight = 0;
  waiters: Array<() => void> = [];
  requests = 0;

  constructor(url: string) {
    this.url = url;
  }

  async init(): Promise<void> {
    const r = await fetch(this.url, { method: "HEAD" });
    if (!r.ok) throw new Error(`HEAD ${this.url}: ${r.status}`);
    const len = r.headers.get("content-length");
    if (len === null) throw new Error("HEAD: no Content-Length");
    this.size = Number(len);
  }

  async acquire(): Promise<void> {
    if (this.inflight < MAX_INFLIGHT) {
      this.inflight++;
      return;
    }
    await new Promise<void>((res) => this.waiters.push(res));
    this.inflight++;
  }

  release(): void {
    this.inflight--;
    const w = this.waiters.shift();
    if (w) w();
  }

  async fetchRange(start: number, end: number): Promise<Uint8Array> {
    // [start, end) exclusive
    await this.acquire();
    try {
      for (let attempt = 0; ; attempt++) {
        try {
          this.requests++;
          const r = await fetch(this.url, { headers: { Range: `bytes=${start}-${end - 1}` } });
          if (r.status !== 206) throw new Error(`GET range ${start}-${end - 1}: status ${r.status}`);
          const buf = new Uint8Array(await r.arrayBuffer());
          if (buf.length !== end - start) throw new Error(`short read at ${start}`);
          return buf;
        } catch (e) {
          if (attempt >= 3) throw e;
          await new Promise((res) => setTimeout(res, 500 * (attempt + 1)));
        }
      }
    } finally {
      this.release();
    }
  }

  block(i: number): Promise<Uint8Array> {
    let p = this.blocks.get(i);
    if (!p) {
      const s = i * BLOCK;
      p = this.fetchRange(s, Math.min(this.size, s + BLOCK));
      this.blocks.set(i, p);
    }
    return p;
  }

  async read(offset: number, length: number): Promise<Uint8Array> {
    if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(length) || offset < 0 || length < 0 || offset + length > this.size) {
      reject(`read of ${length} bytes at ${offset} is outside the file (size ${this.size})`);
    }
    if (length === 0) return new Uint8Array(0);
    if (length > 4 * BLOCK) return this.fetchRange(offset, offset + length);
    const b0 = Math.floor(offset / BLOCK);
    const b1 = Math.floor((offset + length - 1) / BLOCK);
    const parts: Uint8Array[] = [];
    for (let b = b0; b <= b1; b++) parts.push(await this.block(b));
    const out = new Uint8Array(length);
    let pos = 0;
    for (let b = b0; b <= b1; b++) {
      const blk = parts[b - b0];
      const s = b === b0 ? offset - b * BLOCK : 0;
      const e = b === b1 ? offset + length - b * BLOCK : blk.length;
      out.set(blk.subarray(s, e), pos);
      pos += e - s;
    }
    return out;
  }
}

// §2.3
const UNITS: Record<string, string> = {
  "µm": "micrometer",
  "μm": "micrometer",
  um: "micrometer",
  nm: "nanometer",
  mm: "millimeter",
  cm: "centimeter",
  m: "meter",
  "Å": "angstrom",
  pm: "picometer",
  in: "inch",
  ft: "foot",
  s: "second",
  ms: "millisecond",
  min: "minute",
  h: "hour",
};

export function unitOf(symbol: string): string | undefined {
  return Object.prototype.hasOwnProperty.call(UNITS, symbol) ? UNITS[symbol] : undefined;
}

export type Axis = { name: string; type: string; unit?: string };

export function axisType(name: string): string {
  return name === "t" ? "time" : name === "c" ? "channel" : "space";
}

// §2.1
export function arrayJson(opts: {
  shape: number[];
  dataType: string;
  chunkShape: number[];
  axes: string[];
  interleaved: boolean;
  arrayToBytes: "bytes" | "jpeg2k";
  endian: "little" | "big";
  itemSize: number;
  compressor: "zlib" | "zstd" | null;
}): unknown {
  const codecs: unknown[] = [];
  if (opts.interleaved) {
    const ci = opts.axes.indexOf("c");
    const order: number[] = [];
    for (let i = 0; i < opts.axes.length; i++) if (i !== ci) order.push(i);
    order.push(ci);
    codecs.push({ name: "transpose", configuration: { order } });
  }
  if (opts.arrayToBytes === "jpeg2k") codecs.push({ name: "imagecodecs_jpeg2k" });
  else if (opts.itemSize === 1) codecs.push({ name: "bytes" });
  else codecs.push({ name: "bytes", configuration: { endian: opts.endian } });
  if (opts.compressor === "zlib") codecs.push({ name: "zlib", configuration: { level: 1 } });
  else if (opts.compressor === "zstd") codecs.push({ name: "zstd", configuration: { level: 0, checksum: false } });
  return {
    zarr_format: 3,
    node_type: "array",
    shape: opts.shape,
    data_type: opts.dataType,
    chunk_grid: { name: "regular", configuration: { chunk_shape: opts.chunkShape } },
    chunk_key_encoding: { name: "default", configuration: { separator: "/" } },
    fill_value: 0,
    codecs,
    dimension_names: opts.axes,
    attributes: {},
  };
}

export type Entry = { ranges: Array<[number, number, number]> } | { json: unknown } | { base64: string };

export class Output {
  entries = new Map<string, Entry>();
  set(key: string, e: Entry): void {
    this.entries.set(key, e);
  }
  ref(key: string, ranges: Array<[number, number, number]>): void {
    this.entries.set(key, { ranges });
  }
  json(key: string, v: unknown): void {
    this.entries.set(key, { json: v });
  }
}

export function u64(dv: DataView, off: number, le: boolean): number {
  const v = dv.getBigUint64(off, le);
  if (v > BigInt(Number.MAX_SAFE_INTEGER)) reject(`64-bit value ${v} too large`);
  return Number(v);
}

export function dv(b: Uint8Array): DataView {
  return new DataView(b.buffer, b.byteOffset, b.byteLength);
}
