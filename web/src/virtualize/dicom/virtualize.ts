// Virtualizing a DICOM Part 10 file by the DICOM profile (profiles/dicom.md,
// §6): its frames, native or JPEG/JPEG 2000 encapsulated, become Zarr chunks
// that reference the file.

import { type ByteReader, MAX_PAYLOAD, payloadSize } from "../common.ts";
import {
  type Dataset,
  DicomError,
  type Element,
  type Encoding,
  EXPLICIT_LE,
  IMPLICIT_LE,
  ITEM,
  PIXEL_DATA,
  reject,
  tagName,
  UNDEFINED,
  Walker,
} from "./dataset.ts";
import type { Range } from "../../protobuf.ts";
import type { ArchiveDesc, EntryDesc } from "../../writer.ts";

export { DicomError };

type Codec = "jpeg" | "jpeg2k" | null;

// Transfer syntaxes (§6.2): the dataset's encoding, and the codec of
// encapsulated frames (null for native pixel data).
const SYNTAXES = new Map<string, [Encoding, Codec]>([
  ["1.2.840.10008.1.2", [IMPLICIT_LE, null]],
  ["1.2.840.10008.1.2.1", [EXPLICIT_LE, null]],
  ["1.2.840.10008.1.2.2", [{ explicit: true, little: false }, null]],
  ["1.2.840.10008.1.2.4.50", [EXPLICIT_LE, "jpeg"]],
  ["1.2.840.10008.1.2.4.90", [EXPLICIT_LE, "jpeg2k"]],
  ["1.2.840.10008.1.2.4.91", [EXPLICIT_LE, "jpeg2k"]],
]);
const WHOLE_SLIDE = "1.2.840.10008.5.1.4.1.1.77.1.6";
// Photometric interpretations by pixel data and samples per pixel (§6.5).
const MONOCHROME = ["MONOCHROME1", "MONOCHROME2"];
const PHOTOMETRIC: Record<string, string[]> = {
  "null 1": MONOCHROME, "null 3": ["RGB"],
  "jpeg 1": MONOCHROME, "jpeg 3": ["RGB", "YBR_FULL", "YBR_FULL_422"],
  "jpeg2k 1": MONOCHROME, "jpeg2k 3": ["RGB", "YBR_ICT", "YBR_RCT"],
};
// SOI and the Adobe APP14 marker, without its last byte, the colour transform (§6.6).
const ADOBE = [0xff, 0xd8, 0xff, 0xee, 0x00, 0x0e, 0x41, 0x64, 0x6f, 0x62, 0x65, 0x00, 0x64, 0x00, 0x00, 0x00, 0x00];
const DECIMAL = /^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$/;
const INTEGER = /^([+-]?)([0-9]+)$/;

const latin1 = (b: Uint8Array) => {
  let s = "";
  for (const c of b) s += String.fromCharCode(c);
  return s;
};
const dv = (b: Uint8Array) => new DataView(b.buffer, b.byteOffset, b.byteLength);

/** The DICOM row of VIRTUALIZE.md §1.2. */
export function isDicom(head: Uint8Array): boolean {
  return head.length >= 132 && latin1(head.subarray(128, 132)) === "DICM";
}

// ---- attribute values (§6.4)

/** Reads the attributes of a dataset by their kind (§6.4). */
class Values {
  read: ByteReader;
  ds: Dataset;

  constructor(read: ByteReader, ds: Dataset | undefined) {
    this.read = read;
    this.ds = ds ?? new Map();
  }

  element(tag: number, vrs: string[]): Element | undefined {
    const el = this.ds.get(tag);
    if (el === undefined || el.length === 0) return undefined;
    if (el.vr !== null && !vrs.includes(el.vr)) reject(`${tagName(tag)} has VR ${el.vr}, not ${vrs.join(" or ")}`);
    if (el.length === UNDEFINED) reject(`${tagName(tag)} has an undefined length`);
    return el;
  }

  async integer(tag: number, vr: "US" | "UL"): Promise<number | undefined> {
    const el = this.element(tag, [vr]);
    if (el === undefined) return undefined;
    const n = vr === "US" ? 2 : 4;
    if (el.length % n) reject(`${tagName(tag)} has a length that is not a multiple of ${n}`);
    const v = dv(await this.read(el.value, n));
    return n === 2 ? v.getUint16(0, el.little) : v.getUint32(0, el.little);
  }

  async integers64(tag: number): Promise<number[] | undefined> {
    const el = this.element(tag, ["OV"]);
    if (el === undefined) return undefined;
    if (el.length % 8) reject(`${tagName(tag)} has a length that is not a multiple of 8`);
    const v = dv(await this.read(el.value, el.length));
    return Array.from({ length: el.length / 8 }, (_, i) => {
      const x = v.getBigUint64(8 * i, el.little);
      if (x > BigInt(Number.MAX_SAFE_INTEGER)) reject(`${tagName(tag)} has a value above 2^53 - 1`);
      return Number(x);
    });
  }

  async strings(tag: number, vr: string): Promise<string[] | undefined> {
    const el = this.element(tag, [vr]);
    if (el === undefined) return undefined;
    const bytes = await this.read(el.value, el.length);
    const out: string[] = [];
    let start = 0;
    for (let i = 0; i <= bytes.length; i++) {
      if (i < bytes.length && bytes[i] !== 0x5c) continue;
      let a = start;
      let b = i;
      while (a < b && (bytes[a] === 0x20 || bytes[a] === 0)) a++;
      while (b > a && (bytes[b - 1] === 0x20 || bytes[b - 1] === 0)) b--;
      out.push(latin1(bytes.subarray(a, b)));
      start = i + 1;
    }
    return out;
  }

  async string(tag: number, vr: string): Promise<string | undefined> {
    return (await this.strings(tag, vr))?.[0];
  }

  async integerString(tag: number): Promise<number | undefined> {
    const value = await this.string(tag, "IS");
    if (value === undefined) return undefined;
    const m = value.match(INTEGER);
    if (m === null) return reject(`${tagName(tag)} is not an integer string: ${JSON.stringify(value)}`);
    // Without leading zeros; more than 16 digits is above 2^53 - 1 anyway.
    const digits = m[2].replace(/^0+(?=.)/, "");
    const magnitude = digits.length > 16 ? Infinity : Number(digits);
    return m[1] === "-" && magnitude !== 0 ? -magnitude : magnitude;
  }

  /** The string values as numbers, null for an invalid one. */
  async decimals(tag: number): Promise<(number | null)[] | undefined> {
    const values = await this.strings(tag, "DS");
    return values?.map((v) => {
      const x = DECIMAL.test(v) ? Number(v) : null;
      return x !== null && Number.isFinite(x) ? x : null;
    });
  }
}

function need<T>(value: T | undefined, what: string): T {
  return value === undefined ? reject(`missing ${what}`) : value;
}

export interface DicomSummary {
  axes: string[];
  shape: number[];
  dataType: string;
  transferSyntax: string;
  photometric: string;
  frames: number;
  wholeSlide: boolean;
  references: number;
}

type Ranges = ([number, number] | Uint8Array)[];

/** Describes the DICOM file at `url` (read through `read`) by the DICOM profile. */
export async function virtualizeDicom(
  url: string,
  read: ByteReader,
  fileSize: number,
): Promise<ArchiveDesc & { summary: DicomSummary }> {
  // §6.2
  if (fileSize < 132 || latin1(await read(128, 4)) !== "DICM") reject("not a DICOM file");
  const walker = new Walker(read, fileSize);
  const [meta, start] = await walker.meta();
  const syntax = need(await new Values(read, meta).string(0x00020010, "UI"), "Transfer Syntax UID");
  const known = SYNTAXES.get(syntax);
  if (known === undefined) return reject(`unsupported transfer syntax ${syntax}`);
  const [encoding, codec] = known;

  // §6.3
  const [top] = await walker.dataset(start, fileSize, false, encoding, 0, true);
  const pixel = top.get(PIXEL_DATA)!;

  // §6.4: the Pixel Measures item of the Shared Functional Groups, if any.
  let measures: Dataset | undefined;
  const shared = top.get(0x52009229);
  if (shared?.items?.length) {
    const pm = shared.items[0].get(0x00289110);
    if (pm?.items?.length) measures = pm.items[0];
  }
  const v = new Values(read, top);
  const m = new Values(read, measures);
  const sopClass = await v.string(0x00080016, "UI");
  const organization = await v.string(0x00209311, "CS");
  const sppValue = await v.integer(0x00280002, "US");
  const photometricValue = await v.string(0x00280004, "CS");
  const planar = await v.integer(0x00280006, "US");
  const frames = await v.integerString(0x00280008);
  const rowsValue = await v.integer(0x00280010, "US");
  const columnsValue = await v.integer(0x00280011, "US");
  const bitsAllocatedValue = await v.integer(0x00280100, "US");
  const bitsStoredValue = await v.integer(0x00280101, "US");
  const highBit = await v.integer(0x00280102, "US");
  const representationValue = await v.integer(0x00280103, "US");
  const center = await v.decimals(0x00281050);
  const width = await v.decimals(0x00281051);
  const intercept = await v.decimals(0x00281052);
  const slope = await v.decimals(0x00281053);
  const totalColumns = await v.integer(0x00480006, "UL");
  const totalRows = await v.integer(0x00480007, "UL");
  const opticalPaths = await v.integer(0x00480302, "UL");
  const focalPlanes = await v.integer(0x00480303, "UL");
  const eot = await v.integers64(0x7fe00001);
  const eotLengths = await v.integers64(0x7fe00002);
  if (pixel.vr !== null && pixel.vr !== "OB" && pixel.vr !== "OW") reject(`Pixel Data has VR ${pixel.vr}`);
  // (FG) attributes: the Pixel Measures item where present there, else the top level.
  const fg = new Map<number, (number | null)[] | undefined>();
  for (const tag of [0x00280030, 0x00180088]) {
    const inItem = await m.decimals(tag);
    const atTop = await v.decimals(tag);
    fg.set(tag, inItem ?? atTop);
  }

  // §6.5
  const spp = need(sppValue, "Samples per Pixel");
  const photometric = need(photometricValue, "Photometric Interpretation");
  const rows = need(rowsValue, "Rows");
  const columns = need(columnsValue, "Columns");
  const bitsAllocated = need(bitsAllocatedValue, "Bits Allocated");
  const bitsStored = need(bitsStoredValue, "Bits Stored");
  const representation = need(representationValue, "Pixel Representation");
  if (rows < 1 || columns < 1) reject("Rows and Columns must be at least 1");
  if (spp !== 1 && spp !== 3) reject(`unsupported Samples per Pixel ${spp}`);
  if (spp === 3 && planar !== 0 && planar !== 1) {
    reject(`Planar Configuration ${planar ?? "None"} with 3 samples per pixel`);
  }
  const n = frames ?? 1;
  if (!(n >= 1 && n <= Number.MAX_SAFE_INTEGER)) reject(`Number of Frames ${n} is not from 1 to 2^53 - 1`);
  if (bitsStored < 1 || bitsStored > bitsAllocated) {
    reject(`Bits Stored ${bitsStored} is not from 1 to Bits Allocated ${bitsAllocated}`);
  }
  if (highBit !== undefined && highBit !== bitsStored - 1) reject(`High Bit ${highBit} is not Bits Stored - 1`);
  if (representation !== 0 && representation !== 1) reject(`unsupported Pixel Representation ${representation}`);
  const allowedBits = codec === null ? [8, 16, 32] : codec === "jpeg" ? [8] : [8, 16];
  if (!allowedBits.includes(bitsAllocated)) reject(`unsupported Bits Allocated ${bitsAllocated}`);
  if (codec === "jpeg" && (bitsStored !== 8 || representation !== 0)) {
    reject("JPEG Baseline needs 8 unsigned bits stored");
  }
  if (!PHOTOMETRIC[`${codec} ${spp}`].includes(photometric)) {
    reject(`unsupported Photometric Interpretation ${JSON.stringify(photometric)} with ${spp} samples per pixel`);
  }
  const dataType = `${representation === 0 ? "u" : ""}int${bitsAllocated}`;
  const wholeSlide = sopClass === WHOLE_SLIDE;
  let widthPx = columns;
  let heightPx = rows;
  let across = 1;
  if (wholeSlide) {
    if (organization !== "TILED_FULL") {
      reject("a whole-slide image whose Dimension Organization Type is not TILED_FULL");
    }
    widthPx = need(totalColumns, "Total Pixel Matrix Columns");
    heightPx = need(totalRows, "Total Pixel Matrix Rows");
    if (widthPx < 1 || heightPx < 1) reject("the total pixel matrix must be at least 1 by 1");
    if ((opticalPaths ?? 1) !== 1) reject(`${opticalPaths} optical paths`);
    if ((focalPlanes ?? 1) !== 1) reject(`${focalPlanes} focal planes`);
    across = Math.ceil(widthPx / columns);
    const down = Math.ceil(heightPx / rows);
    if (n !== across * down) reject(`${n} frames for ${down} by ${across} tiles`);
  }

  // §6.6: each frame's ranges.
  const planarNative = codec === null && spp === 3 && planar === 1;
  const frameSize = rows * columns * spp * (bitsAllocated / 8);
  const frameRanges: Ranges[] = [];
  if (codec === null) {
    if (pixel.length === UNDEFINED) reject("native Pixel Data with an undefined length");
    if (pixel.length < n * frameSize) {
      reject(`Pixel Data has ${pixel.length} bytes, less than ${n} frames of ${frameSize}`);
    }
    if (!encoding.little && bitsAllocated === 8 && pixel.vr === "OW") reject("8-bit Pixel Data of VR OW in big endian");
    for (let f = 0; f < n; f++) frameRanges.push([[pixel.value + f * frameSize, frameSize]]);
  } else {
    if (pixel.length !== UNDEFINED) reject("encapsulated Pixel Data with a defined length");
    if (pixel.value + 8 > fileSize) reject("no Basic Offset Table");
    const head = dv(await read(pixel.value, 8));
    const botLength = head.getUint32(4, true);
    if (head.getUint16(0, true) * 0x10000 + head.getUint16(2, true) !== ITEM || botLength === UNDEFINED || botLength % 4) {
      reject("the Basic Offset Table is not an item of a multiple of 4 bytes");
    }
    const q = pixel.value + 8 + botLength;
    let fragments: [number, number][][];
    if (eot !== undefined || eotLengths !== undefined) {
      if (eot === undefined || eotLengths === undefined) {
        return reject("an Extended Offset Table without its lengths, or lengths without the table");
      }
      if (eot.length !== n || eotLengths.length !== n) {
        reject("the Extended Offset Table does not have one value per frame");
      }
      if (botLength !== 0) reject("an Extended Offset Table with a Basic Offset Table");
      fragments = eot.map((o, f) => [[q + o + 8, eotLengths[f]]]);
    } else {
      const items = (await walker.fragments(pixel.value, fileSize, true))[0].slice(1);
      if (items.length === 0) reject("encapsulated Pixel Data without fragments");
      const botData = dv(await read(pixel.value + 8, botLength));
      const bot = Array.from({ length: botLength / 4 }, (_, i) => botData.getUint32(4 * i, true));
      const data = items.map(([, o, length]): [number, number] => [o, length]);
      if (bot.length > 0) {
        const position = new Map(items.map(([p], i) => [p - q, i]));
        if (bot.length !== n) reject(`the Basic Offset Table has ${bot.length} offsets for ${n} frames`);
        if (bot[0] !== 0 || bot.some((o, i) => i > 0 && bot[i - 1] >= o)) {
          reject("the Basic Offset Table does not start at 0 and increase");
        }
        if (bot.some((o) => !position.has(o))) reject("a Basic Offset Table offset is not a fragment's");
        const bounds = [...bot.map((o) => position.get(o)!), items.length];
        fragments = Array.from({ length: n }, (_, f) => data.slice(bounds[f], bounds[f + 1]));
      } else if (items.length === n) {
        fragments = data.map((d) => [d]);
      } else if (n === 1) {
        fragments = [data];
      } else {
        return reject(`${items.length} fragments for ${n} frames without an offset table`);
      }
    }
    for (const [f, frags] of fragments.entries()) {
      let ranges: Ranges = frags.filter(([, length]) => length > 0);
      if (ranges.length === 0) reject(`frame ${f} has no data`);
      if (codec === "jpeg" && spp === 3) {
        const [o, length] = ranges[0] as [number, number];
        if (length <= 2) reject(`frame ${f}'s first fragment is too short for a JPEG stream`);
        const prefix = Uint8Array.from([...ADOBE, photometric === "RGB" ? 0 : 1]);
        ranges = [prefix, [o + 2, length - 2], ...ranges.slice(1)];
      }
      frameRanges.push(ranges);
    }
  }

  // §6.7
  const axes: string[] = [];
  if (spp === 3) axes.push("c");
  if (!wholeSlide && n > 1) axes.push("z");
  axes.push("y", "x");
  const shape: Record<string, number> = { c: 3, z: n, y: heightPx, x: widthPx };
  const chunkShape: Record<string, number> = { c: planarNative ? 1 : 3, z: 1, y: rows, x: columns };
  const codecs: unknown[] = [];
  if (spp === 3 && !planarNative) {
    const stored = axes.filter((a) => a !== "c").concat("c");
    codecs.push({ name: "transpose", configuration: { order: stored.map((a) => axes.indexOf(a)) } });
  }
  if (codec === null) {
    codecs.push(bitsAllocated > 8
      ? { name: "bytes", configuration: { endian: encoding.little ? "little" : "big" } }
      : { name: "bytes" });
  } else {
    codecs.push({ name: `imagecodecs_${codec}` });
  }
  const spacing = fg.get(0x00280030);
  const usable = spacing !== undefined && spacing.length === 2 && spacing.every((s) => s !== null && s > 0);
  const between = fg.get(0x00180088);
  const step = between !== undefined && between[0] !== null && between[0] > 0 ? between[0] : undefined;
  const scale: Record<string, number> = {
    c: 1,
    z: step ?? 1,
    y: usable ? spacing[0]! : 1,
    x: usable ? spacing[1]! : 1,
  };
  const unit: Record<string, string | undefined> = {
    z: step ? "millimeter" : undefined,
    y: usable ? "millimeter" : undefined,
    x: usable ? "millimeter" : undefined,
  };
  const type: Record<string, string> = { c: "channel", z: "space", y: "space", x: "space" };

  const low = representation === 0 ? 0 : -(2 ** (bitsStored - 1));
  const high = representation === 0 ? 2 ** bitsStored - 1 : 2 ** (bitsStored - 1) - 1;
  let windowStart = low;
  let windowEnd = high;
  if (spp === 1 && center?.[0] != null && width?.[0] != null) {
    const c = center[0];
    const w = width[0];
    const k = intercept === undefined ? 0 : intercept[0];
    const s = slope === undefined ? 1 : slope[0];
    if (w >= 1 && k !== null && s !== null && s !== 0) {
      windowStart = (c - w / 2 - k) / s;
      windowEnd = (c + w / 2 - k) / s;
      if (s < 0) [windowStart, windowEnd] = [windowEnd, windowStart];
      if (!Number.isFinite(windowStart) || !Number.isFinite(windowEnd)) reject("the VOI window is not finite");
    }
  }
  const window = { min: low, max: high, start: windowStart, end: windowEnd };
  const channels = spp === 1
    ? [{ label: "gray", color: "FFFFFF", active: true, window, ...(photometric === "MONOCHROME1" ? { inverted: true } : {}) }]
    : [["R", "FF0000"], ["G", "00FF00"], ["B", "0000FF"]].map(([label, color]) => ({ label, color, active: true, window }));

  const utf8 = new TextEncoder();
  const json = (value: unknown) => utf8.encode(JSON.stringify(value, null, 2));
  const entries: EntryDesc[] = [];
  for (const [f, ranges] of frameRanges.entries()) {
    const [row, col] = wholeSlide ? [Math.floor(f / across), f % across] : [0, 0];
    for (let s = 0; s < (planarNative ? 3 : 1); s++) {
      let part = ranges;
      if (planarNative) {
        const [o, length] = ranges[0] as [number, number];
        part = [[o + s * (length / 3), length / 3]];
      }
      for (const r of part) {
        if (!(r instanceof Uint8Array) && r[0] + r[1] > fileSize) {
          reject(`range [${r[0]}, ${r[0] + r[1]}) outside the ${fileSize}-byte file`);
        }
      }
      if (payloadSize(part) > MAX_PAYLOAD) reject(`frame ${f}'s reference payload exceeds ${MAX_PAYLOAD} bytes`);
      const coords: Record<string, number> = { c: s, z: f, y: row, x: col };
      entries.push({
        key: `0/c/${axes.map((a) => coords[a]).join("/")}`,
        ranges: part.map((r): Range =>
          r instanceof Uint8Array ? { data: r } : { source: 0, offset: BigInt(r[0]), length: BigInt(r[1]) }),
      });
    }
  }
  const references = entries.length;
  entries.push(
    {
      key: "zarr.json",
      bytes: json({
        zarr_format: 3,
        node_type: "group",
        attributes: {
          ome: {
            version: "0.5",
            multiscales: [{
              axes: axes.map((a) => ({ name: a, type: type[a], ...(unit[a] ? { unit: unit[a] } : {}) })),
              datasets: [{ path: "0", coordinateTransformations: [{ type: "scale", scale: axes.map((a) => scale[a]) }] }],
            }],
            omero: { channels },
          },
        },
      }),
    },
    {
      key: "0/zarr.json",
      bytes: json({
        zarr_format: 3,
        node_type: "array",
        shape: axes.map((a) => shape[a]),
        data_type: dataType,
        chunk_grid: { name: "regular", configuration: { chunk_shape: axes.map((a) => chunkShape[a]) } },
        chunk_key_encoding: { name: "default", configuration: { separator: "/" } },
        fill_value: 0,
        codecs,
        dimension_names: axes,
        attributes: {},
      }),
    },
  );
  return {
    sources: [{ url }],
    entries,
    summary: {
      axes, shape: axes.map((a) => shape[a]), dataType, transferSyntax: syntax, photometric,
      frames: n, wholeSlide, references,
    },
  };
}
