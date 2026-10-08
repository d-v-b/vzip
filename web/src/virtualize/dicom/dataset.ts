// Walking a DICOM file's elements (profiles/dicom.md §6.1–§6.3).

import type { ByteReader } from "../common.ts";

export class DicomError extends Error {}

export const reject = (message: string): never => {
  throw new DicomError(message);
};

const LONG_VRS = new Set(["OB", "OD", "OF", "OL", "OV", "OW", "SQ", "SV", "UC", "UN", "UR", "UT", "UV"]);
const VRS = new Set([
  ...LONG_VRS, "AE", "AS", "AT", "CS", "DA", "DS", "DT", "FD", "FL", "IS", "LO", "LT", "PN", "SH", "SL",
  "SS", "ST", "TM", "UI", "UL", "US",
]);
export const UNDEFINED = 0xffffffff;
export const ITEM = 0xfffee000;
export const ITEM_END = 0xfffee00d;
export const SEQUENCE_END = 0xfffee0dd;
export const PIXEL_DATA = 0x7fe00010;
// The sequences of defined length that are walked (§6.3, rule 3): Shared
// Functional Groups and Pixel Measures.
const WALKED = new Set([0x52009229, 0x00289110]);
export const MAX_DEPTH = 64;

export const tagName = (tag: number) => {
  const h = (v: number) => v.toString(16).toUpperCase().padStart(4, "0");
  return `(${h(Math.floor(tag / 0x10000))},${h(tag % 0x10000)})`;
};

export interface Encoding {
  explicit: boolean;
  little: boolean;
}

export const IMPLICIT_LE: Encoding = { explicit: false, little: true };
export const EXPLICIT_LE: Encoding = { explicit: true, little: true };

export interface Element {
  tag: number;
  vr: string | null; // null in implicit VR
  value: number; // where the value starts
  length: number; // the defined length, or UNDEFINED
  little: boolean; // the byte order of the value
  items?: Dataset[]; // a walked sequence's datasets
}

export type Dataset = Map<number, Element>;

const dv = (b: Uint8Array) => new DataView(b.buffer, b.byteOffset, b.byteLength);

export class Walker {
  readonly read: ByteReader;
  readonly size: number;

  constructor(read: ByteReader, size: number) {
    this.read = read;
    this.size = size;
  }

  /** The element at `pos`, whose header must end by `end` (§6.1). */
  async header(pos: number, end: number, enc: Encoding): Promise<Element> {
    if (pos + 8 > end) reject(`the element at ${pos} runs past its container`);
    const b = dv(await this.read(pos, 8));
    const group = b.getUint16(0, enc.little);
    const tag = group * 0x10000 + b.getUint16(2, enc.little);
    if (group === 0xfffe || !enc.explicit) {
      return { tag, vr: null, value: pos + 8, length: b.getUint32(4, enc.little), little: enc.little };
    }
    const vr = String.fromCharCode(b.getUint8(4), b.getUint8(5));
    if (!VRS.has(vr)) reject(`unknown VR ${JSON.stringify(vr)} at ${pos}`);
    if (LONG_VRS.has(vr)) {
      if (pos + 12 > end) reject(`the element at ${pos} runs past its container`);
      const length = dv(await this.read(pos + 8, 4)).getUint32(0, enc.little);
      return { tag, vr, value: pos + 12, length, little: enc.little };
    }
    return { tag, vr, value: pos + 8, length: b.getUint16(6, enc.little), little: enc.little };
  }

  /** The File Meta Information, and where the dataset starts (§6.2). */
  async meta(): Promise<[Dataset, number]> {
    const elements: Dataset = new Map();
    let pos = 132;
    while (pos + 2 <= this.size && dv(await this.read(pos, 2)).getUint16(0, true) === 2) {
      const el = await this.header(pos, this.size, EXPLICIT_LE);
      if (el.length === UNDEFINED) reject(`meta element ${tagName(el.tag)} has an undefined length`);
      if (el.value + el.length > this.size) reject(`meta element ${tagName(el.tag)} runs past the end of the file`);
      if (!elements.has(el.tag)) elements.set(el.tag, el);
      pos = el.value + el.length;
    }
    return [elements, pos];
  }

  /** The elements from `pos`: to `end` (defined), or to an item delimiter
   * within `end` (§6.3). The top-level dataset stops at Pixel Data, whose
   * header is the last one read. */
  async dataset(
    pos: number,
    end: number,
    defined: boolean,
    enc: Encoding,
    depth: number,
    top = false,
  ): Promise<[Dataset, number]> {
    const elements: Dataset = new Map();
    for (;;) {
      if (defined && pos === end) return [elements, pos];
      if (top && pos >= end) reject("no Pixel Data element");
      const el = await this.header(pos, end, enc);
      if (Math.floor(el.tag / 0x10000) === 0xfffe) {
        if (el.tag === ITEM_END && !defined && !top) {
          if (el.length !== 0) reject(`the item delimiter at ${pos} has a nonzero length`);
          return [elements, el.value];
        }
        reject(`unexpected ${tagName(el.tag)} at ${pos}`);
      }
      if (top && el.tag === PIXEL_DATA) {
        if (!elements.has(el.tag)) elements.set(el.tag, el);
        return [elements, el.value];
      }
      if (el.length === UNDEFINED) {
        if (
          enc.explicit && el.vr !== "SQ" && el.vr !== "UN" &&
          !(el.tag === PIXEL_DATA && (el.vr === "OB" || el.vr === "OW"))
        ) {
          reject(`${tagName(el.tag)} with VR ${el.vr} has an undefined length`);
        }
        if (el.tag === PIXEL_DATA) {
          pos = (await this.fragments(el.value, end, enc.little))[1];
        } else {
          const inner = el.vr === "UN" ? IMPLICIT_LE : enc;
          [el.items, pos] = await this.sequence(el.value, null, end, inner, depth);
        }
      } else {
        if (el.value + el.length > end) reject(`${tagName(el.tag)} at ${pos} runs past its container`);
        if (WALKED.has(el.tag)) {
          if (enc.explicit && el.vr !== "SQ") reject(`${tagName(el.tag)} has VR ${el.vr}, not SQ`);
          [el.items] = await this.sequence(el.value, el.value + el.length, end, enc, depth);
        }
        pos = el.value + el.length;
      }
      if (!elements.has(el.tag)) elements.set(el.tag, el);
    }
  }

  /** A sequence's items: to `end` if its length is defined, else to a
   * sequence delimiter within `limit` (§6.3). */
  async sequence(
    pos: number,
    end: number | null,
    limit: number,
    enc: Encoding,
    depth: number,
  ): Promise<[Dataset[], number]> {
    const container = end ?? limit;
    const items: Dataset[] = [];
    for (;;) {
      if (end !== null && pos === end) return [items, pos];
      const el = await this.header(pos, container, enc);
      if (end === null && el.tag === SEQUENCE_END) {
        if (el.length !== 0) reject(`the sequence delimiter at ${pos} has a nonzero length`);
        return [items, el.value];
      }
      if (el.tag !== ITEM) reject(`expected an item at ${pos}, found ${tagName(el.tag)}`);
      if (depth + 1 > MAX_DEPTH) reject(`items nested more than ${MAX_DEPTH} deep`);
      let ds: Dataset;
      if (el.length === UNDEFINED) {
        [ds, pos] = await this.dataset(el.value, container, false, enc, depth + 1);
      } else {
        if (el.value + el.length > container) reject(`the item at ${pos} runs past its container`);
        [ds] = await this.dataset(el.value, el.value + el.length, true, enc, depth + 1);
        pos = el.value + el.length;
      }
      items.push(ds);
    }
  }

  /** The items of a fragment sequence at `pos`, as [item offset, data
   * offset, data length], and where the sequence ends (§6.5). */
  async fragments(pos: number, limit: number, little: boolean): Promise<[[number, number, number][], number]> {
    const items: [number, number, number][] = [];
    for (;;) {
      if (pos + 8 > limit) reject(`the fragment sequence runs past its container at ${pos}`);
      const b = dv(await this.read(pos, 8));
      const tag = b.getUint16(0, little) * 0x10000 + b.getUint16(2, little);
      const length = b.getUint32(4, little);
      if (tag === SEQUENCE_END) {
        if (length !== 0) reject(`the sequence delimiter at ${pos} has a nonzero length`);
        return [items, pos + 8];
      }
      if (tag !== ITEM) reject(`expected a fragment item at ${pos}, found ${tagName(tag)}`);
      if (length === UNDEFINED || pos + 8 + length > limit) {
        reject(`the fragment at ${pos} has an undefined length or runs past its container`);
      }
      items.push([pos, pos + 8, length]);
      pos += 8 + length;
    }
  }
}
