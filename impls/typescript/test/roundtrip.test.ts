import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { spawnSync } from "node:child_process";
import { Archive } from "../src/reader.ts";
import { CLI, tmpDir, unzipTest } from "./helpers.ts";
import { validateArchive } from "./validate.ts";

const enc = (s: string) => Buffer.from(s, "utf8").toString("hex");

function runCli(args: string[], cwd: string) {
  return spawnSync(CLI, args, { cwd, encoding: "utf8" });
}

test("write/read round trip across layouts, via library and CLI", async () => {
  const dir = tmpDir();
  fs.mkdirSync(path.join(dir, "data"));
  const ext = Buffer.from("0123456789abcdefghijKLMNOPQRST");
  fs.writeFileSync(path.join(dir, "data", "a b.bin"), ext);
  const mtime = Math.floor(fs.statSync(path.join(dir, "data", "a b.bin")).mtimeMs / 1000);
  const hdr = Buffer.from("HDR!");
  const big = Buffer.alloc(5000, 7);
  const keys = ["\u{1F600}", "\u{FF5E}", "﻿bom", "a/../b", "x/", "x/zarr.json", "x/c/0", "x/c/1", "x/c/2", "x/c/3", "x/big", "y"];
  const sources = [
    { url: "data/a%20b.bin", size: ext.length, modified_not_after: mtime },
    { key: "__vz__/hdr" },
    { data: enc("shared") },
    { url: "#frag" }, // the archive itself
    { key: "x/big" },
  ];
  const expected = new Map<string, Buffer>();
  const entries: Record<string, unknown>[] = [];
  const addBytes = (key: string, b: Buffer, extra: Record<string, unknown> = {}) => {
    entries.push({ key, bytes: b.toString("hex"), ...extra });
    expected.set(key, b);
  };
  addBytes("\u{1F600}", Buffer.from("emoji"), { compress: true });
  addBytes("\u{FF5E}", Buffer.from("tilde"));
  addBytes("﻿bom", Buffer.from(""), { compress: true });
  addBytes("a/../b", Buffer.from("dots"));
  addBytes("x/", Buffer.from("dir?"));
  addBytes("x/zarr.json", Buffer.from("{}"), { pinned: "maybe" });
  addBytes("x/big", big, { compress: true });
  entries.push({ key: "__vz__/hdr", bytes: hdr.toString("hex"), compress: true });
  entries.push({ key: "x/c/0", ranges: [{ source: 0, offset: 10, length: 4 }] });
  expected.set("x/c/0", ext.subarray(10, 14));
  entries.push({ key: "x/c/1", ranges: [{ source: 2, offset: 0, length: 3 }, { data: "00ff" }, { source: 1, offset: 1, length: 2 }, { source: 4, offset: 4990, length: 10 }] });
  expected.set("x/c/1", Buffer.concat([Buffer.from("sha"), Buffer.from([0, 0xff]), hdr.subarray(1, 3), big.subarray(4990)]));
  entries.push({ key: "x/c/2", ranges: [] });
  expected.set("x/c/2", Buffer.alloc(0));
  entries.push({ key: "x/c/3", ranges: [{ data: "" }, { source: 3, offset: 0, length: 4 }, { source: 0, offset: 0, length: 0 }] });
  expected.set("x/c/3", Buffer.from("PK\x03\x04", "latin1"));
  entries.push({ key: "y", ranges: [{ data: "abcdef" }] });
  expected.set("y", Buffer.from("abcdef", "hex"));

  let n = 0;
  for (const pageSize of [null, 1, 120, 100000]) {
    for (const mirror of [true, false]) {
      const desc = {
        page_size: pageSize,
        mirror,
        sources,
        entries: entries.map((e) => {
          const c = { ...e };
          if (c.pinned === "maybe") c.pinned = pageSize !== null;
          return c;
        }),
        unknown_member: 1,
      };
      const dp = path.join(dir, `d${n}.json`);
      const ap = path.join(dir, `a${n}.vzip`);
      n++;
      fs.writeFileSync(dp, JSON.stringify(desc));
      const w = runCli(["write", dp, ap], "/");
      assert.equal(w.status, 0, w.stderr);
      const u = unzipTest(ap);
      assert.ok(u.ok, u.out);
      validateArchive(fs.readFileSync(ap));

      const a = await Archive.open(ap);
      assert.equal(a.paged, pageSize !== null);
      for (const k of keys) {
        const want = expected.get(k)!;
        const kind = entries.find((e) => e.key === k)!.bytes !== undefined ? "bytes" : "reference";
        assert.equal(await a.classify(k), kind, k);
        assert.deepEqual(await a.get(k), want, k);
        const L = BigInt(want.length);
        for (const [s, e] of [[0n, 0n], [1n, 3n], [2n, 100n], [L, L + 5n], [L + 2n, L + 9n]]) {
          const lo = Math.min(Number(s), want.length);
          const hi = Math.min(Number(e), want.length);
          assert.deepEqual(await a.get(k, { type: "range", start: s, end: e }), want.subarray(lo, hi), `${k} ${s}-${e}`);
        }
        assert.deepEqual(await a.get(k, { type: "offset", start: 2n }), want.subarray(Math.min(2, want.length)));
        assert.deepEqual(await a.get(k, { type: "suffix", count: 3n }), want.subarray(Math.max(0, want.length - 3)));
        assert.deepEqual(await a.get(k, { type: "suffix", count: 0n }), Buffer.alloc(0));
        const raw = await a.raw(k);
        if (kind === "bytes") assert.deepEqual(raw, want);
        else if (!mirror) assert.equal(raw!.length, 0);
        else assert.ok(raw!.length > 0 || k === "x/c/2");
      }
      for (const k of ["__vz__/hdr", "__vz__/sources", "__vz__/index", "nope", "x", "x/c", ""]) {
        assert.equal(await a.classify(k), "missing", k);
        assert.equal(await a.get(k), null, k);
      }
      assert.deepEqual(await a.raw("__vz__/hdr"), hdr);
      assert.ok((await a.raw("__vz__/sources"))!.length > 0);
      assert.equal((await a.raw("__vz__/index")) === null, pageSize === null);
      const sorted = [...keys].sort((p, q) => Buffer.compare(Buffer.from(p), Buffer.from(q)));
      assert.deepEqual(await a.list(""), sorted);
      assert.deepEqual(await a.list("x/"), ["x/", "x/big", "x/c/0", "x/c/1", "x/c/2", "x/c/3", "x/zarr.json"]);
      assert.deepEqual(await a.list("x/c/"), ["x/c/0", "x/c/1", "x/c/2", "x/c/3"]);
      assert.deepEqual(await a.list("\u{1F600}"), ["\u{1F600}"]);
      assert.deepEqual(await a.list("﻿"), ["﻿bom"]);
      assert.deepEqual(await a.list("__vz__/"), []);
      assert.deepEqual(await a.list("z"), []);
      assert.deepEqual(await a.list("\xff"), []);
      await a.close();

      // CLI read, run from another directory with a relative archive path
      const qp = path.join(dir, "q.json");
      fs.writeFileSync(qp, JSON.stringify([
        { op: "classify", key: "x/c/1" },
        { op: "get", key: "x/c/1", range: { start: 1, end: 4 } },
        { op: "get", key: "x/c/0", range: { suffix: 2 } },
        { op: "get", key: "x/c/0", range: { offset: 3 } },
        { op: "get", key: "missing" },
        { op: "get", key: "x/c/0", range: { start: 3, end: 2 } },
        { op: "get_raw", key: "__vz__/hdr" },
        { op: "list", prefix: "x/c" },
      ]));
      const r = runCli(["read", path.basename(ap), qp], dir);
      assert.equal(r.status, 0, r.stderr);
      assert.deepEqual(JSON.parse(r.stdout), {
        open: { ok: true },
        results: [
          { ok: true, kind: "reference" },
          { ok: true, value: expected.get("x/c/1")!.subarray(1, 4).toString("hex") },
          { ok: true, value: enc("cd") },
          { ok: true, value: enc("d") },
          { ok: true, value: null },
          { ok: false, class: "request", error: "range start > end" },
          { ok: true, value: hdr.toString("hex") },
          { ok: true, keys: ["x/c/0", "x/c/1", "x/c/2", "x/c/3"] },
        ],
      });
    }
  }
});

test("writer output is byte-identical for identical input and uses ZIP64 only when needed", async () => {
  const dir = tmpDir();
  // 70000 entries: entry count needs the zip64 end records
  const entries = [];
  for (let i = 0; i < 70000; i++) entries.push({ key: `k/${String(i).padStart(6, "0")}`, bytes: "" });
  for (const pageSize of [null, 4096]) {
    const dp = path.join(dir, `d${pageSize}.json`);
    fs.writeFileSync(dp, JSON.stringify({ page_size: pageSize, entries }));
    const p1 = path.join(dir, `a${pageSize}.vzip`);
    const p2 = path.join(dir, `b${pageSize}.vzip`);
    assert.equal(runCli(["write", dp, p1], dir).status, 0);
    assert.equal(runCli(["write", dp, p2], dir).status, 0);
    const b = fs.readFileSync(p1);
    assert.deepEqual(b, fs.readFileSync(p2));
    const commentLen = pageSize === null ? 22 : 38;
    const eocd = b.length - 22 - commentLen;
    assert.equal(b.readUInt16LE(eocd + 10), 0xffff);
    assert.equal(b.readUInt32LE(eocd - 20), 0x07064b50);
    assert.ok(unzipTest(p1).ok);
    validateArchive(b);
    const a = await Archive.open(p1);
    assert.equal((await a.list("k/")).length, 70000);
    assert.deepEqual(await a.get("k/069999"), Buffer.alloc(0));
    assert.equal(await a.classify("k/070000"), "missing");
    await a.close();
  }
  // small archive: no zip64 end records
  const dp = path.join(dir, "small.json");
  fs.writeFileSync(dp, JSON.stringify({ entries: [{ key: "a", bytes: "00" }] }));
  const sp = path.join(dir, "small.vzip");
  assert.equal(runCli(["write", dp, sp], dir).status, 0);
  const s = fs.readFileSync(sp);
  assert.notEqual(s.readUInt32LE(s.length - 44 - 20), 0x07064b50);
});
