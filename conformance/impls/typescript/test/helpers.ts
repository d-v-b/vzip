// Test helpers: temp dirs and a low-level ZIP builder for crafting invalid archives.

import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";
import { execFileSync, spawnSync } from "node:child_process";
import { encodeSourceTable, type SourceMsg } from "../src/proto.ts";

export const ROOT = path.resolve(import.meta.dirname, "..");
export const CLI = path.join(ROOT, "vzip");

export function tmpdir(): string {
  const base = path.join(ROOT, "test", ".tmp");
  fs.mkdirSync(base, { recursive: true });
  return fs.mkdtempSync(path.join(base, "t-"));
}

export function unzipOk(p: string): boolean {
  const r = spawnSync("unzip", ["-tqq", p], { stdio: "ignore" });
  return r.status === 0;
}

export function runCli(args: string[], cwd = ROOT): { status: number | null; stdout: string; stderr: string } {
  const r = spawnSync(CLI, args, { encoding: "utf8", cwd });
  return { status: r.status, stdout: r.stdout, stderr: r.stderr };
}

export type RawEntry = {
  name: string | Buffer;
  body?: Buffer;
  method?: number;
  flags?: number;
  extra?: Buffer; // central directory extra field
  csize?: number;
  usize?: number;
  lho?: number; // override local header offset field
};

export function extraBlock(id: number, data: Buffer): Buffer {
  const h = Buffer.alloc(4);
  h.writeUInt16LE(id, 0);
  h.writeUInt16LE(data.length, 2);
  return Buffer.concat([h, data]);
}

/**
 * Builds a ZIP with a vzip comment. `sources` is the raw (uninflated) source
 * table body, or SourceMsg[]; it is DEFLATEd unless `sourcesBody` is given.
 */
export function rawZip(opts: {
  entries: RawEntry[];
  sources?: SourceMsg[] | Buffer;
  sourcesBody?: Buffer; // exact compressed body
  index?: Buffer; // raw CdIndex (inflated); deflated
  indexBody?: Buffer;
  magic?: string;
  cdHook?: (cd: Buffer) => Buffer;
  pageRecs?: (recs: Buffer[], names: string[]) => void;
}): Buffer {
  const out: Buffer[] = [];
  let off = 0;
  const recs: Buffer[] = [];
  const names: string[] = [];
  const add = (e: RawEntry) => {
    const name = typeof e.name === "string" ? Buffer.from(e.name, "utf8") : e.name;
    const body = e.body ?? Buffer.alloc(0);
    const method = e.method ?? 0;
    const flags = e.flags ?? 0x800;
    const crc = zlib.crc32(body);
    const lh = Buffer.alloc(30);
    lh.writeUInt32LE(0x04034b50, 0);
    lh.writeUInt16LE(20, 4);
    lh.writeUInt16LE(flags, 6);
    lh.writeUInt16LE(method, 8);
    lh.writeUInt32LE(crc, 14);
    lh.writeUInt32LE(e.csize ?? body.length, 18);
    lh.writeUInt32LE(e.usize ?? body.length, 22);
    lh.writeUInt16LE(name.length, 26);
    const lho = off;
    out.push(lh, name, body);
    off += 30 + name.length + body.length;
    const extra = e.extra ?? Buffer.alloc(0);
    const cd = Buffer.alloc(46);
    cd.writeUInt32LE(0x02014b50, 0);
    cd.writeUInt16LE(20, 4);
    cd.writeUInt16LE(20, 6);
    cd.writeUInt16LE(flags, 8);
    cd.writeUInt16LE(method, 10);
    cd.writeUInt32LE(crc, 16);
    cd.writeUInt32LE(e.csize ?? body.length, 20);
    cd.writeUInt32LE(e.usize ?? body.length, 24);
    cd.writeUInt16LE(name.length, 28);
    cd.writeUInt16LE(extra.length, 30);
    cd.writeUInt32LE(e.lho ?? lho, 42);
    recs.push(Buffer.concat([cd, name, extra]));
    names.push(name.toString("latin1"));
    return lho + 30 + name.length;
  };
  for (const e of opts.entries) add(e);
  const st = opts.sources === undefined ? Buffer.alloc(0) : Buffer.isBuffer(opts.sources) ? opts.sources : encodeSourceTable(opts.sources);
  const sBody = opts.sourcesBody ?? zlib.deflateRawSync(st);
  const sOff = add({ name: "__vz__/sources", body: sBody, method: 8, usize: st.length });
  let iOff = 0, iBody: Buffer | null = null;
  if (opts.index !== undefined || opts.indexBody !== undefined) {
    iBody = opts.indexBody ?? zlib.deflateRawSync(opts.index!);
    iOff = add({ name: "__vz__/index", body: iBody, method: 8, usize: opts.index?.length ?? 0 });
  }
  let cd = Buffer.concat(recs);
  if (opts.cdHook) cd = opts.cdHook(cd);
  const cdOff = off;
  out.push(cd);
  const comment = Buffer.alloc(iBody ? 38 : 22);
  comment.write(opts.magic ?? "vzip/0", 0, "latin1");
  comment.writeBigUInt64LE(BigInt(sOff), 6);
  comment.writeBigUInt64LE(BigInt(sBody.length), 14);
  if (iBody) {
    comment.writeBigUInt64LE(BigInt(iOff), 22);
    comment.writeBigUInt64LE(BigInt(iBody.length), 30);
  }
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(recs.length, 8);
  eocd.writeUInt16LE(recs.length, 10);
  eocd.writeUInt32LE(cd.length, 12);
  eocd.writeUInt32LE(cdOff, 16);
  eocd.writeUInt16LE(comment.length, 20);
  out.push(eocd, comment);
  return Buffer.concat(out);
}

export function writeTmp(dir: string, name: string, data: Buffer | string): string {
  const p = path.join(dir, name);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, data);
  return p;
}

export { execFileSync };
