// Virtualizing an Imaris IMS file (HDF5) by the IMS profile (profiles/ims.md,
// §8): every chunk of every resolution level, time point and channel becomes a
// Zarr chunk that references the file.

import type { Range } from "../../protobuf.ts";
import { type ByteReader, declare } from "../common.ts";
import { sourceJson } from "./source.ts";
import type { ArchiveDesc, EntryDesc } from "../../writer.ts";
import { attributeValue, type Datatype, Hdf5, ImsError, latin1, type Links, u, untilNul } from "./hdf5.ts";

export { ImsError } from "./hdf5.ts";

const MAX_LEVELS = 64;
const MAX_DATASETS = 100000;
const DECIMAL = /^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$/;
const DIGITS = /^[0-9]+$/;
const TIMESTAMP = /^([0-9]{4})-([0-9]{2})-([0-9]{2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]+))?$/;

// The length units of conventions §5, by symbol.
const LENGTH_UNITS: Record<string, string> = {
  "\u00b5m": "micrometer", "\u03bcm": "micrometer", "um": "micrometer", "nm": "nanometer", "mm": "millimeter",
  "cm": "centimeter", "m": "meter", "\u00c5": "angstrom", "\u212b": "angstrom", "pm": "picometer", "in": "inch",
  "ft": "foot",
};

const reject = (message: string): never => {
  throw new ImsError(message);
};

const trim = (s: string) => s.replace(/^[ \t\r\n]+|[ \t\r\n]+$/g, "");

// ---- attribute values (conventions/ims/README.md §2.1)

/** The text of a string attribute, or undefined if it is absent. */
function text(attrs: Map<string, Uint8Array>, name: string): string | undefined {
  const d = attrs.get(name);
  if (d === undefined) return undefined;
  const [datatype, data] = attributeValue(d);
  if (datatype.cls !== 3) reject(`attribute ${name} is not a string`);
  const raw = untilNul(data);
  try {
    return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(raw);
  } catch {
    return latin1(raw);
  }
}

/** A decimal number, or undefined if `s` is absent or not one. */
function decimal(s: string | undefined): number | undefined {
  if (s === undefined || !DECIMAL.test(trim(s))) return undefined;
  const v = Number(trim(s));
  return Number.isFinite(v) ? v : undefined;
}

/** `n` decimal numbers separated by whitespace, or undefined. */
function decimals(s: string | undefined, n: number): number[] | undefined {
  if (s === undefined) return undefined;
  const parts = trim(s).split(/[ \t\r\n]+/);
  if (parts.length !== n) return undefined;
  const values = parts.map(decimal);
  return values.every((v) => v !== undefined) ? (values as number[]) : undefined;
}

/** Days from 1970-01-01 to y-m-d in the proleptic Gregorian calendar. */
function days(y: number, m: number, d: number): number {
  y -= m <= 2 ? 1 : 0;
  const era = Math.floor(y / 400);
  const yoe = y - era * 400;
  const doy = Math.floor((153 * (m + (m > 2 ? -3 : 9)) + 2) / 5) + d - 1;
  const doe = yoe * 365 + Math.floor(yoe / 4) - Math.floor(yoe / 100) + doy;
  return era * 146097 + doe - 719468;
}

/** [whole seconds from 1970-01-01 00:00:00, fraction of a second], or undefined (conventions/ims/README.md §3). */
function timestamp(s: string | undefined): [number, number] | undefined {
  const m = s === undefined ? null : s.match(TIMESTAMP);
  if (m === null) return undefined;
  const [y, mo, d, h, mi, sec] = m.slice(1, 7).map(Number);
  const leap = y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0);
  const monthDays = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (!(mo >= 1 && mo <= 12 && d >= 1 && d <= monthDays[mo - 1] && h <= 23 && mi <= 59 && sec <= 59)) {
    return undefined;
  }
  return [days(y, mo, d) * 86400 + h * 3600 + mi * 60 + sec, Number("0." + (m[7] ?? "0"))];
}

/** [Zarr data type, byte order or undefined for one byte, integer range or
 * undefined] of a Data dataset (conventions/ims/README.md §2.2). */
function dataType(dt: Datatype): [string, string | undefined, [number, number] | undefined] {
  const order = dt.bits & 1 ? "big" : "little";
  if (dt.cls === 0 && (dt.size === 1 || dt.size === 2 || dt.size === 4)) {
    if (u(dt.props, 0, 2) !== 0 || u(dt.props, 2, 2) !== 8 * dt.size) reject("fixed-point data with padding bits");
    const bits = 8 * dt.size;
    const byteOrder = bits > 8 ? order : undefined;
    if (dt.bits & 8) return [`int${bits}`, byteOrder, [-(2 ** (bits - 1)), 2 ** (bits - 1) - 1]];
    return [`uint${bits}`, byteOrder, [0, 2 ** bits - 1]];
  }
  if (dt.cls === 1 && dt.size === 4) {
    const props = [u(dt.props, 0, 2), u(dt.props, 2, 2), ...dt.props.subarray(4, 8), u(dt.props, 8, 4)];
    if (dt.bits & 0x40 || ((dt.bits >> 4) & 3) !== 2 || ((dt.bits >> 8) & 0xff) !== 31 ||
      props.join() !== [0, 32, 23, 8, 0, 23, 127].join()) {
      reject("floating-point data that is not IEEE binary32");
    }
    return ["float32", order, undefined];
  }
  return reject(`unsupported HDF5 datatype (class ${dt.cls}, ${dt.size} bytes)`);
}

export interface ImsSummary {
  levels: number;
  sizes: Record<string, number>;
  dataType: string;
  chunkShape: number[];
  compressed: boolean[];
  chunks: number;
  channels: string[];
}

interface Level {
  sizes: number[]; // z, y, x
  chunk: number[]; // z, y, x
  compressed: boolean;
}

export async function virtualizeIms(
  url: string,
  read: ByteReader,
  fileSize: number,
): Promise<ArchiveDesc & { summary: ImsSummary }> {
  const f = await Hdf5.open(read, fileSize);
  /** The object a group's link `name` leads to. */
  const follow = async (links: Links, name: string) => {
    if (!links.has(name)) reject(`no ${name}`);
    return f.follow(links, name);
  };

  // conventions/ims/README.md §2.2: the levels, time points and channels.
  const root = await f.links(f.root);
  if (!root.has("DataSet")) reject("not an Imaris file (no DataSet group)");
  const dataset = await f.links(await follow(root, "DataSet"));
  if (!dataset.has("ResolutionLevel 0")) reject("not an Imaris file (no ResolutionLevel 0)");
  let levels = 0;
  while (dataset.has(`ResolutionLevel ${levels}`)) {
    levels++;
    if (levels > MAX_LEVELS) reject(`more than ${MAX_LEVELS} resolution levels`);
  }
  const level0 = await f.links(await follow(dataset, "ResolutionLevel 0"));
  let times = 0;
  while (level0.has(`TimePoint ${times}`)) times++;
  if (times === 0) reject("no TimePoint 0");
  const firstTime = await f.links(await follow(level0, "TimePoint 0"));
  let channels = 0;
  while (firstTime.has(`Channel ${channels}`)) channels++;
  if (channels === 0) reject("no Channel 0");
  if (levels * times * channels > MAX_DATASETS) reject(`more than ${MAX_DATASETS} datasets`);

  let type: ReturnType<typeof dataType> | undefined;
  const levelInfo: Level[] = [];
  const chunkRefs: [number, number, number, number[], [number, number]][] = [];
  const channelGroups: [string, number][] = []; // for the source metadata
  for (let r = 0; r < levels; r++) {
    const links = r === 0 ? level0 : await f.links(await follow(dataset, `ResolutionLevel ${r}`));
    let info: Level | undefined;
    for (let t = 0; t < times; t++) {
      const timeLinks = r === 0 && t === 0 ? firstTime : await f.links(await follow(links, `TimePoint ${t}`));
      for (let c = 0; c < channels; c++) {
        const channel = await follow(timeLinks, `Channel ${c}`);
        channelGroups.push([`ResolutionLevel ${r}/TimePoint ${t}/Channel ${c}`, channel]);
        const attrs = await f.attributes(channel);
        const sizes = ["Z", "Y", "X"].map((axis) => {
          const s = text(attrs, `ImageSize${axis}`);
          const v = s === undefined || !DIGITS.test(trim(s)) ? 0 : Number(trim(s));
          if (!(v >= 1 && v <= Number.MAX_SAFE_INTEGER)) reject(`ImageSize${axis} of level ${r} is not a positive integer`);
          return v;
        });
        const ds = await f.dataset(await follow(await f.links(channel), "Data"));
        const dt = dataType(ds.datatype);
        if (ds.dims.length !== 3) reject(`a dataset of level ${r} is not 3-dimensional`);
        if (sizes.some((s, i) => s > ds.dims[i])) reject(`the image of level ${r} is larger than its dataset`);
        if (!(ds.filters.length === 0 || (ds.filters.length === 1 && ds.filters[0] === 1))) {
          reject(`unsupported HDF5 filters [${ds.filters.join(", ")}]`);
        }
        if (type === undefined) type = dt;
        else if (dt[0] !== type[0] || dt[1] !== type[1]) reject("the datasets have different data types");
        const here: Level = { sizes, chunk: ds.chunk, compressed: ds.filters.length > 0 };
        if (info === undefined) info = here;
        else if (JSON.stringify(here) !== JSON.stringify(info)) {
          reject(`the datasets of level ${r} differ in size, chunk shape or filters`);
        }
        for (const [key, ref] of await f.chunks(ds)) chunkRefs.push([r, t, c, key.split("/").map(Number), ref]);
      }
    }
    levelInfo.push(info!);
  }
  const [dtype, endian, range] = type!;

  // conventions/ims/README.md §3: metadata.
  const meta: Links = root.has("DataSetInfo") ? await f.links(await follow(root, "DataSetInfo")) : new Map();
  const metaAttrs = async (name: string) => {
    if (!meta.has(name)) return new Map<string, Uint8Array>();
    const at = await f.follow(meta, name);
    await f.links(at); // it MUST be a group
    return f.attributes(at);
  };
  const image = await metaAttrs("Image");
  const extMin = [0, 1, 2].map((i) => decimal(text(image, `ExtMin${i}`)));
  const extMax = [0, 1, 2].map((i) => decimal(text(image, `ExtMax${i}`)));
  const unitText = text(image, "Unit");
  const name = text(image, "Name");
  const labels: string[] = [], colors: string[] = [], ranges: (number[] | undefined)[] = [];
  for (let c = 0; c < channels; c++) {
    const attrs = await metaAttrs(`Channel ${c}`);
    labels.push(text(attrs, "Name") || `Channel ${c}`);
    const rgb = decimals(text(attrs, "Color"), 3);
    colors.push(rgb !== undefined && rgb.every((v) => v >= 0 && v <= 1)
      ? rgb.map((v) => Math.floor(v * 255 + 0.5).toString(16).toUpperCase().padStart(2, "0")).join("")
      : "FFFFFF");
    ranges.push(decimals(text(attrs, "ColorRange"), 2));
  }
  const timeAttrs = await metaAttrs("TimeInfo");
  let period: number | undefined;
  if (times > 1) {
    const first = timestamp(text(timeAttrs, "TimePoint1"));
    const last = timestamp(text(timeAttrs, `TimePoint${times}`));
    if (first !== undefined && last !== undefined) {
      const elapsed = (last[0] - first[0]) + (last[1] - first[1]);
      if (elapsed > 0) period = elapsed / (times - 1);
    }
  }

  // conventions/ims/README.md §4: output; profiles/ims.md §8.8: the chunk references.
  const z0 = levelInfo[0].sizes[0];
  // A z axis also when chunks hold several z planes, so that they decode to Zarr chunks.
  const hasZ = z0 > 1 || levelInfo.some((l) => l.chunk[0] > 1);
  const axes = [...(times > 1 ? ["t"] : []), ...(channels > 1 ? ["c"] : []), ...(hasZ ? ["z"] : []), "y", "x"];
  if (z0 === 1 && levelInfo.some((l) => l.sizes[0] !== 1)) reject("a level has more than one z plane, and level 0 has one");
  const unit = !unitText ? "micrometer" : Object.hasOwn(LENGTH_UNITS, unitText) ? LENGTH_UNITS[unitText] : undefined;
  const extent: Record<string, number> = {};
  for (const [axis, i] of [["x", 0], ["y", 1], ["z", 2]] as const) {
    if (extMin[i] === undefined || extMax[i] === undefined) continue;
    const e = extMax[i]! - extMin[i]!;
    if (!Number.isFinite(e)) reject(`ExtMax${i} - ExtMin${i} is not finite`);
    if (e > 0) extent[axis] = e;
  }
  const spatial = axes.filter((a) => a === "z" || a === "y" || a === "x");
  const units: Record<string, string | undefined> = { t: period !== undefined ? "second" : undefined };
  for (const a of spatial) units[a] = a in extent ? unit : undefined;
  const types: Record<string, string> = { t: "time", c: "channel", z: "space", y: "space", x: "space" };
  const translation = spatial.every((a) => a in extent)
    ? axes.map((a) => (a === "x" ? extMin[0]! : a === "y" ? extMin[1]! : a === "z" ? extMin[2]! : 0))
    : undefined;
  const utf8 = new TextEncoder();
  const json = (v: unknown) => utf8.encode(JSON.stringify(v, null, 2));
  const datasets = levelInfo.map((l, r) => {
    const n: Record<string, number> = { z: l.sizes[0], y: l.sizes[1], x: l.sizes[2] };
    const scale = axes.map((a) => a === "t" ? period ?? 1 : a === "c" ? 1 : a in extent ? extent[a] / n[a] : 1);
    return {
      path: String(r),
      coordinateTransformations: [
        { type: "scale", scale },
        ...(translation ? [{ type: "translation", translation }] : []),
      ],
    };
  });
  const omeroChannels = labels.map((label, k) => {
    const rng = ranges[k];
    let window = {};
    if (range !== undefined) {
      const [start, end] = rng ?? range;
      window = { window: { min: range[0], max: range[1], start, end } };
    } else if (rng !== undefined) {
      window = { window: { min: rng[0], max: rng[1], start: rng[0], end: rng[1] } };
    }
    return { label, color: colors[k], active: true, ...window };
  });
  // The source metadata (conventions/ims/README.md §5).
  const source = await sourceJson(f, meta, channelGroups);
  const entries: EntryDesc[] = [{
    key: "zarr.json",
    bytes: json({
      zarr_format: 3,
      node_type: "group",
      attributes: declare({
        ome: {
          version: "0.5",
          multiscales: [{
            ...(name ? { name } : {}),
            axes: axes.map((a) => ({ name: a, type: types[a], ...(units[a] ? { unit: units[a] } : {}) })),
            datasets,
          }],
          omero: { channels: omeroChannels },
        },
      }, "ims", url, source),
    }),
  }];
  for (const [r, l] of levelInfo.entries()) {
    const shape: Record<string, number> = { t: times, c: channels, z: l.sizes[0], y: l.sizes[1], x: l.sizes[2] };
    const chunkShape: Record<string, number> = { t: 1, c: 1, z: l.chunk[0], y: l.chunk[1], x: l.chunk[2] };
    entries.push({
      key: `${r}/zarr.json`,
      bytes: json({
        zarr_format: 3,
        node_type: "array",
        shape: axes.map((a) => shape[a]),
        data_type: dtype,
        chunk_grid: { name: "regular", configuration: { chunk_shape: axes.map((a) => chunkShape[a]) } },
        chunk_key_encoding: { name: "default", configuration: { separator: "/" } },
        fill_value: 0,
        codecs: [
          endian ? { name: "bytes", configuration: { endian } } : { name: "bytes" },
          ...(l.compressed ? [{ name: "zlib", configuration: { level: 1 } }] : []),
        ],
        dimension_names: axes,
        attributes: {},
      }),
    });
  }
  let chunks = 0;
  for (const [r, t, c, coords, [offset, length]] of chunkRefs) {
    const { sizes, chunk } = levelInfo[r];
    if (coords.some((i, k) => i * chunk[k] >= sizes[k])) continue; // padding, wholly outside the image
    const index: Record<string, number> = { t, c, z: coords[0], y: coords[1], x: coords[2] };
    const ranges: Range[] = [{ source: 0, offset: BigInt(offset), length: BigInt(length) }];
    entries.push({ key: `${r}/c/${axes.map((a) => index[a]).join("/")}`, ranges });
    chunks++;
  }
  return {
    sources: [{ url }],
    entries,
    summary: {
      levels,
      sizes: { t: times, c: channels, z: levelInfo[0].sizes[0], y: levelInfo[0].sizes[1], x: levelInfo[0].sizes[2] },
      dataType: dtype,
      chunkShape: levelInfo[0].chunk,
      compressed: levelInfo.map((l) => l.compressed),
      chunks,
      channels: labels,
    },
  };
}
