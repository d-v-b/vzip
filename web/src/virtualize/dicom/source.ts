// A DICOM file's File Meta Information and dataset in the DICOM JSON Model
// (PS3.18 §F.2): the source metadata of the DICOM convention
// (conventions/dicom/README.md §5).

import { base64, type ByteReader, decodeText, jsonNumber } from "../common.ts";
import {
  DicomError, type Element, type Encoding, EXPLICIT_LE, IMPLICIT_LE, ITEM, ITEM_END, MAX_DEPTH, PIXEL_DATA,
  SEQUENCE_END, UNDEFINED, Walker,
} from "./dataset.ts";

const MAX_VALUE_BYTES = 2 ** 26; // the most value bytes recorded per file
const MULTI = new Set(["AE", "AS", "CS", "DA", "DS", "DT", "IS", "LO", "PN", "SH", "TM", "UC", "UI"]);
const TEXT = new Set([...MULTI, "LT", "ST", "UT", "UR"]);
const LEADING = new Set(["AE", "AS", "CS", "DA", "DS", "DT", "IS", "LO", "PN", "SH", "TM", "UI"]);
const NUMBERS: Record<string, [number, (v: DataView, at: number, le: boolean) => number | bigint]> = {
  FL: [4, (v, at, le) => v.getFloat32(at, le)],
  FD: [8, (v, at, le) => v.getFloat64(at, le)],
  SL: [4, (v, at, le) => v.getInt32(at, le)],
  SS: [2, (v, at, le) => v.getInt16(at, le)],
  UL: [4, (v, at, le) => v.getUint32(at, le)],
  US: [2, (v, at, le) => v.getUint16(at, le)],
  SV: [8, (v, at, le) => v.getBigInt64(at, le)],
  UV: [8, (v, at, le) => v.getBigUint64(at, le)],
};
const DECIMAL = /^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$/;
const INTEGER = /^[+-]?[0-9]+$/;
const PN_GROUPS = ["Alphabetic", "Ideographic", "Phonetic"];

type Json = Record<string, unknown>;
const key = (tag: number) => tag.toString(16).toUpperCase().padStart(8, "0");
const sorted = (o: Json): Json => Object.fromEntries(Object.entries(o).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)));

/** One string value: trimmed, decoded, then typed (PS3.18 §F.2.3). */
function textValue(vr: string, b: Uint8Array): unknown {
  let s = 0;
  let e = b.length;
  while (e > s && (b[e - 1] === 0x20 || b[e - 1] === 0)) e--;
  if (LEADING.has(vr)) while (s < e && b[s] === 0x20) s++;
  if (s === e) return null;
  const t = decodeText(b.subarray(s, e));
  if (vr === "PN") {
    const parts: string[] = [];
    let rest = t;
    for (let i = 0; i < 2; i++) {
      const at = rest.indexOf("=");
      if (at < 0) break;
      parts.push(rest.slice(0, at));
      rest = rest.slice(at + 1);
    }
    parts.push(rest);
    const groups = Object.fromEntries(parts.map((g, i) => [PN_GROUPS[i], g]).filter(([, g]) => g));
    return Object.keys(groups).length ? groups : null;
  }
  if (vr === "DS" && DECIMAL.test(t)) {
    const v = Number(t);
    return Number.isFinite(v) ? v : t;
  }
  if (vr === "IS" && INTEGER.test(t)) return jsonNumber(BigInt(t));
  return t;
}

/** An element of defined length with VR `vr` and value `data` (never SQ). */
export function valueJson(vr: string, data: Uint8Array, little: boolean): Json {
  if (data.length === 0) return { vr };
  const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
  if (TEXT.has(vr)) {
    const parts: Uint8Array[] = [];
    if (MULTI.has(vr)) {
      let from = 0;
      for (let i = 0; i <= data.length; i++) {
        if (i === data.length || data[i] === 0x5c) {
          parts.push(data.subarray(from, i));
          from = i + 1;
        }
      }
    } else parts.push(data);
    return { vr, Value: parts.map((p) => textValue(vr, p)) };
  }
  if (vr === "AT") {
    const out: string[] = [];
    const h = (v: number) => v.toString(16).toUpperCase().padStart(4, "0");
    for (let at = 0; at + 4 <= data.length; at += 4) out.push(h(view.getUint16(at, little)) + h(view.getUint16(at + 2, little)));
    return { vr, Value: out };
  }
  const num = NUMBERS[vr];
  if (num !== undefined) {
    const [size, get] = num;
    const out: unknown[] = [];
    for (let at = 0; at + size <= data.length; at += size) out.push(jsonNumber(get(view, at, little)));
    return { vr, Value: out };
  }
  return { vr, InlineBinary: base64(data) }; // OB OD OF OL OV OW UN
}

/** Walks the file again, after the profile has accepted its structure, and
 * translates every element; one value budget is shared across the file. */
class Translator {
  private used = 0;
  private read: ByteReader;
  private walker: Walker;

  constructor(read: ByteReader, size: number) {
    this.read = read;
    this.walker = new Walker(read, size);
  }

  private take(n: number): boolean {
    if (this.used + n > MAX_VALUE_BYTES) return false;
    this.used += n;
    return true;
  }

  async meta(start: number): Promise<Json> {
    const out: Json = {};
    for (let pos = 132; pos < start;) {
      const el = await this.walker.header(pos, start, EXPLICIT_LE);
      await this.defined(el, EXPLICIT_LE, out, el.value + el.length, 0);
      pos = el.value + el.length;
    }
    return sorted(out);
  }

  /** An element of defined length, recorded in `out` unless a duplicate or a group length. */
  private async defined(el: Element, enc: Encoding, out: Json, end: number, depth: number): Promise<void> {
    const k = key(el.tag);
    if (k in out || el.tag % 0x10000 === 0) return;
    if (!enc.explicit) {
      out[k] = el.length === 0 || !this.take(el.length)
        ? { vr: "UN" }
        : { vr: "UN", InlineBinary: base64(await this.read(el.value, el.length)) };
    } else if (el.vr === "SQ") {
      const saved = this.used;
      try {
        const [items] = await this.sequence(el.value, el.value + el.length, end, enc, depth, true);
        out[k] = { vr: "SQ", Value: items };
      } catch (e) {
        if (!(e instanceof DicomError)) throw e;
        this.used = saved; // a sequence without a Value uses none of the budget
        out[k] = { vr: "SQ" };
      }
    } else if (el.length === 0 || !this.take(el.length)) {
      out[k] = { vr: el.vr };
    } else {
      out[k] = valueJson(el.vr!, await this.read(el.value, el.length), el.little);
    }
  }

  async dataset(
    pos: number, end: number, defined: boolean, enc: Encoding, depth: number, top = false, record = true,
  ): Promise<[Json, number]> {
    const out: Json = {};
    for (;;) {
      if (defined && pos === end) return [sorted(out), pos];
      const el = await this.walker.header(pos, end, enc);
      const k = key(el.tag);
      if (Math.floor(el.tag / 0x10000) === 0xfffe) {
        if (el.tag === ITEM_END && !defined && !top && el.length === 0) return [sorted(out), el.value];
        throw new DicomError(`unexpected item tag at ${pos}`);
      }
      const first = record && !(k in out);
      if (top && el.tag === PIXEL_DATA) {
        if (first) out[k] = { vr: el.vr ?? "OW" };
        return [sorted(out), el.value];
      }
      if (el.length === UNDEFINED) {
        if (enc.explicit && el.vr !== "SQ" && el.vr !== "UN" && !(el.tag === PIXEL_DATA && (el.vr === "OB" || el.vr === "OW"))) {
          throw new DicomError(`undefined length with VR ${el.vr}`);
        }
        let member: Json;
        if (el.tag === PIXEL_DATA) {
          pos = (await this.walker.fragments(el.value, end, enc.little))[1];
          member = { vr: el.vr ?? "OB" };
        } else {
          const inner = el.vr === "UN" ? IMPLICIT_LE : enc;
          let items: Json[];
          [items, pos] = await this.sequence(el.value, null, end, inner, depth, first);
          member = { vr: "SQ", Value: items };
        }
        if (first) out[k] = member;
      } else {
        if (el.value + el.length > end) throw new DicomError(`element at ${pos} runs past its container`);
        if (record) await this.defined(el, enc, out, end, depth);
        pos = el.value + el.length;
      }
    }
  }

  async sequence(
    pos: number, end: number | null, limit: number, enc: Encoding, depth: number, record: boolean,
  ): Promise<[Json[], number]> {
    const container = end ?? limit;
    const items: Json[] = [];
    for (;;) {
      if (end !== null && pos === end) return [items, pos];
      const el = await this.walker.header(pos, container, enc);
      if (end === null && el.tag === SEQUENCE_END && el.length === 0) return [items, el.value];
      if (el.tag !== ITEM || depth + 1 > MAX_DEPTH) throw new DicomError(`expected an item at ${pos}`);
      let ds: Json;
      if (el.length === UNDEFINED) {
        [ds, pos] = await this.dataset(el.value, container, false, enc, depth + 1, false, record);
      } else {
        if (el.value + el.length > container) throw new DicomError(`the item at ${pos} runs past its container`);
        [ds] = await this.dataset(el.value, el.value + el.length, true, enc, depth + 1, false, record);
        pos = el.value + el.length;
      }
      items.push(ds);
    }
  }
}

/** {meta, dataset}, for a file whose structure the profile has accepted. */
export async function sourceJson(read: ByteReader, size: number, start: number, encoding: Encoding): Promise<Json> {
  const t = new Translator(read, size);
  const meta = await t.meta(start);
  const [dataset] = await t.dataset(start, size, false, encoding, 0, true);
  return { meta, dataset };
}
