// Virtualizing a NIfTI-1 or NIfTI-2 single file (.nii) by the NIfTI profile
// (profiles/nifti.md, §7): the voxel data is one contiguous block, and each
// z-slice, or each row block of a large one, becomes a Zarr chunk that
// references it.

import type { ByteReader } from "../common.ts";
import type { ArchiveDesc, EntryDesc } from "../../writer.ts";

export class NiftiError extends Error {}

const reject = (message: string): never => {
  throw new NiftiError(message);
};
const view = (b: Uint8Array) => new DataView(b.buffer, b.byteOffset, b.byteLength);

const MAX_SAFE = BigInt(Number.MAX_SAFE_INTEGER);
const BLOCK_BYTES = 1 << 17; // the most bytes of a row block (§7.3)

type Kind = "i16" | "i32" | "i64" | "u8" | "f32" | "f64";
type Field = [offset: number, kind: Kind, count?: number];
interface Layout {
  length: number;
  dim: Field; datatype: Field; bitpix: Field; pixdim: Field; vox_offset: Field;
  scl_slope: Field; scl_inter: Field; xyzt_units: Field; cal_max: Field; cal_min: Field;
  qform_code: Field; sform_code: Field; quatern: Field; qoffset: Field; srow: Field;
}

// Header length and the offset and kind of each field (§7.1).
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

// datatype: [Zarr data type, bitpix, samples per voxel] (§7.2).
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
const COLOURS: [string, string][] = [["R", "FF0000"], ["G", "00FF00"], ["B", "0000FF"], ["A", "FFFFFF"]];
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
  const colour = samples > 1;
  if (colour && n >= 5) reject("colour data with a fifth dimension");
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

  // §7.3
  const row = X * b;
  let h = 1;
  for (let d = Math.min(Y, Math.floor(BLOCK_BYTES / row)); d > 1; d--) {
    if (Y % d === 0) {
      h = d;
      break;
    }
  }

  // §7.4
  let scaling: [number, number] | undefined;
  if (!colour) {
    const slope = num("scl_slope");
    const inter = num("scl_inter");
    if (Number.isFinite(slope) && slope !== 0) scaling = [slope, Number.isFinite(inter) ? inter : 0];
  }
  const nontrivial = scaling !== undefined && (scaling[0] !== 1 || scaling[1] !== 0);

  // §7.5
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

  // §7.6
  let channels: object[] | undefined;
  if (colour) {
    channels = COLOURS.slice(0, samples).map(([label, color]) => ({
      label, color, active: true, window: { min: 0, max: 255, start: 0, end: 255 },
    }));
  } else {
    let lo = num("cal_min");
    let hi = num("cal_max");
    if (Number.isFinite(lo) && Number.isFinite(hi) && hi > lo) {
      if (scaling) {
        const [s, i] = scaling;
        const a = (lo - i) / s;
        const c = (hi - i) / s;
        [lo, hi] = a <= c ? [a, c] : [c, a];
        if (!Number.isFinite(lo) || !Number.isFinite(hi)) reject("the display window is not finite");
      }
      channels = Array.from({ length: C }, (_, k) => ({
        label: `C${k}`, color: "FFFFFF", active: true, window: { min: lo, max: hi, start: lo, end: hi },
      }));
    }
  }

  // §7.7
  const axes: string[] = [];
  if (n >= 4) axes.push("t");
  if (n >= 5 || colour) axes.push("c");
  if (n >= 3) axes.push("z");
  axes.push("y", "x");
  const shape: Record<string, number> = { t: T, c: colour ? samples : C, z: Z, y: Y, x: X };
  const chunkShape: Record<string, number> = { t: 1, c: samples, z: 1, y: h, x: X };
  const translation = diagonal ? axes.map((a) => (a === "x" ? offset![0] : a === "y" ? offset![1] : a === "z" ? offset![2] : 0)) : undefined;
  const endian = little ? "little" : "big";
  const codecs: unknown[] = [];
  if (colour) {
    const stored = axes.filter((a) => a !== "c").concat("c");
    codecs.push({ name: "transpose", configuration: { order: stored.map((a) => axes.indexOf(a)) } });
  }
  codecs.push(bits / samples > 8 ? { name: "bytes", configuration: { endian } } : { name: "bytes" });

  const utf8 = new TextEncoder();
  const json = (x: unknown) => utf8.encode(JSON.stringify(x, null, 2));
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
  const attributes = {
    ome,
    ...(nontrivial ? { nifti: { scl_slope: scaling![0], scl_inter: scaling![1] } } : {}),
  };
  const entries: EntryDesc[] = [];
  const slab = Y * row;
  for (let k = 0; k < C; k++) {
    for (let t = 0; t < T; t++) {
      for (let z = 0; z < Z; z++) {
        const start = v + ((k * T + t) * Z + z) * slab;
        for (let j = 0; j < Y / h; j++) {
          const coords: Record<string, number> = { t, c: k, z, y: j, x: 0 };
          entries.push({
            key: `0/c/${axes.map((a) => coords[a]).join("/")}`,
            ranges: [{ source: 0, offset: BigInt(start + j * h * row), length: BigInt(h * row) }],
          });
        }
      }
    }
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
  const extender = await read(length, 1);
  return {
    sources: [{ url }],
    entries,
    summary: {
      version, byteOrder: endian, sizes: Object.fromEntries(axes.map((a) => [a, shape[a]])), dataType, colour,
      rowBlock: h, chunks, scaling: nontrivial ? { slope: scaling![0], inter: scaling![1] } : null, affine,
      translation: translation !== undefined, extensions: extender[0] !== 0,
    },
  };
}
