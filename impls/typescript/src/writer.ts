// vzip writer (spec §3, §4, §6, §7, §9).

import fs from "node:fs";
import zlib from "node:zlib";
import { InvalidInputError } from "./errors.ts";
import { isStrongEtag } from "./http.ts";
import {
  encodeCdIndex,
  encodeConcat,
  encodeRange,
  encodeSourceTable,
  U64_MAX,
  type PageMsg,
  type PinnedMsg,
  type RangeMsg,
  type SourceMsg,
} from "./proto.ts";
import { INDEX_KEY, SOURCES_KEY } from "./reader.ts";
import { parseUriReference } from "./uri.ts";

export type WSource =
  | { url: string; size?: bigint; etag?: string; modifiedNotAfter?: bigint }
  | { key: string }
  | { data: Uint8Array };

export type WRange = { source: bigint; offset: bigint; length: bigint } | { data: Uint8Array };

export type WEntry = {
  key: string;
  bytes?: Uint8Array;
  ranges?: WRange[];
  compress?: boolean;
  pinned?: boolean;
};

export type WArchive = {
  pageSize: number | null;
  mirror: boolean;
  sources: WSource[];
  entries: WEntry[];
};

const MAX_PAYLOAD = 65519;
const U32_ALL = 0xffffffff;

const reject = (m: string): never => {
  throw new InvalidInputError(m);
};

export type Built = {
  name: Buffer; // UTF-8
  nameStr: string; // latin1 byte string, for sorting
  method: number;
  crc: number;
  csize: number;
  usize: number;
  body: Uint8Array;
  refBlock: Buffer | null; // extra block (header + payload) for reference entries
  pinned: boolean;
  isRef: boolean;
  lho: number;
};

/** Whether an entry needs ZIP64 sizes (§3.1 rule 7). */
const isLarge = (b: Built) => b.usize >= U32_ALL || b.csize >= U32_ALL;

/** The local header; a large entry's sizes go in a 20-byte ZIP64 extra field (§3.1 rules 4, 7). */
export function localHeader(b: Built): Buffer {
  const large = isLarge(b);
  const h = Buffer.alloc(30);
  h.writeUInt32LE(0x04034b50, 0);
  h.writeUInt16LE(large ? 45 : 20, 4);
  h.writeUInt16LE(0x0800, 6);
  h.writeUInt16LE(b.method, 8);
  h.writeUInt16LE(0, 10); // time 00:00
  h.writeUInt16LE(0x21, 12); // 1980-01-01
  h.writeUInt32LE(b.crc, 14);
  h.writeUInt32LE(large ? U32_ALL : b.csize, 18);
  h.writeUInt32LE(large ? U32_ALL : b.usize, 22);
  h.writeUInt16LE(b.name.length, 26);
  if (!large) {
    h.writeUInt16LE(0, 28);
    return Buffer.concat([h, b.name]);
  }
  const z = Buffer.alloc(20);
  z.writeUInt16LE(0x0001, 0);
  z.writeUInt16LE(16, 2);
  z.writeBigUInt64LE(BigInt(b.usize), 4);
  z.writeBigUInt64LE(BigInt(b.csize), 12);
  h.writeUInt16LE(z.length, 28);
  return Buffer.concat([h, b.name, z]);
}

/** The central directory record; its ZIP64 block holds the sizes of a large
 * entry, then an offset that does not fit, and nothing else (§3.2). */
export function cdRecord(b: Built): Buffer {
  const large = isLarge(b);
  const bigOff = b.lho >= U32_ALL;
  const vals: bigint[] = [];
  if (large) vals.push(BigInt(b.usize), BigInt(b.csize));
  if (bigOff) vals.push(BigInt(b.lho));
  const zip64 = vals.length > 0;
  const extras: Buffer[] = [];
  if (zip64) {
    const z = Buffer.alloc(4 + 8 * vals.length);
    z.writeUInt16LE(0x0001, 0);
    z.writeUInt16LE(8 * vals.length, 2);
    vals.forEach((v, i) => z.writeBigUInt64LE(v, 4 + 8 * i));
    extras.push(z);
  }
  if (b.refBlock) extras.push(b.refBlock);
  const extra = Buffer.concat(extras);
  const h = Buffer.alloc(46);
  h.writeUInt32LE(0x02014b50, 0);
  h.writeUInt16LE(20, 4); // version made by
  h.writeUInt16LE(zip64 ? 45 : 20, 6);
  h.writeUInt16LE(0x0800, 8);
  h.writeUInt16LE(b.method, 10);
  h.writeUInt16LE(0, 12);
  h.writeUInt16LE(0x21, 14);
  h.writeUInt32LE(b.crc, 16);
  h.writeUInt32LE(large ? U32_ALL : b.csize, 20);
  h.writeUInt32LE(large ? U32_ALL : b.usize, 24);
  h.writeUInt16LE(b.name.length, 28);
  h.writeUInt16LE(extra.length, 30);
  h.writeUInt16LE(0, 32);
  h.writeUInt16LE(0, 34);
  h.writeUInt16LE(0, 36);
  h.writeUInt32LE(0, 38);
  h.writeUInt32LE(bigOff ? U32_ALL : b.lho, 42);
  return Buffer.concat([h, b.name, extra]);
}

function makeBytes(name: string, data: Uint8Array, compress: boolean, pinned: boolean): Built {
  const nameBuf = Buffer.from(name, "utf8");
  if (compress && data.length >= U32_ALL) throw new InvalidInputError(`entry ${JSON.stringify(name)} of 4 GiB or more must be STORED`);
  const body = compress ? zlib.deflateRawSync(data) : data;
  return {
    name: nameBuf,
    nameStr: nameBuf.toString("latin1"),
    method: compress ? 8 : 0,
    crc: zlib.crc32(data),
    csize: body.length,
    usize: data.length,
    body,
    refBlock: null,
    pinned,
    isRef: false,
    lho: 0,
  };
}

/** Validates the input per spec §9.1 and returns the encoded archive as a list of buffers. */
export function buildArchive(a: WArchive): Buffer[] {
  // ---- validation
  if (a.pageSize !== null && !(Number.isInteger(a.pageSize) && a.pageSize >= 1)) reject("page_size must be >= 1");
  const byKey = new Map<string, WEntry>();
  for (const e of a.entries) {
    if (e.key === "") reject("empty key");
    if (!e.key.isWellFormed()) reject(`key ${JSON.stringify(e.key)} is not valid UTF-8`);
    if (Buffer.byteLength(e.key, "utf8") > 65535) reject("key longer than 65535 bytes");
    if (e.key === SOURCES_KEY || e.key === INDEX_KEY) reject(`key ${e.key} is reserved for a format entry`);
    if (byKey.has(e.key)) reject(`duplicate key ${JSON.stringify(e.key)}`);
    if ((e.bytes === undefined) === (e.ranges === undefined)) reject(`entry ${JSON.stringify(e.key)}: needs exactly one of bytes, ranges`);
    if (e.ranges !== undefined && e.compress) reject(`entry ${JSON.stringify(e.key)}: compress on a reference entry`);
    if (e.pinned) {
      if (a.pageSize === null) reject(`entry ${JSON.stringify(e.key)}: pinned without a page index`);
      if (e.ranges !== undefined) reject(`entry ${JSON.stringify(e.key)}: pinned reference entry`);
    }
    byKey.set(e.key, e);
  }
  const sourceSizes: (bigint | null)[] = [];
  const srcMsgs: SourceMsg[] = a.sources.map((s, i) => {
    const m: SourceMsg = { kind: null, size: null, etag: null, modifiedNotAfter: null };
    if ("url" in s) {
      if (s.url === "") reject(`source ${i}: empty url`);
      if (parseUriReference(s.url) === null) reject(`source ${i}: url is not an RFC 3986 URI-reference: ${JSON.stringify(s.url)}`);
      if (s.etag !== undefined && !isStrongEtag(s.etag)) reject(`source ${i}: etag is not a strong entity tag`);
      if (s.size !== undefined && (s.size < 0n || s.size > U64_MAX)) reject(`source ${i}: size out of range`);
      if (s.modifiedNotAfter !== undefined && (s.modifiedNotAfter < -(1n << 63n) || s.modifiedNotAfter >= 1n << 63n))
        reject(`source ${i}: modified_not_after out of range`);
      m.kind = "url";
      m.url = s.url;
      m.size = s.size ?? null;
      m.etag = s.etag ?? null;
      m.modifiedNotAfter = s.modifiedNotAfter ?? null;
      sourceSizes.push(null);
    } else if ("key" in s) {
      if (s.key === "") reject(`source ${i}: empty key`);
      if (!s.key.isWellFormed()) reject(`source ${i}: key is not valid UTF-8`);
      if (s.key === SOURCES_KEY || s.key === INDEX_KEY) reject(`source ${i}: key names a format entry`);
      const t = byKey.get(s.key);
      if (t === undefined) reject(`source ${i}: key ${JSON.stringify(s.key)} is absent`);
      if (t!.bytes === undefined) reject(`source ${i}: key ${JSON.stringify(s.key)} is a reference entry`);
      m.kind = "key";
      m.key = Buffer.from(s.key, "utf8").toString("latin1");
      sourceSizes.push(BigInt(t!.bytes!.length));
    } else {
      m.kind = "data";
      m.data = s.data;
      sourceSizes.push(BigInt(s.data.length));
    }
    return m;
  });

  // ---- entries
  const built: Built[] = [];
  for (const e of a.entries) {
    if (e.bytes !== undefined) {
      built.push(makeBytes(e.key, e.bytes, !!e.compress, !!e.pinned));
      continue;
    }
    const parts: RangeMsg[] = [];
    let total = 0n;
    e.ranges!.forEach((r, j) => {
      if ("data" in r) {
        parts.push({ source: 0n, offset: 0n, length: 0n, data: r.data });
        total += BigInt(r.data.length);
        return;
      }
      const where = `entry ${JSON.stringify(e.key)} range ${j}`;
      if (r.source < 0n || r.offset < 0n || r.length < 0n) reject(`${where}: negative value`);
      if (r.source >= BigInt(a.sources.length)) reject(`${where}: source ${r.source} out of bounds`);
      if (r.offset > U64_MAX || r.length > U64_MAX || r.offset + r.length > U64_MAX) reject(`${where}: offset + length exceeds 2^64-1`);
      const sz = sourceSizes[Number(r.source)];
      if (sz !== null && r.offset + r.length > sz) reject(`${where}: extends past the end of its source (${sz} bytes)`);
      parts.push({ source: r.source, offset: r.offset, length: r.length, data: null });
      total += r.length;
    });
    if (total > U64_MAX) reject(`entry ${JSON.stringify(e.key)}: total size exceeds 2^64-1`);
    const single = parts.length === 1;
    const payload = single ? encodeRange(parts[0]) : encodeConcat(parts);
    if (payload.length > MAX_PAYLOAD) reject(`entry ${JSON.stringify(e.key)}: reference payload is ${payload.length} bytes (max ${MAX_PAYLOAD})`);
    const blk = Buffer.alloc(4);
    blk.writeUInt16LE(single ? 0x7a76 : 0x7a77, 0);
    blk.writeUInt16LE(payload.length, 2);
    const body = a.mirror ? payload : Buffer.alloc(0);
    const nameBuf = Buffer.from(e.key, "utf8");
    built.push({
      name: nameBuf,
      nameStr: nameBuf.toString("latin1"),
      method: 0,
      crc: zlib.crc32(body),
      csize: body.length,
      usize: body.length,
      body,
      refBlock: Buffer.concat([blk, payload]),
      pinned: false,
      isRef: true,
      lho: 0,
    });
  }

  // ---- layout
  const out: Buffer[] = [];
  let off = 0;
  const emit = (b: Built) => {
    b.lho = off;
    const h = localHeader(b);
    out.push(h, Buffer.from(b.body.buffer, b.body.byteOffset, b.body.byteLength));
    off += h.length + b.body.length;
  };
  const bodyOffset = (b: Built) => b.lho + 30 + b.name.length + (isLarge(b) ? 20 : 0);

  for (const b of built) if (!b.pinned) emit(b);
  const sources = makeBytes(SOURCES_KEY, encodeSourceTable(srcMsgs), true, false);
  emit(sources);
  for (const b of built) if (b.pinned) emit(b);

  const sorted = [...built].sort((x, y) => (x.nameStr < y.nameStr ? -1 : x.nameStr > y.nameStr ? 1 : 0));
  const bodyRecs = sorted.map(cdRecord);
  let index: Built | null = null;
  if (a.pageSize !== null) {
    const pages: PageMsg[] = [];
    let pos = 0;
    let cur: PageMsg | null = null;
    sorted.forEach((b, i) => {
      const len = bodyRecs[i].length;
      if (cur === null || Number(cur.length) + len > a.pageSize!) {
        cur = { firstKey: b.nameStr, offset: BigInt(pos), length: 0n };
        pages.push(cur);
      }
      cur.length += BigInt(len);
      pos += len;
    });
    const pinned: PinnedMsg[] = built
      .filter((b) => b.pinned)
      .map((b) => ({
        key: b.nameStr,
        dataOffset: BigInt(bodyOffset(b)),
        size: BigInt(b.usize),
        csize: BigInt(b.csize),
        method: BigInt(b.method),
      }));
    index = makeBytes(INDEX_KEY, encodeCdIndex({ pages, pinned }), true, false);
    emit(index);
  }

  const cdOffset = off;
  const cd = [...bodyRecs, cdRecord(sources)];
  if (index) cd.push(cdRecord(index));
  const cdBuf = Buffer.concat(cd);
  out.push(cdBuf);
  off += cdBuf.length;
  const count = cd.length;
  const cdSize = cdBuf.length;

  // §3.2: the zip64 end records are written in every archive, and the end record's
  // counts, size and offset are always all ones.
  {
    const z = Buffer.alloc(56);
    z.writeUInt32LE(0x06064b50, 0);
    z.writeBigUInt64LE(44n, 4);
    z.writeUInt16LE(45, 12);
    z.writeUInt16LE(45, 14);
    z.writeUInt32LE(0, 16);
    z.writeUInt32LE(0, 20);
    z.writeBigUInt64LE(BigInt(count), 24);
    z.writeBigUInt64LE(BigInt(count), 32);
    z.writeBigUInt64LE(BigInt(cdSize), 40);
    z.writeBigUInt64LE(BigInt(cdOffset), 48);
    const loc = Buffer.alloc(20);
    loc.writeUInt32LE(0x07064b50, 0);
    loc.writeUInt32LE(0, 4);
    loc.writeBigUInt64LE(BigInt(off), 8);
    loc.writeUInt32LE(1, 16);
    out.push(z, loc);
    off += 76;
  }
  const comment = Buffer.alloc(index ? 38 : 22);
  comment.write("vzip/0", 0, "latin1");
  comment.writeBigUInt64LE(BigInt(bodyOffset(sources)), 6);
  comment.writeBigUInt64LE(BigInt(sources.csize), 14);
  if (index) {
    comment.writeBigUInt64LE(BigInt(bodyOffset(index)), 22);
    comment.writeBigUInt64LE(BigInt(index.csize), 30);
  }
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(0, 4);
  eocd.writeUInt16LE(0, 6);
  eocd.writeUInt16LE(0xffff, 8);
  eocd.writeUInt16LE(0xffff, 10);
  eocd.writeUInt32LE(U32_ALL, 12);
  eocd.writeUInt32LE(U32_ALL, 16);
  eocd.writeUInt16LE(comment.length, 20);
  out.push(eocd, comment);
  return out;
}

/** Writes an archive to `path`; the file is not created if the input is invalid. */
export function writeArchive(a: WArchive, path: string): void {
  const bufs = buildArchive(a);
  const fd = fs.openSync(path, "wx");
  try {
    for (const b of bufs) {
      let o = 0;
      while (o < b.length) o += fs.writeSync(fd, b, o, b.length - o);
    }
  } catch (e) {
    fs.closeSync(fd);
    fs.rmSync(path, { force: true });
    throw e;
  }
  fs.closeSync(fd);
}
