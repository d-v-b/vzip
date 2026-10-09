// A DICOM file's File Meta Information and dataset in the DICOM JSON Model
// (PS3.18 §F.2): the source metadata of the DICOM convention
// (conventions/dicom/README.md §5).

import {
  base64, type ByteReader, familyPlans, gridChunks, jsonNumber, latin1, MAX_PAYLOAD, type Part, payloadSize, type Plan,
  rowChunks, SOURCE_NODE, textJson,
} from "../common.ts";
import {
  DicomError, type Element, type Encoding, EXPLICIT_LE, IMPLICIT_LE, ITEM, ITEM_END, MAX_DEPTH, PIXEL_DATA,
  SEQUENCE_END, UNDEFINED, Walker,
} from "./dataset.ts";
import dictionary from "../../../../src/vzip/virtualize/dicom/dictionary.json" with { type: "json" };

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
// Specific Character Set, and the character sets in which the bytes 5C (\) and
// 3D (=) can occur inside a character.
const CHARSET = 0x00080005;
const MULTIBYTE = new Set(["GB18030", "GBK", "ISO 2022 IR 58", "ISO 2022 IR 87", "ISO 2022 IR 149", "ISO 2022 IR 159"]);
// The VRs whose values are split at 5C (PN also at 3D) and to which Specific
// Character Set applies: kept as bytes under a multibyte character set.
const CHARSET_SPLIT = new Set(["LO", "PN", "SH", "UC"]);
const TAGS: Record<string, string> = {};
for (const [vr, s] of Object.entries(dictionary.tags as Record<string, string>)) {
  for (let i = 0; i < s.length; i += 8) TAGS[s.slice(i, i + 8)] = vr;
}
const MASKS: [string, string][] = Object.entries(dictionary.masks as Record<string, string>);

/** The VR the data dictionary gives a tag, or undefined (conventions/dicom/README.md §5). */
export function dictionaryVr(tag: number): string | undefined {
  const group = Math.floor(tag / 0x10000);
  const element = tag % 0x10000;
  if (group & 1) return element >= 0x0010 && element <= 0x00ff ? "LO" : undefined; // private creators
  const k = tag.toString(16).toUpperCase().padStart(8, "0");
  if (k in TAGS) return TAGS[k];
  for (const [mask, vr] of MASKS) {
    if ([...mask].every((m, i) => m === "x" || m === k[i])) return vr;
  }
  return undefined;
}

type Json = Record<string, unknown>;
const key = (tag: number) => tag.toString(16).toUpperCase().padStart(8, "0");
const sorted = (o: Json): Json => Object.fromEntries(Object.entries(o).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)));

/** Whether a Specific Character Set value names a character set in which 5C or 3D
 * can occur inside a character. */
function isMultibyte(value: Uint8Array): boolean {
  return latin1(value).split("\\").some((v) => MULTIBYTE.has(v.replace(/^[ \0]+|[ \0]+$/g, "")));
}

const DECIMAL_PARTS = /^([+-]?)([0-9]*)(?:\.([0-9]*))?(?:[eE]([+-]?)([0-9]+))?$/;
// The most digits of a DS exponent, without its sign and leading zeros, that can
// leave a value of at most 2^16 bytes in binary64's range; and the most digits of
// an IS value, without its sign and leading zeros, that can be at most 2^53 - 1
// (conventions/dicom/README.md §5).
const MAX_EXPONENT_DIGITS = 5;
const MAX_INTEGER_DIGITS = 16;

/** A decimal string's value, normalized: its sign, its digits without leading or
 * trailing zeros, and the exponent of the last digit ("0" for zero); undefined for
 * a value that is not zero and whose exponent has more than MAX_EXPONENT_DIGITS
 * digits: out of binary64's range. */
function decimalKey(t: string): string | undefined {
  const m = DECIMAL_PARTS.exec(t)!;
  const frac = m[3] ?? "";
  const digits = (m[2] + frac).replace(/^0+/, "");
  if (digits === "") return "0";
  const exponentDigits = (m[5] ?? "").replace(/^0+/, "");
  if (exponentDigits.length > MAX_EXPONENT_DIGITS) return undefined;
  const stripped = digits.replace(/0+$/, "");
  const exponent = (m[4] === "-" ? -1 : 1) * Number(exponentDigits || "0") - frac.length + digits.length - stripped.length;
  return `${m[1] === "-" ? "-" : ""}${stripped}e${exponent}`;
}

/** A DS value as a JSON number when that number is the text's decimal value
 * exactly, else undefined (conventions/dicom/README.md §5). */
function dsNumber(t: string): number | undefined {
  if (!DECIMAL.test(t)) return undefined;
  const k = decimalKey(t);
  if (k === undefined) return undefined;
  const v = Number(t);
  if (!Number.isFinite(v) || k !== decimalKey(String(v))) return undefined;
  return v;
}

/** An IS value of the form [+-]?[0-9]+ as conventions/README.md §6 writes an
 * integer: a number up to 2^53 - 1, else the string of its digits. */
function isNumber(t: string): unknown {
  const negative = t[0] === "-";
  const digits = t.replace(/^[+-]/, "").replace(/^0+/, "");
  if (digits.length > MAX_INTEGER_DIGITS) return (negative ? "-" : "") + digits; // above 2^53 - 1
  return jsonNumber(BigInt(negative ? `-${digits || "0"}` : digits || "0"));
}

const INT64 = 2n ** 63n;

/** A DS or IS value as float64 or int64 values in little endian, when every value
 * is a number exactly: a DS value that §5 writes as a number, an IS value of the
 * form [+-]?[0-9]+ within int64's range; else undefined (conventions/dicom/README.md
 * §5, Per-frame values). */
export function typedValues(vr: string, data: Uint8Array): [string, Uint8Array] | undefined {
  const parts: Uint8Array[] = [];
  let from = 0;
  for (let i = 0; i <= data.length; i++) {
    if (i === data.length || data[i] === 0x5c) {
      parts.push(data.subarray(from, i));
      from = i + 1;
    }
  }
  const out = new Uint8Array(8 * parts.length);
  const view = new DataView(out.buffer);
  for (const [k, b] of parts.entries()) {
    let s = 0;
    let e = b.length;
    while (e > s && (b[e - 1] === 0x20 || b[e - 1] === 0)) e--;
    while (s < e && b[s] === 0x20) s++;
    if (b.subarray(s, e).some((c) => c >= 0x80)) return undefined;
    const t = latin1(b.subarray(s, e));
    if (vr === "DS") {
      const v = dsNumber(t);
      if (v === undefined) return undefined;
      view.setFloat64(8 * k, v, true);
    } else {
      if (!INTEGER.test(t) || t.replace(/^[+-]/, "").replace(/^0+/, "").length > 19) return undefined;
      const v = BigInt(t);
      if (v < -INT64 || v >= INT64) return undefined;
      view.setBigInt64(8 * k, v, true);
    }
  }
  return [vr === "DS" ? "float64" : "int64", out];
}

/** One string value: trimmed, decoded, then typed (PS3.18 §F.2.3). */
function textValue(vr: string, b: Uint8Array): unknown {
  let s = 0;
  let e = b.length;
  while (e > s && (b[e - 1] === 0x20 || b[e - 1] === 0)) e--;
  if (LEADING.has(vr)) while (s < e && b[s] === 0x20) s++;
  if (s === e) return null;
  const t = textJson(b.subarray(s, e));
  if (typeof t !== "string") return t; // not UTF-8: its ISO 8859-1 reading, tagged, and not typed further
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
  if (vr === "DS") return dsNumber(t) ?? t;
  if (vr === "IS" && INTEGER.test(t)) return isNumber(t);
  return t;
}

/** The size of one value of an AT or numeric VR. */
const valueSize = (vr: string): number | undefined => (vr === "AT" ? 4 : NUMBERS[vr]?.[0]);

/** An element of defined length with VR `vr` and value `data` (never SQ), under a
 * multibyte character set or not. */
export function valueJson(vr: string, data: Uint8Array, little: boolean, multibyte = false): Json {
  if (data.length === 0) return { vr };
  const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
  if (TEXT.has(vr)) {
    if (multibyte && CHARSET_SPLIT.has(vr)) return { vr, InlineBinary: base64(data) }; // decoded by its character set
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
  const size = valueSize(vr);
  if (size !== undefined && data.length % size) return { vr, InlineBinary: base64(data) }; // not whole values
  if (vr === "AT") {
    const out: string[] = [];
    const h = (v: number) => v.toString(16).toUpperCase().padStart(4, "0");
    for (let at = 0; at < data.length; at += 4) out.push(h(view.getUint16(at, little)) + h(view.getUint16(at + 2, little)));
    return { vr, Value: out };
  }
  const num = NUMBERS[vr];
  if (num !== undefined) {
    const [n, get] = num;
    const out: unknown[] = [];
    for (let at = 0; at < data.length; at += n) out.push(jsonNumber(get(view, at, little)));
    return { vr, Value: out };
  }
  return { vr, InlineBinary: base64(data) }; // OB OD OF OL OV OW UN
}

const utf8 = new TextEncoder();
/** The length of `v` in UTF-8 as JSON.stringify writes it (conventions/dicom/README.md §5). */
const jsonSize = (v: unknown) => utf8.encode(JSON.stringify(v)).length;

const BINARY: Record<string, [string, number]> = {
  OB: ["uint8", 1], UN: ["uint8", 1], OW: ["uint16", 2], OF: ["float32", 4], OD: ["float64", 8], OL: ["uint32", 4],
  OV: ["uint64", 8],
};
const NUMERIC_TYPES: Record<string, string> = {
  FL: "float32", FD: "float64", SL: "int32", SS: "int16", UL: "uint32", US: "uint16", SV: "int64", UV: "uint64",
};
const INLINE = 64; // the most bytes of a binary value, or values of a numeric, DS or IS one, kept as JSON
const MAX_TEXT = 2 ** 16; // the longest text value kept as JSON
const MAX_MEMBER = 2 ** 14; // the largest top-level attribute, as JSON, kept on the root
const MAX_ROOT = 2 ** 16; // the largest the root's meta and dataset, as JSON, may be together
// Data Set Trailing Padding: dead space. (Group lengths and the Extended Offset Table
// are layout only where conventions/dicom/README.md §5 says.)
const PADDING = 0xfffcfffc;
const EOT_TAGS = new Set([0x7fe00001, 0x7fe00002]); // the Extended Offset Table and its lengths
const MAX_CHUNK = 2 ** 24; // the largest chunk of a metadata array (conventions/README.md §7)
const ITEM_SIZES: Record<string, number> = {
  uint8: 1, int16: 2, uint16: 2, int32: 4, uint32: 4, float32: 4, float64: 8, int64: 8, uint64: 8,
};
const PER_FRAME = "52009230"; // Per-frame Functional Groups Sequence: on vzip_source, always gathered
const GATHER_ITEMS = 64; // a sequence of more items is gathered
// The group, under a gathered sequence's path, of its gathered arrays: no item
// index, so no item's path.
const ITEMS = "items";
// The name of a gathered array (or family) within its path's group: no tag path
// has it, so no gathered path is a prefix of another.
const GATHERED = "value";
// The array of each item's structure, beside the gathered values' groups (whose
// names are tags or `duplicates`).
const STRUCTURE = "structure";
// The bounds of a gathered sequence, past which it keeps its bytes: its distinct
// paths, the JSON size of its Structures, and its arrays' cost, per byte of the
// sequence (conventions/dicom/README.md §5, Bounds).
const MAX_PATHS = 1024;
const MAX_STRUCTURES = 2 ** 16;
const MAX_COST = 16;
// The members of an attribute whose value a reader takes as bytes: marked
// `LittleEndian` when they are an explicit UN value's, in big endian.
const AS_BYTES = ["InlineBinary", "BulkDataURI", "Gathered"];

/** The dimension name of an array of values: `byte` for bytes, else `value`. */
const dimension = (dataType: string) => (dataType === "uint8" ? "byte" : "value");

/** A value kept as an array: `n` bytes at `offset`, values of `dataType` of `size`
 * bytes each; `attribute` names the array in its BulkDataURI. */
interface Extent {
  path: string;
  attribute: Json;
  offset: number;
  n: number;
  dataType: string;
  size: number;
  little: boolean;
  values?: [string, Uint8Array]; // a gathered DS or IS value's typed values
  dropped?: boolean; // a gathered group length that is layout
  item?: number; // a gathered value's item index
  rest?: string; // a gathered value's path within its item
}

/** The group lengths of one dataset (conventions/dicom/README.md §5): each is layout,
 * and dropped, when it is the only element of its tag in the dataset, of an even
 * group, UL (or implicit) with 4 bytes, and its value is the length of the run of
 * elements of its group that follows it. */
class GroupLengths {
  // group, key, value, where the run starts, and the value's gathered extent
  open: [number, string, number | undefined, number, Extent | undefined] | undefined;
  closed: [string, number | undefined, number, Extent | undefined][] = []; // key, value, the run's length, extent

  /** An element of tag `tag` starts at `pos`. */
  step(tag: number, pos: number): void {
    if (this.open !== undefined && Math.floor(tag / 0x10000) !== this.open[0]) this.close(pos);
  }

  close(pos: number): void {
    if (this.open === undefined) return;
    const [, k, value, start, extent] = this.open;
    this.closed.push([k, value, pos - start, extent]);
    this.open = undefined;
  }

  /** After the first element of its tag in the dataset, a group length (`extent`: its
   * value, when gathered). */
  async begin(el: Element, enc: Encoding, read: ByteReader, extent: Extent | undefined): Promise<void> {
    const group = Math.floor(el.tag / 0x10000);
    let value: number | undefined;
    if (group % 2 === 0 && el.length === 4 && (!enc.explicit || el.vr === "UL")) {
      const b = await read(el.value, 4);
      value = new DataView(b.buffer, b.byteOffset, 4).getUint32(0, el.little);
    }
    this.open = [group, key(el.tag), value, el.value + el.length, extent];
  }

  /** The dataset's elements end at `pos`: drops the group lengths that are layout. */
  finish(pos: number, out: Json): void {
    this.close(pos);
    const duplicated = new Set(((out.duplicates ?? []) as Json[]).flatMap((d) => Object.keys(d)));
    for (const [k, value, length, extent] of this.closed) {
      if (value !== undefined && value === length && !duplicated.has(k)) {
        delete out[k];
        if (extent !== undefined) extent.dropped = true;
      }
    }
  }
}

/** Walks the file again, after the profile has accepted its structure, and translates
 * every element (conventions/dicom/README.md §5): small values as JSON, large ones as
 * arrays of the source metadata node, named in the element's BulkDataURI. */
class Translator {
  arrays: Plan[] = []; // the families of encapsulated pixel data in items
  extents: Extent[] = []; // every other array
  // The top-level dataset, and the tags of the offset tables omitted from it as
  // layout: a later element of such a tag is a duplicate.
  top: Json | undefined;
  omitted = new Set<string>();
  // The path of the gathered sequence whose items are being walked.
  within: string | undefined;
  readonly read: ByteReader;
  walker: Walker;

  constructor(read: ByteReader, size: number) {
    this.read = read;
    this.walker = new Walker(read, size);
  }

  mark(): [number, number] {
    return [this.arrays.length, this.extents.length];
  }

  rollback([a, e]: [number, number]): void {
    this.arrays.length = a;
    this.extents.length = e;
  }

  array(vr: string, path: string, offset: number, n: number, dataType = "uint8", size = 1, little = true): Json {
    const attribute: Json = { vr, BulkDataURI: `${SOURCE_NODE}/${path}` };
    this.extents.push({ path, attribute, offset, n, dataType, size, little });
    return attribute;
  }

  /** A value of an item of a gathered sequence, gathered with the others of its path
   * (conventions/dicom/README.md §5). */
  async gather(vr: string, el: Element, path: string): Promise<Json> {
    const inner = path.slice(this.within!.length + 1);
    const slash = inner.indexOf("/");
    const n = el.length;
    let [dataType, size] = Object.hasOwn(BINARY, vr) ? BINARY[vr]
      : Object.hasOwn(NUMERIC_TYPES, vr) ? [NUMERIC_TYPES[vr], valueSize(vr)!] : ["uint8", 1];
    if (n % size) [dataType, size] = ["uint8", 1];
    const values = (vr === "DS" || vr === "IS") && n <= MAX_TEXT ? typedValues(vr, await this.read(el.value, n)) : undefined;
    this.extents.push({
      path, attribute: {}, offset: el.value, n, dataType, size, little: el.little || dataType === "uint8", values,
      item: Number(inner.slice(0, slash)), rest: inner.slice(slash + 1),
    });
    return { vr, Gathered: true };
  }

  /** An element of defined length with VR `vr`: not a sequence, or the bytes of one
   * that breaks a rule (`SQ`). */
  async member(vr: string, el: Element, path: string, multibyte: boolean): Promise<Json> {
    const n = el.length;
    if (n === 0) return { vr };
    if (this.within !== undefined) return this.gather(vr, el, path);
    const size = valueSize(vr);
    if (Object.hasOwn(BINARY, vr) || vr === "SQ" || (size !== undefined && n % size)) {
      if (n <= INLINE) return { vr, InlineBinary: base64(await this.read(el.value, n)) };
      let [dataType, s] = BINARY[vr] ?? ["uint8", 1];
      if (n % s) [dataType, s] = ["uint8", 1];
      return this.array(vr, path, el.value, n, dataType, s, el.little);
    }
    if (Object.hasOwn(NUMERIC_TYPES, vr) && Math.floor(n / size!) > INLINE) {
      return this.array(vr, path, el.value, n, NUMERIC_TYPES[vr], size!, el.little);
    }
    if (TEXT.has(vr) && n > MAX_TEXT) return this.array(vr, path, el.value, n);
    const data = await this.read(el.value, n);
    if (vr === "DS" || vr === "IS") {
      let values = 1;
      for (const b of data) if (b === 0x5c) values++;
      if (values > INLINE) return this.array(vr, path, el.value, n);
    }
    return valueJson(vr, data, el.little, multibyte);
  }

  /** A sequence of defined length; if it breaks a rule, its bytes. */
  async sequenceOrBytes(
    ifBytes: string, el: Element, enc: Encoding, end: number, depth: number, path: string, multibyte: boolean,
  ): Promise<Json> {
    const mark = this.mark();
    try {
      const [attribute] = await this.items(el, el.value + el.length, end, enc, depth, path, multibyte, ifBytes);
      return attribute;
    } catch (e) {
      if (!(e instanceof DicomError)) throw e;
      this.rollback(mark); // what the failed walk planned
      return this.member(ifBytes, el, path, multibyte); // its bytes, by the binary rule
    }
  }

  /** An element of defined length. */
  async defined(el: Element, enc: Encoding, end: number, depth: number, path: string, multibyte: boolean): Promise<Json> {
    if (!enc.explicit || el.vr === "UN") {
      // The file does not state the VR: the dictionary's, else UN. An explicit UN
      // value is in little endian in every transfer syntax (PS3.5 §6.2.2), as its
      // items are in implicit VR little endian.
      if (el.vr === "UN") el = { ...el, little: true };
      const vr = dictionaryVr(el.tag) ?? "UN";
      return vr === "SQ"
        ? this.sequenceOrBytes("UN", el, enc.explicit ? IMPLICIT_LE : enc, end, depth, path, multibyte)
        : this.member(vr, el, path, multibyte);
    }
    if (el.vr === "SQ") return this.sequenceOrBytes("SQ", el, enc, end, depth, path, multibyte);
    return this.member(el.vr!, el, path, multibyte);
  }

  /** Translates the element `el` of the dataset `out`, at path `base` and within
   * `end`, into it, and returns where the next element starts. An element with a tag
   * already in `out` goes to its `duplicates`. */
  async element(
    el: Element, enc: Encoding, out: Json, end: number, depth: number, base: string, multibyte: boolean,
  ): Promise<number> {
    const k = key(el.tag);
    const duplicates = (out.duplicates ?? []) as Json[];
    const duplicate = k in out || (out === this.top && this.omitted.has(k));
    const path = duplicate ? `${base}/duplicates/${duplicates.length}/${k}` : `${base}/${k}`;
    let pos: number;
    let attribute: Json;
    if (el.length === UNDEFINED) {
      if (enc.explicit && el.vr !== "SQ" && el.vr !== "UN" && !(el.tag === PIXEL_DATA && (el.vr === "OB" || el.vr === "OW"))) {
        throw new DicomError(`undefined length with VR ${el.vr}`);
      }
      if (el.tag === PIXEL_DATA) {
        let fragments: [number, number, number][];
        [fragments, pos] = await this.walker.fragments(el.value, end, enc.little);
        attribute = { vr: el.vr ?? "OB" };
        if (fragments.length > 0) {
          const members = fragments.map(([, o, n], i): [number, number, number] => [i, o, n]);
          this.arrays.push(...await familyPlans(path, members, this.read));
          attribute.BulkDataURI = `${SOURCE_NODE}/${path}`;
        }
      } else {
        const inner = el.vr === "UN" ? IMPLICIT_LE : enc;
        [attribute, pos] = await this.items(el, null, end, inner, depth, path, multibyte,
          enc.explicit && el.vr === "SQ" ? "SQ" : "UN");
      }
    } else {
      if (el.value + el.length > end) throw new DicomError(`the element at ${el.value} runs past its container`);
      pos = el.value + el.length;
      if (el.tag === PADDING) return pos; // dead space: not recorded
      attribute = await this.defined(el, enc, end, depth, path, multibyte);
    }
    if (enc.explicit && !enc.little && el.vr === "UN" && AS_BYTES.some((k) => k in attribute)) {
      attribute.LittleEndian = true; // an explicit UN value: in little endian (PS3.5 §6.2.2)
    }
    if (duplicate) {
      duplicates.push({ [k]: attribute });
      out.duplicates = duplicates;
    } else {
      out[k] = attribute;
    }
    return pos;
  }

  /** `element`, for the element at `pos`, keeping track of the dataset's group lengths. */
  async translate(
    el: Element, enc: Encoding, out: Json, end: number, depth: number, base: string, multibyte: boolean, pos: number,
    groups: GroupLengths,
  ): Promise<number> {
    groups.step(el.tag, pos);
    const first = !(key(el.tag) in out);
    const planned = this.extents.length;
    const after = await this.element(el, enc, out, end, depth, base, multibyte);
    if (el.tag % 0x10000 === 0 && el.length !== UNDEFINED && first && key(el.tag) in out) {
      await groups.begin(el, enc, this.read, this.extents.length > planned ? this.extents[this.extents.length - 1] : undefined);
    }
    return after;
  }

  async meta(start: number): Promise<Json> {
    const out: Json = {};
    const groups = new GroupLengths();
    let pos = 132;
    while (pos < start) {
      const el = await this.walker.header(pos, start, EXPLICIT_LE);
      pos = await this.translate(el, EXPLICIT_LE, out, start, 0, "meta", false, pos, groups);
    }
    groups.finish(pos, out);
    return sorted(out);
  }

  /** An item's dataset, under the character set of the dataset that holds it
   * (`multibyte`) unless its own Specific Character Set says otherwise. */
  async dataset(
    pos: number, end: number, defined: boolean, enc: Encoding, depth: number, base: string, multibyte = false,
  ): Promise<[Json, number]> {
    const out: Json = {};
    const groups = new GroupLengths();
    let charset = false; // whether the dataset's Specific Character Set has been read
    for (;;) {
      if (defined && pos === end) {
        groups.finish(pos, out);
        return [sorted(out), pos];
      }
      const el = await this.walker.header(pos, end, enc);
      if (Math.floor(el.tag / 0x10000) === 0xfffe) {
        if (el.tag === ITEM_END && !defined && el.length === 0) {
          groups.finish(pos, out);
          return [sorted(out), el.value];
        }
        throw new DicomError(`unexpected item tag at ${pos}`);
      }
      pos = await this.translate(el, enc, out, end, depth, base, multibyte, pos, groups);
      if (el.tag === CHARSET && el.length !== UNDEFINED && !charset) {
        charset = true;
        multibyte = isMultibyte(await this.read(el.value, el.length));
      }
    }
  }

  /** A sequence's attribute, and where it ends: its items, or, gathered (the top-level
   * Per-frame Functional Groups Sequence, or one of more than 64 items outside a
   * gathered item), its structures, unless it is past the bounds of one, when it keeps
   * its bytes (`ifBytes`). */
  async items(
    el: Element, end: number | null, limit: number, enc: Encoding, depth: number, path: string, multibyte: boolean,
    ifBytes: string,
  ): Promise<[Json, number]> {
    if (this.within !== undefined) {
      const [items, pos] = await this.sequence(el.value, end, limit, enc, depth, path, multibyte);
      return [{ vr: "SQ", Value: items }, pos];
    }
    const mark = this.mark();
    if (path !== `dataset/${PER_FRAME}`) {
      const [some, pos] = await this.sequence(el.value, end, limit, enc, depth, path, multibyte, GATHER_ITEMS);
      if (some !== undefined) return [{ vr: "SQ", Value: some }, pos];
      this.rollback(mark); // more than 64 items: walked again, gathered
    }
    this.within = path;
    let items: Json[] | undefined;
    let pos: number;
    try {
      [items, pos] = await this.sequence(el.value, end, limit, enc, depth, path, multibyte);
    } finally {
      this.within = undefined;
    }
    const length = (end ?? pos - 8) - el.value; // without the sequence delimiter
    const attribute = await this.gathered(path, items!, mark, length);
    if (attribute === undefined) { // past the bounds: its bytes
      this.rollback(mark);
      return [await this.member(ifBytes, { ...el, length }, path, multibyte), pos];
    }
    return [attribute, pos];
  }

  /** The attribute of the gathered sequence at `path`, whose `length` bytes hold
   * `items`, with its arrays planned; or undefined when it is past the bounds
   * (conventions/dicom/README.md §5, Bounds). */
  async gathered(path: string, items: Json[], mark: [number, number], length: number): Promise<Json | undefined> {
    const count = items.length;
    const columns = new Map<string, [number, Extent][]>();
    for (const e of this.extents.slice(mark[1])) {
      if (e.dropped) continue;
      if (!columns.has(e.rest!)) columns.set(e.rest!, []);
      columns.get(e.rest!)!.push([e.item!, e]);
    }
    const structures: Json[] = [];
    const index = new Map<string, number>();
    const each = new Uint8Array(4 * count);
    const view = new DataView(each.buffer);
    for (const [i, item] of items.entries()) {
      const k = JSON.stringify(item);
      if (!index.has(k)) {
        index.set(k, structures.length);
        structures.push(item);
      }
      view.setInt32(4 * i, index.get(k)!, true);
    }
    let cost = 0;
    for (const members of columns.values()) cost += gatheredCost(members, count);
    if (columns.size > MAX_PATHS || jsonSize(structures) > MAX_STRUCTURES || cost > MAX_COST * length) return undefined;
    this.extents.length = mark[1];
    for (const [rest, members] of columns) {
      this.arrays.push(...await gatheredPlans(`${path}/${ITEMS}/${rest}/${GATHERED}`, members, count, this.read));
    }
    if (count === 0) return { vr: "SQ", Value: [] };
    this.arrays.push(await copiedPlan(`${path}/${ITEMS}/${STRUCTURE}`, "int32", [count], ["index"], each, 4));
    return { vr: "SQ", Structures: structures };
  }

  /** A sequence's items, and where it ends; with `most`, undefined at the item after
   * the first `most`. */
  async sequence(
    pos: number, end: number | null, limit: number, enc: Encoding, depth: number, base: string, multibyte: boolean,
    most?: number,
  ): Promise<[Json[] | undefined, number]> {
    const container = end ?? limit;
    const items: Json[] = [];
    for (;;) {
      if (end !== null && pos === end) return [items, pos];
      const el = await this.walker.header(pos, container, enc);
      if (end === null && el.tag === SEQUENCE_END && el.length === 0) return [items, el.value];
      if (el.tag !== ITEM || depth + 1 > MAX_DEPTH) throw new DicomError(`expected an item at ${pos}`);
      if (most !== undefined && items.length === most) return [undefined, pos];
      const path = `${base}/${items.length}`;
      let ds: Json;
      if (el.length === UNDEFINED) {
        [ds, pos] = await this.dataset(el.value, container, false, enc, depth + 1, path, multibyte);
      } else {
        if (el.value + el.length > container) throw new DicomError(`the item at ${pos} runs past its container`);
        [ds] = await this.dataset(el.value, el.value + el.length, true, enc, depth + 1, path, multibyte);
        pos = el.value + el.length;
      }
      items.push(ds);
    }
  }

  /** The arrays. */
  plans(): Plan[] {
    const plans = [...this.arrays];
    for (const e of this.extents) {
      const [rows, chunks] = rowChunks(e.offset, e.n / e.size, e.size);
      plans.push({
        path: e.path, dataType: e.dataType, shape: [e.n / e.size], chunkShape: [rows], dims: [dimension(e.dataType)],
        chunks, endian: e.little ? "little" : "big",
      });
    }
    return plans;
  }
}

/** The array of one path of a gathered sequence's items: [data type, row bytes,
 * little endian] of a 2-D array, or undefined for a family (conventions/dicom/README.md
 * §5, Gathered values). */
function gatheredKind(members: [number, Extent][]): [string, number, boolean] | undefined {
  const kind = (e: Extent): [string, number, boolean] => (e.values !== undefined
    ? [e.values[0], e.values[1].length, true] : [e.dataType, e.n, e.little]);
  const [dataType, n, little] = kind(members[0][1]);
  if (members.every(([, e]) => { const k = kind(e); return k[0] === dataType && k[1] === n && k[2] === little; })
    && n <= MAX_CHUNK) return [dataType, n, little];
  const length = members[0][1].n;
  if (members.every(([, e]) => e.n === length) && length <= MAX_CHUNK) return ["uint8", length, true];
  return undefined;
}

/** The bytes that one path's array holds (conventions/dicom/README.md §5, Bounds): a
 * family's offsets and data; a 2-D array's rows, only those with a value when they are
 * sparse. */
function gatheredCost(members: [number, Extent][], count: number): number {
  const kind = gatheredKind(members);
  if (kind === undefined) return 8 * (count + 1) + members.reduce((a, [, e]) => a + e.n, 0);
  return (2 * members.length < count ? members.length : count) * kind[1];
}

/** The values of one path of a gathered sequence's items (conventions/dicom/README.md
 * §5, Gathered values): a 2-D array of their data type when they all have one data
 * type, length and byte order (a DS or IS value its typed values, when it has them),
 * else of their bytes as stored when they all have one length, else a family of
 * those bytes. */
async function gatheredPlans(path: string, members: [number, Extent][], count: number, read: ByteReader): Promise<Plan[]> {
  const kind = gatheredKind(members);
  if (kind !== undefined) {
    const [dataType, n, little] = kind;
    const rows = new Map(members.map(([i, e]): [number, Part] =>
      [i, e.values !== undefined && dataType !== "uint8" ? e.values[1] : [e.offset, e.n]]));
    const size = ITEM_SIZES[dataType];
    return [await gatheredRows(path, dataType, size, count, n / size, rows, little, read)];
  }
  return familyPlans(path, members.map(([i, e]) => [i, e.offset, e.n]), read, count, true);
}

const concat = (parts: Uint8Array[]): Uint8Array => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
};

/** A 2-D array [count, m] of `size`-byte values, whose row i is `rows[i]`: a range of
 * the file, or bytes; a row without a value is zero bytes (conventions/dicom/README.md
 * §5, Gathered values). */
async function gatheredRows(
  path: string, dataType: string, size: number, count: number, m: number, rows: Map<number, Part>, little: boolean,
  read: ByteReader,
): Promise<Plan> {
  const row = m * size;
  const dims = ["index", dimension(dataType)];
  const endian = little ? "little" : "big";
  const ordered = [...rows].sort(([a], [b]) => a - b);
  const first = ordered[0][1];
  if (ordered.length === count && !(first instanceof Uint8Array)
    && ordered.every(([, r], j) => !(r instanceof Uint8Array) && r[0] === first[0] + j * row)) {
    // Every row, adjacent in the file in index order: contiguous values.
    const [chunkShape, chunks] = await gridChunks(first[0], [count, m], size, read);
    return { path, dataType, shape: [count, m], chunkShape, dims, chunks, endian };
  }
  // A row takes at most max(16, b + 8) bytes of a reference payload: as a range, or as
  // bytes of a literal.
  const k = Math.ceil(count / Math.max(1, Math.floor(MAX_PAYLOAD / Math.max(16, row + 8))));
  const c = 2 * rows.size >= count ? Math.ceil(count / k) : 1; // sparse: a chunk per row with a value
  const chunks = new Map<string, Part[] | Uint8Array>();
  // A chunk none of whose rows has a value is absent.
  for (const q of [...new Set([...rows.keys()].map((i) => Math.floor(i / c)))].sort((a, b) => a - b)) {
    // Each part: a range of the file, or the bytes that make one literal.
    const parts: ({ range: [number, number] } | { bytes: Uint8Array[] })[] = [];
    for (let i = q * c; i < (q + 1) * c; i++) {
      const r = rows.get(i) ?? new Uint8Array(row);
      const last = parts[parts.length - 1];
      if (!(r instanceof Uint8Array)) {
        const [o, n] = r as [number, number];
        if (last !== undefined && "range" in last && last.range[0] + last.range[1] === o) {
          last.range[1] += n; // adjacent in the file: one range
        } else {
          parts.push({ range: [o, n] });
        }
      } else if (last !== undefined && "bytes" in last) {
        last.bytes.push(r); // bytes after bytes: one literal
      } else {
        parts.push({ bytes: [r] });
      }
    }
    const ranges: Part[] = parts.map((p) => ("range" in p ? p.range : concat(p.bytes)));
    if (payloadSize(ranges) > MAX_PAYLOAD) {
      chunks.set(`${q}/0`, concat(await Promise.all(ranges.map((r) => (r instanceof Uint8Array ? r : read(r[0], r[1]))))));
    } else {
      chunks.set(`${q}/0`, ranges);
    }
  }
  return { path, dataType, shape: [count, m], chunkShape: [c, m], dims, chunks, endian };
}

/** Values that the hierarchy holds itself, `packed` in C order, cut as contiguous
 * values (conventions/README.md §7), every chunk copied. */
async function copiedPlan(
  path: string, dataType: string, shape: number[], dims: string[], packed: Uint8Array, item: number,
): Promise<Plan> {
  const [chunkShape, cut] = await gridChunks(0, shape, item, async (o, n) => packed.slice(o, o + n));
  const chunks = new Map<string, Uint8Array>();
  for (const [k, v] of cut) {
    chunks.set(k, v instanceof Uint8Array ? v
      : concat(v.map((r) => (r instanceof Uint8Array ? r : packed.subarray(r[0], r[0] + r[1])))));
  }
  return { path, dataType, shape, chunkShape, dims, chunks };
}

/** The JSON size of an object whose members' values have the JSON sizes `sizes`. */
const objectSize = (sizes: Map<string, number>) =>
  2 + Math.max(sizes.size - 1, 0) + [...sizes].reduce((a, [k, n]) => a + jsonSize(k) + 1 + n, 0);

/** Moves the members of the root's `meta` and `dataset` that would make the root
 * large to the node's (conventions/dicom/README.md §5): each over MAX_MEMBER, then,
 * while the two are over MAX_ROOT together, the largest left (of equal sizes, the
 * first by name, `meta`'s before `dataset`'s). */
function moveLarge(root: Record<string, Json>, node: Record<string, Json>): void {
  const sizes: Record<string, Map<string, number>> = {};
  for (const [name, members] of Object.entries(root)) {
    sizes[name] = new Map();
    for (const k of Object.keys(members)) {
      const n = jsonSize(members[k]);
      if (n > MAX_MEMBER) {
        (node[name] ??= {})[k] = members[k];
        delete members[k];
      } else {
        sizes[name].set(k, n);
      }
    }
  }
  const order = Object.keys(root); // meta, then dataset
  let total = Object.values(sizes).reduce((a, s) => a + objectSize(s), 0);
  const largest = Object.entries(sizes).flatMap(([name, s]) => [...s].map(([k, n]) => ({ name, k, n })))
    .sort((a, b) => b.n - a.n || (a.k < b.k ? -1 : a.k > b.k ? 1 : 0) || order.indexOf(a.name) - order.indexOf(b.name));
  for (const { name, k, n } of largest) {
    if (total <= MAX_ROOT) break;
    const left = sizes[name];
    total -= jsonSize(k) + 1 + n + (left.size > 1 ? 1 : 0); // the member and its comma
    left.delete(k);
    (node[name] ??= {})[k] = root[name][k];
    delete root[name][k];
  }
}

/** [root S, node S, arrays] for a file whose structure the profile has accepted
 * (conventions/dicom/README.md §5). Past Pixel Data (which ends at `pixelEnd`), what
 * does not parse is kept as bytes. `pixelExtra` is the [offset, length] of native
 * pixel data past the frames, and `unreferenced` the members and count of the family
 * of bytes that an Extended Offset Table skips (when not all empty), and whether they
 * hold the frames' item headers. */
export async function sourceMetadata(
  read: ByteReader, size: number, start: number, encoding: Encoding, pixelEnd: number,
  pixelExtra?: [number, number], unreferenced?: [[number, number, number][], number, boolean],
  eotLayout = false, fragments?: [number, number][], offsetTable = false,
): Promise<[Json, Json, Plan[]]> {
  const t = new Translator(read, size);
  const root: Json = {};
  const preamble = await read(0, 128);
  if (preamble.some((b) => b !== 0)) root.preamble = base64(preamble);
  const meta = await t.meta(start);
  const dataset: Json = {};
  t.top = dataset;
  const groups = new GroupLengths();
  const seen = new Set<number>(); // the tags of the top-level dataset
  let multibyte = false;
  let charset = false;
  // The dataset up to Pixel Data: the profile has walked it.
  for (let pos = start; ;) {
    const el = await t.walker.header(pos, size, encoding);
    if (el.tag === PIXEL_DATA) {
      groups.step(el.tag, pos);
      seen.add(el.tag);
      dataset[key(el.tag)] = { vr: el.vr ?? "OW" }; // the image itself
      break;
    }
    if (eotLayout && EOT_TAGS.has(el.tag) && !seen.has(el.tag)) {
      groups.step(el.tag, pos); // the offset table that the profile read the frames from: layout
      t.omitted.add(key(el.tag));
      pos = el.value + el.length;
    } else {
      pos = await t.translate(el, encoding, dataset, size, 0, "dataset", multibyte, pos, groups);
    }
    seen.add(el.tag);
    if (el.tag === CHARSET && el.length !== UNDEFINED && !charset) {
      charset = true;
      multibyte = isMultibyte(await read(el.value, el.length));
    }
  }
  // After Pixel Data: elements as long as they parse and are not duplicates, then
  // the rest as bytes.
  let tailStart = pixelEnd;
  let mark = t.mark();
  try {
    for (let pos = pixelEnd; pos < size;) {
      const el = await t.walker.header(pos, size, encoding);
      if (Math.floor(el.tag / 0x10000) === 0xfffe || seen.has(el.tag)) throw new DicomError("not an element of the dataset");
      pos = await t.translate(el, encoding, dataset, size, 0, "dataset", multibyte, pos, groups);
      seen.add(el.tag);
      tailStart = pos;
      mark = t.mark();
    }
  } catch (e) {
    if (!(e instanceof DicomError)) throw e;
    t.rollback(mark);
  }
  groups.finish(tailStart, dataset);
  // The per-frame groups (gathered), and the large members, are vzip_source's.
  const node: Record<string, Json> = {};
  if (PER_FRAME in dataset) {
    node.dataset = { [PER_FRAME]: dataset[PER_FRAME] };
    delete dataset[PER_FRAME];
  }
  moveLarge({ meta, dataset }, node);
  root.meta = sorted(meta);
  root.dataset = sorted(dataset);
  if (pixelExtra !== undefined) root.pixel_extra = t.array("OB", "pixel_extra", ...pixelExtra).BulkDataURI;
  const plans: Plan[] = [];
  if (unreferenced !== undefined) {
    plans.push(...await familyPlans("pixel_unreferenced", unreferenced[0], read, unreferenced[1]));
    root.pixel_unreferenced = `${SOURCE_NODE}/pixel_unreferenced`;
    if (unreferenced[2]) root.pixel_unreferenced_headers = true;
  }
  if (fragments !== undefined) {
    const packed = new Uint8Array(16 * fragments.length);
    const view = new DataView(packed.buffer);
    fragments.forEach(([f, length], j) => {
      view.setBigInt64(16 * j, BigInt(f), true);
      view.setBigInt64(16 * j + 8, BigInt(length), true);
    });
    plans.push(await copiedPlan("pixel_fragments", "int64", [fragments.length, 2], ["index", "value"], packed, 8));
    root.pixel_fragments = `${SOURCE_NODE}/pixel_fragments`;
  }
  if (offsetTable) root.pixel_offset_table = true;
  if (tailStart < size) root.trailing = t.array("UN", "trailing", tailStart, size - tailStart).BulkDataURI;
  plans.push(...t.plans());
  const out: Json = {};
  for (const name of ["meta", "dataset"]) if (node[name] !== undefined) out[name] = sorted(node[name]);
  return [root, out, plans];
}
