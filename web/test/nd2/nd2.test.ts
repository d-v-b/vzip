import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { virtualizeImage } from "../../conformance/reference/index.ts";
import { Nd2Error } from "../../conformance/reference/nd2/virtualize.ts";
import { gridCut } from "../../conformance/reference/nd2/source.ts";
import { blockReader, planGrid } from "../../src/virtualize/common.ts";
import { decodeLV, LVError, TooLargeError } from "../../conformance/reference/nd2/lv.ts";
import { deflateSync } from "node:zlib";

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

test("reads the frame headers ahead, at once, through an exact reader", async () => {
  for (const [name, frames] of [["nd2_tz_uint16.nd2", 12], ["nd2_compressed_positions.nd2", 8]] as const) {
    const bytes = new Uint8Array(fs.readFileSync(new URL(name, FIXTURES)));
    const exact: [number, number][] = [];
    const read = blockReader(async (o, n) => bytes.subarray(o, o + n), bytes.length, 64);
    const ahead = await virtualizeImage(`https://data.test/${name}`, read, bytes.length,
      async (o, n) => (exact.push([o, n]), bytes.subarray(o, o + n)));
    assert.deepEqual(ahead, await virtualize(name), name);
    // one exact read per placed frame, of its header and a name padded to 4 KiB
    assert.equal(exact.length, frames, name);
    assert.ok(exact.every(([o, n]) => n === Math.min(16 + 4096, bytes.length - o)), name);
  }
});

for (const [name, message] of [
  ["nd2_reject_lossy.nd2", /lossy/],
  ["nd2_reject_tiled.nd2", /tiled/],
  ["nd2_reject_loop_type.nd2", /loop type 7/],
  ["nd2_reject_version2.nd2", /version Ver2.0/],
  ["nd2_reject_header_lengths.nd2", /name length/],
  ["nd2_reject_frame_name_length.nd2", /frame 1's chunk header differs in name length/],
  ["nd2_reject_frame_magic.nd2", /no ND2 chunk/],
  ["nd2_reject_frame_short.nd2", /frame 1's chunk is too short for its pixels/],
  ["nd2_reject_positions.nd2", /65537 positions of 1 components/],
  ["nd2_reject_position_channels.nd2", /1025 positions of 1024 components/],
  ["nd2_reject_profile_records.nd2", /more than 1048576 LV records/],
] as const) {
  test(`rejects ${name}`, async () => {
    await assert.rejects(virtualize(name), (e) => e instanceof Nd2Error && message.test(e.message));
  });
}

test("rejects a profile chunk that inflates past 64 MiB", async () => {
  await assert.rejects(virtualize("nd2_reject_inflate_limit.nd2"),
    (e) => e instanceof LVError && /inflates to more than 67108864 bytes/.test(e.message));
});

test("other and empty past vzip_source's budget are arrays", async () => {
  const { v, node } = await source("nd2_source_names_spill.nd2");
  assert.deepEqual(node, { other: "other/names", empty: "other/empty" });
  const text = (key: string) => {
    const e = v.entries.find((x) => x.key === key);
    return new TextDecoder().decode((e as { bytes: Uint8Array }).bytes).replace(/\0+$/, "");
  };
  assert.deepEqual(JSON.parse(text("vzip_source/other/names/c/0")), Array.from({ length: 4000 }, (_, i) => `CustomData|a/${i}!`));
  assert.deepEqual(JSON.parse(text("vzip_source/other/empty/c/0")), Array.from({ length: 6000 }, (_, i) => `Empty|${i}!`));
});

/** The root's and vzip_source's source metadata of a fixture, and vzip_source's array paths. */
async function source(name: string) {
  const v = await virtualize(name);
  const doc = (key: string) => {
    const e = v.entries.find((x) => x.key === key);
    return e && "bytes" in e ? JSON.parse(new TextDecoder().decode(e.bytes as Uint8Array)) : undefined;
  };
  const arrays = v.entries.map((e) => e.key).filter((k) => k.startsWith("vzip_source/") && k.endsWith("/zarr.json"))
    .filter((k) => doc(k).node_type === "array").map((k) => k.slice("vzip_source/".length, -"/zarr.json".length));
  return {
    v,
    doc,
    root: doc("zarr.json").attributes.vzip_virtualized.nd2,
    node: doc("vzip_source/zarr.json").attributes.vzip_virtualized?.nd2 ?? {},
    arrays: new Set(arrays),
  };
}

test("the source metadata keeps every value", async () => {
  const { root, node, arrays } = await source("nd2_source_values.nd2");
  assert.deepEqual(root.chunks["ImageTextInfoLV!"], {
    SLxImageTextInfo: [["a", 1], ["a", 2], ["b", "x"], ["s", { utf16: "QQAA3A==" }], ["\0ANg=", 5],
      ["pairs", [["", ["k", 1]], ["", ["l", 2]]]]],
  });
  assert.deepEqual(root.chunks["CustomDataVar|NDControlV1_0!"].NDControl.LoopSize, [["no_name", 57], ["no_name", 0]]);
  assert.ok(!("CustomData|S!" in root.chunks) && arrays.has("CustomData/S") && !arrays.has("CustomData/T"));
  assert.deepEqual(Object.keys(node.chunks), ["ImageCalibrationLV|0!"]);
  assert.ok("CustomDataVar|DeepEnoughV1_0!" in root.chunks && arrays.has("CustomDataVar/DeepV1_0"));
  assert.ok(arrays.has("ImageDataSeq.trailing/offsets") && arrays.has("ImageDataSeq.trailing/data"));
});

test("the source metadata keeps every chunk", async () => {
  const { node, arrays } = await source("nd2_source_chunks.nd2");
  assert.deepEqual(node, {
    other: ["ImageDataSeq|01!", "ImageDataSeq!", "CustomData|X|1!", "CustomData|Foo|Bar!", "CustomData|a/b!",
      "CustomData|...!", "CustomData|__x!", "other!", "CustomData|é!", "CustomDataSeq|F|16777216!", "CustomDataSeq|F!"],
    empty: ["ImageDataSeq|x!", "CustomData|Empty!"],
  });
  for (const p of ["ImageDataSeq.beyond", "CustomData/AcqTimesCache", "CustomData/AcqTimesCache.rest", "CustomData/é",
    "CustomDataSeq/a\nb", "other/10"]) assert.ok(arrays.has(p), p);
});

test("a chunk over 16 KiB of JSON is on vzip_source", async () => {
  const { root, node } = await source("nd2_source_json_size_tagged.nd2");
  assert.ok("AtLimitLV!" in root.chunks);
  assert.deepEqual(Object.keys(node.chunks), ["OverLimitLV!"]);
});

test("values JSON cannot hold are tags, and integer-like names keep their order", async () => {
  const { root, node, doc } = await source("nd2_source_tags.nd2");
  assert.deepEqual(root.chunks["ImageTextInfoLV!"], {
    SLxTags: {
      big: { int: "18446744073709551615" }, neg: { int: "-9007199254740992" }, safe: 2 ** 53 - 1,
      nan: { float: "NaN" }, inf: { float: "Infinity" }, ninf: { float: "-Infinity" }, text: "NaN",
      one: [["utf16", "x"]], i: [["int", "5"]], fl: [["float", 1]], two: { int: 1, float: 2 },
      list: [1, { float: "Infinity" }],
    },
  });
  assert.deepEqual(root.chunks["ImageTagLV!"], [["int", 5]]);
  // The first declaration of S, under Tag_b, is before the one under "5".
  assert.equal(doc("vzip_source/CustomData/S/zarr.json").data_type, "int32");
  const good = { a: true, b: 2.5, n: { float: "NaN" }, l: [1, 2], t: { z: 1, B: 2, a: 3 } };
  assert.deepEqual(root.chunks["CustomData|Good!"], good);
  assert.deepEqual(root.chunks["CustomData|Zipped!"], good);
  assert.deepEqual(root.chunks["CustomData|Unnamed!"], { "": 1 });
  assert.deepEqual(root.chunks["CustomData|Unsorted!"], { t: { z: 1, B: 2, a: 3 } });
  assert.deepEqual(root.chunks["CustomData|Ties!"], { l: [1, 2] });
  for (const lossy of ["BoolByte", "EmptyNul", "AfterNul", "Skipped", "BadTable", "DupTable", "NaNBits", "ZippedLossy"]) {
    assert.ok(!(`CustomData|${lossy}!` in root.chunks), lossy);
    assert.equal(doc(`vzip_source/CustomData/${lossy}/zarr.json`).data_type, "uint8", lossy);
  }
  assert.ok("ImageMetadataSeqLV|00!" in root.chunks);
  assert.deepEqual(Object.keys(node.chunks), ["ImageMetadataSeqLV|01!"]);
});

test("sparse frames and families are bounded", async () => {
  const { v, node, doc } = await source("nd2_source_sparse.nd2");
  // Copied values: chunks of at most 64 KiB, cut as contiguous values with that limit.
  assert.deepEqual(doc("vzip_source/ImageDataSeq/zarr.json").chunk_grid.configuration.chunk_shape, [8043]);
  assert.deepEqual(v.entries.map((e) => e.key).filter((k) => k.startsWith("vzip_source/ImageDataSeq/c/")),
    ["vzip_source/ImageDataSeq/c/0"]);
  assert.deepEqual(node.other, ["ImageDataSeq|2000!", "ImageDataSeq|3003000!", "CustomDataSeq|x|5000!"]);
  assert.deepEqual(doc("vzip_source/CustomDataSeq/x/zarr.json").shape, [1, 1]);
});

test("the root keeps at most 64 KiB of chunks", async () => {
  const { root, node } = await source("nd2_source_root_budget.nd2");
  assert.deepEqual(Object.keys(node.chunks), ["BLV!"]);
  assert.deepEqual(Object.keys(root.chunks), ["ImageAttributesLV!", "ALV!", "CLV!", "DLV!", "ELV!"]);
  assert.ok(new TextEncoder().encode(JSON.stringify(root.chunks)).length <= 2 ** 16);
});

test("vzip_source keeps at most 64 KiB of chunks", async () => {
  const { root, node } = await source("nd2_source_node_budget.nd2");
  assert.deepEqual(Object.keys(node.chunks), ["ImageMetadataSeqLV|1!", "ImageMetadataSeqLV|2!", "ImageMetadataSeqLV|4!"]);
  assert.ok(new TextEncoder().encode(JSON.stringify(node.chunks)).length <= 2 ** 16);
  assert.deepEqual(root.chunks["BytesLV!"], { b: Array.from({ length: 100 }, (_, i) => i) });
  assert.deepEqual(root.chunks["CustomData|Index!"], [["b", 1], ["1", 2], ["a", [["z", 1], ["0", 2]]]]);
  assert.deepEqual(root.chunks["IndexLV!"], { L: [["x", 1], ["01", 2], ["10", 3]] });
  assert.deepEqual(root.chunks["CustomData|NegZero!"], { a: { float: "-0" }, b: 1 });
  assert.deepEqual(root.chunks["NegZeroLV!"], { a: { float: "-0" } });
  assert.deepEqual(root.chunks["CustomDataVar|NegZeroV1_0!"], { z: { float: "-0" }, one: 1 });
});

test("a chunk that does not fit vzip_source is bytes", async () => {
  const { root, node, doc } = await source("nd2_source_node_budget.nd2");
  for (const path of ["ImageMetadataSeqLV/3", "BigBytesLV", "HugeBytesLV"]) {
    assert.equal(doc(`vzip_source/${path}/zarr.json`).data_type, "uint8", path);
  }
  for (const name of ["ImageMetadataSeqLV|3!", "BigBytesLV!", "HugeBytesLV!"]) {
    assert.ok(!(name in root.chunks) && !(name in node.chunks), name);
  }
});

test("a failed decode is charged", async () => {
  const { root, doc } = await source("nd2_source_charged.nd2");
  assert.deepEqual(Object.keys(root.chunks), ["ImageAttributesLV!"]);
  assert.equal(doc("vzip_source/CustomData/Ok/zarr.json").data_type, "uint8");
});

test("an invalid stream is charged the most it could inflate to", async () => {
  const { root, doc } = await source("nd2_source_charged_invalid.nd2");
  assert.deepEqual(Object.keys(root.chunks), ["ImageAttributesLV!"]);
  assert.equal(doc("vzip_source/CustomData/Ok/zarr.json").data_type, "uint8");
});

test("frame times are copies of at most max(1 MiB, 512 bytes per placed frame)", async () => {
  for (const [name, chunkShape, keys] of [
    ["nd2_streams.nd2", [2, 3], ["0/0"]],
    ["nd2_source_frame_times.nd2", [1, 2048], Array.from({ length: 40 }, (_, t) => `${t}/0`)],
    ["nd2_source_frame_times_sparse.nd2", [64], Array.from({ length: 2000 }, (_, k) => `${128 * k}`)],
  ] as [string, number[], string[]][]) {
    const { v, doc } = await source(name);
    assert.deepEqual(doc("vzip_source/ImageDataSeq/zarr.json").chunk_grid.configuration.chunk_shape, chunkShape);
    const stamps = v.entries.filter((e) => e.key.startsWith("vzip_source/ImageDataSeq/c/"));
    assert.deepEqual(stamps.map((e) => e.key).sort(), keys.map((k) => `vzip_source/ImageDataSeq/c/${k}`).sort());
  }
});

test("a declared tag over 16 KiB of JSON is its index", async () => {
  const { doc } = await source("nd2_source_tag_index.nd2");
  const tag = (path: string) => doc(`vzip_source/${path}/zarr.json`).attributes.vzip_virtualized.nd2;
  assert.deepEqual(tag("CustomData/Small"), { tag: { ID: "Small", Type: 3, Desc: "small" } });
  assert.deepEqual(tag("CustomData/Big"), { tag_index: 2 });
});

test("path segments a store cannot hold go to other", async () => {
  const { node, arrays } = await source("nd2_source_document_names.nd2");
  assert.deepEqual(node.other, ["zarr.json!", "Foo|zarr.json!", "Foo|.zarray!", ".zgroup|x!", "Foo\0Baz!"]);
  assert.ok(arrays.has("Foo/Bar") && arrays.has("zarr.json~") && arrays.has("other/4"));
});

const zipped = (b: Uint8Array) => new Uint8Array([0x4c, 0, ...new Uint8Array(10), ...deflateSync(b)]);
const byteArray = new Uint8Array([9, 2, 98, 0, 0, 0, 3, 0, 0, 0, 0, 0, 0, 0, 1, 2, 3]); // "b": [1, 2, 3]

test("a byte array is one value, and the inflated size is reported", async () => {
  const inflated: number[] = [];
  const value = await decodeLV(zipped(byteArray), 100, inflated, false, 5);
  assert.deepEqual(value.get("b"), new Uint8Array([1, 2, 3]));
  assert.deepEqual(inflated, [17]);
});

for (const [what, data, limit] of [
  ["truncated", zipped(new Uint8Array(1000)).subarray(0, -3), 1e6],
  ["bytes after the stream", new Uint8Array([...zipped(new Uint8Array(1000)), 120]), 1e6],
  ["past the limit", zipped(new Uint8Array(1000)), 999],
] as [string, Uint8Array, number][]) {
  test(`a stream that does not inflate is charged the most it could: ${what}`, async () => {
    const inflated: number[] = [];
    await assert.rejects(decodeLV(data, limit, inflated), LVError);
    assert.deepEqual(inflated, [Math.min(limit, 1032 * data.length)]);
  });
}

test("LV data past its room is too large", async () => {
  await decodeLV(byteArray, Infinity, undefined, false, 4);
  await assert.rejects(decodeLV(byteArray, Infinity, undefined, false, 3), TooLargeError);
});

test("gridCut is the cut of contiguous values", () => {
  const cases: [number[], number][] = [[[1], 8], [[5, 7], 4], [[3_000_000], 8], [[2_097_153], 8], [[4096, 1024], 8],
    [[3, 5_000_000], 4], [[1000, 3, 7000], 8], [[2 ** 21 + 1, 3], 8], [[65537, 257], 8], [[4195, 1000], 8],
    [[40, 2 ** 21], 8]];
  let seed = 3;
  const rand = (n: number) => (seed = (seed * 1103515245 + 12345) % 2 ** 31) % n + 1; // 1 to n
  while (cases.length < 600) {
    const shape = Array.from({ length: rand(3) }, () => [1, 2, 3, 7, rand(60), rand(5000), rand(300_000)][rand(7) - 1]);
    if (shape.reduce((p, v) => p * v, 1) <= 2 ** 28) cases.push([shape, [1, 4, 8][rand(3) - 1]]);
  }
  for (const [shape, item] of cases) {
    for (const limit of [2 ** 24, 2 ** 16, 2 ** 12]) {
      const [a, c] = gridCut(shape, item, limit);
      if (shape.slice(0, a).reduce((p, v) => p * v, 1) > 2 ** 16) continue; // too many chunks for planGrid to list
      assert.deepEqual([...new Array(a).fill(1), c, ...shape.slice(a + 1)], planGrid(0, shape, item, true, limit)[0],
        `${shape} ${item} ${limit}`);
    }
  }
});
