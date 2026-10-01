import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import * as zlib from "node:zlib";
import { Archive } from "../src/reader.ts";
import { encodeCdIndex, encodeConcat, encodeRange, encodeSourceTable, type CdIndex, type Source } from "../src/proto.ts";
import { buildRaw, errClass, extraBlock, openBuf, tmpDir, type RawEntry, type RawOptions } from "./helpers.ts";

const dir = tmpDir();
fs.writeFileSync(path.join(dir, "ext.bin"), "0123456789");

const ref = (r: Parameters<typeof encodeRange>[0]) => extraBlock(0x7a76, Buffer.from(encodeRange(r)));
const st = (s: Source[]) => Buffer.from(encodeSourceTable(s));
const R = (source: number, offset: number, length: number) => ({ source, offset: BigInt(offset), length: BigInt(length) });

/** A paged archive whose index is computed from the records, with `mutate` applied. */
function paged(entries: RawEntry[], mutate: (idx: CdIndex, recs: { name: Buffer; offset: number; length: number }[]) => void, extra: RawOptions = {}): Buffer {
  return buildRaw(entries, {
    ...extra,
    index: (recs) => {
      const idx: CdIndex = {
        pages: recs.map((r) => ({ firstKey: r.name.toString("utf8"), offset: BigInt(r.offset), length: BigInt(r.length) })),
        pinned: [],
      };
      mutate(idx, recs);
      return Buffer.from(encodeCdIndex(idx));
    },
  });
}

// ------------------------------------------------------------------ archive errors

const archiveErrors: [string, () => Buffer][] = [
  ["empty file", () => Buffer.alloc(0)],
  ["plain zip without vzip comment", () => {
    const b = buildRaw([{ name: "a", body: Buffer.from("x") }]);
    return Buffer.concat([b.subarray(0, b.length - 24), Buffer.from([0, 0])]); // drop comment, length 0
  }],
  ["wrong magic", () => {
    const b = buildRaw([]);
    b.write("vzip/2", b.length - 22, "latin1");
    return b;
  }],
  ["zip64 indicated but no locator", () => buildRaw([], { eocdCount: 0xffff })],
  ["central directory outside the file", () => buildRaw([], { cdOffsetOverride: 100000 })],
  ["central directory does not parse (trailing garbage)", () => buildRaw([{ name: "a" }], { extraCd: Buffer.from([1, 2, 3]) })],
  ["central directory does not parse (bad signature)", () => buildRaw([{ name: "a" }], { extraCd: Buffer.alloc(46) })],
  ["sources body outside the file", () => {
    const c = Buffer.alloc(22);
    c.write("vzip/1");
    c.writeBigUInt64LE(10n ** 6n, 6);
    c.writeBigUInt64LE(2n, 14);
    return buildRaw([], { comment: c });
  }],
  ["sources body has trailing bytes", () => buildRaw([], { sourcesBody: Buffer.concat([zlib.deflateRawSync(Buffer.alloc(0)), Buffer.from([0])]) })],
  ["sources body truncated", () => buildRaw([], { sourcesBody: zlib.deflateRawSync(st([{ kind: "data", data: Buffer.alloc(100, 1) }])).subarray(0, 5) })],
  ["sources body is STORED, not DEFLATE", () => buildRaw([], { sourcesBody: st([{ kind: "data", data: Buffer.from("x") }]) })],
  ["source table malformed", () => buildRaw([], { sources: Buffer.from([0x0b]) })],
  ["source without kind", () => buildRaw([], { sources: Buffer.from([0x0a, 0x02, 0x20, 0x01]) })],
  ["empty url", () => buildRaw([], { sources: Buffer.from([0x0a, 0x02, 0x0a, 0x00]) })],
  ["pin on key source", () => buildRaw([], { sources: st([{ kind: "key", key: "a", size: 1n }]) })],
  ["pin on data source", () => buildRaw([], { sources: st([{ kind: "data", data: Buffer.from("a"), modifiedNotAfter: 1n }]) })],
  ["weak etag", () => buildRaw([], { sources: st([{ kind: "url", url: "x", etag: 'W/"a"' }]) })],
  ["etag without quotes", () => buildRaw([], { sources: st([{ kind: "url", url: "x", etag: "abc" }]) })],
  ["etag with inner quote", () => buildRaw([], { sources: st([{ kind: "url", url: "x", etag: '"a"b"' }]) })],
  ["__vz__/index record in an archive without a page index", () => buildRaw([{ name: "__vz__/index" }])],
  ["page index does not decode", () => buildRaw([{ name: "a" }], { index: () => Buffer.from([0x0f]) })],
  ["page index body has trailing bytes", () => buildRaw([{ name: "a" }], { indexBody: Buffer.concat([zlib.deflateRawSync(Buffer.alloc(0)), Buffer.from([0])]) })],
  ["page of length 0", () => paged([{ name: "a" }], (i) => { i.pages.push({ firstKey: "b", offset: i.pages[0].length, length: 0n }); })],
  ["page outside the central directory", () => paged([{ name: "a" }], (i) => { i.pages[0].length = 100000n; })],
  ["pages not contiguous from 0", () => paged([{ name: "a" }, { name: "b" }], (i) => { i.pages.shift(); })],
  ["first page not at offset 0", () => paged([{ name: "a" }], (i) => { i.pages[0].offset = 1n; i.pages[0].length -= 1n; })],
  ["first_key not strictly increasing", () => paged([{ name: "b" }, { name: "a" }], () => {})],
  ["first_key repeated", () => paged([{ name: "a" }, { name: "a\u0000" }], (i) => { i.pages[1].firstKey = "a"; })],
  ["pinned key listed twice", () => paged([{ name: "a" }], (i) => {
    i.pinned.push({ key: "a", dataOffset: 0n, size: 0n, csize: 0n, method: 0 }, { key: "a", dataOffset: 0n, size: 0n, csize: 0n, method: 0 });
  })],
  ["pinned format entry", () => paged([], (i) => { i.pinned.push({ key: "__vz__/sources", dataOffset: 0n, size: 0n, csize: 0n, method: 8 }); })],
  ["pinned method not 0 or 8", () => paged([], (i) => { i.pinned.push({ key: "p", dataOffset: 0n, size: 0n, csize: 0n, method: 9 }); })],
  ["pinned body outside the file", () => paged([], (i) => { i.pinned.push({ key: "p", dataOffset: 10n ** 9n, size: 1n, csize: 1n, method: 0 }); })],
];
for (const [name, build] of archiveErrors) {
  test(`archive error: ${name}`, async () => {
    assert.equal(await errClass(() => openBuf(dir, build())), "archive");
  });
}

test("archive error: file does not exist", async () => {
  assert.equal(await errClass(() => Archive.open(path.join(dir, "nope.vzip"))), "archive");
});

test("archive error: zip64 record with wrong size field", async () => {
  // Build a valid archive, then splice in zip64 end records with a bad size.
  const b = buildRaw([], { eocdCount: 0xffff });
  const eocdPos = b.length - 44;
  const cdOff = b.readUInt32LE(eocdPos + 16);
  const cdSize = b.readUInt32LE(eocdPos + 12);
  const z = Buffer.alloc(56);
  z.writeUInt32LE(0x06064b50, 0);
  z.writeBigUInt64LE(45n, 4);
  z.writeBigUInt64LE(1n, 24);
  z.writeBigUInt64LE(1n, 32);
  z.writeBigUInt64LE(BigInt(cdSize), 40);
  z.writeBigUInt64LE(BigInt(cdOff), 48);
  const loc = Buffer.alloc(20);
  loc.writeUInt32LE(0x07064b50, 0);
  loc.writeBigUInt64LE(BigInt(eocdPos), 8);
  loc.writeUInt32LE(1, 16);
  const out = Buffer.concat([b.subarray(0, eocdPos), z, loc, b.subarray(eocdPos)]);
  assert.equal(await errClass(() => openBuf(dir, out)), "archive");
  z.writeBigUInt64LE(44n, 4); // and with the right size it opens
  const good = Buffer.concat([b.subarray(0, eocdPos), z, loc, b.subarray(eocdPos)]);
  await (await openBuf(dir, good)).close();
  loc.writeBigUInt64LE(10n ** 9n, 8); // locator pointing outside the file
  const out2 = Buffer.concat([b.subarray(0, eocdPos), z, loc, b.subarray(eocdPos)]);
  assert.equal(await errClass(() => openBuf(dir, out2)), "archive");
});

// ------------------------------------------------------------------ per-key errors

type Op = "classify" | "get" | "raw" | "list";
async function run(a: Archive, op: Op, key: string): Promise<string> {
  return errClass(async () => {
    if (op === "classify") return a.classify(key);
    if (op === "get") return a.get(key);
    if (op === "raw") return a.raw(key);
    return a.list(key);
  });
}

const good: RawEntry = { name: "ok", body: Buffer.from("fine") };
const sources = st([
  { kind: "url", url: "ext.bin" },
  { kind: "key", key: "missing" },
  { kind: "key", key: "refent" },
  { kind: "key", key: "__vz__/sources" },
  { kind: "key", key: "badbody" },
  { kind: "url", url: "ext.bin", size: 11n },
  { kind: "url", url: "ext.bin", etag: '"x"' },
  { kind: "url", url: "ext.bin", modifiedNotAfter: 0n },
  { kind: "url", url: "nope.bin" },
  { kind: "url", url: "s3://bucket/x" },
  { kind: "url", url: "é.bin" },
  { kind: "url", url: "%2E%2E/ext.bin" },
  { kind: "url", url: "ext.bin?" },
  { kind: "url", url: "file://remote/ext.bin" },
  { kind: "key", key: "__vz__/hidden" },
  { kind: "key", key: "badextra" },
]);
const refEntry = (name: string, extra: Buffer): RawEntry => ({ name, extra, body: Buffer.alloc(0) });

const keyErrors: [string, Op[], RawEntry[], string][] = [
  ["entry: two reference blocks", ["classify", "get", "raw"], [refEntry("k", Buffer.concat([ref(R(0, 0, 1)), ref(R(0, 0, 1))]))], "entry"],
  ["entry: 0x7A76 and 0x7A77 blocks", ["classify", "get", "raw"], [refEntry("k", Buffer.concat([ref(R(0, 0, 1)), extraBlock(0x7a77, Buffer.alloc(0))]))], "entry"],
  ["entry: extra field does not parse", ["classify", "get", "raw"], [{ name: "k", extra: Buffer.from([1, 0, 5, 0, 0]) }], "entry"],
  ["entry: extra field with 3 trailing bytes", ["classify", "get", "raw"], [{ name: "k", extra: Buffer.from([1, 0, 0]) }], "entry"],
  ["entry: reference with method 8", ["classify", "get", "raw"], [{ name: "k", method: 8, body: zlib.deflateRawSync(Buffer.alloc(0)), usize: 0, extra: ref(R(0, 0, 1)) }], "entry"],
  ["entry: method 12", ["classify", "get", "raw"], [{ name: "k", method: 12 }], "entry"],
  ["entry: encrypted", ["classify", "get", "raw"], [{ name: "k", flags: 0x801 }], "entry"],
  ["entry: offset 0xFFFFFFFF without ZIP64 block", ["classify", "get", "raw"], [{ name: "k", lho: 0xffffffff }], "entry"],
  ["body: STORED sizes differ", ["get", "raw"], [{ name: "k", body: Buffer.from("abc"), usize: 2 }], "body"],
  ["body: DEFLATE corrupt", ["get", "raw"], [{ name: "k", method: 8, body: Buffer.from([0xff, 0xff]), usize: 1 }], "body"],
  ["body: DEFLATE trailing bytes", ["get", "raw"], [{ name: "k", method: 8, body: Buffer.concat([zlib.deflateRawSync(Buffer.from("a")), Buffer.from([0])]), usize: 1 }], "body"],
  ["body: DEFLATE wrong uncompressed size", ["get", "raw"], [{ name: "k", method: 8, body: zlib.deflateRawSync(Buffer.from("ab")), usize: 3 }], "body"],
  ["body: outside the file", ["get", "raw"], [{ name: "k", body: Buffer.from("abc"), lho: 1_000_000 }], "body"],
  ["payload: malformed", ["get"], [refEntry("k", extraBlock(0x7a76, Buffer.from([0x0b])))], "payload"],
  ["payload: literal with source", ["get"], [refEntry("k", extraBlock(0x7a76, Buffer.from([0x08, 0x01, 0x2a, 0x00])))], "payload"],
  ["payload: source out of bounds outside the window", ["get"], [refEntry("k", extraBlock(0x7a77, Buffer.from(encodeConcat([{ source: 0, offset: 0n, length: 0n, data: Buffer.from("x") }, R(99, 0, 0)]))))], "payload"],
  ["payload: Concat size above 2^64-1", ["get"], [refEntry("k", extraBlock(0x7a77, Buffer.from(encodeConcat([R(0, 0, 2 ** 53), { source: 0, offset: 0n, length: (1n << 64n) - 2n }]))))], "payload"],
  ["payload: bad part inside Concat", ["get"], [refEntry("k", extraBlock(0x7a77, Buffer.from([0x0a, 0x01, 0x0f])))], "payload"],
];
for (const [name, ops, entries, cls] of keyErrors) {
  test(`${name}`, async () => {
    const a = await openBuf(dir, buildRaw([good, ...entries], { sources }));
    for (const op of ops) assert.equal(await run(a, op, "k"), cls, op);
    assert.equal(await run(a, "list", ""), "ok"); // keys with entry errors are still listed
    assert.deepEqual(await a.get("ok"), Buffer.from("fine")); // other keys unaffected
    if (cls === "payload") assert.equal(await a.classify("k"), "reference");
    await a.close();
  });
}

const resolutionCases: [string, number, number, number][] = [
  ["external object shorter than the range", 0, 5, 6],
  ["key source missing", 1, 0, 1],
  ["key source is a reference entry", 2, 0, 1],
  ["key source is a format entry", 3, 0, 1],
  ["key source has a body error", 4, 0, 1],
  ["key source has an entry error", 15, 0, 1],
  ["size pin fails", 5, 0, 1],
  ["etag pin on file: cannot be checked", 6, 0, 1],
  ["modified_not_after pin fails", 7, 0, 1],
  ["url object does not exist", 8, 0, 1],
  ["unsupported scheme", 9, 0, 1],
  ["url not a valid URI reference", 10, 0, 1],
  ["file: url with encoded dot segment", 11, 0, 1],
  ["file: url with empty query", 12, 0, 1],
  ["file: url with remote authority", 13, 0, 1],
];
for (const [name, src, off, len] of resolutionCases) {
  test(`resolution: ${name}`, async () => {
    const entries: RawEntry[] = [
      refEntry("refent", ref(R(0, 0, 1))),
      { name: "badbody", body: Buffer.from("abc"), usize: 9 },
      { name: "badextra", extra: Buffer.from([1]) },
      { name: "__vz__/hidden", body: Buffer.from("H") },
      refEntry("k", extraBlock(0x7a77, Buffer.from(encodeConcat([{ source: 0, offset: 0n, length: 0n, data: Buffer.from("L") }, R(src, off, len)])))),
    ];
    const a = await openBuf(dir, buildRaw(entries, { sources }), `res-${src}.vzip`);
    assert.equal(await run(a, "get", "k"), "resolution");
    // the window that only covers the literal needs no resolution
    assert.deepEqual(await a.get("k", { type: "range", start: 0n, end: 1n }), Buffer.from("L"));
    assert.equal(await a.classify("k"), "reference");
    await a.close();
  });
}

test("resolution successes: hidden key source, fragment-only url, unreferenced bad sources", async () => {
  const entries: RawEntry[] = [
    { name: "__vz__/hidden", body: Buffer.from("H") },
    refEntry("k", extraBlock(0x7a77, Buffer.from(encodeConcat([R(14, 0, 1), R(0, 2, 3), R(8, 0, 0)])))),
  ];
  const a = await openBuf(dir, buildRaw(entries, { sources }), "res-ok.vzip");
  assert.deepEqual(await a.get("k"), Buffer.from("H234"));
  await a.close();
});

test("request error: start > end, checked before hidden/missing", async () => {
  const a = await openBuf(dir, buildRaw([good]));
  for (const k of ["ok", "missing", "__vz__/x"]) {
    assert.equal(await errClass(() => a.get(k, { type: "range", start: 2n, end: 1n })), "request", k);
  }
  await a.close();
});

test("request error: request beyond the documented memory bound", async () => {
  const p = path.join(dir, "big.vzip");
  fs.writeFileSync(p, buildRaw([{ name: "k", method: 8, body: zlib.deflateRawSync(Buffer.alloc(5000)), usize: 5000 }]));
  const a = await Archive.open(p, { maxRequestBytes: 1000 });
  assert.equal(await errClass(() => a.get("k")), "request");
  await a.close();
});

test("entry error: page cannot be parsed (only keys in that page and list are affected)", async () => {
  const b = paged([{ name: "a" }, { name: "b" }, { name: "c" }], () => {});
  // corrupt the signature of record "b" (second CD record)
  const eocd = b.length - 60;
  const cdOff = b.readUInt32LE(eocd + 16);
  const recA = 46 + 1;
  b.writeUInt32LE(0, cdOff + recA);
  const a = await openBuf(dir, b);
  assert.equal(await run(a, "classify", "b"), "entry");
  assert.equal(await run(a, "classify", "bz"), "entry"); // same page
  assert.equal(await a.classify("a"), "bytes");
  assert.equal(await a.classify("c"), "bytes");
  assert.equal(await run(a, "list", ""), "entry");
  assert.equal(await run(a, "list", "b"), "entry");
  assert.deepEqual(await a.list("c"), ["c"]);
  assert.deepEqual(await a.list("a"), ["a"]);
  await a.close();
});
