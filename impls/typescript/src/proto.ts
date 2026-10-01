// Hand-written protobuf (proto3) codec for the vzip schema (spec §5, Appendix A).
import { MalformedError } from "./errors.ts";

export const U64_MAX = (1n << 64n) - 1n;
export const U32_MAX = (1n << 32n) - 1n;

const utf8Decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
const utf8Encoder = new TextEncoder();

/** Decode UTF-8 strictly (BOM kept). Returns null when invalid. */
export function decodeUtf8(b: Uint8Array): string | null {
  try {
    return utf8Decoder.decode(b);
  } catch {
    return null;
  }
}

export function encodeUtf8(s: string): Uint8Array {
  return utf8Encoder.encode(s);
}

// ---------------------------------------------------------------- decoding

export type WireField =
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
    result |= BigInt(b & 0x7f) << shift;
    if ((b & 0x80) === 0) {
      if (result > U64_MAX) throw new MalformedError("varint exceeds 2^64-1");
      return [result, pos];
    }
    shift += 7n;
  }
  throw new MalformedError("varint longer than 10 bytes");
}

/** Split a message into its wire fields (§5.1 decoding rules). */
export function parseFields(buf: Uint8Array): WireField[] {
  const out: WireField[] = [];
  let pos = 0;
  while (pos < buf.length) {
    const [tag, p1] = readVarint(buf, pos);
    pos = p1;
    const wt = Number(tag & 7n);
    const numBig = tag >> 3n;
    if (numBig === 0n || numBig > (1n << 29n) - 1n) {
      throw new MalformedError(`invalid field number ${numBig}`);
    }
    const num = Number(numBig);
    switch (wt) {
      case 0: {
        const [v, p2] = readVarint(buf, pos);
        pos = p2;
        out.push({ num, wt: 0, v });
        break;
      }
      case 1: {
        if (pos + 8 > buf.length) throw new MalformedError("truncated I64");
        out.push({ num, wt: 1, v: buf.subarray(pos, pos + 8) });
        pos += 8;
        break;
      }
      case 2: {
        const [len, p2] = readVarint(buf, pos);
        pos = p2;
        if (len > BigInt(buf.length - pos)) {
          throw new MalformedError("LEN field extends past end of message");
        }
        const n = Number(len);
        out.push({ num, wt: 2, v: buf.subarray(pos, pos + n) });
        pos += n;
        break;
      }
      case 5: {
        if (pos + 4 > buf.length) throw new MalformedError("truncated I32");
        out.push({ num, wt: 5, v: buf.subarray(pos, pos + 4) });
        pos += 4;
        break;
      }
      default:
        throw new MalformedError(`invalid wire type ${wt}`);
    }
  }
  return out;
}

function expectVarint(f: WireField): bigint {
  if (f.wt !== 0) throw new MalformedError(`field ${f.num}: expected VARINT`);
  return f.v;
}

function expectLen(f: WireField): Uint8Array {
  if (f.wt !== 2) throw new MalformedError(`field ${f.num}: expected LEN`);
  return f.v;
}

function expectUint32(f: WireField): number {
  const v = expectVarint(f);
  if (v > U32_MAX) throw new MalformedError(`field ${f.num}: uint32 overflow`);
  return Number(v);
}

function expectString(f: WireField): string {
  const s = decodeUtf8(expectLen(f));
  if (s === null) throw new MalformedError(`field ${f.num}: invalid UTF-8`);
  return s;
}

function expectInt64(f: WireField): bigint {
  return BigInt.asIntN(64, expectVarint(f));
}

// ---------------------------------------------------------------- encoding

class Enc {
  parts: number[] = [];
  varint(v: bigint): void {
    if (v < 0n) v = BigInt.asUintN(64, v);
    while (v >= 0x80n) {
      this.parts.push(Number(v & 0x7fn) | 0x80);
      v >>= 7n;
    }
    this.parts.push(Number(v));
  }
  tag(num: number, wt: number): void {
    this.varint(BigInt((num << 3) | wt));
  }
  bytes(num: number, b: Uint8Array): void {
    this.tag(num, 2);
    this.varint(BigInt(b.length));
    for (const x of b) this.parts.push(x);
  }
  uint(num: number, v: bigint): void {
    this.tag(num, 0);
    this.varint(v);
  }
  done(): Uint8Array {
    return Uint8Array.from(this.parts);
  }
}

// ---------------------------------------------------------------- messages

export interface Range {
  source: number;
  offset: bigint;
  length: bigint;
  data?: Uint8Array; // present => literal range
}

export interface Source {
  kind: "url" | "key" | "data";
  url?: string;
  key?: string;
  data?: Uint8Array;
  size?: bigint;
  etag?: string;
  modifiedNotAfter?: bigint;
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

/** Decode a Range, including the §5.2 semantic checks except `source` bounds. */
export function decodeRange(buf: Uint8Array): Range {
  const r: Range = { source: 0, offset: 0n, length: 0n };
  for (const f of parseFields(buf)) {
    switch (f.num) {
      case 1: r.source = expectUint32(f); break;
      case 3: r.offset = expectVarint(f); break;
      case 4: r.length = expectVarint(f); break;
      case 5: r.data = expectLen(f); break;
      default: break; // unknown or reserved (2): skipped
    }
  }
  if (r.data !== undefined) {
    if (r.source !== 0 || r.offset !== 0n || r.length !== 0n) {
      throw new MalformedError("literal range with non-zero source/offset/length");
    }
  } else if (r.offset + r.length > U64_MAX) {
    throw new MalformedError("offset + length exceeds 2^64-1");
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

export function rangeSize(r: Range): bigint {
  return r.data !== undefined ? BigInt(r.data.length) : r.length;
}

export function encodeRange(r: Range): Uint8Array {
  const e = new Enc();
  if (r.source !== 0) e.uint(1, BigInt(r.source));
  if (r.offset !== 0n) e.uint(3, r.offset);
  if (r.length !== 0n) e.uint(4, r.length);
  if (r.data !== undefined) e.bytes(5, r.data);
  return e.done();
}

export function encodeConcat(parts: Range[]): Uint8Array {
  const e = new Enc();
  for (const p of parts) e.bytes(1, encodeRange(p));
  return e.done();
}

function decodeSource(buf: Uint8Array): Source {
  const s: Partial<Source> = {};
  for (const f of parseFields(buf)) {
    switch (f.num) {
      case 1:
        delete s.key; delete s.data; s.kind = "url"; s.url = expectString(f);
        break;
      case 2:
        delete s.url; delete s.data; s.kind = "key"; s.key = expectString(f);
        break;
      case 3:
        delete s.url; delete s.key; s.kind = "data"; s.data = expectLen(f);
        break;
      case 4: s.size = expectVarint(f); break;
      case 5: s.etag = expectString(f); break;
      case 6: s.modifiedNotAfter = expectInt64(f); break;
      default: break;
    }
  }
  return s as Source; // kind may be undefined; checked by caller
}

export function decodeSourceTable(buf: Uint8Array): Source[] {
  const out: Source[] = [];
  for (const f of parseFields(buf)) {
    if (f.num === 1) out.push(decodeSource(expectLen(f)));
  }
  return out;
}

export function encodeSourceTable(sources: Source[]): Uint8Array {
  const e = new Enc();
  for (const s of sources) {
    const m = new Enc();
    if (s.kind === "url") m.bytes(1, encodeUtf8(s.url!));
    else if (s.kind === "key") m.bytes(2, encodeUtf8(s.key!));
    else m.bytes(3, s.data!);
    if (s.size !== undefined) m.uint(4, s.size);
    if (s.etag !== undefined) m.bytes(5, encodeUtf8(s.etag));
    if (s.modifiedNotAfter !== undefined) m.uint(6, s.modifiedNotAfter);
    e.bytes(1, m.done());
  }
  return e.done();
}

export function decodeCdIndex(buf: Uint8Array): CdIndex {
  const idx: CdIndex = { pages: [], pinned: [] };
  for (const f of parseFields(buf)) {
    if (f.num === 1) {
      const p: Page = { firstKey: "", offset: 0n, length: 0n };
      for (const g of parseFields(expectLen(f))) {
        switch (g.num) {
          case 1: p.firstKey = expectString(g); break;
          case 2: p.offset = expectVarint(g); break;
          case 3: p.length = expectVarint(g); break;
          default: break;
        }
      }
      idx.pages.push(p);
    } else if (f.num === 2) {
      const p: Pinned = { key: "", dataOffset: 0n, size: 0n, csize: 0n, method: 0 };
      for (const g of parseFields(expectLen(f))) {
        switch (g.num) {
          case 1: p.key = expectString(g); break;
          case 2: p.dataOffset = expectVarint(g); break;
          case 3: p.size = expectVarint(g); break;
          case 4: p.csize = expectVarint(g); break;
          case 5: p.method = expectUint32(g); break;
          default: break;
        }
      }
      idx.pinned.push(p);
    }
  }
  return idx;
}

export function encodeCdIndex(idx: CdIndex): Uint8Array {
  const e = new Enc();
  for (const p of idx.pages) {
    const m = new Enc();
    if (p.firstKey !== "") m.bytes(1, encodeUtf8(p.firstKey));
    if (p.offset !== 0n) m.uint(2, p.offset);
    if (p.length !== 0n) m.uint(3, p.length);
    e.bytes(1, m.done());
  }
  for (const p of idx.pinned) {
    const m = new Enc();
    if (p.key !== "") m.bytes(1, encodeUtf8(p.key));
    if (p.dataOffset !== 0n) m.uint(2, p.dataOffset);
    if (p.size !== 0n) m.uint(3, p.size);
    if (p.csize !== 0n) m.uint(4, p.csize);
    if (p.method !== 0) m.uint(5, BigInt(p.method));
    e.bytes(2, m.done());
  }
  return e.done();
}
