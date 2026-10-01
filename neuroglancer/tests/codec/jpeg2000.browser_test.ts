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

import { expect, test } from "vitest";
import { checkJpeg2kChunkShape } from "#src/datasource/zarr/codec/jpeg2k/decode.js";
import { decompressJpeg2000 } from "#src/sliceview/jpeg2000/index.js";

declare const TEST_DATA_SERVER: string;

async function fixture(name: string) {
  const get = async (ext: string) => {
    const response = await fetch(
      `${TEST_DATA_SERVER}codec/jpeg2k/${name}.${ext}`,
    );
    expect(response.ok).toBe(true);
    return new Uint8Array(await response.arrayBuffer());
  };
  const info = JSON.parse(new TextDecoder().decode(await get("json"))) as {
    shape: number[];
    dtype: string;
  };
  return { encoded: await get("j2k"), expected: await get("raw"), info };
}

test("decodes like OpenJPEG", async () => {
  for (const name of [
    "uint8_gray",
    "uint8_rgb",
    "uint16_gray",
    "int16_gray",
    "idr0096_level8_c0",
  ]) {
    const { encoded, expected, info } = await fixture(name);
    const bytesPerSample = info.dtype.endsWith("8") ? 1 : 2;
    const decoded = await decompressJpeg2000(
      encoded,
      bytesPerSample,
      info.dtype.startsWith("int"),
    );
    const [height, width, numComponents = 1] = info.shape;
    expect({ name, ...decoded, data: undefined }).toEqual({
      name,
      width,
      height,
      numComponents,
      data: undefined,
    });
    expect(decoded.data).toEqual(expected);
  }
});

test("decoded images fill chunks ending in [h, w] or [h, w, c]", () => {
  for (const [chunkShape, width, height, numComponents] of [
    [[1024, 1024], 1024, 1024, 1],
    [[1, 155, 127], 127, 155, 1],
    [[1, 1, 48, 64], 64, 48, 1],
    [[48, 64, 3], 64, 48, 3],
    [[1, 48, 64, 3], 64, 48, 3],
  ] as const) {
    checkJpeg2kChunkShape(chunkShape, width, height, numComponents);
  }
});

test("rejects a chunk the image does not fill", () => {
  expect(() => checkJpeg2kChunkShape([1, 64, 48], 64, 48, 1)).toThrow(
    /does not fill a chunk of shape \[1, 64, 48\]/,
  );
  expect(() => checkJpeg2kChunkShape([3, 48, 64], 64, 48, 3)).toThrow(
    /does not fill/,
  );
  expect(() => checkJpeg2kChunkShape([2, 48, 64], 64, 48, 1)).toThrow(
    /does not fill/,
  );
});

test("rejects a malformed codestream", async () => {
  const { encoded } = await fixture("uint8_gray");
  await expect(
    decompressJpeg2000(encoded.slice(0, 20), 1, false),
  ).rejects.toThrow(/^jpeg2000: /);
});

test("rejects samples wider than the data type", async () => {
  const { encoded } = await fixture("uint16_gray");
  await expect(decompressJpeg2000(encoded, 1, false)).rejects.toThrow(
    /more than the data type's 8/,
  );
});
