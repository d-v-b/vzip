import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { ImageError } from "../../src/virtualize/common.ts";
import { virtualizeImage } from "../../src/virtualize/index.ts";
import { NiftiError } from "../../src/virtualize/nifti/virtualize.ts";

const FIXTURES = new URL("../../../fixtures/nifti/", import.meta.url);

async function virtualize(name: string) {
  const bytes = new Uint8Array(fs.readFileSync(new URL(name, FIXTURES)));
  return virtualizeImage(`https://data.test/${name}`, async (o, n) => bytes.subarray(o, o + n), bytes.length);
}

const fileSize = (name: string) => fs.statSync(new URL(name, FIXTURES)).size;

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
    ["nifti_n1_be_rgb24_4d.nii", { color: true, scaling: null, sizes: { t: 2, c: 3, z: 2, y: 3, x: 2 } }],
    ["nifti_nibabel_n2_vector_ext.nii", { version: 2, extensions: true, chunkShape: [2, 3, 3, 4, 5], chunks: 1 }],
    ["nifti_rowblock_split.nii", { chunkShape: [1, 350, 200], chunks: 2 }],
    ["nifti_block_rows_edge_rgba.nii", { chunkShape: [4, 110, 150], chunks: 2 }],
    ["nifti_block_x_split.nii", { chunkShape: [1, 10000], chunks: 2 }],
    ["nifti_block_x_split_rgb24.nii", { chunkShape: [3, 1, 1, 22500], chunks: 4 }],
    ["nifti_chunk_time_series.nii", { chunkShape: [20000, 1, 1, 1], chunks: 2 }],
    ["nifti_chunk_many_channels.nii", { chunkShape: [3, 100, 1, 2, 2], chunks: 1 }],
    ["nifti_chunk_edge_literal.nii", { chunkShape: [1, 65537], chunks: 2 }],
    ["nifti_edge_inter_inf.nii", { scaling: { slope: 2, inter: 0 } }],
  ];
  for (const [name, expected] of cases) {
    const v = await virtualize(name);
    assert.equal(v.format, "nifti");
    assert.deepEqual({ ...v.summary, ...expected }, v.summary, name);
    // no data sources; source 0 pins the file's size (spec/virtualize.md §1.2)
    assert.deepEqual(v.sources, [{ url: `https://data.test/${name}`, size: BigInt(fileSize(name)) }], name);
  }
  // A color type is interleaved, and its omero channels are R, G, B.
  const rgb = await virtualize("nifti_n1_be_rgb24_4d.nii");
  assert.deepEqual(decoded(rgb, "0/zarr.json").codecs, [
    { name: "transpose", configuration: { order: [0, 2, 3, 4, 1] } },
    { name: "bytes" },
  ]);
  assert.deepEqual(decoded(rgb, "zarr.json").attributes.ome.omero.channels.map((c: { label: string }) => c.label), ["R", "G", "B"]);
  // Scaling is recorded in the convention's property (conventions §2), and the window is in raw values.
  const scaled = decoded(await virtualize("nifti_n1_be_int16_4d_scaled.nii"), "zarr.json").attributes;
  assert.deepEqual(Object.keys(scaled).sort(), ["ome", "vzip_virtualized", "zarr_conventions"]);
  assert.deepEqual(scaled.vzip_virtualized.nifti.scaling, { slope: 0.5, inter: -20 });
  assert.equal(scaled.vzip_virtualized.nifti.header.scl_slope, 0.5);
  assert.deepEqual(scaled.ome.omero.channels[0].window, { min: -20, max: 60, start: -20, end: 60 });
  // A fifth dimension is stored before the fourth: transposed. Over 64 channels, no display windows.
  const many = await virtualize("nifti_chunk_many_channels.nii");
  assert.deepEqual(decoded(many, "0/zarr.json").codecs[0], { name: "transpose", configuration: { order: [1, 0, 2, 3, 4] } });
  assert.equal(decoded(many, "zarr.json").attributes.ome.omero, undefined);
  // The second chunk of the slice starts 350 rows in.
  const blocks = await virtualize("nifti_rowblock_split.nii");
  const second = blocks.entries.find((e) => e.key === "0/c/0/1/0");
  assert.ok(second && "ranges" in second);
  assert.deepEqual(second.ranges, [{ source: 0, offset: BigInt(352 + 70000), length: 70000n }]);
  // A row of color voxels split along x: chunks of 22500 voxels, their samples interleaved.
  const rgb24 = await virtualize("nifti_block_x_split_rgb24.nii");
  assert.deepEqual(decoded(rgb24, "0/zarr.json").chunk_grid.configuration.chunk_shape, [3, 1, 1, 22500]);
  const edge = rgb24.entries.find((e) => e.key === "0/c/0/1/0/1");
  assert.ok(edge && "ranges" in edge);
  assert.deepEqual(edge.ranges, [{ source: 0, offset: BigInt(544 + 135000 + 67500), length: 67500n }]);
  // An edge chunk's padding is a literal.
  const literal = (await virtualize("nifti_chunk_edge_literal.nii")).entries.find((e) => e.key === "0/c/0/1");
  assert.ok(literal && "ranges" in literal);
  assert.deepEqual(literal.ranges, [{ source: 0, offset: BigInt(544 + 65537), length: 65536n }, { data: new Uint8Array(1) }]);
  // A display window that overflows binary64 is left out.
  assert.equal(decoded(await virtualize("nifti_edge_n2_window_overflow.nii"), "zarr.json").attributes.ome.omero, undefined);
});

test("keeps the bytes between the header and the voxels", async () => {
  const abc = [{ ecode: 6, text: "abcdefg" }];
  const compact = { ecode: "vzip_source/extensions/ecode", esize: "vzip_source/extensions/esize", data: "vzip_source/extensions/data" };
  const cases: [string, object][] = [
    ["nifti_header_rest.nii", { header_rest: {
      descrip: "aGlkZGVuIGFmdGVyIHRoZSBOVUwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
      aux_file: "AABtb3JlAAAAAAAAAAAAAAAAAAA=", intent_name: "eAAAAAAAAAAAAAAAAAAA" } }],
    ["nifti_region_no_extender.nii", { unparsed: "vzip_source/unparsed" }],
    ["nifti_region_extender_reserved.nii", { extender: "AQcICQ==", extensions: [{ ecode: 6, text: "hello" }] }],
    ["nifti_region_chain_tail.nii", { extensions: abc, extensions_truncated: true, unparsed: "vzip_source/unparsed" }],
    ["nifti_region_chain_zero_pad.nii", { extensions: abc }],
    ["nifti_region_extensions_moved.nii", { extensions: compact }],
    ["nifti_region_text_large.nii", { extensions: compact }],
    // Text extensions whose esize is not the fewest NULs' keep it.
    ["nifti_region_text_esize.nii", { extensions: [
      { ecode: 6, text: "abc", esize: 32 }, { ecode: 6, text: "hello", esize: 13 }, { ecode: 4, text: "<x/>" },
      { ecode: 6, text: "12345678" }] }],
    ["nifti_reconstruction.nii", {
      extensions: [{ ecode: 6, text: "hello" }, { ecode: 2, data: "vzip_source/extensions/1" }],
      extensions_truncated: true, unparsed: "vzip_source/unparsed", trailing: "vzip_source/trailing",
    }],
  ];
  for (const [name, expected] of cases) {
    const meta = decoded(await virtualize(name), "zarr.json").attributes.vzip_virtualized.nifti;
    const { nifti_version: _v, byte_order: _b, header: _h, affine: _a, ...rest } = meta;
    assert.deepEqual(rest, expected, name);
  }
  // Over the root's budget: the ecodes, the text extensions that fit, and the others' data as a family.
  const over = await virtualize("nifti_region_extensions_compact.nii");
  const { extensions } = decoded(over, "zarr.json").attributes.vzip_virtualized.nifti;
  assert.deepEqual(Object.keys(extensions.text).length, 374);
  assert.equal(extensions.text["1"], "z".repeat(9000));
  assert.equal(extensions.text["374"], "comment 372");
  assert.ok(new TextEncoder().encode(JSON.stringify(extensions)).length <= 16384);
  assert.equal(decoded(over, "vzip_source/zarr.json").attributes.vzip_virtualized, undefined);
  assert.deepEqual(decoded(over, "vzip_source/extensions/data/data/zarr.json").shape, [104 + 527 * 24]);
  const values = (v: typeof over, name: string) => {
    const e = v.entries.find((x) => x.key === `vzip_source/extensions/${name}/c/0`);
    assert.ok(e && "bytes" in e);
    return [...new Int32Array(e.bytes.slice().buffer)];
  };
  assert.deepEqual(values(over, "ecode"), [2, ...new Array(901).fill(6)]);
  assert.deepEqual(values(over, "esize"), [112, 9008, ...new Array(900).fill(32)]);
  const padded = await virtualize("nifti_region_extensions_compact_esize.nii");
  assert.deepEqual(values(padded, "esize"), new Array(700).fill(48));
  // Kept as packed columns: over the budget once built, over it by the count alone, and a family
  // whose second chunk holds over a payload's worth of ranges, so its bytes are copied.
  const text = async (name: string) =>
    decoded(await virtualize(name), "zarr.json").attributes.vzip_virtualized.nifti.extensions.text;
  assert.deepEqual(await text("nifti_region_extensions_built_over.nii"),
    Object.fromEntries(Array.from({ length: 760 }, (_, i) => [String(i), ""])));
  assert.deepEqual(Object.keys(await text("nifti_region_extensions_count_over.nii")).length, 1736);
  const family = await virtualize("nifti_region_extensions_family_chunks.nii");
  const second = family.entries.find((x) => x.key === "vzip_source/extensions/data/data/c/1");
  assert.ok(second && "bytes" in second && second.bytes.length === 632584);
});

test("keeps the bits of float header fields", async () => {
  // A negative zero, or a NaN other than the canonical one, is {"bits": hex} (spec/virtualize/nifti.md §5).
  const header = async (name: string) => decoded(await virtualize(name), "zarr.json").attributes.vzip_virtualized.nifti.header;
  const one = await header("nifti_header_float_bits.nii");
  assert.deepEqual([one.intent_p1, one.scl_slope, one.cal_min, one.slice_duration, one.toffset, one.pixdim[5], one.srow_x[1]],
    [{ bits: "80000000" }, { bits: "7fa00000" }, "NaN", { bits: "ffc00000" }, { bits: "7fc00001" }, { bits: "80000000" },
      { bits: "80000000" }]);
  const two = await header("nifti_header_n2_float_bits.nii");
  assert.deepEqual([two.intent_p1, two.cal_min, two.slice_duration, two.toffset, two.pixdim[1]],
    [{ bits: "8000000000000000" }, "NaN", { bits: "fff8000000000000" }, { bits: "7ff8000000000001" },
      { bits: "7ff4000000000000" }]);
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
