"""File inputs of the profiles that are not the IR's under the reader policy (SPEC.md
§8.7): the http reader (vzip.virtualize.common.http_reader) makes every request (the
size's, each range's, prefetch's) and follows every redirect under `Policy`, as a
store's reads do (tests/test_store_policy.py).
"""

import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import vzip.store
from vzip.errors import ResolutionError
from vzip.policy import Policy
from vzip.virtualize import virtualize
from vzip.virtualize.common import http_reader

ROOT = Path(__file__).parent.parent
BODY = (ROOT / "web" / "test" / "fixtures" / "nifti" / "nifti_n1_le_qform_identity.nii").read_bytes()


class ServerIsPublic(Policy):
    """`Policy()`, except that the test server's address (127.0.0.1) counts as a public one."""

    def check_address(self, url: str, address: str) -> None:
        if address != "127.0.0.1":
            super().check_address(url, address)


@pytest.fixture
def server():
    """A local range server, and an HTTP proxy, serving BODY at `/s/f.nii` and again
    at `/r/f.nii`. With `state["redirect"]`, a request under `/s/` other than the
    first `state["direct"]` is redirected to that URL. `state["paths"]` records each
    request's method and target, as sent (absolute through a proxy)."""
    state: dict = {"redirect": None, "direct": 0, "paths": []}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def respond(self, send_body: bool):
            with lock:
                state["paths"].append((self.command, self.path))
                direct = sum(urllib.parse.urlsplit(p).path.startswith("/s/") for _, p in state["paths"])
            path = urllib.parse.urlsplit(self.path).path
            headers = {}
            if path.startswith("/s/") and state["redirect"] is not None and direct > state["direct"]:
                status, body, headers = 307, b"", {"Location": state["redirect"]}
            elif "Range" not in self.headers:
                status, body = 200, BODY
            else:
                a, b = (int(x) for x in self.headers["Range"][6:].split("-"))
                b = min(b, len(BODY) - 1)
                status, body = 206, BODY[a:b + 1]
                headers = {"Content-Range": f"bytes {a}-{b}/{len(BODY)}"}
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def do_GET(self):
            self.respond(True)

        def do_HEAD(self):
            self.respond(False)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], state
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def names(monkeypatch):
    """Host names the reader resolves (name → address); others are refused here."""
    table: dict[str, str] = {}

    def fake(host, port, *args, **kwargs):
        if host == "127.0.0.1":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port))]

    monkeypatch.setattr(vzip.store, "_getaddrinfo", fake)
    return table


@pytest.fixture
def env(monkeypatch):
    """The environment, without proxy settings."""
    for name in ["http_proxy", "https_proxy", "all_proxy", "no_proxy"]:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    return monkeypatch


def test_reads_a_file_the_policy_allows(server, names, env):
    """Each way a policy admits a file's requests gives the same output, and the same
    bytes from the reader's block reads, uncached reads and prefetch threads: a name
    that resolves to an allowed address, its reads redirected to another such name; a
    private host with allow_private_hosts; and, through a proxy, a name that resolves
    to a public address with allow_unchecked_proxy, or to a private one with
    allow_private_hosts."""
    port, state = server
    names["public.test"] = "127.0.0.1"  # ServerIsPublic reaches it
    names["other.test"] = "127.0.0.1"
    names["localhost"] = "127.0.0.1"
    names["proxied.test"] = "93.184.215.14"
    names["internal.test"] = "10.0.0.1"
    cases = [
        (ServerIsPublic(), f"http://public.test:{port}/s/f.nii", f"http://other.test:{port}/r/f.nii", None),
        (Policy(allow_private_hosts=True), f"http://127.0.0.1:{port}/s/f.nii", None, None),
        (Policy(allow_private_hosts=True), f"http://localhost:{port}/s/f.nii", None, None),
        (Policy(allow_unchecked_proxy=True), "http://proxied.test/s/f.nii", None, f"http://127.0.0.1:{port}"),
        (Policy(allow_private_hosts=True), "http://internal.test/s/f.nii", None, f"http://127.0.0.1:{port}"),
    ]
    spans = [(0, 10), (100, 300), (len(BODY) - 7, 7)]
    outputs = set()
    for policy, url, redirect, proxy in cases:
        state["redirect"] = redirect
        if proxy is None:
            env.delenv("http_proxy", raising=False)
        else:
            env.setenv("http_proxy", proxy)
        # virtualize opens the source (its first request) and passes the policy to the reader
        del state["paths"][:]
        state["direct"] = 1
        fmt, out = virtualize(url, policy=policy)
        assert fmt == "nifti"
        outputs.add(repr((sorted(out.bytes_entries.items()), sorted(out.refs.items()))).replace(url, "<url>"))
        # the reader itself: its size, block reads, uncached reads and prefetch
        state["direct"] = 0
        read, size = http_reader(url, block=64, policy=policy)
        assert size == len(BODY)
        assert [read(o, n) for o, n in spans] == [BODY[o:o + n] for o, n in spans]
        assert [read.uncached(o, n) for o, n in spans] == [BODY[o:o + n] for o, n in spans]
        read, _ = http_reader(url, block=64, policy=policy)
        read.prefetch([(o, 3) for o in range(0, len(BODY), 5)])
        assert [read(o, 3) for o in range(0, len(BODY) - 3, 5)] == [BODY[o:o + 3] for o in range(0, len(BODY) - 3, 5)]
        if redirect is not None:  # every read after the first went where it was redirected
            assert any(urllib.parse.urlsplit(p).path.startswith("/r/") for _, p in state["paths"])
        if proxy is not None:  # every request went to the proxy
            assert all(p.startswith(url.split("/s/")[0]) for _, p in state["paths"])
    assert len(outputs) == 1


def test_refuses_a_private_host(server, env):
    port, state = server
    with pytest.raises(ResolutionError, match="is a loopback address.*allow_private_hosts"):
        http_reader(f"http://127.0.0.1:{port}/s/f.nii")
    assert state["paths"] == []


def test_refuses_a_name_that_resolves_to_a_private_address(server, names, env):
    port, state = server
    names["private.test"] = "127.0.0.1"
    with pytest.raises(ResolutionError, match="at 127.0.0.1.*is a loopback address"):
        http_reader(f"http://private.test:{port}/s/f.nii")
    assert state["paths"] == []


def test_refuses_a_redirect_to_a_private_host_before_requesting_it(server, names, env):
    """A prefetch thread's read is redirected to `localhost`, refused as written before
    it is requested (were it requested, the server would see it: under ServerIsPublic
    its address is allowed)."""
    port, state = server
    names["public.test"] = "127.0.0.1"
    read, _ = http_reader(f"http://public.test:{port}/s/f.nii", block=64, policy=ServerIsPublic())
    state["redirect"] = f"http://localhost:{port}/r/f.nii"
    state["direct"] = len(state["paths"])
    with pytest.raises(ResolutionError, match="http://localhost.*is a loopback address"):
        read.prefetch([(o, 3) for o in range(0, 100, 5)])
    assert not any(urllib.parse.urlsplit(p).path.startswith("/r/") for _, p in state["paths"])


def test_refuses_a_request_through_a_proxy(server, names, env):
    port, state = server
    names["proxied.test"] = "93.184.215.14"
    env.setenv("http_proxy", f"http://127.0.0.1:{port}")
    with pytest.raises(ResolutionError, match="through the proxy.*allow_unchecked_proxy=True"):
        http_reader("http://proxied.test/s/f.nii")
    assert state["paths"] == []
