// Hand-written protobuf wire format for the vzip schema (spec §5, Appendix A).
import { MalformedError, decodeUtf8 } from "./errors.ts";

const U64_MAX = (1n << 64n) - 1n;
const U32_MAX = (1n << 32n) - 1n;
const MAX_FIELD = (1n << 29n) - 1n;

// ---------- data types ----------

export interface Range {
  source: number; // uint32
  offset: bigint; // uint64
  length: bigint; // uint64
  data: Uint8Array | null; // optional bytes; non-null = literal range
}

export type SourceKind =
  | { type: "url"; value: string }
  | { type: "key"; value: string }
  | { type: "data"; value: Uint8Array };

export interface Source {
  kind: SourceKind | null;
  size: bigint | null;
  etag: string | null;
  modifiedNotAfter: bigint | null; // int64
}

export interface Page {
  firstKey: string;
  offset: bigint;
  length: bigint;
}

export interface Pinned {
  key: string;
  dataOffset: bigint;
  size: bigint;
  csize: bigint;
  method: number;
}

export interface CdIndex {
  pages: Page[];
  pinned: Pinned[];
}

// ---------- decoding ----------

type Field =
  | { num: number; wt: 0; v: bigint }
  | { num: number; wt: 1; v: Uint8Array }
  | { num: number; wt: 2; v: Uint8Array }
  | { num: number; wt: 5; v: Uint8Array };

function readVarint(buf: Uint8Array, pos: number): [bigint, number] {
  let result = 0n;
  let shift = 0n;
  for (let i = 0; i < 10; i++) {
    if (pos >= buf.length) throw new MalformedError("truncated varint");
    const b = buf[pos++];
    if (i === 9) {
      if (b & 0x80) throw new MalformedError("varint longer than 10 bytes");
      if (b > 1) throw new MalformedError("varint exceeds 2^64-1");
    }
    result |= BigInt(b & 0x7f) << shift;
    shift += 7n;
    if (!(b & 0x80)) return [result, pos];
  }
  throw new MalformedError("varint longer than 10 bytes");
}

function parseFields(buf: Uint8Array): Field[] {
  const out: Field[] = [];
  let pos = 0;
  while (pos < buf.length) {
    const [tag, p1] = readVarint(buf, pos);
    pos = p1;
    const wt = Number(tag & 7n);
    const numBig = tag >> 3n;
    if (numBig === 0n || numBig > MAX_FIELD) throw new MalformedError(`bad field number ${numBig}`);
    const num = Number(numBig);
    switch (wt) {
      case 0: {
        const [v, p2] = readVarint(buf, pos);
        pos = p2;
        out.push({ num, wt: 0, v });
        break;
      }
      case 1:
        if (pos + 8 > buf.length) throw new MalformedError("truncated I64");
        out.push({ num, wt: 1, v: buf.subarray(pos, pos + 8) });
        pos += 8;
        break;
      case 5:
        if (pos + 4 > buf.length) throw new MalformedError("truncated I32");
        out.push({ num, wt: 5, v: buf.subarray(pos, pos + 4) });
        pos += 4;
        break;
      case 2: {
        const [len, p2] = readVarint(buf, pos);
        pos = p2;
        if (len > BigInt(buf.length - pos)) throw new MalformedError("LEN field past end of message");
        const n = Number(len);
        out.push({ num, wt: 2, v: buf.subarray(pos, pos + n) });
        pos += n;
        break;
      }
      default:
        throw new MalformedError(`invalid wire type ${wt}`);
    }
  }
  return out;
}

function expectVarint(f: Field): bigint {
  if (f.wt !== 0) throw new MalformedError(`field ${f.num}: expected VARINT, got wire type ${f.wt}`);
  return f.v;
}
function expectLen(f: Field): Uint8Array {
  if (f.wt !== 2) throw new MalformedError(`field ${f.num}: expected LEN, got wire type ${f.wt}`);
  return f.v;
}
function expectU32(f: Field): number {
  const v = expectVarint(f);
  if (v > U32_MAX) throw new MalformedError(`field ${f.num}: uint32 overflow`);
  return Number(v);
}
function expectString(f: Field): string {
  const s = decodeUtf8(expectLen(f));
  if (s === null) throw new MalformedError(`field ${f.num}: invalid UTF-8`);
  return s;
}

export function decodeRange(buf: Uint8Array): Range {
  const r: Range = { source: 0, offset: 0n, length: 0n, data: null };
  for (const f of parseFields(buf)) {
    switch (f.num) {
      case 1: r.source = expectU32(f); break;
      case 3: r.offset = expectVarint(f); break;
      case 4: r.length = expectVarint(f); break;
      case 5: r.data = expectLen(f); break;
      default: break; // unknown or reserved (2): skip
    }
  }
  return r;
}

export function decodeConcat(buf: Uint8Array): Range[] {
  const parts: Range[] = [];
  for (const f of parseFields(buf)) {
    if (f.num === 1) parts.push(decodeRange(expectLen(f)));
  }
  return parts;
}

export function decodeSource(buf: Uint8Array): Source {
  const s: Source = { kind: null, size: null, etag: null, modifiedNotAfter: null };
  for (const f of parseFields(buf)) {
    switch (f.num) {
      case 1: s.kind = { type: "url", value: expectString(f) }; break;
      case 2: s.kind = { type: "key", value: expectString(f) }; break;
      case 3: s.kind = { type: "data", value: expectLen(f) }; break;
      case 4: s.size = expectVarint(f); break;
      case 5: s.etag = expectString(f); break;
      case 6: s.modifiedNotAfter = BigInt.asIntN(64, expectVarint(f)); break;
      default: break;
    }
  }
  return s;
}

export function decodeSourceTable(buf: Uint8Array): Source[] {
  const out: Source[] = [];
  for (const f of parseFields(buf)) {
    if (f.num === 1) out.push(decodeSource(expectLen(f)));
  }
  return out;
}

function decodePage(buf: Uint8Array): Page {
  const p: Page = { firstKey: "", offset: 0n, length: 0n };
  for (const f of parseFields(buf)) {
    switch (f.num) {
      case 1: p.firstKey = expectString(f); break;
      case 2: p.offset = expectVarint(f); break;
      case 3: p.length = expectVarint(f); break;
      default: break;
    }
  }
  return p;
}

function decodePinned(buf: Uint8Array): Pinned {
  const p: Pinned = { key: "", dataOffset: 0n, size: 0n, csize: 0n, method: 0 };
  for (const f of parseFields(buf)) {
    switch (f.num) {
      case 1: p.key = expectString(f); break;
      case 2: p.dataOffset = expectVarint(f); break;
      case 3: p.size = expectVarint(f); break;
      case 4: p.csize = expectVarint(f); break;
      case 5: p.method = expectU32(f); break;
      default: break;
    }
  }
  return p;
}

export function decodeCdIndex(buf: Uint8Array): CdIndex {
  const idx: CdIndex = { pages: [], pinned: [] };
  for (const f of parseFields(buf)) {
    if (f.num === 1) idx.pages.push(decodePage(expectLen(f)));
    else if (f.num === 2) idx.pinned.push(decodePinned(expectLen(f)));
  }
  return idx;
}

// ---------- encoding ----------

class Enc {
  chunks: Uint8Array[] = [];
  varint(v: bigint): void {
    if (v < 0n) v = BigInt.asUintN(64, v);
    if (v > U64_MAX) throw new Error("varint too large");
    const bytes: number[] = [];
    do {
      let b = Number(v & 0x7fn);
      v >>= 7n;
      if (v !== 0n) b |= 0x80;
      bytes.push(b);
    } while (v !== 0n);
    this.chunks.push(Uint8Array.from(bytes));
  }
  tag(num: number, wt: number): void {
    this.varint(BigInt(num * 8 + wt));
  }
  uint(num: number, v: bigint, always = false): void {
    if (v === 0n && !always) return;
    this.tag(num, 0);
    this.varint(v);
  }
  bytes(num: number, v: Uint8Array, always = false): void {
    if (v.length === 0 && !always) return;
    this.tag(num, 2);
    this.varint(BigInt(v.length));
    this.chunks.push(v);
  }
  str(num: number, v: string, always = false): void {
    this.bytes(num, Buffer.from(v, "utf8"), always);
  }
  done(): Buffer {
    return Buffer.concat(this.chunks);
  }
}

export function encodeRange(r: Range): Buffer {
  const e = new Enc();
  e.uint(1, BigInt(r.source));
  e.uint(3, r.offset);
  e.uint(4, r.length);
  if (r.data !== null) e.bytes(5, r.data, true);
  return e.done();
}

export function encodeConcat(parts: Range[]): Buffer {
  const e = new Enc();
  // A repeated message element is always emitted, even if it encodes to zero bytes.
  for (const p of parts) e.bytes(1, encodeRange(p), true);
  return e.done();
}

export function encodeSource(s: Source): Buffer {
  const e = new Enc();
  const k = s.kind!;
  if (k.type === "url") e.str(1, k.value, true);
  else if (k.type === "key") e.str(2, k.value, true);
  else e.bytes(3, k.value, true);
  if (s.size !== null) e.uint(4, s.size, true);
  if (s.etag !== null) e.str(5, s.etag, true);
  if (s.modifiedNotAfter !== null) e.uint(6, s.modifiedNotAfter, true);
  return e.done();
}

export function encodeSourceTable(sources: Source[]): Buffer {
  const e = new Enc();
  for (const s of sources) e.bytes(1, encodeSource(s), true);
  return e.done();
}

export function encodeCdIndex(idx: CdIndex): Buffer {
  const e = new Enc();
  for (const p of idx.pages) {
    const pe = new Enc();
    pe.str(1, p.firstKey);
    pe.uint(2, p.offset);
    pe.uint(3, p.length);
    e.bytes(1, pe.done(), true);
  }
  for (const p of idx.pinned) {
    const pe = new Enc();
    pe.str(1, p.key);
    pe.uint(2, p.dataOffset);
    pe.uint(3, p.size);
    pe.uint(4, p.csize);
    pe.uint(5, BigInt(p.method));
    e.bytes(2, pe.done(), true);
  }
  return e.done();
}
