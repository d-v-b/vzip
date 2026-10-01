// Reading a vzip archive held in memory (SPEC.md §3, §4, §8).

import { inflateRaw } from "./deflate.ts";
import { HttpResolutionError, readHttpRange } from "./http.ts";
import {
  decodeReference,
  decodeSourceTable,
  MalformedError,
  rangeSize,
  type Range,
  type Source,
} from "./protobuf.ts";
import { getScheme, resolveReference } from "./uri.ts";
import { CONCAT_ID, RANGE_ID, SOURCES_KEY } from "./writer.ts";

export type ErrorClass =
  | "archive"
  | "entry"
  | "body"
  | "payload"
  | "resolution"
  | "request";

export class VzipError extends Error {
  errorClass: ErrorClass;
  constructor(errorClass: ErrorClass, message: string) {
    super(`vzip ${errorClass} error: ${message}`);
    this.errorClass = errorClass;
  }
}

export interface Entry {
  key: string;
  method: number;
  csize: number;
  size: number;
  bodyOffset: number;
  reference?: { headerId: number; payload: Uint8Array };
  /** Set if the record has an entry error (spec §4.1, §3.2). */
  error?: string;
}

/** Reads bytes `[start, end)` of an http(s) object (replaceable in tests). */
export type RangeFetcher = (
  url: string,
  start: number,
  end: number,
  pins: Pick<Source, "size" | "etag" | "modifiedNotAfter">,
) => Promise<{ data: Uint8Array; size: number | undefined }>;

const U16_ALL = 0xffff;
const U32_ALL = 0xffffffff;
const strictUtf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

function safe(v: bigint, what: string): number {
  if (v > BigInt(Number.MAX_SAFE_INTEGER)) {
    throw new VzipError("archive", `${what} is too large`);
  }
  return Number(v);
}

function parseExtra(extra: Uint8Array, view: DataView, base: number) {
  const blocks: { id: number; data: Uint8Array }[] = [];
  let at = 0;
  while (at < extra.length) {
    if (at + 4 > extra.length) return undefined;
    const id = view.getUint16(base + at, true);
    const n = view.getUint16(base + at + 2, true);
    if (at + 4 + n > extra.length) return undefined;
    blocks.push({ id, data: extra.subarray(at + 4, at + 4 + n) });
    at += 4 + n;
  }
  return blocks;
}

export class Archive {
  bytes: Uint8Array;
  baseUrl: string;
  sources: Source[];
  entries: Map<string, Entry>;
  private fetchRange: RangeFetcher;

  private constructor(
    bytes: Uint8Array,
    baseUrl: string,
    sources: Source[],
    entries: Map<string, Entry>,
    fetchRange: RangeFetcher,
  ) {
    this.bytes = bytes;
    this.baseUrl = baseUrl;
    this.sources = sources;
    this.entries = entries;
    this.fetchRange = fetchRange;
  }

  /** Opens an archive (spec §8.1); `baseUrl` resolves relative `url` sources. */
  static async open(
    bytes: Uint8Array,
    baseUrl: string,
    fetchRange: RangeFetcher = (url, start, end, pins) =>
      readHttpRange(url, start, end, pins),
  ): Promise<Archive> {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const n = bytes.length;
    let eocd = -1;
    for (const commentLength of [38, 22]) {
      const at = n - 22 - commentLength;
      if (
        at >= 0 &&
        view.getUint32(at, true) === 0x06054b50 &&
        view.getUint16(at + 20, true) === commentLength
      ) {
        eocd = at;
        break;
      }
    }
    if (eocd < 0) throw new VzipError("archive", "not a vzip archive");
    const comment = bytes.subarray(eocd + 22);
    const magic = new TextDecoder().decode(comment.subarray(0, 6));
    if (!magic.startsWith("vzip/")) {
      throw new VzipError("archive", "not a vzip archive");
    }
    if (magic !== "vzip/0") {
      throw new VzipError("archive", `unsupported version ${magic}`);
    }
    let count = BigInt(view.getUint16(eocd + 10, true));
    let cdSize = BigInt(view.getUint32(eocd + 12, true));
    let cdOffset = BigInt(view.getUint32(eocd + 16, true));
    if (
      view.getUint16(eocd + 8, true) === U16_ALL ||
      count === BigInt(U16_ALL) ||
      cdSize === BigInt(U32_ALL) ||
      cdOffset === BigInt(U32_ALL)
    ) {
      const loc = eocd - 20;
      if (loc < 0 || view.getUint32(loc, true) !== 0x07064b50) {
        throw new VzipError("archive", "missing zip64 locator");
      }
      const at = safe(view.getBigUint64(loc + 8, true), "zip64 record offset");
      if (
        at + 56 > n ||
        view.getUint32(at, true) !== 0x06064b50 ||
        view.getBigUint64(at + 4, true) !== 44n
      ) {
        throw new VzipError("archive", "invalid zip64 end of central directory");
      }
      count = view.getBigUint64(at + 32, true);
      cdSize = view.getBigUint64(at + 40, true);
      cdOffset = view.getBigUint64(at + 48, true);
    }
    void count; // spec §3.2: entry counts are not used beyond this point
    const cdStart = safe(cdOffset, "central directory offset");
    const cdEnd = cdStart + safe(cdSize, "central directory size");
    if (cdEnd > n) throw new VzipError("archive", "central directory outside the file");

    const entries = new Map<string, Entry>();
    let at = cdStart;
    while (at < cdEnd) {
      if (at + 46 > cdEnd || view.getUint32(at, true) !== 0x02014b50) {
        throw new VzipError("archive", `bad central directory record at ${at}`);
      }
      const flags = view.getUint16(at + 8, true);
      const method = view.getUint16(at + 10, true);
      const csize = view.getUint32(at + 20, true);
      const size = view.getUint32(at + 24, true);
      const nameLen = view.getUint16(at + 28, true);
      const extraLen = view.getUint16(at + 30, true);
      const commentLen = view.getUint16(at + 32, true);
      let offset: number | bigint = view.getUint32(at + 42, true);
      const end = at + 46 + nameLen + extraLen + commentLen;
      if (end > cdEnd) throw new VzipError("archive", "truncated central directory");
      const nameBytes = bytes.subarray(at + 46, at + 46 + nameLen);
      const extraStart = at + 46 + nameLen;
      const extra = bytes.subarray(extraStart, extraStart + extraLen);
      at = end;
      let key: string;
      try {
        key = strictUtf8.decode(nameBytes);
      } catch {
        continue; // names no key (spec §3.3)
      }
      if (key === "") continue;
      if (entries.has(key)) throw new VzipError("archive", `duplicate key ${key}`);
      const entry: Entry = { key, method, csize, size, bodyOffset: 0 };
      entries.set(key, entry);
      const blocks = parseExtra(extra, view, extraStart);
      const refs = blocks?.filter((b) => b.id === RANGE_ID || b.id === CONCAT_ID) ?? [];
      const zip64 = blocks?.filter((b) => b.id === 0x0001) ?? [];
      if (blocks === undefined) entry.error = "unparseable extra field";
      else if (refs.length > 1) entry.error = "more than one reference block";
      else if (zip64.length > 1) entry.error = "more than one zip64 block";
      else if (csize === U32_ALL || size === U32_ALL) entry.error = "size field is 0xFFFFFFFF";
      else if (offset === U32_ALL) {
        if (zip64.length === 0 || zip64[0].data.length < 8) {
          entry.error = "offset needs a zip64 block";
        } else {
          offset = new DataView(
            zip64[0].data.buffer,
            zip64[0].data.byteOffset,
          ).getBigUint64(0, true);
        }
      }
      if (entry.error === undefined && method !== 0 && method !== 8) {
        entry.error = `compression method ${method}`;
      } else if (entry.error === undefined && flags & 1) {
        entry.error = "encrypted";
      }
      if (entry.error === undefined && refs.length === 1) {
        if (method !== 0) entry.error = "reference entry with method 8";
        entry.reference = { headerId: refs[0].id, payload: refs[0].data };
      }
      entry.bodyOffset =
        typeof offset === "bigint"
          ? Number(offset) + 30 + nameLen
          : offset + 30 + nameLen;
    }

    const cview = new DataView(comment.buffer, comment.byteOffset, comment.byteLength);
    const sOffset = safe(cview.getBigUint64(6, true), "sources offset");
    const sSize = safe(cview.getBigUint64(14, true), "sources size");
    if (sOffset + sSize > n) throw new VzipError("archive", "source table outside the file");
    let sources: Source[];
    try {
      sources = decodeSourceTable(
        await inflateRaw(bytes.subarray(sOffset, sOffset + sSize)),
      );
    } catch (e) {
      throw new VzipError("archive", `source table: ${(e as Error).message}`);
    }
    return new Archive(bytes, baseUrl, sources, entries, fetchRange);
  }

  /** The visible entry for `key`, or undefined (spec §8.2). */
  lookup(key: string): Entry | undefined {
    if (key.startsWith("__vz__/")) return undefined;
    const entry = this.entries.get(key);
    if (entry?.error !== undefined) {
      throw new VzipError("entry", `${JSON.stringify(key)}: ${entry.error}`);
    }
    return entry;
  }

  /** Visible keys, in UTF-8 order. */
  keys(): string[] {
    return [...this.entries.keys()]
      .filter((k) => !k.startsWith("__vz__/"))
      .sort((a, b) => compareUtf8(a, b));
  }

  ranges(entry: Entry): Range[] {
    try {
      return decodeReference(
        entry.reference!.headerId,
        entry.reference!.payload,
        this.sources.length,
      );
    } catch (e) {
      if (!(e instanceof MalformedError)) throw e;
      throw new VzipError("payload", `${JSON.stringify(entry.key)}: ${e.message}`);
    }
  }

  /** Size of an entry's value. */
  size(entry: Entry): number {
    if (entry.reference === undefined) return entry.size;
    const total = this.ranges(entry).reduce((n, r) => n + rangeSize(r), 0n);
    if (total > BigInt(Number.MAX_SAFE_INTEGER)) {
      throw new VzipError("request", "value is too large for this reader");
    }
    return Number(total);
  }

  private async body(entry: Entry): Promise<Uint8Array> {
    const end = entry.bodyOffset + entry.csize;
    if (end > this.bytes.length) {
      throw new VzipError("body", `${entry.key}: body outside the file`);
    }
    const stored = this.bytes.subarray(entry.bodyOffset, end);
    let body = stored;
    if (entry.method === 8) {
      try {
        body = await inflateRaw(stored);
      } catch (e) {
        throw new VzipError("body", `${entry.key}: ${(e as Error).message}`);
      }
    } else if (entry.csize !== entry.size) {
      throw new VzipError("body", `${entry.key}: STORED sizes differ`);
    }
    if (body.length !== entry.size) {
      throw new VzipError("body", `${entry.key}: inflates to ${body.length} bytes`);
    }
    return body;
  }

  private async source(r: Extract<Range, { source: number }>): Promise<Uint8Array> {
    const s = this.sources[r.source];
    const start = r.offset;
    const end = r.offset + r.length;
    const fail = (m: string): never => {
      throw new VzipError("resolution", `source ${r.source}: ${m}`);
    };
    if (s.data !== undefined) {
      if (end > BigInt(s.data.length)) fail("range past the end of the data");
      return s.data.subarray(Number(start), Number(end));
    }
    if (s.key !== undefined) {
      const target = this.entries.get(s.key);
      if (target === undefined || target.reference !== undefined || target.error) {
        fail(`key ${JSON.stringify(s.key)} is not a bytes entry`);
      }
      const value = await this.body(target!);
      if (end > BigInt(value.length)) fail("range past the end of the key's value");
      return value.subarray(Number(start), Number(end));
    }
    let url: string;
    try {
      url = resolveReference(this.baseUrl, s.url!);
    } catch (e) {
      return fail((e as Error).message);
    }
    const scheme = getScheme(url);
    if (scheme !== "http" && scheme !== "https") fail(`unsupported scheme in ${url}`);
    try {
      const { data, size } = await this.fetchRange(
        url,
        safe(start, "offset"),
        safe(end, "end"),
        s,
      );
      if (s.size !== undefined && BigInt(size ?? -1) !== s.size) {
        fail(`size pin ${s.size} != ${size ?? "unknown"}`);
      }
      return data;
    } catch (e) {
      if (e instanceof HttpResolutionError) fail(e.message);
      throw e;
    }
  }

  /** Bytes `[start, end)` of a visible key's value, or undefined if absent. */
  async read(key: string, start = 0, end?: number): Promise<Uint8Array | undefined> {
    const entry = this.lookup(key);
    if (entry === undefined) return undefined;
    const size = this.size(entry);
    end ??= size;
    if (start < 0 || end < start || end > size) {
      throw new VzipError("request", `range [${start}, ${end}) of a ${size}-byte value`);
    }
    if (entry.reference === undefined) {
      return (await this.body(entry)).subarray(start, end);
    }
    const parts: Promise<Uint8Array>[] = [];
    let at = 0;
    for (const r of this.ranges(entry)) {
      const n = Number(rangeSize(r));
      const a = Math.max(start, at);
      const b = Math.min(end, at + n);
      // Only ranges that overlap the request are resolved.
      if (a < b) {
        const lo = BigInt(a - at);
        const hi = BigInt(b - at);
        parts.push(
          "data" in r
            ? Promise.resolve(r.data.subarray(a - at, b - at))
            : this.source({ source: r.source, offset: r.offset + lo, length: hi - lo }),
        );
      }
      at += n;
    }
    const chunks = await Promise.all(parts);
    const out = new Uint8Array(end - start);
    let o = 0;
    for (const c of chunks) {
      out.set(c, o);
      o += c.length;
    }
    return out;
  }
}

export function compareUtf8(a: string, b: string): number {
  // Code point order equals UTF-8 byte order.
  const x = [...a];
  const y = [...b];
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    const d = x[i].codePointAt(0)! - y[i].codePointAt(0)!;
    if (d !== 0) return d;
  }
  return x.length - y.length;
}

export { SOURCES_KEY };
