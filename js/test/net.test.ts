// Rule 3 of spec/archive.md §8.7 on the network, under Node (`#net`): the twin of the
// network tests in python/tests/test_policy.py.

import assert from "node:assert/strict";
import http from "node:http";
import type { AddressInfo } from "node:net";
import { after, before, test } from "node:test";
import { Archive, checkAddress, VzipError } from "../src/archive.ts";
import { readHttpRange } from "../src/http.ts";
import { checkedFetch, envProxy, resolver } from "../src/net_node.ts";
import { writeVzip } from "../src/writer.ts";

const BLOB = Uint8Array.from({ length: 16384 }, (_, i) => i % 256);
const state = { requests: 0, hosts: [] as string[], location: "/blob.bin" };
let server: http.Server;
let port = 0;
let base = "";

before(async () => {
  server = http.createServer((req, res) => {
    state.requests++;
    state.hosts.push(req.headers.host ?? "");
    if (req.url === "/moved") {
      res.writeHead(302, { Location: state.location, "Content-Length": "0" }).end();
      return;
    }
    const [a, b] = (req.headers.range ?? "bytes=0-0").slice(6).split("-").map(Number);
    res.writeHead(206, { "Content-Range": `bytes ${a}-${b}/${BLOB.length}`, "Content-Length": String(b - a + 1) });
    res.end(BLOB.subarray(a, b + 1));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  port = (server.address() as AddressInfo).port;
  base = `http://127.0.0.1:${port}`;
});
after(() => {
  server.closeAllConnections();
  server.close();
});

/** Maps host names to addresses for the reader (`names`), recording each lookup. */
function mockResolver(names: Record<string, string>) {
  const looked: string[] = [];
  const saved = resolver.lookup;
  resolver.lookup = async (host) => {
    looked.push(host);
    if (host in names) return [{ address: names[host], family: names[host].includes(":") ? 6 : 4 }];
    return saved(host);
  };
  return { looked, restore: () => (resolver.lookup = saved) };
}

/** An address check that allows the test server's address, and what the default policy allows. */
const loopbackOnly = (seen?: string[]) => (url: string, ip: string) => {
  seen?.push(ip);
  if (ip !== "127.0.0.1") checkAddress({}, url, ip);
};

const read = (url: string, check: (url: string, ip: string) => void, allow?: (u: string) => void) =>
  readHttpRange(url, 0, 4, {}, undefined, { fetch: checkedFetch(check, {}), ...(allow ? { allow } : {}) });

async function archiveOf(url: string, policy = {}) {
  const bytes = await writeVzip({ sources: [{ url }], entries: [{ key: "r", ranges: [{ source: 0, offset: 0n, length: 4n }] }] });
  return Archive.open(bytes, "file:///d/a.vzip", undefined, policy);
}

const resolution = (re: RegExp) => (e: unknown) => e instanceof VzipError && e.errorClass === "resolution" && re.test(e.message);

test("reads the hosts rule 3 allows", async () => {
  const { restore } = mockResolver({ "data.test": "127.0.0.1" });
  try {
    const seen: string[] = [];
    const { data } = await read(`http://data.test:${port}/blob.bin`, loopbackOnly(seen));
    assert.deepEqual([...data], [...BLOB.subarray(0, 4)]);
    assert.deepEqual(seen, ["127.0.0.1"]); // checked at the lookup, then connected to
    assert.equal(state.hosts.at(-1), `data.test:${port}`); // the Host stays the name
    // the opt-out: the platform's fetch, unchecked
    const archive = await archiveOf(`${base}/blob.bin`, { allowPrivateHosts: true });
    assert.deepEqual([...(await archive.read("r"))!], [...BLOB.subarray(0, 4)]);
  } finally {
    restore();
  }
});

test("refuses a name that resolves to a refused address", async () => {
  const before = state.requests;
  for (const [address, cls] of [["127.0.0.1", "loopback"], ["10.0.0.1", "private"], ["169.254.169.254", "link-local"], ["0.0.0.0", "special"]]) {
    const name = `rebind-${cls}.test`;
    const { looked, restore } = mockResolver({ [name]: address });
    try {
      const archive = await archiveOf(`http://${name}:${port}/blob.bin`); // a local archive, the default policy
      await assert.rejects(archive.read("r"), resolution(new RegExp(`at ${address}.*is a ${cls} address`)));
      assert.deepEqual(looked, [name]);
    } finally {
      restore();
    }
  }
  assert.equal(state.requests, before);
});

test("refuses a redirect to a refused address", async () => {
  const { restore } = mockResolver({ "metadata.test": "169.254.169.254", "loop.test": "127.0.0.2" });
  try {
    for (const [location, cls] of [
      ["http://169.254.169.254/latest/meta-data", "link-local"],
      [`http://metadata.test:${port}/blob.bin`, "link-local"],
      [`http://loop.test:${port}/blob.bin`, "loopback"],
    ]) {
      state.location = location;
      const before = state.requests;
      await assert.rejects(read(`${base}/moved`, loopbackOnly()), new RegExp(`is a ${cls} address`));
      assert.equal(state.requests, before + 1); // the redirect only
    }
  } finally {
    state.location = "/blob.bin";
    restore();
  }
});

test("refuses a reused connection to a refused address", async () => {
  const { looked, restore } = mockResolver({ "reuse.test": "127.0.0.1" });
  let refuse = false;
  const check = (url: string, ip: string) => (refuse ? checkAddress({}, url, ip) : undefined);
  const fetch = checkedFetch(check, {});
  try {
    const url = `http://reuse.test:${port}/blob.bin`;
    await readHttpRange(url, 0, 4, {}, undefined, { fetch });
    const before = state.requests;
    const n = looked.length;
    refuse = true;
    await assert.rejects(readHttpRange(url, 0, 4, {}, undefined, { fetch }), /at 127\.0\.0\.1.*is a loopback address/);
    assert.equal(looked.length, n); // the kept-alive connection, not a new one
    assert.equal(state.requests, before);
  } finally {
    restore();
  }
});

test("envProxy", () => {
  const on = { NODE_USE_ENV_PROXY: "1", HTTP_PROXY: "http://proxy:3128", https_proxy: "http://sproxy:3128" };
  const cases: [Record<string, string>, string, string | undefined][] = [
    [on, "http://a.example/x", "http://proxy:3128"],
    [on, "https://a.example/x", "http://sproxy:3128"],
    [{ ...on, NODE_USE_ENV_PROXY: "" }, "http://a.example/x", undefined],
    [{ ...on, NO_PROXY: "a.example" }, "http://a.example/x", undefined],
    [{ ...on, NO_PROXY: ".example" }, "http://b.a.example/x", undefined],
    [{ ...on, no_proxy: "*" }, "http://a.example/x", undefined],
    [{ ...on, NO_PROXY: "a.example:8080" }, "http://a.example/x", "http://proxy:3128"],
    [{ ...on, NO_PROXY: "a.example:8080" }, "http://a.example:8080/x", undefined],
    [{ ...on, NO_PROXY: "example.org, ab.example" }, "http://b.example/x", "http://proxy:3128"],
    [{ NODE_USE_ENV_PROXY: "1", ALL_PROXY: "http://proxy:3128" }, "http://a.example/x", undefined],
  ];
  assert.deepEqual(cases.map(([env, url]) => envProxy(new URL(url), env)), cases.map(([, , want]) => want));
});

test("refuses a request through a proxy", async () => {
  const saved = { ...process.env };
  const { restore } = mockResolver({ "public.test": "93.184.215.14", "internal.test": "10.0.0.1" });
  Object.assign(process.env, { NODE_USE_ENV_PROXY: "1", HTTP_PROXY: "http://127.0.0.1:9" });
  const before = state.requests;
  try {
    await assert.rejects(
      readHttpRange("http://public.test/x", 0, 4, {}, undefined, { fetch: checkedFetch(loopbackOnly(), {}) }),
      /through the proxy.*allowUncheckedProxy: true/,
    );
    // with the opt-out, the name is still checked where the reader runs
    await assert.rejects(
      readHttpRange("http://internal.test/x", 0, 4, {}, undefined, { fetch: checkedFetch(loopbackOnly(), { allowUncheckedProxy: true }) }),
      /is a private address/,
    );
    // NO_PROXY: sent directly, and checked
    process.env.NO_PROXY = "127.0.0.1";
    const archive = await archiveOf(`${base}/blob.bin`, { allowUncheckedProxy: true });
    await assert.rejects(archive.read("r"), resolution(/is a loopback address/));
    const { data } = await read(`${base}/blob.bin`, loopbackOnly());
    assert.deepEqual([...data], [...BLOB.subarray(0, 4)]);
    assert.equal(state.requests, before + 1);
  } finally {
    for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
    restore();
  }
});
