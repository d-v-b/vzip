// TIFF profile (VIRTUALIZE.md §3).

import { Reader, Output, reject, dv, u64, unitOf, arrayJson, axisType } from "./util.ts";
import type { Axis } from "./util.ts";
import { parseXml, findFirst, findAll } from "./xml.ts";
import type { XmlElement } from "./xml.ts";

const TYPE_SIZE: Record<number, number> = {
  1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8,
};

type TagEntry = { type: number; count: number; data: Uint8Array | null; offset: number };

type Ifd = { offset: number; tags: Map<number, TagEntry>; subIfds: Ifd[] };

type Format = { bps: number; spp: number; sf: number; planar: number; comp: number; pred: number };

type Info = {
  width: number;
  length: number;
  tiled: boolean;
  tileW: number;
  tileL: number;
  format: Format;
};

class Tiff {
  r: Reader;
  le = true;
  big = false;
  main: Ifd[] = [];
  count = 0;
  infoCache = new Map<Ifd, Promise<Info>>();

  constructor(r: Reader) {
    this.r = r;
  }

  async header(): Promise<number> {
    const h = await this.r.read(0, Math.min(16, this.r.size));
    if (h.length < 8) reject("file too short for TIFF");
    if (h[0] === 0x49 && h[1] === 0x49) this.le = true;
    else if (h[0] === 0x4d && h[1] === 0x4d) this.le = false;
    else reject("not a TIFF");
    const d = dv(h);
    const magic = d.getUint16(2, this.le);
    if (magic === 42) {
      this.big = false;
      return d.getUint32(4, this.le);
    }
    if (magic === 43) {
      if (h.length < 16) reject("file too short for BigTIFF");
      if (d.getUint16(4, this.le) !== 8 || d.getUint16(6, this.le) !== 0) reject("unsupported BigTIFF offset size");
      this.big = true;
      return u64(d, 8, this.le);
    }
    return reject(`bad TIFF magic ${magic}`);
  }

  async readIfd(offset: number): Promise<{ ifd: Ifd; next: number }> {
    if (++this.count > 100000) reject("more than 100000 IFDs");
    const cs = this.big ? 8 : 2;
    const es = this.big ? 20 : 12;
    const os = this.big ? 8 : 4;
    const cb = dv(await this.r.read(offset, cs));
    const n = this.big ? u64(cb, 0, this.le) : cb.getUint16(0, this.le);
    const body = await this.r.read(offset + cs, n * es + os);
    const d = dv(body);
    const tags = new Map<number, TagEntry>();
    for (let i = 0; i < n; i++) {
      const p = i * es;
      const tag = d.getUint16(p, this.le);
      const type = d.getUint16(p + 2, this.le);
      const count = this.big ? u64(d, p + 4, this.le) : d.getUint32(p + 4, this.le);
      const vpos = p + (this.big ? 12 : 8);
      const size = TYPE_SIZE[type];
      let entry: TagEntry;
      if (size !== undefined && size * count <= os) {
        entry = { type, count, data: body.slice(vpos, vpos + size * count), offset: 0 };
      } else {
        entry = { type, count, data: null, offset: this.big ? u64(d, vpos, this.le) : d.getUint32(vpos, this.le) };
      }
      if (!tags.has(tag)) tags.set(tag, entry);
    }
    const next = this.big ? u64(d, n * es, this.le) : d.getUint32(n * es, this.le);
    return { ifd: { offset, tags, subIfds: [] }, next };
  }

  async bytes(ifd: Ifd, tag: number): Promise<Uint8Array | null> {
    const e = ifd.tags.get(tag);
    if (!e) return null;
    const size = TYPE_SIZE[e.type];
    if (size === undefined) reject(`tag ${tag} has unknown type ${e.type}`);
    if (e.data) return e.data;
    return this.r.read(e.offset, size * e.count);
  }

  async values(ifd: Ifd, tag: number): Promise<number[] | null> {
    const e = ifd.tags.get(tag);
    if (!e) return null;
    const b = await this.bytes(ifd, tag);
    const d = dv(b!);
    const out: number[] = new Array(e.count);
    const le = this.le;
    for (let i = 0; i < e.count; i++) {
      switch (e.type) {
        case 1: case 2: case 7: out[i] = d.getUint8(i); break;
        case 6: out[i] = d.getInt8(i); break;
        case 3: out[i] = d.getUint16(2 * i, le); break;
        case 8: out[i] = d.getInt16(2 * i, le); break;
        case 4: case 13: out[i] = d.getUint32(4 * i, le); break;
        case 9: out[i] = d.getInt32(4 * i, le); break;
        case 5: out[i] = d.getUint32(8 * i, le) / d.getUint32(8 * i + 4, le); break;
        case 10: out[i] = d.getInt32(8 * i, le) / d.getInt32(8 * i + 4, le); break;
        case 11: out[i] = d.getFloat32(4 * i, le); break;
        case 12: out[i] = d.getFloat64(8 * i, le); break;
        case 16: case 18: out[i] = u64(d, 8 * i, le); break;
        case 17: out[i] = Number(d.getBigInt64(8 * i, le)); break;
        default: reject(`tag ${tag} has unknown type ${e.type}`);
      }
    }
    return out;
  }

  async scalar(ifd: Ifd, tag: number, def: number | null): Promise<number> {
    const v = await this.values(ifd, tag);
    if (v === null || v.length === 0) {
      if (def === null) reject(`IFD at ${ifd.offset}: required tag ${tag} missing`);
      return def;
    }
    return v[0];
  }

  async readAll(): Promise<void> {
    let off = await this.header();
    const seen = new Set<number>();
    while (off !== 0) {
      if (seen.has(off)) reject("IFD cycle");
      seen.add(off);
      const { ifd, next } = await this.readIfd(off);
      this.main.push(ifd);
      off = next;
    }
    for (const ifd of this.main) {
      const subs = await this.values(ifd, 330);
      if (subs) {
        for (const s of subs) ifd.subIfds.push((await this.readIfd(s)).ifd);
      }
    }
  }

  info(ifd: Ifd): Promise<Info> {
    let p = this.infoCache.get(ifd);
    if (!p) {
      p = this.computeInfo(ifd);
      this.infoCache.set(ifd, p);
    }
    return p;
  }

  async computeInfo(ifd: Ifd): Promise<Info> {
    const width = await this.scalar(ifd, 256, null);
    const length = await this.scalar(ifd, 257, null);
    const bpsAll = await this.values(ifd, 258);
    if (!bpsAll || bpsAll.length === 0) reject(`IFD at ${ifd.offset}: BitsPerSample missing`);
    if (bpsAll.some((b) => b !== bpsAll[0])) reject(`IFD at ${ifd.offset}: BitsPerSample values differ`);
    const spp = await this.scalar(ifd, 277, 1);
    const format: Format = {
      bps: bpsAll[0],
      spp,
      sf: await this.scalar(ifd, 339, 1),
      planar: spp > 1 ? await this.scalar(ifd, 284, 1) : 1,
      comp: await this.scalar(ifd, 259, 1),
      pred: await this.scalar(ifd, 317, 1),
    };
    const tiled = ifd.tags.has(322) && ifd.tags.has(324);
    return {
      width,
      length,
      tiled,
      tileW: tiled ? await this.scalar(ifd, 322, null) : 0,
      tileL: tiled ? await this.scalar(ifd, 323, null) : 0,
      format,
    };
  }
}

function sameFormat(a: Format, b: Format): boolean {
  return a.bps === b.bps && a.spp === b.spp && a.sf === b.sf && a.planar === b.planar && a.comp === b.comp && a.pred === b.pred;
}

function parseIntAttr(el: XmlElement, name: string, def: number): number {
  const v = el.attrs.get(name);
  if (v === undefined) return def;
  const t = v.trim();
  if (!/^[+]?[0-9]+$/.test(t)) reject(`${el.name}/@${name} is not a non-negative integer: ${JSON.stringify(v)}`);
  return Number(t);
}

function parseFloatAttr(el: XmlElement, name: string): number | undefined {
  const v = el.attrs.get(name);
  if (v === undefined) return undefined;
  const t = v.trim();
  if (!/^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$/.test(t)) return undefined;
  return Number(t);
}

type Plane = { z: number; c: number; t: number; ifd: number };

export async function virtualizeTiff(r: Reader, url: string, out: Output): Promise<void> {
  const tf = new Tiff(r);
  await tf.readAll();
  if (tf.main.length === 0) reject("TIFF has no IFDs");
  const first = tf.main[0];
  const firstInfo = await tf.info(first);
  const spp = firstInfo.format.spp;

  // §3.2 OME-XML
  let xmlBytes: Uint8Array | null = null;
  let xmlText: string | null = null;
  const desc = await tf.bytes(first, 270);
  if (desc) {
    let end = desc.indexOf(0);
    if (end < 0) end = desc.length;
    const b = desc.subarray(0, end);
    const text = new TextDecoder("utf-8", { ignoreBOM: true }).decode(b);
    if (text.includes("<OME")) {
      xmlBytes = b;
      xmlText = text;
    }
  }

  let sizeZ = 1, sizeC = spp, sizeT = 1;
  let name: string | undefined;
  const phys: Record<string, number | undefined> = {};
  const physUnit: Record<string, string> = { X: "µm", Y: "µm", Z: "µm" };
  const planes: Plane[] = [];

  if (xmlText !== null) {
    const doc = parseXml(xmlText);
    const image = findFirst(doc, "Image");
    if (image && image.attrs.has("Name")) name = image.attrs.get("Name");
    const pixels = findFirst(doc, "Pixels");
    let tiffData: XmlElement[] = [];
    let order = "XYZCT";
    if (pixels) {
      sizeZ = parseIntAttr(pixels, "SizeZ", 1);
      sizeC = parseIntAttr(pixels, "SizeC", spp);
      sizeT = parseIntAttr(pixels, "SizeT", 1);
      const dimo = pixels.attrs.get("DimensionOrder");
      if (dimo !== undefined) order = dimo;
      for (const a of ["X", "Y", "Z"]) {
        phys[a] = parseFloatAttr(pixels, `PhysicalSize${a}`);
        const u = pixels.attrs.get(`PhysicalSize${a}Unit`);
        if (u !== undefined) physUnit[a] = u;
      }
      tiffData = findAll(pixels, "TiffData");
    }
    if (!/^XY(ZCT|ZTC|CZT|CTZ|TZC|TCZ)$/.test(order)) reject(`bad DimensionOrder ${order}`);
    if (spp > 1) {
      if (sizeC === 1) sizeC = spp;
      else if (sizeC !== spp) reject(`SizeC ${sizeC} differs from SamplesPerPixel ${spp}`);
    }
    if (sizeZ < 1 || sizeC < 1 || sizeT < 1) reject("a Size attribute is 0");
    const cp = spp > 1 ? 1 : sizeC;
    const total = sizeZ * sizeT * cp;
    // dims fastest first
    const sizes: Record<string, number> = { Z: sizeZ, C: cp, T: sizeT };
    const dims = order.slice(2).split("");
    const mapped: Array<number | undefined> = new Array(total);
    const tds = tiffData.length > 0 ? tiffData : [null];
    for (const td of tds) {
      const at = (k: string, def: number) => (td ? parseIntAttr(td, k, def) : def);
      if (td) {
        for (const ch of td.children) {
          if (ch.name === "UUID" && ch.attrs.has("FileName")) reject("TiffData with UUID FileName (multi-file dataset)");
        }
      }
      const pos: Record<string, number> = { Z: at("FirstZ", 0), C: at("FirstC", 0), T: at("FirstT", 0) };
      for (const d of dims) if (pos[d] >= sizes[d]) reject(`TiffData First${d} ${pos[d]} out of range`);
      const ifd0 = at("IFD", 0);
      const defCount = tds.length === 1 && !(td && td.attrs.has("IFD")) ? total : 1;
      const count = at("PlaneCount", defCount);
      let lin = 0, stride = 1;
      for (const d of dims) {
        lin += pos[d] * stride;
        stride *= sizes[d];
      }
      for (let i = 0; i < count && lin + i < total; i++) mapped[lin + i] = ifd0 + i;
    }
    for (let p = 0; p < total; p++) {
      const ifd = mapped[p];
      if (ifd === undefined) reject(`plane ${p} is not mapped to an IFD`);
      if (ifd >= tf.main.length) reject(`plane ${p} maps to IFD ${ifd}, which does not exist`);
      const pos: Record<string, number> = {};
      let rem = p;
      for (const d of dims) {
        pos[d] = rem % sizes[d];
        rem = Math.floor(rem / sizes[d]);
      }
      planes.push({ z: pos.Z, c: pos.C, t: pos.T, ifd });
    }
  } else {
    planes.push({ z: 0, c: 0, t: 0, ifd: 0 });
  }

  // §3.4 levels: levels[L][planeIndex] = Ifd
  const levels: Ifd[][] = [planes.map((p) => tf.main[p.ifd])];
  if (first.subIfds.length > 0 || first.tags.has(330)) {
    const nsub = first.subIfds.length;
    for (let k = 1; k <= nsub; k++) {
      levels.push(
        planes.map((p) => {
          const s = tf.main[p.ifd].subIfds[k - 1];
          if (!s) reject(`IFD ${p.ifd} has no SubIFD ${k}`);
          return s;
        }),
      );
    }
  } else if (xmlText === null) {
    let prev = firstInfo;
    for (let i = 1; i < tf.main.length; i++) {
      let inf: Info;
      try {
        inf = await tf.info(tf.main[i]);
      } catch (e) {
        continue; // an IFD missing required tags cannot be a level
      }
      if (inf.tiled && sameFormat(inf.format, firstInfo.format) && inf.width < prev.width && inf.length < prev.length) {
        levels.push([tf.main[i]]);
        prev = inf;
      }
    }
  }

  // validation and data type (§3.4, §3.5)
  const fmt = firstInfo.format;
  const levelInfo: Info[] = [];
  for (let L = 0; L < levels.length; L++) {
    let li: Info | null = null;
    for (const ifd of levels[L]) {
      const inf = await tf.info(ifd);
      if (!inf.tiled) reject(`level ${L} image at ${ifd.offset} is not tiled`);
      if (!sameFormat(inf.format, fmt)) reject(`level ${L} image at ${ifd.offset} has a different format`);
      if (li === null) li = inf;
      else if (inf.width !== li.width || inf.length !== li.length || inf.tileW !== li.tileW || inf.tileL !== li.tileL) {
        reject(`level ${L} planes differ in size or tiling`);
      }
    }
    levelInfo.push(li!);
  }

  const kind = fmt.sf === 1 ? "uint" : fmt.sf === 2 ? "int" : fmt.sf === 3 ? "float" : reject(`SampleFormat ${fmt.sf} unsupported`);
  const okBits = kind === "float" ? [32, 64] : [8, 16, 32, 64];
  if (!okBits.includes(fmt.bps)) reject(`BitsPerSample ${fmt.bps} unsupported for ${kind}`);
  const dataType = `${kind}${fmt.bps}`;
  let arrayToBytes: "bytes" | "jpeg2k" = "bytes";
  let compressor: "zlib" | "zstd" | null = null;
  if (fmt.comp === 1 && fmt.pred === 1) compressor = null;
  else if ((fmt.comp === 8 || fmt.comp === 32946) && fmt.pred === 1) compressor = "zlib";
  else if (fmt.comp === 50000 && fmt.pred === 1) compressor = "zstd";
  else if ([33003, 33004, 33005, 34712].includes(fmt.comp)) arrayToBytes = "jpeg2k";
  else reject(`Compression ${fmt.comp} with Predictor ${fmt.pred} unsupported`);
  const planarSep = spp > 1 && fmt.planar !== 1;
  const interleaved = spp > 1 && fmt.planar === 1;
  // PlanarConfiguration other than 1 and 2
  if (spp > 1 && fmt.planar !== 1 && fmt.planar !== 2) reject(`PlanarConfiguration ${fmt.planar} unsupported`);

  // §3.6 output
  const channels = spp > 1 ? spp : sizeC;
  const axes: string[] = [];
  if (sizeT > 1) axes.push("t");
  if (channels > 1) axes.push("c");
  if (sizeZ > 1) axes.push("z");
  axes.push("y", "x");

  const axisObjs: Axis[] = axes.map((a) => {
    const o: Axis = { name: a, type: axisType(a) };
    if (xmlText !== null && (a === "x" || a === "y" || a === "z")) {
      const A = a.toUpperCase();
      if (phys[A] !== undefined) {
        const u = unitOf(physUnit[A]);
        if (u !== undefined) o.unit = u;
      }
    }
    return o;
  });

  const H0 = levelInfo[0].length, W0 = levelInfo[0].width;
  const datasets: unknown[] = [];
  for (let L = 0; L < levels.length; L++) {
    const li = levelInfo[L];
    const scale = axes.map((a) => {
      if (a === "y") return (phys.Y ?? 1) * (H0 / li.length);
      if (a === "x") return (phys.X ?? 1) * (W0 / li.width);
      if (a === "z") return phys.Z ?? 1;
      return 1;
    });
    datasets.push({ path: String(L), coordinateTransformations: [{ type: "scale", scale }] });

    const shape: number[] = [];
    const chunk: number[] = [];
    for (const a of axes) {
      if (a === "t") { shape.push(sizeT); chunk.push(1); }
      else if (a === "c") { shape.push(channels); chunk.push(interleaved ? spp : 1); }
      else if (a === "z") { shape.push(sizeZ); chunk.push(1); }
    }
    shape.push(li.length, li.width);
    chunk.push(li.tileL, li.tileW);
    out.json(`${L}/zarr.json`, arrayJson({
      shape, dataType, chunkShape: chunk, axes, interleaved, arrayToBytes,
      endian: tf.le ? "little" : "big", itemSize: fmt.bps / 8, compressor,
    }));

    const across = Math.ceil(li.width / li.tileW);
    const down = Math.ceil(li.length / li.tileL);
    const T = across * down;
    const nSamplePlanes = planarSep ? spp : 1;
    for (let pi = 0; pi < planes.length; pi++) {
      const pl = planes[pi];
      const ifd = levels[L][pi];
      const offs = await tf.values(ifd, 324);
      const counts = await tf.values(ifd, 325);
      if (!offs || !counts) reject(`IFD at ${ifd.offset}: TileOffsets/TileByteCounts missing`);
      if (offs.length !== T * nSamplePlanes) reject(`IFD at ${ifd.offset}: ${offs.length} tiles, expected ${T * nSamplePlanes}`);
      if (counts.length !== offs.length) reject(`IFD at ${ifd.offset}: TileByteCounts length differs from TileOffsets`);
      for (let k = 0; k < offs.length; k++) {
        const n = counts[k];
        if (!(n > 0)) continue;
        const s = Math.floor(k / T);
        const j = k % T;
        const coords: number[] = [];
        for (const a of axes) {
          if (a === "t") coords.push(pl.t);
          else if (a === "c") coords.push(spp > 1 ? (planarSep ? s : 0) : pl.c);
          else if (a === "z") coords.push(pl.z);
        }
        coords.push(Math.floor(j / across), j % across);
        out.ref(`${L}/c/${coords.join("/")}`, [[0, offs[k], n]]);
      }
    }
  }

  const ms: Record<string, unknown> = {};
  if (name !== undefined) ms.name = name;
  ms.axes = axisObjs;
  ms.datasets = datasets;
  out.json("zarr.json", { zarr_format: 3, node_type: "group", attributes: { ome: { version: "0.5", multiscales: [ms] } } });
  if (xmlText !== null) {
    out.set("OME/METADATA.ome.xml", { base64: Buffer.from(new TextEncoder().encode(xmlText)).toString("base64") });
  }
}
