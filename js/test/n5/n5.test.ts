import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { directoryStore } from "../../conformance/directory_store.ts";
import { virtualizeStore } from "../../src/virtualize/index.ts";
import { N5Error } from "../../src/virtualize/n5/virtualize.ts";
import { StoreError } from "../../src/virtualize/store.ts";

const FIXTURES = fileURLToPath(new URL("../../../fixtures/n5/", import.meta.url));
const URL_OF = (name: string) => `https://data.test/n5/${name}/`;

async function virtualize(name: string) {
  return virtualizeStore(directoryStore(FIXTURES + name, URL_OF(name)));
}

function doc(v: Awaited<ReturnType<typeof virtualize>>, key: string) {
  const e = v.entries.find((x) => x.key === key);
  assert.ok(e && "bytes" in e, key);
  return JSON.parse(new TextDecoder().decode(e.bytes as Uint8Array));
}

test("virtualizes the synthetic N5 stores", async () => {
  const cases: [string, object][] = [
    ["n5_compressions", { arrays: 8, groups: 1, images: [], chunks: 10 }],
    ["n5_root_dataset", { arrays: 1, groups: 0, chunks: 1 }],
    ["n5_hierarchy", { arrays: 3, groups: 6, chunks: 3, emptyChunks: 1 }],
    ["n5_cosem", { images: [{ path: "em/fibsem-uint8", convention: "cosem" }] }],
    ["n5_viewer_scales", { images: [{ path: "setup0/timepoint0", convention: "n5-viewer" }] }],
    ["n5_viewer_downsampling", { images: [{ path: "g", convention: "n5-viewer" }] }],
    ["n5_multiscales_unrecognized", { images: [], arrays: 4 }],
    ["n5_shared_levels", { images: [{ path: "a", convention: "cosem" }] }],
    ["n5_source_metadata", { arrays: 7, otherObjects: 0 }],
    ["n5_objects", { arrays: 1, chunks: 1, otherObjects: 4 }],
  ];
  for (const [name, expected] of cases) {
    const v = await virtualize(name);
    assert.equal(v.format, "n5");
    assert.deepEqual({ ...v.summary, ...expected }, v.summary, name);
    // One url source per chunk entry, in key order, each referencing the whole object.
    const refs = v.entries.filter((e) => "ranges" in e);
    assert.equal(v.sources.length, refs.length, name);
    for (const [i, e] of refs.entries()) {
      assert.ok("ranges" in e);
      const origin = e.key.replace(/^vzip_source\/objects\//, "");
      assert.equal(v.sources[i].url, URL_OF(name) + origin.replaceAll(" ", "%20").replaceAll("é", "%C3%A9"));
      assert.deepEqual(e.ranges, [{ source: i, offset: 0n, length: (e.ranges[0] as { length: bigint }).length }]);
    }
  }
  // The array: dimensions not reversed, n5_default with the full transpose and big-endian bytes.
  const c = await virtualize("n5_compressions");
  const zlib = doc(c, "zlib_f64/zarr.json");
  assert.deepEqual([zlib.shape, zlib.chunk_grid.configuration.chunk_shape], [[6, 6], [6, 6]]);
  assert.deepEqual(zlib.codecs, [{ name: "n5_default", configuration: { codecs: [
    { name: "transpose", configuration: { order: [1, 0] } },
    { name: "bytes", configuration: { endian: "big" } },
    { name: "zlib", configuration: { level: 6 } },
  ] } }]);
  assert.deepEqual(zlib.chunk_key_encoding, { name: "v2", configuration: { separator: "/" } });
  assert.deepEqual(doc(c, "blosc_u8/zarr.json").codecs[0].configuration.codecs.slice(1), [
    { name: "bytes" },
    { name: "blosc", configuration: { cname: "lz4", clevel: 5, shuffle: "shuffle", typesize: 1, blocksize: 0 } },
  ]);
  // COSEM: the transform's C-order values reversed onto the dimension order; levels get dimension names.
  const cosem = await virtualize("n5_cosem");
  const ms = doc(cosem, "em/fibsem-uint8/zarr.json").attributes.ome.multiscales[0];
  assert.deepEqual(ms.datasets[1].coordinateTransformations, [
    { type: "scale", scale: [8, 8, 10.48] }, { type: "translation", translation: [2, 2, 2.62] },
  ]);
  assert.deepEqual(doc(cosem, "em/fibsem-uint8/s2/zarr.json").dimension_names, ["x", "y", "z"]);
  // Implicit groups, and nodes inside datasets that are not nodes.
  // Block keys follow the grid in N5 dimension order.
  assert.deepEqual(c.entries.filter((e) => "ranges" in e && /^(raw_u16|gzip_i32_padded)\//.test(e.key)).map((e) => e.key),
    ["gzip_i32_padded/0/0/0", "gzip_i32_padded/1/0/0", "raw_u16/0/0", "raw_u16/0/1"]);
  const h = await virtualize("n5_hierarchy");
  assert.deepEqual(doc(h, "a/zarr.json"), { zarr_format: 3, node_type: "group", attributes: {} });
  // The missing block (1/0) and the empty one (2/0) have no entry.
  assert.deepEqual(h.entries.filter((e) => "ranges" in e && e.key.startsWith("a/b/sparse/")).map((e) => e.key),
    ["a/b/sparse/0/0"]);
  // The empty block's key is listed with the empty objects (§3.1, §6).
  assert.deepEqual(doc(h, "vzip_source/zarr.json").attributes.vzip_virtualized, { n5: { empty: ["a/b/sparse/2/0"] } });
  assert.ok(!h.entries.some((e) => e.key === "a/b/sparse/0/zarr.json" || e.key.startsWith("docs/")));
  // Source metadata (spec/virtualize/n5.md §5): the attributes and the layout members the Zarr
  // metadata does not reproduce; the levels are the codecs'; integers beyond 2^53 keep every digit.
  assert.deepEqual(doc(h, "zarr.json").attributes.vzip_virtualized.n5,
    { attributes: { description: "groups" }, metadata: { n5: "4.0.0" } });
  const s = await virtualize("n5_source_metadata");
  const root = s.entries.find((e) => e.key === "zarr.json")!;
  assert.ok(new TextDecoder().decode((root as { bytes: Uint8Array }).bytes)
    .includes('"id":18446744073709551615,"neg":-9007199254740993'));
  const n5Of = (k: string) => doc(s, k).attributes.vzip_virtualized?.n5;
  assert.deepEqual(n5Of("zstd_extra/zarr.json"), { metadata: { compression: { nbWorkers: 2 } } });
  assert.deepEqual(n5Of("both/zarr.json"), { metadata: { n5: "4.0.0", compressionType: "gzip" } });
  assert.deepEqual(doc(s, "zstd_extra/zarr.json").codecs[0].configuration.codecs[2],
    { name: "zstd", configuration: { level: -5, checksum: false } });
  assert.deepEqual(doc(s, "gzip_no_level/zarr.json").codecs[0].configuration.codecs[2],
    { name: "gzip", configuration: { level: 6 } });
  assert.deepEqual(doc(s, "layout_only/zarr.json").attributes, {});
  // Other objects, a dataset nested in a dataset included, are kept whole (spec/virtualize/n5.md §6).
  const o = await virtualize("n5_objects");
  assert.deepEqual(o.entries.filter((e) => "ranges" in e).map((e) => e.key), ["a/0", "vzip_source/objects/README",
    "vzip_source/objects/a/labels/0", "vzip_source/objects/a/labels/attributes.json", "vzip_source/objects/g/notes.txt"]);
  // The empty g/empty is listed in vzip_source's source metadata.
  assert.deepEqual(doc(o, "vzip_source/zarr.json").attributes.vzip_virtualized, { n5: { empty: ["g/empty"] } });
  // An object named zarr.json is escaped with ~, one to one (spec/virtualize/n5.md §6).
  const n = await virtualize("n5_node_names");
  const kept = n.entries.filter((e) => e.key.startsWith("vzip_source/objects/")).map((e) => e.key);
  assert.deepEqual(kept, ["vzip_source/objects/a/zarr.json~", "vzip_source/objects/g/zarr.json~~", "vzip_source/objects/zarr.json~"]);
  assert.deepEqual(n.sources.slice(1).map((x) => x.url), ["a/zarr.json", "g/zarr.json~", "zarr.json"].map((k) => URL_OF("n5_node_names") + k));
  assert.deepEqual(doc(n, "vzip_source/zarr.json").attributes.vzip_virtualized, { n5: { empty: ["e"] } });
});

for (const [name, message] of [
  ["n5_reject_datatype_string", /dataType "string"/],
  ["n5_reject_compression_lz4", /compression "lz4"/],
  ["n5_reject_compression_jpeg", /compression "jpeg"/],
  ["n5_reject_blosc_cname", /cname "lz5"/],
  ["n5_reject_gzip_usezlib_string", /useZlib/],
  ["n5_reject_dimensions_fraction", /dimensions \[4,2.5\]/],
  ["n5_reject_blocksize_mismatch", /blockSize \[2\]/],
  ["n5_reject_no_compression", /no compression/],
  ["n5_reject_gzip_level", /gzip level 10/],
  ["n5_reject_zstd_level", /zstd level 2.5/],
  ["n5_reject_objects_collision", /the node vzip_source is where/],
] as const) {
  test(`rejects ${name}`, async () => {
    await assert.rejects(virtualize(name), (e) => e instanceof N5Error && message.test(e.message));
  });
}

for (const name of ["n5_reject_json_nan", "n5_reject_json_bom", "n5_reject_json_not_utf8", "n5_reject_json_too_deep",
  "n5_reject_reserved_key", "n5_reject_no_root_attributes"]) {
  test(`rejects ${name}`, async () => {
    await assert.rejects(virtualize(name), StoreError);
  });
}
