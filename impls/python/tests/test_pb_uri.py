import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vzip_impl import pb, uri  # noqa: E402
from vzip_impl.httpfetch import format_imf_fixdate, parse_imf_fixdate  # noqa: E402


class TestProtobuf(unittest.TestCase):
    def test_encode_decode(self):
        cases = [
            (dict(), b""),
            (dict(source=1, offset=2, length=3), bytes.fromhex("080118022003")),
            (dict(data=b""), bytes.fromhex("2a00")),
            (dict(data=b"ab"), bytes.fromhex("2a026162")),
        ]
        for kw, enc in cases:
            self.assertEqual(pb.encode_range(**kw), enc)
            d = pb.decode(enc, pb.RANGE)
            self.assertEqual(d.get("data"), kw.get("data"))
        self.assertEqual(pb.encode_concat([b""]), bytes.fromhex("0a00"))
        self.assertEqual(pb.enc_varint(-1), bytes.fromhex("ffffffffffffffffff01"))
        s = pb.encode_source("url", "x", size=0, etag='"a"', mnf=-1)
        d = pb.decode(s, pb.SOURCE)
        self.assertEqual(d, {"kind": ("url", "x"), "size": 0, "etag": '"a"', "modified_not_after": -1})
        # last oneof member wins; unknown + reserved fields skipped; non-minimal varints accepted
        d = pb.decode(bytes.fromhex("0a0178") + bytes.fromhex("1a00") + bytes.fromhex("3d01020304"), pb.SOURCE)
        self.assertEqual(d["kind"], ("data", b""))
        d = pb.decode(bytes.fromhex("1005") + bytes.fromhex("188100") + bytes.fromhex("11" + "00" * 8), pb.RANGE)
        self.assertEqual(d, {"offset": 1})
        # last occurrence wins
        self.assertEqual(pb.decode(bytes.fromhex("08010802"), pb.RANGE)["source"], 2)

    def _bad(self, hexs, schema=pb.RANGE):
        with self.assertRaises(pb.Malformed):
            pb.decode(bytes.fromhex(hexs), schema)

    def test_group_wire_type(self):
        self._bad("6b")  # field 13, wire type 3

    def test_end_group(self):
        self._bad("6c")

    def test_wire_type_6_7(self):
        self._bad("6e00")
        self._bad("6f00")

    def test_field_zero(self):
        self._bad("0001")

    def test_field_too_large(self):
        self._bad("808080801001")  # field 2^29
        self.assertEqual(pb.decode(bytes.fromhex("f8ffffff0f01"), pb.RANGE), {})

    def test_truncated(self):
        self._bad("08")
        self._bad("2a05616263")
        self._bad("1180")

    def test_long_varint(self):
        self._bad("18" + "80" * 10 + "00")

    def test_varint_overflow(self):
        self._bad("18ffffffffffffffffff02")

    def test_wrong_wire_type(self):
        self._bad("0d00000000")  # field 1 as I32
        self._bad("2801")  # data as VARINT

    def test_uint32_overflow(self):
        self._bad("0880808080 10".replace(" ", ""))

    def test_bad_utf8(self):
        self._bad("0a01ff", pb.SOURCE)
        self._bad("0a03eda080", pb.SOURCE)  # surrogate


class TestURI(unittest.TestCase):
    def test_uri_reference(self):
        good = ["a.bin", "a%20b.bin", "%C3%A9.bin", "http://h/x", "file:///x", "#x", "", "//h", "?q",
                "http://[::1]:80/", "http://[v1.x]/", "a:b", "../x", "http://u@h:/p?q#f",
                "http://192.168.0.1/", "mailto:a@b"]
        bad = ["a b", "é", "%zz", "1a:b", "http://[::1/", "a\nb", "http://h/x y", "[", "x#a#b",
               "http://[::1::2]/"]
        for s in good:
            self.assertTrue(uri.is_uri_reference(s), s)
        for s in bad:
            self.assertFalse(uri.is_uri_reference(s), s)

    def test_resolve_rfc_examples(self):
        base = "http://a/b/c/d;p?q"
        ex = {
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
            "g?y/./x": "http://a/b/c/g?y/./x", "g?y/../x": "http://a/b/c/g?y/../x",
            "g#s/./x": "http://a/b/c/g#s/./x", "g#s/../x": "http://a/b/c/g#s/../x", "http:g": "http:g",
        }
        for r, t in ex.items():
            self.assertEqual(str(uri.resolve(base, r)), t, r)

    def test_base_uri(self):
        self.assertEqual(uri.path_to_file_uri("/data/my file.vzip"), "file:///data/my%20file.vzip")
        self.assertEqual(uri.path_to_file_uri("/a//b/./c/../d é#?.v"), "file:///a/b/d%20%C3%A9%23%3F.v")
        self.assertEqual(uri.path_to_file_uri("/../x"), "file:///x")
        cwd = os.getcwd()
        self.assertEqual(uri.path_to_file_uri("q.vzip"), uri.path_to_file_uri(cwd + "/q.vzip"))

    def test_file_mapping(self):
        ok = {"file:///x/a%20b": b"/x/a b", "file:/x": b"/x", "FILE://LocalHost/x#frag": b"/x",
              "file:///a//b": b"/a//b", "file:///%C3%A9": "/é".encode()}
        for s, p in ok.items():
            self.assertEqual(uri.file_uri_to_path(uri.split(s)), p, s)

    def _badfile(self, s):
        with self.assertRaises(uri.FileMappingError):
            uri.file_uri_to_path(uri.split(s))

    def test_file_host(self):
        self._badfile("file://host/x")

    def test_file_port_userinfo_pct(self):
        self._badfile("file://localhost:80/x")
        self._badfile("file://u@localhost/x")
        self._badfile("file://local%68ost/x")

    def test_file_relative_path(self):
        self._badfile("file:x")

    def test_file_query(self):
        self._badfile("file:///x?")
        self._badfile("file:///x?a")

    def test_file_encoded_slash_nul(self):
        self._badfile("file:///a%2fb")
        self._badfile("file:///a%00b")

    def test_file_encoded_dots(self):
        self._badfile("file:///a/%2E%2E/b")
        self._badfile("file:///a/%2e")

    def test_http_host_port(self):
        self.assertEqual(uri.http_host_port(uri.split("http://h:8080/x")), ("h", 8080))
        self.assertEqual(uri.http_host_port(uri.split("http://h:/x")), ("h", 80))
        self.assertEqual(uri.http_host_port(uri.split("http://[::1]/x")), ("[::1]", 80))
        for bad in ["http://u@h/", "http:///x", "http:/x", "http://:80/"]:
            with self.assertRaises(uri.HttpUrlError):
                uri.http_host_port(uri.split(bad))

    def test_imf_fixdate(self):
        self.assertEqual(parse_imf_fixdate("Sun, 06 Nov 1994 08:49:37 GMT"), 784111777)
        self.assertEqual(format_imf_fixdate(784111777), "Sun, 06 Nov 1994 08:49:37 GMT")
        self.assertEqual(format_imf_fixdate(-1), "Wed, 31 Dec 1969 23:59:59 GMT")
        for bad in ["Mon, 06 Nov 1994 08:49:37 GMT", "Sunday, 06-Nov-94 08:49:37 GMT",
                    "Sun Nov  6 08:49:37 1994", "Sun, 6 Nov 1994 08:49:37 GMT", "Sun, 06 Nov 1994 08:49:37 UTC",
                    "Thu, 30 Feb 1995 00:00:00 GMT", "sun, 06 Nov 1994 08:49:37 GMT"]:
            self.assertIsNone(parse_imf_fixdate(bad), bad)


if __name__ == "__main__":
    unittest.main()
