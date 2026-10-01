import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import * as zlib from "node:zlib";
import { Archive, type Request } from "../src/reader.ts";
import { VzError } from "../src/errors.ts";
import {
  type RawEntry,
  type RawOpts,
  extraBlock,
  fLen,
  fVarint,
  msg,
  rangeMsg,
  rawZip,
  readQueries,
  refExtra,
  srcUrl,
  table,
  tmpdir,
} from "./helpers.ts";

const dir = tmpdir();
let counter = 0;
function save(buf: Buffer): string {
  const p = path.join(dir, `a${counter++}.vzip`);
  fs.writeFileSync(p, buf);
  return p;
}
function openRaw(entries: RawEntry[], opts: RawOpts = {}): Archive {
  return Archive.open(save(rawZip(entries, opts)));
}
function archiveError(buf: Buffer): void {
  assert.throws(() => Archive.open(save(buf)), (e: unknown) => e instanceof VzError && e.cls === "archive");
}
async function errClass(p: Promise<unknown> | (() => unknown)): Promise<string> {
  try {
    if (typeof p === "function") p();
    else await p;
  } catch (e) {
    if (e instanceof VzError) return e.cls;
    throw e;
  }
  return "ok";
}
const deflate = (s: Buffer | string) => zlib.deflateRawSync(typeof s === "string" ? Buffer.from(s) : s);

// ---------------- archive errors ----------------

test("archive error: not a zip / too short / empty", () => {
  archiveError(Buffer.alloc(0));
  archiveError(Buffer.from("hello"));
  archiveError(Buffer.alloc(200, 0x41));
});

test("archive error: CLI reports unreadable file as an open failure", () => {
  const r = readQueries(path.join(dir, "does-not-exist.vzip"), [{ op: "list", prefix: "" }]);
  assert.equal(r.open.ok, false);
  assert.equal(r.open.class, "archive");
  assert.deepEqual(r.results, []);
});

test("archive error: unsupported version", () => archiveError(rawZip([], { magic: "vzip/1" })));
test("archive error: wrong magic", () => archiveError(rawZip([], { magic: "vzap/0" })));
test("archive error: plain zip with a different comment length", () => {
  const b = rawZip([]);
  // Append one byte to the comment and fix the comment length: no longer 22 or 38.
  const c = Buffer.concat([b, Buffer.from([0])]);
  c.writeUInt16LE(23, c.length - 23 - 2);
  archiveError(c);
});

test("archive error: no fallback from 60 to 44 when magic is wrong", () => {
  // A 38-byte comment whose magic is wrong, but whose last 22 bytes look like a 22-byte-comment EOCD.
  const good = rawZip([]); // 22-byte comment form
  const eocd44 = good.subarray(good.length - 44);
  const body = good.subarray(0, good.length - 44);
  // 16 bytes of padding that start with an EOCD signature, then the real 22-byte-comment record
  // whose disk-number field (38) doubles as the fake record's comment length.
  const pad = Buffer.alloc(16);
  pad.writeUInt32LE(0x06054b50, 0);
  const real = Buffer.from(eocd44);
  real.writeUInt16LE(38, 4); // disk number = 38 (readers ignore disk numbers)
  const buf = Buffer.concat([body, pad, real]);
  archiveError(buf);
  // Sanity: with disk number 0 the same file opens through step 2.
  real.writeUInt16LE(0, 4);
  Archive.open(save(Buffer.concat([body, pad, real]))).close();
});

test("archive error: source table body outside the file", () => {
  const b = rawZip([]);
  b.writeBigUInt64LE(10_000n, b.length - 22 + 14);
  archiveError(b);
});

test("archive error: source table does not inflate cleanly (trailing bytes)", () => {
  archiveError(rawZip([], { sourcesBody: Buffer.concat([deflate(table(srcUrl("a"))), Buffer.from([0])]) }));
});

test("archive error: source table does not inflate cleanly (truncated)", () => {
  const d = deflate(table(srcUrl("abcdefgh")));
  archiveError(rawZip([], { sourcesBody: d.subarray(0, d.length - 1) }));
});

test("archive error: source table is malformed (group wire type)", () => {
  archiveError(rawZip([], { sources: Buffer.from([0x0b]) }));
});

test("archive error: source table is malformed (bad UTF-8 url)", () => {
  archiveError(rawZip([], { sources: table(fLen(1, Buffer.from([0xff]))) }));
});

test("archive error: source with no kind", () => archiveError(rawZip([], { sources: table(fVarint(4, 1)) })));
test("archive error: empty url", () => archiveError(rawZip([], { sources: table(fLen(1, "")) })));
test("archive error: pin on key source", () => archiveError(rawZip([], { sources: table(msg(fLen(2, "k"), fVarint(4, 1))) })));
test("archive error: pin on data source", () => archiveError(rawZip([], { sources: table(msg(fLen(3, "k"), fLen(5, '"x"'))) })));
test("archive error: weak etag pin", () => archiveError(rawZip([], { sources: table(srcUrl("a", { etag: 'W/"x"' })) })));

test("open succeeds: invalid URL syntax is only checked at resolution", async () => {
  const a = openRaw([{ name: "r", extra: refExtra(rangeMsg(0, 0, 1)) }], { sources: table(srcUrl("a b")) });
  assert.equal(await errClass(a.get("r")), "resolution");
});

test("archive error: central directory outside the file", () => {
  const b = rawZip([{ name: "a", body: Buffer.from("x") }]);
  b.writeUInt32LE(b.length, b.length - 22 - 22 + 16);
  archiveError(b);
});

test("archive error: central directory does not parse (unpaged)", () => {
  const b = rawZip([{ name: "a", body: Buffer.from("x") }]);
  const cdOff = b.readUInt32LE(b.length - 44 + 16);
  b.writeUInt32LE(0x12345678, cdOff);
  archiveError(b);
});

test("archive error: __vz__/index entry in an archive without a page index", () => {
  archiveError(rawZip([{ name: "__vz__/index", body: deflate(""), method: 8 }]));
});

test("archive error: zip64 marker without locator", () => {
  const b = rawZip([]);
  b.writeUInt16LE(0xffff, b.length - 44 + 8);
  archiveError(b);
});

// ---------------- ZIP64 accepted ----------------

test("ZIP64 end records and ZIP64 offset extra are used", async () => {
  const b = rawZip([{ name: "a", body: Buffer.from("hi") }]);
  const eocdPos = b.length - 44;
  const cdSize = b.readUInt32LE(eocdPos + 12);
  const cdOff = b.readUInt32LE(eocdPos + 16);
  const z = Buffer.alloc(56);
  z.writeUInt32LE(0x06064b50, 0);
  z.writeBigUInt64LE(44n, 4);
  z.writeBigUInt64LE(2n, 24);
  z.writeBigUInt64LE(2n, 32);
  z.writeBigUInt64LE(BigInt(cdSize), 40);
  z.writeBigUInt64LE(BigInt(cdOff), 48);
  const loc = Buffer.alloc(20);
  loc.writeUInt32LE(0x07064b50, 0);
  loc.writeBigUInt64LE(BigInt(eocdPos), 8);
  loc.writeUInt32LE(1, 16);
  const eocd = Buffer.from(b.subarray(eocdPos));
  eocd.writeUInt32LE(0xffffffff, 16);
  eocd.writeUInt32LE(0, 12);
  eocd.writeUInt16LE(0xffff, 8);
  const out = Buffer.concat([b.subarray(0, eocdPos), z, loc, eocd]);
  const a = Archive.open(save(out));
  assert.equal(Buffer.from((await a.get("a"))!).toString(), "hi");

  // ZIP64 offset in the CD extra.
  const a2 = openRaw([{ name: "x", body: Buffer.from("yo"), lho: 0xffffffff, extra: extraBlock(1, Buffer.from([0, 0, 0, 0, 0, 0, 0, 0])) }]);
  assert.equal(Buffer.from((await a2.get("x"))!).toString(), "yo");
});

// ---------------- entry errors ----------------

test("entry errors: each record problem fails classify/get/raw for that key only", async () => {
  const cases: RawEntry[] = [
    { name: "badextra", extra: Buffer.from([1, 2, 3]) },
    { name: "tworefs", extra: Buffer.concat([refExtra(rangeMsg(0, 0, 0)), refExtra(msg(), true)]) },
    { name: "method12", method: 12 },
    { name: "encrypted", flags: 0x801 },
    { name: "refdeflate", method: 8, body: deflate(""), extra: refExtra(msg()) },
    { name: "z64missing", lho: 0xffffffff },
    { name: "z64short", lho: 0xffffffff, extra: extraBlock(1, Buffer.alloc(4)) },
    { name: "z64twice", lho: 0xffffffff, extra: Buffer.concat([extraBlock(1, Buffer.alloc(8)), extraBlock(1, Buffer.alloc(8))]) },
  ];
  const a = openRaw([...cases, { name: "good", body: Buffer.from("g") }], { sources: table(fLen(2, "badextra")) });
  for (const c of cases) {
    const k = c.name as string;
    assert.equal(await errClass(() => a.classify(k)), "entry", k);
    assert.equal(await errClass(a.get(k)), "entry", k);
    assert.equal(await errClass(a.raw(k)), "entry", k);
  }
  assert.equal(a.classify("good"), "bytes");
  // Keys with entry errors are listed.
  assert.deepEqual(a.list(""), [...cases.map((c) => c.name as string), "good"].sort());
});

test("entry error: page that cannot be parsed", async () => {
  // Page index whose second page boundary is off by one so the page doesn't hold whole records.
  const entries: RawEntry[] = ["a", "b", "c"].map((n) => ({ name: n, body: Buffer.from(n) }));
  const a = openRaw(entries, {
    indexFn: ({ names, offsets, lengths }) =>
      msg(
        fLen(1, msg(fLen(1, names[0]), fVarint(3, lengths[0] + 1))),
        fLen(1, msg(fLen(1, names[2]), fVarint(2, offsets[1] + 1), fVarint(3, lengths[1] - 1 + lengths[2]))),
      ),
  });
  assert.equal(await errClass(() => a.classify("a")), "entry");
  assert.equal(await errClass(() => a.classify("c")), "entry");
  assert.equal(await errClass(() => a.list("")), "entry");
  assert.equal(a.classify("0"), "missing"); // before the first page: no page read
});

// ---------------- page index checks ----------------

function pagedArchive(indexFn: RawOpts["indexFn"]): Buffer {
  return rawZip(["a", "b"].map((n) => ({ name: n, body: Buffer.from(n) })), { indexFn });
}
const page = (k: string, off: number, len: number) => fLen(1, msg(fLen(1, k), fVarint(2, off), fVarint(3, len)));
const pin = (k: string, off: number, size: number, csize: number, method: number) =>
  fLen(2, msg(fLen(1, k), fVarint(2, off), fVarint(3, size), fVarint(4, csize), fVarint(5, method)));

test("page index ok, including pinned keys and list across pages", async () => {
  const a = Archive.open(
    save(
      pagedArchive(({ lengths, bodyOffsets }) =>
        msg(page("a", 0, lengths[0]), page("b", lengths[0], lengths[1]), pin("__vz__/p", bodyOffsets[0], 1, 1, 0)),
      ),
    ),
  );
  assert.deepEqual(a.list(""), ["a", "b"]);
  assert.equal(Buffer.from((await a.raw("__vz__/p"))!).toString(), "a");
});

test("archive error: page with length 0", () => archiveError(pagedArchive(({ lengths }) => msg(page("a", 0, 0), page("b", 0, lengths[0] + lengths[1])))));
test("archive error: page outside the central directory", () => archiveError(pagedArchive(() => msg(page("a", 0, 100000)))));
test("archive error: pages not contiguous", () => archiveError(pagedArchive(({ lengths }) => msg(page("a", 0, lengths[0]), page("b", lengths[0] + 1, lengths[1] - 1)))));
test("archive error: first page not at offset 0", () => archiveError(pagedArchive(({ lengths }) => msg(page("b", lengths[0], lengths[1])))));
test("archive error: first_key not increasing", () => archiveError(pagedArchive(({ lengths }) => msg(page("b", 0, lengths[0]), page("a", lengths[0], lengths[1])))));
test("archive error: empty first_key", () => archiveError(pagedArchive(({ lengths }) => msg(page("", 0, lengths[0] + lengths[1])))));
test("archive error: empty pinned key", () => archiveError(pagedArchive(({ bodyOffsets }) => msg(pin("", bodyOffsets[0], 1, 1, 0)))));
test("archive error: pinned key twice", () => archiveError(pagedArchive(({ bodyOffsets }) => msg(pin("a", bodyOffsets[0], 1, 1, 0), pin("a", bodyOffsets[0], 1, 1, 0)))));
test("archive error: pinned format entry", () => archiveError(pagedArchive(({ bodyOffsets }) => msg(pin("__vz__/sources", bodyOffsets[0], 1, 1, 0)))));
test("archive error: pinned method 3", () => archiveError(pagedArchive(({ bodyOffsets }) => msg(pin("a", bodyOffsets[0], 1, 1, 3)))));
test("archive error: pinned body outside the file", () => archiveError(pagedArchive(() => msg(pin("a", 1_000_000, 1, 1, 0)))));
test("archive error: page index does not decode", () => archiveError(pagedArchive(() => Buffer.from([0x0f]))));

// ---------------- body errors ----------------

test("body errors: get and raw fail; key source naming the entry is a resolution error", async () => {
  const cases: RawEntry[] = [
    { name: "corrupt", method: 8, body: Buffer.from([0xff, 0xff, 0xff]), usize: 3 },
    { name: "trailing", method: 8, body: Buffer.concat([deflate("abc"), Buffer.from([0])]), usize: 3 },
    { name: "wrongsize", method: 8, body: deflate("abc"), usize: 4 },
    { name: "storedsizes", body: Buffer.from("abc"), usize: 2 },
    { name: "outside", body: Buffer.from("abc"), csize: 100000, usize: 100000 },
  ];
  const srcs = table(...cases.map((c) => fLen(2, c.name as string)));
  const refs: RawEntry[] = cases.map((c, i) => ({ name: `ref-${c.name}`, extra: refExtra(rangeMsg(i, 0, 1)) }));
  const a = openRaw([...cases, ...refs], { sources: srcs });
  for (const c of cases) {
    const k = c.name as string;
    assert.equal(a.classify(k), "bytes");
    assert.equal(await errClass(a.get(k)), "body", k);
    assert.equal(await errClass(a.get(k, { type: "range", start: 0n, end: 1n })), "body", k);
    assert.equal(await errClass(a.raw(k)), "body", k);
    assert.equal(await errClass(a.get(`ref-${k}`)), "resolution", k);
  }
});

test("body error: raw of a reference entry with a bad body", async () => {
  const a = openRaw([{ name: "r", body: Buffer.from("abc"), usize: 5, extra: refExtra(msg(fLen(5, "x"))) }]);
  assert.equal(Buffer.from((await a.get("r"))!).toString(), "x"); // body is ignored by get
  assert.equal(await errClass(a.raw("r")), "body");
});

// ---------------- payload errors ----------------

test("payload errors", async () => {
  const payloads: [string, Buffer, boolean][] = [
    ["groups", Buffer.from([0x0b, 0x0c]), false],
    ["fieldzero", Buffer.from([0x00, 0x00]), false],
    ["truncated", Buffer.from([0x18]), false],
    ["lenpast", Buffer.from([0x2a, 0x05, 0x00]), false],
    ["longvarint", Buffer.from([0x18, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x00]), false],
    ["varint>2^64", Buffer.from([0x18, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x02]), false],
    ["wrongwt", Buffer.from([0x1a, 0x00]), false],
    ["u32overflow", Buffer.concat([fVarint(1, 1n << 32n)]), false],
    ["literal+source", msg(fVarint(3, 1), fLen(5, "")), false],
    ["oob", rangeMsg(5, 0, 0), false],
    ["overflow", msg(fVarint(3, (1n << 64n) - 1n), fVarint(4, 1)), false],
    ["concat-oob-outside-window", msg(fLen(1, msg(fLen(5, "ab"))), fLen(1, rangeMsg(9, 0, 0))), true],
    ["concat-total", msg(fLen(1, rangeMsg(0, 0, (1n << 64n) - 1n)), fLen(1, rangeMsg(0, 0, 1))), true],
    ["concat-badpart", msg(fLen(1, Buffer.from([0x0b]))), true],
  ];
  const a = openRaw(
    payloads.map(([n, p, c]) => ({ name: n, extra: refExtra(p, c) })),
    { sources: table(fLen(3, "")) },
  );
  for (const [n] of payloads) {
    assert.equal(a.classify(n), "reference", n);
    assert.equal(await errClass(a.get(n, { type: "range", start: 0n, end: 1n })), "payload", n);
  }
});

test("payload ok: unknown fields of all wire types, reserved field 2, non-minimal varints, last wins", async () => {
  const p = Buffer.concat([
    fVarint(2, 7), // reserved
    Buffer.from([0x81, 0x01, 0x01, 0, 0, 0, 0, 0, 0, 0]), // field 16 I64
    Buffer.from([0x85, 0x01, 0, 0, 0, 0]), // field 16 I32
    fLen(99, "zz"),
    fLen(5, "first"),
    Buffer.from([0x2a, 0x82, 0x00, 0x68, 0x69]), // field 5, non-minimal length 2: "hi"
  ]);
  const a = openRaw([{ name: "r", extra: refExtra(p) }]);
  assert.equal(Buffer.from((await a.get("r"))!).toString(), "hi");
});

// ---------------- resolution errors ----------------

test("resolution errors and non-resolution of ranges outside the window", async () => {
  const sub = path.join(dir, "res");
  fs.mkdirSync(sub, { recursive: true });
  fs.writeFileSync(path.join(sub, "f.bin"), "0123456789");
  const mtime = Math.floor(fs.statSync(path.join(sub, "f.bin")).mtimeMs / 1000);
  const srcs: Buffer[] = [
    srcUrl("f.bin"), // 0 ok
    srcUrl("missing.bin"), // 1
    srcUrl("f.bin", { etag: '"x"' }), // 2 etag on file
    srcUrl("f.bin", { size: 11 }), // 3 size mismatch
    srcUrl("f.bin", { size: 10, mna: mtime }), // 4 ok
    srcUrl("f.bin", { mna: mtime - 1 }), // 5 too new
    srcUrl("f.bin?x"), // 6 query
    srcUrl("a%2Fb"), // 7 encoded slash
    srcUrl("%2E%2E/f.bin"), // 8 encoded dot-dot
    srcUrl("file://otherhost/x"), // 9 authority
    srcUrl("s3://bucket/x"), // 10 scheme
    fLen(2, "missingkey"), // 11
    fLen(2, "ref"), // 12
    fLen(2, "__vz__/sources"), // 13
    fLen(3, "abc"), // 14 data
    srcUrl("file://LOCALHOST" + "/" + encodeURI(path.join(sub, "f.bin").slice(1))), // 15 ok
    srcUrl("FILE:" + encodeURI(path.join(sub, "f.bin"))), // 16 ok (no authority)
    srcUrl("f.bin?"), // 17 empty query
    srcUrl("a%00b"), // 18 NUL
    srcUrl("file:rel"), // 19 not absolute
    fLen(2, "__vz__/hidden"), // 20 ok (hidden key)
  ];
  const ref = (n: string, s: number, off: number, len: number): RawEntry => ({ name: n, extra: refExtra(rangeMsg(s, off, len)) });
  const entries: RawEntry[] = srcs.map((_, i) => ref(`s${i}`, i, 2, 3));
  entries.push({ name: "ref", extra: refExtra(rangeMsg(14, 0, 1)) });
  entries.push({ name: "__vz__/hidden", body: deflate("hidden!"), method: 8, usize: 7 });
  entries.push(ref("short", 0, 8, 5));
  entries.push(ref("datashort", 14, 2, 2));
  entries.push({ name: "partial", extra: refExtra(msg(fLen(1, msg(fLen(5, "ok"))), fLen(1, rangeMsg(1, 0, 5))), true) });
  const p = path.join(sub, "res.vzip");
  fs.writeFileSync(p, rawZip(entries, { sources: table(...srcs) }));
  const a = Archive.open(p);
  const expectOk: Record<number, string> = { 0: "234", 4: "234", 14: "", 15: "234", 16: "234", 20: "dde" };
  for (let i = 0; i < srcs.length; i++) {
    if (i === 14) continue;
    if (i in expectOk) assert.equal(Buffer.from((await a.get(`s${i}`))!).toString(), expectOk[i], `s${i}`);
    else assert.equal(await errClass(a.get(`s${i}`)), "resolution", `s${i}`);
  }
  assert.equal(await errClass(a.get("s14")), "resolution"); // data "abc" has 3 bytes, needs 5
  assert.equal(await errClass(a.get("short")), "resolution");
  assert.equal(Buffer.from((await a.get("short", { type: "range", start: 0n, end: 2n }))!).toString(), "89");
  assert.equal(await errClass(a.get("datashort")), "resolution");
  // Ranges outside the window are not resolved.
  assert.equal(Buffer.from((await a.get("partial", { type: "range", start: 0n, end: 2n }))!).toString(), "ok");
  assert.equal(await errClass(a.get("partial", { type: "offset", start: 1n })), "resolution");
  // Zero-length windows resolve nothing.
  assert.equal(Buffer.from((await a.get("s1", { type: "range", start: 1n, end: 1n }))!).length, 0);
});

test("request error: start > end even for missing keys", async () => {
  const a = openRaw([]);
  const req: Request = { type: "range", start: 2n, end: 1n };
  assert.equal(await errClass(a.get("nope", req)), "request");
  assert.equal(await a.get("nope", { type: "range", start: 1n, end: 1n }), null);
});

test("hidden keys are missing for classify/get, visible to raw; format entries visible to raw", async () => {
  const a = openRaw([{ name: "__vz__/x", body: Buffer.from("x") }]);
  assert.equal(a.classify("__vz__/x"), "missing");
  assert.equal(await a.get("__vz__/x"), null);
  assert.equal(Buffer.from((await a.raw("__vz__/x"))!).toString(), "x");
  assert.notEqual(await a.raw("__vz__/sources"), null);
  assert.equal(await a.raw("__vz__/index"), null);
  assert.deepEqual(a.list(""), []);
});

test("records with empty or invalid UTF-8 names are ignored", () => {
  const a = openRaw([{ name: Buffer.from([0xff]) }, { name: Buffer.alloc(0) }, { name: "ok" }]);
  assert.deepEqual(a.list(""), ["ok"]);
});
