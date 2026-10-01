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
 * @file The `imagecodecs_jpeg2k` array-to-bytes codec: each chunk is one
 * JPEG 2000 codestream or JP2 file, as written by imagecodecs'
 * `jpeg2k_encode` (and as found in TIFF tiles with compression 33003,
 * 33004, 33005 or 34712). A single-component image of height `h` and width
 * `w` fills a chunk whose shape ends in `[h, w]`; a `c`-component image
 * fills a chunk whose shape ends in `[h, w, c]`. All other chunk dimensions
 * must be 1. Encoder settings in the configuration are ignored.
 */

import type {
  CodecArrayInfo,
  CodecArrayLayoutInfo,
} from "#src/datasource/zarr/codec/index.js";
import { CodecKind } from "#src/datasource/zarr/codec/index.js";
import { registerCodec } from "#src/datasource/zarr/codec/resolve.js";
import { DataType } from "#src/util/data_type.js";
import { verifyObject } from "#src/util/json.js";

export type Configuration = Record<string, never>;

export const JPEG2K_DATA_TYPES = [
  DataType.UINT8,
  DataType.INT8,
  DataType.UINT16,
  DataType.INT16,
];

registerCodec({
  name: "imagecodecs_jpeg2k",
  kind: CodecKind.arrayToBytes,
  resolve(
    configuration: unknown,
    decodedArrayInfo: CodecArrayInfo,
  ): { configuration: Configuration } {
    if (configuration !== undefined) verifyObject(configuration);
    if (!JPEG2K_DATA_TYPES.includes(decodedArrayInfo.dataType)) {
      throw new Error(
        `imagecodecs_jpeg2k supports 8- and 16-bit integer data types, not ${DataType[decodedArrayInfo.dataType].toLowerCase()}`,
      );
    }
    return { configuration: {} };
  },
  getDecodedArrayLayoutInfo(
    configuration: Configuration,
    decodedArrayInfo: CodecArrayInfo,
  ): CodecArrayLayoutInfo {
    configuration;
    return {
      physicalToLogicalDimension: Array.from(
        decodedArrayInfo.chunkShape,
        (_, i) => i,
      ),
      readChunkShape: decodedArrayInfo.chunkShape,
    };
  },
});
