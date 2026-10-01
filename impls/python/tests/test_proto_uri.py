import os
import unittest

from helpers import ROOT  # noqa: F401  (sets sys.path)

from vzip_impl import proto, uri
from vzip_impl.fetch import imf_fixdate, parse_http_date
from vzip_impl.proto import ProtoError, decode, enc_varint


class TestProtoDecode(unittest.TestCase):
    def test_valid_decodings(self):
        cases = [
            (b"", proto.RANGE, {}),
            (b"\x08\x05\x18\x0a\x20\x04", proto.RANGE, {"source": 5, "offset": 10, "length": 4}),
            (b"\x2a\x00", proto.RANGE, {"data": b""}),
            # unknown fields of each skippable wire type, and reserved field 2
            (b"\x10\x07\x31" + b"\0" * 8 + b"\x35" + b"\0" * 4 + b"\x3a\x01x\x08\x01",
             proto.RANGE, {"source": 1}),
            # last occurrence wins, non-minimal varint accepted
            (b"\x08\x01\x08\x82\x00", proto.RANGE, {"source": 2}),
            # 10-byte varint of 2^64-1
            (b"\x18" + b"\xff" * 9 + b"\x01", proto.RANGE, {"offset": (1 << 64) - 1}),
            # oneof: last member wins
            (b"\x0a\x01a\x12\x01b", proto.SOURCE, {"key": "b"}),
            # int64 negative
            (b"\x0a\x01a\x30" + enc_varint(-5), proto.SOURCE, {"url": "a", "modified_not_after": -5}),
            (b"\x0a\x02\x2a\x00\x0a\x00", proto.CONCAT, {"parts": [{"data": b""}, {}]}),
        ]
        for buf, schema, want in cases:
            got = decode(buf, schema)
            got = {k: v for k, v in got.items() if v != [] or k in want}
            self.assertEqual(got, want, buf)

    def test_wire_type_3_4(self):
        for wt in (3, 4, 6, 7):
            with self.assertRaises(ProtoError):
                decode(bytes([(9 << 3) | wt]), proto.RANGE)

    def test_field_number_zero(self):
        with self.assertRaises(ProtoError):
            decode(b"\x00\x00", proto.RANGE)

    def test_field_number_too_large(self):
        with self.assertRaises(ProtoError):
            decode(enc_varint((1 << 29) << 3) + b"\x00", proto.RANGE)

    def test_truncated(self):
        for buf in (b"\x08", b"\x08\x80", b"\x2a\x05ab", b"\x31\x00"):
            with self.assertRaises(ProtoError):
                decode(buf, proto.RANGE)

    def test_varint_too_long(self):
        with self.assertRaises(ProtoError):
            decode(b"\x08" + b"\x80" * 10 + b"\x00", proto.RANGE)

    def test_varint_overflow(self):
        with self.assertRaises(ProtoError):
            decode(b"\x18" + b"\xff" * 9 + b"\x02", proto.RANGE)

    def test_wrong_wire_type_known_field(self):
        with self.assertRaises(ProtoError):
            decode(b"\x0a\x00", proto.RANGE)  # source as LEN
        with self.assertRaises(ProtoError):
            decode(b"\x28\x00", proto.RANGE)  # data as VARINT

    def test_uint32_overflow(self):
        with self.assertRaises(ProtoError):
            decode(b"\x08" + enc_varint(1 << 32), proto.RANGE)

    def test_bad_utf8(self):
        with self.assertRaises(ProtoError):
            decode(b"\x0a\x01\xff", proto.SOURCE)

    def test_nested_len_overrun(self):
        with self.assertRaises(ProtoError):
            decode(b"\x0a\x02\x2a\x05", proto.CONCAT)


class TestProtoEncode(unittest.TestCase):
    def test_canonical(self):
        self.assertEqual(proto.encode_range({"source": 0, "offset": 0, "length": 0}), b"")
        self.assertEqual(proto.encode_range({"data": b""}), b"\x2a\x00")
        self.assertEqual(proto.encode_source({"data": b""}), b"\x1a\x00")
        self.assertEqual(proto.encode_source({"url": "a", "size": 0}), b"\x0a\x01a\x20\x00")
        self.assertEqual(proto.encode_concat([]), b"")
        self.assertEqual(proto.encode_concat([{"source": 0, "offset": 0, "length": 0}]),
                         b"\x0a\x00")
        neg = proto.encode_source({"url": "a", "modified_not_after": -1})
        self.assertEqual(neg, b"\x0a\x01a\x30" + b"\xff" * 9 + b"\x01")


class TestURI(unittest.TestCase):
    def test_valid_and_invalid(self):
        good = ["a.bin", "a%20b.bin", "%C3%A9.bin", "#x", "", "http://h:80/p?q#f", "//h/p",
                "file:///x", "../x", "./a:b", "http://[::1]:8/x", "http://[v1.x]/",
                "mailto:x@y", "s3://bucket/k", "?q", "a/b/c;p=1"]
        bad = ["a b", "é.bin", "a%2", "a%zz", ":x", "a:b/c" if False else "1a:b", "http://h:x/",
               "http://[::1/", "a[b]", "http://a@b@c/", "x\\y", "http://[fe80::1%25e]/"]
        for g in good:
            self.assertTrue(uri.is_uri_reference(g), g)
        for b in bad:
            self.assertFalse(uri.is_uri_reference(b), b)

    def test_rfc3986_examples(self):
        base = uri.parse("http://a/b/c/d;p?q")
        ex = {"g:h": "g:h", "g": "http://a/b/c/g", "./g": "http://a/b/c/g", "g/": "http://a/b/c/g/",
              "/g": "http://a/g", "//g": "http://g", "?y": "http://a/b/c/d;p?y",
              "g?y": "http://a/b/c/g?y", "#s": "http://a/b/c/d;p?q#s", "g#s": "http://a/b/c/g#s",
              ";x": "http://a/b/c/;x", "": "http://a/b/c/d;p?q", ".": "http://a/b/c/",
              "./": "http://a/b/c/", "..": "http://a/b/", "../g": "http://a/b/g",
              "../..": "http://a/", "../../g": "http://a/g", "../../../g": "http://a/g",
              "/./g": "http://a/g", "/../g": "http://a/g", "g.": "http://a/b/c/g.",
              "..g": "http://a/b/c/..g", "./../g": "http://a/b/g", "g/./h": "http://a/b/c/g/h",
              "g/../h": "http://a/b/c/h", "g;x=1/../y": "http://a/b/c/y", "http:g": "http:g"}
        for r, want in ex.items():
            self.assertEqual(str(uri.resolve(base, uri.parse(r))), want, r)

    def test_base_uri(self):
        self.assertEqual(uri.base_uri_for_path("/data/my file.vzip"),
                         "file:///data/my%20file.vzip")
        self.assertEqual(uri.base_uri_for_path("/a//b/./c/../d"), "file:///a/b/d")
        self.assertEqual(uri.base_uri_for_path("/é?#%"), "file:///%C3%A9%3F%23%25")
        cwd = os.getcwd()
        self.assertEqual(uri.base_uri_for_path("x.vzip"),
                         uri.base_uri_for_path(cwd + "/x.vzip"))

    def test_file_mapping(self):
        ok = {"file:///a/b": b"/a/b", "file:/a/%20b": b"/a/ b", "file://localhost/x": b"/x",
              "FILE://LocalHost/x#frag": b"/x"}
        for u, p in ok.items():
            self.assertEqual(uri.file_uri_to_path(uri.parse(u)), p, u)
        bad = ["file://host/x", "file:x", "file:///x?", "file:///a%2Fb", "file:///a%00",
               "file:///a/%2E%2E/b", "file:///%2e"]
        for u in bad:
            with self.assertRaises(uri.URIError, msg=u):
                uri.file_uri_to_path(uri.parse(u))


class TestHttpDate(unittest.TestCase):
    def test_roundtrip_and_formats(self):
        self.assertEqual(imf_fixdate(784111777), "Sun, 06 Nov 1994 08:49:37 GMT")
        self.assertEqual(parse_http_date("Sun, 06 Nov 1994 08:49:37 GMT"), 784111777)
        self.assertEqual(parse_http_date("Sunday, 06-Nov-94 08:49:37 GMT", 2026), 784111777)
        self.assertEqual(parse_http_date("Sun Nov  6 08:49:37 1994"), 784111777)
        self.assertEqual(imf_fixdate(-62135596800), "Mon, 01 Jan 0001 00:00:00 GMT")
        self.assertIsNone(imf_fixdate(-62135596801))
        self.assertIsNone(imf_fixdate(253402300800))
        self.assertIsNone(parse_http_date("yesterday"))
        self.assertIsNone(parse_http_date("Sun, 31 Feb 1994 08:49:37 GMT"))


if __name__ == "__main__":
    unittest.main()
