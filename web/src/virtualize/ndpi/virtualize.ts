// The NDPI profile (profiles/ndpi.md, §4), a variant of the TIFF profile.
//
// Hamamatsu NDPI is a little-endian classic TIFF with 64-bit offsets (an
// 8-byte first-IFD offset, 8-byte next-IFD offsets, and a high word per entry
// after each IFD), whose pyramid levels are single JPEG strips with restart
// markers. Each chunk is a JPEG stream rebuilt from the strip's header (held in
// data sources, since every chunk shares it), a literal frame header for the
// chunk's size, and a × b restart intervals.

import { type ByteReader, DataSources, MAX_PAYLOAD, type Part, payloadSize, toRange } from "../common.ts";
import { TiffError } from "../tiff/ifd.ts";
import type { ArchiveDesc, EntryDesc } from "../../writer.ts";

const MAX_IFDS = 100000;
const CHUNK = 1024; // target chunk size in pixels
const SIZES: Record<number, number> = {
  1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8,
};
const INTEGER = new Set([1, 3, 4, 13, 16, 18]);
const OFFSET = new Set([3, 4, 8, 9]); // X/YOffsetFromSlideCenter may be signed
// The tags of §4: allowed field types, and whether each is a scalar.
const TAGS: Record<number, [Set<number>, boolean]> = {
  256: [INTEGER, true], 257: [INTEGER, true], 258: [INTEGER, false], 259: [INTEGER, true],
  262: [INTEGER, true], 277: [INTEGER, true], 273: [INTEGER, true], 279: [INTEGER, true],
  282: [new Set([5]), true], 283: [new Set([5]), true], 296: [INTEGER, true],
  65420: [INTEGER, true], 65421: [new Set([11, 12]), true], 65422: [OFFSET, true], 65423: [OFFSET, true],
  65426: [INTEGER, false], 65432: [INTEGER, false],
};

const reject = (message: string): never => {
  throw new TiffError(message);
};
const view = (b: Uint8Array) => new DataView(b.buffer, b.byteOffset, b.byteLength);
const u64 = (v: DataView, at: number) => {
  const x = v.getBigUint64(at, true);
  return x > BigInt(Number.MAX_SAFE_INTEGER) ? reject("an NDPI offset is above 2^53 - 1") : Number(x);
};

/** The first IFD's offset if the file is NDPI (§4), else undefined. */
export async function detectNdpi(read: ByteReader, size: number): Promise<number | undefined> {
  if (size < 12) return undefined;
  const head = await read(0, 12);
  if (String.fromCharCode(...head.subarray(0, 4)) !== "II*\0") return undefined;
  const v = Number(view(head).getBigUint64(4, true));
  if (v < 16 || v + 2 > size) return undefined;
  const n = view(await read(v, 2)).getUint16(0, true);
  if (v + 2 + 12 * n > size) return undefined;
  const entries = view(await read(v + 2, 12 * n));
  for (let i = 0; i < n; i++) if (entries.getUint16(12 * i, true) === 65420) return v;
  return undefined;
}

type Values = number[] | [number, number][];

/** The main chain's IFDs, as tag → values (§4). */
async function readIfds(read: ByteReader, first: number): Promise<Map<number, Values>[]> {
  const seen = new Set<number>();
  const ifds: Map<number, Values>[] = [];
  for (let offset = first; offset !== 0;) {
    if (offset < 16) reject(`IFD offset ${offset} is not in the file`);
    if (seen.has(offset)) reject(`IFD offset ${offset} read twice`);
    if (seen.size >= MAX_IFDS) reject("too many IFDs");
    seen.add(offset);
    const n = view(await read(offset, 2)).getUint16(0, true);
    const body = await read(offset + 2, 12 * n + 8 + 4 * n);
    const b = view(body);
    const tags = new Map<number, Values>();
    for (let i = 0; i < n; i++) {
      const tag = b.getUint16(12 * i, true);
      if (!(tag in TAGS) || tags.has(tag)) continue; // unused, or a duplicate (the first is used)
      const type = b.getUint16(12 * i + 2, true);
      const count = b.getUint32(12 * i + 4, true);
      const [allowed, scalar] = TAGS[tag];
      if (!allowed.has(type)) reject(`tag ${tag} has field type ${type}`);
      if (scalar && count === 0) reject(`tag ${tag} has no value`);
      const low = b.getUint32(12 * i + 8, true);
      const high = b.getUint32(12 * n + 8 + 4 * i, true);
      const nbytes = count * SIZES[type];
      let values: Values;
      if (nbytes <= 4) {
        values = count === 1 && (type === 4 || type === 13)
          ? [low + high * 2 ** 32]
          : decode(body.subarray(12 * i + 8, 12 * i + 8 + nbytes), type, count);
      } else {
        values = decode(await read(low + high * 2 ** 32, nbytes), type, count);
      }
      if (INTEGER.has(type) && (values as number[]).some((v) => v > Number.MAX_SAFE_INTEGER)) {
        reject(`tag ${tag} has a value above 2^53 - 1`);
      }
      tags.set(tag, values);
    }
    ifds.push(tags);
    offset = u64(b, 12 * n);
  }
  if (ifds.length === 0) reject("no images");
  return ifds;
}

function decode(bytes: Uint8Array, type: number, count: number): Values {
  const v = view(bytes);
  if (type === 5) return Array.from({ length: count }, (_, i): [number, number] => [v.getUint32(8 * i, true), v.getUint32(8 * i + 4, true)]);
  return Array.from({ length: count }, (_, i) => {
    switch (type) {
      case 1: return v.getUint8(i);
      case 3: return v.getUint16(2 * i, true);
      case 8: return v.getInt16(2 * i, true);
      case 9: return v.getInt32(4 * i, true);
      case 4: case 13: return v.getUint32(4 * i, true);
      case 11: return v.getFloat32(4 * i, true);
      case 12: return v.getFloat64(8 * i, true);
      default: return Number(v.getBigUint64(8 * i, true)); // 16, 18 (values above 2^53 are rejected)
    }
  });
}

function one(tags: Map<number, Values>, tag: number, what: string, fallback?: number): number {
  const v = tags.get(tag) as number[] | undefined;
  if (v === undefined) return fallback ?? reject(`an NDPI image has no ${what}`);
  return v[0];
}

/** [SOF0 start, SOF0 end, MCU width, MCU height, restart interval] of a strip's header (§4). */
export function jpegHeader(header: Uint8Array): [number, number, number, number, number] {
  if (header[0] !== 0xff || header[1] !== 0xd8) reject("an NDPI strip does not start with a JPEG SOI marker");
  const v = view(header);
  let pos = 2;
  let sof: [number, number] | undefined;
  let dri: number | undefined;
  for (;;) {
    if (pos + 4 > header.length || header[pos] !== 0xff) reject("malformed JPEG header in an NDPI strip");
    const marker = header[pos + 1];
    const length = v.getUint16(pos + 2);
    const end = pos + 2 + length;
    if (length < 2 || end > header.length) reject("malformed JPEG header in an NDPI strip");
    if (marker === 0xc0) {
      if (sof !== undefined) reject("an NDPI strip has two SOF0 segments");
      sof = [pos, end];
    } else if (marker >= 0xc1 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc) {
      reject(`an NDPI strip is not baseline JPEG (marker FF${marker.toString(16).toUpperCase()})`);
    } else if (marker === 0xdd) {
      if (length !== 4) reject("malformed DRI segment in an NDPI strip");
      dri = v.getUint16(pos + 4);
    } else if (marker === 0xda) {
      if (end !== header.length) reject("an NDPI strip's SOS does not end at McuStarts[0]");
      break;
    }
    pos = end;
  }
  if (sof === undefined || !dri) return reject("an NDPI strip has no SOF0 or no restart interval");
  const seg = header.subarray(sof[0], sof[1]);
  const nf = seg.length > 9 ? seg[9] : 0;
  if (nf === 0 || seg.length !== 10 + 3 * nf) reject("malformed SOF0 segment in an NDPI strip");
  // Sampling factors: horizontal in the high 4 bits, vertical in the low 4.
  let horizontal = 0;
  let vertical = 0;
  for (let k = 0; k < nf; k++) {
    horizontal = Math.max(horizontal, seg[11 + 3 * k] >> 4);
    vertical = Math.max(vertical, seg[11 + 3 * k] & 15);
  }
  if (horizontal === 0 || vertical === 0) reject("an NDPI strip has a sampling factor of 0");
  return [sof[0], sof[1], 8 * horizontal, 8 * vertical, dri];
}

export async function virtualizeNdpi(
  url: string,
  read: ByteReader,
  size: number,
  first: number,
): Promise<ArchiveDesc & { summary: object }> {
  const ifds = await readIfds(read, first);
  const levels: { mag: number; w: number; h: number; tags: Map<number, Values> }[] = [];
  for (const tags of ifds) {
    const mag = one(tags, 65421, "Magnification");
    if (!(mag > 0)) continue;
    const w = one(tags, 256, "ImageWidth");
    const h = one(tags, 257, "ImageLength");
    const bits = tags.get(258) as number[] | undefined;
    if (one(tags, 259, "Compression", 1) !== 7 || one(tags, 262, "PhotometricInterpretation") !== 6 ||
        one(tags, 277, "SamplesPerPixel", 1) !== 3 || !bits?.length || bits.some((b) => b !== 8)) {
      reject("an NDPI level is not 8-bit YCbCr JPEG with 3 samples");
    }
    if (tags.get(273)?.length !== 1 || tags.get(279)?.length !== 1) reject("an NDPI level does not have exactly one strip");
    const last = levels[levels.length - 1];
    if (last && !(w < last.w && h < last.h)) reject("NDPI levels do not decrease in size");
    if (levels.some((l) => l.mag === mag)) reject("NDPI focal planes (two levels with one magnification) are not supported");
    if (Math.min(w, h) < 1) reject("an NDPI level is empty");
    levels.push({ mag, w, h, tags });
  }
  if (levels.length === 0) reject("no NDPI levels");

  // Scale (§4).
  const base = levels[0];
  const perUnit = ({ 3: 10000, 2: 25400 } as Record<number, number>)[one(base.tags, 296, "ResolutionUnit", 2)];
  const physical = (tag: number) => {
    const r = (base.tags.get(tag) as [number, number][] | undefined)?.[0];
    return perUnit === undefined || r === undefined || r[0] === 0 || r[1] === 0 ? undefined : perUnit / (r[0] / r[1]);
  };
  const px = physical(282);
  const py = physical(283);
  const axes = ["c", "y", "x"];
  const codecs = [{ name: "transpose", configuration: { order: [1, 2, 0] } }, { name: "imagecodecs_jpeg" }];
  const utf8 = new TextEncoder();
  const json = (v: unknown) => utf8.encode(JSON.stringify(v, null, 2));
  const entries: EntryDesc[] = [];
  const data = new DataSources();
  const datasets = [];
  for (const [li, level] of levels.entries()) {
    const s0 = one(level.tags, 273, "StripOffsets");
    const n = one(level.tags, 279, "StripByteCounts");
    if (s0 + n > size || n < 4) reject("an NDPI strip is outside the file");
    let chunk: number[];
    const refs: [string, Part[]][] = [];
    const starts = level.tags.get(65426) as number[] | undefined;
    if (starts === undefined) {
      chunk = [3, level.h, level.w];
      refs.push([`${li}/c/0/0/0`, [[s0, n]]]);
    } else {
      chunk = await intervals(refs, data, li, read, level.tags, starts, s0, n, level.w, level.h);
    }
    for (const [key, parts] of refs) {
      entries.push({ key, ranges: parts.map(toRange) });
    }
    entries.push({
      key: `${li}/zarr.json`,
      bytes: json({
        zarr_format: 3, node_type: "array", shape: [3, level.h, level.w], data_type: "uint8",
        chunk_grid: { name: "regular", configuration: { chunk_shape: chunk } },
        chunk_key_encoding: { name: "default", configuration: { separator: "/" } },
        fill_value: 0, codecs, dimension_names: axes, attributes: {},
      }),
    });
    datasets.push({
      path: String(li),
      coordinateTransformations: [{ type: "scale", scale: [1, (py ?? 1) * (base.h / level.h), (px ?? 1) * (base.w / level.w)] }],
    });
  }
  const unit = (p: number | undefined) => (p === undefined ? {} : { unit: "micrometer" });
  // Position (§4): the image's centre, from the slide's centre in nm.
  const offsetX = (base.tags.get(65422) as number[] | undefined)?.[0];
  const offsetY = (base.tags.get(65423) as number[] | undefined)?.[0];
  if (px !== undefined && py !== undefined && offsetX !== undefined && offsetY !== undefined) {
    const translation = [0, offsetY / 1000 - base.h * py / 2, offsetX / 1000 - base.w * px / 2];
    for (const d of datasets) (d.coordinateTransformations as unknown[]).push({ type: "translation", translation });
  }
  entries.push({
    key: "zarr.json",
    bytes: json({
      zarr_format: 3, node_type: "group",
      attributes: {
        ome: {
          version: "0.5",
          multiscales: [{
            axes: [{ name: "c", type: "channel" }, { name: "y", type: "space", ...unit(py) }, { name: "x", type: "space", ...unit(px) }],
            datasets,
          }],
        },
      },
    }),
  });
  return {
    sources: data.table(url),
    entries,
    summary: { axes, levels: levels.map((l) => [3, l.h, l.w]), references: entries.length - levels.length - 1, codec: "imagecodecs_jpeg" },
  };
}

/** Adds a McuStarts level's chunk references (§4); returns its chunk shape. */
async function intervals(
  refs: [string, Part[]][], data: DataSources, li: number, read: ByteReader, tags: Map<number, Values>,
  startsLow: number[], s0: number, n: number, w: number, h: number,
): Promise<number[]> {
  const high = tags.get(65432) as number[] | undefined;
  if (high !== undefined && high.length !== startsLow.length) reject("McuStartsHighBytes and McuStarts differ in length");
  const starts = high === undefined ? startsLow : startsLow.map((s, i) => s + high[i] * 2 ** 32);
  if (starts.length === 0 || starts[0] < 2 || starts[0] > n) reject("McuStarts[0] is outside the strip");
  const header = await read(s0, starts[0]);
  const [sofStart, sofEnd, mw, mh, interval] = jpegHeader(header);
  const q = Math.ceil(w / (interval * mw));
  const r = Math.ceil(h / mh);
  if (starts.length !== q * r || starts[starts.length - 1] >= n || starts.some((s, i) => i > 0 && s <= starts[i - 1])) {
    reject("McuStarts does not match the strip's intervals");
  }
  const ends = starts.map((_, i) => (i + 1 < starts.length ? starts[i + 1] : n) - 2);
  if (starts.some((s, i) => ends[i] <= s)) reject("an NDPI restart interval is empty");
  const a = Math.min(q, Math.max(1, Math.floor(CHUNK / (interval * mw))));
  const sof = header.slice(sofStart, sofEnd);
  // The header around SOF0 is the same in every chunk: data sources (§4).
  const before = data.range(header.subarray(0, sofStart));
  const after = data.range(header.subarray(sofEnd));

  const chunks = (b: number): [number, number, Part[]][] => {
    const s = sof.slice();
    view(s).setUint16(5, b * mh);
    view(s).setUint16(7, a * interval * mw);
    const head: Part[] = [before, s, after];
    const out: [number, number, Part[]][] = [];
    for (let u = 0; u < Math.ceil(r / b); u++) {
      for (let v = 0; v < Math.ceil(q / a); v++) {
        const parts: Part[] = [...head];
        let t = 0;
        for (let y = 0; y < b; y++) {
          for (let x = 0; x < a; x++) {
            const i = Math.min(u * b + y, r - 1) * q + Math.min(v * a + x, q - 1);
            if (t) parts.push(Uint8Array.of(0xff, 0xd0 + ((t - 1) % 8)));
            parts.push([s0 + starts[i], ends[i] - starts[i]]);
            t++;
          }
        }
        parts.push(Uint8Array.of(0xff, 0xd9));
        out.push([u, v, parts]);
      }
    }
    return out;
  };
  let b = Math.max(1, Math.min(r, Math.floor(CHUNK / mh)));
  let all = chunks(b);
  while (b > 1 && all.some(([, , parts]) => payloadSize(parts) > MAX_PAYLOAD)) all = chunks(--b);
  for (const [u, v, parts] of all) {
    if (payloadSize(parts) > MAX_PAYLOAD) reject(`an NDPI chunk's reference payload exceeds ${MAX_PAYLOAD} bytes`);
    refs.push([`${li}/c/0/${u}/${v}`, parts]);
  }
  return [3, b * mh, a * interval * mw];
}
