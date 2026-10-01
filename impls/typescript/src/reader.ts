// vzip reader (spec §3, §4, §7, §8).
import * as fs from "node:fs";
import * as zlib from "node:zlib";
import { MalformedError, VzError, decodeUtf8 } from "./errors.ts";
import {
  type CdIndex,
  type Range,
  type Source,
  decodeCdIndex,
  decodeConcat,
  decodeRange,
  decodeSourceTable,
} from "./proto.ts";
import { type Pins, readUrlRange } from "./fetch.ts";
import { type Uri, fileBaseUri, parseUriRef } from "./url.ts";

export const RESERVED_PREFIX = "__vz__/";
export const SOURCES_KEY = "__vz__/sources";
export const INDEX_KEY = "__vz__/index";
export const EXTRA_RANGE = 0x7a76;
export const EXTRA_CONCAT = 0x7a77;

/**
 * Resource limit (spec §10): the largest value a single request may produce, and the largest
 * DEFLATE body this reader will inflate. Exceeding it is a request error.
 */
export const MAX_REQUEST_BYTES = 1 << 30;

const U64_MAX = (1n << 64n) - 1n;
const ALL32 = 0xffffffffn;

const SIG_EOCD = 0x06054b50;
const SIG_Z64_LOC = 0x07064b50;
const SIG_Z64_EOCD = 0x06064b50;
const SIG_CDR = 0x02014b50;

export type Kind = "bytes" | "reference" | "missing";

export type Request =
  | { type: "whole" }
  | { type: "range"; start: bigint; end: bigint }
  | { type: "offset"; start: bigint }
  | { type: "suffix"; count: bigint };

/** Where an entry lives and how it is stored. */
interface Loc {
  kind: "bytes" | "reference";
  method: number;
  csize: bigint;
  usize: bigint;
  bodyOffset: bigint;
  refId: number; // 0 for bytes
  payload: Uint8Array | null;
  error: string | null; // entry error
}

interface Rec {
  nameBytes: Buffer;
  name: string | null; // null if empty or not valid UTF-8
  loc: Loc;
}

function archiveErr(msg: string): VzError {
  return new VzError("archive", msg);
}

function u64(b: Buffer, off: number): bigint {
  return b.readBigUInt64LE(off);
}

function isHidden(key: Buffer): boolean {
  return key.length >= 7 && key.subarray(0, 7).toString("latin1") === RESERVED_PREFIX;
}

const SOURCES_BYTES = Buffer.from(SOURCES_KEY);
const INDEX_BYTES = Buffer.from(INDEX_KEY);

function isFormatKey(key: Buffer): boolean {
  return key.equals(SOURCES_BYTES) || key.equals(INDEX_BYTES);
}

/** Smallest byte string greater than every string with this prefix, or null if unbounded. */
function prefixUpper(prefix: Buffer): Buffer | null {
  let n = prefix.length;
  while (n > 0 && prefix[n - 1] === 0xff) n--;
  if (n === 0) return null;
  const out = Buffer.from(prefix.subarray(0, n));
  out[n - 1]++;
  return out;
}

/**
 * Inflate a raw DEFLATE body, requiring it to be a single complete stream that ends exactly at
 * the end of the body (spec §8.1). Throws Error on failure, VzError("request") on the size limit.
 */
export function inflateCleanly(body: Buffer, limit: number): Buffer {
  let r: { buffer: Buffer; engine: zlib.InflateRaw };
  try {
    r = zlib.inflateRawSync(body, { info: true, maxOutputLength: limit }) as unknown as {
      buffer: Buffer;
      engine: zlib.InflateRaw;
    };
  } catch (e) {
    const err = e as NodeJS.ErrnoException;
    if (err.code === "ERR_BUFFER_TOO_LARGE") {
      throw new VzError("request", `inflated body exceeds the ${limit}-byte limit`);
    }
    throw new Error(`DEFLATE stream does not inflate: ${err.message}`);
  }
  if (r.engine.bytesWritten !== body.length) {
    throw new Error(`DEFLATE stream ends after ${r.engine.bytesWritten} of ${body.length} bytes`);
  }
  return r.buffer;
}

/** Parse extra field blocks; null if they don't exactly fill the field. */
function parseExtra(extra: Buffer): { id: number; data: Buffer }[] | null {
  const out: { id: number; data: Buffer }[] = [];
  let p = 0;
  while (p < extra.length) {
    if (p + 4 > extra.length) return null;
    const id = extra.readUInt16LE(p);
    const n = extra.readUInt16LE(p + 2);
    if (p + 4 + n > extra.length) return null;
    out.push({ id, data: extra.subarray(p + 4, p + 4 + n) });
    p += 4 + n;
  }
  return out;
}

/** Analyse one central directory record's contents (spec §4.1, §3.2, §8.4 entry errors). */
function analyseRecord(flags: number, method: number, csize32: number, usize32: number, lho32: number, nameLen: number, extra: Buffer): Loc {
  const loc: Loc = {
    kind: "bytes",
    method,
    csize: BigInt(csize32),
    usize: BigInt(usize32),
    bodyOffset: 0n,
    refId: 0,
    payload: null,
    error: null,
  };
  const fail = (m: string): Loc => {
    loc.error = m;
    return loc;
  };
  const blocks = parseExtra(extra);
  if (blocks === null) return fail("extra field does not parse");
  const refs = blocks.filter((b) => b.id === EXTRA_RANGE || b.id === EXTRA_CONCAT);
  if (refs.length > 1) return fail("more than one reference block");
  if (refs.length === 1) {
    loc.kind = "reference";
    loc.refId = refs[0].id;
    loc.payload = refs[0].data;
  }
  if (method !== 0 && method !== 8) return fail(`unsupported compression method ${method}`);
  if (flags & 1) return fail("entry is encrypted");
  if (loc.kind === "reference" && method === 8) return fail("reference entry uses method 8");
  // ZIP64 extended information.
  const z64 = blocks.filter((b) => b.id === 0x0001);
  let lho = BigInt(lho32);
  const needU = BigInt(usize32) === ALL32;
  const needC = BigInt(csize32) === ALL32;
  const needO = lho === ALL32;
  if (needO) {
    if (z64.length !== 1) return fail(z64.length === 0 ? "offset is 0xFFFFFFFF but there is no ZIP64 block" : "more than one ZIP64 block");
  }
  if ((needU || needC || needO) && z64.length === 1) {
    const d = z64[0].data;
    const need = (needU ? 8 : 0) + (needC ? 8 : 0) + (needO ? 8 : 0);
    if (d.length < need) {
      if (needO) return fail("ZIP64 block too short");
    } else {
      let p = 0;
      if (needU) { loc.usize = u64(d, p); p += 8; }
      if (needC) { loc.csize = u64(d, p); p += 8; }
      if (needO) { lho = u64(d, p); p += 8; }
    }
  }
  loc.bodyOffset = lho + 30n + BigInt(nameLen);
  return loc;
}

/** Parse a run of central directory records that must exactly fill `buf`. Throws Error on failure. */
function parseRecords(buf: Buffer): Rec[] {
  const out: Rec[] = [];
  let p = 0;
  while (p < buf.length) {
    if (p + 46 > buf.length) throw new Error(`truncated central directory record at +${p}`);
    if (buf.readUInt32LE(p) !== SIG_CDR) throw new Error(`bad central directory signature at +${p}`);
    const flags = buf.readUInt16LE(p + 8);
    const method = buf.readUInt16LE(p + 10);
    const csize = buf.readUInt32LE(p + 20);
    const usize = buf.readUInt32LE(p + 24);
    const nameLen = buf.readUInt16LE(p + 28);
    const extraLen = buf.readUInt16LE(p + 30);
    const commentLen = buf.readUInt16LE(p + 32);
    const lho = buf.readUInt32LE(p + 42);
    const end = p + 46 + nameLen + extraLen + commentLen;
    if (end > buf.length) throw new Error(`central directory record at +${p} extends past its end`);
    const nameBytes = Buffer.from(buf.subarray(p + 46, p + 46 + nameLen));
    const extra = Buffer.from(buf.subarray(p + 46 + nameLen, p + 46 + nameLen + extraLen));
    const name = nameLen === 0 ? null : decodeUtf8(nameBytes);
    out.push({ nameBytes, name, loc: analyseRecord(flags, method, csize, usize, lho, nameLen, extra) });
    p = end;
  }
  return out;
}

interface Piece {
  range: Range;
  lo: bigint; // overlap relative to range start
  hi: bigint;
}

export class Archive {
  private fd: number;
  readonly fileSize: bigint;
  readonly baseUri: Uri;
  readonly sources: Source[];
  private sourcesBody: Buffer;
  private indexBody: Buffer | null = null;
  private index: CdIndex | null = null;
  private cdOffset = 0n;
  private cdSize = 0n;
  private records: Map<string, Rec> | null = null; // unpaged
  private allNames: Buffer[] = []; // unpaged, valid names
  private pinned: Map<string, Loc> = new Map();
  private pageCache: Map<number, Rec[] | Error> = new Map();

  private constructor(fd: number, fileSize: bigint, baseUri: Uri) {
    this.fd = fd;
    this.fileSize = fileSize;
    this.baseUri = baseUri;
    this.sources = [];
    this.sourcesBody = Buffer.alloc(0);
  }

  /** Open an archive from a local path. Throws VzError("archive") on failure. */
  static open(localPath: string): Archive {
    let fd: number;
    let size: bigint;
    try {
      fd = fs.openSync(localPath, "r");
      const st = fs.fstatSync(fd, { bigint: true });
      if (!st.isFile()) {
        fs.closeSync(fd);
        throw new Error("not a regular file");
      }
      size = st.size;
    } catch (e) {
      throw archiveErr(`cannot open ${localPath}: ${(e as Error).message}`);
    }
    const base = parseUriRef(fileBaseUri(localPath))!;
    const a = new Archive(fd, size, base);
    try {
      a.openChecks();
    } catch (e) {
      a.close();
      if (e instanceof VzError && e.cls === "archive") throw e;
      throw archiveErr((e as Error).message);
    }
    return a;
  }

  close(): void {
    if (this.fd >= 0) {
      try {
        fs.closeSync(this.fd);
      } catch {
        /* ignore */
      }
      this.fd = -1;
    }
  }

  private readAt(off: bigint, len: number): Buffer {
    const buf = Buffer.alloc(len);
    let got = 0;
    while (got < len) {
      const n = fs.readSync(this.fd, buf, got, len - got, Number(off) + got);
      if (n === 0) throw new Error("unexpected end of file");
      got += n;
    }
    return buf;
  }

  private within(off: bigint, len: bigint): boolean {
    return off >= 0n && len >= 0n && off + len <= this.fileSize;
  }

  // ---------- §8.1 ----------

  private openChecks(): void {
    const fsz = this.fileSize;
    let eocdPos = -1n;
    let commentLen = 0;
    if (fsz >= 60n) {
      const b = this.readAt(fsz - 60n, 22);
      if (b.readUInt32LE(0) === SIG_EOCD && b.readUInt16LE(20) === 38) {
        eocdPos = fsz - 60n;
        commentLen = 38;
      }
    }
    if (eocdPos < 0n && fsz >= 44n) {
      const b = this.readAt(fsz - 44n, 22);
      if (b.readUInt32LE(0) === SIG_EOCD && b.readUInt16LE(20) === 22) {
        eocdPos = fsz - 44n;
        commentLen = 22;
      }
    }
    if (eocdPos < 0n) throw archiveErr("not a vzip archive: no end of central directory record with a vzip comment");
    const eocd = this.readAt(eocdPos, 22 + commentLen);
    const comment = eocd.subarray(22);
    if (comment.subarray(0, 5).toString("latin1") !== "vzip/") throw archiveErr("not a vzip archive: comment does not start with vzip/");
    if (comment[5] !== 0x30) throw archiveErr(`unsupported vzip format version (magic ${JSON.stringify(comment.subarray(0, 6).toString("latin1"))})`);

    const entriesDisk = eocd.readUInt16LE(8);
    const entriesTotal = eocd.readUInt16LE(10);
    let cdSize = BigInt(eocd.readUInt32LE(12));
    let cdOffset = BigInt(eocd.readUInt32LE(16));
    if (entriesDisk === 0xffff || entriesTotal === 0xffff || cdSize === ALL32 || cdOffset === ALL32) {
      if (eocdPos < 20n) throw archiveErr("ZIP64 end of central directory locator missing");
      const loc = this.readAt(eocdPos - 20n, 20);
      if (loc.readUInt32LE(0) !== SIG_Z64_LOC) throw archiveErr("ZIP64 end of central directory locator missing");
      const recOff = u64(loc, 8);
      if (!this.within(recOff, 56n)) throw archiveErr("ZIP64 end of central directory record lies outside the file");
      const rec = this.readAt(recOff, 56);
      if (rec.readUInt32LE(0) !== SIG_Z64_EOCD) throw archiveErr("bad ZIP64 end of central directory signature");
      if (u64(rec, 4) !== 44n) throw archiveErr("ZIP64 end of central directory record size field is not 44");
      cdSize = u64(rec, 40);
      cdOffset = u64(rec, 48);
    }
    if (!this.within(cdOffset, cdSize)) throw archiveErr("central directory lies outside the file");
    this.cdOffset = cdOffset;
    this.cdSize = cdSize;

    // Format entries.
    const srcOff = u64(comment, 6);
    const srcSize = u64(comment, 14);
    this.sourcesBody = this.readFormatBody("__vz__/sources", srcOff, srcSize);
    let table: Source[];
    try {
      table = decodeSourceTable(this.sourcesBody);
    } catch (e) {
      throw archiveErr(`source table is malformed: ${(e as Error).message}`);
    }
    for (let i = 0; i < table.length; i++) {
      const s = table[i];
      if (s.kind === null) throw archiveErr(`source ${i} has no kind`);
      if (s.kind.type === "url" && s.kind.value === "") throw archiveErr(`source ${i} has an empty url`);
      const hasPin = s.size !== null || s.etag !== null || s.modifiedNotAfter !== null;
      if (hasPin && s.kind.type !== "url") throw archiveErr(`source ${i} has a pin on a ${s.kind.type} source`);
      if (s.etag !== null && !isStrongEtag(s.etag)) throw archiveErr(`source ${i} has an etag pin that is not a strong entity tag`);
    }
    (this as { sources: Source[] }).sources = table;

    if (commentLen === 38) {
      const idxOff = u64(comment, 22);
      const idxSize = u64(comment, 30);
      this.indexBody = this.readFormatBody("__vz__/index", idxOff, idxSize);
      let idx: CdIndex;
      try {
        idx = decodeCdIndex(this.indexBody);
      } catch (e) {
        throw archiveErr(`page index is malformed: ${(e as Error).message}`);
      }
      this.checkIndex(idx);
      this.index = idx;
      for (const p of idx.pinned) {
        this.pinned.set(Buffer.from(p.key).toString("latin1"), {
          kind: "bytes",
          method: p.method,
          csize: p.csize,
          usize: p.size,
          bodyOffset: p.dataOffset,
          refId: 0,
          payload: null,
          error: null,
        });
      }
    } else {
      if (cdSize > BigInt(MAX_REQUEST_BYTES)) throw archiveErr("central directory too large for this reader");
      const cd = this.readAt(cdOffset, Number(cdSize));
      let recs: Rec[];
      try {
        recs = parseRecords(cd);
      } catch (e) {
        throw archiveErr(`central directory does not parse: ${(e as Error).message}`);
      }
      this.records = new Map();
      for (const r of recs) {
        if (r.name === null) continue;
        if (r.nameBytes.equals(INDEX_BYTES)) throw archiveErr("archive without a page index has an __vz__/index entry");
        const k = r.nameBytes.toString("latin1");
        if (!this.records.has(k)) {
          this.records.set(k, r);
          this.allNames.push(r.nameBytes);
        }
      }
      this.allNames.sort(Buffer.compare);
    }
  }

  private readFormatBody(what: string, off: bigint, size: bigint): Buffer {
    if (!this.within(off, size)) throw archiveErr(`${what} body lies outside the file`);
    if (size > BigInt(MAX_REQUEST_BYTES)) throw archiveErr(`${what} body too large for this reader`);
    const body = this.readAt(off, Number(size));
    try {
      return inflateCleanly(body, MAX_REQUEST_BYTES);
    } catch (e) {
      throw archiveErr(`${what} does not inflate cleanly: ${(e as Error).message}`);
    }
  }

  private checkIndex(idx: CdIndex): void {
    const bad = (m: string) => archiveErr(`page index is malformed: ${m}`);
    let expect = 0n;
    let prev: Buffer | null = null;
    for (let i = 0; i < idx.pages.length; i++) {
      const p = idx.pages[i];
      if (p.length === 0n) throw bad(`page ${i} has length 0`);
      if (p.offset + p.length > this.cdSize) throw bad(`page ${i} lies outside the central directory`);
      if (p.offset !== expect) throw bad(`page ${i} does not start where the previous page ends`);
      expect = p.offset + p.length;
      if (p.firstKey === "") throw bad(`page ${i} has an empty first_key`);
      const k = Buffer.from(p.firstKey);
      if (prev !== null && Buffer.compare(prev, k) >= 0) throw bad(`first_key values do not strictly increase at page ${i}`);
      prev = k;
    }
    const seen = new Set<string>();
    for (const p of idx.pinned) {
      if (p.key === "") throw bad("a pinned key is empty");
      const k = Buffer.from(p.key);
      const ks = k.toString("latin1");
      if (seen.has(ks)) throw bad(`pinned key ${JSON.stringify(p.key)} is listed twice`);
      seen.add(ks);
      if (isFormatKey(k)) throw bad(`pinned key ${JSON.stringify(p.key)} is a format entry`);
      if (p.method !== 0 && p.method !== 8) throw bad(`pinned key ${JSON.stringify(p.key)} has method ${p.method}`);
      if (!this.within(p.dataOffset, p.csize)) throw bad(`pinned body of ${JSON.stringify(p.key)} lies outside the file`);
    }
  }

  // ---------- lookup (§7.2) ----------

  private pageRecords(i: number): Rec[] {
    let r = this.pageCache.get(i);
    if (r === undefined) {
      const p = this.index!.pages[i];
      try {
        if (p.length > BigInt(MAX_REQUEST_BYTES)) throw new Error("page too large for this reader");
        r = parseRecords(this.readAt(this.cdOffset + p.offset, Number(p.length)));
      } catch (e) {
        r = e as Error;
      }
      this.pageCache.set(i, r);
    }
    if (r instanceof Error) throw new VzError("entry", `page ${i} cannot be parsed: ${r.message}`);
    return r;
  }

  private pageFirstKeys: Buffer[] | null = null;
  private firstKeys(): Buffer[] {
    if (this.pageFirstKeys === null) this.pageFirstKeys = this.index!.pages.map((p) => Buffer.from(p.firstKey));
    return this.pageFirstKeys;
  }

  /**
   * Find the record for a key that is not a format entry. Returns null if missing.
   * Throws VzError("entry") if the page holding it cannot be parsed. Does not check the record's own entry error.
   */
  private lookup(key: Buffer): Loc | null {
    if (this.index === null) {
      const r = this.records!.get(key.toString("latin1"));
      return r ? r.loc : null;
    }
    const pin = this.pinned.get(key.toString("latin1"));
    if (pin) return pin;
    const fk = this.firstKeys();
    // last page with first_key <= key
    let lo = 0;
    let hi = fk.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (Buffer.compare(fk[mid], key) <= 0) lo = mid + 1;
      else hi = mid;
    }
    const pi = lo - 1;
    if (pi < 0) return null;
    for (const r of this.pageRecords(pi)) {
      if (r.name !== null && r.nameBytes.equals(key)) return r.loc;
    }
    return null;
  }

  /** Lookup plus entry-error check (§8.4 step 3). */
  private lookupChecked(key: Buffer): Loc | null {
    const loc = this.lookup(key);
    if (loc && loc.error !== null) throw new VzError("entry", loc.error);
    return loc;
  }

  // ---------- bodies ----------

  private checkBodyBounds(loc: Loc): void {
    if (!this.within(loc.bodyOffset, loc.csize)) throw new VzError("body", "entry body lies outside the file");
  }

  /** Inflate (method 8) or validate (method 0) the body; returns full value for method 8, null for method 0. */
  private inflateBody(loc: Loc): Buffer | null {
    this.checkBodyBounds(loc);
    if (loc.method === 8) {
      if (loc.csize > BigInt(MAX_REQUEST_BYTES) || loc.usize > BigInt(MAX_REQUEST_BYTES)) {
        throw new VzError("request", `entry larger than the ${MAX_REQUEST_BYTES}-byte limit`);
      }
      const comp = this.readAt(loc.bodyOffset, Number(loc.csize));
      let out: Buffer;
      try {
        out = inflateCleanly(comp, MAX_REQUEST_BYTES);
      } catch (e) {
        if (e instanceof VzError) throw e;
        throw new VzError("body", (e as Error).message);
      }
      if (BigInt(out.length) !== loc.usize) {
        throw new VzError("body", `inflated to ${out.length} bytes, record says ${loc.usize}`);
      }
      return out;
    }
    if (loc.csize !== loc.usize) throw new VzError("body", "STORED entry's compressed and uncompressed sizes differ");
    return null;
  }

  /** Bytes [a, b) of a bytes entry's (or raw) value, with body checks. */
  private readBodyWindow(loc: Loc, a: bigint, b: bigint): Buffer {
    const full = this.inflateBody(loc);
    if (full !== null) return full.subarray(Number(a), Number(b));
    if (b - a > BigInt(MAX_REQUEST_BYTES)) throw new VzError("request", `request larger than the ${MAX_REQUEST_BYTES}-byte limit`);
    return this.readAt(loc.bodyOffset + a, Number(b - a));
  }

  // ---------- operations (§8.2) ----------

  classify(key: string): Kind {
    const kb = Buffer.from(key, "utf8");
    if (!key.isWellFormed() || kb.length === 0) return "missing";
    if (isHidden(kb)) return "missing";
    const loc = this.lookupChecked(kb);
    return loc ? loc.kind : "missing";
  }

  async get(key: string, req: Request = { type: "whole" }): Promise<Uint8Array | null> {
    if (req.type === "range" && req.start > req.end) throw new VzError("request", "range start > end");
    const kb = Buffer.from(key, "utf8");
    if (!key.isWellFormed() || kb.length === 0) return null;
    if (isHidden(kb)) return null;
    const loc = this.lookupChecked(kb);
    if (!loc) return null;
    if (loc.kind === "bytes") {
      // Body checks judge the whole body first (§8.4 step 4).
      this.checkBodyBounds(loc);
      if (loc.method === 8) {
        const full = this.inflateBody(loc)!;
        const [a, b] = windowOf(req, loc.usize);
        return full.subarray(Number(a), Number(b));
      }
      if (loc.csize !== loc.usize) throw new VzError("body", "STORED entry's compressed and uncompressed sizes differ");
      const [a, b] = windowOf(req, loc.usize);
      return this.readBodyWindow(loc, a, b);
    }
    return this.resolveReference(loc, req);
  }

  async raw(key: string): Promise<Uint8Array | null> {
    const kb = Buffer.from(key, "utf8");
    if (!key.isWellFormed() || kb.length === 0) return null;
    if (kb.equals(SOURCES_BYTES)) return this.sourcesBody;
    if (kb.equals(INDEX_BYTES)) return this.indexBody;
    const loc = this.lookupChecked(kb);
    if (!loc) return null;
    const full = this.inflateBody(loc);
    if (full !== null) return full;
    return this.readBodyWindow(loc, 0n, loc.usize);
  }

  list(prefix: string): string[] {
    const pb = Buffer.from(prefix, "utf8");
    const out = new Map<string, Buffer>();
    const consider = (name: Buffer) => {
      if (isHidden(name)) return;
      if (name.length < pb.length || !name.subarray(0, pb.length).equals(pb)) return;
      out.set(name.toString("latin1"), name);
    };
    if (this.index === null) {
      for (const n of this.allNames) consider(n);
    } else {
      for (const p of this.index.pinned) consider(Buffer.from(p.key));
      const fk = this.firstKeys();
      const upper = prefixUpper(pb);
      for (let i = 0; i < fk.length; i++) {
        const lo = fk[i];
        const hi = i + 1 < fk.length ? fk[i + 1] : null;
        // Page range [lo, hi) intersects prefix range [pb, upper)?
        const maxLo = Buffer.compare(lo, pb) >= 0 ? lo : pb;
        let minHi: Buffer | null;
        if (hi === null) minHi = upper;
        else if (upper === null) minHi = hi;
        else minHi = Buffer.compare(hi, upper) <= 0 ? hi : upper;
        if (minHi !== null && Buffer.compare(maxLo, minHi) >= 0) continue;
        for (const r of this.pageRecords(i)) {
          if (r.name === null) continue;
          if (Buffer.compare(r.nameBytes, lo) < 0) continue;
          if (hi !== null && Buffer.compare(r.nameBytes, hi) >= 0) continue;
          consider(r.nameBytes);
        }
      }
    }
    return [...out.values()].sort(Buffer.compare).map((b) => b.toString("utf8"));
  }

  // ---------- references (§8.3) ----------

  private decodePayload(loc: Loc): Range[] {
    let parts: Range[];
    try {
      parts = loc.refId === EXTRA_RANGE ? [decodeRange(loc.payload!)] : decodeConcat(loc.payload!);
    } catch (e) {
      if (e instanceof MalformedError) throw new VzError("payload", `reference payload is malformed: ${e.message}`);
      throw e;
    }
    let total = 0n;
    for (let i = 0; i < parts.length; i++) {
      const r = parts[i];
      if (r.data !== null) {
        if (r.source !== 0 || r.offset !== 0n || r.length !== 0n) {
          throw new VzError("payload", `range ${i}: literal range with non-zero source, offset or length`);
        }
        total += BigInt(r.data.length);
      } else {
        if (r.source >= this.sources.length) {
          throw new VzError("payload", `range ${i}: source ${r.source} out of bounds (${this.sources.length} sources)`);
        }
        if (r.offset + r.length > U64_MAX) throw new VzError("payload", `range ${i}: offset + length exceeds 2^64-1`);
        total += r.length;
      }
    }
    if (total > U64_MAX) throw new VzError("payload", "reference size exceeds 2^64-1");
    return parts;
  }

  private async resolveReference(loc: Loc, req: Request): Promise<Uint8Array> {
    const parts = this.decodePayload(loc);
    const size = (r: Range) => (r.data !== null ? BigInt(r.data.length) : r.length);
    const n = parts.reduce((s, r) => s + size(r), 0n);
    const [a, b] = windowOf(req, n);
    const pieces: Piece[] = [];
    let pos = 0n;
    for (const r of parts) {
      const s = size(r);
      const lo = a > pos ? a : pos;
      const hi = b < pos + s ? b : pos + s;
      if (hi > lo) pieces.push({ range: r, lo: lo - pos, hi: hi - pos });
      pos += s;
    }
    const out: Uint8Array[] = [];
    let acc = 0n;
    for (const pc of pieces) {
      acc += pc.hi - pc.lo;
      if (acc > BigInt(MAX_REQUEST_BYTES)) {
        // Check resolvability of this piece first where that needs no I/O-heavy allocation.
        throw new VzError("request", `request larger than the ${MAX_REQUEST_BYTES}-byte limit`);
      }
      const r = pc.range;
      if (r.data !== null) {
        out.push(r.data.subarray(Number(pc.lo), Number(pc.hi)));
      } else {
        out.push(await this.readSource(r.source, r.offset + pc.lo, r.offset + pc.hi));
      }
    }
    return Buffer.concat(out);
  }

  /** Bytes [a, b) (a < b) of the source value of source `idx`. Throws resolution errors. */
  private async readSource(idx: number, a: bigint, b: bigint): Promise<Uint8Array> {
    const s = this.sources[idx];
    const k = s.kind!;
    if (k.type === "data") {
      if (BigInt(k.value.length) < b) throw new VzError("resolution", `data source ${idx} has ${k.value.length} bytes, range needs ${b}`);
      return k.value.subarray(Number(a), Number(b));
    }
    if (k.type === "key") {
      const kb = Buffer.from(k.value, "utf8");
      if (isFormatKey(kb)) throw new VzError("resolution", `key source ${idx} names a format entry`);
      let loc: Loc | null;
      try {
        loc = this.lookupChecked(kb);
        if (loc === null) throw new VzError("resolution", `key source ${idx} names missing key ${JSON.stringify(k.value)}`);
        if (loc.kind !== "bytes") throw new VzError("resolution", `key source ${idx} names a reference entry`);
        this.checkBodyBounds(loc);
        const full = this.inflateBody(loc);
        if (loc.usize < b) throw new VzError("resolution", `key source ${idx} has ${loc.usize} bytes, range needs ${b}`);
        if (full !== null) return full.subarray(Number(a), Number(b));
        return this.readBodyWindow(loc, a, b);
      } catch (e) {
        if (e instanceof VzError && e.cls !== "resolution" && e.cls !== "request") {
          throw new VzError("resolution", `key source ${idx}: ${e.message}`);
        }
        throw e;
      }
    }
    const pins: Pins = { size: s.size, etag: s.etag, modifiedNotAfter: s.modifiedNotAfter };
    return readUrlRange(this.baseUri, k.value, pins, a, b);
  }
}

/** The requested window [a, b) of a value of size n (§8.2). */
export function windowOf(req: Request, n: bigint): [bigint, bigint] {
  const min = (x: bigint, y: bigint) => (x < y ? x : y);
  switch (req.type) {
    case "whole":
      return [0n, n];
    case "range":
      return [min(req.start, n), min(req.end, n)];
    case "offset":
      return [min(req.start, n), n];
    case "suffix":
      return [n - req.count > 0n ? n - req.count : 0n, n];
  }
}

export function isStrongEtag(s: string): boolean {
  return /^"[\x21\x23-\x7e]*"$/.test(s);
}
