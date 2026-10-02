// Describing a tiled (OME-)TIFF as an OME-NGFF 0.5 multiscale image whose
// chunks are vzip references to the TIFF's tiles: the TIFF profile of
// VIRTUALIZE.md (§3).

import type { Range, Source } from "./protobuf.ts";
import { type ByteReader, type Ifd, num, nums, readTiff, Tag, TiffError } from "./tiff.ts";
import type { ArchiveDesc, EntryDesc } from "./writer.ts";

const JPEG2000 = new Set([33003, 33004, 33005, 34712]);
const reject = (message: string): never => {
  throw new TiffError(message);
};

// ---- OME-XML (§3.2)

interface XmlTag {
  start: number;
  end: number;
  closing: boolean;
  name: string; // without namespace prefix
  attrs: Record<string, string>;
  selfClosing: boolean;
}

const SKIP = /<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<\?[\s\S]*?\?>|<![^>]*>/g;
const TAG = /<(\/?)(?:[\w.-]+:)?([\w.-]+)((?:\s+[^\s=/>]+\s*=\s*(?:"[^"]*"|'[^']*'))*)\s*(\/?)>/g;
const ATTR = /([^\s=/>]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g;
const DECIMAL = /^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$/;

function decodeXml(s: string): string {
  return s.replace(/&(?:#x([0-9a-fA-F]+)|#([0-9]+)|(lt|gt|amp|quot|apos));/g, (_, hex, dec, named) =>
    hex ? String.fromCodePoint(parseInt(hex, 16))
      : dec ? String.fromCodePoint(Number(dec))
      : ({ amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" } as Record<string, string>)[named]);
}

function xmlTags(xml: string): XmlTag[] {
  const text = xml.replace(SKIP, (m) => " ".repeat(m.length));
  return [...text.matchAll(TAG)].map((m) => {
    const attrs: Record<string, string> = {};
    for (const a of m[3].matchAll(ATTR)) attrs[a[1]] = decodeXml(a[2] ?? a[3]);
    return { start: m.index!, end: m.index! + m[0].length, closing: m[1] === "/", name: m[2], attrs, selfClosing: m[4] === "/" };
  });
}

interface TiffData {
  attrs: Record<string, string>;
  uuid?: string;
}

interface Ome {
  name?: string;
  pixels: Record<string, string>;
  tiffData: TiffData[];
}

export function parseOme(xml: string): Ome | undefined {
  const tags = xmlTags(xml);
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
  const tiffData: TiffData[] = [];
  inside.forEach((t, i) => {
    if (t.closing || t.name !== "TiffData") return;
    const td: TiffData = { attrs: t.attrs };
    if (!t.selfClosing) {
      for (let j = i + 1; j < inside.length; j++) {
        const u = inside[j];
        if (u.closing && u.name === "TiffData") break;
        if (!u.closing && u.name === "UUID") {
          let text = "";
          if (!u.selfClosing) {
            const end = j + 1 < inside.length ? inside[j + 1].start : xml.length;
            text = decodeXml(xml.slice(u.end, end).replace(SKIP, "")).trim();
          }
          td.uuid = u.attrs.FileName ?? text;
          break;
        }
      }
    }
    tiffData.push(td);
  });
  return { name, pixels: tags[pi].attrs, tiffData };
}

function intAttr(attrs: Record<string, string>, key: string, fallback: number, minimum = 0): number {
  const v = attrs[key];
  if (v === undefined) return fallback;
  const s = v.trim();
  if (!/^[0-9]+$/.test(s)) reject(`${key}="${v}" is not an integer`);
  const n = Number(s);
  if (!Number.isSafeInteger(n)) reject(`${key}="${v}" is too large`);
  if (n < minimum) reject(`${key}="${v}" is less than ${minimum}`);
  return n;
}

function physical(attrs: Record<string, string>, d: string): number | undefined {
  const v = attrs[`PhysicalSize${d}`];
  if (v === undefined || !DECIMAL.test(v)) return undefined;
  const x = Number(v);
  return Number.isFinite(x) && x > 0 ? x : undefined;
}

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
  if (bits.some((b) => b !== bits[0])) reject("samples of different sizes");
  const formats = (ifd.tags.get(Tag.SampleFormat) as number[] | undefined) ?? [1];
  if (formats.some((f) => f !== formats[0])) reject("samples of different SampleFormats");
  const spp = num(ifd, Tag.SamplesPerPixel, 1);
  const planar = spp > 1 ? num(ifd, Tag.PlanarConfiguration, 1) : 1;
  if (planar !== 1 && planar !== 2) reject(`PlanarConfiguration ${planar}`);
  return {
    bits: bits[0],
    spp,
    sampleFormat: formats[0],
    planar,
    compression: num(ifd, Tag.Compression, 1),
    predictor: num(ifd, Tag.Predictor, 1),
  };
}

const sameFormat = (a: Ifd, b: Ifd) => JSON.stringify(format(a)) === JSON.stringify(format(b));
const tiled = (ifd: Ifd) => ifd.tags.has(Tag.TileWidth) && ifd.tags.has(Tag.TileOffsets);

function level(ifds: Ifd[]): Level {
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
  const [ifd0] = tiff.ifds;
  if (ifd0 === undefined) reject("no images");

  // §3.2: OME-XML in IFD 0's ImageDescription (type ASCII, valid UTF-8).
  const description = ifd0.tags.get(Tag.ImageDescription);
  let raw: Uint8Array | undefined;
  let ome: Ome | undefined;
  if (ifd0.types.get(Tag.ImageDescription) === 2 && description instanceof Uint8Array) {
    const nul = description.indexOf(0);
    const d = nul < 0 ? description : description.subarray(0, nul);
    let text: string | undefined;
    try {
      text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(d);
    } catch {
      text = undefined;
    }
    if (text !== undefined) {
      ome = parseOme(text);
      if (ome !== undefined) raw = d;
    }
  }
  const f = format(ifd0);
  if (!JPEG2000.has(f.compression) && f.predictor !== 1) reject(`unsupported predictor ${f.predictor}`);

  // §3.3: planes, in (t, c, z) order, as main-chain IFD indices.
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

  // §3.4: pyramid levels.
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
        if (sameFormat(ifd, ifd0) && num(ifd, Tag.ImageWidth) < prev.width && num(ifd, Tag.ImageLength) < prev.height) {
          levels.push(level([ifd]));
        }
      }
    }
  }
  for (const l of levels) {
    if (!l.ifds.every((i) => sameFormat(i, ifd0))) reject("pyramid levels differ in sample format or compression");
  }

  // §3.5: data type and codecs.
  const contig = f.spp > 1 && f.planar === 1;
  const axes: { name: string; type: string; unit?: string; size: number }[] = [];
  const unit = (d: string) =>
    physical(px, d) === undefined ? undefined : UNITS[px[`PhysicalSize${d}Unit`] ?? "µm"];
  if (sizeT > 1) axes.push({ name: "t", type: "time", size: sizeT });
  if (sizeC > 1) axes.push({ name: "c", type: "channel", size: sizeC });
  if (sizeZ > 1) axes.push({ name: "z", type: "space", unit: unit("Z"), size: sizeZ });
  axes.push({ name: "y", type: "space", unit: unit("Y"), size: 0 });
  axes.push({ name: "x", type: "space", unit: unit("X"), size: 0 });
  const cAxis = axes.findIndex((a) => a.name === "c");

  const dataType = dtype(f.bits, f.sampleFormat);
  const itemsize = f.bits / 8;
  let codecs: unknown[];
  let codecName: string;
  if (JPEG2000.has(f.compression)) {
    codecs = [{ name: "imagecodecs_jpeg2k" }];
    codecName = "imagecodecs_jpeg2k";
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

  // §3.6: output.
  const sources: Source[] = [{ url }];
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
              const range: Range = { source: 0, offset: BigInt(offsets[k]), length: BigInt(counts[k]) };
              entries.push({ key: `${li}/c/${coords.join("/")}`, ranges: [range] });
              references++;
            }
          }
        }
      }
    }
    const scale = axes.map((a) => {
      if (a.name === "y") return (physical(px, "Y") ?? 1) * (base.height / l.height);
      if (a.name === "x") return (physical(px, "X") ?? 1) * (base.width / l.width);
      if (a.name === "z") return physical(px, "Z") ?? 1;
      return 1;
    });
    datasets.push({ path: String(li), coordinateTransformations: [{ type: "scale", scale }] });
  }
  const name = ome?.name || undefined;
  meta.push({
    key: "zarr.json",
    bytes: json({
      zarr_format: 3,
      node_type: "group",
      attributes: {
        ome: {
          version: "0.5",
          multiscales: [{
            ...(name === undefined ? {} : { name }),
            axes: axes.map(({ name, type, unit }) => ({ name, type, ...(unit ? { unit } : {}) })),
            datasets,
          }],
        },
      },
    }),
  });
  if (raw !== undefined) meta.push({ key: "OME/METADATA.ome.xml", bytes: raw.slice(), compress: true });
  return {
    sources,
    entries: [...entries, ...meta],
    summary: { name, axes: axes.map((a) => a.name), levels: shapes, references, codec: codecName },
  };
}
