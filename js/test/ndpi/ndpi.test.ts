// The NDPI JPEG header parser of the browser virtualizer (spec/virtualize/ndpi/profile.md §4),
// and the data sources that hold the header.
// Whole files are checked against tifffile by ndpi/verify.py.

import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { virtualizeImage } from "../../src/virtualize/index.ts";
import { jpegHeader } from "../../src/virtualize/ndpi/virtualize.ts";

/** SOI, SOF0 for a 16 × 32 image with the given sampling factors, DRI 4, SOS. */
function header(factors: number[], sofMarker = 0xc0): Uint8Array {
  const sof = [8, 0, 16, 0, 32, factors.length, ...factors.flatMap((f, k) => [k, f, 0])];
  const sos = [factors.length, ...factors.flatMap((_, k) => [k, 0]), 0, 0x3f, 0];
  return Uint8Array.from([
    0xff, 0xd8,
    0xff, sofMarker, 0, 2 + sof.length, ...sof,
    0xff, 0xdd, 0, 4, 0, 4,
    0xff, 0xda, 0, 2 + sos.length, ...sos,
  ]);
}

test("the MCU size comes from the largest horizontal and vertical sampling factors", () => {
  for (const [factors, mcu] of [
    [[0x11, 0x11, 0x11], [8, 8]],
    [[0x21, 0x11, 0x11], [16, 8]],
    [[0x22, 0x11, 0x11], [16, 16]],
    [[0x12, 0x11, 0x11], [8, 16]],
  ] as const) {
    const [, , mw, mh, interval] = jpegHeader(header([...factors]));
    assert.deepEqual([mw, mh, interval], [...mcu, 4]);
  }
});

test("rejects a progressive JPEG", () => {
  assert.throws(() => jpegHeader(header([0x11], 0xc2)), /not baseline/);
});

test("the header around SOF0 is in data sources, numbered in order of first use", async () => {
  const bytes = new Uint8Array(fs.readFileSync(new URL("../../../fixtures/ndpi/ndpi_levels.ndpi", import.meta.url)));
  const out = await virtualizeImage("https://data.test/x.ndpi", async (o, n) => bytes.subarray(o, o + n), bytes.length);
  assert.equal(out.sources[0].url, "https://data.test/x.ndpi");
  const data = out.sources.slice(1).map((s) => s.data!);
  assert.ok(data.length >= 2 && data[0][0] === 0xff && data[0][1] === 0xd8);
  let next = 1;
  for (const e of out.entries) {
    if (!("ranges" in e) || e.ranges.length === 1) continue;
    const [before, sof, after] = e.ranges;
    assert.ok("data" in sof && sof.data[1] === 0xc0, e.key);
    for (const r of [before, after]) {
      assert.ok("source" in r && r.source >= 1 && r.offset === 0n, e.key);
      assert.equal(r.length, BigInt(data[r.source - 1].length), e.key);
      assert.ok(r.source <= next, e.key); // a new source takes the next number
      if (r.source === next) next++;
    }
  }
  assert.equal(next, data.length + 1);
  // Each McuStarts level's own SOF0, which the literal replaces, is in its IFD's source metadata.
  const group = (k: number) => {
    const e = out.entries.find((x) => x.key === `vzip_source/ifds/${k}/zarr.json`) as { bytes: Uint8Array };
    return JSON.parse(new TextDecoder().decode(e.bytes)).attributes.vzip_virtualized.ndpi;
  };
  for (const k of [0, 1]) assert.equal(Buffer.from(group(k).sof0, "base64").subarray(0, 2).toString("hex"), "ffc0");
  assert.equal(group(2).sof0, undefined);
});

for (const [name, message] of [
  ["edge_reject_ndpi_wide_interval.ndpi", /65536 pixels wide/],
] as const) {
  test(`rejects ${name}`, async () => {
    const bytes = new Uint8Array(fs.readFileSync(new URL(`../../../fixtures/ndpi/${name}`, import.meta.url)));
    await assert.rejects(
      virtualizeImage("https://data.test/x.ndpi", async (o, n) => bytes.subarray(o, o + n), bytes.length), message);
  });
}

async function virtualizeFixture(name: string) {
  const bytes = new Uint8Array(fs.readFileSync(new URL(`../../../fixtures/ndpi/${name}`, import.meta.url)));
  return virtualizeImage(`https://data.test/${name}`, async (o, n) => bytes.subarray(o, o + n), bytes.length);
}

test("rejects a strip shorter than a JPEG stream's SOI and EOI", async () => {
  await assert.rejects(virtualizeFixture("edge_reject_ndpi_short_strip.ndpi"), /3 bytes is shorter than 4/);
});

test("the values of an IFD that is not a level are not read", async () => {
  const out = await virtualizeFixture("ndpi_unused_values.ndpi");
  assert.ok(out.entries.some((e) => e.key === "vzip_source/ifds/1/data/zarr.json"));
});
