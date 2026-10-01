import { test } from "node:test";
import assert from "node:assert/strict";
import * as http from "node:http";
import type { AddressInfo } from "node:net";
import { writeArchive } from "../src/writer.ts";
import { Archive } from "../src/reader.ts";
import { errClass, tmpDir, tmpFile } from "./helpers.ts";

const OBJ = Buffer.from("0123456789abcdef");
const ETAG = '"v1"';
const LASTMOD = 1_000_000_000; // seconds

function startServer(): Promise<http.Server> {
  const srv = http.createServer((req, res) => {
    if (req.headers["if-match"] !== undefined && req.headers["if-match"] !== ETAG) {
      res.writeHead(412).end();
      return;
    }
    const ius = req.headers["if-unmodified-since"];
    if (ius !== undefined && Date.parse(ius) / 1000 < LASTMOD) {
      res.writeHead(412).end();
      return;
    }
    const m = /^bytes=(\d+)-(\d+)$/.exec(req.headers.range ?? "");
    if (!m) {
      res.writeHead(200, { ETag: ETAG }).end(OBJ);
      return;
    }
    const s = Number(m[1]);
    const e = Math.min(Number(m[2]), OBJ.length - 1);
    if (s >= OBJ.length) {
      res.writeHead(416, { "Content-Range": `bytes */${OBJ.length}` }).end();
      return;
    }
    res.writeHead(206, { ETag: ETAG, "Content-Range": `bytes ${s}-${e}/${OBJ.length}` }).end(OBJ.subarray(s, e + 1));
  });
  return new Promise((r) => srv.listen(0, "127.0.0.1", () => r(srv)));
}

test("http sources: ranges and pins that hold", async () => {
  const srv = await startServer();
  const url = `http://127.0.0.1:${(srv.address() as AddressInfo).port}/obj`;
  try {
    const b = writeArchive(
      [{ kind: "url", url, size: 16n, etag: ETAG, modifiedNotAfter: BigInt(LASTMOD) }],
      [{ key: "k", ranges: [{ source: 0, offset: 2n, length: 5n }] }],
    );
    const a = await Archive.open(tmpFile(tmpDir(), b));
    assert.deepEqual(await a.get("k"), Buffer.from("23456"));
    assert.deepEqual(await a.get("k", { type: "suffix", count: 2n }), Buffer.from("56"));
    await a.close();
  } finally {
    srv.close();
  }
});

const failing: [string, { size?: bigint; etag?: string; modifiedNotAfter?: bigint }, bigint, bigint][] = [
  ["size pin fails", { size: 17n }, 0n, 1n],
  ["etag pin fails", { etag: '"v2"' }, 0n, 1n],
  ["modified_not_after pin fails", { modifiedNotAfter: BigInt(LASTMOD - 1) }, 0n, 1n],
  ["object shorter than range", {}, 10n, 7n],
  ["range starts beyond end", {}, 20n, 1n],
];
for (const [name, pins, offset, length] of failing) {
  test(`http resolution error: ${name}`, async () => {
    const srv = await startServer();
    const url = `http://127.0.0.1:${(srv.address() as AddressInfo).port}/obj`;
    try {
      const b = writeArchive([{ kind: "url", url, ...pins }], [{ key: "k", ranges: [{ source: 0, offset, length }] }]);
      const a = await Archive.open(tmpFile(tmpDir(), b));
      assert.equal(await errClass(() => a.get("k")), "resolution");
      await a.close();
    } finally {
      srv.close();
    }
  });
}
