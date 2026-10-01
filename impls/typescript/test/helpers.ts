import * as fs from "node:fs";
import * as path from "node:path";
import { build, type WriterInput } from "../src/writer.ts";
import type { Range, Source } from "../src/proto.ts";

export function tmpDir(): string {
  const base = path.join(import.meta.dirname, "..", "tmp");
  fs.mkdirSync(base, { recursive: true });
  return fs.mkdtempSync(path.join(base, "t-"));
}

export const src = {
  url: (url: string, pins: Partial<Pick<Source, "size" | "etag" | "modifiedNotAfter">> = {}): Source => ({
    kind: "url", url, size: pins.size ?? null, etag: pins.etag ?? null, modifiedNotAfter: pins.modifiedNotAfter ?? null,
  }),
  key: (key: string): Source => ({ kind: "key", key, size: null, etag: null, modifiedNotAfter: null }),
  data: (d: Uint8Array): Source => ({ kind: "data", data: d, size: null, etag: null, modifiedNotAfter: null }),
};

export const rng = {
  src: (source: number, offset: number | bigint, length: number | bigint): Range => ({
    source, offset: BigInt(offset), length: BigInt(length), data: null,
  }),
  lit: (d: Uint8Array | string): Range => ({
    source: 0, offset: 0n, length: 0n, data: typeof d === "string" ? Buffer.from(d) : d,
  }),
};

export function buildBuf(input: Partial<WriterInput>): Buffer {
  return Buffer.concat(build({ sources: [], entries: [], pageSize: null, mirror: true, ...input }));
}

export function writeTmp(dir: string, name: string, data: Uint8Array): string {
  const p = path.join(dir, name);
  fs.writeFileSync(p, data);
  return p;
}

// ---- raw ZIP surgery for crafting invalid archives ----

export type RawRec = { fixed: Buffer; name: Buffer; extra: Buffer; comment: Buffer };

export type Parsed = {
  pre: Buffer; // everything before the central directory
  recs: RawRec[];
  comment: Buffer;
  cdOffset: number;
};

/** Parses a non-ZIP64 archive produced by our writer. */
export function parseZip(buf: Buffer): Parsed {
  const commentLen = buf.readUInt16LE(buf.length - 60 + 20) === 38 && buf.readUInt32LE(buf.length - 60) === 0x06054b50 ? 38 : 22;
  const eocd = buf.length - 22 - commentLen;
  const cdSize = buf.readUInt32LE(eocd + 12);
  const cdOffset = buf.readUInt32LE(eocd + 16);
  const recs: RawRec[] = [];
  let p = cdOffset;
  while (p < cdOffset + cdSize) {
    const n = buf.readUInt16LE(p + 28), e = buf.readUInt16LE(p + 30), c = buf.readUInt16LE(p + 32);
    recs.push({
      fixed: Buffer.from(buf.subarray(p, p + 46)),
      name: Buffer.from(buf.subarray(p + 46, p + 46 + n)),
      extra: Buffer.from(buf.subarray(p + 46 + n, p + 46 + n + e)),
      comment: Buffer.from(buf.subarray(p + 46 + n + e, p + 46 + n + e + c)),
    });
    p += 46 + n + e + c;
  }
  return { pre: Buffer.from(buf.subarray(0, cdOffset)), recs, comment: Buffer.from(buf.subarray(eocd + 22)), cdOffset };
}

export function recBytes(r: RawRec): Buffer {
  const f = Buffer.from(r.fixed);
  f.writeUInt16LE(r.name.length, 28);
  f.writeUInt16LE(r.extra.length, 30);
  f.writeUInt16LE(r.comment.length, 32);
  return Buffer.concat([f, r.name, r.extra, r.comment]);
}

/** Reassembles; `zip64` forces ZIP64 end records (with EOCD offset field all ones). */
export function assemble(p: Parsed, opts: { zip64?: boolean; cdRaw?: Buffer } = {}): Buffer {
  const cd = opts.cdRaw ?? Buffer.concat(p.recs.map(recBytes));
  const parts: Buffer[] = [p.pre, cd];
  const cdOffset = p.pre.length;
  const n = p.recs.length;
  if (opts.zip64) {
    const z = Buffer.alloc(56);
    z.writeUInt32LE(0x06064b50, 0);
    z.writeBigUInt64LE(44n, 4);
    z.writeUInt16LE(45, 12); z.writeUInt16LE(45, 14);
    z.writeBigUInt64LE(BigInt(n), 24); z.writeBigUInt64LE(BigInt(n), 32);
    z.writeBigUInt64LE(BigInt(cd.length), 40); z.writeBigUInt64LE(BigInt(cdOffset), 48);
    const loc = Buffer.alloc(20);
    loc.writeUInt32LE(0x07064b50, 0);
    loc.writeBigUInt64LE(BigInt(cdOffset + cd.length), 8);
    loc.writeUInt32LE(1, 16);
    parts.push(z, loc);
  }
  const e = Buffer.alloc(22);
  e.writeUInt32LE(0x06054b50, 0);
  e.writeUInt16LE(n, 8); e.writeUInt16LE(n, 10);
  e.writeUInt32LE(cd.length, 12);
  e.writeUInt32LE(opts.zip64 ? 0xffffffff : cdOffset, 16);
  e.writeUInt16LE(p.comment.length, 20);
  parts.push(e, p.comment);
  return Buffer.concat(parts);
}

export function findRec(p: Parsed, name: string): RawRec {
  const r = p.recs.find((x) => x.name.toString("utf8") === name);
  if (!r) throw new Error(`no record ${name}`);
  return r;
}

export function block(id: number, data: Uint8Array): Buffer {
  const h = Buffer.alloc(4);
  h.writeUInt16LE(id, 0);
  h.writeUInt16LE(data.length, 2);
  return Buffer.concat([h, data]);
}

/** Appends `body` before the central directory and points the comment's sources/index fields at it. */
export function replaceFormatBody(buf: Buffer, which: "sources" | "index", body: Uint8Array): Buffer {
  const p = parseZip(buf);
  const off = p.pre.length;
  p.pre = Buffer.concat([p.pre, body]);
  // Fix local header offsets? Records keep pointing to their old local headers, which did not move.
  const c = Buffer.from(p.comment);
  const at = which === "sources" ? 6 : 22;
  c.writeBigUInt64LE(BigInt(off), at);
  c.writeBigUInt64LE(BigInt(body.length), at + 8);
  p.comment = c;
  return assemble(p);
}
