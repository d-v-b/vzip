// Every tag of a TIFF's IFDs as JSON: the source metadata of the TIFF and NDPI
// conventions (conventions/tiff/README.md §5).

import { base64, type ByteReader, decodeText, jsonNumber } from "../common.ts";

// Bytes per value of each TIFF 6.0 and BigTIFF field type.
export const SIZES: Record<number, number> = {
  1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8,
};
// Tags whose values locate the file's own structure (strips, tiles, IFDs): only
// their type and count are recorded.
export const STRUCTURE = new Set([273, 279, 288, 289, 324, 325, 330, 513, 514, 34665, 34853, 40965]);
const MAX_VALUE_BYTES = 2 ** 26; // the most tag value bytes recorded per file

/** An IFD entry: its value is `inline` (the value field's bytes), or at `offset`
 * (undefined when it cannot be in the file), or, for NDPI's 64-bit LONGs, `value`. */
export interface Entry {
  tag: number;
  type: number;
  count: bigint;
  inline?: Uint8Array;
  offset?: number;
  value?: number[];
}

function valueJson(data: Uint8Array, type: number, count: number, le: boolean): unknown {
  if (type === 1 || type === 7) return base64(data);
  if (type === 2) return decodeText(data.length > 0 && data[data.length - 1] === 0 ? data.subarray(0, -1) : data);
  const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
  if (type === 5 || type === 10) {
    const out: number[][] = [];
    for (let i = 0; i < count; i++) {
      out.push(type === 5
        ? [view.getUint32(8 * i, le), view.getUint32(8 * i + 4, le)]
        : [view.getInt32(8 * i, le), view.getInt32(8 * i + 4, le)]);
    }
    return out;
  }
  const out: (number | string)[] = [];
  for (let i = 0; i < count; i++) {
    switch (type) {
      case 3: out.push(view.getUint16(2 * i, le)); break;
      case 4: case 13: out.push(view.getUint32(4 * i, le)); break;
      case 6: out.push(view.getInt8(i)); break;
      case 8: out.push(view.getInt16(2 * i, le)); break;
      case 9: out.push(view.getInt32(4 * i, le)); break;
      case 11: out.push(jsonNumber(view.getFloat32(4 * i, le))); break;
      case 12: out.push(jsonNumber(view.getFloat64(8 * i, le))); break;
      case 16: case 18: out.push(jsonNumber(view.getBigUint64(8 * i, le))); break;
      case 17: out.push(jsonNumber(view.getBigInt64(8 * i, le))); break;
    }
  }
  return out;
}

/** Translates IFDs in output order, sharing one value budget across the file.
 * `tags` decides what to record synchronously, then reads the values. */
export class Translator {
  private used = 0;
  private read: ByteReader;
  private size: number;
  private le: boolean;
  private structure: Set<number>;
  constructor(read: ByteReader, size: number, le: boolean, structure: Set<number> = STRUCTURE) {
    this.read = read;
    this.size = size;
    this.le = le;
    this.structure = structure;
  }

  async tags(entries: Entry[]): Promise<Record<string, unknown>> {
    const first = new Map<number, Entry>();
    for (const e of entries) if (!first.has(e.tag)) first.set(e.tag, e);
    const out: Record<string, unknown> = {};
    const pending: Promise<void>[] = [];
    for (const tag of [...first.keys()].sort((a, b) => a - b)) {
      const e = first.get(tag)!;
      const m: Record<string, unknown> = { type: e.type, count: jsonNumber(e.count) };
      out[String(tag)] = m;
      const size = SIZES[e.type];
      if (this.structure.has(tag) || size === undefined) continue;
      const nbytes = BigInt(size) * e.count;
      const within = e.inline !== undefined
        || (e.offset !== undefined && BigInt(e.offset) + nbytes <= BigInt(this.size));
      if (!within || BigInt(this.used) + nbytes > BigInt(MAX_VALUE_BYTES)) continue;
      const n = Number(nbytes);
      this.used += n;
      const count = Number(e.count);
      if (e.value !== undefined) m.value = e.value.map(jsonNumber);
      else if (e.inline !== undefined) m.value = valueJson(e.inline.subarray(0, n), e.type, count, this.le);
      else pending.push(this.read(e.offset!, n).then((b) => { m.value = valueJson(b, e.type, count, this.le); }));
    }
    await Promise.all(pending);
    return out;
  }
}
