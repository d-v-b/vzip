// Virtualizing a NIfTI-1 or NIfTI-2 single file (.nii) by the NIfTI profile
// (spec/virtualize/nifti/profile.md, §7): the voxel data is one contiguous block, cut into
// Zarr chunks of at most 128 KiB as conventions §7 cuts contiguous values, each
// chunk referencing it.

import {
  base64, type ByteReader, declare, emitPlans, gridChunks, jsonNumber, MAX_PAYLOAD_BYTES, type Part, payloadSize,
  type Plan, rowChunks, SOURCE_NODE, textJson, toRange, stringifyJson,
} from "../common.ts";
import type { ArchiveDesc, EntryDesc } from "../../writer.ts";

export class NiftiError extends Error {}

const reject = (message: string): never => {
  throw new NiftiError(message);
};
const view = (b: Uint8Array) => new DataView(b.buffer, b.byteOffset, b.byteLength);

const MAX_SAFE = BigInt(Number.MAX_SAFE_INTEGER);
const CHUNK_BYTES = 1 << 17; // the most bytes of a chunk of the image (spec/virtualize/nifti.md §4.1)
const MAX_CHANNELS = 64; // the most channels given display windows (spec/virtualize/nifti.md §4.4)

type Kind = "i16" | "i32" | "i64" | "u8" | "f32" | "f64";
type Field = [offset: number, kind: Kind, count?: number];
interface Layout {
  length: number;
  dim: Field; datatype: Field; bitpix: Field; pixdim: Field; vox_offset: Field;
  scl_slope: Field; scl_inter: Field; xyzt_units: Field; cal_max: Field; cal_min: Field;
  qform_code: Field; sform_code: Field; quatern: Field; qoffset: Field; srow: Field;
}

// Header length and the offset and kind of each field (spec/virtualize/nifti.md §2).
const LAYOUTS: Record<1 | 2, Layout> = {
  1: {
    length: 348, dim: [40, "i16", 8], datatype: [70, "i16"], bitpix: [72, "i16"], pixdim: [76, "f32", 8],
    vox_offset: [108, "f32"], scl_slope: [112, "f32"], scl_inter: [116, "f32"], xyzt_units: [123, "u8"],
    cal_max: [124, "f32"], cal_min: [128, "f32"], qform_code: [252, "i16"], sform_code: [254, "i16"],
    quatern: [256, "f32", 3], qoffset: [268, "f32", 3], srow: [280, "f32", 12],
  },
  2: {
    length: 540, dim: [16, "i64", 8], datatype: [12, "i16"], bitpix: [14, "i16"], pixdim: [104, "f64", 8],
    vox_offset: [168, "i64"], scl_slope: [176, "f64"], scl_inter: [184, "f64"], xyzt_units: [500, "i32"],
    cal_max: [192, "f64"], cal_min: [200, "f64"], qform_code: [344, "i32"], sform_code: [348, "i32"],
    quatern: [352, "f64", 3], qoffset: [376, "f64", 3], srow: [400, "f64", 12],
  },
};

// Every header field, in the standard's order, with its offset and kind: the
// source metadata of spec/virtualize/nifti.md §5. A kind "s<N>" is a
// character field of N bytes; a count makes the field an array.
type HeaderField = [name: string, offset: number, kind: Kind | `s${number}`, count?: number];
const HEADER_FIELDS: Record<1 | 2, HeaderField[]> = {
  1: [
    ["sizeof_hdr", 0, "i32"], ["data_type", 4, "s10"], ["db_name", 14, "s18"], ["extents", 32, "i32"],
    ["session_error", 36, "i16"], ["regular", 38, "s1"], ["dim_info", 39, "u8"], ["dim", 40, "i16", 8],
    ["intent_p1", 56, "f32"], ["intent_p2", 60, "f32"], ["intent_p3", 64, "f32"], ["intent_code", 68, "i16"],
    ["datatype", 70, "i16"], ["bitpix", 72, "i16"], ["slice_start", 74, "i16"], ["pixdim", 76, "f32", 8],
    ["vox_offset", 108, "f32"], ["scl_slope", 112, "f32"], ["scl_inter", 116, "f32"], ["slice_end", 120, "i16"],
    ["slice_code", 122, "u8"], ["xyzt_units", 123, "u8"], ["cal_max", 124, "f32"], ["cal_min", 128, "f32"],
    ["slice_duration", 132, "f32"], ["toffset", 136, "f32"], ["glmax", 140, "i32"], ["glmin", 144, "i32"],
    ["descrip", 148, "s80"], ["aux_file", 228, "s24"], ["qform_code", 252, "i16"], ["sform_code", 254, "i16"],
    ["quatern_b", 256, "f32"], ["quatern_c", 260, "f32"], ["quatern_d", 264, "f32"], ["qoffset_x", 268, "f32"],
    ["qoffset_y", 272, "f32"], ["qoffset_z", 276, "f32"], ["srow_x", 280, "f32", 4], ["srow_y", 296, "f32", 4],
    ["srow_z", 312, "f32", 4], ["intent_name", 328, "s16"], ["magic", 344, "s4"],
  ],
  2: [
    ["sizeof_hdr", 0, "i32"], ["magic", 4, "s8"], ["datatype", 12, "i16"], ["bitpix", 14, "i16"], ["dim", 16, "i64", 8],
    ["intent_p1", 80, "f64"], ["intent_p2", 88, "f64"], ["intent_p3", 96, "f64"], ["pixdim", 104, "f64", 8],
    ["vox_offset", 168, "i64"], ["scl_slope", 176, "f64"], ["scl_inter", 184, "f64"], ["cal_max", 192, "f64"],
    ["cal_min", 200, "f64"], ["slice_duration", 208, "f64"], ["toffset", 216, "f64"], ["slice_start", 224, "i64"],
    ["slice_end", 232, "i64"], ["descrip", 240, "s80"], ["aux_file", 320, "s24"], ["qform_code", 344, "i32"],
    ["sform_code", 348, "i32"], ["quatern_b", 352, "f64"], ["quatern_c", 360, "f64"], ["quatern_d", 368, "f64"],
    ["qoffset_x", 376, "f64"], ["qoffset_y", 384, "f64"], ["qoffset_z", 392, "f64"], ["srow_x", 400, "f64", 4],
    ["srow_y", 432, "f64", 4], ["srow_z", 464, "f64", 4], ["slice_code", 496, "i32"], ["xyzt_units", 500, "i32"],
    ["intent_code", 504, "i32"], ["intent_name", 508, "s16"], ["dim_info", 524, "u8"], ["unused_str", 525, "s15"],
  ],
};
const TEXT_CODES = new Set([4, 6, 8, 32, 44]); // AFNI and XCEDE XML, comment, CIFTI XML, MRS JSON: text
const INLINE = 64; // the most bytes of binary extension data kept as JSON
const MAX_TEXT = 2 ** 16; // the longest text extension kept as JSON
const EXTENSIONS_BUDGET = 2 ** 14; // the most bytes of the extensions' JSON on the root
const LIST_MOST = Math.floor(EXTENSIONS_BUDGET / 12); // the most extensions of an array E in the budget: each is >= 12 bytes
const SCAN = 2 ** 20; // the bytes read at a time to check that a run is all zero
const ECODES = `${SOURCE_NODE}/extensions/ecode`;
const ESIZES = `${SOURCE_NODE}/extensions/esize`;
const EDATA = `${SOURCE_NODE}/extensions/data`;
const ECODE_CHUNK = 2 ** 22; // the most values of a chunk of the ecodes (spec/virtualize/nifti.md §5)
const SIZES: Record<Kind, number> = { u8: 1, i16: 2, i32: 4, i64: 8, f32: 4, f64: 8 };

// datatype: [Zarr data type, bitpix, samples per voxel] (spec/virtualize/nifti.md §3).
const DATATYPES: Record<number, [string, number, number]> = {
  2: ["uint8", 8, 1], 4: ["int16", 16, 1], 8: ["int32", 32, 1], 16: ["float32", 32, 1],
  64: ["float64", 64, 1], 256: ["int8", 8, 1], 512: ["uint16", 16, 1], 768: ["uint32", 32, 1],
  1024: ["int64", 64, 1], 1280: ["uint64", 64, 1], 128: ["uint8", 24, 3], 2304: ["uint8", 32, 4],
};
const UNSUPPORTED: Record<number, string> = {
  1: "BINARY", 32: "COMPLEX64", 1536: "FLOAT128", 1792: "COMPLEX128", 2048: "COMPLEX256",
};
const SPACE_UNITS: Record<number, string> = { 1: "meter", 2: "millimeter", 3: "micrometer" };
const TIME_UNITS: Record<number, string> = { 8: "second", 16: "millisecond", 24: "microsecond" };
const COLORS: [string, string][] = [["R", "FF0000"], ["G", "00FF00"], ["B", "0000FF"], ["A", "FFFFFF"]];
const TYPES: Record<string, string> = { t: "time", c: "channel", z: "space", y: "space", x: "space" };

const ascii = (b: Uint8Array) => String.fromCharCode(...b);

/** The NIfTI version that the file's first bytes select (§1.2), or undefined. */
export function detectNifti(head: Uint8Array): 1 | 2 | undefined {
  if (head.length < 12) return undefined;
  const v = view(head);
  const sizes = [v.getInt32(0, true), v.getInt32(0, false)];
  if (sizes.includes(348) && head.length >= 348 && ascii(head.subarray(344, 348)) === "n+1\0") return 1;
  if (sizes.includes(540) && ascii(head.subarray(4, 12)) === "n+2\0\r\n\x1a\n") return 2;
  return undefined;
}

const valid = (v: number) => Number.isFinite(v) && v > 0;

/** The esize of a text extension of `length` bytes padded with the fewest NULs: the least
 * multiple of 16 that is at least length + 8 (spec/virtualize/nifti.md §5). */
const textEsize = (length: number) => Math.ceil((length + 8) / 16) * 16;

/** The bytes of `value`'s compact JSON, in UTF-8. */
const jsonSize = (value: unknown) => new TextEncoder().encode(JSON.stringify(value)).length;

/** The extension chain from `start` up to the voxel data `v` (spec/virtualize/nifti.md §5),
 * read a block at a time: [its esizes, its ecodes, where it ends]. The chain is kept as two
 * packed columns, so that memory per extension is 8 bytes. */
async function readChain(
  read: ByteReader, little: boolean, start: number, v: number,
): Promise<[Int32Array, Int32Array, number]> {
  let esizes = new Int32Array(64);
  let ecodes = new Int32Array(64);
  let count = 0;
  let q = start;
  chain: while (q + 8 <= v) {
    const block = await read(q, Math.min(SCAN, v - q));
    const at = view(block);
    let p = 0;
    while (p + 8 <= block.length) {
      const esize = at.getInt32(p, little);
      if (esize < 8 || q + p + esize > v) {
        q += p;
        break chain;
      }
      if (count === esizes.length) {
        const [s, c] = [new Int32Array(2 * count), new Int32Array(2 * count)];
        s.set(esizes);
        c.set(ecodes);
        [esizes, ecodes] = [s, c];
      }
      esizes[count] = esize;
      ecodes[count++] = at.getInt32(p + 4, little);
      p += esize;
    }
    q += p;
  }
  return [esizes.subarray(0, count), ecodes.subarray(0, count), q];
}

/** The text value of an extension's data, or undefined when it is not a text extension (§5). */
function extensionText(ecode: number, data: Uint8Array | undefined): [unknown, number] | undefined {
  if (!TEXT_CODES.has(ecode) || data === undefined) return undefined;
  const nul = data.indexOf(0);
  if (nul >= 0 && !data.subarray(nul + 1).every((x) => x === 0)) return undefined;
  const head = nul < 0 ? data : data.subarray(0, nul);
  return [textJson(head), head.length];
}

/** The value of `extensions` and its arrays, for the chain from `start` to `end` (§5). */
async function extensionsValue(
  read: ByteReader, little: boolean, endian: "big" | "little", start: number, end: number, esizes: Int32Array,
  ecodes: Int32Array,
): Promise<[unknown, Plan[]]> {
  const count = esizes.length;
  if (count <= LIST_MOST) { // the array E may fit: build it while its running size does
    const extensions: Record<string, unknown>[] = [];
    const plans: Plan[] = [];
    let size = 1; // the JSON of "[" and of each entry and its separator
    let at = start + 8;
    for (let i = 0; i < count && size <= EXTENSIONS_BUDGET; i++) {
      const n = esizes[i] - 8;
      const entry: Record<string, unknown> = { ecode: ecodes[i] };
      const data = n <= MAX_TEXT ? await read(at, n) : undefined;
      const text = extensionText(ecodes[i], data);
      if (text !== undefined) {
        entry.text = text[0];
        if (n + 8 !== textEsize(text[1])) entry.esize = n + 8; // padded otherwise than with the fewest NULs
      } else if (n <= INLINE) {
        entry.edata = base64(data!);
      } else {
        const path = `extensions/${i}`;
        const [rows, chunks] = rowChunks(at, n, 1);
        plans.push({ path, dataType: "uint8", shape: [n], chunkShape: [rows], dims: ["byte"], chunks });
        entry.data = `${SOURCE_NODE}/${path}`;
      }
      extensions.push(entry);
      size += jsonSize(entry) + 1;
      at += n + 8;
    }
    // The budget counts the UTF-8 bytes of the compact JSON.
    if (size <= EXTENSIONS_BUDGET) return [extensions, plans];
  }
  // Over the budget: the ecodes, the text extensions that fit, and the others' data as a family.
  const compact: Record<string, unknown> = { ecode: ECODES, esize: ESIZES, data: EDATA };
  const inline = new Map<number, unknown>();
  let size = jsonSize(compact);
  for (let i = 0, at = start + 8; i < count; at += esizes[i++]) {
    const least = String(i).length + 2 + 1 + 2 + (inline.size > 0 ? 1 : ',"text":{}'.length); // "i":"" with its separator
    if (size + least > EXTENSIONS_BUDGET) break; // no later text fits: their indexes are no shorter
    const n = esizes[i] - 8;
    if (!TEXT_CODES.has(ecodes[i]) || n > MAX_TEXT) continue;
    const text = extensionText(ecodes[i], await read(at, n));
    if (text === undefined) continue;
    const more = least - 2 + jsonSize(text[0]);
    if (size + more <= EXTENSIONS_BUDGET) {
      inline.set(i, text[0]);
      size += more;
    }
  }
  if (inline.size > 0) compact.text = Object.fromEntries(inline);
  const k = Math.ceil(count / ECODE_CHUNK);
  const c = Math.ceil(count / k);
  const plans: Plan[] = [];
  for (const [name, column] of [["ecode", ecodes], ["esize", esizes]] as const) {
    const values = new Uint8Array(k * c * 4);
    const out = view(values);
    column.forEach((x, i) => out.setInt32(4 * i, x, little));
    plans.push({
      path: `extensions/${name}`, dataType: "int32", shape: [count], chunkShape: [c], dims: ["index"], endian,
      chunks: new Map(Array.from({ length: k }, (_, j) => [String(j), values.slice(j * c * 4, (j + 1) * c * 4)])),
    });
  }
  plans.push(...await dataFamily("extensions/data", window(read, end), start, esizes, inline)); // always offsets and data
  return [compact, plans];
}

const RAGGED_CHUNK = 2 ** 20; // the bytes of a chunk of a family's data, as familyPlans cuts it

/** A range's bytes in a reference of several ranges (payloadSize). */
const term = (r: Part) => payloadSize([r, r]) / 2;

/** The extensions' data as a family of byte values in its second form (conventions §7), member
 * i absent when it is in `inline`: the rule of familyPlans, from the packed esizes, so that
 * memory is 8 bytes per member and one chunk at a time. */
async function dataFamily(
  path: string, read: ByteReader, start: number, esizes: Int32Array, inline: Map<number, unknown>,
): Promise<Plan[]> {
  const count = esizes.length;
  const starts = new Float64Array(count + 1);
  let total = 0;
  for (let i = 0; i < count; i++) {
    starts[i] = total;
    if (!inline.has(i)) total += esizes[i] - 8;
  }
  starts[count] = total;
  const offsets = new Uint8Array(8 * (count + 1));
  const out = view(offsets);
  starts.forEach((x, i) => {
    out.setUint32(8 * i, x % 2 ** 32, true);
    out.setUint32(8 * i + 4, Math.floor(x / 2 ** 32), true);
  });
  // The offsets are copied, and cut as contiguous values (spec/conventions.md §7).
  const [offsetShape, cut] = await gridChunks(0, [count + 1], 8, async (o, n) => offsets.slice(o, o + n));
  const offsetChunks = new Map<string, Uint8Array>();
  for (const [key, v] of cut) {
    offsetChunks.set(key, v instanceof Uint8Array ? v : concat(v.map((r) => (
      r instanceof Uint8Array ? r : offsets.subarray(r[0], r[0] + r[1])))));
  }
  const plans: Plan[] = [{
    path: `${path}/offsets`, dataType: "int64", shape: [count + 1], chunkShape: offsetShape, dims: ["index"],
    chunks: offsetChunks,
  }];
  if (total === 0) return plans;
  const sizeC = Math.ceil(total / Math.ceil(total / RAGGED_CHUNK)); // balanced: k = ceil(total / 2^20) chunks
  const dataChunks = new Map<string, Part[] | Uint8Array>();
  let first = 0; // the first member that may reach the chunk
  let source = start; // where its extension is
  for (let c = 0; c < Math.ceil(total / sizeC); c++) {
    const lo = c * sizeC;
    const hi = Math.min(total, (c + 1) * sizeC);
    while (first < count && (inline.has(first) || starts[first + 1] <= lo)) source += esizes[first++];
    const ranges: Part[] = [];
    let fixed = 0; // the payload of the ranges but the last, as payloadSize counts them
    let copy: Uint8Array | undefined; // the chunk's bytes, once its ranges are over the payload
    let filled = 0; // how many
    for (let i = first, o = source; i < count && starts[i] < hi; o += esizes[i++]) {
      if (inline.has(i) || starts[i + 1] <= lo) continue;
      const at = starts[i];
      const begin = o + 8 + Math.max(lo, at) - at;
      const n = Math.min(hi, starts[i + 1]) - Math.max(lo, at);
      const last = ranges[ranges.length - 1] as [number, number] | undefined;
      if (copy !== undefined) {
        copy.set(await read(begin, n), filled);
        filled += n;
      } else if (last !== undefined && last[0] + last[1] === begin) {
        last[1] += n; // adjacent in the source: one range
      } else {
        if (last !== undefined) fixed += term(last);
        ranges.push([begin, n]);
      }
      if (copy === undefined && ranges.length > 1 && fixed + term(ranges[ranges.length - 1]) > MAX_PAYLOAD_BYTES) {
        copy = new Uint8Array(sizeC); // it stays over: copy it, the padding zero
        for (const [o, n] of ranges as [number, number][]) {
          copy.set(await read(o, n), filled);
          filled += n;
        }
      }
    }
    const pad = hi - lo < sizeC ? [new Uint8Array(sizeC - (hi - lo))] : [];
    if (copy !== undefined) {
      dataChunks.set(String(c), copy);
      continue;
    }
    ranges.push(...pad);
    if (payloadSize(ranges) > MAX_PAYLOAD_BYTES) {
      const parts: Uint8Array[] = [];
      for (const r of ranges) parts.push(r instanceof Uint8Array ? r : await read(r[0], r[1]));
      dataChunks.set(String(c), concat(parts));
    } else {
      dataChunks.set(String(c), ranges);
    }
  }
  plans.push({ path: `${path}/data`, dataType: "uint8", shape: [total], chunkShape: [sizeC], dims: ["byte"], chunks: dataChunks });
  return plans;
}

/** Reads of the chain, in increasing order, through a window of SCAN bytes, so that the data
 * of many short extensions is one read (it ends at the chain's `end`). */
function window(read: ByteReader, end: number): ByteReader {
  let start = 0;
  let data: Uint8Array = new Uint8Array();
  return async (offset, length) => {
    if (!(start <= offset && offset + length <= start + data.length)) {
      start = offset;
      data = await read(offset, Math.max(length, Math.min(SCAN, end - offset)));
    }
    return data.subarray(offset - start, offset - start + length);
  };
}

function concat(parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

async function allZero(read: ByteReader, start: number, end: number): Promise<boolean> {
  for (; start < end; start += SCAN) {
    if (!(await read(start, Math.min(SCAN, end - start))).every((x) => x === 0)) return false;
  }
  return true;
}

export async function virtualizeNifti(
  url: string,
  read: ByteReader,
  fileSize: number,
): Promise<ArchiveDesc & { summary: object }> {
  const version = detectNifti(await read(0, Math.min(552, fileSize)));
  if (version === undefined) return reject("not a NIfTI-1 or NIfTI-2 single file");
  // §7.1
  const layout = LAYOUTS[version];
  const length = layout.length;
  if (fileSize < length) reject(`file too short for a ${length}-byte NIfTI-${version} header`);
  const header = view(await read(0, length));
  const little = header.getInt32(0, true) === length;
  const one = (at: number, kind: Kind): number | bigint => {
    switch (kind) {
      case "u8": return header.getUint8(at);
      case "i16": return header.getInt16(at, little);
      case "i32": return header.getInt32(at, little);
      case "i64": return header.getBigInt64(at, little);
      case "f32": return header.getFloat32(at, little);
      case "f64": return header.getFloat64(at, little);
    }
  };
  const field = (name: keyof Omit<Layout, "length">): number | bigint => one(layout[name][0], layout[name][1]);
  const fields = (name: keyof Omit<Layout, "length">): (number | bigint)[] => {
    const [at, kind, count] = layout[name];
    const size = { u8: 1, i16: 2, i32: 4, i64: 8, f32: 4, f64: 8 }[kind];
    return Array.from({ length: count! }, (_, i) => one(at + i * size, kind));
  };
  const num = (name: keyof Omit<Layout, "length">) => Number(field(name));
  const nums = (name: keyof Omit<Layout, "length">) => fields(name).map(Number);

  const dim = fields("dim").map(BigInt);
  const n = Number(dim[0]);
  if (!(n >= 1 && n <= 7)) reject(`dim[0] = ${dim[0]} is not from 1 to 7`);
  const sizes = Array<bigint>(8).fill(1n);
  for (let i = 1; i <= n; i++) {
    if (dim[i] < 1n || dim[i] > MAX_SAFE) reject(`dim[${i}] = ${dim[i]} is not from 1 to 2^53 - 1`);
    sizes[i] = dim[i];
  }
  if (sizes[6] !== 1n || sizes[7] !== 1n) reject("dimensions 6 and 7 must have size 1");
  const code = num("datatype");
  const bitpix = num("bitpix");
  if (code in UNSUPPORTED) reject(`unsupported NIfTI datatype ${code} (${UNSUPPORTED[code]})`);
  if (!(code in DATATYPES)) reject(`unknown NIfTI datatype ${code}`);
  const [dataType, bits, samples] = DATATYPES[code];
  if (bitpix !== bits) reject(`bitpix ${bitpix} does not match datatype ${code}`);
  const color = samples > 1;
  if (color && n >= 5) reject("color data with a fifth dimension");
  const rawOffset = field("vox_offset");
  let vox: bigint;
  if (typeof rawOffset === "number") {
    if (!Number.isInteger(rawOffset)) reject(`vox_offset ${rawOffset} is not an integer`);
    vox = BigInt(rawOffset);
  } else {
    vox = rawOffset;
  }
  if (vox < BigInt(length + 4) || vox > MAX_SAFE) reject(`vox_offset ${vox} is not from ${length + 4} to 2^53 - 1`);
  const b = bits / 8;
  const total = sizes[1] * sizes[2] * sizes[3] * sizes[4] * sizes[5] * BigInt(b);
  if (total > MAX_SAFE || vox + total > BigInt(fileSize)) {
    reject(`the ${total}-byte voxel data at ${vox} is outside the ${fileSize}-byte file`);
  }
  const [X, Y, Z, T, C] = sizes.slice(1, 6).map(Number);
  const v = Number(vox);

  // spec/virtualize/nifti.md §4.2
  let scaling: [number, number] | undefined;
  if (!color) {
    const slope = num("scl_slope");
    const inter = num("scl_inter");
    if (Number.isFinite(slope) && slope !== 0) scaling = [slope, Number.isFinite(inter) ? inter : 0];
  }
  const nontrivial = scaling !== undefined && (scaling[0] !== 1 || scaling[1] !== 0);

  // spec/virtualize/nifti.md §4.3
  const unitsCode = num("xyzt_units");
  const space = SPACE_UNITS[unitsCode & 7];
  const time = TIME_UNITS[unitsCode & 56];
  const pixdim = nums("pixdim");
  let affine: "sform" | "qform" | null = null;
  let diagonal: number[] | undefined;
  let offset: number[] | undefined;
  if (num("sform_code") > 0) {
    affine = "sform";
    const s = nums("srow");
    if (s.every(Number.isFinite) && [1, 2, 4, 6, 8, 9].every((k) => s[k] === 0) && s[0] > 0 && s[5] > 0 && s[10] > 0) {
      diagonal = [s[0], s[5], s[10]];
      offset = [s[3], s[7], s[11]];
    }
  } else if (num("qform_code") > 0) {
    affine = "qform";
    const q = nums("quatern");
    const o = nums("qoffset");
    if (q.every((x) => x === 0) && !(pixdim[0] < 0) && pixdim.slice(1, 4).every(valid) && o.every(Number.isFinite)) {
      diagonal = pixdim.slice(1, 4);
      offset = o;
    }
  }
  const scale: Record<string, number> = { t: valid(pixdim[4]) ? pixdim[4] : 1, c: 1 };
  const unit: Record<string, string | undefined> = { t: valid(pixdim[4]) ? time : undefined };
  ["x", "y", "z"].forEach((a, k) => {
    const s = diagonal ? diagonal[k] : pixdim[k + 1];
    scale[a] = valid(s) ? s : 1;
    unit[a] = valid(s) ? space : undefined;
  });

  // spec/virtualize/nifti.md §4.4
  let channels: object[] | undefined;
  if (color) {
    channels = COLORS.slice(0, samples).map(([label, color]) => ({
      label, color, active: true, window: { min: 0, max: 255, start: 0, end: 255 },
    }));
  } else {
    let lo = num("cal_min");
    let hi = num("cal_max");
    if (Number.isFinite(lo) && Number.isFinite(hi) && hi > lo && C <= MAX_CHANNELS) {
      if (scaling) {
        const [s, i] = scaling;
        const a = (lo - i) / s;
        const c = (hi - i) / s;
        [lo, hi] = a <= c ? [a, c] : [c, a];
      }
      if (Number.isFinite(lo) && Number.isFinite(hi)) { // else the scaled window overflowed: none
        channels = Array.from({ length: C }, (_, k) => ({
          label: `C${k}`, color: "FFFFFF", active: true, window: { min: lo, max: hi, start: lo, end: hi },
        }));
      }
    }
  }

  // spec/virtualize/nifti.md §4.1
  const axes: string[] = [];
  if (n >= 4) axes.push("t");
  if (n >= 5 || color) axes.push("c");
  if (n >= 3) axes.push("z");
  axes.push("y", "x");
  const shape: Record<string, number> = { t: T, c: color ? samples : C, z: Z, y: Y, x: X };
  // The voxels in file order: dimension 5 outermost, a color type's samples innermost.
  const stored = [...(n >= 5 ? ["c"] : []), ...axes.filter((a) => a !== "c"), ...(color ? ["c"] : [])];
  const [grid, voxelChunks] = await gridChunks(v, stored.map((a) => shape[a]), b / samples, read, CHUNK_BYTES);
  const chunkShape = Object.fromEntries(stored.map((a, i) => [a, grid[i]]));
  const translation = diagonal ? axes.map((a) => (a === "x" ? offset![0] : a === "y" ? offset![1] : a === "z" ? offset![2] : 0)) : undefined;
  const endian = little ? "little" : "big";
  const codecs: unknown[] = [];
  if (stored.join() !== axes.join()) {
    codecs.push({ name: "transpose", configuration: { order: stored.map((a) => axes.indexOf(a)) } });
  }
  codecs.push(bits / samples > 8 ? { name: "bytes", configuration: { endian } } : { name: "bytes" });

  const utf8 = new TextEncoder();
  const json = (x: unknown) => utf8.encode(stringifyJson(x));
  const ome = {
    version: "0.5",
    multiscales: [{
      axes: axes.map((a) => ({ name: a, type: TYPES[a], ...(unit[a] ? { unit: unit[a] } : {}) })),
      datasets: [{
        path: "0",
        coordinateTransformations: [
          { type: "scale", scale: axes.map((a) => scale[a]) },
          ...(translation ? [{ type: "translation", translation }] : []),
        ],
      }],
    }],
    ...(channels ? { omero: { channels } } : {}),
  };
  // The source metadata (spec/virtualize/nifti.md §5). A float field is a number, or
  // {"bits": hex} for a negative zero or a NaN other than the canonical one, whose bits
  // JSON numbers and "NaN" do not keep.
  const floatJson = (at: number, kind: "f32" | "f64"): unknown => {
    const bits = kind === "f32" ? BigInt(header.getUint32(at, little)) : header.getBigUint64(at, little);
    const v = Number(one(at, kind));
    const canonical = kind === "f32" ? 0x7fc00000n : 0x7ff8000000000000n;
    if (Object.is(v, -0) || (Number.isNaN(v) && bits !== canonical)) {
      return { bits: bits.toString(16).padStart(kind === "f32" ? 8 : 16, "0") };
    }
    return jsonNumber(v);
  };
  const headerJson: Record<string, unknown> = {};
  const headerRest: Record<string, string> = {};
  for (const [name, at, kind, count] of HEADER_FIELDS[version]) {
    if (kind[0] === "s") {
      const raw = new Uint8Array(header.buffer, header.byteOffset + at, Number(kind.slice(1)));
      const nul = raw.indexOf(0);
      headerJson[name] = textJson(nul < 0 ? raw : raw.subarray(0, nul));
      const tail = nul < 0 ? new Uint8Array() : raw.subarray(nul + 1);
      if (name !== "magic" && tail.some((x) => x !== 0)) headerRest[name] = base64(tail);
    } else {
      const k = kind as Kind;
      const value = (p: number) => (k === "f32" || k === "f64" ? floatJson(p, k) : jsonNumber(one(p, k)));
      headerJson[name] = count === undefined ? value(at) : Array.from({ length: count }, (_, i) => value(at + i * SIZES[k]));
    }
  }
  const meta: Record<string, unknown> = { nifti_version: version, byte_order: endian, header: headerJson };
  if (Object.keys(headerRest).length > 0) meta.header_rest = headerRest;
  const extender = await read(length, 4);
  if (!(extender[0] <= 1 && extender[1] === 0 && extender[2] === 0 && extender[3] === 0)) {
    meta.extender = base64(extender);
  }
  const extended = extender[0] !== 0;
  const plans: Plan[] = [];
  const bytesPlan = (path: string, at: number, n: number): Plan => {
    const [rows, chunks] = rowChunks(at, n, 1);
    return { path, dataType: "uint8", shape: [n], chunkShape: [rows], dims: ["byte"], chunks };
  };
  let q = length + 4;
  if (extended) {
    const [esizes, ecodes, end] = await readChain(read, little, q, v);
    const [value, more] = await extensionsValue(read, little, endian, q, end, esizes, ecodes);
    meta.extensions = value;
    plans.push(...more);
    q = end;
  }
  if (q < v && !(await allZero(read, q, v))) { // bytes the chain does not hold, and not padding
    if (extended) meta.extensions_truncated = true;
    plans.push(bytesPlan("unparsed", q, v - q));
    meta.unparsed = `${SOURCE_NODE}/unparsed`;
  }
  const end = v + Number(total);
  if (end < fileSize) { // after the voxel data
    plans.push(bytesPlan("trailing", end, fileSize - end));
    meta.trailing = `${SOURCE_NODE}/trailing`;
  }
  if (nontrivial) meta.scaling = { slope: scaling![0], inter: scaling![1] };
  if (affine !== null) meta.affine = { form: affine, applied: diagonal !== undefined };
  const attributes = declare({ ome }, "nifti", url, meta);
  const entries: EntryDesc[] = [];
  for (const [coords, parts] of voxelChunks) { // spec/virtualize/nifti/profile.md §7.2
    const at = coords.split("/");
    const key = `0/c/${axes.map((a) => at[stored.indexOf(a)]).join("/")}`;
    entries.push(parts instanceof Uint8Array ? { key, bytes: parts, compress: true } : { key, ranges: parts.map(toRange) });
  }
  const chunks = entries.length;
  entries.push(
    { key: "zarr.json", bytes: json({ zarr_format: 3, node_type: "group", attributes }) },
    {
      key: "0/zarr.json",
      bytes: json({
        zarr_format: 3,
        node_type: "array",
        shape: axes.map((a) => shape[a]),
        data_type: dataType,
        chunk_grid: { name: "regular", configuration: { chunk_shape: axes.map((a) => chunkShape[a]) } },
        chunk_key_encoding: { name: "default", configuration: { separator: "/" } },
        fill_value: 0,
        codecs,
        dimension_names: axes,
        attributes: {},
      }),
    },
  );
  if (plans.length > 0) entries.push(...emitPlans(plans, declare({}, "nifti", undefined)));
  return {
    sources: [{ url }],
    entries,
    summary: {
      version, byteOrder: endian, sizes: Object.fromEntries(axes.map((a) => [a, shape[a]])), dataType, color,
      chunkShape: axes.map((a) => chunkShape[a]), chunks, scaling: nontrivial ? { slope: scaling![0], inter: scaling![1] } : null, affine,
      translation: translation !== undefined, extensions: extender[0] !== 0,
    },
  };
}
