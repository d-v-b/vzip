// One test per class of invalid description (spec §9.1 and HARNESS.md).
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { runCli, tmpdir, writeTmp } from "./helpers.ts";

const dir = tmpdir();
let n = 0;

function rejects(name: string, desc: string | object) {
  test(`writer rejects: ${name}`, () => {
    const p = writeTmp(dir, `d${n}.json`, typeof desc === "string" ? desc : JSON.stringify(desc));
    const out = path.join(dir, `o${n++}.vzip`);
    const r = runCli(["write", p, out]);
    assert.notEqual(r.status, 0);
    assert.ok(r.stderr.length > 0);
    assert.equal(fs.existsSync(out), false);
  });
}

const B = (key: string, bytes = "00") => ({ key, bytes });

rejects("empty key", { entries: [B("")] });
rejects("lone surrogate key (not UTF-8)", '{"entries":[{"key":"\\ud800","bytes":""}]}');
rejects("duplicate key", { entries: [B("a"), B("a")] });
rejects("format entry key sources", { entries: [B("__vz__/sources")] });
rejects("format entry key index", { entries: [B("__vz__/index")] });
rejects("source index out of bounds", { sources: [{ data: "00" }], entries: [{ key: "a", ranges: [{ source: 1 }] }] });
rejects("empty range object with no sources", { entries: [{ key: "a", ranges: [{}] }] });
rejects("empty url", { sources: [{ url: "" }] });
rejects("url not a URI-reference (space)", { sources: [{ url: "a b.bin" }] });
rejects("url not a URI-reference (non-ASCII)", { sources: [{ url: "é.bin" }] });
rejects("url not a URI-reference (colon in first segment)", { sources: [{ url: "1a:b" }] });
rejects("url not a URI-reference (bad percent)", { sources: [{ url: "a%zz" }] });
rejects("empty key source", { sources: [{ key: "" }] });
rejects("key source absent", { sources: [{ key: "nope" }] });
rejects("key source is a reference", {
  sources: [{ data: "00" }, { key: "r" }],
  entries: [{ key: "r", ranges: [{ source: 0, length: 1 }] }],
});
rejects("key source is a format entry", { sources: [{ key: "__vz__/sources" }] });
rejects("range past end of data source", { sources: [{ data: "0011" }], entries: [{ key: "a", ranges: [{ offset: 1, length: 2 }] }] });
rejects("range past end of key source", { sources: [{ key: "k" }], entries: [B("k", "00"), { key: "a", ranges: [{ length: 2 }] }] });
rejects("pin on key source", { sources: [{ key: "k", size: 1 }], entries: [B("k")] });
rejects("pin on data source", { sources: [{ data: "00", etag: '"x"' }] });
rejects("weak etag", { sources: [{ url: "a", etag: 'W/"x"' }] });
rejects("etag without quotes", { sources: [{ url: "a", etag: "x" }] });
rejects("payload too large", {
  sources: [{ data: "00" }],
  entries: [{ key: "a", ranges: [{ data: "00".repeat(65520) }] }],
});
rejects("key longer than 65535 bytes", { entries: [B("k".repeat(65536))] });
rejects("pinned reference entry", { page_size: 10, sources: [{ data: "00" }], entries: [{ key: "a", ranges: [], pinned: true }] });
rejects("pinned without page index", { entries: [{ key: "a", bytes: "", pinned: true }] });
rejects("compress on reference", { entries: [{ key: "a", ranges: [], compress: true }] });
rejects("both bytes and ranges", { entries: [{ key: "a", bytes: "", ranges: [] }] });
rejects("neither bytes nor ranges", { entries: [{ key: "a" }] });
rejects("mixed range", { sources: [{ data: "00" }], entries: [{ key: "a", ranges: [{ data: "00", source: 0 }] }] });
rejects("source with two kinds", { sources: [{ url: "a", data: "00" }] });
rejects("source with no kind", { sources: [{}] });
rejects("page_size 0", { page_size: 0 });
rejects("page_size float", '{"page_size": 1.0}');
rejects("page_size string", { page_size: "1" });
rejects("non-integer offset", '{"sources":[{"data":"00"}],"entries":[{"key":"a","ranges":[{"length":1.0}]}]}');
rejects("negative length", { sources: [{ data: "00" }], entries: [{ key: "a", ranges: [{ length: -1 }] }] });
rejects("uppercase hex", { entries: [B("a", "AB")] });
rejects("odd hex", { entries: [B("a", "abc")] });
rejects("null mirror", { mirror: null });
rejects("null entries", { entries: null });
rejects("null compress", { entries: [{ key: "a", bytes: "", compress: null }] });
rejects("string flag", { mirror: "true" });
rejects("duplicate member names", '{"mirror": true, "mirror": false}');
rejects("not JSON", "{");
rejects("negative size pin", { sources: [{ url: "a", size: -1 }] });

test("writer accepts compress:false on reference entries and unknown members", () => {
  const p = writeTmp(dir, "ok.json", JSON.stringify({ extra: null, entries: [{ key: "a", ranges: [], compress: false, foo: [1.5] }] }));
  const out = path.join(dir, "ok.vzip");
  const r = runCli(["write", p, out]);
  assert.equal(r.status, 0, r.stderr);
});
