// The NDPI JPEG header parser of the browser virtualizer (VIRTUALIZE.md §3.7).
// Whole files are checked against tifffile by verify_tiff.py.

import assert from "node:assert/strict";
import { test } from "node:test";
import { jpegHeader } from "../src/ndpi.ts";

/** SOI, SOF0 for a 16 × 32 image with the given sampling factors, DRI 4, SOS. */
function header(factors: number[], sofMarker = 0xc0): Uint8Array {
  const sof = [8, 0, 16, 0, 32, factors.length, ...factors.flatMap((f, k) => [k, f, 0])];
  const sos = [factors.length, ...factors.flatMap((_, k) => [k, 0]), 0, 0x3f, 0];
  return Uint8Array.from([
    0xff, 0xd8,
    0xff, sofMarker, 0, 2 + sof.length, ...sof,
    0xff, 0xdd, 0, 4, 0, 4,
    0xff, 0xda, 0, 2 + sos.length, ...sos,
  ]);
}

test("the MCU size comes from the largest horizontal and vertical sampling factors", () => {
  for (const [factors, mcu] of [
    [[0x11, 0x11, 0x11], [8, 8]],
    [[0x21, 0x11, 0x11], [16, 8]],
    [[0x22, 0x11, 0x11], [16, 16]],
    [[0x12, 0x11, 0x11], [8, 16]],
  ] as const) {
    const [, , mw, mh, interval] = jpegHeader(header([...factors]));
    assert.deepEqual([mw, mh, interval], [...mcu, 4]);
  }
});

test("rejects a progressive JPEG", () => {
  assert.throws(() => jpegHeader(header([0x11], 0xc2)), /not baseline/);
});
