import { test } from "node:test";
import assert from "node:assert/strict";
import * as path from "node:path";
import { fileBaseUri, fileUriToPath, isUriReference, parseUriRef, resolve, uriToString } from "../src/url.ts";

test("RFC 3986 §5.4 reference resolution examples", () => {
  const base = parseUriRef("http://a/b/c/d;p?q")!;
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
    const r = parseUriRef(ref);
    assert.ok(r, `parse ${ref}`);
    assert.equal(uriToString(resolve(base, r)), want, ref);
  }
});

test("URI-reference validation", () => {
  for (const ok of ["a", "a%20b.bin", "%C3%A9.bin", "http://[::1]:80/x", "http://[v1.x]/", "http://1.2.3.4/", "#x", "?q", "", "s3://b/k", "file:///x", "//h", "a/b:c", "./a:b", "http://u:p@h:/x?a/b?#f/?"])
    assert.ok(isUriReference(ok), ok);
  for (const bad of ["a b", "é", "a%2", "a%zz", "1a:b", "a:b c", "http://[::1", "http://[1::2::3]/", "http://h:x/", "a\\b", "a#b#c", "http://a b/", "[x]", "a{b}", "http://[::1]x/"])
    assert.ok(!isUriReference(bad), bad);
});

test("file base URI and file: path mapping", () => {
  assert.equal(fileBaseUri("/data/my file.vzip"), "file:///data/my%20file.vzip");
  assert.equal(fileBaseUri("/a//b/./c/../d é"), "file:///a/b/d%20%C3%A9");
  assert.equal(fileBaseUri("x"), "file://" + encodeURI(path.join(process.cwd(), "x")).replace(/%5B/g, "%5B"));
  assert.equal(fileUriToPath(parseUriRef("file:///a%20b/%C3%A9")!).toString(), "/a b/é");
  assert.equal(fileUriToPath(parseUriRef("file://LocalHost/x")!).toString(), "/x");
  for (const bad of ["file://h/x", "file:///x?", "file:///a%2Fb", "file:///a%00", "file:///%2E%2E/x", "file:///a/%2e", "file:x"])
    assert.throws(() => fileUriToPath(parseUriRef(bad)!), bad);
});
