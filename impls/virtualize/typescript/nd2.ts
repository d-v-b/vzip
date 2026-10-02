// ND2 profile (VIRTUALIZE.md §4).
import { MAX_SAFE, Output, Source, arrayJson, axisType, buildCodecs, finite, reject } from "./io.ts";
import type { Axis, Range } from "./io.ts";
import { decodeLv, flag, flagOf, get, getLevel, membersOf, num, numOf, uint } from "./lv.ts";
import type { LvLevel, LvValue } from "./lv.ts";

const MAGIC = 0x0abeceda;
const SIG_NAME = "ND2 FILE SIGNATURE CHUNK NAME01!";
const MAP_SIG = "ND2 CHUNK MAP SIGNATURE 0000001!";
const FILEMAP_NAME = "ND2 FILEMAP SIGNATURE NAME 0001!";

function latin1(b: Uint8Array): string {
  let s = "";
  for (const c of b) s += String.fromCharCode(c);
  return s;
}

function u64(dv: DataView, o: number, what: string): number {
  const v = dv.getBigUint64(o, true);
  if (v > BigInt(MAX_SAFE)) reject(`${what}: ${v} above 2^53 - 1`);
  return Number(v);
}

type ChunkHeader = { o: number; n: number; d: number; name: Uint8Array };

async function chunkHeader(src: Source, o: number): Promise<ChunkHeader> {
  const h = await src.read(o, 16);
  const dv = new DataView(h.buffer, h.byteOffset, 16);
  if (dv.getUint32(0, true) !== MAGIC) reject(`no chunk magic at ${o}`);
  const n = dv.getUint32(4, true);
  const d = u64(dv, 8, `chunk at ${o}: data length`);
  const name = await src.read(o + 16, n);
  return { o, n, d, name };
}

async function chunkData(src: Source, h: ChunkHeader): Promise<Uint8Array> {
  return await src.read(h.o + 16 + h.n, h.d);
}

type Loop = { kind: "time" | "position" | "z"; eType: number; depth: number; count: number; node: LvLevel };

function loopKind(eType: number): "time" | "position" | "z" {
  return eType === 1 || eType === 8 ? "time" : eType === 2 ? "position" : "z";
}

function validMembers(pars: LvLevel, listPath: string, validHolder: LvLevel, validPath: string): LvValue[] {
  const lv = get(pars, listPath);
  if (lv === undefined) return [];
  const items = membersOf(lv, listPath);
  const vv = get(validHolder, validPath);
  if (vv === undefined) return items;
  const valid = membersOf(vv, validPath);
  return items.filter((_, i) => i < valid.length && flagOf(valid[i], `${validPath}[${i}]`));
}

function asNode(v: LvValue, what: string): LvLevel {
  if (v.t !== 11) reject(`${what} is not a level`);
  return v.v;
}

function nodeCount(eType: number, pars: LvLevel, node: LvLevel): number {
  switch (eType) {
    case 1:
    case 4:
      return uint(pars, "uiCount", 0);
    case 8: {
      let sum = 0;
      for (const p of validMembers(pars, "pPeriod", pars, "pPeriodValid")) sum += uint(asNode(p, "pPeriod member"), "uiCount");
      if (!Number.isSafeInteger(sum)) reject("time loop count too large");
      return sum;
    }
    case 2:
      return validMembers(pars, "Points", node, "pItemValid").length;
    case 6:
      if (get(pars, "uiCount") !== undefined) return uint(pars, "uiCount");
      if (get(pars, "pPlanes/uiCount") !== undefined) return uint(pars, "pPlanes/uiCount");
      return 0;
  }
  reject(`eType ${eType}`);
}

function flattenExperiment(root: LvLevel): Loop[] {
  const loops: Loop[] = [];
  const visit = (node: LvLevel, depth: number) => {
    const eType = num(node, "eType");
    if (![1, 2, 4, 6, 8].includes(eType)) reject(`unsupported loop eType ${eType}`);
    const pars = getLevel(node, "uLoopPars");
    const children = (): LvLevel[] => {
      const c = get(node, "ppNextLevelEx");
      if (c === undefined) return [];
      return membersOf(c, "ppNextLevelEx").map((v) => asNode(v, "ppNextLevelEx member"));
    };
    if (pars === undefined) return;
    const count = nodeCount(eType, pars, node);
    if (count === 0) return;
    if (eType === 6) {
      for (const ch of children()) visit(ch, depth);
      return;
    }
    const last = loops[loops.length - 1];
    const loop: Loop = { kind: loopKind(eType), eType, depth, count, node: pars };
    if (!last || last.depth < depth) loops.push(loop);
    else if (last.depth === depth && last.eType === eType && last.count < count) loops[loops.length - 1] = loop;
    for (const ch of children()) visit(ch, depth + 1);
  };
  visit(root, 0);
  const kinds = new Set<string>();
  for (const l of loops) {
    if (kinds.has(l.kind)) reject(`two ${l.kind} loops`);
    kinds.add(l.kind);
  }
  return loops;
}

export async function virtualizeNd2(src: Source, out: Output): Promise<void> {
  // ---- §4.1 signature
  const sig = await chunkHeader(src, 0);
  if (sig.n !== 32 || sig.d !== 64 || latin1(sig.name) !== SIG_NAME) reject("bad signature chunk");
  const sigData = latin1(await chunkData(src, sig));
  const vm = /^Ver([0-9]+)\./.exec(sigData);
  if (!vm) reject("signature chunk data does not start with Ver<digits>.");
  if (Number(vm[1]) < 3) reject(`ND2 version ${vm[1]} < 3`);

  // ---- chunk map
  if (src.size < 40) reject("file shorter than 40 bytes");
  const tail = await src.read(src.size - 40, 40);
  if (latin1(tail.subarray(0, 32)) !== MAP_SIG) reject("no chunk map signature at the end of the file");
  const m = u64(new DataView(tail.buffer, tail.byteOffset, 40), 32, "chunk map offset");
  const mh = await chunkHeader(src, m);
  let mname = latin1(mh.name);
  const nul = mname.indexOf("\0");
  if (nul >= 0) mname = mname.slice(0, nul);
  if (mname !== FILEMAP_NAME) reject(`chunk map chunk named "${mname}"`);
  const md = await chunkData(src, mh);
  const mdv = new DataView(md.buffer, md.byteOffset, md.byteLength);
  const map = new Map<string, number>();
  let p = 0;
  for (;;) {
    const bang = md.indexOf(0x21, p);
    if (bang < 0) reject("chunk map: record without '!'");
    const name = latin1(md.subarray(p, bang + 1));
    p = bang + 1;
    if (p + 16 > md.length) reject("chunk map: truncated record");
    if (name === MAP_SIG) break;
    map.set(name, u64(mdv, p, `chunk map offset of ${name}`));
    p += 16;
  }

  const lvChunk = async (name: string): Promise<LvLevel | undefined> => {
    const o = map.get(name);
    if (o === undefined) return undefined;
    const h = await chunkHeader(src, o);
    return decodeLv(await chunkData(src, h));
  };

  // ---- §4.3 attributes
  const attrChunk = await lvChunk("ImageAttributesLV!");
  if (!attrChunk) reject("no ImageAttributesLV! chunk");
  const A = getLevel(attrChunk, "SLxImageAttributes");
  if (!A) reject("no SLxImageAttributes");
  const W = uint(A, "uiWidth");
  const H = uint(A, "uiHeight");
  const WB = uint(A, "uiWidthBytes");
  const comp = uint(A, "uiComp");
  const bpc = uint(A, "uiBpcInMemory");
  const bpcSigRaw = num(A, "uiBpcSignificant");
  if (W < 1 || H < 1 || comp < 1) reject("uiWidth, uiHeight and uiComp must be at least 1");
  const ecomp = num(A, "eCompression", 2);
  const tw = num(A, "uiTileWidth", 0);
  const th = num(A, "uiTileHeight", 0);
  const dataType = bpc === 8 ? "uint8" : bpc === 16 ? "uint16" : bpc === 32 ? "float32" : null;
  if (!dataType) reject(`uiBpcInMemory ${bpc} unsupported`);
  let compressed: boolean;
  if (ecomp === 2) compressed = false;
  else if (ecomp === 0) compressed = true;
  else reject(`eCompression ${ecomp} unsupported`);
  if ((tw > 0 && tw !== W) || (th > 0 && th !== H)) reject("tiled ND2 unsupported");

  // ---- experiment
  const metaChunk = await lvChunk("ImageMetadataLV!");
  let loops: Loop[] = [];
  if (metaChunk) {
    const exp = get(metaChunk, "SLxExperiment");
    if (exp === undefined) reject("ImageMetadataLV! has no SLxExperiment");
    loops = flattenExperiment(asNode(exp, "SLxExperiment"));
    if (process.env.VZ_DEBUG) process.stderr.write(JSON.stringify(loops.map((l) => [l.kind, l.eType, l.depth, l.count])) + "\n");
  }

  // ---- picture metadata
  const picChunk = await lvChunk("ImageMetadataSeqLV|0!");
  const P = picChunk ? getLevel(picChunk, "SLxPictureMetadata") : undefined;

  // ---- §4.4 frames
  const R = (W * comp * bpc) / 8;
  if (WB < R) reject(`uiWidthBytes ${WB} < ${R}`);
  let N = 1;
  for (const l of loops) N *= l.count;
  const frames: Array<[number, number]> = []; // [f, offset]
  for (const [name, o] of map) {
    const fm = /^ImageDataSeq\|(0|[1-9][0-9]*)!$/.exec(name);
    if (!fm) continue;
    const f = Number(fm[1]);
    if (f < N) frames.push([f, o]);
  }
  frames.sort((a, b) => a[0] - b[0]);
  const frameRanges = new Map<number, Range[]>();
  if (frames.length > 0) {
    if (!compressed) {
      const lo = await chunkHeader(src, frames[0][1]);
      const hi = await chunkHeader(src, frames[frames.length - 1][1]);
      if (lo.n !== hi.n) reject("frame chunk name lengths differ");
      const need = 8 + H * WB;
      if (lo.d < need || hi.d < need) reject("frame chunk data too short");
      for (const [f, o] of frames) {
        const start = o + 16 + lo.n + 8;
        if (WB === R) frameRanges.set(f, [[0, start, H * R]]);
        else {
          const rs: Range[] = [];
          for (let r = 0; r < H; r++) rs.push([0, start + r * WB, R]);
          frameRanges.set(f, rs);
        }
      }
    } else {
      if (WB !== R) reject("compressed frames with row padding");
      await src.prefetch(frames.map(([, o]) => [o, 16 + 64] as [number, number]));
      for (const [f, o] of frames) {
        const h = await chunkHeader(src, o);
        if (h.d <= 8) reject(`frame ${f}: compressed data length ${h.d} <= 8`);
        frameRanges.set(f, [[0, o + 16 + h.n + 8, h.d - 8]]);
      }
    }
  }

  // ---- §4.5 channels
  const labels: string[] = [];
  const colors: string[] = [];
  const planeCount = P ? uint(P, "sPicturePlanes/uiCount", 0) : 0;
  let labeled = false;
  if (planeCount >= 1) {
    const planes: LvLevel[] = [];
    let ok = true;
    for (let i = 0; i < planeCount; i++) {
      const v = get(P, `sPicturePlanes/sPlaneNew/a${i}`);
      if (v === undefined) { ok = false; break; }
      planes.push(asNode(v, `a${i}`));
    }
    if (ok) {
      const cc = planes.map((pl) => uint(pl, "uiCompCount", 1));
      if (cc.every((c) => c === 1 || c === 3) && cc.reduce((a, b) => a + b, 0) === comp) {
        labeled = true;
        for (let i = 0; i < planes.length; i++) {
          const dv = get(planes[i], "sDescription");
          let desc = "";
          if (dv !== undefined) {
            if (dv.t !== 8) reject("sDescription is not a string");
            desc = dv.v;
          }
          if (cc[i] === 1) {
            const c = num(planes[i], "uiColor", 0xffffff);
            if (!Number.isInteger(c)) reject("uiColor is not an integer");
            const u = ((c % 2 ** 32) + 2 ** 32) % 2 ** 32;
            const hex = (x: number) => x.toString(16).toUpperCase().padStart(2, "0");
            labels.push(desc);
            colors.push(hex(u & 0xff) + hex((u >>> 8) & 0xff) + hex((u >>> 16) & 0xff));
          } else {
            labels.push(`${desc} R`, `${desc} G`, `${desc} B`);
            colors.push("FF0000", "00FF00", "0000FF");
          }
        }
      }
    }
  }
  if (!labeled) {
    for (let k = 0; k < comp; k++) {
      labels.push(`C${k}`);
      colors.push("FFFFFF");
    }
  }

  // calibration
  let calibrated = false;
  let cal = 0;
  let aspect = 1;
  if (P) {
    const dc = get(P, "dCalibration");
    if (flag(P, "bCalibrated", false) && dc !== undefined) {
      const v = numOf(dc, "dCalibration");
      if (v > 0) {
        calibrated = true;
        cal = v;
      }
    }
    if (calibrated) {
      const da = num(P, "dAspect", 1);
      aspect = da > 0 ? da : 1;
    }
  }

  // ---- §4.6 output
  const tLoop = loops.find((l) => l.kind === "time");
  const zLoop = loops.find((l) => l.kind === "z");
  const pLoop = loops.find((l) => l.kind === "position");
  const nPos = pLoop ? pLoop.count : 1;

  const axes: string[] = [];
  if (tLoop) axes.push("t");
  if (comp > 1) axes.push("c");
  if (zLoop) axes.push("z");
  axes.push("y", "x");

  let period = 0;
  if (tLoop) {
    if (tLoop.eType === 1) period = num(tLoop.node, "dPeriod", 0);
    else {
      const first = validMembers(tLoop.node, "pPeriod", tLoop.node, "pPeriodValid")[0];
      period = first ? num(asNode(first, "pPeriod member"), "dPeriod", 0) : 0;
    }
  }
  let step = 0;
  if (zLoop) {
    step = finite(Math.abs(num(zLoop.node, "dZStep", 0)), "z step");
    if (step === 0 && zLoop.count > 1) {
      const hi = num(zLoop.node, "dZHigh", 0);
      const lo = num(zLoop.node, "dZLow", 0);
      step = finite(Math.abs(finite(hi - lo, "dZHigh - dZLow")) / (zLoop.count - 1), "z step");
    }
  }

  const axisObjs: Axis[] = [];
  const scale: number[] = [];
  for (const a of axes) {
    const o: Axis = { name: a, type: axisType(a) };
    let s = 1;
    if (a === "x" && calibrated) { o.unit = "micrometer"; s = cal; }
    else if (a === "y" && calibrated) { o.unit = "micrometer"; s = finite(cal * aspect, "y scale"); }
    else if (a === "z" && step > 0) { o.unit = "micrometer"; s = step; }
    else if (a === "t" && period > 0) { o.unit = "second"; s = finite(period / 1000, "t scale"); }
    axisObjs.push(o);
    scale.push(s);
  }

  let window: { min: number; max: number; start: number; end: number } | null = null;
  if (dataType !== "float32") {
    const b = Number.isInteger(bpcSigRaw) && bpcSigRaw >= 1 && bpcSigRaw <= bpc ? bpcSigRaw : bpc;
    const V = 2 ** b - 1;
    window = { min: 0, max: V, start: 0, end: V };
  }
  const channels = labels.map((label, k) => {
    const ch: Record<string, unknown> = { label, color: colors[k], active: true };
    if (window) ch.window = window;
    return ch;
  });

  const shape: number[] = [];
  const chunk: number[] = [];
  for (const a of axes) {
    if (a === "t") { shape.push(tLoop!.count); chunk.push(1); }
    else if (a === "c") { shape.push(comp); chunk.push(comp); }
    else if (a === "z") { shape.push(zLoop!.count); chunk.push(1); }
    else if (a === "y") { shape.push(H); chunk.push(H); }
    else { shape.push(W); chunk.push(W); }
  }
  const arr = arrayJson({
    shape, dataType, chunkShape: chunk,
    codecs: buildCodecs({
      axes, interleaved: comp > 1, arrayToBytes: "bytes", itemSize: bpc / 8, littleEndian: true,
      compressor: compressed ? "zlib" : null,
    }),
    dimensionNames: axes,
  });

  out.json("zarr.json", { zarr_format: 3, node_type: "group", attributes: { ome: { version: "0.5", "bioformats2raw.layout": 3 } } });
  out.json("OME/zarr.json", {
    zarr_format: 3, node_type: "group",
    attributes: { ome: { version: "0.5", series: Array.from({ length: nPos }, (_, i) => `${i}`) } },
  });
  for (let pIdx = 0; pIdx < nPos; pIdx++) {
    out.json(`${pIdx}/zarr.json`, {
      zarr_format: 3, node_type: "group",
      attributes: {
        ome: {
          version: "0.5",
          multiscales: [{
            name: `position ${pIdx}`,
            axes: axisObjs,
            datasets: [{ path: "0", coordinateTransformations: [{ type: "scale", scale }] }],
          }],
          omero: { channels },
        },
      },
    });
    out.json(`${pIdx}/0/zarr.json`, arr);
  }

  // chunks
  for (const [f, ranges] of frameRanges) {
    const coord: Record<string, number> = {};
    let rem = f;
    for (let i = loops.length - 1; i >= 0; i--) {
      coord[loops[i].kind] = rem % loops[i].count;
      rem = Math.floor(rem / loops[i].count);
    }
    const pIdx = coord.position ?? 0;
    const cs: number[] = [];
    for (const a of axes) {
      if (a === "t") cs.push(coord.time);
      else if (a === "c") cs.push(0);
      else if (a === "z") cs.push(coord.z);
    }
    cs.push(0, 0);
    out.ref(`${pIdx}/0/c/${cs.join("/")}`, ranges);
  }
}
