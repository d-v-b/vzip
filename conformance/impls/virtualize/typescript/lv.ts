// The ND2 lite-variant structure, spec/virtualize.md §4.2.

import { inflateSync } from "node:zlib";
import { MAX_SAFE, reject } from "./lib.ts";

export type LV =
  | { k: "scalar"; type: number; v: number | bigint }
  | { k: "string"; v: string }
  | { k: "bytes"; v: Uint8Array }
  | { k: "object"; m: Map<string, LV> }
  | { k: "list"; a: LV[] };

export const MAX_DEPTH = 100;

const utf16 = new TextDecoder("utf-16le");

class Parser {
  b: Uint8Array;
  dv: DataView;
  maxDepth = 0;
  constructor(b: Uint8Array) {
    this.b = b;
    this.dv = new DataView(b.buffer, b.byteOffset, b.byteLength);
  }
  need(p: number, n: number): void {
    if (p + n > this.b.length) reject("LV record runs past the end of its data");
  }
  /** Parses one record at p; returns [name, value, end]. */
  record(p: number, depth: number): [string, LV, number] {
    const start = p;
    this.need(p, 2);
    const type = this.b[p];
    const k = this.b[p + 1];
    p += 2;
    this.need(p, 2 * k);
    let nameUnits = k;
    for (let i = 0; i < k; i++) {
      if (this.dv.getUint16(p + 2 * i, true) === 0) {
        nameUnits = i;
        break;
      }
    }
    const name = utf16.decode(this.b.subarray(p, p + 2 * nameUnits));
    p += 2 * k;
    let v: LV;
    switch (type) {
      case 1:
        this.need(p, 1);
        v = { k: "scalar", type, v: this.b[p] };
        p += 1;
        break;
      case 2:
        this.need(p, 4);
        v = { k: "scalar", type, v: this.dv.getInt32(p, true) };
        p += 4;
        break;
      case 3:
        this.need(p, 4);
        v = { k: "scalar", type, v: this.dv.getUint32(p, true) };
        p += 4;
        break;
      case 4:
        this.need(p, 8);
        v = { k: "scalar", type, v: this.dv.getBigInt64(p, true) };
        p += 8;
        break;
      case 5:
      case 7:
        this.need(p, 8);
        v = { k: "scalar", type, v: this.dv.getBigUint64(p, true) };
        p += 8;
        break;
      case 6:
        this.need(p, 8);
        v = { k: "scalar", type, v: this.dv.getFloat64(p, true) };
        p += 8;
        break;
      case 8: {
        let q = p;
        for (;;) {
          this.need(q, 2);
          if (this.dv.getUint16(q, true) === 0) break;
          q += 2;
        }
        v = { k: "string", v: utf16.decode(this.b.subarray(p, q)) };
        p = q + 2;
        break;
      }
      case 9: {
        this.need(p, 8);
        const len = this.dv.getBigUint64(p, true);
        p += 8;
        if (len > BigInt(this.b.length - p)) reject("LV byte array runs past the end of its data");
        const n = Number(len);
        v = { k: "bytes", v: this.b.subarray(p, p + n) };
        p += n;
        break;
      }
      case 11: {
        this.need(p, 12);
        const c = this.dv.getUint32(p, true);
        const L = this.dv.getBigUint64(p + 4, true);
        p += 12;
        if (depth + 1 > MAX_DEPTH) reject("LV levels nested more than 100 deep");
        const recs: [string, LV][] = [];
        for (let i = 0; i < c; i++) {
          const [nm, val, e] = this.record(p, depth + 1);
          recs.push([nm, val]);
          p = e;
        }
        if (BigInt(p - start) !== L) reject("LV level does not end at its length");
        this.need(p, 8 * c);
        p += 8 * c;
        v = toValue(recs, false);
        break;
      }
      default:
        reject(`LV record type ${type}`);
    }
    return [name, v, p];
  }
}

function toValue(recs: [string, LV][], top: boolean): LV {
  if (!top && recs.length > 0 && recs.every(([n]) => n === "")) return { k: "list", a: recs.map(([, v]) => v) };
  const m = new Map<string, LV>();
  for (const [n, v] of recs) m.set(n, v); // Map keeps first-insertion order; set replaces the value
  return { k: "object", m };
}

function parseRecords(b: Uint8Array): LV {
  const p = new Parser(b);
  const recs: [string, LV][] = [];
  let pos = 0;
  while (pos < b.length) {
    const [n, v, e] = p.record(pos, 0);
    recs.push([n, v]);
    pos = e;
  }
  return toValue(recs, true);
}

function inflateExact(z: Uint8Array): Uint8Array {
  let out: Uint8Array;
  try {
    out = inflateSync(z);
  } catch (e) {
    reject(`bad zlib stream: ${(e as Error).message}`);
  }
  // The stream must end exactly at the end of the data: without its last byte it must be incomplete.
  let shorterOk = true;
  try {
    inflateSync(z.subarray(0, z.length - 1));
  } catch {
    shorterOk = false;
  }
  if (shorterOk) reject("zlib stream ends before the end of the chunk's data");
  return out;
}

/** Parses a metadata chunk's data into its top-level object. */
export function parseChunk(data: Uint8Array): LV {
  if (data.length > 0 && data[0] === 76) {
    if (data.length < 12) reject("compressed LV record too short");
    const inner = inflateExact(data.subarray(12));
    if (inner.length > 0 && inner[0] === 76) reject("nested compressed LV record");
    return parseRecords(inner);
  }
  return parseRecords(data);
}

// ---- Typed access (§4.2 "Paths") ----

export function isObj(v: LV): v is { k: "object"; m: Map<string, LV> } {
  return v.k === "object";
}

export function member(obj: LV, name: string): LV | undefined {
  if (obj.k !== "object") reject(`path step "${name}" from a value that is not an object`);
  return obj.m.get(name);
}

export function asNumber(v: LV, what: string): number {
  if (v.k !== "scalar" || v.type < 2 || v.type > 6) reject(`${what} is not a number`);
  const x = Number(v.v);
  if (!Number.isFinite(x)) reject(`${what} is not finite`);
  return x;
}

export function asInteger(v: LV, what: string): number {
  const x = asNumber(v, what);
  if (!Number.isInteger(x) || x < 0 || x > MAX_SAFE) reject(`${what} is not an integer in range`);
  return x;
}

export function asColor(v: LV, what: string): number {
  const x = asNumber(v, what);
  if (!Number.isInteger(x) || x < -(2 ** 31) || x > 2 ** 32 - 1) reject(`${what} is not a color`);
  return x < 0 ? x + 2 ** 32 : x;
}

export function asFlag(v: LV, what: string): boolean {
  if (v.k !== "scalar" || v.type < 1 || v.type > 5) reject(`${what} is not a flag`);
  return v.v !== 0 && v.v !== 0n;
}

export function asString(v: LV, what: string): string {
  if (v.k !== "string") reject(`${what} is not a string`);
  return v.v;
}

export function asObject(v: LV, what: string): LV {
  if (v.k !== "object") reject(`${what} is not an object`);
  return v;
}

/** The members of an object or a list (a byte array is a list of type-3 values). */
export function members(v: LV, what: string, allowObject = true): LV[] {
  if (v.k === "list") return v.a;
  if (v.k === "bytes") return Array.from(v.v, (x) => ({ k: "scalar", type: 3, v: x }) as LV);
  if (v.k === "object" && allowObject) return [...v.m.values()];
  reject(`${what} is not ${allowObject ? "an object or a list" : "a list"}`);
}

export function asList(v: LV, what: string): LV[] {
  return members(v, what, false);
}
