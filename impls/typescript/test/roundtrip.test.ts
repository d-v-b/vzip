import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { runCli, tmpdir, unzipOk, writeTmp } from "./helpers.ts";

type Desc = {
  page_size?: number | null;
  mirror?: boolean;
  sources?: Record<string, unknown>[];
  entries?: Record<string, unknown>[];
};

function rng(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 2 ** 32;
  };
}

const KEY_POOL = [
  "a", "a/b", "a/b/c", "a/zarr.json", "b", "b/0", "b/1", "x/", "a/../b", "\u{FF5E}", "\u{1F600}", "é",
  "﻿bom", "z", "zz/0.0", "__vz__/hidden", "__vz__/hdr2", "c/0", "c/1", "c/2", "c/10", "d", "日本",
];

function expectedValues(dir: string, d: Desc): Map<string, Buffer> {
  const bytes = new Map<string, Buffer>();
  for (const e of d.entries ?? []) if ("bytes" in e) bytes.set(e.key as string, Buffer.from(e.bytes as string, "hex"));
  const srcVal = (i: number): Buffer => {
    const s = d.sources![i];
    if ("url" in s) return fs.readFileSync(path.join(dir, decodeURIComponent(s.url as string)));
    if ("key" in s) return bytes.get(s.key as string)!;
    return Buffer.from(s.data as string, "hex");
  };
  const out = new Map<string, Buffer>();
  for (const e of d.entries ?? []) {
    if ("bytes" in e) out.set(e.key as string, bytes.get(e.key as string)!);
    else {
      const parts = (e.ranges as Record<string, unknown>[]).map((r) => {
        if ("data" in r) return Buffer.from(r.data as string, "hex");
        const off = (r.offset as number) ?? 0, len = (r.length as number) ?? 0;
        return srcVal((r.source as number) ?? 0).subarray(off, off + len);
      });
      out.set(e.key as string, Buffer.concat(parts));
    }
  }
  return out;
}

function randomDesc(r: () => number, dir: string): Desc {
  const pick = <T>(a: T[]) => a[Math.floor(r() * a.length)];
  const hex = (n: number) => Buffer.from(Array.from({ length: n }, () => Math.floor(r() * 256))).toString("hex");
  const keys = [...new Set(Array.from({ length: 1 + Math.floor(r() * 15) }, () => pick(KEY_POOL)))];
  const ext = Buffer.from(Array.from({ length: 200 }, (_, i) => i));
  writeTmp(dir, "data/ext file.bin", ext);
  writeTmp(dir, "data/é.bin", ext.subarray(0, 50));
  const nBytes = Math.max(1, Math.floor(keys.length / 2));
  const entries: Record<string, unknown>[] = keys.slice(0, nBytes).map((k) => ({
    key: k,
    bytes: hex(Math.floor(r() * 40)),
    compress: r() < 0.5,
  }));
  const keySrc = entries.find((e) => (e.bytes as string).length >= 20);
  const sources: Record<string, unknown>[] = [
    { url: "data/ext%20file.bin", size: 200 },
    { url: "data/%C3%A9.bin" },
    { data: hex(16) },
  ];
  if (keySrc) sources.push({ key: keySrc.key });
  const srcLen = [200, 50, 16, keySrc ? (keySrc.bytes as string).length / 2 : 0];
  for (const k of keys.slice(nBytes)) {
    const n = Math.floor(r() * 4);
    const ranges = Array.from({ length: n }, () => {
      if (r() < 0.25) return { data: hex(Math.floor(r() * 5)) };
      const s = Math.floor(r() * sources.length);
      const off = Math.floor(r() * srcLen[s]);
      const len = Math.floor(r() * (srcLen[s] - off + 1));
      return { source: s, offset: off, length: len };
    });
    entries.push({ key: k, ranges });
  }
  // pin a few bytes entries when paged
  const paged = r() < 0.6;
  if (paged) for (const e of entries) if ("bytes" in e && r() < 0.3) e.pinned = true;
  return {
    page_size: paged ? pick([1, 60, 200, 100000]) : null,
    mirror: r() < 0.5,
    sources,
    entries,
  };
}

function checkArchive(dir: string, d: Desc, label: string) {
  const descPath = writeTmp(dir, "desc.json", JSON.stringify(d));
  const out = path.join(dir, "out.vzip");
  fs.rmSync(out, { force: true });
  const w = runCli(["write", descPath, out], "/");
  assert.equal(w.status, 0, `${label}: write failed: ${w.stderr}`);
  assert.ok(unzipOk(out), `${label}: unzip -t failed`);
  const exp = expectedValues(dir, d);
  const visible = [...exp.keys()].filter((k) => !k.startsWith("__vz__/"));
  const sortU8 = (a: string[]) => [...a].sort((x, y) => Buffer.compare(Buffer.from(x), Buffer.from(y)));
  const queries: unknown[] = [];
  const expects: unknown[] = [];
  for (const [k, v] of exp) {
    const hidden = k.startsWith("__vz__/");
    const isRef = (d.entries ?? []).find((e) => e.key === k)!.ranges !== undefined;
    queries.push({ op: "classify", key: k });
    expects.push({ ok: true, kind: hidden ? "missing" : isRef ? "reference" : "bytes" });
    queries.push({ op: "get", key: k });
    expects.push({ ok: true, value: hidden ? null : v.toString("hex") });
    for (const [s, e] of [[0, 3], [2, 5], [5, 1000], [1000, 2000]]) {
      queries.push({ op: "get", key: k, range: { start: s, end: e } });
      expects.push({ ok: true, value: hidden ? null : v.subarray(s, e).toString("hex") });
    }
    queries.push({ op: "get", key: k, range: { offset: 3 } });
    expects.push({ ok: true, value: hidden ? null : v.subarray(3).toString("hex") });
    queries.push({ op: "get", key: k, range: { suffix: 4 } });
    expects.push({ ok: true, value: hidden ? null : v.subarray(Math.max(0, v.length - 4)).toString("hex") });
    if (!isRef) {
      queries.push({ op: "get_raw", key: k });
      expects.push({ ok: true, value: v.toString("hex") });
    }
  }
  for (const p of ["", "a", "a/", "b/", "c/", "z", "\u{FF5E}", "__vz__/", "q"]) {
    queries.push({ op: "list", prefix: p });
    expects.push({ ok: true, keys: sortU8(visible.filter((k) => k.startsWith(p))) });
  }
  queries.push({ op: "get", key: "does/not/exist" });
  expects.push({ ok: true, value: null });
  queries.push({ op: "get_raw", key: "__vz__/index" });
  const qPath = writeTmp(dir, "q.json", JSON.stringify(queries));
  const rd = runCli(["read", out, qPath], "/");
  assert.equal(rd.status, 0, rd.stderr);
  const res = JSON.parse(rd.stdout);
  assert.deepEqual(res.open, { ok: true });
  const idxRes = res.results.pop();
  assert.equal(idxRes.ok, true);
  assert.equal(idxRes.value === null, d.page_size === null || d.page_size === undefined, `${label}: index presence`);
  assert.deepEqual(res.results, expects, label);
}

test("round trip: fixed and random descriptions", () => {
  const dir = tmpdir();
  writeTmp(dir, "data/a.bin", Buffer.from(Array.from({ length: 32 }, (_, i) => i)));
  const fixed: Desc[] = [
    {},
    { page_size: 1 },
    {
      sources: [{ url: "data/a.bin" }, { key: "__vz__/hdr" }, { data: "48445221" }],
      entries: [
        { key: "x/zarr.json", bytes: "7b7d", compress: false, pinned: false },
        { key: "__vz__/hdr", bytes: "00112233", compress: true },
        { key: "x/c/0", ranges: [{ source: 0, offset: 10, length: 4 }] },
        { key: "x/c/1", ranges: [{ source: 2, offset: 0, length: 3 }, { data: "00ff" }, { source: 1, offset: 1, length: 2 }] },
        { key: "x/c/2", ranges: [] },
        { key: "x/c/3", ranges: [{ data: "" }] },
        { key: "x/c/4", ranges: [{}] },
      ],
    },
    {
      page_size: 50,
      mirror: false,
      sources: [{ url: "data/a.bin", size: 32 }],
      entries: [
        { key: "\u{1F600}", bytes: "01", pinned: true },
        { key: "\u{FF5E}", bytes: "02", compress: true, pinned: true },
        { key: "__vz__/p", bytes: "03", pinned: true },
        ...Array.from({ length: 30 }, (_, i) => ({ key: `c/${i}`, ranges: [{ source: 0, offset: i, length: 2 }] })),
      ],
    },
  ];
  fixed.forEach((d, i) => checkArchive(dir, d, `fixed ${i}`));
  const r = rng(12345);
  for (let i = 0; i < 40; i++) checkArchive(dir, randomDesc(r, dir), `random ${i}`);
});

test("writer output is deterministic and canonical", () => {
  const dir = tmpdir();
  const d = { page_size: 100, entries: [{ key: "b", bytes: "00" }, { key: "a", bytes: "11", pinned: true }] };
  const p = writeTmp(dir, "d.json", JSON.stringify(d));
  assert.equal(runCli(["write", p, path.join(dir, "1.vzip")]).status, 0);
  assert.equal(runCli(["write", p, path.join(dir, "2.vzip")]).status, 0);
  const a = fs.readFileSync(path.join(dir, "1.vzip"));
  assert.deepEqual(a, fs.readFileSync(path.join(dir, "2.vzip")));
  // 38-byte comment with the magic
  assert.equal(a.subarray(a.length - 38, a.length - 32).toString(), "vzip/0");
  assert.equal(a.readUInt16LE(a.length - 38 - 2), 38);
});

test("zip64 end records in every archive, with an all-ones end record", () => {
  const dir = tmpdir();
  for (const [pageSize, clen, count] of [[null, 22, 2n], [64, 38, 3n]] as [number | null, number, bigint][]) {
    const p = writeTmp(dir, `d${clen}.json`, JSON.stringify({ page_size: pageSize, entries: [{ key: "a", bytes: "00" }] }));
    const out = path.join(dir, `small${clen}.vzip`);
    assert.equal(runCli(["write", p, out]).status, 0);
    const a = fs.readFileSync(out);
    const eocd = a.length - 22 - clen;
    assert.equal(a.readUInt32LE(eocd), 0x06054b50);
    assert.deepEqual([a.readUInt16LE(eocd + 8), a.readUInt16LE(eocd + 10)], [0xffff, 0xffff]);
    assert.deepEqual([a.readUInt32LE(eocd + 12), a.readUInt32LE(eocd + 16)], [0xffffffff, 0xffffffff]);
    assert.equal(a.readUInt32LE(eocd - 20), 0x07064b50);
    const z = eocd - 76;
    assert.equal(a.readBigUInt64LE(eocd - 20 + 8), BigInt(z));
    assert.equal(a.readUInt32LE(z), 0x06064b50);
    assert.equal(a.readBigUInt64LE(z + 4), 44n);
    assert.deepEqual([a.readBigUInt64LE(z + 24), a.readBigUInt64LE(z + 32)], [count, count]);
    assert.equal(a.readBigUInt64LE(z + 40) + a.readBigUInt64LE(z + 48), BigInt(z));
    assert.ok(unzipOk(out));
  }
});

test("0xFFFF entries: the end record's count is all ones and the zip64 record holds it", () => {
  const dir = tmpdir();
  const entries = Array.from({ length: 0xffff }, (_, i) => ({ key: `k/${i}`, bytes: "" }));
  const p = writeTmp(dir, "d.json", JSON.stringify({ page_size: 4096, entries }));
  const out = path.join(dir, "big.vzip");
  assert.equal(runCli(["write", p, out]).status, 0);
  const a = fs.readFileSync(out);
  const eocd = a.length - 60;
  assert.equal(a.readUInt32LE(eocd), 0x06054b50);
  assert.equal(a.readUInt16LE(eocd + 10), 0xffff);
  assert.equal(a.readUInt32LE(eocd - 20), 0x07064b50);
  assert.ok(unzipOk(out));
  const q = writeTmp(dir, "q.json", JSON.stringify([
    { op: "get", key: "k/65534" }, { op: "classify", key: "k/1" }, { op: "list", prefix: "k/6553" },
  ]));
  const res = JSON.parse(runCli(["read", out, q]).stdout);
  assert.deepEqual(res.results, [
    { ok: true, value: "" },
    { ok: true, kind: "bytes" },
    { ok: true, keys: ["k/6553", "k/65530", "k/65531", "k/65532", "k/65533", "k/65534"] },
  ]);
});
