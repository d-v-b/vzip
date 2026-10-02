// ND2 lite variant decoder (VIRTUALIZE.md §4.2).
import { inflateSync } from "node:zlib";
import { MAX_SAFE, reject } from "./io.ts";

export type LvLevel = { kind: "object"; members: Map<string, LvValue> } | { kind: "list"; items: LvValue[] };

// t: record type (1-9, 11), or 0 for a byte of a byte array
export type LvValue =
  | { t: 1 | 2 | 3 | 6 | 0; v: number }
  | { t: 4 | 5; v: bigint }
  | { t: 7; v: null }
  | { t: 8; v: string }
  | { t: 9; v: LvLevel }
  | { t: 11; v: LvLevel };

function decodeUtf16(units: number[]): string {
  let s = "";
  for (let i = 0; i < units.length; i++) {
    const u = units[i];
    if (u >= 0xd800 && u <= 0xdbff) {
      const w = units[i + 1];
      if (w !== undefined && w >= 0xdc00 && w <= 0xdfff) {
        s += String.fromCharCode(u, w);
        i++;
      } else s += "�";
    } else if (u >= 0xdc00 && u <= 0xdfff) s += "�";
    else s += String.fromCharCode(u);
  }
  return s;
}

class Parser {
  b: Uint8Array;
  dv: DataView;
  constructor(b: Uint8Array) {
    this.b = b;
    this.dv = new DataView(b.buffer, b.byteOffset, b.byteLength);
  }

  need(p: number, n: number, end: number): void {
    if (p + n > end) reject("LV: record runs past the end of its data");
  }

  /** Parses records in [p, end) until count records (or end, if count is null). Returns [records, pos]. */
  records(p: number, end: number, count: number | null): [Array<[string, LvValue]>, number] {
    const recs: Array<[string, LvValue]> = [];
    while (count === null ? p < end : recs.length < count) {
      const [name, val, q] = this.record(p, end);
      recs.push([name, val]);
      p = q;
    }
    return [recs, p];
  }

  record(p: number, end: number): [string, LvValue, number] {
    const start = p;
    this.need(p, 2, end);
    const type = this.b[p];
    const k = this.b[p + 1];
    p += 2;
    this.need(p, 2 * k, end);
    const units: number[] = [];
    for (let i = 0; i < k; i++) {
      const u = this.dv.getUint16(p + 2 * i, true);
      if (u === 0) break;
      units.push(u);
    }
    const name = decodeUtf16(units);
    p += 2 * k;
    let val: LvValue;
    switch (type) {
      case 1:
        this.need(p, 1, end);
        val = { t: 1, v: this.b[p] };
        p += 1;
        break;
      case 2:
        this.need(p, 4, end);
        val = { t: 2, v: this.dv.getInt32(p, true) };
        p += 4;
        break;
      case 3:
        this.need(p, 4, end);
        val = { t: 3, v: this.dv.getUint32(p, true) };
        p += 4;
        break;
      case 4:
        this.need(p, 8, end);
        val = { t: 4, v: this.dv.getBigInt64(p, true) };
        p += 8;
        break;
      case 5:
        this.need(p, 8, end);
        val = { t: 5, v: this.dv.getBigUint64(p, true) };
        p += 8;
        break;
      case 6:
        this.need(p, 8, end);
        val = { t: 6, v: this.dv.getFloat64(p, true) };
        p += 8;
        break;
      case 7:
        this.need(p, 8, end);
        val = { t: 7, v: null };
        p += 8;
        break;
      case 8: {
        const units: number[] = [];
        for (;;) {
          this.need(p, 2, end);
          const u = this.dv.getUint16(p, true);
          p += 2;
          if (u === 0) break;
          units.push(u);
        }
        val = { t: 8, v: decodeUtf16(units) };
        break;
      }
      case 9: {
        this.need(p, 8, end);
        const bl = this.dv.getBigUint64(p, true);
        p += 8;
        if (bl > BigInt(end - p)) reject("LV: byte array runs past the end of its data");
        const n = Number(bl);
        const items: LvValue[] = [];
        for (let i = 0; i < n; i++) items.push({ t: 0, v: this.b[p + i] });
        p += n;
        val = { t: 9, v: { kind: "list", items } };
        break;
      }
      case 11: {
        this.need(p, 12, end);
        const c = this.dv.getUint32(p, true);
        const L = this.dv.getBigUint64(p + 4, true);
        p += 12;
        if (L > BigInt(end - start)) reject("LV: level length runs past the end of its data");
        const lend = start + Number(L);
        const [recs, q] = this.records(p, lend, c);
        if (q !== lend) reject(`LV: level "${name}" records end at ${q - start}, not at its length ${L}`);
        p = q;
        this.need(p, 8 * c, end);
        p += 8 * c;
        val = { t: 11, v: makeLevel(recs) };
        break;
      }
      default:
        reject(`LV: unknown record type ${type}`);
    }
    return [name, val, p];
  }
}

function makeLevel(recs: Array<[string, LvValue]>): LvLevel {
  if (recs.length > 0 && recs.every(([n]) => n === "")) return { kind: "list", items: recs.map(([, v]) => v) };
  const members = new Map<string, LvValue>();
  for (const [n, v] of recs) members.set(n, v); // Map keeps first-insertion order, last value
  return { kind: "object", members };
}

/** Decodes a metadata chunk's data into its LV structure (an object or a list). */
export function decodeLv(data: Uint8Array): LvLevel {
  if (data.length > 0 && data[0] === 76) {
    if (data.length < 12) reject("LV: compressed record too short");
    const stream = data.subarray(12);
    let res: { buffer: Buffer; engine: { bytesWritten: number } };
    try {
      res = inflateSync(stream, { info: true }) as unknown as typeof res;
    } catch (e) {
      reject(`LV: bad zlib stream: ${(e as Error).message}`);
    }
    if (res.engine.bytesWritten !== stream.length) reject("LV: zlib stream does not end at the end of the chunk");
    return decodeLvPlain(new Uint8Array(res.buffer.buffer, res.buffer.byteOffset, res.buffer.byteLength));
  }
  return decodeLvPlain(data);
}

function decodeLvPlain(data: Uint8Array): LvLevel {
  const p = new Parser(data);
  const [recs] = p.records(0, data.length, null);
  return makeLevel(recs);
}

// ---------------------------------------------------------------- access

export function asLevel(v: LvValue, what: string): LvLevel {
  if (v.t === 11 || v.t === 9) return v.v;
  reject(`${what} is not a level`);
}

/** Member at a path below a level; undefined if missing. Each step must be an object. */
export function get(root: LvLevel | undefined, path: string): LvValue | undefined {
  if (!root) return undefined;
  let cur: LvLevel = root;
  const parts = path.split("/");
  for (let i = 0; i < parts.length; i++) {
    if (cur.kind !== "object") reject(`LV: "${parts.slice(0, i).join("/")}" is not an object`);
    const v = cur.members.get(parts[i]);
    if (v === undefined) return undefined;
    if (i === parts.length - 1) return v;
    if (v.t !== 11) reject(`LV: "${parts.slice(0, i + 1).join("/")}" is not a level`);
    cur = v.v;
  }
  return undefined;
}

export function getLevel(root: LvLevel | undefined, path: string): LvLevel | undefined {
  const v = get(root, path);
  if (v === undefined) return undefined;
  if (v.t !== 11) reject(`LV: "${path}" is not a level`);
  return v.v;
}

/** A value used as a number (types 2-6). */
export function numOf(v: LvValue, what: string): number {
  if (v.t === 2 || v.t === 3 || v.t === 6 || v.t === 0) return v.v;
  if (v.t === 4 || v.t === 5) {
    if (v.v > BigInt(MAX_SAFE) || v.v < -BigInt(MAX_SAFE)) reject(`${what}: 64-bit value ${v.v} out of range`);
    return Number(v.v);
  }
  reject(`${what}: type ${v.t} cannot be used as a number`);
}

/** A value used as a flag (types 1-5). */
export function flagOf(v: LvValue, what: string): boolean {
  if (v.t === 1 || v.t === 2 || v.t === 3 || v.t === 0) return v.v !== 0;
  if (v.t === 4 || v.t === 5) return v.v !== 0n;
  reject(`${what}: type ${v.t} cannot be used as a flag`);
}

export function num(root: LvLevel | undefined, path: string, def?: number): number {
  const v = get(root, path);
  if (v === undefined) {
    if (def === undefined) reject(`LV: required member "${path}" missing`);
    return def;
  }
  return numOf(v, path);
}

/** A number that must be a non-negative integer (sizes, counts, enumerations). */
export function uint(root: LvLevel | undefined, path: string, def?: number): number {
  const n = num(root, path, def);
  if (!Number.isSafeInteger(n) || n < 0) reject(`LV: "${path}" = ${n} is not a non-negative integer`);
  return n;
}

export function flag(root: LvLevel | undefined, path: string, def: boolean): boolean {
  const v = get(root, path);
  if (v === undefined) return def;
  return flagOf(v, path);
}

/** "The members of" a level or byte array: its values in order. */
export function membersOf(v: LvValue, what: string): LvValue[] {
  if (v.t === 11 || v.t === 9) return v.v.kind === "list" ? v.v.items : [...v.v.members.values()];
  reject(`${what} has no members (type ${v.t})`);
}
