import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from vzip.pb import Source
from vzip.store import http_range

BODY = bytes(range(256))


@pytest.fixture
def server():
    """A range server that records each request's client port, and closes the
    connection after a response when `close_after` says so."""
    state = {"ports": [], "close_after": False}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            state["ports"].append(self.client_address[1])
            a, b = (int(x) for x in self.headers["Range"][6:].split("-"))
            data = BODY[a:b + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(BODY)}")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            if state["close_after"]:  # without saying so: the client finds out on reuse
                self.close_connection = True

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/blob.bin", state
    srv.shutdown()


def test_range_reads_reuse_connections(server):
    url, state = server
    for close_after in (False, True):
        state["ports"].clear()
        state["close_after"] = close_after
        for i in range(20):
            data, size = http_range(url, i, i + 10, Source(url=url))
            assert (data, size) == (BODY[i:i + 10], len(BODY))
        # kept alive: one connection; closed by the server: a new one each time, still correct
        assert len(set(state["ports"])) == (20 if close_after else 1)
