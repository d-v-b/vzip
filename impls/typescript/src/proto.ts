// Hand-written protobuf wire format (spec §5.1) and the vzip messages
// (Appendix A).

import { MalformedError } from "./errors.ts";

export const U64_MAX = (1n << 64n) - 1n;
const U32_MAX = (1n << 32n) - 1n;
const MAX_FIELD = (1n << 29n) - 1n;

const utf8Strict = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

/** Validates UTF-8 and returns the bytes as a latin1 "byte string". */
export function utf8ByteString(b: Uint8Array): string {
  utf8Strict.decode(b); // throws on invalid
  return Buffer.from(b.buffer, b.byteOffset, b.byteLength).toString("latin1");
}

export function isValidUtf8(b: Uint8Array): boolean {
  try {
    utf8Strict.decode(b);
    return true;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------- decoding

type RawField = { num: number; wt: number; v: bigint | Uint8Array };

function readVarint(buf: Uint8Array, pos: number): [bigint, number] {
  let result = 0n;
  let shift = 0n;
  for (let i = 0; i < 10; i++) {
    if (pos >= buf.length) throw new MalformedError("truncated varint");
    const b = buf[pos++];
    if (i === 9 && b > 1) throw new MalformedError("varint exceeds 2^64-1");
    result |= BigInt(b & 0x7f) << shift;
    shift += 7n;
    if ((b & 0x80) === 0) return [result, pos];
  }
  throw new MalformedError("varint longer than 10 bytes");
}

function parseFields(buf: Uint8Array): RawField[] {
  const out: RawField[] = [];
  let pos = 0;
  while (pos < buf.length) {
    let tag: bigint;
    [tag, pos] = readVarint(buf, pos);
    const wt = Number(tag & 7n);
    const numBig = tag >> 3n;
    if (numBig === 0n || numBig > MAX_FIELD) throw new MalformedError(`invalid field number ${numBig}`);
    const num = Number(numBig);
    switch (wt) {
      case 0: {
        let v: bigint;
        [v, pos] = readVarint(buf, pos);
        out.push({ num, wt, v });
        break;
      }
      case 1:
        if (pos + 8 > buf.length) throw new MalformedError("truncated I64");
        out.push({ num, wt, v: buf.subarray(pos, pos + 8) });
        pos += 8;
        break;
      case 5:
        if (pos + 4 > buf.length) throw new MalformedError("truncated I32");
        out.push({ num, wt, v: buf.subarray(pos, pos + 4) });
        pos += 4;
        break;
      case 2: {
        let len: bigint;
        [len, pos] = readVarint(buf, pos);
        if (len > BigInt(buf.length - pos)) throw new MalformedError("LEN field past end of message");
        const n = Number(len);
        out.push({ num, wt, v: buf.subarray(pos, pos + n) });
        pos += n;
        break;
      }
      default:
        throw new MalformedError(`invalid wire type ${wt}`);
    }
  }
  return out;
}

type FieldType = "uint32" | "uint64" | "int64" | "string" | "bytes" | "message";

/** Returns the value of a known field, checking wire type and range. */
function known(f: RawField, t: FieldType): bigint | Uint8Array | string {
  const wantWt = t === "string" || t === "bytes" || t === "message" ? 2 : 0;
  if (f.wt !== wantWt) throw new MalformedError(`field ${f.num}: wire type ${f.wt}, expected ${wantWt}`);
  switch (t) {
    case "uint32":
      if ((f.v as bigint) > U32_MAX) throw new MalformedError(`field ${f.num}: uint32 overflow`);
      return f.v as bigint;
    case "uint64":
      return f.v as bigint;
    case "int64":
      return BigInt.asIntN(64, f.v as bigint);
    case "string":
      try {
        return utf8ByteString(f.v as Uint8Array);
      } catch {
        throw new MalformedError(`field ${f.num}: invalid UTF-8`);
      }
    case "bytes":
    case "message":
      return f.v as Uint8Array;
  }
}

// ---------------------------------------------------------------- encoding

export class Enc {
  parts: Uint8Array[] = [];
  varint(v: bigint) {
    v = BigInt.asUintN(64, v);
    const bytes: number[] = [];
    do {
      let b = Number(v & 0x7fn);
      v >>= 7n;
      if (v !== 0n) b |= 0x80;
      bytes.push(b);
    } while (v !== 0n);
    this.parts.push(Uint8Array.from(bytes));
  }
  tag(num: number, wt: number) {
    this.varint(BigInt(num * 8 + wt));
  }
  /** Emit a varint field; skip if default unless `force`. */
  uint(num: number, v: bigint, force = false) {
    if (v === 0n && !force) return;
    this.tag(num, 0);
    this.varint(v);
  }
  len(num: number, b: Uint8Array, force = false) {
    if (b.length === 0 && !force) return;
    this.tag(num, 2);
    this.varint(BigInt(b.length));
    this.parts.push(b);
  }
  str(num: number, s: string, force = false) {
    this.len(num, Buffer.from(s, "latin1"), force);
  }
  finish(): Buffer {
    return Buffer.concat(this.parts);
  }
}

// ---------------------------------------------------------------- messages
// Strings are latin1 "byte strings" holding UTF-8 bytes.

export type RangeMsg = { source: bigint; offset: bigint; length: bigint; data: Uint8Array | null };

export function decodeRange(buf: Uint8Array): RangeMsg {
  const r: RangeMsg = { source: 0n, offset: 0n, length: 0n, data: null };
  for (const f of parseFields(buf)) {
    switch (f.num) {
      case 1: r.source = known(f, "uint32") as bigint; break;
      case 3: r.offset = known(f, "uint64") as bigint; break;
      case 4: r.length = known(f, "uint64") as bigint; break;
      case 5: r.data = known(f, "bytes") as Uint8Array; break;
      default: break; // unknown, including reserved field 2
    }
  }
  return r;
}

export function encodeRange(r: RangeMsg): Buffer {
  const e = new Enc();
  e.uint(1, r.source);
  e.uint(3, r.offset);
  e.uint(4, r.length);
  if (r.data !== null) e.len(5, r.data, true);
  return e.finish();
}

export function decodeConcat(buf: Uint8Array): RangeMsg[] {
  const parts: RangeMsg[] = [];
  for (const f of parseFields(buf)) {
    if (f.num === 1) parts.push(decodeRange(known(f, "message") as Uint8Array));
  }
  return parts;
}

export function encodeConcat(parts: RangeMsg[]): Buffer {
  const e = new Enc();
  for (const p of parts) e.len(1, encodeRange(p), true);
  return e.finish();
}

export type SourceMsg = {
  kind: "url" | "key" | "data" | null;
  url?: string; // byte string
  key?: string; // byte string
  data?: Uint8Array;
  size: bigint | null;
  etag: string | null;
  modifiedNotAfter: bigint | null;
};

export function decodeSource(buf: Uint8Array): SourceMsg {
  const s: SourceMsg = { kind: null, size: null, etag: null, modifiedNotAfter: null };
  for (const f of parseFields(buf)) {
    switch (f.num) {
      case 1: s.kind = "url"; s.url = known(f, "string") as string; delete s.key; delete s.data; break;
      case 2: s.kind = "key"; s.key = known(f, "string") as string; delete s.url; delete s.data; break;
      case 3: s.kind = "data"; s.data = known(f, "bytes") as Uint8Array; delete s.url; delete s.key; break;
      case 4: s.size = known(f, "uint64") as bigint; break;
      case 5: s.etag = known(f, "string") as string; break;
      case 6: s.modifiedNotAfter = known(f, "int64") as bigint; break;
      default: break;
    }
  }
  return s;
}

export function encodeSource(s: SourceMsg): Buffer {
  const e = new Enc();
  if (s.kind === "url") e.str(1, s.url!, true);
  else if (s.kind === "key") e.str(2, s.key!, true);
  else if (s.kind === "data") e.len(3, s.data!, true);
  if (s.size !== null) e.uint(4, s.size, true);
  if (s.etag !== null) e.str(5, s.etag, true);
  if (s.modifiedNotAfter !== null) e.uint(6, s.modifiedNotAfter, true);
  return e.finish();
}

export function decodeSourceTable(buf: Uint8Array): SourceMsg[] {
  const out: SourceMsg[] = [];
  for (const f of parseFields(buf)) {
    if (f.num === 1) out.push(decodeSource(known(f, "message") as Uint8Array));
  }
  return out;
}

export function encodeSourceTable(sources: SourceMsg[]): Buffer {
  const e = new Enc();
  for (const s of sources) e.len(1, encodeSource(s), true);
  return e.finish();
}

export type PageMsg = { firstKey: string; offset: bigint; length: bigint };
export type PinnedMsg = { key: string; dataOffset: bigint; size: bigint; csize: bigint; method: bigint };
export type CdIndexMsg = { pages: PageMsg[]; pinned: PinnedMsg[] };

export function decodeCdIndex(buf: Uint8Array): CdIndexMsg {
  const idx: CdIndexMsg = { pages: [], pinned: [] };
  for (const f of parseFields(buf)) {
    if (f.num === 1) {
      const p: PageMsg = { firstKey: "", offset: 0n, length: 0n };
      for (const g of parseFields(known(f, "message") as Uint8Array)) {
        if (g.num === 1) p.firstKey = known(g, "string") as string;
        else if (g.num === 2) p.offset = known(g, "uint64") as bigint;
        else if (g.num === 3) p.length = known(g, "uint64") as bigint;
      }
      idx.pages.push(p);
    } else if (f.num === 2) {
      const p: PinnedMsg = { key: "", dataOffset: 0n, size: 0n, csize: 0n, method: 0n };
      for (const g of parseFields(known(f, "message") as Uint8Array)) {
        if (g.num === 1) p.key = known(g, "string") as string;
        else if (g.num === 2) p.dataOffset = known(g, "uint64") as bigint;
        else if (g.num === 3) p.size = known(g, "uint64") as bigint;
        else if (g.num === 4) p.csize = known(g, "uint64") as bigint;
        else if (g.num === 5) p.method = known(g, "uint32") as bigint;
      }
      idx.pinned.push(p);
    }
  }
  return idx;
}

export function encodeCdIndex(idx: CdIndexMsg): Buffer {
  const e = new Enc();
  for (const p of idx.pages) {
    const pe = new Enc();
    pe.str(1, p.firstKey);
    pe.uint(2, p.offset);
    pe.uint(3, p.length);
    e.len(1, pe.finish(), true);
  }
  for (const p of idx.pinned) {
    const pe = new Enc();
    pe.str(1, p.key);
    pe.uint(2, p.dataOffset);
    pe.uint(3, p.size);
    pe.uint(4, p.csize);
    pe.uint(5, p.method);
    e.len(2, pe.finish(), true);
  }
  return e.finish();
}
