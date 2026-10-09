// Store inputs under the reader policy (SPEC.md §8.7), under Node: the listing, every
// object read (the concurrent document reads' too) and every redirect go through the
// policy, as an image file's requests do. The twin of tests/test_store_policy.py.

import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import type { AddressInfo } from "node:net";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { after, before, test } from "node:test";
import { checkAddress, type Policy, VzipError } from "../src/archive.ts";
import { resolver } from "../src/net_node.ts";
import { virtualizeStore } from "../src/virtualize/index.ts";
import { openHttpStore, storeFetch } from "../src/virtualize/store.ts";

const STORE = path.join(import.meta.dirname, "fixtures", "zarr2", "zarr2_hierarchy");
const OBJECTS = new Map<string, Uint8Array>();
for (const rel of fs.readdirSync(STORE, { recursive: true, encoding: "utf8" })) {
  const full = path.join(STORE, rel);
  if (fs.statSync(full).isFile()) OBJECTS.set(rel.split(path.sep).join("/"), new Uint8Array(fs.readFileSync(full)));
}
const escape = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// A local S3-like server, and an HTTP proxy, serving OBJECTS as the store `b/s/` and
// again as `b/r/`. With `state.redirect`, a read of an object under `b/s/` is redirected
// to that URL followed by its key. `state.paths` records each request's target, as sent
// (absolute through a proxy).
const state = { redirect: undefined as string | undefined, paths: [] as string[] };
let server: http.Server;
let port = 0;

before(async () => {
  server = http.createServer((req, res) => {
    state.paths.push(req.url!);
    const p = decodeURIComponent(new URL(req.url!, "http://x").pathname);
    if (p === "/b/") {
      const contents = [...OBJECTS].sort(([a], [b]) => (a < b ? -1 : 1))
        .map(([k, v]) => `<Contents><Key>s/${escape(k)}</Key><Size>${v.length}</Size></Contents>`).join("");
      const body = `<?xml version="1.0"?><ListBucketResult><IsTruncated>false</IsTruncated>${contents}</ListBucketResult>`;
      res.writeHead(200, { "Content-Length": String(Buffer.byteLength(body)) }).end(body);
    } else if (p.startsWith("/b/s/") && state.redirect !== undefined) {
      const key = p.slice("/b/s/".length).split("/").map(encodeURIComponent).join("/");
      res.writeHead(307, { Location: state.redirect + key, "Content-Length": "0" }).end();
    } else {
      const [a, b] = req.headers.range!.slice(6).split("-").map(Number);
      const body = OBJECTS.get(p.slice("/b/s/".length))!.subarray(a, b + 1);
      res.writeHead(206, { "Content-Length": String(body.length) }).end(body);
    }
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  port = (server.address() as AddressInfo).port;
});
after(() => {
  server.closeAllConnections();
  server.close();
});

/** Maps host names to addresses for the reader; the others are not resolved. */
function mockResolver(names: Record<string, string>) {
  const saved = resolver.lookup;
  resolver.lookup = async (host) => {
    if (!(host in names)) throw new Error(`${host}: not resolved in this test`);
    return [{ address: names[host], family: 4 }];
  };
  return () => (resolver.lookup = saved);
}

/** The default policy's address check, except that the test server's address counts as a public one. */
const serverIsPublic = (url: string, ip: string) => {
  if (ip !== "127.0.0.1") checkAddress({}, url, ip);
};

/** The archive's contents (bytes as Latin-1 text), the store URL in them replaced by `<store>`. */
function show(desc: object, url: string): string {
  return JSON.stringify(desc, (_k, v) => (typeof v === "bigint" ? String(v) : v instanceof Uint8Array ? Buffer.from(v).toString("latin1") : v))
    .replaceAll(url, "<store>");
}

/** Virtualizes the store at `url` under `policy` in a new Node process, whose environment
 * has `env` (Node's `fetch` reads the proxy settings when it starts) and whose resolver
 * gives `address` for every name. */
function inChild(url: string, policy: Policy, env: Record<string, string>, address: string): Promise<string> {
  const src = (f: string) => pathToFileURL(path.join(import.meta.dirname, "..", "src", f)).href;
  const code = `
    import { resolver } from ${JSON.stringify(src("net_node.ts"))};
    import { virtualizeStore } from ${JSON.stringify(src("virtualize/index.ts"))};
    resolver.lookup = async () => [{ address: ${JSON.stringify(address)}, family: 4 }];
    const v = await virtualizeStore(${JSON.stringify(url)}, { policy: ${JSON.stringify(policy)} });
    process.stdout.write(JSON.stringify(v, (_k, v) => (typeof v === "bigint" ? String(v) : v instanceof Uint8Array ? Buffer.from(v).toString("latin1") : v)));
  `;
  return new Promise((resolve, reject) => {
    execFile(process.execPath, ["--input-type=module", "-e", code], { env: { ...process.env, ...env } }, (e, stdout, stderr) =>
      e ? reject(new Error(`${e.message}\n${stderr}`)) : resolve(JSON.stringify(JSON.parse(stdout)).replaceAll(url, "<store>"))
    );
  });
}

const resolution = (re: RegExp) => (e: unknown) => e instanceof VzipError && e.errorClass === "resolution" && re.test(e.message);

test("reads a store the policy allows", async () => {
  // Each way a policy admits a store's requests, read one document at a time and
  // concurrently, gives the same output: a name that resolves to an allowed address, its
  // object reads redirected to another such name; a private host with allowPrivateHosts,
  // redirected to localhost; and, through a proxy, a name that resolves to a public
  // address with allowUncheckedProxy, or to a private one with allowPrivateHosts.
  const restore = mockResolver({ "public.test": "127.0.0.1", "other.test": "127.0.0.1", localhost: "127.0.0.1" });
  const outputs = new Set<string>();
  try {
    const cases: [string, Policy, string | undefined, ((u: string, ip: string) => void) | undefined][] = [
      [`http://public.test:${port}/b/s/`, {}, `http://other.test:${port}/b/r/`, serverIsPublic],
      [`http://127.0.0.1:${port}/b/s/`, { allowPrivateHosts: true }, `http://localhost:${port}/b/r/`, undefined],
    ];
    for (const [url, policy, redirect, check] of cases) {
      state.redirect = redirect;
      for (const concurrency of [1, 16]) {
        state.paths = [];
        const fetch = check ? storeFetch(url, policy, {}, check) : undefined;
        const store = await openHttpStore(url, { policy, concurrency, ...(fetch ? { fetch } : {}) });
        outputs.add(show(await virtualizeStore(store), url));
        assert.ok(state.paths.some((p) => p.startsWith("/b/r/"))); // the reads went where they were redirected
      }
    }
  } finally {
    state.redirect = undefined;
    restore();
  }
  const proxy = { NODE_USE_ENV_PROXY: "1", HTTP_PROXY: `http://127.0.0.1:${port}`, NO_PROXY: "" };
  for (const [url, policy, address] of [
    ["http://proxied.test/b/s/", { allowUncheckedProxy: true }, "93.184.215.14"],
    ["http://internal.test/b/s/", { allowPrivateHosts: true }, "10.0.0.1"],
  ] as const) {
    state.paths = [];
    outputs.add(await inChild(url, policy, proxy, address));
    assert.ok(state.paths.length > 0 && state.paths.every((p) => p.startsWith(url.split("/b/")[0]))); // all through the proxy
  }
  assert.equal(outputs.size, 1);
});

test("refuses a private host", async () => {
  state.paths = [];
  await assert.rejects(virtualizeStore(`http://127.0.0.1:${port}/b/s/`), resolution(/is a loopback address.*allowPrivateHosts/));
  assert.deepEqual(state.paths, []);
});

test("refuses a name that resolves to a private address", async () => {
  const restore = mockResolver({ "private.test": "127.0.0.1" });
  state.paths = [];
  try {
    await assert.rejects(virtualizeStore(`http://private.test:${port}/b/s/`), resolution(/at 127\.0\.0\.1.*is a loopback address/));
  } finally {
    restore();
  }
  assert.deepEqual(state.paths, []);
});

test("refuses a redirect to a private host before requesting it", async () => {
  // A concurrent document read is redirected to `localhost`, refused as written before it
  // is requested (were it requested, the server would see it: serverIsPublic allows its address).
  const restore = mockResolver({ "public.test": "127.0.0.1", localhost: "127.0.0.1" });
  const url = `http://public.test:${port}/b/s/`;
  state.redirect = `http://localhost:${port}/b/r/`;
  state.paths = [];
  try {
    const store = await openHttpStore(url, { concurrency: 16, fetch: storeFetch(url, {}, {}, serverIsPublic) });
    await assert.rejects(virtualizeStore(store), resolution(/http:\/\/localhost.*is a loopback address/));
  } finally {
    state.redirect = undefined;
    restore();
  }
  assert.ok(state.paths.length > 0 && !state.paths.some((p) => p.startsWith("/b/r/")));
});

test("refuses a request through a proxy", async () => {
  const saved = { ...process.env };
  const restore = mockResolver({ "proxied.test": "93.184.215.14" });
  Object.assign(process.env, { NODE_USE_ENV_PROXY: "1", HTTP_PROXY: `http://127.0.0.1:${port}` });
  delete process.env.NO_PROXY;
  delete process.env.no_proxy;
  state.paths = [];
  try {
    await assert.rejects(virtualizeStore("http://proxied.test/b/s/"), resolution(/through the proxy.*allowUncheckedProxy: true/));
  } finally {
    for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
    Object.assign(process.env, saved);
    restore();
  }
  assert.deepEqual(state.paths, []);
});

test("refuses a redirect to a file URL whatever the policy", async () => {
  // allowFilesFromRemoteArchives does not apply to stores: a store is read over http(s) only.
  const restore = mockResolver({ "public.test": "127.0.0.1" });
  const url = `http://public.test:${port}/b/s/`;
  const policy = { allowFilesFromRemoteArchives: true };
  state.redirect = "file:///etc/";
  try {
    const store = await openHttpStore(url, { concurrency: 1, policy, fetch: storeFetch(url, policy, {}, serverIsPublic) });
    await assert.rejects(virtualizeStore(store), resolution(/redirect to a non-http URL/));
  } finally {
    state.redirect = undefined;
    restore();
  }
});
