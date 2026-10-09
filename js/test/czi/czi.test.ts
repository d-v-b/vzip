import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { blockReader } from "../../src/virtualize/common.ts";
import { virtualizeImage } from "../../../conformance/virtualize/reference/ts/index.ts";
import { boundedHead, jpegFrame, jpegxrSize, zstd1Header, zstdContentSize } from "../../../conformance/virtualize/reference/ts/czi/coding.ts";
import { layer, rowBand } from "../../../conformance/virtualize/reference/ts/czi/layout.ts";
import { guid } from "../../../conformance/virtualize/reference/ts/czi/segments.ts";
import { CziError } from "../../../conformance/virtualize/reference/ts/czi/virtualize.ts";

const FIXTURES = fileURLToPath(new URL("../../../fixtures/czi/", import.meta.url));
const utf8 = new TextDecoder();

async function virtualize(name: string) {
  const bytes = new Uint8Array(fs.readFileSync(FIXTURES + name));
  return virtualizeImage(`https://data.test/czi/${name}`,
    blockReader(async (o, n) => bytes.subarray(o, o + n), bytes.length), bytes.length);
}

type V = Awaited<ReturnType<typeof virtualize>>;
function doc(v: V, key: string) {
  const e = v.entries.find((x) => x.key === key);
  assert.ok(e && "bytes" in e, key);
  return JSON.parse(utf8.decode(e.bytes as Uint8Array));
}

const one = [[[1, 1, 1]]];
const s = (subblocks: number, images: number[][][], extra: object = {}) =>
  ({ subblocks, images, tiles: 0, unplaced: 0, segments: 0, attachments: 0, tail: 0, ...extra });

// The Python reference's summaries (python -m vzip.virtualize).
const SUMMARIES: [string, object][] = [
  ["czi_attachments", s(1, one, { attachments: 12 })],
  ["czi_bgr24_raw", s(1, one)],
  ["czi_bgr48_raw", s(2, one)],
  ["czi_bgra32_raw", s(1, one)],
  ["czi_clipped_raw", s(12, [[[1, 3, 2]]])],
  ["czi_deleted", s(1, one, { segments: 4, tail: 30 })],
  ["czi_empty_directory", s(0, [])],
  ["czi_entry_mismatch", s(2, one, { unplaced: 1 })],
  ["czi_gray16_tczyx", s(24, one, { attachments: 3 })],
  ["czi_gray8_single", s(1, one)],
  ["czi_irregular", s(6, [], { tiles: 5 })],
  ["czi_jpeg_grid", s(6, [[[1, 2, 2]], [[1, 2, 1]]])],
  ["czi_jxr", s(8, Array(8).fill(one[0]))],
  ["czi_jxr_pyramid", s(9, [[[2, 2, 2], [4, 1, 1]]], { tiles: 4 })],
  ["czi_line_scan", s(5, one)],
  ["czi_many_attachments", s(1, one, { attachments: 700 })],
  ["czi_metadata_attachment", s(1, one)],
  ["czi_mixed_types", s(2, [], { tiles: 2 })],
  ["czi_mosaic_overlap", s(12, [], { tiles: 3 })],
  ["czi_multiscene", s(5, Array(5).fill(one[0]))],
  ["czi_pyramid_edge_bug", s(2, one, { tiles: 1 })],
  ["czi_regular_holes", s(9, [[[1, 3, 2]]])],
  ["czi_spare_bytes", s(2, [[[1, 1, 1], [2, 1, 1]]])],
  ["czi_subblock_attachments", s(3, one)],
  ["czi_subblock_masks", s(3, one)],
  ["czi_subsampled", s(3, [[[2, 1, 1]]])],
  ["czi_trailing", s(1, one)],
  ["czi_types", s(6, Array(6).fill(one[0]))],
  ["czi_unplaced", s(7, one, { unplaced: 6 })],
  ["czi_xml_latin1", s(1, one)],
  ["czi_xml_odd", s(2, one)],
  ["czi_zstd", s(6, Array(6).fill(one[0]))],
];

test("virtualizes the synthetic CZI files", async () => {
  const names = fs.readdirSync(FIXTURES).filter((f) => f.endsWith(".czi") && !f.startsWith("czi_reject_"));
  assert.deepEqual(names.map((f) => f.slice(0, -4)).sort(), SUMMARIES.map(([n]) => n).sort());
  for (const [name, summary] of SUMMARIES) {
    const v = await virtualize(`${name}.czi`);
    assert.equal(v.format, "czi", name);
    assert.deepEqual(v.summary, summary, name);
    const root = doc(v, "zarr.json");
    assert.equal(root.attributes.vzip_virtualized.profile, "czi", name);
    if (name === "czi_jxr_pyramid") {
      assert.deepEqual(doc(v, "0/0/zarr.json").chunk_grid,
        { name: "rectilinear", configuration: { kind: "inline", chunk_shapes: [[[8, 1], 5], [[10, 1], 9]] } });
    }
    if (name === "czi_zstd") {
      const shuffled = [0, 1, 2, 3, 4, 5].filter((k) =>
        doc(v, `${k}/0/zarr.json`).codecs.some((c: { name: string }) => c.name === "numcodecs.shuffle"));
      assert.deepEqual(shuffled, [2, 3]); // Gray16 and Bgr48, hi-lo packed
      assert.deepEqual(doc(v, "2/0/zarr.json").codecs[1], { name: "numcodecs.shuffle", configuration: { elementsize: 2 } });
    }
    if (name === "czi_irregular") {
      const tile = doc(v, "tiles/0/zarr.json");
      assert.deepEqual(tile.attributes.vzip_virtualized.czi,
        { x: 0, y: 0, size: [10, 10], stored_size: [10, 10], planes: { t: 0, c: 0, z: 0 }, copy: 0 });
      const lazy = v.entries.find((e) => e.key === "tiles/0/zarr.json");
      assert.ok(lazy && "bytes" in lazy && lazy.compress);
    }
    if (name === "czi_many_attachments") {
      assert.equal(doc(v, "vzip_source/zarr.json").attributes.vzip_virtualized.czi.attachments, "attachments/index");
      assert.deepEqual(doc(v, "vzip_source/attachments/index/zarr.json").shape, [103491]);
      const index = v.entries.find((e) => e.key === "vzip_source/attachments/index/c/0");
      assert.ok(index && "bytes" in index);
      assert.equal(JSON.parse(utf8.decode(index.bytes as Uint8Array)).length, 700);
    }
  }
});

test("rejects each czi_reject_ fixture with CziError", async () => {
  const names = fs.readdirSync(FIXTURES).filter((f) => f.startsWith("czi_reject_"));
  assert.ok(names.length > 30);
  for (const name of names) await assert.rejects(virtualize(name), CziError, name);
});

test("layer and rowBand", () => {
  const layers: [[number, number, number, number], [number, number] | undefined][] = [
    [[10, 10, 10, 10], [1, 0]], [[20, 10, 10, 5], [2, 1]], [[10, 20, 5, 10], [2, 1]], [[30, 30, 10, 10], [3, 1]],
    [[1024, 1024, 1, 1], [2, 10]], [[2100, 10, 1, 1], undefined], [[100, 100, 7, 7], undefined],
    [[17, 17, 10, 10], undefined],
  ];
  for (const [args, expected] of layers) assert.deepEqual(layer(...args), expected, String(args));
  const bands: [[number, number, number, number], number][] = [
    [[100, 100, 100, 1], 100], [[4096, 4096, 4096, 1], 4096], [[4096, 1000, 8192, 1], 8], [[4097, 4097, 4096, 2], 241],
    [[6000, 6000, 6000, 3], 750], [[2 ** 20, 2 ** 20, 64, 1], 262144],
  ];
  for (const [args, expected] of bands) assert.equal(rowBand(...args), expected, String(args));
});

const fromHex = (h: string) => Uint8Array.from(h.match(/../g) ?? [], (x) => parseInt(x, 16));
const head = (h: string) => {
  const b = fromHex(h);
  return boundedHead(async (o, n) => b.slice(o, o + n), 0, b.length);
};

test("codec header parsers", async () => {
  for (const [h, expected] of [
    ["01", [1, false]], ["030101", [3, true]], ["030100", [3, false]], ["030001", null], ["02", null], ["", null],
  ] as const) {
    assert.deepEqual(await zstd1Header(head(h)), expected, h);
  }
  for (const [h, expected] of [
    ["28b52ffd2064", 100n], ["28b52ffd0000", null], ["28b52ffd40001000", 272n], ["28b52ffd601000", 272n],
    ["28b52ffd800001020304", 67305985n], ["28b52ffdc0000102030405060708", 578437695752307201n], ["28b52ffd2801", null],
    ["28b52ffd2101", null], ["000000000000", null], ["28b52ffd20", null],
  ] as const) {
    assert.equal(await zstdContentSize(head(h), 0), expected, h);
  }
  for (const [h, p, expected] of [
    ["ffd8ffc0001108001e002803000000000000000000", 3, [40, 30]],
    ["ffd8ffe000046162ffffffc2000b080005000601000000", 1, [6, 5]],
    ["ffd8ffc0001108001e002803000000000000000000", 1, null],
    ["ffd8ffc1000b0c0003000401000000", 1, null],
    ["ffd8ffc3000b080003000401000000", 1, null],
    ["ffd8ffda0002", 1, null],
    ["ffd8ffc400046162ffc0000b080007000901000000", 1, [9, 7]],
    ["ffd8ffd0ffc0000b080007000901000000", 1, [9, 7]],
    ["ffd9", 1, null],
  ] as const) {
    assert.deepEqual(await jpegFrame(head(h), p), expected, h);
  }
  const jxr = (sizes: string, format: string) =>
    `4949bc0108000000030001bc01001000000032000000${sizes}0000000024c3dd6f034efe4bb1853d77768dc9${format}`;
  for (const [h, pixelType, expected] of [
    [jxr("80bc0300010000008002000081bc040001000000e0010000", "08"), 0, [640, 480]],
    [jxr("80bc0400010000007011010081bc04000100000003000000", "0d"), 3, [70000, 3]],
    [jxr("80bc0300010000000500000081bc04000100000005000000", "0b"), 0, null],
    [jxr("80bc0300010000000000000081bc04000100000005000000", "08"), 0, null],
    [jxr("80bc0100010000000500000081bc04000100000005000000", "08"), 0, null],
  ] as const) {
    assert.deepEqual(await jpegxrSize(head(h), pixelType), expected, h);
  }
});

test("GUID text", () => {
  assert.equal(guid(Uint8Array.from({ length: 16 }, (_, i) => i)), "03020100-0504-0706-0809-0a0b0c0d0e0f");
  assert.equal(guid(fromHex("ffeeddccbbaa99887766554433221100")), "ccddeeff-aabb-8899-7766-554433221100");
});
