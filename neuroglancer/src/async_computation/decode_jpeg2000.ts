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
import { registerAsyncComputation } from "#src/async_computation/handler.js";
import { decompressJpeg2000 } from "#src/sliceview/jpeg2000/index.js";

registerAsyncComputation(
  decodeJpeg2000,
  async (data: Uint8Array, bytesPerSample: 1 | 2, signed: boolean) => {
    const result = await decompressJpeg2000(data, bytesPerSample, signed);
    return { value: result, transfer: [result.data.buffer] };
  },
);
