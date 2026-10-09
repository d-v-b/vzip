// Virtualizing a Nikon ND2 file (format version 3+) by the ND2 profile
// (spec/virtualize/nd2/profile.md, §5): the frames become Zarr chunks that reference the file.

import { REVISION } from "../revision.ts";
import { type ByteReader, declare, emitPlans, jsonText, MAX_PAYLOAD, payloadSize, Prefetched, stringifyJson } from "../../../../../js/src/virtualize/common.ts";
import { sourceMetadata } from "./source.ts";
import { decodeLV, type LV, type LVObject, type Scalar, TooLargeError } from "./lv.ts";
import type { Range } from "../../../../../js/src/protobuf.ts";
import type { ArchiveDesc, EntryDesc } from "../../../../../js/src/writer.ts";

export class Nd2Error extends Error {}

const CHUNK_MAGIC = 0x0abeceda;
const FILE_SIGNATURE = "ND2 FILE SIGNATURE CHUNK NAME01!";
const MAP_SIGNATURE = "ND2 CHUNK MAP SIGNATURE 0000001!";
const FILEMAP_NAME = "ND2 FILEMAP SIGNATURE NAME 0001!";
const FRAME = /^ImageDataSeq\|(0|[1-9][0-9]*)!$/;
const MAX_INFLATE = 2 ** 26; // the most a chunk the profile reads may inflate to (spec/virtualize/nd2/profile.md §5.1)
const MAX_RECORDS = 2 ** 20; // the most LV records and byte-array bytes of the chunks the profile reads (§5.1)
const MAX_POSITIONS = 2 ** 16; // the most positions (spec/virtualize/nd2/profile.md §5.3)
const MAX_POSITION_CHANNELS = 2 ** 20; // the most positions x components (spec/virtualize/nd2/profile.md §5.3)
const FRAME_HEAD = 16 + 4096; // the bytes prefetched at each frame chunk: its header and a padded name
const FRAME_HEADS_IN_FLIGHT = 32; // as the Python reader's prefetch

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
  if (offset > Number.MAX_SAFE_INTEGER) reject(`chunk offset ${offset} is too large`);
  const v = dv(await read(offset, 16));
  if (v.getUint32(0, true) !== CHUNK_MAGIC) reject(`no ND2 chunk at ${offset}`);
  const nameLength = v.getUint32(4, true);
  const dataLength = Number(v.getBigUint64(8, true));
  if (dataLength > Number.MAX_SAFE_INTEGER) reject("chunk length too large");
  const name = ascii(await read(offset + 16, nameLength)).split("\0", 1)[0];
  return { nameLength, dataLength, name };
}

// ---- typed member access (spec/virtualize/nd2.md §2.2)

const REQUIRED = Symbol("required");
type Fallback<T> = T | typeof REQUIRED;

const isScalar = (v: LV): v is Scalar => !Array.isArray(v) && !(v instanceof Map) && !(v instanceof Uint8Array);
/** A byte array as a list: each byte a value of type 3. */
const byteList = (v: Uint8Array): LV[] => Array.from(v, (b): LV => ({ type: 3, value: b }));

function missing<T>(what: string, fallback: Fallback<T>): T {
  return fallback === REQUIRED ? reject(`missing ${what}`) : fallback;
}

function number<T = number>(v: LV | undefined, what: string, fallback: Fallback<T> = REQUIRED): number | T {
  if (v === undefined) return missing(what, fallback);
  if (!isScalar(v) || v.type < 2 || v.type > 6) return reject(`${what} is not a number`);
  const x = Number(v.value); // a bigint rounds to the nearest binary64
  if (!Number.isFinite(x)) reject(`${what} is not finite`);
  return x;
}

function integer<T = number>(v: LV | undefined, what: string, fallback: Fallback<T> = REQUIRED): number | T {
  if (v === undefined) return missing(what, fallback);
  const x = number(v, what);
  if (!Number.isInteger(x) || x < 0 || x > Number.MAX_SAFE_INTEGER) {
    reject(`${what} = ${x} is not an integer from 0 to 2^53 - 1`);
  }
  return x;
}

function color(v: LV | undefined, what: string, fallback: number): number {
  if (v === undefined) return fallback;
  const x = number(v, what);
  if (!Number.isInteger(x) || x < -(2 ** 31) || x > 2 ** 32 - 1) reject(`${what} = ${x} is not a color`);
  return x >>> 0; // modulo 2^32
}

function flag(v: LV | undefined, what: string, fallback: Fallback<boolean> = REQUIRED): boolean {
  if (v === undefined) return missing(what, fallback);
  if (!isScalar(v) || v.type < 1 || v.type > 5) return reject(`${what} is not a flag`);
  return v.value !== 0 && v.value !== 0n && v.value !== false;
}

function string(v: LV | undefined, what: string, fallback: string): string {
  if (v === undefined) return fallback;
  if (!isScalar(v) || v.type !== 8) return reject(`${what} is not a string`);
  return v.value as string;
}

function obj<T = LVObject>(v: LV | undefined, what: string, fallback: Fallback<T> = REQUIRED): LVObject | T {
  if (v === undefined) return missing(what, fallback);
  if (!(v instanceof Map)) return reject(`${what} is not an object`);
  return v;
}

function list(v: LV | undefined, what: string): LV[] | undefined {
  if (v === undefined) return undefined;
  if (v instanceof Uint8Array) return byteList(v);
  if (!Array.isArray(v)) return reject(`${what} is not a list`);
  return v;
}

/** The members of an object or a list (default: none). */
function members(v: LV | undefined, what: string): LV[] {
  if (v === undefined) return [];
  if (Array.isArray(v)) return v;
  if (v instanceof Uint8Array) return byteList(v);
  if (v instanceof Map) return [...v.values()];
  return reject(`${what} is not an object or a list`);
}

/** The members of `items` that are valid by the validity list `flags` (spec/virtualize/nd2.md §3). */
function valid<T>(items: T[], flags: LV | undefined, what: string): T[] {
  const f = list(flags, what)?.map((x) => flag(x, `${what} entry`));
  return f === undefined ? items : items.filter((_, i) => i < f.length && f[i]);
}

// ---- experiment (spec/virtualize/nd2.md §3)

interface Loop {
  kind: "t" | "p" | "z";
  depth: number;
  count: number;
  scale: number; // period (ms) or step (µm)
  /** For z: a negative step, so the z index is flipped (§4.3). */
  flip?: boolean;
  /** For positions: each valid point's stage position (µm), or null if absent. */
  stage?: [number | null, number | null][];
}

/** One experiment node's loop, "spectral", or undefined (skipped). */
function nodeLoop(node: LVObject): Omit<Loop, "depth"> | "spectral" | undefined {
  const type = integer(node.get("eType"), "eType");
  if (![1, 2, 4, 6, 8].includes(type)) reject(`unsupported experiment loop type ${type}`);
  const pars = obj(node.get("uLoopPars"), "uLoopPars", null);
  const itemValid = list(node.get("pItemValid"), "pItemValid");
  for (const x of itemValid ?? []) flag(x, "pItemValid entry");
  if (pars === null) return undefined;
  let loop: Omit<Loop, "depth"> | "spectral";
  let count: number;
  switch (type) {
    case 1:
      count = integer(pars.get("uiCount"), "uiCount", 0);
      loop = { kind: "t", count, scale: number(pars.get("dPeriod"), "dPeriod", 0) };
      break;
    case 8: {
      const periods = members(pars.get("pPeriod"), "pPeriod").map((p) => obj(p, "pPeriod member"));
      const ok = valid(periods, pars.get("pPeriodValid"), "pPeriodValid");
      count = ok.reduce<number>((n, p) => n + integer(p.get("uiCount"), "uiCount"), 0);
      if (count > Number.MAX_SAFE_INTEGER) reject("the time loop's count is more than 2^53 - 1");
      const ms = ok.map((p) => number(p.get("dPeriod"), "dPeriod", 0));
      // One period only when every valid phase has it (else no time step).
      loop = { kind: "t", count, scale: ms.length && ms.every((p) => p === ms[0]) ? ms[0] : 0 };
      break;
    }
    case 2: {
      const points = valid(members(pars.get("Points"), "Points"), itemValid, "pItemValid")
        .map((q) => obj(q, "Points member"));
      count = points.length;
      const stage = points.map((q): [number | null, number | null] =>
        [number(q.get("dPosX"), "dPosX", null), number(q.get("dPosY"), "dPosY", null)]);
      loop = { kind: "p", count, scale: 0, stage };
      break;
    }
    case 4: {
      count = integer(pars.get("uiCount"), "uiCount", 0);
      let step = Math.abs(number(pars.get("dZStep"), "dZStep", 0));
      const high = number(pars.get("dZHigh"), "dZHigh", 0);
      const low = number(pars.get("dZLow"), "dZLow", 0);
      if (step === 0 && count > 1) step = Math.abs(high - low) / (count - 1);
      if (!Number.isFinite(step)) reject("the z step is not finite");
      // A negative step: the stage moves down, and the z index is flipped (§4.3).
      loop = { kind: "z", count, scale: step, flip: number(pars.get("dZStep"), "dZStep", 0) < 0 };
      break;
    }
    default: {
      const own = integer(pars.get("uiCount"), "uiCount", null);
      if (own !== null) count = own;
      else {
        const planes = obj(pars.get("pPlanes"), "pPlanes", null);
        count = planes === null ? 0 : integer(planes.get("uiCount"), "pPlanes/uiCount", 0);
      }
      loop = "spectral";
    }
  }
  return count ? loop : undefined;
}

/** Flattens the experiment tree into loops (spec/virtualize/nd2.md §3). */
export function flattenExperiment(root: LV | undefined): Loop[] {
  const loops: Loop[] = [];
  const visit = (node: LVObject, depth: number) => {
    const loop = nodeLoop(node);
    if (loop === undefined) return;
    let childDepth = depth + 1;
    if (loop === "spectral") {
      childDepth = depth;
    } else {
      const last = loops[loops.length - 1];
      if (last === undefined || last.depth < depth) loops.push({ ...loop, depth });
      else if (last.depth === depth && last.kind === loop.kind && last.count < loop.count) {
        loops[loops.length - 1] = { ...loop, depth };
      }
    }
    for (const child of members(node.get("ppNextLevelEx"), "ppNextLevelEx")) {
      visit(obj(child, "experiment node"), childDepth);
    }
  };
  // Every node is checked, whether or not the flattening visits it.
  const check = (node: LVObject) => {
    nodeLoop(node);
    for (const child of members(node.get("ppNextLevelEx"), "ppNextLevelEx")) check(obj(child, "experiment node"));
  };
  if (root !== undefined) {
    check(obj(root, "SLxExperiment"));
    visit(obj(root, "SLxExperiment"), 0);
  }
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
  rowBlock: number;
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
  exact?: ByteReader,
): Promise<ArchiveDesc & { summary: Nd2Summary }> {
  // §5.1: signature and chunk map.
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
    // Offsets beyond 2^53 are rejected when the chunk is read (`header`).
    chunks.set(name, Number(dv(mapData).getBigUint64(end + 1, true)));
    pos = end + 17;
  }
  const room: [number] = [MAX_RECORDS]; // shared by the three chunks (spec/virtualize/nd2/profile.md §5.1)
  const chunk = async (name: string) => {
    const offset = chunks.get(name);
    if (offset === undefined) return undefined;
    const h = await header(read, offset);
    try {
      return await decodeLV(await read(offset + 16 + h.nameLength, h.dataLength), MAX_INFLATE, undefined, false, room);
    } catch (e) {
      if (e instanceof TooLargeError) {
        reject(`the profile's chunks hold more than ${MAX_RECORDS} LV records and byte-array bytes`);
      }
      throw e;
    }
  };

  // spec/virtualize/nd2.md §3: attributes.
  const attributes = await chunk("ImageAttributesLV!");
  if (attributes === undefined) return reject("no ImageAttributesLV! chunk");
  const attrs = obj(attributes.get("SLxImageAttributes"), "SLxImageAttributes");
  const width = integer(attrs.get("uiWidth"), "uiWidth");
  const height = integer(attrs.get("uiHeight"), "uiHeight");
  const widthBytes = integer(attrs.get("uiWidthBytes"), "uiWidthBytes");
  const comp = integer(attrs.get("uiComp"), "uiComp");
  const bpc = integer(attrs.get("uiBpcInMemory"), "uiBpcInMemory");
  const significant = number(attrs.get("uiBpcSignificant"), "uiBpcSignificant");
  const compression = integer(attrs.get("eCompression"), "eCompression", 2);
  const tileWidth = integer(attrs.get("uiTileWidth"), "uiTileWidth", 0);
  const tileHeight = integer(attrs.get("uiTileHeight"), "uiTileHeight", 0);
  if (Math.min(width, height, comp) < 1 || comp > 1024) {
    reject("image width and height must be at least 1, and components from 1 to 1024");
  }
  const dataType = ({ 8: "uint8", 16: "uint16", 32: "float32" } as Record<number, string>)[bpc];
  if (dataType === undefined) reject(`unsupported bits per component ${bpc}`);
  if (compression === 1) reject("lossy ND2 compression is not supported");
  if (compression !== 0 && compression !== 2) reject(`unknown ND2 compression ${compression}`);
  if ((tileWidth > 0 && tileWidth !== width) || (tileHeight > 0 && tileHeight !== height)) {
    reject("tiled ND2 frames are not supported");
  }
  const compressed = compression === 0;
  const rowBytes = (width * comp * bpc) / 8;
  if (widthBytes < rowBytes) reject("uiWidthBytes is less than a row");
  if (compressed && widthBytes !== rowBytes) reject("compressed frames with padded rows are not supported");

  // spec/virtualize/nd2.md §3: experiment.
  const exp = await chunk("ImageMetadataLV!");
  const loops = flattenExperiment(exp?.get("SLxExperiment"));
  const positions = loops.find((l) => l.kind === "p")?.count ?? 1;
  if (positions > MAX_POSITIONS || positions * comp > MAX_POSITION_CHANNELS) {
    reject(`${positions} positions of ${comp} components: more than ${MAX_POSITIONS} positions, ` +
      `or more than ${MAX_POSITION_CHANNELS} positions x components`);
  }

  // spec/virtualize/nd2.md §3: picture metadata.
  const seq = await chunk("ImageMetadataSeqLV|0!");
  const picture: LVObject = (seq && obj(seq.get("SLxPictureMetadata"), "SLxPictureMetadata", null)) ??
    new Map();
  const bCalibrated = flag(picture.get("bCalibrated"), "bCalibrated", false);
  const cal = number(picture.get("dCalibration"), "dCalibration", null);
  let aspect = number(picture.get("dAspect"), "dAspect", 1);
  const [m11, m12, m21, m22] = ([["11", 1], ["12", 0], ["21", 0], ["22", 1]] as const)
    .map(([k, fallback]) => number(picture.get(`dStgLgCT${k}`), `dStgLgCT${k}`, fallback));
  // The stage position without a position loop (spec/virtualize/nd2.md §4.3).
  const pictureStage: [number | null, number | null] = [
    number(picture.get("dXPos"), "dXPos", null),
    number(picture.get("dYPos"), "dYPos", null),
  ];
  const calibrated = bCalibrated && cal !== null && cal > 0;
  if (!(aspect > 0)) aspect = 1;
  const pp: LVObject = obj(picture.get("sPicturePlanes"), "sPicturePlanes", null) ?? new Map();
  const planeCount = integer(pp.get("uiCount"), "uiCount", 0);
  const planeNew: LVObject = obj(pp.get("sPlaneNew"), "sPlaneNew", null) ?? new Map();
  const planes = new Map<number, { desc: string; abgr: number; k: number }>();
  for (const [key, value] of planeNew) {
    const m = key.match(/^a(0|[1-9][0-9]*)$/);
    if (m === null || Number(m[1]) >= planeCount) continue;
    const p = obj(value, key);
    planes.set(Number(m[1]), {
      desc: string(p.get("sDescription"), "sDescription", ""),
      abgr: color(p.get("uiColor"), "uiColor", 0xffffff),
      k: integer(p.get("uiCompCount"), "uiCompCount", 1),
    });
  }

  // spec/virtualize/nd2.md §4.2: channels.
  let labels: string[] = [];
  let colors: string[] = [];
  const counts = [...planes.values()].map((p) => p.k);
  if (
    planeCount >= 1 && planes.size === planeCount &&
    counts.every((k) => k === 1 || k === 3) && counts.reduce((a, b) => a + b, 0) === comp
  ) {
    for (let i = 0; i < planeCount; i++) {
      const { desc, abgr, k } = planes.get(i)!;
      if (k === 3) {
        labels.push(`${desc} R`, `${desc} G`, `${desc} B`);
        colors.push("FF0000", "00FF00", "0000FF");
      } else {
        labels.push(desc);
        colors.push(hexColor(abgr));
      }
    }
  } else {
    labels = Array.from({ length: comp }, (_, k) => `C${k}`);
    colors = labels.map(() => "FFFFFF");
  }

  // spec/virtualize/nd2/profile.md §5.3: frames.
  const total = loops.reduce((n, l) => n * l.count, 1);
  if (total > Number.MAX_SAFE_INTEGER) reject("more than 2^53 - 1 frames");
  const frameOffsets = new Map<number, number>();
  for (const [name, offset] of chunks) {
    const m = name.match(FRAME);
    if (m !== null && Number(m[1]) < total) frameOffsets.set(Number(m[1]), offset);
  }
  const present = [...frameOffsets.keys()].sort((a, b) => a - b);
  // Every placed frame's header is read below: fetch them all at once where an exact
  // reader is given, and serve every later read inside one from it. Each is the 16-byte
  // chunk header and the name, which real files pad so that the pixels start on a 4 KiB
  // boundary (4072 bytes, then the 8-byte stamp the source metadata reads).
  if (exact !== undefined) {
    const ahead = new Prefetched(exact, fileSize, FRAME_HEADS_IN_FLIGHT);
    await ahead.fetch(present.map((f): [number, number] => [frameOffsets.get(f)!, FRAME_HEAD]));
    const blocks = read;
    read = (o, n) => (ahead.covers(o, n) ? ahead.read(o, n) : blocks(o, n));
  }
  let h = height;
  let frameNameLength = 0; // uncompressed: the name length of every frame
  let frameChunks: (f: number) => Promise<[number, number][][]>;
  if (compressed) {
    frameChunks = async (f) => {
      const o = frameOffsets.get(f)!;
      const head = await header(read, o);
      if (head.dataLength <= 8) reject(`compressed frame ${f} has no data`);
      return [[[o + 16 + head.nameLength + 8, head.dataLength - 8]]];
    };
  } else {
    // Every placed frame's header: the image takes each frame's pixels at the
    // same distance from its chunk's offset.
    let nameLength: number | undefined;
    for (const f of present) {
      const head = await header(read, frameOffsets.get(f)!);
      if (nameLength !== undefined && head.nameLength !== nameLength) {
        reject(`frame ${f}'s chunk header differs in name length from frame ${present[0]}'s`);
      }
      if (head.dataLength < 8 + height * widthBytes) reject(`frame ${f}'s chunk is too short for its pixels`);
      nameLength = head.nameLength;
    }
    nameLength ??= 0;
    frameNameLength = nameLength;
    const start = (f: number) => frameOffsets.get(f)! + 16 + nameLength + 8;
    const rows = (s: number, from: number, to: number) =>
      Array.from({ length: to - from }, (_, i): [number, number] => [s + (from + i) * widthBytes, rowBytes]);
    if (widthBytes !== rowBytes && present.length > 0) {
      // The block with the largest payload is the last block of the frame
      // that starts last: varint sizes grow with offsets.
      const far = Math.max(...present.map(start));
      for (h = height; h > 1; h--) {
        if (height % h === 0 && payloadSize(rows(far, height - h, height)) <= MAX_PAYLOAD) break;
      }
    }
    frameChunks = async (f) => {
      if (widthBytes === rowBytes) return [[[start(f), height * rowBytes]]];
      return Array.from({ length: height / h }, (_, j) => rows(start(f), j * h, j * h + h));
    };
  }

  // spec/virtualize/nd2.md §4.3: output.
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
  const chunkSize: Record<string, number> = { t: 1, c: comp, z: 1, y: h, x: width };
  const period = t && t.scale > 0 ? t.scale : undefined;
  const step = z && z.scale > 0 ? z.scale : undefined;
  const scale: Record<string, number> = {
    t: period ? period / 1000 : 1,
    c: 1,
    z: step ?? 1,
    y: calibrated ? cal! * aspect : 1,
    x: calibrated ? cal! : 1,
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
  const json = (v: unknown) => utf8.encode(stringifyJson(v));
  const group = (attributes: unknown) => json({ zarr_format: 3, node_type: "group", attributes });
  // spec/virtualize/nd2.md §4.3 stage positions: where each position's image goes.
  const det = m11 * m22 - m12 * m21;
  let translations: number[][] | undefined;
  const stages = p ? p.stage ?? [] : [pictureStage];
  if (calibrated && det !== 0 && stages.every(([x, y]) => x !== null && y !== null)) {
    translations = stages.map(([sx, sy]) => {
      const u = (m22 * sx! - m12 * sy!) / det;
      const v = (m11 * sy! - m21 * sx!) / det;
      const shift: Record<string, number> = { x: u - width * scale.x / 2, y: v - height * scale.y / 2 };
      if (!Object.values(shift).every(Number.isFinite)) reject("a stage position is not finite");
      return axes.map((a) => shift[a] ?? 0);
    });
  }
  // The source metadata (spec/virtualize/nd2.md §5): the small file-level
  // chunks at the root; the rest on vzip_source.
  const stamps = new Map<number, number>();
  for (const f of present) stamps.set(f, (await frameChunks(f))[0][0][0] - 8);
  const meta = await sourceMetadata(read, fileSize, chunks, loops, {
    count: total, placed: frameOffsets, stamps,
    ...(compressed ? {} : { nameLength: frameNameLength, pixels: height * widthBytes }),
  });
  const rootMeta = { signature: jsonText(await read(48, 64)), chunks: meta.root };
  const entries: EntryDesc[] = [
    { key: "zarr.json", bytes: group(declare({ ome: { version: "0.5", "bioformats2raw.layout": 3 } }, "nd2", url, rootMeta, REVISION)) },
    { key: "OME/zarr.json", bytes: group({ ome: { version: "0.5", series: Array.from({ length: positions }, (_, i) => String(i)) } }) },
  ];
  const b = Number.isInteger(significant) && significant >= 1 && significant <= bpc ? significant : bpc;
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
            datasets: [{
              path: "0",
              coordinateTransformations: [
                { type: "scale", scale: axes.map((a) => scale[a]) },
                ...(translations ? [{ type: "translation", translation: translations[pi] }] : []),
              ],
            }],
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
  if (Object.keys(meta.node).length > 0 || meta.arrays.length > 0) {
    entries.push(...emitPlans(meta.arrays, declare({}, "nd2", undefined, Object.keys(meta.node).length > 0 ? meta.node : undefined)));
  }
  // Row-major coordinates of each frame over the loops. Rows share their length.
  const rowLength = BigInt(rowBytes);
  const refs = (await Promise.all(present.map(async (f) => {
    const coords: Record<string, number> = {};
    let rest = f;
    for (let i = loops.length - 1; i >= 0; i--) {
      const c = rest % loops[i].count;
      coords[loops[i].kind] = loops[i].flip ? loops[i].count - 1 - c : c; // a negative z step is flipped
      rest = Math.floor(rest / loops[i].count);
    }
    return (await frameChunks(f)).map((ranges, j) => {
      const [o, n] = ranges[ranges.length - 1]; // the ranges ascend
      if (o + n > fileSize) reject(`frame ${f} is outside the file`);
      if (payloadSize(ranges) > MAX_PAYLOAD) reject(`frame ${f}'s reference payload exceeds ${MAX_PAYLOAD} bytes`);
      const index = axes.map((a) => (a === "t" || a === "z" ? coords[a] ?? 0 : a === "y" ? j : 0));
      const r: Range[] = ranges.map(([o, n]) => ({ source: 0, offset: BigInt(o), length: n === rowBytes ? rowLength : BigInt(n) }));
      return { key: `${coords.p ?? 0}/0/c/${index.join("/")}`, ranges: r };
    });
  }))).flat();
  entries.unshift(...refs);
  return {
    sources: [{ url }],
    entries,
    summary: {
      sizes: Object.fromEntries([
        ...loops.map((l): [string, number] => [l.kind, l.count]),
        ["c", comp], ["y", height], ["x", width],
      ]),
      dataType: dataType as string, compressed, paddedRows: widthBytes !== rowBytes, rowBlock: h, positions,
      frames: total, missing: total - present.length, channels: labels,
    },
  };
}
