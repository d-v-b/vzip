import assert from "node:assert/strict";
import { test } from "node:test";
import { Archive, mergeRuns, type ReadOptions, VzipError } from "../src/archive.ts";
import { type ArchiveDesc, writeVzip } from "../src/writer.ts";

const utf8 = (s: string) => new TextEncoder().encode(s);
const text = (b: Uint8Array | undefined) => new TextDecoder().decode(b);
const remote = utf8("0123456789abcdefghij");

async function open(desc: ArchiveDesc) {
  const bytes = await writeVzip(desc);
  return Archive.open(bytes, "https://data.test/dir/archive.vzip", async (url, start, end) => {
    assert.equal(url, "https://data.test/dir/blob.bin");
    return { data: remote.subarray(start, end), size: remote.length };
  });
}

test("reads back what the writer wrote", async () => {
  for (const mirror of [true, false]) {
    const a = await open({
      mirror,
      sources: [{ url: "blob.bin" }, { key: "__vz__/hdr" }, { data: utf8("DATA") }],
      entries: [
        { key: "plain", bytes: utf8("hello") },
        { key: "packed", bytes: utf8("x".repeat(1000)), compress: true },
        { key: "__vz__/hdr", bytes: utf8("HEADER") },
        { key: "url", ranges: [{ source: 0, offset: 10n, length: 5n }] },
        {
          key: "concat",
          ranges: [
            { source: 1, offset: 0n, length: 3n },
            { data: utf8("-") },
            { source: 2, offset: 1n, length: 2n },
            { source: 0, offset: 0n, length: 2n },
          ],
        },
        { key: "empty", ranges: [] },
      ],
    });
    assert.equal(text(await a.read("plain")), "hello");
    assert.equal(text(await a.read("packed")), "x".repeat(1000));
    assert.equal(text(await a.read("url")), "abcde");
    assert.equal(text(await a.read("concat")), "HEA-AT01");
    assert.equal(text(await a.read("concat", 2, 6)), "A-AT");
    assert.equal(text(await a.read("empty")), "");
    assert.equal(await a.read("__vz__/hdr"), undefined, "hidden");
    assert.equal(await a.read("absent"), undefined);
    assert.deepEqual(a.keys(), ["concat", "empty", "packed", "plain", "url"]);
  }
});

test("writes zip64 end records for 65535 or more entries", async () => {
  const entries = Array.from({ length: 70000 }, (_, i) => ({
    key: `c/${i}`,
    ranges: [{ source: 0, offset: BigInt(i % 10), length: 1n }],
  }));
  const bytes = await writeVzip({ sources: [{ url: "blob.bin" }], entries });
  const view = new DataView(bytes.buffer);
  const eocd = bytes.length - 44;
  assert.equal(view.getUint16(eocd + 10, true), 0xffff);
  assert.equal(view.getUint32(eocd - 20, true), 0x07064b50, "zip64 locator");
  const a = await Archive.open(bytes, "https://data.test/dir/a.vzip", async (_u, s, e) => ({
    data: remote.subarray(s, e),
    size: undefined,
  }));
  assert.equal(a.keys().length, 70000);
  assert.equal(text(await a.read("c/69999")), "9");
});

test("rejects a file that is not a vzip archive", async () => {
  await assert.rejects(Archive.open(utf8("not a zip at all, not even close"), "https://x/"), (e) =>
    e instanceof VzipError && e.errorClass === "archive");
});

test("rejects an unsupported format version", async () => {
  const bytes = await writeVzip({ sources: [], entries: [] });
  bytes[bytes.length - 22 + 5] = "9".charCodeAt(0);
  await assert.rejects(Archive.open(bytes, "https://x/"), /unsupported version vzip\/9/);
});

test("reports an entry error for a reference stored with DEFLATE", async () => {
  const bytes = await writeVzip({ sources: [{ data: utf8("abc") }], entries: [{ key: "r", ranges: [{ source: 0, offset: 0n, length: 1n }] }] });
  const view = new DataView(bytes.buffer);
  const cd = bytes.length - 22 - 22 - view.getUint32(bytes.length - 44 + 12, true);
  view.setUint16(cd + 10, 8, true); // the reference's central directory record
  const a = await Archive.open(bytes, "https://x/");
  await assert.rejects(a.read("r"), (e) => e instanceof VzipError && e.errorClass === "entry");
});

test("reports a payload error for a malformed reference", async () => {
  const bytes = await writeVzip({ sources: [{ data: utf8("abc") }], entries: [{ key: "r", ranges: [{ source: 0, offset: 0n, length: 1n }] }] });
  // Payload `20 01` (length = 1) becomes `0b 01`: wire type 3.
  const at = bytes.indexOf(0x7a, bytes.length - 200);
  const payload = bytes.indexOf(0x20, at);
  bytes[payload] = 0x0b;
  const a = await Archive.open(bytes, "https://x/");
  await assert.rejects(a.read("r"), (e) => e instanceof VzipError && e.errorClass === "payload");
});

test("reports a resolution error for a source with an unsupported scheme", async () => {
  const a = await open({ sources: [{ url: "s3://bucket/blob.bin" }], entries: [{ key: "r", ranges: [{ source: 0, offset: 0n, length: 1n }] }] });
  await assert.rejects(a.read("r"), (e) => e instanceof VzipError && e.errorClass === "resolution");
});

test("reports a request error for a range past the end", async () => {
  const a = await open({ sources: [], entries: [{ key: "b", bytes: utf8("abc") }] });
  await assert.rejects(a.read("b", 2, 5), (e) => e instanceof VzipError && e.errorClass === "request");
});

test("combines nearby reads of a url source into one request", async () => {
  const blob = Uint8Array.from({ length: 300_000 }, (_, i) => i % 251);
  const requests: string[] = [];
  const openWith = async (desc: ArchiveDesc) =>
    Archive.open(await writeVzip(desc), "https://data.test/dir/archive.vzip", async (url, s, e) => {
      requests.push(`${url.split("/").pop()} ${s}-${e}`);
      return { data: blob.subarray(s, e), size: blob.length };
    });
  const expect = (ranges: [number, number][]) =>
    Uint8Array.from(ranges.flatMap(([o, n]) => [...blob.subarray(o, o + n)]));
  const rows = Array.from({ length: 100 }, (_, i) => [i * 103, 100] as [number, number]); // 3 bytes of padding per row
  const cases: { ranges: [number, number][]; sources?: number[]; requests: number }[] = [
    { ranges: rows, requests: 1 },
    { ranges: [[0, 10], [200_000, 10]], requests: 2 }, // more than MERGE_GAP apart
    { ranges: [...rows].reverse(), requests: 1 }, // order on the wire does not matter
    { ranges: [[0, 10], [20, 10]], sources: [0, 1], requests: 2 }, // never across sources
  ];
  for (const c of cases) {
    requests.length = 0;
    const a = await openWith({
      sources: [{ url: "blob.bin" }, { url: "other.bin" }],
      entries: [{
        key: "v",
        ranges: c.ranges.map(([o, n], i) => ({ source: c.sources?.[i] ?? 0, offset: BigInt(o), length: BigInt(n) })),
      }],
    });
    assert.deepEqual(await a.read("v"), expect(c.ranges));
    assert.equal(requests.length, c.requests, requests.join(", "));
  }
});

test("plans the reads of values requested together, with read options", async () => {
  // Two chunks of a row-major strip: rows of 4 bytes, each chunk 2 bytes of each row
  const blob = Uint8Array.from({ length: 64 }, (_, i) => i);
  const rows = 8;
  const chunk = (x: number) =>
    Array.from({ length: rows }, (_, y) => ({ source: 0, offset: BigInt(y * 4 + x), length: 2n }));
  const bytes = await writeVzip({
    sources: [{ url: "blob.bin" }],
    entries: [{ key: "a", ranges: chunk(0) }, { key: "b", ranges: chunk(2) }],
  });
  const expect = (x: number) =>
    Uint8Array.from({ length: 2 * rows }, (_, i) => Math.floor(i / 2) * 4 + x + (i % 2));
  const cases: { options: ReadOptions; requests: number; again: number }[] = [
    { options: {}, requests: 2, again: 2 }, // each value alone, rows 2 bytes apart merge
    { options: { mergeGap: 0 }, requests: 2 * rows, again: 2 * rows },
    { options: { mergeGap: 0, batchWindowMs: 1 }, requests: 1, again: 1 }, // the rows meet
    { options: { mergeGap: 0, batchWindowMs: 1, spanCacheBytes: 1 << 10 }, requests: 1, again: 0 },
    { options: { mergeGap: 0, spanCacheBytes: 1 << 10 }, requests: 2 * rows, again: 0 },
  ];
  for (const c of cases) {
    let n = 0;
    const a = await Archive.open(bytes, "https://data.test/dir/archive.vzip", async (_u, s, e) => {
      n++;
      return { data: blob.subarray(s, e), size: blob.length };
    }, c.options);
    assert.deepEqual(await Promise.all([a.read("a"), a.read("b")]), [expect(0), expect(2)]);
    assert.equal(n, c.requests, JSON.stringify(c.options));
    n = 0;
    assert.deepEqual(await Promise.all([a.read("a"), a.read("b")]), [expect(0), expect(2)]);
    assert.equal(n, c.again, `again ${JSON.stringify(c.options)}`);
    assert.deepEqual(await a.read("b", 1, 5), expect(2).subarray(1, 5));
  }
  assert.deepEqual(
    mergeRuns([{ source: 0, offset: 10n, end: 12n }, { source: 0, offset: 0n, end: 4n }, { source: 1, offset: 4n, end: 5n }], 6),
    [{ source: 0, offset: 0n, end: 12n }, { source: 1, offset: 4n, end: 5n }],
  );
});
