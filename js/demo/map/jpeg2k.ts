// The `imagecodecs_jpeg2k` codec for zarrita (and so for OpenLayers' GeoZarr
// source, which shares this copy of zarrita), as vzip's SAFE convention uses
// it (spec/virtualize/safe.md §4): an array-to-bytes codec whose chunk is one
// JPEG 2000 codestream. A codestream of `C` components decodes to rows of
// interleaved samples, `[y, x]` or `[y, x, c]`; a 3-component array declares
// that order with a `transpose` codec (order [1, 2, 0]) before this one, so
// this codec returns the chunk with the array's chunk shape and C-order
// strides, and the transpose codec replaces the strides with the ones of the
// stored order. Codestreams are decoded in a small pool of workers.

import { registry } from "zarrita";
import type { DecodedJpeg2000 } from "./jpeg2k_decoder.ts";

const WASM = new URL("jpeg2000_decoder.wasm", location.href).href;
const WORKER = new URL("jpeg2k_worker.js", location.href).href;

type Pending = { resolve: (d: DecodedJpeg2000) => void; reject: (e: Error) => void };
const workers: Worker[] = [];
const pending = new Map<number, Pending>();
let nextId = 0;

function worker(id: number): Worker {
  const size = Math.max(1, Math.min(4, (navigator.hardwareConcurrency || 2) - 1));
  if (workers.length < size) {
    const w = new Worker(WORKER);
    w.onmessage = ({ data }) => {
      const p = pending.get(data.id);
      pending.delete(data.id);
      if (data.error !== undefined) p?.reject(new Error(data.error));
      else p?.resolve(data.decoded);
    };
    workers.push(w);
  }
  return workers[id % workers.length];
}

function decode(bytes: Uint8Array, bytesPerSample: 1 | 2, signed: boolean): Promise<DecodedJpeg2000> {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    // zarrita may hand over a view of a buffer it keeps (a cache); copy it.
    const copy = bytes.slice();
    worker(id).postMessage({ id, wasm: WASM, bytes: copy, bytesPerSample, signed }, [copy.buffer]);
  });
}

const TYPES = {
  uint8: [Uint8Array, 1, false],
  int8: [Int8Array, 1, true],
  uint16: [Uint16Array, 2, false],
  int16: [Int16Array, 2, true],
} as const;

export class Jpeg2kCodec {
  kind = "array_to_bytes" as const;
  #shape: number[];
  #dataType: keyof typeof TYPES;

  constructor(meta: { dataType: string; shape: number[] }) {
    if (!(meta.dataType in TYPES)) {
      throw new Error(`imagecodecs_jpeg2k: unsupported data type ${meta.dataType}`);
    }
    this.#dataType = meta.dataType as keyof typeof TYPES;
    this.#shape = meta.shape;
  }

  static fromConfig(_configuration: unknown, meta: { dataType: string; shape: number[] }) {
    return new Jpeg2kCodec(meta);
  }

  encode(): never {
    throw new Error("imagecodecs_jpeg2k: encoding is not supported");
  }

  async decode(bytes: Uint8Array) {
    const [Ctr, bytesPerSample, signed] = TYPES[this.#dataType];
    const d = await decode(bytes, bytesPerSample, signed);
    const shape = this.#shape;
    const size = shape.reduce((a, b) => a * b, 1);
    if (d.width * d.height * d.numComponents !== size) {
      throw new Error(
        `imagecodecs_jpeg2k: a ${d.width} × ${d.height} image of ${d.numComponents} components ` +
          `does not fill a chunk of shape [${shape.join(", ")}]`,
      );
    }
    const stride = shape.map((_, i) => shape.slice(i + 1).reduce((a, b) => a * b, 1));
    return { data: new Ctr(d.data.buffer, d.data.byteOffset, size), shape, stride };
  }
}

/** Adds the codec to zarrita's registry. */
export function registerJpeg2k() {
  registry.set("imagecodecs_jpeg2k", async () => Jpeg2kCodec as never);
}
