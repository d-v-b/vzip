import { test } from "node:test";
import assert from "node:assert/strict";
import { UriMapError, baseUriForPath, fileUriToPath, parseUriReference, resolve, uriToString } from "../src/uri.ts";
import { formatImfFixdate, parseHttpDate } from "../src/http.ts";

test("RFC 3986 §5.4 resolution examples, validation and base URIs", () => {
  const base = parseUriReference("http://a/b/c/d;p?q")!;
  const cases: Record<string, string> = {
    "g:h": "g:h", g: "http://a/b/c/g", "./g": "http://a/b/c/g", "g/": "http://a/b/c/g/", "/g": "http://a/g",
    "//g": "http://g", "?y": "http://a/b/c/d;p?y", "g?y": "http://a/b/c/g?y", "#s": "http://a/b/c/d;p?q#s",
    "": "http://a/b/c/d;p?q", "../../../g": "http://a/g", "g;x=1/../y": "http://a/b/c/y", "http:g": "http:g",
  };
  for (const [r, want] of Object.entries(cases)) assert.equal(uriToString(resolve(base, parseUriReference(r)!)), want, r);
  for (const ok of ["a%20b.bin", "%C3%A9.bin", "http://[::1]:8080/x", "http://[v7.a]/", "file:///x", "a?b#c", "http://u:p@h/"])
    assert.ok(parseUriReference(ok), ok);
  for (const bad of ["a b", "é.bin", "%zz", "a:b:c d", "http://h:x/", "http://[::g]/", "1a:b", "a#b#c", "\\x"])
    assert.equal(parseUriReference(bad), null, bad);
  assert.equal(baseUriForPath("/data/my file.vzip"), "file:///data/my%20file.vzip");
  assert.equal(baseUriForPath("/a//b/./c/../d é"), "file:///a/b/d%20%C3%A9");
  assert.equal(fileUriToPath(parseUriReference("file:/x/a%20b")!).toString(), "/x/a b");
  assert.equal(fileUriToPath(parseUriReference("FILE://LocalHost/x")!).toString(), "/x");
  assert.equal(fileUriToPath(parseUriReference("file:///x#frag")!).toString(), "/x");
  // HTTP-date
  assert.equal(formatImfFixdate(784111777n), "Sun, 06 Nov 1994 08:49:37 GMT");
  assert.equal(parseHttpDate("Sun, 06 Nov 1994 08:49:37 GMT"), 784111777n);
  assert.equal(parseHttpDate("Sunday, 06-Nov-94 08:49:37 GMT"), 784111777n);
  assert.equal(parseHttpDate("Sun Nov  6 08:49:37 1994"), 784111777n);
  assert.equal(formatImfFixdate(-62135596800n), "Mon, 01 Jan 0001 00:00:00 GMT");
  assert.equal(formatImfFixdate(-62135596801n), null);
  assert.equal(formatImfFixdate(253402300800n), null);
  assert.equal(parseHttpDate("garbage"), null);
});

test("file: mapping rejects a remote authority", () => {
  assert.throws(() => fileUriToPath(parseUriReference("file://host/x")!), UriMapError);
});
test("file: mapping rejects a relative path", () => {
  assert.throws(() => fileUriToPath(parseUriReference("file:x")!), UriMapError);
});
test("file: mapping rejects a query, even empty", () => {
  assert.throws(() => fileUriToPath(parseUriReference("file:///x?")!), UriMapError);
});
test("file: mapping rejects encoded slash", () => {
  assert.throws(() => fileUriToPath(parseUriReference("file:///a%2Fb")!), UriMapError);
});
test("file: mapping rejects NUL", () => {
  assert.throws(() => fileUriToPath(parseUriReference("file:///a%00b")!), UriMapError);
});
test("file: mapping rejects encoded dot segments", () => {
  assert.throws(() => fileUriToPath(parseUriReference("file:///a/%2E%2E/b")!), UriMapError);
  assert.throws(() => fileUriToPath(parseUriReference("file:///a/%2e/b")!), UriMapError);
});
