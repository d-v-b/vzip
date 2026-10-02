// The lite-variant (LV) decoder of VIRTUALIZE.md §4.2.

import { inflateSync } from "node:zlib";
import { MAX_SAFE, reject } from "./io.ts";

export type LV =
  | { k: "bool"; t: 1; v: number }
  | { k: "int"; t: 2 | 3; v: number }
  | { k: "big"; t: 4 | 5; v: bigint }
  | { k: "f64"; t: 6; v: number }
  | { k: "ptr"; t: 7 }
  | { k: "str"; t: 8; v: string }
  | { k: "bytes"; t: 9; v: Uint8Array }
  | { k: "obj"; t: 11; v: Map<string, LV> }
  | { k: "list"; t: 11; v: LV[] };

const utf16 = new TextDecoder("utf-16le");

type Frame = { items: [string, LV][]; left: number; recStart: number; L: bigint; c: number; name: string };

function makeLevel(items: [string, LV][], top: boolean): LV {
  if (!top && items.length > 0 && items.every(([n]) => n === "")) return { k: "list", t: 11, v: items.map((i) => i[1]) };
  const m = new Map<string, LV>();
  for (const [n, v] of items) m.set(n, v); // Map keeps first-appearance order, last value
  return { k: "obj", t: 11, v: m };
}

function parseRecords(d: Uint8Array): Map<string, LV> {
  const dv = new DataView(d.buffer, d.byteOffset, d.byteLength);
  const len = d.length;
  const need = (p: number, n: number) => {
    if (n > len - p) reject("LV record runs past the end of the data");
  };
  const root: Frame = { items: [], left: -1, recStart: 0, L: 0n, c: 0, name: "" };
  const stack: Frame[] = [root];
  let p = 0;
  for (;;) {
    const f = stack[stack.length - 1];
    if (f === root ? p >= len : f.left === 0) {
      if (f === root) break;
      if (BigInt(p - f.recStart) !== f.L) reject("LV level length mismatch");
      need(p, 8 * f.c);
      p += 8 * f.c;
      stack.pop();
      stack[stack.length - 1].items.push([f.name, makeLevel(f.items, false)]);
      continue;
    }
    if (f !== root) f.left--;
    const recStart = p;
    need(p, 2);
    const type = d[p];
    const k = d[p + 1];
    p += 2;
    need(p, 2 * k);
    let nameEnd = p;
    while (nameEnd < p + 2 * k && (d[nameEnd] | d[nameEnd + 1]) !== 0) nameEnd += 2;
    const name = utf16.decode(d.subarray(p, nameEnd));
    p += 2 * k;
    let v: LV;
    switch (type) {
      case 1: need(p, 1); v = { k: "bool", t: 1, v: d[p] }; p += 1; break;
      case 2: need(p, 4); v = { k: "int", t: 2, v: dv.getInt32(p, true) }; p += 4; break;
      case 3: need(p, 4); v = { k: "int", t: 3, v: dv.getUint32(p, true) }; p += 4; break;
      case 4: need(p, 8); v = { k: "big", t: 4, v: dv.getBigInt64(p, true) }; p += 8; break;
      case 5: need(p, 8); v = { k: "big", t: 5, v: dv.getBigUint64(p, true) }; p += 8; break;
      case 6: need(p, 8); v = { k: "f64", t: 6, v: dv.getFloat64(p, true) }; p += 8; break;
      case 7: need(p, 8); v = { k: "ptr", t: 7 }; p += 8; break;
      case 8: {
        let e = p;
        for (;;) {
          need(e, 2);
          if ((d[e] | d[e + 1]) === 0) break;
          e += 2;
        }
        v = { k: "str", t: 8, v: utf16.decode(d.subarray(p, e)) };
        p = e + 2;
        break;
      }
      case 9: {
        need(p, 8);
        const b = dv.getBigUint64(p, true);
        p += 8;
        if (b > BigInt(len - p)) reject("LV byte array runs past the end of the data");
        v = { k: "bytes", t: 9, v: d.subarray(p, p + Number(b)) };
        p += Number(b);
        break;
      }
      case 11: {
        need(p, 12);
        const c = dv.getUint32(p, true);
        const L = dv.getBigUint64(p + 4, true);
        p += 12;
        stack.push({ items: [], left: c, recStart, L, c, name });
        continue;
      }
      default:
        reject(`LV record type ${type}`);
    }
    f.items.push([name, v]);
  }
  return (makeLevel(root.items, true) as { v: Map<string, LV> }).v;
}

/** Decode a metadata chunk's data (possibly a single compressed record). */
export function decodeChunk(data: Uint8Array): Map<string, LV> {
  if (data.length > 0 && data[0] === 76) {
    if (data.length < 12) reject("compressed LV record truncated");
    const z = data.subarray(12);
    let res: { buffer: Buffer; engine: { bytesWritten: number } };
    try {
      res = inflateSync(z, { info: true }) as unknown as { buffer: Buffer; engine: { bytesWritten: number } };
    } catch (e) {
      reject(`bad zlib stream in compressed LV record: ${(e as Error).message}`);
    }
    if (res.engine.bytesWritten !== z.length) reject("zlib stream does not end at the end of the chunk data");
    const inner = new Uint8Array(res.buffer.buffer, res.buffer.byteOffset, res.buffer.byteLength);
    if (inner.length > 0 && inner[0] === 76) reject("nested compressed LV record");
    return parseRecords(inner);
  }
  return parseRecords(data);
}

// ---- typed access (§4.2 "Paths") ----

export function isObj(v: LV): v is { k: "obj"; t: 11; v: Map<string, LV> } {
  return v.k === "obj";
}

/** Look up `path` from object `o`; a step from a non-object rejects; missing gives undefined. */
export function at(o: LV | Map<string, LV>, path: string): LV | undefined {
  let cur: LV | undefined = o instanceof Map ? { k: "obj", t: 11, v: o } : o;
  for (const step of path.split("/")) {
    if (cur === undefined) return undefined;
    if (cur.k !== "obj") reject(`path ${path}: step through a non-object`);
    cur = cur.v.get(step);
  }
  return cur;
}

export function asNumber(v: LV, what: string): number {
  let x: number;
  if (v.k === "int" || v.k === "f64") x = v.v;
  else if (v.k === "big") x = Number(v.v);
  else reject(`${what}: not a number (type ${v.t})`);
  if (!Number.isFinite(x)) reject(`${what}: not finite`);
  return x;
}

export function asInt(v: LV, what: string): number {
  const x = asNumber(v, what);
  if (!Number.isInteger(x) || x < 0 || x > MAX_SAFE) reject(`${what}: not an integer in range: ${x}`);
  return x === 0 ? 0 : x;
}

export function asColor(v: LV, what: string): number {
  const x = asNumber(v, what);
  if (!Number.isInteger(x) || x < -(2 ** 31) || x > 2 ** 32 - 1) reject(`${what}: not a color: ${x}`);
  return x < 0 ? x + 2 ** 32 : x;
}

export function asFlag(v: LV, what: string): boolean {
  if (v.k === "bool" || v.k === "int") return v.v !== 0;
  if (v.k === "big") return v.v !== 0n;
  reject(`${what}: not a flag (type ${v.t})`);
}

export function asString(v: LV, what: string): string {
  if (v.k !== "str") reject(`${what}: not a string (type ${v.t})`);
  return v.v;
}

export function asObject(v: LV, what: string): Map<string, LV> {
  if (v.k !== "obj") reject(`${what}: not an object`);
  return v.v;
}

/** A list (a byte array is a list of type-3 values). */
export function asList(v: LV, what: string): LV[] {
  if (v.k === "list") return v.v;
  if (v.k === "bytes") return Array.from(v.v, (b) => ({ k: "int", t: 3, v: b }) as LV);
  reject(`${what}: not a list`);
}

/** The members of an object or a list, in order. */
export function members(v: LV, what: string): LV[] {
  if (v.k === "obj") return [...v.v.values()];
  return asList(v, what);
}
