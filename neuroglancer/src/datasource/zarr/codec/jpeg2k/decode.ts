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

import { decodeJpeg2000 } from "#src/async_computation/decode_jpeg2000_request.js";
import { requestAsyncComputation } from "#src/async_computation/request.js";
import { registerCodec } from "#src/datasource/zarr/codec/decode.js";
import type { CodecArrayInfo } from "#src/datasource/zarr/codec/index.js";
import { CodecKind } from "#src/datasource/zarr/codec/index.js";
import type { Configuration } from "#src/datasource/zarr/codec/jpeg2k/resolve.js";
import {
  DATA_TYPE_BYTES,
  DATA_TYPE_SIGNED,
  makeDataTypeArrayView,
} from "#src/util/data_type.js";
import { convertEndian, Endianness } from "#src/util/endian.js";

/**
 * Checks that a decoded image of the given size fills a chunk of shape
 * `chunkShape` (see `resolve.ts`), and throws if it does not.
 */
export function checkJpeg2kChunkShape(
  chunkShape: readonly number[],
  width: number,
  height: number,
  numComponents: number,
) {
  const imageShape =
    numComponents === 1 ? [height, width] : [height, width, numComponents];
  const lead = chunkShape.length - imageShape.length;
  if (
    lead < 0 ||
    chunkShape.slice(0, lead).some((n) => n !== 1) ||
    imageShape.some((n, i) => chunkShape[lead + i] !== n)
  ) {
    throw new Error(
      `JPEG 2000 image of shape [${imageShape.join(", ")}] (height, width` +
        `${numComponents === 1 ? "" : ", components"}) does not fill a chunk ` +
        `of shape [${chunkShape.join(", ")}]`,
    );
  }
}

registerCodec({
  name: "imagecodecs_jpeg2k",
  kind: CodecKind.arrayToBytes,
  async decode(
    configuration: Configuration,
    decodedArrayInfo: CodecArrayInfo,
    encoded,
    signal: AbortSignal,
  ) {
    configuration;
    const { dataType, chunkShape } = decodedArrayInfo;
    const bytesPerSample = DATA_TYPE_BYTES[dataType] as 1 | 2;
    const { width, height, numComponents, data } =
      await requestAsyncComputation(
        decodeJpeg2000,
        signal,
        [encoded.buffer],
        encoded,
        bytesPerSample,
        DATA_TYPE_SIGNED[dataType] ?? false,
      );
    checkJpeg2kChunkShape(chunkShape, width, height, numComponents);
    const array = makeDataTypeArrayView(
      dataType,
      data.buffer,
      data.byteOffset,
      data.byteLength,
    );
    convertEndian(array, Endianness.LITTLE, bytesPerSample);
    return array;
  },
});
