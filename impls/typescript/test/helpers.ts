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
  localExtra?: Buffer; // local header extra field (a large entry's 20-byte ZIP64 field)
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
  zip64?: boolean; // false leaves out the zip64 end records (invalid since revision 9)
  // (count, count, cd size, cd offset) of the end record; all ones in a valid archive
  eocdFields?: [number, number, number, number];
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
    const lx = e.localExtra ?? Buffer.alloc(0);
    lh.writeUInt16LE(lx.length, 28);
    const lho = off;
    out.push(lh, name, lx, body);
    off += 30 + name.length + lx.length + body.length;
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
    return lho + 30 + name.length + lx.length;
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
  if (opts.zip64 ?? true) {
    const z = Buffer.alloc(56 + 20);
    z.writeUInt32LE(0x06064b50, 0);
    z.writeBigUInt64LE(44n, 4);
    z.writeUInt16LE(45, 12);
    z.writeUInt16LE(45, 14);
    z.writeBigUInt64LE(BigInt(recs.length), 24);
    z.writeBigUInt64LE(BigInt(recs.length), 32);
    z.writeBigUInt64LE(BigInt(cd.length), 40);
    z.writeBigUInt64LE(BigInt(cdOff), 48);
    z.writeUInt32LE(0x07064b50, 56);
    z.writeBigUInt64LE(BigInt(cdOff + cd.length), 64);
    z.writeUInt32LE(1, 72);
    out.push(z);
  }
  const fields = opts.eocdFields ?? [0xffff, 0xffff, 0xffffffff, 0xffffffff];
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(fields[0], 8);
  eocd.writeUInt16LE(fields[1], 10);
  eocd.writeUInt32LE(fields[2], 12);
  eocd.writeUInt32LE(fields[3], 16);
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
