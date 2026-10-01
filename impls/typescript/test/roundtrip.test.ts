import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { execFileSync } from "node:child_process";
import { Archive } from "../src/reader.ts";
import { writeArchive, type WriterInput } from "../src/writer.ts";
import { buildBuf, rng, src, tmpDir, writeTmp, parseZip } from "./helpers.ts";

const VZIP = path.join(import.meta.dirname, "..", "vzip");
const b = (s: string) => Buffer.from(s);

function unzipOk(p: string): void {
  execFileSync("unzip", ["-tq", p], { stdio: "pipe" });
}

test("round trip across layouts, kinds, sources and requests", async () => {
  const dir = tmpDir();
  const data = b("0123456789abcdefghij");
  writeTmp(dir, "data.bin", data);
  fs.mkdirSync(path.join(dir, "sub dir"));
  writeTmp(path.join(dir, "sub dir"), "é.bin", b("UNICODE"));
  const mtime = BigInt(Math.floor(fs.statSync(path.join(dir, "data.bin")).mtimeMs / 1000));
  const big = Buffer.alloc(5000, 7);
  for (const pageSize of [null, 1, 120, 100000]) {
    for (const mirror of [true, false]) {
      const input: WriterInput = {
        pageSize,
        mirror,
        sources: [
          src.url("data.bin", { size: 20n, modifiedNotAfter: mtime }),
          src.key("__vz__/hdr"),
          src.data(b("HDR!")),
          src.url("sub%20dir/%C3%A9.bin"),
          src.url("#self"),
          src.key("z/deflated"),
          src.url("does-not-exist.bin"),
        ],
        entries: [
          { key: "x/zarr.json", bytes: b("{}"), compress: true, pinned: pageSize !== null },
          { key: "__vz__/hdr", bytes: b("HIDDEN"), pinned: pageSize !== null },
          { key: "z/deflated", bytes: big, compress: true },
          { key: "x/c/0", ranges: [rng.src(0, 10, 5)] },
          { key: "x/c/1", ranges: [rng.src(2, 1, 3), rng.lit("--"), rng.src(1, 0, 2), rng.src(3, 0, 7)] },
          { key: "x/c/2", ranges: [] },
          { key: "x/c/3", ranges: [rng.lit("")] },
          { key: "x/c/4", ranges: [rng.src(4, 0, 4)] },
          { key: "x/c/5", ranges: [rng.src(5, 4990, 10), rng.src(6, 0, 0), rng.lit("tail")] },
          { key: "x/empty", bytes: new Uint8Array() },
          { key: "\u{1F600}", bytes: b("smile") },
          { key: "\u{FF5E}", bytes: b("tilde") },
          { key: "\uFEFFbom", bytes: b("bom") },
          { key: "a/../b", bytes: b("dots") },
          { key: "x/", bytes: b("slash") },
        ],
      };
      const p = path.join(dir, `a-${pageSize}-${mirror}.vzip`);
      writeArchive(input, p);
      unzipOk(p);
      const a = Archive.open(p);
      const tag = `page=${pageSize} mirror=${mirror}`;
      const get = async (k: string, r?: any) => {
        const v = await a.get(k, r);
        return v === null ? null : Buffer.from(v).toString("latin1");
      };
      assert.equal(a.classify("x/c/0"), "reference", tag);
      assert.equal(a.classify("x/zarr.json"), "bytes", tag);
      assert.equal(a.classify("__vz__/hdr"), "missing", tag);
      assert.equal(a.classify("nope"), "missing", tag);
      assert.equal(await get("x/c/0"), "abcde", tag);
      assert.equal(await get("x/c/1"), "DR!--HIUNICODE", tag);
      assert.equal(await get("x/c/1", { type: "range", start: 2n, end: 6n }), "!--H", tag);
      assert.equal(await get("x/c/1", { type: "range", start: 5n, end: 5n }), "", tag);
      assert.equal(await get("x/c/1", { type: "range", start: 100n, end: 200n }), "", tag);
      assert.equal(await get("x/c/1", { type: "offset", start: 7n }), "UNICODE", tag);
      assert.equal(await get("x/c/1", { type: "suffix", count: 3n }), "ODE", tag);
      assert.equal(await get("x/c/1", { type: "suffix", count: 300n }), "DR!--HIUNICODE", tag);
      assert.equal(await get("x/c/2"), "", tag);
      assert.equal(await get("x/c/3"), "", tag);
      assert.equal(await get("x/c/4"), "PK\x03\x04", tag);
      assert.equal(await get("x/c/5"), "\x07".repeat(10) + "tail", tag);
      assert.equal(await get("x/c/5", { type: "suffix", count: 4n }), "tail", tag);
      assert.equal(await get("x/zarr.json"), "{}", tag);
      assert.equal(await get("z/deflated", { type: "range", start: 1n, end: 3n }), "\x07\x07", tag);
      assert.equal(await get("x/empty"), "", tag);
      assert.equal(await get("__vz__/hdr"), null, tag);
      assert.equal(await get("missing"), null, tag);
      assert.equal(await get("\uFEFFbom"), "bom", tag);
      assert.equal(await get("bom"), null, tag);
      assert.equal(Buffer.from(a.raw("__vz__/hdr")!).toString(), "HIDDEN", tag);
      assert.equal(Buffer.from(a.raw("x/c/0")!).length, mirror ? 4 : 0, tag);
      assert.ok(a.raw("__vz__/sources")!.length > 0, tag);
      assert.equal(a.raw("__vz__/index") === null, pageSize === null, tag);
      assert.deepEqual(a.list(""), [
        "a/../b", "x/", "x/c/0", "x/c/1", "x/c/2", "x/c/3", "x/c/4", "x/c/5", "x/empty", "x/zarr.json", "z/deflated", "\uFEFFbom",
        "\u{FF5E}", "\u{1F600}",
      ], tag);
      assert.deepEqual(a.list("x/c/"), ["x/c/0", "x/c/1", "x/c/2", "x/c/3", "x/c/4", "x/c/5"], tag);
      assert.deepEqual(a.list("x/c/0"), ["x/c/0"], tag);
      assert.deepEqual(a.list("__vz__/"), [], tag);
      assert.deepEqual(a.list("q"), [], tag);
      assert.deepEqual(a.list("\u{FF5E}"), ["\u{FF5E}"], tag);
      a.close();
    }
  }
});

test("writer layout: canonical structure, sorted paged directory, comment form", () => {
  const buf = buildBuf({
    pageSize: 1,
    entries: [{ key: "b", bytes: b("1") }, { key: "a", bytes: b("2"), pinned: true }, { key: "c", ranges: [rng.lit("x")] }],
  });
  const p = parseZip(buf);
  assert.deepEqual(p.recs.map((r) => r.name.toString()), ["a", "b", "c", "__vz__/sources", "__vz__/index"]);
  assert.equal(p.comment.length, 38);
  assert.equal(p.comment.subarray(0, 6).toString(), "vzip/0");
  for (const r of p.recs) {
    assert.equal(r.fixed.readUInt16LE(8), 0x0800); // flags: UTF-8 only
  }
  // local headers have no extra field
  let off = 0;
  while (buf.readUInt32LE(off) === 0x04034b50) {
    assert.equal(buf.readUInt16LE(off + 28), 0);
    off += 30 + buf.readUInt16LE(off + 26) + buf.readUInt32LE(off + 18);
  }
  const unpaged = parseZip(buildBuf({ entries: [{ key: "a", bytes: b("1") }] }));
  assert.equal(unpaged.comment.length, 22);
});

test("many entries use ZIP64 end records; reader accepts them", async () => {
  const dir = tmpDir();
  const entries = Array.from({ length: 0x10000 }, (_, i) => ({ key: `k${i}`, bytes: new Uint8Array([i & 0xff]) }));
  for (const pageSize of [null, 4096]) {
    const p = path.join(dir, `many-${pageSize}.vzip`);
    writeArchive({ sources: [], entries, pageSize, mirror: true }, p);
    const buf = fs.readFileSync(p);
    const eocd = buf.length - 22 - (pageSize === null ? 22 : 38);
    assert.equal(buf.readUInt16LE(eocd + 10), 0xffff);
    assert.equal(buf.readUInt32LE(eocd - 20), 0x07064b50);
    const a = Archive.open(p);
    assert.equal(Buffer.from((await a.get("k65535"))!).toString("hex"), "ff");
    assert.equal(a.list("k6553").length, 7);
    a.close();
  }
});

test("CLI write + read", () => {
  const dir = tmpDir();
  writeTmp(dir, "data.bin", b("0123456789"));
  const desc = {
    page_size: null,
    sources: [{ url: "data.bin", size: 10 }, { key: "__vz__/hdr" }, { data: "48445221" }],
    entries: [
      { key: "x/zarr.json", bytes: "7b7d", compress: false, pinned: false },
      { key: "__vz__/hdr", bytes: "00112233" },
      { key: "x/c/0", ranges: [{ source: 0, offset: 2, length: 4 }] },
      { key: "x/c/1", ranges: [{ source: 2, offset: 0, length: 3 }, { data: "00ff" }, {}], compress: false },
      { key: "x/c/2", ranges: [{ source: 1, offset: 1, length: 2 }] },
    ],
    unknown_member: null,
  };
  const d = writeTmp(dir, "d.json", b(JSON.stringify(desc)));
  const out = path.join(dir, "o.vzip");
  execFileSync(VZIP, ["write", d, out], { cwd: "/" });
  unzipOk(out);
  const q = writeTmp(dir, "q.json", b(JSON.stringify([
    { op: "classify", key: "x/c/0" },
    { op: "get", key: "x/c/0" },
    { op: "get", key: "x/c/1" },
    { op: "get", key: "x/c/2", range: { suffix: 1 } },
    { op: "get", key: "x/c/0", range: { start: 3, end: 1 } },
    { op: "get_raw", key: "__vz__/hdr" },
    { op: "list", prefix: "x/" },
    { op: "get", key: "missing", range: { offset: 2 } },
  ])));
  const res = JSON.parse(execFileSync(VZIP, ["read", path.relative(dir, out), q], { cwd: dir }).toString());
  assert.deepEqual(res, {
    open: { ok: true },
    results: [
      { ok: true, kind: "reference" },
      { ok: true, value: Buffer.from("2345").toString("hex") },
      { ok: true, value: "48445200ff" },
      { ok: true, value: "22" },
      { ok: false, class: "request", error: "range start 3 > end 1" },
      { ok: true, value: "00112233" },
      { ok: true, keys: ["x/c/0", "x/c/1", "x/c/2", "x/zarr.json"] },
      { ok: true, value: null },
    ],
  });
});

test("CLI read: open failure is reported as JSON with exit 0", () => {
  const dir = tmpDir();
  const q = writeTmp(dir, "q.json", b(JSON.stringify([{ op: "list", prefix: "" }])));
  for (const target of [writeTmp(dir, "junk.vzip", b("not a zip at all, definitely not")), path.join(dir, "absent.vzip")]) {
    const res = JSON.parse(execFileSync(VZIP, ["read", target, q]).toString());
    assert.equal(res.open.ok, false);
    assert.equal(res.open.class, "archive");
    assert.deepEqual(res.results, []);
  }
});

const badQueries: Record<string, string> = {
  "not JSON": "[",
  "not an array": "{}",
  "unknown op": JSON.stringify([{ op: "delete", key: "a" }]),
  "missing key": JSON.stringify([{ op: "get" }]),
  "list without prefix": JSON.stringify([{ op: "list" }]),
  "range with several forms": JSON.stringify([{ op: "get", key: "a", range: { offset: 1, suffix: 2 } }]),
  "range with start only": JSON.stringify([{ op: "get", key: "a", range: { start: 1 } }]),
  "range with fractional number": '[{"op": "get", "key": "a", "range": {"suffix": 1.5}}]',
  "range with negative number": JSON.stringify([{ op: "get", key: "a", range: { offset: -1 } }]),
};
for (const [name, text] of Object.entries(badQueries)) {
  test(`CLI read: invalid queries file: ${name}`, () => {
    const dir = tmpDir();
    const archive = path.join(dir, "a.vzip");
    writeArchive({ sources: [], entries: [{ key: "a", bytes: b("x") }], pageSize: null, mirror: true }, archive);
    const q = writeTmp(dir, "q.json", b(text));
    assert.throws(() => execFileSync(VZIP, ["read", archive, q], { stdio: "pipe" }));
  });
}
