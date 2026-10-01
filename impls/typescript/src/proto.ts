// Hand-written protobuf (proto3) wire-format encoding/decoding for the vzip
// schema (spec §5, Appendix A).

export class ProtoError extends Error {}

const U64_MAX = (1n << 64n) - 1n;
const U32_MAX = (1n << 32n) - 1n;

const utf8Fatal = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

export function decodeUtf8Strict(b: Uint8Array): string | null {
  try {
    return utf8Fatal.decode(b);
  } catch {
    return null;
  }
}

type RawField = { num: number; wire: number; int?: bigint; bytes?: Uint8Array };

function readVarint(buf: Uint8Array, pos: number): [bigint, number] {
  let result = 0n;
  let shift = 0n;
  for (let i = 0; i < 10; i++) {
    if (pos >= buf.length) throw new ProtoError("truncated varint");
    const b = buf[pos++];
    result |= BigInt(b & 0x7f) << shift;
    shift += 7n;
    if ((b & 0x80) === 0) {
      if (result > U64_MAX) throw new ProtoError("varint exceeds 2^64-1");
      return [result, pos];
    }
  }
  throw new ProtoError("varint longer than 10 bytes");
}

function readRawFields(buf: Uint8Array): RawField[] {
  const out: RawField[] = [];
  let pos = 0;
  while (pos < buf.length) {
    let tag: bigint;
    [tag, pos] = readVarint(buf, pos);
    const wire = Number(tag & 7n);
    const numBig = tag >> 3n;
    if (numBig === 0n || numBig > (1n << 29n) - 1n) throw new ProtoError(`invalid field number ${numBig}`);
    const num = Number(numBig);
    switch (wire) {
      case 0: {
        let v: bigint;
        [v, pos] = readVarint(buf, pos);
        out.push({ num, wire, int: v });
        break;
      }
      case 1: {
        if (pos + 8 > buf.length) throw new ProtoError("truncated I64");
        let v = 0n;
        for (let i = 7; i >= 0; i--) v = (v << 8n) | BigInt(buf[pos + i]);
        pos += 8;
        out.push({ num, wire, int: v });
        break;
      }
      case 2: {
        let len: bigint;
        [len, pos] = readVarint(buf, pos);
        if (len > BigInt(buf.length - pos)) throw new ProtoError("LEN field past end of message");
        const n = Number(len);
        out.push({ num, wire, bytes: buf.subarray(pos, pos + n) });
        pos += n;
        break;
      }
      case 5: {
        if (pos + 4 > buf.length) throw new ProtoError("truncated I32");
        let v = 0n;
        for (let i = 3; i >= 0; i--) v = (v << 8n) | BigInt(buf[pos + i]);
        pos += 4;
        out.push({ num, wire, int: v });
        break;
      }
      default:
        throw new ProtoError(`invalid wire type ${wire}`);
    }
  }
  return out;
}

type FieldType = "uint32" | "uint64" | "int64" | "string" | "bytes" | "message";

function expectWire(f: RawField, t: FieldType): void {
  const want = t === "string" || t === "bytes" || t === "message" ? 2 : 0;
  if (f.wire !== want) throw new ProtoError(`field ${f.num}: wire type ${f.wire}, expected ${want}`);
}

function asUint32(f: RawField): number {
  expectWire(f, "uint32");
  if (f.int! > U32_MAX) throw new ProtoError(`field ${f.num}: uint32 overflow`);
  return Number(f.int!);
}
function asUint64(f: RawField): bigint {
  expectWire(f, "uint64");
  return f.int!;
}
function asInt64(f: RawField): bigint {
  expectWire(f, "int64");
  return BigInt.asIntN(64, f.int!);
}
function asString(f: RawField): string {
  expectWire(f, "string");
  const s = decodeUtf8Strict(f.bytes!);
  if (s === null) throw new ProtoError(`field ${f.num}: invalid UTF-8`);
  return s;
}
function asBytes(f: RawField): Uint8Array {
  expectWire(f, "bytes");
  return f.bytes!;
}

// ---------- message types ----------

export type Range = {
  source: number;
  offset: bigint;
  length: bigint;
  data: Uint8Array | null; // null = field 5 absent (source range)
};

export type Source = {
  kind: "url" | "key" | "data" | null;
  url?: string;
  key?: string;
  data?: Uint8Array;
  size: bigint | null;
  etag: string | null;
  modifiedNotAfter: bigint | null;
};

export type Page = { firstKey: string; offset: bigint; length: bigint };
export type Pinned = { key: string; dataOffset: bigint; size: bigint; csize: bigint; method: number };
export type CdIndex = { pages: Page[]; pinned: Pinned[] };

export function decodeRange(buf: Uint8Array): Range {
  const r: Range = { source: 0, offset: 0n, length: 0n, data: null };
  for (const f of readRawFields(buf)) {
    switch (f.num) {
      case 1: r.source = asUint32(f); break;
      case 3: r.offset = asUint64(f); break;
      case 4: r.length = asUint64(f); break;
      case 5: r.data = asBytes(f); break;
      default: break; // unknown or reserved (2): skip
    }
  }
  return r;
}

export function decodeConcat(buf: Uint8Array): Range[] {
  const parts: Range[] = [];
  for (const f of readRawFields(buf)) {
    if (f.num === 1) {
      expectWire(f, "message");
      parts.push(decodeRange(f.bytes!));
    }
  }
  return parts;
}

export function decodeSource(buf: Uint8Array): Source {
  const s: Source = { kind: null, size: null, etag: null, modifiedNotAfter: null };
  for (const f of readRawFields(buf)) {
    switch (f.num) {
      case 1: s.kind = "url"; s.url = asString(f); break;
      case 2: s.kind = "key"; s.key = asString(f); break;
      case 3: s.kind = "data"; s.data = asBytes(f); break;
      case 4: s.size = asUint64(f); break;
      case 5: s.etag = asString(f); break;
      case 6: s.modifiedNotAfter = asInt64(f); break;
      default: break;
    }
  }
  return s;
}

export function decodeSourceTable(buf: Uint8Array): Source[] {
  const out: Source[] = [];
  for (const f of readRawFields(buf)) {
    if (f.num === 1) {
      expectWire(f, "message");
      out.push(decodeSource(f.bytes!));
    }
  }
  return out;
}

function decodePage(buf: Uint8Array): Page {
  const p: Page = { firstKey: "", offset: 0n, length: 0n };
  for (const f of readRawFields(buf)) {
    switch (f.num) {
      case 1: p.firstKey = asString(f); break;
      case 2: p.offset = asUint64(f); break;
      case 3: p.length = asUint64(f); break;
      default: break;
    }
  }
  return p;
}

function decodePinned(buf: Uint8Array): Pinned {
  const p: Pinned = { key: "", dataOffset: 0n, size: 0n, csize: 0n, method: 0 };
  for (const f of readRawFields(buf)) {
    switch (f.num) {
      case 1: p.key = asString(f); break;
      case 2: p.dataOffset = asUint64(f); break;
      case 3: p.size = asUint64(f); break;
      case 4: p.csize = asUint64(f); break;
      case 5: p.method = asUint32(f); break;
      default: break;
    }
  }
  return p;
}

export function decodeCdIndex(buf: Uint8Array): CdIndex {
  const idx: CdIndex = { pages: [], pinned: [] };
  for (const f of readRawFields(buf)) {
    if (f.num === 1) {
      expectWire(f, "message");
      idx.pages.push(decodePage(f.bytes!));
    } else if (f.num === 2) {
      expectWire(f, "message");
      idx.pinned.push(decodePinned(f.bytes!));
    }
  }
  return idx;
}

// ---------- encoding ----------

class Enc {
  chunks: Uint8Array[] = [];
  small: number[] = [];
  flush(): void {
    if (this.small.length) {
      this.chunks.push(Uint8Array.from(this.small));
      this.small = [];
    }
  }
  varint(v: bigint): void {
    if (v < 0n) v = BigInt.asUintN(64, v);
    while (v >= 0x80n) {
      this.small.push(Number(v & 0x7fn) | 0x80);
      v >>= 7n;
    }
    this.small.push(Number(v));
  }
  tag(num: number, wire: number): void {
    this.varint(BigInt((num << 3) | wire));
  }
  int(num: number, v: bigint, always = false): void {
    if (v === 0n && !always) return;
    this.tag(num, 0);
    this.varint(v);
  }
  bytes(num: number, b: Uint8Array, always = false): void {
    if (b.length === 0 && !always) return;
    this.tag(num, 2);
    this.varint(BigInt(b.length));
    this.flush();
    this.chunks.push(b);
  }
  str(num: number, s: string, always = false): void {
    this.bytes(num, new TextEncoder().encode(s), always);
  }
  done(): Uint8Array {
    this.flush();
    return Buffer.concat(this.chunks);
  }
}

export function encodeRange(r: Range): Uint8Array {
  const e = new Enc();
  e.int(1, BigInt(r.source));
  e.int(3, r.offset);
  e.int(4, r.length);
  if (r.data !== null) e.bytes(5, r.data, true);
  return e.done();
}

export function encodeConcat(parts: Range[]): Uint8Array {
  const e = new Enc();
  for (const p of parts) e.bytes(1, encodeRange(p), true);
  return e.done();
}

export function encodeSource(s: Source): Uint8Array {
  const e = new Enc();
  if (s.kind === "url") e.str(1, s.url!, true);
  else if (s.kind === "key") e.str(2, s.key!, true);
  else if (s.kind === "data") e.bytes(3, s.data!, true);
  if (s.size !== null) e.int(4, s.size, true);
  if (s.etag !== null) e.str(5, s.etag, true);
  if (s.modifiedNotAfter !== null) e.int(6, s.modifiedNotAfter, true);
  return e.done();
}

export function encodeSourceTable(sources: Source[]): Uint8Array {
  const e = new Enc();
  for (const s of sources) e.bytes(1, encodeSource(s), true);
  return e.done();
}

export function encodeCdIndex(idx: CdIndex): Uint8Array {
  const e = new Enc();
  for (const p of idx.pages) {
    const pe = new Enc();
    pe.str(1, p.firstKey);
    pe.int(2, p.offset);
    pe.int(3, p.length);
    e.bytes(1, pe.done(), true);
  }
  for (const p of idx.pinned) {
    const pe = new Enc();
    pe.str(1, p.key);
    pe.int(2, p.dataOffset);
    pe.int(3, p.size);
    pe.int(4, p.csize);
    pe.int(5, BigInt(p.method));
    e.bytes(2, pe.done(), true);
  }
  return e.done();
}
