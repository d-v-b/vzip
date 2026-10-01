// HTTP source resolution against a local server (spec §6.1, §6.2).
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import * as http from "node:http";
import type { AddressInfo } from "node:net";
import { Archive } from "../src/reader.ts";
import { VzipError } from "../src/errors.ts";
import type { Source } from "../src/proto.ts";
import { buildBuf, rng, src, tmpDir, writeTmp } from "./helpers.ts";

const BODY = Buffer.from("0123456789abcdefghijklmnopqrstuvwxyz");
const LM = "Sun, 06 Nov 1994 08:49:37 GMT";
const LM_SECS = 784111777n;
const ETAG = '"v1"';
let server: http.Server;
let origin = "";
const log: { method: string; url: string; headers: http.IncomingHttpHeaders }[] = [];

function serveRange(req: http.IncomingMessage, res: http.ServerResponse, opts: { etag?: string | null; lm?: string | null; star?: boolean; shift?: number } = {}) {
  const etag = opts.etag === undefined ? ETAG : opts.etag;
  const lm = opts.lm === undefined ? LM : opts.lm;
  const h: Record<string, string> = {};
  if (etag !== null) h.ETag = etag;
  if (lm !== null) h["Last-Modified"] = lm;
  const im = req.headers["if-match"];
  if (im !== undefined && im !== etag) {
    res.writeHead(412, h).end();
    return;
  }
  const ius = req.headers["if-unmodified-since"];
  if (ius !== undefined && lm !== null && Date.parse(lm) > Date.parse(ius)) {
    res.writeHead(412, h).end();
    return;
  }
  const m = /^bytes=(\d+)-(\d+)$/.exec(req.headers.range ?? "");
  if (!m) {
    res.writeHead(200, h).end(BODY);
    return;
  }
  const a = Number(m[1]) + (opts.shift ?? 0);
  const z = Math.min(Number(m[2]) + (opts.shift ?? 0), BODY.length - 1);
  if (a >= BODY.length) {
    res.writeHead(416, { ...h, "Content-Range": `bytes */${BODY.length}` }).end();
    return;
  }
  h["Content-Range"] = `bytes ${a}-${z}/${opts.star ? "*" : BODY.length}`;
  res.writeHead(206, h).end(BODY.subarray(a, z + 1));
}

before(async () => {
  server = http.createServer((req, res) => {
    log.push({ method: req.method!, url: req.url!, headers: req.headers });
    const u = req.url!;
    if (u === "/obj") return serveRange(req, res);
    if (u === "/ignore-range") return res.writeHead(200, { ETag: ETAG, "Last-Modified": LM }).end(BODY);
    if (u === "/star") return serveRange(req, res, { star: true });
    if (u === "/shifted") return serveRange(req, res, { shift: 1 });
    if (u === "/no-etag") return serveRange(req, res, { etag: null });
    if (u === "/weak") return serveRange(req, res, { etag: 'W/"v1"' });
    if (u === "/no-lm") return serveRange(req, res, { lm: null });
    if (u === "/bad-lm") return serveRange(req, res, { lm: "yesterday" });
    if (u === "/gzip") return res.writeHead(206, { "Content-Encoding": "gzip", "Content-Range": `bytes 0-1/36` }).end("01");
    if (u === "/identity") return res.writeHead(206, { "Content-Encoding": "identity", "Content-Range": `bytes 0-1/36` }).end("01");
    if (u === "/500") return res.writeHead(500).end();
    if (u === "/404") return res.writeHead(404).end();
    if (u === "/ignores-preconditions") {
      // returns content regardless of If-Match/If-Unmodified-Since, with a different ETag and newer date
      return res.writeHead(200, { ETag: '"v2"', "Last-Modified": "Mon, 07 Nov 1994 08:49:37 GMT" }).end(BODY);
    }
    let m = /^\/redir\/(\d+)$/.exec(u);
    if (m) {
      const k = Number(m[1]);
      return res.writeHead([301, 302, 303, 307, 308][k % 5], { Location: k === 0 ? "/obj" : `${k - 1}` }).end();
    }
    if (u === "/redir-abs") return res.writeHead(302, { Location: `${origin}/obj` }).end();
    if (u === "/redir-file") return res.writeHead(302, { Location: "file:///etc/hosts" }).end();
    if (u === "/redir-none") return res.writeHead(302).end();
    m = /^\/q\?(.*)$/.exec(u);
    if (m && m[1] === "x=1") return serveRange(req, res);
    res.writeHead(404).end();
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
after(() => server.close());

const dir = tmpDir();
let n = 0;
async function getWith(s: Source, start: number, end: number): Promise<Buffer> {
  const buf = buildBuf({ sources: [s], entries: [{ key: "r", ranges: [rng.src(0, start, end - start)] }] });
  const a = Archive.open(writeTmp(dir, `h${n++}.vzip`, buf));
  try {
    return Buffer.from((await a.get("r"))!);
  } finally {
    a.close();
  }
}
async function expectResolution(s: Source, start = 2, end = 5): Promise<void> {
  await assert.rejects(getWith(s, start, end), (e: unknown) => e instanceof VzipError && e.cls === "resolution");
}

test("HTTP reads: range requests, headers, pins, 200 fallback, redirects, query", async () => {
  log.length = 0;
  assert.equal((await getWith(src.url(`${origin}/obj`), 2, 5)).toString(), "234");
  assert.equal(log.length, 1);
  assert.equal(log[0].method, "GET");
  assert.equal(log[0].headers.range, "bytes=2-4");
  assert.equal(log[0].headers["accept-encoding"], "identity");
  assert.equal(log[0].headers["if-match"], undefined);

  log.length = 0;
  const pinned = src.url(`${origin}/obj`, { size: 36n, etag: ETAG, modifiedNotAfter: LM_SECS });
  assert.equal((await getWith(pinned, 10, 12)).toString(), "ab");
  assert.equal(log.length, 1);
  assert.equal(log[0].headers["if-match"], ETAG);
  assert.equal(log[0].headers["if-unmodified-since"], LM);

  assert.equal((await getWith(src.url(`${origin}/obj`, { modifiedNotAfter: LM_SECS + 1000n }), 0, 1)).toString(), "0");
  assert.equal((await getWith(src.url(`${origin}/ignore-range`, { size: 36n, etag: ETAG }), 30, 36)).toString(), "uvwxyz");
  assert.equal((await getWith(src.url(`${origin}/star`), 1, 3)).toString(), "12");
  assert.equal((await getWith(src.url(`${origin}/identity`), 0, 2)).toString(), "01");
  assert.equal((await getWith(src.url(`${origin}/q?x=1`), 0, 2)).toString(), "01");

  log.length = 0;
  assert.equal((await getWith(src.url(`${origin}/redir/4`, { etag: ETAG }), 3, 4)).toString(), "3");
  assert.equal(log.length, 6);
  for (const l of log) {
    assert.equal(l.headers.range, "bytes=3-3");
    assert.equal(l.headers["if-match"], ETAG);
    assert.equal(l.headers["accept-encoding"], "identity");
  }
  assert.equal((await getWith(src.url(`${origin}/redir-abs`), 0, 1)).toString(), "0");
  assert.ok(log.every((l) => l.method === "GET"));
});

const resolutionCases: Record<string, () => Source> = {
  "size pin mismatch (206)": () => src.url(`${origin}/obj`, { size: 35n }),
  "size pin mismatch (200)": () => src.url(`${origin}/ignore-range`, { size: 35n }),
  "size pin with unknown total": () => src.url(`${origin}/star`, { size: 36n }),
  "etag pin 412": () => src.url(`${origin}/obj`, { etag: '"v0"' }),
  "etag pin, server ignores If-Match": () => src.url(`${origin}/ignores-preconditions`, { etag: ETAG }),
  "etag pin, no ETag header": () => src.url(`${origin}/no-etag`, { etag: '""' }),
  "etag pin vs weak response ETag": () => src.url(`${origin}/weak`, { etag: '"v1"' }),
  "modified_not_after pin 412": () => src.url(`${origin}/obj`, { modifiedNotAfter: LM_SECS - 1n }),
  "modified_not_after, server ignores If-Unmodified-Since": () => src.url(`${origin}/ignores-preconditions`, { modifiedNotAfter: LM_SECS }),
  "modified_not_after, no Last-Modified": () => src.url(`${origin}/no-lm`, { modifiedNotAfter: LM_SECS }),
  "modified_not_after, unparseable Last-Modified": () => src.url(`${origin}/bad-lm`, { modifiedNotAfter: LM_SECS }),
  "modified_not_after outside years 1-9999": () => src.url(`${origin}/obj`, { modifiedNotAfter: 253402300800n }),
  "returned range differs": () => src.url(`${origin}/shifted`),
  "Content-Encoding gzip": () => src.url(`${origin}/gzip`),
  "status 500": () => src.url(`${origin}/500`),
  "status 404": () => src.url(`${origin}/404`),
  "six redirects": () => src.url(`${origin}/redir/5`),
  "redirect to file:": () => src.url(`${origin}/redir-file`),
  "redirect without Location": () => src.url(`${origin}/redir-none`),
  "connection refused": () => src.url("http://127.0.0.1:1/x"),
};
for (const [name, mk] of Object.entries(resolutionCases)) {
  test(`HTTP resolution error: ${name}`, async () => {
    await expectResolution(mk());
  });
}
test("HTTP resolution error: 416 (object shorter than range)", async () => {
  await expectResolution(src.url(`${origin}/obj`), 40, 45);
});
test("HTTP resolution error: 200 body shorter than range", async () => {
  await expectResolution(src.url(`${origin}/ignore-range`), 30, 40);
});
test("HTTP resolution error: 206 truncated at end of object", async () => {
  await expectResolution(src.url(`${origin}/obj`), 30, 40);
});
