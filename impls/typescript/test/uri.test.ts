import { test } from "node:test";
import assert from "node:assert/strict";
import * as path from "node:path";
import { fileBaseUri, fileUriToPath, isUriReference, resolveUri } from "../src/uri.ts";

test("RFC 3986 §5.4 resolution examples", () => {
  const base = "http://a/b/c/d;p?q";
  const cases: Record<string, string> = {
    "g:h": "g:h", g: "http://a/b/c/g", "./g": "http://a/b/c/g", "g/": "http://a/b/c/g/", "/g": "http://a/g",
    "//g": "http://g", "?y": "http://a/b/c/d;p?y", "g?y": "http://a/b/c/g?y", "#s": "http://a/b/c/d;p?q#s",
    "g#s": "http://a/b/c/g#s", "g?y#s": "http://a/b/c/g?y#s", ";x": "http://a/b/c/;x", "g;x": "http://a/b/c/g;x",
    "g;x?y#s": "http://a/b/c/g;x?y#s", "": "http://a/b/c/d;p?q", ".": "http://a/b/c/", "./": "http://a/b/c/",
    "..": "http://a/b/", "../": "http://a/b/", "../g": "http://a/b/g", "../..": "http://a/", "../../": "http://a/",
    "../../g": "http://a/g", "../../../g": "http://a/g", "../../../../g": "http://a/g", "/./g": "http://a/g",
    "/../g": "http://a/g", "g.": "http://a/b/c/g.", ".g": "http://a/b/c/.g", "g..": "http://a/b/c/g..",
    "..g": "http://a/b/c/..g", "./../g": "http://a/b/g", "./g/.": "http://a/b/c/g/", "g/./h": "http://a/b/c/g/h",
    "g/../h": "http://a/b/c/h", "g;x=1/./y": "http://a/b/c/g;x=1/y", "g;x=1/../y": "http://a/b/c/y",
    "g?y/./x": "http://a/b/c/g?y/./x", "g?y/../x": "http://a/b/c/g?y/../x", "g#s/./x": "http://a/b/c/g#s/./x",
    "g#s/../x": "http://a/b/c/g#s/../x", "http:g": "http:g",
  };
  for (const [ref, want] of Object.entries(cases)) {
    assert.ok(isUriReference(ref), ref);
    assert.equal(resolveUri(base, ref), want, ref);
  }
  // file: base built from a local path
  assert.equal(fileBaseUri("/data/my file.vzip"), "file:///data/my%20file.vzip");
  assert.equal(fileBaseUri("/a//b/./c/../é%.vzip"), "file:///a/b/%C3%A9%25.vzip");
  assert.equal(fileBaseUri("x.vzip"), "file://" + path.resolve("x.vzip").split("/").map(encodeURIComponent).join("/"));
  assert.equal(resolveUri("file:///d/x.vzip", "a%20b.bin"), "file:///d/a%20b.bin");
  // file: URI -> path
  const ok: Record<string, string> = {
    "file:///x/a%20b": "/x/a b", "file:/x": "/x", "FILE://LocalHost/x#frag": "/x", "file:///%C3%A9": "/é",
  };
  for (const [u, p] of Object.entries(ok)) assert.equal((fileUriToPath(u) as Buffer).toString("utf8"), p, u);
  // validity
  for (const v of ["a b", "%zz", "%C3%A9.bin", "a%20b.bin", "http://[::1]:80/x", "http://[v1.x]/", "http://u:p@h:/x", "a:b", "#x", "?q"]) {
    assert.equal(isUriReference(v), !["a b", "%zz"].includes(v), v);
  }
});

const badFileUris: [string, string][] = [
  ["remote authority", "file://host/x"],
  ["query", "file:///x?"],
  ["encoded slash", "file:///a%2Fb"],
  ["NUL", "file:///a%00b"],
  ["encoded dot-dot", "file:///a/%2E%2E/b"],
  ["encoded dot", "file:///a/%2e/b"],
  ["relative path", "file:x"],
];
for (const [name, u] of badFileUris) {
  test(`file: URI rejected: ${name}`, () => {
    assert.equal(typeof fileUriToPath(u), "string");
  });
}

const invalidRefs = ["é.bin", "a b", "1a:b", "//h[/x", "http://[zz]/", "x#a#b", "%2", "http://a@b@c/", "http://h:8x/"];
for (const r of invalidRefs) {
  test(`invalid URI-reference: ${JSON.stringify(r)}`, () => {
    assert.equal(isUriReference(r), false);
  });
}
