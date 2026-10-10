// JPEG 2000 decoding with jpeg2000_decoder.wasm, the standalone decoder of the
// Neuroglancer fork (https://github.com/d-v-b/neuroglancer, branch vzip,
// src/sliceview/jpeg2000/: hayro-jpeg2000 0.4.0, Apache-2.0 OR MIT, built to
// wasm32 by its build.sh), which build.mjs copies beside the page. The module
// needs no imports and exports `memory`, `malloc`, `decode` and `free`; this
// is the fork's index.ts, adapted.

export interface DecodedJpeg2000 {
  width: number;
  height: number;
  numComponents: number;
  /** Samples interleaved by component, row-major, little-endian. */
  data: Uint8Array<ArrayBuffer>;
}

interface Exports {
  memory: WebAssembly.Memory;
  malloc(size: number): number;
  free(ptr: number, size: number): void;
  decode(ptr: number, len: number, bytesPerSample: number, signed: number): number;
}

const HEADER = 16;
let module: Promise<Exports> | undefined;

function getModule(url: string | URL): Promise<Exports> {
  module ??= WebAssembly.instantiateStreaming(fetch(url), {}).then(
    (r) => r.instance.exports as unknown as Exports,
  );
  return module;
}

/** Decodes one codestream (or JP2 file) to samples of `bytesPerSample` bytes. */
export async function decodeJpeg2000(
  wasmUrl: string | URL,
  buffer: Uint8Array,
  bytesPerSample: 1 | 2,
  signed: boolean,
): Promise<DecodedJpeg2000> {
  const m = await getModule(wasmUrl);
  const input = m.malloc(buffer.byteLength);
  if (input === 0) throw new Error("jpeg2000: out of memory");
  let result = 0;
  let resultSize = 0;
  try {
    new Uint8Array(m.memory.buffer).set(buffer, input);
    result = m.decode(input, buffer.byteLength, bytesPerSample, signed ? 1 : 0);
    if (result === 0) throw new Error("jpeg2000: out of memory");
    // `memory.buffer` is read again after `decode`: growing the memory
    // detaches the previous buffer.
    const [width, height, numComponents, bodyLength] = new Uint32Array(
      m.memory.buffer.slice(result, result + HEADER),
    );
    resultSize = HEADER + bodyLength;
    const body = new Uint8Array(m.memory.buffer, result + HEADER, bodyLength);
    if (width === 0) throw new Error(`jpeg2000: ${new TextDecoder().decode(body)}`);
    return { width, height, numComponents, data: body.slice() };
  } finally {
    m.free(input, buffer.byteLength);
    if (result !== 0) m.free(result, resultSize);
  }
}
