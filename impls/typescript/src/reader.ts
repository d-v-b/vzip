// vzip reader (spec §3, §4, §6, §7, §8).
import * as fs from "node:fs/promises";
import * as zlib from "node:zlib";
import { MalformedError, VzipError } from "./errors.ts";
import {
  type CdIndex,
  type Pinned,
  type Range,
  type Source,
  U64_MAX,
  decodeCdIndex,
  decodeConcat,
  decodeRange,
  decodeSourceTable,
  decodeUtf8,
  encodeUtf8,
  rangeSize,
} from "./proto.ts";
import {
  fileBaseUri,
  fileUriToPath,
  isUriReference,
  parseUri,
  resolveUri,
} from "./uri.ts";
import {
  EXTRA_CONCAT,
  EXTRA_RANGE,
  EXTRA_ZIP64,
  INDEX_KEY,
  MAGIC,
  SIG_CENTRAL,
  SIG_EOCD,
  SIG_ZIP64_EOCD,
  SIG_ZIP64_LOCATOR,
  SOURCES_KEY,
  compareBytes,
  isFormatKey,
  isHidden,
} from "./zipconst.ts";

export type Kind = "bytes" | "reference" | "missing";

export type Request =
  | { type: "whole" }
  | { type: "range"; start: bigint; end: bigint }
  | { type: "offset"; start: bigint }
  | { type: "suffix"; count: bigint };

export interface OpenOptions {
  /**
   * Upper bound on the bytes a single operation may materialise (decompressed
   * body, or assembled reference window). Exceeding it is a request error
   * (§10). Default: 256 MiB, or $VZIP_MAX_REQUEST_BYTES.
   */
  maxRequestBytes?: number;
  /**
   * Optional allow-list of URL prefixes (after resolution) that references may
   * read. Default: undefined = everything allowed (§10).
   */
  allowedUrlPrefixes?: string[];
}

const DEFAULT_LIMIT = Number(process.env.VZIP_MAX_REQUEST_BYTES ?? 256 * 1024 * 1024);

/** A central directory record, as parsed from the bytes (no validation of contents). */
interface CdRecord {
  nameBytes: Buffer;
  name: string | null; // null when empty or invalid UTF-8 (record ignored)
  flags: number;
  method: number;
  csize32: number;
  usize32: number;
  lho32: number;
  extra: Buffer;
}

/** A fully classified entry. */
interface EntryInfo {
  kind: "bytes" | "reference";
  method: number;
  bodyOffset: bigint;
  csize: bigint;
  usize: bigint;
  payloadId?: number;
  payload?: Buffer;
}

type Located =
  | { t: "record"; rec: CdRecord }
  | { t: "pinned"; p: Pinned }
  | { t: "format"; key: string };

const err = (c: VzipError["errorClass"], m: string) => new VzipError(c, m);

function rdU16(b: Buffer, o: number): number {
  return b.readUInt16LE(o);
}
function rdU32(b: Buffer, o: number): number {
  return b.readUInt32LE(o);
}
function rdU64(b: Buffer, o: number): bigint {
  return b.readBigUInt64LE(o);
}

/**
 * Parse a byte region as a sequence of whole central directory records that
 * exactly fills it. Returns null when it cannot be parsed.
 */
function parseRecords(buf: Buffer): CdRecord[] | null {
  const out: CdRecord[] = [];
  let pos = 0;
  while (pos < buf.length) {
    if (pos + 46 > buf.length) return null;
    if (rdU32(buf, pos) !== SIG_CENTRAL) return null;
    const nlen = rdU16(buf, pos + 28);
    const xlen = rdU16(buf, pos + 30);
    const clen = rdU16(buf, pos + 32);
    const end = pos + 46 + nlen + xlen + clen;
    if (end > buf.length) return null;
    const nameBytes = buf.subarray(pos + 46, pos + 46 + nlen);
    const name = nlen === 0 ? null : decodeUtf8(nameBytes);
    out.push({
      nameBytes,
      name: name === "" ? null : name,
      flags: rdU16(buf, pos + 8),
      method: rdU16(buf, pos + 10),
      csize32: rdU32(buf, pos + 20),
      usize32: rdU32(buf, pos + 24),
      lho32: rdU32(buf, pos + 42),
      extra: buf.subarray(pos + 46 + nlen, pos + 46 + nlen + xlen),
    });
    pos = end;
  }
  return out;
}

/** Classify and decode a record's contents (§4.1, §4.3); throws entry errors. */
function entryFromRecord(rec: CdRecord): EntryInfo {
  if (rec.flags & 1) throw err("entry", "entry is encrypted (general purpose bit 0 set)");
  if (rec.method !== 0 && rec.method !== 8) {
    throw err("entry", `unsupported compression method ${rec.method}`);
  }
  const blocks: { id: number; data: Buffer }[] = [];
  let pos = 0;
  const x = rec.extra;
  while (pos < x.length) {
    if (pos + 4 > x.length) throw err("entry", "extra field does not parse");
    const id = rdU16(x, pos);
    const size = rdU16(x, pos + 2);
    if (pos + 4 + size > x.length) throw err("entry", "extra field does not parse");
    blocks.push({ id, data: x.subarray(pos + 4, pos + 4 + size) });
    pos += 4 + size;
  }
  const refs = blocks.filter((b) => b.id === EXTRA_RANGE || b.id === EXTRA_CONCAT);
  if (refs.length > 1) throw err("entry", "several reference extra blocks");
  const kind = refs.length === 1 ? "reference" : "bytes";
  if (kind === "reference" && rec.method !== 0) {
    throw err("entry", "reference entry uses method 8");
  }
  // ZIP64 extended information (APPNOTE 4.5.3): fields present in order for
  // those header values that are all ones.
  let usize = BigInt(rec.usize32);
  let csize = BigInt(rec.csize32);
  let lho = BigInt(rec.lho32);
  if (rec.usize32 === 0xffffffff || rec.csize32 === 0xffffffff || rec.lho32 === 0xffffffff) {
    const z = blocks.find((b) => b.id === EXTRA_ZIP64);
    let zp = 0;
    const take = (what: string): bigint => {
      if (!z || zp + 8 > z.data.length) {
        throw err("entry", `value 0xFFFFFFFF of ${what} without a ZIP64 extra block holding it`);
      }
      const v = rdU64(z.data, zp);
      zp += 8;
      return v;
    };
    if (rec.usize32 === 0xffffffff) usize = take("uncompressed size");
    if (rec.csize32 === 0xffffffff) csize = take("compressed size");
    if (rec.lho32 === 0xffffffff) lho = take("local header offset");
  }
  const info: EntryInfo = {
    kind,
    method: rec.method,
    bodyOffset: lho + 30n + BigInt(rec.nameBytes.length),
    csize,
    usize,
  };
  if (kind === "reference") {
    info.payloadId = refs[0].id;
    info.payload = refs[0].data;
  }
  return info;
}

/** Inflate a raw DEFLATE body cleanly (§8.1). Returns null if not clean. */
function inflateClean(body: Buffer, maxOut: number): Buffer | "too-large" | null {
  try {
    const r = zlib.inflateRawSync(body, { info: true, maxOutputLength: Math.max(1, maxOut) }) as unknown as {
      buffer: Buffer;
      engine: { bytesWritten: number };
    };
    if (r.engine.bytesWritten !== body.length) return null; // trailing bytes
    return r.buffer;
  } catch (e) {
    if ((e as { code?: string }).code === "ERR_BUFFER_TOO_LARGE") return "too-large";
    return null;
  }
}

function bmin(a: bigint, b: bigint): bigint {
  return a < b ? a : b;
}
function bmax(a: bigint, b: bigint): bigint {
  return a > b ? a : b;
}

function windowOf(req: Request, n: bigint): [bigint, bigint] {
  switch (req.type) {
    case "whole":
      return [0n, n];
    case "range":
      return [bmin(req.start, n), bmin(req.end, n)];
    case "offset":
      return [bmin(req.start, n), n];
    case "suffix":
      return [bmax(n - req.count, 0n), n];
  }
}

/** Successor bound of all byte strings with prefix p, or null for +infinity. */
function prefixUpper(p: Uint8Array): Buffer | null {
  const b = Buffer.from(p);
  let n = b.length;
  while (n > 0 && b[n - 1] === 0xff) n--;
  if (n === 0) return null;
  const u = Buffer.from(b.subarray(0, n));
  u[n - 1]++;
  return u;
}

function startsWithBytes(a: Uint8Array, p: Uint8Array): boolean {
  if (a.length < p.length) return false;
  for (let i = 0; i < p.length; i++) if (a[i] !== p[i]) return false;
  return true;
}

export class Archive {
  private fh: fs.FileHandle;
  readonly fileSize: bigint;
  readonly baseUri: string;
  private limit: number;
  private allowed?: string[];

  private sourcesRaw!: Buffer;
  private indexRaw: Buffer | null = null;
  sources!: Source[];
  private cdOffset!: bigint;
  private cdSize!: bigint;
  private index: CdIndex | null = null;
  private pageKeys: Buffer[] = [];
  private pageCache = new Map<number, CdRecord[] | null>();
  private pinnedMap = new Map<string, Pinned>();
  private records: Map<string, CdRecord> | null = null; // unpaged archives

  private constructor(fh: fs.FileHandle, size: bigint, baseUri: string, opts: OpenOptions) {
    this.fh = fh;
    this.fileSize = size;
    this.baseUri = baseUri;
    this.limit = opts.maxRequestBytes ?? DEFAULT_LIMIT;
    this.allowed = opts.allowedUrlPrefixes;
  }

  /** Open an archive from a local path or a file: URL. */
  static async open(location: string, opts: OpenOptions = {}): Promise<Archive> {
    let localPath: string | Buffer;
    let base: string;
    if (/^file:/i.test(location)) {
      const p = fileUriToPath(location);
      if (typeof p === "string") throw err("archive", p);
      localPath = p;
      base = location;
    } else {
      localPath = location;
      base = fileBaseUri(location);
    }
    let fh: fs.FileHandle;
    let size: bigint;
    try {
      fh = await fs.open(localPath, "r");
      const st = await fh.stat({ bigint: true });
      if (!st.isFile()) {
        await fh.close();
        throw new Error("not a regular file");
      }
      size = st.size;
    } catch (e) {
      throw err("archive", `cannot open archive: ${(e as Error).message}`);
    }
    const a = new Archive(fh, size, base, opts);
    try {
      await a.init();
    } catch (e) {
      await fh.close();
      throw e;
    }
    return a;
  }

  async close(): Promise<void> {
    await this.fh.close();
  }

  private async readAt(offset: bigint, length: bigint): Promise<Buffer> {
    const len = Number(length);
    const buf = Buffer.alloc(len);
    let done = 0;
    while (done < len) {
      const { bytesRead } = await this.fh.read(buf, done, len - done, Number(offset) + done);
      if (bytesRead === 0) throw new Error("unexpected end of file");
      done += bytesRead;
    }
    return buf;
  }

  // ------------------------------------------------------------- opening

  private async init(): Promise<void> {
    const size = this.fileSize;
    // §3.4: locate the end of central directory record.
    let eocdPos = -1n;
    let commentLen = 0;
    if (size >= 60n) {
      const b = await this.readAt(size - 60n, 22n);
      if (rdU32(b, 0) === SIG_EOCD && rdU16(b, 20) === 38) {
        eocdPos = size - 60n;
        commentLen = 38;
      }
    }
    if (eocdPos < 0n && size >= 44n) {
      const b = await this.readAt(size - 44n, 22n);
      if (rdU32(b, 0) === SIG_EOCD && rdU16(b, 20) === 22) {
        eocdPos = size - 44n;
        commentLen = 22;
      }
    }
    if (eocdPos < 0n) throw err("archive", "no end of central directory record with a vzip comment");
    const eocd = await this.readAt(eocdPos, BigInt(22 + commentLen));
    const comment = eocd.subarray(22);
    if (comment.subarray(0, 6).toString("latin1") !== MAGIC) {
      throw err("archive", "archive comment does not start with vzip/1");
    }
    const sourcesOffset = rdU64(comment, 6);
    const sourcesSize = rdU64(comment, 14);
    let indexOffset: bigint | null = null;
    let indexSize = 0n;
    if (commentLen === 38) {
      indexOffset = rdU64(comment, 22);
      indexSize = rdU64(comment, 30);
    }

    // §3.2: ZIP64 end records.
    const entriesDisk = rdU16(eocd, 8);
    const entriesTotal = rdU16(eocd, 10);
    let cdSize = BigInt(rdU32(eocd, 12));
    let cdOffset = BigInt(rdU32(eocd, 16));
    if (entriesDisk === 0xffff || entriesTotal === 0xffff || cdSize === 0xffffffffn || cdOffset === 0xffffffffn) {
      if (eocdPos < 20n) throw err("archive", "zip64 end of central directory locator missing");
      const loc = await this.readAt(eocdPos - 20n, 20n);
      if (rdU32(loc, 0) !== SIG_ZIP64_LOCATOR) {
        throw err("archive", "zip64 end of central directory locator missing");
      }
      const recOff = rdU64(loc, 8);
      if (recOff + 56n > size) throw err("archive", "zip64 end of central directory record outside the file");
      const z = await this.readAt(recOff, 56n);
      if (rdU32(z, 0) !== SIG_ZIP64_EOCD) throw err("archive", "bad zip64 end of central directory signature");
      if (rdU64(z, 4) !== 44n) throw err("archive", "zip64 end of central directory record size is not 44");
      cdSize = rdU64(z, 40);
      cdOffset = rdU64(z, 48);
    }
    if (cdOffset + cdSize > size) throw err("archive", "central directory lies outside the file");
    this.cdOffset = cdOffset;
    this.cdSize = cdSize;

    // Format entries.
    const readFormat = async (off: bigint, len: bigint, what: string): Promise<Buffer> => {
      if (off + len > size) throw err("archive", `${what} body lies outside the file`);
      if (len > BigInt(this.limit)) throw err("archive", `${what} exceeds the reader's size limit`);
      const body = await this.readAt(off, len);
      const out = inflateClean(body, this.limit);
      if (out === "too-large") throw err("archive", `${what} inflates beyond the reader's size limit`);
      if (out === null) throw err("archive", `${what} does not inflate cleanly`);
      return out;
    };
    this.sourcesRaw = await readFormat(sourcesOffset, sourcesSize, SOURCES_KEY);
    try {
      this.sources = decodeSourceTable(this.sourcesRaw);
    } catch (e) {
      throw err("archive", `source table is malformed: ${(e as Error).message}`);
    }
    this.sources.forEach((s, i) => {
      if (s.kind === undefined) throw err("archive", `source ${i} has no kind`);
      if (s.kind === "url" && s.url === "") throw err("archive", `source ${i} has an empty url`);
      const pinned = s.size !== undefined || s.etag !== undefined || s.modifiedNotAfter !== undefined;
      if (pinned && s.kind !== "url") throw err("archive", `source ${i} has a pin but is not a url source`);
      if (s.etag !== undefined && !/^"[\x21\x23-\x7e]*"$/.test(s.etag)) {
        throw err("archive", `source ${i} has an etag pin that is not a strong entity tag`);
      }
    });

    if (indexOffset !== null) {
      this.indexRaw = await readFormat(indexOffset, indexSize, INDEX_KEY);
      let idx: CdIndex;
      try {
        idx = decodeCdIndex(this.indexRaw);
      } catch (e) {
        throw err("archive", `page index is malformed: ${(e as Error).message}`);
      }
      let expect = 0n;
      let prev: Buffer | null = null;
      for (const [i, p] of idx.pages.entries()) {
        if (p.length === 0n) throw err("archive", `page index is malformed: page ${i} is empty`);
        if (p.offset + p.length > cdSize) throw err("archive", `page index is malformed: page ${i} lies outside the central directory`);
        if (p.offset !== expect) throw err("archive", `page index is malformed: page ${i} is not contiguous`);
        expect = p.offset + p.length;
        const fk = Buffer.from(encodeUtf8(p.firstKey));
        if (prev !== null && compareBytes(prev, fk) >= 0) {
          throw err("archive", `page index is malformed: first_key values do not strictly increase at page ${i}`);
        }
        prev = fk;
        this.pageKeys.push(fk);
      }
      for (const p of idx.pinned) {
        if (isFormatKey(p.key)) throw err("archive", `page index is malformed: format entry ${p.key} is pinned`);
        if (this.pinnedMap.has(p.key)) throw err("archive", `page index is malformed: ${p.key} pinned twice`);
        if (p.method !== 0 && p.method !== 8) throw err("archive", `page index is malformed: pinned method ${p.method}`);
        if (p.dataOffset + p.csize > size) throw err("archive", `page index is malformed: pinned body of ${p.key} lies outside the file`);
        this.pinnedMap.set(p.key, p);
      }
      this.index = idx;
    } else {
      if (cdSize > BigInt(this.limit)) throw err("archive", "central directory exceeds the reader's size limit");
      const cd = await this.readAt(cdOffset, cdSize);
      const recs = parseRecords(cd);
      if (recs === null) throw err("archive", "central directory does not parse");
      this.records = new Map();
      for (const r of recs) {
        if (r.name === null) continue;
        if (r.name === INDEX_KEY) throw err("archive", "archive without a page index has an __vz__/index entry");
        if (!this.records.has(r.name)) this.records.set(r.name, r); // duplicates: first wins
      }
    }
  }

  get paged(): boolean {
    return this.index !== null;
  }

  // ------------------------------------------------------------- lookup

  private async loadPage(i: number): Promise<CdRecord[]> {
    let recs = this.pageCache.get(i);
    if (recs === undefined) {
      const p = this.index!.pages[i];
      const buf = await this.readAt(this.cdOffset + p.offset, p.length);
      recs = parseRecords(buf);
      this.pageCache.set(i, recs);
    }
    if (recs === null) throw err("entry", `page ${i} of the central directory cannot be parsed`);
    return recs;
  }

  /** Find the entry for `key` (any key, hidden or not). Throws entry errors. */
  private async locate(key: string): Promise<Located | null> {
    if (key === "") return null;
    if (isFormatKey(key)) {
      if (key === INDEX_KEY && this.indexRaw === null) return null;
      return { t: "format", key };
    }
    if (this.index === null) {
      const r = this.records!.get(key);
      return r ? { t: "record", rec: r } : null;
    }
    const pin = this.pinnedMap.get(key);
    if (pin) return { t: "pinned", p: pin };
    const kb = Buffer.from(encodeUtf8(key));
    // last page with first_key <= key
    let lo = 0;
    let hi = this.pageKeys.length; // invariant: answer in [lo-1, hi-1]
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (compareBytes(this.pageKeys[mid], kb) <= 0) lo = mid + 1;
      else hi = mid;
    }
    const pageIdx = lo - 1;
    if (pageIdx < 0) return null;
    const recs = await this.loadPage(pageIdx);
    for (const r of recs) {
      if (r.name !== null && compareBytes(r.nameBytes, kb) === 0) return { t: "record", rec: r };
    }
    return null;
  }

  private entryOf(loc: Located): EntryInfo {
    if (loc.t === "record") return entryFromRecord(loc.rec);
    if (loc.t === "pinned") {
      return {
        kind: "bytes",
        method: loc.p.method,
        bodyOffset: loc.p.dataOffset,
        csize: loc.p.csize,
        usize: loc.p.size,
      };
    }
    throw new Error("format entries have no EntryInfo");
  }

  // ------------------------------------------------------------- bodies

  /** Read bytes [a, b) of a bytes entry's value, checking the body (§8.4 step 4). */
  private async readBody(e: EntryInfo, a: bigint | null, b: bigint | null): Promise<Buffer> {
    if (e.bodyOffset + e.csize > this.fileSize) throw err("body", "body lies outside the file");
    if (e.method === 0) {
      if (e.csize !== e.usize) throw err("body", "STORED entry with differing compressed and uncompressed sizes");
      const s = a ?? 0n;
      const t = b ?? e.usize;
      if (t - s > BigInt(this.limit)) throw err("request", `request exceeds the reader's limit of ${this.limit} bytes`);
      return this.readAt(e.bodyOffset + s, t - s);
    }
    if (e.usize > BigInt(this.limit) || e.csize > BigInt(this.limit)) {
      throw err("request", `entry exceeds the reader's limit of ${this.limit} bytes`);
    }
    const body = await this.readAt(e.bodyOffset, e.csize);
    const out = inflateClean(body, Number(e.usize) + 1);
    if (out === null || out === "too-large" || out.length !== Number(e.usize)) {
      throw err("body", "DEFLATE body does not inflate cleanly to the recorded size");
    }
    return out.subarray(Number(a ?? 0n), Number(b ?? e.usize));
  }

  // ------------------------------------------------------------- operations

  async classify(key: string): Promise<Kind> {
    if (isHidden(key)) return "missing";
    const loc = await this.locate(key);
    if (loc === null) return "missing";
    return this.entryOf(loc).kind;
  }

  async raw(key: string): Promise<Buffer | null> {
    const loc = await this.locate(key);
    if (loc === null) return null;
    if (loc.t === "format") return loc.key === SOURCES_KEY ? this.sourcesRaw : this.indexRaw!;
    const e = this.entryOf(loc);
    return this.readBody(e, null, null);
  }

  async get(key: string, req: Request = { type: "whole" }): Promise<Buffer | null> {
    if (req.type === "range" && req.start > req.end) throw err("request", "range start > end");
    if (isHidden(key)) return null;
    const loc = await this.locate(key);
    if (loc === null) return null;
    const e = this.entryOf(loc);
    if (e.kind === "bytes") {
      const [a, b] = windowOf(req, e.usize);
      return this.readBody(e, a, b);
    }
    // Reference (§8.3).
    let parts: Range[];
    try {
      parts = e.payloadId === EXTRA_RANGE ? [decodeRange(e.payload!)] : decodeConcat(e.payload!);
    } catch (x) {
      if (x instanceof MalformedError) throw err("payload", `malformed reference payload: ${x.message}`);
      throw x;
    }
    let total = 0n;
    for (const [i, p] of parts.entries()) {
      if (p.data === undefined && p.source >= this.sources.length) {
        throw err("payload", `range ${i} names source ${p.source}, but there are ${this.sources.length} sources`);
      }
      total += rangeSize(p);
    }
    if (total > U64_MAX) throw err("payload", "reference size exceeds 2^64-1");
    const [a, b] = windowOf(req, total);
    const chunks: Buffer[] = [];
    let acc = 0n;
    let start = 0n;
    for (const p of parts) {
      const sz = rangeSize(p);
      const rs = start;
      const re = start + sz;
      start = re;
      const lo = bmax(a, rs);
      const hi = bmin(b, re);
      if (lo >= hi) continue;
      const i = lo - rs;
      const j = hi - rs;
      if (p.data !== undefined) {
        acc += j - i;
        if (acc > BigInt(this.limit)) throw err("request", `request exceeds the reader's limit of ${this.limit} bytes`);
        chunks.push(Buffer.from(p.data.subarray(Number(i), Number(j))));
      } else {
        const got = await this.readSource(p.source, p.offset + i, p.offset + j, acc);
        acc += j - i;
        chunks.push(got);
      }
    }
    return Buffer.concat(chunks);
  }

  async list(prefix: string): Promise<string[]> {
    const pb = Buffer.from(encodeUtf8(prefix));
    const found = new Map<string, Buffer>();
    const consider = (name: string | null, nb: Buffer) => {
      if (name === null || isHidden(name) || !startsWithBytes(nb, pb)) return;
      found.set(name, nb);
    };
    if (this.index === null) {
      for (const r of this.records!.values()) consider(r.name, r.nameBytes);
    } else {
      for (const p of this.index.pinned) consider(p.key === "" ? null : p.key, Buffer.from(encodeUtf8(p.key)));
      const upper = prefixUpper(pb);
      const n = this.pageKeys.length;
      for (let i = 0; i < n; i++) {
        const fk = this.pageKeys[i];
        const next = i + 1 < n ? this.pageKeys[i + 1] : null;
        if (upper !== null && compareBytes(fk, upper) >= 0) break;
        if (next !== null && compareBytes(next, pb) <= 0) continue;
        const recs = await this.loadPage(i);
        for (const r of recs) {
          if (r.name === null) continue;
          if (compareBytes(r.nameBytes, fk) < 0) continue;
          if (next !== null && compareBytes(r.nameBytes, next) >= 0) continue;
          if (this.pinnedMap.has(r.name) || isFormatKey(r.name)) continue;
          consider(r.name, r.nameBytes);
        }
      }
    }
    return [...found.entries()].sort((x, y) => compareBytes(x[1], y[1])).map((x) => x[0]);
  }

  // ------------------------------------------------------------- sources

  private checkLimit(acc: bigint, n: bigint): void {
    if (acc + n > BigInt(this.limit)) {
      throw err("request", `request exceeds the reader's limit of ${this.limit} bytes`);
    }
  }

  /** Read [s, t) of source `idx`'s value (§6, §8.3 step 3). */
  private async readSource(idx: number, s: bigint, t: bigint, acc: bigint): Promise<Buffer> {
    const src = this.sources[idx];
    if (src.kind === "data") {
      if (BigInt(src.data!.length) < t) throw err("resolution", `data source ${idx} is shorter than ${t} bytes`);
      this.checkLimit(acc, t - s);
      return Buffer.from(src.data!.subarray(Number(s), Number(t)));
    }
    if (src.kind === "key") {
      const k = src.key!;
      if (isFormatKey(k)) throw err("resolution", `key source ${idx} names format entry ${k}`);
      let e: EntryInfo;
      try {
        const loc = await this.locate(k);
        if (loc === null) throw err("resolution", `key source ${idx}: key '${k}' is missing`);
        e = this.entryOf(loc);
      } catch (x) {
        if (x instanceof VzipError && x.errorClass === "entry") {
          throw err("resolution", `key source ${idx}: ${x.message}`);
        }
        throw x;
      }
      if (e.kind !== "bytes") throw err("resolution", `key source ${idx}: '${k}' is a reference entry`);
      if (e.usize < t) throw err("resolution", `key source ${idx}: '${k}' is shorter than ${t} bytes`);
      this.checkLimit(acc, t - s);
      try {
        return await this.readBody(e, s, t);
      } catch (x) {
        if (x instanceof VzipError && x.errorClass === "body") {
          throw err("resolution", `key source ${idx}: ${x.message}`);
        }
        throw x;
      }
    }
    return this.readUrl(idx, src, s, t, acc);
  }

  private async readUrl(idx: number, src: Source, s: bigint, t: bigint, acc: bigint): Promise<Buffer> {
    const ref = src.url!;
    if (!isUriReference(ref)) throw err("resolution", `source ${idx}: '${ref}' is not a valid URI reference`);
    const target = resolveUri(this.baseUri, ref);
    if (this.allowed && !this.allowed.some((p) => target.startsWith(p))) {
      throw err("resolution", `source ${idx}: '${target}' is not in the allowed URL prefixes`);
    }
    const scheme = (parseUri(target).scheme ?? "").toLowerCase();
    if (scheme === "file") {
      const p = fileUriToPath(target);
      if (typeof p === "string") throw err("resolution", `source ${idx}: ${p}`);
      if (src.etag !== undefined) throw err("resolution", `source ${idx}: an etag pin cannot be checked for file: URLs`);
      let fh: fs.FileHandle;
      try {
        fh = await fs.open(p, "r");
      } catch (e) {
        throw err("resolution", `source ${idx}: cannot open ${target}: ${(e as Error).message}`);
      }
      try {
        const st = await fh.stat({ bigint: true });
        if (!st.isFile()) throw err("resolution", `source ${idx}: ${target} is not a regular file`);
        if (src.size !== undefined && st.size !== src.size) {
          throw err("resolution", `source ${idx}: size pin failed (pinned ${src.size}, actual ${st.size})`);
        }
        if (src.modifiedNotAfter !== undefined) {
          const ns = st.mtimeNs;
          let sec = ns / 1000000000n;
          if (ns < 0n && ns % 1000000000n !== 0n) sec -= 1n; // floor
          if (sec > src.modifiedNotAfter) {
            throw err("resolution", `source ${idx}: modified_not_after pin failed (mtime ${sec} > ${src.modifiedNotAfter})`);
          }
        }
        if (st.size < t) throw err("resolution", `source ${idx}: ${target} is shorter than ${t} bytes`);
        this.checkLimit(acc, t - s);
        const len = Number(t - s);
        const buf = Buffer.alloc(len);
        let done = 0;
        while (done < len) {
          const { bytesRead } = await fh.read(buf, done, len - done, Number(s) + done);
          if (bytesRead === 0) throw err("resolution", `source ${idx}: unexpected end of ${target}`);
          done += bytesRead;
        }
        return buf;
      } finally {
        await fh.close();
      }
    }
    if (scheme === "http" || scheme === "https") {
      return this.readHttp(idx, src, target, s, t, acc);
    }
    throw err("resolution", `source ${idx}: unsupported URL scheme '${scheme}'`);
  }

  private async readHttp(idx: number, src: Source, target: string, s: bigint, t: bigint, acc: bigint): Promise<Buffer> {
    this.checkLimit(acc, t - s);
    const url = target.replace(/#.*$/s, "");
    const headers: Record<string, string> = { Range: `bytes=${s}-${t - 1n}` };
    if (src.etag !== undefined) headers["If-Match"] = src.etag;
    if (src.modifiedNotAfter !== undefined) {
      const d = new Date(Number(src.modifiedNotAfter) * 1000);
      if (Number.isNaN(d.getTime())) throw err("resolution", `source ${idx}: modified_not_after pin cannot be expressed as an HTTP-date`);
      headers["If-Unmodified-Since"] = d.toUTCString();
    }
    let res: Response;
    try {
      res = await fetch(url, { headers });
    } catch (e) {
      throw err("resolution", `source ${idx}: request to ${url} failed: ${(e as Error).message}`);
    }
    if (res.status === 412) throw err("resolution", `source ${idx}: pin failed (412 Precondition Failed)`);
    if (res.status === 416) throw err("resolution", `source ${idx}: ${url} is shorter than ${t} bytes`);
    if (res.status === 206) {
      const cr = res.headers.get("content-range") ?? "";
      const m = /^bytes (\d+)-(\d+)\/(\d+|\*)$/.exec(cr.trim());
      if (!m) throw err("resolution", `source ${idx}: bad Content-Range '${cr}'`);
      const total = m[3] === "*" ? null : BigInt(m[3]);
      if (src.size !== undefined && (total === null || total !== src.size)) {
        throw err("resolution", `source ${idx}: size pin failed`);
      }
      if (BigInt(m[1]) !== s || BigInt(m[2]) !== t - 1n) {
        throw err("resolution", `source ${idx}: server returned a different range (${cr}); object shorter than ${t} bytes?`);
      }
      const body = Buffer.from(await res.arrayBuffer());
      if (BigInt(body.length) !== t - s) throw err("resolution", `source ${idx}: short response body`);
      return body;
    }
    if (res.status === 200) {
      const body = Buffer.from(await res.arrayBuffer());
      if (src.size !== undefined && BigInt(body.length) !== src.size) throw err("resolution", `source ${idx}: size pin failed`);
      if (BigInt(body.length) < t) throw err("resolution", `source ${idx}: ${url} is shorter than ${t} bytes`);
      return body.subarray(Number(s), Number(t));
    }
    throw err("resolution", `source ${idx}: HTTP status ${res.status} from ${url}`);
  }
}
