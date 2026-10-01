import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import { tmpdir, writeDesc } from "./helpers.ts";

const dir = tmpdir();
let n = 0;

function rejects(name: string, desc: unknown): void {
  test(`writer rejects: ${name}`, () => {
    const w = writeDesc(dir, desc, `bad${n++}.vzip`);
    assert.notEqual(w.status, 0, "expected non-zero exit");
    assert.ok(w.stderr.length > 0, "expected a message on stderr");
    assert.equal(fs.existsSync(w.out), false, "no file may be created");
  });
}

const big = "ab".repeat(70000);

// §9.1
rejects("empty key", { entries: [{ key: "", bytes: "" }] });
rejects("key with lone surrogate (not valid UTF-8)", '{"entries":[{"key":"a\\ud800","bytes":""}]}');
rejects("duplicate key", { entries: [{ key: "a", bytes: "" }, { key: "a", bytes: "00" }] });
rejects("format entry key __vz__/sources", { entries: [{ key: "__vz__/sources", bytes: "" }] });
rejects("format entry key __vz__/index", { entries: [{ key: "__vz__/index", bytes: "" }] });
rejects("source index out of range", { sources: [{ data: "00" }], entries: [{ key: "a", ranges: [{ source: 1, length: 1 }] }] });
rejects("source range with no sources ({} range)", { entries: [{ key: "a", ranges: [{}] }] });
rejects("empty url", { sources: [{ url: "" }] });
rejects("url not a URI-reference (space)", { sources: [{ url: "a b.bin" }] });
rejects("url not a URI-reference (non-ASCII)", { sources: [{ url: "é.bin" }] });
rejects("url not a URI-reference (colon in first segment)", { sources: [{ url: "1a:b" }] });
rejects("url not a URI-reference (bad percent)", { sources: [{ url: "a%2" }] });
rejects("key source names absent key", { sources: [{ key: "nope" }] });
rejects("key source names a reference entry", { sources: [{ key: "r" }, { data: "00" }], entries: [{ key: "r", ranges: [{ source: 1, length: 1 }] }] });
rejects("key source names a format entry", { sources: [{ key: "__vz__/sources" }] });
rejects("pin on key source", { sources: [{ key: "a", size: 1 }], entries: [{ key: "a", bytes: "00" }] });
rejects("pin on data source", { sources: [{ data: "00", etag: '"x"' }] });
rejects("weak etag", { sources: [{ url: "a", etag: 'W/"x"' }] });
rejects("etag without quotes", { sources: [{ url: "a", etag: "x" }] });
rejects("etag with space", { sources: [{ url: "a", etag: '"a b"' }] });
rejects("reference payload over 65519 bytes", { entries: [{ key: "a", ranges: [{ data: big }] }] });
rejects("key longer than 65535 bytes", { entries: [{ key: "k".repeat(65536), bytes: "" }] });
rejects("pinned reference entry", { page_size: 10, sources: [{ data: "00" }], entries: [{ key: "a", ranges: [{ source: 0, length: 1 }], pinned: true }] });
rejects("pinned without page index", { entries: [{ key: "a", bytes: "", pinned: true }] });

// HARNESS.md rules
rejects("compress on reference entry", { sources: [{ data: "00" }], entries: [{ key: "a", ranges: [{ length: 1 }], compress: true }] });
rejects("range mixing literal and source fields", { sources: [{ data: "00" }], entries: [{ key: "a", ranges: [{ data: "00", source: 0 }] }] });
rejects("entry with both bytes and ranges", { entries: [{ key: "a", bytes: "", ranges: [] }] });
rejects("entry with neither bytes nor ranges", { entries: [{ key: "a" }] });
rejects("source with two kinds", { sources: [{ url: "a", data: "00" }] });
rejects("source with no kind", { sources: [{}] });
rejects("page_size 0", { page_size: 0 });
rejects("page_size not an integer", { page_size: 1.5 });
rejects("page_size 1.0", '{"page_size": 1.0}');
rejects("offset 1.0", '{"sources":[{"data":"00"}],"entries":[{"key":"a","ranges":[{"offset":1.0}]}]}');
rejects("uppercase hex", { entries: [{ key: "a", bytes: "AB" }] });
rejects("odd-length hex", { entries: [{ key: "a", bytes: "abc" }] });
rejects("compress not boolean", { entries: [{ key: "a", bytes: "", compress: 1 }] });
rejects("mirror null", { mirror: null });
rejects("sources null", { sources: null });
rejects("compress null", { entries: [{ key: "a", bytes: "", compress: null }] });
rejects("negative offset", { sources: [{ data: "00" }], entries: [{ key: "a", ranges: [{ offset: -1 }] }] });
rejects("negative size pin", { sources: [{ url: "a", size: -1 }] });
rejects("invalid JSON", "{");

test("writer accepts unknown members, null unknowns, compress:false on references", () => {
  const w = writeDesc(dir, { extra: null, sources: [{ data: "00", foo: 1 }], entries: [{ key: "a", ranges: [{ length: 1 }], compress: false, x: null }] }, "ok.vzip");
  assert.equal(w.status, 0, w.stderr);
});
