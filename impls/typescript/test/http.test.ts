import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import * as http from "node:http";
import type { AddressInfo } from "node:net";
import * as zlib from "node:zlib";
import * as fs from "node:fs";
import * as path from "node:path";
import { Archive } from "../src/reader.ts";
import { VzError } from "../src/errors.ts";
import { imfFixdate } from "../src/fetch.ts";
import { type RawEntry, fLen, msg, rangeMsg, rawZip, refExtra, srcUrl, table, tmpdir, writeDesc } from "./helpers.ts";

const DATA = Buffer.from("0123456789abcdefghijklmnopqrstuvwxyz");
const ETAG = '"v1"';
const MTIME = 1_700_000_000; // seconds
const requests: { method: string; url: string; headers: http.IncomingHttpHeaders }[] = [];
let server: http.Server;
let base = "";

function handler(req: http.IncomingMessage, res: http.ServerResponse): void {
  requests.push({ method: req.method!, url: req.url!, headers: req.headers });
  const u = req.url!;
  if (u === "/redirect") {
    res.writeHead(302, { Location: "/a.bin" }).end();
    return;
  }
  if (u.startsWith("/loop")) {
    const n = Number(u.slice(5) || "0");
    res.writeHead(307, { Location: `/loop${n + 1}` }).end();
    return;
  }
  if (u === "/notfound") {
    res.writeHead(404).end();
    return;
  }
  if (u === "/gzip") {
    res.writeHead(200, { "Content-Encoding": "gzip" }).end(zlib.gzipSync(DATA));
    return;
  }
  if (u === "/full") {
    res.writeHead(200).end(DATA);
    return;
  }
  // Conditional headers.
  const im = req.headers["if-match"];
  if (im !== undefined && im !== ETAG) {
    res.writeHead(412).end();
    return;
  }
  const ius = req.headers["if-unmodified-since"];
  if (ius !== undefined && MTIME > Date.parse(ius) / 1000) {
    res.writeHead(412).end();
    return;
  }
  const m = /^bytes=(\d+)-(\d+)$/.exec(String(req.headers.range ?? ""));
  if (!m) {
    res.writeHead(200).end(DATA);
    return;
  }
  const a = Number(m[1]);
  let z = Number(m[2]);
  if (a >= DATA.length) {
    res.writeHead(416, { "Content-Range": `bytes */${DATA.length}` }).end();
    return;
  }
  z = Math.min(z, DATA.length - 1);
  if (u === "/wrongrange") {
    res.writeHead(206, { "Content-Range": `bytes ${a + 1}-${z}/${DATA.length}` }).end(DATA.subarray(a + 1, z + 1));
    return;
  }
  const total = u === "/star" ? "*" : String(DATA.length);
  res.writeHead(206, { "Content-Range": `bytes ${a}-${z}/${total}`, ETag: ETAG }).end(DATA.subarray(a, z + 1));
}

before(async () => {
  server = http.createServer(handler);
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", () => r()));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
after(() => server.close());

async function cls(p: Promise<unknown>): Promise<string> {
  try {
    await p;
    return "ok";
  } catch (e) {
    if (e instanceof VzError) return e.cls;
    throw e;
  }
}

test("HTTP sources: ranges, pins, status handling", async () => {
  const dir = tmpdir();
  const srcs = [
    srcUrl(`${base}/a.bin`), // 0
    srcUrl(`${base}/a.bin`, { size: 36, etag: ETAG, mna: MTIME }), // 1 all pins pass
    srcUrl(`${base}/a.bin`, { size: 37 }), // 2 size fails
    srcUrl(`${base}/a.bin`, { etag: '"v2"' }), // 3 etag fails (412)
    srcUrl(`${base}/a.bin`, { mna: MTIME - 1 }), // 4 mna fails (412)
    srcUrl(`${base}/star`), // 5 ok
    srcUrl(`${base}/star`, { size: 36 }), // 6 size cannot be checked
    srcUrl(`${base}/full`, { size: 36 }), // 7 200 accepted
    srcUrl(`${base}/full`, { size: 35 }), // 8 200 size fails
    srcUrl(`${base}/gzip`), // 9 content-encoding
    srcUrl(`${base}/notfound`), // 10 404
    srcUrl(`${base}/wrongrange`), // 11 wrong range
    srcUrl(`${base}/redirect`), // 12 redirect ok
    srcUrl(`${base}/loop`), // 13 too many redirects
    srcUrl(`${base}/a.bin`, { mna: 253402300800n }), // 14 year 10000: cannot send
    srcUrl(`HTTP://127.0.0.1:${(server.address() as AddressInfo).port}/a.bin#frag`), // 15 ok
  ];
  const entries: RawEntry[] = srcs.map((_, i) => ({ name: `s${i}`, extra: refExtra(rangeMsg(i, 10, 4)) }));
  entries.push({ name: "past", extra: refExtra(rangeMsg(0, 34, 4)) });
  entries.push({ name: "beyond", extra: refExtra(rangeMsg(0, 40, 4)) });
  entries.push({
    name: "concat",
    extra: refExtra(msg(fLen(1, rangeMsg(0, 0, 2)), fLen(1, msg(fLen(5, "-"))), fLen(1, rangeMsg(1, 34, 2))), true),
  });
  const p = path.join(dir, "h.vzip");
  fs.writeFileSync(p, rawZip(entries, { sources: table(...srcs) }));
  const a = Archive.open(p);
  const ok = new Set([0, 1, 5, 7, 12, 15]);
  for (let i = 0; i < srcs.length; i++) {
    requests.length = 0;
    if (ok.has(i)) {
      assert.equal(Buffer.from((await a.get(`s${i}`))!).toString(), "abcd", `s${i}`);
    } else {
      assert.equal(await cls(a.get(`s${i}`)), "resolution", `s${i}`);
    }
    for (const r of requests) {
      assert.equal(r.method, "GET");
      assert.equal(r.headers.range, "bytes=10-13");
      assert.equal(r.headers["accept-encoding"], "identity");
    }
    if (i === 1) {
      assert.equal(requests.length, 1);
      assert.equal(requests[0].headers["if-match"], ETAG);
      assert.equal(requests[0].headers["if-unmodified-since"], "Tue, 14 Nov 2023 22:13:20 GMT");
    }
    if (i === 14) assert.equal(requests.length, 0);
  }
  assert.equal(await cls(a.get("past")), "resolution");
  assert.equal(await cls(a.get("beyond")), "resolution");
  requests.length = 0;
  assert.equal(Buffer.from((await a.get("concat", { type: "range", start: 1n, end: 4n }))!).toString(), "1-y");
  assert.deepEqual(requests.map((r) => r.headers.range), ["bytes=1-1", "bytes=34-34"]);
  // An unreachable source doesn't affect other keys or open.
  assert.equal(await cls(a.get("s10", { type: "range", start: 0n, end: 0n })), "ok");
});

test("HTTP sources via the CLI write/read round trip", () => {
  const dir = tmpdir();
  const w = writeDesc(dir, { sources: [{ url: `${base}/a.bin`, size: 36 }], entries: [{ key: "k", ranges: [{ source: 0, offset: 0, length: 3 }] }] });
  assert.equal(w.status, 0, w.stderr);
  // The CLI runs in a child process; the server must stay responsive, so use an async child.
  return new Promise<void>((resolve, reject) => {
    const qp = path.join(dir, "q.json");
    fs.writeFileSync(qp, JSON.stringify([{ op: "get", key: "k" }]));
    import("node:child_process").then(({ execFile }) => {
      execFile(path.resolve(dir, "../../vzip"), ["read", w.out, qp], (err, stdout) => {
        if (err) return reject(err);
        try {
          assert.deepEqual(JSON.parse(stdout).results, [{ ok: true, value: Buffer.from("012").toString("hex") }]);
          resolve();
        } catch (e) {
          reject(e);
        }
      });
    });
  });
});

test("IMF-fixdate formatting and bounds", () => {
  assert.equal(imfFixdate(0n), "Thu, 01 Jan 1970 00:00:00 GMT");
  assert.equal(imfFixdate(-62135596800n), "Mon, 01 Jan 0001 00:00:00 GMT");
  assert.equal(imfFixdate(253402300799n), "Fri, 31 Dec 9999 23:59:59 GMT");
  assert.equal(imfFixdate(-62135596801n), null);
  assert.equal(imfFixdate(253402300800n), null);
});

