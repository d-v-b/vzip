// The source metadata node of the browser IMS virtualizer on the files of the
// review (spec/virtualize/ims.md §5); python/tests/test_virtualize_ims.py has the
// same cases.

import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { virtualizeImage } from "../../src/virtualize/index.ts";

const FIXTURES = new URL("../../../fixtures/ims/", import.meta.url);

/** The paths of the object table (spec/virtualize/ims.md §5.7). */
function objects(docs: Map<string, Uint8Array>): string[] {
  const parents = new Int32Array(docs.get("vzip_source/objects/parent/c/0")!.slice().buffer);
  const names = family(docs, "objects/name").map((b) => new TextDecoder().decode(b));
  const table: string[] = [];
  parents.forEach((p, i) => table.push(p < 0 ? "/" : `${table[p].replace(/\/$/, "")}/${names[i]}`));
  return table;
}

/** The members of a family of byte values (conventions §7) copied under vzip_source. */
function family(docs: Map<string, Uint8Array>, path: string): Uint8Array[] {
  const doc = JSON.parse(new TextDecoder().decode(docs.get(`vzip_source/${path}/zarr.json`)!));
  if (doc.node_type === "array") return Array.from({ length: doc.shape[0] }, (_, i) => docs.get(`vzip_source/${path}/c/${i}/0`)!);
  const raw = docs.get(`vzip_source/${path}/offsets/c/0`)!;
  const starts = [...new BigInt64Array(raw.slice().buffer)].map(Number);
  const parts = [...docs.keys()].filter((k) => k.startsWith(`vzip_source/${path}/data/c/`))
    .sort((a, b) => Number(a.split("/").pop()) - Number(b.split("/").pop())).map((k) => docs.get(k)!);
  const data = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    data.set(p, at);
    at += p.length;
  }
  return starts.slice(0, -1).map((a, i) => data.subarray(a, starts[i + 1]));
}

async function documents(name: string): Promise<Map<string, Uint8Array>> {
  const bytes = new Uint8Array(fs.readFileSync(new URL(name, FIXTURES)));
  const v = await virtualizeImage(`https://data.test/${name}`, async (o, n) => bytes.subarray(o, o + n), bytes.length);
  // Chunks that reference the file are read from it.
  return new Map(v.entries.map((e) => [e.key, "bytes" in e ? e.bytes
    : concat(e.ranges.map((r) => ("data" in r ? r.data : bytes.subarray(Number(r.offset), Number(r.offset + r.length)))))]));
}

function concat(parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

test("keeps what the review found", async () => {
  for (const libver of ["earliest", "latest"]) {
    const docs = await documents(`ims_${libver}_review.ims`);
    const doc = (p: string) => JSON.parse(new TextDecoder().decode(docs.get(`vzip_source/hdf5/${p}zarr.json`)!));
    const source = (p: string) => doc(p).attributes.vzip_virtualized.ims;
    const x = source("X/"), a = x.attributes;
    assert.equal(a.imaris, "2.5 micrometer");
    assert.deepEqual(a.imaris_nul.value, ["a", "", "b"]);
    assert.deepEqual(a.u64.value, ["18446744073709551615", "9007199254740992", 3]);
    assert.deepEqual(a.null, { datatype: { class: "float", size: 4, order: "little" }, shape: null });
    const table = objects(docs);
    assert.deepEqual(a.refs.value, [table.indexOf("/DataSet"), table.indexOf("/X"), null]);
    assert.deepEqual(a.typed, { named: table.indexOf("/X/zT"), value: [0, 1] });
    assert.deepEqual(x.attribute_collisions.map((c: { name: unknown }) => c.name), ["nÃA", { latin1: "né" }]);
    assert.deepEqual(x.names, { "latén": { latin1: "latén" } });
    assert.deepEqual(x.collisions, [{ latin1: "café" }]);
    assert.deepEqual(x.links.elsewhere, { external: { file: "other.h5", path: "/data" } });
    assert.deepEqual(Object.keys(x.unsupported).sort(), ["__reserved", "external", "zarr.json"]);
    assert.deepEqual(x.unsupported.external.external, [{ file: "external.bin", offset: 0, size: 16 }]);
    assert.deepEqual(source("X/a_typed/"), { named: table.indexOf("/X/zT") });
    const refs = docs.get("vzip_source/hdf5/X/refs/c/0")!;
    assert.deepEqual([...new Int32Array(refs.slice().buffer)].map((i) => (i >= 0 ? table[i] : null)), ["/DataSet", "/X", "/Z", null]);
    assert.equal(doc("X/allempty/").node_type, "group");
    assert.equal(source("X/negzero/").fill, "gAAAAAAAAAA=");
    const held = source("DataSetInfo/CustomData/").attributes;
    assert.deepEqual(Object.keys(held).filter((k) => typeof held[k] === "object" && "array" in held[k]), ["Protocol 08", "Protocol 09", "Protocol 10", "Protocol 11"]);
  }
});

test("keeps integer fill values beyond 2^53 exactly", async () => {
  const docs = await documents("exact/ims_big_fill.ims");
  const text = (n: string) => new TextDecoder().decode(docs.get(`vzip_source/hdf5/${n}/zarr.json`)!);
  assert.match(text("bigfill"), /"fill_value":1152921504606846977,/);
  assert.match(text("u64fill"), /"fill_value":18446744073709551615,/);
  assert.match(text("negfill"), /"fill_value":-9223372036854775808,/);
});

test("keeps what the second review found", async () => {
  for (const libver of ["earliest", "latest"]) {
    const docs = await documents(`ims_${libver}_review2.ims`);
    const doc = (p: string) => JSON.parse(new TextDecoder().decode(docs.get(`vzip_source/hdf5/${p}zarr.json`)!));
    const source = (p: string) => doc(p).attributes.vzip_virtualized.ims;
    const text = (b: Uint8Array) => new TextDecoder().decode(b);
    const table = objects(docs);
    const deep = "/Deep" + [0, 1, 2, 3].map((i) => `/${"n".repeat(200)}${i}`).join("");
    assert.equal(source("").datatypes.T.datatype.class, "compound");
    const shared = Object.values(source("Shared/").attributes) as Record<string, unknown>[];
    assert.ok(shared.length === 120 && shared.every((v) => v.named === table.indexOf("/T") && !("datatype" in v)));
    const refs = source("Refs/").attributes;
    assert.deepEqual(refs.few.value, [table.indexOf(deep), null]);
    assert.deepEqual(refs.many, { datatype: { class: "reference", size: 8, kind: 0 }, array: "attributes/1" });
    assert.deepEqual(new Set(new Int32Array(docs.get("vzip_source/attributes/1/c/0")!.slice().buffer)), new Set([table.indexOf(deep)]));
    const hard = source("Hard/");
    assert.deepEqual(Object.keys(hard), ["links"]);
    assert.ok(Object.keys(hard.links).length === 100 && Object.values(hard.links).every((v) => (v as { hard: number }).hard === table.indexOf(deep)));
    const many = source("Many/");
    assert.ok(docs.get("vzip_source/hdf5/Many/zarr.json")!.length < 2 ** 16 + 4096 && many.spilled === "spilled/0");
    const spilled = family(docs, "spilled/0").map((b) => JSON.parse(text(b)));
    const held = { ...many.attributes, ...Object.assign({}, ...spilled.map((m) => m.attributes)) };
    assert.equal(Object.keys(held).length, 300);
    if (libver === "earliest") assert.deepEqual(source("Empty/").attributes.zz.value, ["", "", ""]);
    const index = (p: string) => table.indexOf(p);
    assert.deepEqual(source("X/withdim/").attributes.DIMENSION_LIST.value, [[index("/X/scale")]]);
    assert.deepEqual(source("X/scale/").attributes.REFERENCE_LIST.value, [{ dataset: index("/X/withdim"), dimension: 0 }]);
    const a = source("X/").attributes;
    assert.deepEqual(a.nested.value, [{ a: { i: 1, r: index("/X") }, b: [index("/X"), index("/X/scale")] }]);
    assert.deepEqual(a.points.value.selection.points, [[0, 1], [5, 9], [2, 2]]);
    assert.ok(a.nan.value[0] === "NaN" && "data" in a.nan_payload && "data" in a.negzero);
    assert.deepEqual(doc("X/cvlen/data/").shape, [3, 44]);
    assert.equal(text(family(docs, "hdf5/X/regions")[1]), `{"object":${index("/X/target")},"selection":{"select":"all"}}`);
    assert.equal(source("X/").unsupported.scaleoffset.reason, "unsupported HDF5 filters [6]");
    assert.deepEqual(source("X/").unsupported.virtual.virtual.map((m: { file: string }) => m.file), [".", "other.h5"]);
  }
});

test("keeps what the third review found", async () => {
  const name = "ims_latest_review3.ims";
  const docs = await documents(name);
  const doc = (p: string) => JSON.parse(new TextDecoder().decode(docs.get(`vzip_source/hdf5/${p}zarr.json`)!));
  const source = (p: string) => doc(p).attributes.vzip_virtualized.ims;
  const text = (b: Uint8Array) => new TextDecoder().decode(b);
  const table = objects(docs);
  const deep = "/Deep" + [0, 1, 2, 3].map((i) => `/${"n".repeat(200)}${i}`).join("");
  const d = table.indexOf(`${deep}/d`);
  const selection = '{"block":[2,1],"count":[1,3],"rank":2,"select":"hyperslab","start":[1,2],"stride":[1,2]}';
  assert.deepEqual(family(docs, "hdf5/R/regions").map(text), new Array(40).fill(`{"object":${d},"selection":${selection}}`));
  const r = source("R/");
  assert.equal(r.attributes.region.value.object, d);
  assert.deepEqual(Object.keys(r.unsupported).sort(), ["bigchunk", "over"]);
  const spill = source("Spill/");
  const spilled = family(docs, spill.spilled).map((b) => JSON.parse(text(b)));
  const held = Object.assign({}, ...[spill, ...spilled].map((m) => m.attributes ?? {}));
  assert.ok(!("zz" in spill.attributes));
  assert.deepEqual(held.zz.value, [d, null, table.indexOf(deep)]);
  assert.deepEqual(family(docs, "hdf5/R/names").map(text), ["a", "bb", "", "dddd"]);
  const keys = [...docs.keys()];
  const grid = Object.fromEntries(["tiny", "slabs", "rows", "gaps", "blocks"].map((n) => [n, [
    doc(`R/${n}/`).chunk_grid.configuration.chunk_shape, keys.filter((k) => k.startsWith(`vzip_source/hdf5/R/${n}/c/`)).length]]));
  assert.deepEqual(grid, { tiny: [[1000], 1], slabs: [[6, 4], 1], rows: [[1, 5], 3], gaps: [[1, 4], 3], blocks: [[2, 2], 4] });
  const bytes = new Uint8Array(fs.readFileSync(new URL(name, FIXTURES)));
  const v = await virtualizeImage("https://data.test/x", async (o, n) => bytes.subarray(o, o + n), bytes.length);
  const referenced = new Set(v.entries.filter((e) => "ranges" in e && e.ranges.every((p) => !("data" in p))).map((e) => e.key));
  for (const array of [r.attributes.header.array, held.managed.array, held.huge.array]) assert.ok(referenced.has(`vzip_source/${array}/c/0`), array);
  assert.deepEqual([...new Int16Array(docs.get(`vzip_source/${r.attributes.header.array}/c/0`)!.slice().reverse().buffer)].reverse(),
    Array.from({ length: 100 }, (_, i) => i));
  assert.deepEqual(source("DataSet/ResolutionLevel 0/TimePoint 0/Channel 0/").images.Data, { level: 0, t: 0, c: 0, shape: [2, 8, 8] });
  const root = async (n: string) => JSON.parse(text((await documents(`ims_latest_${n}.ims`)).get("zarr.json")!)).attributes.ome;
  const long = await root("long_names"), many = await root("many_channels");
  assert.ok(!("name" in long.multiscales[0]));
  assert.deepEqual(long.omero.channels.map((c: { label: string }) => c.label), ["Channel 0", "é".repeat(128)]);
  assert.ok(!("omero" in many) && many.multiscales[0].name === "y".repeat(256));
});

test("lists region references over the budget of selections", async () => {
  const r = JSON.parse(new TextDecoder().decode((await documents("ims_latest_review3.ims")).get("vzip_source/hdf5/R/zarr.json")!))
    .attributes.vzip_virtualized.ims;
  assert.equal(r.unsupported.over.reason, "over the budget of region selections");
  assert.equal(r.attributes.region.value.selection.select, "hyperslab");
});

test("lists references in a chunk too large to decode", async () => {
  const r = JSON.parse(new TextDecoder().decode((await documents("ims_latest_review3.ims")).get("vzip_source/hdf5/R/zarr.json")!))
    .attributes.vzip_virtualized.ims;
  assert.equal(r.unsupported.bigchunk.reason, "a chunk of more than 2^24 bytes to decode");
});

test("reads every chunk index", async () => {
  const docs = await documents("ims_latest_indexes.ims");
  const bytes = new Uint8Array(fs.readFileSync(new URL("ims_latest_indexes.ims", FIXTURES)));
  const v = await virtualizeImage("https://data.test/x", async (o, n) => bytes.subarray(o, o + n), bytes.length);
  const keys = v.entries.map((e) => e.key);
  const count = (name: string) => keys.filter((k) => k.startsWith(`vzip_source/hdf5/Indexes/${name}/c/`)).length;
  assert.deepEqual(
    Object.fromEntries(["ea", "ea_last", "ea_deflate", "bt2", "bt2_deflate", "implicit", "implicit_max", "farray_max"].map((n) => [n, count(n)])),
    { ea: 600, ea_last: 28, ea_deflate: 72, bt2: 255, bt2_deflate: 4, implicit: 1, implicit_max: 4, farray_max: 9 },
  );
  assert.ok(!("vzip_virtualized" in JSON.parse(new TextDecoder().decode(docs.get("vzip_source/hdf5/Indexes/zarr.json")!)).attributes));
});

test("walks a time-lapse whole", async () => {
  // 57 objects of the image's tree and 8 others: within 100,000 + 5 per Data dataset (spec/virtualize/ims.md §5.1).
  const docs = await documents("ims_latest_timelapse.ims");
  for (const [k, v] of docs) {
    if (!k.startsWith("vzip_source/hdf5") || !k.endsWith("zarr.json")) continue;
    const s = JSON.parse(new TextDecoder().decode(v)).attributes.vzip_virtualized?.ims ?? {};
    assert.ok(!("overflow" in s) && !("unsupported" in s), k);
  }
  const info = JSON.parse(new TextDecoder().decode(docs.get("vzip_source/hdf5/DataSetInfo/TimeInfo/zarr.json")!));
  assert.ok("TimePoint3" in info.attributes.vzip_virtualized.ims.attributes);
});

test("copies partial edge chunks stored unfiltered", async () => {
  const name = "ims_latest_raw_edges.ims";
  const docs = await documents(name);
  assert.ok(!("vzip_virtualized" in JSON.parse(new TextDecoder().decode(docs.get("vzip_source/hdf5/E/zarr.json")!)).attributes));
  const ints = (key: string) => [...new Int32Array(docs.get(key)!.slice().buffer)];
  assert.deepEqual([0, 1, 2].flatMap((i) => ints(`vzip_source/hdf5/E/fixed/c/${i}`)), [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 0, 0]);
  assert.deepEqual(ints("vzip_source/hdf5/E/single/c/0"), [0, 1, 2, 0]);
  assert.deepEqual(family(docs, "hdf5/E/names").map((b) => new TextDecoder().decode(b)), ["a", "bb", "", "dddd", "e"]);
  // Without a partial chunk, the chunks are referenced.
  const bytes = new Uint8Array(fs.readFileSync(new URL(name, FIXTURES)));
  const v = await virtualizeImage("https://data.test/x", async (o, n) => bytes.subarray(o, o + n), bytes.length);
  const referenced = new Set(v.entries.filter((e) => "ranges" in e && e.ranges.every((p) => !("data" in p))).map((e) => e.key));
  assert.ok(referenced.has("vzip_source/hdf5/E/whole/c/0") && !referenced.has("vzip_source/hdf5/E/fixed/c/0"));
});
