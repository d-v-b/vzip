"""The virtualizers' http range reader (vzip.virtualize.common.http_reader): its block
cache, multi-block reads in one request, prefetched ranges, and use from many threads."""

import random
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from vzip.virtualize import common
from vzip.virtualize.common import Rejected, http_reader

BODY = bytes(random.Random(0).randrange(256) for _ in range(1000))
BLOCK = 16


@pytest.fixture
def server():
    """A range server over BODY that records each GET's range, and serves `short` bytes
    fewer than asked when `short` is set."""
    state = {"ranges": [], "short": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()

        def do_GET(self):
            a, b = (int(x) for x in self.headers["Range"][6:].split("-"))
            with lock:
                state["ranges"].append((a, b + 1))
            data = BODY[a : b + 1 - state["short"]]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(BODY)}")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/blob.bin", state
    srv.shutdown()


def test_http_reader(server):
    url, state = server
    read, size = http_reader(url, block=BLOCK)
    assert size == len(BODY)
    # (offset, length, the ranges requested): a span of uncached blocks is one request;
    # cached blocks are not fetched again, and the uncached runs around them are one
    # request each.
    cases = [
        (0, 0, []),
        (3, 5, [(0, 16)]),
        (3, 5, []),
        (40, 40, [(32, 80)]),
        (20, 80, [(16, 32), (80, 112)]),
        (990, 10, [(976, 1000)]),
        (0, 1000, [(112, 976)]),
    ]
    for offset, length, ranges in cases:
        state["ranges"].clear()
        assert read(offset, length) == BODY[offset : offset + length], (offset, length)
        assert state["ranges"] == ranges, (offset, length)
    # prefetched ranges are read in one request each, and reads inside one are not requested again
    read, _ = http_reader(url, block=BLOCK)
    state["ranges"].clear()
    read.prefetch([(100, 50), (500, 4), (995, 100)])
    assert sorted(state["ranges"]) == [(100, 150), (500, 504), (995, 1000)]
    state["ranges"].clear()
    assert [read(110, 40), read(500, 4), read(996, 4)] == [BODY[110:150], BODY[500:504], BODY[996:1000]]
    assert state["ranges"] == []
    assert read.requests == [3, 59]  # requests, bytes (the HEAD is not counted)


def test_http_reader_is_shared_by_threads(server, monkeypatch):
    monkeypatch.setattr(common, "MAX_BLOCKS", 4)  # constant eviction
    url, _ = server
    read, _ = http_reader(url, block=BLOCK)
    rng = random.Random(1)
    spans = [(o, rng.randrange(0, 80)) for o in (rng.randrange(0, 900) for _ in range(2000))]
    with ThreadPoolExecutor(16) as pool:
        got = list(pool.map(lambda s: read(*s), spans))
    assert got == [BODY[o : o + n] for o, n in spans]


def test_http_reader_rejects_a_read_outside_the_file(server):
    read, _ = http_reader(server[0], block=BLOCK)
    with pytest.raises(Rejected, match="outside the 1000-byte file"):
        read(990, 11)


def test_http_reader_fails_on_a_short_read(server):
    url, state = server
    read, _ = http_reader(url, block=BLOCK)
    state["short"] = 1
    with pytest.raises(OSError, match="short read at 0"):
        read(0, 40)
