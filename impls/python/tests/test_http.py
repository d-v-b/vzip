"""HTTP source resolution (spec §6.1, §6.2) against a local server."""

import json
import os
import subprocess
import tempfile
import unittest

from helpers import ROOT, ObjectServer
from vzip_impl import proto
from vzip_impl.errors import ResolutionError
from vzip_impl.reader import Archive, Request
from vzip_impl.writer import WEntry, build_archive

R = proto.Range
S = proto.Source
DATA = bytes(range(100))
ETAG = '"v1"'
MTIME = 1_700_000_000


class HTTPBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ObjectServer().__enter__()
        cls.srv.objects["obj"] = (DATA, ETAG, MTIME)
        cls.srv.objects["noetag"] = (DATA, None, None)
        cls.srv.objects["dir/x y.bin"] = (b"spacey", None, None)

    @classmethod
    def tearDownClass(cls):
        cls.srv.__exit__()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.srv.log.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def arch(self, src: S, ranges, base_uri=None):
        data = build_archive([WEntry("r", ranges=ranges)], [src], None)
        path = os.path.join(self.tmp.name, "a.vzip")
        with open(path, "wb") as f:
            f.write(data)
        ar = Archive(path, base_uri=base_uri)
        self.addCleanup(ar.close)
        return ar

    def get(self, src, ranges, req=Request()):
        return self.arch(src, ranges).get("r", req)


class TestHTTPSuccess(HTTPBase):
    def test_reads(self):
        u = self.srv.url
        cases = [
            (S("url", u("plain/obj")), [R(0, 10, 5)], DATA[10:15]),
            (S("url", u("plain/obj"), size=100, etag=ETAG, modified_not_after=MTIME), [R(0, 0, 100)], DATA),
            (S("url", u("plain/obj"), modified_not_after=MTIME + 1), [R(0, 99, 1)], DATA[99:]),
            (S("url", u("full/obj"), size=100), [R(0, 3, 4)], DATA[3:7]),
            (S("url", u("star/obj")), [R(0, 3, 4)], DATA[3:7]),
            (S("url", u("redir3/plain/obj"), size=100), [R(0, 1, 2)], DATA[1:3]),
            (S("url", u("plain/dir/x%20y.bin")), [R(0, 0, 6)], b"spacey"),
            (S("url", u("plain/obj")), [R(0, 0, 3), R(data=b"-"), R(0, 50, 2)], DATA[0:3] + b"-" + DATA[50:52]),
        ]
        for src, ranges, want in cases:
            with self.subTest(src=src):
                self.assertEqual(self.get(src, ranges), want)

    def test_request_shape(self):
        self.get(S("url", self.srv.url("plain/obj"), etag=ETAG, modified_not_after=MTIME), [R(0, 10, 5)])
        self.assertEqual(len(self.srv.log), 1)
        method, path, hdrs = self.srv.log[0]
        self.assertEqual((method, path), ("GET", "/plain/obj"))
        self.assertEqual(hdrs["Range"], "bytes=10-14")
        self.assertEqual(hdrs["Accept-Encoding"], "identity")
        self.assertEqual(hdrs["If-Match"], ETAG)
        self.assertEqual(hdrs["If-Unmodified-Since"], "Tue, 14 Nov 2023 22:13:20 GMT")

    def test_only_window_requested(self):
        ar = self.arch(S("url", self.srv.url("plain/obj")), [R(0, 0, 10), R(0, 20, 10)])
        self.assertEqual(ar.get("r", Request("range", 12, 15)), DATA[22:25])
        self.assertEqual([h["Range"] for _, _, h in self.srv.log], ["bytes=22-24"])
        self.srv.log.clear()
        self.assertEqual(ar.get("r", Request("suffix", 0)), b"")
        self.assertEqual(self.srv.log, [])

    def test_relative_url_against_http_base(self):
        ar = self.arch(S("url", "obj"), [R(0, 0, 2)], base_uri=self.srv.url("plain/archive.vzip"))
        self.assertEqual(ar.get("r"), DATA[:2])

    def test_cli(self):
        d = self.tmp.name
        desc = {"sources": [{"url": self.srv.url("plain/obj"), "size": 100, "etag": ETAG}],
                "entries": [{"key": "a", "ranges": [{"source": 0, "offset": 5, "length": 2}]},
                            {"key": "b", "ranges": [{"source": 0, "offset": 99, "length": 2}]}]}
        dp, out, qp = (os.path.join(d, x) for x in ("d.json", "o.vzip", "q.json"))
        with open(dp, "w") as f:
            json.dump(desc, f)
        with open(qp, "w") as f:
            json.dump([{"op": "get", "key": "a"}, {"op": "get", "key": "b"}], f)
        cli = os.path.join(ROOT, "vzip")
        self.assertEqual(subprocess.run([cli, "write", dp, out]).returncode, 0)
        r = subprocess.run([cli, "read", out, qp], capture_output=True)
        res = json.loads(r.stdout)["results"]
        self.assertEqual(res[0], {"ok": True, "value": DATA[5:7].hex()})
        self.assertEqual(res[1]["class"], "resolution")


class TestHTTPErrors(HTTPBase):
    def bad(self, src, ranges=(R(0, 0, 4),)):
        with self.assertRaises(ResolutionError):
            self.get(src, list(ranges))

    def test_404(self):
        self.bad(S("url", self.srv.url("plain/none")))

    def test_500(self):
        self.bad(S("url", self.srv.url("status500/obj")))

    def test_412_etag(self):
        self.bad(S("url", self.srv.url("plain/obj"), etag='"v2"'))

    def test_412_etag_no_etag(self):
        self.bad(S("url", self.srv.url("plain/noetag"), etag='"v1"'))

    def test_412_modified(self):
        self.bad(S("url", self.srv.url("plain/obj"), modified_not_after=MTIME - 1))

    def test_416(self):
        self.bad(S("url", self.srv.url("plain/obj")), [R(0, 200, 1)])

    def test_size_pin_206(self):
        self.bad(S("url", self.srv.url("plain/obj"), size=99))

    def test_size_pin_200(self):
        self.bad(S("url", self.srv.url("full/obj"), size=101))

    def test_size_pin_star(self):
        self.bad(S("url", self.srv.url("star/obj"), size=100))

    def test_short_200(self):
        self.bad(S("url", self.srv.url("full/obj")), [R(0, 98, 4)])

    def test_clamped_range(self):
        self.bad(S("url", self.srv.url("clamp/obj")), [R(0, 98, 4)])

    def test_content_encoding(self):
        self.bad(S("url", self.srv.url("gzip/obj")))

    def test_too_many_redirects(self):
        self.bad(S("url", self.srv.url("redir6/plain/obj")))

    def test_connection_refused(self):
        self.bad(S("url", "http://127.0.0.1:1/x"))

    def test_mtime_out_of_range(self):
        self.bad(S("url", self.srv.url("plain/obj"), modified_not_after=-62135596801))

    def test_mtime_year_10000(self):
        self.bad(S("url", self.srv.url("plain/obj"), modified_not_after=253402300800))

    def test_no_head_requests(self):
        self.bad(S("url", self.srv.url("plain/obj"), size=1))
        self.assertTrue(all(m == "GET" for m, _, _ in self.srv.log))


if __name__ == "__main__":
    unittest.main()
