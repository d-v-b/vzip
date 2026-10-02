// Reader error cases (spec §8). One test per error case.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";
import { Archive, type Request } from "../src/reader.ts";
import { VzError } from "../src/errors.ts";
import { encodeCdIndex, encodeConcat, encodeRange, type SourceMsg } from "../src/proto.ts";
import { extraBlock, rawZip, runCli, tmpdir, writeTmp, type RawEntry } from "./helpers.ts";

const dir = tmpdir();
let n = 0;
const WHOLE: Request = { kind: "whole" };

function save(buf: Buffer): string {
  return writeTmp(dir, `a${n++}.vzip`, buf);
}
function open(buf: Buffer): Archive {
  return Archive.open(save(buf));
}
const url = (u: string, pins: Partial<SourceMsg> = {}): SourceMsg => ({
  kind: "url", url: u, size: null, etag: null, modifiedNotAfter: null, ...pins,
});
const data = (d: Buffer): SourceMsg => ({ kind: "data", data: d, size: null, etag: null, modifiedNotAfter: null });
const keySrc = (k: string): SourceMsg => ({ kind: "key", key: k, size: null, etag: null, modifiedNotAfter: null });
const ref1 = (name: string, r: Parameters<typeof encodeRange>[0], extra: Partial<RawEntry> = {}): RawEntry => {
  const p = encodeRange(r);
  return { name, body: p, extra: extraBlock(0x7a76, p), ...extra };
};
const R = (source: number, offset: number, length: number) => ({
  source: BigInt(source), offset: BigInt(offset), length: BigInt(length), data: null,
});

async function errClass(f: () => unknown): Promise<string> {
  try {
    await f();
  } catch (e) {
    if (e instanceof VzError) return e.cls;
    throw e;
  }
  return "none";
}

function assertArchiveErr(buf: Buffer) {
  assert.throws(() => open(buf), (e: unknown) => e instanceof VzError && e.cls === "archive");
}

// ---------------------------------------------------------------- archive errors

test("archive error: not a zip", () => assertArchiveErr(Buffer.from("hello world, this is not a zip file at all!!!!!!!!!!!!")));
test("archive error: empty file", () => assertArchiveErr(Buffer.alloc(0)));
test("archive error: bad magic", () => assertArchiveErr(rawZip({ entries: [], magic: "vzop/0" })));
test("archive error: unsupported version", () => assertArchiveErr(rawZip({ entries: [], magic: "vzip/1" })));
test("archive error: plain zip comment of 22 bytes without magic", () =>
  assertArchiveErr(rawZip({ entries: [], magic: "abcdef" })));
test("archive error: sources body with trailing bytes", () => {
  const body = Buffer.concat([zlib.deflateRawSync(Buffer.alloc(0)), Buffer.from([0])]);
  assertArchiveErr(rawZip({ entries: [], sourcesBody: body }));
});
test("archive error: sources body truncated", () => {
  const body = zlib.deflateRawSync(Buffer.from("0a020801", "hex"));
  assertArchiveErr(rawZip({ entries: [], sourcesBody: body.subarray(0, body.length - 1) }));
});
test("archive error: source table malformed (wire type 3)", () =>
  assertArchiveErr(rawZip({ entries: [], sources: Buffer.from([0x0b]) })));
test("archive error: source with no kind", () => assertArchiveErr(rawZip({ entries: [], sources: Buffer.from("0a00", "hex") })));
test("archive error: empty url", () => assertArchiveErr(rawZip({ entries: [], sources: [url("")] })));
test("archive error: empty key source", () => assertArchiveErr(rawZip({ entries: [], sources: [keySrc("")] })));
test("archive error: pin on data source", () =>
  assertArchiveErr(rawZip({ entries: [], sources: [{ ...data(Buffer.alloc(1)), size: 1n }] })));
test("archive error: weak etag pin", () => assertArchiveErr(rawZip({ entries: [], sources: [url("a", { etag: 'W/"x"' })] })));
test("archive error: invalid UTF-8 url string", () =>
  assertArchiveErr(rawZip({ entries: [], sources: Buffer.from("0a030a01ff", "hex") })));
test("archive error: uint32 overflow is fine in sources but size pin as LEN is not", () =>
  assertArchiveErr(rawZip({ entries: [], sources: Buffer.from("0a050a01612200", "hex") })));
test("archive error: unpaged archive with __vz__/index record", () =>
  assertArchiveErr(rawZip({ entries: [{ name: "__vz__/index", body: Buffer.alloc(0) }] })));
test("archive error: central directory does not parse", () =>
  assertArchiveErr(rawZip({ entries: [{ name: "a" }], cdHook: (cd) => { const c = Buffer.from(cd); c[0] = 0; return c; } })));
// offset of the zip64 end of central directory record in a rawZip() archive with a 22-byte comment
const z64At = (b: Buffer): number => b.length - 44 - 20 - 56;
test("archive error: central directory outside the file", () => {
  const b = rawZip({ entries: [] });
  b.writeBigUInt64LE(0xfffffff0n, z64At(b) + 48);
  assertArchiveErr(b);
});
test("archive error: no zip64 end records", () => {
  assertArchiveErr(rawZip({ entries: [], zip64: false }));
});
test("archive error: revision-8 archive (actual values in the end record, no zip64 records)", () => {
  const b = rawZip({ entries: [{ name: "a" }] });
  const z = z64At(b);
  const fields = [24, 32, 40, 48].map((o) => Number(b.readBigUInt64LE(z + o))) as [number, number, number, number];
  assertArchiveErr(rawZip({ entries: [{ name: "a" }], zip64: false, eocdFields: fields }));
});
test("archive error: zip64 record with a wrong signature", () => {
  const b = rawZip({ entries: [] });
  b.writeUInt32LE(0x06064b51, z64At(b));
  assertArchiveErr(b);
});
test("archive error: zip64 record whose size field is not 44", () => {
  const b = rawZip({ entries: [] });
  b.writeBigUInt64LE(45n, z64At(b) + 4);
  assertArchiveErr(b);
});
test("archive error: zip64 locator points outside the file", () => {
  const b = rawZip({ entries: [] });
  b.writeBigUInt64LE(BigInt(b.length), z64At(b) + 56 + 8);
  assertArchiveErr(b);
});
test("the end record's counts, size and offset are ignored", async () => {
  for (const eocdFields of [[0, 0, 0, 0], [7, 7, 12345, 99999]] as [number, number, number, number][]) {
    const a = open(rawZip({ entries: [{ name: "k", body: Buffer.from("hi") }], eocdFields }));
    assert.deepEqual(await a.get("k", WHOLE), Buffer.from("hi"));
    assert.deepEqual(a.list(""), ["k"]);
  }
});
test("archive error: page index with zero-length page", () =>
  assertArchiveErr(rawZip({ entries: [], index: encodeCdIndex({ pages: [{ firstKey: "a", offset: 0n, length: 0n }], pinned: [] }) })));
test("archive error: page index pages not contiguous", () =>
  assertArchiveErr(rawZip({ entries: [{ name: "a" }], index: encodeCdIndex({ pages: [{ firstKey: "a", offset: 1n, length: 10n }], pinned: [] }) })));
test("archive error: page outside central directory", () =>
  assertArchiveErr(rawZip({ entries: [{ name: "a" }], index: encodeCdIndex({ pages: [{ firstKey: "a", offset: 0n, length: 100000n }], pinned: [] }) })));
test("archive error: first_key not increasing", () =>
  assertArchiveErr(rawZip({
    entries: [{ name: "a" }, { name: "b" }],
    index: encodeCdIndex({ pages: [{ firstKey: "b", offset: 0n, length: 47n }, { firstKey: "a", offset: 47n, length: 47n }], pinned: [] }),
  })));
test("archive error: pinned key twice", () => {
  const p = { key: "a", dataOffset: 0n, size: 0n, csize: 0n, method: 0n };
  assertArchiveErr(rawZip({ entries: [], index: encodeCdIndex({ pages: [], pinned: [p, p] }) }));
});
test("archive error: pinned format entry", () =>
  assertArchiveErr(rawZip({ entries: [], index: encodeCdIndex({ pages: [], pinned: [{ key: "__vz__/sources", dataOffset: 0n, size: 0n, csize: 0n, method: 0n }] }) })));
test("archive error: pinned method 9", () =>
  assertArchiveErr(rawZip({ entries: [], index: encodeCdIndex({ pages: [], pinned: [{ key: "a", dataOffset: 0n, size: 0n, csize: 0n, method: 9n }] }) })));
test("archive error: pinned body outside file", () =>
  assertArchiveErr(rawZip({ entries: [], index: encodeCdIndex({ pages: [], pinned: [{ key: "a", dataOffset: 0n, size: 5n, csize: 1000000n, method: 0n }] }) })));
test("archive error: empty pinned key", () =>
  assertArchiveErr(rawZip({ entries: [], index: encodeCdIndex({ pages: [], pinned: [{ key: "", dataOffset: 0n, size: 0n, csize: 0n, method: 0n }] }) })));
test("archive error via CLI: open failure is reported as JSON with exit 0", () => {
  const p = save(Buffer.from("nope"));
  const q = writeTmp(dir, "q-open.json", "[]");
  const r = runCli(["read", p, q]);
  assert.equal(r.status, 0);
  const j = JSON.parse(r.stdout);
  assert.equal(j.open.ok, false);
  assert.equal(j.open.class, "archive");
  assert.deepEqual(j.results, []);
});

// ---------------------------------------------------------------- entry errors

async function entryErrorFor(e: RawEntry) {
  const a = open(rawZip({ entries: [e, { name: "ok", body: Buffer.from("x") }], sources: [data(Buffer.alloc(4))] }));
  assert.equal(await errClass(() => a.classify("bad")), "entry");
  assert.equal(await errClass(() => a.get("bad", WHOLE)), "entry");
  assert.equal(await errClass(() => a.raw("bad")), "entry");
  assert.deepEqual(await a.get("ok", WHOLE), Buffer.from("x")); // others unaffected
  assert.ok(a.list("").includes("bad")); // entry errors are listed
}

test("entry error: unknown method", () => entryErrorFor({ name: "bad", method: 99 }));
test("entry error: encrypted", () => entryErrorFor({ name: "bad", flags: 0x801 }));
test("entry error: extra field does not parse", () => entryErrorFor({ name: "bad", extra: Buffer.from([1, 0, 9]) }));
test("entry error: two reference blocks", () =>
  entryErrorFor({ name: "bad", extra: Buffer.concat([extraBlock(0x7a76, Buffer.alloc(0)), extraBlock(0x7a77, Buffer.alloc(0))]) }));
test("entry error: reference with method 8", () => entryErrorFor({ name: "bad", method: 8, body: zlib.deflateRawSync(Buffer.alloc(0)), extra: extraBlock(0x7a76, Buffer.alloc(0)) }));
test("entry error: exactly one size field 0xFFFFFFFF", () =>
  entryErrorFor({ name: "bad", usize: 0xffffffff, extra: extraBlock(1, Buffer.alloc(16)) }));
test("entry error: large entry without zip64 block", () => entryErrorFor({ name: "bad", usize: 0xffffffff, csize: 0xffffffff }));
test("entry error: large entry with zip64 block shorter than 16 bytes", () =>
  entryErrorFor({ name: "bad", usize: 0xffffffff, csize: 0xffffffff, extra: extraBlock(1, Buffer.alloc(15)) }));
test("entry error: large entry with offset 0xFFFFFFFF and zip64 block without the offset", () =>
  entryErrorFor({ name: "bad", usize: 0xffffffff, csize: 0xffffffff, lho: 0xffffffff, extra: extraBlock(1, Buffer.alloc(16)) }));
test("entry error: large reference entry", () =>
  entryErrorFor({
    name: "bad", usize: 0xffffffff, csize: 0xffffffff,
    extra: Buffer.concat([extraBlock(1, u64s(0x100000000n, 0x100000000n)), extraBlock(0x7a76, Buffer.alloc(0))]),
  }));
test("entry error: large entry with method 8", () =>
  entryErrorFor({
    name: "bad", body: zlib.deflateRawSync(Buffer.from("hello")), method: 8, usize: 0xffffffff, csize: 0xffffffff,
    extra: extraBlock(1, u64s(0x100000000n, 7n)),
  }));
test("entry error: offset 0xFFFFFFFF without zip64 block", () => entryErrorFor({ name: "bad", lho: 0xffffffff }));
test("entry error: offset 0xFFFFFFFF with short zip64 block", () =>
  entryErrorFor({ name: "bad", lho: 0xffffffff, extra: extraBlock(1, Buffer.alloc(4)) }));
test("entry error: two zip64 blocks", () =>
  entryErrorFor({ name: "bad", extra: Buffer.concat([extraBlock(1, Buffer.alloc(8)), extraBlock(1, Buffer.alloc(8))]) }));

function u64s(...vs: bigint[]): Buffer {
  const b = Buffer.alloc(8 * vs.length);
  vs.forEach((v, i) => b.writeBigUInt64LE(v, 8 * i));
  return b;
}

test("zip64 extra block decoding (§3.2): sizes, offset, both, ignored blocks and trailing bytes", async () => {
  // Sizes in the block are taken as given, so small bodies stand in for large ones here;
  // the local header of a large entry carries the 20-byte ZIP64 field (§3.1 rule 4).
  const body = Buffer.from("hello");
  const lx = extraBlock(1, u64s(5n, 5n));
  const ALL = 0xffffffff;
  const entries: RawEntry[] = [
    // offset only (the first entry is at offset 0)
    { name: "off", body: Buffer.from("hi"), lho: ALL, extra: extraBlock(1, u64s(0n)) },
    // sizes only, with trailing bytes after them
    { name: "sizes", body, usize: ALL, csize: ALL, localExtra: lx, extra: extraBlock(1, Buffer.concat([u64s(5n, 5n), Buffer.alloc(3)])) },
    // a block on a record that needs nothing is ignored
    { name: "ignored", body: Buffer.from("x"), extra: extraBlock(1, u64s(99n)) },
  ];
  const a = open(rawZip({ entries }));
  assert.deepEqual(await a.get("off", WHOLE), Buffer.from("hi"));
  assert.deepEqual(await a.get("sizes", WHOLE), body);
  assert.deepEqual(await a.get("sizes", { kind: "range", start: 1n, end: 3n }), Buffer.from("el"));
  assert.deepEqual(await a.raw("sizes"), body);
  assert.deepEqual(await a.get("ignored", WHOLE), Buffer.from("x"));

  // sizes and offset: the offset follows the sizes; a second archive whose large entry is first
  const b = open(rawZip({
    entries: [{ name: "both", body, usize: ALL, csize: ALL, lho: ALL, localExtra: lx, extra: extraBlock(1, u64s(5n, 5n, 0n)) }],
  }));
  assert.deepEqual(await b.get("both", WHOLE), body);
});

test("entry error: unparseable page; key reported as entry error even if not in page", async () => {
  // two pages; corrupt the signature of the second
  const a = open(rawZip({
    entries: [{ name: "a" }, { name: "b" }],
    index: encodeCdIndex({ pages: [{ firstKey: "a", offset: 0n, length: 47n }, { firstKey: "b", offset: 47n, length: 47n }], pinned: [] }),
    cdHook: (cd) => { const c = Buffer.from(cd); c[47] = 0; return c; },
  }));
  assert.equal(a.classify("a"), "bytes");
  assert.equal(await errClass(() => a.classify("b")), "entry");
  assert.equal(await errClass(() => a.classify("c")), "entry");
  assert.equal(a.classify("0"), "missing"); // before the first page
  assert.equal(await errClass(() => a.list("")), "entry");
  assert.deepEqual(a.list("a"), ["a"]); // second page not read for prefix "a"
});

// ---------------------------------------------------------------- body errors

test("body error: STORED sizes differ", async () => {
  const a = open(rawZip({ entries: [{ name: "k", body: Buffer.from("abc"), usize: 2 }] }));
  assert.equal(await errClass(() => a.get("k", { kind: "range", start: 0n, end: 1n })), "body");
  assert.equal(await errClass(() => a.raw("k")), "body");
});
test("body error: DEFLATE with trailing bytes", async () => {
  const body = Buffer.concat([zlib.deflateRawSync(Buffer.from("abc")), Buffer.from([0])]);
  const a = open(rawZip({ entries: [{ name: "k", body, method: 8, usize: 3 }] }));
  assert.equal(await errClass(() => a.get("k", { kind: "range", start: 0n, end: 1n })), "body");
});
test("body error: DEFLATE inflates to wrong size", async () => {
  const a = open(rawZip({ entries: [{ name: "k", body: zlib.deflateRawSync(Buffer.from("abc")), method: 8, usize: 4 }] }));
  assert.equal(await errClass(() => a.get("k", WHOLE)), "body");
});
test("body error: body outside file", async () => {
  const a = open(rawZip({ entries: [{ name: "k", body: Buffer.from("abc"), csize: 100000, usize: 100000 }] }));
  assert.equal(await errClass(() => a.get("k", { kind: "range", start: 0n, end: 1n })), "body");
});
test("body error on a key source becomes a resolution error", async () => {
  const a = open(rawZip({
    entries: [{ name: "__vz__/h", body: Buffer.from("abc"), usize: 2 }, ref1("r", R(0, 0, 1))],
    sources: [keySrc("__vz__/h")],
  }));
  assert.equal(await errClass(() => a.get("r", WHOLE)), "resolution");
  assert.equal(await errClass(() => a.raw("__vz__/h")), "body");
});
test("body error: raw of a reference entry with mismatched sizes", async () => {
  const a = open(rawZip({ entries: [ref1("r", R(0, 0, 1), { usize: 0 })], sources: [data(Buffer.alloc(1))] }));
  assert.equal(await errClass(() => a.raw("r")), "body");
  assert.deepEqual(await a.get("r", WHOLE), Buffer.alloc(1)); // get ignores the body
});

// ---------------------------------------------------------------- payload errors

async function payloadErr(extra: Buffer, sources: SourceMsg[] = [data(Buffer.alloc(4))]) {
  const a = open(rawZip({ entries: [{ name: "r", extra }], sources }));
  assert.equal(a.classify("r"), "reference");
  assert.equal(await errClass(() => a.get("r", { kind: "range", start: 0n, end: 0n })), "payload");
}
test("payload error: source index out of bounds (even outside the window)", () =>
  payloadErr(extraBlock(0x7a77, encodeConcat([R(0, 0, 1), R(5, 0, 0)]))));
test("payload error: literal range with non-zero offset", () =>
  payloadErr(extraBlock(0x7a76, Buffer.concat([encodeRange({ ...R(0, 1, 0), data: Buffer.alloc(0) })]))));
test("payload error: wire type 3 in unknown field", () => payloadErr(extraBlock(0x7a76, Buffer.from([0x33]))));
test("payload error: known field with wrong wire type", () => payloadErr(extraBlock(0x7a76, Buffer.from([0x1a, 0x00]))));
test("payload error: uint32 source overflow", () =>
  payloadErr(extraBlock(0x7a76, Buffer.from([0x08, 0x80, 0x80, 0x80, 0x80, 0x10]))));
test("payload error: varint longer than 10 bytes", () =>
  payloadErr(extraBlock(0x7a76, Buffer.from([0x18, ...Array(10).fill(0x80), 0x01]))));
test("payload error: varint exceeds 2^64-1", () =>
  payloadErr(extraBlock(0x7a76, Buffer.from([0x18, ...Array(9).fill(0xff), 0x02]))));
test("payload error: field number 0", () => payloadErr(extraBlock(0x7a76, Buffer.from([0x00, 0x00]))));
test("payload error: truncated LEN", () => payloadErr(extraBlock(0x7a76, Buffer.from([0x2a, 0x05, 0x00]))));
test("payload error: offset + length overflow", () =>
  payloadErr(extraBlock(0x7a76, encodeRange({ source: 0n, offset: (1n << 64n) - 1n, length: 1n, data: null }))));
test("payload error: total size overflow", () =>
  payloadErr(extraBlock(0x7a77, encodeConcat([
    { source: 0n, offset: 0n, length: (1n << 63n), data: null },
    { source: 0n, offset: 0n, length: (1n << 63n), data: null },
  ]))));
test("payload error: payload longer than 65519 bytes", () =>
  payloadErr(extraBlock(0x7a76, encodeRange({ source: 0n, offset: 0n, length: 0n, data: Buffer.alloc(65520) }))));

test("payload: reserved field 2, unknown fields and non-minimal varints are accepted", async () => {
  const payload = Buffer.from([0x10, 0x05, 0x18, 0x81, 0x00, 0x20, 0x02, 0x3a, 0x00, 0x45, 1, 2, 3, 4]);
  const a = open(rawZip({ entries: [{ name: "r", extra: extraBlock(0x7a76, payload) }], sources: [data(Buffer.from("abcd"))] }));
  assert.deepEqual(await a.get("r", WHOLE), Buffer.from("bc"));
});

// ---------------------------------------------------------------- resolution errors

test("resolution error: missing file, but other ranges and windows still work", async () => {
  const a = open(rawZip({ entries: [{ name: "r", extra: extraBlock(0x7a77, encodeConcat([R(0, 0, 2), R(1, 0, 2)])) }], sources: [data(Buffer.from("ab")), url("nonexistent.bin")] }));
  assert.equal(await errClass(() => a.get("r", WHOLE)), "resolution");
  assert.deepEqual(await a.get("r", { kind: "range", start: 0n, end: 2n }), Buffer.from("ab"));
});
test("resolution error: data source too short", async () => {
  const a = open(rawZip({ entries: [ref1("r", R(0, 1, 4))], sources: [data(Buffer.from("ab"))] }));
  assert.equal(await errClass(() => a.get("r", WHOLE)), "resolution");
  assert.deepEqual(await a.get("r", { kind: "range", start: 0n, end: 1n }), Buffer.from("b"));
});
test("resolution error: key source names a reference", async () => {
  const a = open(rawZip({ entries: [ref1("r", R(0, 0, 1)), ref1("s", R(1, 0, 1))], sources: [data(Buffer.from("a")), keySrc("r")] }));
  assert.equal(await errClass(() => a.get("s", WHOLE)), "resolution");
});
test("resolution error: key source missing", async () => {
  const a = open(rawZip({ entries: [ref1("s", R(0, 0, 1))], sources: [keySrc("nope")] }));
  assert.equal(await errClass(() => a.get("s", WHOLE)), "resolution");
});
test("resolution error: key source names a format entry", async () => {
  const a = open(rawZip({ entries: [ref1("s", R(0, 0, 1))], sources: [keySrc("__vz__/sources")] }));
  assert.equal(await errClass(() => a.get("s", WHOLE)), "resolution");
});
test("resolution error: key source with an entry error", async () => {
  const a = open(rawZip({ entries: [{ name: "h", method: 77 }, ref1("s", R(0, 0, 1))], sources: [keySrc("h")] }));
  assert.equal(await errClass(() => a.get("s", WHOLE)), "resolution");
});
test("resolution error: unsupported scheme", async () => {
  const a = open(rawZip({ entries: [ref1("s", R(0, 0, 1))], sources: [url("ftp://x/y")] }));
  assert.equal(await errClass(() => a.get("s", WHOLE)), "resolution");
});
test("resolution error: invalid URL syntax (checked only at resolve)", async () => {
  const a = open(rawZip({ entries: [ref1("s", R(0, 0, 1)), { name: "b", body: Buffer.from("x") }], sources: [url("a b")] }));
  assert.deepEqual(await a.get("b", WHOLE), Buffer.from("x"));
  assert.equal(await errClass(() => a.get("s", WHOLE)), "resolution");
});
for (const [label, u] of [
  ["file host", "file://example.com/etc/passwd"],
  ["file port", "file://localhost:1/x"],
  ["file query", "/x?"],
  ["encoded slash", "a%2Fb"],
  ["encoded dot-dot", "%2E%2E/x"],
  ["NUL", "a%00b"],
  ["relative file path", "file:x"],
]) {
  test(`resolution error: file: rule (${label})`, async () => {
    const a = open(rawZip({ entries: [ref1("s", R(0, 0, 1))], sources: [url(u)] }));
    assert.equal(await errClass(() => a.get("s", WHOLE)), "resolution");
  });
}
test("file: URL forms that are accepted (absolute, LOCALHOST, fragment, #-only)", async () => {
  const sub = fs.mkdtempSync(path.join(dir, "f-"));
  fs.writeFileSync(path.join(sub, "d a.bin"), "0123456789");
  const abs = "file://LOCALHOST" + encodeURI(path.join(sub, "d a.bin")) + "#frag";
  const arch = rawZip({
    entries: [ref1("a", R(0, 2, 3)), ref1("b", R(1, 1, 2)), ref1("c", R(2, 0, 4)), ref1("d", R(3, 0, 1))],
    sources: [url(abs), url("./x/../d%20a.bin"), url("#self"), url("file:" + encodeURI(path.join(sub, "d a.bin")))],
  });
  const p = path.join(sub, "arch.vzip");
  fs.writeFileSync(p, arch);
  const a = Archive.open(p);
  assert.deepEqual(await a.get("a", WHOLE), Buffer.from("234"));
  assert.deepEqual(await a.get("b", WHOLE), Buffer.from("12"));
  assert.deepEqual(await a.get("c", WHOLE), arch.subarray(0, 4));
  assert.deepEqual(await a.get("d", WHOLE), Buffer.from("0"));
});
test("file pins: size, modified_not_after pass and fail; etag cannot be checked", async () => {
  const sub = fs.mkdtempSync(path.join(dir, "p-"));
  const f = path.join(sub, "x.bin");
  fs.writeFileSync(f, "0123456789");
  fs.utimesSync(f, 1000000, 1000000.7);
  const arch = rawZip({
    entries: [ref1("ok", R(0, 0, 2)), ref1("size", R(1, 0, 2)), ref1("mtime", R(2, 0, 2)), ref1("etag", R(3, 0, 2)), ref1("zero", R(1, 0, 0))],
    sources: [
      url("x.bin", { size: 10n, modifiedNotAfter: 1000000n }),
      url("x.bin", { size: 11n }),
      url("x.bin", { modifiedNotAfter: 999999n }),
      url("x.bin", { etag: '"e"' }),
    ],
  });
  fs.writeFileSync(path.join(sub, "a.vzip"), arch);
  const a = Archive.open(path.join(sub, "a.vzip"));
  assert.deepEqual(await a.get("ok", WHOLE), Buffer.from("01"));
  assert.equal(await errClass(() => a.get("size", WHOLE)), "resolution");
  assert.equal(await errClass(() => a.get("mtime", WHOLE)), "resolution");
  assert.equal(await errClass(() => a.get("etag", WHOLE)), "resolution");
  assert.deepEqual(await a.get("zero", WHOLE), Buffer.alloc(0)); // zero-length ranges are never resolved
});

// ---------------------------------------------------------------- request errors and order

test("request error: start > end, even for missing and hidden keys", async () => {
  const a = open(rawZip({ entries: [] }));
  const req: Request = { kind: "range", start: 2n, end: 1n };
  assert.equal(await errClass(() => a.get("nope", req)), "request");
  assert.equal(await errClass(() => a.get("__vz__/x", req)), "request");
});
test("hidden keys are missing for classify/get but visible to raw", async () => {
  const a = open(rawZip({ entries: [{ name: "__vz__/h", method: 99 }, { name: "__vz__/g", body: Buffer.from("g") }] }));
  assert.equal(a.classify("__vz__/h"), "missing");
  assert.equal(await a.get("__vz__/h", WHOLE), null);
  assert.equal(await errClass(() => a.raw("__vz__/h")), "entry");
  assert.deepEqual(await a.raw("__vz__/g"), Buffer.from("g"));
  assert.equal(await a.raw("__vz__/index"), null);
  assert.deepEqual(a.list(""), []);
});
test("records with invalid UTF-8 names are ignored", async () => {
  const a = open(rawZip({ entries: [{ name: Buffer.from([0xff, 0x41]), method: 99 }, { name: "ok" }] }));
  assert.deepEqual(a.list(""), ["ok"]);
});
test("queries CLI: malformed queries file exits non-zero", () => {
  const p = save(rawZip({ entries: [] }));
  for (const q of [
    '[{"op":"nope","key":"a"}]',
    '[{"op":"get"}]',
    '[{"op":"list"}]',
    '[{"op":"classify","key":"a","range":{"offset":1}}]',
    '[{"op":"get","key":"a","range":null}]',
    '[{"op":"get","key":"a","range":{"start":1}}]',
    '[{"op":"get","key":"a","range":{"offset":1,"suffix":2}}]',
    '[{"op":"get","key":"a","range":{"offset":-1}}]',
    '[{"op":"get","key":"a","range":{"offset":1.5}}]',
    '[{"op":"get","key":"a","key":"b"}]',
  ]) {
    const qp = writeTmp(dir, `bad-q-${n++}.json`, q);
    assert.notEqual(runCli(["read", p, qp]).status, 0, q);
  }
});
