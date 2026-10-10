import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from vzip_impl import reader, writer  # noqa: E402
from vzip_impl.errors import VzError  # noqa: E402

OBJ = b"abcdefghijklmnopqrstuvwxyz"
LM = "Sun, 06 Nov 1994 08:49:37 GMT"
LM_T = 784111777
ETAG = '"v1"'
LOG = []


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        LOG.append(("HEAD", self.path, dict(self.headers)))
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send(self, status, body=b"", headers=(), chunked=False):
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(0, len(body), 3):
                c = body[i:i + 3]
                self.wfile.write(b"%x\r\n%s\r\n" % (len(c), c))
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_GET(self):
        LOG.append(("GET", self.path, dict(self.headers)))
        p = self.path.split("?")[0]
        rng = self.headers.get("Range", "")
        a, z = (int(x) for x in rng[len("bytes="):].split("-"))
        std = [("ETag", ETAG), ("Last-Modified", LM)]
        if p.startswith("/redir/"):
            n = int(p.split("/")[2])
            loc = "/redir/%d" % (n - 1) if n > 1 else "/obj"
            return self.send(302, b"", [("Location", loc)])
        if p == "/redir-noloc":
            return self.send(301)
        if p == "/redir-file":
            return self.send(307, b"", [("Location", "file:///etc/passwd")])
        if p == "/redir-rel":
            return self.send(308, b"", [("Location", "obj?x=1")])
        if p == "/norange":
            return self.send(200, OBJ, std)
        if p == "/status500":
            return self.send(500)
        im = self.headers.get("If-Match")
        ius = self.headers.get("If-Unmodified-Since")
        if p == "/obj" or p == "/chunked":
            if im is not None and im != ETAG:
                return self.send(412)
            if ius is not None:
                from vzip_impl.httpfetch import parse_imf_fixdate
                if parse_imf_fixdate(ius) < LM_T:
                    return self.send(412)
            if a >= len(OBJ):
                return self.send(416, b"", [("Content-Range", "bytes */%d" % len(OBJ))])
            z = min(z, len(OBJ) - 1)
            return self.send(206, OBJ[a:z + 1], std + [("Content-Range", "bytes %d-%d/%d" % (a, z, len(OBJ)))],
                             chunked=(p == "/chunked"))
        cr = [("Content-Range", "bytes %d-%d/%d" % (a, z, len(OBJ)))]
        body = OBJ[a:z + 1]
        if p == "/star":
            return self.send(206, body, std + [("Content-Range", "bytes %d-%d/*" % (a, z))])
        if p == "/gzip":
            return self.send(206, body, std + cr + [("Content-Encoding", "gzip")])
        if p == "/identity":
            return self.send(206, body, std + cr + [("Content-Encoding", " Identity ")])
        if p == "/identity2":
            return self.send(206, body, std + cr + [("Content-Encoding", "identity, identity")])
        if p == "/multi":
            return self.send(206, body, std + cr + [("Content-Type", "multipart/byteranges; boundary=x")])
        if p == "/wrongrange":
            return self.send(206, OBJ[a + 1:z + 2], std + [("Content-Range", "bytes %d-%d/%d" % (a + 1, z + 1, len(OBJ)))])
        if p == "/norange206":
            return self.send(206, body, std)
        if p == "/badlen":
            return self.send(206, body + b"x", std + cr)
        if p == "/weak":
            return self.send(206, body, [("ETag", 'W/"v1"'), ("Last-Modified", LM)] + cr)
        if p == "/lie":  # ignores If-Match, reports a different etag and newer date
            return self.send(206, body, [("ETag", '"v2"'), ("Last-Modified", "Mon, 07 Nov 1994 08:49:37 GMT")] + cr)
        if p == "/rfc850":
            return self.send(206, body, [("ETag", ETAG), ("Last-Modified", "Sunday, 06-Nov-94 08:49:37 GMT")] + cr)
        if p == "/wrongday":
            return self.send(206, body, [("ETag", ETAG), ("Last-Modified", "Mon, 06 Nov 1994 08:49:37 GMT")] + cr)
        if p == "/nometa":
            return self.send(206, body, cr)
        return self.send(404)


class TestHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.srv.handle_error = lambda *a: None
        cls.port = cls.srv.server_address[1]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()
        cls.dir = tempfile.mkdtemp(prefix="vzh")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def url(self, p):
        return "http://127.0.0.1:%d%s" % (self.port, p)

    def archive(self, sources, base_uri=None):
        entries = [writer.WEntry("k%d" % i, ranges=[writer.WRange(i, 2, 5)]) for i in range(len(sources))]
        data = writer.build(entries, sources)
        p = os.path.join(self.dir, "a%d.vzip" % id(sources))
        with open(p, "wb") as f:
            f.write(data)
        return reader.Archive(p, base_uri=base_uri)

    def one(self, path, **pins):
        return self.archive([writer.WSource("url", self.url(path), **pins)])

    def ok(self, path, **pins):
        self.assertEqual(self.one(path, **pins).get("k0"), b"cdefg")

    def fail(self, path, **pins):
        with self.assertRaises(VzError) as cm:
            self.one(path, **pins).get("k0")
        self.assertEqual(cm.exception.cls, "resolution", str(cm.exception))

    def test_successes(self):
        LOG.clear()
        self.ok("/obj")
        self.assertEqual(len(LOG), 1)
        method, _p, h = LOG[0]
        self.assertEqual(method, "GET")
        self.assertEqual(h["Range"], "bytes=2-6")
        self.assertEqual(h["Accept-Encoding"], "identity")
        self.assertNotIn("If-Match", h)
        LOG.clear()
        self.ok("/obj", size=26, etag=ETAG, modified_not_after=LM_T)
        self.assertEqual(LOG[0][2]["If-Match"], ETAG)
        self.assertEqual(LOG[0][2]["If-Unmodified-Since"], LM)
        self.ok("/chunked", size=26, etag=ETAG)
        self.ok("/norange", size=26, etag=ETAG, modified_not_after=LM_T)
        self.ok("/star")
        self.ok("/star", etag=ETAG)
        self.ok("/identity")
        LOG.clear()
        self.ok("/redir/5", size=26, etag=ETAG)
        self.assertEqual(len(LOG), 6)
        self.assertTrue(all(m == "GET" and h["If-Match"] == ETAG and h["Range"] == "bytes=2-6" for m, _, h in LOG))
        self.ok("/redir-rel")
        self.ok("/obj", modified_not_after=LM_T + 10)
        # relative URL against an http base URI
        ar = self.archive([writer.WSource("url", "obj")], base_uri=self.url("/dir/../archive.vzip"))
        self.assertEqual(ar.get("k0"), b"cdefg")
        # zero-length ranges need no request
        LOG.clear()
        ar = self.archive([writer.WSource("url", self.url("/status500"))])
        self.assertEqual(ar.get("k0", ("range", 0, 0)), b"")
        self.assertEqual(LOG, [])
        self.assertTrue(all(m != "HEAD" for m, _, _ in LOG))

    def test_size_pin_mismatch(self):
        self.fail("/obj", size=25)
        self.fail("/norange", size=27)

    def test_size_pin_unknown_total(self):
        self.fail("/star", size=26)

    def test_etag_pin_412(self):
        self.fail("/obj", etag='"v2"')

    def test_etag_pin_ignored_by_server(self):
        self.fail("/lie", etag=ETAG)

    def test_etag_weak_response(self):
        self.fail("/weak", etag=ETAG)

    def test_etag_missing(self):
        self.fail("/nometa", etag=ETAG)

    def test_mnf_412(self):
        self.fail("/obj", modified_not_after=LM_T - 1)

    def test_mnf_ignored_by_server(self):
        self.fail("/lie", modified_not_after=LM_T)

    def test_mnf_bad_dates(self):
        self.fail("/rfc850", modified_not_after=LM_T)
        self.fail("/wrongday", modified_not_after=LM_T)
        self.fail("/nometa", modified_not_after=LM_T)

    def test_mnf_unsendable(self):
        self.fail("/obj", modified_not_after=-62135596801)  # year 0
        self.fail("/obj", modified_not_after=253402300800)  # year 10000

    def test_content_encoding(self):
        self.fail("/gzip")
        self.fail("/identity2")

    def test_multipart(self):
        self.fail("/multi")

    def test_wrong_range(self):
        self.fail("/wrongrange")

    def test_206_without_content_range(self):
        self.fail("/norange206")

    def test_body_length(self):
        self.fail("/badlen")

    def test_416(self):
        ar = self.archive([writer.WSource("url", self.url("/obj"))])
        e = writer.WEntry("x", ranges=[writer.WRange(0, 30, 2)])
        data = writer.build([e], [writer.WSource("url", self.url("/obj"))])
        p = os.path.join(self.dir, "416.vzip")
        with open(p, "wb") as f:
            f.write(data)
        with self.assertRaises(VzError) as cm:
            reader.Archive(p).get("x")
        self.assertEqual(cm.exception.cls, "resolution")
        ar.close()

    def test_short_200(self):
        data = writer.build([writer.WEntry("x", ranges=[writer.WRange(0, 20, 10)])],
                            [writer.WSource("url", self.url("/norange"))])
        p = os.path.join(self.dir, "s200.vzip")
        with open(p, "wb") as f:
            f.write(data)
        with self.assertRaises(VzError) as cm:
            reader.Archive(p).get("x")
        self.assertEqual(cm.exception.cls, "resolution")

    def test_other_status(self):
        self.fail("/status500")
        self.fail("/nothere")

    def test_too_many_redirects(self):
        self.fail("/redir/6")

    def test_redirect_without_location(self):
        self.fail("/redir-noloc")

    def test_redirect_to_file(self):
        self.fail("/redir-file")

    def test_userinfo_and_empty_host(self):
        ar = self.archive([writer.WSource("url", "http://u@127.0.0.1:%d/obj" % self.port),
                           writer.WSource("url", "http:///obj")])
        for k in ("k0", "k1"):
            with self.assertRaises(VzError) as cm:
                ar.get(k)
            self.assertEqual(cm.exception.cls, "resolution")

    def test_cli(self):
        import json
        import subprocess
        d = os.path.join(self.dir, "cli")
        os.makedirs(d, exist_ok=True)
        desc = {"sources": [{"url": self.url("/obj"), "size": 26, "etag": ETAG, "modified_not_after": LM_T},
                            {"url": self.url("/obj"), "size": 1}],
                "entries": [{"key": "a", "ranges": [{"source": 0, "offset": 0, "length": 3}]},
                            {"key": "b", "ranges": [{"source": 1, "offset": 0, "length": 3}]}]}
        with open(os.path.join(d, "d.json"), "w") as f:
            json.dump(desc, f)
        with open(os.path.join(d, "q.json"), "w") as f:
            json.dump([{"op": "get", "key": "a"}, {"op": "get", "key": "b"},
                       {"op": "get", "key": "a", "range": {"suffix": 1}}], f)
        vz = os.path.join(os.path.dirname(HERE), "vzip")
        r = subprocess.run([vz, "write", "d.json", "o.vzip"], cwd=d, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([vz, "read", "o.vzip", "q.json"], cwd=d, capture_output=True)
        res = json.loads(r.stdout)["results"]
        self.assertEqual(res[0], {"ok": True, "value": "616263"})
        self.assertEqual(res[1]["class"], "resolution")
        self.assertEqual(res[2]["value"], "63")

    def test_connection_refused(self):
        ar = self.archive([writer.WSource("url", "http://127.0.0.1:1/obj")])
        with self.assertRaises(VzError) as cm:
            ar.get("k0")
        self.assertEqual(cm.exception.cls, "resolution")


if __name__ == "__main__":
    unittest.main()
