// TIFF profile, VIRTUALIZE.md §3.

import {
  MAX_SAFE,
  Output,
  Source,
  arrayDoc,
  axisObj,
  big2num,
  bytesCodec,
  fin,
  groupDoc,
  reject,
  transposeCodec,
} from "./io.ts";
import { decodeRefs, scan, textBetween, trimWs } from "./xml.ts";

const TYPE_SIZE: Record<number, number> = {
  1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8,
};
const UINT_TYPES = new Set([1, 3, 4, 13, 16, 18]);
const SCALAR_TAGS = new Set([256, 257, 259, 277, 284, 317, 322, 323]);
const ARRAY_TAGS = new Set([258, 324, 325, 330, 339]);
const TEXT_TAG = 270;
const MAX_IFDS = 100000;

type Entry = { type: number; count: number; pos: number };
type IFD = { offset: number; entries: Map<number, Entry>; subOffsets: number[]; subs: IFD[] };
type Fmt = { bps: number; spp: number; sf: number; planar: number; comp: number; pred: number };
type Size = { w: number; h: number; tiled: boolean; tw: number; tl: number };

const fmtEq = (a: Fmt, b: Fmt) =>
  a.bps === b.bps && a.spp === b.spp && a.sf === b.sf && a.planar === b.planar && a.comp === b.comp && a.pred === b.pred;

const UNITS: Record<string, string> = {
  "µm": "micrometer", "μm": "micrometer", um: "micrometer", nm: "nanometer", mm: "millimeter",
  cm: "centimeter", m: "meter", "Å": "angstrom", "Å": "angstrom", pm: "picometer", in: "inch",
  ft: "foot", s: "second", ms: "millisecond", min: "minute", h: "hour",
};

class Tiff {
  src: Source;
  le = true;
  big = false;
  nRead = 0;
  seen = new Set<number>();

  constructor(src: Source) {
    this.src = src;
  }

  u16(b: Uint8Array, i: number): number {
    return this.le ? b[i] | (b[i + 1] << 8) : (b[i] << 8) | b[i + 1];
  }
  u32(b: Uint8Array, i: number): number {
    return new DataView(b.buffer, b.byteOffset, b.byteLength).getUint32(i, this.le);
  }
  u64(b: Uint8Array, i: number): bigint {
    return new DataView(b.buffer, b.byteOffset, b.byteLength).getBigUint64(i, this.le);
  }

  async readIFD(off: number, main: boolean): Promise<{ ifd: IFD; next: number }> {
    if (off < (this.big ? 16 : 8)) reject(`IFD offset ${off} below the header`);
    if (this.seen.has(off)) reject(`IFD offset ${off} read twice`);
    this.seen.add(off);
    if (++this.nRead > MAX_IFDS) reject("more than 100000 IFDs");
    const big = this.big;
    const cntLen = big ? 8 : 2;
    const cb = await this.src.read(off, cntLen);
    const n = big ? big2num(this.u64(cb, 0), "IFD entry count") : this.u16(cb, 0);
    const entSize = big ? 20 : 12;
    if (n > MAX_SAFE / entSize) reject("IFD too large");
    const eb = await this.src.read(off + cntLen, n * entSize);
    let next = 0;
    if (main) {
      const nb = await this.src.read(off + cntLen + n * entSize, big ? 8 : 4);
      next = big ? big2num(this.u64(nb, 0), "next IFD offset") : this.u32(nb, 0);
    }
    const entries = new Map<number, Entry>();
    for (let i = 0; i < n; i++) {
      const e = i * entSize;
      const tag = this.u16(eb, e);
      if (tag !== TEXT_TAG && !SCALAR_TAGS.has(tag) && !ARRAY_TAGS.has(tag)) continue;
      if (entries.has(tag)) continue; // duplicate: first is used
      const type = this.u16(eb, e + 2);
      if (tag === TEXT_TAG) {
        if (!((type >= 1 && type <= 13) || (type >= 16 && type <= 18))) reject(`tag ${tag}: field type ${type}`);
      } else if (!UINT_TYPES.has(type)) reject(`tag ${tag}: field type ${type} is not unsigned integer`);
      const count = big ? this.u64(eb, e + 4) : BigInt(this.u32(eb, e + 4));
      if (SCALAR_TAGS.has(tag) && count < 1n) reject(`scalar tag ${tag} has no value`);
      const total = count * BigInt(TYPE_SIZE[type]);
      let pos: number;
      const valField = off + cntLen + e + (big ? 12 : 8);
      if (total <= (big ? 8n : 4n)) pos = valField;
      else {
        const vo = big ? this.u64(eb, e + 12) : BigInt(this.u32(eb, e + 8));
        if (vo + total > BigInt(this.src.size)) reject(`tag ${tag}: value outside the file`);
        pos = Number(vo);
      }
      entries.set(tag, { type, count: Number(count), pos });
    }
    const ifd: IFD = { offset: off, entries, subOffsets: [], subs: [] };
    const sub = entries.get(330);
    if (sub) ifd.subOffsets = await this.uints(sub);
    return { ifd, next };
  }

  async uints(e: Entry, limit = Infinity): Promise<number[]> {
    const cnt = Math.min(e.count, limit);
    const sz = TYPE_SIZE[e.type];
    const b = await this.src.read(e.pos, cnt * sz);
    const out: number[] = new Array(cnt);
    for (let i = 0; i < cnt; i++) {
      const o = i * sz;
      switch (e.type) {
        case 1: out[i] = b[o]; break;
        case 3: out[i] = this.u16(b, o); break;
        case 4: case 13: out[i] = this.u32(b, o); break;
        default: out[i] = big2num(this.u64(b, o), "tag value");
      }
    }
    return out;
  }

  async scalar(ifd: IFD, tag: number): Promise<number | undefined> {
    const e = ifd.entries.get(tag);
    if (!e) return undefined;
    return (await this.uints(e, 1))[0];
  }

  async format(ifd: IFD): Promise<Fmt> {
    const bpsE = ifd.entries.get(258);
    if (!bpsE) reject(`IFD at ${ifd.offset}: no BitsPerSample`);
    const bpsV = await this.uints(bpsE);
    if (bpsV.length === 0) reject("BitsPerSample has no values");
    if (!bpsV.every((v) => v === bpsV[0])) reject("BitsPerSample values differ");
    if (bpsV[0] < 1) reject("BitsPerSample is 0");
    const spp = (await this.scalar(ifd, 277)) ?? 1;
    if (spp < 1) reject("SamplesPerPixel is 0");
    let sf = 1;
    const sfE = ifd.entries.get(339);
    if (sfE) {
      const v = await this.uints(sfE);
      if (v.length === 0) reject("SampleFormat has no values");
      if (!v.every((x) => x === v[0])) reject("SampleFormat values differ");
      sf = v[0];
    }
    let planar = 1;
    if (spp > 1) {
      planar = (await this.scalar(ifd, 284)) ?? 1;
      if (planar !== 1 && planar !== 2) reject(`PlanarConfiguration ${planar}`);
    }
    const comp = (await this.scalar(ifd, 259)) ?? 1;
    const pred = (await this.scalar(ifd, 317)) ?? 1;
    return { bps: bpsV[0], spp, sf, planar, comp, pred };
  }

  async size(ifd: IFD): Promise<Size> {
    const w = await this.scalar(ifd, 256);
    const h = await this.scalar(ifd, 257);
    if (w === undefined || h === undefined) reject(`IFD at ${ifd.offset}: no ImageWidth/ImageLength`);
    if (w < 1 || h < 1) reject(`IFD at ${ifd.offset}: zero size`);
    const tiled = ifd.entries.has(322) && ifd.entries.has(324);
    let tw = 0, tl = 0;
    if (tiled) {
      if (!ifd.entries.has(323) || !ifd.entries.has(325)) reject(`IFD at ${ifd.offset}: incomplete tile tags`);
      tw = (await this.scalar(ifd, 322))!;
      tl = (await this.scalar(ifd, 323))!;
      if (tw < 1 || tl < 1) reject(`IFD at ${ifd.offset}: zero tile size`);
    }
    return { w, h, tiled, tw, tl };
  }
}

type Ome = {
  xml: Uint8Array;
  imageName?: string;
  pixels: Map<string, string> | null; // decoded attributes of the first Pixels start tag
  tiffData: { attrs: Map<string, string>; file?: string }[];
};

function parseIntAttr(v: string, what: string): number {
  const m = /^[ \t\r\n]*([0-9]+)[ \t\r\n]*$/.exec(v);
  if (!m) reject(`${what}: not an integer: ${JSON.stringify(v)}`);
  const d = m[1].replace(/^0+(?=.)/, "");
  if (d.length > 16 || Number(d) > MAX_SAFE) reject(`${what}: too large`);
  return Number(d);
}

function physSize(v: string | undefined): number | undefined {
  if (v === undefined) return undefined;
  if (!/^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$/.test(v)) return undefined;
  const x = Number(v);
  if (!Number.isFinite(x) || !(x > 0)) return undefined;
  return x;
}

function decodedAttrs(raw: Map<string, string>): Map<string, string> {
  const m = new Map<string, string>();
  for (const [k, v] of raw) m.set(k, decodeRefs(v));
  return m;
}

async function readOme(t: Tiff, ifd0: IFD): Promise<Ome | null> {
  const e = ifd0.entries.get(TEXT_TAG);
  if (!e || e.type !== 2) return null;
  const all = await t.src.read(e.pos, e.count);
  const nul = all.indexOf(0);
  const d = nul < 0 ? all : all.subarray(0, nul);
  let x: string;
  try {
    x = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(d);
  } catch {
    return null;
  }
  const { tags, skips } = scan(x);
  if (!tags.some((tg) => !tg.isEnd && tg.name === "OME")) return null;
  const ome: Ome = { xml: d, pixels: null, tiffData: [] };
  const img = tags.find((tg) => !tg.isEnd && tg.name === "Image");
  if (img && img.attrs.has("Name")) ome.imageName = decodeRefs(img.attrs.get("Name")!);
  const pi = tags.findIndex((tg) => !tg.isEnd && tg.name === "Pixels");
  if (pi < 0) return ome;
  ome.pixels = decodedAttrs(tags[pi].attrs);
  if (tags[pi].self) return ome;
  let pe = tags.findIndex((tg, i) => i > pi && tg.isEnd && tg.name === "Pixels");
  if (pe < 0) pe = tags.length;
  for (let i = pi + 1; i < pe; i++) {
    const td = tags[i];
    if (td.isEnd || td.name !== "TiffData") continue;
    const entry: { attrs: Map<string, string>; file?: string } = { attrs: decodedAttrs(td.attrs) };
    if (!td.self) {
      for (let k = i + 1; k < pe; k++) {
        const u = tags[k];
        if (u.name === "TiffData" || (u.isEnd && u.name === "Pixels")) break;
        if (!u.isEnd && u.name === "UUID") {
          if (u.attrs.has("FileName")) entry.file = decodeRefs(u.attrs.get("FileName")!);
          else if (!u.self) {
            const nextStart = k + 1 < tags.length ? tags[k + 1].start : x.length;
            entry.file = trimWs(decodeRefs(textBetween(x, u.end, nextStart, skips)));
          } else entry.file = ""; // self-closing UUID without FileName: no text (SPEC_NOTES)
          break;
        }
      }
    }
    ome.tiffData.push(entry);
  }
  return ome;
}

type Plane = { z: number; c: number; t: number; ifd: number };

export async function virtualizeTiff(src: Source, out: Output): Promise<void> {
  const t = new Tiff(src);
  const hb = await src.read(0, Math.min(16, src.size));
  t.le = hb[0] === 0x49;
  const magic = t.u16(hb, 2);
  let first: number;
  if (magic === 43) {
    t.big = true;
    if (hb.length < 16) reject("BigTIFF header truncated");
    if (t.u16(hb, 4) !== 8 || t.u16(hb, 6) !== 0) reject("BigTIFF offset size / reserved word");
    first = big2num(t.u64(hb, 8), "first IFD offset");
  } else {
    if (hb.length < 8) reject("TIFF header truncated");
    first = t.u32(hb, 4);
  }

  // IFDs: main chain, then SubIFDs of each main-chain IFD.
  const main: IFD[] = [];
  let off = first;
  while (off !== 0) {
    const { ifd, next } = await t.readIFD(off, true);
    main.push(ifd);
    off = next;
  }
  if (main.length === 0) reject("no IFD");
  for (const ifd of main) {
    for (const so of ifd.subOffsets) ifd.subs.push((await t.readIFD(so, false)).ifd);
  }

  const ifd0 = main[0];
  const fmt0 = await t.format(ifd0);
  const spp = fmt0.spp;
  const ome = await readOme(t, ifd0);

  // §3.3 planes
  let sizeZ = 1, sizeT = 1, sizeC = spp;
  let planes: Plane[];
  let units: { x?: string; y?: string; z?: string } = {};
  let PX = 1, PY = 1, PZ = 1;
  if (!ome) {
    planes = [{ z: 0, c: 0, t: 0, ifd: 0 }];
  } else {
    const px = ome.pixels ?? new Map<string, string>();
    const intA = (m: Map<string, string>, k: string, min: number): number | undefined => {
      const v = m.get(k);
      if (v === undefined) return undefined;
      const n = parseIntAttr(v, k);
      if (n < min) reject(`${k} is ${n}`);
      return n;
    };
    sizeZ = intA(px, "SizeZ", 1) ?? 1;
    sizeT = intA(px, "SizeT", 1) ?? 1;
    sizeC = intA(px, "SizeC", 1) ?? spp;
    const order = px.get("DimensionOrder") ?? "XYZCT";
    if (!/^XY(ZCT|ZTC|CZT|CTZ|TZC|TCZ)$/.test(order)) reject(`DimensionOrder ${JSON.stringify(order)}`);
    if (spp > 1) {
      if (sizeC === 1) sizeC = spp;
      else if (sizeC !== spp) reject(`SizeC ${sizeC} != SamplesPerPixel ${spp}`);
    }
    const cp = spp > 1 ? 1 : sizeC;
    const nPlanes = sizeZ * cp * sizeT;
    if (nPlanes > 100000) reject(`${nPlanes} planes`);
    for (const [k, axis] of [["PhysicalSizeX", "x"], ["PhysicalSizeY", "y"], ["PhysicalSizeZ", "z"]] as const) {
      const v = physSize(px.get(k));
      if (v === undefined) continue;
      if (axis === "x") PX = v;
      if (axis === "y") PY = v;
      if (axis === "z") PZ = v;
      const sym = px.get(k + "Unit") ?? "µm";
      units[axis] = Object.hasOwn(UNITS, sym) ? UNITS[sym] : undefined;
    }
    // multi-file check
    const files = new Set<string>();
    for (const td of ome.tiffData) if (td.file !== undefined) files.add(td.file);
    if (files.size > 1) reject("TiffData elements name more than one file");
    // coverage and stepping
    const dims = order.slice(2).split("");
    const sizeOf: Record<string, number> = { Z: sizeZ, C: cp, T: sizeT };
    const map: number[] = new Array(nPlanes).fill(-1);
    const tds = ome.tiffData.length > 0 ? ome.tiffData : [{ attrs: new Map<string, string>() }];
    for (const td of tds) {
      const fz = intA(td.attrs, "FirstZ", 0) ?? 0;
      const fc = intA(td.attrs, "FirstC", 0) ?? 0;
      const ft = intA(td.attrs, "FirstT", 0) ?? 0;
      const ifdAttr = intA(td.attrs, "IFD", 0);
      let pc = intA(td.attrs, "PlaneCount", 1);
      if (fz >= sizeZ || fc >= cp || ft >= sizeT) reject("TiffData First* out of range");
      if (pc === undefined) pc = tds.length === 1 && ifdAttr === undefined ? nPlanes : 1;
      const ifdStart = ifdAttr ?? 0;
      const coord: Record<string, number> = { Z: fz, C: fc, T: ft };
      const lin = coord[dims[0]] + sizeOf[dims[0]] * (coord[dims[1]] + sizeOf[dims[1]] * coord[dims[2]]);
      for (let i = 0; i < pc && lin + i < nPlanes; i++) map[lin + i] = ifdStart + i;
    }
    planes = [];
    for (let l = 0; l < nPlanes; l++) {
      if (map[l] < 0 || map[l] >= main.length) reject(`plane ${l} not mapped to an existing IFD`);
      const a = l % sizeOf[dims[0]];
      const b = Math.floor(l / sizeOf[dims[0]]) % sizeOf[dims[1]];
      const c = Math.floor(l / (sizeOf[dims[0]] * sizeOf[dims[1]]));
      const co: Record<string, number> = { [dims[0]]: a, [dims[1]]: b, [dims[2]]: c };
      planes.push({ z: co.Z, c: co.C, t: co.T, ifd: map[l] });
    }
  }

  // §3.4 levels: each level is a list of IFDs, one per plane
  const levels: IFD[][] = [planes.map((p) => main[p.ifd])];
  const s = ifd0.subOffsets.length;
  if (s > 0) {
    for (const p of planes) if (main[p.ifd].subs.length < s) reject("plane IFD has fewer SubIFDs than IFD 0");
    for (let k = 1; k <= s; k++) levels.push(planes.map((p) => main[p.ifd].subs[k - 1]));
  }
  const levelInfo: { size: Size }[] = [];
  const checkLevel = async (ifds: IFD[]) => {
    let sz0: Size | null = null;
    for (const ifd of ifds) {
      const f = await t.format(ifd);
      const sz = await t.size(ifd);
      if (!sz.tiled) reject(`IFD at ${ifd.offset} is not tiled`);
      if (!fmtEq(f, fmt0)) reject(`IFD at ${ifd.offset} has a different format from IFD 0`);
      if (sz0 && (sz.w !== sz0.w || sz.h !== sz0.h || sz.tw !== sz0.tw || sz.tl !== sz0.tl)) {
        reject("planes of a level differ in size");
      }
      sz0 = sz0 ?? sz;
    }
    return sz0!;
  };
  for (const lv of levels) levelInfo.push({ size: await checkLevel(lv) });
  if (s === 0 && !ome) {
    for (let i = 1; i < main.length; i++) {
      const ifd = main[i];
      const tiled = ifd.entries.has(322) && ifd.entries.has(324);
      if (!tiled || !ifd.entries.has(258)) continue;
      const f = await t.format(ifd);
      const sz = await t.size(ifd);
      const last = levelInfo[levelInfo.length - 1].size;
      if (fmtEq(f, fmt0) && sz.w < last.w && sz.h < last.h) {
        levels.push([ifd]);
        levelInfo.push({ size: sz });
      }
    }
  }

  // §3.5 data type and codecs
  const kind = ({ 1: "uint", 2: "int", 3: "float" } as Record<number, string>)[fmt0.sf];
  if (!kind) reject(`SampleFormat ${fmt0.sf}`);
  if (![8, 16, 32, 64].includes(fmt0.bps) || (kind === "float" && fmt0.bps < 32)) {
    reject(`BitsPerSample ${fmt0.bps} for SampleFormat ${fmt0.sf}`);
  }
  const dataType = kind + fmt0.bps;
  const interleaved = spp > 1 && fmt0.planar === 1;
  const comp = fmt0.comp, pred = fmt0.pred;
  const codecs: unknown[] = [];
  const isJ2k = [33003, 33004, 33005, 34712].includes(comp);
  if (!isJ2k) {
    if (pred !== 1) reject(`Predictor ${pred}`);
    if (![1, 8, 32946, 50000].includes(comp)) reject(`Compression ${comp}`);
  }

  // §3.6 output
  const channels = sizeC;
  const dims: string[] = [];
  if (sizeT > 1) dims.push("t");
  if (channels > 1) dims.push("c");
  if (sizeZ > 1) dims.push("z");
  dims.push("y", "x");
  if (interleaved) codecs.push(transposeCodec(dims));
  if (isJ2k) codecs.push({ name: "imagecodecs_jpeg2k" });
  else {
    codecs.push(bytesCodec(fmt0.bps / 8, t.le));
    if (comp === 8 || comp === 32946) codecs.push({ name: "zlib", configuration: { level: 1 } });
    if (comp === 50000) codecs.push({ name: "zstd", configuration: { level: 0, checksum: false } });
  }

  const axes = dims.map((d) =>
    axisObj(d, ome && (d === "x" || d === "y" || d === "z") ? units[d as "x" | "y" | "z"] : undefined),
  );
  const W0 = levelInfo[0].size.w, H0 = levelInfo[0].size.h;
  const datasets = levelInfo.map((li, L) => {
    const scale = dims.map((d) => {
      if (d === "y") return fin(PY * (H0 / li.size.h), "y scale");
      if (d === "x") return fin(PX * (W0 / li.size.w), "x scale");
      if (d === "z") return PZ;
      return 1;
    });
    return { path: String(L), coordinateTransformations: [{ type: "scale", scale }] };
  });
  const ms: Record<string, unknown> = {};
  if (ome && ome.imageName !== undefined && ome.imageName !== "") ms.name = ome.imageName;
  ms.axes = axes;
  ms.datasets = datasets;
  out.json("zarr.json", groupDoc({ ome: { version: "0.5", multiscales: [ms] } }));

  const nsp = spp > 1 && fmt0.planar === 2 ? spp : 1;
  for (let L = 0; L < levels.length; L++) {
    const sz = levelInfo[L].size;
    const shape: number[] = [];
    const chunk: number[] = [];
    for (const d of dims) {
      if (d === "t") { shape.push(sizeT); chunk.push(1); }
      if (d === "c") { shape.push(channels); chunk.push(interleaved ? spp : 1); }
      if (d === "z") { shape.push(sizeZ); chunk.push(1); }
    }
    shape.push(sz.h, sz.w);
    chunk.push(sz.tl, sz.tw);
    out.json(`${L}/zarr.json`, arrayDoc(shape, dataType, chunk, codecs, dims));
    const across = Math.ceil(sz.w / sz.tw);
    const T = Math.ceil(sz.h / sz.tl) * across;
    for (let pi = 0; pi < planes.length; pi++) {
      const p = planes[pi];
      const ifd = levels[L][pi];
      const offs = await t.uints(ifd.entries.get(324)!);
      const cnts = await t.uints(ifd.entries.get(325)!);
      if (offs.length !== T * nsp || cnts.length !== T * nsp) {
        reject(`IFD at ${ifd.offset}: ${offs.length}/${cnts.length} tiles, expected ${T * nsp}`);
      }
      for (let sIdx = 0; sIdx < nsp; sIdx++) {
        for (let j = 0; j < T; j++) {
          const k = sIdx * T + j;
          const n = cnts[k];
          if (n === 0) continue;
          const co: number[] = [];
          if (dims.includes("t")) co.push(p.t);
          if (dims.includes("c")) co.push(spp === 1 ? p.c : nsp > 1 ? sIdx : 0);
          if (dims.includes("z")) co.push(p.z);
          co.push(Math.floor(j / across), j % across);
          out.ref(`${L}/c/${co.join("/")}`, [[offs[k], n]]);
        }
      }
    }
  }
  if (ome) out.bytes("OME/METADATA.ome.xml", ome.xml);
}
