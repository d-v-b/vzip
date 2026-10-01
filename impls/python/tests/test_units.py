import json
import os
import unittest

from helpers import cli, tmpdir, write_desc
from vzip_impl import pb, uri


class UriTests(unittest.TestCase):
    def test_rfc3986_examples(self):
        base = "http://a/b/c/d;p?q"
        cases = {
            "g:h": "g:h", "g": "http://a/b/c/g", "./g": "http://a/b/c/g", "g/": "http://a/b/c/g/",
            "/g": "http://a/g", "//g": "http://g", "?y": "http://a/b/c/d;p?y", "g?y": "http://a/b/c/g?y",
            "#s": "http://a/b/c/d;p?q#s", "g#s": "http://a/b/c/g#s", "g?y#s": "http://a/b/c/g?y#s",
            ";x": "http://a/b/c/;x", "g;x": "http://a/b/c/g;x", "g;x?y#s": "http://a/b/c/g;x?y#s",
            "": "http://a/b/c/d;p?q", ".": "http://a/b/c/", "./": "http://a/b/c/", "..": "http://a/b/",
            "../": "http://a/b/", "../g": "http://a/b/g", "../..": "http://a/", "../../": "http://a/",
            "../../g": "http://a/g", "../../../g": "http://a/g", "../../../../g": "http://a/g",
            "/./g": "http://a/g", "/../g": "http://a/g", "g.": "http://a/b/c/g.", ".g": "http://a/b/c/.g",
            "g..": "http://a/b/c/g..", "..g": "http://a/b/c/..g", "./../g": "http://a/b/g",
            "./g/.": "http://a/b/c/g/", "g/./h": "http://a/b/c/g/h", "g/../h": "http://a/b/c/h",
            "g;x=1/./y": "http://a/b/c/g;x=1/y", "g;x=1/../y": "http://a/b/c/y",
            "g?y/./x": "http://a/b/c/g?y/./x", "g#s/../x": "http://a/b/c/g#s/../x",
            "http:g": "http:g",  # strict
        }
        for ref, exp in cases.items():
            self.assertTrue(uri.is_uri_reference(ref), ref)
            self.assertEqual(uri.recompose(*uri.resolve(base, ref)), exp, ref)

    def test_uri_reference_validation(self):
        good = ["a%20b.bin", "%C3%A9.bin", "http://[::1]:80/x", "http://[v1.x]/", "s3://b/k?x#y",
                "//host", "?", "#", "a/b:c", "file:///x", "http://u:p@h:8080/p"]
        bad = ["a b", "é", ":x", "a:b c", "%2", "%gg", "http://[::1/", "a\\b", "x#a#b",
               "1a:b"[:0] + "[x]", "http://h/<>", "\x00"]
        for g in good:
            self.assertTrue(uri.is_uri_reference(g), g)
        for b in bad:
            self.assertFalse(uri.is_uri_reference(b), b)

    def test_base_uri(self):
        self.assertEqual(uri.path_to_base_uri("/data/my file.vzip"), "file:///data/my%20file.vzip")
        self.assertEqual(uri.path_to_base_uri("/a//b/./c/../d.vzip"), "file:///a/b/d.vzip")
        self.assertEqual(uri.path_to_base_uri("/é[]#?%.v"), "file:///%C3%A9%5B%5D%23%3F%25.v")
        self.assertEqual(uri.path_to_base_uri("/x/!$&'()*+,;=:@~-_.v"), "file:///x/!$&'()*+,;=:@~-_.v")
        self.assertEqual(uri.path_to_base_uri("/../../x"), "file:///x")
        self.assertTrue(uri.path_to_base_uri("rel.vzip").endswith("/rel.vzip"))


class ProtoTests(unittest.TestCase):
    def test_canonical_encoding(self):
        self.assertEqual(pb.encode_range(pb.Range()), b"")
        self.assertEqual(pb.encode_range(pb.Range(data=b"")), b"\x2a\x00")
        self.assertEqual(pb.encode_range(pb.Range(source=1, offset=300, length=2)), b"\x08\x01\x18\xac\x02\x20\x02")
        self.assertEqual(pb.encode_concat([]), b"")
        self.assertEqual(pb.encode_concat([pb.Range(), pb.Range(data=b"a")]), b"\x0a\x00\x0a\x03\x2a\x01a")
        s = pb.Source("url", b"", size=0, etag=b"", modified_not_after=-1)
        self.assertEqual(pb.encode_source(s), b"\x0a\x00\x20\x00\x2a\x00\x30" + b"\xff" * 9 + b"\x01")
        self.assertEqual(pb.encode_source(pb.Source("data", b"")), b"\x1a\x00")
        idx = pb.CdIndex([pb.Page(b"a", 0, 5)], [pb.Pinned(b"p", 0, 0, 0, 0)])
        self.assertEqual(pb.encode_cdindex(idx), b"\x0a\x05\x0a\x01a\x18\x05\x12\x03\x0a\x01p")

    def test_decode_round_trip(self):
        s = pb.Source("url", b"x", size=5, etag=b'"e"', modified_not_after=-5)
        self.assertEqual(pb.decode_source(pb.encode_source(s)), s)
        r = pb.Range(source=3, offset=2 ** 64 - 2, length=1)
        self.assertEqual(pb.decode_range(pb.encode_range(r)), r)

    def test_string_validation(self):
        for bad in (b"\x0a\x01\xff", b"\x0a\x03\xed\xa0\x80", b"\x0a\x02\xc0\x80"):
            with self.assertRaises(pb.Malformed):
                pb.decode_source(bad)
        self.assertEqual(pb.decode_source(b"\x0a\x03\xef\xbb\xbf").value, b"\xef\xbb\xbf")


class CliTests(unittest.TestCase):
    def test_invalid_queries_exit_nonzero(self):
        with tmpdir() as d:
            a = os.path.join(d, "a.vzip")
            write_desc({}, a)
            for qs in ['{"op":"get","key":"a"}', '[{"op":"nope"}]', '[{"op":"get"}]',
                       '[{"op":"get","key":"a","range":{"start":1,"end":2,"suffix":1}}]',
                       '[{"op":"get","key":"a","range":{"start":1}}]', 'not json']:
                qp = os.path.join(d, "q.json")
                with open(qp, "w") as fh:
                    fh.write(qs)
                self.assertNotEqual(cli("read", a, qp).returncode, 0, qs)
            self.assertNotEqual(cli("read", a, os.path.join(d, "missing.json")).returncode, 0)

    def test_open_failure_is_reported_with_status_0(self):
        with tmpdir() as d:
            qp = os.path.join(d, "q.json")
            with open(qp, "w") as fh:
                fh.write('[{"op":"list","prefix":""}]')
            for target in (os.path.join(d, "missing.vzip"), qp, d):
                p = cli("read", target, qp)
                self.assertEqual(p.returncode, 0, p.stderr)
                out = json.loads(p.stdout)
                self.assertEqual(out["open"]["ok"], False)
                self.assertEqual(out["open"]["class"], "archive")
                self.assertEqual(out["results"], [])


if __name__ == "__main__":
    unittest.main()
