// Describing a tiled (OME-)TIFF as an OME-NGFF 0.5 multiscale image whose
// chunks are vzip references to the TIFF's tiles: the TIFF profile
// (spec/virtualize.md, §3).

import { REVISION } from "../revision.ts";
import { type ByteReader, DataSources, declare, MAX_PAYLOAD, type Part, payloadSize, toRange, stringifyJson } from "../../../../../js/src/virtualize/common.ts";
import { entriesReader, extent, type Ifd, num, nums, readTiff, Tag, TiffError, WANTED } from "./ifd.ts";
import { LAYOUT, Translator } from "./tags.ts";
import type { ArchiveDesc, EntryDesc } from "../../../../../js/src/writer.ts";

const JPEG2000 = new Set([33003, 33004, 33005, 34712]);
const JPEG = 7;
// The Adobe APP14 marker without its last byte, the color transform (spec/virtualize.md §3.3).
const ADOBE = [0xff, 0xee, 0x00, 0x0e, 0x41, 0x64, 0x6f, 0x62, 0x65, 0x00, 0x64, 0x00, 0x00, 0x00, 0x00];

/** What each JPEG tile's stream has after its SOI marker (spec/virtualize.md §3.3), a data source:
 * the Adobe color marker for 3 samples, and the IFD's tables. */
function jpegPrefix(ifd: Ifd, spp: number, photometric: number | null): Uint8Array {
  const out: number[] = [];
  if (spp === 3) out.push(...ADOBE, photometric === 2 ? 0 : 1);
  const tables = ifd.tags.get(Tag.JPEGTables) as Uint8Array | undefined;
  if (tables !== undefined) {
    const n = tables.length;
    if (n < 4 || tables[0] !== 0xff || tables[1] !== 0xd8 || tables[n - 2] !== 0xff || tables[n - 1] !== 0xd9) {
      reject(`the IFD at ${ifd.offset} has malformed JPEGTables`);
    }
    for (const b of tables.subarray(2, n - 2)) out.push(b);
  }
  return Uint8Array.from(out);
}
const reject = (message: string): never => {
  throw new TiffError(message);
};

// ---- OME-XML (spec/virtualize/tiff.md §3)

interface XmlTag {
  start: number;
  end: number;
  closing: boolean;
  name: string; // without namespace prefix
  attrs: Record<string, string>;
  selfClosing: boolean;
}

const WS = "[ \\t\\r\\n]";
const NAME = "[A-Za-z0-9_.-]+";
const ANAME = `[^ \\t\\r\\n=/>"'<]+`;
const SKIP = "<!--[^]*?(?:-->|$)|<!\\[CDATA\\[[^]*?(?:\\]\\]>|$)|<\\?[^]*?(?:\\?>|$)|<![^]*?(?:>|$)";
const TAG = `<(/?)(?:${NAME}:)?(${NAME})((?:${WS}+${ANAME}${WS}*=${WS}*(?:"[^"]*"|'[^']*'))*)${WS}*(/?)>`;
const SCAN = new RegExp(`(${SKIP})|(${TAG})|<`, "g"); // no "m" flag: "$" is the end of X
const ATTR = new RegExp(`(${ANAME})${WS}*=${WS}*(?:"([^"]*)"|'([^']*)')`, "g");
export const DECIMAL = /^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$/;
const NAMED: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" };

function decodeXml(s: string): string {
  return s.replace(/&(?:#x([0-9a-fA-F]+)|#([0-9]+)|(lt|gt|amp|quot|apos));/g, (ref, hex, dec, named) => {
    if (named) return NAMED[named];
    const c = hex ? parseInt(hex, 16) : Number(dec);
    if (c === 0 || (c >= 0xd800 && c <= 0xdfff) || c > 0x10ffff) return ref;
    return String.fromCodePoint(c);
  });
}

/** The tags of `xml` in order, and the spans of its skipped sections (spec/virtualize/tiff.md §3). */
export function scan(xml: string): { tags: XmlTag[]; skipped: [number, number][] } {
  const tags: XmlTag[] = [];
  const skipped: [number, number][] = [];
  for (const m of xml.matchAll(SCAN)) {
    const start = m.index!;
    const end = start + m[0].length;
    if (m[1] !== undefined) {
      skipped.push([start, end]);
    } else if (m[2] !== undefined) {
      const attrs: Record<string, string> = {};
      for (const a of m[5].matchAll(ATTR)) {
        if (!Object.hasOwn(attrs, a[1])) attrs[a[1]] = decodeXml(a[2] ?? a[3]); // first wins
      }
      tags.push({ start, end, closing: m[3] === "/", name: m[4], attrs, selfClosing: m[6] === "/" });
    }
  }
  return { tags, skipped };
}

interface TiffData {
  attrs: Record<string, string>;
  uuid?: string;
}

interface Ome {
  name?: string;
  pixels: Record<string, string>;
  tiffData: TiffData[];
  /** The attributes of the first Plane at z, c, t = 0 (its stage position). */
  plane?: Record<string, string>;
}

export function parseOme(xml: string): Ome | undefined {
  const { tags, skipped } = scan(xml);
  if (!tags.some((t) => !t.closing && t.name === "OME")) return undefined;
  const image = tags.find((t) => !t.closing && t.name === "Image");
  const pi = tags.findIndex((t) => !t.closing && t.name === "Pixels");
  const name = image?.attrs.Name;
  if (pi < 0) return { name, pixels: {}, tiffData: [] };
  const inside: XmlTag[] = [];
  if (!tags[pi].selfClosing) {
    for (const t of tags.slice(pi + 1)) {
      if (t.closing && t.name === "Pixels") break;
      inside.push(t);
    }
  }
  // The characters of `xml` in [start, end), without skipped sections (which do not
  // overlap, so their ends ascend: the first that ends after `start` is found by bisection).
  const text = (start: number, end: number) => {
    let out = "";
    let pos = start;
    let lo = 0;
    let hi = skipped.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (skipped[mid][1] <= start) lo = mid + 1;
      else hi = mid;
    }
    for (let k = lo; k < skipped.length; k++) {
      const [a, b] = skipped[k];
      if (a >= end) break;
      out += xml.slice(pos, a);
      pos = b;
    }
    return decodeXml(out + xml.slice(pos, end)).replace(/^[ \t\r\n]+|[ \t\r\n]+$/g, "");
  };
  const tiffData: TiffData[] = [];
  inside.forEach((t, i) => {
    if (t.closing || t.name !== "TiffData") return;
    const td: TiffData = { attrs: t.attrs };
    if (!t.selfClosing) {
      for (let j = i + 1; j < inside.length; j++) {
        const u = inside[j];
        if (u.name === "TiffData") break; // its end tag, or the next TiffData
        if (!u.closing && u.name === "UUID") {
          if (u.attrs.FileName !== undefined) td.uuid = u.attrs.FileName;
          else if (u.selfClosing) td.uuid = "";
          else {
            const next = tags[pi + 2 + j]; // the tag after u (inside[j] is tags[pi + 1 + j])
            td.uuid = text(u.end, next === undefined ? xml.length : next.start);
          }
          break;
        }
      }
    }
    tiffData.push(td);
  });
  const plane = inside.find((t) => !t.closing && t.name === "Plane" &&
    ["TheZ", "TheC", "TheT"].every((k) => intAttr(t.attrs, k, 0) === 0))?.attrs;
  return { name, pixels: tags[pi].attrs, tiffData, plane };
}

function intAttr(attrs: Record<string, string>, key: string, fallback: number, minimum = 0): number {
  const v = attrs[key];
  if (v === undefined) return fallback;
  const s = v.replace(/^[ \t\r\n]+|[ \t\r\n]+$/g, "");
  if (!/^[0-9]+$/.test(s)) reject(`${key}="${v}" is not an integer`);
  const n = Number(s);
  if (!Number.isSafeInteger(n)) reject(`${key}="${v}" is not an integer`);
  if (n < minimum) reject(`${key}="${v}" is less than ${minimum}`);
  return n;
}

function physical(attrs: Record<string, string>, d: string): number | undefined {
  return decimal(attrs[`PhysicalSize${d}`], true);
}

/** A decimal value (spec/virtualize/tiff.md §3), or undefined. */
function decimal(v: string | undefined, positive = false): number | undefined {
  if (v === undefined || !DECIMAL.test(v)) return undefined;
  const x = Number(v);
  return Number.isFinite(x) && (x > 0 || !positive) ? x : undefined;
}

/** The `name = value` fields of an Aperio ImageDescription (spec/virtualize/tiff.md §4.4), or undefined. */
function aperioFields(description: Uint8Array): Map<string, string> | undefined {
  if (String.fromCharCode(...description.subarray(0, 6)) !== "Aperio") return undefined;
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(description);
  } catch {
    return undefined;
  }
  const fields = new Map<string, string>();
  const trim = (s: string) => s.replace(/^[ \t\r\n]+|[ \t\r\n]+$/g, "");
  for (const part of text.split("|")) {
    const at = part.indexOf("=");
    if (at < 0) continue;
    const name = trim(part.slice(0, at));
    if (!fields.has(name)) fields.set(name, trim(part.slice(at + 1)));
  }
  return fields;
}

// Sizes of the length units in metres (conventions §5).
const LENGTHS: Record<string, number> = {
  micrometer: 1e-6, nanometer: 1e-9, millimeter: 1e-3, centimeter: 1e-2, meter: 1,
  angstrom: 1e-10, picometer: 1e-12, inch: 0.0254, foot: 0.3048,
};

const UNITS: Record<string, string> = {
  "µm": "micrometer", "um": "micrometer", "μm": "micrometer", "nm": "nanometer",
  "mm": "millimeter", "cm": "centimeter", "m": "meter", "Å": "angstrom", "Å": "angstrom",
  "pm": "picometer", "in": "inch", "ft": "foot", "s": "second", "ms": "millisecond", "min": "minute", "h": "hour",
};

// ---- profile

interface Level {
  width: number;
  height: number;
  tileWidth: number;
  tileHeight: number;
  /** One IFD per plane, in plane order. */
  ifds: Ifd[];
}

function format(ifd: Ifd) {
  const bits = nums(ifd, Tag.BitsPerSample);
  if (bits.length === 0 || bits.some((b) => b !== bits[0]) || bits[0] < 1) {
    reject("BitsPerSample values are missing, differ or are 0");
  }
  const formats = (ifd.tags.get(Tag.SampleFormat) as number[] | undefined) ?? [1];
  if (formats.length === 0 || formats.some((f) => f !== formats[0])) {
    reject("SampleFormat values are missing or differ");
  }
  const spp = num(ifd, Tag.SamplesPerPixel, 1);
  if (spp < 1) reject("SamplesPerPixel is 0");
  const planar = spp > 1 ? num(ifd, Tag.PlanarConfiguration, 1) : 1;
  if (planar !== 1 && planar !== 2) reject(`PlanarConfiguration ${planar}`);
  return {
    bits: bits[0],
    spp,
    sampleFormat: formats[0],
    planar,
    compression: num(ifd, Tag.Compression, 1),
    predictor: num(ifd, Tag.Predictor, 1),
    // For JPEG, PhotometricInterpretation is part of the format (spec/virtualize/tiff.md §2).
    photometric: num(ifd, Tag.Compression, 1) === JPEG ? (ifd.tags.get(Tag.PhotometricInterpretation) as number[] | undefined)?.[0] ?? null : null,
  };
}

const sameFormat = (a: Ifd, b: Ifd) => JSON.stringify(format(a)) === JSON.stringify(format(b));
const tiled = (ifd: Ifd) => ifd.fields.has(Tag.TileWidth) && ifd.fields.has(Tag.TileOffsets);

/** The size checks of spec/virtualize/tiff.md §2, for planes, levels and level-scan candidates. */
function checkSize(ifd: Ifd) {
  if (num(ifd, Tag.ImageWidth) < 1 || num(ifd, Tag.ImageLength) < 1) reject(`the image at ${ifd.offset} is empty`);
  if (tiled(ifd)) {
    if (!ifd.fields.has(Tag.TileByteCounts)) reject(`IFD at ${ifd.offset} has no tag ${Tag.TileByteCounts}`);
    if (num(ifd, Tag.TileWidth) < 1 || num(ifd, Tag.TileLength) < 1) {
      reject(`the image at ${ifd.offset} has an empty tile size`);
    }
  }
}

/** How an image's bytes hold its samples (spec/virtualize/tiff.md §4.3): the codecs
 * decode full-resolution samples, most significant bit first. */
function checkSamples(ifd: Ifd) {
  const compression = num(ifd, Tag.Compression, 1);
  if ([1, 8, 32946, 50000].includes(compression) && num(ifd, Tag.FillOrder, 1) !== 1) {
    reject(`the image at ${ifd.offset} has FillOrder ${num(ifd, Tag.FillOrder, 1)}`);
  }
  const subsampling = (ifd.tags.get(Tag.YCbCrSubsampling) as number[] | undefined) ?? [2, 2]; // TIFF 6.0's default
  if (compression !== JPEG && !JPEG2000.has(compression) && num(ifd, Tag.PhotometricInterpretation, 0) === 6 &&
      !(subsampling.length === 2 && subsampling[0] === 1 && subsampling[1] === 1)) {
    reject(`the image at ${ifd.offset} is YCbCr with subsampling ${subsampling}`);
  }
}

function level(ifds: Ifd[]): Level {
  for (const i of ifds) {
    checkSize(i);
    format(i);
    checkSamples(i);
  }
  const [first] = ifds;
  const stripped = ifds.find((i) => !tiled(i));
  if (stripped !== undefined) {
    reject(`only tiled TIFFs are supported; the image at ${stripped.offset} is stored in strips`);
  }
  const l = {
    width: num(first, Tag.ImageWidth),
    height: num(first, Tag.ImageLength),
    tileWidth: num(first, Tag.TileWidth),
    tileHeight: num(first, Tag.TileLength),
    ifds,
  };
  for (const ifd of ifds) {
    if (
      num(ifd, Tag.ImageWidth) !== l.width || num(ifd, Tag.ImageLength) !== l.height ||
      num(ifd, Tag.TileWidth) !== l.tileWidth || num(ifd, Tag.TileLength) !== l.tileHeight ||
      !sameFormat(ifd, first)
    ) {
      reject("planes of one pyramid level differ in size, tiling or format");
    }
  }
  return l;
}

function dtype(bits: number, sampleFormat: number): string {
  const kind = ({ 1: "uint", 2: "int", 3: "float" } as Record<number, string>)[sampleFormat];
  if (kind === undefined || ![8, 16, 32, 64].includes(bits) || (kind === "float" && bits < 32)) {
    reject(`unsupported sample type: ${bits}-bit, SampleFormat ${sampleFormat}`);
  }
  return `${kind}${bits}`;
}

export interface Virtualized extends ArchiveDesc {
  /** A summary of what was found, for display. */
  summary: { name?: string; axes: string[]; levels: number[][]; references: number; codec: string };
}

/**
 * Describes the TIFF at `url` (read through `read`) as an OME-Zarr archive.
 * The archive's only source is `url`, unpinned.
 */
export async function virtualizeTiff(
  url: string,
  read: ByteReader,
  fileSize: number,
): Promise<Virtualized> {
  const tiff = await readTiff(read, fileSize);
  const source = { byte_order: tiff.littleEndian ? "little" : "big", bigtiff: tiff.bigTiff };
  const [ifd0] = tiff.ifds;
  if (ifd0 === undefined) reject("no images");
  await tiff.load(ifd0, { description: true });
  // A level's IFDs, with every value they use read (spec/virtualize.md §3.1).
  const loaded = async (ifds: Ifd[]) => {
    for (const i of ifds) await tiff.load(i, { tiles: true });
    return level(ifds);
  };

  // spec/virtualize/tiff.md §3: OME-XML in IFD 0's ImageDescription (type ASCII, valid UTF-8).
  const description = ifd0.tags.get(Tag.ImageDescription);
  let xml: string | undefined; // X, the OME-XML's text
  let ome: Ome | undefined;
  let ascii: Uint8Array | undefined; // D: the ASCII description up to its first NUL
  if (ifd0.types.get(Tag.ImageDescription) === 2 && description instanceof Uint8Array) {
    const nul = description.indexOf(0);
    const d = nul < 0 ? description : description.subarray(0, nul);
    ascii = d;
    let text: string | undefined;
    try {
      text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(d);
    } catch {
      text = undefined;
    }
    if (text !== undefined) {
      ome = parseOme(text);
      if (ome !== undefined) xml = text;
    }
  }
  const f = format(ifd0);
  if (!JPEG2000.has(f.compression) && f.predictor !== 1) reject(`unsupported predictor ${f.predictor}`);

  // spec/virtualize/tiff.md §4.1: planes, in (t, c, z) order, as main-chain IFD indices.
  const px = ome?.pixels ?? {};
  const sizeZ = intAttr(px, "SizeZ", 1, 1);
  const sizeT = intAttr(px, "SizeT", 1, 1);
  let sizeC = intAttr(px, "SizeC", f.spp, 1);
  const order = px.DimensionOrder ?? "XYZCT";
  if (ome && !/^XY(ZCT|ZTC|CZT|CTZ|TZC|TCZ)$/.test(order)) reject(`DimensionOrder ${order}`);
  if (f.spp > 1 && sizeC !== f.spp) {
    if (sizeC === 1) sizeC = f.spp;
    else reject(`SizeC ${sizeC} with ${f.spp} samples per pixel is not supported`);
  }
  const planeC = f.spp > 1 ? 1 : sizeC;
  const plane = (t: number, c: number, z: number) => (t * planeC + c) * sizeZ + z;
  if (sizeT * planeC * sizeZ > 100000) reject(`${sizeT * planeC * sizeZ} planes is more than 100000`);
  const planeIfd = new Array<number>(sizeT * planeC * sizeZ).fill(-1);
  if (ome === undefined) {
    planeIfd[0] = 0;
  } else {
    const size = { Z: sizeZ, C: planeC, T: sizeT } as Record<string, number>;
    const entries: TiffData[] = ome.tiffData.length > 0 ? ome.tiffData : [{ attrs: {} }];
    const files = new Set(entries.flatMap((td) => (td.uuid === undefined ? [] : [td.uuid])));
    if (files.size > 1) reject("multi-file OME-TIFF is not supported");
    let total = 0;
    const bound = 4 * planeIfd.length + 1000;
    for (const td of entries) {
      const a = td.attrs;
      const pos = { Z: intAttr(a, "FirstZ", 0), C: intAttr(a, "FirstC", 0), T: intAttr(a, "FirstT", 0) } as Record<string, number>;
      if ("ZCT".split("").some((d) => pos[d] >= size[d])) reject("TiffData starts outside the planes");
      let ifd = intAttr(a, "IFD", 0);
      let count = intAttr(a, "PlaneCount", entries.length === 1 && a.IFD === undefined ? planeIfd.length : 1, 1);
      const [d0, d1, d2] = order.slice(2);
      const first = pos[d0] + size[d0] * (pos[d1] + size[d1] * pos[d2]); // in stepping order
      count = Math.min(count, planeIfd.length - first); // planes past the last position are ignored
      total += count;
      if (total > bound) reject(`the TiffData elements cover more than ${bound} planes`);
      while (count-- > 0) {
        planeIfd[plane(pos.T, pos.C, pos.Z)] = ifd++;
        let stepped = false;
        for (const d of order.slice(2)) {
          if (++pos[d] < size[d]) {
            stepped = true;
            break;
          }
          pos[d] = 0;
        }
        if (!stepped) break;
      }
    }
  }
  if (planeIfd.some((i) => i < 0 || i >= tiff.ifds.length)) reject("OME-XML planes do not match the TIFF's images");
  const planes = planeIfd.map((i) => tiff.ifds[i]);

  // spec/virtualize/tiff.md §4.2: pyramid levels.
  const levels: Level[] = [];
  if (ifd0.subIfds.length > 0) {
    const s = ifd0.subIfds.length;
    for (const p of planes) {
      if (p.subIfds.length < s) reject(`the IFD at ${p.offset} has fewer SubIFDs than IFD 0`);
    }
    for (let k = -1; k < s; k++) levels.push(await loaded(planes.map((p) => (k < 0 ? p : p.subIfds[k]))));
  } else {
    levels.push(await loaded(planes));
    if (ome === undefined) {
      for (const ifd of tiff.ifds.slice(1)) {
        const prev = levels[levels.length - 1];
        if (!tiled(ifd) || !ifd.fields.has(Tag.BitsPerSample)) continue;
        await tiff.load(ifd);
        checkSize(ifd);
        if (sameFormat(ifd, ifd0) && num(ifd, Tag.ImageWidth) < prev.width && num(ifd, Tag.ImageLength) < prev.height) {
          levels.push(await loaded([ifd]));
        }
      }
    }
  }
  for (const l of levels) {
    if (!l.ifds.every((i) => sameFormat(i, ifd0))) reject("pyramid levels differ in sample format or compression");
  }

  // spec/virtualize/tiff.md §4.3: data type and codecs.
  const contig = f.spp > 1 && f.planar === 1;
  // spec/virtualize/tiff.md §4.4: pixel size and position.
  const sizes: Record<string, number> = {};
  const units: Record<string, string> = {};
  let centre: Record<string, number> | undefined;
  let corner: Record<string, number> | undefined;
  if (ome !== undefined) {
    for (const [d, a] of [["Z", "z"], ["Y", "y"], ["X", "x"]]) {
      const v = physical(px, d);
      if (v === undefined) continue;
      sizes[a] = v;
      const u = UNITS[px[`PhysicalSize${d}Unit`] ?? "µm"];
      if (u) units[a] = u;
    }
    const stage = ome.plane ?? {};
    const pos = { x: decimal(stage.PositionX), y: decimal(stage.PositionY) };
    const posUnits = { x: UNITS[stage.PositionXUnit ?? ""], y: UNITS[stage.PositionYUnit ?? ""] };
    if ((["x", "y"] as const).every((a) => pos[a] !== undefined && posUnits[a] in LENGTHS && units[a] in LENGTHS)) {
      centre = {
        x: pos.x! * (LENGTHS[posUnits.x] / LENGTHS[units.x]),
        y: pos.y! * (LENGTHS[posUnits.y] / LENGTHS[units.y]),
      };
    }
  } else {
    const fields = ascii === undefined ? undefined : aperioFields(ascii);
    const mpp = decimal(fields?.get("MPP"), true);
    if (mpp !== undefined) {
      sizes.x = sizes.y = mpp;
      units.x = units.y = "micrometer";
      const left = decimal(fields!.get("Left"));
      const top = decimal(fields!.get("Top"));
      if (left !== undefined && top !== undefined) corner = { x: left * 1000, y: top * 1000 };
    } else {
      // Only an explicit ResolutionUnit, and a pixel under 25.4 µm: 72, 96 or
      // 300 dpi is a document's resolution, not a pixel size.
      const perUnit = ({ 2: 25400, 3: 10000 } as Record<number, number>)[num(ifd0, Tag.ResolutionUnit, 0)];
      for (const [tag, a] of [[Tag.XResolution, "x"], [Tag.YResolution, "y"]] as const) {
        const r = ifd0.tags.get(tag) as number[] | undefined;
        if (perUnit !== undefined && r !== undefined && r[0] > 0 && r[1] > 0) {
          const pixel = perUnit / (r[0] / r[1]);
          if (pixel < 25.4) {
            sizes[a] = pixel;
            units[a] = "micrometer";
          }
        }
      }
    }
  }
  const axes: { name: string; type: string; unit?: string; size: number }[] = [];
  if (sizeT > 1) axes.push({ name: "t", type: "time", size: sizeT });
  if (sizeC > 1) axes.push({ name: "c", type: "channel", size: sizeC });
  if (sizeZ > 1) axes.push({ name: "z", type: "space", unit: units.z, size: sizeZ });
  axes.push({ name: "y", type: "space", unit: units.y, size: 0 });
  axes.push({ name: "x", type: "space", unit: units.x, size: 0 });
  const cAxis = axes.findIndex((a) => a.name === "c");

  const dataType = dtype(f.bits, f.sampleFormat);
  const itemsize = f.bits / 8;
  let codecs: unknown[];
  let codecName: string;
  if (JPEG2000.has(f.compression)) {
    codecs = [{ name: "imagecodecs_jpeg2k" }];
    codecName = "imagecodecs_jpeg2k";
  } else if (f.compression === JPEG) {
    if (f.bits !== 8 || f.sampleFormat !== 1 ||
        !(f.spp === 1 || (f.spp === 3 && f.planar === 1 && (f.photometric === 2 || f.photometric === 6)))) {
      reject(`unsupported JPEG: ${f.bits}-bit, ${f.spp} samples, planar ${f.planar}, photometric ${f.photometric}`);
    }
    codecs = [{ name: "imagecodecs_jpeg" }];
    codecName = "imagecodecs_jpeg";
  } else {
    const bytes = itemsize > 1
      ? { name: "bytes", configuration: { endian: tiff.littleEndian ? "little" : "big" } }
      : { name: "bytes" };
    codecs = [bytes];
    if (f.compression === 8 || f.compression === 32946) {
      codecs.push({ name: "zlib", configuration: { level: 1 } });
      codecName = "zlib";
    } else if (f.compression === 50000) {
      codecs.push({ name: "zstd", configuration: { level: 0, checksum: false } });
      codecName = "zstd";
    } else if (f.compression === 1) {
      codecName = "bytes";
    } else {
      return reject(`unsupported compression ${f.compression}`);
    }
  }
  if (contig) {
    // Tiles hold [y, x, sample]; move the channel axis last.
    const stored = [...axes.keys()].filter((i) => i !== cAxis).concat([cAxis]);
    codecs.unshift({ name: "transpose", configuration: { order: stored } });
  }

  // spec/virtualize/tiff.md §4.4: the arrays; spec/virtualize.md §3.3: their chunks.
  const data = new DataSources();
  const entries: EntryDesc[] = [];
  const meta: EntryDesc[] = [];
  const utf8 = new TextEncoder();
  const json = (v: unknown) => utf8.encode(stringifyJson(v));
  const datasets = [];
  let references = 0;
  const shapes: number[][] = [];
  const [base] = levels;
  for (const [li, l] of levels.entries()) {
    const shape = axes.map((a) => a.size);
    shape[shape.length - 2] = l.height;
    shape[shape.length - 1] = l.width;
    const chunk = axes.map((a) => (a.name === "c" && contig ? f.spp : 1));
    chunk[chunk.length - 2] = l.tileHeight;
    chunk[chunk.length - 1] = l.tileWidth;
    shapes.push(shape);
    meta.push({
      key: `${li}/zarr.json`,
      bytes: json({
        zarr_format: 3,
        node_type: "array",
        shape,
        data_type: dataType,
        chunk_grid: { name: "regular", configuration: { chunk_shape: chunk } },
        chunk_key_encoding: { name: "default", configuration: { separator: "/" } },
        fill_value: 0,
        codecs,
        dimension_names: axes.map((a) => a.name),
        attributes: {},
      }),
    });
    const across = Math.ceil(l.width / l.tileWidth);
    const down = Math.ceil(l.height / l.tileHeight);
    const perSample = across * down;
    for (let t = 0; t < sizeT; t++) {
      for (let c = 0; c < planeC; c++) {
        for (let z = 0; z < sizeZ; z++) {
          const ifd = l.ifds[plane(t, c, z)];
          const offsets = nums(ifd, Tag.TileOffsets);
          const counts = nums(ifd, Tag.TileByteCounts);
          const prefix = f.compression === JPEG ? jpegPrefix(ifd, f.spp, f.photometric) : undefined;
          const samples = f.spp > 1 && !contig ? f.spp : 1;
          if (offsets.length !== samples * perSample || counts.length !== samples * perSample) {
            reject(`IFD at ${ifd.offset} has ${offsets.length} tiles, expected ${samples * perSample}`);
          }
          for (let s = 0; s < samples; s++) {
            for (let j = 0; j < perSample; j++) {
              const k = s * perSample + j;
              if (counts[k] === 0) continue; // missing tile: fill value
              if (offsets[k] + counts[k] > fileSize) reject(`tile ${k} of the IFD at ${ifd.offset} is outside the file`);
              const coords: number[] = [];
              if (sizeT > 1) coords.push(t);
              if (sizeC > 1) coords.push(f.spp > 1 ? (contig ? 0 : s) : c);
              if (sizeZ > 1) coords.push(z);
              coords.push(Math.floor(j / across), j % across);
              if (prefix !== undefined && counts[k] <= 2) reject(`JPEG tile ${k} of the IFD at ${ifd.offset} is too short`);
              // For JPEG, the tile's first 2 bytes (its SOI), the prefix, the rest of the tile.
              const ranges: Part[] = prefix === undefined || prefix.length === 0
                ? [[offsets[k], counts[k]]]
                : [[offsets[k], 2], data.range(prefix), [offsets[k] + 2, counts[k] - 2]];
              if (payloadSize(ranges) > MAX_PAYLOAD) reject(`tile ${k}'s reference payload exceeds ${MAX_PAYLOAD} bytes`);
              entries.push({
                key: `${li}/c/${coords.join("/")}`,
                ranges: ranges.map(toRange),
              });
              references++;
            }
          }
        }
      }
    }
    const scale = axes.map((a) => {
      if (a.name === "y") return (sizes.y ?? 1) * (base.height / l.height);
      if (a.name === "x") return (sizes.x ?? 1) * (base.width / l.width);
      if (a.name === "z") return sizes.z ?? 1;
      return 1;
    });
    datasets.push({ path: String(li), coordinateTransformations: [{ type: "scale", scale }] as unknown[] });
  }
  // The same translation at every level (conventions §5).
  if (units.x !== undefined && units.y !== undefined) {
    if (centre !== undefined) {
      corner = { x: centre.x - base.width * (sizes.x ?? 1) / 2, y: centre.y - base.height * (sizes.y ?? 1) / 2 };
    }
    if (corner !== undefined) {
      const translation = axes.map((a) => corner![a.name] ?? 0);
      if (!translation.every(Number.isFinite)) reject("a translation is not finite");
      for (const d of datasets) d.coordinateTransformations.push({ type: "translation", translation });
    }
  }
  const name = ome?.name || undefined;
  meta.push({
    key: "zarr.json",
    bytes: json({
      zarr_format: 3,
      node_type: "group",
      attributes: declare({
        ome: {
          version: "0.5",
          multiscales: [{
            ...(name === undefined ? {} : { name }),
            axes: axes.map(({ name, type, unit }) => ({ name, type, ...(unit ? { unit } : {}) })),
            datasets,
          }],
        },
      }, "tiff", url, source, REVISION),
    }),
  });
  // The source metadata (spec/virtualize/tiff.md §5): one group per IFD (main chain,
  // SubIFDs and the IFDs pointer tags lead to), and the strips or tiles of the IFDs
  // that are not images here, on vzip_source.
  const tree: [Ifd, string, number][] = []; // every IFD read, depth first, with its path and depth
  const walk = (ifd: Ifd, path: string, depth: number) => {
    tree.push([ifd, path, depth]);
    for (const [j, sub] of ifd.subIfds.entries()) walk(sub, `${path}/subifds/${j}`, depth + 1);
  };
  for (const [k, ifd] of tiff.ifds.entries()) walk(ifd, `ifds/${k}`, 0);
  const translate = new Translator(read, fileSize, tiff.littleEndian, LAYOUT, WANTED,
    entriesReader(read, fileSize, tiff.bigTiff, tiff.littleEndian),
    tree.map(([ifd, path]) => [ifd.offset, ifd.offset + extent(ifd.entries.length, tiff.bigTiff), path]));
  const mapped = new Set<Ifd>(levels.flatMap((lv) => lv.ifds));
  for (const [ifd, path, depth] of tree) await translate.ifd(ifd.entries, path, depth, !mapped.has(ifd));
  return {
    sources: data.table(url),
    entries: [...entries, ...meta, ...translate.emit("tiff", { ifd_count: tiff.ifds.length })],
    summary: { name, axes: axes.map((a) => a.name), levels: shapes, references, codec: codecName },
  };
}
