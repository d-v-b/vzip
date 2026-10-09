// The reader policy (spec/archive.md §8.7), range checksums (§5.2), pins (§6.1) and
// the recorded revision (§1.3) in the browser reader and writer: the twin of
// python/tests/test_policy.py.

import assert from "node:assert/strict";
import { test } from "node:test";
import {
  addressClass,
  Archive,
  checkPolicy,
  matchesPrefix,
  type Policy,
  type RangeFetcher,
  VzipError,
} from "../src/archive.ts";
import { crc32c, inflateRaw } from "../src/deflate.ts";
import { readHttpRange } from "../src/http.ts";
import { decodeTable } from "../src/protobuf.ts";
import { type ArchiveDesc, InvalidInputError, SPEC_REVISION, writeVzip } from "../src/writer.ts";

const BLOB = Uint8Array.from({ length: 16384 }, (_, i) => i % 256);
const BASE = "https://data.test/dir/archive.vzip";
const BLOB_URL = "https://data.test/dir/blob.bin";

const isError = (cls: string, re: RegExp) => (e: unknown) =>
  e instanceof VzipError && e.errorClass === cls && re.test(e.message);

/** An archive at BASE whose url sources read `body`, counting requests and bytes. */
async function open(desc: ArchiveDesc, policy: Policy = {}, body = BLOB, extra: Partial<Awaited<ReturnType<RangeFetcher>>> = {}) {
  const log = { requests: 0, bytes: 0 };
  const fetcher: RangeFetcher = async (_url, start, end) => {
    log.requests++;
    log.bytes += end - start;
    return { data: body.subarray(start, end), size: body.length, ...extra };
  };
  return { archive: await Archive.open(await writeVzip(desc), BASE, fetcher, policy), log };
}

/** Replaces `fetch` with one that answers every request with `make()`. */
async function withFetch<T>(make: (url: string) => Response, body: () => Promise<T>): Promise<T> {
  const saved = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request) => make(String(input))) as typeof fetch;
  try {
    return await body();
  } finally {
    globalThis.fetch = saved;
  }
}

function response(status: number, data: Uint8Array, headers: Record<string, string>, props: Record<string, unknown> = {}) {
  const r = new Response(data as BodyInit, { status, headers });
  for (const [k, v] of Object.entries(props)) Object.defineProperty(r, k, { value: v });
  return r;
}

test("crc32c has the check value of RFC 3720", () => {
  assert.equal(crc32c(new TextEncoder().encode("123456789")), 0xe3069283);
  assert.equal(crc32c(new Uint8Array()), 0);
});

test("the policy allows these URLs", () => {
  const local = "file:///d/a.vzip";
  const remote = "https://h.example/a.vzip";
  const cases: [Policy, string, string][] = [
    [{}, local, "file:///d/blob.bin"],
    [{}, local, "https://g.example/blob.bin"], // a local archive may read the web
    [{}, remote, "https://g.example/blob.bin"],
    [{}, remote, "http://g.example/blob.bin"], // http and https are one scheme
    [{}, "HTTP://h.example/a.vzip", "HTTPS://g.example/blob.bin"],
    [{ schemes: ["s3"] }, remote, "s3://bucket/blob.bin"],
    [{ schemes: ["S3"] }, local, "s3://bucket/blob.bin"],
    [{ allowFilesFromRemoteArchives: true }, remote, "file:///d/blob.bin"],
    [{ prefixes: ["https://data.example/a/"] }, local, "https://data.example/a/x.bin"],
    [{ prefixes: ["https://data.example/a"] }, local, "https://data.example/a/x.bin"],
    [{ prefixes: ["https://data.example/a"] }, local, "https://data.example/a?x=1"],
    [{ prefixes: ["file:///d/", "https://g/"] }, remote, "file:///d/x"],
    // public literals, and names (which a browser cannot resolve)
    [{}, remote, "http://93.184.215.14/x"],
    [{}, remote, "http://[2606:4700::1111]/x"],
    [{}, remote, "http://internal.example/x"],
    // the unsafe opt-out
    [{ allowPrivateHosts: true }, remote, "http://169.254.169.254/latest/meta-data"],
    [{ allowPrivateHosts: true }, remote, "http://127.0.0.1/x"],
    [{ allowPrivateHosts: true }, local, "http://localhost:8000/x"],
    [{ allowPrivateHosts: true }, local, "http://10.1.2.3/x"],
    [{ allowPrivateHosts: true }, local, "http://[fd00::1]/x"],
    [{ allowPrivateHosts: true }, local, "http://0.0.0.0/x"],
    [{ allowPrivateHosts: true, prefixes: ["http://10.0.0.1/"] }, remote, "http://10.0.0.1/x"],
  ];
  for (const [policy, base, url] of cases) checkPolicy(policy, base, url);
});

test("addressClass", () => {
  const cases: Record<string, string | undefined> = {
    "127.0.0.1": "loopback", "127.255.0.9": "loopback", "::1": "loopback",
    "10.0.0.1": "private", "172.16.0.1": "private", "172.31.255.255": "private",
    "192.168.1.1": "private", "fc00::1": "private", "fdff::1": "private",
    "169.254.169.254": "link-local", "fe80::1": "link-local", "fe80::1%en0": "link-local",
    "0.0.0.0": "special", "::": "special", "100.64.0.1": "special", "192.0.2.1": "special",
    "198.18.0.1": "special", "224.0.0.1": "special", "239.1.2.3": "special",
    "240.0.0.1": "special", "255.255.255.255": "special", "ff02::1": "special",
    "2001:db8::1": "special", "::a00:1": "special", "fec0::1": "special",
    // IPv4 embedded in IPv6: the IPv4 address's class
    "::ffff:127.0.0.1": "loopback", "::ffff:a00:1": "private",
    "::ffff:169.254.169.254": "link-local", "64:ff9b::a9fe:a9fe": "link-local",
    "2002:a00:1::1": "private", "::ffff:8.8.8.8": "public",
    "8.8.8.8": "public", "172.32.0.1": "public", "2606:4700::1111": "public",
    // not addresses
    "example.test": undefined, "1.2.3": undefined, "256.0.0.1": undefined, "1::2::3": undefined,
    "1:2:3:4:5:6:7:8:9": undefined,
  };
  assert.deepEqual(
    Object.fromEntries(Object.keys(cases).map((a) => [a, addressClass(a)])),
    cases,
  );
});

test("matchesPrefix", () => {
  const cases: [string, string, boolean][] = [
    ["https://h/a/x", "https://h/a/", true],
    ["https://h/a/x", "https://h/a", true],
    ["https://h/a", "https://h/a", true],
    ["https://h/a#f", "https://h/a", true],
    ["https://h/ab", "https://h/a", false],
    ["https://h.evil.test/", "https://h", false],
    ["https://h/a/%2E%2E/b", "https://h/a/", false],
    ["https://h/a/%2fb", "https://h/a/", false],
    ["https://h/a/%5Cb", "https://h/a/", false],
    ["https://h/a/b?q=%2F", "https://h/a/", true], // the query is not a path
    ["HTTPS://h/a/x", "https://h/a/", false], // exact comparison fails closed
  ];
  assert.deepEqual(cases.map(([u, p]) => matchesPrefix(u, p)), cases.map(([, , want]) => want));
});

test("reads checksummed ranges and pinned sources, and the recorded revision", async () => {
  const utf8 = new TextEncoder();
  const good = crc32c(BLOB.subarray(100, 200));
  const { archive, log } = await open({
    sources: [
      { url: "blob.bin", size: BigInt(BLOB.length), etag: '"v1"' },
      { data: utf8.encode("HEADER") },
    ],
    entries: [
      { key: "crc", ranges: [{ source: 0, offset: 100n, length: 100n, crc32c: good }] },
      {
        key: "concat",
        ranges: [
          { source: 1, offset: 0n, length: 6n, crc32c: crc32c(utf8.encode("HEADER")) },
          { source: 0, offset: 0n, length: 10n, crc32c: crc32c(BLOB.subarray(0, 10)) },
          { data: utf8.encode("!") },
        ],
      },
      { key: "plain", ranges: [{ source: 0, offset: 5n, length: 5n }] },
    ],
  });
  const cat = (...xs: Uint8Array[]) => Uint8Array.from(xs.flatMap((x) => [...x]));
  const cases: [string, number | undefined, number | undefined, Uint8Array][] = [
    ["crc", undefined, undefined, BLOB.subarray(100, 200)],
    ["crc", 10, 20, BLOB.subarray(110, 120)],
    ["crc", 90, 100, BLOB.subarray(190, 200)],
    ["concat", undefined, undefined, cat(utf8.encode("HEADER"), BLOB.subarray(0, 10), utf8.encode("!"))],
    ["concat", 4, 8, cat(utf8.encode("ER"), BLOB.subarray(0, 2))],
    ["plain", undefined, undefined, BLOB.subarray(5, 10)],
  ];
  for (const [key, a, b, want] of cases) assert.deepEqual(await archive.read(key, a, b), want, `${key} ${a}-${b}`);
  assert.equal(archive.revision, SPEC_REVISION);
  // a partial window of a checksummed range reads the whole range
  log.bytes = 0;
  await archive.read("crc", 0, 1);
  assert.equal(log.bytes, 100);
});

test("skips a size pin the response hides only when the policy allows it", async () => {
  const desc: ArchiveDesc = {
    sources: [{ url: "blob.bin", size: BigInt(BLOB.length) }],
    entries: [{ key: "r", ranges: [{ source: 0, offset: 0n, length: 4n }] }],
  };
  const hidden = { size: undefined, hidden: true };
  const { archive } = await open(desc, { unverifiablePins: true }, BLOB, hidden);
  assert.deepEqual(await archive.read("r"), BLOB.subarray(0, 4));
  const strict = (await open(desc, {}, BLOB, hidden)).archive;
  await assert.rejects(strict.read("r"), isError("resolution", /does not give/));
});

test("the writer records the revision and rejects a checksum on a literal range", async () => {
  const bytes = await writeVzip({ sources: [{ url: "a", size: 5n }], entries: [] });
  const view = new DataView(bytes.buffer);
  const at = Number(view.getBigUint64(bytes.length - 22 + 6, true));
  const n = Number(view.getBigUint64(bytes.length - 22 + 14, true));
  const table = decodeTable(await inflateRaw(bytes.subarray(at, at + n)));
  assert.deepEqual(table, { sources: [{ url: "a", size: 5n }], revision: SPEC_REVISION });
  await assert.rejects(
    writeVzip({ sources: [], entries: [{ key: "k", ranges: [{ data: new Uint8Array(1), crc32c: 0 } as never] }] }),
    (e) => e instanceof InvalidInputError && /crc32c/.test(e.message),
  );
});

test("refuses a file source of a remote archive", async () => {
  for (const policy of [{}, { schemes: ["s3"] }]) {
    assert.throws(() => checkPolicy(policy, BASE, "file:///etc/passwd"), isError("resolution", /may not read local files/));
  }
  const desc: ArchiveDesc = { sources: [{ url: "file:///etc/hosts" }], entries: [{ key: "r", ranges: [{ source: 0, offset: 0n, length: 4n }] }] };
  const { archive, log } = await open(desc);
  await assert.rejects(archive.read("r"), isError("resolution", /may not read local files/));
  assert.equal(log.requests, 0);
});

test("refuses an unlisted scheme", () => {
  for (const base of ["file:///d/a.vzip", BASE]) {
    for (const url of ["s3://bucket/x", "gs://bucket/x", "data:,abc", "ftp://h.example/x"]) {
      assert.throws(() => checkPolicy({ schemes: ["az"] }, base, url), isError("resolution", /is not allowed/));
    }
  }
});

test("the policy refuses file in schemes", async () => {
  assert.throws(() => checkPolicy({ schemes: ["FILE"] }, BASE, BLOB_URL), /allowFilesFromRemoteArchives/);
  await assert.rejects(Archive.open(await writeVzip({ sources: [], entries: [] }), BASE, undefined, { schemes: ["file"] }), /allowFilesFromRemoteArchives/);
});

test("refuses a private host written in the URL", async () => {
  const remote = "https://h.example/a.vzip";
  const local = "file:///d/a.vzip";
  const cases: [string, string, string][] = [
    [remote, "http://127.0.0.1/x", "loopback"],
    [remote, "http://localhost:8000/x", "loopback"],
    [remote, "http://[::ffff:127.0.0.1]/x", "loopback"],
    [remote, "http://2130706433/x", "loopback"], // decimal: the URL parser normalizes it
    [remote, "http://0x7f.1/x", "loopback"],
    [remote, "http://10.0.0.1/x", "private"],
    [remote, "https://[fd12::1]/x", "private"],
    [remote, "http://169.254.169.254/latest/meta-data", "link-local"],
    [remote, "http://0251.0376.0251.0376/x", "link-local"], // octal
    [remote, "http://[fe80::1]/x", "link-local"],
    [remote, "http://0.0.0.0/x", "special"],
    [remote, "http://[::]/x", "special"],
    [remote, "http://224.0.0.1/x", "special"],
    [local, "http://localhost:8000/x", "loopback"],
    [local, "http://a.localhost/x", "loopback"],
    [local, "http://127.0.0.1/x", "loopback"],
    [local, "http://10.1.2.3/x", "private"],
    [local, "http://[::ffff:192.168.0.1]/x", "private"],
    [local, "http://169.254.169.254/x", "link-local"],
    [local, "http://[64:ff9b::a9fe:a9fe]/x", "link-local"],
    [local, "http://0.0.0.0/x", "special"],
    // an archive on a host of that class is no exception
    ["http://127.0.0.1:8000/a.vzip", "http://127.0.0.1:8000/x", "loopback"],
    ["http://localhost/a.vzip", "http://[::1]/x", "loopback"],
    ["http://10.0.0.1/a.vzip", "http://10.0.0.1/x", "private"],
  ];
  for (const [base, url, cls] of cases) {
    assert.throws(() => checkPolicy({}, base, url), isError("resolution", new RegExp(`is a ${cls} address.*allowPrivateHosts`)), url);
    // neither a prefix list nor another unsafe setting lifts rule 3
    for (const policy of [{ prefixes: [url] }, { allowUncheckedProxy: true }]) {
      assert.throws(() => checkPolicy(policy, base, url), isError("resolution", new RegExp(`is a ${cls} address`)), url);
    }
  }
  // through an archive: no request is made
  const desc: ArchiveDesc = { sources: [{ url: "http://169.254.169.254/x" }], entries: [{ key: "r", ranges: [{ source: 0, offset: 0n, length: 4n }] }] };
  const { archive, log } = await open(desc);
  await assert.rejects(archive.read("r"), isError("resolution", /is a link-local address/));
  assert.equal(log.requests, 0);
});

test("refuses a redirect to a private host", async () => {
  // fetch follows the redirect itself: the final URL is checked (spec §6.2, §8.7)
  const allow = (u: string) => checkPolicy({}, BASE, u);
  for (const url of ["http://169.254.169.254/latest/meta-data", "http://[::ffff:a00:1]/x"]) {
    await withFetch(
      () => response(206, BLOB.subarray(0, 4), { "Content-Range": `bytes 0-3/${BLOB.length}` }, { redirected: true, url }),
      () => assert.rejects(readHttpRange(BLOB_URL, 0, 4, {}, undefined, { allow }), /is a (link-local|private) address/),
    );
  }
});

test("refuses a URL outside the prefixes", async () => {
  const desc: ArchiveDesc = { sources: [{ url: "blob.bin" }], entries: [{ key: "r", ranges: [{ source: 0, offset: 0n, length: 4n }] }] };
  const { archive, log } = await open(desc, { prefixes: ["https://data.test/other/"] });
  await assert.rejects(archive.read("r"), isError("resolution", /prefix/));
  assert.equal(log.requests, 0);
});

test("refuses a redirect outside the prefixes", async () => {
  const allow = (u: string) => checkPolicy({ prefixes: ["https://data.test/"] }, BASE, u);
  await withFetch(
    () => response(206, BLOB.subarray(0, 4), { "Content-Range": `bytes 0-3/${BLOB.length}` }, {
      redirected: true, url: "http://169.254.169.254/latest/meta-data",
    }),
    () => assert.rejects(readHttpRange(BLOB_URL, 0, 4, {}, undefined, { allow }), /prefix/),
  );
});

test("refuses too many sources", async () => {
  const bytes = await writeVzip({ sources: [{ data: new Uint8Array(1) }, { data: new Uint8Array(1) }, { data: new Uint8Array(1) }], entries: [] });
  await assert.rejects(Archive.open(bytes, BASE, undefined, { maxSources: 2 }), isError("archive", /3 sources/));
});

test("refuses too many reads", async () => {
  const far = (o: bigint) => ({ source: 0, offset: o, length: 1n });
  const desc: ArchiveDesc = { sources: [{ url: "blob.bin" }], entries: [{ key: "r", ranges: [far(0n), far(200_000n), far(400_000n)] }] };
  const { archive, log } = await open(desc, { maxReads: 2 });
  await assert.rejects(archive.read("r"), isError("request", /3 reads/));
  assert.equal(log.requests, 0);
});

test("refuses an inflation bomb in the source table", async () => {
  const bytes = await writeVzip({ sources: [{ data: new Uint8Array(4 << 20) }], entries: [] });
  await assert.rejects(Archive.open(bytes, BASE, undefined, { maxFormatEntry: 1 << 20 }), isError("archive", /inflates to more than/));
});

test("refuses a bytes entry that inflates past its size", async () => {
  const bytes = await writeVzip({ sources: [], entries: [{ key: "bomb", bytes: new Uint8Array(1 << 20), compress: true }] });
  const view = new DataView(bytes.buffer);
  const cd = view.getUint32(bytes.length - 22 - 22 + 16, true);
  view.setUint32(cd + 24, 10, true); // the record now says 10 bytes
  const archive = await Archive.open(bytes, BASE);
  await assert.rejects(archive.read("bomb"), isError("body", /inflates to more than 10 bytes/));
});

test("detects a size mismatch", async () => {
  const desc: ArchiveDesc = { sources: [{ url: "blob.bin", size: BigInt(BLOB.length) }], entries: [{ key: "r", ranges: [{ source: 0, offset: 0n, length: 4n }] }] };
  const longer = Uint8Array.from([...BLOB, 1, 2, 3]);
  const { archive } = await open(desc, {}, longer);
  await assert.rejects(archive.read("r"), isError("resolution", /the source changed/));
});

test("detects an ETag mismatch", async () => {
  await withFetch(
    () => response(206, BLOB.subarray(0, 4), { "Content-Range": `bytes 0-3/${BLOB.length}`, ETag: '"v2"' }),
    () => assert.rejects(readHttpRange(BLOB_URL, 0, 4, { etag: '"v1"' }), /the source changed/),
  );
});

test("detects a crc32c mismatch", async () => {
  const desc: ArchiveDesc = {
    sources: [{ url: "blob.bin" }],
    entries: [{ key: "r", ranges: [{ source: 0, offset: 0n, length: 100n, crc32c: crc32c(BLOB.subarray(0, 100)) }] }],
  };
  const changed = Uint8Array.from(BLOB);
  changed[0] ^= 0xff; // same size, no ETag pin: only the checksum sees it
  const { archive } = await open(desc, {}, changed);
  await assert.rejects(archive.read("r", 50, 60), isError("resolution", /crc32c/));
});

test("rejects an unknown format version", async () => {
  const bytes = await writeVzip({ sources: [], entries: [] });
  bytes[bytes.length - 22 + 5] = "1".charCodeAt(0);
  await assert.rejects(Archive.open(bytes, BASE), isError("archive", /unsupported version vzip\/1/));
});
