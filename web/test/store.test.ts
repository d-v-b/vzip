// Store inputs (VIRTUALIZE.md §1.4–§1.6), as tests/test_virtualize_store.py
// checks them for the Python reference: the listing endpoint, the listing
// body parser, the strict JSON reader, and listing over HTTP.

import assert from "node:assert/strict";
import { test } from "node:test";
import { declare, stringifyJson } from "../src/virtualize/common.ts";
import { virtualizeN5 } from "../src/virtualize/n5/virtualize.ts";
import { virtualizeZarr2 } from "../src/virtualize/zarr2/virtualize.ts";
import {
  asInt,
  compareKeys,
  findChunks,
  implicitGroups,
  insideArray,
  listingEndpoint,
  NoSourceText,
  PathTrie,
  nesting,
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
    [listing("", "false", "<x>".repeat(255) + "</x>".repeat(255)), [], undefined], // 256 deep, the most allowed
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
  ["truncated without a Contents", listing("", "true", "<NextContinuationToken>t</NextContinuationToken>")],
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
  ["257 deep", listing("", "false", "<x>".repeat(256) + "</x>".repeat(256))],
  ["far deeper than the call stack", listing("", "false", "<x>".repeat(50000) + "</x>".repeat(50000))],
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
    [" [1, 1.0, 6.4e1, -0, 1e-400] ", [1, 1, 64, 0, 0]], // the integer -0 is 0, as in Python
    ['"\\ud800"', "\ud800"],
    ["[".repeat(256) + "]".repeat(256), nested(256)],
    ['{"é": "\\u00e9"}', { é: "é" }],
  ];
  for (const [text, want] of cases) assert.deepEqual(parseJson(utf8.encode(text)), want, text);
  // An integer beyond 2^53 − 1 is kept exact, and written back with every digit.
  const big = parseJson(utf8.encode('{"x": 9007199254740993, "y": -18446744073709551615, "z": 9007199254740993.0}'));
  assert.equal(JSON.stringify(big), '{"x":9007199254740993,"y":-18446744073709551615,"z":9007199254740992}');
  // A non-integer -0 stays -0.0 when written back, as Python writes it, and reads as 0 for the layout.
  const zero = parseJson(utf8.encode('{"t": [-0.0, -0e0, 0.0]}')) as { t: unknown[] };
  assert.equal(JSON.stringify(zero), '{"t":[-0.0,-0.0,0]}');
  assert.equal(asInt(zero.t[0]), 0);
  // A string of 16M characters, plain or of escapes, does not overflow the nesting scan.
  for (const body of ["A".repeat(16_000_000), "\\n".repeat(8_000_000)]) {
    assert.equal(nesting(`{"s": "${body}", "a": [[1]]}`), 3);
  }
});

for (const data of ['{"a": NaN}', "[Infinity]", "[-Infinity]", "[1e400]", "[-1e999]", "1" + "0".repeat(5000), "[1,]",
  "{,}", "// x\n1", "﻿{}", "", "[".repeat(257) + "]".repeat(257), "'a'", "[01]", '"\x01"']) {
  test(`rejects the JSON ${JSON.stringify(data.slice(0, 20))}`, () => {
    assert.throws(() => parseJson(utf8.encode(data)), StoreError);
  });
}

test("fails, rather than rounds, where JSON.parse gives no source text", () => {
  // An engine without JSON.parse source text access calls the reviver without a context.
  const parse = JSON.parse;
  JSON.parse = ((text: string, reviver?: (k: string, v: unknown) => unknown) =>
    parse(text, reviver && ((k: string, v: unknown) => reviver(k, v)))) as typeof JSON.parse;
  try {
    assert.throws(() => parseJson(utf8.encode('{"x": 9007199254740993}')), NoSourceText);
  } finally {
    JSON.parse = parse;
  }
});

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
      [`${BASE}&continuation-token=t%201%2F%C3%A9`]: listing(obj("s/x/1", "0") + obj("s/e/", "0") + obj("s/f/../g", "0")),
    }),
  });
  assert.deepEqual([...store.objects], [["attributes.json", 10], ["x/0", 1], ["x/1", 0]]);
  // Ignored objects are recorded, but for empty folder markers (VIRTUALIZE.md §1.4).
  assert.deepEqual(store.ignored, ["", "a//b", "./c", "d/", "f/../g"]);
  assert.deepEqual([store.requests, store.listed], [2, 9]);
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
  // A truncated page that gives a continuation token again (§1.5).
  ["a repeated continuation token", {
    [BASE]: listing(obj("s/a"), "true", "<NextContinuationToken>t</NextContinuationToken>"),
    [`${BASE}&continuation-token=t`]: listing(obj("s/b"), "true", "<NextContinuationToken>t</NextContinuationToken>"),
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

test("records ignored keys and empty chunks", async () => {
  // The recorded ignored keys (VIRTUALIZE.md §1.4) and the empty chunk objects' keys are
  // vzip_source's source metadata under a root group, and vzip_source/empty.json under a root
  // array (conventions/zarr2/README.md §5); in UTF-8 byte order, each member only when not empty.
  const zarray = '{"zarr_format": 2, "shape": [4], "chunks": [2], "dtype": "|u1", "compressor": null, ' +
    '"fill_value": 7, "order": "C", "filters": null}';
  const n5 = '{"dimensions": [4], "blockSize": [2], "dataType": "uint8", "compression": {"type": "raw"}}';
  const zgroup = '{"zarr_format": 2}';
  const len = (s: string) => String(utf8.encode(s).length);
  const store = (keys: string, docs: Record<string, string>) => {
    const pages: Record<string, Uint8Array> = { [BASE]: listing(keys) };
    for (const [k, v] of Object.entries(docs)) pages[`http://h/b/s/${k}`] = utf8.encode(v);
    return openHttpStore("http://h/b/s/", { fetch: fakeFetch(pages) });
  };
  const weird = obj("s/a//0", "9") + obj("s/\u00e9/./x") + obj("s/b/../c", "0") + obj("s/m/", "0");
  const cases: [typeof virtualizeZarr2, string, Record<string, string>, string | null, object | null][] = [
    // zarr2, root group: an empty chunk, ignored keys, a folder marker that is not recorded
    [virtualizeZarr2, obj("s/.zgroup", len(zgroup)) + obj("s/a/.zarray", len(zarray)) + obj("s/a/0") + obj("s/a/1", "0") + weird,
      { ".zgroup": zgroup, "a/.zarray": zarray }, "vzip_source/zarr.json",
      { empty: ["a/1"], ignored: ["a//0", "b/../c", "\u00e9/./x"] }],
    // zarr2, root array: only the empty chunk
    [virtualizeZarr2, obj("s/.zarray", len(zarray)) + obj("s/0") + obj("s/1", "0"), { ".zarray": zarray },
      "vzip_source/empty.json", { empty: ["1"] }],
    // zarr2, root array: only ignored keys
    [virtualizeZarr2, obj("s/.zarray", len(zarray)) + obj("s/0") + weird, { ".zarray": zarray },
      "vzip_source/empty.json", { ignored: ["a//0", "b/../c", "\u00e9/./x"] }],
    // n5, root dataset: an empty block
    [virtualizeN5, obj("s/attributes.json", len(n5)) + obj("s/0") + obj("s/1", "0"), { "attributes.json": n5 },
      "vzip_source/empty.json", { empty: ["1"] }],
    // zarr2, root group: nothing empty or ignored, no vzip_source
    [virtualizeZarr2, obj("s/.zgroup", len(zgroup)) + obj("s/m/", "0"), { ".zgroup": zgroup }, null, null],
  ];
  for (const [fn, keys, docs, where, want] of cases) {
    const out = await fn(await store(keys, docs));
    assert.ok(out.chunks.every(([, n]) => n > 0), keys);
    if (where === null) {
      assert.ok(![...out.docs.keys(), ...out.chunks.map(([k]) => k)].some((k) => k.startsWith("vzip_source/")), keys);
    } else if (where === "vzip_source/empty.json") {
      assert.deepEqual(out.docs.get(where), want, keys);
      assert.ok(!out.docs.has("vzip_source/zarr.json"), keys);
    } else {
      const profile = fn === virtualizeN5 ? "n5" : "zarr2";
      assert.deepEqual((out.docs.get(where) as { attributes: unknown }).attributes, declare({}, profile, undefined, want as never), keys);
    }
  }
});

test("finds nearest arrays and nodes", () => {
  // findChunks and the classification by nearest ancestor (§1.4; conventions/zarr2/README.md §2),
  // at any depth, the root array included.
  const isChunk = (rest: string) => rest === "0" || rest === "0/0";
  const deep = Array(500).fill("p").join("/");
  const objects = new Map<string, number>([["a/0", 1], ["a/x", 1], ["a/b/0", 0], ["a/0/0", 2], ["c/d/e/0", 3], ["c/0", 1],
    ["z", 1], [`${deep}/0`, 1]]);
  const cases: [[string, (rest: string) => boolean][], [string, number][]][] = [
    [[["a", isChunk]], [["a/0", 1], ["a/0/0", 2]]],
    [[["a", isChunk], ["a/b", isChunk]], [["a/0", 1], ["a/0/0", 2], ["a/b/0", 0]]],
    [[["c/d/e", isChunk], ["c", isChunk]], [["c/0", 1], ["c/d/e/0", 3]]],
    [[["", (rest) => rest === "z"]], [["z", 1]]],
    [[[deep, isChunk]], [[`${deep}/0`, 1]]],
    [[], []],
  ];
  for (const [arrays, want] of cases) assert.deepEqual(findChunks(objects, new Map(arrays)), want, String(arrays.map(([p]) => p)));
  const trie = new PathTrie<true>();
  for (const p of ["a", "c/d"]) trie.add(p, true);
  assert.deepEqual(["", "a", "a/b", "c", "c/d", "c/d/e/f", "x/y/z"].map((p) => insideArray(p, trie)),
    [false, false, true, false, false, true, false]);
  const root = new PathTrie<true>();
  root.add("", true);
  assert.deepEqual(["", "a", "a/b"].map((p) => insideArray(p, root)), [false, true, true]);
  assert.deepEqual([...implicitGroups(["", "a", "c/d", "x/y/z"])].sort(), ["c", "x", "x/y"]);
  const q = Array(500).fill("q").join("/");
  const implicit = implicitGroups([q]);
  assert.ok(implicit.size === 500 && implicit.has("") && !implicit.has(q));
});

test("stringifyJson writes a binary64 integer beyond 2^53 - 1 with its exact digits (VIRTUALIZE.md §1.1)", () => {
  const cases: [unknown, string][] = [
    [-(2 ** 64), "-18446744073709551616"], // JSON.stringify: -18446744073709552000
    [2 ** 53, "9007199254740992"],
    [2 ** 53 - 1, "9007199254740991"],
    [1e21, "1e+21"], // an exponent already: its binary64 value
    [1.5e300, "1.5e+300"],
    [0.5, "0.5"],
    [-0, "0"],
    [{ a: [2 ** 60, 1, "x"] }, '{"a":[1152921504606846976,1,"x"]}'],
  ];
  assert.deepEqual(cases.map(([v]) => stringifyJson(v)), cases.map(([, want]) => want));
});
