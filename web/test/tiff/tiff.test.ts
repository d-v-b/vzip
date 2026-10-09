// The source metadata of the browser TIFF virtualizer (conventions/tiff/README.md §5):
// one group per IFD, pointer tags and the IFDs they lead to. Equivalence with the
// Python virtualizer is checked by conformance/virtualize/compare.py.

import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { virtualizeImage } from "../../conformance/reference/index.ts";

async function virtualizeFixture(name: string) {
  const bytes = new Uint8Array(fs.readFileSync(new URL(`../fixtures/tiff/${name}`, import.meta.url)));
  return virtualizeImage(`https://data.test/${name}`, async (o, n) => bytes.subarray(o, o + n), bytes.length);
}

test("every IFD has its own group, and pointer tags name the IFDs they lead to", async () => {
  const out = await virtualizeFixture("edge_pointers.tif");
  const docs = new Map<string, unknown>();
  for (const e of out.entries) {
    if ("bytes" in e && e.key.startsWith("vzip_source") && e.key.endsWith("zarr.json")) {
      docs.set(e.key, JSON.parse(new TextDecoder().decode(e.bytes)));
    }
  }
  const source = (path: string) => {
    const doc = docs.get(["vzip_source", ...(path ? [path] : []), "zarr.json"].join("/")) as
      { attributes: { vzip_virtualized?: { tiff: Record<string, unknown> } } };
    return doc.attributes.vzip_virtualized?.tiff ?? {};
  };
  assert.deepEqual(source(""), { ifd_count: 2 });
  assert.deepEqual(source("ifds/0").pointers, {
    330: { type: 4, count: 1, ifds: ["ifds/0/subifds/0"] },
    400: { type: 13, count: 1, ifds: ["ifds/0/ifd_400"] },
    34665: { type: 4, count: 1, ifds: ["ifds/0/exif"] },
    34853: { type: 4, count: 1, ifds: [null] },
    65000: { type: 13, count: 2, ifds: ["ifds/0/ifd_400", "ifds/0/ifd_65000/1"] },
  });
  assert.deepEqual(source("ifds/1").pointers, { 34665: { type: 4, count: 1, ifds: ["ifds/0/exif"] } });
  assert.deepEqual(source("ifds/0/subifds/0").pointers, { 330: { type: 4, count: 1, ifds: ["ifds/0/subifds/0/subifds/0"] } });
  assert.deepEqual(source("ifds/0/ifd_65000"), {});
  assert.ok(docs.has("vzip_source/ifds/1/jpeg_interchange/zarr.json"));
});

test("a pointer tag of more than 64 values is an int32 array of record numbers", async () => {
  const out = await virtualizeFixture("edge_pointer_list.tif");
  const entry = (key: string) => out.entries.find((e) => e.key === key);
  const doc = (path: string) => {
    const e = entry(`vzip_source/${path}/zarr.json`) as { bytes: Uint8Array } | undefined;
    return e && JSON.parse(new TextDecoder().decode(e.bytes)).attributes.vzip_virtualized?.tiff;
  };
  assert.deepEqual(doc("ifds/0").pointers, {
    400: { type: 4, count: 1, ifds: ["ifds/0/ifd_400"] }, 50001: { type: 13, count: 71 },
  });
  const chunk = (entry("vzip_source/ifds/0/50001/c/0") as { bytes: Uint8Array }).bytes;
  const numbers = Array.from(new Int32Array(chunk.buffer.slice(chunk.byteOffset, chunk.byteOffset + chunk.length)));
  assert.deepEqual(numbers, [...Array.from({ length: 66 }, (_, i) => i + 3), -1, -1, 3, -1, 1]);
  assert.equal(doc("ifds/0/ifd_50001/0").record, 3);
  assert.equal(doc("ifds/1").record, 1);
  assert.equal(doc("ifds/0/ifd_50001/66"), undefined); // an IFD of no entries is not recorded
  for (const name of ["jpeg_q_tables", "jpeg_dc_tables", "jpeg_ac_tables"]) {
    assert.ok(entry(`vzip_source/ifds/1/${name}/zarr.json`), name);
  }
});

test("rejects a subsampled YCbCr image that is not JPEG", async () => {
  await assert.rejects(virtualizeFixture("edge_reject_ycbcr_subsampled.tif"), /YCbCr with subsampling 2,2/);
});

test("rejects a YCbCr image without YCbCrSubsampling (TIFF 6.0's default is [2, 2])", async () => {
  await assert.rejects(virtualizeFixture("edge_reject_ycbcr_default.tif"), /YCbCr with subsampling 2,2/);
});

test("rejects TiffData that step through more than 4 × the planes + 1000", async () => {
  await assert.rejects(virtualizeFixture("edge_reject_tiffdata_steps.tif"), /cover more than 1008 planes/);
});

test("rejects FillOrder 2 for uncompressed data", async () => {
  await assert.rejects(virtualizeFixture("edge_reject_fill_order.tif"), /FillOrder 2/);
});

test("shared tables are kept once, a tiled IFD keeps its strips, and values as JSON have a budget", async () => {
  const docs = async (name: string) => {
    const out = await virtualizeFixture(name);
    const source = (path: string) => {
      const e = out.entries.find((x) => x.key === `vzip_source/${path}/zarr.json`) as { bytes: Uint8Array };
      return JSON.parse(new TextDecoder().decode(e.bytes)).attributes.vzip_virtualized?.tiff;
    };
    return { out, source };
  };
  const shared = await docs("edge_shared_tables.tif");
  assert.deepEqual(shared.source("ifds/2").same_as, {
    50001: "ifds/1/50001", jpeg_q_tables: "ifds/1/jpeg_q_tables", data: "ifds/1/data",
  });
  assert.ok(shared.out.entries.some((e) => e.key === "vzip_source/ifds/0/strips/zarr.json"));
  const budget = await docs("edge_budget.tif");
  const tags = budget.source("ifds/1").tags as Record<string, { value?: unknown }>;
  assert.deepEqual(Object.keys(tags).filter((t) => tags[t].value !== undefined), ["1000", "1001", "1003", "1004", "1005", "1006"]);
  assert.deepEqual(budget.source("ifds/3"), { tags: { 305: { type: 2, count: 12 } } });
});
