// Store inputs (VIRTUALIZE.md §1.4–§1.6), as tests/test_virtualize_store.py
// checks them for the Python reference: the listing endpoint, the listing
// body parser, the strict JSON reader, and listing over HTTP.

import assert from "node:assert/strict";
import { test } from "node:test";
import {
  compareKeys,
  listingEndpoint,
  objectUrl,
  openHttpStore,
  parseJson,
  parseListing,
  StoreError,
  StoreLimitError,
  StoreReadError,
} from "../src/virtualize/store.ts";

const utf8 = new TextEncoder();
const XML = (s: string) =>
  `<?xml version="1.0" encoding="UTF-8"?>\n<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">${s}</ListBucketResult>`;
const listing = (contents: string, truncated = "false", extra = "") =>
  utf8.encode(XML(`<IsTruncated>${truncated}</IsTruncated>${extra}${contents}`));
const obj = (key: string, size = "1") => `<Contents><Key>${key}</Key><LastModified>x</LastModified><Size>${size}</Size></Contents>`;

test("finds listing endpoints and object URLs", () => {
  const cases: [string, [string, string]][] = [
    ["https://janelia-cosem-datasets.s3.amazonaws.com/jrc_hela-2/jrc_hela-2.n5/em/",
      ["https://janelia-cosem-datasets.s3.amazonaws.com/", "jrc_hela-2/jrc_hela-2.n5/em/"]],
    ["https://janelia-cosem-datasets.s3.amazonaws.com/", ["https://janelia-cosem-datasets.s3.amazonaws.com/", ""]],
    ["https://bucket.s3.us-east-1.amazonaws.com/a/b/", ["https://bucket.s3.us-east-1.amazonaws.com/", "a/b/"]],
    ["https://bucket.s3-us-west-2.amazonaws.com/a/", ["https://bucket.s3-us-west-2.amazonaws.com/", "a/"]],
    ["HTTPS://Bucket.S3.AmazonAWS.com:443/a/", ["HTTPS://Bucket.S3.AmazonAWS.com:443/", "a/"]],
    ["https://s3.amazonaws.com/janelia-cosem-datasets/jrc_hela-2/jrc_hela-2.n5/em/",
      ["https://s3.amazonaws.com/janelia-cosem-datasets/", "jrc_hela-2/jrc_hela-2.n5/em/"]],
    ["https://s3.us-east-1.amazonaws.com/bucket/", ["https://s3.us-east-1.amazonaws.com/bucket/", ""]],
    ["http://127.0.0.1:8765/f/n5/x/", ["http://127.0.0.1:8765/f/", "n5/x/"]],
    ["https://storage.googleapis.com/b/p%20q/%C3%A9/", ["https://storage.googleapis.com/b/", "p q/é/"]],
    ["http://[::1]:9000/b/k/", ["http://[::1]:9000/b/", "k/"]],
  ];
  for (const [url, want] of cases) assert.deepEqual(listingEndpoint(url), want, url);
  assert.equal(objectUrl("https://h/x/", "a b/0.0"), "https://h/x/a%20b/0.0");
  assert.equal(objectUrl("https://h/x/", "é/%/:@!$&'()*+,;=~"), "https://h/x/%C3%A9/%25/:@!$&'()*+,;=~");
  // Keys sort in code point (UTF-8) order, not UTF-16 order.
  assert.deepEqual(["\u{1F600}", "｡", "a"].sort(compareKeys), ["a", "｡", "\u{1F600}"]);
});

for (const url of ["https://h/b/x", "https://h/b/x/?q=1", "https://h/b/x/?", "https://h/b/x/#f", "https://user@h/b/x/",
  "ftp://h/b/x/", "https://h/", "https://h//x/", "https://h/b/%FF/", "https://h/b/a b/"]) {
  test(`rejects the store URL ${url}`, () => {
    assert.throws(() => listingEndpoint(url), StoreError);
  });
}

test("parses listings", () => {
  const cases: [Uint8Array, [string, number][], string | undefined][] = [
    [listing(obj("p/a", "5") + obj("p/b", "0")), [["p/a", 5], ["p/b", 0]], undefined],
    [listing(obj("p/a"), "true", "<NextContinuationToken>tok/+=</NextContinuationToken>"), [["p/a", 1]], "tok/+="],
    [listing(obj("p/a&amp;b&#x41;&#66;&lt;&gt;&quot;&apos;")), [["p/a&bAB<>\"'", 1]], undefined],
    [listing(obj("p/x", "007")), [["p/x", 7]], undefined],
    [listing("", "false", "<!-- a comment --><Name>bucket</Name><CommonPrefixes><Prefix>p/</Prefix></CommonPrefixes>"),
      [], undefined],
    [utf8.encode("  <!--c--><ListBucketResult a='1' b=\"&amp;\"><IsTruncated>false</IsTruncated><Empty/></ListBucketResult>\n"),
      [], undefined],
    [listing(obj("p/é \t")), [["p/é \t", 1]], undefined],
    [listing(obj("p/a"), "false", "<NextContinuationToken>ignored</NextContinuationToken>"), [["p/a", 1]], undefined],
  ];
  for (const [body, objects, token] of cases) {
    assert.deepEqual(parseListing(body, "p/"), token === undefined ? { objects } : { objects, token });
  }
});

const badListings: [string, Uint8Array][] = [
  ["html", utf8.encode("<html><body>Index of /</body></html>")],
  ["truncated", listing(obj("p/a")).subarray(0, -5)],
  ["after root", utf8.encode(new TextDecoder().decode(listing(obj("p/a"))) + "<x/>")],
  ["outside prefix", listing(obj("q/a"))],
  ["negative size", listing(obj("p/a", "-1"))],
  ["fraction size", listing(obj("p/a", "1.5"))],
  ["huge size", listing(obj("p/a", "9007199254740992"))],
  ["empty size", listing(obj("p/a", ""))],
  ["no size", listing("<Contents><Key>p/a</Key></Contents>")],
  ["two keys", listing("<Contents><Key>p/a</Key><Key>p/b</Key><Size>1</Size></Contents>")],
  ["key child", listing("<Contents><Key>p/<b/></Key><Size>1</Size></Contents>")],
  ["no token", listing(obj("p/a"), "true")],
  ["TRUE", listing(obj("p/a"), "TRUE")],
  ["no IsTruncated", utf8.encode(XML(obj("p/a")))],
  ["two IsTruncated", listing(obj("p/a"), "false", "<IsTruncated>false</IsTruncated>")],
  ["two tokens", listing(obj("p/a"), "true", "<NextContinuationToken>a</NextContinuationToken>".repeat(2))],
  ["unknown entity", listing(obj("p/&nbsp;"))],
  ["not a character", listing(obj("p/&#0;"))],
  ["control", listing(obj("p/\x01"))],
  ["]]>", listing(obj("p/a]]>b"))],
  ["CDATA", listing("<![CDATA[x]]>")],
  ["DOCTYPE", utf8.encode("<!DOCTYPE x><ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>")],
  ["PI", utf8.encode("<?pi x?><ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>")],
  ["mismatched", utf8.encode("<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResul>")],
  ["-- in comment", utf8.encode("<ListBucketResult><!-- a -- b --><IsTruncated>false</IsTruncated></ListBucketResult>")],
  ["BOM", Uint8Array.from([0xef, 0xbb, 0xbf, ...utf8.encode("<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>")])],
  ["not UTF-8", Uint8Array.from([...utf8.encode("<ListBucketResult><IsTruncated>false</IsTruncated><Key>"), 0xff,
    ...utf8.encode("</Key></ListBucketResult>")])],
  ["other root", utf8.encode("<ListAllMyBucketsResult><IsTruncated>false</IsTruncated></ListAllMyBucketsResult>")],
  ["error", utf8.encode("<Error><Code>NoSuchBucket</Code></Error>")],
];
for (const [name, body] of badListings) {
  test(`rejects a listing: ${name}`, () => {
    assert.throws(() => parseListing(body, "p/"), StoreError);
  });
}

test("reads JSON documents", () => {
  const nested = (d: number): unknown => (d === 1 ? [] : [nested(d - 1)]);
  const cases: [string, unknown][] = [
    ['{"a": 1, "a": 2}', { a: 2 }],
    [" [1, 1.0, 6.4e1, -0, 1e-400] ", [1, 1, 64, -0, 0]],
    ['{"x": 9007199254740993}', { x: 9007199254740992 }],
    ['"\\ud800"', "\ud800"],
    ["[".repeat(256) + "]".repeat(256), nested(256)],
    ['{"é": "\\u00e9"}', { é: "é" }],
  ];
  for (const [text, want] of cases) assert.deepEqual(parseJson(utf8.encode(text)), want, text);
});

for (const data of ['{"a": NaN}', "[Infinity]", "[-Infinity]", "[1e400]", "[-1e999]", "1" + "0".repeat(5000), "[1,]",
  "{,}", "// x\n1", "﻿{}", "", "[".repeat(257) + "]".repeat(257), "'a'", "[01]", '"\x01"']) {
  test(`rejects the JSON ${JSON.stringify(data.slice(0, 20))}`, () => {
    assert.throws(() => parseJson(utf8.encode(data)), StoreError);
  });
}

test("rejects JSON that is not UTF-8", () => {
  assert.throws(() => parseJson(Uint8Array.from([0x22, 0xff, 0x22])), StoreError);
});

function fakeFetch(pages: Record<string, Uint8Array | number>) {
  return async (url: string) => {
    const v = pages[url];
    if (v === undefined) throw new Error(`unexpected request ${url}`);
    return typeof v === "number" ? new Response("x", { status: v }) : new Response(v as BodyInit, { status: 200 });
  };
}

const BASE = "http://h/b/?list-type=2&prefix=s%2F";

test("lists a store over HTTP", async () => {
  const store = await openHttpStore("http://h/b/s/", {
    fetch: fakeFetch({
      [BASE]: listing(obj("s/attributes.json", "10") + obj("s/x/0") + obj("s/") + obj("s/a//b") + obj("s/./c") + obj("s/d/"),
        "true", "<NextContinuationToken>t 1/é</NextContinuationToken>"),
      [`${BASE}&continuation-token=t%201%2F%C3%A9`]: listing(obj("s/x/1", "0")),
    }),
  });
  assert.deepEqual([...store.objects], [["attributes.json", 10], ["x/0", 1], ["x/1", 0]]);
  assert.deepEqual([store.requests, store.listed], [2, 7]);
});

const listingErrors: [string, Record<string, Uint8Array | number>, Function][] = [
  ["404", { [BASE]: 404 }, StoreError],
  ["403", { [BASE]: 403 }, StoreError],
  ["an HTML page", { [BASE]: utf8.encode("<html>a directory index</html>") }, StoreError],
  ["400", { [BASE]: 400 }, StoreError],
  ["a duplicate key", { [BASE]: listing(obj("s/a") + obj("s/a")) }, StoreError],
  ["a key listed on two pages", {
    [BASE]: listing(obj("s/a"), "true", "<NextContinuationToken>t</NextContinuationToken>"),
    [`${BASE}&continuation-token=t`]: listing(obj("s/a")),
  }, StoreError],
  ["408", { [BASE]: 408 }, StoreReadError],
  ["503", { [BASE]: 503 }, StoreReadError],
];
for (const [name, pages, error] of listingErrors) {
  test(`listing: ${name}`, async () => {
    await assert.rejects(openHttpStore("http://h/b/s/", { fetch: fakeFetch(pages) }), error as never);
  });
}

test("fails past the resource limit", async () => {
  await assert.rejects(
    openHttpStore("http://h/b/s/", { fetch: fakeFetch({ [BASE]: listing(obj("s/a") + obj("s/b") + obj("s/c")) }), maxObjects: 2 }),
    StoreLimitError,
  );
});
