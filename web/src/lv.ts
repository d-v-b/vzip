// Nikon's "lite variant" (LV) metadata encoding (VIRTUALIZE.md §4.2).

export type LV = boolean | number | string | LV[] | { [name: string]: LV };

export class LVError extends Error {}

async function inflate(data: Uint8Array): Promise<Uint8Array> {
  const stream = new Blob([data as BlobPart]).stream().pipeThrough(new DecompressionStream("deflate"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

class Reader {
  bytes: Uint8Array;
  view: DataView;
  pos = 0;
  constructor(bytes: Uint8Array) {
    this.bytes = bytes;
    this.view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  }
  need(n: number) {
    if (this.pos + n > this.bytes.length) throw new LVError("truncated LV data");
  }
  u8() { this.need(1); return this.view.getUint8(this.pos++); }
  u32() { this.need(4); const v = this.view.getUint32(this.pos, true); this.pos += 4; return v; }
  i32() { this.need(4); const v = this.view.getInt32(this.pos, true); this.pos += 4; return v; }
  u64() { this.need(8); const v = this.view.getBigUint64(this.pos, true); this.pos += 8; return Number(v); }
  i64() { this.need(8); const v = this.view.getBigInt64(this.pos, true); this.pos += 8; return Number(v); }
  f64() { this.need(8); const v = this.view.getFloat64(this.pos, true); this.pos += 8; return v; }
  utf16(units: number) {
    this.need(2 * units);
    const s = new TextDecoder("utf-16le").decode(this.bytes.subarray(this.pos, this.pos + 2 * units));
    this.pos += 2 * units;
    return s;
  }
}

/** Decodes `count` records (or, with count undefined, records to the end). */
function records(r: Reader, end: number, count?: number): [string, LV][] {
  const out: [string, LV][] = [];
  while (r.pos < end && (count === undefined || out.length < count)) {
    const start = r.pos;
    const type = r.u8();
    const nameLength = r.u8();
    if (type === 76) throw new LVError("compressed LV data inside a structure");
    const name = r.utf16(nameLength).replace(/\0$/, "");
    let value: LV;
    switch (type) {
      case 1: value = r.u8() !== 0; break;
      case 2: value = r.i32(); break;
      case 3: value = r.u32(); break;
      case 4: value = r.i64(); break;
      case 5: case 7: value = r.u64(); break;
      case 6: value = r.f64(); break;
      case 8: {
        const from = r.pos;
        for (;;) {
          r.need(2);
          const u = r.view.getUint16(r.pos, true);
          r.pos += 2;
          if (u === 0) break;
        }
        value = new TextDecoder("utf-16le").decode(r.bytes.subarray(from, r.pos - 2));
        break;
      }
      case 9: {
        const n = r.u64();
        r.need(n);
        value = Array.from(r.bytes.subarray(r.pos, r.pos + n));
        r.pos += n;
        break;
      }
      case 11: {
        const items = r.u32();
        const length = r.u64();
        const levelEnd = start + length;
        if (levelEnd > r.bytes.length || levelEnd < r.pos) throw new LVError("bad LV level length");
        const members = records(r, levelEnd, items);
        r.pos = levelEnd + 8 * items;
        value = members.length > 0 && members.every(([k]) => k === "")
          ? members.map(([, v]) => v)
          : Object.fromEntries(members);
        break;
      }
      default:
        throw new LVError(`unknown LV record type ${type}`);
    }
    out.push([name, value]);
  }
  return out;
}

/** Decodes an LV structure (a metadata chunk's data). */
export async function decodeLV(data: Uint8Array): Promise<{ [name: string]: LV }> {
  if (data.length >= 2 && data[0] === 76) {
    return decodeLV(await inflate(data.subarray(12)));
  }
  const r = new Reader(data);
  return Object.fromEntries(records(r, data.length));
}

/** The member at a slash-separated path, or undefined. */
export function at(value: LV | undefined, path: string): LV | undefined {
  let v: LV | undefined = value;
  for (const part of path.split("/")) {
    if (v === undefined || v === null || typeof v !== "object") return undefined;
    v = Array.isArray(v) ? v[Number(part)] : v[part];
  }
  return v;
}

/** The members of a level or list, in order. */
export function members(value: LV | undefined): LV[] {
  if (Array.isArray(value)) return value;
  if (value !== undefined && typeof value === "object") return Object.values(value);
  return [];
}
