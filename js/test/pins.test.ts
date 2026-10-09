// The virtualizers' pins and range checksums (spec/virtualize.md §1.2, §1.4;
// spec/archive.md §5.2, §6.1): the twin of python/tests/test_virtualize_pins.py.

import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { directoryStore } from "../conformance/directory_store.ts";
import { crc32c } from "../src/deflate.ts";
import { EtagLog } from "../src/http.ts";
import { virtualizeImage, virtualizeStore } from "../src/virtualize/index.ts";
import { blockReader } from "../src/virtualize/common.ts";

const FIXTURES = new URL("../../fixtures/", import.meta.url);
const read = (rel: string) => new Uint8Array(fs.readFileSync(new URL(rel, FIXTURES)));

async function image(rel: string, etag: string | undefined, checksums: boolean) {
  const bytes = read(rel);
  const reader = async (o: number, n: number) => bytes.subarray(o, o + n);
  const v = await virtualizeImage(`https://data.test/${rel}`, blockReader(reader, bytes.length), bytes.length, reader,
    { etag: () => etag, checksums });
  return { v, bytes };
}

test("pins sources and checksums ranges", async () => {
  const cases: [string, string | undefined, boolean][] = [
    ["tiff/tczyx_uint16_deflate.ome.tif", undefined, false],
    ["tiff/tczyx_uint16_deflate.ome.tif", '"fixed"', true],
    ["tiff/jpeg_ycbcr.tif", '"fixed"', true], // has data sources (shared JPEG tables)
  ];
  for (const [rel, etag, checksums] of cases) {
    const { v, bytes } = await image(rel, etag, checksums);
    assert.deepEqual(v.sources[0], { url: `https://data.test/${rel}`, size: BigInt(bytes.length), ...(etag ? { etag } : {}) });
    for (const e of v.entries) {
      if (!("ranges" in e)) continue;
      for (const r of e.ranges) {
        if ("data" in r) continue;
        const want = checksums && r.source === 0
          ? crc32c(bytes.subarray(Number(r.offset), Number(r.offset + r.length)))
          : undefined;
        assert.equal(r.crc32c, want, `${rel} ${e.key}`);
      }
    }
  }
  // A store input: every source pins its object's size.
  const dir = new URL("zarr2/zarr2_dtypes/", FIXTURES);
  for (const checksums of [false, true]) {
    const v = await virtualizeStore(directoryStore(fileURLToPath(dir), "https://data.test/s/"), { checksums });
    assert.ok(v.sources.length > 0);
    for (const s of v.sources) {
      const body = new Uint8Array(fs.readFileSync(new URL(s.url!.slice("https://data.test/s/".length), dir)));
      assert.equal(s.size, BigInt(body.length));
    }
    for (const e of v.entries) {
      if (!("ranges" in e)) continue;
      const [r] = e.ranges as { source: number; crc32c?: number }[];
      const body = new Uint8Array(fs.readFileSync(new URL(v.sources[r.source].url!.slice("https://data.test/s/".length), dir)));
      assert.equal(r.crc32c, checksums ? crc32c(body) : undefined, e.key);
    }
  }
});

test("an ETag pin needs the same strong ETag on every response", () => {
  const cases: [(string | null)[], string | undefined][] = [
    [['"a"', '"a"'], '"a"'],
    [['"a"', null], undefined], // some responses have none
    [["W/\"a\""], undefined], // weak
    [[null], undefined],
  ];
  for (const [seen, want] of cases) {
    const log = new EtagLog();
    for (const e of seen) log.saw(e);
    assert.equal(log.pin(), want, JSON.stringify(seen));
  }
});

test("fails when the ETag changes while reading", () => {
  const log = new EtagLog();
  log.saw('"v1"');
  assert.throws(() => log.saw('"v2"'), /changed while it was read/);
});
