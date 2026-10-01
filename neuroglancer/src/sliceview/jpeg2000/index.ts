/**
 * @license
 * Copyright 2026 Google Inc.
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * @file JPEG 2000 decoding with `jpeg2000_decoder.wasm` (see `src/lib.rs`
 * and `build.sh`).
 */

export interface DecodedJpeg2000 {
  width: number;
  height: number;
  numComponents: number;
  /** Samples interleaved by component, row-major, little-endian. */
  data: Uint8Array<ArrayBuffer>;
}

const HEADER = 16;

let modulePromise: Promise<WebAssembly.Instance> | undefined;

function getModule() {
  if (modulePromise === undefined) {
    modulePromise = (async () =>
      (
        await WebAssembly.instantiateStreaming(
          fetch(new URL("./jpeg2000_decoder.wasm", import.meta.url)),
          {},
        )
      ).instance)();
  }
  return modulePromise;
}

export async function decompressJpeg2000(
  buffer: Uint8Array,
  bytesPerSample: 1 | 2,
  signed: boolean,
): Promise<DecodedJpeg2000> {
  const m = await getModule();
  const exports = m.exports as {
    memory: WebAssembly.Memory;
    malloc(size: number): number;
    free(ptr: number, size: number): void;
    decode(
      ptr: number,
      len: number,
      bytesPerSample: number,
      signed: number,
    ): number;
  };
  const inputPtr = exports.malloc(buffer.byteLength);
  if (inputPtr === 0) throw new Error("jpeg2000: out of memory");
  let resultPtr = 0;
  let resultSize = 0;
  try {
    new Uint8Array(exports.memory.buffer).set(buffer, inputPtr);
    resultPtr = exports.decode(
      inputPtr,
      buffer.byteLength,
      bytesPerSample,
      signed ? 1 : 0,
    );
    if (resultPtr === 0) throw new Error("jpeg2000: out of memory");
    // `memory.buffer` is re-read after `decode`: memory growth detaches the
    // previous buffer.
    const [width, height, numComponents, bodyLength] = new Uint32Array(
      exports.memory.buffer.slice(resultPtr, resultPtr + HEADER),
    );
    resultSize = HEADER + bodyLength;
    const body = new Uint8Array(
      exports.memory.buffer,
      resultPtr + HEADER,
      bodyLength,
    );
    if (width === 0) {
      throw new Error(`jpeg2000: ${new TextDecoder().decode(body)}`);
    }
    return { width, height, numComponents, data: body.slice() };
  } finally {
    exports.free(inputPtr, buffer.byteLength);
    if (resultPtr !== 0) exports.free(resultPtr, resultSize);
  }
}
