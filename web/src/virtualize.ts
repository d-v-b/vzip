// Describing a tiled (OME-)TIFF as an OME-NGFF 0.5 multiscale image whose
// chunks are vzip references to the TIFF's tiles.
//
// Supported: tiled images; pyramid levels as SubIFDs (OME-TIFF) or, without
// OME-XML, as later main-chain IFDs of decreasing size (e.g. SVS); OME planes
// over Z, C and T in any DimensionOrder (single-file); samples per pixel
// stored planar or interleaved; uncompressed, DEFLATE, zstd and JPEG 2000
// tiles without a predictor.

import type { Range, Source } from "./protobuf.ts";
import { type ByteReader, type Ifd, num, nums, readTiff, Tag, TiffError } from "./tiff.ts";
import type { ArchiveDesc, EntryDesc } from "./writer.ts";

const JPEG2000 = new Set([33003, 33004, 33005, 34712]);

interface Pixels {
  attrs: Record<string, string>;
  tiffData: Record<string, string>[];
  name?: string;
}

function decodeXml(s: string): string {
  return s.replace(/&(?:#x([0-9a-fA-F]+)|#(\d+)|(amp|lt|gt|quot|apos));/g, (_, hex, dec, named) =>
    hex ? String.fromCodePoint(parseInt(hex, 16))
      : dec ? String.fromCodePoint(Number(dec))
      : ({ amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" } as Record<string, string>)[named]);
}

function attributes(tag: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of tag.matchAll(/([\w:]+)\s*=\s*"([^"]*)"/g)) out[m[1]] = decodeXml(m[2]);
  return out;
}

/** The first image's Pixels element of an OME-XML document. */
export function parseOmePixels(xml: string): Pixels | undefined {
  const image = xml.match(/<(?:\w+:)?Image\b[^>]*>/);
  const start = xml.search(/<(?:\w+:)?Pixels\b/);
  if (start < 0) return undefined;
  const end = xml.indexOf("Pixels>", start);
  const body = xml.slice(start, end < 0 ? undefined : end);
  const tag = body.match(/^<(?:\w+:)?Pixels\b[^>]*>/)![0];
  const tiffData = [...body.matchAll(/<(?:\w+:)?TiffData\b[^>]*?(\/>|>[\s\S]*?<\/(?:\w+:)?TiffData>)/g)].map(
    (m) => {
      const attrs = attributes(m[0].match(/^<[^>]*>/)![0]);
      const uuid = m[0].match(/<(?:\w+:)?UUID\b[^>]*>/);
      if (uuid && attributes(uuid[0]).FileName !== undefined) attrs.FileName = attributes(uuid[0]).FileName;
      return attrs;
    },
  );
  return { attrs: attributes(tag), tiffData, name: image ? attributes(image[0]).Name : undefined };
}

const UNITS: Record<string, string> = {
  "µm": "micrometer", "um": "micrometer", "μm": "micrometer", "nm": "nanometer",
  "mm": "millimeter", "cm": "centimeter", "m": "meter", "Å": "angstrom", "pm": "picometer",
  "in": "inch", "ft": "foot", "s": "second", "ms": "millisecond", "min": "minute", "h": "hour",
};

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
  const spp = num(ifd, Tag.SamplesPerPixel, 1);
  if (bits.some((b) => b !== bits[0])) throw new TiffError("samples of different sizes");
  return {
    bits: bits[0],
    spp,
    sampleFormat: num(ifd, Tag.SampleFormat, 1),
    planar: spp > 1 ? num(ifd, Tag.PlanarConfiguration, 1) : 1,
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
    throw new TiffError(`only tiled TIFFs are supported; the image at ${stripped.offset} is stored in strips`);
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
      throw new TiffError("planes of one pyramid level differ in size, tiling or format");
    }
  }
  return l;
}

function dtype(bits: number, sampleFormat: number): string {
  const kind = { 1: "uint", 2: "int", 3: "float" }[sampleFormat];
  if (kind === undefined || ![8, 16, 32, 64].includes(bits) || (kind === "float" && bits < 32)) {
    throw new TiffError(`unsupported sample type: ${bits}-bit, SampleFormat ${sampleFormat}`);
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
  if (ifd0 === undefined) throw new TiffError("no images");
  const description = ifd0.tags.get(Tag.ImageDescription);
  const xml = typeof description === "string" && description.includes("<OME") ? description : undefined;
  const ome = xml === undefined ? undefined : parseOmePixels(xml);
  const f = format(ifd0);
  if (!JPEG2000.has(f.compression) && f.predictor !== 1) {
    throw new TiffError(`unsupported predictor ${f.predictor}`);
  }

  // Planes, in (t, c, z) order, as indices into the main IFD chain.
  const sizeZ = Number(ome?.attrs.SizeZ ?? 1);
  const sizeT = Number(ome?.attrs.SizeT ?? 1);
  let sizeC = Number(ome?.attrs.SizeC ?? f.spp);
  if (f.spp > 1 && sizeC !== f.spp) {
    if (sizeC === 1) sizeC = f.spp;
    else throw new TiffError(`SizeC ${sizeC} with ${f.spp} samples per pixel is not supported`);
  }
  const planeC = f.spp > 1 ? 1 : sizeC; // channels stored as separate planes
  const plane = (t: number, c: number, z: number) => (t * planeC + c) * sizeZ + z;
  const planeIfd = new Array<number>(sizeT * planeC * sizeZ).fill(-1);
  if (ome === undefined) {
    planeIfd[0] = 0;
  } else {
    const order = (ome.attrs.DimensionOrder ?? "XYZCT").slice(2); // e.g. "ZCT"
    const size = { Z: sizeZ, C: planeC, T: sizeT } as Record<string, number>;
    const step = (pos: Record<string, number>) => {
      for (const d of order) {
        if (++pos[d] < size[d]) return true;
        pos[d] = 0;
      }
      return false;
    };
    const entries = ome.tiffData.length > 0 ? ome.tiffData : [{}];
    for (const td of entries) {
      if (td.FileName !== undefined) throw new TiffError("multi-file OME-TIFF is not supported");
      const pos = { Z: Number(td.FirstZ ?? 0), C: Number(td.FirstC ?? 0), T: Number(td.FirstT ?? 0) };
      let ifd = Number(td.IFD ?? 0);
      let count = Number(td.PlaneCount ?? (entries.length === 1 && td.IFD === undefined ? planeIfd.length : 1));
      while (count-- > 0) {
        planeIfd[plane(pos.T, pos.C, pos.Z)] = ifd++;
        if (!step(pos)) break;
      }
    }
  }
  if (planeIfd.some((i) => i < 0 || i >= tiff.ifds.length)) {
    throw new TiffError("OME-XML planes do not match the TIFF's images");
  }
  const planes = planeIfd.map((i) => tiff.ifds[i]);

  // Pyramid levels.
  const levels: Level[] = [];
  if (ifd0.subIfds.length > 0) {
    for (let k = -1; k < ifd0.subIfds.length; k++) {
      levels.push(level(planes.map((p) => (k < 0 ? p : p.subIfds[k]))));
    }
  } else {
    levels.push(level(planes));
    if (ome === undefined) {
      for (const ifd of tiff.ifds.slice(1)) {
        const prev = levels[levels.length - 1];
        if (
          tiled(ifd) && sameFormat(ifd, ifd0) &&
          num(ifd, Tag.ImageWidth) < prev.width && num(ifd, Tag.ImageLength) < prev.height
        ) {
          levels.push(level([ifd]));
        }
      }
    }
  }
  for (const l of levels) {
    if (!l.ifds.every(tiled)) throw new TiffError("only tiled TIFFs are supported");
    if (!l.ifds.every((i) => sameFormat(i, ifd0))) {
      throw new TiffError("pyramid levels differ in sample format or compression");
    }
  }

  // Axes: t, c, z (when larger than 1), y, x.
  const contig = f.spp > 1 && f.planar === 1;
  const axes: { name: string; type: string; unit?: string; size: number }[] = [];
  const physical = (d: string) => {
    const v = ome?.attrs[`PhysicalSize${d}`];
    return v === undefined ? undefined : Number(v);
  };
  const unit = (d: string) =>
    physical(d) === undefined ? undefined
      : UNITS[ome?.attrs[`PhysicalSize${d}Unit`] ?? "µm"] ?? undefined;
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
      throw new TiffError(`unsupported compression ${f.compression}`);
    }
  }
  if (contig) {
    // Tiles hold [y, x, sample]; move the channel axis last.
    const order = [...axes.keys()].filter((i) => i !== cAxis).concat([cAxis]);
    codecs.unshift({ name: "transpose", configuration: { order } });
  }

  const sources: Source[] = [{ url }];
  const entries: EntryDesc[] = [];
  const meta: EntryDesc[] = [];
  const utf8 = new TextEncoder();
  const json = (v: unknown) => utf8.encode(JSON.stringify(v, null, 2));
  const datasets = [];
  let references = 0;
  const shapes: number[][] = [];
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
          if (offsets.length !== samples * perSample || counts.length !== offsets.length) {
            throw new TiffError(`IFD at ${ifd.offset} has ${offsets.length} tiles, expected ${samples * perSample}`);
          }
          for (let s = 0; s < samples; s++) {
            for (let j = 0; j < perSample; j++) {
              const k = s * perSample + j;
              if (counts[k] === 0) continue; // missing tile: fill value
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
    const [base] = levels;
    const scale = axes.map((a) => {
      if (a.name === "y") return (physical("Y") ?? 1) * (base.height / l.height);
      if (a.name === "x") return (physical("X") ?? 1) * (base.width / l.width);
      if (a.name === "z") return physical("Z") ?? 1;
      return 1;
    });
    datasets.push({ path: String(li), coordinateTransformations: [{ type: "scale", scale }] });
  }
  const name = ome?.name;
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
  if (xml !== undefined) {
    meta.push({ key: "OME/METADATA.ome.xml", bytes: utf8.encode(xml), compress: true });
  }
  return {
    sources,
    entries: [...entries, ...meta],
    summary: { name, axes: axes.map((a) => a.name), levels: shapes, references, codec: codecName },
  };
}
