/**
 * @license
 * Copyright 2026 Google Inc.
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * @file Opening a vzip archive (format version 0): the end records and
 * comment (spec §3.2, §3.4), the source table (§6), the page index (§7),
 * and central directory records with their entry errors (§4.1, §8.4).
 */

import type {
  VzipPage,
  VzipPinned,
  VzipSource,
} from "#src/kvstore/vzip/proto.js";
import {
  decodeCdIndex,
  decodeSourceTable,
  VzipMalformedError,
} from "#src/kvstore/vzip/proto.js";
import { decodeGzip } from "#src/util/gzip.js";
import type { ProgressOptions } from "#src/util/progress_listener.js";

export const FORMAT_VERSION = 0;
export const SOURCES_KEY = "__vz__/sources";
export const INDEX_KEY = "__vz__/index";
export const RESERVED_PREFIX = "__vz__/";

const RANGE_EXTRA_ID = 0x7a76;
const CONCAT_EXTRA_ID = 0x7a77;
const ZIP64_EXTRA_ID = 0x0001;
const U16 = 0xffff;
const U32 = 0xffffffff;
const LOCAL_HEADER_SIZE = 30;
const CD_HEADER_SIZE = 46;

/** Error classes of spec §8.4. The class is part of the message. */
export class VzipError extends Error {
  constructor(
    public errorClass:
      | "archive"
      | "entry"
      | "body"
      | "payload"
      | "resolution"
      | "request",
    message: string,
  ) {
    super(`vzip ${errorClass} error: ${message}`);
  }
}

export type Reader = (
  offset: number,
  length: number,
  options: Partial<ProgressOptions>,
) => Promise<Uint8Array<ArrayBuffer>>;

export interface VzipEntry {
  name: string;
  size: number;
  csize: number;
  method: number;
  bodyOffset: number;
  // (extra field header ID, payload) of a reference entry.
  reference?: { headerId: number; payload: Uint8Array };
  // Set for an entry error (spec §8.4).
  error?: string;
}

export interface VzipMetadata {
  fileSize: number;
  sources: VzipSource[];
  // Entries known without loading any page: every entry of an unpaged
  // archive, or the format and pinned entries of a paged one.
  entries: Map<string, VzipEntry>;
  // Present for an archive with a page index.
  paged?: {
    cdOffset: number;
    pages: VzipPage[];
    firstKeys: Uint8Array[];
  };
  // Approximate memory use, for the cache.
  sizeEstimate: number;
}

const utf8Encoder = new TextEncoder();
const strictUtf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

/** Compares byte strings lexicographically ("UTF-8 order", spec §1.1). */
export function compareBytes(a: Uint8Array, b: Uint8Array): number {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; ++i) {
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return a.length - b.length;
}

export function utf8(s: string): Uint8Array {
  return utf8Encoder.encode(s);
}

function startsWith(a: Uint8Array, prefix: Uint8Array): boolean {
  if (prefix.length > a.length) return false;
  for (let i = 0; i < prefix.length; ++i) if (a[i] !== prefix[i]) return false;
  return true;
}

async function inflateClean(
  raw: Uint8Array<ArrayBuffer>,
  signal?: AbortSignal,
): Promise<Uint8Array<ArrayBuffer>> {
  // DecompressionStream rejects truncated streams and trailing bytes, which
  // is exactly "inflates cleanly" (spec §8.1).
  return new Uint8Array(await decodeGzip(raw, "deflate-raw", signal));
}

export function inflateEntry(
  raw: Uint8Array<ArrayBuffer>,
  signal?: AbortSignal,
) {
  return inflateClean(raw, signal);
}

function u64(view: DataView, offset: number): number {
  const v = view.getBigUint64(offset, true);
  if (v > BigInt(Number.MAX_SAFE_INTEGER)) {
    throw new VzipError(
      "archive",
      "a 64-bit field is too large for this reader",
    );
  }
  return Number(v);
}

function view(bytes: Uint8Array) {
  return new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
}

/**
 * Parses central directory records (spec §4.1). Problems confined to one
 * record become that entry's `error`; structural problems throw.
 */
export function parseCentralDirectory(cd: Uint8Array): VzipEntry[] {
  const entries: VzipEntry[] = [];
  const dv = view(cd);
  let pos = 0;
  while (pos < cd.length) {
    if (pos + CD_HEADER_SIZE > cd.length) {
      throw new Error("truncated central directory record");
    }
    if (dv.getUint32(pos, true) !== 0x02014b50) {
      throw new Error(`bad central directory record signature at ${pos}`);
    }
    const flags = dv.getUint16(pos + 8, true);
    const method = dv.getUint16(pos + 10, true);
    const csize = dv.getUint32(pos + 20, true);
    const size = dv.getUint32(pos + 24, true);
    const nameLength = dv.getUint16(pos + 28, true);
    const extraLength = dv.getUint16(pos + 30, true);
    const commentLength = dv.getUint16(pos + 32, true);
    let offset = dv.getUint32(pos + 42, true);
    const end = pos + CD_HEADER_SIZE + nameLength + extraLength + commentLength;
    if (end > cd.length) {
      throw new Error("central directory record runs past the directory");
    }
    const nameBytes = cd.subarray(
      pos + CD_HEADER_SIZE,
      pos + CD_HEADER_SIZE + nameLength,
    );
    const extra = cd.subarray(
      pos + CD_HEADER_SIZE + nameLength,
      pos + CD_HEADER_SIZE + nameLength + extraLength,
    );
    pos = end;
    let name: string;
    try {
      name = strictUtf8.decode(nameBytes);
    } catch {
      continue; // spec §3.3: such records name no key
    }
    if (name === "") continue;

    let error: string | undefined;
    let reference: VzipEntry["reference"];
    let referenceBlocks = 0;
    const zip64Blocks: Uint8Array[] = [];
    for (let p = 0; p < extra.length; ) {
      if (p + 4 > extra.length) {
        error = "unparseable extra field";
        break;
      }
      const ev = view(extra);
      const id = ev.getUint16(p, true);
      const n = ev.getUint16(p + 2, true);
      if (p + 4 + n > extra.length) {
        error = "unparseable extra field";
        break;
      }
      const data = extra.subarray(p + 4, p + 4 + n);
      if (id === RANGE_EXTRA_ID || id === CONCAT_EXTRA_ID) {
        ++referenceBlocks;
        reference = { headerId: id, payload: data.slice() };
      } else if (id === ZIP64_EXTRA_ID) {
        zip64Blocks.push(data);
      }
      p += 4 + n;
    }
    if (error === undefined) {
      if (referenceBlocks > 1) error = "more than one reference extra block";
      else if (zip64Blocks.length > 1)
        error = "more than one ZIP64 extra block";
      else if (size === U32 || csize === U32)
        error = "a size field is 0xFFFFFFFF";
      else if (offset === U32) {
        if (zip64Blocks.length === 0 || zip64Blocks[0].length < 8) {
          error = "ZIP64 extra block missing or too short";
        } else {
          const v = view(zip64Blocks[0]).getBigUint64(0, true);
          if (v > BigInt(Number.MAX_SAFE_INTEGER)) {
            error = "local header offset too large for this reader";
          } else offset = Number(v);
        }
      }
    }
    if (error === undefined) {
      if (method !== 0 && method !== 8) error = `unsupported method ${method}`;
      else if (flags & 1) error = "encrypted entry";
      else if (reference !== undefined && method !== 0) {
        error = "reference entry is not STORED";
      }
    }
    entries.push({
      name,
      size,
      csize,
      method,
      bodyOffset: offset + LOCAL_HEADER_SIZE + nameLength,
      reference,
      error,
    });
  }
  return entries;
}

function checkPageIndex(
  pages: VzipPage[],
  pinned: VzipPinned[],
  cdSize: number,
  fileSize: number,
) {
  let pos = 0;
  let previous: Uint8Array | undefined;
  for (const page of pages) {
    if (page.firstKey === "") throw new Error("a page has an empty first_key");
    if (
      page.length === 0 ||
      page.offset !== pos ||
      page.offset + page.length > cdSize
    ) {
      throw new Error(
        "pages are not contiguous, empty, or outside the directory",
      );
    }
    const key = utf8(page.firstKey);
    if (previous !== undefined && compareBytes(key, previous) <= 0) {
      throw new Error("page first_key values do not strictly increase");
    }
    previous = key;
    pos = page.offset + page.length;
  }
  const seen = new Set<string>();
  for (const p of pinned) {
    if (p.key === "") throw new Error("a pinned key is empty");
    if (seen.has(p.key) || p.key === SOURCES_KEY || p.key === INDEX_KEY) {
      throw new Error(
        `pinned key ${JSON.stringify(p.key)} is duplicated or a format entry`,
      );
    }
    if (p.method !== 0 && p.method !== 8) {
      throw new Error(`pinned ${JSON.stringify(p.key)} has method ${p.method}`);
    }
    if (p.dataOffset + p.csize > fileSize) {
      throw new Error(`pinned ${JSON.stringify(p.key)} lies outside the file`);
    }
    seen.add(p.key);
  }
}

/** Opens an archive (spec §8.1). Every failure is an archive error. */
export async function readVzipMetadata(
  read: Reader,
  fileSize: number,
  options: Partial<ProgressOptions>,
): Promise<VzipMetadata> {
  try {
    return await readVzipMetadataImpl(read, fileSize, options);
  } catch (e) {
    if (e instanceof VzipError) throw e;
    options.signal?.throwIfAborted();
    throw new VzipError("archive", (e as Error).message);
  }
}

async function readVzipMetadataImpl(
  read: Reader,
  fileSize: number,
  options: Partial<ProgressOptions>,
): Promise<VzipMetadata> {
  // The end of central directory record is 44 or 60 bytes from the end, and
  // a zip64 locator, if any, is the 20 bytes before it (spec §3.4).
  const tailLength = Math.min(fileSize, 80);
  const tail = await read(fileSize - tailLength, tailLength, options);
  const tv = view(tail);
  let eocd = -1;
  for (const commentLength of [38, 22]) {
    const i = tail.length - 22 - commentLength;
    if (i >= 0 && tv.getUint32(i, true) === 0x06054b50) {
      if (tv.getUint16(i + 20, true) === commentLength) {
        eocd = i;
        break;
      }
    }
  }
  if (eocd < 0) {
    throw new Error(
      "not a vzip archive (no end of central directory record with a 22- or 38-byte comment)",
    );
  }
  const comment = tail.subarray(eocd + 22);
  const magic = new TextDecoder().decode(comment.subarray(0, 6));
  if (!magic.startsWith("vzip/")) {
    throw new Error(
      `not a vzip archive (comment starts ${JSON.stringify(magic)})`,
    );
  }
  if (magic !== `vzip/${FORMAT_VERSION}`) {
    throw new Error(
      `unsupported vzip format version ${JSON.stringify(magic.slice(5))}; ` +
        `this reader implements version ${FORMAT_VERSION}`,
    );
  }
  const nDisk = tv.getUint16(eocd + 8, true);
  const n = tv.getUint16(eocd + 10, true);
  let cdSize = tv.getUint32(eocd + 12, true);
  let cdOffset = tv.getUint32(eocd + 16, true);
  if (nDisk === U16 || n === U16 || cdSize === U32 || cdOffset === U32) {
    const loc = eocd - 20;
    if (loc < 0 || tv.getUint32(loc, true) !== 0x07064b50) {
      throw new Error(
        "end of central directory needs zip64 records, which are missing",
      );
    }
    const eocd64Offset = u64(tv, loc + 8);
    if (eocd64Offset + 56 > fileSize) {
      throw new Error(
        "zip64 end of central directory record lies outside the file",
      );
    }
    const rec = view(await read(eocd64Offset, 56, options));
    if (rec.getUint32(0, true) !== 0x06064b50 || u64(rec, 4) !== 44) {
      throw new Error("bad zip64 end of central directory record");
    }
    cdSize = u64(rec, 40);
    cdOffset = u64(rec, 48);
  }
  if (cdOffset + cdSize > fileSize) {
    throw new Error("central directory lies outside the file");
  }
  const cv = view(comment);
  const sourcesOffset = u64(cv, 6);
  const sourcesSize = u64(cv, 14);
  const paged = comment.length === 38;
  const indexOffset = paged ? u64(cv, 22) : 0;
  const indexSize = paged ? u64(cv, 30) : 0;
  for (const [offset, size] of [
    [sourcesOffset, sourcesSize],
    [indexOffset, indexSize],
  ]) {
    if (offset + size > fileSize) {
      throw new Error("a format entry's body lies outside the file");
    }
  }

  const sourcesBody = await inflateClean(
    await read(sourcesOffset, sourcesSize, options),
    options.signal,
  );
  const sources = decodeSourceTable(sourcesBody);
  const entries = new Map<string, VzipEntry>();
  entries.set(SOURCES_KEY, {
    name: SOURCES_KEY,
    size: sourcesBody.length,
    csize: sourcesSize,
    method: 8,
    bodyOffset: sourcesOffset,
  });
  let sizeEstimate = sourcesBody.length + 1024;

  if (!paged) {
    const cd = await read(cdOffset, cdSize, options);
    for (const entry of parseCentralDirectory(cd)) {
      if (!entries.has(entry.name) || entry.name === SOURCES_KEY) {
        if (entry.name === SOURCES_KEY) continue; // known from the comment
        entries.set(entry.name, entry);
      }
    }
    if (entries.has(INDEX_KEY)) {
      throw new Error("__vz__/index entry in an archive without a page index");
    }
    sizeEstimate += cd.length * 2;
    return { fileSize, sources, entries, sizeEstimate };
  }

  const indexBody = await inflateClean(
    await read(indexOffset, indexSize, options),
    options.signal,
  );
  const { pages, pinned } = decodeCdIndex(indexBody);
  checkPageIndex(pages, pinned, cdSize, fileSize);
  entries.set(INDEX_KEY, {
    name: INDEX_KEY,
    size: indexBody.length,
    csize: indexSize,
    method: 8,
    bodyOffset: indexOffset,
  });
  for (const p of pinned) {
    entries.set(p.key, {
      name: p.key,
      size: p.size,
      csize: p.csize,
      method: p.method,
      bodyOffset: p.dataOffset,
    });
  }
  sizeEstimate += indexBody.length * 2;
  return {
    fileSize,
    sources,
    entries,
    paged: {
      cdOffset,
      pages,
      firstKeys: pages.map((p) => utf8(p.firstKey)),
    },
    sizeEstimate,
  };
}

/** Index of the page that lookup selects for `key` (spec §7.2), or -1. */
export function pageForKey(firstKeys: Uint8Array[], key: Uint8Array): number {
  let lo = 0;
  let hi = firstKeys.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (compareBytes(firstKeys[mid], key) <= 0) lo = mid + 1;
    else hi = mid;
  }
  return lo - 1;
}

/** Indices of the pages `list(prefix)` reads (spec §8.2). */
export function pagesForPrefix(
  firstKeys: Uint8Array[],
  prefix: Uint8Array,
): number[] {
  const result: number[] = [];
  for (let i = 0; i < firstKeys.length; ++i) {
    const lo = firstKeys[i];
    const hi = firstKeys[i + 1];
    if (
      (hi === undefined || compareBytes(prefix, hi) < 0) &&
      (compareBytes(lo, prefix) <= 0 || startsWith(lo, prefix))
    ) {
      result.push(i);
    }
  }
  return result;
}

export { VzipMalformedError };
