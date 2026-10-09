// Every tag of a TIFF's IFDs as JSON: the source metadata of the TIFF and NDPI
// conventions (conventions/tiff/README.md §5).

import {
  base64, blobChunks, type ByteReader, declare, emitPlans, familyPlans, jsonNumber, type Plan, planEntries, type Profile,
  rowChunks,
  SOURCE_NODE, textJson, stringifyJson,
} from "../../../src/virtualize/common.ts";

// Bytes per value of each TIFF 6.0 and BigTIFF field type.
export const SIZES: Record<number, number> = {
  1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8,
};
// The layout: tags whose values only locate the file's own bytes (strips,
// tiles, free space, a JPEG interchange stream, old-style JPEG tables). They
// are not recorded; the bytes they locate are, where the convention says so.
export const LAYOUT = new Set([273, 279, 288, 289, 324, 325, 513, 514, 519, 520, 521]);
// JPEGQTables, JPEGDCTables and JPEGACTables: each value is the offset of a
// table, kept as the family <path>/<name> (conventions/tiff/README.md §5).
const TABLES: [number, string][] = [[519, "jpeg_q_tables"], [520, "jpeg_dc_tables"], [521, "jpeg_ac_tables"]];
// The pointer tags (conventions/tiff/README.md §5): SubIFDs, these, and any
// other tag of type IFD or IFD8 that is not in the convention's table. The
// IFDs they point to are recorded, each in its own group, at <path>/<name>.
const POINTERS: Record<number, string> = { 34665: "exif", 34853: "gps", 40965: "interoperability" };
const SUBIFDS = 330;
const GLOBAL_PARAMETERS = 400; // GlobalParametersIFD: a pointer when LONG, IFD or IFD8
const UNSIGNED = new Set([1, 3, 4, 13, 16, 18]); // the field types of offsets
const MAX_DEPTH = 4; // the deepest an IFD is read: main-chain IFDs are at depth 0
const MAX_IFDS = 100000;
const MAX_POINTED = 10000; // the most offsets tried through pointer tags
const INLINE = 64; // the most values of a numeric tag (or of a pointer tag's IFD list) kept as JSON
const MAX_TEXT = 2 ** 16; // the longest ASCII value kept as JSON
const IFD_BUDGET = 2 ** 16; // the most bytes of values as JSON in an IFD object
const TOTAL_BUDGET = 2 ** 20; // with the file's size (at most 2^24 in all), the most bytes of values measured for JSON
const MAX_TOTAL_BUDGET = 2 ** 24;
const MAX_CHUNK = 2 ** 24; // the largest chunk of a metadata array (conventions §7)
const DTYPES: Record<number, string> = {
  1: "uint8", 7: "uint8", 6: "int8", 2: "uint8", 3: "uint16", 8: "int16", 4: "uint32", 13: "uint32",
  9: "int32", 16: "uint64", 18: "uint64", 17: "int64", 11: "float32", 12: "float64", 5: "uint32", 10: "int32",
};

/** An IFD entry: its value is `inline` (the value field's bytes), or at `offset`
 * (undefined when it cannot be in the file), or, for NDPI's 64-bit LONGs, `value`.
 * `field` is the entry's raw value field (for NDPI, followed by its high word). */
export interface Entry {
  tag: number;
  type: number;
  count: bigint;
  inline?: Uint8Array;
  offset?: number;
  value?: (number | bigint)[];
  field?: Uint8Array;
}

function valueJson(data: Uint8Array, type: number, count: number, le: boolean): unknown {
  if (type === 2) return textJson(data.length > 0 && data[data.length - 1] === 0 ? data.subarray(0, -1) : data);
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
      case 1: case 7: out.push(view.getUint8(i)); break;
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

/** An IFD found for a pointer tag: its entry count, the end of its extent, and a
 * function that reads its entries. */
export type Found = [number, number, () => Promise<Entry[]>];

const utf8 = new TextEncoder();
/** The bytes of a value's compact JSON in UTF-8 (conventions/tiff/README.md §5). */
const jsonSize = (v: unknown) => utf8.encode(JSON.stringify(v)).length;

/** A lower bound of the size of a value's JSON (jsonSize), from its type and count. */
function leastSize(e: Entry): number {
  const count = Number(e.count);
  if (e.type === 2) return count + 1; // its bytes, perhaps without a final NUL, in quotes
  return e.type === 5 || e.type === 10 ? 6 * count + 1 : 2 * count + 1;
}

const hex = (b: Uint8Array | undefined) => Array.from(b ?? [], (x) => x.toString(16).padStart(2, "0")).join("");
/** An entry's field type, count and value field, which locate its value. */
const where = (e: Entry) => `${e.type},${e.count},${hex(e.field)}`;

/** Translates IFDs (conventions/tiff/README.md §5): each IFD's source metadata
 * (collected in `groups`, by path), small values as JSON and large ones as arrays
 * of the source metadata node (collected in `arrays`). `table` holds the tags of
 * the convention's table, which are never pointers; `entriesAt` finds the IFD at an
 * offset for the pointer tags; `recorded` lists the IFDs already read as (offset, end
 * of extent, path), in order. */
export class Translator {
  arrays: Plan[] = [];
  groups = new Map<string, Record<string, unknown>>();
  private recorded = new Map<number, string>(); // offset -> path
  private numbers = new Map<string, number>(); // path -> record number
  private referenced = new Set<string>(); // the paths a pointer's array refers to
  private tries = 0; // the offsets tried through pointer tags
  private failed = new Set<number>(); // the offsets tried that led to no IFD
  private total: number; // what is left of the total budget
  private kept = new Map<string, string | null>(); // the families kept, by the entries that locate them
  private pointerArrays = new Map<string, [Uint8Array, string][]>(); // (type, count, field) -> (numbers, path)
  private resolved = new Map<string, [number, (string | null)[]]>(); // (type, count, field, deep) -> (tries, paths)
  private starts: number[] = []; // the extents of the IFDs recorded, merged, sorted
  private ends: number[] = [];
  private read: ByteReader;
  private size: number;
  private le: boolean;
  private layout: Set<number>;
  private table: Set<number>;
  private entriesAt?: (offset: number) => Promise<Found | undefined>;
  constructor(read: ByteReader, size: number, le: boolean, layout: Set<number> = LAYOUT, table = new Set<number>(),
    entriesAt?: (offset: number) => Promise<Found | undefined>, recorded: [number, number, string][] = []) {
    this.read = read;
    this.size = size;
    this.le = le;
    this.layout = layout;
    this.table = table;
    this.entriesAt = entriesAt;
    this.total = Math.min(MAX_TOTAL_BUDGET, TOTAL_BUDGET + size);
    for (const [offset, , path] of recorded) {
      if (!this.recorded.has(offset)) this.recorded.set(offset, path);
      this.numbers.set(path, this.numbers.size);
    }
    for (const [offset, end] of [...recorded].sort((a, b) => a[0] - b[0] || a[1] - b[1])) {
      const last = this.ends.length - 1;
      if (last >= 0 && offset < this.ends[last]) this.ends[last] = Math.max(this.ends[last], end);
      else {
        this.starts.push(offset);
        this.ends.push(end);
      }
    }
  }

  /** The number of recorded extents that start before `at`. */
  private before(at: number): number {
    let lo = 0;
    let hi = this.starts.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.starts[mid] < at) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  /** Whether [start, end) overlaps the extent of an IFD recorded. */
  private overlaps(start: number, end: number): boolean {
    const i = this.before(end) - 1;
    return i >= 0 && this.ends[i] > start;
  }

  /** An entry's integer values, if it has them within the file (for the layout and pointer tags). */
  async value(e: Entry): Promise<number[] | undefined> {
    if (e.value !== undefined) return e.value.map(Number);
    const size = SIZES[e.type];
    if (size === undefined || !UNSIGNED.has(e.type)) return undefined;
    const count = Number(e.count);
    const n = size * count;
    let data: Uint8Array;
    if (e.inline !== undefined) data = e.inline.subarray(0, n);
    else if (e.offset !== undefined && e.offset + n <= this.size) data = await this.read(e.offset, n);
    else return undefined;
    const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
    return Array.from({ length: count }, (_, i) => {
      switch (e.type) {
        case 1: return view.getUint8(i);
        case 3: return view.getUint16(2 * i, this.le);
        case 4: case 13: return view.getUint32(4 * i, this.le);
        default: return Number(view.getBigUint64(8 * i, this.le));
      }
    });
  }

  private pointer(e: Entry): boolean {
    if (e.tag === SUBIFDS || Object.hasOwn(POINTERS, e.tag)) return UNSIGNED.has(e.type);
    if (e.tag === GLOBAL_PARAMETERS && e.type === 4) return true;
    return (e.type === 13 || e.type === 18) && !this.table.has(e.tag) && !this.layout.has(e.tag);
  }

  /** Whether a value of JSON `size` is JSON within the IFD's `budget` and the total
   * (conventions/tiff/README.md §5); either way, it spends the total. */
  private spend(budget: [number], size: number): boolean {
    const fits = size <= budget[0] && size <= this.total;
    if (fits) budget[0] -= size;
    this.total -= Math.min(size, this.total);
    return fits;
  }

  /** Records the IFD at `path` (and its image data if `data`), and the IFDs its pointer tags
   * lead to; returns its source metadata. */
  async ifd(entries: Entry[], path: string, depth = 0, data = true): Promise<Record<string, unknown>> {
    const first = new Map<number, Entry>();
    const later: Entry[] = [];
    for (const e of entries) {
      if (first.has(e.tag)) later.push(e);
      else first.set(e.tag, e);
    }
    const tags: Record<string, unknown> = {};
    const out: Record<string, unknown> = { tags };
    this.groups.set(path, out);
    const budget: [number] = [IFD_BUDGET];
    const same: Record<string, string> = {};
    const pointers: Record<string, unknown> = {};
    for (const tag of [...first.keys()].sort((a, b) => a - b)) {
      const e = first.get(tag)!;
      if (this.pointer(e)) pointers[String(tag)] = await this.follow(e, path, depth, budget, same);
      else if (!this.layout.has(tag)) tags[String(tag)] = await this.tag(e, `${path}/${tag}`, budget);
    }
    if (Object.keys(pointers).length > 0) out.pointers = pointers;
    const duplicates = later.filter((e) => !this.pointer(e) && !this.layout.has(e.tag));
    if (duplicates.length > 0) {
      const items = [];
      for (const [k, e] of duplicates.entries()) {
        items.push({ tag: e.tag, ...await this.tag(e, `${path}/duplicates/${k}`, budget) });
      }
      out.duplicates = items;
    }
    if (first.has(513) && first.has(514)) { // JPEGInterchangeFormat and its length
      const start = await this.value(first.get(513)!);
      const length = await this.value(first.get(514)!);
      if (start !== undefined && length !== undefined && start.length > 0 && length.length > 0 &&
          length[0] >= 1 && start[0] + length[0] <= this.size) {
        const [chunk, chunks] = blobChunks(start[0], length[0]);
        this.arrays.push({
          path: `${path}/jpeg_interchange`, dataType: "uint8", shape: [length[0]], chunkShape: [chunk], dims: ["byte"],
          chunks,
        });
      }
    }
    for (const [tag, name] of TABLES) {
      if (first.has(tag)) await this.tables(first.get(tag)!, path, name, tag === 519, same);
    }
    const tiles = first.has(324) && first.has(325);
    const strips = first.has(273) && first.has(279);
    if (data && (tiles || strips)) {
      await this.family(first.get(tiles ? 324 : 273)!, first.get(tiles ? 325 : 279)!, path, "data", same);
    }
    if (tiles && strips) await this.family(first.get(273)!, first.get(279)!, path, "strips", same); // a tiled IFD's strips
    if (Object.keys(same).length > 0) out.same_as = same;
    return out;
  }

  /** The family `<path>/<name>` of `members()`, or, when a family with the same `key` was
   * kept earlier, the member `name` of `same`: its path. */
  private async keep(key: string, path: string, name: string, same: Record<string, string>,
    members: () => Promise<[number, number, number][] | undefined>): Promise<void> {
    const known = this.kept.get(key);
    if (known !== undefined) {
      if (known !== null) same[name] = known;
      return;
    }
    const found = await members();
    const kept = found !== undefined && found.length > 0;
    this.kept.set(key, kept ? `${path}/${name}` : null);
    if (kept) this.arrays.push(...await familyPlans(`${path}/${name}`, found, this.read));
  }

  /** Old-style JPEG tables (JPEGQTables, JPEGDCTables, JPEGACTables): member i of the
   * family at `path/name` is the table at value i, 64 bytes for a quantization table, else
   * 16 bytes of counts and as many values as they sum to (conventions/tiff/README.md §5). */
  private async tables(e: Entry, path: string, name: string, quantization: boolean,
    same: Record<string, string>): Promise<void> {
    await this.keep(`${quantization ? "q" : "huffman"},${where(e)}`, path, name, same, async () => {
      const offsets = await this.value(e);
      if (offsets === undefined) return undefined;
      const members: [number, number, number][] = [];
      for (const [i, o] of offsets.entries()) {
        let n = 64;
        if (!quantization) {
          if (o + 16 > this.size) continue;
          n = 16 + (await this.read(o, 16)).reduce((a, b) => a + b, 0);
        }
        if (o + n <= this.size) members.push([i, o, n]);
      }
      return members;
    });
  }

  /** Strips or tiles as the family of bytes `<path>/<name>` (conventions/tiff/README.md §5). */
  private async family(offsets: Entry, counts: Entry, path: string, name: string,
    same: Record<string, string>): Promise<void> {
    await this.keep(`data,${where(offsets)},${where(counts)}`, path, name, same, async () => {
      const o = await this.value(offsets);
      const n = await this.value(counts);
      if (o === undefined || n === undefined) return undefined;
      const members: [number, number, number][] = [];
      for (let i = 0; i < Math.min(o.length, n.length); i++) {
        if (n[i] > 0 && o[i] + n[i] <= this.size) members.push([i, o[i], n[i]]);
      }
      return members;
    });
  }

  /** A pointer tag: its type, count and the IFDs it leads to, as paths (at most INLINE
   * values, within the budget) or as the record numbers of the array <path>/<tag>. */
  private async follow(e: Entry, path: string, depth: number, budget: [number],
    same: Record<string, string>): Promise<Record<string, unknown>> {
    const m: Record<string, unknown> = { type: e.type, count: jsonNumber(e.count) };
    const name = e.tag === SUBIFDS ? "subifds" : POINTERS[e.tag] ?? `ifd_${e.tag}`;
    // The same values, resolved as deep with no offset tried since, lead to the same IFDs.
    const memo = `${where(e)},${depth < MAX_DEPTH}`;
    const done = this.resolved.get(memo);
    let paths: (string | null)[];
    if (done !== undefined && done[0] === this.tries) paths = done[1];
    else {
      const values = await this.value(e);
      if (values === undefined) return m;
      paths = [];
      for (const [j, at] of values.entries()) {
        paths.push(await this.lead(at, e.tag === SUBIFDS || values.length > 1 ? `${path}/${name}/${j}` : `${path}/${name}`,
          depth));
      }
      this.resolved.set(memo, [this.tries, paths]);
    }
    if (paths.length <= INLINE && this.spend(budget, jsonSize(paths))) {
      m.ifds = paths;
      return m;
    }
    const n = paths.length;
    const packed = new Uint8Array(4 * n);
    const view = new DataView(packed.buffer);
    for (const [i, p] of paths.entries()) view.setInt32(4 * i, p === null ? -1 : this.numbers.get(p)!, true);
    const key = where(e);
    const earlier = this.pointerArrays.get(key) ?? [];
    this.pointerArrays.set(key, earlier);
    for (const [other, at] of earlier) {
      if (other.length === packed.length && other.every((b, i) => b === packed[i])) {
        same[String(e.tag)] = at;
        return m;
      }
    }
    earlier.push([packed, `${path}/${e.tag}`]);
    for (const p of paths) if (p !== null) this.referenced.add(p);
    const k = Math.ceil(4 * n / MAX_CHUNK);
    const c = Math.ceil(n / k);
    const chunks = new Map<string, Uint8Array>();
    for (let q = 0; q < k; q++) {
      const bytes = new Uint8Array(4 * c);
      bytes.set(packed.subarray(4 * q * c, Math.min(4 * n, 4 * (q + 1) * c)));
      chunks.set(String(q), bytes);
    }
    this.arrays.push({ path: `${path}/${e.tag}`, dataType: "int32", shape: [n], chunkShape: [c], dims: ["value"], chunks });
    return m;
  }

  /** The path of the IFD a pointer value `at` leads to, recording it at `child` (and
   * translating it) when it is new (conventions/tiff/README.md §5), or null. */
  private async lead(at: number, child: string, depth: number): Promise<string | null> {
    const known = this.recorded.get(at);
    if (known !== undefined) return known;
    if (this.failed.has(at) || depth >= MAX_DEPTH || this.recorded.size >= MAX_IFDS || this.tries >= MAX_POINTED ||
        this.entriesAt === undefined) return null;
    this.tries++;
    const found = await this.entriesAt(at);
    // Its extent comes from its entry count alone: an IFD that overlaps one recorded is not read.
    if (found === undefined || found[0] === 0 || this.overlaps(at, found[1])) {
      this.failed.add(at);
      return null;
    }
    this.recorded.set(at, child);
    this.numbers.set(child, this.numbers.size);
    const i = this.before(at);
    this.starts.splice(i, 0, at);
    this.ends.splice(i, 0, found[1]);
    await this.ifd(await found[2](), child, depth + 1);
    return child;
  }

  /** A tag as JSON; a large value, or one past the budget, is the array at `array`,
   * referenced where the file holds it. */
  private async tag(e: Entry, array: string, budget: [number]): Promise<Record<string, unknown>> {
    const m: Record<string, unknown> = { type: e.type, count: jsonNumber(e.count) };
    const size = SIZES[e.type];
    if (size === undefined) { // a field type TIFF does not define: its size is unknown
      m.field = base64(e.field ?? new Uint8Array());
      return m;
    }
    const nbytes = BigInt(size) * e.count;
    if (e.value !== undefined) {
      m.value = e.value.map(jsonNumber);
      return m;
    }
    const count = Number(e.count);
    if (e.inline !== undefined) { // in the entry's value field: always JSON
      m.value = valueJson(e.inline.subarray(0, Number(nbytes)), e.type, count, this.le);
      return m;
    }
    if (e.offset === undefined || BigInt(e.offset) + nbytes > BigInt(this.size)) return m; // its bytes cannot be found
    const small = (e.type === 2 && nbytes <= BigInt(MAX_TEXT)) || (e.type !== 2 && e.count <= BigInt(INLINE));
    if (small) {
      if (leastSize(e) > this.total) this.total = 0; // past the total budget, whatever its size
      else {
        const value = valueJson(await this.read(e.offset, Number(nbytes)), e.type, count, this.le);
        if (this.spend(budget, jsonSize(value))) {
          m.value = value;
          return m;
        }
      }
    }
    const pair = e.type === 5 || e.type === 10;
    const [rows, chunks] = rowChunks(e.offset, count, size, pair ? "/0" : "");
    this.arrays.push({
      path: array, dataType: DTYPES[e.type], shape: pair ? [count, 2] : [count],
      chunkShape: pair ? [rows, 2] : [rows], dims: pair ? ["value", "part"] : ["value"], chunks,
      endian: this.le ? "little" : "big",
    });
    return m;
  }

  /** The source metadata node (with its own source metadata `node`), one group per
   * IFD, and the arrays. */
  emit(profile: Profile, node: Record<string, unknown>): ReturnType<typeof emitPlans> {
    for (const p of this.referenced) this.groups.get(p)!.record = this.numbers.get(p);
    const group = (attributes: unknown) => utf8.encode(stringifyJson({ zarr_format: 3, node_type: "group", attributes }));
    const parents = new Set<string>();
    for (const p of this.groups.keys()) {
      const parts = p.split("/");
      for (let k = 1; k < parts.length; k++) parents.add(parts.slice(0, k).join("/"));
    }
    // As emitPlans, without spreading an array's entries (a family may have millions).
    const out = emitPlans([], declare({}, profile, undefined, node));
    const holders = new Set<string>();
    for (const a of this.arrays) {
      const parts = a.path.split("/");
      for (let k = 1; k < parts.length; k++) holders.add(parts.slice(0, k).join("/"));
    }
    for (const g of [...holders].sort()) {
      if (!parents.has(g) && !this.groups.has(g)) out.push({ key: `${SOURCE_NODE}/${g}/zarr.json`, bytes: group({}), compress: true });
    }
    for (const a of this.arrays) for (const e of planEntries(SOURCE_NODE, a)) out.push(e);
    for (const p of [...parents].sort()) if (!this.groups.has(p)) out.push({ key: `${SOURCE_NODE}/${p}/zarr.json`, bytes: group({}), compress: true });
    for (const [p, s] of this.groups) {
      out.push({ key: `${SOURCE_NODE}/${p}/zarr.json`, bytes: group(declare({}, profile, undefined, s)), compress: true });
    }
    return out;
  }
}
