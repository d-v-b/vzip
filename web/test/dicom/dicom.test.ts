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

test("the source metadata keeps what the file holds", async () => {
  const source = async (name: string) => {
    const v = await virtualize(name);
    const docs = Object.fromEntries(v.entries.filter((e) => e.key.endsWith("zarr.json")).map((e) => [e.key, json(v, e.key)]));
    const node = docs["vzip_source/zarr.json"]?.attributes?.vzip_virtualized?.dicom ?? {};
    return { root: docs["zarr.json"].attributes.vzip_virtualized.dicom, node, docs };
  };
  // Duplicates, in file order; after Pixel Data, a sequence of undefined length, then
  // a duplicate that ends the elements.
  let { root, node, docs } = await source("dicom_duplicates.dcm");
  assert.deepEqual(root.dataset["00100010"].Value, [{ Alphabetic: "First^Name" }]);
  assert.deepEqual(root.dataset.duplicates.map((d: object) => Object.keys(d)[0]), ["00081140", "00100010"]);
  assert.equal(root.dataset.duplicates[0]["00081140"].Value.length, 2);
  assert.deepEqual(root.dataset["00082112"].Value[0].duplicates, [{ "00080100": { vr: "SH", Value: ["B2"] } }]);
  assert.deepEqual(root.dataset["7FE11010"].Value, [{ "00080100": { vr: "SH", Value: ["C3"] } }]);
  assert.deepEqual(docs["vzip_source/trailing/zarr.json"].dimension_names, ["byte"]);
  // A multibyte character set: its values unsplit, in the dataset and the items that inherit it.
  ({ root } = await source("dicom_charset_gbk.dcm"));
  assert.deepEqual(root.dataset["00080080"], { vr: "LO", InlineBinary: "gVwgSG9zcGl0YWwg" });
  assert.deepEqual(root.dataset["0040A043"].Value.map((i: any) => i["00080104"]), [
    { vr: "LO", InlineBinary: "gVw=" }, { vr: "LO", Value: ["\u00dcn\u00efcode"] },
  ]);
  // Large values; a sequence of 300 items, gathered; a DS value that is not a binary64 value.
  ({ root, node, docs } = await source("dicom_large_values.dcm"));
  assert.ok(root.preamble.startsWith("VlpJUCBQUkVBTUJMRS4u"));
  assert.deepEqual(root.dataset["30060050"], { vr: "DS", BulkDataURI: "vzip_source/dataset/30060050" });
  assert.deepEqual(root.dataset["00091001"], { vr: "US", InlineBinary: "AQID" });
  assert.deepEqual(root.dataset["00181050"].Value, ["9007199254740993"]);
  assert.deepEqual(Object.keys(root.dataset["00081115"]), ["vr", "Structures"]);
  assert.deepEqual(docs["vzip_source/dataset/00081115/items/structure/zarr.json"].shape, [300]);
  // Per-frame values: every value gathered by its path within the item, the items
  // keeping only their structure.
  ({ node, docs } = await source("dicom_per_frame_values.dcm"));
  const base = "vzip_source/dataset/52009230/items";
  // Items that differ only in their values: one structure.
  assert.equal(node.dataset["52009230"].Structures.length, 1);
  assert.deepEqual(node.dataset["52009230"].Structures[0]["00091002"], { vr: "US", Gathered: true });
  assert.deepEqual(docs[`${base}/structure/zarr.json`].shape, [4]);
  assert.equal(docs[`${base}/structure/zarr.json`].data_type, "int32");
  assert.deepEqual(docs[`${base}/00091002/value/zarr.json`].shape, [4, 70]);
  assert.deepEqual(docs[`${base}/00091002/value/zarr.json`].dimension_names, ["index", "value"]);
  assert.deepEqual(docs[`${base}/00091003/value/offsets/zarr.json`].shape, [5]);
  // Columns of typed values, of text, and families; chunks of several rows.
  ({ node, docs } = await source("dicom_per_frame_columns.dcm"));
  assert.equal(node.dataset["52009230"].Structures.length, 4); // items 2 and 4 share one
  const structure = (await virtualize("dicom_per_frame_columns.dcm")).entries.find((e) => e.key === `${base}/structure/c/0`);
  assert.ok(structure && "bytes" in structure);
  assert.deepEqual([...new Int32Array(structure.bytes.slice().buffer, 0, 5)], [0, 1, 2, 3, 2]);
  for (const [k, dataType, shape] of [
    ["00209113/0/00200032", "float64", [5, 3]], ["00209111/0/00209128", "int64", [5, 1]],
    ["00209111/0/00209057", "uint32", [5, 1]], ["00289145/0/00281050", "uint8", [5, 22]],
  ] as const) {
    const doc = docs[`${base}/${k}/value/zarr.json`];
    assert.deepEqual([doc.data_type, doc.shape, doc.chunk_grid.configuration.chunk_shape], [dataType, shape, shape], k);
  }
  assert.deepEqual(docs[`${base}/00289145/0/00281051/value/offsets/zarr.json`].shape, [6]);
  // Paths that nest across the items (a value in one, a sequence that holds one in
  // the other): each array is the leaf `value` of its path's group.
  ({ node, docs } = await source("dicom_per_frame_nested.dcm"));
  const [first, second] = node.dataset["52009230"].Structures;
  assert.deepEqual(first["00209111"], { vr: "SQ", Gathered: true }); // breaks a rule: its bytes
  assert.deepEqual(second["00209111"].Value[0]["00091001"], { vr: "OB", Gathered: true });
  assert.ok(`${base}/00209111/0/00091001/value/zarr.json` in docs && `${base}/00091001/0/00091002/value/zarr.json` in docs);
  const arrays = Object.keys(docs).filter((k) => docs[k].node_type === "array").map((k) => k.slice(0, -"/zarr.json".length));
  assert.deepEqual(arrays.filter((a) => arrays.some((b) => b.startsWith(`${a}/`))), []); // no array has children
  // Any sequence of more than 64 items is gathered, at any depth, of defined or
  // undefined length, and the sequences in its items are not; sparse columns take a
  // chunk per row with a value. One of 64 items is not gathered.
  ({ root, node, docs } = await source("dicom_gathered_sequences.dcm"));
  const instances = root.dataset["00081115"].Value[0]["0008114A"];
  assert.equal(instances.Structures.length, 2);
  assert.equal(instances.Structures[0]["0040A170"].Value.length, 66);
  const nested = "vzip_source/dataset/00081115/0/0008114A/items";
  for (const [k, dataType, shape, chunks] of [
    ["00081150/value", "uint8", [100, 26], [100, 26]], ["0040A170/65/00080100/value", "uint8", [100, 5], [1, 5]],
    ["00081155/value/offsets", "int64", [101], [101]],
  ] as const) {
    const doc = docs[`${nested}/${k}/zarr.json`];
    assert.deepEqual([doc.data_type, doc.shape, doc.chunk_grid.configuration.chunk_shape], [dataType, shape, chunks], k);
  }
  assert.deepEqual(docs["vzip_source/dataset/00082112/items/structure/zarr.json"].shape, [70]);
  assert.deepEqual(docs["vzip_source/dataset/00082112/items/00091001/value/zarr.json"].chunk_grid.configuration.chunk_shape, [1, 2]);
  assert.equal(root.dataset["0040A730"].Value.length, 64);
  assert.deepEqual(docs[`${base}/00291001/value/zarr.json`].chunk_grid.configuration.chunk_shape, [1, 1]);
  const sparse = (await virtualize("dicom_gathered_sequences.dcm")).entries.map((e) => e.key)
    .filter((k) => k.startsWith(`${base}/00291001/value/c/`)).sort();
  assert.deepEqual(sparse, [3, 40, 79].map((f) => `${base}/00291001/value/c/${f}/0`).sort());
  // An explicit UN value in big endian keeps its bytes in little endian, and says so.
  ({ root, node } = await source("dicom_un_bigendian_gathered.dcm"));
  assert.deepEqual(root.dataset["00091001"], { vr: "UN", InlineBinary: "AQACAA==", LittleEndian: true });
  assert.deepEqual(node.dataset["52009230"].Structures[1]["00209111"].Value[0]["00209057"],
    { vr: "UL", Gathered: true, LittleEndian: true });
  // Pixel data no frame holds.
  ({ root, docs } = await source("dicom_pixel_extra.dcm"));
  assert.deepEqual(docs["vzip_source/pixel_extra/zarr.json"].shape, [6]);
  ({ root, docs } = await source("dicom_eot_unreferenced.dcm"));
  assert.deepEqual(docs["vzip_source/pixel_unreferenced/offsets/zarr.json"].shape, [4]);
  assert.equal(root.pixel_unreferenced_headers, undefined);
  // A frame whose 8 bytes before its data are not its item header: every frame's are
  // kept, with the bytes between the frames.
  ({ root } = await source("dicom_eot_item_headers.dcm"));
  assert.equal(root.pixel_unreferenced_headers, true);
  const data = (await virtualize("dicom_eot_item_headers.dcm")).entries.find((e) => e.key === "vzip_source/pixel_unreferenced/data/c/0");
  assert.ok(data && "ranges" in data);
  assert.deepEqual(data.ranges.map((r) => ("length" in r ? Number(r.length) : -1)), [8, 8, 10]); // 2 bytes of padding
  // Numbers too long to convert.
  ({ root } = await source("dicom_long_numbers.dcm"));
  assert.deepEqual(root.dataset["00200012"].Value, [`-${"9".repeat(4999)}`]);
  assert.deepEqual(root.dataset["00201041"].Value, [10, `1e-${"1".repeat(5001)}`, 0]);
  // The root is kept small: a member of meta over 16 KiB, then, while meta and dataset
  // are over 64 KiB together, the largest left, the first by tag.
  ({ root, node } = await source("dicom_root_budget.dcm"));
  assert.deepEqual(Object.keys(node), ["meta", "dataset"]);
  assert.deepEqual(Object.keys(node.meta), ["duplicates"]);
  assert.equal(node.meta.duplicates.length, 20);
  assert.deepEqual(Object.keys(node.dataset), ["00111001"]); // of 15002 bytes, as 00111003 is
  assert.ok("00111003" in root.dataset && "00020013" in root.meta);
  assert.ok(JSON.stringify([root.meta, root.dataset]).length - 3 <= 2 ** 16);
  // Group lengths and offset tables that are not layout; a small sequence that breaks
  // a rule, inline.
  ({ root } = await source("dicom_layout_tags.dcm"));
  assert.ok(!("00020000" in root.meta) && !("00080000" in root.dataset));
  assert.deepEqual([root.dataset["00090000"].Value, root.dataset["00100000"].Value], [[12], [99]]);
  assert.deepEqual(root.dataset.duplicates, [{ "00180000": { vr: "UL", Value: [0] } }]);
  assert.deepEqual(root.dataset["7FE00001"], { vr: "OV", InlineBinary: "Fc1bBwAAAAA=" });
  assert.deepEqual(root.dataset["00209222"], { vr: "SQ", InlineBinary: "EAAQAFBOAgBBQg==" });
  ({ root } = await source("dicom_eot_duplicate.dcm"));
  assert.deepEqual(root.dataset.duplicates.map((d: object) => Object.keys(d)[0]), ["7FE00001", "7FE00002"]);
  // UN values in explicit VR big endian are in little endian.
  ({ root } = await source("dicom_un_bigendian.dcm"));
  assert.deepEqual(root.dataset["00181310"], { vr: "US", Value: [0, 256, 256, 0] });
  // A frame of several fragments, one empty, after a Basic Offset Table.
  ({ root, docs } = await source("dicom_jpeg_empty_fragment.dcm"));
  assert.equal(root.pixel_offset_table, true);
  assert.deepEqual(docs["vzip_source/pixel_fragments/zarr.json"].shape, [4, 2]);
});

// Past a bound, a sequence that would be gathered keeps its bytes.
for (const [tag, what, length] of [
  ["52009230", "over 1024 paths", 8 + 12 + 1025 * 10],
  ["00081250", "structures over 64 KiB", 65 * 8 + 3000 * 8],
  ["00081140", "arrays that cost over 16 times its bytes", 65 * 8 + 64 * (10 + 12)],
] as const) {
  test(`a gathered sequence of ${what} keeps its bytes`, async () => {
    const v = await virtualize("dicom_gathered_bounds.dcm");
    const root = json(v, "zarr.json").attributes.vzip_virtualized.dicom;
    const node = json(v, "vzip_source/zarr.json").attributes.vzip_virtualized.dicom;
    const attribute = { ...root.dataset, ...node.dataset }[tag];
    assert.deepEqual(attribute, { vr: "SQ", BulkDataURI: `vzip_source/dataset/${tag}` });
    assert.deepEqual(json(v, `vzip_source/dataset/${tag}/zarr.json`).shape, [length]);
    assert.ok(!v.entries.some((e) => e.key.startsWith(`vzip_source/dataset/${tag}/items/`)));
  });
}

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
  ["dicom_reject_jpeg_soi.dcm", /does not start with a JPEG SOI marker \(FF D8\)/],
  ["dicom_reject_pixel_past_eof.dcm", /104 bytes run past the end of the 514-byte file/],
  ["dicom_reject_eot_overlap.dcm", /Extended Offset Table's frames overlap/],
] as const) {
  test(`rejects ${name}`, async () => {
    await assert.rejects(virtualize(name), (e) => e instanceof DicomError && message.test(e.message));
  });
}
