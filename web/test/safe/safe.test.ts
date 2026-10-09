import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { directoryStore } from "../../conformance/directory_store.ts";
import { blockReader } from "../../src/virtualize/common.ts";
import { virtualizeImage, virtualizeStore } from "../../src/virtualize/index.ts";
import { chunkParts, emptyPackets, grid, readBand } from "../../src/virtualize/safe/jp2.ts";
import { jsonSize, MULTISCALES, typed, wkt2 } from "../../src/virtualize/safe/metadata.ts";
import { folders } from "../../src/virtualize/safe/product.ts";
import { SafeError } from "../../src/virtualize/safe/virtualize.ts";
import { StoreError } from "../../src/virtualize/store.ts";

const FIXTURES = fileURLToPath(new URL("../fixtures/safe/", import.meta.url));
const utf8 = new TextDecoder();

async function virtualize(name: string) {
  const path = FIXTURES + name;
  if (fs.statSync(path).isDirectory()) return virtualizeStore(directoryStore(path, `https://data.test/safe/${name}/`));
  const bytes = new Uint8Array(fs.readFileSync(path));
  return virtualizeImage(`https://data.test/safe/${name}`, blockReader(async (o, n) => bytes.subarray(o, o + n), bytes.length),
    bytes.length);
}

type V = Awaited<ReturnType<typeof virtualize>>;
function doc(v: V, key: string) {
  const e = v.entries.find((x) => x.key === key);
  assert.ok(e && "bytes" in e, key);
  return JSON.parse(utf8.decode(e.bytes as Uint8Array));
}

const J2K = [{ name: "imagecodecs_jpeg2k" }];
const RGB = [{ name: "transpose", configuration: { order: [1, 2, 0] } }, { name: "imagecodecs_jpeg2k" }];

test("virtualizes the synthetic SAFE products", async () => {
  const cases: [string, object, [string, number[], string, number[], object[]][]][] = [
    ["safe_l1c", {
      level: "L1C", form: "store", groups: 3, bands: 14, chunks: 215, edgeChunks: 92, dataSources: 12, objects: 52,
      folderMarkers: 10, xmlText: 12, xmlArrays: 5, otherObjects: 10, emptyObjects: 1, tileParts: 215,
    }, [
      ["r10m/B02", [110, 110], "uint16", [32, 32], J2K],
      ["r10m/TCI", [3, 110, 110], "uint8", [3, 16, 16], RGB],
      ["r60m/B10", [19, 19], "uint16", [6, 6], J2K],
    ]],
    ["safe_l1c.SAFE.zip", { level: "L1C", form: "zip", bands: 14, chunks: 215, objects: 42, xmlText: 12, xmlArrays: 5 }, []],
    ["safe_l2a", { level: "L2A", groups: 3, bands: 36, chunks: 461, edgeChunks: 218, dataSources: 27, xmlArrays: 6 }, [
      ["r20m/SCL", [55, 55], "uint8", [20, 20], J2K],
      ["r20m/TCI", [3, 55, 55], "uint8", [3, 40, 40], RGB],
    ]],
    ["safe_l2a_deflated_xml.SAFE.zip", { form: "zip", bands: 36, chunks: 461, xmlText: 12, xmlArrays: 6 }, []],
    ["safe_l1c_pb0207", { bands: 4, otherObjects: 12 }, []],
    ["safe_l2a_pb0212", { bands: 22, groups: 3 }, []],
    ["safe_latin1_xml", { bands: 1, xmlText: 4, xmlArrays: 0 }, []],
    ["safe_metadata_gaps", { bands: 4, groups: 2 }, []],
    ["safe_big_header", { bands: 2, chunks: 25, dataSources: 6 }, []],
    ["safe_zip64.SAFE.zip", { form: "zip", bands: 2, chunks: 25 }, []],
    // Three granules (processing baseline 02.07), and an empty directory.
    ["safe_l2a_pb0207", { level: "L2A", bands: 22, groups: 3, emptyDirs: 1, otherObjects: 11 }, [
      ["r60m/B01", [19, 19], "uint16", [6, 6], J2K],
    ]],
    ["safe_l2a_pb0207.SAFE.zip", { form: "zip", bands: 22, emptyDirs: 1, otherObjects: 11 }, []],
    // The names of PSD 14.2, and image files outside IMG_DATA (the cloud probability an other object).
    ["safe_l2a_pb0206", { level: "L2A", bands: 22, emptyDirs: 0, otherObjects: 12 }, [
      ["r20m/SCL", [55, 55], "uint8", [20, 20], J2K],
    ]],
    ["safe_metadata_many_records", { level: "L2A", bands: 2 }, []],
  ];
  for (const [name, summary, arrays] of cases) {
    const v = await virtualize(name);
    assert.equal(v.format, "safe", name);
    assert.deepEqual({ ...v.summary, ...summary }, v.summary, name);
    for (const [path, shape, dtype, chunk, codecs] of arrays) {
      const d = doc(v, `${path}/zarr.json`);
      assert.deepEqual([d.shape, d.data_type, d.chunk_grid.configuration.chunk_shape, d.codecs], [shape, dtype, chunk, codecs], path);
    }
    const root = doc(v, "zarr.json").attributes;
    assert.equal("multiscales" in root, (v.summary as { level: string }).level === "L2A", name);
  }
  const l2a = await virtualize("safe_l2a");
  const root = doc(l2a, "zarr.json").attributes;
  assert.deepEqual(root.zarr_conventions[1], MULTISCALES);
  assert.deepEqual(root.multiscales.layout.map((l: { asset: string }) => l.asset), ["r10m", "r20m", "r60m"]);
  const g = doc(l2a, "r20m/zarr.json").attributes;
  assert.deepEqual(g["spatial:transform"], [20, 0, 499980, 0, -20, 5200020]);
  assert.equal(g["proj:wkt2"], wkt2("EPSG:32632"));
  const b04 = doc(l2a, "r10m/B04/zarr.json").attributes.vzip_virtualized.safe;
  assert.deepEqual([b04.bandId, b04.physicalBand, b04.BOA_ADD_OFFSET, b04.SOLAR_IRRADIANCE], [3, "B4", -1000,
    { value: 1512.79, unit: "W/m²/µm" }]);
  const s = doc(l2a, "vzip_source/zarr.json").attributes.vzip_virtualized.safe;
  assert.ok(jsonSize(s) <= 65536);
  // The names of PSD 14.2 give the same members as the later ones; more than 64 records are absent.
  const [old, now] = [await virtualize("safe_l2a_pb0206"), await virtualize("safe_l2a_pb0207")];
  for (const key of ["r20m/B05/zarr.json", "r20m/SCL/zarr.json"]) assert.deepEqual(doc(old, key), doc(now, key), key);
  assert.equal(doc(old, "r20m/SCL/zarr.json").attributes.vzip_virtualized.safe.Scene_Classification_List.length, 12);
  const many = await virtualize("safe_metadata_many_records");
  assert.equal(doc(many, "zarr.json").attributes.vzip_virtualized.safe.Special_Values, undefined);
  assert.equal(doc(many, "r60m/SCL/zarr.json").attributes.vzip_virtualized.safe.Scene_Classification_List, undefined);
});

test("the two forms give the same documents", async () => {
  const docs = (v: V) => new Map(v.entries.filter((e) => e.key.endsWith("zarr.json") && e.key !== "zarr.json")
    .map((e) => [e.key, utf8.decode((e as { bytes: Uint8Array }).bytes)]));
  for (const [d, z, n] of [["safe_l1c", "safe_l1c.SAFE.zip", 12], ["safe_l2a_pb0207", "safe_l2a_pb0207.SAFE.zip", 15]] as const) {
    const a = await virtualize(d), b = await virtualize(z);
    assert.deepEqual(docs(a), docs(b));
    // the zip file pins its size (VIRTUALIZE.md §1.2), each object its listed size (§1.4)
    assert.deepEqual(b.sources[0], { url: `https://data.test/safe/${z}`, size: BigInt(fs.statSync(new URL(`../fixtures/safe/${z}`, import.meta.url)).size) });
    assert.ok(a.sources.every((x) => x.url === undefined || x.size !== undefined));
    assert.deepEqual(a.sources.slice(-n), b.sources.slice(-n)); // the same data sources
  }
  const s = doc(await virtualize("safe_l2a_pb0207.SAFE.zip"), "vzip_source/zarr.json").attributes.vzip_virtualized.safe;
  assert.deepEqual(s.empty_dirs, ["AUX_DATA"]);
});

test("chunks are standalone codestreams with empty tiles at the edges", async () => {
  const data = new Uint8Array(fs.readFileSync(
    `${FIXTURES}safe_l1c/GRANULE/L1C_T32TNS_A035655_20240103T101328/IMG_DATA/T32TNS_20240103T101329_B01.jp2`));
  const [cs] = await readBand(async (o, n) => data.subarray(o, o + n), 0, data.length);
  const [head, , , , padded0] = chunkParts(cs, 0);
  assert.equal(padded0, false);
  const v = new DataView(head.buffer);
  assert.deepEqual([4, 6, 10, 14, 18, 22, 26, 30, 34].map((o) => (o === 4 ? v.getUint16(o + 2) : v.getUint32(o + 2))),
    [0, 6, 6, 0, 0, 6, 6, 0, 0]);
  const [, , , tail, padded] = chunkParts(cs, 15);
  assert.equal(padded, true);
  let o = 0, tiles = 0;
  while (tail[o] === 0xff && tail[o + 1] === 0x90) {
    o += new DataView(tail.buffer).getUint32(o + 6);
    tiles++;
  }
  assert.equal(tiles, 35);
  // The packet count against the PLT markers of every tile of the band file.
  const [nx] = grid(cs);
  for (const [t, [s]] of cs.tiles.entries()) {
    let q = s + 12, n = 0;
    while (!(data[q] === 0xff && data[q + 1] === 0x93)) {
      const dv = new DataView(data.buffer, data.byteOffset);
      const m = dv.getUint16(q), len = dv.getUint16(q + 2);
      if (m === 0xff58) for (let i = q + 5; i < q + 2 + len; i++) n += data[i] & 0x80 ? 0 : 1;
      q += 2 + len;
    }
    const x0 = (t % nx) * cs.tileW, y0 = Math.floor(t / nx) * cs.tileH;
    assert.equal(emptyPackets(cs, x0, Math.min(x0 + cs.tileW, cs.width), y0, Math.min(y0 + cs.tileH, cs.height)), n, `${t}`);
  }
});

test("source values", () => {
  const cases: [string, unknown][] = [["10", 10], ["-1000", -1000], ["+7", 7], ["007", 7], ["-0", 0], ["1.5", 1.5],
    ["1e3", 1000], ["9007199254740991", 9007199254740991], ["9007199254740992", "9007199254740992"],
    ["-00012345678901234567890", "-12345678901234567890"], ["1e999", "Infinity"], [".5", ".5"], ["NODATA", "NODATA"]];
  for (const [t, v] of cases) assert.deepEqual(typed(t), v, t);
  assert.equal(wkt2("EPSG:32761"), undefined);
  assert.match(wkt2("EPSG:32756")!, /UTM zone 56S.*"Longitude of natural origin",153,.*"False northing",10000000,.*ID\["EPSG",32756\]\]$/);
});

const REJECTIONS = fs.readdirSync(FIXTURES).filter((n) => n.startsWith("safe_reject_")).sort();

test("every rejection fixture is a case", () => {
  assert.ok(REJECTIONS.length >= 50);
});

for (const name of REJECTIONS) {
  test(`rejects ${name}`, async () => {
    await assert.rejects(virtualize(name), (e) => e instanceof SafeError || (name.endsWith("no_manifest") && e instanceof StoreError));
  });
}

test("rejects an empty tile of more than 2^32 − 15 packets rather than wrap its Psot", () => {
  const cs = {
    n: 1 << 20, c0: 0, siz: new Uint8Array(43), rest: new Uint8Array(0), width: 3 * 2 ** 19, height: 2 ** 20,
    tileW: 2 ** 20, tileH: 2 ** 20, components: 1, precision: 8, layers: 65535, coding: [[0, [[0, 0]]]],
    tiles: [[0, 14], [14, 14]],
  } as unknown as Parameters<typeof chunkParts>[0];
  assert.ok(emptyPackets(cs, 3 * 2 ** 19, 2 ** 21, 0, 2 ** 20) > 2 ** 32);
  assert.throws(() => chunkParts(cs, 1), (e) => e instanceof SafeError && /more than 4096 bytes/.test(e.message));
});

test("folder markers and directory keys name the empty directories", () => {
  const cases: [string[], string[], string[], string[]][] = [
    [["a/x", "a_$folder$"], [], ["a_$folder$"], []],
    [["a/x", "a_$folder$", "b_$folder$", "c/d_$folder$", "c_$folder$"], [], ["a_$folder$", "b_$folder$", "c/d_$folder$", "c_$folder$"], ["b", "c/d"]],
    [["_$folder$", "x$folder$"], [], [], []],
    [["a/x"], ["a/", "e/", "e/f/", "g/"], [], ["e/f", "g"]],
    [["g/h_$folder$"], ["g/"], ["g/h_$folder$"], ["g/h"]],
  ];
  for (const [keys, dirs, markers, empty] of cases) {
    const [m, e] = folders(keys, dirs);
    assert.deepEqual([[...m].sort(), e], [markers, empty], keys.join());
  }
});
