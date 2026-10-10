// The zip form of a SAFE product (spec/virtualize.md §12.4): the ZIP archive's end
// records, its central directory, its local headers, and its deflated XML documents.

import type { ByteReader } from "../common.ts";
import { crc32 } from "../../deflate.ts";
import { SafeError } from "./product.ts";

const MAX_TAIL = 65557; // an EOCD record (22 bytes) and the longest comment
/** The largest deflated XML document (a datastrip metadata is 15–25 MB). */
export const MAX_DEFLATED = 1 << 26;
/** The most bytes of all the deflated XML documents together. */
export const MAX_INFLATED = 1 << 27;
const reject = (m: string): never => {
  throw new SafeError(m);
};
const view = (b: Uint8Array) => new DataView(b.buffer, b.byteOffset, b.byteLength);
const sig = (b: Uint8Array, at: number, s: string) => [...s].every((c, i) => b[at + i] === c.charCodeAt(0));
const EOCD = "PK\x05\x06", LOCATOR = "PK\x06\x07", ZIP64 = "PK\x06\x06", CENTRAL = "PK\x01\x02", LOCAL = "PK\x03\x04";

function u64(v: DataView, at: number): number {
  const x = v.getBigUint64(at, true);
  return x > BigInt(Number.MAX_SAFE_INTEGER) ? reject("a zip file's offset or size is above 2^53 − 1") : Number(x);
}

/** A central directory file header, as §12.4 reads it. */
export interface Entry {
  name: string;
  flags: number;
  method: number;
  crc: number;
  cs: number;
  us: number;
  lho: number;
  ds: number; // the data start, from the local header
}

export interface Directory {
  entries: Entry[];
  cdOffset: number;
}

function zip64Values(extra: Uint8Array, wanted: number): number[] {
  const v = view(extra);
  for (let p = 0; p + 4 <= extra.length;) {
    const id = v.getUint16(p, true), size = v.getUint16(p + 2, true);
    if (p + 4 + size > extra.length) break;
    if (id === 1) {
      if (size < 8 * wanted) reject("a zip entry's ZIP64 extra field is too short");
      return Array.from({ length: wanted }, (_, i) => u64(v, p + 4 + 8 * i));
    }
    p += 4 + size;
  }
  return reject("a zip entry needs a ZIP64 extra field and has none");
}

/** The entries of the zip file of `n` bytes (§12.4). */
export async function centralDirectory(read: ByteReader, n: number): Promise<Directory> {
  const tailLen = Math.min(n, MAX_TAIL);
  const tail = await read(n - tailLen, tailLen);
  const tv = view(tail);
  let at = tail.length - 4;
  for (;; at--) {
    if (at < 0) reject("not a zip file: no end of central directory record");
    if (sig(tail, at, EOCD) && at + 22 <= tail.length && at + 22 + tv.getUint16(at + 20, true) === tail.length) break;
  }
  const eocd = n - tailLen + at;
  const [disk, cdDisk, here0, count0] = [4, 6, 8, 10].map((o) => tv.getUint16(at + o, true));
  let [cdSize, cdOffset] = [12, 16].map((o) => tv.getUint32(at + o, true));
  let here = here0, count = count0;
  if (disk || cdDisk) reject("a zip file that spans several disks");
  if (here !== count) reject("a zip file's entry counts differ");
  let end = eocd;
  if (count === 0xffff || cdSize === 0xffffffff || cdOffset === 0xffffffff) {
    if (eocd < 20) reject("a ZIP64 zip file has no ZIP64 end of central directory locator");
    const loc = await read(eocd - 20, 20);
    const lv = view(loc);
    const rec = u64(lv, 8);
    if (!sig(loc, 0, LOCATOR) || lv.getUint32(4, true) !== 0 || lv.getUint32(16, true) !== 1) {
      reject("a ZIP64 zip file has no valid ZIP64 end of central directory locator");
    }
    if (rec + 56 > eocd - 20) reject("a zip file's ZIP64 end of central directory record is outside it");
    const body = await read(rec, 56);
    const bv = view(body);
    if (!sig(body, 0, ZIP64)) reject("a zip file has no ZIP64 end of central directory record where its locator says");
    if (bv.getUint32(16, true) || bv.getUint32(20, true)) reject("a zip file that spans several disks");
    here = u64(bv, 24);
    count = u64(bv, 32);
    cdSize = u64(bv, 40);
    cdOffset = u64(bv, 48);
    if (here !== count) reject("a zip file's entry counts differ");
    end = rec;
  }
  if (cdOffset + cdSize !== end) reject("a zip file's central directory does not end where its end records start");
  const cd = await read(cdOffset, cdSize);
  const v = view(cd);
  const entries: Entry[] = [];
  let p = 0;
  for (let i = 0; i < count; i++) {
    if (p + 46 > cd.length || !sig(cd, p, CENTRAL)) reject("a zip file's central directory has fewer entries than it says");
    const flags = v.getUint16(p + 8, true), method = v.getUint16(p + 10, true), crc = v.getUint32(p + 16, true);
    let cs = v.getUint32(p + 20, true), us = v.getUint32(p + 24, true), lho = v.getUint32(p + 42, true);
    const nn = v.getUint16(p + 28, true), ne = v.getUint16(p + 30, true), nc = v.getUint16(p + 32, true);
    const diskNo = v.getUint16(p + 34, true);
    if (p + 46 + nn + ne + nc > cd.length) reject("a zip file's central directory entry is truncated");
    const rawName = cd.subarray(p + 46, p + 46 + nn);
    const extra = cd.subarray(p + 46 + nn, p + 46 + nn + ne);
    const wanted = [us, cs, lho].map((x) => x === 0xffffffff);
    if (wanted.some((w) => w)) {
      const values = zip64Values(extra, wanted.filter((w) => w).length);
      if (wanted[0]) us = values.shift()!;
      if (wanted[1]) cs = values.shift()!;
      if (wanted[2]) lho = values.shift()!;
    }
    if (diskNo) reject("a zip entry on another disk");
    if (flags & 0x41) reject("a zip entry is encrypted");
    let name = "";
    try {
      name = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(rawName);
    } catch {
      reject("a zip entry's name is not UTF-8");
    }
    entries.push({ name, flags, method, crc, cs, us, lho, ds: -1 });
    p += 46 + nn + ne + nc;
  }
  if (p !== cd.length) reject("a zip file's central directory has more bytes than its entries");
  return { entries, cdOffset };
}

/** [the root directory D, the objects by key, the recorded keys of the directory entries, and the
 * keys of every directory entry but D's] (§12.4). */
export function productEntries(d: Directory): [string, Map<string, Entry>, string[], string[]] {
  const names = new Set<string>();
  let root: string | undefined;
  const objects = new Map<string, Entry>();
  const ignored: string[] = [];
  const dirs: string[] = [];
  for (const e of d.entries) {
    if (names.has(e.name)) reject(`a zip file has two entries named ${JSON.stringify(e.name)}`);
    names.add(e.name);
    const i = e.name.indexOf("/");
    const head = i < 0 ? e.name : e.name.slice(0, i);
    if (i < 0 || !head.endsWith(".SAFE")) reject(`a zip entry ${JSON.stringify(e.name.slice(0, 200))} is not under a .SAFE directory`);
    const key = e.name.slice(i + 1);
    if (root === undefined) root = head;
    else if (head !== root) reject("a zip file has entries under two root directories");
    if (key === "" || key.endsWith("/")) {
      if (e.us) ignored.push(key);
      if (key) dirs.push(key);
      continue;
    }
    if (key.includes("\\") || key.split("/").some((s) => s === "" || s === "." || s === "..")) {
      reject(`a zip entry's name ${JSON.stringify(e.name.slice(0, 200))} has an empty, . or .. segment, or a backslash`);
    }
    objects.set(key, e);
  }
  if (root === undefined) reject("an empty zip file");
  return [root!, objects, ignored, dirs];
}

/** Each nonempty object's data start, from its local header (§12.4). */
export async function locate(read: ByteReader, d: Directory, objects: Map<string, Entry>): Promise<void> {
  const todo = [...objects.values()].filter((e) => e.us);
  for (const e of todo) if (e.lho + 30 > d.cdOffset) reject("a zip entry's local header is beyond the entries");
  const heads = await Promise.all(todo.map((e) => read(e.lho, 30)));
  for (const [i, e] of todo.entries()) {
    const head = heads[i];
    if (!sig(head, 0, LOCAL)) reject("a zip entry has no local header where the central directory says");
    const hv = view(head);
    e.ds = e.lho + 30 + hv.getUint16(26, true) + hv.getUint16(28, true);
    if (e.ds + e.cs > d.cdOffset) reject("a zip entry's data reaches beyond the entries");
  }
  // Each object's local header and data are its own: two entries that share bytes would make
  // the output repeat them.
  let end = 0;
  for (const e of [...todo].sort((x, y) => x.lho - y.lho)) {
    if (e.lho < end) reject("two zip entries' local headers and data overlap");
    end = e.ds + e.cs;
  }
}

/** A deflated entry's bytes (§12.4): the raw deflate stream must end exactly at its
 * compressed size and give exactly its size, with its CRC-32. */
export async function inflate(data: Uint8Array, e: Entry): Promise<Uint8Array> {
  const out = new Uint8Array(e.us);
  let n = 0;
  try {
    const stream = new Blob([data as BlobPart]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
    const reader = stream.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (n + value.length > e.us) {
        await reader.cancel();
        reject("a zip entry's deflate stream does not end at its compressed size with its size");
      }
      out.set(value, n);
      n += value.length;
    }
  } catch (err) {
    if (err instanceof SafeError) throw err;
    reject(`a zip entry's deflate stream is invalid: ${(err as Error).message}`);
  }
  if (n !== e.us) reject("a zip entry's deflate stream does not end at its compressed size with its size");
  if (crc32(out) !== e.crc) reject("a zip entry's CRC-32 does not match its bytes");
  return out;
}
