import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { spawnSync } from "node:child_process";
import { CLI, tmpDir, unzipTest } from "./helpers.ts";
import { Archive } from "../src/reader.ts";
import { validateArchive } from "./validate.ts";

const dir = tmpDir();
let n = 0;

function write(descText: string): { status: number | null; stderr: string; out: string } {
  const dp = path.join(dir, `d${n}.json`);
  const out = path.join(dir, `o${n}.vzip`);
  n++;
  fs.writeFileSync(dp, descText);
  const r = spawnSync(CLI, ["write", dp, out], { encoding: "utf8" });
  return { status: r.status, stderr: r.stderr, out };
}

test("writer accepts boundary inputs", async () => {
  // payload of exactly 65519 bytes: literal of 65515 bytes = 1 tag + 3 length + 65515
  const lit = "ab".repeat(65515);
  const r = write(JSON.stringify({
    page_size: 1,
    sources: [{ url: "x.bin", size: 0, etag: '"!~"', modified_not_after: 0 }, { data: "" }, { key: "__vz__/h" }],
    entries: [
      { key: "big", ranges: [{ data: lit }] },
      { key: "__vz__/h", bytes: "", pinned: true, compress: true },
      { key: "e", ranges: [{}] },
      { key: "f", ranges: [{ source: 2, length: 0 }], compress: false, pinned: false },
    ],
  }));
  assert.equal(r.status, 0, r.stderr);
  assert.ok(unzipTest(r.out).ok);
  validateArchive(fs.readFileSync(r.out));
  const a = await Archive.open(r.out);
  assert.equal((await a.get("big"))!.length, 65515);
  assert.deepEqual(await a.get("e"), Buffer.alloc(0));
  assert.deepEqual(a.sources[0], { kind: "url", url: "x.bin", size: 0n, etag: '"!~"', modifiedNotAfter: 0n });
  await a.close();
});

const rejected: [string, unknown][] = [
  ["empty key", { entries: [{ key: "", bytes: "" }] }],
  ["duplicate key", { entries: [{ key: "a", bytes: "" }, { key: "a", bytes: "00" }] }],
  ["key __vz__/sources", { entries: [{ key: "__vz__/sources", bytes: "" }] }],
  ["key __vz__/index", { entries: [{ key: "__vz__/index", bytes: "" }] }],
  ["source index out of range", { sources: [{ data: "00" }], entries: [{ key: "a", ranges: [{ source: 1, length: 1 }] }] }],
  ["source range with no sources", { entries: [{ key: "a", ranges: [{}] }] }],
  ["empty url", { sources: [{ url: "" }] }],
  ["url not a URI-reference", { sources: [{ url: "a b.bin" }] }],
  ["url with non-ASCII", { sources: [{ url: "é.bin" }] }],
  ["key source absent", { sources: [{ key: "nope" }] }],
  ["key source names a reference entry", { sources: [{ key: "r" }, { data: "00" }], entries: [{ key: "r", ranges: [{ source: 1, length: 1 }] }] }],
  ["key source names a format entry", { sources: [{ key: "__vz__/sources" }] }],
  ["pin on key source", { sources: [{ key: "a", size: 1 }], entries: [{ key: "a", bytes: "" }] }],
  ["pin on data source", { sources: [{ data: "", etag: '"x"' }] }],
  ["weak etag", { sources: [{ url: "x", etag: 'W/"x"' }] }],
  ["etag without quotes", { sources: [{ url: "x", etag: "x" }] }],
  ["payload over 65519 bytes", { entries: [{ key: "a", ranges: [{ data: "ab".repeat(65516) }] }] }],
  ["pinned reference entry", { page_size: 1, sources: [{ data: "00" }], entries: [{ key: "a", ranges: [{ length: 1 }], pinned: true }] }],
  ["pinned without page index", { entries: [{ key: "a", bytes: "", pinned: true }] }],
  ["compress on reference entry", { entries: [{ key: "a", ranges: [], compress: true }] }],
  ["page_size 0", { page_size: 0 }],
  ["page_size string", { page_size: "1" }],
  ["mirror not boolean", { mirror: 1 }],
  ["compress not boolean", { entries: [{ key: "a", bytes: "", compress: "yes" }] }],
  ["uppercase hex", { entries: [{ key: "a", bytes: "AB" }] }],
  ["odd-length hex", { entries: [{ key: "a", bytes: "abc" }] }],
  ["null sources", { sources: null }],
  ["null member of entry", { entries: [{ key: "a", bytes: "", compress: null }] }],
  ["null range member", { sources: [{ data: "00" }], entries: [{ key: "a", ranges: [{ source: 0, offset: null }] }] }],
  ["both bytes and ranges", { entries: [{ key: "a", bytes: "", ranges: [] }] }],
  ["neither bytes nor ranges", { entries: [{ key: "a" }] }],
  ["entry without key", { entries: [{ bytes: "" }] }],
  ["source with two kinds", { sources: [{ data: "", url: "x" }] }],
  ["source with no kind", { sources: [{}] }],
  ["range mixing literal and source", { sources: [{ data: "00" }], entries: [{ key: "a", ranges: [{ data: "00", source: 0 }] }] }],
  ["negative offset", { sources: [{ data: "00" }], entries: [{ key: "a", ranges: [{ offset: -1 }] }] }],
  ["description not an object", []],
];
for (const [name, desc] of rejected) {
  test(`writer rejects: ${name}`, () => {
    const r = write(JSON.stringify(desc));
    assert.notEqual(r.status, 0);
    assert.ok(r.stderr.length > 0);
    assert.equal(fs.existsSync(r.out), false);
  });
}

const rejectedText: [string, string][] = [
  ["invalid UTF-8 key (lone surrogate)", '{"entries":[{"key":"\\ud800","bytes":""}]}'],
  ["non-integer number 1.0", '{"page_size":1.0}'],
  ["exponent number", '{"sources":[{"data":"00"}],"entries":[{"key":"a","ranges":[{"length":1e0}]}]}'],
  ["not JSON", "{"],
];
for (const [name, text] of rejectedText) {
  test(`writer rejects: ${name}`, () => {
    const r = write(text);
    assert.notEqual(r.status, 0);
    assert.equal(fs.existsSync(r.out), false);
  });
}

const badQueries: [string, unknown][] = [
  ["unknown op", [{ op: "frob", key: "a" }]],
  ["missing key", [{ op: "get" }]],
  ["range with several forms", [{ op: "get", key: "a", range: { offset: 1, suffix: 1 } }]],
  ["not an array", { op: "get", key: "a" }],
];
for (const [name, q] of badQueries) {
  test(`read rejects queries file: ${name}`, () => {
    const okDesc = write("{}");
    const qp = path.join(dir, `q${n++}.json`);
    fs.writeFileSync(qp, JSON.stringify(q));
    const r = spawnSync(CLI, ["read", okDesc.out, qp], { encoding: "utf8" });
    assert.notEqual(r.status, 0);
  });
}

test("read reports an archive error as JSON with exit status 0", () => {
  const qp = path.join(dir, `q${n++}.json`);
  fs.writeFileSync(qp, JSON.stringify([{ op: "list", prefix: "" }]));
  const r = spawnSync(CLI, ["read", path.join(dir, "does-not-exist.vzip"), qp], { encoding: "utf8" });
  assert.equal(r.status, 0);
  const j = JSON.parse(r.stdout);
  assert.equal(j.open.ok, false);
  assert.equal(j.open.class, "archive");
  assert.deepEqual(j.results, []);
});
