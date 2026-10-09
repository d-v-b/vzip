// Series, layers, levels and tiles (conventions/czi/README.md §3.3–§4.4).

// libCZI's pyramid layer tables (conventions/czi/README.md §3.4): [v, delta, n].
const LAYERS_2: [number, number, number][] = [[2, 0.1, 1], [4, 0.2, 2], [8, 0.4, 3], [16, 0.8, 4], [32, 1, 5],
  [64, 1, 6], [128, 1, 7], [256, 2, 8], [512, 4, 9], [1024, 10, 10]];
const LAYERS_3: [number, number, number][] = [[3, 0.1, 1], [9, 0.2, 2], [27, 0.8, 3], [81, 1.5, 4], [243, 2, 5],
  [729, 5, 6], [2187, 15, 7]];
export const SERIES_LETTERS = "SBHIRV";
const MAX_BAND = 2 ** 24; // the most bytes of an uncompressed chunk, when a tile is larger (row bands)

/** A subblock's layer [kind, n] from its logical and stored sizes, or undefined. */
export function layer(wl: number, hl: number, w: number, h: number): [number, number] | undefined {
  if (wl === w && hl === h) return [1, 0];
  const f = w > h ? wl / w : hl / h;
  for (const [table, base] of [[LAYERS_2, 2], [LAYERS_3, 3]] as const) {
    for (const [v, delta, n] of table) if (v - delta <= f && f <= v + delta) return [base, n];
  }
  return undefined;
}

/** Lexicographic order of number tuples (a shorter prefix first). */
export function compareTuples(a: readonly number[], b: readonly number[]): number {
  for (let k = 0; k < Math.min(a.length, b.length); k++) if (a[k] !== b[k]) return a[k] < b[k] ? -1 : 1;
  return a.length - b.length;
}

/** [PixelType, Compression, hi-lo]. */
export type Form = [number, number, boolean];

/** A placed subblock (conventions/czi/README.md §3.2). */
export interface Placed {
  index: number;
  /** Per series letter, its Start, or -Infinity when absent: sorted as tuples,
   * an absent letter before any Start (conventions/czi/README.md §3.3). */
  series: number[];
  seriesKey: string;
  plane: [number, number, number]; // [t, c, z]
  x: number;
  y: number;
  wl: number;
  hl: number;
  w: number; // stored
  h: number;
  cw: number; // coded
  ch: number;
  form: Form;
  header: number; // the Zstd1 header's length
  layer: [number, number] | undefined;
}

export const conforming = (s: Placed) => s.cw === s.w && s.ch === s.h;

export interface Level {
  layer: [number, number];
  form: Form;
  factor: number;
  tile: [number, number]; // [W, H]
  edge: [number, number]; // [W', H']
  origin: [number, number]; // [x0, y0]
  grid: [number, number]; // [m columns, r rows]
  cells: [Placed, number, number][]; // [subblock, column, row]
}

/** The level `b` is when it is regular (conventions/czi/README.md §3.5), else undefined. */
export function classify(b: Placed[]): Level | undefined {
  const forms = new Set(b.map((s) => s.form.join(",")));
  if (forms.size !== 1) return undefined;
  const max = (f: (s: Placed) => number) => b.reduce((m, s) => Math.max(m, f(s)), -Infinity);
  const min = (f: (s: Placed) => number) => b.reduce((m, s) => Math.min(m, f(s)), Infinity);
  const wl = max((s) => s.wl), hl = max((s) => s.hl), w = max((s) => s.w), h = max((s) => s.h);
  if (wl % w || hl % h || Math.floor(wl / w) !== Math.floor(hl / h)) return undefined;
  const x0 = min((s) => s.x), y0 = min((s) => s.y);
  const cells: [Placed, number, number][] = [];
  const seen = new Set<string>();
  for (const s of b) {
    if ((s.x - x0) % wl || (s.y - y0) % hl) return undefined;
    const i = (s.x - x0) / wl, j = (s.y - y0) / hl;
    const key = `${s.plane.join(",")};${i};${j}`;
    if (seen.has(key)) return undefined;
    seen.add(key);
    cells.push([s, i, j]);
  }
  const m = cells.reduce((v, [, i]) => Math.max(v, i), 0) + 1;
  const r = cells.reduce((v, [, , j]) => Math.max(v, j), 0) + 1;
  const lastCol = new Set<string>(), lastRow = new Set<string>();
  let edgeW = 0, edgeH = 0;
  for (const [s, i, j] of cells) {
    if (i < m - 1 && (s.wl !== wl || s.w !== w)) return undefined;
    if (j < r - 1 && (s.hl !== hl || s.h !== h)) return undefined;
    if (i === m - 1) {
      lastCol.add(`${s.wl},${s.w}`);
      edgeW = s.w;
    }
    if (j === r - 1) {
      lastRow.add(`${s.hl},${s.h}`);
      edgeH = s.h;
    }
  }
  if (lastCol.size !== 1 || lastRow.size !== 1) return undefined;
  return {
    layer: b[0].layer!, form: b[0].form, factor: wl / w, tile: [w, h], edge: [edgeW, edgeH], origin: [x0, y0],
    grid: [m, r], cells,
  };
}

function gcd(a: number, b: number): number {
  while (b) [a, b] = [b, a % b];
  return a;
}

/** The rows of a band of an uncompressed tile of w x h (edge height h2) of q-byte pixels:
 * h itself when the tile is at most 2^24 bytes, else the largest divisor of gcd(h, h2)
 * whose band is at most 2^24 bytes, or 1. */
export function rowBand(h: number, h2: number, w: number, q: number): number {
  if (w * h * q <= MAX_BAND) return h;
  const g = gcd(h, h2);
  let best = 1;
  for (let d = 1; d * d <= g; d++) {
    if (g % d === 0) {
      for (const v of [d, g / d]) if (v * w * q <= MAX_BAND && v > best) best = v;
    }
  }
  return best;
}

/** The axes of an image or tile array over its subblocks' planes, the least
 * t, c, z, and the extent along each (conventions/czi/README.md §4.2). */
export function planeAxes(
  planes: [number, number, number][], p: number,
): [string[], Record<string, number>, Record<string, number>] {
  const lo: Record<string, number> = {}, hi: Record<string, number> = {}, extent: Record<string, number> = {};
  for (const [k, a] of ["t", "c", "z"].entries()) {
    lo[a] = planes.reduce((m, pl) => Math.min(m, pl[k]), Infinity);
    hi[a] = planes.reduce((m, pl) => Math.max(m, pl[k]), -Infinity);
    extent[a] = hi[a] - lo[a] + 1;
  }
  const axes = [...(hi.t > lo.t ? ["t"] : []), ...(hi.c > lo.c || p > 1 ? ["c"] : []), ...(hi.z > lo.z ? ["z"] : []),
    "y", "x"];
  return [axes, lo, extent];
}
