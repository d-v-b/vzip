// TIFF profile (VIRTUALIZE.md §3).
import {
  MAX_SAFE,
  Output,
  Source,
  UNITS,
  arrayJson,
  axisType,
  buildCodecs,
  finite,
  reject,
} from "./io.ts";
import type { Axis, Range } from "./io.ts";
import { parseOme } from "./omexml.ts";
import type { OmeInfo } from "./omexml.ts";

const TYPE_SIZE: Record<number, number> = {
  1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8,
};
const INT_TYPES = new Set([1, 3, 4, 6, 8, 9, 13, 16, 17, 18]);

type Field = { tag: number; type: number; count: number; valueField: Uint8Array; valueFieldPos: number };

class Ifd {
  offset: number;
  fields = new Map<number, Field>();
  next = 0;
  constructor(offset: number) {
    this.offset = offset;
  }
}

class Tiff {
  src: Source;
  le = true;
  big = false;
  ifdsRead = 0;
  seen = new Set<number>();
  cache = new Map<string, unknown>();

  constructor(src: Source) {
    this.src = src;
  }

  u16(b: Uint8Array, o: number): number {
    return new DataView(b.buffer, b.byteOffset, b.byteLength).getUint16(o, this.le);
  }
  u32(b: Uint8Array, o: number): number {
    return new DataView(b.buffer, b.byteOffset, b.byteLength).getUint32(o, this.le);
  }
  u64(b: Uint8Array, o: number): number {
    const v = new DataView(b.buffer, b.byteOffset, b.byteLength).getBigUint64(o, this.le);
    if (v > BigInt(MAX_SAFE)) reject(`64-bit value ${v} above 2^53 - 1`);
    return Number(v);
  }

  async header(): Promise<number> {
    const h = await this.src.read(0, 4);
    this.le = h[0] === 0x49;
    const magic = this.u16(h, 2);
    if (magic === 42) {
      const b = await this.src.read(0, 8);
      return this.u32(b, 4);
    }
    this.big = true;
    const b = await this.src.read(0, 16);
    if (this.u16(b, 4) !== 8) reject("BigTIFF offset size is not 8");
    if (this.u16(b, 6) !== 0) reject("BigTIFF reserved word is not 0");
    return this.u64(b, 8);
  }

  async readIfd(offset: number): Promise<Ifd> {
    if (this.seen.has(offset)) reject(`IFD cycle at offset ${offset}`);
    this.seen.add(offset);
    if (++this.ifdsRead > 100000) reject("more than 100000 IFDs");
    const ifd = new Ifd(offset);
    const cs = this.big ? 8 : 2;
    const es = this.big ? 20 : 12;
    const os = this.big ? 8 : 4;
    const cb = await this.src.read(offset, cs);
    const n = this.big ? this.u64(cb, 0) : this.u16(cb, 0);
    if (n * es > MAX_SAFE) reject("IFD too large");
    const body = await this.src.read(offset + cs, n * es + os);
    for (let i = 0; i < n; i++) {
      const p = i * es;
      const tag = this.u16(body, p);
      const type = this.u16(body, p + 2);
      let count: number;
      if (this.big) {
        const v = new DataView(body.buffer, body.byteOffset).getBigUint64(p + 4, this.le);
        count = v > BigInt(MAX_SAFE) ? Infinity : Number(v);
      } else count = this.u32(body, p + 4);
      const vf = body.subarray(p + (this.big ? 12 : 8), p + es);
      // Of duplicate tags in an IFD, the first is used.
      if (!ifd.fields.has(tag))
        ifd.fields.set(tag, { tag, type, count, valueField: vf, valueFieldPos: offset + cs + p + (this.big ? 12 : 8) });
    }
    ifd.next = this.big ? this.u64(body, n * es) : this.u32(body, n * es);
    return ifd;
  }

  /** Raw value bytes of a field; rejects unknown field types. */
  async valueBytes(f: Field): Promise<Uint8Array> {
    const sz = TYPE_SIZE[f.type];
    if (sz === undefined) reject(`tag ${f.tag}: unknown field type ${f.type}`);
    const total = f.count * sz;
    if (!Number.isSafeInteger(total)) reject(`tag ${f.tag}: count too large`);
    const inline = this.big ? 8 : 4;
    if (total <= inline) return f.valueField.subarray(0, total);
    const off = this.big ? this.u64(f.valueField, 0) : this.u32(f.valueField, 0);
    return await this.src.read(off, total);
  }

  /** All values of an integer tag, or undefined if absent. */
  async ints(ifd: Ifd, tag: number): Promise<number[] | undefined> {
    const key = `${ifd.offset}:${tag}`;
    if (this.cache.has(key)) return this.cache.get(key) as number[];
    const f = ifd.fields.get(tag);
    if (!f) return undefined;
    const b = await this.valueBytes(f);
    if (!INT_TYPES.has(f.type)) reject(`tag ${tag}: field type ${f.type} is not an integer type`);
    const dv = new DataView(b.buffer, b.byteOffset, b.byteLength);
    const out: number[] = new Array(f.count);
    for (let i = 0; i < f.count; i++) {
      let v: number;
      switch (f.type) {
        case 1: v = dv.getUint8(i); break;
        case 6: v = dv.getInt8(i); break;
        case 3: v = dv.getUint16(i * 2, this.le); break;
        case 8: v = dv.getInt16(i * 2, this.le); break;
        case 4: case 13: v = dv.getUint32(i * 4, this.le); break;
        case 9: v = dv.getInt32(i * 4, this.le); break;
        default: {
          const bv = f.type === 17 ? dv.getBigInt64(i * 8, this.le) : dv.getBigUint64(i * 8, this.le);
          if (bv > BigInt(MAX_SAFE) || bv < 0n) reject(`tag ${tag}: value ${bv} out of range`);
          v = Number(bv);
        }
      }
      if (v < 0) reject(`tag ${tag}: negative value ${v}`);
      out[i] = v;
    }
    this.cache.set(key, out);
    return out;
  }

  /** First value of an integer tag; default when absent; rejects if absent without default. */
  async int(ifd: Ifd, tag: number, def?: number): Promise<number> {
    const v = await this.ints(ifd, tag);
    if (v === undefined) {
      if (def === undefined) reject(`IFD at ${ifd.offset}: required tag ${tag} missing`);
      return def;
    }
    if (v.length === 0) reject(`IFD at ${ifd.offset}: tag ${tag} has no values`);
    return v[0];
  }

  isTiled(ifd: Ifd): boolean {
    return ifd.fields.has(322) && ifd.fields.has(324);
  }
}

type Format = { bps: number; spp: number; sf: number; planar: number; comp: number; pred: number };

function sameFormat(a: Format, b: Format): boolean {
  return a.bps === b.bps && a.spp === b.spp && a.sf === b.sf && a.planar === b.planar && a.comp === b.comp && a.pred === b.pred;
}

async function format(t: Tiff, ifd: Ifd): Promise<Format> {
  const bpsAll = await t.ints(ifd, 258);
  if (bpsAll === undefined) reject(`IFD at ${ifd.offset}: BitsPerSample missing`);
  if (bpsAll.length === 0) reject(`IFD at ${ifd.offset}: BitsPerSample has no values`);
  if (bpsAll.some((v) => v !== bpsAll[0])) reject(`IFD at ${ifd.offset}: BitsPerSample values differ`);
  const spp = await t.int(ifd, 277, 1);
  const sfAll = (await t.ints(ifd, 339)) ?? [1];
  if (sfAll.length === 0) reject(`IFD at ${ifd.offset}: SampleFormat has no values`);
  if (sfAll.some((v) => v !== sfAll[0])) reject(`IFD at ${ifd.offset}: SampleFormat values differ`);
  let planar = 1;
  if (spp > 1) {
    planar = await t.int(ifd, 284, 1);
    if (planar !== 1 && planar !== 2) reject(`IFD at ${ifd.offset}: PlanarConfiguration ${planar}`);
  }
  const comp = await t.int(ifd, 259, 1);
  const pred = await t.int(ifd, 317, 1);
  return { bps: bpsAll[0], spp, sf: sfAll[0], planar, comp, pred };
}

type Geometry = { w: number; h: number; tw: number; th: number };

async function geometry(t: Tiff, ifd: Ifd): Promise<Geometry> {
  const w = await t.int(ifd, 256);
  const h = await t.int(ifd, 257);
  if (!t.isTiled(ifd)) reject(`IFD at ${ifd.offset} is not tiled`);
  const tw = await t.int(ifd, 322);
  const th = await t.int(ifd, 323);
  if (!ifd.fields.has(325)) reject(`IFD at ${ifd.offset}: TileByteCounts missing`);
  if (w < 1 || h < 1) reject(`IFD at ${ifd.offset}: zero width or length`);
  if (tw < 1 || th < 1) reject(`IFD at ${ifd.offset}: zero tile width or length`);
  return { w, h, tw, th };
}

type Plane = { z: number; c: number; t: number; ifd: number };

export async function virtualizeTiff(src: Source, out: Output): Promise<void> {
  const t = new Tiff(src);
  // ---- §3.1 IFDs
  const main: Ifd[] = [];
  let off = await t.header();
  if (off === 0) reject("no IFD 0");
  while (off !== 0) {
    const ifd = await t.readIfd(off);
    main.push(ifd);
    off = ifd.next;
  }
  const subs = new Map<Ifd, Ifd[]>();
  for (const ifd of main) {
    const offs = (await t.ints(ifd, 330)) ?? [];
    const list: Ifd[] = [];
    for (const o of offs) list.push(await t.readIfd(o));
    subs.set(ifd, list);
  }
  const ifd0 = main[0];

  // ---- §3.2 OME-XML
  let D: Uint8Array | null = null;
  let ome: OmeInfo | null = null;
  const desc = ifd0.fields.get(270);
  if (desc) {
    if (TYPE_SIZE[desc.type] === undefined) reject(`ImageDescription: unknown field type ${desc.type}`);
    if (desc.type === 2) {
      const bytes = await t.valueBytes(desc);
      const nul = bytes.indexOf(0);
      const d = nul < 0 ? bytes : bytes.subarray(0, nul);
      let text: string | null = null;
      try {
        text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(d);
      } catch {
        text = null;
      }
      if (text !== null) {
        const info = parseOme(text);
        if (info) {
          D = d;
          ome = info;
        }
      }
    }
  }

  // ---- §3.3 planes
  const fmt0 = await format(t, ifd0);
  const spp = fmt0.spp;
  let sizeZ = 1, sizeT = 1, sizeC = spp;
  let planes: Plane[];
  if (!ome) {
    planes = [{ z: 0, c: 0, t: 0, ifd: 0 }];
  } else {
    sizeZ = ome.sizeZ ?? 1;
    sizeT = ome.sizeT ?? 1;
    sizeC = ome.sizeC ?? spp;
    if (spp > 1) {
      if (sizeC === 1) sizeC = spp;
      else if (sizeC !== spp) reject(`SizeC ${sizeC} differs from SamplesPerPixel ${spp}`);
    }
    const cp = spp > 1 ? 1 : sizeC;
    const sizes: Record<string, number> = { Z: sizeZ, C: cp, T: sizeT };
    const order = ome.dimensionOrder.slice(2); // fastest first
    const count = sizeZ * cp * sizeT;
    if (!Number.isSafeInteger(count)) reject("plane count too large");
    const tds = ome.tiffData.length ? ome.tiffData : [{ attrs: {}, uuidKey: null }];
    // multi-file
    const files = new Set<string>();
    for (const td of tds) if (td.uuidKey !== null) files.add(td.uuidKey);
    if (files.size > 1) reject("multi-file dataset");
    // capacity bound: a TiffData maps at most (chain length - IFD) planes to existing IFDs.
    const parsed = tds.map((td) => {
      const a = td.attrs;
      const fz = a.FirstZ ?? 0, fc = a.FirstC ?? 0, ft = a.FirstT ?? 0;
      if (fz >= sizeZ) reject(`FirstZ ${fz} >= SizeZ ${sizeZ}`);
      if (fc >= cp) reject(`FirstC ${fc} >= ${cp}`);
      if (ft >= sizeT) reject(`FirstT ${ft} >= SizeT ${sizeT}`);
      const ifdIdx = a.IFD ?? 0;
      const pc = a.PlaneCount ?? (tds.length === 1 && a.IFD === undefined ? count : 1);
      return { fz, fc, ft, ifdIdx, pc };
    });
    let capacity = 0;
    for (const p of parsed) capacity += Math.max(0, Math.min(p.pc, main.length - p.ifdIdx));
    if (count > capacity) reject("not every plane can be mapped to an existing IFD");
    const map = new Float64Array(count).fill(-1);
    const pos: Record<string, number> = {};
    const stride: Record<string, number> = {};
    let s = 1;
    for (const d of order) {
      stride[d] = s;
      s *= sizes[d];
    }
    for (const p of parsed) {
      pos.Z = p.fz; pos.C = p.fc; pos.T = p.ft;
      const start = pos.Z * stride.Z + pos.C * stride.C + pos.T * stride.T;
      const n = Math.min(p.pc, count - start);
      for (let k = 0; k < n; k++) map[start + k] = p.ifdIdx + k;
    }
    planes = [];
    for (let i = 0; i < count; i++) {
      const ifdIdx = map[i];
      if (ifdIdx < 0 || ifdIdx >= main.length) reject(`plane ${i} not mapped to an existing IFD`);
      const z = Math.floor(i / stride.Z) % sizeZ;
      const c = Math.floor(i / stride.C) % cp;
      const tt = Math.floor(i / stride.T) % sizeT;
      planes.push({ z, c, t: tt, ifd: ifdIdx });
    }
  }

  // ---- §3.4 levels: levels[L][planeIndex] = Ifd
  const levels: Ifd[][] = [planes.map((p) => main[p.ifd])];
  const s0 = subs.get(ifd0)!;
  if (s0.length > 0) {
    const s = s0.length;
    for (const pl of levels[0]) if (subs.get(pl)!.length < s) reject("plane IFD has fewer SubIFDs than IFD 0");
    for (let k = 1; k <= s; k++) levels.push(levels[0].map((pl) => subs.get(pl)![k - 1]));
  } else if (!ome) {
    let last = await geometryWH(t, ifd0);
    for (let i = 1; i < main.length; i++) {
      const ifd = main[i];
      if (!t.isTiled(ifd)) continue;
      if (!ifd.fields.has(258)) continue;
      if (!sameFormat(await format(t, ifd), fmt0)) continue;
      const wh = await geometryWH(t, ifd);
      if (wh.w < last.w && wh.h < last.h) {
        levels.push([ifd]);
        last = wh;
      }
    }
  }

  // validate levels
  const geoms: Geometry[] = [];
  for (const lvl of levels) {
    let g: Geometry | null = null;
    for (const ifd of lvl) {
      const f = await format(t, ifd);
      if (!sameFormat(f, fmt0)) reject(`IFD at ${ifd.offset}: format differs from IFD 0`);
      const gi = await geometry(t, ifd);
      if (g && (g.w !== gi.w || g.h !== gi.h || g.tw !== gi.tw || g.th !== gi.th))
        reject("planes of a level differ in size or tile size");
      g = gi;
    }
    geoms.push(g!);
  }

  // ---- §3.5 data type and codecs
  const kind = fmt0.sf === 1 ? "uint" : fmt0.sf === 2 ? "int" : fmt0.sf === 3 ? "float" : null;
  if (!kind) reject(`SampleFormat ${fmt0.sf} unsupported`);
  const okBits = kind === "float" ? [32, 64] : [8, 16, 32, 64];
  if (!okBits.includes(fmt0.bps)) reject(`BitsPerSample ${fmt0.bps} unsupported for ${kind}`);
  const dataType = `${kind}${fmt0.bps}`;
  let a2b: "bytes" | "jpeg2k" = "bytes";
  let compressor: "zlib" | "zstd" | null = null;
  const c = fmt0.comp;
  if ([33003, 33004, 33005, 34712].includes(c)) a2b = "jpeg2k";
  else {
    if (fmt0.pred !== 1) reject(`Predictor ${fmt0.pred} unsupported`);
    if (c === 1) compressor = null;
    else if (c === 8 || c === 32946) compressor = "zlib";
    else if (c === 50000) compressor = "zstd";
    else reject(`Compression ${c} unsupported`);
  }
  const interleaved = spp > 1 && fmt0.planar === 1;
  const planarSamples = spp > 1 && fmt0.planar === 2;

  // ---- §3.6 output
  const channels = ome ? sizeC : spp;
  const axes: string[] = [];
  if (sizeT > 1) axes.push("t");
  if (channels > 1) axes.push("c");
  if (sizeZ > 1) axes.push("z");
  axes.push("y", "x");

  const px = ome?.physX ?? null, py = ome?.physY ?? null, pz = ome?.physZ ?? null;
  const axisObjs: Axis[] = axes.map((n) => {
    const a: Axis = { name: n, type: axisType(n) };
    if (ome && (n === "x" || n === "y" || n === "z")) {
      const ph = n === "x" ? px : n === "y" ? py : pz;
      if (ph !== null) {
        const sym = n === "x" ? ome.unitX : n === "y" ? ome.unitY : ome.unitZ;
        const u = UNITS[sym ?? "µm"];
        if (u) a.unit = u;
      }
    }
    return a;
  });

  const H0 = geoms[0].h, W0 = geoms[0].w;
  const datasets: unknown[] = [];
  for (let L = 0; L < levels.length; L++) {
    const g = geoms[L];
    const scale = axes.map((n) => {
      if (n === "y") return finite((py ?? 1) * (H0 / g.h), "y scale");
      if (n === "x") return finite((px ?? 1) * (W0 / g.w), "x scale");
      if (n === "z") return pz ?? 1;
      return 1;
    });
    datasets.push({ path: `${L}`, coordinateTransformations: [{ type: "scale", scale }] });
  }
  const ms: Record<string, unknown> = {};
  if (ome && ome.imageName) ms.name = ome.imageName;
  ms.axes = axisObjs;
  ms.datasets = datasets;
  out.json("zarr.json", { zarr_format: 3, node_type: "group", attributes: { ome: { version: "0.5", multiscales: [ms] } } });

  for (let L = 0; L < levels.length; L++) {
    const g = geoms[L];
    const shape: number[] = [];
    const chunk: number[] = [];
    for (const n of axes) {
      if (n === "t") { shape.push(sizeT); chunk.push(1); }
      else if (n === "c") { shape.push(channels); chunk.push(interleaved ? spp : 1); }
      else if (n === "z") { shape.push(sizeZ); chunk.push(1); }
      else if (n === "y") { shape.push(g.h); chunk.push(g.th); }
      else { shape.push(g.w); chunk.push(g.tw); }
    }
    out.json(`${L}/zarr.json`, arrayJson({
      shape, dataType, chunkShape: chunk,
      codecs: buildCodecs({ axes, interleaved, arrayToBytes: a2b, itemSize: fmt0.bps / 8, littleEndian: t.le, compressor }),
      dimensionNames: axes,
    }));

    const rows = Math.ceil(g.h / g.th), cols = Math.ceil(g.w / g.tw);
    const T = rows * cols;
    const nsp = planarSamples ? spp : 1;
    for (let pi = 0; pi < planes.length; pi++) {
      const plane = planes[pi];
      const ifd = levels[L][pi];
      const offs = (await t.ints(ifd, 324))!;
      const cnts = await t.ints(ifd, 325);
      if (!cnts) reject("TileByteCounts missing");
      if (offs.length !== T * nsp || cnts.length !== T * nsp)
        reject(`IFD at ${ifd.offset}: tile count ${offs.length}/${cnts.length} != ${T * nsp}`);
      for (let sIdx = 0; sIdx < nsp; sIdx++) {
        for (let j = 0; j < T; j++) {
          const k = sIdx * T + j;
          const n = cnts[k];
          if (n <= 0) continue;
          const coords: number[] = [];
          for (const a of axes) {
            if (a === "t") coords.push(plane.t);
            else if (a === "c") coords.push(planarSamples ? sIdx : interleaved ? 0 : plane.c);
            else if (a === "z") coords.push(plane.z);
          }
          coords.push(Math.floor(j / cols), j % cols);
          const r: Range = [0, offs[k], n];
          out.ref(`${L}/c/${coords.join("/")}`, [r]);
        }
      }
    }
  }
  if (D) out.bytes("OME/METADATA.ome.xml", D);
}

async function geometryWH(t: Tiff, ifd: Ifd): Promise<{ w: number; h: number }> {
  return { w: await t.int(ifd, 256), h: await t.int(ifd, 257) };
}
