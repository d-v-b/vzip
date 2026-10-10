import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { directoryStore } from "../../conformance/directory_store.ts";
import { declare } from "../../src/virtualize/common.ts";
import { virtualizeStore } from "../../src/virtualize/index.ts";
import { StoreError } from "../../src/virtualize/store.ts";
import { Zarr2Error } from "../../src/virtualize/zarr2/virtualize.ts";

const FIXTURES = fileURLToPath(new URL("../../../fixtures/zarr2/", import.meta.url));

async function virtualize(name: string) {
  return virtualizeStore(directoryStore(FIXTURES + name, `https://data.test/zarr2/${name}/`));
}

function doc(v: Awaited<ReturnType<typeof virtualize>>, key: string) {
  const e = v.entries.find((x) => x.key === key);
  assert.ok(e && "bytes" in e, key);
  return JSON.parse(new TextDecoder().decode(e.bytes as Uint8Array));
}

test("virtualizes the synthetic Zarr v2 stores", async () => {
  const cases: [string, object][] = [
    ["zarr2_compressors", { arrays: 10, groups: 1, chunks: 12 }],
    ["zarr2_dtypes", { arrays: 14 }],
    ["zarr2_fill_values", { emptyChunks: 1, chunks: 10 }],
    ["zarr2_scalar_root", { arrays: 1, groups: 0, chunks: 1 }],
    ["zarr2_hierarchy", { arrays: 5, groups: 6, chunks: 5 }],
    ["zarr2_ome_attrs", { groups: 8 }],
    ["zarr2_source_metadata", { arrays: 10, otherObjects: 0 }],
    ["zarr2_objects", { chunks: 3, otherObjects: 6 }],
  ];
  for (const [name, expected] of cases) {
    const v = await virtualize(name);
    assert.equal(v.format, "zarr2");
    assert.deepEqual({ ...v.summary, ...expected }, v.summary, name);
  }
  const c = await virtualize("zarr2_compressors");
  assert.deepEqual(doc(c, "zlib_f/zarr.json").codecs, [
    { name: "transpose", configuration: { order: [2, 1, 0] } },
    { name: "bytes", configuration: { endian: "little" } },
    { name: "zlib", configuration: { level: 1 } },
  ]);
  assert.deepEqual(doc(c, "blosc_autoshuffle_i2/zarr.json").codecs[1],
    { name: "blosc", configuration: { cname: "lz4hc", clevel: 2, shuffle: "shuffle", typesize: 2, blocksize: 0 } });
  assert.deepEqual(doc(c, "gzip_nested/zarr.json").chunk_key_encoding, { name: "v2", configuration: { separator: "/" } });
  // Two chunks with a partial edge chunk, along the first axis in C order and along the second in F order.
  assert.deepEqual(c.entries.filter((e) => "ranges" in e && /^(raw_c|zstd_f_nested)\//.test(e.key)).map((e) => e.key),
    ["raw_c/0.0", "raw_c/1.0", "zstd_f_nested/0/0/0", "zstd_f_nested/0/1/0"]);
  const f = await virtualize("zarr2_fill_values");
  assert.deepEqual(["nan", "inf", "neg_inf", "null_int", "null_bool"].map((p) => doc(f, `${p}/zarr.json`).fill_value),
    ["NaN", "Infinity", "-Infinity", 0, false]);
  // The empty chunk's key is listed with the empty objects (§3.2, §5).
  assert.deepEqual(doc(f, "vzip_source/zarr.json").attributes.vzip_virtualized, { zarr2: { empty: ["nan/0.0"] } });
  const h = await virtualize("zarr2_hierarchy");
  assert.deepEqual(h.entries.filter((e) => "ranges" in e).map((e) => e.key), [
    "a/b/zero_d/0", "a/c/0/0", "a/c/1/0", "g/h/0", "sp ace/é/x y/0.0", "vzip_source/objects/.zmetadata", "vzip_source/objects/a/c/0.0",
    "vzip_source/objects/a/c/0/9", "vzip_source/objects/a/c/00/1", "vzip_source/objects/a/c/inside/.zgroup~",
    "vzip_source/objects/g/h/0.0", "vzip_source/objects/notes/README.md", "vzip_source/objects/orphan/.zattrs",
  ]);
  assert.equal(h.sources.at(-1)!.url, "https://data.test/zarr2/zarr2_hierarchy/orphan/.zattrs");
  // Attributes are copied unchanged under the convention (conventions §2), OME-NGFF 0.4 ones included
  // (the root does not declare 0.4, §1.4).
  const o = await virtualize("zarr2_ome_attrs");
  assert.equal(o.format, "zarr2");
  for (const path of ["0", "0/labels/cells", "has_ome"]) {
    const zattrs = JSON.parse(fs.readFileSync(`${FIXTURES}zarr2_ome_attrs/${path}/.zattrs`, "utf8"));
    assert.deepEqual(doc(o, `${path}/zarr.json`).attributes, declare({}, "zarr2", undefined, { attributes: zattrs }), path);
  }
  assert.ok(!("dimension_names" in doc(o, "0/1/zarr.json")));
  // Source metadata (spec/virtualize/zarr2.md §4), the levels the codecs carry, and -0.0 fills.
  const s = await virtualize("zarr2_source_metadata");
  const bytes = (k: string) => new TextDecoder().decode((s.entries.find((e) => e.key === k) as { bytes: Uint8Array }).bytes);
  assert.ok(bytes("zarr.json").includes('"id":18446744073709551615,"neg":-9007199254740993'));
  assert.ok(bytes("neg_zero_f4/zarr.json").includes('"fill_value":"0x80000000"'));
  assert.deepEqual(["neg_zero_f2", "neg_zero_f8", "big_fill_f8"].map((p) => doc(s, `${p}/zarr.json`).fill_value),
    ["0x8000", "0x8000000000000000", 18446744073709551616]);
  assert.deepEqual(doc(s, "zarr.json").attributes.vzip_virtualized.zarr2.metadata, { creator: { name: "a writer" } });
  assert.deepEqual(doc(s, "null_fill/zarr.json").attributes.vzip_virtualized, { zarr2: { metadata: { fill_value: null } } });
  assert.deepEqual(doc(s, "extra_members/zarr.json").attributes.vzip_virtualized.zarr2, {
    attributes: { _ARRAY_DIMENSIONS: ["x"] }, metadata: { custom: { x: 1 }, compressor: { nthreads: 2 } } });
  assert.deepEqual(["zlib_9", "zlib_no_level", "gzip_default", "zstd_checksum"].map((p) => doc(s, `${p}/zarr.json`).codecs[1]), [
    { name: "zlib", configuration: { level: 9 } }, { name: "zlib", configuration: { level: 1 } },
    { name: "gzip", configuration: { level: 6 } }, { name: "zstd", configuration: { level: -3, checksum: true } }]);
  // Other objects are kept whole under vzip_source/objects/ (spec/virtualize/zarr2.md §5).
  const ob = await virtualize("zarr2_objects");
  assert.deepEqual(ob.entries.filter((e) => "ranges" in e).map((e) => e.key), ["a/0", "a/1", "sub/b/0",
    "vzip_source/objects/.zmetadata", "vzip_source/objects/OME/METADATA.ome.xml", "vzip_source/objects/README.md", "vzip_source/objects/a/inner/.zgroup~",
    "vzip_source/objects/a/notes.txt", "vzip_source/objects/sub/.zattrs"]);
  assert.equal(ob.sources[4].url, "https://data.test/zarr2/zarr2_objects/OME/METADATA.ome.xml");
  // The keys of empty objects are vzip_source's source metadata.
  assert.equal(doc(ob, "zarr.json").attributes.vzip_virtualized.zarr2, undefined);
  assert.deepEqual(doc(ob, "vzip_source/zarr.json").attributes, declare({}, "zarr2", undefined, { empty: ["empty.txt"] }));
  // Escaping is one to one: one ~ more on each last segment that is a node document's name and ~s.
  const nn = await virtualize("zarr2_node_names");
  const others = nn.entries.flatMap((e, i) => (e.key.startsWith("vzip_source/objects/") ? [[e.key, nn.sources[i].url]] : []));
  const base = "https://data.test/zarr2/zarr2_node_names/";
  assert.deepEqual(others, [
    ["vzip_source/objects/a/inner/.zgroup~", "a/inner/.zgroup"], ["vzip_source/objects/a/zarr.json~", "a/zarr.json"],
    ["vzip_source/objects/g/zarr.json~", "g/zarr.json"], ["vzip_source/objects/x/.zarray~~~", "x/.zarray~~"],
    ["vzip_source/objects/x/zarr.jsonx", "x/zarr.jsonx"], ["vzip_source/objects/zarr.json~", "zarr.json"],
    ["vzip_source/objects/zarr.json~~", "zarr.json~"]].map(([k, u]) => [k, base + u]));
  // A float fill written as an integer beyond 2^53 - 1 keeps its digits in M (§4).
  const fi = await virtualize("zarr2_fill_float_int");
  const m = (p: string) => new TextDecoder().decode((fi.entries.find((e) => e.key === `${p}/zarr.json`) as { bytes: Uint8Array }).bytes);
  assert.ok(m("h").includes('"vzip_virtualized":{"zarr2":{"metadata":{"fill_value":9007199254740993}}}'));
  assert.ok(m("k").includes('"metadata":{"fill_value":-18446744073709551616}'));
  assert.ok(!m("s").includes("vzip_virtualized"));
});

for (const [name, message] of [
  ["zarr2_reject_filters", /filters/],
  ["zarr2_reject_compressor_lz4", /compressor "lz4"/],
  ["zarr2_reject_dtype_unicode", /dtype "<U4"/],
  ["zarr2_reject_dtype_complex", /dtype "<c8"/],
  ["zarr2_reject_order", /order "A"/],
  ["zarr2_reject_fill_out_of_range", /fill_value 300/],
  ["zarr2_reject_fill_uint64_max", /is not a uint64/],
  ["zarr2_reject_separator", /dimension_separator "-"/],
  ["zarr2_reject_zarray_and_zgroup", /both .zarray and .zgroup/],
  ["zarr2_reject_json_duplicate_last_wins", /order "K"/],
  ["zarr2_reject_zlib_level", /zlib level 10/],
  ["zarr2_reject_gzip_level", /gzip level "1"/],
  ["zarr2_reject_zstd_level", /zstd level 23/],
  ["zarr2_reject_zstd_checksum", /zstd checksum 1/],
  ["zarr2_reject_objects_collision", /the node vzip_source is where/],
] as const) {
  test(`rejects ${name}`, async () => {
    await assert.rejects(virtualize(name), (e) => e instanceof Zarr2Error && message.test(e.message));
  });
}

for (const name of ["zarr2_reject_json_nan_literal", "zarr2_reject_no_root_metadata"]) {
  test(`rejects ${name}`, async () => {
    await assert.rejects(virtualize(name), StoreError);
  });
}

test("keeps a root array's other objects without a vzip_source group", async () => {
  // An array has no children (spec/virtualize/zarr2.md §5; spec/virtualize/n5.md §6).
  const fixtures = fileURLToPath(new URL("../../../fixtures/", import.meta.url));
  for (const name of ["zarr2/zarr2_root_array_objects", "n5/n5_root_dataset_objects"]) {
    const v = await virtualizeStore(directoryStore(fixtures + name, `https://data.test/${name}/`));
    const keys = v.entries.map((e) => e.key);
    assert.ok(!keys.includes("vzip_source/zarr.json"), name);
    assert.ok(keys.includes("vzip_source/objects/README.md") || keys.includes("vzip_source/objects/README"), name);
  }
  // The keys of empty objects are the plain key vzip_source/empty.json, in UTF-8.
  const v = await virtualizeStore(directoryStore(fixtures + "zarr2/zarr2_root_array_empty", "https://data.test/e/"));
  const e = v.entries.find((x) => x.key === "vzip_source/empty.json") as { bytes: Uint8Array };
  assert.equal(new TextDecoder().decode(e.bytes), '{"empty":["empty","\u00e9mpty"]}');
  assert.ok(!v.entries.some((x) => x.key === "vzip_source/zarr.json"));
  // An empty chunk object of a root array is listed there too (spec/virtualize/zarr2.md §3.2,
  // spec/virtualize/n5.md §3.1).
  for (const name of ["zarr2/zarr2_root_array_empty_chunk", "n5/n5_root_dataset_empty_block"]) {
    const w = await virtualizeStore(directoryStore(fixtures + name, "https://data.test/e/"));
    assert.deepEqual(w.entries.filter((x) => "ranges" in x).map((x) => x.key), ["0"], name);
    const x = w.entries.find((y) => y.key === "vzip_source/empty.json") as { bytes: Uint8Array };
    assert.equal(new TextDecoder().decode(x.bytes), '{"empty":["1"]}', name);
  }
});
