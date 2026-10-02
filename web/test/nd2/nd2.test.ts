import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { virtualizeImage } from "../../src/virtualize/index.ts";
import { Nd2Error } from "../../src/virtualize/nd2/virtualize.ts";

const FIXTURES = new URL("../fixtures/nd2/", import.meta.url);

async function virtualize(name: string) {
  const bytes = new Uint8Array(fs.readFileSync(new URL(name, FIXTURES)));
  return virtualizeImage(`https://data.test/${name}`, async (o, n) => bytes.subarray(o, o + n), bytes.length);
}

test("virtualizes the synthetic ND2 files", async () => {
  const cases: [string, object][] = [
    ["nd2_tz_uint16.nd2", { sizes: { t: 3, z: 4, c: 2, y: 6, x: 5 }, compressed: false, paddedRows: false, frames: 12, missing: 0, channels: ["DAPI", "GFP"] }],
    ["nd2_padded_rgb.nd2", { sizes: { z: 2, c: 3, y: 4, x: 13 }, paddedRows: true, channels: ["Brightfield R", "Brightfield G", "Brightfield B"] }],
    ["nd2_compressed_positions.nd2", { sizes: { t: 3, p: 3, c: 1, y: 5, x: 7 }, compressed: true, positions: 3, frames: 9, missing: 1, channels: ["mCherry"] }],
    ["nd2_float_uncalibrated.nd2", { sizes: { c: 1, y: 3, x: 4 }, dataType: "float32", channels: ["C0"] }],
  ];
  for (const [name, expected] of cases) {
    const v = await virtualize(name);
    assert.equal(v.format, "nd2");
    assert.deepEqual({ ...v.summary, ...expected }, v.summary, name);
  }
  // Padded rows: one range per row of 39 bytes, 40 bytes apart.
  const rows = (await virtualize("nd2_padded_rgb.nd2")).entries.find((e) => e.key === "0/0/c/0/1/0/0");
  assert.ok(rows && "ranges" in rows && rows.ranges.length === 4);
  const [r0, r1] = rows.ranges as { offset: bigint; length: bigint }[];
  assert.equal(r0.length, 39n);
  assert.equal(r1.offset - r0.offset, 40n);
});

for (const [name, message] of [
  ["nd2_reject_lossy.nd2", /lossy/],
  ["nd2_reject_tiled.nd2", /tiled/],
  ["nd2_reject_loop_type.nd2", /loop type 7/],
  ["nd2_reject_version2.nd2", /version Ver2.0/],
  ["nd2_reject_header_lengths.nd2", /name length/],
] as const) {
  test(`rejects ${name}`, async () => {
    await assert.rejects(virtualize(name), (e) => e instanceof Nd2Error && message.test(e.message));
  });
}
