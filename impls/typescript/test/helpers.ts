// Test helpers: a low-level archive builder that can produce invalid archives.
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { spawnSync } from "node:child_process";
import * as zlib from "node:zlib";
import { Archive } from "../src/reader.ts";

export interface RawEntry {
  name: string | Buffer;
  body?: Buffer; // bytes as stored
  method?: number;
  flags?: number;
  extra?: Buffer; // CD extra field
  usize?: number;
  csize?: number;
  crc?: number;
  lho?: number; // override CD local header offset field
}

export interface RawOptions {
  sources?: Buffer; // raw (uncompressed) SourceTable bytes
  sourcesBody?: Buffer; // stored bytes for the sources entry (overrides deflate of `sources`)
  index?: (recs: { name: Buffer; offset: number; length: number }[]) => Buffer; // returns CdIndex bytes
  indexBody?: Buffer;
  comment?: Buffer;
  eocdCount?: number;
  cdSizeOverride?: number;
  cdOffsetOverride?: number;
  extraCd?: Buffer; // appended to central directory (counted in size)
}

export function extraBlock(id: number, data: Buffer): Buffer {
  const h = Buffer.alloc(4);
  h.writeUInt16LE(id, 0);
  h.writeUInt16LE(data.length, 2);
  return Buffer.concat([h, data]);
}

/** Build a ZIP with vzip comment. Body records are written in the given order. */
export function buildRaw(entries: RawEntry[], opts: RawOptions = {}): Buffer {
  const chunks: Buffer[] = [];
  let pos = 0;
  const placed: { e: RawEntry; nb: Buffer; lho: number; body: Buffer; method: number; usize: number; crc: number }[] = [];
  const place = (e: RawEntry) => {
    const nb = Buffer.isBuffer(e.name) ? e.name : Buffer.from(e.name, "utf8");
    const body = e.body ?? Buffer.alloc(0);
    const method = e.method ?? 0;
    const usize = e.usize ?? body.length;
    const crc = e.crc ?? zlib.crc32(body);
    const h = Buffer.alloc(30);
    h.writeUInt32LE(0x04034b50, 0);
    h.writeUInt16LE(20, 4);
    h.writeUInt16LE(e.flags ?? 0x800, 6);
    h.writeUInt16LE(method, 8);
    h.writeUInt32LE(crc, 14);
    h.writeUInt32LE(e.csize ?? body.length, 18);
    h.writeUInt32LE(usize, 22);
    h.writeUInt16LE(nb.length, 26);
    const lho = pos;
    chunks.push(h, nb, body);
    pos += 30 + nb.length + body.length;
    const p = { e, nb, lho, body, method, usize, crc };
    placed.push(p);
    return p;
  };
  for (const e of entries) place(e);
  const srcBody = opts.sourcesBody ?? zlib.deflateRawSync(opts.sources ?? Buffer.alloc(0));
  const src = place({ name: "__vz__/sources", body: srcBody, method: 8, usize: (opts.sources ?? Buffer.alloc(0)).length });
  const rec = (p: (typeof placed)[0]) => {
    const extra = p.e.extra ?? Buffer.alloc(0);
    const r = Buffer.alloc(46);
    r.writeUInt32LE(0x02014b50, 0);
    r.writeUInt16LE(20, 4);
    r.writeUInt16LE(20, 6);
    r.writeUInt16LE(p.e.flags ?? 0x800, 8);
    r.writeUInt16LE(p.method, 10);
    r.writeUInt32LE(p.crc, 16);
    r.writeUInt32LE(p.e.csize ?? p.body.length, 20);
    r.writeUInt32LE(p.usize, 24);
    r.writeUInt16LE(p.nb.length, 28);
    r.writeUInt16LE(extra.length, 30);
    r.writeUInt32LE(p.e.lho ?? p.lho, 42);
    return Buffer.concat([r, p.nb, extra]);
  };
  const bodyRecs = placed.filter((p) => p !== src).map(rec);
  let idx: (typeof placed)[0] | null = null;
  if (opts.index || opts.indexBody) {
    let off = 0;
    const info = bodyRecs.map((r, i) => {
      const o = { name: placed[i].nb, offset: off, length: r.length };
      off += r.length;
      return o;
    });
    const ib = opts.index ? opts.index(info) : Buffer.alloc(0);
    const body = opts.indexBody ?? zlib.deflateRawSync(ib);
    idx = place({ name: "__vz__/index", body, method: 8, usize: ib.length });
  }
  const cdOffset = pos;
  const fmtRecs = [rec(src)];
  if (idx) fmtRecs.push(rec(idx));
  const cd = Buffer.concat([...bodyRecs, ...fmtRecs, opts.extraCd ?? Buffer.alloc(0)]);
  chunks.push(cd);
  pos += cd.length;
  let comment = opts.comment;
  if (!comment) {
    comment = Buffer.alloc(idx ? 38 : 22);
    comment.write("vzip/1", 0, "latin1");
    comment.writeBigUInt64LE(BigInt(src.lho + 30 + src.nb.length), 6);
    comment.writeBigUInt64LE(BigInt(src.body.length), 14);
    if (idx) {
      comment.writeBigUInt64LE(BigInt(idx.lho + 30 + idx.nb.length), 22);
      comment.writeBigUInt64LE(BigInt(idx.body.length), 30);
    }
  }
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  const n = opts.eocdCount ?? bodyRecs.length + fmtRecs.length;
  eocd.writeUInt16LE(n, 8);
  eocd.writeUInt16LE(n, 10);
  eocd.writeUInt32LE(opts.cdSizeOverride ?? cd.length, 12);
  eocd.writeUInt32LE(opts.cdOffsetOverride ?? cdOffset, 16);
  eocd.writeUInt16LE(comment.length, 20);
  chunks.push(eocd, comment);
  return Buffer.concat(chunks);
}

let counter = 0;
export function tmpDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), "vzip-test-"));
}

export function tmpFile(dir: string, data: Buffer | string, name?: string): string {
  const p = path.join(dir, name ?? `f${counter++}.vzip`);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, data);
  return p;
}

export async function openBuf(dir: string, data: Buffer, name?: string): Promise<Archive> {
  return Archive.open(tmpFile(dir, data, name));
}

/** Run `fn` and return the error class it throws (or "ok"). */
export async function errClass(fn: () => Promise<unknown>): Promise<string> {
  try {
    await fn();
    return "ok";
  } catch (e) {
    if (process.env.VZIP_TEST_DEBUG) console.error("   ->", (e as Error).message);
    return (e as { errorClass?: string }).errorClass ?? `unexpected: ${(e as Error).message}`;
  }
}

export function unzipTest(p: string): { ok: boolean; out: string } {
  const r = spawnSync("unzip", ["-tq", p], { encoding: "utf8", maxBuffer: 1 << 28 });
  return { ok: r.status === 0, out: r.stdout + r.stderr };
}

export const CLI = path.resolve(import.meta.dirname, "..", "vzip");
