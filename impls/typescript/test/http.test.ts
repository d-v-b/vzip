// HTTP source resolution (spec §6.1, §6.2) against a local test server.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import zlib from "node:zlib";
import type { AddressInfo } from "node:net";
import { Archive, type Request } from "../src/reader.ts";
import { VzError } from "../src/errors.ts";
import { encodeRange, type SourceMsg } from "../src/proto.ts";
import { formatImfFixdate, parseImfFixdate } from "../src/http.ts";
import { CLI, extraBlock, rawZip, runCli, tmpdir, writeTmp, type RawEntry } from "./helpers.ts";

const OBJ = Buffer.from("abcdefghijklmnopqrstuvwxyz");
const LM = "Sun, 06 Nov 1994 08:49:37 GMT";
const LM_SECS = 784111777n;
const log: { method: string; url: string; headers: http.IncomingHttpHeaders }[] = [];
let server: http.Server;
let base = "";

function serveRange(req: http.IncomingMessage, res: http.ServerResponse, opts: { etag?: string; lm?: string; honorPins?: boolean } = {}) {
  const etag = opts.etag ?? '"v1"';
  const lm = opts.lm ?? LM;
  if (opts.honorPins !== false) {
    const im = req.headers["if-match"];
    if (im !== undefined && im !== etag) return void res.writeHead(412).end();
    const ius = req.headers["if-unmodified-since"];
    if (ius !== undefined && parseImfFixdate(ius)! < parseImfFixdate(lm)!) return void res.writeHead(412).end();
  }
  const m = /^bytes=(\d+)-(\d+)$/.exec(req.headers.range ?? "");
  const headers: Record<string, string> = {};
  if (etag) headers.ETag = etag;
  if (lm) headers["Last-Modified"] = lm;
  if (!m) return void res.writeHead(200, headers).end(OBJ);
  const a = +m[1];
  if (a >= OBJ.length) return void res.writeHead(416, { "Content-Range": `bytes */${OBJ.length}` }).end();
  const z = Math.min(+m[2], OBJ.length - 1);
  res.writeHead(206, { ...headers, "Content-Range": `bytes ${a}-${z}/${OBJ.length}`, "Content-Length": String(z - a + 1) });
  res.end(OBJ.subarray(a, z + 1));
}

before(async () => {
  server = http.createServer((req, res) => {
    log.push({ method: req.method!, url: req.url!, headers: req.headers });
    const u = req.url!;
    const m = /^\/redir\/(\d+)$/.exec(u);
    if (m) {
      const k = +m[1];
      return void res.writeHead(k % 2 ? 302 : 307, { Location: k > 1 ? `/redir/${k - 1}` : "../obj" }).end();
    }
    switch (u) {
      case "/obj":
        return serveRange(req, res);
      case "/full":
        res.writeHead(200, { ETag: '"v1"', "Last-Modified": LM });
        return void res.end(OBJ);
      case "/chunked": {
        const mm = /^bytes=(\d+)-(\d+)$/.exec(req.headers.range!)!;
        res.writeHead(206, { "Content-Range": `bytes ${mm[1]}-${mm[2]}/${OBJ.length}`, "Transfer-Encoding": "chunked" });
        const body = OBJ.subarray(+mm[1], +mm[2] + 1);
        res.write(body.subarray(0, 1));
        return void res.end(body.subarray(1));
      }
      case "/gzip":
        res.writeHead(200, { "Content-Encoding": "gzip" });
        return void res.end(zlib.gzipSync(OBJ));
      case "/identity2":
        res.writeHead(200, { "Content-Encoding": "identity, identity" });
        return void res.end(OBJ);
      case "/identityws":
        res.writeHead(200, { "Content-Encoding": " IDENTITY " });
        return void res.end(OBJ);
      case "/star": {
        const mm = /^bytes=(\d+)-(\d+)$/.exec(req.headers.range!)!;
        res.writeHead(206, { "Content-Range": `bytes ${mm[1]}-${mm[2]}/*` });
        return void res.end(OBJ.subarray(+mm[1], +mm[2] + 1));
      }
      case "/wrongrange":
        res.writeHead(206, { "Content-Range": `bytes 0-1/${OBJ.length}` });
        return void res.end(OBJ.subarray(0, 2));
      case "/norange":
        res.writeHead(206);
        return void res.end(OBJ.subarray(0, 2));
      case "/multipart":
        res.writeHead(206, { "Content-Type": "multipart/byteranges; boundary=x" });
        return void res.end("--x--");
      case "/shortbody":
        res.writeHead(206, { "Content-Range": "bytes 0-1/26" });
        return void res.end(OBJ.subarray(0, 1));
      case "/redirfile":
        return void res.writeHead(302, { Location: "file:///etc/passwd" }).end();
      case "/noloc":
        return void res.writeHead(302).end();
      case "/500":
        return void res.writeHead(500).end();
      case "/ignorepins":
        return serveRange(req, res, { honorPins: false, etag: '"v2"', lm: "Mon, 07 Nov 1994 08:49:37 GMT" });
      case "/weak":
        return serveRange(req, res, { honorPins: false, etag: 'W/"v1"' });
      case "/noetag":
        return serveRange(req, res, { honorPins: false, etag: "" });
      case "/rfc850":
        return serveRange(req, res, { honorPins: false, lm: "Sunday, 06-Nov-94 08:49:37 GMT" });
      case "/wrongday":
        return serveRange(req, res, { honorPins: false, lm: "Mon, 06 Nov 1994 08:49:37 GMT" });
      default:
        res.writeHead(404).end();
    }
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
after(() => server.close());

const dir = tmpdir();
let n = 0;
const WHOLE: Request = { kind: "whole" };

function archiveFor(sources: SourceMsg[], refs: [string, number, number, number][]): Archive {
  const entries: RawEntry[] = refs.map(([name, s, o, l]) => {
    const p = encodeRange({ source: BigInt(s), offset: BigInt(o), length: BigInt(l), data: null });
    return { name, body: p, extra: extraBlock(0x7a76, p) };
  });
  return Archive.open(writeTmp(dir, `h${n++}.vzip`, rawZip({ entries, sources })));
}
const url = (u: string, pins: Partial<SourceMsg> = {}): SourceMsg => ({
  kind: "url", url: u, size: null, etag: null, modifiedNotAfter: null, ...pins,
});
async function cls(p: Promise<unknown>): Promise<string> {
  try {
    await p;
    return "none";
  } catch (e) {
    if (e instanceof VzError) return e.cls;
    throw e;
  }
}

test("HTTP: successful reads (206, 200, chunked, redirects, pins)", async () => {
  const a = archiveFor(
    [
      url(base + "/obj", { size: 26n, etag: '"v1"', modifiedNotAfter: LM_SECS }),
      url(base + "/full", { size: 26n }),
      url(base + "/chunked", { size: 26n }),
      url(base + "/redir/5"),
      url(base + "/identityws"),
      url(base + "/star"),
    ],
    [["obj", 0, 2, 3], ["full", 1, 5, 4], ["chunked", 2, 1, 5], ["redir", 3, 0, 2], ["idws", 4, 0, 1], ["star", 5, 3, 2]],
  );
  log.length = 0;
  assert.deepEqual(await a.get("obj", WHOLE), Buffer.from("cde"));
  const h = log[0].headers;
  assert.equal(log[0].method, "GET");
  assert.equal(h.range, "bytes=2-4");
  assert.equal(h["accept-encoding"], "identity");
  assert.equal(h["if-match"], '"v1"');
  assert.equal(h["if-unmodified-since"], LM);
  assert.deepEqual(await a.get("obj", { kind: "range", start: 1n, end: 2n }), Buffer.from("d"));
  assert.equal(log[1].headers.range, "bytes=3-3");
  assert.deepEqual(await a.get("full", WHOLE), Buffer.from("fghi"));
  assert.deepEqual(await a.get("chunked", WHOLE), Buffer.from("bcdef"));
  log.length = 0;
  assert.deepEqual(await a.get("redir", WHOLE), Buffer.from("ab"));
  assert.equal(log.length, 6);
  assert.ok(log.every((l) => l.method === "GET" && l.headers.range === "bytes=0-1" && l.headers["accept-encoding"] === "identity"));
  assert.deepEqual(await a.get("idws", WHOLE), Buffer.from("a"));
  assert.deepEqual(await a.get("star", WHOLE), Buffer.from("de"));
});

const failing: [string, SourceMsg, number, number][] = [
  ["412 from If-Match", url("/obj", { etag: '"other"' }), 0, 1],
  ["412 from If-Unmodified-Since", url("/obj", { modifiedNotAfter: LM_SECS - 1n }), 0, 1],
  ["size pin mismatch on 206", url("/obj", { size: 27n }), 0, 1],
  ["size pin mismatch on 200", url("/full", { size: 25n }), 0, 1],
  ["size pin with unknown total", url("/star", { size: 26n }), 0, 1],
  ["etag ignored by server, checked by reader", url("/ignorepins", { etag: '"v1"' }), 0, 1],
  ["Last-Modified ignored by server, checked by reader", url("/ignorepins", { modifiedNotAfter: LM_SECS }), 0, 1],
  ["weak ETag response fails strong comparison", url("/weak", { etag: '"v1"' }), 0, 1],
  ["missing ETag", url("/noetag", { etag: '"v1"' }), 0, 1],
  ["RFC 850 Last-Modified", url("/rfc850", { modifiedNotAfter: LM_SECS }), 0, 1],
  ["wrong day name in Last-Modified", url("/wrongday", { modifiedNotAfter: LM_SECS }), 0, 1],
  ["pin date outside years 1-9999", url("/obj", { modifiedNotAfter: 300000000000n }), 0, 1],
  ["gzip content-encoding", url("/gzip"), 0, 1],
  ["identity, identity content-encoding", url("/identity2"), 0, 1],
  ["wrong Content-Range", url("/wrongrange"), 2, 2],
  ["206 without Content-Range", url("/norange"), 0, 2],
  ["multipart/byteranges", url("/multipart"), 0, 2],
  ["206 body length mismatch", url("/shortbody"), 0, 2],
  ["416", url("/obj"), 30, 2],
  ["200 body shorter than range", url("/full"), 20, 10],
  ["six redirects", url("/redir/6"), 0, 1],
  ["redirect to file:", url("/redirfile"), 0, 1],
  ["redirect without Location", url("/noloc"), 0, 1],
  ["status 500", url("/500"), 0, 1],
  ["status 404", url("/missing"), 0, 1],
  ["userinfo in URL", url("http://u@127.0.0.1:1/x"), 0, 1],
  ["empty host", url("http:///x"), 0, 1],
  ["connection refused", url("http://127.0.0.1:1/x"), 0, 1],
];
for (const [label, src, off, len] of failing) {
  test(`HTTP resolution error: ${label}`, async () => {
    const s = { ...src, url: src.url!.startsWith("/") ? base + src.url : src.url };
    const a = archiveFor([s], [["r", 0, off, len], ["z", 0, 0, 0]]);
    assert.equal(await cls(a.get("r", WHOLE)), "resolution");
    assert.deepEqual(await a.get("z", WHOLE), Buffer.alloc(0)); // zero-length: no request, no error
  });
}

test("HTTP via CLI: write then read an archive with http sources", async () => {
  const d = {
    sources: [{ url: base + "/obj", size: 26, etag: '"v1"' }, { url: base + "/obj", size: 99 }],
    entries: [
      { key: "k", ranges: [{ source: 0, offset: 24, length: 2 }, { data: "21" }] },
      { key: "bad", ranges: [{ source: 1, offset: 0, length: 1 }] },
    ],
  };
  const dp = writeTmp(dir, "hd.json", JSON.stringify(d));
  const out = dir + "/http.vzip";
  assert.equal(runCli(["write", dp, out]).status, 0);
  const qp = writeTmp(dir, "hq.json", JSON.stringify([{ op: "get", key: "k" }, { op: "get", key: "bad" }]));
  const { spawn } = await import("node:child_process");
  const child = spawn(CLI, ["read", out, qp]);
  let stdout = "";
  child.stdout.on("data", (c) => (stdout += c));
  const code = await new Promise((r) => child.on("close", r));
  assert.equal(code, 0);
  const res = JSON.parse(stdout);
  assert.deepEqual(res.results[0], { ok: true, value: "797a21" });
  assert.equal(res.results[1].class, "resolution");
});

test("IMF-fixdate formatting and parsing", () => {
  assert.equal(formatImfFixdate(LM_SECS), LM);
  assert.equal(formatImfFixdate(-62135596800n), "Mon, 01 Jan 0001 00:00:00 GMT");
  assert.equal(formatImfFixdate(253402300799n), "Fri, 31 Dec 9999 23:59:59 GMT");
  assert.equal(formatImfFixdate(253402300800n), null);
  assert.equal(formatImfFixdate(-62135596801n), null);
  assert.equal(parseImfFixdate(LM), LM_SECS);
  assert.equal(parseImfFixdate("Sun, 06 Nov 1994 08:49:37 UTC"), null);
  assert.equal(parseImfFixdate("Sun, 31 Feb 1994 08:49:37 GMT"), null);
  assert.equal(parseImfFixdate("Sun,  6 Nov 1994 08:49:37 GMT"), null);
});
