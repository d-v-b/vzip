// One test per invalid description (spec §9.1, HARNESS.md `write`).
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { spawnSync } from "node:child_process";
import { tmpDir, writeTmp } from "./helpers.ts";

const VZIP = path.join(import.meta.dirname, "..", "vzip");
const dir = tmpDir();
let n = 0;

function runWrite(descText: string): { status: number | null; out: string; stderr: string } {
  const d = writeTmp(dir, `d${n}.json`, Buffer.from(descText));
  const out = path.join(dir, `o${n++}.vzip`);
  const r = spawnSync(VZIP, ["write", d, out]);
  return { status: r.status, out, stderr: r.stderr.toString() };
}

const ent = (e: object) => ({ entries: [e] });
const bigKey = "k".repeat(65536);
const invalid: Record<string, string> = {
  "not JSON": "{",
  "top-level array": "[]",
  "page_size 0": JSON.stringify({ page_size: 0 }),
  "page_size negative": JSON.stringify({ page_size: -5 }),
  "page_size 1.0": '{"page_size": 1.0}',
  "page_size 1e3": '{"page_size": 1e3}',
  "page_size string": JSON.stringify({ page_size: "10" }),
  "mirror null": JSON.stringify({ mirror: null }),
  "mirror string": JSON.stringify({ mirror: "yes" }),
  "sources null": JSON.stringify({ sources: null }),
  "entries not array": JSON.stringify({ entries: {} }),
  "source with two kinds": JSON.stringify({ sources: [{ url: "a", data: "00" }] }),
  "source with no kind": JSON.stringify({ sources: [{ size: 1 }] }),
  "source url null": JSON.stringify({ sources: [{ url: null }] }),
  "empty url": JSON.stringify({ sources: [{ url: "" }] }),
  "url with space": JSON.stringify({ sources: [{ url: "a b.bin" }] }),
  "url non-ASCII": JSON.stringify({ sources: [{ url: "é.bin" }] }),
  "url bad percent": JSON.stringify({ sources: [{ url: "a%zz" }] }),
  "pin on key source": JSON.stringify({ sources: [{ key: "k", size: 1 }], entries: [{ key: "k", bytes: "00" }] }),
  "pin on data source": JSON.stringify({ sources: [{ data: "00", etag: '"a"' }] }),
  "weak etag": JSON.stringify({ sources: [{ url: "a", etag: 'W/"a"' }] }),
  "unquoted etag": JSON.stringify({ sources: [{ url: "a", etag: "a" }] }),
  "etag with space": JSON.stringify({ sources: [{ url: "a", etag: '"a b"' }] }),
  "negative size pin": JSON.stringify({ sources: [{ url: "a", size: -1 }] }),
  "fractional modified_not_after": '{"sources": [{"url": "a", "modified_not_after": 1.5}]}',
  "key source absent": JSON.stringify({ sources: [{ key: "nope" }] }),
  "key source names reference": JSON.stringify({ sources: [{ key: "r" }], entries: [{ key: "r", ranges: [] }] }),
  "key source names format entry": JSON.stringify({ sources: [{ key: "__vz__/sources" }] }),
  "data uppercase hex": JSON.stringify({ sources: [{ data: "AB" }] }),
  "data odd hex": JSON.stringify({ sources: [{ data: "abc" }] }),
  "entry without key": JSON.stringify(ent({ bytes: "00" })),
  "empty key": JSON.stringify(ent({ key: "", bytes: "00" })),
  "lone surrogate key": '{"entries": [{"key": "\\ud800", "bytes": "00"}]}',
  "key longer than 65535 bytes": JSON.stringify(ent({ key: bigKey, bytes: "" })),
  "duplicate key": JSON.stringify({ entries: [{ key: "a", bytes: "" }, { key: "a", bytes: "00" }] }),
  "__vz__/sources key": JSON.stringify(ent({ key: "__vz__/sources", bytes: "" })),
  "__vz__/index key": JSON.stringify(ent({ key: "__vz__/index", bytes: "" })),
  "both bytes and ranges": JSON.stringify(ent({ key: "a", bytes: "", ranges: [] })),
  "neither bytes nor ranges": JSON.stringify(ent({ key: "a" })),
  "bytes null": JSON.stringify(ent({ key: "a", bytes: null })),
  "compress on reference": JSON.stringify(ent({ key: "a", ranges: [], compress: true })),
  "pinned on reference": JSON.stringify({ page_size: 10, ...ent({ key: "a", ranges: [], pinned: true }) }),
  "pinned without page index": JSON.stringify(ent({ key: "a", bytes: "", pinned: true })),
  "compress null": JSON.stringify(ent({ key: "a", bytes: "", compress: null })),
  "compress 1": JSON.stringify(ent({ key: "a", bytes: "", compress: 1 })),
  "range mixes data and source": JSON.stringify({ sources: [{ data: "00" }], ...ent({ key: "a", ranges: [{ data: "00", length: 1 }] }) }),
  "empty range object with no sources": JSON.stringify(ent({ key: "a", ranges: [{}] })),
  "source index out of range": JSON.stringify({ sources: [{ data: "00" }], ...ent({ key: "a", ranges: [{ source: 1 }] }) }),
  "negative offset": JSON.stringify({ sources: [{ data: "00" }], ...ent({ key: "a", ranges: [{ offset: -1 }] }) }),
  "range offset null": JSON.stringify({ sources: [{ data: "00" }], ...ent({ key: "a", ranges: [{ offset: null }] }) }),
  "payload over 65519 bytes": JSON.stringify(ent({ key: "a", ranges: [{ data: "00".repeat(65520) }] })),
};

for (const [name, text] of Object.entries(invalid)) {
  test(`invalid description: ${name}`, () => {
    const r = runWrite(text);
    assert.notEqual(r.status, 0, r.stderr);
    assert.ok(r.stderr.length > 0);
    assert.equal(fs.existsSync(r.out), false);
  });
}

test("valid descriptions: defaults, unknown members, compress:false on references, hidden pinned, 65519-byte payload", () => {
  const ok = [
    "{}",
    JSON.stringify({ whatever: null, entries: [{ key: "a", ranges: [], compress: false, extra: [1.5] }] }),
    JSON.stringify({ page_size: 1, entries: [{ key: "__vz__/meta", bytes: "00", pinned: true }] }),
    JSON.stringify({ sources: [{ url: "x", modified_not_after: -100, size: 0, etag: '""' }], entries: [{ key: "a", ranges: [{}] }] }),
    // Range{data} payload: tag(1) + len varint(3) + data => 65519 total with 65515 data bytes
    JSON.stringify({ entries: [{ key: "a", ranges: [{ data: "00".repeat(65515) }] }] }),
  ];
  for (const t of ok) {
    const r = runWrite(t);
    assert.equal(r.status, 0, `${t.slice(0, 80)}: ${r.stderr}`);
    const u = spawnSync("unzip", ["-tq", r.out]);
    assert.equal(u.status, 0, u.stdout.toString());
  }
});
