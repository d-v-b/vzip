// Nikon's "lite variant" (LV) metadata encoding (profiles/nd2.md §5.2).
//
// Objects are Maps, which keep each name at the position of its first
// appearance (plain objects would move integer-like names first). Scalars
// keep their record type, which decides how they may be used.

export interface Scalar {
  type: number;
  value: boolean | number | string;
}
export type LV = Scalar | LV[] | LVObject;
export type LVObject = Map<string, LV>;

export class LVError extends Error {}

async function inflate(data: Uint8Array): Promise<Uint8Array> {
  const stream = new Blob([data as BlobPart]).stream().pipeThrough(new DecompressionStream("deflate"));
  try {
    // Fails on a truncated stream and on bytes after its end.
    return new Uint8Array(await new Response(stream).arrayBuffer());
  } catch (e) {
    throw new LVError(`invalid zlib stream in compressed LV data: ${(e as Error).message}`);
  }
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

const utf16 = new TextDecoder("utf-16le"); // replaces unpaired surrogates with U+FFFD

const MAX_DEPTH = 100;

/** The records up to `end`, which are at `depth` (§5.2). */
function records(r: Reader, end: number, count: number | undefined, depth: number): [string, LV][] {
  if (depth > MAX_DEPTH) throw new LVError(`LV levels nested more than ${MAX_DEPTH} deep`);
  const out: [string, LV][] = [];
  while (r.pos < end && (count === undefined || out.length < count)) {
    const start = r.pos;
    r.need(2, end);
    const type = r.bytes[r.pos];
    const nameLength = r.bytes[r.pos + 1];
    r.pos += 2;
    if (type === 76) throw new LVError("compressed LV record inside a structure");
    r.need(2 * nameLength, end);
    const name = utf16.decode(r.bytes.subarray(r.pos, r.pos + 2 * nameLength)).split("\0", 1)[0];
    r.pos += 2 * nameLength;
    const take = (n: number) => {
      r.need(n, end);
      const at = r.pos;
      r.pos += n;
      return at;
    };
    let value: LV;
    switch (type) {
      // 64-bit integers beyond 2^53 lose precision here; they are rejected
      // wherever an integer is needed (§5.2), so the exact value never matters.
      case 1: value = { type, value: r.bytes[take(1)] !== 0 }; break;
      case 2: value = { type, value: r.view.getInt32(take(4), true) }; break;
      case 3: value = { type, value: r.view.getUint32(take(4), true) }; break;
      case 4: value = { type, value: Number(r.view.getBigInt64(take(8), true)) }; break;
      case 5: case 7: value = { type, value: Number(r.view.getBigUint64(take(8), true)) }; break;
      case 6: value = { type, value: r.view.getFloat64(take(8), true) }; break;
      case 8: {
        const from = r.pos;
        for (;;) {
          const at = take(2);
          if (r.view.getUint16(at, true) === 0) break;
        }
        value = { type, value: utf16.decode(r.bytes.subarray(from, r.pos - 2)) };
        break;
      }
      case 9: {
        const n = Number(r.view.getBigUint64(take(8), true));
        const at = take(n);
        // A byte counts as a value of type 3.
        value = Array.from(r.bytes.subarray(at, at + n), (b): LV => ({ type: 3, value: b }));
        break;
      }
      case 11: {
        const items = r.view.getUint32(take(4), true);
        const length = Number(r.view.getBigUint64(take(8), true));
        const levelEnd = start + length;
        if (levelEnd > end || levelEnd < r.pos) throw new LVError("LV level length outside the data");
        const members = records(r, levelEnd, items, depth + 1);
        if (members.length !== items || r.pos !== levelEnd) {
          throw new LVError("LV level records do not end at its length");
        }
        take(8 * items);
        if (members.length > 0 && members.every(([k]) => k === "")) {
          value = members.map(([, v]) => v);
        } else {
          value = new Map(members); // first position, last value
        }
        break;
      }
      default:
        throw new LVError(`unknown LV record type ${type}`);
    }
    out.push([name, value]);
  }
  return out;
}

/** Decodes a chunk's LV structure. */
export async function decodeLV(data: Uint8Array): Promise<LVObject> {
  if (data.length >= 1 && data[0] === 76) {
    if (data.length < 12) throw new LVError("truncated compressed LV record");
    const inner = await inflate(data.subarray(12));
    if (inner.length >= 1 && inner[0] === 76) throw new LVError("compressed LV data inside compressed LV data");
    data = inner;
  }
  const r = new Reader(data);
  return new Map(records(r, data.length, undefined, 0));
}
