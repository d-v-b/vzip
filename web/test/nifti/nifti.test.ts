import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { ImageError } from "../../src/virtualize/common.ts";
import { virtualizeImage } from "../../src/virtualize/index.ts";
import { NiftiError } from "../../src/virtualize/nifti/virtualize.ts";

const FIXTURES = new URL("../fixtures/nifti/", import.meta.url);

async function virtualize(name: string) {
  const bytes = new Uint8Array(fs.readFileSync(new URL(name, FIXTURES)));
  return virtualizeImage(`https://data.test/${name}`, async (o, n) => bytes.subarray(o, o + n), bytes.length);
}

const decoded = (v: Awaited<ReturnType<typeof virtualize>>, key: string) => {
  const e = v.entries.find((x) => x.key === key);
  assert.ok(e && "bytes" in e);
  return JSON.parse(new TextDecoder().decode(e.bytes as Uint8Array));
};

test("virtualizes the synthetic NIfTI files", async () => {
  const cases: [string, object][] = [
    ["nifti_n1_le_int16_3d_sform.nii", { version: 1, byteOrder: "little", sizes: { z: 3, y: 4, x: 5 }, affine: "sform", translation: true }],
    ["nifti_n1_be_int16_4d_scaled.nii", { byteOrder: "big", sizes: { t: 5, z: 2, y: 3, x: 4 }, scaling: { slope: 0.5, inter: -20 }, translation: false }],
    ["nifti_n2_be_float64_5d.nii", { version: 2, sizes: { t: 2, c: 3, z: 2, y: 2, x: 3 }, dataType: "float64", affine: null }],
    ["nifti_n1_be_rgb24_4d.nii", { colour: true, scaling: null, sizes: { t: 2, c: 3, z: 2, y: 3, x: 2 } }],
    ["nifti_nibabel_n2_vector_ext.nii", { version: 2, extensions: true, chunks: 18 }],
    ["nifti_rowblock_split.nii", { rowBlock: 350, chunks: 2 }],
    ["nifti_edge_inter_inf.nii", { scaling: { slope: 2, inter: 0 } }],
  ];
  for (const [name, expected] of cases) {
    const v = await virtualize(name);
    assert.equal(v.format, "nifti");
    assert.deepEqual({ ...v.summary, ...expected }, v.summary, name);
  }
  // A colour type is interleaved, and its omero channels are R, G, B.
  const rgb = await virtualize("nifti_n1_be_rgb24_4d.nii");
  assert.deepEqual(decoded(rgb, "0/zarr.json").codecs, [
    { name: "transpose", configuration: { order: [0, 2, 3, 4, 1] } },
    { name: "bytes" },
  ]);
  assert.deepEqual(decoded(rgb, "zarr.json").attributes.ome.omero.channels.map((c: { label: string }) => c.label), ["R", "G", "B"]);
  // Scaling is recorded next to the OME metadata, and the window is in raw values.
  const scaled = decoded(await virtualize("nifti_n1_be_int16_4d_scaled.nii"), "zarr.json").attributes;
  assert.deepEqual(scaled.nifti, { scl_slope: 0.5, scl_inter: -20 });
  assert.deepEqual(scaled.ome.omero.channels[0].window, { min: -20, max: 60, start: -20, end: 60 });
  // The second row block of the slice starts 350 rows in.
  const blocks = await virtualize("nifti_rowblock_split.nii");
  const second = blocks.entries.find((e) => e.key === "0/c/0/1/0");
  assert.ok(second && "ranges" in second);
  assert.deepEqual(second.ranges, [{ source: 0, offset: BigInt(352 + 350 * 200), length: 70000n }]);
});

for (const [name, message] of [
  ["nifti_reject_dim0.nii", /dim\[0\] = 0/],
  ["nifti_reject_dim6.nii", /dimensions 6 and 7/],
  ["nifti_reject_n2_dim_huge.nii", /dim\[1\] = 9007199254740992/],
  ["nifti_reject_complex64.nii", /COMPLEX64/],
  ["nifti_reject_datatype_unknown.nii", /unknown NIfTI datatype 3/],
  ["nifti_reject_bitpix.nii", /bitpix 8/],
  ["nifti_reject_rgb_5d.nii", /fifth dimension/],
  ["nifti_reject_vox_offset_fraction.nii", /not an integer/],
  ["nifti_reject_n2_vox_offset_low.nii", /vox_offset 540/],
  ["nifti_reject_truncated.nii", /outside the 375-byte file/],
  ["nifti_reject_n2_short_header.nii", /too short/],
] as const) {
  test(`rejects ${name}`, async () => {
    await assert.rejects(virtualize(name), (e) => e instanceof NiftiError && message.test(e.message));
  });
}

test("rejects a header-and-image pair", async () => {
  await assert.rejects(virtualize("nifti_reject_pair_magic.nii"), (e) => e instanceof ImageError);
});
