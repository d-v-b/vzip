// ND2 profile (VIRTUALIZE.md §4).

import { Reader, Output, reject, dv, u64, arrayJson, axisType } from "./util.ts";
import type { Axis } from "./util.ts";
import { decodeLV, lvGet, lvMembers, lvNum, LVLevel } from "./lv.ts";
import type { LVValue } from "./lv.ts";

const CHUNK_MAGIC = 0x0abeceda;
const SIG_NAME = "ND2 FILE SIGNATURE CHUNK NAME01!";
const MAP_SIG = "ND2 CHUNK MAP SIGNATURE 0000001!";
const FILEMAP_NAME = "ND2 FILEMAP SIGNATURE NAME 0001!";
const JP2_SIG = [0x00, 0x00, 0x00, 0x0c, 0x6a, 0x50, 0x20, 0x20, 0x0d, 0x0a, 0x87, 0x0a];

const ascii = (b: Uint8Array) => String.fromCharCode(...b);

type ChunkHeader = { offset: number; nameLen: number; dataLen: number; name: string };

async function readHeader(r: Reader, o: number): Promise<ChunkHeader> {
  const h = dv(await r.read(o, 16));
  if (h.getUint32(0, true) !== CHUNK_MAGIC) reject(`no chunk magic at ${o}`);
  const n = h.getUint32(4, true);
  const d = u64(h, 8, true);
  const nb = await r.read(o + 16, n);
  let e = nb.indexOf(0);
  if (e < 0) e = n;
  return { offset: o, nameLen: n, dataLen: d, name: ascii(nb.subarray(0, e)) };
}

export function isNd2(head: Uint8Array): boolean {
  if (head.length >= 4 && dv(head).getUint32(0, true) === CHUNK_MAGIC) return true;
  return head.length >= JP2_SIG.length && JP2_SIG.every((v, i) => head[i] === v);
}

type Loop = { kind: "time" | "position" | "z"; eType: number; depth: number; count: number; period?: number; step?: number };

function num(v: LVValue | undefined): number | undefined {
  return lvNum(v);
}

function flattenExperiment(root: LVValue | undefined): Loop[] {
  const loops: Loop[] = [];
  const visit = (node: LVValue, depth: number) => {
    if (!(node instanceof LVLevel)) reject("experiment node is not a level");
    const eType = num(node.get("eType"));
    if (eType === undefined || ![1, 2, 4, 6, 8].includes(eType)) reject(`experiment loop type ${eType} unsupported`);
    const pars = node.get("uLoopPars");
    const children = lvMembers(node.get("ppNextLevelEx")) ?? [];
    if (pars === undefined) return;
    let loop: Loop | null = null;
    let count = 0;
    if (eType === 1) {
      count = num(lvGet(pars, "uiCount")) ?? 0;
      loop = { kind: "time", eType, depth, count, period: num(lvGet(pars, "dPeriod")) };
    } else if (eType === 8) {
      const periods = lvMembers(lvGet(pars, "pPeriod")) ?? [];
      const valid = lvMembers(lvGet(pars, "pPeriodValid"));
      let period: number | undefined;
      let first = true;
      for (let i = 0; i < periods.length; i++) {
        const ok = valid === undefined ? true : (num(valid[i]) ?? 0) !== 0;
        if (!ok) continue;
        count += num(lvGet(periods[i], "uiCount")) ?? 0;
        if (first) {
          period = num(lvGet(periods[i], "dPeriod"));
          first = false;
        }
      }
      loop = { kind: "time", eType, depth, count, period };
    } else if (eType === 2) {
      const pts = lvMembers(lvGet(pars, "Points")) ?? [];
      const valid = lvMembers(node.get("pItemValid"));
      if (valid === undefined) count = pts.length;
      else for (let i = 0; i < pts.length; i++) if ((num(valid[i]) ?? 0) !== 0) count++;
      loop = { kind: "position", eType, depth, count };
    } else if (eType === 4) {
      count = num(lvGet(pars, "uiCount")) ?? 0;
      let step = Math.abs(num(lvGet(pars, "dZStep")) ?? 0);
      if (step === 0 && count > 1) {
        const hi = num(lvGet(pars, "dZHigh")) ?? 0;
        const lo = num(lvGet(pars, "dZLow")) ?? 0;
        step = Math.abs(hi - lo) / (count - 1);
      }
      loop = { kind: "z", eType, depth, count, step };
    } else {
      count = num(lvGet(pars, "uiCount")) ?? num(lvGet(pars, "pPlanes/uiCount")) ?? 0;
    }
    if (count === 0) return;
    if (eType === 6) {
      for (const ch of children) visit(ch, depth);
      return;
    }
    const last = loops[loops.length - 1];
    if (!last || last.depth < depth) loops.push(loop!);
    else if (last.depth === depth && last.eType === eType && last.count < count) loops[loops.length - 1] = loop!;
    for (const ch of children) visit(ch, depth + 1);
  };
  if (root !== undefined) visit(root, 0);
  const kinds = new Set<string>();
  for (const l of loops) {
    if (kinds.has(l.kind)) reject(`two ${l.kind} loops`);
    kinds.add(l.kind);
  }
  return loops;
}

function hex2(n: number): string {
  return n.toString(16).toUpperCase().padStart(2, "0");
}

export async function virtualizeNd2(r: Reader, url: string, out: Output): Promise<void> {
  // §4.1 signature
  const head = await r.read(0, Math.min(r.size, 16 + 32 + 64));
  if (head.length >= JP2_SIG.length && JP2_SIG.every((v, i) => head[i] === v)) reject("legacy (JPEG 2000) ND2 file");
  if (head.length < 112) reject("file too short for ND2");
  const hd = dv(head);
  if (hd.getUint32(0, true) !== CHUNK_MAGIC) reject("no ND2 chunk magic at 0");
  if (hd.getUint32(4, true) !== 32 || u64(hd, 8, true) !== 64) reject("bad signature chunk lengths");
  if (ascii(head.subarray(16, 48)) !== SIG_NAME) reject("bad signature chunk name");
  const ver = /^Ver([0-9]+)\.([0-9]+)/.exec(ascii(head.subarray(48, 112)));
  if (!ver) reject("signature data does not start with VerM.m");
  if (Number(ver[1]) < 3) reject(`ND2 version ${ver[1]}.${ver[2]} unsupported`);

  // chunk map
  if (r.size < 40) reject("file too short for chunk map");
  const tail = await r.read(r.size - 40, 40);
  if (ascii(tail.subarray(0, 32)) !== MAP_SIG) reject("no chunk map signature at end of file");
  const m = u64(dv(tail), 32, true);
  const mh = await readHeader(r, m);
  if (mh.name !== FILEMAP_NAME) reject(`chunk at ${m} is not the file map`);
  const mapData = await r.read(m + 16 + mh.nameLen, mh.dataLen);
  const md = dv(mapData);
  const chunkMap = new Map<string, number>();
  let p = 0;
  for (;;) {
    const e = mapData.indexOf(0x21, p);
    if (e < 0 || e + 17 > mapData.length) reject("chunk map not terminated");
    const name = ascii(mapData.subarray(p, e + 1));
    if (name === MAP_SIG) break;
    chunkMap.set(name, u64(md, e + 1, true));
    p = e + 17;
  }

  const chunkData = async (name: string): Promise<Uint8Array | null> => {
    const o = chunkMap.get(name);
    if (o === undefined) return null;
    const h = await readHeader(r, o);
    return r.read(o + 16 + h.nameLen, h.dataLen);
  };

  // §4.3 attributes
  const attrData = await chunkData("ImageAttributesLV!");
  if (!attrData) reject("no ImageAttributesLV! chunk");
  const attrs = lvGet(decodeLV(attrData), "SLxImageAttributes");
  if (!(attrs instanceof LVLevel)) reject("no SLxImageAttributes");
  const req = (k: string): number => {
    const v = num(attrs.get(k));
    if (v === undefined) reject(`SLxImageAttributes/${k} missing`);
    return v;
  };
  const width = req("uiWidth");
  const height = req("uiHeight");
  const widthBytes = req("uiWidthBytes");
  const comp = req("uiComp");
  const bpc = req("uiBpcInMemory");
  const bpcSig = req("uiBpcSignificant");
  const eCompression = num(attrs.get("eCompression")) ?? 2;
  const tileW = num(attrs.get("uiTileWidth")) ?? 0;
  const tileH = num(attrs.get("uiTileHeight")) ?? 0;
  const dataType = bpc === 8 ? "uint8" : bpc === 16 ? "uint16" : bpc === 32 ? "float32" : reject(`uiBpcInMemory ${bpc} unsupported`);
  if (eCompression === 1) reject("lossy compression");
  if (eCompression !== 0 && eCompression !== 2) reject(`eCompression ${eCompression} unsupported`);
  const compressed = eCompression === 0;
  if ((tileW > 0 && tileW !== width) || (tileH > 0 && tileH !== height)) reject("tiled ND2 unsupported");

  // experiment
  const metaData = await chunkData("ImageMetadataLV!");
  const expRoot = metaData ? lvGet(decodeLV(metaData), "SLxExperiment") : undefined;
  const loops = flattenExperiment(expRoot);

  // picture metadata
  const picData = await chunkData("ImageMetadataSeqLV|0!");
  const pic = picData ? lvGet(decodeLV(picData), "SLxPictureMetadata") : undefined;
  type PPlane = { desc: string; color: number; comps: number };
  const pplanes: PPlane[] = [];
  let planesOk = false;
  let calib: number | undefined;
  let aspect = 1;
  if (pic instanceof LVLevel) {
    const nPlanes = num(lvGet(pic, "sPicturePlanes/uiCount")) ?? 0;
    planesOk = true;
    for (let i = 0; i < nPlanes; i++) {
      const pl = lvGet(pic, `sPicturePlanes/sPlaneNew/a${i}`);
      if (!(pl instanceof LVLevel)) {
        planesOk = false;
        break;
      }
      const d = pl.get("sDescription");
      pplanes.push({
        desc: typeof d === "string" ? d : "",
        color: num(pl.get("uiColor")) ?? 0,
        comps: num(pl.get("uiCompCount")) ?? 1,
      });
    }
    if (pic.get("bCalibrated") === true || num(pic.get("bCalibrated")) === 1) {
      calib = num(pic.get("dCalibration"));
    }
    aspect = num(pic.get("dAspect")) ?? 1;
  }

  // §4.5 channels
  type Chan = { label: string; color: string };
  let chans: Chan[] = [];
  if (planesOk && pplanes.reduce((s, q) => s + q.comps, 0) === comp && pplanes.every((q) => q.comps === 1 || q.comps === 3)) {
    for (const q of pplanes) {
      if (q.comps === 1) {
        const c = q.color >>> 0;
        chans.push({ label: q.desc, color: hex2(c & 0xff) + hex2((c >>> 8) & 0xff) + hex2((c >>> 16) & 0xff) });
      } else {
        chans.push({ label: `${q.desc} R`, color: "FF0000" }, { label: `${q.desc} G`, color: "00FF00" }, { label: `${q.desc} B`, color: "0000FF" });
      }
    }
  } else {
    chans = [];
    for (let k = 0; k < comp; k++) chans.push({ label: `C${k}`, color: "FFFFFF" });
  }

  // §4.4 frames
  const N = loops.reduce((a, l) => a * l.count, 1);
  const R = (width * comp * bpc) / 8;
  if (compressed && widthBytes !== R) reject("compressed ND2 with row padding");
  const present: number[] = [];
  for (let f = 0; f < N; f++) if (chunkMap.has(`ImageDataSeq|${f}!`)) present.push(f);
  const frameRanges = new Map<number, Array<[number, number, number]>>();
  if (present.length > 0) {
    if (!compressed) {
      const h0 = await readHeader(r, chunkMap.get(`ImageDataSeq|${present[0]}!`)!);
      const h1 = await readHeader(r, chunkMap.get(`ImageDataSeq|${present[present.length - 1]}!`)!);
      if (h0.nameLen !== h1.nameLen) reject("first and last frame chunk name lengths differ");
      const n = h0.nameLen;
      for (const f of present) {
        const start = chunkMap.get(`ImageDataSeq|${f}!`)! + 16 + n + 8;
        if (widthBytes === R) frameRanges.set(f, [[0, start, height * R]]);
        else {
          const rs: Array<[number, number, number]> = [];
          for (let row = 0; row < height; row++) rs.push([0, start + row * widthBytes, R]);
          frameRanges.set(f, rs);
        }
      }
    } else {
      await Promise.all(
        present.map(async (f) => {
          const o = chunkMap.get(`ImageDataSeq|${f}!`)!;
          const h = await readHeader(r, o);
          if (h.dataLen < 8) reject(`frame ${f} data shorter than its timestamp`);
          frameRanges.set(f, [[0, o + 16 + h.nameLen + 8, h.dataLen - 8]]);
        }),
      );
    }
  }

  // §4.6 output
  const tLoop = loops.find((l) => l.kind === "time");
  const zLoop = loops.find((l) => l.kind === "z");
  const pLoop = loops.find((l) => l.kind === "position");
  const nPos = pLoop ? pLoop.count : 1;
  out.json("zarr.json", { zarr_format: 3, node_type: "group", attributes: { ome: { version: "0.5", "bioformats2raw.layout": 3 } } });
  const series: string[] = [];
  for (let i = 0; i < nPos; i++) series.push(String(i));
  out.json("OME/zarr.json", { zarr_format: 3, node_type: "group", attributes: { ome: { version: "0.5", series } } });

  const axes: string[] = [];
  if (tLoop) axes.push("t");
  if (comp > 1) axes.push("c");
  if (zLoop) axes.push("z");
  axes.push("y", "x");
  const shape: number[] = [];
  const chunkShape: number[] = [];
  const axisObjs: Axis[] = [];
  const scale: number[] = [];
  const calibrated = calib !== undefined;
  for (const a of axes) {
    const o: Axis = { name: a, type: axisType(a) };
    let s = 1;
    if (a === "t") {
      shape.push(tLoop!.count);
      chunkShape.push(1);
      const per = tLoop!.period;
      if (per !== undefined && per > 0) {
        o.unit = "second";
        s = per / 1000;
      }
    } else if (a === "c") {
      shape.push(comp);
      chunkShape.push(comp);
    } else if (a === "z") {
      shape.push(zLoop!.count);
      chunkShape.push(1);
      const st = zLoop!.step;
      if (st !== undefined && st > 0) {
        o.unit = "micrometer";
        s = st;
      }
    } else if (a === "y") {
      shape.push(height);
      chunkShape.push(height);
      if (calibrated) {
        o.unit = "micrometer";
        s = calib! * aspect;
      }
    } else {
      shape.push(width);
      chunkShape.push(width);
      if (calibrated) {
        o.unit = "micrometer";
        s = calib!;
      }
    }
    axisObjs.push(o);
    scale.push(s);
  }
  const arr = arrayJson({
    shape, dataType, chunkShape, axes, interleaved: comp > 1, arrayToBytes: "bytes",
    endian: "little", itemSize: bpc / 8, compressor: compressed ? "zlib" : null,
  });
  const channels = chans.map((c) => {
    const o: Record<string, unknown> = { label: c.label, color: c.color, active: true };
    if (dataType !== "float32") {
      const V = 2 ** bpcSig - 1;
      o.window = { min: 0, max: V, start: 0, end: V };
    }
    return o;
  });
  for (let pos = 0; pos < nPos; pos++) {
    out.json(`${pos}/zarr.json`, {
      zarr_format: 3,
      node_type: "group",
      attributes: {
        ome: {
          version: "0.5",
          multiscales: [{
            name: `position ${pos}`,
            axes: axisObjs,
            datasets: [{ path: "0", coordinateTransformations: [{ type: "scale", scale }] }],
          }],
          omero: { channels },
        },
      },
    });
    out.json(`${pos}/0/zarr.json`, arr);
  }
  for (const f of present) {
    // row-major coordinates over loops, last fastest
    const coord = new Map<string, number>();
    let rem = f;
    for (let i = loops.length - 1; i >= 0; i--) {
      coord.set(loops[i].kind, rem % loops[i].count);
      rem = Math.floor(rem / loops[i].count);
    }
    const pos = coord.get("position") ?? 0;
    const cs: number[] = [];
    if (tLoop) cs.push(coord.get("time")!);
    if (comp > 1) cs.push(0);
    if (zLoop) cs.push(coord.get("z")!);
    cs.push(0, 0);
    out.ref(`${pos}/0/c/${cs.join("/")}`, frameRanges.get(f)!);
  }
}
