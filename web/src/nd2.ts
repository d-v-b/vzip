// Virtualizing a Nikon ND2 file (format version 3+) by the ND2 profile of
// VIRTUALIZE.md (§4): the frames become Zarr chunks that reference the file.

import { at, decodeLV, type LV, members } from "./lv.ts";
import { encodeConcat, type Range } from "./protobuf.ts";
import type { ByteReader } from "./tiff.ts";
import type { ArchiveDesc, EntryDesc } from "./writer.ts";

export class Nd2Error extends Error {}

const CHUNK_MAGIC = 0x0abeceda;
const FILE_SIGNATURE = "ND2 FILE SIGNATURE CHUNK NAME01!";
const MAP_SIGNATURE = "ND2 CHUNK MAP SIGNATURE 0000001!";
const FILEMAP_NAME = "ND2 FILEMAP SIGNATURE NAME 0001!";
const MAX_PAYLOAD = 65519;

const ascii = (b: Uint8Array) => String.fromCharCode(...b);
const dv = (b: Uint8Array) => new DataView(b.buffer, b.byteOffset, b.byteLength);
const reject = (message: string): never => {
  throw new Nd2Error(message);
};

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
  const v = dv(await read(offset, 16));
  if (v.getUint32(0, true) !== CHUNK_MAGIC) reject(`no ND2 chunk at ${offset}`);
  const nameLength = v.getUint32(4, true);
  const dataLength = Number(v.getBigUint64(8, true));
  if (dataLength > Number.MAX_SAFE_INTEGER) reject("chunk length too large");
  const name = ascii(await read(offset + 16, nameLength)).replace(/\0+$/, "");
  return { nameLength, dataLength, name };
}

const REQUIRED = Symbol("required");

/** A member used as a number (§4.2): types 2-6. */
function num(v: LV | undefined, what: string, fallback: number | typeof REQUIRED = REQUIRED): number {
  if (v === undefined) return fallback === REQUIRED ? reject(`missing ${what}`) : fallback;
  if (typeof v !== "number") reject(`${what} is not a number`);
  return v as number;
}

/** A member used as a flag (§4.2): types 1-5, true if nonzero. */
function flag(v: LV | undefined, what: string): boolean {
  if (v === undefined) return false;
  if (typeof v === "boolean") return v;
  if (typeof v === "number" && Number.isInteger(v)) return v !== 0;
  return reject(`${what} is not a flag`);
}

/** The members of `list` that are valid by `flags` (§4.3). */
function valid(list: LV[], flags: LV | undefined): LV[] {
  if (flags === undefined) return list;
  const f = members(flags);
  return list.filter((_, i) => i < f.length && flag(f[i], "validity entry"));
}

interface Loop {
  kind: "t" | "p" | "z";
  type: number;
  depth: number;
  count: number;
  period: number; // ms
  step: number; // µm
}

/** One experiment node's loop (§4.3), "spectral", or undefined (skipped). */
function nodeLoop(node: LV): Omit<Loop, "depth"> | "spectral" | undefined {
  const type = num(at(node, "eType"), "eType");
  if (![1, 2, 4, 6, 8].includes(type)) reject(`unsupported experiment loop type ${type}`);
  const pars = at(node, "uLoopPars");
  if (pars === undefined) return undefined;
  const loop = (kind: Loop["kind"], count: number, period = 0, step = 0) =>
    count ? { kind, type, count, period, step } : undefined;
  switch (type) {
    case 1:
      return loop("t", num(at(pars, "uiCount"), "uiCount", 0), num(at(pars, "dPeriod"), "dPeriod", 0));
    case 8: {
      const periods = valid(members(at(pars, "pPeriod")), at(pars, "pPeriodValid"));
      const count = periods.reduce<number>((n, p) => n + num(at(p, "uiCount"), "uiCount", 0), 0);
      return loop("t", count, periods.length ? num(at(periods[0], "dPeriod"), "dPeriod", 0) : 0);
    }
    case 2:
      return loop("p", valid(members(at(pars, "Points")), at(node, "pItemValid")).length);
    case 4: {
      const count = num(at(pars, "uiCount"), "uiCount", 0);
      let step = Math.abs(num(at(pars, "dZStep"), "dZStep", 0));
      if (step === 0 && count > 1) {
        step = Math.abs(num(at(pars, "dZHigh"), "dZHigh", 0) - num(at(pars, "dZLow"), "dZLow", 0)) / (count - 1);
      }
      return loop("z", count, 0, step);
    }
    default: {
      const count = at(pars, "uiCount") ?? at(pars, "pPlanes/uiCount");
      return num(count, "uiCount", 0) ? "spectral" : undefined;
    }
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
  if (new Set(kinds).size !== kinds.length) reject(`repeated loop kinds ${kinds.join(", ")}`);
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
    reject("not an ND2 file (bad signature chunk)");
  }
  const version = ascii(await read(48, 64)).match(/^Ver([0-9]+)\./);
  if (version === null || Number(version[1]) < 3) reject(`unsupported ND2 version ${ascii(await read(48, 8))}`);
  if (fileSize < 40) reject("file too short for an ND2 chunk map");
  const tail = await read(fileSize - 40, 40);
  if (ascii(tail.subarray(0, 32)) !== MAP_SIGNATURE) reject("no ND2 chunk map signature");
  const mapOffset = Number(dv(tail).getBigUint64(32, true));
  const mapHeader = await header(read, mapOffset);
  if (mapHeader.name !== FILEMAP_NAME) reject("bad ND2 chunk map chunk");
  const mapData = await read(mapOffset + 16 + mapHeader.nameLength, mapHeader.dataLength);
  const chunks = new Map<string, number>();
  for (let pos = 0; ;) {
    const end = mapData.indexOf(0x21, pos); // "!"
    if (end < 0) reject("unterminated ND2 chunk map");
    const name = ascii(mapData.subarray(pos, end + 1));
    if (name === MAP_SIGNATURE) break;
    if (end + 17 > mapData.length) reject("truncated ND2 chunk map record");
    const offset = dv(mapData).getBigUint64(end + 1, true);
    if (offset > BigInt(Number.MAX_SAFE_INTEGER)) reject("chunk offset too large");
    chunks.set(name, Number(offset));
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
  if (!(attrs instanceof Map)) reject("no ImageAttributesLV! chunk");
  const width = num(at(attrs, "uiWidth"), "uiWidth");
  const height = num(at(attrs, "uiHeight"), "uiHeight");
  const widthBytes = num(at(attrs, "uiWidthBytes"), "uiWidthBytes");
  const comp = num(at(attrs, "uiComp"), "uiComp");
  const bpc = num(at(attrs, "uiBpcInMemory"), "uiBpcInMemory");
  const significant = num(at(attrs, "uiBpcSignificant"), "uiBpcSignificant");
  const compression = num(at(attrs, "eCompression"), "eCompression", 2);
  if (Math.min(width, height, comp) < 1) reject("image width, height and components must be at least 1");
  const dataType = ({ 8: "uint8", 16: "uint16", 32: "float32" } as Record<number, string>)[bpc];
  if (dataType === undefined) reject(`unsupported bits per component ${bpc}`);
  if (compression === 1) reject("lossy ND2 compression is not supported");
  if (compression !== 0 && compression !== 2) reject(`unknown ND2 compression ${compression}`);
  for (const [k, full] of [["uiTileWidth", width], ["uiTileHeight", height]] as const) {
    const tile = num(at(attrs, k), k, 0);
    if (tile > 0 && tile !== full) reject("tiled ND2 frames are not supported");
  }
  const compressed = compression === 0;
  const rowBytes = (width * comp * bpc) / 8;
  if (widthBytes < rowBytes) reject("uiWidthBytes is less than a row");
  if (compressed && widthBytes !== rowBytes) reject("compressed frames with padded rows are not supported");

  const exp = await chunk("ImageMetadataLV!");
  const loops = flattenExperiment(exp === undefined ? undefined : at(exp, "SLxExperiment"));
  const picture = at(await chunk("ImageMetadataSeqLV|0!"), "SLxPictureMetadata");

  // §4.5: channels.
  const planeCount = num(at(picture, "sPicturePlanes/uiCount"), "uiCount", 0);
  const planes = Array.from({ length: planeCount }, (_, i) => at(picture, `sPicturePlanes/sPlaneNew/a${i}`));
  let labels: string[] = [];
  let colors: string[] = [];
  const counts = planes.map((p) => (p === undefined ? 0 : num(at(p, "uiCompCount"), "uiCompCount", 1)));
  if (
    planes.length > 0 && planes.every((p) => p !== undefined) &&
    counts.every((k) => k === 1 || k === 3) && counts.reduce((a, b) => a + b, 0) === comp
  ) {
    planes.forEach((p, i) => {
      const desc = at(p, "sDescription") ?? "";
      if (typeof desc !== "string") reject("sDescription is not a string");
      const name = desc as string;
      if (counts[i] === 3) {
        labels.push(`${name} R`, `${name} G`, `${name} B`);
        colors.push("FF0000", "00FF00", "0000FF");
      } else {
        labels.push(name);
        colors.push(hexColor(num(at(p, "uiColor"), "uiColor", 0xffffff)));
      }
    });
  } else {
    labels = Array.from({ length: comp }, (_, k) => `C${k}`);
    colors = labels.map(() => "FFFFFF");
  }

  // §4.4: frames.
  const total = loops.reduce((n, l) => n * l.count, 1);
  const frameOffsets: (number | undefined)[] = Array.from({ length: total }, (_, f) => chunks.get(`ImageDataSeq|${f}!`));
  const present = frameOffsets.flatMap((o, f) => (o === undefined ? [] : [f]));
  let ranges: (f: number) => Promise<Range[]>;
  if (compressed) {
    ranges = async (f) => {
      const o = frameOffsets[f]!;
      const h = await header(read, o);
      if (h.dataLength <= 8) reject(`compressed frame ${f} has no data`);
      return [{ source: 0, offset: BigInt(o + 16 + h.nameLength + 8), length: BigInt(h.dataLength - 8) }];
    };
  } else {
    let nameLength = 0;
    if (present.length > 0) {
      const first = await header(read, frameOffsets[present[0]]!);
      const last = await header(read, frameOffsets[present[present.length - 1]]!);
      if (first.nameLength !== last.nameLength) reject("frame chunk headers differ in name length");
      if (Math.min(first.dataLength, last.dataLength) < 8 + height * widthBytes) {
        reject("frame chunk too short for its pixels");
      }
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
  const cal = num(at(picture, "dCalibration"), "dCalibration", 0);
  const calibrated = flag(at(picture, "bCalibrated"), "bCalibrated") && cal > 0;
  let aspect = num(at(picture, "dAspect"), "dAspect", 1);
  if (!(aspect > 0)) aspect = 1;
  const period = t && t.period > 0 ? t.period : undefined;
  const step = z && z.step > 0 ? z.step : undefined;
  const scale: Record<string, number> = {
    t: period ? period / 1000 : 1,
    c: 1,
    z: step ?? 1,
    y: calibrated ? cal * aspect : 1,
    x: calibrated ? cal : 1,
  };
  if (Object.values(scale).some((v) => !Number.isFinite(v))) reject("a scale is not finite");
  const unit: Record<string, string | undefined> = {
    t: period ? "second" : undefined,
    z: step ? "micrometer" : undefined,
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
  const b = significant >= 1 && significant <= bpc ? significant : bpc;
  const window = dataType === "float32" ? {} : { window: { min: 0, max: 2 ** b - 1, start: 0, end: 2 ** b - 1 } };
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
    const r = await ranges(f);
    for (const { offset, length } of r as { offset: bigint; length: bigint }[]) {
      if (offset + length > BigInt(fileSize)) reject(`frame ${f} is outside the file`);
    }
    if (r.length > 1 && encodeConcat(r).length > MAX_PAYLOAD) {
      reject(`frame ${f}'s reference payload exceeds ${MAX_PAYLOAD} bytes`);
    }
    const index = axes.map((a) => (a === "t" || a === "z" ? coords[a] ?? 0 : 0));
    return { key: `${coords.p ?? 0}/0/c/${index.join("/")}`, ranges: r };
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
      dataType: dataType as string, compressed, paddedRows: widthBytes !== rowBytes, positions,
      frames: total, missing: total - present.length, channels: labels,
    },
  };
}
