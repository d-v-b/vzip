// JPEG 2000 band files (spec/virtualize/safe/profile.md §12.3) and their chunks (§12.6).
//
// A band file is a JP2 file whose codestream has one tile-part per tile, in
// raster order. Each tile becomes a standalone codestream: the file's main
// header with a SIZ marker of its own, the tile's tile-part, and, at the
// array's right and bottom edges, empty tiles that fill the chunk.

import { SafeError } from "./product.ts";

const MAX_BOXES = 1024;
/** The most bytes of a main header after its SIZ segment (spec/virtualize/safe/profile.md §12.3). */
export const MAX_REST = 1 << 16;
/** The most bytes of a chunk's empty tiles and EOC (§12.6). */
export const MAX_TAIL = 1 << 12;
const BLOCK = 1 << 16;
const SMALL_TILE = 1 << 14; // after a tile-part this small, the next tile-part headers are read in a block
const SOC = 0xff4f, SIZ = 0xff51, SOT = 0xff90, EOC = 0xffd9, COD = 0xff52, COC = 0xff53, QCD = 0xff5c;
// The markers a main header may have after SIZ (§12.3).
const ALLOWED = new Set([COD, COC, QCD, 0xff5d, 0xff5e, 0xff5f, 0xff63, 0xff64]);
const NAMES: Record<number, string> = { 0xff55: "TLM", 0xff57: "PLM", 0xff60: "PPM", 0xff50: "CAP", 0xff59: "CPF" };

const reject = (m: string): never => {
  throw new SafeError(m);
};
const view = (b: Uint8Array) => new DataView(b.buffer, b.byteOffset, b.byteLength);
const u16 = (b: Uint8Array, at = 0) => view(b).getUint16(at);

export type Raw = (offset: number, length: number) => Promise<Uint8Array>;

/** Reads one band file, whose bytes are `raw(offset, length)` of a source from `base` on,
 * `n` bytes long: a block for the boxes and the main header, and small reads of their own
 * for the tile-part headers, with a block when the tile-parts are small. */
export class Reader {
  private start = 0;
  private data: Uint8Array = new Uint8Array(0);
  requests = 0;
  readonly raw: Raw;
  readonly base: number;
  readonly n: number;
  constructor(raw: Raw, base: number, n: number) {
    this.raw = raw;
    this.base = base;
    this.n = n;
  }

  async read(offset: number, length: number, ahead = BLOCK): Promise<Uint8Array> {
    if (offset < 0 || offset + length > this.n) {
      reject(`a band file read of [${offset}, ${offset + length}) is outside its ${this.n} bytes`);
    }
    if (this.start <= offset && offset + length <= this.start + this.data.length) {
      return this.data.subarray(offset - this.start, offset - this.start + length);
    }
    const size = Math.min(this.n - offset, Math.max(length, ahead));
    this.requests++;
    this.data = await this.raw(this.base + offset, size);
    this.start = offset;
    return this.data.subarray(0, length);
  }
}

/** A band file's structure (§12.3). */
export interface Codestream {
  n: number;
  c0: number; // where the codestream starts: the JP2 header is [0, c0)
  siz: Uint8Array; // the SIZ segment, from its marker
  rest: Uint8Array; // the main header after the SIZ segment, up to the first SOT
  width: number;
  height: number;
  tileW: number;
  tileH: number;
  components: number;
  precision: number;
  layers: number;
  /** Per component: [decomposition levels N, [[PPx_r, PPy_r] for r = 0..N]]. */
  coding: [number, [number, number][]][];
  tiles: [number, number][]; // [s_k, Psot] per tile
}

export function grid(cs: Codestream): [number, number] {
  return [Math.ceil(cs.width / cs.tileW), Math.ceil(cs.height / cs.tileH)];
}

/** c0, the start of the codestream: the contents of the first jp2c box, which must end at
 * the end of the file (§12.3). */
async function boxes(r: Reader): Promise<number> {
  const n = r.n;
  let o = 0;
  for (let i = 0; i < MAX_BOXES; i++) {
    if (o + 8 > n) reject("a band file is not a JP2 file: its boxes end without a jp2c box");
    const head = await r.read(o, 8);
    const lbox = view(head).getUint32(0);
    const tbox = String.fromCharCode(...head.subarray(4, 8));
    if (i === 0) {
      const sig = lbox === 12 && tbox === "jP  " ? await r.read(o + 8, 4) : undefined;
      if (sig === undefined || sig[0] !== 0x0d || sig[1] !== 0x0a || sig[2] !== 0x87 || sig[3] !== 0x0a) {
        reject("a band file is not a JP2 file: no JPEG 2000 signature box");
      }
    }
    let length: number, hl: number;
    if (lbox === 1) {
      if (o + 16 > n) reject("a JP2 box's XLBox is beyond the file");
      const xl = view(await r.read(o + 8, 8)).getBigUint64(0);
      if (xl < 16n) reject(`a JP2 box's XLBox is ${xl}, less than 16`);
      length = xl > BigInt(Number.MAX_SAFE_INTEGER) ? Infinity : Number(xl);
      hl = 16;
    } else if (lbox === 0) {
      [length, hl] = [n - o, 8];
    } else {
      [length, hl] = [lbox, 8];
      if (length < 8) reject(`a JP2 box's LBox is ${length}`);
    }
    if (o + length > n) reject("a JP2 box reaches beyond the file");
    if (i === 1 && tbox !== "ftyp") reject("a band file's second box is not ftyp");
    if (tbox === "jp2c") {
      if (o + length !== n) reject("a band file's jp2c box does not end at the end of the file");
      return o + hl;
    }
    o += length;
  }
  return reject(`a band file has no jp2c box in its first ${MAX_BOXES} boxes`);
}

function coding(seg: Uint8Array, at: number, scod: number, what: string): [number, [number, number][]] {
  const levels = seg[at];
  if (levels > 32) reject(`${what} has ${levels} decomposition levels, more than 32`);
  const pp: [number, number][] = [];
  for (let r = 0; r <= levels; r++) {
    const b = scod & 1 ? seg[at + 5 + r] : 0xff;
    pp.push([b & 0x0f, b >> 4]);
  }
  return [levels, pp];
}

/** The SIZ segment and the main header rest of the codestream at c0 (§12.3). */
async function mainHeader(r: Reader, c0: number): Promise<Codestream> {
  const c1 = r.n;
  if (c0 + 4 > c1 || u16(await r.read(c0, 2)) !== SOC) reject("a band file's codestream does not start with SOC");
  if (u16(await r.read(c0 + 2, 2)) !== SIZ) reject("a band file's codestream has no SIZ marker after SOC");
  if (c0 + 6 > c1) reject("a band file's SIZ marker is truncated");
  const lsiz = u16(await r.read(c0 + 4, 2));
  if (c0 + 4 + lsiz > c1 || lsiz < 41) reject("a band file's SIZ segment is truncated");
  const siz = (await r.read(c0 + 2, 2 + lsiz)).slice();
  const v = view(siz);
  const rsiz = v.getUint16(4);
  const [xs, ys, xo, yo, xt, yt, xto, yto] = [6, 10, 14, 18, 22, 26, 30, 34].map((at) => v.getUint32(at));
  const csiz = v.getUint16(38);
  if (lsiz !== 38 + 3 * csiz) reject(`a band file's Lsiz ${lsiz} is not 38 + 3 × Csiz`);
  if (rsiz & 0xc000) reject("a band file uses JPEG 2000 Part 2 or High Throughput (Rsiz)");
  if (xo || yo || xto || yto) reject("a band file's image or tile origin is not 0");
  if (!(xs && ys && xt && yt)) reject("a band file's image or tile size is 0");
  if (csiz !== 1 && csiz !== 3) reject(`a band file has ${csiz} components, not 1 or 3`);
  const comps = Array.from({ length: csiz }, (_, c) => siz.subarray(40 + 3 * c, 43 + 3 * c));
  if (new Set(comps.map((c) => c[0])).size !== 1) reject("a band file's components differ in precision or sign");
  if (comps[0][0] & 0x80) reject("a band file's components are signed");
  const precision = (comps[0][0] & 0x7f) + 1;
  if (precision > 16) reject(`a band file's components have ${precision} bits, more than 16`);
  if (comps.some((c) => c[1] !== 1 || c[2] !== 1)) reject("a band file's components are subsampled");
  let pos = c0 + 4 + lsiz;
  let cod: [number, [number, [number, number][]]] | undefined;
  let qcd = 0;
  const cocs = new Map<number, [number, [number, number][]]>();
  for (;;) {
    if (pos + 2 > c1) reject("a band file's main header has no SOT");
    const marker = u16(await r.read(pos, 2));
    if (marker === SOT) break;
    if (!ALLOWED.has(marker)) {
      reject(`a band file's main header has a ${NAMES[marker] ?? marker.toString(16).toUpperCase()} marker`);
    }
    if (pos + 4 > c1) reject("a band file's main header marker segment is truncated");
    const length = u16(await r.read(pos + 2, 2));
    if (length < 2 || pos + 2 + length > c1) reject("a band file's main header marker segment is truncated");
    const seg = await r.read(pos + 4, length - 2);
    if (marker === COD) {
      if (cod !== undefined) reject("a band file's main header has two COD markers");
      if (length < 12) reject("a band file's COD segment is truncated");
      const scod = seg[0], layers = u16(seg, 2);
      if (scod > 1) reject("a band file's COD uses SOP or EPH markers, or precinct anchors (Scod)");
      if (layers < 1) reject("a band file's COD has no layers");
      if (length !== (scod ? 13 + seg[5] : 12)) reject("a band file's COD segment has the wrong length");
      cod = [layers, coding(seg, 5, scod, "a band file's COD")];
    } else if (marker === COC) {
      if (length < 9) reject("a band file's COC segment is truncated");
      const c = seg[0], scoc = seg[1];
      if (c >= csiz || cocs.has(c)) reject("a band file's COC names no component, or one already named");
      if (scoc > 1) reject("a band file's COC has a bad Scoc");
      if (length !== (scoc ? 10 + seg[2] : 9)) reject("a band file's COC segment has the wrong length");
      cocs.set(c, coding(seg, 2, scoc, "a band file's COC"));
    } else if (marker === QCD) {
      qcd++;
    }
    pos += 2 + length;
  }
  if (cod === undefined || qcd !== 1) reject("a band file's main header does not have exactly one COD and one QCD");
  const restStart = c0 + 4 + lsiz;
  if (pos - restStart > MAX_REST) reject(`a band file's main header is more than ${MAX_REST} bytes`);
  const rest = (await r.read(restStart, pos - restStart)).slice();
  const cs: Codestream = {
    n: r.n, c0, siz, rest, width: xs, height: ys, tileW: xt, tileH: yt, components: csiz, precision,
    layers: cod![0], coding: Array.from({ length: csiz }, (_, c) => cocs.get(c) ?? cod![1]), tiles: [[pos, 0]],
  };
  const [nx, ny] = grid(cs);
  if (nx * ny > 65535) reject(`a band file has ${nx * ny} tiles, more than 65535`);
  return cs;
}

/** The tile-parts (§12.3): one per tile, in raster order, then EOC at the end. */
async function walk(r: Reader, cs: Codestream): Promise<void> {
  const c1 = r.n;
  const [nx, ny] = grid(cs);
  let s = cs.tiles[0][0];
  const tiles: [number, number][] = [];
  let ahead = BLOCK;
  for (let k = 0; k < nx * ny; k++) {
    if (s + 12 > c1) reject(`a band file's tile-part ${k} is beyond the codestream`);
    const v = view(await r.read(s, 12, ahead));
    if (v.getUint16(0) !== SOT || v.getUint16(2) !== 10) reject(`a band file has no SOT marker segment for tile ${k}`);
    const isot = v.getUint16(4), psot = v.getUint32(6), tpsot = v.getUint8(10), tnsot = v.getUint8(11);
    if (isot !== k) reject(`a band file's tile-parts are not in raster order (tile ${isot} where ${k} is)`);
    if (psot < 14) reject(`a band file's tile-part ${k} has Psot ${psot}`);
    if (tpsot !== 0 || tnsot !== 1) reject(`a band file's tile ${k} has more than one tile-part`);
    if (s + psot > c1 - 2) reject(`a band file's tile-part ${k} reaches beyond the codestream`);
    tiles.push([s, psot]);
    ahead = psot <= SMALL_TILE ? BLOCK : 12;
    s += psot;
  }
  if (s + 2 !== c1 || u16(await r.read(s, 2, 2)) !== EOC) {
    reject("a band file's codestream does not end with EOC after its last tile-part");
  }
  cs.tiles = tiles;
}

/** The structure of the band file of `n` bytes at `base` of a source, and the reads it took. */
export async function readBand(raw: Raw, base: number, n: number): Promise<[Codestream, number]> {
  const r = new Reader(raw, base, n);
  const cs = await mainHeader(r, await boxes(r));
  await walk(r, cs);
  return [cs, r.requests];
}

function nprec(z0: number, z1: number, d: number, e: number): number {
  const a = Math.ceil(z0 / 2 ** d), b = Math.ceil(z1 / 2 ** d);
  return b > a ? Math.ceil(b / 2 ** e) - Math.floor(a / 2 ** e) : 0;
}

/** e_k: the packets of a tile of area [x0, x1) × [y0, y1) (§12.6). */
export function emptyPackets(cs: Codestream, x0: number, x1: number, y0: number, y1: number): number {
  let n = 0;
  for (const [levels, pp] of cs.coding) {
    for (let res = 0; res <= levels; res++) {
      n += nprec(x0, x1, levels - res, pp[res][0]) * nprec(y0, y1, levels - res, pp[res][1]);
    }
  }
  return cs.layers * n;
}

/** [x0, y0, w, h, a, b] of the chunk of tile t (§12.6). */
function area(cs: Codestream, t: number): [number, number, number, number, number, number] {
  const [nx] = grid(cs);
  const T = cs.tileW, U = cs.tileH;
  const x0 = (t % nx) * T, y0 = Math.floor(t / nx) * U;
  const w = Math.min(T, cs.width - x0), h = Math.min(U, cs.height - y0);
  if (x0 + T > 0xffffffff || y0 + U > 0xffffffff) reject("a chunk's image area reaches beyond 2^32 − 1");
  return [x0, y0, w, h, Math.ceil(T / w), Math.ceil(U / h)];
}

/** [e_1, …, e_{a×b−1}], the empty packets of the empty tiles of the chunk of tile t, once their
 * tail (§12.6), of 2 + Σ (14 + e_k) bytes, is known to be at most MAX_TAIL: it is measured
 * before anything is made, and rejected past MAX_TAIL. */
export function tailPackets(cs: Codestream, t: number): number[] {
  const [x0, y0, w, h, a, b] = area(cs, t);
  const T = cs.tileW, U = cs.tileH;
  const tooLong = `the empty tiles of a chunk of tile ${t} would take more than ${MAX_TAIL} bytes`;
  if (2 + 14 * (a * b - 1) > MAX_TAIL) reject(tooLong);
  const out: number[] = [];
  let length = 2;
  for (let k = 1; k < a * b; k++) {
    const i = k % a, j = Math.floor(k / a);
    const e = emptyPackets(cs, x0 + i * w, Math.min(x0 + (i + 1) * w, x0 + T), y0 + j * h, Math.min(y0 + (j + 1) * h, y0 + U));
    length += 14 + e;
    if (!(length <= MAX_TAIL)) reject(tooLong);
    out.push(e);
  }
  return out;
}

/** The length of the chunk of tile t's empty tiles and EOC (§12.6). */
export function tailLength(cs: Codestream, t: number): number {
  return tailPackets(cs, t).reduce((m, e) => m + 14 + e, 2);
}

/** The chunk of tile t (§12.6): [SOC + SIZ', SOT, [offset, length] of the body in the band
 * file, the empty tiles and EOC, whether it has empty tiles]. */
export function chunkParts(cs: Codestream, t: number): [Uint8Array, Uint8Array, [number, number], Uint8Array, boolean] {
  const [x0, y0, w, h, a, b] = area(cs, t);
  const T = cs.tileW, U = cs.tileH;
  const packets = tailPackets(cs, t);
  const head = new Uint8Array(2 + cs.siz.length);
  head.set([0xff, 0x4f]);
  head.set(cs.siz, 2);
  const hv = new DataView(head.buffer);
  hv.setUint16(6, 0);
  [x0 + T, y0 + U, x0, y0, w, h, x0, y0].forEach((x, i) => hv.setUint32(8 + 4 * i, x));
  const [s, psot] = cs.tiles[t];
  const sot = sotSegment(0, psot);
  const tail = new Uint8Array(packets.reduce((m, e) => m + 14 + e, 2));
  let at = 0;
  for (const [i, e] of packets.entries()) {
    tail.set(sotSegment(i + 1, 14 + e), at);
    tail.set([0xff, 0x93], at + 12);
    at += 14 + e;
  }
  tail.set([0xff, 0xd9], at);
  return [head, sot, [s + 12, psot - 12], tail, a * b > 1];
}

function sotSegment(isot: number, psot: number): Uint8Array {
  const b = new Uint8Array(12);
  const v = new DataView(b.buffer);
  v.setUint16(0, SOT);
  v.setUint16(2, 10);
  v.setUint16(4, isot);
  v.setUint32(6, psot);
  v.setUint8(10, 0);
  v.setUint8(11, 1);
  return b;
}
