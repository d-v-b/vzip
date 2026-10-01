import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import * as zlib from "node:zlib";
import { fileURLToPath } from "node:url";

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const CLI = path.join(ROOT, "vzip");

export function tmpdir(): string {
  const base = path.join(ROOT, "tmp");
  fs.mkdirSync(base, { recursive: true });
  return fs.mkdtempSync(path.join(base, "t-"));
}

export function runCli(args: string[], cwd?: string): { status: number; stdout: string; stderr: string } {
  const r = spawnSync(CLI, args, { cwd: cwd ?? os.tmpdir(), encoding: "utf8" });
  return { status: r.status ?? -1, stdout: r.stdout, stderr: r.stderr };
}

/** Write description, run `vzip write`, return {status, path}. */
export function writeDesc(dir: string, desc: unknown, name = "out.vzip"): { status: number; out: string; stderr: string } {
  const dp = path.join(dir, name + ".json");
  fs.writeFileSync(dp, typeof desc === "string" ? desc : JSON.stringify(desc));
  const out = path.join(dir, name);
  const r = runCli(["write", dp, out], dir);
  return { status: r.status, out, stderr: r.stderr };
}

/** Run `vzip read` and return parsed JSON. */
export function readQueries(archive: string, queries: unknown[], cwd?: string): any {
  const dir = path.dirname(archive);
  const qp = path.join(dir, `q-${Math.random().toString(36).slice(2)}.json`);
  fs.writeFileSync(qp, JSON.stringify(queries));
  const r = runCli(["read", archive, qp], cwd);
  if (r.status !== 0) throw new Error(`read exited ${r.status}: ${r.stderr}`);
  return JSON.parse(r.stdout);
}

// ---------- raw ZIP construction for invalid archives ----------

export function varint(v: bigint | number): Buffer {
  let x = BigInt(v);
  if (x < 0n) x = BigInt.asUintN(64, x);
  const out: number[] = [];
  do {
    let b = Number(x & 0x7fn);
    x >>= 7n;
    if (x) b |= 0x80;
    out.push(b);
  } while (x);
  return Buffer.from(out);
}
export function fVarint(num: number, v: bigint | number): Buffer {
  return Buffer.concat([varint(num * 8), varint(v)]);
}
export function fLen(num: number, data: Buffer | string): Buffer {
  const d = typeof data === "string" ? Buffer.from(data) : data;
  return Buffer.concat([varint(num * 8 + 2), varint(d.length), d]);
}
export function msg(...parts: Buffer[]): Buffer {
  return Buffer.concat(parts);
}

export interface RawEntry {
  name: string | Buffer;
  body?: Buffer;
  method?: number;
  flags?: number;
  extra?: Buffer; // CD extra
  usize?: number;
  csize?: number;
  lho?: number; // override
}

export function extraBlock(id: number, data: Buffer): Buffer {
  const h = Buffer.alloc(4);
  h.writeUInt16LE(id, 0);
  h.writeUInt16LE(data.length, 2);
  return Buffer.concat([h, data]);
}

export interface RawOpts {
  sources?: Buffer; // encoded SourceTable (uncompressed)
  sourcesBody?: Buffer; // raw body override (already compressed)
  index?: Buffer; // encoded CdIndex => 38-byte comment
  indexFn?: (cdLayout: { names: Buffer[]; offsets: number[]; lengths: number[]; bodyOffsets: number[] }) => Buffer;
  magic?: string;
  /** Entries listed in this order in the CD (default: given order). */
  sortCd?: boolean;
}

/** Build a ZIP with a vzip comment from raw entries. Format entries are appended automatically. */
export function rawZip(entries: RawEntry[], opts: RawOpts = {}): Buffer {
  const chunks: Buffer[] = [];
  let off = 0;
  const recs: { name: Buffer; e: RawEntry; lho: number; body: Buffer; crc: number }[] = [];
  const put = (e: RawEntry) => {
    const name = typeof e.name === "string" ? Buffer.from(e.name) : e.name;
    const body = e.body ?? Buffer.alloc(0);
    const lh = Buffer.alloc(30);
    lh.writeUInt32LE(0x04034b50, 0);
    lh.writeUInt16LE(20, 4);
    lh.writeUInt16LE(e.flags ?? 0x800, 6);
    lh.writeUInt16LE(e.method ?? 0, 8);
    const crc = zlib.crc32(body) >>> 0;
    lh.writeUInt32LE(crc, 14);
    lh.writeUInt32LE(e.csize ?? body.length, 18);
    lh.writeUInt32LE(e.usize ?? body.length, 22);
    lh.writeUInt16LE(name.length, 26);
    recs.push({ name, e, lho: off, body, crc });
    chunks.push(lh, name, body);
    off += 30 + name.length + body.length;
    return off - body.length;
  };
  for (const e of entries) put(e);
  const srcBody = opts.sourcesBody ?? zlib.deflateRawSync(opts.sources ?? Buffer.alloc(0));
  const srcOff = put({ name: "__vz__/sources", body: srcBody, method: 8 });
  const cdRec = (r: (typeof recs)[number]) => {
    const extra = r.e.extra ?? Buffer.alloc(0);
    const h = Buffer.alloc(46);
    h.writeUInt32LE(0x02014b50, 0);
    h.writeUInt16LE(20, 4);
    h.writeUInt16LE(20, 6);
    h.writeUInt16LE(r.e.flags ?? 0x800, 8);
    h.writeUInt16LE(r.e.method ?? 0, 10);
    h.writeUInt32LE(r.crc, 16);
    h.writeUInt32LE(r.e.csize ?? r.body.length, 20);
    h.writeUInt32LE(r.e.usize ?? r.body.length, 24);
    h.writeUInt16LE(r.name.length, 28);
    h.writeUInt16LE(extra.length, 30);
    h.writeUInt32LE(r.e.lho ?? r.lho, 42);
    return Buffer.concat([h, r.name, extra]);
  };
  let bodyRecs = recs.filter((r) => r.name.toString() !== "__vz__/sources");
  if (opts.sortCd) bodyRecs = [...bodyRecs].sort((a, b) => Buffer.compare(a.name, b.name));
  const bodyCd = bodyRecs.map(cdRec);
  let idxBody: Buffer | null = null;
  let idxOff = 0;
  let idxRec: (typeof recs)[number] | null = null;
  if (opts.index || opts.indexFn) {
    const offsets: number[] = [];
    let p = 0;
    for (const b of bodyCd) {
      offsets.push(p);
      p += b.length;
    }
    const idx = opts.index ?? opts.indexFn!({ names: bodyRecs.map((r) => r.name), offsets, lengths: bodyCd.map((b) => b.length), bodyOffsets: bodyRecs.map((r) => r.lho + 30 + r.name.length) });
    idxBody = zlib.deflateRawSync(idx);
    idxOff = put({ name: "__vz__/index", body: idxBody, method: 8 });
    idxRec = recs[recs.length - 1];
  }
  const srcRec = recs.find((r) => r.name.toString() === "__vz__/sources")!;
  const cd = Buffer.concat([...bodyCd, cdRec(srcRec), ...(idxRec ? [cdRec(idxRec)] : [])]);
  const cdOff = off;
  chunks.push(cd);
  const comment = Buffer.alloc(idxBody ? 38 : 22);
  comment.write(opts.magic ?? "vzip/0", 0, "latin1");
  comment.writeBigUInt64LE(BigInt(srcOff), 6);
  comment.writeBigUInt64LE(BigInt(srcBody.length), 14);
  if (idxBody) {
    comment.writeBigUInt64LE(BigInt(idxOff), 22);
    comment.writeBigUInt64LE(BigInt(idxBody.length), 30);
  }
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  const n = bodyCd.length + 1 + (idxRec ? 1 : 0);
  eocd.writeUInt16LE(n, 8);
  eocd.writeUInt16LE(n, 10);
  eocd.writeUInt32LE(cd.length, 12);
  eocd.writeUInt32LE(cdOff, 16);
  eocd.writeUInt16LE(comment.length, 20);
  chunks.push(eocd, comment);
  return Buffer.concat(chunks);
}

/** Encode a Source message. */
export function srcUrl(url: string, pins: { size?: bigint | number; etag?: string; mna?: bigint | number } = {}): Buffer {
  const parts = [fLen(1, url)];
  if (pins.size !== undefined) parts.push(fVarint(4, pins.size));
  if (pins.etag !== undefined) parts.push(fLen(5, pins.etag));
  if (pins.mna !== undefined) parts.push(fVarint(6, pins.mna));
  return msg(...parts);
}
export function table(...sources: Buffer[]): Buffer {
  return Buffer.concat(sources.map((s) => fLen(1, s)));
}
export function rangeMsg(source: number, offset: number | bigint, length: number | bigint): Buffer {
  const p: Buffer[] = [];
  if (source) p.push(fVarint(1, source));
  if (offset) p.push(fVarint(3, offset));
  if (length) p.push(fVarint(4, length));
  return msg(...p);
}
export function refExtra(payload: Buffer, concat = false): Buffer {
  return extraBlock(concat ? 0x7a77 : 0x7a76, payload);
}
