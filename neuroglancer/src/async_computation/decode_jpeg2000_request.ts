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

import { asyncComputation } from "#src/async_computation/index.js";
import type { DecodedJpeg2000 } from "#src/sliceview/jpeg2000/index.js";

export const decodeJpeg2000 =
  asyncComputation<
    (
      data: Uint8Array,
      bytesPerSample: 1 | 2,
      signed: boolean,
    ) => DecodedJpeg2000
  >("decodeJpeg2000");
