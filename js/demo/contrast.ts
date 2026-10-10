// Contrast ranges from the data, for images whose metadata gives none that
// is usable. A file's display windows are kept when they say something, and
// replaced when they are clearly unusable (see `usableWindow`); then the
// range is the 5th to 99.9th percentile of a chunk of the coarsest level, at
// the position the view opens on, decoded here in the page, leaving out the
// fill value. Neuroglancer can
// compute such a range itself (its invlerp "auto-range" from the histogram of
// the visible data), but only from the layer panel's widget: a viewer state
// cannot ask for it.

import { decodeJpeg2000 } from "./map/jpeg2k_decoder.ts";
import { decodeJpegXr } from "./jpegxr_decoder.ts";

const JPEG2K_WASM = new URL("jpeg2000_decoder.wasm", location.href);
const JPEGXR_WASM = new URL("jpegxr.wasm", location.href);

const TYPES: Record<string, [number, (v: DataView, i: number, le: boolean) => number]> = {
  uint8: [1, (v, i) => v.getUint8(i)], int8: [1, (v, i) => v.getInt8(i)],
  uint16: [2, (v, i, le) => v.getUint16(i, le)], int16: [2, (v, i, le) => v.getInt16(i, le)],
  uint32: [4, (v, i, le) => v.getUint32(i, le)], int32: [4, (v, i, le) => v.getInt32(i, le)],
  float32: [4, (v, i, le) => v.getFloat32(i, le)], float64: [8, (v, i, le) => v.getFloat64(i, le)],
};

export interface Window {
  min?: number;
  max?: number;
  start: number;
  end: number;
}

/**
 * Whether an omero window of integer data spans less than one value: it maps
 * every value to black or white (a CZI display setting can be such, its Low
 * and High being tiny fractions of the type's range). It is never used.
 */
export function tooNarrow(w: Window, dataType: string): boolean {
  return /int/.test(dataType) && w.end - w.start < 1;
}

/**
 * Whether an omero window of integer data spans the data's whole nominal
 * range, `[min, max]`: it then carries no contrast (a CZI or OME-TIFF without
 * display settings gets the window of its bit depth, which leaves a dim
 * 14-bit image black). It is replaced by a sampled range when there is one.
 */
export function fullRange(w: Window, dataType: string): boolean {
  if (!/int/.test(dataType) || w.min === undefined || w.max === undefined || w.max <= w.min) return false;
  return w.start <= w.min && w.end - w.start >= 0.99 * (w.max - w.min);
}

/** Whether an omero window is used as it is (neither too narrow nor the full range). */
export function usableWindow(w: Window | undefined, dataType: string): boolean {
  return w !== undefined && !tooNarrow(w, dataType) && !fullRange(w, dataType);
}

/**
 * The decoded samples of a chunk, as bytes and their byte order, for the
 * codecs this page decodes: `bytes`, `imagecodecs_jpeg2k` and
 * `imagecodecs_jpegxr`, after any `transpose` (the order of the samples does
 * not matter for their percentiles), with `gzip` or `zlib` after them.
 * Undefined for any other chain.
 */
async function decodeChunk(meta: any, encoded: Uint8Array): Promise<[Uint8Array, boolean] | undefined> {
  const codecs: { name: string; configuration?: any }[] = meta.codecs.filter((c: { name: string }) => c.name !== "transpose");
  const [first, ...rest] = codecs;
  if (rest.some((c) => c.name !== "gzip" && c.name !== "zlib")) return undefined;
  let bytes = encoded;
  for (const c of [...rest].reverse()) {
    const inflate = new DecompressionStream(c.name === "gzip" ? "gzip" : "deflate") as TransformStream<Uint8Array, Uint8Array>;
    bytes = new Uint8Array(await new Response(new Blob([bytes as Uint8Array<ArrayBuffer>]).stream().pipeThrough(inflate)).arrayBuffer());
  }
  const [size] = TYPES[meta.data_type] ?? [0];
  switch (first?.name) {
    case "bytes":
      return [bytes, first.configuration?.endian !== "big"];
    case "imagecodecs_jpeg2k":
      if (size !== 1 && size !== 2) return undefined;
      return [(await decodeJpeg2000(JPEG2K_WASM, bytes, size, /^int/.test(meta.data_type))).data, true];
    case "imagecodecs_jpegxr": {
      const d = await decodeJpegXr(JPEGXR_WASM, bytes);
      return d.bytesPerSample === size ? [d.data, true] : undefined;
    }
  }
  return undefined;
}

/**
 * The 5th and 99.9th percentiles of the samples of a decoded chunk, without
 * those equal to the fill value: in a coarse level they are mostly the area
 * outside the acquired region (a slide scan's unscanned tiles), and would
 * set the black point below the data's own background. The 5th rather than
 * the 1st, since a lossy slide scan has a thin tail of near-zero values at
 * its tiles' edges (2–3% of the S-BIAD3625 example's samples), which would
 * leave its background a visible gray in every channel; the 99.9th rather
 * than the 99th, since a fluorescence channel's signal can be sparser than 1%
 * of its samples (IDR idr0011's), which would stretch its noise to white.
 */
function percentiles(bytes: Uint8Array, le: boolean, dataType: string, fill: unknown): [number, number] | undefined {
  const [size, get] = TYPES[dataType];
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const n = Math.floor(bytes.length / size);
  const step = Math.max(1, Math.floor(n / 100000));
  const values: number[] = [];
  for (let i = 0; i < n; i += step) {
    const v = get(view, i * size, le);
    if (v !== fill) values.push(v);
  }
  if (values.length === 0) return undefined;
  values.sort((x, y) => x - y);
  const lo = values[Math.floor(values.length * 0.05)], hi = values[Math.min(values.length - 1, Math.floor(values.length * 0.999))];
  return lo < hi ? [lo, hi] : undefined;
}

/**
 * Contrast ranges sampled from `level` (an array at `url`, of axes `axes`,
 * chunked by `chunks`): one per channel in `channels` (indices along the `c`
 * axis, which must be chunked one channel at a time), or with no channels a
 * single one. Each is undefined where the chunk is missing or not decodable
 * here.
 */
export async function sampleRanges(
  url: string, meta: any, axes: { name: string }[], chunks: number[], channels?: number[],
): Promise<([number, number] | undefined)[]> {
  if (!TYPES[meta.data_type] || meta.chunk_grid.name !== "regular") return channels?.map(() => undefined) ?? [undefined];
  const c = axes.findIndex((a) => a.name === "c");
  if (channels !== undefined && (c < 0 || chunks[c] !== 1)) return channels.map(() => undefined);
  // The chunk the view opens on: the first time point, the middle elsewhere.
  const index = meta.shape.map((n: number, i: number) => axes[i]?.name === "t" ? 0 : Math.floor(Math.floor(n / 2) / chunks[i]));
  const enc = meta.chunk_key_encoding ?? { name: "default" };
  const sep = enc.configuration?.separator ?? (enc.name === "v2" ? "." : "/");
  const one = async (k?: number) => {
    const at = index.slice();
    if (k !== undefined) at[c] = k;
    const key = enc.name === "v2" ? at.join(sep) || "0" : ["c", ...at].join(sep);
    try {
      const r = await fetch(`${url}${key}`);
      if (!r.ok) return undefined;
      const decoded = await decodeChunk(meta, new Uint8Array(await r.arrayBuffer()));
      return decoded && percentiles(decoded[0], decoded[1], meta.data_type, meta.fill_value);
    } catch {
      return undefined;
    }
  };
  return Promise.all(channels === undefined ? [one()] : channels.map(one));
}
