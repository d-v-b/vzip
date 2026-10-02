// Lite-variant (LV) decoder (VIRTUALIZE.md §4.2).

import { inflateSync } from "node:zlib";
import { reject, dv } from "./util.ts";

export type LVValue = number | boolean | string | number[] | LVLevel;

// A level keeps its records in order; whether it is an "object" (named
// members) or a "list" (all names empty) only matters for lookups, which
// use the last record of a given name, and for "members", which are the
// values in order. Both views are available on the same structure.
export class LVLevel {
  recs: Array<[string, LVValue]>;
  constructor(recs: Array<[string, LVValue]>) {
    this.recs = recs;
  }
  get isList(): boolean {
    return this.recs.every(([n]) => n === "");
  }
  get(name: string): LVValue | undefined {
    for (let i = this.recs.length - 1; i >= 0; i--) if (this.recs[i][0] === name) return this.recs[i][1];
    return undefined;
  }
  members(): LVValue[] {
    if (this.isList) return this.recs.map(([, v]) => v);
    // object: one member per distinct name, in order of first appearance, with the last value
    const seen = new Map<string, LVValue>();
    for (const [n, v] of this.recs) seen.set(n, v);
    return [...seen.values()];
  }
}

function utf16(b: Uint8Array, pos: number, units: number): string {
  let s = "";
  for (let i = 0; i < units; i++) {
    const u = b[pos + 2 * i] | (b[pos + 2 * i + 1] << 8);
    if (u === 0) break;
    s += String.fromCharCode(u);
  }
  return s;
}

function need(b: Uint8Array, pos: number, n: number, end: number): void {
  if (pos + n > end || pos + n > b.length) reject(`LV structure truncated at ${pos}`);
}

function parseSeq(b: Uint8Array, start: number, end: number, count: number | null): { recs: Array<[string, LVValue]>; pos: number } {
  const d = dv(b);
  const recs: Array<[string, LVValue]> = [];
  let pos = start;
  while (count === null ? pos < end : recs.length < count) {
    const recStart = pos;
    need(b, pos, 2, end);
    const type = b[pos];
    const k = b[pos + 1];
    pos += 2;
    if (type === 76) {
      need(b, pos, 10, end);
      pos += 10;
      let inflated: Uint8Array;
      try {
        inflated = new Uint8Array(inflateSync(b.subarray(pos, end)));
      } catch (e) {
        return reject(`LV compressed record: bad zlib stream (${(e as Error).message})`);
      }
      const inner = parseSeq(inflated, 0, inflated.length, null);
      return { recs: inner.recs, pos: end };
    }
    need(b, pos, 2 * k, end);
    const name = utf16(b, pos, k);
    pos += 2 * k;
    let value: LVValue;
    switch (type) {
      case 1:
        need(b, pos, 1, end);
        value = b[pos] !== 0;
        pos += 1;
        break;
      case 2:
        need(b, pos, 4, end);
        value = d.getInt32(pos, true);
        pos += 4;
        break;
      case 3:
        need(b, pos, 4, end);
        value = d.getUint32(pos, true);
        pos += 4;
        break;
      case 4:
        need(b, pos, 8, end);
        value = Number(d.getBigInt64(pos, true));
        pos += 8;
        break;
      case 5:
      case 7:
        need(b, pos, 8, end);
        value = Number(d.getBigUint64(pos, true));
        pos += 8;
        break;
      case 6:
        need(b, pos, 8, end);
        value = d.getFloat64(pos, true);
        pos += 8;
        break;
      case 8: {
        let s = "";
        for (;;) {
          need(b, pos, 2, end);
          const u = d.getUint16(pos, true);
          pos += 2;
          if (u === 0) break;
          s += String.fromCharCode(u);
        }
        value = s;
        break;
      }
      case 9: {
        need(b, pos, 8, end);
        const n = Number(d.getBigUint64(pos, true));
        pos += 8;
        need(b, pos, n, end);
        value = Array.from(b.subarray(pos, pos + n));
        pos += n;
        break;
      }
      case 11: {
        need(b, pos, 12, end);
        const c = d.getUint32(pos, true);
        const L = Number(d.getBigUint64(pos + 4, true));
        pos += 12;
        const recEnd = recStart + L;
        if (recEnd < pos || recEnd > end) reject(`LV level ${name}: bad length ${L}`);
        const inner = parseSeq(b, pos, recEnd, c);
        if (inner.pos !== recEnd) reject(`LV level ${name}: records end at ${inner.pos}, length says ${recEnd}`);
        pos = recEnd + 8 * c;
        if (pos > end) reject(`LV level ${name}: trailing offsets past end`);
        value = new LVLevel(inner.recs);
        break;
      }
      default:
        return reject(`LV record type ${type} unknown at ${recStart}`);
    }
    recs.push([name, value]);
  }
  return { recs, pos };
}

export function decodeLV(b: Uint8Array): LVLevel {
  return new LVLevel(parseSeq(b, 0, b.length, null).recs);
}

export function lvGet(v: LVValue | undefined, path: string): LVValue | undefined {
  for (const part of path.split("/")) {
    if (!(v instanceof LVLevel)) return undefined;
    v = v.get(part);
  }
  return v;
}

export function lvMembers(v: LVValue | undefined): LVValue[] | undefined {
  if (v instanceof LVLevel) return v.members();
  if (Array.isArray(v)) return v;
  return undefined;
}

export function lvNum(v: LVValue | undefined): number | undefined {
  if (typeof v === "number") return v;
  if (typeof v === "boolean") return v ? 1 : 0;
  return undefined;
}
