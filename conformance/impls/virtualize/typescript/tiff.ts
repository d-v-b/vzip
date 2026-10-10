// TIFF profile, spec/virtualize.md §3.

import {
  AXIS_TYPE,
  MAX_SAFE_BIG,
  Output,
  Source,
  arrayJson,
  bytesCodec,
  finite,
  reject,
  safe,
  transposeCodec,
} from "./lib.ts";
import { scan, decodeRefs, type Tag, type Token } from "./xml.ts";

const TYPE_SIZE: Record<number, number> = {
  1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8,
};
const UNSIGNED = new Set([1, 3, 4, 13, 16, 18]);

const SCALAR = new Set([256, 257, 259, 277, 284, 317, 322, 323]);
const ARRAY = new Set([258, 324, 325, 330, 339]);
const DESCRIPTION = 270;

interface Field {
  type: number;
  count: number;
  valueOffset: number;
  values: number[]; // for integer-typed table tags (not ImageDescription)
}

interface IFD {
  offset: number;
  tags: Map<number, Field>;
  next: number;
}

interface Format {
  bps: number;
  spp: number;
  sf: number;
  planar: number;
  compression: number;
  predictor: number;
}

const MAX_IFDS = 100000;

class Reader {
  src: Source;
  le: boolean;
  big: boolean;
  seen: Set<number> = new Set();
  constructor(src: Source, le: boolean, big: boolean) {
    this.src = src;
    this.le = le;
    this.big = big;
  }

  u16(b: Uint8Array, o: number): number {
    return new DataView(b.buffer, b.byteOffset, b.byteLength).getUint16(o, this.le);
  }
  u32(b: Uint8Array, o: number): number {
    return new DataView(b.buffer, b.byteOffset, b.byteLength).getUint32(o, this.le);
  }
  u64(b: Uint8Array, o: number): bigint {
    return new DataView(b.buffer, b.byteOffset, b.byteLength).getBigUint64(o, this.le);
  }

  async readIFD(offset: number): Promise<IFD> {
    if (offset < (this.big ? 16 : 8)) reject(`IFD offset ${offset} is too small`);
    if (this.seen.has(offset)) reject(`IFD offset ${offset} read twice`);
    if (this.seen.size >= MAX_IFDS) reject("more than 100000 IFDs");
    this.seen.add(offset);
    const big = this.big;
    const head = await this.src.read(offset, big ? 8 : 2);
    const count = big ? safe(this.u64(head, 0), "IFD entry count") : this.u16(head, 0);
    const esize = big ? 20 : 12;
    const body = await this.src.read(offset + head.length, count * esize + (big ? 8 : 4));
    const tags = new Map<number, Field>();
    for (let e = 0; e < count; e++) {
      const p = e * esize;
      const tag = this.u16(body, p);
      if (tag !== DESCRIPTION && !SCALAR.has(tag) && !ARRAY.has(tag)) continue;
      if (tags.has(tag)) continue;
      const type = this.u16(body, p + 2);
      const cnt = big ? safe(this.u64(body, p + 4), "tag count") : this.u32(body, p + 4);
      if (tag === DESCRIPTION) {
        if (TYPE_SIZE[type] === undefined) reject(`ImageDescription has field type ${type}`);
      } else if (!UNSIGNED.has(type)) reject(`tag ${tag} has field type ${type}`);
      const size = TYPE_SIZE[type];
      const len = cnt * size;
      if (!Number.isSafeInteger(len)) reject(`tag ${tag} value too long`);
      const inlineCap = big ? 8 : 4;
      const vpos = p + (big ? 12 : 8);
      let valueOffset: number;
      if (len <= inlineCap) valueOffset = offset + head.length + vpos;
      else valueOffset = big ? safe(this.u64(body, vpos), "tag value offset") : this.u32(body, vpos);
      if (valueOffset + len > this.src.size) reject(`tag ${tag} value lies outside the file`);
      const values: number[] = [];
      const needValues = tag !== DESCRIPTION || type === 16 || type === 17 || type === 18;
      if (needValues && len > 0) {
        const vb = await this.src.read(valueOffset, len);
        const dv = new DataView(vb.buffer, vb.byteOffset, vb.byteLength);
        for (let k = 0; k < cnt; k++) {
          let v: number;
          switch (type) {
            case 1:
              v = vb[k];
              break;
            case 3:
              v = dv.getUint16(k * 2, this.le);
              break;
            case 4:
            case 13:
              v = dv.getUint32(k * 4, this.le);
              break;
            case 16:
            case 18:
              v = safe(dv.getBigUint64(k * 8, this.le), `tag ${tag} value`);
              break;
            case 17: {
              const s = dv.getBigInt64(k * 8, this.le);
              if (s > MAX_SAFE_BIG || s < -MAX_SAFE_BIG) reject(`tag ${tag} value out of range`);
              v = Number(s);
              break;
            }
            default:
              throw new Error("unreachable");
          }
          values.push(v);
        }
      }
      if (SCALAR.has(tag) && cnt < 1) reject(`scalar tag ${tag} has no values`);
      tags.set(tag, { type, count: cnt, valueOffset, values });
    }
    const np = count * esize;
    const next = big ? safe(this.u64(body, np), "next IFD offset") : this.u32(body, np);
    return { offset, tags, next };
  }
}

function scalar(ifd: IFD, tag: number, dflt?: number): number | undefined {
  const f = ifd.tags.get(tag);
  if (f === undefined) return dflt;
  return f.values[0];
}

function computeFormat(ifd: IFD, what: string): Format {
  const bpsF = ifd.tags.get(258);
  if (bpsF === undefined || bpsF.count < 1) reject(`${what}: no BitsPerSample`);
  const bps = bpsF.values[0];
  if (!bpsF.values.every((v) => v === bps) || bps < 1) reject(`${what}: bad BitsPerSample`);
  const spp = scalar(ifd, 277, 1)!;
  if (spp < 1) reject(`${what}: SamplesPerPixel is 0`);
  const sfF = ifd.tags.get(339);
  let sf = 1;
  if (sfF !== undefined) {
    if (sfF.count < 1) reject(`${what}: SampleFormat has no values`);
    sf = sfF.values[0];
    if (!sfF.values.every((v) => v === sf)) reject(`${what}: SampleFormat values differ`);
  }
  let planar = scalar(ifd, 284, 1)!;
  if (spp === 1) planar = 1;
  else if (planar !== 1 && planar !== 2) reject(`${what}: PlanarConfiguration ${planar}`);
  return { bps, spp, sf, planar, compression: scalar(ifd, 259, 1)!, predictor: scalar(ifd, 317, 1)! };
}

function sameFormat(a: Format, b: Format): boolean {
  return (
    a.bps === b.bps &&
    a.spp === b.spp &&
    a.sf === b.sf &&
    a.planar === b.planar &&
    a.compression === b.compression &&
    a.predictor === b.predictor
  );
}

function isTiled(ifd: IFD): boolean {
  return ifd.tags.has(322) && ifd.tags.has(324);
}

interface Geom {
  w: number;
  h: number;
  tw: number;
  th: number;
}

/** Size checks of §3.1 for an IFD that is a plane, a level or a candidate. */
function checkSize(ifd: IFD, what: string): { w: number; h: number } {
  const w = scalar(ifd, 256);
  const h = scalar(ifd, 257);
  if (w === undefined || h === undefined) reject(`${what}: no ImageWidth/ImageLength`);
  if (w < 1 || h < 1) reject(`${what}: zero size`);
  if (isTiled(ifd)) {
    const tw = scalar(ifd, 322);
    const th = scalar(ifd, 323);
    if (tw === undefined || th === undefined || !ifd.tags.has(324) || !ifd.tags.has(325))
      reject(`${what}: incomplete tile tags`);
    if (tw < 1 || th < 1) reject(`${what}: zero tile size`);
  }
  return { w, h };
}

function geom(ifd: IFD, what: string): Geom {
  const { w, h } = checkSize(ifd, what);
  if (!isTiled(ifd)) reject(`${what}: not tiled`);
  return { w, h, tw: scalar(ifd, 322)!, th: scalar(ifd, 323)! };
}

const UNITS: Record<string, string> = {
  "µm": "micrometer",
  "μm": "micrometer",
  um: "micrometer",
  nm: "nanometer",
  mm: "millimeter",
  cm: "centimeter",
  m: "meter",
  "Å": "angstrom",
  "Å": "angstrom",
  pm: "picometer",
  in: "inch",
  ft: "foot",
  s: "second",
  ms: "millisecond",
  min: "minute",
  h: "hour",
};
export { UNITS };

function intAttr(v: string | undefined, name: string, min: number): number | undefined {
  if (v === undefined) return undefined;
  const m = /^[ \t\r\n]*([0-9]+)[ \t\r\n]*$/.exec(v);
  if (!m) reject(`${name}="${v}" is not an integer`);
  const b = BigInt(m[1]);
  if (b > MAX_SAFE_BIG) reject(`${name} too large`);
  const n = Number(b);
  if (n < min) reject(`${name} is less than ${min}`);
  return n;
}

function physAttr(v: string | undefined): number | undefined {
  if (v === undefined) return undefined;
  if (!/^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$/.test(v)) return undefined;
  const x = Number(v);
  if (!Number.isFinite(x) || !(x > 0)) return undefined;
  return x;
}

function trimWs(s: string): string {
  return s.replace(/^[ \t\r\n]+/, "").replace(/[ \t\r\n]+$/, "");
}

interface OmeInfo {
  imageName?: string;
  pixels?: Map<string, string>;
  tiffData: { attrs: Map<string, string>; uuid?: { fileName?: string; text: string } }[];
}

function parseOme(x: string, tokens: Token[]): OmeInfo {
  const tags: { t: Tag; idx: number }[] = [];
  tokens.forEach((t, idx) => {
    if (t.kind === "tag") tags.push({ t, idx });
  });
  const info: OmeInfo = { tiffData: [] };
  const img = tags.find((e) => !e.t.isEnd && e.t.name === "Image");
  if (img) info.imageName = img.t.attrs.get("Name");
  const pi = tags.findIndex((e) => !e.t.isEnd && e.t.name === "Pixels");
  if (pi < 0) return info;
  const pix = tags[pi].t;
  info.pixels = pix.attrs;
  if (pix.selfClosing) return info;
  let pend = tags.length;
  for (let k = pi + 1; k < tags.length; k++) {
    if (tags[k].t.isEnd && tags[k].t.name === "Pixels") {
      pend = k;
      break;
    }
  }
  for (let k = pi + 1; k < pend; k++) {
    const td = tags[k].t;
    if (td.isEnd || td.name !== "TiffData") continue;
    const entry: OmeInfo["tiffData"][number] = { attrs: td.attrs };
    if (!td.selfClosing) {
      for (let q = k + 1; q < tags.length; q++) {
        const u = tags[q].t;
        if (u.name === "TiffData") break; // start or end tag
        if (u.isEnd && u.name === "Pixels") break;
        if (!u.isEnd && u.name === "UUID") {
          let text = "";
          if (!u.selfClosing) {
            // characters from the end of the UUID tag to the start of the next tag, skipped sections removed
            let pos = u.end;
            for (let ti = tags[q].idx + 1; ti < tokens.length; ti++) {
              const tok = tokens[ti];
              text += x.slice(pos, tok.start);
              if (tok.kind === "tag") {
                pos = -1;
                break;
              }
              pos = tok.end;
            }
            if (pos >= 0) text += x.slice(pos);
            text = trimWs(decodeRefs(text));
          }
          entry.uuid = { fileName: u.attrs.get("FileName"), text };
          break;
        }
      }
    }
    info.tiffData.push(entry);
  }
  return info;
}

export async function virtualizeTiff(src: Source, out: Output): Promise<void> {
  const hdr = await src.read(0, 8);
  const le = hdr[0] === 0x49;
  const dv0 = new DataView(hdr.buffer, hdr.byteOffset, 8);
  const magic = dv0.getUint16(2, le);
  const big = magic === 43;
  const rd = new Reader(src, le, big);
  let first: number;
  if (big) {
    const h16 = await src.read(0, 16);
    if (rd.u16(h16, 4) !== 8 || rd.u16(h16, 6) !== 0) reject("BigTIFF offset size / reserved word");
    first = safe(rd.u64(h16, 8), "first IFD offset");
  } else first = rd.u32(hdr, 4);

  // §3.1 IFDs
  const main: IFD[] = [];
  let off = first;
  if (off === 0) reject("no IFDs");
  while (off !== 0) {
    const ifd = await rd.readIFD(off);
    main.push(ifd);
    off = ifd.next;
  }
  const subs: IFD[][] = [];
  for (const ifd of main) {
    const list: IFD[] = [];
    const f = ifd.tags.get(330);
    if (f !== undefined) for (const o of f.values) list.push(await rd.readIFD(o));
    subs.push(list);
  }

  const ifd0 = main[0];
  const fmt0 = computeFormat(ifd0, "IFD 0");
  const spp = fmt0.spp;

  // §3.2 OME-XML
  let D: Uint8Array | undefined;
  let ome: OmeInfo | undefined;
  const desc = ifd0.tags.get(DESCRIPTION);
  if (desc !== undefined && desc.type === 2) {
    const all = await src.read(desc.valueOffset, desc.count);
    const nul = all.indexOf(0);
    const d = nul < 0 ? all : all.subarray(0, nul);
    let text: string | undefined;
    try {
      text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(d);
    } catch {
      text = undefined;
    }
    if (text !== undefined) {
      const tokens = scan(text);
      if (tokens.some((t) => t.kind === "tag" && !t.isEnd && t.name === "OME")) {
        D = d;
        ome = parseOme(text, tokens);
      }
    }
  }

  // §3.3 Planes
  let sizeZ = 1;
  let sizeT = 1;
  let sizeC = spp;
  let Cp = 1;
  // plane index = z + Z*(c + Cp*t) -> main-chain IFD index
  let planeIfd: number[];
  let px: number | undefined, py: number | undefined, pz: number | undefined;
  let ux: string | undefined, uy: string | undefined, uz: string | undefined;
  let name: string | undefined;
  if (ome === undefined) {
    planeIfd = [0];
  } else {
    const pa = ome.pixels ?? new Map<string, string>();
    sizeZ = intAttr(pa.get("SizeZ"), "SizeZ", 1) ?? 1;
    sizeT = intAttr(pa.get("SizeT"), "SizeT", 1) ?? 1;
    sizeC = intAttr(pa.get("SizeC"), "SizeC", 1) ?? spp;
    const dimOrder = pa.get("DimensionOrder") ?? "XYZCT";
    if (!/^XY(ZCT|ZTC|CZT|CTZ|TZC|TCZ)$/.test(dimOrder)) reject(`DimensionOrder ${dimOrder}`);
    px = physAttr(pa.get("PhysicalSizeX"));
    py = physAttr(pa.get("PhysicalSizeY"));
    pz = physAttr(pa.get("PhysicalSizeZ"));
    const unit = (u: string | undefined) => UNITS[u ?? "µm"];
    ux = px !== undefined ? unit(pa.get("PhysicalSizeXUnit")) : undefined;
    uy = py !== undefined ? unit(pa.get("PhysicalSizeYUnit")) : undefined;
    uz = pz !== undefined ? unit(pa.get("PhysicalSizeZUnit")) : undefined;
    if (ome.imageName !== undefined && ome.imageName !== "") name = ome.imageName;

    if (spp > 1) {
      if (sizeC === 1) sizeC = spp;
      else if (sizeC !== spp) reject(`SizeC ${sizeC} differs from SamplesPerPixel ${spp}`);
      Cp = 1;
    } else Cp = sizeC;
    const nPlanes = sizeZ * Cp * sizeT;
    if (nPlanes > 100000) reject(`too many planes (${nPlanes})`);

    // Parse every TiffData's attributes first (all are checked).
    const tds = ome.tiffData.length > 0 ? ome.tiffData : [{ attrs: new Map<string, string>() } as OmeInfo["tiffData"][number]];
    const files = new Set<string>();
    for (const td of ome.tiffData) {
      if (td.uuid) files.add(td.uuid.fileName !== undefined ? td.uuid.fileName : td.uuid.text);
    }
    if (files.size > 1) reject("TiffData elements name more than one file");
    const sizes: Record<string, number> = { Z: sizeZ, C: Cp, T: sizeT };
    const order = dimOrder.slice(2).split(""); // fastest first
    planeIfd = new Array(nPlanes).fill(-1);
    for (const td of tds) {
      const a = td.attrs;
      const fz = intAttr(a.get("FirstZ"), "FirstZ", 0) ?? 0;
      const fc = intAttr(a.get("FirstC"), "FirstC", 0) ?? 0;
      const ft = intAttr(a.get("FirstT"), "FirstT", 0) ?? 0;
      const ifdAttr = intAttr(a.get("IFD"), "IFD", 0);
      const pcAttr = intAttr(a.get("PlaneCount"), "PlaneCount", 1);
      if (fz >= sizeZ || fc >= Cp || ft >= sizeT) reject("TiffData First* out of range");
      const ifdStart = ifdAttr ?? 0;
      const pc = pcAttr ?? (tds.length === 1 && ifdAttr === undefined ? nPlanes : 1);
      const pos: Record<string, number> = { Z: fz, C: fc, T: ft };
      for (let k = 0; k < pc; k++) {
        planeIfd[pos.Z + sizeZ * (pos.C + Cp * pos.T)] = ifdStart + k;
        // step
        let carry = true;
        for (const ax of order) {
          if (!carry) break;
          pos[ax]++;
          if (pos[ax] >= sizes[ax]) pos[ax] = 0;
          else carry = false;
        }
        if (carry) break; // past the last position
      }
    }
    for (const v of planeIfd) if (v < 0 || v >= main.length) reject("a plane is not mapped to an existing IFD");
  }

  // §3.4 Levels
  const levels: IFD[][] = [planeIfd.map((i) => main[i])];
  const s = ifd0.tags.get(330)?.count ?? 0;
  if (s > 0) {
    for (let k = 1; k <= s; k++) {
      levels.push(
        planeIfd.map((i) => {
          const l = subs[i];
          if (l.length < s) reject(`plane IFD ${i} has fewer than ${s} SubIFDs`);
          return l[k - 1];
        }),
      );
    }
  } else if (ome === undefined) {
    let last = geom(main[0], "level 0");
    computeFormat(main[0], "level 0");
    for (let i = 1; i < main.length; i++) {
      const ifd = main[i];
      if (!isTiled(ifd) || !ifd.tags.has(258)) continue;
      const f = computeFormat(ifd, `candidate IFD ${i}`);
      const sz = checkSize(ifd, `candidate IFD ${i}`);
      if (sameFormat(f, fmt0) && sz.w < last.w && sz.h < last.h) {
        levels.push([ifd]);
        last = geom(ifd, `level`);
      }
    }
  }

  // Level checks
  const geoms: Geom[] = [];
  levels.forEach((lv, L) => {
    let g0: Geom | undefined;
    for (const ifd of lv) {
      const f = computeFormat(ifd, `level ${L}`);
      const g = geom(ifd, `level ${L}`);
      if (!sameFormat(f, fmt0)) reject(`level ${L}: format differs from IFD 0`);
      if (g0 === undefined) g0 = g;
      else if (g.w !== g0.w || g.h !== g0.h || g.tw !== g0.tw || g.th !== g0.th)
        reject(`level ${L}: planes differ in size`);
    }
    geoms.push(g0!);
  });

  // §3.5 Data type and codecs
  const prefix = { 1: "uint", 2: "int", 3: "float" }[fmt0.sf as 1 | 2 | 3];
  if (prefix === undefined) reject(`SampleFormat ${fmt0.sf}`);
  if (![8, 16, 32, 64].includes(fmt0.bps) || (prefix === "float" && fmt0.bps < 32))
    reject(`BitsPerSample ${fmt0.bps} for ${prefix}`);
  const dataType = prefix + fmt0.bps;
  const itemSize = fmt0.bps / 8;
  const comp = fmt0.compression;
  const pred = fmt0.predictor;
  const interleaved = spp > 1 && fmt0.planar === 1;
  const codecsTail: unknown[] = [];
  if (comp === 1 && pred === 1) codecsTail.push(bytesCodec(itemSize, le ? "little" : "big"));
  else if ((comp === 8 || comp === 32946) && pred === 1)
    codecsTail.push(bytesCodec(itemSize, le ? "little" : "big"), { name: "zlib", configuration: { level: 1 } });
  else if (comp === 50000 && pred === 1)
    codecsTail.push(bytesCodec(itemSize, le ? "little" : "big"), { name: "zstd", configuration: { level: 0, checksum: false } });
  else if ([33003, 33004, 33005, 34712].includes(comp)) codecsTail.push({ name: "imagecodecs_jpeg2k" });
  else reject(`Compression ${comp} with Predictor ${pred}`);

  // §3.6 Output
  const channels = ome !== undefined ? sizeC : spp;
  const dims: string[] = [];
  if (sizeT > 1) dims.push("t");
  if (channels > 1) dims.push("c");
  if (sizeZ > 1) dims.push("z");
  dims.push("y", "x");
  const codecs = interleaved ? [transposeCodec(dims), ...codecsTail] : codecsTail;

  const axes = dims.map((d) => {
    const a: Record<string, string> = { name: d, type: AXIS_TYPE[d] };
    const u = d === "x" ? ux : d === "y" ? uy : d === "z" ? uz : undefined;
    if (u !== undefined) a.unit = u;
    return a;
  });
  const W0 = geoms[0].w;
  const H0 = geoms[0].h;
  const datasets = geoms.map((g, L) => {
    const scale = dims.map((d) => {
      if (d === "y") return finite((py ?? 1) * (H0 / g.h), "scale");
      if (d === "x") return finite((px ?? 1) * (W0 / g.w), "scale");
      if (d === "z") return pz ?? 1;
      return 1;
    });
    return { path: String(L), coordinateTransformations: [{ type: "scale", scale }] };
  });
  const ms: Record<string, unknown> = {};
  if (name !== undefined) ms.name = name;
  ms.axes = axes;
  ms.datasets = datasets;
  out.json("zarr.json", { zarr_format: 3, node_type: "group", attributes: { ome: { version: "0.5", multiscales: [ms] } } });

  const samplePlanes = spp > 1 && fmt0.planar === 2 ? spp : 1;
  levels.forEach((lv, L) => {
    const g = geoms[L];
    const shape: number[] = [];
    const chunk: number[] = [];
    for (const d of dims) {
      if (d === "t") (shape.push(sizeT), chunk.push(1));
      if (d === "c") (shape.push(channels), chunk.push(interleaved ? spp : 1));
      if (d === "z") (shape.push(sizeZ), chunk.push(1));
    }
    shape.push(g.h, g.w);
    chunk.push(g.th, g.tw);
    out.json(`${L}/zarr.json`, arrayJson({ shape, dataType, chunkShape: chunk, codecs, dims }));
    const rows = Math.ceil(g.h / g.th);
    const cols = Math.ceil(g.w / g.tw);
    const T = rows * cols;
    for (let t = 0; t < sizeT; t++)
      for (let c = 0; c < Cp; c++)
        for (let z = 0; z < sizeZ; z++) {
          const ifd = lv[z + sizeZ * (c + Cp * t)];
          const offs = ifd.tags.get(324)!.values;
          const cnts = ifd.tags.get(325)!.values;
          if (offs.length !== T * samplePlanes || cnts.length !== T * samplePlanes)
            reject(`level ${L}: tile count ${offs.length}/${cnts.length}, expected ${T * samplePlanes}`);
          for (let sp = 0; sp < samplePlanes; sp++)
            for (let j = 0; j < T; j++) {
              const k = sp * T + j;
              const n = cnts[k];
              if (n <= 0) continue;
              const coords: number[] = [];
              for (const d of dims) {
                if (d === "t") coords.push(t);
                if (d === "c") coords.push(samplePlanes > 1 ? sp : interleaved ? 0 : c);
                if (d === "z") coords.push(z);
              }
              coords.push(Math.floor(j / cols), j % cols);
              out.ref(`${L}/c/${coords.join("/")}`, [[0, offs[k], n]]);
            }
        }
  });
  if (D !== undefined) out.bytes("OME/METADATA.ome.xml", D);
}
