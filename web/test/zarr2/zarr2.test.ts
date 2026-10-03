import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { directoryStore } from "../../conformance/directory_store.ts";
import { virtualizeStore } from "../../src/virtualize/index.ts";
import { StoreError } from "../../src/virtualize/store.ts";
import { Zarr2Error } from "../../src/virtualize/zarr2/virtualize.ts";

const FIXTURES = fileURLToPath(new URL("../fixtures/zarr2/", import.meta.url));

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
    ["zarr2_compressors", { arrays: 10, groups: 1 }],
    ["zarr2_dtypes", { arrays: 14 }],
    ["zarr2_fill_values", { emptyChunks: 1, chunks: 20 }],
    ["zarr2_scalar_root", { arrays: 1, groups: 0, chunks: 1 }],
    ["zarr2_hierarchy", { arrays: 5, groups: 6 }],
    ["zarr2_ome_attrs", { groups: 8 }],
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
  const f = await virtualize("zarr2_fill_values");
  assert.deepEqual(["nan", "inf", "neg_inf", "null_int", "null_bool"].map((p) => doc(f, `${p}/zarr.json`).fill_value),
    ["NaN", "Infinity", "-Infinity", 0, false]);
  const h = await virtualize("zarr2_hierarchy");
  assert.deepEqual(h.entries.filter((e) => "ranges" in e).map((e) => e.key), [
    "a/b/zero_d/0", "a/c/0/0", "a/c/0/1", "a/c/1/0", "a/c/1/1", "g/h/0", "g/h/1", "sp ace/é/x y/0.0", "sp ace/é/x y/1.0",
  ]);
  // Attributes are copied unchanged, OME-NGFF 0.4 ones included (the root does not declare 0.4, §1.4).
  const o = await virtualize("zarr2_ome_attrs");
  assert.equal(o.format, "zarr2");
  for (const path of ["0", "0/labels/cells", "has_ome"]) {
    const zattrs = JSON.parse(fs.readFileSync(`${FIXTURES}zarr2_ome_attrs/${path}/.zattrs`, "utf8"));
    assert.deepEqual(doc(o, `${path}/zarr.json`).attributes, zattrs, path);
  }
  assert.ok(!("dimension_names" in doc(o, "0/1/zarr.json")));
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
