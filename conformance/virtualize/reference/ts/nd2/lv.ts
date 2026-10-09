// Nikon's "lite variant" (LV) metadata encoding (spec/virtualize/nd2.md §2.2).
//
// Objects are Maps, which keep each name at the position of its first
// appearance (plain objects would move integer-like names first). Scalars
// keep their record type, which decides how they may be used; 64-bit integers
// are bigints, so that the source metadata has their exact values. A byte
// array is one Uint8Array (as a list, each byte is a value of type 3).

export interface Scalar {
  type: number;
  value: boolean | number | bigint | string;
  /** A string's exact JSON value: the text, or {"utf16": base64} if it is not well-formed. */
  exact?: string | { utf16: string };
}
export type LV = Scalar | LV[] | LVObject | Uint8Array;
export type LVObject = Map<string, LV>;

/** Every record of an object in order, with its exact name (spec/virtualize/nd2.md §2.2):
 * the Map holds the members as the profile reads them (first position, last value). */
export const RECORDS = new WeakMap<LVObject, [string, LV][]>();

export class LVError extends Error {}
/** LV data whose JSON would not keep every byte (spec/virtualize/nd2.md §5.1). */
export class LossyError extends LVError {}
/** LV data with more records and array bytes than its JSON may have bytes. */
export class TooLargeError extends LVError {}

const DEFLATE_RATIO = 1032; // the most bytes one byte of a deflate stream inflates to

async function inflate(data: Uint8Array, limit: number): Promise<Uint8Array> {
  const stream = new Blob([data as BlobPart]).stream().pipeThrough(new DecompressionStream("deflate"));
  const reader = stream.getReader();
  const parts: Uint8Array[] = [];
  let size = 0;
  try {
    // Fails on a truncated stream and on bytes after its end.
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > limit) {
        await reader.cancel();
        throw new LVError(`compressed LV data inflates to more than ${limit} bytes`);
      }
      parts.push(value);
    }
  } catch (e) {
    if (e instanceof LVError) throw e;
    throw new LVError(`invalid zlib stream in compressed LV data: ${(e as Error).message}`);
  }
  const out = new Uint8Array(size);
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

class Reader {
  bytes: Uint8Array;
  view: DataView;
  pos = 0;
  constructor(bytes: Uint8Array) {
    this.bytes = bytes;
    this.view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  }
  need(n: number, end: number) {
    if (this.pos + n > end) throw new LVError("truncated LV data");
  }
}

const utf16 = new TextDecoder("utf-16le", { ignoreBOM: true }); // replaces unpaired surrogates with U+FFFD, keeps a BOM

/** True if UTF-16LE `units` have no unpaired surrogate. */
function wellFormed(units: Uint8Array): boolean {
  const n = units.length >> 1;
  const at = (i: number) => units[2 * i] | (units[2 * i + 1] << 8);
  for (let i = 0; i < n; i++) {
    const u = at(i);
    if (u >= 0xd800 && u <= 0xdbff) {
      if (i + 1 < n && at(i + 1) >= 0xdc00 && at(i + 1) <= 0xdfff) {
        i++;
        continue;
      }
      return false;
    }
    if (u >= 0xdc00 && u <= 0xdfff) return false;
  }
  return true;
}

function base64(b: Uint8Array): string {
  let s = "";
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode(...b.subarray(i, i + 0x8000));
  return btoa(s);
}

/** UTF-16LE code units up to the first NUL unit. */
function units(b: Uint8Array): Uint8Array {
  for (let i = 0; i + 1 < b.length; i += 2) if (b[i] === 0 && b[i + 1] === 0) return b.subarray(0, i);
  return b;
}

/** A string's exact JSON value (spec/virtualize/nd2.md §5.1). */
export function exactText(u: Uint8Array): string | { utf16: string } {
  return wellFormed(u) ? utf16.decode(u) : { utf16: base64(u) };
}

/** A name's exact form: the text, or U+0000 then the base64 of its UTF-16LE units
 * when it is not well-formed (a name holds no NUL). */
export function exactName(u: Uint8Array): string {
  return wellFormed(u) ? utf16.decode(u) : "\0" + base64(u);
}

const MAX_DEPTH = 100;

/** True if a level's skipped bytes are its offset table: each record's offset from
 * the level record's start, as a u64, each record once, in any order
 * (spec/virtualize/nd2.md §5.1). */
function isOffsetTable(layout: [number, Uint8Array][], level: number, table: Uint8Array): boolean {
  const view = new DataView(table.buffer, table.byteOffset, table.byteLength);
  const got = Array.from({ length: layout.length }, (_, i) => view.getBigUint64(8 * i, true)).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  const want = layout.map(([start]) => BigInt(start - level)).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  return got.every((x, i) => x === want[i]);
}

/** The records up to `end`, which are at `depth` (spec/virtualize/nd2.md §2.2).
 * With `exact`, data whose JSON would lose bytes throws LossyError. Each record's
 * [start, name units] is appended to `layout`. `room` ([n]) is spent by 1 per
 * record and by a byte array's length, each at least that many bytes of JSON:
 * past n, TooLargeError. */
function records(
  r: Reader, end: number, count: number | undefined, depth: number, exact: boolean, layout?: [number, Uint8Array][],
  room?: [number],
): [string, string, LV][] {
  if (depth > MAX_DEPTH) throw new LVError(`LV levels nested more than ${MAX_DEPTH} deep`);
  const out: [string, string, LV][] = [];
  const spend = (n: number) => {
    if (room !== undefined && (room[0] -= n) < 0) throw new TooLargeError("LV data with more JSON than its budget");
  };
  while (r.pos < end && (count === undefined || out.length < count)) {
    const start = r.pos;
    spend(1);
    r.need(2, end);
    const type = r.bytes[r.pos];
    const nameLength = r.bytes[r.pos + 1];
    r.pos += 2;
    if (type === 76) throw new LVError("compressed LV record inside a structure");
    r.need(2 * nameLength, end);
    const name = utf16.decode(r.bytes.subarray(r.pos, r.pos + 2 * nameLength)).split("\0", 1)[0];
    const nameUnits = units(r.bytes.subarray(r.pos, r.pos + 2 * nameLength));
    // The canonical name: none (k = 0) when empty, else its units and one NUL.
    if (exact && (nameUnits.length > 0 ? nameUnits.length !== 2 * (nameLength - 1) : nameLength !== 0)) {
      throw new LossyError("an LV name other than its units and one NUL, or k = 0 when empty");
    }
    layout?.push([start, nameUnits]);
    const writtenName = exactName(nameUnits);
    r.pos += 2 * nameLength;
    const take = (n: number) => {
      r.need(n, end);
      const at = r.pos;
      r.pos += n;
      return at;
    };
    let value: LV;
    switch (type) {
      case 1: {
        const b = r.bytes[take(1)];
        if (exact && b > 1) throw new LossyError("an LV bool other than 0 or 1");
        value = { type, value: b !== 0 };
        break;
      }
      case 2: value = { type, value: r.view.getInt32(take(4), true) }; break;
      case 3: value = { type, value: r.view.getUint32(take(4), true) }; break;
      case 4: value = { type, value: r.view.getBigInt64(take(8), true) }; break;
      case 5: case 7: value = { type, value: r.view.getBigUint64(take(8), true) }; break;
      case 6: {
        const at = take(8);
        value = { type, value: r.view.getFloat64(at, true) };
        if (exact && Number.isNaN(value.value) && r.view.getBigUint64(at, true) !== 0x7ff8000000000000n) {
          throw new LossyError("an LV NaN other than 0x7FF8000000000000");
        }
        break;
      }
      case 8: {
        const from = r.pos;
        for (;;) {
          const at = take(2);
          if (r.view.getUint16(at, true) === 0) break;
        }
        const u = r.bytes.subarray(from, r.pos - 2);
        value = { type, value: utf16.decode(u), exact: exactText(u) };
        break;
      }
      case 9: {
        const n = Number(r.view.getBigUint64(take(8), true));
        spend(n);
        const at = take(n);
        value = r.bytes.slice(at, at + n);
        break;
      }
      case 11: {
        const items = r.view.getUint32(take(4), true);
        const length = Number(r.view.getBigUint64(take(8), true));
        const levelEnd = start + length;
        if (levelEnd > end || levelEnd < r.pos) throw new LVError("LV level length outside the data");
        const layoutOf: [number, Uint8Array][] = [];
        const members = records(r, levelEnd, items, depth + 1, exact, layoutOf, room);
        if (members.length !== items || r.pos !== levelEnd) {
          throw new LVError("LV level records do not end at its length");
        }
        const skipped = take(8 * items);
        const table = r.bytes.subarray(skipped, skipped + 8 * items);
        if (exact && table.some((b) => b !== 0) && !isOffsetTable(layoutOf, start, table)) {
          throw new LossyError("an LV level whose skipped bytes are neither zero nor its offset table");
        }
        value = members.length > 0 && members.every(([k]) => k === "") ? members.map(([, , v]) => v) : object(members);
        break;
      }
      default:
        throw new LVError(`unknown LV record type ${type}`);
    }
    out.push([name, writtenName, value]);
  }
  return out;
}

function object(members: [string, string, LV][]): LVObject {
  const value: LVObject = new Map(members.map(([k, , v]) => [k, v])); // first position, last value
  RECORDS.set(value, members.map(([, e, v]) => [e, v]));
  return value;
}

/** Decodes a chunk's LV structure; compressed data may inflate to at most `limit`
 * bytes. The inflated size is appended to `inflated`, or, when the stream does not
 * inflate (it is invalid, or inflates past `limit`), the most it could:
 * min(limit, 1032 x the data's length) (spec/virtualize/nd2.md §5.1, the
 * budget). With `exact`, data that its JSON would not keep whole (up to the
 * layout of compression and offset tables) throws LossyError (the lossless test
 * of spec/virtualize/nd2.md §5.1). With `room`, data of more records and
 * array bytes than that throws TooLargeError, its JSON being longer; `room`
 * given as [n] is shared, and what the data spends is taken from it. */
export async function decodeLV(
  data: Uint8Array, limit = Infinity, inflated?: number[], exact = false, room?: number | [number],
): Promise<LVObject> {
  if (data.length >= 1 && data[0] === 76) {
    if (data.length < 12) throw new LVError("truncated compressed LV record");
    let inner: Uint8Array;
    try {
      inner = await inflate(data.subarray(12), limit);
    } catch (e) {
      inflated?.push(Math.min(limit, DEFLATE_RATIO * data.length));
      throw e;
    }
    inflated?.push(inner.length);
    if (inner.length >= 1 && inner[0] === 76) throw new LVError("compressed LV data inside compressed LV data");
    data = inner;
  }
  const r = new Reader(data);
  return object(records(r, data.length, undefined, 0, exact, undefined, typeof room === "number" ? [room] : room));
}
