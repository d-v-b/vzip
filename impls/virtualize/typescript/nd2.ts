// ND2 profile, VIRTUALIZE.md §4.

import {
  MAX_SAFE,
  Output,
  type Range,
  Source,
  arrayDoc,
  axisObj,
  big2num,
  bytesCodec,
  fin,
  groupDoc,
  payloadLen,
  reject,
  transposeCodec,
} from "./io.ts";
import {
  type LV,
  asColor,
  asFlag,
  asInt,
  asList,
  asNumber,
  asObject,
  asString,
  at,
  decodeChunk,
  members,
} from "./lv.ts";

const latin1 = (b: Uint8Array) => Buffer.from(b).toString("latin1");
const SIG = "ND2 FILE SIGNATURE CHUNK NAME01!";
const MAPSIG = "ND2 CHUNK MAP SIGNATURE 0000001!";
const FILEMAP = "ND2 FILEMAP SIGNATURE NAME 0001!";

type Header = { o: number; n: number; d: number; dataOff: number };

async function header(src: Source, o: bigint | number): Promise<Header> {
  const off = typeof o === "bigint" ? big2num(o, "chunk offset") : o;
  const b = await src.read(off, 16);
  const dv = new DataView(b.buffer, b.byteOffset, 16);
  if (dv.getUint32(0, true) !== 0x0abeceda) reject(`no chunk magic at ${off}`);
  const n = dv.getUint32(4, true);
  const d = big2num(dv.getBigUint64(8, true), "chunk data length");
  return { o: off, n, d, dataOff: off + 16 + n };
}

type Loop = { kind: "t" | "p" | "z"; depth: number; count: number; val: number };

const req = <T>(v: T | undefined, what: string): T => {
  if (v === undefined) reject(`missing ${what}`);
  return v;
};

function validity(list: LV[] | undefined): (i: number) => boolean {
  return (i) => list === undefined || (i < list.length && asFlag(list[i], "validity"));
}

function readExperiment(root: LV): Loop[] {
  const loops: Loop[] = [];
  const stack: { node: LV; depth: number }[] = [{ node: root, depth: 0 }];
  while (stack.length > 0) {
    const { node, depth } = stack.pop()!;
    const obj = asObject(node, "experiment node");
    const eType = asInt(req(at(obj, "eType"), "eType"), "eType");
    if (![1, 2, 4, 6, 8].includes(eType)) reject(`eType ${eType}`);
    const lpV = at(obj, "uLoopPars");
    const lp = lpV === undefined ? undefined : asObject(lpV, "uLoopPars");
    const ivV = at(obj, "pItemValid");
    const itemValid = ivV === undefined ? undefined : asList(ivV, "pItemValid");
    itemValid?.forEach((x) => asFlag(x, "pItemValid member"));
    const nlV = at(obj, "ppNextLevelEx");
    const children = nlV === undefined ? [] : members(nlV, "ppNextLevelEx");
    children.forEach((c) => asObject(c, "ppNextLevelEx member"));

    let count = 0;
    let val = 0;
    let kind: Loop["kind"] | "s" = "t";
    if (lp) {
      const num = (k: string, dflt: number) => {
        const v = at(lp, k);
        return v === undefined ? dflt : asNumber(v, k);
      };
      const int = (k: string, dflt: number | undefined) => {
        const v = at(lp, k);
        return v === undefined ? dflt : asInt(v, k);
      };
      if (eType === 1) {
        kind = "t";
        count = int("uiCount", 0)!;
        val = num("dPeriod", 0);
      } else if (eType === 8) {
        kind = "t";
        const ppV = at(lp, "pPeriod");
        const ps = ppV === undefined ? [] : members(ppV, "pPeriod");
        ps.forEach((p) => asObject(p, "pPeriod member"));
        const pvV = at(lp, "pPeriodValid");
        const pv = pvV === undefined ? undefined : asList(pvV, "pPeriodValid");
        pv?.forEach((x) => asFlag(x, "pPeriodValid member"));
        const valid = validity(pv);
        let first = true;
        for (let i = 0; i < ps.length; i++) {
          if (!valid(i)) continue;
          count += asInt(req(at(ps[i], "uiCount"), "pPeriod/uiCount"), "pPeriod/uiCount");
          if (count > MAX_SAFE) reject("time loop count above 2^53-1");
          const dpV = at(ps[i], "dPeriod");
          const dp = dpV === undefined ? 0 : asNumber(dpV, "pPeriod/dPeriod");
          if (first) val = dp;
          first = false;
        }
      } else if (eType === 2) {
        kind = "p";
        const ptV = at(lp, "Points");
        const pts = ptV === undefined ? [] : members(ptV, "Points");
        const valid = validity(itemValid);
        for (let i = 0; i < pts.length; i++) if (valid(i)) count++;
      } else if (eType === 4) {
        kind = "z";
        count = int("uiCount", 0)!;
        const step = num("dZStep", 0);
        const lo = num("dZLow", 0);
        const hi = num("dZHigh", 0);
        val = Math.abs(step);
        if (val === 0 && count > 1) val = fin(Math.abs(fin(hi - lo, "dZHigh - dZLow")) / (count - 1), "z step");
      } else {
        kind = "s";
        const c1 = int("uiCount", undefined);
        const ppl = at(lp, "pPlanes");
        const c2 = ppl === undefined ? undefined : at(lp, "pPlanes/uiCount");
        const c2n = c2 === undefined ? undefined : asInt(c2, "pPlanes/uiCount");
        count = c1 ?? c2n ?? 0;
      }
    }
    if (!lp || count === 0) continue; // node and children skipped
    let childDepth = depth + 1;
    if (kind === "s") childDepth = depth;
    else {
      const last = loops[loops.length - 1];
      const loop: Loop = { kind, depth, count, val };
      if (!last || last.depth < depth) loops.push(loop);
      else if (last.depth === depth && last.kind === kind && last.count < count) loops[loops.length - 1] = loop;
    }
    for (let i = children.length - 1; i >= 0; i--) stack.push({ node: children[i], depth: childDepth });
  }
  const kinds = new Set<string>();
  for (const l of loops) {
    if (kinds.has(l.kind)) reject(`two loops of kind ${l.kind}`);
    kinds.add(l.kind);
  }
  return loops;
}

export async function virtualizeNd2(src: Source, out: Output): Promise<void> {
  // §4.1 signature
  const sh = await header(src, 0);
  if (sh.n !== 32 || sh.d !== 64) reject("signature chunk lengths");
  const sname = latin1(await src.read(16, 32));
  if (sname !== SIG) reject("signature chunk name");
  const sdata = latin1(await src.read(48, 64));
  const vm = /^Ver([0-9]+)\./.exec(sdata);
  if (!vm) reject("no version in signature");
  if (BigInt(vm[1]) < 3n) reject(`ND2 version ${vm[1]}`);

  // chunk map
  if (src.size < 40) reject("file shorter than 40 bytes");
  const tail = await src.read(src.size - 40, 40);
  if (latin1(tail.subarray(0, 32)) !== MAPSIG) reject("no chunk map signature");
  const m = new DataView(tail.buffer, tail.byteOffset + 32, 8).getBigUint64(0, true);
  const mh = await header(src, m);
  const mnameB = await src.read(mh.o + 16, mh.n);
  const nul = mnameB.indexOf(0);
  if (latin1(nul < 0 ? mnameB : mnameB.subarray(0, nul)) !== FILEMAP) reject("chunk map chunk name");
  const md = await src.read(mh.dataOff, mh.d);
  const map = new Map<string, bigint>();
  {
    let p = 0;
    for (;;) {
      const bang = md.indexOf(0x21, p);
      if (bang < 0) reject("chunk map without its last record");
      const name = latin1(md.subarray(p, bang + 1));
      if (name === MAPSIG) break;
      if (bang + 1 + 16 > md.length) reject("chunk map record runs past the data");
      map.set(name, new DataView(md.buffer, md.byteOffset + bang + 1, 8).getBigUint64(0, true));
      p = bang + 17;
    }
  }
  const chunkLV = async (name: string): Promise<Map<string, LV> | undefined> => {
    const o = map.get(name);
    if (o === undefined) return undefined;
    const h = await header(src, o);
    return decodeChunk(await src.read(h.dataOff, h.d));
  };

  // §4.3 attributes
  const attrChunk = await chunkLV("ImageAttributesLV!");
  if (!attrChunk) reject("no ImageAttributesLV! chunk");
  const ia = asObject(req(at(attrChunk, "SLxImageAttributes"), "SLxImageAttributes"), "SLxImageAttributes");
  const reqInt = (k: string) => asInt(req(at(ia, k), k), k);
  const optInt = (k: string, d: number) => {
    const v = at(ia, k);
    return v === undefined ? d : asInt(v, k);
  };
  const width = reqInt("uiWidth");
  const height = reqInt("uiHeight");
  const widthBytes = reqInt("uiWidthBytes");
  const comp = reqInt("uiComp");
  const bpc = reqInt("uiBpcInMemory");
  const bpcSig = asNumber(req(at(ia, "uiBpcSignificant"), "uiBpcSignificant"), "uiBpcSignificant");
  const eComp = optInt("eCompression", 2);
  const tileW = optInt("uiTileWidth", 0);
  const tileH = optInt("uiTileHeight", 0);
  if (width < 1 || height < 1 || comp < 1) reject("uiWidth, uiHeight and uiComp must be at least 1");
  const dataType = ({ 8: "uint8", 16: "uint16", 32: "float32" } as Record<number, string>)[bpc];
  if (!dataType) reject(`uiBpcInMemory ${bpc}`);
  if (eComp !== 0 && eComp !== 2) reject(`eCompression ${eComp}`);
  const compressed = eComp === 0;
  if ((tileW > 0 && tileW !== width) || (tileH > 0 && tileH !== height)) reject("tiled ND2");

  // experiment
  const metaChunk = await chunkLV("ImageMetadataLV!");
  const expV = metaChunk ? at(metaChunk, "SLxExperiment") : undefined;
  const loops = expV === undefined ? [] : readExperiment(expV);

  // picture metadata
  const picChunk = await chunkLV("ImageMetadataSeqLV|0!");
  const pmV = picChunk ? at(picChunk, "SLxPictureMetadata") : undefined;
  let calibrated = false;
  let cal = 0;
  let aspect = 1;
  const planes = new Map<number, { desc: string; color: number; comps: number }>();
  let planeCount = 0;
  if (pmV !== undefined) {
    const pm = asObject(pmV, "SLxPictureMetadata");
    const bc = at(pm, "bCalibrated");
    const bCal = bc === undefined ? false : asFlag(bc, "bCalibrated");
    const dc = at(pm, "dCalibration");
    const dCal = dc === undefined ? undefined : asNumber(dc, "dCalibration");
    const da = at(pm, "dAspect");
    aspect = da === undefined ? 1 : asNumber(da, "dAspect");
    if (!(aspect > 0)) aspect = 1;
    if (bCal && dCal !== undefined && dCal > 0) {
      calibrated = true;
      cal = dCal;
    }
    const spV = at(pm, "sPicturePlanes");
    if (spV !== undefined) {
      const sp = asObject(spV, "sPicturePlanes");
      const uc = at(sp, "uiCount");
      planeCount = uc === undefined ? 0 : asInt(uc, "sPicturePlanes/uiCount");
      const pnV = at(sp, "sPlaneNew");
      if (pnV !== undefined) {
        const pn = asObject(pnV, "sPlaneNew");
        for (const [name, v] of pn) {
          const mm = /^a(0|[1-9][0-9]*)$/.exec(name);
          if (!mm || mm[1].length > 16 || Number(mm[1]) >= planeCount) continue;
          const po = asObject(v, `plane ${name}`);
          const sd = at(po, "sDescription");
          const uc2 = at(po, "uiColor");
          const cc = at(po, "uiCompCount");
          planes.set(Number(mm[1]), {
            desc: sd === undefined ? "" : asString(sd, "sDescription"),
            color: uc2 === undefined ? 0xffffff : asColor(uc2, "uiColor"),
            comps: cc === undefined ? 1 : asInt(cc, "uiCompCount"),
          });
        }
      }
    }
  }

  // §4.4 frames
  const R = (width * comp * bpc) / 8;
  if (!Number.isSafeInteger(R)) reject("row size too large");
  if (widthBytes < R) reject(`uiWidthBytes ${widthBytes} < ${R}`);
  if (compressed && widthBytes !== R) reject("compressed frames with padded rows");
  let N = 1n;
  for (const l of loops) N *= BigInt(l.count);
  const frames: { f: bigint; o: bigint }[] = [];
  for (const [name, o] of map) {
    const fm = /^ImageDataSeq\|(0|[1-9][0-9]*)!$/.exec(name);
    if (!fm) continue;
    const f = BigInt(fm[1]);
    if (f < N) frames.push({ f, o });
  }
  frames.sort((a, b) => (a.f < b.f ? -1 : a.f > b.f ? 1 : 0));

  type FrameRef = { f: bigint; ranges: Range[][] }; // one range list per chunk (block)
  const frameRefs: FrameRef[] = [];
  let h = height;
  if (frames.length > 0) {
    if (!compressed) {
      const lo = await header(src, frames[0].o);
      const hi = await header(src, frames[frames.length - 1].o);
      if (lo.n !== hi.n) reject("frame header name lengths differ");
      const needD = 8 + height * widthBytes;
      if (lo.d < needD || hi.d < needD) reject("frame data too short");
      const starts = frames.map((fr) => {
        const s = big2num(fr.o, "frame offset") + 16 + lo.n + 8;
        if (!Number.isSafeInteger(s)) reject("frame offset too large");
        return s;
      });
      if (widthBytes === R) {
        frames.forEach((fr, i) => frameRefs.push({ f: fr.f, ranges: [[[starts[i], height * R]]] }));
      } else {
        // largest divisor h of height with every block payload <= 65519
        const maxStart = Math.max(...starts);
        const blockOk = (hh: number) => {
          const b0 = maxStart + (height - hh) * widthBytes; // last block of the frame starting last
          const rs: Range[] = [];
          for (let r = 0; r < hh; r++) rs.push([b0 + r * widthBytes, R]);
          return payloadLen(rs) <= 65519;
        };
        h = 1;
        const divs: number[] = [];
        for (let d = 1; d * d <= height; d++) {
          if (height % d === 0) {
            divs.push(d);
            if (d * d !== height) divs.push(height / d);
          }
        }
        divs.sort((a, b) => b - a);
        for (const d of divs) {
          if (d > 10920) continue; // a row costs at least 6 payload bytes
          if (blockOk(d)) {
            h = d;
            break;
          }
        }
        frames.forEach((fr, i) => {
          const blocks: Range[][] = [];
          for (let j = 0; j < height / h; j++) {
            const rs: Range[] = [];
            for (let r = j * h; r < j * h + h; r++) rs.push([starts[i] + r * widthBytes, R]);
            blocks.push(rs);
          }
          frameRefs.push({ f: fr.f, ranges: blocks });
        });
      }
    } else {
      const hs: Header[] = new Array(frames.length);
      const CONC = 16;
      for (let i = 0; i < frames.length; i += CONC) {
        const batch = frames.slice(i, i + CONC);
        const res = await Promise.all(batch.map((fr) => header(src, fr.o)));
        res.forEach((r, k) => (hs[i + k] = r));
      }
      frames.forEach((fr, i) => {
        const hd = hs[i];
        if (hd.d <= 8) reject("compressed frame without data");
        frameRefs.push({ f: fr.f, ranges: [[[hd.dataOff + 8, hd.d - 8]]] });
      });
    }
  }

  // §4.5 channels
  let channels: { label: string; color: string }[] = [];
  const hex = (c: number) => {
    const r = c & 0xff, g = (c >>> 8) & 0xff, b = (c >>> 16) & 0xff;
    return [r, g, b].map((x) => x.toString(16).toUpperCase().padStart(2, "0")).join("");
  };
  let labeled = planeCount >= 1;
  if (labeled) {
    let sum = 0;
    for (let i = 0; i < planeCount; i++) {
      const pl = planes.get(i);
      if (!pl || (pl.comps !== 1 && pl.comps !== 3)) {
        labeled = false;
        break;
      }
      sum += pl.comps;
    }
    if (labeled && sum !== comp) labeled = false;
  }
  if (labeled) {
    for (let i = 0; i < planeCount; i++) {
      const pl = planes.get(i)!;
      if (pl.comps === 1) channels.push({ label: pl.desc, color: hex(pl.color) });
      else {
        channels.push({ label: `${pl.desc} R`, color: "FF0000" });
        channels.push({ label: `${pl.desc} G`, color: "00FF00" });
        channels.push({ label: `${pl.desc} B`, color: "0000FF" });
      }
    }
  } else {
    channels = Array.from({ length: comp }, (_, k) => ({ label: `C${k}`, color: "FFFFFF" }));
  }

  // §4.6 output
  const tLoop = loops.find((l) => l.kind === "t");
  const zLoop = loops.find((l) => l.kind === "z");
  const pLoop = loops.find((l) => l.kind === "p");
  const nPos = pLoop ? pLoop.count : 1;
  const dims: string[] = [];
  if (tLoop) dims.push("t");
  if (comp > 1) dims.push("c");
  if (zLoop) dims.push("z");
  dims.push("y", "x");
  const shape: number[] = [];
  const chunk: number[] = [];
  const scale: number[] = [];
  const axes = dims.map((d) => {
    switch (d) {
      case "t":
        shape.push(tLoop!.count); chunk.push(1);
        if (tLoop!.val > 0) { scale.push(fin(tLoop!.val / 1000, "t scale")); return axisObj("t", "second"); }
        scale.push(1); return axisObj("t", undefined);
      case "c":
        shape.push(comp); chunk.push(comp); scale.push(1); return axisObj("c", undefined);
      case "z":
        shape.push(zLoop!.count); chunk.push(1);
        if (zLoop!.val > 0) { scale.push(zLoop!.val); return axisObj("z", "micrometer"); }
        scale.push(1); return axisObj("z", undefined);
      case "y":
        shape.push(height); chunk.push(h);
        if (calibrated) { scale.push(fin(cal * aspect, "y scale")); return axisObj("y", "micrometer"); }
        scale.push(1); return axisObj("y", undefined);
      default:
        shape.push(width); chunk.push(width);
        if (calibrated) { scale.push(cal); return axisObj("x", "micrometer"); }
        scale.push(1); return axisObj("x", undefined);
    }
  });
  const codecs: unknown[] = [];
  if (dims.includes("c")) codecs.push(transposeCodec(dims));
  codecs.push(bytesCodec(bpc / 8, true));
  if (compressed) codecs.push({ name: "zlib", configuration: { level: 1 } });
  let b = bpc;
  if (Number.isInteger(bpcSig) && bpcSig >= 1 && bpcSig <= bpc) b = bpcSig;
  const omero = {
    channels: channels.map((c) => {
      const o: Record<string, unknown> = { label: c.label, color: c.color, active: true };
      if (dataType !== "float32") {
        const V = 2 ** b - 1;
        o.window = { min: 0, max: V, start: 0, end: V };
      }
      return o;
    }),
  };

  out.json("zarr.json", groupDoc({ ome: { version: "0.5", "bioformats2raw.layout": 3 } }));
  out.json("OME/zarr.json", groupDoc({ ome: { version: "0.5", series: Array.from({ length: nPos }, (_, p) => String(p)) } }));
  for (let p = 0; p < nPos; p++) {
    out.json(
      `${p}/zarr.json`,
      groupDoc({
        ome: {
          version: "0.5",
          multiscales: [
            {
              name: `position ${p}`,
              axes,
              datasets: [{ path: "0", coordinateTransformations: [{ type: "scale", scale }] }],
            },
          ],
          omero,
        },
      }),
    );
    out.json(`${p}/0/zarr.json`, arrayDoc(shape, dataType, chunk, codecs, dims));
  }
  for (const fr of frameRefs) {
    // row-major coordinates over the loops, last fastest
    let rem = fr.f;
    const idx: Record<string, number> = { t: 0, z: 0, p: 0 };
    for (let i = loops.length - 1; i >= 0; i--) {
      const c = BigInt(loops[i].count);
      idx[loops[i].kind] = Number(rem % c);
      rem /= c;
    }
    const co: number[] = [];
    if (tLoop) co.push(idx.t);
    if (comp > 1) co.push(0);
    if (zLoop) co.push(idx.z);
    fr.ranges.forEach((rs, j) => out.ref(`${idx.p}/0/c/${[...co, j, 0].join("/")}`, rs));
  }
}
