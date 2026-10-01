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

import type {
  AutoDetectFileOptions,
  AutoDetectMatch,
  AutoDetectRegistry,
} from "#src/kvstore/auto_detect.js";

// The vzip comment is 22 or 38 bytes, so the end of central directory
// record starts 44 or 60 bytes before the end of the file (spec §3.4).
async function detectVzip(
  options: AutoDetectFileOptions,
): Promise<AutoDetectMatch[]> {
  const { suffix } = options;
  if (suffix === undefined) return [];
  const view = new DataView(
    suffix.buffer,
    suffix.byteOffset,
    suffix.byteLength,
  );
  for (const commentLength of [38, 22]) {
    const i = suffix.length - 22 - commentLength;
    if (i < 0 || view.getUint32(i, true) !== 0x06054b50) continue;
    if (view.getUint16(i + 20, true) !== commentLength) continue;
    const magic = new TextDecoder().decode(suffix.subarray(i + 22, i + 28));
    if (magic.startsWith("vzip/")) {
      return [{ suffix: "vzip:", description: `vzip archive (${magic})` }];
    }
  }
  return [];
}

export function registerAutoDetect(registry: AutoDetectRegistry) {
  registry.registerFileFormat({
    prefixLength: 0,
    suffixLength: 60,
    match: detectVzip,
  });
}
