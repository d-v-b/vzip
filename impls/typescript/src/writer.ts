// vzip writer (spec §3, §4, §7, §9).
import * as fs from "node:fs";
import * as zlib from "node:zlib";
import { InputError } from "./errors.ts";
import {
  type CdIndex,
  type Page,
  type Pinned,
  type Range,
  type Source,
  encodeCdIndex,
  encodeConcat,
  encodeRange,
  encodeSourceTable,
} from "./proto.ts";
import { EXTRA_CONCAT, EXTRA_RANGE, INDEX_KEY, SOURCES_KEY, isStrongEtag } from "./reader.ts";
import { isUriReference } from "./url.ts";

const U64_MAX = (1n << 64n) - 1n;
const U32_MAX = (1n << 32n) - 1n;
const MAX_PAYLOAD = 65519;

export type EntryInput =
  | { key: string; bytes: Uint8Array; compress: boolean; pinned: boolean }
  | { key: string; ranges: Range[] };

export interface WriteInput {
  pageSize: number | null;
  mirror: boolean;
  sources: Source[];
  entries: EntryInput[];
}

interface Prepared {
  key: string;
  name: Buffer;
  method: number;
  crc: number;
  usize: number;
  body: Uint8Array;
  extraBlock: { id: number; data: Uint8Array } | null;
  pinned: boolean;
  lho: bigint;
}

function crc32(b: Uint8Array): number {
  return zlib.crc32(b) >>> 0;
}

function deflate(b: Uint8Array): Buffer {
  return zlib.deflateRawSync(b);
}

/** Validate the writer input (spec §9.1). Throws InputError. */
export function validate(inp: WriteInput): void {
  const bad = (m: string) => {
    throw new InputError(m);
  };
  const nsrc = inp.sources.length;
  for (let i = 0; i < nsrc; i++) {
    const s = inp.sources[i];
    if (s.kind === null) bad(`source ${i} has no kind`);
    const k = s.kind!;
    const hasPin = s.size !== null || s.etag !== null || s.modifiedNotAfter !== null;
    if (k.type === "url") {
      if (k.value === "") bad(`source ${i}: url is empty`);
      if (!isUriReference(k.value)) bad(`source ${i}: url ${JSON.stringify(k.value)} is not an RFC 3986 URI-reference`);
    } else if (hasPin) bad(`source ${i}: pins are only allowed on url sources`);
    if (k.type === "key" && !k.value.isWellFormed()) bad(`source ${i}: key is not valid Unicode`);
    if (s.etag !== null && !isStrongEtag(s.etag)) bad(`source ${i}: etag ${JSON.stringify(s.etag)} is not a strong entity tag`);
    if (s.size !== null && (s.size < 0n || s.size > U64_MAX)) bad(`source ${i}: size out of range`);
    if (s.modifiedNotAfter !== null && (s.modifiedNotAfter < -(1n << 63n) || s.modifiedNotAfter >= 1n << 63n)) {
      bad(`source ${i}: modified_not_after out of range`);
    }
  }
  const keys = new Map<string, EntryInput>();
  for (const e of inp.entries) {
    if (e.key === "") bad("empty key");
    if (!e.key.isWellFormed()) bad(`key ${JSON.stringify(e.key)} is not valid UTF-8`);
    if (e.key === SOURCES_KEY || e.key === INDEX_KEY) bad(`key ${e.key} is reserved for a format entry`);
    if (Buffer.byteLength(e.key, "utf8") > 65535) bad("key longer than 65535 bytes");
    if (keys.has(e.key)) bad(`duplicate key ${JSON.stringify(e.key)}`);
    keys.set(e.key, e);
    if ("bytes" in e) {
      if (e.pinned && inp.pageSize === null) bad(`entry ${JSON.stringify(e.key)} is pinned but there is no page index`);
      if (e.bytes.length >= 0xffffffff) bad(`entry ${JSON.stringify(e.key)} is 4 GiB or larger`);
    }
  }
  for (let i = 0; i < nsrc; i++) {
    const k = inp.sources[i].kind!;
    if (k.type === "key") {
      const target = keys.get(k.value);
      if (k.value === SOURCES_KEY || k.value === INDEX_KEY) bad(`source ${i}: key source names a format entry`);
      if (target === undefined) bad(`source ${i}: key source names absent key ${JSON.stringify(k.value)}`);
      if (!("bytes" in target!)) bad(`source ${i}: key source names a reference entry`);
    }
  }
  for (const e of inp.entries) {
    if (!("ranges" in e)) continue;
    let total = 0n;
    for (const r of e.ranges) {
      if (r.data !== null) {
        if (r.source !== 0 || r.offset !== 0n || r.length !== 0n) bad(`entry ${JSON.stringify(e.key)}: literal range with source fields`);
        total += BigInt(r.data.length);
      } else {
        if (r.source < 0 || BigInt(r.source) > U32_MAX || r.source >= nsrc) {
          bad(`entry ${JSON.stringify(e.key)}: source ${r.source} out of range (${nsrc} sources)`);
        }
        if (r.offset < 0n || r.length < 0n) bad(`entry ${JSON.stringify(e.key)}: negative offset or length`);
        if (r.offset + r.length > U64_MAX) bad(`entry ${JSON.stringify(e.key)}: offset + length exceeds 2^64-1`);
        total += r.length;
      }
    }
    if (total > U64_MAX) bad(`entry ${JSON.stringify(e.key)}: reference size exceeds 2^64-1`);
    const payload = e.ranges.length === 1 ? encodeRange(e.ranges[0]) : encodeConcat(e.ranges);
    if (payload.length > MAX_PAYLOAD) bad(`entry ${JSON.stringify(e.key)}: reference payload is ${payload.length} bytes (max ${MAX_PAYLOAD})`);
  }
  if (inp.pageSize !== null && !(Number.isInteger(inp.pageSize) && inp.pageSize >= 1)) bad("page_size must be an integer >= 1");
}

function prepare(e: EntryInput, mirror: boolean): Prepared {
  const name = Buffer.from(e.key, "utf8");
  if ("bytes" in e) {
    const body = e.compress ? deflate(e.bytes) : e.bytes;
    if (body.length >= 0xffffffff) throw new InputError(`entry ${JSON.stringify(e.key)}: compressed size is 4 GiB or larger`);
    return {
      key: e.key,
      name,
      method: e.compress ? 8 : 0,
      crc: crc32(e.bytes),
      usize: e.bytes.length,
      body,
      extraBlock: null,
      pinned: e.pinned,
      lho: 0n,
    };
  }
  const single = e.ranges.length === 1;
  const payload = single ? encodeRange(e.ranges[0]) : encodeConcat(e.ranges);
  const body = mirror ? payload : new Uint8Array(0);
  return {
    key: e.key,
    name,
    method: 0,
    crc: crc32(body),
    usize: body.length,
    body,
    extraBlock: { id: single ? EXTRA_RANGE : EXTRA_CONCAT, data: payload },
    pinned: false,
    lho: 0n,
  };
}

function formatEntry(key: string, content: Uint8Array): Prepared {
  const body = deflate(content);
  return {
    key,
    name: Buffer.from(key, "utf8"),
    method: 8,
    crc: crc32(content),
    usize: content.length,
    body,
    extraBlock: null,
    pinned: false,
    lho: 0n,
  };
}

const DOS_TIME = 0;
const DOS_DATE = (0 << 9) | (1 << 5) | 1; // 1980-01-01

function localHeader(p: Prepared, versionNeeded: number): Buffer {
  const h = Buffer.alloc(30);
  h.writeUInt32LE(0x04034b50, 0);
  h.writeUInt16LE(versionNeeded, 4);
  h.writeUInt16LE(0x0800, 6);
  h.writeUInt16LE(p.method, 8);
  h.writeUInt16LE(DOS_TIME, 10);
  h.writeUInt16LE(DOS_DATE, 12);
  h.writeUInt32LE(p.crc, 14);
  h.writeUInt32LE(p.body.length, 18);
  h.writeUInt32LE(p.usize, 22);
  h.writeUInt16LE(p.name.length, 26);
  h.writeUInt16LE(0, 28);
  return Buffer.concat([h, p.name]);
}

function cdRecord(p: Prepared): Buffer {
  const blocks: Buffer[] = [];
  const z64 = p.lho >= 0xffffffffn;
  if (z64) {
    const b = Buffer.alloc(12);
    b.writeUInt16LE(0x0001, 0);
    b.writeUInt16LE(8, 2);
    b.writeBigUInt64LE(p.lho, 4);
    blocks.push(b);
  }
  if (p.extraBlock) {
    const hdr = Buffer.alloc(4);
    hdr.writeUInt16LE(p.extraBlock.id, 0);
    hdr.writeUInt16LE(p.extraBlock.data.length, 2);
    blocks.push(hdr, Buffer.from(p.extraBlock.data));
  }
  const extra = Buffer.concat(blocks);
  const h = Buffer.alloc(46);
  h.writeUInt32LE(0x02014b50, 0);
  h.writeUInt16LE(20, 4);
  h.writeUInt16LE(z64 ? 45 : 20, 6);
  h.writeUInt16LE(0x0800, 8);
  h.writeUInt16LE(p.method, 10);
  h.writeUInt16LE(DOS_TIME, 12);
  h.writeUInt16LE(DOS_DATE, 14);
  h.writeUInt32LE(p.crc, 16);
  h.writeUInt32LE(p.body.length, 20);
  h.writeUInt32LE(p.usize, 24);
  h.writeUInt16LE(p.name.length, 28);
  h.writeUInt16LE(extra.length, 30);
  h.writeUInt16LE(0, 32);
  h.writeUInt16LE(0, 34);
  h.writeUInt16LE(0, 36);
  h.writeUInt32LE(0, 38);
  h.writeUInt32LE(z64 ? 0xffffffff : Number(p.lho), 42);
  return Buffer.concat([h, p.name, extra]);
}

/** Build a vzip archive and write it to outPath. Throws InputError on invalid input (no file is created). */
export function writeArchive(inp: WriteInput, outPath: string): void {
  validate(inp);
  const prepared = inp.entries.map((e) => prepare(e, inp.mirror));
  const paged = inp.pageSize !== null;
  const regular = prepared.filter((p) => !p.pinned);
  const pinnedEntries = prepared.filter((p) => p.pinned).sort((x, y) => Buffer.compare(x.name, y.name));
  const sourcesEntry = formatEntry(SOURCES_KEY, encodeSourceTable(inp.sources));

  // Layout: [entries] [__vz__/sources] [pinned entries] [__vz__/index] [CD] [zip64 end] [EOCD+comment]
  const chunks: Uint8Array[] = [];
  let off = 0n;
  const place = (p: Prepared) => {
    p.lho = off;
    const lh = localHeader(p, p.lho >= 0xffffffffn ? 45 : 20);
    chunks.push(lh, p.body);
    off += BigInt(lh.length + p.body.length);
  };
  for (const p of regular) place(p);
  place(sourcesEntry);
  for (const p of pinnedEntries) place(p);

  const body = [...prepared].sort((x, y) => Buffer.compare(x.name, y.name));
  const bodyRecords = body.map(cdRecord);

  let indexEntry: Prepared | null = null;
  if (paged) {
    const pages: Page[] = [];
    let cur: Page | null = null;
    let pos = 0n;
    for (let i = 0; i < body.length; i++) {
      const len = BigInt(bodyRecords[i].length);
      if (cur === null || cur.length >= BigInt(inp.pageSize!)) {
        cur = { firstKey: body[i].key, offset: pos, length: 0n };
        pages.push(cur);
      }
      cur.length += len;
      pos += len;
    }
    const pinned: Pinned[] = pinnedEntries.map((p) => ({
      key: p.key,
      dataOffset: p.lho + 30n + BigInt(p.name.length),
      size: BigInt(p.usize),
      csize: BigInt(p.body.length),
      method: p.method,
    }));
    const idx: CdIndex = { pages, pinned };
    indexEntry = formatEntry(INDEX_KEY, encodeCdIndex(idx));
    place(indexEntry);
  }

  const cdOffset = off;
  const cdParts = [...bodyRecords, cdRecord(sourcesEntry)];
  if (indexEntry) cdParts.push(cdRecord(indexEntry));
  const cd = Buffer.concat(cdParts);
  chunks.push(cd);
  off += BigInt(cd.length);
  const cdSize = BigInt(cd.length);
  const nEntries = cdParts.length;

  const comment = Buffer.alloc(indexEntry ? 38 : 22);
  comment.write("vzip/0", 0, "latin1");
  comment.writeBigUInt64LE(sourcesEntry.lho + 30n + BigInt(sourcesEntry.name.length), 6);
  comment.writeBigUInt64LE(BigInt(sourcesEntry.body.length), 14);
  if (indexEntry) {
    comment.writeBigUInt64LE(indexEntry.lho + 30n + BigInt(indexEntry.name.length), 22);
    comment.writeBigUInt64LE(BigInt(indexEntry.body.length), 30);
  }

  const needZ64 = nEntries >= 0xffff || cdSize >= 0xffffffffn || cdOffset >= 0xffffffffn;
  if (needZ64) {
    const z = Buffer.alloc(56);
    z.writeUInt32LE(0x06064b50, 0);
    z.writeBigUInt64LE(44n, 4);
    z.writeUInt16LE(45, 12);
    z.writeUInt16LE(45, 14);
    z.writeUInt32LE(0, 16);
    z.writeUInt32LE(0, 20);
    z.writeBigUInt64LE(BigInt(nEntries), 24);
    z.writeBigUInt64LE(BigInt(nEntries), 32);
    z.writeBigUInt64LE(cdSize, 40);
    z.writeBigUInt64LE(cdOffset, 48);
    const loc = Buffer.alloc(20);
    loc.writeUInt32LE(0x07064b50, 0);
    loc.writeUInt32LE(0, 4);
    loc.writeBigUInt64LE(off, 8);
    loc.writeUInt32LE(1, 16);
    chunks.push(z, loc);
    off += 76n;
  }
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  const n16 = nEntries >= 0xffff ? 0xffff : nEntries;
  eocd.writeUInt16LE(n16, 8);
  eocd.writeUInt16LE(n16, 10);
  eocd.writeUInt32LE(cdSize >= 0xffffffffn ? 0xffffffff : Number(cdSize), 12);
  eocd.writeUInt32LE(cdOffset >= 0xffffffffn ? 0xffffffff : Number(cdOffset), 16);
  eocd.writeUInt16LE(comment.length, 20);
  chunks.push(eocd, comment);

  const fd = fs.openSync(outPath, "wx");
  try {
    for (const c of chunks) {
      let w = 0;
      while (w < c.length) w += fs.writeSync(fd, c, w, c.length - w);
    }
    fs.closeSync(fd);
  } catch (e) {
    try {
      fs.closeSync(fd);
    } catch {
      /* ignore */
    }
    try {
      fs.unlinkSync(outPath);
    } catch {
      /* ignore */
    }
    throw e;
  }
}
