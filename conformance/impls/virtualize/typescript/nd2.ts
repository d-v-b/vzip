// ND2 profile, spec/virtualize.md §4.

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
import {
  type LV,
  asColor,
  asFlag,
  asInteger,
  asList,
  asNumber,
  asObject,
  asString,
  member,
  members,
  parseChunk,
} from "./lv.ts";

const MAGIC = 0x0abeceda;
const SIG_NAME = "ND2 FILE SIGNATURE CHUNK NAME01!";
const MAP_SIG = "ND2 CHUNK MAP SIGNATURE 0000001!";
const FILEMAP_NAME = "ND2 FILEMAP SIGNATURE NAME 0001!";

function latin1(b: Uint8Array): string {
  let s = "";
  for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
  return s;
}

interface ChunkHeader {
  offset: number;
  n: number; // name length
  d: number; // data length
}

async function readHeader(src: Source, offset: bigint | number, what: string): Promise<ChunkHeader> {
  const o = typeof offset === "bigint" ? safe(offset, `${what} offset`) : offset;
  const h = await src.read(o, 16);
  const dv = new DataView(h.buffer, h.byteOffset, 16);
  if (dv.getUint32(0, true) !== MAGIC) reject(`${what}: no chunk magic at ${o}`);
  const n = dv.getUint32(4, true);
  const d = safe(dv.getBigUint64(8, true), `${what} data length`);
  return { offset: o, n, d };
}

async function readChunk(src: Source, offset: bigint | number, what: string): Promise<{ h: ChunkHeader; name: Uint8Array; data: Uint8Array }> {
  const h = await readHeader(src, offset, what);
  const name = await src.read(h.offset + 16, h.n);
  const data = await src.read(h.offset + 16 + h.n, h.d);
  return { h, name, data };
}

interface Loop {
  kind: "time" | "position" | "z";
  depth: number;
  count: number;
  value: number; // period (ms) or step
}

interface Node {
  eType: number;
  hasPars: boolean;
  count: number;
  value: number;
  children: Node[];
}

function validityList(v: LV | undefined, what: string): boolean[] | undefined {
  if (v === undefined) return undefined;
  return asList(v, what).map((x, i) => asFlag(x, `${what}[${i}]`));
}

function isValid(list: boolean[] | undefined, i: number): boolean {
  return list === undefined || (i < list.length && list[i]);
}

function opt<T>(obj: LV, name: string, f: (v: LV, what: string) => T): T | undefined {
  const v = member(obj, name);
  return v === undefined ? undefined : f(v, name);
}

function checkNode(node: LV, path: string): Node {
  asObject(node, path);
  const et = member(node, "eType");
  if (et === undefined) reject(`${path}: no eType`);
  const eType = asInteger(et, `${path}/eType`);
  if (![1, 2, 4, 6, 8].includes(eType)) reject(`${path}: eType ${eType}`);
  const pars = member(node, "uLoopPars");
  if (pars !== undefined) asObject(pars, `${path}/uLoopPars`);
  const itemValid = validityList(member(node, "pItemValid"), `${path}/pItemValid`);
  const nextV = member(node, "ppNextLevelEx");
  const kids = nextV === undefined ? [] : members(nextV, `${path}/ppNextLevelEx`);
  const children = kids.map((c, i) => checkNode(asObject(c, `${path}/ppNextLevelEx[${i}]`), `${path}/ppNextLevelEx[${i}]`));
  let count = 0;
  let value = 0;
  if (pars !== undefined) {
    const P = `${path}/uLoopPars`;
    switch (eType) {
      case 1:
        count = opt(pars, "uiCount", (v) => asInteger(v, `${P}/uiCount`)) ?? 0;
        value = opt(pars, "dPeriod", (v) => asNumber(v, `${P}/dPeriod`)) ?? 0;
        break;
      case 8: {
        const pv = member(pars, "pPeriod");
        const ps = pv === undefined ? [] : members(pv, `${P}/pPeriod`);
        const valid = validityList(member(pars, "pPeriodValid"), `${P}/pPeriodValid`);
        let sum = 0n;
        let first = true;
        ps.forEach((p, i) => {
          asObject(p, `${P}/pPeriod[${i}]`);
          if (!isValid(valid, i)) return;
          const c = member(p, "uiCount");
          if (c === undefined) reject(`${P}/pPeriod[${i}]: no uiCount`);
          sum += BigInt(asInteger(c, `${P}/pPeriod[${i}]/uiCount`));
          const per = opt(p, "dPeriod", (v) => asNumber(v, `${P}/pPeriod[${i}]/dPeriod`)) ?? 0;
          if (first) {
            value = per;
            first = false;
          }
        });
        if (sum > MAX_SAFE_BIG) reject(`${P}: period count sum too large`);
        count = Number(sum);
        break;
      }
      case 2: {
        const pts = member(pars, "Points");
        const ps = pts === undefined ? [] : members(pts, `${P}/Points`);
        count = ps.filter((_, i) => isValid(itemValid, i)).length;
        break;
      }
      case 4: {
        count = opt(pars, "uiCount", (v) => asInteger(v, `${P}/uiCount`)) ?? 0;
        const st = opt(pars, "dZStep", (v) => asNumber(v, `${P}/dZStep`)) ?? 0;
        const lo = opt(pars, "dZLow", (v) => asNumber(v, `${P}/dZLow`)) ?? 0;
        const hi = opt(pars, "dZHigh", (v) => asNumber(v, `${P}/dZHigh`)) ?? 0;
        value = Math.abs(st);
        if (value === 0 && count > 1) value = finite(Math.abs(hi - lo) / (count - 1), `${P} z step`);
        break;
      }
      case 6: {
        const c = member(pars, "uiCount");
        if (c !== undefined) count = asInteger(c, `${P}/uiCount`);
        else {
          const pp = member(pars, "pPlanes");
          if (pp === undefined) count = 0;
          else {
            const c2 = member(pp, "uiCount");
            count = c2 === undefined ? 0 : asInteger(c2, `${P}/pPlanes/uiCount`);
          }
        }
        break;
      }
    }
  }
  return { eType, hasPars: pars !== undefined, count, value, children };
}

function flatten(root: Node): Loop[] {
  const loops: Loop[] = [];
  const visit = (node: Node, depth: number) => {
    if (!node.hasPars || node.count === 0) return;
    if (node.eType === 6) {
      for (const c of node.children) visit(c, depth);
      return;
    }
    const kind = node.eType === 2 ? "position" : node.eType === 4 ? "z" : "time";
    const loop: Loop = { kind, depth, count: node.count, value: node.value };
    const last = loops[loops.length - 1];
    if (last === undefined || last.depth < depth) loops.push(loop);
    else if (last.depth === depth && last.kind === kind && last.count < node.count) loops[loops.length - 1] = loop;
    for (const c of node.children) visit(c, depth + 1);
  };
  visit(root, 0);
  const kinds = new Set<string>();
  for (const l of loops) {
    if (kinds.has(l.kind)) reject(`two ${l.kind} loops`);
    kinds.add(l.kind);
  }
  return loops;
}

function hex6(c: number): string {
  const r = c & 0xff;
  const g = (c >>> 8) & 0xff;
  const b = (c >>> 16) & 0xff;
  return [r, g, b].map((x) => x.toString(16).toUpperCase().padStart(2, "0")).join("");
}

export async function virtualizeNd2(src: Source, out: Output): Promise<void> {
  // §4.1 signature
  const sig = await readChunk(src, 0, "signature chunk");
  if (sig.h.n !== 32 || sig.h.d !== 64 || latin1(sig.name) !== SIG_NAME) reject("bad signature chunk");
  const sd = latin1(sig.data);
  const vm = /^Ver([0-9]+)\./.exec(sd);
  if (!vm) reject("signature data has no version");
  if (BigInt(vm[1]) < 3n) reject(`ND2 version ${vm[1]} is below 3`);

  // chunk map
  if (src.size < 40) reject("file shorter than 40 bytes");
  const tail = await src.read(src.size - 40, 40);
  if (latin1(tail.subarray(0, 32)) !== MAP_SIG) reject("no chunk map signature");
  const m = new DataView(tail.buffer, tail.byteOffset, 40).getBigUint64(32, true);
  const mapChunk = await readChunk(src, m, "chunk map");
  const nul = mapChunk.name.indexOf(0);
  const mapName = latin1(nul < 0 ? mapChunk.name : mapChunk.name.subarray(0, nul));
  if (mapName !== FILEMAP_NAME) reject("chunk map chunk has the wrong name");
  const md = mapChunk.data;
  const mdv = new DataView(md.buffer, md.byteOffset, md.byteLength);
  const chunks = new Map<string, bigint>();
  let p = 0;
  for (;;) {
    const ex = md.indexOf(0x21, p);
    if (ex < 0) reject("chunk map has no terminating record");
    const name = latin1(md.subarray(p, ex + 1));
    if (name === MAP_SIG) break;
    if (ex + 1 + 16 > md.length) reject("chunk map record runs past the end of the data");
    chunks.set(name, mdv.getBigUint64(ex + 1, true));
    p = ex + 1 + 16;
  }

  const metaChunk = async (name: string): Promise<LV | undefined> => {
    const o = chunks.get(name);
    if (o === undefined) return undefined;
    const c = await readChunk(src, o, name);
    return parseChunk(c.data);
  };

  // §4.3 attributes
  const attrChunk = await metaChunk("ImageAttributesLV!");
  if (attrChunk === undefined) reject("no ImageAttributesLV! chunk");
  const attrsV = member(attrChunk, "SLxImageAttributes");
  if (attrsV === undefined) reject("no SLxImageAttributes");
  const A = asObject(attrsV, "SLxImageAttributes");
  const reqInt = (name: string) => {
    const v = member(A, name);
    if (v === undefined) reject(`no ${name}`);
    return asInteger(v, name);
  };
  const width = reqInt("uiWidth");
  const height = reqInt("uiHeight");
  const widthBytes = reqInt("uiWidthBytes");
  const comp = reqInt("uiComp");
  const bpc = reqInt("uiBpcInMemory");
  const sigV = member(A, "uiBpcSignificant");
  if (sigV === undefined) reject("no uiBpcSignificant");
  const bpcSig = asNumber(sigV, "uiBpcSignificant");
  const compression = opt(A, "eCompression", asInteger) ?? 2;
  const tileW = opt(A, "uiTileWidth", asInteger) ?? 0;
  const tileH = opt(A, "uiTileHeight", asInteger) ?? 0;
  if (width < 1 || height < 1 || comp < 1) reject("zero width, height or components");
  const dataType = { 8: "uint8", 16: "uint16", 32: "float32" }[bpc as 8 | 16 | 32];
  if (dataType === undefined) reject(`uiBpcInMemory ${bpc}`);
  if (compression !== 0 && compression !== 2) reject(`eCompression ${compression}`);
  const compressed = compression === 0;
  if ((tileW > 0 && tileW !== width) || (tileH > 0 && tileH !== height)) reject("tiled image");

  // experiment
  let loops: Loop[] = [];
  const metaV = await metaChunk("ImageMetadataLV!");
  if (metaV !== undefined) {
    const exp = member(metaV, "SLxExperiment");
    if (exp !== undefined) loops = flatten(checkNode(exp, "SLxExperiment"));
  }

  // picture metadata
  let calibrated = false;
  let calib = 0;
  let aspect = 1;
  let planeCount = 0;
  const planes = new Map<number, { desc: string; color: number; comps: number }>();
  const picV = await metaChunk("ImageMetadataSeqLV|0!");
  if (picV !== undefined) {
    const pm = member(picV, "SLxPictureMetadata");
    if (pm !== undefined) {
      const bCal = opt(pm, "bCalibrated", asFlag) ?? false;
      const dCal = opt(pm, "dCalibration", asNumber);
      let dAsp = opt(pm, "dAspect", asNumber) ?? 1;
      if (!(dAsp > 0)) dAsp = 1;
      aspect = dAsp;
      if (bCal && dCal !== undefined && dCal > 0) {
        calibrated = true;
        calib = dCal;
      }
      const spp = member(pm, "sPicturePlanes");
      if (spp !== undefined) {
        asObject(spp, "sPicturePlanes");
        planeCount = opt(spp, "uiCount", asInteger) ?? 0;
        const spn = member(spp, "sPlaneNew");
        if (spn !== undefined) {
          asObject(spn, "sPlaneNew");
          if (spn.k === "object") {
            for (const [nm, pv] of spn.m) {
              const mm = /^a(0|[1-9][0-9]*)$/.exec(nm);
              if (!mm) continue;
              const i = Number(mm[1]);
              if (!(i < planeCount)) continue;
              asObject(pv, nm);
              planes.set(i, {
                desc: opt(pv, "sDescription", asString) ?? "",
                color: opt(pv, "uiColor", asColor) ?? 0xffffff,
                comps: opt(pv, "uiCompCount", asInteger) ?? 1,
              });
            }
          }
        }
      }
    }
  }

  // §4.4 frames
  let N = 1n;
  for (const l of loops) N *= BigInt(l.count);
  if (N > MAX_SAFE_BIG) reject("too many frames");
  const frames: { f: number; o: bigint }[] = [];
  for (const [name, o] of chunks) {
    const fm = /^ImageDataSeq\|(0|[1-9][0-9]*)!$/.exec(name);
    if (!fm) continue;
    const f = BigInt(fm[1]);
    if (f >= N) continue;
    frames.push({ f: Number(f), o });
  }
  frames.sort((a, b) => a.f - b.f);
  const itemSize = bpc / 8;
  const Rb = BigInt(width) * BigInt(comp) * BigInt(itemSize);
  if (Rb > MAX_SAFE_BIG) reject("row too long");
  const R = Number(Rb);
  if (widthBytes < R) reject("uiWidthBytes is less than the row size");
  const frameRanges = new Map<number, [number, number, number][][]>(); // f -> blocks
  let h = height;
  if (!compressed) {
    if (frames.length > 0) {
      const lo = await readHeader(src, frames[0].o, "first frame");
      const hi = await readHeader(src, frames[frames.length - 1].o, "last frame");
      if (lo.n !== hi.n) reject("frame name lengths differ");
      const need = 8n + BigInt(height) * BigInt(widthBytes);
      if (BigInt(lo.d) < need || BigInt(hi.d) < need) reject("frame data too short");
      const n = lo.n;
      const starts = frames.map((fr) => safe(fr.o + BigInt(16 + n + 8), "frame start"));
      if (widthBytes === R) {
        const len = safe(BigInt(height) * Rb, "frame length");
        frames.forEach((fr, i) => frameRanges.set(fr.f, [[[0, starts[i], len]]]));
      } else {
        // row blocks: largest divisor h of height with every block's payload <= 65519
        const rowLen = (s: number, r: number) => {
          const o = s + r * widthBytes;
          const l = (o > 0 ? 1 : 0) + (o > 0 ? varint(o) : 0) + 1 + varint(R);
          return 1 + varint(l) + l;
        };
        const divisors: number[] = [];
        for (let d = 1; d * d <= height; d++)
          if (height % d === 0) {
            divisors.push(d);
            if (d !== height / d) divisors.push(height / d);
          }
        divisors.sort((a, b) => b - a);
        h = 1;
        for (const d of divisors) {
          if (d === 1) break;
          let ok = true;
          for (const s of starts) {
            if (!Number.isSafeInteger(s + height * widthBytes)) reject("frame offset out of range");
            for (let j = 0; ok && j < height / d; j++) {
              let t = 0;
              for (let r = j * d; r < j * d + d; r++) t += rowLen(s, r);
              if (t > 65519) ok = false;
            }
            if (!ok) break;
          }
          if (ok) {
            h = d;
            break;
          }
        }
        frames.forEach((fr, i) => {
          const blocks: [number, number, number][][] = [];
          for (let j = 0; j < height / h; j++) {
            const rs: [number, number, number][] = [];
            for (let r = j * h; r < j * h + h; r++) rs.push([0, starts[i] + r * widthBytes, R]);
            blocks.push(rs);
          }
          frameRanges.set(fr.f, blocks);
        });
      }
    }
  } else {
    if (widthBytes !== R) reject("compressed frames with padded rows");
    for (const fr of frames) {
      const hd = await readHeader(src, fr.o, `frame ${fr.f}`);
      if (hd.d <= 8) reject(`frame ${fr.f} data too short`);
      frameRanges.set(fr.f, [[[0, hd.offset + 16 + hd.n + 8, hd.d - 8]]]);
    }
  }

  // §4.5 channels
  const channels: { label: string; color: string }[] = [];
  let labeled = false;
  if (planeCount >= 1) {
    let ok = true;
    let sum = 0;
    for (let i = 0; i < planeCount; i++) {
      const pl = planes.get(i);
      if (pl === undefined || (pl.comps !== 1 && pl.comps !== 3)) {
        ok = false;
        break;
      }
      sum += pl.comps;
    }
    if (ok && sum === comp) {
      labeled = true;
      for (let i = 0; i < planeCount; i++) {
        const pl = planes.get(i)!;
        if (pl.comps === 1) channels.push({ label: pl.desc, color: hex6(pl.color) });
        else
          channels.push(
            { label: `${pl.desc} R`, color: "FF0000" },
            { label: `${pl.desc} G`, color: "00FF00" },
            { label: `${pl.desc} B`, color: "0000FF" },
          );
      }
    }
  }
  if (!labeled) for (let k = 0; k < comp; k++) channels.push({ label: `C${k}`, color: "FFFFFF" });

  // §4.6 output
  const tLoop = loops.find((l) => l.kind === "time");
  const zLoop = loops.find((l) => l.kind === "z");
  const pLoop = loops.find((l) => l.kind === "position");
  const nPos = pLoop ? pLoop.count : 1;
  const dims: string[] = [];
  if (tLoop) dims.push("t");
  if (comp > 1) dims.push("c");
  if (zLoop) dims.push("z");
  dims.push("y", "x");
  const shape: number[] = [];
  const chunk: number[] = [];
  const axes: Record<string, string>[] = [];
  const scale: number[] = [];
  for (const d of dims) {
    const ax: Record<string, string> = { name: d, type: AXIS_TYPE[d] };
    let sc = 1;
    if (d === "t") {
      shape.push(tLoop!.count);
      chunk.push(1);
      if (tLoop!.value > 0) {
        ax.unit = "second";
        sc = finite(tLoop!.value / 1000, "time scale");
      }
    } else if (d === "c") {
      shape.push(comp);
      chunk.push(comp);
    } else if (d === "z") {
      shape.push(zLoop!.count);
      chunk.push(1);
      if (zLoop!.value > 0) {
        ax.unit = "micrometer";
        sc = zLoop!.value;
      }
    } else if (d === "y") {
      shape.push(height);
      chunk.push(h);
      if (calibrated) {
        ax.unit = "micrometer";
        sc = finite(calib * aspect, "y scale");
      }
    } else {
      shape.push(width);
      chunk.push(width);
      if (calibrated) {
        ax.unit = "micrometer";
        sc = calib;
      }
    }
    axes.push(ax);
    scale.push(sc);
  }
  const codecs: unknown[] = [];
  if (comp > 1) codecs.push(transposeCodec(dims));
  codecs.push(bytesCodec(itemSize, "little"));
  if (compressed) codecs.push({ name: "zlib", configuration: { level: 1 } });
  let bits = bpc;
  if (Number.isInteger(bpcSig) && bpcSig >= 1 && bpcSig <= bpc) bits = bpcSig;
  const omeroChannels = channels.map((c) => {
    const o: Record<string, unknown> = { label: c.label, color: c.color, active: true };
    if (dataType !== "float32") {
      const V = 2 ** bits - 1;
      o.window = { min: 0, max: V, start: 0, end: V };
    }
    return o;
  });

  out.json("zarr.json", { zarr_format: 3, node_type: "group", attributes: { ome: { version: "0.5", "bioformats2raw.layout": 3 } } });
  const series: string[] = [];
  for (let pi = 0; pi < nPos; pi++) series.push(String(pi));
  out.json("OME/zarr.json", { zarr_format: 3, node_type: "group", attributes: { ome: { version: "0.5", series } } });
  for (let pi = 0; pi < nPos; pi++) {
    out.json(`${pi}/zarr.json`, {
      zarr_format: 3,
      node_type: "group",
      attributes: {
        ome: {
          version: "0.5",
          multiscales: [
            {
              name: `position ${pi}`,
              axes,
              datasets: [{ path: "0", coordinateTransformations: [{ type: "scale", scale }] }],
            },
          ],
          omero: { channels: omeroChannels },
        },
      },
    });
    out.json(`${pi}/0/zarr.json`, arrayJson({ shape, dataType, chunkShape: chunk, codecs, dims }));
  }
  for (const fr of frames) {
    // coordinates: row-major over the loops, last fastest
    let rem = fr.f;
    const idx = new Map<string, number>();
    for (let i = loops.length - 1; i >= 0; i--) {
      idx.set(loops[i].kind, rem % loops[i].count);
      rem = Math.floor(rem / loops[i].count);
    }
    const pos = idx.get("position") ?? 0;
    const blocks = frameRanges.get(fr.f)!;
    blocks.forEach((ranges, j) => {
      const coords: number[] = [];
      for (const d of dims) {
        if (d === "t") coords.push(idx.get("time")!);
        if (d === "c") coords.push(0);
        if (d === "z") coords.push(idx.get("z")!);
      }
      coords.push(j, 0);
      out.ref(`${pos}/0/c/${coords.join("/")}`, ranges);
    });
  }
}

function varint(v: number): number {
  let n = 1;
  while (v >= 128) {
    v = Math.floor(v / 128);
    n++;
  }
  return n;
}
