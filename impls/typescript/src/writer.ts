// vzip writer (spec §3, §4, §6, §7, §9).
import * as zlib from "node:zlib";
import { WriterInputError } from "./errors.ts";
import {
  type Pinned,
  type Range,
  type Source,
  U64_MAX,
  decodeUtf8,
  encodeCdIndex,
  encodeConcat,
  encodeRange,
  encodeSourceTable,
  encodeUtf8,
} from "./proto.ts";
import { isUriReference } from "./uri.ts";
import {
  EXTRA_CONCAT,
  EXTRA_RANGE,
  EXTRA_ZIP64,
  INDEX_KEY,
  MAGIC,
  MAX_PAYLOAD,
  SIG_CENTRAL,
  SIG_EOCD,
  SIG_LOCAL,
  SIG_ZIP64_EOCD,
  SIG_ZIP64_LOCATOR,
  SOURCES_KEY,
  compareBytes,
  isFormatKey,
} from "./zipconst.ts";

export type WriterEntry =
  | { key: string; bytes: Uint8Array; compress?: boolean; pinned?: boolean }
  | { key: string; ranges: Range[] };

export interface WriteOptions {
  /** null/undefined: no page index; otherwise target bytes of CD records per page. */
  pageSize?: number | null;
  /** Reference bodies mirror the payload (true, default) or are empty. */
  mirror?: boolean;
}

const DOS_TIME = 0;
const DOS_DATE = (0 << 9) | (1 << 5) | 1; // 1980-01-01

interface Prepared {
  key: string;
  nameBytes: Buffer;
  method: number;
  body: Buffer;
  crc: number;
  usize: number;
  refBlock?: { id: number; payload: Buffer };
  pinned: boolean;
  lho: bigint;
  isFormat: boolean;
}

const fail = (m: string): never => {
  throw new WriterInputError(m);
};

/** True iff `s` is a strong entity tag per §6.1. */
export function isStrongEtag(s: string): boolean {
  return /^"[\x21\x23-\x7e]*"$/.test(s);
}

function validKey(k: string): boolean {
  if (k === "") return false;
  // JS strings may hold lone surrogates, which are not valid UTF-8.
  return decodeUtf8(Buffer.from(k, "utf8")) === k;
}

/** Build a vzip archive in memory. Throws WriterInputError for rejected input (§9.1). */
export function writeArchive(sources: Source[], entries: WriterEntry[], opts: WriteOptions = {}): Buffer {
  const pageSize = opts.pageSize ?? null;
  const mirror = opts.mirror ?? true;
  if (pageSize !== null && (!Number.isInteger(pageSize) || pageSize < 1)) fail("page size must be an integer >= 1");

  // ---- validate (§9.1)
  const byKey = new Map<string, WriterEntry>();
  for (const e of entries) {
    if (!validKey(e.key)) fail(`invalid key ${JSON.stringify(e.key)}`);
    if (isFormatKey(e.key)) fail(`key ${e.key} is reserved for a format entry`);
    if (Buffer.byteLength(e.key, "utf8") > 0xffff) fail(`key is longer than 65535 UTF-8 bytes`);
    if (byKey.has(e.key)) fail(`duplicate key ${e.key}`);
    byKey.set(e.key, e);
    if ("bytes" in e) {
      if (e.pinned && pageSize === null) fail(`entry ${e.key} is pinned but there is no page index`);
    }
  }
  sources.forEach((s, i) => {
    const hasPin = s.size !== undefined || s.etag !== undefined || s.modifiedNotAfter !== undefined;
    if (s.kind === "url") {
      if (s.url === undefined || s.url === "") fail(`source ${i}: empty url`);
      if (!isUriReference(s.url!)) fail(`source ${i}: '${s.url}' is not an RFC 3986 URI-reference`);
      if (s.etag !== undefined && !isStrongEtag(s.etag)) fail(`source ${i}: etag pin is not a strong entity tag`);
      if (s.size !== undefined && (s.size < 0n || s.size > U64_MAX)) fail(`source ${i}: size pin out of range`);
      if (s.modifiedNotAfter !== undefined && (s.modifiedNotAfter < -(1n << 63n) || s.modifiedNotAfter >= 1n << 63n)) {
        fail(`source ${i}: modified_not_after out of range`);
      }
    } else {
      if (hasPin) fail(`source ${i}: pins are allowed only on url sources`);
      if (s.kind === "key") {
        const k = s.key!;
        const target = byKey.get(k);
        if (isFormatKey(k)) fail(`source ${i}: key source names format entry ${k}`);
        if (target === undefined) fail(`source ${i}: key source names absent key ${JSON.stringify(k)}`);
        if (target !== undefined && !("bytes" in target)) fail(`source ${i}: key source names reference entry ${k}`);
      }
    }
  });

  // ---- prepare entries
  const prepared: Prepared[] = [];
  for (const e of entries) {
    const nameBytes = Buffer.from(e.key, "utf8");
    if ("bytes" in e) {
      const raw = Buffer.from(e.bytes);
      if (raw.length >= 0xffffffff) fail(`entry ${e.key}: too large`);
      const body = e.compress ? zlib.deflateRawSync(raw) : raw;
      if (body.length >= 0xffffffff) fail(`entry ${e.key}: compressed size too large`);
      prepared.push({
        key: e.key, nameBytes, method: e.compress ? 8 : 0, body, crc: zlib.crc32(raw), usize: raw.length,
        pinned: !!e.pinned, lho: 0n, isFormat: false,
      });
    } else {
      let total = 0n;
      for (const [j, r] of e.ranges.entries()) {
        if (r.data !== undefined) {
          if (r.source !== 0 || r.offset !== 0n || r.length !== 0n) fail(`entry ${e.key}: range ${j} mixes literal and source fields`);
          total += BigInt(r.data.length);
        } else {
          if (r.source < 0 || r.source >= sources.length) fail(`entry ${e.key}: range ${j} names source ${r.source}, but there are ${sources.length} sources`);
          if (r.offset < 0n || r.length < 0n || r.offset + r.length > U64_MAX) fail(`entry ${e.key}: range ${j}: offset + length exceeds 2^64-1`);
          total += r.length;
        }
      }
      if (total > U64_MAX) fail(`entry ${e.key}: total size exceeds 2^64-1`);
      const single = e.ranges.length === 1;
      const payload = Buffer.from(single ? encodeRange(e.ranges[0]) : encodeConcat(e.ranges));
      if (payload.length > MAX_PAYLOAD) fail(`entry ${e.key}: reference payload is ${payload.length} bytes (max ${MAX_PAYLOAD})`);
      const body = mirror ? payload : Buffer.alloc(0);
      prepared.push({
        key: e.key, nameBytes, method: 0, body, crc: zlib.crc32(body), usize: body.length,
        refBlock: { id: single ? EXTRA_RANGE : EXTRA_CONCAT, payload }, pinned: false, lho: 0n, isFormat: false,
      });
    }
  }

  const fmt = (key: string, content: Uint8Array): Prepared => {
    const raw = Buffer.from(content);
    return {
      key, nameBytes: Buffer.from(key, "utf8"), method: 8, body: zlib.deflateRawSync(raw), crc: zlib.crc32(raw),
      usize: raw.length, pinned: false, lho: 0n, isFormat: true,
    };
  };
  const sourcesEntry = fmt(SOURCES_KEY, encodeSourceTable(sources));

  // ---- layout (§9.2): entries, sources, pinned entries, index, CD
  const chunks: Buffer[] = [];
  let pos = 0n;
  const emit = (p: Prepared) => {
    p.lho = pos;
    const h = Buffer.alloc(30);
    h.writeUInt32LE(SIG_LOCAL, 0);
    h.writeUInt16LE(20, 4);
    h.writeUInt16LE(0x0800, 6);
    h.writeUInt16LE(p.method, 8);
    h.writeUInt16LE(DOS_TIME, 10);
    h.writeUInt16LE(DOS_DATE, 12);
    h.writeUInt32LE(p.crc, 14);
    h.writeUInt32LE(p.body.length, 18);
    h.writeUInt32LE(p.usize, 22);
    h.writeUInt16LE(p.nameBytes.length, 26);
    h.writeUInt16LE(0, 28);
    chunks.push(h, p.nameBytes, p.body);
    pos += BigInt(30 + p.nameBytes.length + p.body.length);
  };
  const bodyOffset = (p: Prepared) => p.lho + 30n + BigInt(p.nameBytes.length);

  for (const p of prepared) if (!p.pinned) emit(p);
  emit(sourcesEntry);
  for (const p of prepared) if (p.pinned) emit(p);

  // CD records for body entries (sorted in UTF-8 order)
  const body = [...prepared].sort((x, y) => compareBytes(x.nameBytes, y.nameBytes));
  const bodyRecs = body.map(cdRecord);
  let indexEntry: Prepared | null = null;
  if (pageSize !== null) {
    const pages: { firstKey: string; offset: bigint; length: bigint }[] = [];
    let off = 0n;
    for (let i = 0; i < bodyRecs.length; i++) {
      const len = BigInt(bodyRecs[i].length);
      const last = pages[pages.length - 1];
      if (last === undefined || last.length >= BigInt(pageSize)) {
        pages.push({ firstKey: body[i].key, offset: off, length: len });
      } else {
        last.length += len;
      }
      off += len;
    }
    const pinned: Pinned[] = prepared
      .filter((p) => p.pinned)
      .map((p) => ({ key: p.key, dataOffset: bodyOffset(p), size: BigInt(p.usize), csize: BigInt(p.body.length), method: p.method }));
    indexEntry = fmt(INDEX_KEY, encodeCdIndex({ pages, pinned }));
    emit(indexEntry);
  }

  const cdOffset = pos;
  const fmtRecs = [cdRecord(sourcesEntry)];
  if (indexEntry) fmtRecs.push(cdRecord(indexEntry));
  const cd = Buffer.concat([...bodyRecs, ...fmtRecs]);
  chunks.push(cd);
  pos += BigInt(cd.length);
  const cdSize = BigInt(cd.length);
  const count = BigInt(bodyRecs.length + fmtRecs.length);

  const needZip64 = count >= 0xffffn || cdSize >= 0xffffffffn || cdOffset >= 0xffffffffn;
  if (needZip64) {
    const z = Buffer.alloc(56);
    z.writeUInt32LE(SIG_ZIP64_EOCD, 0);
    z.writeBigUInt64LE(44n, 4);
    z.writeUInt16LE(45, 12);
    z.writeUInt16LE(45, 14);
    z.writeUInt32LE(0, 16);
    z.writeUInt32LE(0, 20);
    z.writeBigUInt64LE(count, 24);
    z.writeBigUInt64LE(count, 32);
    z.writeBigUInt64LE(cdSize, 40);
    z.writeBigUInt64LE(cdOffset, 48);
    const loc = Buffer.alloc(20);
    loc.writeUInt32LE(SIG_ZIP64_LOCATOR, 0);
    loc.writeUInt32LE(0, 4);
    loc.writeBigUInt64LE(pos, 8);
    loc.writeUInt32LE(1, 16);
    chunks.push(z, loc);
    pos += 76n;
  }

  const comment = Buffer.alloc(indexEntry ? 38 : 22);
  comment.write(MAGIC, 0, "latin1");
  comment.writeBigUInt64LE(bodyOffset(sourcesEntry), 6);
  comment.writeBigUInt64LE(BigInt(sourcesEntry.body.length), 14);
  if (indexEntry) {
    comment.writeBigUInt64LE(bodyOffset(indexEntry), 22);
    comment.writeBigUInt64LE(BigInt(indexEntry.body.length), 30);
  }
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(SIG_EOCD, 0);
  const cnt16 = count >= 0xffffn ? 0xffff : Number(count);
  eocd.writeUInt16LE(cnt16, 8);
  eocd.writeUInt16LE(cnt16, 10);
  eocd.writeUInt32LE(cdSize >= 0xffffffffn ? 0xffffffff : Number(cdSize), 12);
  eocd.writeUInt32LE(cdOffset >= 0xffffffffn ? 0xffffffff : Number(cdOffset), 16);
  eocd.writeUInt16LE(comment.length, 20);
  chunks.push(eocd, comment);
  return Buffer.concat(chunks);
}

function cdRecord(p: Prepared): Buffer {
  const blocks: Buffer[] = [];
  const big = p.lho >= 0xffffffffn;
  if (big) {
    const z = Buffer.alloc(12);
    z.writeUInt16LE(EXTRA_ZIP64, 0);
    z.writeUInt16LE(8, 2);
    z.writeBigUInt64LE(p.lho, 4);
    blocks.push(z);
  }
  if (p.refBlock) {
    const h = Buffer.alloc(4);
    h.writeUInt16LE(p.refBlock.id, 0);
    h.writeUInt16LE(p.refBlock.payload.length, 2);
    blocks.push(h, p.refBlock.payload);
  }
  const extra = Buffer.concat(blocks);
  const r = Buffer.alloc(46);
  r.writeUInt32LE(SIG_CENTRAL, 0);
  r.writeUInt16LE(20, 4);
  r.writeUInt16LE(big ? 45 : 20, 6);
  r.writeUInt16LE(0x0800, 8);
  r.writeUInt16LE(p.method, 10);
  r.writeUInt16LE(DOS_TIME, 12);
  r.writeUInt16LE(DOS_DATE, 14);
  r.writeUInt32LE(p.crc, 16);
  r.writeUInt32LE(p.body.length, 20);
  r.writeUInt32LE(p.usize, 24);
  r.writeUInt16LE(p.nameBytes.length, 28);
  r.writeUInt16LE(extra.length, 30);
  r.writeUInt16LE(0, 32);
  r.writeUInt16LE(0, 34);
  r.writeUInt16LE(0, 36);
  r.writeUInt32LE(0, 38);
  r.writeUInt32LE(big ? 0xffffffff : Number(p.lho), 42);
  return Buffer.concat([r, p.nameBytes, extra]);
}


