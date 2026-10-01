// vzip reader (spec §3, §4, §7, §8).

import * as fs from "node:fs";
import {
  EXTRA_CONCAT, EXTRA_RANGE, INDEX_KEY, MEMORY_LIMIT, SOURCES_KEY, U64_MAX,
  InflateError, compareBytes, inflateClean, isFormatKey, isHidden, isStrongEtag, startsWithBytes, utf8,
} from "./common.ts";
import { VzipError, fail } from "./errors.ts";
import {
  ProtoError, decodeCdIndex, decodeConcat, decodeRange, decodeSourceTable, decodeUtf8Strict,
  type CdIndex, type Range, type Source,
} from "./proto.ts";
import { UriMapError, baseUriForPath, fileUriToPath, parseUriReference, resolve, uriToString, type Uri } from "./uri.ts";
import { httpReadRange } from "./http.ts";

export type Kind = "bytes" | "reference" | "missing";

export type Request =
  | { type: "whole" }
  | { type: "range"; start: bigint; end: bigint }
  | { type: "offset"; start: bigint }
  | { type: "suffix"; count: bigint };

/** Random-access byte source for the archive file. */
interface Blob {
  size: bigint;
  read(off: bigint, len: number): Uint8Array;
  close(): void;
}

class FileBlob implements Blob {
  fd: number;
  size: bigint;
  constructor(p: string | Buffer) {
    this.fd = fs.openSync(p, "r");
    this.size = fs.fstatSync(this.fd, { bigint: true }).size;
  }
  read(off: bigint, len: number): Uint8Array {
    const buf = Buffer.alloc(len);
    let done = 0;
    while (done < len) {
      const n = fs.readSync(this.fd, buf, done, len - done, Number(off) + done);
      if (n === 0) throw new Error("unexpected end of file");
      done += n;
    }
    return buf;
  }
  close(): void {
    fs.closeSync(this.fd);
  }
}

function u16(b: Uint8Array, o: number): number {
  return b[o] | (b[o + 1] << 8);
}
function u32(b: Uint8Array, o: number): number {
  return (b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)) >>> 0;
}
function u64(b: Uint8Array, o: number): bigint {
  return BigInt(u32(b, o)) | (BigInt(u32(b, o + 4)) << 32n);
}

const SIG_CD = 0x02014b50;
const SIG_EOCD = 0x06054b50;
const SIG_Z64_EOCD = 0x06064b50;
const SIG_Z64_LOC = 0x07064b50;

/** Location of a body plus what is needed to decode it. */
type BodyLoc = { bodyOffset: bigint; csize: bigint; usize: bigint; method: number };

type Rec = {
  key: string;
  kind: "bytes" | "reference";
  entryError: string | null;
  loc: BodyLoc | null; // null when entryError prevents computing it
  payloadId: number;
  payload: Uint8Array | null;
};

/**
 * Parses a sequence of central directory records filling `buf` exactly.
 * Throws a plain Error describing the problem when it does not parse.
 * Records whose names are empty or invalid UTF-8 are dropped.
 */
function parseRecords(buf: Uint8Array): Rec[] {
  const out: Rec[] = [];
  let pos = 0;
  while (pos < buf.length) {
    if (pos + 46 > buf.length) throw new Error(`truncated central directory record at ${pos}`);
    if (u32(buf, pos) !== SIG_CD) throw new Error(`bad central directory signature at ${pos}`);
    const nameLen = u16(buf, pos + 28);
    const extraLen = u16(buf, pos + 30);
    const commentLen = u16(buf, pos + 32);
    const end = pos + 46 + nameLen + extraLen + commentLen;
    if (end > buf.length) throw new Error(`central directory record at ${pos} overruns its container`);
    const name = buf.subarray(pos + 46, pos + 46 + nameLen);
    const extra = buf.subarray(pos + 46 + nameLen, pos + 46 + nameLen + extraLen);
    const key = nameLen === 0 ? null : decodeUtf8Strict(name);
    if (key !== null && key !== "") {
      out.push(analyseRecord(key, buf.subarray(pos, pos + 46), nameLen, extra));
    }
    pos = end;
  }
  return out;
}

function analyseRecord(key: string, fixed: Uint8Array, nameLen: number, extra: Uint8Array): Rec {
  const flags = u16(fixed, 8);
  const method = u16(fixed, 10);
  const csize32 = u32(fixed, 20);
  const usize32 = u32(fixed, 24);
  const lho32 = u32(fixed, 42);
  const rec: Rec = { key, kind: "bytes", entryError: null, loc: null, payloadId: 0, payload: null };

  // Parse the extra field into blocks.
  const blocks: { id: number; data: Uint8Array }[] = [];
  let p = 0;
  let parsed = true;
  while (p < extra.length) {
    if (p + 4 > extra.length) { parsed = false; break; }
    const id = u16(extra, p);
    const sz = u16(extra, p + 2);
    if (p + 4 + sz > extra.length) { parsed = false; break; }
    blocks.push({ id, data: extra.subarray(p + 4, p + 4 + sz) });
    p += 4 + sz;
  }
  if (!parsed) {
    rec.entryError = "extra field does not parse";
    return rec;
  }
  const refs = blocks.filter((b) => b.id === EXTRA_RANGE || b.id === EXTRA_CONCAT);
  if (refs.length > 1) {
    rec.entryError = "more than one reference extra block";
    return rec;
  }
  if (refs.length === 1) {
    rec.kind = "reference";
    rec.payloadId = refs[0].id;
    rec.payload = refs[0].data;
  }
  if (flags & 1) {
    rec.entryError = "entry is encrypted (general purpose bit 0)";
    return rec;
  }
  if (method !== 0 && method !== 8) {
    rec.entryError = `unsupported compression method ${method}`;
    return rec;
  }
  if (rec.kind === "reference" && method === 8) {
    rec.entryError = "reference entry uses method 8";
    return rec;
  }
  let usize = BigInt(usize32);
  let csize = BigInt(csize32);
  let lho = BigInt(lho32);
  if (usize32 === 0xffffffff || csize32 === 0xffffffff || lho32 === 0xffffffff) {
    const z = blocks.filter((b) => b.id === 0x0001);
    if (z.length === 0) {
      rec.entryError = "field is 0xFFFFFFFF but there is no ZIP64 extra block";
      return rec;
    }
    if (z.length > 1) {
      rec.entryError = "more than one ZIP64 extra block";
      return rec;
    }
    const d = z[0].data;
    let q = 0;
    const take = (): bigint | null => {
      if (q + 8 > d.length) return null;
      const v = u64(d, q);
      q += 8;
      return v;
    };
    if (usize32 === 0xffffffff) { const v = take(); if (v === null) { rec.entryError = "ZIP64 block too short"; return rec; } usize = v; }
    if (csize32 === 0xffffffff) { const v = take(); if (v === null) { rec.entryError = "ZIP64 block too short"; return rec; } csize = v; }
    if (lho32 === 0xffffffff) { const v = take(); if (v === null) { rec.entryError = "ZIP64 block too short"; return rec; } lho = v; }
  }
  rec.loc = { bodyOffset: lho + 30n + BigInt(nameLen), csize, usize, method };
  return rec;
}

function windowOf(req: Request, n: bigint): [bigint, bigint] {
  const min = (a: bigint, b: bigint) => (a < b ? a : b);
  switch (req.type) {
    case "whole": return [0n, n];
    case "range": return [min(req.start, n), min(req.end, n)];
    case "offset": return [min(req.start, n), n];
    case "suffix": return [n - req.count > 0n ? n - req.count : 0n, n];
  }
}

type PageState = { firstKey: string; fk: Uint8Array; offset: bigint; length: bigint; recs?: Map<string, Rec>; error?: string };

type Located =
  | { type: "missing" }
  | { type: "format"; key: string }
  | { type: "rec"; rec: Rec };

export type OpenOptions = {
  /** Called to decide whether a resolved URL may be read; default allows all. */
  allowUrl?: (url: string) => boolean;
};

export class Archive {
  private blob: Blob;
  readonly baseUri: Uri;
  private sources: Source[] = [];
  private sourcesBody: Uint8Array = new Uint8Array();
  private indexBody: Uint8Array | null = null;
  private index: CdIndex | null = null;
  private cdOffset = 0n;
  private cdSize = 0n;
  private records: Map<string, Rec> | null = null; // unpaged
  private pages: PageState[] = [];
  private pinned = new Map<string, BodyLoc>();
  private opts: OpenOptions;

  private constructor(blob: Blob, baseUri: Uri, opts: OpenOptions) {
    this.blob = blob;
    this.baseUri = baseUri;
    this.opts = opts;
  }

  /** Opens an archive from a local path. Throws VzipError("archive") on failure. */
  static open(p: string, opts: OpenOptions = {}): Archive {
    let blob: Blob;
    try {
      blob = new FileBlob(p);
    } catch (e) {
      fail("archive", `cannot open archive: ${(e as Error).message}`);
    }
    const base = parseUriReference(baseUriForPath(p))!;
    const a = new Archive(blob, base, opts);
    try {
      a.init();
    } catch (e) {
      blob.close();
      if (e instanceof VzipError) throw e;
      fail("archive", (e as Error).message);
    }
    return a;
  }

  close(): void {
    this.blob.close();
  }

  private readAt(off: bigint, len: bigint): Uint8Array {
    return this.blob.read(off, Number(len));
  }

  private init(): void {
    const size = this.blob.size;
    const A = (m: string): never => fail("archive", m);
    // §3.4: locate the end of central directory record.
    let eocdPos = -1n;
    let commentLen = 0;
    if (size >= 60n) {
      const b = this.readAt(size - 60n, 22n);
      if (u32(b, 0) === SIG_EOCD && u16(b, 20) === 38) { eocdPos = size - 60n; commentLen = 38; }
    }
    if (eocdPos < 0n && size >= 44n) {
      const b = this.readAt(size - 44n, 22n);
      if (u32(b, 0) === SIG_EOCD && u16(b, 20) === 22) { eocdPos = size - 44n; commentLen = 22; }
    }
    if (eocdPos < 0n) A("not a vzip archive: no end of central directory record with a vzip comment");
    const eocd = this.readAt(eocdPos, BigInt(22 + commentLen));
    const comment = eocd.subarray(22);
    if (Buffer.from(comment.subarray(0, 5)).toString("latin1") !== "vzip/") A("not a vzip archive: comment lacks vzip/ magic");
    if (comment[5] !== 0x30) A(`unsupported vzip format version '${String.fromCharCode(comment[5])}'`);

    // §3.2: ZIP64.
    const entriesDisk = u16(eocd, 8);
    const entriesTotal = u16(eocd, 10);
    let cdSize = BigInt(u32(eocd, 12));
    let cdOffset = BigInt(u32(eocd, 16));
    if (entriesDisk === 0xffff || entriesTotal === 0xffff || cdSize === 0xffffffffn || cdOffset === 0xffffffffn) {
      if (eocdPos < 20n) A("ZIP64 end of central directory locator missing");
      const loc = this.readAt(eocdPos - 20n, 20n);
      if (u32(loc, 0) !== SIG_Z64_LOC) A("ZIP64 end of central directory locator missing");
      const recOff = u64(loc, 8);
      if (recOff + 56n > size) A("ZIP64 end of central directory record lies outside the file");
      const z = this.readAt(recOff, 56n);
      if (u32(z, 0) !== SIG_Z64_EOCD) A("ZIP64 end of central directory record has a bad signature");
      if (u64(z, 4) !== 44n) A("ZIP64 end of central directory record size field is not 44");
      cdSize = u64(z, 40);
      cdOffset = u64(z, 48);
    }
    if (cdOffset + cdSize > size) A("central directory lies outside the file");
    this.cdOffset = cdOffset;
    this.cdSize = cdSize;

    // Format entries via the comment.
    const sourcesOffset = u64(comment, 6);
    const sourcesSize = u64(comment, 14);
    this.sourcesBody = this.readFormatBody(sourcesOffset, sourcesSize, SOURCES_KEY);
    try {
      this.sources = decodeSourceTable(this.sourcesBody);
    } catch (e) {
      if (e instanceof ProtoError) A(`source table is malformed: ${e.message}`);
      throw e;
    }
    this.sources.forEach((s, i) => {
      if (s.kind === null) A(`source ${i} has no kind`);
      if (s.kind === "url" && s.url === "") A(`source ${i} has an empty url`);
      if (s.kind !== "url" && (s.size !== null || s.etag !== null || s.modifiedNotAfter !== null))
        A(`source ${i} has a pin on a ${s.kind} source`);
      if (s.etag !== null && !isStrongEtag(s.etag)) A(`source ${i} etag pin is not a strong entity tag`);
    });

    if (commentLen === 38) {
      const indexOffset = u64(comment, 22);
      const indexSize = u64(comment, 30);
      this.indexBody = this.readFormatBody(indexOffset, indexSize, INDEX_KEY);
      let idx: CdIndex;
      try {
        idx = decodeCdIndex(this.indexBody);
      } catch (e) {
        if (e instanceof ProtoError) A(`page index is malformed: ${e.message}`);
        throw e;
      }
      this.index = idx;
      let expectOff = 0n;
      let prev: Uint8Array | null = null;
      for (const [i, pg] of idx.pages.entries()) {
        if (pg.length === 0n) A(`page ${i} has length 0`);
        if (pg.offset !== expectOff) A(`page ${i} is not contiguous`);
        if (pg.offset + pg.length > cdSize) A(`page ${i} lies outside the central directory`);
        if (pg.firstKey === "") A(`page ${i} has an empty first_key`);
        const fk = utf8(pg.firstKey);
        if (prev !== null && compareBytes(prev, fk) >= 0) A(`page first_key values do not strictly increase at page ${i}`);
        prev = fk;
        expectOff = pg.offset + pg.length;
        this.pages.push({ firstKey: pg.firstKey, fk, offset: pg.offset, length: pg.length });
      }
      for (const pn of idx.pinned) {
        if (pn.key === "") A("a pinned key is empty");
        if (isFormatKey(pn.key)) A(`pinned key ${pn.key} is a format entry`);
        if (this.pinned.has(pn.key)) A(`pinned key ${pn.key} is listed twice`);
        if (pn.method !== 0 && pn.method !== 8) A(`pinned key ${pn.key} has method ${pn.method}`);
        if (pn.dataOffset + pn.csize > size) A(`pinned body of ${pn.key} lies outside the file`);
        this.pinned.set(pn.key, { bodyOffset: pn.dataOffset, csize: pn.csize, usize: pn.size, method: pn.method });
      }
    } else {
      // Unpaged: the whole central directory is parsed at open.
      if (cdSize > BigInt(MEMORY_LIMIT)) A("central directory exceeds this reader's memory limit");
      const cd = this.readAt(cdOffset, cdSize);
      let recs: Rec[];
      try {
        recs = parseRecords(cd);
      } catch (e) {
        A(`central directory does not parse: ${(e as Error).message}`);
      }
      this.records = new Map();
      for (const r of recs!) {
        if (r.key === INDEX_KEY) A("archive without a page index has an __vz__/index entry");
        if (!this.records.has(r.key)) this.records.set(r.key, r);
      }
    }
  }

  private readFormatBody(off: bigint, csize: bigint, name: string): Uint8Array {
    if (off + csize > this.blob.size) fail("archive", `${name} body lies outside the file`);
    if (csize > BigInt(MEMORY_LIMIT)) fail("archive", `${name} exceeds this reader's memory limit`);
    const body = this.readAt(off, csize);
    try {
      return inflateClean(body, null);
    } catch (e) {
      fail("archive", `${name} does not inflate cleanly: ${(e as Error).message}`);
    }
  }

  // ---------- lookup ----------

  private pageFor(key: Uint8Array): number {
    // last page with first_key <= key
    let lo = 0;
    let hi = this.pages.length - 1;
    let ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (compareBytes(this.pages[mid].fk, key) <= 0) {
        ans = mid;
        lo = mid + 1;
      } else hi = mid - 1;
    }
    return ans;
  }

  /** Loads a page; throws VzipError("entry") if it cannot be parsed. */
  private loadPage(i: number): Map<string, Rec> {
    const pg = this.pages[i];
    if (pg.recs) return pg.recs;
    if (pg.error) fail("entry", pg.error);
    if (pg.length > BigInt(MEMORY_LIMIT)) fail("request", "page exceeds this reader's memory limit");
    const buf = this.readAt(this.cdOffset + pg.offset, pg.length);
    try {
      const m = new Map<string, Rec>();
      for (const r of parseRecords(buf)) if (!m.has(r.key)) m.set(r.key, r);
      pg.recs = m;
      return m;
    } catch (e) {
      pg.error = `page ${i} cannot be parsed: ${(e as Error).message}`;
      fail("entry", pg.error);
    }
  }

  /** Step 3 of §8.4: finds the key's record. Throws entry errors. */
  private locate(key: string): Located {
    if (isFormatKey(key)) {
      if (key === INDEX_KEY && this.index === null) return { type: "missing" };
      return { type: "format", key };
    }
    if (this.records !== null) {
      const r = this.records.get(key);
      return r ? { type: "rec", rec: r } : { type: "missing" };
    }
    const pin = this.pinned.get(key);
    if (pin) return { type: "rec", rec: { key, kind: "bytes", entryError: null, loc: pin, payloadId: 0, payload: null } };
    const i = this.pageFor(utf8(key));
    if (i < 0) return { type: "missing" };
    const r = this.loadPage(i).get(key);
    return r ? { type: "rec", rec: r } : { type: "missing" };
  }

  private locateChecked(key: string): Located {
    const l = this.locate(key);
    if (l.type === "rec" && l.rec.entryError !== null) fail("entry", `${key}: ${l.rec.entryError}`);
    return l;
  }

  // ---------- bodies ----------

  /** Reads [s, e) of the decoded body. Throws body / request errors. */
  private readBody(key: string, loc: BodyLoc, s: bigint, e: bigint): Uint8Array {
    if (loc.bodyOffset + loc.csize > this.blob.size) fail("body", `${key}: body lies outside the file`);
    if (loc.method === 0) {
      if (loc.csize !== loc.usize) fail("body", `${key}: STORED entry has compressed size ${loc.csize} != uncompressed size ${loc.usize}`);
      if (e - s > BigInt(MEMORY_LIMIT)) fail("request", `${key}: request exceeds memory limit`);
      return this.readAt(loc.bodyOffset + s, e - s);
    }
    if (loc.usize > BigInt(MEMORY_LIMIT) || loc.csize > BigInt(MEMORY_LIMIT))
      fail("request", `${key}: DEFLATE body exceeds this reader's memory limit`);
    const body = this.readAt(loc.bodyOffset, loc.csize);
    let out: Buffer;
    try {
      out = inflateClean(body, loc.usize);
    } catch (err) {
      if (err instanceof InflateError) fail("body", `${key}: ${err.message}`);
      throw err;
    }
    return out.subarray(Number(s), Number(e));
  }

  private formatBody(key: string): Uint8Array {
    return key === SOURCES_KEY ? this.sourcesBody : this.indexBody!;
  }

  // ---------- operations (§8.2) ----------

  classify(key: string): Kind {
    if (isHidden(key)) return "missing";
    const l = this.locateChecked(key);
    if (l.type !== "rec") return "missing";
    return l.rec.kind;
  }

  raw(key: string): Uint8Array | null {
    const l = this.locateChecked(key);
    if (l.type === "missing") return null;
    if (l.type === "format") return this.formatBody(l.key);
    const loc = l.rec.loc!;
    return this.readBody(key, loc, 0n, loc.usize);
  }

  async get(key: string, req: Request = { type: "whole" }): Promise<Uint8Array | null> {
    if (req.type === "range" && req.start > req.end) fail("request", `range start ${req.start} > end ${req.end}`);
    if (isHidden(key)) return null;
    const l = this.locateChecked(key);
    if (l.type !== "rec") return null;
    const rec = l.rec;
    if (rec.kind === "bytes") {
      const [s, e] = windowOf(req, rec.loc!.usize);
      return this.readBody(key, rec.loc!, s, e);
    }
    const parts = this.decodePayload(rec);
    let total = 0n;
    for (const p of parts) total += partSize(p);
    const [a, b] = windowOf(req, total);
    if (b - a > BigInt(MEMORY_LIMIT)) fail("request", `${key}: request of ${b - a} bytes exceeds memory limit`);
    const out: Uint8Array[] = [];
    let ps = 0n;
    for (const p of parts) {
      const sz = partSize(p);
      const pe = ps + sz;
      const lo = a > ps ? a : ps;
      const hi = b < pe ? b : pe;
      if (lo < hi) {
        const i = lo - ps;
        const j = hi - ps;
        if (p.data !== null) out.push(p.data.subarray(Number(i), Number(j)));
        else out.push(await this.readSource(p.source, p.offset + i, p.offset + j));
      }
      ps = pe;
    }
    return Buffer.concat(out);
  }

  /** Decodes and validates a reference payload; throws payload errors. */
  private decodePayload(rec: Rec): Range[] {
    let parts: Range[];
    try {
      parts = rec.payloadId === EXTRA_RANGE ? [decodeRange(rec.payload!)] : decodeConcat(rec.payload!);
    } catch (e) {
      if (e instanceof ProtoError) fail("payload", `${rec.key}: malformed payload: ${e.message}`);
      throw e;
    }
    let total = 0n;
    for (const [i, p] of parts.entries()) {
      if (p.data !== null) {
        if (p.source !== 0 || p.offset !== 0n || p.length !== 0n)
          fail("payload", `${rec.key}: literal range ${i} has non-zero source/offset/length`);
      } else {
        if (p.source >= this.sources.length) fail("payload", `${rec.key}: range ${i} names source ${p.source} of ${this.sources.length}`);
        if (p.offset + p.length > U64_MAX) fail("payload", `${rec.key}: range ${i} end exceeds 2^64-1`);
      }
      total += partSize(p);
    }
    if (total > U64_MAX) fail("payload", `${rec.key}: total size exceeds 2^64-1`);
    return parts;
  }

  // ---------- sources (§6) ----------

  /** Reads [from, to) (to > from) of a source value. Throws resolution errors. */
  private async readSource(idx: number, from: bigint, to: bigint): Promise<Uint8Array> {
    const s = this.sources[idx];
    const R = (m: string): never => fail("resolution", `source ${idx}: ${m}`);
    if (s.kind === "data") {
      if (BigInt(s.data!.length) < to) R(`data source has ${s.data!.length} bytes, need ${to}`);
      return s.data!.subarray(Number(from), Number(to));
    }
    if (s.kind === "key") {
      const key = s.key!;
      if (isFormatKey(key)) R(`key source names format entry ${key}`);
      let l: Located;
      try {
        l = this.locateChecked(key);
      } catch (e) {
        if (e instanceof VzipError && e.cls === "entry") R(`key source ${key}: ${e.message}`);
        throw e;
      }
      if (l.type !== "rec") R(`key source ${key} is missing`);
      const rec = (l as { rec: Rec }).rec;
      if (rec.kind !== "bytes") R(`key source ${key} is a reference entry`);
      if (rec.loc!.usize < to) R(`key source ${key} has ${rec.loc!.usize} bytes, need ${to}`);
      try {
        return this.readBody(key, rec.loc!, from, to);
      } catch (e) {
        if (e instanceof VzipError && e.cls === "body") R(`key source ${key}: ${e.message}`);
        throw e;
      }
    }
    // url
    const ref = parseUriReference(s.url!);
    if (ref === null) R(`'${s.url}' is not a valid URI reference`);
    const target = resolve(this.baseUri, ref!);
    const targetStr = uriToString(target);
    if (this.opts.allowUrl && !this.opts.allowUrl(targetStr)) R(`${targetStr} is not allowed by policy`);
    const scheme = target.scheme!.toLowerCase();
    if (scheme === "file") return this.readFile(idx, s, target, from, to);
    if (scheme === "http" || scheme === "https") {
      try {
        return await httpReadRange(target, from, to, { size: s.size, etag: s.etag, modifiedNotAfter: s.modifiedNotAfter });
      } catch (e) {
        if (e instanceof VzipError) throw e;
        R(`${targetStr}: ${(e as Error).message}`);
      }
    }
    return R(`unsupported URL scheme '${scheme}'`);
  }

  private readFile(idx: number, s: Source, target: Uri, from: bigint, to: bigint): Uint8Array {
    const R = (m: string): never => fail("resolution", `source ${idx}: ${m}`);
    let p: Buffer;
    try {
      p = fileUriToPath(target);
    } catch (e) {
      if (e instanceof UriMapError) R(e.message);
      throw e;
    }
    if (s.etag !== null) R("etag pin cannot be checked for file: URLs");
    let fd: number;
    try {
      fd = fs.openSync(p!, "r");
    } catch (e) {
      return R(`cannot open ${uriToString(target)}: ${(e as Error).message}`);
    }
    try {
      const st = fs.fstatSync(fd, { bigint: true });
      if (!st.isFile()) R(`${uriToString(target)} is not a regular file`);
      if (s.size !== null && st.size !== s.size) R(`size ${st.size} does not match pin ${s.size}`);
      if (s.modifiedNotAfter !== null) {
        const ns = st.mtimeNs;
        let secs = ns / 1_000_000_000n;
        if (ns < 0n && secs * 1_000_000_000n !== ns) secs -= 1n; // floor
        if (secs > s.modifiedNotAfter) R(`modification time ${secs} is after pin ${s.modifiedNotAfter}`);
      }
      if (st.size < to) R(`${uriToString(target)} has ${st.size} bytes, need ${to}`);
      const len = Number(to - from);
      const buf = Buffer.alloc(len);
      let done = 0;
      while (done < len) {
        const n = fs.readSync(fd, buf, done, len - done, Number(from) + done);
        if (n === 0) R("unexpected end of file");
        done += n;
      }
      return buf;
    } finally {
      fs.closeSync(fd);
    }
  }

  list(prefix: string): string[] {
    const pb = utf8(prefix);
    const found = new Map<string, Uint8Array>();
    const consider = (k: string) => {
      if (isHidden(k)) return;
      const kb = utf8(k);
      if (startsWithBytes(kb, pb)) found.set(k, kb);
    };
    if (this.records !== null) {
      for (const k of this.records.keys()) consider(k);
    } else {
      for (const k of this.pinned.keys()) consider(k);
      for (let i = 0; i < this.pages.length; i++) {
        const lo = this.pages[i].fk;
        const hi = i + 1 < this.pages.length ? this.pages[i + 1].fk : null;
        let intersects: boolean;
        if (compareBytes(lo, pb) <= 0) intersects = hi === null || compareBytes(pb, hi) < 0;
        else intersects = startsWithBytes(lo, pb);
        if (!intersects) continue;
        const recs = this.loadPage(i);
        for (const k of recs.keys()) {
          const kb = utf8(k);
          if (compareBytes(kb, lo) < 0) continue;
          if (hi !== null && compareBytes(kb, hi) >= 0) continue;
          consider(k);
        }
      }
    }
    return [...found.entries()].sort((x, y) => compareBytes(x[1], y[1])).map((x) => x[0]);
  }

  /** Number of sources (for tests/diagnostics). */
  get sourceCount(): number {
    return this.sources.length;
  }
}

function partSize(p: Range): bigint {
  return p.data !== null ? BigInt(p.data.length) : p.length;
}

export { VzipError };
