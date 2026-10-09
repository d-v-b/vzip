// Virtualizing a Zeiss CZI file by the CZI profile (spec/virtualize/czi/profile.md, spec/virtualize.md §13).

import { REVISION } from "../revision.ts";
import { batched, type ByteReader, declare, emitPlans, SOURCE_NODE, stringifyJson } from "../../../../../js/src/virtualize/common.ts";
import type { Range } from "../../../../../js/src/protobuf.ts";
import type { ArchiveDesc, EntryDesc } from "../../../../../js/src/writer.ts";
import { boundedHead, codecChain, codedSize, PIXEL_TYPES, sampleLetters, UNCOMPRESSED, ZSTD1 } from "./coding.ts";
import {
  classify, compareTuples, conforming, type Form, type Level, type Placed, layer, planeAxes, rowBand, SERIES_LETTERS,
} from "./layout.ts";
import {
  ATTACH, ATTDIR, CziError, dim, DIRECTORY, FILE, guid, METADATA, readAttachments, readDirectory,
  readFileHeader, readMetadataSegment, readSubblocks, reject, SUBBLOCK, walk,
} from "./segments.ts";
import {
  attachmentPlans, directoryPlans, metadataPlans, nodeMetadata, segmentPlans, subblockPlans, tailPlan,
} from "./source.ts";
import { emptyValues, MAX_XML, readXmlValues, type XmlValues } from "./xml.ts";

export { CziError };

const MAX_IMAGES = 2 ** 16; // series with an image (spec/virtualize/czi/profile.md §13.3)
const MAX_LEVELS = 64; // levels per image
const MAX_EXTENT = 2 ** 31; // each dimension of an array's shape
const MAX_OMERO = 64; // an image's channel indexes, for omero
const MAX_NAME = 256; // bytes of a name in UTF-8
const TILES = "tiles";
const MAGIC = "ZISRAWFILE";

const utf8 = new TextEncoder();
const TYPES: Record<string, string> = { t: "time", c: "channel", z: "space", y: "space", x: "space" };

/** True if `head` (the file's first bytes) starts with a CZI file header's segment id. */
export function isCzi(head: Uint8Array): boolean {
  if (head.length < 16) return false;
  for (let i = 0; i < 16; i++) if (head[i] !== (i < MAGIC.length ? MAGIC.charCodeAt(i) : 0)) return false;
  return true;
}

const nameOf = (v: string | undefined) => (v !== undefined && v !== "" && utf8.encode(v).length <= MAX_NAME ? v : undefined);

function extent(v: number): number {
  if (v > MAX_EXTENT) reject(`an array dimension of ${v}, more than 2^31`);
  return v;
}

function dimensions(series: number[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [k, letter] of [...SERIES_LETTERS].entries()) if (series[k] !== -Infinity) out[letter] = series[k];
  return out;
}

type Length = number | (number | number[])[];

/** An array's zarr.json, with the rectilinear chunk grid when a length is not one integer. */
function arrayDoc(shape: number[], dataType: string, lengths: Length[], codecs: unknown[], axes: string[]) {
  const regular = lengths.every((v) => typeof v === "number");
  return {
    zarr_format: 3,
    node_type: "array",
    shape,
    data_type: dataType,
    chunk_grid: regular
      ? { name: "regular", configuration: { chunk_shape: lengths } }
      : { name: "rectilinear", configuration: { kind: "inline", chunk_shapes: lengths } },
    chunk_key_encoding: { name: "default", configuration: { separator: "/" } },
    fill_value: dataType === "complex64" ? [0, 0] : 0,
    codecs,
    dimension_names: axes,
    attributes: {} as Record<string, unknown>,
  };
}

function codecs(form: Form, axes: string[]): unknown[] {
  const p = PIXEL_TYPES[form[0]][2];
  const stored = axes.filter((a) => a !== "c").concat("c");
  return [...(p > 1 ? [{ name: "transpose", configuration: { order: stored.map((a) => axes.indexOf(a)) } }] : []),
    ...codecChain(...form)];
}

/** The ranges of a subblock's chunks along y, in order: one, or one per band of `band` rows. */
function chunkRanges(s: Placed, data: Map<number, [number, number]>, band: number | undefined, q: number): [number, number][] {
  const [start, n] = data.get(s.index)!;
  const comp = s.form[1];
  if (comp === UNCOMPRESSED) {
    if (band === undefined || band >= s.h) return [[start, s.w * s.h * q]];
    const row = s.w * q;
    return Array.from({ length: Math.floor(s.h / band) }, (_, k): [number, number] => [start + k * band * row, band * row]);
  }
  if (comp === ZSTD1) return [[start + s.header, n - s.header]];
  return [[start, n]];
}

function omero(values: XmlValues, form: Form, loC: number, channels: number): object | undefined {
  const [pixelType, compression] = form;
  const [, dataType, p] = PIXEL_TYPES[pixelType];
  if (channels * p > MAX_OMERO) return undefined;
  const letters = sampleLetters(pixelType, compression);
  const colors: Record<string, string> = { B: "0000FF", G: "00FF00", R: "FF0000", A: "FFFFFF" };
  const typeBits = ({ uint8: 8, uint16: 16 } as Record<string, number>)[dataType];
  const out: object[] = [];
  for (let c = loC; c < loC + channels; c++) {
    const info = c >= 0 && c < values.info.length ? values.info[c] : undefined;
    const display = c >= 0 && c < values.display.length ? values.display[c] : undefined;
    const label = nameOf(info?.name) ?? nameOf(display?.name) ?? `C${c}`;
    const color = display?.color || info?.color || "FFFFFF";
    let window = {};
    if (typeBits !== undefined) {
      const bits = [info?.bits, values.bits].find((b) => b !== undefined && b >= 1 && b <= typeBits) ?? typeBits;
      const top = 2 ** bits - 1, full = 2 ** typeBits - 1;
      const low = display?.low, high = display?.high;
      window = {
        window: { min: 0, max: top, start: low !== undefined ? low * full : 0, end: high !== undefined ? high * full : top },
      };
    }
    for (const letter of letters) {
      out.push({ label: letter ? `${label} ${letter}` : label, color: letter ? colors[letter] : color, active: true, ...window });
    }
  }
  return { channels: out };
}

function imageOme(
  axes: string[], units: Record<string, string | undefined>, scales: number[][], name: string | undefined,
  translations: number[][],
): Record<string, unknown> {
  for (const t of translations) if (!t.every(Number.isFinite)) reject("a translation is not finite");
  const ms: Record<string, unknown> = {};
  if (name !== undefined) ms.name = name;
  ms.axes = axes.map((a) => ({ name: a, type: TYPES[a], ...(units[a] ? { unit: units[a] } : {}) }));
  ms.datasets = scales.map((s, i) => ({
    path: String(i),
    coordinateTransformations: [{ type: "scale", scale: s }, { type: "translation", translation: translations[i] }],
  }));
  return { version: "0.5", multiscales: [ms] };
}

export interface CziSummary {
  subblocks: number;
  images: number[][][];
  tiles: number;
  unplaced: number;
  segments: number;
  attachments: number;
  tail: number;
}

/** Describes the CZI file at `url` (read through `read`) by the CZI profile. `exact`,
 * when given, reads exactly the ranges asked for, past `read`'s block cache: the
 * subblocks' headers and codec headers, scattered through the file, are read with it. */
export async function virtualizeCzi(
  url: string, read: ByteReader, fileSize: number, exact: ByteReader = read,
): Promise<ArchiveDesc & { summary: CziSummary }> {
  const fh = await readFileHeader(read, fileSize);
  const d = await readDirectory(read, fileSize, fh.directory, exact);
  const [s, ahead] = await readSubblocks(read, fileSize, d, exact);
  const meta = fh.metadata ? await readMetadataSegment(read, fileSize, fh.metadata) : undefined;
  const [attAllocated, atts] = fh.attachments ? await readAttachments(read, fileSize, fh.attachments, exact) : [0, []];
  const values = meta && meta.xml > 0 && meta.xml <= MAX_XML
    ? readXmlValues(await read(meta.offset + 288, meta.xml))
    : emptyValues();

  // Placement (spec/virtualize/czi.md §3.2): each agreeing subblock's coded size, its
  // codec header read ahead with the subblocks' headers.
  const starts = (i: number) => d.filePosition[i] + 32 + s.length[i] + s.metadata[i];
  const agreeing = [...s.agrees.keys()].filter((i) => s.agrees[i]);
  const coded = await batched(agreeing, async (i) => {
    return codedSize(d.pixelType[i], d.compression[i], dim(d, i, "X")![2], dim(d, i, "Y")![2], s.data[i],
      boundedHead(ahead.read, starts(i), s.data[i]));
  });
  const codedOf = new Map(agreeing.map((i, k) => [i, coded[k]]));
  const placed: Placed[] = [];
  const unplaced: number[] = [], trailing: [number, number, number][] = [];
  const data = new Map<number, [number, number]>();
  for (let i = 0; i < d.count; i++) {
    const c = codedOf.get(i);
    if (!s.agrees[i] || c === null || c === undefined) {
      unplaced.push(i);
      continue;
    }
    const pt = d.pixelType[i], comp = d.compression[i], n = s.data[i];
    const x = dim(d, i, "X")!, y = dim(d, i, "Y")!;
    const start = starts(i);
    const series = [...SERIES_LETTERS].map((letter) => dim(d, i, letter)?.[0] ?? -Infinity);
    const plane = [..."TCZ"].map((letter) => dim(d, i, letter)?.[0] ?? 0) as [number, number, number];
    placed.push({
      index: i, series, seriesKey: series.join(","), plane, x: x[0], y: y[0], wl: x[1], hl: y[1], w: x[2], h: y[2],
      cw: c[0], ch: c[1], form: [pt, comp, c[2]], header: c[3], layer: layer(x[1], y[1], x[2], y[2]),
    });
    data.set(i, [start, n]);
    const pixels = x[2] * y[2] * PIXEL_TYPES[pt][3];
    if (comp === UNCOMPRESSED && n > pixels) trailing.push([i, start + pixels, n - pixels]);
  }

  // Series, layers, levels and tiles (§3.3–§3.5).
  const bySeries = new Map<string, Placed[]>();
  for (const p of placed) {
    const list = bySeries.get(p.seriesKey);
    if (list === undefined) bySeries.set(p.seriesKey, [p]);
    else list.push(p);
  }
  const images: [number[], Level[]][] = [];
  let tiles: Placed[] = [];
  for (const members of [...bySeries.values()].sort((a, b) => compareTuples(a[0].series, b[0].series))) {
    const layers = new Map<string, [[number, number], Placed[]]>();
    for (const p of members) {
      if (p.layer !== undefined && conforming(p)) {
        const key = p.layer.join(",");
        const entry = layers.get(key);
        if (entry === undefined) layers.set(key, [p.layer, [p]]);
        else entry[1].push(p);
      } else {
        tiles.push(p);
      }
    }
    const levels: Level[] = [];
    for (const [, b] of [...layers.values()].sort((a, b) => compareTuples(a[0], b[0]))) {
      const level = classify(b);
      if (level === undefined) tiles.push(...b);
      else levels.push(level);
    }
    levels.sort((a, b) => compareTuples([a.factor, ...a.layer], [b.factor, ...b.layer]));
    if (levels.length) {
      const kept = levels.filter((lv) => lv.form[0] === levels[0].form[0]);
      for (const lv of levels) if (lv.form[0] !== levels[0].form[0]) tiles.push(...lv.cells.map((c) => c[0]));
      if (kept.length > MAX_LEVELS) reject(`an image of ${kept.length} levels, more than ${MAX_LEVELS}`);
      images.push([members[0].series, kept]);
    }
  }
  if (images.length > MAX_IMAGES) reject(`${images.length} series with an image, more than ${MAX_IMAGES}`);

  const entries: EntryDesc[] = [];
  const json = (key: string, v: unknown, compress = false) =>
    entries.push({ key, bytes: utf8.encode(stringifyJson(v)), ...(compress ? { compress } : {}) });
  const ref = (key: string, [o, n]: [number, number]) =>
    entries.push({ key, ranges: [{ source: 0, offset: BigInt(o), length: BigInt(n) } as Range] });
  const group = (attributes: unknown) => ({ zarr_format: 3, node_type: "group", attributes });
  const rootS = {
    version: [fh.major, fh.minor], primary_file_guid: guid(fh.primaryFileGuid), file_guid: guid(fh.fileGuid),
    file_part: fh.filePart, update_pending: fh.updatePending,
  };
  const rootOme = images.length ? { ome: { version: "0.5", "bioformats2raw.layout": 3 } } : {};
  json("zarr.json", group(declare(rootOme, "czi", url, rootS, REVISION)));
  if (images.length) {
    json("OME/zarr.json", group({ ome: { version: "0.5", series: images.map((_, k) => String(k)) } }));
  }
  const levelsSummary: number[][][] = [];
  for (const [k, [key, levels]] of images.entries()) {
    emitImage(json, ref, k, key, levels, values, data);
    levelsSummary.push(levels.map((lv) => [lv.factor, lv.grid[0], lv.grid[1]]));
  }

  // Tiles, one array per tile position and copy (§4.4).
  const groups = new Map<string, Placed[]>();
  const copies = new Map<string, number>();
  tiles = [...tiles].sort((a, b) => a.index - b.index);
  const copyOf = new Map<Placed[], number>();
  for (const p of tiles) {
    const g = [p.seriesKey, p.x, p.y, p.wl, p.hl, p.w, p.h, p.cw, p.ch, ...p.form].join(";");
    const planeKey = `${g}|${p.plane.join(",")}`;
    const copy = copies.get(planeKey) ?? 0;
    copies.set(planeKey, copy + 1);
    const groupKey = `${g}|${copy}`;
    const members = groups.get(groupKey);
    if (members === undefined) {
      const created = [p];
      groups.set(groupKey, created);
      copyOf.set(created, copy);
    } else {
      members.push(p);
    }
  }
  if (groups.size) json(`${TILES}/zarr.json`, group({}), true);
  for (const [n, members] of [...groups.values()].entries()) {
    emitTile(json, ref, `${TILES}/${n}`, members, copyOf.get(members)!, data);
  }

  // The source metadata node (§5).
  const known = new Map<number, [Uint8Array, number, number]>([[0, [FILE, fh.allocated, 0]],
    [fh.directory, [DIRECTORY, d.allocated, 0]]]);
  for (let i = 0; i < d.count; i++) known.set(d.filePosition[i], [SUBBLOCK, s.allocated[i], 0]);
  if (meta !== undefined) known.set(meta.offset, [METADATA, meta.allocated, 0]);
  if (fh.attachments) known.set(fh.attachments, [ATTDIR, attAllocated, 0]);
  for (const a of atts) if (a.a1) known.set(a.offset, [ATTACH, a.allocated, 0]);
  const [segments, tail] = await walk(read, fileSize, known);
  const others = segments.filter(([o]) => !known.has(o));
  const [listed, attPlans, groupPlans] = await attachmentPlans(read, atts);
  const [node, indexPlans] = nodeMetadata(listed);
  const plans = [
    ...(await directoryPlans(d)), ...(await subblockPlans(read, d, s, unplaced, trailing)), ...metadataPlans(meta),
    ...attPlans, ...indexPlans, ...(await segmentPlans(read, others)), ...tailPlan(tail, fileSize),
  ];
  if (Object.keys(node).length || plans.length) {
    const emitted = emitPlans(plans, declare({}, "czi", undefined, node));
    for (const [path, attributes] of groupPlans) {
      const key = `${SOURCE_NODE}/${path}/zarr.json`;
      const bytes = utf8.encode(stringifyJson(group(attributes)));
      const at = emitted.findIndex((e) => e.key === key);
      if (at < 0) emitted.push({ key, bytes, compress: true });
      else emitted[at] = { key, bytes, compress: true };
    }
    entries.push(...emitted);
  }
  return {
    sources: [{ url }],
    entries,
    summary: {
      subblocks: d.count, images: levelsSummary, tiles: groups.size, unplaced: unplaced.length, segments: others.length,
      attachments: atts.length, tail: fileSize - tail,
    },
  };
}

type Json = (key: string, v: unknown, compress?: boolean) => void;
type Ref = (key: string, range: [number, number]) => void;

function emitImage(
  json: Json, ref: Ref, k: number, key: number[], levels: Level[], values: XmlValues, data: Map<number, [number, number]>,
): void {
  const pixelType = levels[0].form[0];
  const [, dataType, p, q] = PIXEL_TYPES[pixelType];
  const [axes, lo, ext] = planeAxes(levels.flatMap((lv) => lv.cells.map((c) => c[0].plane)), p);
  const dims = dimensions(key);
  const name = "S" in dims ? nameOf(values.scenes.get(dims.S)) : undefined;
  const um = 1 / 1e-6; // metres in micrometres (conventions §5)
  const px = values.px !== undefined ? values.px * um : undefined;
  const py = values.py !== undefined ? values.py * um : undefined;
  const units: Record<string, string | undefined> = {
    x: px !== undefined ? "micrometer" : undefined, y: py !== undefined ? "micrometer" : undefined,
    z: values.pz !== undefined ? "micrometer" : undefined, t: values.inc !== undefined ? "second" : undefined,
  };
  const scales: number[][] = [], translations: number[][] = [];
  for (const lv of levels) {
    const f = lv.factor;
    const scale: Record<string, number> = {
      t: values.inc ?? 1, c: 1, z: values.pz !== undefined ? values.pz * um : 1,
      y: py !== undefined ? py * f : f, x: px !== undefined ? px * f : f,
    };
    const shift: Record<string, number> = {
      x: (lv.origin[0] + (f - 1) / 2) * (px !== undefined ? px : 1),
      y: (lv.origin[1] + (f - 1) / 2) * (py !== undefined ? py : 1),
    };
    scales.push(axes.map((a) => scale[a]));
    translations.push(axes.map((a) => shift[a] ?? 0));
  }
  const ome = imageOme(axes, units, scales, name, translations);
  const o = omero(values, levels[0].form, lo.c, ext.c);
  if (o !== undefined) ome.omero = o;
  json(`${k}/zarr.json`, {
    zarr_format: 3, node_type: "group",
    attributes: declare({ ome }, "czi", undefined, Object.keys(dims).length ? { dimensions: dims } : undefined),
  });
  for (const [di, lv] of levels.entries()) {
    const [w, h] = lv.tile, [w2, h2] = lv.edge, [m, r] = lv.grid;
    const band = lv.form[1] === UNCOMPRESSED ? rowBand(h, h2, w, q) : h;
    const banded = band < h;
    const size: Record<string, number> = {
      t: extent(ext.t), c: extent(ext.c * p), z: extent(ext.z), y: extent((r - 1) * h + h2), x: extent((m - 1) * w + w2),
    };
    const yLen: Length = banded || h2 === h ? band : r > 1 ? [[h, r - 1], h2] : [h2];
    const xLen: Length = w2 === w ? w : m > 1 ? [[w, m - 1], w2] : [w2];
    const lengths: Record<string, Length> = { t: 1, c: p, z: 1, y: yLen, x: xLen };
    const path = `${k}/${di}`;
    json(`${path}/zarr.json`, arrayDoc(axes.map((a) => size[a]), dataType, axes.map((a) => lengths[a]),
      codecs(lv.form, axes), axes));
    const perTile = Math.floor(h / band);
    for (const [sb, col, row] of lv.cells) {
      const index: Record<string, number> = { t: sb.plane[0] - lo.t, c: sb.plane[1] - lo.c, z: sb.plane[2] - lo.z };
      for (const [bk, rng] of chunkRanges(sb, data, banded ? band : undefined, q).entries()) {
        index.y = row * perTile + bk;
        index.x = col;
        ref(`${path}/c/` + axes.map((a) => index[a]).join("/"), rng);
      }
    }
  }
}

function emitTile(
  json: Json, ref: Ref, path: string, members: Placed[], copy: number, data: Map<number, [number, number]>,
): void {
  const first = members[0];
  const [, dataType, p, q] = PIXEL_TYPES[first.form[0]];
  const [axes, lo, ext] = planeAxes(members.map((m) => m.plane), p);
  const band = first.form[1] === UNCOMPRESSED ? rowBand(first.ch, first.ch, first.cw, q) : first.ch;
  const size: Record<string, number> = {
    t: extent(ext.t), c: extent(ext.c * p), z: extent(ext.z), y: first.ch, x: first.cw,
  };
  const lengths: Record<string, number> = { t: 1, c: p, z: 1, y: band, x: first.cw };
  const dims = dimensions(first.series);
  const own = {
    ...(Object.keys(dims).length ? { dimensions: dims } : {}), x: first.x, y: first.y, size: [first.wl, first.hl],
    stored_size: [first.w, first.h], planes: { t: lo.t, c: lo.c, z: lo.z }, copy,
  };
  const doc = arrayDoc(axes.map((a) => size[a]), dataType, axes.map((a) => lengths[a]), codecs(first.form, axes), axes);
  doc.attributes = declare({}, "czi", undefined, own);
  json(`${path}/zarr.json`, doc, true);
  for (const sb of members) {
    const index: Record<string, number> = { t: sb.plane[0] - lo.t, c: sb.plane[1] - lo.c, z: sb.plane[2] - lo.z, x: 0 };
    for (const [bk, rng] of chunkRanges(sb, data, band < first.ch ? band : undefined, q).entries()) {
      index.y = bk;
      ref(`${path}/c/` + axes.map((a) => index[a]).join("/"), rng);
    }
  }
}
