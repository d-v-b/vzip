// vzip writer (spec §3, §4, §6, §7, §9).

import * as fs from "node:fs";
import * as zlib from "node:zlib";
import {
  EXTRA_CONCAT, EXTRA_RANGE, INDEX_KEY, MAX_PAYLOAD, SOURCES_KEY, U64_MAX,
  compareBytes, isFormatKey, isStrongEtag, isWellFormed, utf8,
} from "./common.ts";
import { encodeCdIndex, encodeConcat, encodeRange, encodeSourceTable, type Page, type Pinned, type Range, type Source } from "./proto.ts";
import { parseUriReference } from "./uri.ts";

export class WriteError extends Error {}

export type WriterEntry = {
  key: string;
  bytes?: Uint8Array; // bytes entry
  ranges?: Range[]; // reference entry
  compress?: boolean;
  pinned?: boolean;
};

export type WriterInput = {
  sources: Source[];
  entries: WriterEntry[];
  pageSize: number | null; // null: no page index
  mirror: boolean;
};

const DOS_TIME = 0;
const DOS_DATE = (0 << 9) | (1 << 5) | 1; // 1980-01-01
const FLAG_UTF8 = 0x0800;

function le16(v: number): Buffer {
  const b = Buffer.alloc(2);
  b.writeUInt16LE(v);
  return b;
}
function le32(v: number): Buffer {
  const b = Buffer.alloc(4);
  b.writeUInt32LE(v);
  return b;
}
function le64(v: bigint): Buffer {
  const b = Buffer.alloc(8);
  b.writeBigUInt64LE(v);
  return b;
}

type Prepared = {
  key: string;
  name: Uint8Array;
  method: number;
  body: Uint8Array;
  usize: number;
  crc: number;
  refExtra: { id: number; payload: Uint8Array } | null;
  pinned: boolean;
  isFormat: boolean;
  lho?: bigint;
};

function payloadFor(ranges: Range[]): { id: number; payload: Uint8Array } {
  if (ranges.length === 1) return { id: EXTRA_RANGE, payload: encodeRange(ranges[0]) };
  return { id: EXTRA_CONCAT, payload: encodeConcat(ranges) };
}

/** Validates the input (§9.1). Throws WriteError. */
export function validate(input: WriterInput): void {
  const W = (m: string): never => {
    throw new WriteError(m);
  };
  const byKey = new Map<string, WriterEntry>();
  for (const e of input.entries) {
    if (e.key === "") W("empty key");
    if (!isWellFormed(e.key)) W(`key ${JSON.stringify(e.key)} is not valid Unicode (cannot be UTF-8)`);
    if (isFormatKey(e.key)) W(`key ${e.key} is reserved for the format entry`);
    if (utf8(e.key).length > 65535) W(`key UTF-8 encoding longer than 65535 bytes`);
    if (byKey.has(e.key)) W(`duplicate key ${JSON.stringify(e.key)}`);
    if ((e.bytes === undefined) === (e.ranges === undefined)) W(`entry ${JSON.stringify(e.key)} must have exactly one of bytes and ranges`);
    byKey.set(e.key, e);
    if (e.ranges !== undefined) {
      if (e.compress) W(`reference entry ${JSON.stringify(e.key)} cannot be compressed`);
      if (e.pinned) W(`reference entry ${JSON.stringify(e.key)} cannot be pinned`);
      let total = 0n;
      for (const r of e.ranges) {
        if (r.data !== null) {
          if (r.source !== 0 || r.offset !== 0n || r.length !== 0n) W("literal range with source/offset/length");
          total += BigInt(r.data.length);
        } else {
          if (r.source >= input.sources.length) W(`entry ${JSON.stringify(e.key)}: source ${r.source} out of range (${input.sources.length} sources)`);
          if (r.offset < 0n || r.length < 0n) W("negative offset or length");
          if (r.offset + r.length > U64_MAX) W(`entry ${JSON.stringify(e.key)}: range end exceeds 2^64-1`);
          total += r.length;
        }
      }
      if (total > U64_MAX) W(`entry ${JSON.stringify(e.key)}: total size exceeds 2^64-1`);
      const { payload } = payloadFor(e.ranges);
      if (payload.length > MAX_PAYLOAD) W(`entry ${JSON.stringify(e.key)}: reference payload of ${payload.length} bytes exceeds ${MAX_PAYLOAD}`);
    } else {
      if (e.bytes!.length >= 0xffffffff) W(`entry ${JSON.stringify(e.key)} too large`);
      if (e.pinned && input.pageSize === null) W(`entry ${JSON.stringify(e.key)} is pinned but there is no page index`);
    }
  }
  if (input.pageSize !== null && (!Number.isSafeInteger(input.pageSize) || input.pageSize < 1)) W("page_size must be an integer >= 1");
  input.sources.forEach((s, i) => {
    if (s.kind === "url") {
      if (s.url === "") W(`source ${i}: empty url`);
      if (parseUriReference(s.url!) === null) W(`source ${i}: '${s.url}' is not an RFC 3986 URI-reference`);
    } else if (s.kind === "key") {
      if (!isWellFormed(s.key!)) W(`source ${i}: key is not valid Unicode`);
      if (isFormatKey(s.key!)) W(`source ${i}: key source names format entry ${s.key}`);
      const t = byKey.get(s.key!);
      if (t === undefined) W(`source ${i}: key source names absent key ${JSON.stringify(s.key)}`);
      if (t!.bytes === undefined) W(`source ${i}: key source names reference entry ${JSON.stringify(s.key)}`);
    } else if (s.kind !== "data") {
      W(`source ${i}: no kind`);
    }
    if (s.kind !== "url" && (s.size !== null || s.etag !== null || s.modifiedNotAfter !== null)) W(`source ${i}: pins are only allowed on url sources`);
    if (s.etag !== null && !isStrongEtag(s.etag)) W(`source ${i}: etag pin ${JSON.stringify(s.etag)} is not a strong entity tag`);
    if (s.size !== null && (s.size < 0n || s.size > U64_MAX)) W(`source ${i}: size pin out of range`);
    if (s.modifiedNotAfter !== null && (s.modifiedNotAfter < -(1n << 63n) || s.modifiedNotAfter >= 1n << 63n))
      W(`source ${i}: modified_not_after out of range`);
  });
}

function prepare(e: WriterEntry, mirror: boolean): Prepared {
  const name = utf8(e.key);
  if (e.ranges !== undefined) {
    const ex = payloadFor(e.ranges);
    const body = mirror ? ex.payload : new Uint8Array();
    return { key: e.key, name, method: 0, body, usize: body.length, crc: zlib.crc32(body), refExtra: ex, pinned: false, isFormat: false };
  }
  const raw = e.bytes!;
  const body = e.compress ? zlib.deflateRawSync(raw) : raw;
  if (body.length >= 0xffffffff) throw new WriteError(`entry ${JSON.stringify(e.key)}: compressed size too large`);
  return { key: e.key, name, method: e.compress ? 8 : 0, body, usize: raw.length, crc: zlib.crc32(raw), refExtra: null, pinned: !!e.pinned, isFormat: false };
}

function formatEntry(key: string, msg: Uint8Array): Prepared {
  const body = zlib.deflateRawSync(msg);
  return { key, name: utf8(key), method: 8, body, usize: msg.length, crc: zlib.crc32(msg), refExtra: null, pinned: false, isFormat: true };
}

function localHeader(p: Prepared): Buffer {
  return Buffer.concat([
    le32(0x04034b50), le16(20), le16(FLAG_UTF8), le16(p.method), le16(DOS_TIME), le16(DOS_DATE),
    le32(p.crc), le32(p.body.length), le32(p.usize), le16(p.name.length), le16(0), p.name,
  ]);
}

function cdRecord(p: Prepared): Buffer {
  const lho = p.lho!;
  const z64 = lho >= 0xffffffffn;
  const extras: Buffer[] = [];
  if (z64) extras.push(le16(0x0001), le16(8), le64(lho));
  if (p.refExtra) extras.push(le16(p.refExtra.id), le16(p.refExtra.payload.length), Buffer.from(p.refExtra.payload));
  const extra = Buffer.concat(extras);
  if (extra.length > 65535) throw new WriteError(`${p.key}: extra field too long`);
  return Buffer.concat([
    le32(0x02014b50), le16(20), le16(z64 ? 45 : 20), le16(FLAG_UTF8), le16(p.method), le16(DOS_TIME), le16(DOS_DATE),
    le32(p.crc), le32(p.body.length), le32(p.usize), le16(p.name.length), le16(extra.length), le16(0),
    le16(0), le16(0), le32(0), le32(z64 ? 0xffffffff : Number(lho)), p.name, extra,
  ]);
}

/** Builds the archive as a list of buffers. Validates first; throws WriteError. */
export function build(input: WriterInput): Buffer[] {
  validate(input);
  const out: Buffer[] = [];
  let off = 0n;
  const emit = (b: Uint8Array) => {
    out.push(Buffer.from(b.buffer, b.byteOffset, b.byteLength));
    off += BigInt(b.length);
  };
  const place = (p: Prepared) => {
    p.lho = off;
    emit(localHeader(p));
    emit(p.body);
  };

  const prepared = input.entries.map((e) => prepare(e, input.mirror));
  for (const p of prepared) if (!p.pinned) place(p);
  const sources = formatEntry(SOURCES_KEY, encodeSourceTable(input.sources));
  place(sources);
  const sourcesBodyOffset = sources.lho! + 30n + BigInt(sources.name.length);
  for (const p of prepared) if (p.pinned) place(p);

  const body = [...prepared].sort((a, b) => compareBytes(a.name, b.name));
  const bodyRecords = body.map(cdRecord);

  let index: Prepared | null = null;
  if (input.pageSize !== null) {
    const pages: Page[] = [];
    let cur: Page | null = null;
    let cdOff = 0n;
    body.forEach((p, i) => {
      const len = BigInt(bodyRecords[i].length);
      if (cur === null || (cur.length > 0n && cur.length + len > BigInt(input.pageSize!))) {
        cur = { firstKey: p.key, offset: cdOff, length: 0n };
        pages.push(cur);
      }
      cur.length += len;
      cdOff += len;
    });
    const pinned: Pinned[] = body
      .filter((p) => p.pinned)
      .map((p) => ({
        key: p.key,
        dataOffset: p.lho! + 30n + BigInt(p.name.length),
        size: BigInt(p.usize),
        csize: BigInt(p.body.length),
        method: p.method,
      }));
    index = formatEntry(INDEX_KEY, encodeCdIndex({ pages, pinned }));
    place(index);
  }

  const cdOffset = off;
  for (const r of bodyRecords) emit(r);
  emit(cdRecord(sources));
  if (index) emit(cdRecord(index));
  const cdSize = off - cdOffset;
  const count = BigInt(body.length + 1 + (index ? 1 : 0));

  const needZ64 = count >= 0xffffn || cdSize >= 0xffffffffn || cdOffset >= 0xffffffffn;
  if (needZ64) {
    const z64Off = off;
    emit(Buffer.concat([
      le32(0x06064b50), le64(44n), le16(45), le16(45), le32(0), le32(0), le64(count), le64(count), le64(cdSize), le64(cdOffset),
    ]));
    emit(Buffer.concat([le32(0x07064b50), le32(0), le64(z64Off), le32(1)]));
  }
  const comment: Buffer[] = [Buffer.from("vzip/0", "latin1"), le64(sourcesBodyOffset), le64(BigInt(sources.body.length))];
  if (index) comment.push(le64(index.lho! + 30n + BigInt(index.name.length)), le64(BigInt(index.body.length)));
  const c = Buffer.concat(comment);
  const clamp16 = (v: bigint) => (v >= 0xffffn ? 0xffff : Number(v));
  const clamp32 = (v: bigint) => (v >= 0xffffffffn ? 0xffffffff : Number(v));
  emit(Buffer.concat([
    le32(0x06054b50), le16(0), le16(0), le16(clamp16(count)), le16(clamp16(count)), le32(clamp32(cdSize)), le32(clamp32(cdOffset)),
    le16(c.length), c,
  ]));
  return out;
}

/** Writes an archive to `outPath`. Nothing is created if validation fails. */
export function writeArchive(input: WriterInput, outPath: string): void {
  const chunks = build(input);
  const fd = fs.openSync(outPath, "wx");
  try {
    for (const ch of chunks) {
      let done = 0;
      while (done < ch.length) done += fs.writeSync(fd, ch, done, ch.length - done);
    }
  } catch (e) {
    fs.closeSync(fd);
    try { fs.unlinkSync(outPath); } catch { /* ignore */ }
    throw e;
  }
  fs.closeSync(fd);
}
