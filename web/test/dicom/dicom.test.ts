import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { DicomError } from "../../src/virtualize/dicom/virtualize.ts";
import { virtualizeImage } from "../../src/virtualize/index.ts";

const FIXTURES = new URL("../fixtures/dicom/", import.meta.url);

async function virtualize(name: string) {
  const bytes = new Uint8Array(fs.readFileSync(new URL(name, FIXTURES)));
  return virtualizeImage(`https://data.test/${name}`, async (o, n) => bytes.subarray(o, o + n), bytes.length);
}

const json = (v: Awaited<ReturnType<typeof virtualize>>, key: string) => {
  const e = v.entries.find((e) => e.key === key);
  assert.ok(e && "bytes" in e, key);
  return JSON.parse(new TextDecoder().decode(e.bytes));
};

test("virtualizes the synthetic DICOM files", async () => {
  const cases: [string, object][] = [
    ["dicom_implicit_mono16.dcm", { axes: ["y", "x"], shape: [12, 10], dataType: "uint16", transferSyntax: "1.2.840.10008.1.2" }],
    ["dicom_explicit_signed16_frames.dcm", { axes: ["z", "y", "x"], shape: [4, 6, 9], dataType: "int16", photometric: "MONOCHROME1", frames: 4 }],
    ["dicom_bigendian_mono16.dcm", { shape: [2, 5, 7], transferSyntax: "1.2.840.10008.1.2.2" }],
    ["dicom_explicit_int32.dcm", { dataType: "int32" }],
    ["dicom_rgb_interleaved.dcm", { axes: ["c", "z", "y", "x"], shape: [3, 2, 5, 6], references: 2 }],
    ["dicom_rgb_planar.dcm", { axes: ["c", "z", "y", "x"], references: 6 }],
    ["dicom_jpeg_rgb_fragments_bot.dcm", { photometric: "RGB", frames: 2, references: 2 }],
    ["dicom_jpeg_single_frame_fragments.dcm", { axes: ["y", "x"], frames: 1 }],
    ["dicom_j2k_mono16_eot.dcm", { shape: [3, 9, 7], transferSyntax: "1.2.840.10008.1.2.4.90" }],
    ["dicom_wsi_tiled_full_jpeg.dcm", { axes: ["c", "y", "x"], shape: [3, 30, 40], wholeSlide: true, frames: 6 }],
    ["dicom_wsi_tiled_full_native.dcm", { axes: ["y", "x"], shape: [9, 13], wholeSlide: true, references: 4 }],
  ];
  for (const [name, expected] of cases) {
    const v = await virtualize(name);
    assert.equal(v.format, "dicom", name);
    assert.deepEqual({ ...v.summary, ...expected }, v.summary, name);
  }

  // Pixel spacing (mm) from the shared functional groups, and the VOI window in stored values.
  const ct = json(await virtualize("dicom_implicit_mono16.dcm"), "zarr.json").attributes.ome;
  assert.deepEqual(ct.multiscales[0].datasets[0].coordinateTransformations[0].scale, [0.5, 0.25]);
  assert.deepEqual(ct.omero.channels[0].window, { min: 0, max: 4095, start: 864, end: 1264 });
  const sq = json(await virtualize("dicom_explicit_sequences_mono8.dcm"), "zarr.json").attributes.ome;
  assert.deepEqual(sq.multiscales[0].datasets[0].coordinateTransformations[0].scale, [0.2, 0.3]);

  // A JPEG frame of three fragments: the Adobe marker, then the fragments without their item headers.
  const rgb = (await virtualize("dicom_jpeg_rgb_fragments_bot.dcm")).entries.find((e) => e.key === "0/c/0/1/0/0");
  assert.ok(rgb && "ranges" in rgb && rgb.ranges.length === 4);
  const prefix = rgb.ranges[0];
  assert.ok("data" in prefix && prefix.data.at(-1) === 0); // transform 0: RGB as stored
});

for (const [name, message] of [
  ["dicom_reject_rle.dcm", /transfer syntax 1\.2\.840\.10008\.1\.2\.5/],
  ["dicom_reject_jpegls.dcm", /transfer syntax/],
  ["dicom_reject_palette.dcm", /PALETTE COLOR/],
  ["dicom_reject_native_ybr.dcm", /YBR_FULL/],
  ["dicom_reject_tiled_sparse.dcm", /TILED_FULL/],
  ["dicom_reject_focal_planes.dcm", /focal planes/],
  ["dicom_reject_wsi_frames.dcm", /3 frames for 2 by 2 tiles/],
  ["dicom_reject_fragments_without_offsets.dcm", /without an offset table/],
  ["dicom_reject_bot_offset.dcm", /not a fragment's/],
  ["dicom_reject_eot_with_bot.dcm", /Extended Offset Table with a Basic/],
  ["dicom_reject_short_pixel_data.dcm", /less than 3 frames/],
  ["dicom_reject_high_bit.dcm", /High Bit/],
  ["dicom_reject_bits_allocated.dcm", /Bits Allocated 12/],
  ["dicom_reject_bigendian_ow8.dcm", /OW in big endian/],
  ["dicom_reject_zero_frames.dcm", /Number of Frames 0/],
  ["dicom_reject_no_pixel_data.dcm", /no Pixel Data/],
  ["dicom_reject_misplaced_delimiter.dcm", /expected an item/],
  ["dicom_reject_truncated_sequence.dcm", /runs past its container/],
  ["dicom_reject_unknown_vr.dcm", /unknown VR/],
  ["dicom_reject_rows_vr.dcm", /VR SS, not US/],
  ["dicom_reject_depth.dcm", /nested more than 64/],
] as const) {
  test(`rejects ${name}`, async () => {
    await assert.rejects(virtualize(name), (e) => e instanceof DicomError && message.test(e.message));
  });
}
