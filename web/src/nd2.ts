// Virtualizing a Nikon ND2 file (format version 3+) by the ND2 profile of
// VIRTUALIZE.md (§4): the frames become Zarr chunks that reference the file.

import { at, decodeLV, type LV, members } from "./lv.ts";
import type { Range } from "./protobuf.ts";
import type { ByteReader } from "./tiff.ts";
import type { ArchiveDesc, EntryDesc } from "./writer.ts";

export class Nd2Error extends Error {}

const CHUNK_MAGIC = 0x0abeceda;
const FILE_SIGNATURE = "ND2 FILE SIGNATURE CHUNK NAME01!";
const MAP_SIGNATURE = "ND2 CHUNK MAP SIGNATURE 0000001!";
const FILEMAP_NAME = "ND2 FILEMAP SIGNATURE NAME 0001!";

const ascii = (b: Uint8Array) => String.fromCharCode(...b);
const dv = (b: Uint8Array) => new DataView(b.buffer, b.byteOffset, b.byteLength);

/** True if `head` (the file's first bytes) looks like an ND2 file. */
export function isNd2(head: Uint8Array): boolean {
  return head.length >= 4 && dv(head).getUint32(0, true) === CHUNK_MAGIC;
}

interface Header {
  nameLength: number;
  dataLength: number;
  name: string;
}

async function header(read: ByteReader, offset: number): Promise<Header> {
  const h = await read(offset, 16);
  const v = dv(h);
  if (v.getUint32(0, true) !== CHUNK_MAGIC) throw new Nd2Error(`no ND2 chunk at ${offset}`);
  const nameLength = v.getUint32(4, true);
  const dataLength = Number(v.getBigUint64(8, true));
  const name = ascii(await read(offset + 16, nameLength)).replace(/\0+$/, "");
  return { nameLength, dataLength, name };
}

const num = (v: LV | undefined, fallback = 0): number =>
  typeof v === "number" ? v : typeof v === "boolean" ? Number(v) : fallback;

interface Loop {
  kind: "t" | "p" | "z";
  type: number;
  depth: number;
  count: number;
  period?: number; // ms
  step?: number; // µm
}

/** One experiment node's loop (§4.3), or "spectral", or undefined (no count). */
function nodeLoop(node: LV): Omit<Loop, "depth"> | "spectral" | undefined {
  const type = num(at(node, "eType"));
  const pars = at(node, "uLoopPars");
  if (pars === undefined) return undefined;
  switch (type) {
    case 1: {
      const count = num(at(pars, "uiCount"));
      return count ? { kind: "t", type, count, period: num(at(pars, "dPeriod")) } : undefined;
    }
    case 8: {
      const valid = members(at(pars, "pPeriodValid"));
      let count = 0;
      let period: number | undefined;
      members(at(pars, "pPeriod")).forEach((p, i) => {
        if (!num(valid[i])) return;
        count += num(at(p, "uiCount"));
        period ??= num(at(p, "dPeriod"));
      });
      return count ? { kind: "t", type, count, period } : undefined;
    }
    case 2: {
      const points = members(at(pars, "Points"));
      const valid = at(node, "pItemValid");
      const count = valid === undefined ? points.length : points.filter((_, i) => num(members(valid)[i])).length;
      return count ? { kind: "p", type, count } : undefined;
    }
    case 4: {
      const count = num(at(pars, "uiCount"));
      let step = Math.abs(num(at(pars, "dZStep")));
      if (step === 0 && count > 1) step = Math.abs(num(at(pars, "dZHigh")) - num(at(pars, "dZLow"))) / (count - 1);
      return count ? { kind: "z", type, count, step } : undefined;
    }
    case 6: {
      const count = num(at(pars, "uiCount"), num(at(pars, "pPlanes/uiCount")));
      return count ? "spectral" : undefined;
    }
    default:
      throw new Nd2Error(`unsupported experiment loop type ${type}`);
  }
}

/** Flattens the experiment tree into loops (§4.3). */
export function flattenExperiment(root: LV | undefined): Loop[] {
  const loops: Loop[] = [];
  const visit = (node: LV, depth: number) => {
    const loop = nodeLoop(node);
    if (loop === undefined) return;
    let childDepth = depth + 1;
    if (loop === "spectral") {
      childDepth = depth;
    } else {
      const last = loops[loops.length - 1];
      if (last === undefined || last.depth < depth) loops.push({ ...loop, depth });
      else if (last.depth === depth && last.type === loop.type && last.count < loop.count) {
        loops[loops.length - 1] = { ...loop, depth };
      }
    }
    for (const child of members(at(node, "ppNextLevelEx"))) visit(child, childDepth);
  };
  if (root !== undefined) visit(root, 0);
  const kinds = loops.map((l) => l.kind);
  if (new Set(kinds).size !== kinds.length) throw new Nd2Error(`repeated loop kinds ${kinds.join(", ")}`);
  return loops;
}

function hexColor(abgr: number): string {
  const h = (v: number) => v.toString(16).toUpperCase().padStart(2, "0");
  return h(abgr & 255) + h((abgr >>> 8) & 255) + h((abgr >>> 16) & 255);
}

export interface Nd2Summary {
  sizes: Record<string, number>;
  dataType: string;
  compressed: boolean;
  paddedRows: boolean;
  positions: number;
  frames: number;
  missing: number;
  channels: string[];
}

/** Describes the ND2 file at `url` (read through `read`) by the ND2 profile. */
export async function virtualizeNd2(
  url: string,
  read: ByteReader,
  fileSize: number,
): Promise<ArchiveDesc & { summary: Nd2Summary }> {
  // §4.1: signature and chunk map.
  const sig = await header(read, 0);
  if (sig.name !== FILE_SIGNATURE || sig.nameLength !== 32 || sig.dataLength !== 64) {
    throw new Nd2Error("not an ND2 file (bad signature chunk)");
  }
  const version = ascii(await read(48, 6));
  const major = Number(version[3]);
  if (!version.startsWith("Ver") || !(major >= 3)) throw new Nd2Error(`unsupported ND2 version ${version}`);
  const tail = await read(fileSize - 40, 40);
  if (ascii(tail.subarray(0, 32)) !== MAP_SIGNATURE) throw new Nd2Error("no ND2 chunk map signature");
  const mapOffset = Number(dv(tail).getBigUint64(32, true));
  const mapHeader = await header(read, mapOffset);
  if (mapHeader.name !== FILEMAP_NAME) throw new Nd2Error("bad ND2 chunk map chunk");
  const mapData = await read(mapOffset + 16 + mapHeader.nameLength, mapHeader.dataLength);
  const chunks = new Map<string, number>();
  for (let pos = 0; ;) {
    const end = mapData.indexOf(0x21, pos); // "!"
    if (end < 0) throw new Nd2Error("unterminated ND2 chunk map");
    const name = ascii(mapData.subarray(pos, end + 1));
    if (name === MAP_SIGNATURE) break;
    chunks.set(name, Number(dv(mapData).getBigUint64(end + 1, true)));
    pos = end + 17;
  }
  const chunk = async (name: string) => {
    const offset = chunks.get(name);
    if (offset === undefined) return undefined;
    const h = await header(read, offset);
    return decodeLV(await read(offset + 16 + h.nameLength, h.dataLength));
  };

  // §4.3: metadata.
  const attrs = at(await chunk("ImageAttributesLV!"), "SLxImageAttributes");
  if (attrs === undefined) throw new Nd2Error("no ImageAttributesLV! chunk");
  const width = num(at(attrs, "uiWidth"));
  const height = num(at(attrs, "uiHeight"));
  const widthBytes = num(at(attrs, "uiWidthBytes"));
  const comp = num(at(attrs, "uiComp"));
  const bpc = num(at(attrs, "uiBpcInMemory"));
  const significant = num(at(attrs, "uiBpcSignificant"));
  const compression = num(at(attrs, "eCompression"), 2);
  const dataType = { 8: "uint8", 16: "uint16", 32: "float32" }[bpc];
  if (dataType === undefined) throw new Nd2Error(`unsupported bits per component ${bpc}`);
  if (compression === 1) throw new Nd2Error("lossy ND2 compression is not supported");
  if (compression !== 0 && compression !== 2) throw new Nd2Error(`unknown ND2 compression ${compression}`);
  for (const [k, full] of [["uiTileWidth", width], ["uiTileHeight", height]] as const) {
    const tile = num(at(attrs, k));
    if (tile > 0 && tile !== full) throw new Nd2Error("tiled ND2 frames are not supported");
  }
  const compressed = compression === 0;
  const rowBytes = (width * comp * bpc) / 8;
  if (compressed && widthBytes !== rowBytes) throw new Nd2Error("compressed frames with padded rows are not supported");

  const loops = flattenExperiment(at(await chunk("ImageMetadataLV!"), "SLxExperiment"));
  const picture = at(await chunk("ImageMetadataSeqLV|0!"), "SLxPictureMetadata");

  // §4.5: channels.
  const planes: LV[] = [];
  const planeCount = num(at(picture, "sPicturePlanes/uiCount"));
  for (let i = 0; i < planeCount; i++) planes.push(at(picture, `sPicturePlanes/sPlaneNew/a${i}`) ?? {});
  const compCounts = planes.map((p) => num(at(p, "uiCompCount"), 1));
  let labels: string[] = [];
  let colors: string[] = [];
  if (planes.length > 0 && compCounts.reduce((a, b) => a + b, 0) === comp) {
    planes.forEach((p, i) => {
      const name = String(at(p, "sDescription") ?? "");
      if (compCounts[i] === 3) {
        labels.push(`${name} R`, `${name} G`, `${name} B`);
        colors.push("FF0000", "00FF00", "0000FF");
      } else {
        labels.push(name);
        colors.push(hexColor(num(at(p, "uiColor"))));
      }
    });
  }
  if (labels.length !== comp) {
    labels = Array.from({ length: comp }, (_, k) => `C${k}`);
    colors = labels.map(() => "FFFFFF");
  }

  // §4.4: frames.
  const counts = loops.map((l) => l.count);
  const total = counts.reduce((a, b) => a * b, 1);
  const frameOffsets: (number | undefined)[] = Array.from({ length: total }, (_, f) => chunks.get(`ImageDataSeq|${f}!`));
  const present = frameOffsets.flatMap((o, f) => (o === undefined ? [] : [f]));
  let ranges: (f: number) => Promise<Range[]>;
  if (compressed) {
    ranges = async (f) => {
      const o = frameOffsets[f]!;
      const h = await header(read, o);
      return [{ source: 0, offset: BigInt(o + 16 + h.nameLength + 8), length: BigInt(h.dataLength - 8) }];
    };
  } else {
    let nameLength = 0;
    if (present.length > 0) {
      const first = await header(read, frameOffsets[present[0]]!);
      const last = await header(read, frameOffsets[present[present.length - 1]]!);
      if (first.nameLength !== last.nameLength) throw new Nd2Error("frame chunk headers differ in name length");
      nameLength = first.nameLength;
    }
    ranges = async (f) => {
      const start = frameOffsets[f]! + 16 + nameLength + 8;
      if (widthBytes === rowBytes) return [{ source: 0, offset: BigInt(start), length: BigInt(height * rowBytes) }];
      return Array.from({ length: height }, (_, r) => ({
        source: 0, offset: BigInt(start + r * widthBytes), length: BigInt(rowBytes),
      }));
    };
  }

  // §4.6: output.
  const loopOf = (kind: string) => loops.find((l) => l.kind === kind);
  const t = loopOf("t");
  const z = loopOf("z");
  const p = loopOf("p");
  const axes: string[] = [];
  if (t) axes.push("t");
  if (comp > 1) axes.push("c");
  if (z) axes.push("z");
  axes.push("y", "x");
  const size: Record<string, number> = { t: t?.count ?? 1, c: comp, z: z?.count ?? 1, y: height, x: width };
  const chunkSize: Record<string, number> = { t: 1, c: comp, z: 1, y: height, x: width };
  const calibrated = at(picture, "bCalibrated") === true && num(at(picture, "dCalibration")) > 0;
  const cal = num(at(picture, "dCalibration"));
  const scale: Record<string, number> = {
    t: t?.period && t.period > 0 ? t.period / 1000 : 1,
    c: 1,
    z: z?.step && z.step > 0 ? z.step : 1,
    y: calibrated ? cal * num(at(picture, "dAspect"), 1) : 1,
    x: calibrated ? cal : 1,
  };
  const unit: Record<string, string | undefined> = {
    t: t?.period && t.period > 0 ? "second" : undefined,
    z: z?.step && z.step > 0 ? "micrometer" : undefined,
    y: calibrated ? "micrometer" : undefined,
    x: calibrated ? "micrometer" : undefined,
  };
  const type: Record<string, string> = { t: "time", c: "channel", z: "space", y: "space", x: "space" };
  const codecs: unknown[] = [];
  if (comp > 1) {
    const stored = axes.filter((a) => a !== "c").concat("c");
    codecs.push({ name: "transpose", configuration: { order: stored.map((a) => axes.indexOf(a)) } });
  }
  codecs.push(bpc > 8 ? { name: "bytes", configuration: { endian: "little" } } : { name: "bytes" });
  if (compressed) codecs.push({ name: "zlib", configuration: { level: 1 } });

  const utf8 = new TextEncoder();
  const json = (v: unknown) => utf8.encode(JSON.stringify(v, null, 2));
  const group = (attributes: unknown) => json({ zarr_format: 3, node_type: "group", attributes });
  const positions = p?.count ?? 1;
  const entries: EntryDesc[] = [
    { key: "zarr.json", bytes: group({ ome: { version: "0.5", "bioformats2raw.layout": 3 } }) },
    { key: "OME/zarr.json", bytes: group({ ome: { version: "0.5", series: Array.from({ length: positions }, (_, i) => String(i)) } }) },
  ];
  const window = dataType === "float32" ? {} : (() => {
    const v = 2 ** significant - 1;
    return { window: { min: 0, max: v, start: 0, end: v } };
  })();
  for (let pi = 0; pi < positions; pi++) {
    entries.push({
      key: `${pi}/zarr.json`,
      bytes: group({
        ome: {
          version: "0.5",
          multiscales: [{
            name: `position ${pi}`,
            axes: axes.map((a) => ({ name: a, type: type[a], ...(unit[a] ? { unit: unit[a] } : {}) })),
            datasets: [{ path: "0", coordinateTransformations: [{ type: "scale", scale: axes.map((a) => scale[a]) }] }],
          }],
          omero: {
            channels: labels.map((label, k) => ({ label, color: colors[k], active: true, ...window })),
          },
        },
      }),
    });
    entries.push({
      key: `${pi}/0/zarr.json`,
      bytes: json({
        zarr_format: 3,
        node_type: "array",
        shape: axes.map((a) => size[a]),
        data_type: dataType,
        chunk_grid: { name: "regular", configuration: { chunk_shape: axes.map((a) => chunkSize[a]) } },
        chunk_key_encoding: { name: "default", configuration: { separator: "/" } },
        fill_value: 0,
        codecs,
        dimension_names: axes,
        attributes: {},
      }),
    });
  }
  // Row-major coordinates of each frame over the loops.
  const refs = await Promise.all(present.map(async (f) => {
    const coords: Record<string, number> = {};
    let rest = f;
    for (let i = loops.length - 1; i >= 0; i--) {
      coords[loops[i].kind] = rest % loops[i].count;
      rest = Math.floor(rest / loops[i].count);
    }
    const index = axes.map((a) => (a === "t" || a === "z" ? coords[a] ?? 0 : 0));
    return { key: `${coords.p ?? 0}/0/c/${index.join("/")}`, ranges: await ranges(f) };
  }));
  entries.unshift(...refs);
  return {
    sources: [{ url }],
    entries,
    summary: {
      sizes: Object.fromEntries([
        ...loops.map((l): [string, number] => [l.kind, l.count]),
        ["c", comp], ["y", height], ["x", width],
      ]),
      dataType, compressed, paddedRows: widthBytes !== rowBytes, positions,
      frames: total, missing: total - present.length, channels: labels,
    },
  };
}
