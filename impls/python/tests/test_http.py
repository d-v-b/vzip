"""HTTP source resolution (spec §6.1, §6.2) against a local test server."""

import gzip
import http.server
import re
import threading
import unittest

from helpers import TmpDir, build_raw, read_queries, ref_extra

from vzip_impl import Archive, ResolutionError
from vzip_impl.fetch import imf_fixdate, parse_http_date
from vzip_impl.proto import encode_concat, encode_range, encode_source_table

OBJ = bytes(range(256)) * 4  # 1024 bytes
ETAG = '"v1"'
LAST_MOD = 1_700_000_000


class Handler(http.server.BaseHTTPRequestHandler):
    log = []

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        Handler.log.append(("HEAD", self.path, dict(self.headers)))
        self.send_response(500)
        self.end_headers()

    def do_GET(self):
        Handler.log.append(("GET", self.path, dict(self.headers)))
        path = self.path
        flags = set()
        while True:
            m = re.match(r"/(ignore-range|gzip|star|wrong-range|no-etag|weak-etag|no-lm|"
                         r"bad-lm|ignore-cond|teapot|no-cr|rfc850)(/.*)", path)
            if not m:
                break
            flags.add(m.group(1))
            path = m.group(2)
        m = re.match(r"/redir/(\d+)(/.*)", path)
        if m:
            n = int(m.group(1))
            rest = m.group(2)
            loc = (f"/redir/{n - 1}{rest}" if n > 1 else rest)
            if n == 1 and "/to-ftp" in rest:
                loc = "ftp://example.com/x"
            if n % 2 == 0:  # alternate relative and absolute Locations
                loc = f"http://127.0.0.1:{self.server.server_port}{loc}"
            self.send_response(307 if n % 3 else 301)
            self.send_header("Location", loc)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not path.startswith("/obj"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if "teapot" in flags:
            self.send_response(418)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if "ignore-cond" not in flags:
            im = self.headers.get("If-Match")
            if im is not None and im != ETAG:
                return self._empty(412)
            ius = self.headers.get("If-Unmodified-Since")
            if ius is not None:
                t = parse_http_date(ius)
                if t is not None and LAST_MOD > t:
                    return self._empty(412)
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d+)-(\d+)$", rng or "")
        hdrs = {}
        if "no-etag" not in flags:
            hdrs["ETag"] = 'W/"v1"' if "weak-etag" in flags else ETAG
        if "no-lm" not in flags:
            if "bad-lm" in flags:
                hdrs["Last-Modified"] = "garbage"
            elif "rfc850" in flags:
                hdrs["Last-Modified"] = "Tuesday, 14-Nov-23 22:13:20 GMT"
            else:
                hdrs["Last-Modified"] = imf_fixdate(LAST_MOD)
        if m and "ignore-range" not in flags:
            a, b = int(m.group(1)), int(m.group(2))
            if a >= len(OBJ):
                hdrs["Content-Range"] = f"bytes */{len(OBJ)}"
                return self._empty(416, hdrs)
            b = min(b, len(OBJ) - 1)
            if "wrong-range" in flags:
                a += 1
            body = OBJ[a:b + 1]
            total = "*" if "star" in flags else str(len(OBJ))
            if "no-cr" not in flags:
                hdrs["Content-Range"] = f"bytes {a}-{b}/{total}"
            status = 206
        else:
            body = OBJ
            status = 200
        if "gzip" in flags:
            body = gzip.compress(body)
            hdrs["Content-Encoding"] = "gzip"
        self.send_response(status)
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _empty(self, status, hdrs=None):
        self.send_response(status)
        for k, v in (hdrs or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()


class TestHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_port
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.td = TmpDir()
        Handler.log.clear()
        self.archives = []

    def tearDown(self):
        for a in self.archives:
            a.close()
        self.td.cleanup()

    def arch(self, url, off=10, ln=20, base_uri=None, **pins):
        src = {"url": url}
        src.update(pins)
        pl = encode_concat([{"data": b"<"}, {"source": 0, "offset": off, "length": ln}])
        data = build_raw([{"name": b"r", "body": pl, "extra": ref_extra(pl, False)}],
                         sources_raw=encode_source_table([src]))
        path = self.td.write(f"a{len(self.archives)}.vzip", data)
        a = Archive(path, base_uri=base_uri)
        self.archives.append(a)
        return a

    def test_successful_reads(self):
        want = b"<" + OBJ[10:30]
        ok = [
            (self.base + "/obj", {}),
            (self.base + "/obj", {"size": 1024, "etag": ETAG,
                                  "modified_not_after": LAST_MOD}),
            (self.base + "/ignore-range/obj", {"size": 1024, "etag": ETAG}),
            (self.base + "/redir/5/obj", {"size": 1024}),
            (self.base + "/redir/2/ignore-range/obj", {}),
            (self.base + "/star/obj", {"etag": ETAG}),
            (self.base + "/rfc850/obj", {"modified_not_after": LAST_MOD}),
            ("HTTP://127.0.0.1:%d/obj#frag" % self.port, {}),
        ]
        for url, pins in ok:
            with self.subTest(url, **{k: str(v) for k, v in pins.items()}):
                Handler.log.clear()
                self.assertEqual(self.arch(url, **pins).get("r"), want)
                for method, _path, hdrs in Handler.log:
                    self.assertEqual(method, "GET")
                    self.assertEqual(hdrs.get("Range"), "bytes=10-29")
                    self.assertEqual(hdrs.get("Accept-Encoding"), "identity")
                    if "etag" in pins:
                        self.assertEqual(hdrs.get("If-Match"), ETAG)
                    if "modified_not_after" in pins:
                        self.assertEqual(hdrs.get("If-Unmodified-Since"),
                                         "Tue, 14 Nov 2023 22:13:20 GMT")
        # relative URL against an http base URI
        a = self.arch("obj", base_uri=self.base + "/dir/../x.vzip")
        self.assertEqual(a.get("r"), want)
        # window inside the reference maps to an inner byte range
        Handler.log.clear()
        self.assertEqual(self.arch(self.base + "/obj").get("r", ("range", 3, 5)),
                         OBJ[12:14])
        self.assertEqual(Handler.log[0][2]["Range"], "bytes=12-13")
        # no request at all when only the literal is requested
        Handler.log.clear()
        self.assertEqual(self.arch(self.base + "/teapot/obj").get("r", ("range", 0, 1)), b"<")
        self.assertEqual(Handler.log, [])

    def test_each_http_resolution_error(self):
        bad = {
            "404": ("/nope", {}),
            "other_status": ("/teapot/obj", {}),
            "gzip_encoding": ("/gzip/obj", {}),
            "wrong_range": ("/wrong-range/obj", {}),
            "no_content_range": ("/no-cr/obj", {}),
            "416": ("/obj", {"off": 5000}),
            "short_object_206": ("/obj", {"off": 1000, "ln": 100}),
            "short_object_200": ("/ignore-range/obj", {"off": 1000, "ln": 100}),
            "size_pin_206": ("/obj", {"size": 1023}),
            "size_pin_200": ("/ignore-range/obj", {"size": 1025}),
            "size_pin_star": ("/star/obj", {"size": 1024}),
            "etag_412": ("/obj", {"etag": '"other"'}),
            "etag_ignored_by_server": ("/ignore-cond/obj", {"etag": '"other"'}),
            "etag_missing": ("/no-etag/obj", {"etag": ETAG}),
            "etag_weak_response": ("/ignore-cond/weak-etag/obj", {"etag": ETAG}),
            "mtime_412": ("/obj", {"modified_not_after": LAST_MOD - 1}),
            "mtime_ignored_by_server": ("/ignore-cond/obj", {"modified_not_after": LAST_MOD - 1}),
            "mtime_no_header": ("/no-lm/obj", {"modified_not_after": LAST_MOD}),
            "mtime_bad_header": ("/bad-lm/obj", {"modified_not_after": LAST_MOD}),
            "mtime_unsendable": ("/obj", {"modified_not_after": -62135596801}),
            "six_redirects": ("/redir/6/obj", {}),
            "redirect_to_ftp": ("/redir/1/to-ftp", {}),
        }
        for name, (path, kw) in bad.items():
            with self.subTest(name):
                off = kw.pop("off", 10)
                ln = kw.pop("ln", 20)
                a = self.arch(self.base + path, off=off, ln=ln, **kw)
                with self.assertRaises(ResolutionError):
                    a.get("r")
        with self.assertRaises(ResolutionError):  # connection refused
            srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
            port = srv.server_port
            srv.server_close()
            self.arch(f"http://127.0.0.1:{port}/obj").get("r")

    def test_unreachable_url_does_not_block_open(self):
        path = self.td.write("u.vzip", build_raw(
            [{"name": b"k", "body": b"v"}],
            sources_raw=encode_source_table([{"url": "http://127.0.0.1:1/x"}])))
        res = read_queries(self.td, path, [{"op": "get", "key": "k"}])
        self.assertEqual(res["results"][0]["value"], "76")
        self.assertEqual(Handler.log, [])


if __name__ == "__main__":
    unittest.main()
