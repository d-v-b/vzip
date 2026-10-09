"""Store inputs under the reader policy (SPEC.md §8.7): the listing, every object
read (the prefetch threads' too) and every redirect go through `Policy`, as a file
input's requests do. The browser implementation has the same tests in
web/test/store_policy.test.ts.
"""

import json
import socket
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

import vzip.store
from vzip.errors import ResolutionError
from vzip.policy import Policy
from vzip.virtualize import virtualize_store

ROOT = Path(__file__).parent.parent
STORE = ROOT / "web" / "test" / "fixtures" / "zarr2" / "zarr2_hierarchy"
OBJECTS = {p.relative_to(STORE).as_posix(): p.read_bytes() for p in STORE.rglob("*") if p.is_file()}


class ServerIsPublic(Policy):
    """`Policy()`, except that the test server's address (127.0.0.1) counts as a public one."""

    def check_address(self, url: str, address: str) -> None:
        if address != "127.0.0.1":
            super().check_address(url, address)


@pytest.fixture
def server():
    """A local S3-like server, and an HTTP proxy, serving OBJECTS as the store `b/s/`
    and again as `b/r/`. With `state["redirect"]`, a read of an object under `b/s/`
    is redirected to that URL followed by its key. `state["paths"]` records each
    request's target, as sent (absolute through a proxy)."""
    state: dict = {"redirect": None, "paths": []}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            state["paths"].append(self.path)
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            if path == "/b/":
                contents = "".join(f"<Contents><Key>s/{escape(k)}</Key><Size>{len(v)}</Size></Contents>"
                                   for k, v in sorted(OBJECTS.items()))
                status, body, headers = 200, (
                    f'<?xml version="1.0"?><ListBucketResult><IsTruncated>false</IsTruncated>{contents}'
                    "</ListBucketResult>").encode(), {}
            elif path.startswith("/b/s/") and state["redirect"] is not None:
                status, body, headers = 307, b"", {"Location": state["redirect"] + urllib.parse.quote(path[len("/b/s/"):])}
            else:
                a, b = (int(x) for x in self.headers["Range"][6:].split("-"))
                status, body, headers = 206, OBJECTS[path[len("/b/s/"):]][a:b + 1], {}
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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


def _docs(url: str, policy: Policy, workers: int) -> str:
    fmt, out = virtualize_store(url, workers=workers, policy=policy)
    assert fmt == "zarr2"
    return json.dumps(out.docs, sort_keys=True).replace(url, "<store>")


def test_reads_a_store_the_policy_allows(server, names, env):
    """Each way a policy admits a store's requests, read one document at a time and
    concurrently, gives the same output: a name that resolves to an allowed address,
    its object reads redirected to another such name; a private host with
    allow_private_hosts; and, through a proxy, a name that resolves to a public
    address with allow_unchecked_proxy, or to a private one with allow_private_hosts."""
    port, state = server
    names["public.test"] = "127.0.0.1"  # ServerIsPublic reaches it
    names["other.test"] = "127.0.0.1"
    names["proxied.test"] = "93.184.215.14"
    names["internal.test"] = "10.0.0.1"
    cases = [
        (ServerIsPublic(), f"http://public.test:{port}/b/s/", f"http://other.test:{port}/b/r/", None),
        (Policy(allow_private_hosts=True), f"http://127.0.0.1:{port}/b/s/", None, None),
        (Policy(allow_private_hosts=True), f"http://localhost:{port}/b/s/", None, None),
        (Policy(allow_unchecked_proxy=True), "http://proxied.test/b/s/", None, f"http://127.0.0.1:{port}"),
        (Policy(allow_private_hosts=True), "http://internal.test/b/s/", None, f"http://127.0.0.1:{port}"),
    ]
    names["localhost"] = "127.0.0.1"
    outputs = set()
    for policy, url, redirect, proxy in cases:
        state["redirect"] = redirect
        if proxy is None:
            env.delenv("http_proxy", raising=False)
        else:
            env.setenv("http_proxy", proxy)
        for workers in (1, 8):
            del state["paths"][:]
            outputs.add(_docs(url, policy, workers))
            if redirect is not None:  # every object read went where it was redirected
                assert any(p.startswith("/b/r/") for p in state["paths"])
            if proxy is not None:  # every request went to the proxy
                assert all(p.startswith(url.split("/b/")[0]) for p in state["paths"])
    assert len(outputs) == 1


def test_refuses_a_private_host(server, env):
    port, state = server
    with pytest.raises(ResolutionError, match="is a loopback address.*allow_private_hosts"):
        virtualize_store(f"http://127.0.0.1:{port}/b/s/")
    assert state["paths"] == []


def test_refuses_a_name_that_resolves_to_a_private_address(server, names, env):
    port, state = server
    names["private.test"] = "127.0.0.1"
    with pytest.raises(ResolutionError, match="at 127.0.0.1.*is a loopback address"):
        virtualize_store(f"http://private.test:{port}/b/s/")
    assert state["paths"] == []


def test_refuses_a_redirect_to_a_private_host_before_requesting_it(server, names, env):
    """A prefetch thread's read is redirected to `localhost`, refused as written
    before it is requested (were it requested, the server would see it: under
    ServerIsPublic its address is allowed)."""
    port, state = server
    names["public.test"] = "127.0.0.1"
    state["redirect"] = f"http://localhost:{port}/b/r/"
    with pytest.raises(ResolutionError, match="http://localhost.*is a loopback address"):
        virtualize_store(f"http://public.test:{port}/b/s/", workers=8, policy=ServerIsPublic())
    assert state["paths"] and not any(p.startswith("/b/r/") for p in state["paths"])


def test_refuses_a_request_through_a_proxy(server, names, env):
    port, state = server
    names["proxied.test"] = "93.184.215.14"
    env.setenv("http_proxy", f"http://127.0.0.1:{port}")
    with pytest.raises(ResolutionError, match="through the proxy.*allow_unchecked_proxy=True"):
        virtualize_store("http://proxied.test/b/s/")
    assert state["paths"] == []


def test_refuses_a_redirect_to_a_file_url_whatever_the_policy(server, names, env):
    """allow_files_from_remote_archives does not apply to stores: a store is read over http(s) only."""
    port, state = server
    names["public.test"] = "127.0.0.1"
    state["redirect"] = "file:///etc/"
    with pytest.raises(ResolutionError, match="redirect to a non-http URL"):
        virtualize_store(f"http://public.test:{port}/b/s/", workers=1,
                         policy=ServerIsPublic(allow_files_from_remote_archives=True))


def test_the_cli_passes_allow_private_hosts_to_a_store(server, tmp_path, env):
    port, _ = server
    url = f"http://127.0.0.1:{port}/b/s/"

    def run(*flags):
        return subprocess.run([sys.executable, "-m", "vzip.virtualize", *flags, url, str(tmp_path / "o.vzip")],
                              capture_output=True, text=True, cwd=ROOT)

    refused = run()
    assert refused.returncode == 1 and "allow_private_hosts" in refused.stderr
    assert run("--allow-private-hosts").returncode == 0
