import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { readQueries, tmpdir, writeDesc } from "./helpers.ts";
import { Archive } from "../src/reader.ts";

const hex = (s: string) => Buffer.from(s).toString("hex");

function unzipOk(p: string): void {
  const r = spawnSync("unzip", ["-tq", p], { encoding: "utf8", maxBuffer: 1 << 28 });
  assert.equal(r.status, 0, r.stdout + r.stderr);
}

function utf8Sort(keys: string[]): string[] {
  return [...keys].sort((a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b)));
}

test("round trip: every page size, mirror setting and pinning gives the same values", () => {
  const dir = tmpdir();
  fs.mkdirSync(path.join(dir, "data"));
  fs.writeFileSync(path.join(dir, "data", "a b.bin"), "0123456789abcdefghijklmnopqrstuvwxyz");
  const keys = ["zarr.json", "a/zarr.json", "a/c/0", "a/c/1", "a/c/2", "b/x", "\u{FF5E}", "\u{1F600}", "﻿bom", "x/", "a/../b", "__vz__/hdr", "empty"];
  const entries: any[] = [
    { key: "zarr.json", bytes: hex('{"zarr_format":3}'), compress: true, pinned: true },
    { key: "a/zarr.json", bytes: hex("{}"), pinned: true },
    { key: "a/c/0", ranges: [{ source: 0, offset: 10, length: 4 }] },
    { key: "a/c/1", ranges: [{ source: 2, offset: 0, length: 3 }, { data: "00ff" }, { source: 1, offset: 1, length: 2 }] },
    { key: "a/c/2", ranges: [] },
    { key: "b/x", bytes: hex("hello".repeat(100)), compress: true },
    { key: "\u{FF5E}", bytes: "01" },
    { key: "\u{1F600}", bytes: "02" },
    { key: "﻿bom", bytes: "03" },
    { key: "x/", ranges: [{ data: "" }] },
    { key: "a/../b", ranges: [{ source: 3, offset: 0, length: 2 }] },
    { key: "__vz__/hdr", bytes: "aabbccdd", compress: true, pinned: true },
    { key: "empty", bytes: "" },
  ];
  const sources = [{ url: "data/a%20b.bin", size: 36 }, { key: "__vz__/hdr" }, { data: "48445221" }, { url: "#frag" }];
  const queries: any[] = [
    { op: "list", prefix: "" },
    { op: "list", prefix: "a/" },
    { op: "list", prefix: "a/c/" },
    { op: "list", prefix: "\u{1F600}" },
    { op: "list", prefix: "zz" },
    ...keys.map((k) => ({ op: "classify", key: k })),
    ...keys.map((k) => ({ op: "get", key: k })),
    { op: "get", key: "a/c/1", range: { start: 2, end: 5 } },
    { op: "get", key: "a/c/1", range: { start: 5, end: 100 } },
    { op: "get", key: "a/c/1", range: { offset: 6 } },
    { op: "get", key: "a/c/1", range: { suffix: 100 } },
    { op: "get", key: "b/x", range: { start: 498, end: 1000 } },
    { op: "get", key: "zarr.json", range: { suffix: 2 } },
    { op: "get_raw", key: "__vz__/hdr" },
    { op: "get", key: "missing" },
    { op: "classify", key: "__vz__/sources" },
  ];
  const visible = utf8Sort(keys.filter((k) => !k.startsWith("__vz__/")));
  let reference: any = null;
  for (const pageSize of [null, 1, 50, 200, 100000]) {
    for (const mirror of [true, false]) {
      const desc = { page_size: pageSize, mirror, sources, entries: entries.map((e) => (pageSize === null ? { ...e, pinned: false } : e)) };
      const w = writeDesc(dir, desc, `rt-${pageSize}-${mirror}.vzip`);
      assert.equal(w.status, 0, w.stderr);
      unzipOk(w.out);
      const r = readQueries(w.out, queries);
      assert.deepEqual(r.open, { ok: true });
      const res = r.results;
      assert.deepEqual(res[0].keys, visible);
      assert.deepEqual(res[1].keys, ["a/../b", "a/c/0", "a/c/1", "a/c/2", "a/zarr.json"]);
      assert.deepEqual(res[2].keys, ["a/c/0", "a/c/1", "a/c/2"]);
      assert.deepEqual(res[3].keys, ["\u{1F600}"]);
      assert.deepEqual(res[4].keys, []);
      if (reference === null) reference = res;
      else assert.deepEqual(res, reference, `page_size=${pageSize} mirror=${mirror}`);
    }
  }
  const byKey = (op: string, k: string) => reference[queries.findIndex((q) => q.op === op && q.key === k && !q.range)];
  assert.equal(byKey("classify", "a/c/0").kind, "reference");
  assert.equal(byKey("classify", "zarr.json").kind, "bytes");
  assert.equal(byKey("classify", "__vz__/hdr").kind, "missing");
  assert.equal(byKey("get", "a/c/0").value, hex("abcd"));
  assert.equal(byKey("get", "a/c/1").value, "48445200ffbbcc");
  assert.equal(byKey("get", "a/c/2").value, "");
  assert.equal(byKey("get", "x/").value, "");
  assert.equal(byKey("get", "empty").value, "");
  assert.equal(byKey("get", "__vz__/hdr").value, null);
  // a/../b: "#frag" resolves to the archive itself; its first two bytes are a local header signature.
  assert.equal(byKey("get", "a/../b").value, "504b");
  const n = keys.length * 2 + 5;
  assert.deepEqual(reference.slice(n, n + 6).map((r: any) => r.value), ["5200ff", "bbcc", "cc", "48445200ffbbcc", hex("lo"), hex("3}")]);
  assert.equal(reference[n + 6].value, "aabbccdd");
  assert.equal(reference[n + 7].value, null);
  assert.equal(reference[n + 8].kind, "missing");
});

test("writer output layout: comment, ZIP flags, page index structure", () => {
  const dir = tmpdir();
  const entries = Array.from({ length: 30 }, (_, i) => ({ key: `k/${String(i).padStart(3, "0")}`, bytes: "00" }));
  const w = writeDesc(dir, { page_size: 300, entries: [...entries, { key: "p", bytes: "abcd", pinned: true }] });
  assert.equal(w.status, 0, w.stderr);
  const buf = fs.readFileSync(w.out);
  assert.equal(buf.readUInt32LE(buf.length - 60), 0x06054b50);
  assert.equal(buf.readUInt16LE(buf.length - 60 + 20), 38);
  assert.equal(buf.subarray(buf.length - 38, buf.length - 32).toString(), "vzip/0");
  // Every local header: extra length 0, bit 11 set, no bit 3.
  let p = 0;
  while (buf.readUInt32LE(p) === 0x04034b50) {
    assert.equal(buf.readUInt16LE(p + 28), 0);
    assert.equal(buf.readUInt16LE(p + 6) & 0x808, 0x800);
    p += 30 + buf.readUInt16LE(p + 26) + buf.readUInt32LE(p + 18);
  }
  // Library-level look at the page index via the archive.
  const a = Archive.open(w.out);
  assert.equal(a.list("k/").length, 30);
  assert.equal(a.classify("p"), "bytes");
  a.close();
  unzipOk(w.out);
});

test("empty description writes a valid archive", () => {
  const dir = tmpdir();
  for (const d of [{}, { page_size: 1 }]) {
    const w = writeDesc(dir, d, `e${JSON.stringify(d).length}.vzip`);
    assert.equal(w.status, 0, w.stderr);
    const r = readQueries(w.out, [{ op: "list", prefix: "" }, { op: "get_raw", key: "__vz__/sources" }]);
    assert.deepEqual(r.results, [{ ok: true, keys: [] }, { ok: true, value: "" }]);
    unzipOk(w.out);
  }
});

test("writer output is deterministic and canonical", () => {
  const dir = tmpdir();
  const desc = { sources: [{ data: "" }, { url: "x", size: 0, etag: '""', modified_not_after: -1 }], entries: [{ key: "a", ranges: [{ source: 1 }] }, { key: "b", ranges: [{}, {}] }] };
  const a = writeDesc(dir, desc, "d1.vzip");
  const b = writeDesc(dir, desc, "d2.vzip");
  assert.equal(a.status, 0, a.stderr);
  assert.ok(fs.readFileSync(a.out).equals(fs.readFileSync(b.out)));
  const r = readQueries(a.out, [{ op: "get_raw", key: "__vz__/sources" }, { op: "get_raw", key: "a" }, { op: "get_raw", key: "b" }, { op: "get", key: "b" }]);
  // SourceTable: data "" (emitted, empty), url "x" with size 0, etag "\"\"", mna -1 (10-byte varint).
  assert.equal(r.results[0].value, "0a021a00" + "0a14" + "0a0178" + "2000" + "2a022222" + "30ffffffffffffffffff01");
  assert.equal(r.results[1].value, "0801");
  assert.equal(r.results[2].value, "0a000a00");
  assert.equal(r.results[3].value, "");
});

test("ZIP64 end records when there are 0xFFFF or more entries", () => {
  const dir = tmpdir();
  const entries = Array.from({ length: 0x10000 }, (_, i) => ({ key: `k${i}`, bytes: "" }));
  for (const page_size of [null, 4096]) {
    const w = writeDesc(dir, { page_size, entries }, `z64-${page_size}.vzip`);
    assert.equal(w.status, 0, w.stderr);
    const buf = fs.readFileSync(w.out);
    const eocd = buf.length - (page_size === null ? 44 : 60);
    assert.equal(buf.readUInt16LE(eocd + 10), 0xffff);
    assert.equal(buf.readUInt32LE(eocd - 20), 0x07064b50);
    const r = readQueries(w.out, [{ op: "get", key: "k65535" }, { op: "classify", key: "k12345" }]);
    assert.deepEqual(r.results, [{ ok: true, value: "" }, { ok: true, kind: "bytes" }]);
    const a = Archive.open(w.out);
    assert.equal(a.list("").length, 0x10000);
    a.close();
    unzipOk(w.out);
  }
});
