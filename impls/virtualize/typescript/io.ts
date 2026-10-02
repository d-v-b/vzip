// Input access, rejection, and output helpers.

export class Reject extends Error {}

export function reject(msg: string): never {
  throw new Reject(msg);
}

export const MAX_SAFE = Number.MAX_SAFE_INTEGER; // 2^53 - 1

const BLOCK = 1 << 16;

/** Random access to an HTTP resource via Range requests, cached in 64 KiB blocks. */
export class Source {
  url: string;
  size = 0;
  blocks = new Map<number, Uint8Array>();
  requests = 0;

  constructor(url: string) {
    this.url = url;
  }

  async open(): Promise<void> {
    const r = await fetch(this.url, { method: "HEAD" });
    if (!r.ok) throw new Error(`HEAD ${this.url}: ${r.status}`);
    const len = r.headers.get("content-length");
    if (len === null || !/^[0-9]+$/.test(len)) throw new Error("HEAD: no Content-Length");
    this.size = Number(len);
  }

  private async fetchRange(start: number, end: number): Promise<Uint8Array> {
    // end exclusive
    for (let attempt = 0; ; attempt++) {
      try {
        this.requests++;
        const r = await fetch(this.url, { headers: { Range: `bytes=${start}-${end - 1}` } });
        if (r.status !== 206) throw new Error(`GET ${this.url} ${start}-${end - 1}: status ${r.status}`);
        const b = new Uint8Array(await r.arrayBuffer());
        if (b.length !== end - start) throw new Error(`short read ${b.length} != ${end - start}`);
        return b;
      } catch (e) {
        if (attempt >= 3) throw e;
        await new Promise((res) => setTimeout(res, 500 * (attempt + 1)));
      }
    }
  }

  /** Ensure the blocks covering the given ranges are cached, fetching runs of missing blocks in parallel. */
  async prefetch(ranges: Array<[number, number]>): Promise<void> {
    const need = new Set<number>();
    for (const [off, len] of ranges) {
      if (len <= 0) continue;
      const end = Math.min(off + len, this.size);
      for (let b = Math.floor(off / BLOCK); b * BLOCK < end; b++) if (!this.blocks.has(b)) need.add(b);
    }
    const sorted = [...need].sort((a, b) => a - b);
    // group into runs of at most 64 blocks
    const runs: Array<[number, number]> = [];
    for (const b of sorted) {
      const last = runs[runs.length - 1];
      if (last && last[1] === b && last[1] - last[0] < 64) last[1] = b + 1;
      else runs.push([b, b + 1]);
    }
    let i = 0;
    const worker = async () => {
      while (i < runs.length) {
        const [b0, b1] = runs[i++];
        const start = b0 * BLOCK;
        const end = Math.min(b1 * BLOCK, this.size);
        const data = await this.fetchRange(start, end);
        for (let b = b0; b < b1; b++) {
          const s = (b - b0) * BLOCK;
          this.blocks.set(b, data.subarray(s, Math.min(s + BLOCK, data.length)));
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(16, runs.length) }, worker));
  }

  /** Read [off, off+len); a read outside the file rejects the input. */
  async read(off: number, len: number): Promise<Uint8Array> {
    if (!Number.isSafeInteger(off) || !Number.isSafeInteger(len) || off < 0 || len < 0)
      reject(`read at ${off} length ${len}: bad offset or length`);
    if (off + len > this.size) reject(`read at ${off} length ${len} outside the file (size ${this.size})`);
    if (len === 0) return new Uint8Array(0);
    await this.prefetch([[off, len]]);
    const out = new Uint8Array(len);
    let pos = off;
    while (pos < off + len) {
      const b = Math.floor(pos / BLOCK);
      const blk = this.blocks.get(b)!;
      const s = pos - b * BLOCK;
      const n = Math.min(blk.length - s, off + len - pos);
      out.set(blk.subarray(s, s + n), pos - off);
      pos += n;
    }
    return out;
  }
}

// ---------------------------------------------------------------- output

export type Range = [number, number, number]; // [source, offset, length]

export type Entry = { ranges: Range[] } | { json: unknown } | { base64: string };

function varintLen(v: number): number {
  let n = 1;
  let x = BigInt(v);
  while (x >= 128n) {
    x >>= 7n;
    n++;
  }
  return n;
}

/** Encoded size of a vzip Range message (source 0, offset, length; SPEC.md §5). */
function rangeMsgLen(r: Range): number {
  let n = 0;
  if (r[0] !== 0) n += 1 + varintLen(r[0]);
  if (r[1] !== 0) n += 1 + varintLen(r[1]);
  if (r[2] !== 0) n += 1 + varintLen(r[2]);
  return n;
}

/** Reference payload size (SPEC.md §4.3): a Range for one range, else a Concat. */
export function payloadLen(ranges: Range[]): number {
  if (ranges.length === 1) return rangeMsgLen(ranges[0]);
  let n = 0;
  for (const r of ranges) {
    const m = rangeMsgLen(r);
    n += 1 + varintLen(m) + m;
  }
  return n;
}

export class Output {
  url: string;
  fileSize: number;
  entries: Record<string, Entry> = {};

  constructor(url: string, fileSize: number) {
    this.url = url;
    this.fileSize = fileSize;
  }

  json(key: string, value: unknown): void {
    this.entries[key] = { json: value };
  }

  bytes(key: string, data: Uint8Array): void {
    this.entries[key] = { base64: Buffer.from(data).toString("base64") };
  }

  ref(key: string, ranges: Range[]): void {
    for (const [, off, len] of ranges) {
      if (!Number.isSafeInteger(off) || !Number.isSafeInteger(len) || off < 0 || len < 0)
        reject(`${key}: range offset/length out of range`);
      if (off + len > this.fileSize) reject(`${key}: range ${off}+${len} outside the file`);
    }
    if (payloadLen(ranges) > 65519) reject(`${key}: reference payload exceeds 65519 bytes`);
    this.entries[key] = { ranges };
  }

  serialize(): string {
    return JSON.stringify({ sources: [this.url], entries: this.entries });
  }
}

// ---------------------------------------------------------------- common zarr helpers (§2)

export type Axis = { name: string; type: string; unit?: string };

export function axisType(name: string): string {
  return name === "t" ? "time" : name === "c" ? "channel" : "space";
}

export function arrayJson(opts: {
  shape: number[];
  dataType: string;
  chunkShape: number[];
  codecs: unknown[];
  dimensionNames: string[];
}): unknown {
  return {
    zarr_format: 3,
    node_type: "array",
    shape: opts.shape,
    data_type: opts.dataType,
    chunk_grid: { name: "regular", configuration: { chunk_shape: opts.chunkShape } },
    chunk_key_encoding: { name: "default", configuration: { separator: "/" } },
    fill_value: 0,
    codecs: opts.codecs,
    dimension_names: opts.dimensionNames,
    attributes: {},
  };
}

/** The codec list of §2.1. */
export function buildCodecs(opts: {
  axes: string[];
  interleaved: boolean;
  arrayToBytes: "bytes" | "jpeg2k";
  itemSize: number;
  littleEndian: boolean;
  compressor: "zlib" | "zstd" | null;
}): unknown[] {
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
  else codecs.push({ name: "bytes", configuration: { endian: opts.littleEndian ? "little" : "big" } });
  if (opts.compressor === "zlib") codecs.push({ name: "zlib", configuration: { level: 1 } });
  else if (opts.compressor === "zstd") codecs.push({ name: "zstd", configuration: { level: 0, checksum: false } });
  return codecs;
}

/** §1.3: a computed number that is infinite or NaN rejects the input. */
export function finite(x: number, what: string): number {
  if (!Number.isFinite(x)) reject(`${what} is not finite`);
  return x;
}

export const UNITS: Record<string, string> = {
  "µm": "micrometer",
  "μm": "micrometer",
  um: "micrometer",
  nm: "nanometer",
  mm: "millimeter",
  cm: "centimeter",
  m: "meter",
  "Å": "angstrom",
  "Å": "angstrom",
  pm: "picometer",
  in: "inch",
  ft: "foot",
  s: "second",
  ms: "millisecond",
  min: "minute",
  h: "hour",
};
