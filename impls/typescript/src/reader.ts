// vzip reader (spec §3, §4, §6, §7, §8).

import fs from "node:fs";
import zlib from "node:zlib";
import {
  archiveErr,
  bodyErr,
  entryErr,
  MalformedError,
  payloadErr,
  requestErr,
  resolutionErr,
  VzError,
} from "./errors.ts";
import { httpReadRange, isStrongEtag } from "./http.ts";
import {
  decodeCdIndex,
  decodeConcat,
  decodeRange,
  decodeSourceTable,
  isValidUtf8,
  U64_MAX,
  type CdIndexMsg,
  type PinnedMsg,
  type RangeMsg,
  type SourceMsg,
} from "./proto.ts";
import { fileBaseUri, fileUriToPath, parseUriReference, recompose, resolve, type Uri } from "./uri.ts";

/** Documented resource limit: the most bytes a single request may produce or inflate (1 GiB). */
export const MAX_REQUEST_BYTES = 1n << 30n;

export const RESERVED_PREFIX = "__vz__/";
export const SOURCES_KEY = "__vz__/sources";
export const INDEX_KEY = "__vz__/index";
const ID_RANGE = 0x7a76;
const ID_CONCAT = 0x7a77;
const ID_ZIP64 = 0x0001;
const MAX_PAYLOAD = 65519;

const SIG_EOCD = 0x06054b50;
const SIG_LOC64 = 0x07064b50;
const SIG_EOCD64 = 0x06064b50;
const SIG_CDR = 0x02014b50;

export type Request =
  | { kind: "whole" }
  | { kind: "range"; start: bigint; end: bigint }
  | { kind: "offset"; start: bigint }
  | { kind: "suffix"; count: bigint };

export type Kind = "bytes" | "reference" | "missing";

/** A central directory record, minimally parsed. Names are latin1 byte strings. */
type Rec = {
  name: string;
  flags: number;
  method: number;
  csize: number;
  usize: number;
  lho: number;
  extra: Buffer;
};

/** Where an entry's data is and how to read it. */
type EntryInfo =
  | { kind: "bytes"; bodyOffset: bigint; csize: bigint; usize: bigint; method: number }
  | { kind: "reference"; bodyOffset: bigint; csize: bigint; usize: bigint; payloadId: number; payload: Buffer };

const min = (a: bigint, b: bigint) => (a < b ? a : b);
const max = (a: bigint, b: bigint) => (a > b ? a : b);

/** Converts a JS string to a latin1 byte string of its UTF-8, or null if not well-formed. */
export function toByteString(s: string): string | null {
  if (!s.isWellFormed()) return null;
  return Buffer.from(s, "utf8").toString("latin1");
}

export function fromByteString(s: string): string {
  return Buffer.from(s, "latin1").toString("utf8");
}

function cmp(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

/** Computes the window [a, b) of a value of size n. */
export function window(req: Request, n: bigint): [bigint, bigint] {
  switch (req.kind) {
    case "whole":
      return [0n, n];
    case "range":
      return [min(req.start, n), min(req.end, n)];
    case "offset":
      return [min(req.start, n), n];
    case "suffix":
      return [max(n - req.count, 0n), n];
  }
}

class FileReader {
  fd: number;
  size: bigint;
  constructor(path: string) {
    this.fd = fs.openSync(path, "r");
    const st = fs.fstatSync(this.fd);
    if (!st.isFile()) {
      fs.closeSync(this.fd);
      throw new Error("not a regular file");
    }
    this.size = BigInt(st.size);
  }
  read(off: bigint, len: bigint): Buffer {
    const n = Number(len);
    const buf = Buffer.alloc(n);
    let got = 0;
    while (got < n) {
      const r = fs.readSync(this.fd, buf, got, n - got, Number(off) + got);
      if (r === 0) throw new Error("unexpected end of file");
      got += r;
    }
    return buf;
  }
  within(off: bigint, len: bigint): boolean {
    return off >= 0n && len >= 0n && off + len <= this.size;
  }
  close() {
    fs.closeSync(this.fd);
  }
}

/** Inflates a raw DEFLATE body; it must end exactly at the end of `body`. */
function inflateClean(body: Buffer, expected: bigint | null, limit: bigint): Buffer {
  const maxOut = expected !== null ? Number(expected) + 1 : Number(limit) + 1;
  let res: { buffer: Buffer; engine: zlib.InflateRaw };
  try {
    res = zlib.inflateRawSync(body, { info: true, maxOutputLength: Math.max(1, maxOut) }) as unknown as {
      buffer: Buffer;
      engine: zlib.InflateRaw;
    };
  } catch (e) {
    const err = e as NodeJS.ErrnoException;
    if (err.code === "ERR_BUFFER_TOO_LARGE" && expected === null)
      throw requestErr(`inflated body exceeds resource limit of ${limit} bytes`);
    throw bodyErr(`DEFLATE body does not inflate cleanly: ${err.message}`);
  }
  if (res.engine.bytesWritten !== body.length)
    throw bodyErr(`DEFLATE stream ends before the end of the body (${res.engine.bytesWritten} of ${body.length} bytes)`);
  if (expected !== null && BigInt(res.buffer.length) !== expected)
    throw bodyErr(`DEFLATE body inflates to ${res.buffer.length} bytes, expected ${expected}`);
  return res.buffer;
}

function parseRecords(buf: Buffer): Rec[] | string {
  const out: Rec[] = [];
  let p = 0;
  while (p < buf.length) {
    if (p + 46 > buf.length) return `truncated central directory record at ${p}`;
    if (buf.readUInt32LE(p) !== SIG_CDR) return `bad central directory record signature at ${p}`;
    const nameLen = buf.readUInt16LE(p + 28);
    const extraLen = buf.readUInt16LE(p + 30);
    const commentLen = buf.readUInt16LE(p + 32);
    const end = p + 46 + nameLen + extraLen + commentLen;
    if (end > buf.length) return `central directory record at ${p} extends past its region`;
    out.push({
      name: buf.subarray(p + 46, p + 46 + nameLen).toString("latin1"),
      flags: buf.readUInt16LE(p + 8),
      method: buf.readUInt16LE(p + 10),
      csize: buf.readUInt32LE(p + 20),
      usize: buf.readUInt32LE(p + 24),
      lho: buf.readUInt32LE(p + 42),
      extra: buf.subarray(p + 46 + nameLen, p + 46 + nameLen + extraLen),
    });
    p = end;
  }
  return out;
}

function validName(name: string): boolean {
  return name.length > 0 && isValidUtf8(Buffer.from(name, "latin1"));
}

/** Classifies a record and checks its entry errors (§4.1, §3.2, §8.4). */
function analyze(rec: Rec): EntryInfo {
  const blocks: { id: number; data: Buffer }[] = [];
  let p = 0;
  const ex = rec.extra;
  while (p < ex.length) {
    if (p + 4 > ex.length) throw entryErr("extra field does not parse");
    const id = ex.readUInt16LE(p);
    const sz = ex.readUInt16LE(p + 2);
    if (p + 4 + sz > ex.length) throw entryErr("extra field does not parse");
    blocks.push({ id, data: ex.subarray(p + 4, p + 4 + sz) });
    p += 4 + sz;
  }
  const refs = blocks.filter((b) => b.id === ID_RANGE || b.id === ID_CONCAT);
  if (refs.length > 1) throw entryErr("more than one reference extra block");
  const z64 = blocks.filter((b) => b.id === ID_ZIP64);
  if (z64.length > 1) throw entryErr("more than one ZIP64 extra block");
  // §3.2: a large entry (both size fields all ones) has its sizes first in the
  // ZIP64 block, then the offset if its field is all ones.
  const csAll = rec.csize === 0xffffffff, usAll = rec.usize === 0xffffffff;
  if (csAll !== usAll) throw entryErr("exactly one size field is 0xFFFFFFFF");
  const large = csAll;
  const offAll = rec.lho === 0xffffffff;
  const need = (large ? 16 : 0) + (offAll ? 8 : 0);
  if (need > 0 && (z64.length === 0 || z64[0].data.length < need))
    throw entryErr(`ZIP64 extra block missing or shorter than ${need} bytes`);
  let usize = BigInt(rec.usize), csize = BigInt(rec.csize), lho = BigInt(rec.lho);
  if (large) {
    usize = z64[0].data.readBigUInt64LE(0);
    csize = z64[0].data.readBigUInt64LE(8);
  }
  if (offAll) lho = z64[0].data.readBigUInt64LE(large ? 16 : 0);
  if (rec.method !== 0 && rec.method !== 8) throw entryErr(`unsupported compression method ${rec.method}`);
  if (rec.flags & 1) throw entryErr("entry is encrypted");
  // §3.1 rule 4: a large entry's local header has a 20-byte ZIP64 extra field.
  const bodyOffset = lho + 30n + BigInt(Buffer.byteLength(rec.name, "latin1")) + (large ? 20n : 0n);
  if (large && rec.method !== 0) throw entryErr(`large entry uses method ${rec.method}`);
  if (refs.length === 1) {
    if (rec.method === 8) throw entryErr("reference entry uses method 8");
    if (large) throw entryErr("reference entry is large");
    return { kind: "reference", bodyOffset, csize, usize, payloadId: refs[0].id, payload: refs[0].data };
  }
  return { kind: "bytes", bodyOffset, csize, usize, method: rec.method };
}

type PageState = { lo: string; hi: string | null; offset: bigint; length: bigint; recs?: Map<string, Rec> | string };

export class Archive {
  private f: FileReader;
  readonly baseUri: Uri;
  private sources: SourceMsg[] = [];
  private sourcesRaw!: Buffer;
  private indexRaw: Buffer | null = null;
  private cdOffset = 0n;
  private cdSize = 0n;
  private paged = false;
  private records = new Map<string, Rec>(); // unpaged
  private pages: PageState[] = [];
  private pinned = new Map<string, PinnedMsg>();

  private constructor(f: FileReader, baseUri: Uri) {
    this.f = f;
    this.baseUri = baseUri;
  }

  /** Opens an archive from a local path. Throws VzError("archive") on failure. */
  static open(path: string): Archive {
    let f: FileReader;
    try {
      f = new FileReader(path);
    } catch (e) {
      throw archiveErr(`cannot open ${path}: ${(e as Error).message}`);
    }
    const base = parseUriReference(fileBaseUri(path))!;
    const a = new Archive(f, base);
    try {
      a.init();
    } catch (e) {
      f.close();
      if (e instanceof VzError) throw e;
      throw archiveErr((e as Error).message);
    }
    return a;
  }

  close() {
    this.f.close();
  }

  private init() {
    const f = this.f;
    const fsz = f.size;
    let eocdOff = -1n;
    let comment: Buffer | null = null;
    if (fsz >= 60n) {
      const b = f.read(fsz - 60n, 22n);
      if (b.readUInt32LE(0) === SIG_EOCD && b.readUInt16LE(20) === 38) {
        eocdOff = fsz - 60n;
        comment = f.read(fsz - 38n, 38n);
      }
    }
    if (comment === null && fsz >= 44n) {
      const b = f.read(fsz - 44n, 22n);
      if (b.readUInt32LE(0) === SIG_EOCD && b.readUInt16LE(20) === 22) {
        eocdOff = fsz - 44n;
        comment = f.read(fsz - 22n, 22n);
      }
    }
    if (comment === null) throw archiveErr("not a vzip archive: no end of central directory record with a vzip comment");
    if (comment.subarray(0, 5).toString("latin1") !== "vzip/") throw archiveErr("not a vzip archive: bad magic");
    const ver = String.fromCharCode(comment[5]);
    if (ver !== "0") throw archiveErr(`unsupported vzip format version ${JSON.stringify(ver)}`);
    this.paged = comment.length === 38;

    // §3.2: the directory's size and offset always come from the zip64 record; the end
    // record's own counts, size and offset are ignored.
    if (eocdOff < 20n) throw archiveErr("zip64 locator missing");
    const loc = f.read(eocdOff - 20n, 20n);
    if (loc.readUInt32LE(0) !== SIG_LOC64) throw archiveErr("zip64 locator missing");
    const recOff = loc.readBigUInt64LE(8);
    if (!f.within(recOff, 56n)) throw archiveErr("zip64 end of central directory record outside the file");
    const r = f.read(recOff, 56n);
    if (r.readUInt32LE(0) !== SIG_EOCD64) throw archiveErr("bad zip64 end of central directory signature");
    if (r.readBigUInt64LE(4) !== 44n) throw archiveErr("zip64 end of central directory record size is not 44");
    const cdSize = r.readBigUInt64LE(40);
    const cdOffset = r.readBigUInt64LE(48);
    if (!f.within(cdOffset, cdSize)) throw archiveErr("central directory lies outside the file");
    this.cdOffset = cdOffset;
    this.cdSize = cdSize;

    // Format entries.
    const sOff = comment.readBigUInt64LE(6);
    const sSize = comment.readBigUInt64LE(14);
    this.sourcesRaw = this.readFormatEntry(sOff, sSize, SOURCES_KEY);
    try {
      this.sources = decodeSourceTable(this.sourcesRaw);
    } catch (e) {
      throw archiveErr(`source table is malformed: ${(e as Error).message}`);
    }
    this.sources.forEach((s, i) => {
      if (s.kind === null) throw archiveErr(`source ${i} has no kind`);
      if (s.kind === "url" && s.url === "") throw archiveErr(`source ${i}: empty url`);
      if (s.kind === "key" && s.key === "") throw archiveErr(`source ${i}: empty key`);
      if (s.kind !== "url" && (s.size !== null || s.etag !== null || s.modifiedNotAfter !== null))
        throw archiveErr(`source ${i}: pin on a ${s.kind} source`);
      if (s.etag !== null && !isStrongEtag(s.etag)) throw archiveErr(`source ${i}: etag pin is not a strong entity tag`);
    });

    if (this.paged) {
      const iOff = comment.readBigUInt64LE(22);
      const iSize = comment.readBigUInt64LE(30);
      this.indexRaw = this.readFormatEntry(iOff, iSize, INDEX_KEY);
      let idx: CdIndexMsg;
      try {
        idx = decodeCdIndex(this.indexRaw);
      } catch (e) {
        throw archiveErr(`page index is malformed: ${(e as Error).message}`);
      }
      let expectOff = 0n;
      idx.pages.forEach((p, i) => {
        if (p.length === 0n) throw archiveErr(`page index is malformed: page ${i} has length 0`);
        if (p.offset !== expectOff) throw archiveErr(`page index is malformed: page ${i} is not contiguous`);
        if (p.offset + p.length > cdSize) throw archiveErr(`page index is malformed: page ${i} lies outside the central directory`);
        if (p.firstKey === "") throw archiveErr(`page index is malformed: page ${i} has an empty first_key`);
        if (i > 0 && !(idx.pages[i - 1].firstKey < p.firstKey))
          throw archiveErr("page index is malformed: first_key values do not strictly increase");
        expectOff = p.offset + p.length;
      });
      for (const p of idx.pinned) {
        if (p.key === "") throw archiveErr("page index is malformed: empty pinned key");
        if (this.pinned.has(p.key)) throw archiveErr("page index is malformed: pinned key listed twice");
        if (p.key === SOURCES_KEY || p.key === INDEX_KEY) throw archiveErr("page index is malformed: pinned format entry");
        if (p.method !== 0n && p.method !== 8n) throw archiveErr("page index is malformed: pinned method not 0 or 8");
        if (!f.within(p.dataOffset, p.csize)) throw archiveErr("page index is malformed: pinned body outside the file");
        this.pinned.set(p.key, p);
      }
      this.pages = idx.pages.map((p, i) => ({
        lo: p.firstKey,
        hi: i + 1 < idx.pages.length ? idx.pages[i + 1].firstKey : null,
        offset: p.offset,
        length: p.length,
      }));
    } else {
      const cd = f.read(cdOffset, cdSize);
      const recs = parseRecords(cd);
      if (typeof recs === "string") throw archiveErr(`central directory does not parse: ${recs}`);
      for (const r of recs) {
        if (!validName(r.name)) continue;
        if (r.name === INDEX_KEY) throw archiveErr("archive without a page index has an __vz__/index entry");
        if (!this.records.has(r.name)) this.records.set(r.name, r);
      }
    }
  }

  private readFormatEntry(off: bigint, size: bigint, name: string): Buffer {
    if (!this.f.within(off, size)) throw archiveErr(`${name} body lies outside the file`);
    try {
      return inflateClean(this.f.read(off, size), null, MAX_REQUEST_BYTES);
    } catch (e) {
      throw archiveErr(`${name}: ${(e as Error).message}`);
    }
  }

  // ------------------------------------------------------------ lookup

  /** Returns the record for a key (byte string), or null if missing. Throws entry errors. */
  private lookup(key: string): EntryInfo | null {
    if (!this.paged) {
      const r = this.records.get(key);
      return r ? analyze(r) : null;
    }
    const pin = this.pinned.get(key);
    if (pin) {
      return { kind: "bytes", bodyOffset: pin.dataOffset, csize: pin.csize, usize: pin.size, method: Number(pin.method) };
    }
    // last page with first_key <= key
    let lo = 0, hi = this.pages.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.pages[mid].lo <= key) lo = mid + 1;
      else hi = mid;
    }
    if (lo === 0) return null;
    const recs = this.pageRecords(this.pages[lo - 1]);
    const r = recs.get(key);
    return r ? analyze(r) : null;
  }

  private pageRecords(p: PageState): Map<string, Rec> {
    if (p.recs === undefined) {
      const buf = this.f.read(this.cdOffset + p.offset, p.length);
      const recs = parseRecords(buf);
      if (typeof recs === "string") p.recs = `page starting at ${JSON.stringify(fromByteString(p.lo))} cannot be parsed: ${recs}`;
      else {
        const m = new Map<string, Rec>();
        for (const r of recs) if (validName(r.name) && !m.has(r.name)) m.set(r.name, r);
        p.recs = m;
      }
    }
    if (typeof p.recs === "string") throw entryErr(p.recs);
    return p.recs;
  }

  // ------------------------------------------------------------ bodies

  /** Reads window [a, b) of a bytes entry's value. */
  private readBytesValue(e: EntryInfo & { kind: "bytes" }, a: bigint | null, b: bigint | null): Buffer {
    if (!this.f.within(e.bodyOffset, e.csize)) throw bodyErr("entry body lies outside the file");
    if (e.method === 0) {
      if (e.csize !== e.usize) throw bodyErr("STORED entry's compressed and uncompressed sizes differ");
      const lo = a ?? 0n, hi = b ?? e.usize;
      if (hi - lo > MAX_REQUEST_BYTES) throw requestErr("request exceeds resource limit");
      return this.f.read(e.bodyOffset + lo, hi - lo);
    }
    if (e.usize > MAX_REQUEST_BYTES) throw requestErr("entry's uncompressed size exceeds resource limit");
    const v = inflateClean(this.f.read(e.bodyOffset, e.csize), e.usize, MAX_REQUEST_BYTES);
    return a === null ? v : v.subarray(Number(a), Number(b));
  }

  // ------------------------------------------------------------ operations

  classify(key: string): Kind {
    const k = toByteString(key);
    if (k === null || k === "" || k.startsWith(RESERVED_PREFIX)) return "missing";
    const e = this.lookup(k);
    return e === null ? "missing" : e.kind;
  }

  async get(key: string, req: Request): Promise<Buffer | null> {
    if (req.kind === "range" && req.start > req.end) throw requestErr("range start > end");
    const k = toByteString(key);
    if (k === null || k === "" || k.startsWith(RESERVED_PREFIX)) return null;
    const e = this.lookup(k);
    if (e === null) return null;
    if (e.kind === "bytes") {
      const [a, b] = window(req, e.usize);
      return this.readBytesValue(e, a, b);
    }
    const parts = this.decodePayload(e.payloadId, e.payload);
    let n = 0n;
    for (const p of parts) n += sizeOf(p);
    const [a, b] = window(req, n);
    if (b - a > MAX_REQUEST_BYTES) throw requestErr("request exceeds resource limit");
    const out: Buffer[] = [];
    let pos = 0n;
    for (const p of parts) {
      const sz = sizeOf(p);
      const ps = pos, pe = pos + sz;
      pos = pe;
      const lo = max(ps, a), hi = min(pe, b);
      if (lo >= hi) continue;
      const i = lo - ps, j = hi - ps;
      if (p.data !== null) out.push(Buffer.from(p.data.subarray(Number(i), Number(j))));
      else out.push(await this.readSource(Number(p.source), p.offset + i, p.offset + j));
    }
    return Buffer.concat(out);
  }

  async raw(key: string): Promise<Buffer | null> {
    const k = toByteString(key);
    if (k === null || k === "") return null;
    if (k === SOURCES_KEY) return this.sourcesRaw;
    if (k === INDEX_KEY) return this.indexRaw;
    const e = this.lookup(k);
    if (e === null) return null;
    if (e.kind === "bytes") return this.readBytesValue(e, null, null);
    if (!this.f.within(e.bodyOffset, e.csize)) throw bodyErr("entry body lies outside the file");
    if (e.csize !== e.usize) throw bodyErr("STORED entry's compressed and uncompressed sizes differ");
    if (e.csize > MAX_REQUEST_BYTES) throw requestErr("request exceeds resource limit");
    return this.f.read(e.bodyOffset, e.csize);
  }

  list(prefix: string): string[] {
    const p = toByteString(prefix);
    if (p === null) return [];
    const keys = new Set<string>();
    const ok = (k: string) => k.startsWith(p) && !k.startsWith(RESERVED_PREFIX) && k !== "";
    if (!this.paged) {
      for (const k of this.records.keys()) if (ok(k)) keys.add(k);
    } else {
      for (const k of this.pinned.keys()) if (ok(k)) keys.add(k);
      for (const pg of this.pages) {
        const read = (pg.hi === null || p < pg.hi) && (pg.lo <= p || pg.lo.startsWith(p));
        if (!read) continue;
        const recs = this.pageRecords(pg);
        for (const k of recs.keys()) {
          if (!ok(k)) continue;
          if (k < pg.lo || (pg.hi !== null && k >= pg.hi)) continue;
          keys.add(k);
        }
      }
    }
    return [...keys].sort(cmp).map(fromByteString);
  }

  // ------------------------------------------------------------ references

  private decodePayload(id: number, payload: Buffer): RangeMsg[] {
    if (payload.length > MAX_PAYLOAD) throw payloadErr(`reference payload is ${payload.length} bytes, max ${MAX_PAYLOAD}`);
    let parts: RangeMsg[];
    try {
      parts = id === ID_RANGE ? [decodeRange(payload)] : decodeConcat(payload);
    } catch (e) {
      if (e instanceof MalformedError) throw payloadErr(`malformed reference payload: ${e.message}`);
      throw e;
    }
    let total = 0n;
    parts.forEach((p, i) => {
      if (p.data !== null) {
        if (p.source !== 0n || p.offset !== 0n || p.length !== 0n)
          throw payloadErr(`range ${i}: literal range with non-zero source/offset/length`);
      } else {
        if (p.source >= BigInt(this.sources.length)) throw payloadErr(`range ${i}: source ${p.source} out of bounds`);
        if (p.offset + p.length > U64_MAX) throw payloadErr(`range ${i}: offset + length exceeds 2^64-1`);
      }
      total += sizeOf(p);
    });
    if (total > U64_MAX) throw payloadErr("reference size exceeds 2^64-1");
    return parts;
  }

  /** Reads bytes [from, to) of source `idx`'s value (to > from). */
  private async readSource(idx: number, from: bigint, to: bigint): Promise<Buffer> {
    const s = this.sources[idx];
    if (s.kind === "data") {
      if (BigInt(s.data!.length) < to) throw resolutionErr(`data source ${idx} has ${s.data!.length} bytes, need ${to}`);
      return Buffer.from(s.data!.subarray(Number(from), Number(to)));
    }
    if (s.kind === "key") {
      const k = s.key!;
      if (k === SOURCES_KEY || k === INDEX_KEY) throw resolutionErr(`key source ${idx} names a format entry`);
      let e: EntryInfo | null;
      try {
        e = this.lookup(k);
      } catch (err) {
        if (err instanceof VzError) throw resolutionErr(`key source ${idx}: ${err.message}`);
        throw err;
      }
      if (e === null) throw resolutionErr(`key source ${idx}: key ${JSON.stringify(fromByteString(k))} is missing`);
      if (e.kind !== "bytes") throw resolutionErr(`key source ${idx}: key is a reference entry`);
      if (e.usize < to) throw resolutionErr(`key source ${idx} has ${e.usize} bytes, need ${to}`);
      try {
        return this.readBytesValue(e, from, to);
      } catch (err) {
        if (err instanceof VzError && err.cls === "body") throw resolutionErr(`key source ${idx}: ${err.message}`);
        throw err;
      }
    }
    // url
    const ref = parseUriReference(s.url!);
    if (ref === null) throw resolutionErr(`source ${idx}: not a valid URI reference: ${JSON.stringify(s.url)}`);
    const target = resolve(this.baseUri, ref);
    const scheme = target.scheme!.toLowerCase();
    const pins = { size: s.size, etag: s.etag, modifiedNotAfter: s.modifiedNotAfter };
    if (scheme === "file") {
      const p = fileUriToPath(target);
      if (typeof p === "string") throw resolutionErr(`source ${idx}: ${p}`);
      return readFileRange(p, from, to, pins, idx);
    }
    if (scheme === "http" || scheme === "https") {
      delete target.fragment;
      return httpReadRange(target, from, to, pins);
    }
    throw resolutionErr(`source ${idx}: unsupported URL scheme in ${recompose(target)}`);
  }
}

function sizeOf(p: RangeMsg): bigint {
  return p.data !== null ? BigInt(p.data.length) : p.length;
}

function readFileRange(
  p: Buffer,
  from: bigint,
  to: bigint,
  pins: { size: bigint | null; etag: string | null; modifiedNotAfter: bigint | null },
  idx: number,
): Buffer {
  if (pins.etag !== null) throw resolutionErr(`source ${idx}: etag pin cannot be checked for file: URLs`);
  let fd: number;
  try {
    fd = fs.openSync(p, "r");
  } catch (e) {
    throw resolutionErr(`source ${idx}: cannot open ${p.toString()}: ${(e as Error).message}`);
  }
  try {
    const st = fs.fstatSync(fd, { bigint: true });
    if (!st.isFile()) throw resolutionErr(`source ${idx}: not a regular file`);
    if (pins.size !== null && st.size !== pins.size)
      throw resolutionErr(`source ${idx}: size pin failed (${st.size} != ${pins.size})`);
    if (pins.modifiedNotAfter !== null) {
      const ns = st.mtimeNs;
      const secs = ns >= 0n ? ns / 1000000000n : -((-ns + 999999999n) / 1000000000n);
      if (secs > pins.modifiedNotAfter) throw resolutionErr(`source ${idx}: modified_not_after pin failed`);
    }
    if (st.size < to) throw resolutionErr(`source ${idx} has ${st.size} bytes, need ${to}`);
    if (to - from > MAX_REQUEST_BYTES) throw requestErr("request exceeds resource limit");
    const n = Number(to - from);
    const buf = Buffer.alloc(n);
    let got = 0;
    while (got < n) {
      const r = fs.readSync(fd, buf, got, n - got, Number(from) + got);
      if (r === 0) throw resolutionErr(`source ${idx}: unexpected end of file`);
      got += r;
    }
    return buf;
  } catch (e) {
    if (e instanceof VzError) throw e;
    throw resolutionErr(`source ${idx}: read failed: ${(e as Error).message}`);
  } finally {
    fs.closeSync(fd);
  }
}
