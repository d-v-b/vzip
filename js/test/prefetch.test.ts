// Concurrent document reads of the store profiles (store.ts, prefetchDocuments), as
// python/tests/test_store_prefetch.py checks them for the Python reference: the same outcome as
// reading one document at a time, each document read once, and a bounded number of reads
// in flight.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { virtualizeStore } from "../src/virtualize/index.ts";
import { objectUrl, openHttpStore, Prefetch } from "../src/virtualize/store.ts";

const FIXTURES = path.join(import.meta.dirname, "../../fixtures");
const URL = "http://h/b/s/";
const LISTING = "http://h/b/?list-type=2&prefix=s%2F";
const utf8 = new TextEncoder();
const escape = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/** A fetch serving `objects` (key → bytes, or an HTTP status to answer with) at URL:
 * counting the reads of each key and the reads in flight. */
class FakeS3 {
  reads = new Map<string, number>();
  inflight = 0;
  most = 0;
  #byUrl: Map<string, string>;
  objects: Map<string, Uint8Array | number>;
  delay: number;
  short: Set<string>;
  constructor(objects: Map<string, Uint8Array | number>, delay = 0, short = new Set<string>()) {
    this.objects = objects;
    this.delay = delay;
    this.short = short;
    this.#byUrl = new Map([...objects.keys()].map((k) => [objectUrl(URL, k), k]));
  }
  fetch = async (url: string): Promise<Response> => {
    if (url === LISTING) {
      const contents = [...this.objects].sort(([a], [b]) => (a < b ? -1 : 1))
        .map(([k, v]) => `<Contents><Key>s/${escape(k)}</Key><Size>${typeof v === "number" ? 1 : v.length}</Size></Contents>`)
        .join("");
      return new Response(utf8.encode(`<?xml version="1.0"?><ListBucketResult><IsTruncated>false</IsTruncated>${contents}</ListBucketResult>`));
    }
    const key = this.#byUrl.get(url)!;
    this.reads.set(key, (this.reads.get(key) ?? 0) + 1);
    this.inflight++;
    this.most = Math.max(this.most, this.inflight);
    try {
      await new Promise((r) => setTimeout(r, this.delay));
      const v = this.objects.get(key)!;
      if (typeof v === "number") return new Response("refused", { status: v });
      return new Response((this.short.has(key) ? v.slice(0, -1) : v) as BodyInit, { status: 206 });
    } finally {
      this.inflight--;
    }
  };
}

function directory(root: string): Map<string, Uint8Array> {
  const out = new Map<string, Uint8Array>();
  for (const rel of fs.readdirSync(root, { recursive: true, encoding: "utf8" })) {
    const full = path.join(root, rel);
    if (fs.statSync(full).isFile()) out.set(rel.split(path.sep).join("/"), new Uint8Array(fs.readFileSync(full)));
  }
  return out;
}

/** ["ok", output], ["rejected" or "failed", error class, message]. */
async function outcome(s3: FakeS3, concurrency: number): Promise<unknown[]> {
  try {
    const store = await openHttpStore(URL, { fetch: s3.fetch, concurrency });
    return ["ok", await virtualizeStore(store)];
  } catch (e) {
    const err = e as Error;
    return [/Store(Read|Limit)Error/.test(err.constructor.name) ? "failed" : "rejected", err.constructor.name, err.message];
  }
}

const stores = ["n5", "zarr2", "ome-zarr"].flatMap((f) =>
  fs.readdirSync(path.join(FIXTURES, f)).map((n) => path.join(FIXTURES, f, n)).filter((p) => fs.statSync(p).isDirectory())
);
for (const root of stores) {
  test(`concurrent reads give the same outcome, each document read once: ${path.basename(root)}`, async () => {
    const objects = directory(root);
    const sequential = new FakeS3(objects);
    const concurrent = new FakeS3(objects);
    const expected = await outcome(sequential, 1);
    assert.deepEqual(await outcome(concurrent, 8), expected);
    assert.ok([...concurrent.reads.values()].every((n) => n === 1));
    if (expected[0] === "ok") assert.deepEqual(new Set(concurrent.reads.keys()), new Set(sequential.reads.keys()));
  });
}

const ZARRAY = utf8.encode(JSON.stringify({
  zarr_format: 2, shape: [4], chunks: [2], dtype: "|u1", order: "C", compressor: null, fill_value: 0, filters: null,
}));
const ZGROUP = utf8.encode('{"zarr_format": 2}');

test("reads in flight are bounded", async () => {
  const objects = new Map<string, Uint8Array>([[".zgroup", ZGROUP]]);
  for (let i = 0; i < 40; i++) {
    objects.set(`a${i}/.zarray`, ZARRAY);
    objects.set(`a${i}/.zattrs`, utf8.encode("{}"));
  }
  const s3 = new FakeS3(objects, 5);
  assert.equal((await outcome(s3, 4))[0], "ok");
  assert.equal(s3.most, 4);
  assert.equal(s3.reads.size, 81);
  assert.ok([...s3.reads.values()].every((n) => n === 1));
});

test("bytes held ahead are bounded", async () => {
  let held = 0;
  let most = 0;
  const keys = Array.from({ length: 30 }, (_, i) => `k${i}`);
  const p = new Prefetch(async () => {
    held += 10;
    most = Math.max(most, held);
    await new Promise((r) => setTimeout(r, 1));
    return new Uint8Array(10);
  }, keys.map((k) => [k, 10]), 8, 35);
  for (const k of keys) {
    await new Promise((r) => setTimeout(r, 3));
    const taken = p.take(k)!; // handed over: no longer held ahead
    held -= 10;
    assert.ok((await taken).ok);
  }
  assert.ok(most <= 35, `${most}`);
});

test("a failed read is the same failure", async () => {
  const objects = new Map<string, Uint8Array | number>([[".zgroup", ZGROUP], ["a/.zarray", ZARRAY], ["b/.zarray", 404],
    ["c/.zarray", ZARRAY]]);
  const failed = await outcome(new FakeS3(objects), 8);
  assert.deepEqual(failed, await outcome(new FakeS3(objects), 1));
  assert.deepEqual(failed, ["failed", "StoreReadError", "http://h/b/s/b/.zarray: HTTP 404"]);
});

test("a short read is the same failure", async () => {
  const objects = new Map<string, Uint8Array | number>([[".zgroup", ZGROUP], ["a/.zarray", ZARRAY], ["b/.zarray", ZARRAY]]);
  const failed = await outcome(new FakeS3(objects, 0, new Set(["b/.zarray"])), 8);
  assert.deepEqual(failed, await outcome(new FakeS3(objects, 0, new Set(["b/.zarray"])), 1));
  assert.match(failed[2] as string, /the listing says/);
});

test("a failed read the profile never makes is not a failure", async () => {
  // a/.zarray rejects the input before the profile reads b/.zarray, which the concurrent
  // reads have read (and failed to).
  const objects = new Map<string, Uint8Array | number>([[".zgroup", ZGROUP], ["a/.zarray", utf8.encode('{"zarr_format": 3}')],
    ["b/.zarray", 404]]);
  const rejected = await outcome(new FakeS3(objects), 8);
  assert.deepEqual(rejected, await outcome(new FakeS3(objects), 1));
  assert.deepEqual(rejected, ["rejected", "Zarr2Error", "a/.zarray: zarr_format is not 2"]);
});
