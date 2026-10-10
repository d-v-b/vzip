// JPEG XR decoding with jpegxr.wasm, jxrlib (BSD-2-Clause, Microsoft) built
// with emscripten by the Neuroglancer fork (https://github.com/d-v-b/neuroglancer,
// branch vzip, src/sliceview/jpegxr/), which build.mjs copies beside the page.
// This is the fork's index.ts, adapted: the module's imports are stubs, since
// jxrlib's file streams are never used.

export interface DecodedJpegXr {
  width: number;
  height: number;
  numComponents: number;
  bytesPerSample: number;
  isFloat: boolean;
  /** Samples interleaved by pixel, row-major, little-endian. */
  data: Uint8Array<ArrayBuffer>;
}

interface Exports {
  memory: WebAssembly.Memory;
  malloc(size: number): number;
  free(ptr: number): void;
  jpegxr_decode(ptr: number, len: number): number;
  _initialize(): void;
}

const HEADER = 24;
const ENOSYS = 52; // WASI's "not implemented"
const env = {
  emscripten_notify_memory_growth: () => {},
  clock_time_get: () => ENOSYS,
  fd_write: () => ENOSYS,
  fd_read: () => ENOSYS,
  fd_close: () => ENOSYS,
  fd_seek: () => ENOSYS,
  __syscall_unlinkat: () => -ENOSYS,
  __syscall_rmdir: () => -ENOSYS,
  __syscall_readlinkat: () => -ENOSYS,
};

let module: Promise<Exports> | undefined;

function getModule(url: string | URL): Promise<Exports> {
  module ??= WebAssembly.instantiateStreaming(fetch(url), { env, wasi_snapshot_preview1: env }).then((r) => {
    const e = r.instance.exports as unknown as Exports;
    e._initialize();
    return e;
  });
  return module;
}

/** Decodes one JPEG XR file. */
export async function decodeJpegXr(wasmUrl: string | URL, buffer: Uint8Array): Promise<DecodedJpegXr> {
  const m = await getModule(wasmUrl);
  const input = m.malloc(buffer.byteLength);
  if (input === 0) throw new Error("jpegxr: out of memory");
  let result = 0;
  try {
    new Uint8Array(m.memory.buffer).set(buffer, input);
    result = m.jpegxr_decode(input, buffer.byteLength);
    if (result === 0) throw new Error("jpegxr: out of memory");
    // `memory.buffer` is read again after decoding: growth detaches it.
    const [width, height, numComponents, bytesPerSample, kind, length] = new Uint32Array(
      m.memory.buffer.slice(result, result + HEADER),
    );
    const body = new Uint8Array(m.memory.buffer, result + HEADER, length);
    if (width === 0) throw new Error(`jpegxr: ${new TextDecoder().decode(body)}`);
    return { width, height, numComponents, bytesPerSample, isFloat: kind === 1, data: body.slice() };
  } finally {
    m.free(input);
    if (result !== 0) m.free(result);
  }
}
