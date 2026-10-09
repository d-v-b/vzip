// vzip writer (SPEC.md §3, §4, §6, §9), without a page index.

import { crc32, deflateRaw } from "./deflate.ts";
import {
  encodeConcat,
  encodeRange,
  encodeSourceTable,
  type Range,
  type Source,
} from "./protobuf.ts";
import { isUriReference } from "./uri.ts";

export class InvalidInputError extends Error {}

export type EntryDesc =
  | { key: string; bytes: Uint8Array; compress?: boolean }
  | { key: string; ranges: Range[] };

export interface ArchiveDesc {
  sources: Source[];
  entries: EntryDesc[];
  /** Whether a reference entry's body repeats its payload (default true). */
  mirror?: boolean;
}

/** The revision of SPEC.md the writer follows, recorded in every archive (§1.3, §6). */
export const SPEC_REVISION = 10;
export const SOURCES_KEY = "__vz__/sources";
export const INDEX_KEY = "__vz__/index";
export const RANGE_ID = 0x7a76;
export const CONCAT_ID = 0x7a77;
const U16_ALL = 0xffff;
const U32_ALL = 0xffffffff;
const U64_MAX = (1n << 64n) - 1n;
const MAX_PAYLOAD = 65519;
const STRONG_ETAG = /^"[\x21\x23-\x7e]*"$/;

const utf8 = new TextEncoder();

const reject = (message: string): never => {
  throw new InvalidInputError(message);
};

interface Built {
  name: Uint8Array;
  method: number;
  crc: number;
  size: number;
  body: Uint8Array;
  extra: Uint8Array; // reference block, or empty
  offset: number; // of the local header
}

/** The canonical extra block (header ID and payload) of a reference. */
export function referenceBlock(ranges: Range[]): {
  id: number;
  payload: Uint8Array;
} {
  return ranges.length === 1
    ? { id: RANGE_ID, payload: encodeRange(ranges[0]) }
    : { id: CONCAT_ID, payload: encodeConcat(ranges) };
}

function validate(desc: ArchiveDesc) {
  const n = desc.sources.length;
  const byKey = new Map<string, EntryDesc>();
  for (const e of desc.entries) {
    if (e.key === "") reject("empty key");
    if (!e.key.isWellFormed()) reject(`key ${JSON.stringify(e.key)} is not valid Unicode`);
    if (utf8.encode(e.key).length > U16_ALL) reject(`key longer than 65535 bytes`);
    if (e.key === SOURCES_KEY || e.key === INDEX_KEY) reject(`${e.key} is a format entry`);
    if (byKey.has(e.key)) reject(`duplicate key ${JSON.stringify(e.key)}`);
    byKey.set(e.key, e);
  }
  for (const [i, s] of desc.sources.entries()) {
    const kinds = [s.url, s.key, s.data].filter((k) => k !== undefined);
    if (kinds.length !== 1) reject(`source ${i} must have exactly one of url, key, data`);
    const pinned =
      s.size !== undefined || s.etag !== undefined || s.modifiedNotAfter !== undefined;
    if (pinned && s.url === undefined) reject(`source ${i}: pin on a non-url source`);
    if (s.etag !== undefined && !STRONG_ETAG.test(s.etag)) {
      reject(`source ${i}: etag ${s.etag} is not a strong entity tag`);
    }
    if (s.url !== undefined && (s.url === "" || !isUriReference(s.url))) {
      reject(`source ${i}: url ${JSON.stringify(s.url)} is not a URI reference`);
    }
    if (s.key !== undefined) {
      const target = byKey.get(s.key);
      if (s.key === "") reject(`source ${i}: empty key`);
      if (target === undefined || !("bytes" in target)) {
        reject(`source ${i}: key ${JSON.stringify(s.key)} is not a bytes entry`);
      }
    }
  }
  const sourceSize = (i: number): bigint | undefined => {
    const s = desc.sources[i];
    if (s.data !== undefined) return BigInt(s.data.length);
    if (s.key !== undefined) return BigInt((byKey.get(s.key) as { bytes: Uint8Array }).bytes.length);
    return undefined; // url sources are not checked
  };
  for (const e of desc.entries) {
    if ("bytes" in e) {
      if (e.bytes.length >= U32_ALL) reject(`${e.key}: too large`);
      continue;
    }
    let total = 0n;
    for (const r of e.ranges) {
      if ("data" in r) {
        if ((r as { crc32c?: number }).crc32c !== undefined) {
          reject(`${e.key}: a literal range with a crc32c`);
        }
        total += BigInt(r.data.length);
        continue;
      }
      if (r.crc32c !== undefined && !(Number.isInteger(r.crc32c) && r.crc32c >= 0 && r.crc32c <= U32_ALL)) {
        reject(`${e.key}: crc32c ${r.crc32c} is not a uint32`);
      }
      if (!Number.isInteger(r.source) || r.source < 0 || r.source >= n) {
        reject(`${e.key}: source ${r.source} >= ${n} sources`);
      }
      if (r.offset < 0n || r.length < 0n || r.offset + r.length > U64_MAX) {
        reject(`${e.key}: range exceeds 2^64 - 1`);
      }
      const size = sourceSize(r.source);
      if (size !== undefined && r.offset + r.length > size) {
        reject(`${e.key}: range [${r.offset}, ${r.offset + r.length}) past the end of source ${r.source}`);
      }
      total += r.length;
    }
    if (total > U64_MAX) reject(`${e.key}: value size exceeds 2^64 - 1`);
    if (referenceBlock(e.ranges).payload.length > MAX_PAYLOAD) {
      reject(`${e.key}: reference payload exceeds ${MAX_PAYLOAD} bytes`);
    }
  }
}

class Out {
  chunks: Uint8Array[] = [];
  length = 0;
  push(b: Uint8Array) {
    this.chunks.push(b);
    this.length += b.length;
  }
  concat(): Uint8Array {
    const out = new Uint8Array(this.length);
    let at = 0;
    for (const c of this.chunks) {
      out.set(c, at);
      at += c.length;
    }
    return out;
  }
}

function le(fields: [number, 2 | 4 | 8][]): Uint8Array {
  const size = fields.reduce((n, [, w]) => n + w, 0);
  const out = new Uint8Array(size);
  const view = new DataView(out.buffer);
  let at = 0;
  for (const [v, w] of fields) {
    if (w === 2) view.setUint16(at, v, true);
    else if (w === 4) view.setUint32(at, v, true);
    else view.setBigUint64(at, BigInt(v), true);
    at += w;
  }
  return out;
}

/** Writes an archive, or throws InvalidInputError (spec §9.1). */
export async function writeVzip(desc: ArchiveDesc): Promise<Uint8Array> {
  validate(desc);
  const mirror = desc.mirror ?? true;
  const built: Built[] = [];
  for (const e of desc.entries) {
    if ("bytes" in e) {
      const body = e.compress ? await deflateRaw(e.bytes) : e.bytes;
      if (body.length >= U32_ALL) reject(`${e.key}: compressed body too large`);
      built.push({
        name: utf8.encode(e.key),
        method: e.compress ? 8 : 0,
        crc: crc32(e.bytes),
        size: e.bytes.length,
        body,
        extra: new Uint8Array(),
        offset: 0,
      });
    } else {
      const { id, payload } = referenceBlock(e.ranges);
      const body = mirror ? payload : new Uint8Array();
      const extra = new Uint8Array(4 + payload.length);
      extra.set(le([[id, 2], [payload.length, 2]]));
      extra.set(payload, 4);
      built.push({
        name: utf8.encode(e.key),
        method: 0,
        crc: crc32(body),
        size: body.length,
        body,
        extra,
        offset: 0,
      });
    }
  }
  const table = encodeSourceTable(desc.sources, SPEC_REVISION);
  const sources: Built = {
    name: utf8.encode(SOURCES_KEY),
    method: 8,
    crc: crc32(table),
    size: table.length,
    body: await deflateRaw(table),
    extra: new Uint8Array(),
    offset: 0,
  };
  built.push(sources);

  const out = new Out();
  for (const b of built) {
    b.offset = out.length;
    out.push(
      le([
        [0x04034b50, 4], [20, 2], [0x0800, 2], [b.method, 2], [0, 2], [0x21, 2],
        [b.crc, 4], [b.body.length, 4], [b.size, 4], [b.name.length, 2], [0, 2],
      ]),
    );
    out.push(b.name);
    out.push(b.body);
  }
  const cdOffset = out.length;
  for (const b of built) {
    const zip64 = b.offset >= U32_ALL;
    const extra = zip64
      ? new Uint8Array([...b.extra, ...le([[1, 2], [8, 2], [b.offset, 8]])])
      : b.extra;
    out.push(
      le([
        [0x02014b50, 4], [20, 2], [zip64 ? 45 : 20, 2], [0x0800, 2], [b.method, 2],
        [0, 2], [0x21, 2], [b.crc, 4], [b.body.length, 4], [b.size, 4],
        [b.name.length, 2], [extra.length, 2], [0, 2], [0, 2], [0, 2], [0, 4],
        [zip64 ? U32_ALL : b.offset, 4],
      ]),
    );
    out.push(b.name);
    out.push(extra);
  }
  const cdSize = out.length - cdOffset;
  const count = built.length;
  if (count >= U16_ALL || cdSize >= U32_ALL || cdOffset >= U32_ALL) {
    const eocd64 = out.length;
    out.push(
      le([
        [0x06064b50, 4], [44, 8], [20, 2], [45, 2], [0, 4], [0, 4],
        [count, 8], [count, 8], [cdSize, 8], [cdOffset, 8],
      ]),
    );
    out.push(le([[0x07064b50, 4], [0, 4], [eocd64, 8], [1, 4]]));
  }
  const comment = new Uint8Array(22);
  comment.set(utf8.encode("vzip/0"));
  const view = new DataView(comment.buffer);
  view.setBigUint64(6, BigInt(sources.offset + 30 + sources.name.length), true);
  view.setBigUint64(14, BigInt(sources.body.length), true);
  out.push(
    le([
      [0x06054b50, 4], [0, 2], [0, 2],
      [Math.min(count, U16_ALL), 2], [Math.min(count, U16_ALL), 2],
      [Math.min(cdSize, U32_ALL), 4], [Math.min(cdOffset, U32_ALL), 4],
      [comment.length, 2],
    ]),
  );
  out.push(comment);
  return out.concat();
}
