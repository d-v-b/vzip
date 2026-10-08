// Describing a tiled (OME-)TIFF as an OME-NGFF 0.5 multiscale image whose
// chunks are vzip references to the TIFF's tiles: the TIFF profile
// (profiles/tiff.md, §3).

import { type ByteReader, DataSources, declare, MAX_PAYLOAD, type Part, payloadSize, toRange } from "../common.ts";
import { type Ifd, num, nums, readTiff, Tag, TiffError } from "./ifd.ts";
import { Translator } from "./tags.ts";
import type { ArchiveDesc, EntryDesc } from "../../writer.ts";

const JPEG2000 = new Set([33003, 33004, 33005, 34712]);
const JPEG = 7;
// The Adobe APP14 marker without its last byte, the color transform (profiles/tiff.md §3.3).
const ADOBE = [0xff, 0xee, 0x00, 0x0e, 0x41, 0x64, 0x6f, 0x62, 0x65, 0x00, 0x64, 0x00, 0x00, 0x00, 0x00];

/** The start of each JPEG tile's stream (profiles/tiff.md §3.3), a data source: SOI, the Adobe color
 * marker for 3 samples, and the IFD's tables. */
function jpegPrefix(ifd: Ifd, spp: number, photometric: number | null): Uint8Array {
  const out = [0xff, 0xd8];
  if (spp === 3) out.push(...ADOBE, photometric === 2 ? 0 : 1);
  const tables = ifd.tags.get(Tag.JPEGTables) as Uint8Array | undefined;
  if (tables !== undefined) {
    const n = tables.length;
    if (n < 4 || tables[0] !== 0xff || tables[1] !== 0xd8 || tables[n - 2] !== 0xff || tables[n - 1] !== 0xd9) {
      reject(`the IFD at ${ifd.offset} has malformed JPEGTables`);
    }
    out.push(...tables.subarray(2, n - 2));
  }
  return Uint8Array.from(out);
}
const reject = (message: string): never => {
  throw new TiffError(message);
};

// ---- OME-XML (conventions/tiff/README.md §3)

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
const DECIMAL = /^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$/;
const NAMED: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" };

function decodeXml(s: string): string {
  return s.replace(/&(?:#x([0-9a-fA-F]+)|#([0-9]+)|(lt|gt|amp|quot|apos));/g, (ref, hex, dec, named) => {
    if (named) return NAMED[named];
    const c = hex ? parseInt(hex, 16) : Number(dec);
    if (c === 0 || (c >= 0xd800 && c <= 0xdfff) || c > 0x10ffff) return ref;
    return String.fromCodePoint(c);
  });
}

/** The tags of `xml` in order, and the spans of its skipped sections (conventions/tiff/README.md §3). */
function scan(xml: string): { tags: XmlTag[]; skipped: [number, number][] } {
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
  // The characters of `xml` in [start, end), without skipped sections.
  const text = (start: number, end: number) => {
    let out = "";
    let pos = start;
    for (const [a, b] of skipped) {
      if (b <= start || a >= end) continue;
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
            const next = tags[tags.indexOf(u) + 1];
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

/** A decimal value (conventions/tiff/README.md §3), or undefined. */
function decimal(v: string | undefined, positive = false): number | undefined {
  if (v === undefined || !DECIMAL.test(v)) return undefined;
  const x = Number(v);
  return Number.isFinite(x) && (x > 0 || !positive) ? x : undefined;
}

/** The `name = value` fields of an Aperio ImageDescription (conventions/tiff/README.md §4.4), or undefined. */
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
    // For JPEG, PhotometricInterpretation is part of the format (conventions/tiff/README.md §2).
    photometric: num(ifd, Tag.Compression, 1) === JPEG ? (ifd.tags.get(Tag.PhotometricInterpretation) as number[] | undefined)?.[0] ?? null : null,
  };
}

const sameFormat = (a: Ifd, b: Ifd) => JSON.stringify(format(a)) === JSON.stringify(format(b));
const tiled = (ifd: Ifd) => ifd.tags.has(Tag.TileWidth) && ifd.tags.has(Tag.TileOffsets);

/** The size checks of conventions/tiff/README.md §2, for planes, levels and level-scan candidates. */
function checkSize(ifd: Ifd) {
  if (num(ifd, Tag.ImageWidth) < 1 || num(ifd, Tag.ImageLength) < 1) reject(`the image at ${ifd.offset} is empty`);
  if (tiled(ifd)) {
    nums(ifd, Tag.TileByteCounts);
    if (num(ifd, Tag.TileWidth) < 1 || num(ifd, Tag.TileLength) < 1) {
      reject(`the image at ${ifd.offset} has an empty tile size`);
    }
  }
}

function level(ifds: Ifd[]): Level {
  for (const i of ifds) {
    checkSize(i);
    format(i);
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
  // The source metadata (conventions/tiff/README.md §5): every IFD's tags.
  const translate = new Translator(read, fileSize, tiff.littleEndian);
  const sourceIfds: Record<string, unknown>[] = [];
  for (const ifd of tiff.ifds) {
    const node: Record<string, unknown> = { tags: await translate.tags(ifd.entries) };
    if (ifd.tags.has(Tag.SubIFDs)) {
      const subs = [];
      for (const sub of ifd.subIfds) subs.push({ tags: await translate.tags(sub.entries) });
      node.subifds = subs;
    }
    sourceIfds.push(node);
  }
  const source = { byte_order: tiff.littleEndian ? "little" : "big", bigtiff: tiff.bigTiff, ifds: sourceIfds };
  const [ifd0] = tiff.ifds;
  if (ifd0 === undefined) reject("no images");

  // conventions/tiff/README.md §3: OME-XML in IFD 0's ImageDescription (type ASCII, valid UTF-8).
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

  // conventions/tiff/README.md §4.1: planes, in (t, c, z) order, as main-chain IFD indices.
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
    for (const td of entries) {
      const a = td.attrs;
      const pos = { Z: intAttr(a, "FirstZ", 0), C: intAttr(a, "FirstC", 0), T: intAttr(a, "FirstT", 0) } as Record<string, number>;
      if ("ZCT".split("").some((d) => pos[d] >= size[d])) reject("TiffData starts outside the planes");
      let ifd = intAttr(a, "IFD", 0);
      let count = intAttr(a, "PlaneCount", entries.length === 1 && a.IFD === undefined ? planeIfd.length : 1, 1);
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

  // conventions/tiff/README.md §4.2: pyramid levels.
  const levels: Level[] = [];
  if (ifd0.subIfds.length > 0) {
    const s = ifd0.subIfds.length;
    for (const p of planes) {
      if (p.subIfds.length < s) reject(`the IFD at ${p.offset} has fewer SubIFDs than IFD 0`);
    }
    for (let k = -1; k < s; k++) levels.push(level(planes.map((p) => (k < 0 ? p : p.subIfds[k]))));
  } else {
    levels.push(level(planes));
    if (ome === undefined) {
      for (const ifd of tiff.ifds.slice(1)) {
        const prev = levels[levels.length - 1];
        if (!tiled(ifd) || !ifd.tags.has(Tag.BitsPerSample)) continue;
        checkSize(ifd);
        if (sameFormat(ifd, ifd0) && num(ifd, Tag.ImageWidth) < prev.width && num(ifd, Tag.ImageLength) < prev.height) {
          levels.push(level([ifd]));
        }
      }
    }
  }
  for (const l of levels) {
    if (!l.ifds.every((i) => sameFormat(i, ifd0))) reject("pyramid levels differ in sample format or compression");
  }

  // conventions/tiff/README.md §4.3: data type and codecs.
  const contig = f.spp > 1 && f.planar === 1;
  // conventions/tiff/README.md §4.4: pixel size and position.
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
      const perUnit = ({ 2: 25400, 3: 10000 } as Record<number, number>)[num(ifd0, Tag.ResolutionUnit, 2)];
      for (const [tag, a] of [[Tag.XResolution, "x"], [Tag.YResolution, "y"]] as const) {
        const r = ifd0.tags.get(tag) as number[] | undefined;
        if (perUnit !== undefined && r !== undefined && r[0] > 0 && r[1] > 0) {
          sizes[a] = perUnit / (r[0] / r[1]);
          units[a] = "micrometer";
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

  // conventions/tiff/README.md §4.4: the arrays; profiles/tiff.md §3.3: their chunks.
  const data = new DataSources();
  const entries: EntryDesc[] = [];
  const meta: EntryDesc[] = [];
  const utf8 = new TextEncoder();
  const json = (v: unknown) => utf8.encode(JSON.stringify(v, null, 2));
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
              let ranges: Part[] = [[offsets[k], counts[k]]];
              if (prefix !== undefined) {
                if (counts[k] <= 2) reject(`JPEG tile ${k} of the IFD at ${ifd.offset} is too short`);
                ranges = [data.range(prefix), [offsets[k] + 2, counts[k] - 2]];
              }
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
      }, "tiff", url, source),
    }),
  });
  return {
    sources: data.table(url),
    entries: [...entries, ...meta],
    summary: { name, axes: axes.map((a) => a.name), levels: shapes, references, codec: codecName },
  };
}
