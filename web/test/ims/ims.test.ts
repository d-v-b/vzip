import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { virtualizeImage } from "../../src/virtualize/index.ts";
import { ImsError } from "../../src/virtualize/ims/virtualize.ts";

const FIXTURES = new URL("../fixtures/ims/", import.meta.url);

async function virtualize(name: string) {
  const bytes = new Uint8Array(fs.readFileSync(new URL(name, FIXTURES)));
  return virtualizeImage(`https://data.test/${name}`, async (o, n) => bytes.subarray(o, o + n), bytes.length);
}

test("virtualizes the synthetic IMS files", async () => {
  const cases: [string, object][] = [
    ["ims_earliest_uint16_deflate.ims", { levels: 2, sizes: { t: 2, c: 2, z: 5, y: 12, x: 14 }, dataType: "uint16", compressed: [true, true], channels: ["DAPI", "GFP"] }],
    ["ims_latest_uint8.ims", { levels: 3, sizes: { t: 1, c: 3, z: 4, y: 9, x: 11 }, dataType: "uint8", channels: ["red", "Channel 1", "far red"] }],
    ["ims_latest_float32_be.ims", { sizes: { t: 3, c: 1, z: 1, y: 7, x: 9 }, dataType: "float32", compressed: [true] }],
    ["ims_latest_dense_links.ims", { sizes: { t: 50, c: 1, z: 1, y: 2, x: 3 }, chunks: 50 }],
    ["ims_latest_huge_attribute.ims", { levels: 2, channels: ["Kanal α", "Kanal β"] }],
    ["ims_latest_paged.ims", { sizes: { t: 1, c: 1, z: 1, y: 80, x: 63 }, chunkShape: [1, 1, 2], chunks: 64 }],
    ["ims_latest_soft_links.ims", { sizes: { t: 1, c: 2, z: 1, y: 5, x: 6 }, channels: ["first", "second"] }],
    ["ims_small_k.ims", { sizes: { t: 1, c: 5, z: 3, y: 5, x: 6 }, chunks: 133 }],
    ["ims_2d_no_metadata.ims", { sizes: { t: 1, c: 1, z: 1, y: 5, x: 6 }, dataType: "int16", channels: ["Channel 0"] }],
  ];
  for (const [name, expected] of cases) {
    const v = await virtualize(name);
    assert.equal(v.format, "ims");
    assert.deepEqual({ ...v.summary, ...expected }, v.summary, name);
  }
  // The time step is the mean interval between the first and last time points.
  const v = await virtualize("ims_latest_float32_be.ims");
  const root = v.entries.find((e) => e.key === "zarr.json");
  assert.ok(root && "bytes" in root);
  const ms = JSON.parse(new TextDecoder().decode(root.bytes)).attributes.ome.multiscales[0];
  assert.deepEqual(ms.datasets[0].coordinateTransformations, [
    { type: "scale", scale: [0.125, 1.5, 1.5] },
    { type: "translation", translation: [0, 4, -5] },
  ]);
});

for (const [name, message] of [
  ["ims_reject_not_imaris.ims", /not an Imaris file/],
  ["ims_reject_shuffle.ims", /filters \[2, 1\]/],
  ["ims_reject_fill_value.ims", /fill value/],
  ["ims_reject_extensible.ims", /chunk index type 4/],
  ["ims_reject_size_mismatch.ims", /differ in size/],
  ["ims_reject_imagesize_vlen.ims", /ImageSizeX is not a string/],
  ["ims_reject_relative_soft_link.ims", /relative path/],
] as const) {
  test(`rejects ${name}`, async () => {
    await assert.rejects(virtualize(name), (e) => e instanceof ImsError && message.test(e.message));
  });
}
