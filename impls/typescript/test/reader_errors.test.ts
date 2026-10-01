// One test per reader error case (spec §8.1, §8.4).
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import * as zlib from "node:zlib";
import { Archive, type Request } from "../src/reader.ts";
import { VzipError, type ErrorClass } from "../src/errors.ts";
import { encodeCdIndex, encodeConcat, encodeRange, encodeSourceTable, type Source } from "../src/proto.ts";
import { assemble, block, buildBuf, findRec, parseZip, recBytes, replaceFormatBody, rng, src, tmpDir, writeTmp } from "./helpers.ts";
import type { WriterInput } from "../src/writer.ts";

const dir = tmpDir();
writeTmp(dir, "data.bin", Buffer.from("0123456789"));
let counter = 0;
const b = (s: string) => Buffer.from(s);

function save(buf: Uint8Array): string {
  return writeTmp(dir, `e${counter++}.vzip`, buf);
}

function openErr(buf: Uint8Array): VzipError {
  try {
    Archive.open(save(buf)).close();
  } catch (e) {
    assert.ok(e instanceof VzipError);
    assert.equal(e.cls, "archive");
    if (process.env.VZ_DEBUG) console.error("   ->", e.message);
    return e;
  }
  assert.fail("open succeeded");
}

async function opErr(buf: Uint8Array, op: "classify" | "get" | "raw" | "list", key: string, cls: ErrorClass, req?: Request): Promise<void> {
  const a = Archive.open(save(buf));
  try {
    if (op === "classify") a.classify(key);
    else if (op === "get") await a.get(key, req);
    else if (op === "raw") a.raw(key);
    else a.list(key);
  } catch (e) {
    assert.ok(e instanceof VzipError, String(e));
    assert.equal(e.cls, cls, e.message);
    return;
  } finally {
    a.close();
  }
  assert.fail(`${op}(${key}) succeeded`);
}

const base = (extra: Partial<WriterInput> = {}) =>
  buildBuf({
    sources: [src.url("data.bin")],
    entries: [
      { key: "a", bytes: b("hello hello hello"), compress: true },
      { key: "s", bytes: b("stored") },
      { key: "r", ranges: [rng.src(0, 1, 3)] },
    ],
    ...extra,
  });

function mutateRec(buf: Buffer, name: string, f: (r: ReturnType<typeof findRec>) => void): Buffer {
  const p = parseZip(buf);
  f(findRec(p, name));
  return assemble(p);
}

const sourcesWith = (sources: Source[]) => replaceFormatBody(base(), "sources", zlib.deflateRawSync(encodeSourceTable(sources)));
const rawSources = (bytes: Uint8Array) => replaceFormatBody(base(), "sources", zlib.deflateRawSync(bytes));

// ---------------- archive errors ----------------

const archiveCases: Record<string, () => Uint8Array> = {
  "file shorter than an end record": () => b("tiny"),
  "no vzip comment": () => {
    const p = parseZip(base());
    p.comment = Buffer.alloc(0);
    return assemble(p);
  },
  "comment without vzip/ magic": () => {
    const p = parseZip(base());
    p.comment = Buffer.from(p.comment);
    p.comment.write("vzap/0", 0, "latin1");
    return assemble(p);
  },
  "unsupported version": () => {
    const p = parseZip(base());
    p.comment = Buffer.from(p.comment);
    p.comment.write("vzip/1", 0, "latin1");
    return assemble(p);
  },
  "fake record at size-60 (disk number 38): no fallback": () => {
    const p = parseZip(base());
    p.recs[p.recs.length - 1].comment = Buffer.concat([Buffer.from([0x50, 0x4b, 0x05, 0x06]), Buffer.alloc(12)]);
    const out = assemble(p);
    out.writeUInt16LE(38, out.length - 44 + 4);
    return out;
  },
  "zip64 locator missing": () => {
    const out = base();
    out.writeUInt32LE(0xffffffff, out.length - 44 + 16);
    return out;
  },
  "zip64 record bad signature": () => {
    const out = assemble(parseZip(base()), { zip64: true });
    out.writeUInt32LE(0x12345678, out.length - 44 - 20 - 56);
    return out;
  },
  "zip64 record size field not 44": () => {
    const out = assemble(parseZip(base()), { zip64: true });
    out.writeBigUInt64LE(56n, out.length - 44 - 20 - 56 + 4);
    return out;
  },
  "zip64 record outside the file": () => {
    const out = assemble(parseZip(base()), { zip64: true });
    out.writeBigUInt64LE(1n << 40n, out.length - 44 - 20 + 8);
    return out;
  },
  "central directory outside the file": () => {
    const out = base();
    out.writeUInt32LE(out.length, out.length - 44 + 12);
    return out;
  },
  "sources body outside the file": () => {
    const out = base();
    out.writeBigUInt64LE(BigInt(out.length), out.length - 22 + 6);
    return out;
  },
  "sources body with trailing byte": () => replaceFormatBody(base(), "sources", Buffer.concat([zlib.deflateRawSync(encodeSourceTable([])), Buffer.from([0])])),
  "sources body truncated": () => replaceFormatBody(base(), "sources", zlib.deflateRawSync(Buffer.alloc(100, 1)).subarray(0, 5)),
  "sources body not DEFLATE": () => replaceFormatBody(base(), "sources", Buffer.from([0xff, 0xff, 0xff])),
  "source table malformed": () => rawSources(Buffer.from([0x0a, 0x05, 0x00])),
  "source without kind": () => rawSources(Buffer.from([0x0a, 0x02, 0x20, 0x01])),
  "source with empty url": () => sourcesWith([src.url("")]),
  "pin on a key source": () => sourcesWith([{ ...src.key("a"), size: 3n }]),
  "pin on a data source": () => sourcesWith([{ ...src.data(b("x")), modifiedNotAfter: 0n }]),
  "weak etag pin": () => sourcesWith([src.url("x", { etag: 'W/"a"' })]),
  "unquoted etag pin": () => sourcesWith([src.url("x", { etag: "abc" })]),
  "unparseable central directory (unpaged)": () => {
    const p = parseZip(base());
    const cd = Buffer.concat(p.recs.map(recBytes));
    cd.writeUInt32LE(0, 0);
    return assemble(p, { cdRaw: cd });
  },
  "central directory with trailing bytes (unpaged)": () => {
    const p = parseZip(base());
    return assemble(p, { cdRaw: Buffer.concat([...p.recs.map(recBytes), Buffer.alloc(3)]) });
  },
  "__vz__/index record in archive without page index": () => {
    const p = parseZip(base());
    const r = findRec(p, "s");
    p.recs.push({ ...r, name: Buffer.from("__vz__/index") });
    return assemble(p);
  },
};
const paged = () => base({ pageSize: 1, entries: [{ key: "a", bytes: b("1") }, { key: "b", bytes: b("2") }, { key: "c", bytes: b("3"), pinned: true }] });
const idx = (pages: { firstKey: string; offset: bigint; length: bigint }[], pinned: any[] = []) =>
  replaceFormatBody(paged(), "index", zlib.deflateRawSync(encodeCdIndex({ pages, pinned })));
Object.assign(archiveCases, {
  "page index does not decode": () => replaceFormatBody(paged(), "index", zlib.deflateRawSync(Buffer.from([0x0a, 0x09]))),
  "page index does not inflate": () => replaceFormatBody(paged(), "index", Buffer.from([1, 2, 3])),
  "page of length 0": () => idx([{ firstKey: "a", offset: 0n, length: 0n }]),
  "page outside the central directory": () => idx([{ firstKey: "a", offset: 0n, length: 100000n }]),
  "pages not contiguous from 0": () => idx([{ firstKey: "a", offset: 1n, length: 10n }]),
  "pages with a gap": () => idx([{ firstKey: "a", offset: 0n, length: 10n }, { firstKey: "b", offset: 11n, length: 10n }]),
  "first_key not increasing": () => idx([{ firstKey: "b", offset: 0n, length: 10n }, { firstKey: "a", offset: 10n, length: 10n }]),
  "first_key equal": () => idx([{ firstKey: "a", offset: 0n, length: 10n }, { firstKey: "a", offset: 10n, length: 10n }]),
  "empty first_key": () => idx([{ firstKey: "", offset: 0n, length: 10n }]),
  "empty pinned key": () => idx([], [{ key: "", dataOffset: 0n, size: 0n, csize: 0n, method: 0 }]),
  "pinned key listed twice": () =>
    idx([], [{ key: "q", dataOffset: 0n, size: 0n, csize: 0n, method: 0 }, { key: "q", dataOffset: 0n, size: 0n, csize: 0n, method: 0 }]),
  "pinned format entry": () => idx([], [{ key: "__vz__/sources", dataOffset: 0n, size: 0n, csize: 0n, method: 0 }]),
  "pinned method 3": () => idx([], [{ key: "q", dataOffset: 0n, size: 0n, csize: 0n, method: 3 }]),
  "pinned body outside the file": () => idx([], [{ key: "q", dataOffset: 0n, size: 1n, csize: 1000000n, method: 0 }]),
});

for (const [name, mk] of Object.entries(archiveCases)) {
  test(`archive error: ${name}`, () => {
    openErr(mk());
  });
}

test("archive error: file does not exist", () => {
  assert.throws(() => Archive.open(path.join(dir, "nope.vzip")), (e: unknown) => e instanceof VzipError && e.cls === "archive");
});

// ---------------- valid variants that must open and read ----------------

test("valid variants: ZIP64 end records, ZIP64 offset block, extra blocks in any order, unknown blocks", async () => {
  const z = assemble(parseZip(base()), { zip64: true });
  let a = Archive.open(save(z));
  assert.equal(Buffer.from((await a.get("r"))!).toString(), "123");
  a.close();
  // ZIP64 local header offset for "s", reference block before an unknown block for "r".
  const p = parseZip(base());
  const s = findRec(p, "s");
  const lho = s.fixed.readUInt32LE(42);
  s.fixed.writeUInt32LE(0xffffffff, 42);
  const z64 = Buffer.alloc(8);
  z64.writeBigUInt64LE(BigInt(lho));
  s.extra = Buffer.concat([block(0x1234, b("zz")), block(0x0001, z64)]);
  const r = findRec(p, "r");
  r.extra = Buffer.concat([r.extra, block(0x5555, b(""))]);
  a = Archive.open(save(assemble(p)));
  assert.equal(Buffer.from((await a.get("s"))!).toString(), "stored");
  assert.equal(Buffer.from((await a.get("r"))!).toString(), "123");
  // records with empty names or invalid UTF-8 are ignored
  a.close();
  const p2 = parseZip(base());
  p2.recs.push({ ...findRec(p2, "s"), name: Buffer.from([0xff]) }, { ...findRec(p2, "s"), name: Buffer.alloc(0) });
  a = Archive.open(save(assemble(p2)));
  assert.deepEqual(a.list(""), ["a", "r", "s"]);
  a.close();
});

// ---------------- entry errors ----------------

const entryCases: Record<string, () => Buffer> = {
  "extra field does not parse": () => mutateRec(base(), "s", (r) => (r.extra = Buffer.from([1, 2, 3]))),
  "extra block overruns": () => mutateRec(base(), "s", (r) => (r.extra = Buffer.from([1, 2, 9, 0, 1]))),
  "two reference blocks": () => mutateRec(base(), "r", (r) => (r.extra = Buffer.concat([r.extra, block(0x7a77, new Uint8Array())]))),
  "method 99": () => mutateRec(base(), "s", (r) => r.fixed.writeUInt16LE(99, 10)),
  "encrypted (bit 0)": () => mutateRec(base(), "s", (r) => r.fixed.writeUInt16LE(0x0801, 8)),
  "reference with method 8": () => mutateRec(base(), "r", (r) => r.fixed.writeUInt16LE(8, 10)),
  "offset 0xFFFFFFFF without ZIP64 block": () => mutateRec(base(), "s", (r) => r.fixed.writeUInt32LE(0xffffffff, 42)),
  "ZIP64 block too short": () =>
    mutateRec(base(), "s", (r) => {
      r.fixed.writeUInt32LE(0xffffffff, 42);
      r.extra = block(0x0001, Buffer.alloc(4));
    }),
  "two ZIP64 blocks": () =>
    mutateRec(base(), "s", (r) => {
      r.fixed.writeUInt32LE(0xffffffff, 42);
      r.extra = Buffer.concat([block(0x0001, Buffer.alloc(8)), block(0x0001, Buffer.alloc(8))]);
    }),
};
for (const [name, mk] of Object.entries(entryCases)) {
  test(`entry error: ${name}`, async () => {
    const buf = mk();
    const key = name.includes("reference") ? "r" : "s";
    await opErr(buf, "classify", key, "entry");
    await opErr(buf, "get", key, "entry");
    await opErr(buf, "raw", key, "entry");
    const a = Archive.open(save(buf));
    assert.equal(Buffer.from((await a.get("a"))!).toString(), "hello hello hello"); // other keys unaffected
    assert.ok(a.list("").includes(key)); // keys with entry errors are listed
    // a key source naming it fails with a resolution error
    a.close();
  });
}

test("entry error: page that cannot be parsed", async () => {
  const p = parseZip(paged());
  // corrupt the signature of record "b" (in its own page with page_size 1)
  findRec(p, "b").fixed.writeUInt32LE(0, 0);
  const buf = assemble(p);
  await opErr(buf, "get", "b", "entry");
  await opErr(buf, "classify", "bb", "entry");
  await opErr(buf, "list", "", "entry");
  await opErr(buf, "list", "b", "entry");
  const a = Archive.open(save(buf));
  assert.deepEqual(a.list("a"), ["a"]);
  assert.equal(a.classify("c"), "bytes"); // pinned
  a.close();
});

// ---------------- body errors ----------------

const bodyCases: Record<string, [() => Buffer, string]> = {
  "body outside the file": [() => mutateRec(base(), "s", (r) => r.fixed.writeUInt32LE(0x7fffffff, 42)), "s"],
  "DEFLATE body with trailing bytes": [() => mutateRec(base(), "a", (r) => r.fixed.writeUInt32LE(r.fixed.readUInt32LE(20) + 1, 20)), "a"],
  "DEFLATE body truncated": [() => mutateRec(base(), "a", (r) => r.fixed.writeUInt32LE(r.fixed.readUInt32LE(20) - 1, 20)), "a"],
  "DEFLATE inflates to the wrong size": [() => mutateRec(base(), "a", (r) => r.fixed.writeUInt32LE(r.fixed.readUInt32LE(24) + 1, 24)), "a"],
  "STORED sizes differ": [() => mutateRec(base(), "s", (r) => r.fixed.writeUInt32LE(3, 24)), "s"],
};
for (const [name, [mk, key]] of Object.entries(bodyCases)) {
  test(`body error: ${name}`, async () => {
    const buf = mk();
    await opErr(buf, "get", key, "body");
    await opErr(buf, "get", key, "body", { type: "range", start: 0n, end: 1n });
    await opErr(buf, "raw", key, "body");
    const a = Archive.open(save(buf));
    assert.equal(a.classify(key), "bytes");
    a.close();
  });
}
test("body error: reference entry raw with STORED sizes differing", async () => {
  const buf = mutateRec(base(), "r", (r) => r.fixed.writeUInt32LE(1, 24));
  await opErr(buf, "raw", "r", "body");
  const a = Archive.open(save(buf));
  assert.equal(Buffer.from((await a.get("r"))!).toString(), "123"); // get ignores the body
  a.close();
});

// ---------------- payload errors ----------------

const withPayload = (id: number, payload: Uint8Array, sources: Source[] = [src.url("data.bin")]) =>
  mutateRec(buildBuf({ sources, entries: [{ key: "r", ranges: [rng.lit("x")] }] }), "r", (r) => (r.extra = block(id, payload)));
const payloadCases: Record<string, () => Buffer> = {
  "undecodable Range": () => withPayload(0x7a76, Buffer.from([0x2a, 0x05])),
  "undecodable Concat": () => withPayload(0x7a77, Buffer.from([0x0a, 0x01, 0x07])),
  "source index out of bounds outside the window": () =>
    withPayload(0x7a77, encodeConcat([rng.lit("abc"), rng.src(5, 0, 0)])),
  "source range with no sources": () => withPayload(0x7a76, encodeRange(rng.src(0, 0, 0)), []),
  "literal with non-zero length": () => withPayload(0x7a76, encodeRange({ source: 0, offset: 0n, length: 1n, data: b("a") })),
  "literal with non-zero source": () => withPayload(0x7a76, encodeRange({ source: 1, offset: 0n, length: 0n, data: b("a") }), [src.data(b("x")), src.data(b("y"))]),
  "offset + length exceeds 2^64-1": () => withPayload(0x7a76, encodeRange(rng.src(0, (1n << 64n) - 1n, 1n))),
  "total size exceeds 2^64-1": () =>
    withPayload(0x7a77, encodeConcat([rng.src(0, 0, (1n << 63n)), rng.src(0, 0, (1n << 63n))])),
  "uint32 source overflow": () => withPayload(0x7a76, Buffer.from([0x08, 0x80, 0x80, 0x80, 0x80, 0x10])),
};
for (const [name, mk] of Object.entries(payloadCases)) {
  test(`payload error: ${name}`, async () => {
    const buf = mk();
    await opErr(buf, "get", "r", "payload", { type: "range", start: 0n, end: 1n });
    await opErr(buf, "get", "r", "payload", { type: "range", start: 0n, end: 0n });
    const a = Archive.open(save(buf));
    assert.equal(a.classify("r"), "reference");
    assert.ok(a.raw("r") !== null);
    a.close();
  });
}
test("request error precedes everything; hidden keys are missing before lookup", async () => {
  const buf = withPayload(0x7a76, Buffer.from([0xff]));
  await opErr(buf, "get", "r", "request", { type: "range", start: 2n, end: 1n });
  await opErr(buf, "get", "nope", "request", { type: "range", start: 2n, end: 1n });
  const a = Archive.open(save(buf));
  assert.equal(await a.get("__vz__/sources"), null);
  a.close();
});

// ---------------- resolution errors ----------------

const resolve1 = (s: Source, r = rng.src(0, 0, 2), extraEntries: WriterInput["entries"] = []) =>
  replaceFormatBody(
    buildBuf({ sources: [src.data(b("placeholder"))], entries: [{ key: "r", ranges: [r] }, ...extraEntries] }),
    "sources",
    zlib.deflateRawSync(encodeSourceTable([s])),
  );
const mtime = BigInt(Math.floor(fs.statSync(path.join(dir, "data.bin")).mtimeMs / 1000));
const resolutionCases: Record<string, () => Buffer> = {
  "file does not exist": () => resolve1(src.url("missing.bin")),
  "source shorter than the range": () => resolve1(src.url("data.bin"), rng.src(0, 8, 3)),
  "size pin mismatch": () => resolve1(src.url("data.bin", { size: 11n })),
  "etag pin on file:": () => resolve1(src.url("data.bin", { etag: '"x"' })),
  "modified_not_after pin fails": () => resolve1(src.url("data.bin", { modifiedNotAfter: mtime - 1n })),
  "invalid URI reference": () => resolve1(src.url("a b.bin")),
  "non-ASCII URL": () => resolve1(src.url("é.bin")),
  "unsupported scheme": () => resolve1(src.url("ftp://example.com/x")),
  "file: with remote host": () => resolve1(src.url("file://example.com/x")),
  "file: with query": () => resolve1(src.url("data.bin?")),
  "file: with encoded slash": () => resolve1(src.url("a%2Fb")),
  "file: with encoded dot-dot": () => resolve1(src.url("%2E%2E/data.bin")),
  "file: is a directory": () => resolve1(src.url(".")),
  "data source shorter": () => resolve1(src.data(b("x"))),
  "key source missing": () => resolve1(src.key("nope")),
  "key source is a reference": () => resolve1(src.key("r")),
  "key source is a format entry": () => resolve1(src.key("__vz__/sources")),
  "key source too short": () => resolve1(src.key("k"), rng.src(0, 0, 5), [{ key: "k", bytes: b("abc") }]),
  "key source with a body error": () =>
    mutateRec(resolve1(src.key("k"), rng.src(0, 0, 2), [{ key: "k", bytes: b("abc") }]), "k", (r) => r.fixed.writeUInt32LE(2, 24)),
  "key source with an entry error": () =>
    mutateRec(resolve1(src.key("k"), rng.src(0, 0, 2), [{ key: "k", bytes: b("abc") }]), "k", (r) => r.fixed.writeUInt16LE(77, 10)),
};
for (const [name, mk] of Object.entries(resolutionCases)) {
  test(`resolution error: ${name}`, async () => {
    await opErr(mk(), "get", "r", "resolution");
  });
}

test("resolution: ranges outside the window are not resolved; pins pass; hidden key source; self reference", async () => {
  const buf = buildBuf({
    sources: [src.url("missing.bin"), src.url("data.bin", { size: 10n, modifiedNotAfter: mtime }), src.key("__vz__/h"), src.url("#frag")],
    entries: [
      { key: "__vz__/h", bytes: b("HH"), compress: true },
      { key: "r", ranges: [rng.lit("ab"), rng.src(0, 0, 0), rng.src(1, 2, 3), rng.src(2, 0, 2), rng.src(0, 5, 5)] },
      { key: "self", ranges: [rng.src(3, 0, 2)] },
    ],
  });
  const a = Archive.open(save(buf));
  assert.equal(Buffer.from((await a.get("r", { type: "range", start: 0n, end: 7n }))!).toString(), "ab234HH");
  await assert.rejects(a.get("r"), (e: any) => e.cls === "resolution");
  assert.equal(Buffer.from((await a.get("self"))!).toString(), "PK");
  a.close();
});
