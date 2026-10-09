"""The ND2 IR (round 2): the Rust parser and its schema, the image projection
against today's profile, the compact mirror, and the read planner."""

import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import FIXTURES, same_hierarchy
from vzip.ir import virtualize as via_ir
from vzip.ir.cmirror import rebuild_from_archive
from vzip.errors import ResolutionError
from vzip.ir import parse
from vzip.ir.planner import FileTransport, HttpTransport
from vzip.policy import Policy
from vzip.virtualize import Rejected
from vzip_reference import virtualize  # the frozen reference (conftest)

LOOPBACK = Policy(allow_private_hosts=True)  # the test servers are on 127.0.0.1
ND2S = sorted((FIXTURES / "nd2").glob("*.nd2"))


@pytest.mark.parametrize("path", ND2S, ids=lambda p: p.name)
def test_projects_as_today_and_rebuilds(path, tmp_path):
    data = path.read_bytes()
    url = f"https://data.test/{path.name}"
    try:
        _, today = virtualize(str(path), url)
    except Rejected:
        with pytest.raises(Rejected):
            via_ir(str(path), url)
        return
    fmt, out, ir = via_ir(str(path), url)
    assert fmt == "nd2"
    ir.check()
    same_hierarchy(today, out)
    out.write(str(tmp_path / "a.vzip"))
    buf = io.BytesIO()
    rebuild_from_archive(str(tmp_path / "a.vzip"), lambda o, n: data[o:o + n], buf.write)
    assert buf.getvalue() == data


def test_frames_fold_into_one_run():
    _, _, ir = via_ir(str(FIXTURES / "nd2" / "nd2_tz_uint16.nd2"), "u", mirror=False)
    frames = ir.children(ir.child(0, "frames"))
    assert len(frames) == 1 and ir.run(frames[0])[0] == 12
    assert [ir.name(c) for c in ir.children(frames[0])] == ["header", "name", "timestamp", "pixels"]


class _Ranges(BaseHTTPRequestHandler):
    """Serves `data` by single ranges; answers 503 with Retry-After first, `fail` times."""

    data = bytes(range(256)) * 4096
    fail = 0
    multipart = True
    seen: list = []

    def do_GET(self):
        cls = type(self)
        if cls.fail:
            cls.fail -= 1
            self.send_response(503)
            self.send_header("Retry-After", "0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        spans = [tuple(map(int, r.split("-"))) for r in self.headers["Range"].removeprefix("bytes=").split(",")]
        cls.seen.append(spans)
        if len(spans) > 1:
            if cls.multipart == "crash":  # a server that fails on several ranges
                raise ConnectionResetError
            if not cls.multipart:  # a server that ignores several ranges: the whole object
                self.send_response(200)
                self.send_header("Content-Length", str(len(cls.data)))
                self.end_headers()
                self.wfile.write(cls.data)
                return
            body = b"".join(b"\r\n--XX\r\nContent-Range: bytes %d-%d/%d\r\n\r\n" % (a, b, len(cls.data))
                            + cls.data[a:b + 1] for a, b in spans) + b"\r\n--XX--\r\n"
            self.send_response(206)
            self.send_header("Content-Type", "multipart/byteranges; boundary=XX")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        a, b = spans[0][0], min(spans[0][1] + 1, len(cls.data))
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {a}-{b - 1}/{len(cls.data)}")
        self.send_header("Content-Length", str(b - a))
        self.end_headers()
        self.wfile.write(cls.data[a:b])

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Ranges)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Ranges.seen, _Ranges.fail, _Ranges.multipart = [], 0, True
    yield f"http://127.0.0.1:{srv.server_port}/f"
    srv.shutdown()


def test_transport_retries_honoring_retry_after(server):
    _Ranges.fail = 2
    t = HttpTransport(server, LOOPBACK, base_delay=0.01)
    assert t.size == len(_Ranges.data) and t.head[1] == _Ranges.data[:1 << 16]
    assert t.fetch([(10, 15)])[0] == [_Ranges.data[10:15]]
    assert t.retries == 2


def test_transport_packs_ranges_where_the_server_answers_them(server):
    spans = [(k * 2500, k * 2500 + 7) for k in range(40)]
    for multipart in (True, False, "crash"):
        _Ranges.multipart = multipart
        got, _ = HttpTransport(server, LOOPBACK).fetch(spans)
        # a server that answers several ranges with the whole object (its body unread), or
        # fails on them: None, and the run plans single ranges
        assert got == ([_Ranges.data[a:b] for a, b in spans] if multipart is True else None)


def test_transport_does_not_retry_a_missing_object():
    class Missing(BaseHTTPRequestHandler):
        seen = 0

        def do_GET(self):
            Missing.seen += 1
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Missing)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(OSError, match="HTTP 404"):
            HttpTransport(f"http://127.0.0.1:{srv.server_port}/f", LOOPBACK, base_delay=0.01)
        assert Missing.seen == 1
    finally:
        srv.shutdown()


def test_transport_refuses_a_private_host_by_default(server):
    with pytest.raises(ResolutionError, match="allow_private_hosts"):
        HttpTransport(server)


def test_drive_reads_through_the_planner(server, tmp_path):
    """A run over the server: each batch's bytes as asked, the planner's counts."""
    nd2 = (FIXTURES / "nd2" / "nd2_tz_uint16.nd2").read_bytes()
    _Ranges.data = nd2
    try:
        t = HttpTransport(server, LOOPBACK)
        ir, facts, stats = parse("nd2", t, 4)
        local = parse("nd2", FileTransport(str(FIXTURES / "nd2" / "nd2_tz_uint16.nd2")), 4)
        assert ir.digest() == local[0].digest() and facts == local[1]
        # the file is smaller than the request that opened it: every range is served from it
        assert stats["requests"] == 0 and stats["served_ahead"] == stats["ranges"]
    finally:
        _Ranges.data = bytes(range(256)) * 4096
