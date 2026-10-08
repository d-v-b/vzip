// Every metadata chunk of an ND2 file as JSON: the source metadata of the ND2
// convention (conventions/nd2/README.md §5).

import { base64, type ByteReader, decodeText, jsonNumber } from "../common.ts";
import { decodeLV, type LV, LVError } from "./lv.ts";

const CHUNK_MAGIC = 0x0abeceda;
const MAX_DATA_BYTES = 2 ** 26; // the most chunk data recorded per file

/** An LV value as JSON: objects, lists, and scalars by their type. */
export function lvJson(v: LV): unknown {
  if (Array.isArray(v)) return v.map(lvJson);
  if (v instanceof Map) return Object.fromEntries([...v].map(([k, x]) => [k, lvJson(x)]));
  if (v.type === 1 || v.type === 8) return v.value;
  return jsonNumber(v.value as number | bigint);
}

const isLv = (name: string) => name.endsWith("LV!") || name.includes("LV|");

/** The chunk map's chunks other than frames, in map order, sharing one budget.
 * `chunks` maps each name (its bytes as ISO 8859-1) to its offset. */
export async function chunksJson(
  read: ByteReader,
  size: number,
  chunks: Map<string, number>,
): Promise<Record<string, unknown>> {
  const out: Record<string, unknown> = {};
  let used = 0;
  for (const [name, offset] of chunks) {
    if (name.startsWith("ImageDataSeq|")) continue;
    const entry: Record<string, unknown> = {};
    out[decodeText(Uint8Array.from(name, (c) => c.charCodeAt(0)))] = entry;
    if (offset + 16 > size) continue;
    const head = await read(offset, 16);
    const v = new DataView(head.buffer, head.byteOffset, head.byteLength);
    if (v.getUint32(0, true) !== CHUNK_MAGIC) continue;
    const start = offset + 16 + v.getUint32(4, true);
    const d = v.getBigUint64(8, true);
    if (BigInt(start) + d > BigInt(size) || BigInt(used) + d > BigInt(MAX_DATA_BYTES)) {
      entry.size = jsonNumber(d);
      continue;
    }
    used += Number(d);
    const data = await read(start, Number(d));
    if (isLv(name)) {
      try {
        entry.lv = lvJson(await decodeLV(data, MAX_DATA_BYTES));
        continue;
      } catch (e) {
        if (!(e instanceof LVError)) throw e;
      }
    }
    entry.data = base64(data);
  }
  return out;
}
