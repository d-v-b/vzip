"""The reader policy (spec §8.7), range checksums (§5.2), pins (§6.1) and the
recorded revision (§1.3), in the reference reader and writer."""

import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from zarr.abc.store import RangeByteRequest
from zarr.core.buffer import default_buffer_prototype
from zarr.core.sync import sync

from vzip.archive import SPEC_REVISION, VZipWriter
from vzip.errors import ArchiveError, RequestError, ResolutionError
from vzip.pb import Range, Source, decode_table
from vzip.policy import Policy, address_class, crc32c, matches_prefix
from vzip.store import VZipStore

BLOB = bytes(range(256)) * 64  # 16 KiB
# The test server is on 127.0.0.1, which spec §8.7 rule 3 refuses by default.
SERVED = Policy(allow_private_hosts=True)


def _get(store, key, br=None):
    b = sync(store.get(key, default_buffer_prototype(), br))
    return None if b is None else b.to_bytes()


@pytest.fixture
def server():
    """Serves `state["body"]` at /blob.bin with range requests and `state["etag"]`,
    and redirects /moved to `state["location"]`. Counts requests."""
    state = {"body": BLOB, "etag": '"v1"', "location": "/blob.bin", "requests": 0,
             "hosts": [], "paths": []}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            state["requests"] += 1
            state["hosts"].append(self.headers["Host"])
            state["paths"].append(self.path)
            if self.path.endswith("/moved"):
                self.send_response(302)
                self.send_header("Location", state["location"])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = state["body"]
            a, b = (int(x) for x in self.headers["Range"][6:].split("-"))
            data = body[a:b + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{min(b, len(body) - 1)}/{len(body)}")
            self.send_header("Content-Length", str(len(data)))
            if state["etag"]:
                self.send_header("ETag", state["etag"])
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", state
    srv.shutdown()


def _archive(path, sources, refs, **kw):
    """An archive with `sources` (Source objects) and references {key: [Range]}."""
    with open(path, "wb") as f, VZipWriter(f, **kw) as w:
        for s in sources:
            w.source(s)
        for k, ranges in refs.items():
            w.add_ranges(k, ranges)
    return str(path)


def test_crc32c_check_value():
    assert crc32c(b"123456789") == 0xE3069283
    assert crc32c(b"") == 0


def test_policy_allows_these_urls():
    local, remote = "file:///d/a.vzip", "https://h.example/a.vzip"
    unsafe_files = Policy(allow_files_from_remote_archives=True)
    cases = [
        # (policy, archive base, source url)
        (Policy(), local, "file:///d/blob.bin"),
        (Policy(), local, "https://g.example/blob.bin"),  # a local archive may read the web
        (Policy(), remote, "https://g.example/blob.bin"),
        (Policy(), remote, "http://g.example/blob.bin"),  # http and https are one scheme
        (Policy(), "HTTP://h.example/a.vzip", "HTTPS://g.example/blob.bin"),
        (Policy(schemes=frozenset({"s3"})), remote, "s3://bucket/blob.bin"),
        (Policy(schemes=frozenset({"S3"})), local, "s3://bucket/blob.bin"),
        (unsafe_files, remote, "file:///d/blob.bin"),
        (Policy(prefixes=("https://data.example/a/",)), local, "https://data.example/a/x.bin"),
        (Policy(prefixes=("https://data.example/a",)), local, "https://data.example/a/x.bin"),
        (Policy(prefixes=("https://data.example/a",)), local, "https://data.example/a?x=1"),
        (Policy(prefixes=("file:///d/", "https://g/")), remote, "file:///d/x"),
        # public literals, and names (checked when they are resolved)
        (Policy(), remote, "http://93.184.215.14/x"),
        (Policy(), remote, "http://[2606:4700::1111]/x"),
        (Policy(), remote, "http://internal.example/x"),
        # the unsafe opt-out, for every class and every archive
        (Policy(allow_private_hosts=True), remote, "http://169.254.169.254/latest/meta-data"),
        (Policy(allow_private_hosts=True), remote, "http://127.0.0.1/x"),
        (Policy(allow_private_hosts=True), local, "http://localhost:8000/x"),
        (Policy(allow_private_hosts=True), local, "http://10.1.2.3/x"),
        (Policy(allow_private_hosts=True), local, "http://[fd00::1]/x"),
        (Policy(allow_private_hosts=True), local, "http://0.0.0.0/x"),
        (Policy(allow_private_hosts=True, prefixes=("http://10.0.0.1/",)), remote,
         "http://10.0.0.1/x"),
    ]
    for policy, base, url in cases:
        policy.check(base, url)


def test_address_class():
    cases = {
        "127.0.0.1": "loopback", "127.255.0.9": "loopback", "::1": "loopback",
        "10.0.0.1": "private", "172.16.0.1": "private", "172.31.255.255": "private",
        "192.168.1.1": "private", "fc00::1": "private", "fdff::1": "private",
        "169.254.169.254": "link-local", "fe80::1": "link-local", "fe80::1%en0": "link-local",
        "0.0.0.0": "special", "::": "special", "100.64.0.1": "special", "192.0.2.1": "special",
        "198.18.0.1": "special", "224.0.0.1": "special", "239.1.2.3": "special",
        "240.0.0.1": "special", "255.255.255.255": "special", "ff02::1": "special",
        "2001:db8::1": "special", "::a00:1": "special", "fec0::1": "special",
        # IPv4 embedded in IPv6: the IPv4 address's class
        "::ffff:127.0.0.1": "loopback", "::ffff:10.0.0.1": "private",
        "::ffff:169.254.169.254": "link-local", "64:ff9b::a9fe:a9fe": "link-local",
        "2002:a00:1::1": "private", "::ffff:8.8.8.8": "public",
        "8.8.8.8": "public", "172.32.0.1": "public", "2606:4700::1111": "public",
    }
    assert {a: address_class(a) for a in cases} == cases


def test_matches_prefix():
    cases = {
        ("https://h/a/x", "https://h/a/"): True,
        ("https://h/a/x", "https://h/a"): True,
        ("https://h/a", "https://h/a"): True,
        ("https://h/a#f", "https://h/a"): True,
        ("https://h/ab", "https://h/a"): False,
        ("https://h.evil.test/", "https://h"): False,
        ("https://h/a/%2E%2E/b", "https://h/a/"): False,
        ("https://h/a/%2fb", "https://h/a/"): False,
        ("https://h/a/%5Cb", "https://h/a/"): False,
        ("https://h/a/b?q=%2F", "https://h/a/"): True,  # the query is not a path
        ("HTTPS://h/a/x", "https://h/a/"): False,  # exact comparison fails closed
    }
    assert {k: matches_prefix(*k) for k in cases} == cases


def test_reads_with_checksums_pins_and_revision(tmp_path, server):
    """Valid reads: checksummed ranges (whole and partial windows), pinned
    sources, a source of another scheme that the policy allows, and the
    recorded revision."""
    base, state = server
    local = tmp_path / "blob.bin"
    local.write_bytes(BLOB)
    good = crc32c(BLOB[100:200])
    arc = _archive(tmp_path / "a.vzip", [
        Source(url="blob.bin", size=len(BLOB)),  # 0: next to the archive
        Source(url=f"{base}/blob.bin", size=len(BLOB), etag='"v1"'),  # 1
        Source(data=b"HEADER"),  # 2
    ], {
        "file_crc": [Range(source=0, offset=100, length=100, crc32c=good)],
        "http_crc": [Range(source=1, offset=100, length=100, crc32c=good)],
        "concat": [Range(source=2, offset=0, length=6, crc32c=crc32c(b"HEADER")),
                   Range(source=1, offset=0, length=10, crc32c=crc32c(BLOB[:10])),
                   Range(data=b"!")],
        "plain": [Range(source=1, offset=5, length=5)],
    })
    s = VZipStore(arc, policy=SERVED)
    cases = {
        ("file_crc", None): BLOB[100:200],
        ("file_crc", RangeByteRequest(10, 20)): BLOB[110:120],
        ("http_crc", None): BLOB[100:200],
        ("http_crc", RangeByteRequest(90, 100)): BLOB[190:200],
        ("concat", None): b"HEADER" + BLOB[:10] + b"!",
        ("concat", RangeByteRequest(4, 8)): b"ER" + BLOB[:2],
        ("plain", None): BLOB[5:10],
    }
    assert {k: _get(s, *k) for k in cases} == cases
    assert s.revision == SPEC_REVISION
    # a partial window of a checksummed range reads the whole range
    s.stats.reset()
    _get(s, "http_crc", RangeByteRequest(0, 1))
    assert s.stats.external_bytes == 100


def test_writer_records_revision_and_bulk_size_pins(tmp_path):
    path = tmp_path / "a.vzip"
    with open(path, "wb") as f, VZipWriter(f) as w:
        w.add_url_refs([("a", "https://h/a", 5), ("b", "https://h/b", 0)], pin_size=True)
    import zipfile
    with zipfile.ZipFile(path) as z:
        sources, revision = decode_table(z.read("__vz__/sources"))
    assert revision == SPEC_REVISION
    assert sources == [Source(url="https://h/a", size=5), Source(url="https://h/b", size=0)]


def test_writer_rejects_a_checksum_on_a_literal_range(tmp_path):
    with open(tmp_path / "a.vzip", "wb") as f, pytest.raises(ValueError, match="crc32c"):
        VZipWriter(f).add_ranges("k", [Range(data=b"x", crc32c=0)])


def test_refuses_a_file_source_of_a_remote_archive():
    for policy in [Policy(), Policy(schemes=frozenset({"s3"}))]:
        with pytest.raises(ResolutionError, match="may not read local files"):
            policy.check("https://h.example/a.vzip", "file:///etc/passwd")


def test_refuses_an_unlisted_scheme():
    for base in ["file:///d/a.vzip", "https://h.example/a.vzip"]:
        for url in ["s3://bucket/x", "gs://bucket/x", "data:,abc", "ftp://h.example/x"]:
            with pytest.raises(ResolutionError, match="is not allowed"):
                Policy(schemes=frozenset({"az"})).check(base, url)


def test_policy_refuses_file_in_schemes():
    with pytest.raises(ValueError, match="allow_files_from_remote_archives"):
        Policy(schemes=frozenset({"FILE"}))


def test_refuses_a_private_host_written_in_the_url():
    """Rule 3 on the URL itself: every class, from every archive, local ones included."""
    remote, local = "https://h.example/a.vzip", "file:///d/a.vzip"
    cases = [
        (remote, "http://127.0.0.1/x", "loopback"),
        (remote, "http://localhost:8000/x", "loopback"),
        (remote, "http://[::ffff:127.0.0.1]/x", "loopback"),
        (remote, "http://10.0.0.1/x", "private"),
        (remote, "https://[fd12::1]/x", "private"),
        (remote, "http://169.254.169.254/latest/meta-data", "link-local"),
        (remote, "http://[fe80::1]/x", "link-local"),
        (remote, "http://0.0.0.0/x", "special"),
        (remote, "http://[::]/x", "special"),
        (remote, "http://224.0.0.1/x", "special"),
        (local, "http://localhost:8000/x", "loopback"),
        (local, "http://a.localhost/x", "loopback"),
        (local, "http://127.0.0.1/x", "loopback"),
        (local, "http://10.1.2.3/x", "private"),
        (local, "http://[::ffff:192.168.0.1]/x", "private"),
        (local, "http://169.254.169.254/x", "link-local"),
        (local, "http://[64:ff9b::a9fe:a9fe]/x", "link-local"),
        (local, "http://0.0.0.0/x", "special"),
        # an archive on a host of that class is no exception
        ("http://127.0.0.1:8000/a.vzip", "http://127.0.0.1:8000/x", "loopback"),
        ("http://localhost/a.vzip", "http://[::1]/x", "loopback"),
        ("http://10.0.0.1/a.vzip", "http://10.0.0.1/x", "private"),
    ]
    for base, url, cls in cases:
        with pytest.raises(ResolutionError, match=f"is a {cls} address.*allow_private_hosts"):
            Policy().check(base, url)
        # neither a prefix list nor another unsafe setting lifts rule 3
        for policy in [Policy(prefixes=(url,)), Policy(allow_unchecked_proxy=True)]:
            with pytest.raises(ResolutionError, match=f"is a {cls} address"):
                policy.check(base, url)


def test_refuses_a_url_outside_the_prefixes(tmp_path, server):
    base, state = server
    arc = _archive(tmp_path / "a.vzip", [Source(url=f"{base}/blob.bin")],
                   {"r": [Range(source=0, offset=0, length=4)]})
    with pytest.raises(ResolutionError, match="prefix"):
        _get(VZipStore(arc, policy=Policy(prefixes=(f"{base}/other/",), allow_private_hosts=True)), "r")
    assert state["requests"] == 0


def test_refuses_a_redirect_outside_the_prefixes(tmp_path, server):
    base, state = server
    state["location"] = "http://169.254.169.254/latest/meta-data"
    arc = _archive(tmp_path / "a.vzip", [Source(url=f"{base}/moved")],
                   {"r": [Range(source=0, offset=0, length=4)]})
    with pytest.raises(ResolutionError, match="prefix"):
        _get(VZipStore(arc, policy=Policy(prefixes=(f"{base}/",), allow_private_hosts=True)), "r")
    assert state["requests"] == 1  # the redirect target was never requested


def test_refuses_too_many_sources(tmp_path):
    arc = _archive(tmp_path / "a.vzip", [Source(data=b"a"), Source(data=b"b"), Source(data=b"c")], {})
    with pytest.raises(ArchiveError, match="3 sources"):
        sync(VZipStore(arc, policy=Policy(max_sources=2))._ensure_open())


def test_refuses_too_many_reads(tmp_path, server):
    base, state = server
    # three ranges far apart: three reads after coalescing
    arc = _archive(tmp_path / "a.vzip", [Source(url=f"{base}/blob.bin")], {
        "r": [Range(source=0, offset=0, length=1), Range(source=0, offset=200_000, length=1),
              Range(source=0, offset=400_000, length=1)]})
    s = VZipStore(arc, policy=Policy(max_reads=2, allow_private_hosts=True))
    with pytest.raises(RequestError, match="3 reads"):
        _get(s, "r")
    assert state["requests"] == 0


def test_refuses_an_inflation_bomb(tmp_path):
    # a source table that inflates to 64 MiB from about 64 KiB
    path = tmp_path / "a.vzip"
    with open(path, "wb") as f:
        w = VZipWriter(f)
        w._source_table_override = b"\0" * (64 << 20)
        w._skip_checks = True
        w.close()
    with pytest.raises(ArchiveError, match="inflates to more than"):
        sync(VZipStore(str(path), policy=Policy(max_format_entry=1 << 20))._ensure_open())


def test_refuses_a_bytes_entry_that_inflates_past_its_size(tmp_path):
    path = tmp_path / "a.vzip"
    with open(path, "wb") as f, VZipWriter(f) as w:
        c = zlib.compressobj(9, zlib.DEFLATED, -15)
        w._entry_raw("bomb", b"x" * 10, c.compress(b"x" * (1 << 20)) + c.flush())
    from vzip.errors import BodyError
    with pytest.raises(BodyError, match="inflates to more than 10 bytes"):
        _get(VZipStore(str(path)), "bomb")


def test_detects_a_size_mismatch(tmp_path, server):
    base, state = server
    arc = _archive(tmp_path / "a.vzip", [Source(url=f"{base}/blob.bin", size=len(BLOB))],
                   {"r": [Range(source=0, offset=0, length=4)]})
    state["body"] = BLOB + b"appended"
    with pytest.raises(ResolutionError, match="the source changed"):
        _get(VZipStore(arc, policy=SERVED), "r")


def test_detects_a_size_mismatch_of_a_local_file(tmp_path):
    (tmp_path / "blob.bin").write_bytes(BLOB)
    arc = _archive(tmp_path / "a.vzip", [Source(url="blob.bin", size=len(BLOB) - 1)],
                   {"r": [Range(source=0, offset=0, length=4)]})
    with pytest.raises(ResolutionError, match="the source changed"):
        _get(VZipStore(arc), "r")


def test_detects_an_etag_mismatch(tmp_path, server):
    base, state = server
    arc = _archive(tmp_path / "a.vzip", [Source(url=f"{base}/blob.bin", etag='"v1"')],
                   {"r": [Range(source=0, offset=0, length=4)]})
    state["etag"] = '"v2"'
    with pytest.raises(ResolutionError, match="the source changed"):
        _get(VZipStore(arc, policy=SERVED), "r")


def test_detects_a_crc_mismatch(tmp_path, server):
    base, state = server
    arc = _archive(tmp_path / "a.vzip", [Source(url=f"{base}/blob.bin")],
                   {"r": [Range(source=0, offset=0, length=100, crc32c=crc32c(BLOB[:100]))]})
    state["body"] = b"\xff" + BLOB[1:]  # same size, no ETag pin: only the checksum sees it
    with pytest.raises(ResolutionError, match="crc32c"):
        _get(VZipStore(arc, policy=SERVED), "r", RangeByteRequest(50, 60))


def test_rejects_an_unknown_format_version(tmp_path):
    arc = _archive(tmp_path / "a.vzip", [], {})
    data = bytearray(open(arc, "rb").read())
    i = data.rindex(b"vzip/0")
    data[i + 5] = ord("1")
    open(arc, "wb").write(bytes(data))
    with pytest.raises(ArchiveError, match="unsupported vzip format version"):
        sync(VZipStore(arc)._ensure_open())


# ------------------------------------------------------------ rule 3 on the network

@pytest.fixture
def resolver(monkeypatch):
    """Maps host names to addresses for the reader (`names`), passing others on."""
    import socket

    import vzip.store

    names: dict[str, str] = {}
    looked_up: list[str] = []
    real = socket.getaddrinfo

    def fake(host, port, *args, **kwargs):
        looked_up.append(host)
        if host in names:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (names[host], port))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(vzip.store, "_getaddrinfo", fake)
    return names, looked_up


def _range(policy, base, url):
    """Bytes 0-4 of `url`, read for an archive at `base` under `policy`, as the store does."""
    from vzip.store import http_range

    address = None if policy.allow_private_hosts else policy.check_address
    return http_range(url, 0, 4, Source(url=url), lambda u: policy.check(base, u), address,
                      policy.allow_unchecked_proxy)[0]


def _loopback_only(url, ip):
    """An address check that allows the test server's address, and what Policy() allows."""
    if ip != "127.0.0.1":
        Policy().check_address(url, ip)


def _read(url, seen=None):
    """Bytes 0-4 of `url` through http_range with `_loopback_only`, recording checked addresses."""
    from vzip.store import http_range

    def address(u, ip):
        if seen is not None:
            seen.append(ip)
        _loopback_only(u, ip)

    return http_range(url, 0, 4, Source(url=url), None, address)[0]


@pytest.fixture
def no_proxy_env(monkeypatch):
    for name in ["http_proxy", "https_proxy", "all_proxy", "no_proxy"]:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    return monkeypatch


def test_reads_the_hosts_rule_3_allows(tmp_path, server, resolver):
    """A public name is connected to at the address it was checked at (here the server's,
    by a check that allows it), keeping its Host; and the opt-out reaches the server."""
    base, state = server
    port = base.rsplit(":", 1)[1]
    names, _ = resolver
    names["data.test"] = "127.0.0.1"
    seen: list[str] = []
    assert _read(f"http://data.test:{port}/blob.bin", seen) == BLOB[:4]
    assert seen == ["127.0.0.1", "127.0.0.1"]  # at connect, and the socket's peer
    assert state["hosts"][-1] == f"data.test:{port}"
    for base_ in ["file:///d/a.vzip", "https://h.example/a.vzip", f"{base}/a.vzip"]:
        assert _range(SERVED, base_, f"{base}/blob.bin") == BLOB[:4]
        assert _range(SERVED, base_, f"http://data.test:{port}/blob.bin") == BLOB[:4]
    arc = _archive(tmp_path / "a.vzip", [Source(url=f"http://data.test:{port}/blob.bin")],
                   {"r": [Range(source=0, offset=0, length=4)]})
    assert _get(VZipStore(arc, policy=SERVED), "r") == BLOB[:4]


def test_refuses_a_name_that_resolves_to_a_refused_address(tmp_path, server, resolver):
    """The address connected to is checked, not the name: a name that resolves to a
    refused address (DNS rebinding) is refused before any connection, local archives too."""
    base, state = server
    port = base.rsplit(":", 1)[1]
    names, looked_up = resolver
    for name, address, cls, archive in [
        ("rebind.test", "127.0.0.1", "loopback", "https://h.example/a.vzip"),
        ("rebind.test", "127.0.0.1", "loopback", "file:///d/a.vzip"),
        ("rebind.test", "10.0.0.1", "private", "file:///d/a.vzip"),
        ("metadata.test", "169.254.169.254", "link-local", "file:///d/a.vzip"),
        ("zero.test", "0.0.0.0", "special", f"{base}/a.vzip"),
    ]:
        names[name] = address
        with pytest.raises(ResolutionError, match=f"at {address}.*is a {cls} address"):
            _range(Policy(), archive, f"http://{name}:{port}/blob.bin")
        assert looked_up[-1] == name
    names["local.test"] = "127.0.0.1"
    arc = _archive(tmp_path / "a.vzip", [Source(url=f"http://local.test:{port}/blob.bin")],
                   {"r": [Range(source=0, offset=0, length=4)]})
    with pytest.raises(ResolutionError, match="is a loopback address"):
        _get(VZipStore(arc), "r")  # a local archive by default
    assert state["requests"] == 0


def test_refuses_an_address_written_in_another_form(server):
    """A decimal, hexadecimal or octal IPv4 host is the address it denotes."""
    for url in ["http://2852039166/x", "http://0xa9.0xfe.0xa9.0xfe/x", "http://0251.0376.0251.0376/x"]:
        with pytest.raises(ResolutionError, match="169.254.169.254.*is a link-local address"):
            _range(Policy(), "file:///d/a.vzip", url)


def test_refuses_a_redirect_to_a_refused_address(server, resolver):
    """Neither a literal address nor a name is requested after the redirect."""
    base, state = server
    port = base.rsplit(":", 1)[1]
    names, _ = resolver
    names["metadata.test"] = "169.254.169.254"
    names["loop.test"] = "127.0.0.2"
    for location, cls in [("http://169.254.169.254/latest/meta-data", "link-local"),
                          (f"http://metadata.test:{port}/blob.bin", "link-local"),
                          (f"http://loop.test:{port}/blob.bin", "loopback")]:
        state["location"], state["requests"] = location, 0
        with pytest.raises(ResolutionError, match=f"is a {cls} address"):
            _read(f"{base}/moved")
        assert state["requests"] == 1  # the redirect only


def test_refuses_a_reused_connection_to_a_refused_address(server, resolver):
    """A kept-alive connection opened under the opt-out is checked again for another archive."""
    base, state = server
    names, looked_up = resolver
    names["reuse.test"] = "127.0.0.1"
    url = f"http://reuse.test:{base.rsplit(':', 1)[1]}/blob.bin"
    assert _range(SERVED, "file:///d/a.vzip", url) == BLOB[:4]
    n = len(looked_up)
    with pytest.raises(ResolutionError, match="at 127.0.0.1.*is a loopback address"):
        _range(Policy(), "file:///d/a.vzip", url)
    assert len(looked_up) == n  # the pooled connection, not a new one
    assert state["requests"] == 1


def test_reads_through_a_proxy_the_policy_allows(server, resolver, no_proxy_env):
    """With allow_unchecked_proxy, a name that resolves to a public address here goes
    through the proxy (the test server); with allow_private_hosts, any does."""
    base, state = server
    names, _ = resolver
    names["public.test"] = "93.184.215.14"
    names["private.test"] = "10.0.0.1"
    no_proxy_env.setenv("http_proxy", base)
    cases = [
        (Policy(allow_unchecked_proxy=True), "http://public.test/blob.bin"),
        (Policy(allow_private_hosts=True), "http://private.test/blob.bin"),
    ]
    for policy, url in cases:
        assert _range(policy, "https://h.example/a.vzip", url) == BLOB[:4]
        assert state["paths"][-1] == url  # an absolute target: the request went to the proxy


def test_refuses_a_request_through_a_proxy(server, resolver, no_proxy_env):
    """Rule 3 cannot check where a proxy connects: refused, naming the opt-out."""
    base, state = server
    names, _ = resolver
    names["public.test"] = "93.184.215.14"
    for var in ["http_proxy", "HTTP_PROXY"]:
        no_proxy_env.delenv("http_proxy", raising=False)
        no_proxy_env.delenv("HTTP_PROXY", raising=False)
        no_proxy_env.setenv(var, base)
        with pytest.raises(ResolutionError, match="through the proxy.*allow_unchecked_proxy=True"):
            _range(Policy(), "https://h.example/a.vzip", "http://public.test/blob.bin")
    no_proxy_env.setenv("https_proxy", base)
    with pytest.raises(ResolutionError, match="through the proxy"):
        _range(Policy(), "https://h.example/a.vzip", "https://public.test/blob.bin")
    assert state["requests"] == 0


def test_refuses_a_refused_address_through_an_allowed_proxy(server, resolver, no_proxy_env):
    """With allow_unchecked_proxy, the name is still checked where the reader runs."""
    base, state = server
    names, _ = resolver
    names["internal.test"] = "10.0.0.1"
    no_proxy_env.setenv("http_proxy", base)
    with pytest.raises(ResolutionError, match="is a private address"):
        _range(Policy(allow_unchecked_proxy=True), "https://h.example/a.vzip",
               "http://internal.test/x")
    assert state["requests"] == 0


def test_checks_a_request_the_proxy_settings_send_directly(server, resolver, no_proxy_env):
    """no_proxy exempts a host, and all_proxy is not used: the reader connects itself,
    and checks the address."""
    base, state = server
    port = base.rsplit(":", 1)[1]
    names, _ = resolver
    names["direct.test"] = "127.0.0.1"
    url = f"http://direct.test:{port}/blob.bin"
    for env in [{"http_proxy": "http://127.0.0.1:9", "no_proxy": "direct.test"},
                {"ALL_PROXY": "http://127.0.0.1:9"}]:
        for k, v in env.items():
            no_proxy_env.setenv(k, v)
        with pytest.raises(ResolutionError, match="at 127.0.0.1.*is a loopback address"):
            _range(Policy(), "https://h.example/a.vzip", url)
        assert _read(url) == BLOB[:4]  # direct, not through the (closed) proxy port
        for k in env:
            no_proxy_env.delenv(k)
