// Protocol Buffers encoding and strict decoding of the vzip messages
// (spec/archive.md §5, §6, Appendix A).

export class MalformedError extends Error {}

export type Range =
  | { data: Uint8Array }
  | {
      source: number;
      offset: bigint;
      length: bigint;
      /** CRC-32C of the range's bytes (spec §5.2). */
      crc32c?: number;
    };

export interface Source {
  url?: string;
  key?: string;
  data?: Uint8Array;
  size?: bigint;
  etag?: string;
  modifiedNotAfter?: bigint;
}

const U64_MAX = (1n << 64n) - 1n;
const U32_MAX = 0xffffffffn;

// ---- encoding

class Writer {
  private chunks: number[] = [];
  varint(v: bigint) {
    if (v < 0n) v &= U64_MAX; // int64: two's complement
    do {
      let b = Number(v & 0x7fn);
      v >>= 7n;
      if (v !== 0n) b |= 0x80;
      this.chunks.push(b);
    } while (v !== 0n);
  }
  tag(field: number, wireType: number) {
    this.varint(BigInt((field << 3) | wireType));
  }
  bytes(field: number, value: Uint8Array) {
    this.tag(field, 2);
    this.varint(BigInt(value.length));
    for (const b of value) this.chunks.push(b);
  }
  uint(field: number, value: bigint) {
    this.tag(field, 0);
    this.varint(value);
  }
  finish() {
    return Uint8Array.from(this.chunks);
  }
}

const utf8 = new TextEncoder();

export function encodeRange(r: Range): Uint8Array {
  const w = new Writer();
  if ("data" in r) {
    w.bytes(5, r.data);
  } else {
    if (r.source !== 0) w.uint(1, BigInt(r.source));
    if (r.offset !== 0n) w.uint(3, r.offset);
    if (r.length !== 0n) w.uint(4, r.length);
    // An optional field: emitted whenever set, even if 0 (spec §5.1).
    if (r.crc32c !== undefined) w.uint(6, BigInt(r.crc32c));
  }
  return w.finish();
}

export function encodeConcat(parts: Range[]): Uint8Array {
  const w = new Writer();
  for (const p of parts) w.bytes(1, encodeRange(p));
  return w.finish();
}

/** The SourceTable of `sources`, with the spec revision when given (spec §6). */
export function encodeSourceTable(sources: Source[], revision?: number): Uint8Array {
  const w = new Writer();
  for (const s of sources) {
    const m = new Writer();
    if (s.url !== undefined) m.bytes(1, utf8.encode(s.url));
    else if (s.key !== undefined) m.bytes(2, utf8.encode(s.key));
    else if (s.data !== undefined) m.bytes(3, s.data);
    if (s.size !== undefined) m.uint(4, s.size);
    if (s.etag !== undefined) m.bytes(5, utf8.encode(s.etag));
    if (s.modifiedNotAfter !== undefined) m.uint(6, s.modifiedNotAfter);
    w.bytes(1, m.finish());
  }
  if (revision !== undefined) w.uint(2, BigInt(revision));
  return w.finish();
}

// ---- decoding

const strictUtf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

type Field = { field: number; wireType: number; value: bigint | Uint8Array };

function* fields(buf: Uint8Array): Generator<Field> {
  let pos = 0;
  const varint = (): bigint => {
    let v = 0n;
    for (let i = 0; i < 10; i++) {
      if (pos >= buf.length) throw new MalformedError("truncated varint");
      const b = buf[pos++];
      v |= BigInt(b & 0x7f) << BigInt(7 * i);
      if (!(b & 0x80)) {
        if (v > U64_MAX) throw new MalformedError("varint exceeds 2^64 - 1");
        return v;
      }
    }
    throw new MalformedError("varint longer than 10 bytes");
  };
  while (pos < buf.length) {
    const key = varint();
    const field = Number(key >> 3n);
    const wireType = Number(key & 7n);
    if (key >> 3n === 0n || key >> 3n > (1n << 29n) - 1n) {
      throw new MalformedError(`invalid field number ${key >> 3n}`);
    }
    let value: bigint | Uint8Array;
    switch (wireType) {
      case 0:
        value = varint();
        break;
      case 1:
      case 5: {
        const n = wireType === 1 ? 8 : 4;
        if (pos + n > buf.length) throw new MalformedError("truncated field");
        value = buf.subarray(pos, pos + n);
        pos += n;
        break;
      }
      case 2: {
        const n = varint();
        if (BigInt(pos) + n > BigInt(buf.length)) {
          throw new MalformedError("LEN field extends past the message");
        }
        value = buf.subarray(pos, pos + Number(n));
        pos += Number(n);
        break;
      }
      default:
        throw new MalformedError(`wire type ${wireType}`);
    }
    yield { field, wireType, value };
  }
}

function expect<T extends "varint" | "len">(
  f: Field,
  kind: T,
): T extends "varint" ? bigint : Uint8Array {
  if (f.wireType !== (kind === "varint" ? 0 : 2)) {
    throw new MalformedError(`field ${f.field} has wire type ${f.wireType}`);
  }
  return f.value as never;
}

function string(f: Field): string {
  try {
    return strictUtf8.decode(expect(f, "len"));
  } catch (e) {
    if (e instanceof MalformedError) throw e;
    throw new MalformedError(`field ${f.field} is not valid UTF-8`);
  }
}

/** Decodes a Range; `numSources` checks source ranges (spec §5.2). */
export function decodeRange(buf: Uint8Array, numSources: number): Range {
  let source = 0n;
  let offset = 0n;
  let length = 0n;
  let data: Uint8Array | undefined;
  let crc: bigint | undefined;
  for (const f of fields(buf)) {
    switch (f.field) {
      case 1:
        source = expect(f, "varint");
        if (source > U32_MAX) throw new MalformedError("source exceeds uint32");
        break;
      case 6:
        crc = expect(f, "varint");
        if (crc > U32_MAX) throw new MalformedError("crc32c exceeds uint32");
        break;
      case 3:
        offset = expect(f, "varint");
        break;
      case 4:
        length = expect(f, "varint");
        break;
      case 5:
        data = expect(f, "len");
        break;
    }
  }
  if (data !== undefined) {
    if (source !== 0n || offset !== 0n || length !== 0n) {
      throw new MalformedError("literal range with source, offset or length");
    }
    if (crc !== undefined) throw new MalformedError("literal range with a crc32c");
    return { data };
  }
  if (source >= BigInt(numSources)) {
    throw new MalformedError(`source ${source} >= ${numSources} sources`);
  }
  if (offset + length > U64_MAX) {
    throw new MalformedError("offset + length exceeds 2^64 - 1");
  }
  return crc === undefined
    ? { source: Number(source), offset, length }
    : { source: Number(source), offset, length, crc32c: Number(crc) };
}

export function rangeSize(r: Range): bigint {
  return "data" in r ? BigInt(r.data.length) : r.length;
}

/** Decodes a reference payload (spec §4.3) into its list of ranges. */
export function decodeReference(
  headerId: number,
  payload: Uint8Array,
  numSources: number,
): Range[] {
  if (payload.length > 65519) {
    throw new MalformedError("reference payload exceeds 65519 bytes");
  }
  let parts: Range[];
  if (headerId === 0x7a76) {
    parts = [decodeRange(payload, numSources)];
  } else {
    parts = [];
    for (const f of fields(payload)) {
      if (f.field === 1) parts.push(decodeRange(expect(f, "len"), numSources));
    }
  }
  if (parts.reduce((n, p) => n + rangeSize(p), 0n) > U64_MAX) {
    throw new MalformedError("reference size exceeds 2^64 - 1");
  }
  return parts;
}

const STRONG_ETAG = /^"[\x21\x23-\x7e]*"$/;

export function decodeSourceTable(buf: Uint8Array): Source[] {
  return decodeTable(buf).sources;
}

/** The sources of a SourceTable, and the spec revision it records (spec §6). */
export function decodeTable(buf: Uint8Array): { sources: Source[]; revision?: number } {
  const sources: Source[] = [];
  let revision: number | undefined;
  for (const f of fields(buf)) {
    if (f.field === 2) {
      const v = expect(f, "varint");
      if (v > U32_MAX) throw new MalformedError("revision exceeds uint32");
      revision = Number(v);
      continue;
    }
    if (f.field !== 1) continue;
    const s: Source = {};
    let kind: "url" | "key" | "data" | undefined;
    for (const g of fields(expect(f, "len"))) {
      switch (g.field) {
        case 1:
          s.url = string(g);
          kind = "url";
          break;
        case 2:
          s.key = string(g);
          kind = "key";
          break;
        case 3:
          s.data = expect(g, "len");
          kind = "data";
          break;
        case 4:
          s.size = expect(g, "varint");
          break;
        case 5:
          s.etag = string(g);
          break;
        case 6: {
          const v = expect(g, "varint");
          s.modifiedNotAfter = v > (1n << 63n) - 1n ? v - (1n << 64n) : v;
          break;
        }
      }
    }
    if (kind === undefined) throw new MalformedError("a source has no kind");
    // The last oneof member on the wire wins.
    for (const k of ["url", "key", "data"] as const) {
      if (k !== kind) delete s[k];
    }
    if ((kind === "url" || kind === "key") && s[kind] === "") {
      throw new MalformedError(`empty ${kind} source`);
    }
    const pinned =
      s.size !== undefined ||
      s.etag !== undefined ||
      s.modifiedNotAfter !== undefined;
    if (pinned && kind !== "url") {
      throw new MalformedError(`pin on a ${kind} source`);
    }
    if (s.etag !== undefined && !STRONG_ETAG.test(s.etag)) {
      throw new MalformedError(`etag ${s.etag} is not a strong entity tag`);
    }
    sources.push(s);
  }
  return revision === undefined ? { sources } : { sources, revision };
}
