import os
import unittest

from helpers import ROOT  # noqa: F401
from vzip_impl.uri import (FileURIError, file_base_uri, file_uri_to_path, is_uri_reference, resolve,
                           normalise_abs_path)


class TestURIReference(unittest.TestCase):
    def test_valid_and_invalid(self):
        valid = ["a.bin", "a%20b.bin", "%C3%A9.bin", "../x", "/abs", "//host/p", "http://h:80/p?q#f",
                 "#x", "", "?q", "file:///x", "file:/x", "http://[::1]:8/x", "http://[v1.x]/",
                 "http://u:p@h/", "a/b:c", "mailto:x@y", "http://h:/p", "s3://bucket/key"]
        invalid = ["a b", "é.bin", "1a:b", "http://h:x/", "http://[::g]/", "%zz", "a#b#c", "http://a@b@c/",
                   "x\\y", "a^b", "http://h/[x]", "://x"]
        for s in valid:
            self.assertTrue(is_uri_reference(s), s)
        for s in invalid:
            self.assertFalse(is_uri_reference(s), s)

    def test_rfc3986_examples(self):
        base = "http://a/b/c/d;p?q"
        ex = {
            "g:h": "g:h", "g": "http://a/b/c/g", "./g": "http://a/b/c/g", "g/": "http://a/b/c/g/",
            "/g": "http://a/g", "//g": "http://g", "?y": "http://a/b/c/d;p?y", "g?y": "http://a/b/c/g?y",
            "#s": "http://a/b/c/d;p?q#s", "g#s": "http://a/b/c/g#s", "g?y#s": "http://a/b/c/g?y#s",
            ";x": "http://a/b/c/;x", "g;x": "http://a/b/c/g;x", "": "http://a/b/c/d;p?q",
            ".": "http://a/b/c/", "./": "http://a/b/c/", "..": "http://a/b/", "../": "http://a/b/",
            "../g": "http://a/b/g", "../..": "http://a/", "../../": "http://a/", "../../g": "http://a/g",
            "../../../g": "http://a/g", "../../../../g": "http://a/g", "/./g": "http://a/g",
            "/../g": "http://a/g", "g.": "http://a/b/c/g.", ".g": "http://a/b/c/.g", "g..": "http://a/b/c/g..",
            "..g": "http://a/b/c/..g", "./../g": "http://a/b/g", "./g/.": "http://a/b/c/g/",
            "g/./h": "http://a/b/c/g/h", "g/../h": "http://a/b/c/h", "g;x=1/./y": "http://a/b/c/g;x=1/y",
            "g;x=1/../y": "http://a/b/c/y", "g?y/./x": "http://a/b/c/g?y/./x",
            "g?y/../x": "http://a/b/c/g?y/../x", "g#s/./x": "http://a/b/c/g#s/./x",
            "g#s/../x": "http://a/b/c/g#s/../x", "http:g": "http:g",
        }
        for ref, want in ex.items():
            self.assertEqual(str(resolve(base, ref)), want, ref)

    def test_base_uri(self):
        self.assertEqual(file_base_uri("/data/my file.vzip"), "file:///data/my%20file.vzip")
        self.assertEqual(file_base_uri("//a/./b/../c//é"), "file:///a/c/%C3%A9")
        self.assertEqual(file_base_uri("/a/b:@!$&'()*+,;=~"), "file:///a/b:@!$&'()*+,;=~")
        self.assertEqual(file_base_uri("/a%b?#[]"), "file:///a%25b%3F%23%5B%5D")
        self.assertEqual(normalise_abs_path(b"/../x"), b"/x")
        self.assertEqual(normalise_abs_path(b"rel"), os.getcwdb() + b"/rel")

    def test_file_mapping(self):
        def p(base, ref):
            return file_uri_to_path(resolve(base, ref))
        self.assertEqual(p("file:///d/a.vzip", "x%20y.bin"), b"/d/x y.bin")
        self.assertEqual(p("file:///d/a.vzip", "file://LOCALHOST/z"), b"/z")
        self.assertEqual(p("file:///d/a.vzip", "FILE:/z#frag"), b"/z")
        self.assertEqual(p("file:///d/a.vzip", "%C3%A9"), "/d/é".encode())


class TestFileMappingErrors(unittest.TestCase):
    def bad(self, ref, base="file:///d/a.vzip"):
        with self.assertRaises(FileURIError):
            file_uri_to_path(resolve(base, ref))

    def test_remote_authority(self):
        self.bad("file://host/x")

    def test_relative_path(self):
        self.bad("file:x")

    def test_query(self):
        self.bad("x?")

    def test_encoded_slash(self):
        self.bad("a%2Fb")

    def test_nul(self):
        self.bad("a%00b")

    def test_encoded_dot_dot(self):
        self.bad("%2E%2E/x")

    def test_encoded_dot(self):
        self.bad("%2e/x")


if __name__ == "__main__":
    unittest.main()
