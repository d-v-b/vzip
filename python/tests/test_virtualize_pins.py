"""The virtualizers' pins and range checksums (spec/virtualize.md §1.2, §1.4;
spec/archive.md §5.2, §6.1)."""

import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from zarr.core.buffer import default_buffer_prototype
from zarr.core.sync import sync

from vzip.archive import Entry, parse_central_directory
from vzip.pb import Concat, Range, Source, decode_table
from vzip.policy import Policy, crc32c
from vzip.store import VZipStore
from vzip.virtualize import virtualize

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
TIFF = FIXTURES / "tiff" / "tczyx_uint16_deflate.ome.tif"
JPEG = FIXTURES / "tiff" / "jpeg_ycbcr.tif"  # has data sources (shared JPEG tables)
STORE = FIXTURES / "zarr2" / "zarr2_dtypes"
LOOPBACK = Policy(allow_private_hosts=True)  # the test server is on 127.0.0.1 (spec/archive.md §8.7 rule 3)


@pytest.fixture
def server():
    """Serves files of the repository's fixtures with HEAD and range GETs, and
    the ETag `state["etag"](path, n)` for the n-th response (None: no ETag)."""
    state = {"etag": lambda path, n: '"fixed"', "count": 0}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, head: bool):
            body = (FIXTURES / self.path.lstrip("/")).read_bytes()
            state["count"] += 1
            etag = state["etag"](self.path, state["count"])
            rng = self.headers.get("Range")
            if rng and "," in rng:  # several ranges: the whole object, as a server without multipart answers
                rng = None
            if rng:
                a, b = (int(x) for x in rng[6:].split("-"))
                data, status = body[a:b + 1], 206
            else:
                data, status = body, 200
            self.send_response(status)
            if rng:
                self.send_header("Content-Range", f"bytes {a}-{a + len(data) - 1}/{len(body)}")
            self.send_header("Content-Length", str(len(data)))
            if etag:
                self.send_header("ETag", etag)
            self.end_headers()
            if not head:
                self.wfile.write(data)

        def do_GET(self):
            self._send(False)

        def do_HEAD(self):
            self._send(True)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", state
    srv.shutdown()


def _table(path) -> tuple[list[Source], dict[str, list[Range]]]:
    """The source table and the references of an archive."""
    with zipfile.ZipFile(path) as z:
        sources, _ = decode_table(z.read("__vz__/sources"))
    with open(path, "rb") as f:
        data = f.read()
    import struct
    eocd = data.rindex(b"PK\x05\x06")
    cd_size, cd_off = struct.unpack_from("<II", data, eocd + 12)
    entries: dict[str, Entry] = parse_central_directory(data[cd_off:cd_off + cd_size], trust_offsets=True)
    refs = {k: list(e.ref.parts if isinstance(e.ref, Concat) else [e.ref])
            for k, e in entries.items() if e.is_ref}
    return sources, refs


def test_pins_and_checksums(tmp_path, server):
    base, state = server
    tiff_size, jpeg_size = TIFF.stat().st_size, JPEG.stat().st_size
    cases = [
        # (location, url, checksums, expected pins of source 0)
        (str(TIFF), "https://example.test/a.tif", False, {"size": tiff_size}),
        (str(TIFF), "https://example.test/a.tif", True, {"size": tiff_size}),
        (f"{base}/tiff/{TIFF.name}", None, False, {"size": tiff_size, "etag": '"fixed"'}),
        (f"{base}/tiff/{JPEG.name}", None, True, {"size": jpeg_size, "etag": '"fixed"'}),
        # the output names another URL than the one read: no ETag
        (f"{base}/tiff/{TIFF.name}", "https://example.test/a.tif", False, {"size": tiff_size}),
    ]
    for location, url, checksums, pins in cases:
        out = tmp_path / "out.vzip"
        _, o = virtualize(location, url, checksums=checksums, policy=LOOPBACK)
        o.write(str(out))
        sources, refs = _table(out)
        assert sources[0] == Source(url=url or location, **pins), (location, url)
        body = (TIFF if TIFF.name in location else JPEG).read_bytes()
        for k, ranges in refs.items():
            for r in ranges:
                if r.data is None and r.source == 0:
                    want = crc32c(body[r.offset:r.offset + r.length]) if checksums else None
                    assert r.crc32c == want, (k, r)
        if url is None:  # read back, checking the pins and checksums
            s = VZipStore(str(out), policy=Policy(allow_private_hosts=True))  # a 127.0.0.1 server
            for k in refs:
                assert sync(s.get(k, default_buffer_prototype())) is not None

    # a store input: every source pins its object's size
    for checksums in (False, True):
        _, o = virtualize(str(STORE), "https://example.test/s/", checksums=checksums)
        o.write(str(tmp_path / "s.vzip"))
        sources, refs = _table(tmp_path / "s.vzip")
        sizes = {f"https://example.test/s/{p.relative_to(STORE).as_posix()}": p.stat().st_size
                 for p in STORE.rglob("*") if p.is_file()}
        assert sources and all(s.size == sizes[s.url] and s.etag is None for s in sources)
        for k, (r,) in refs.items():
            obj = STORE / sources[r.source].url.removeprefix("https://example.test/s/")
            assert r.crc32c == (crc32c(obj.read_bytes()) if checksums else None), k


def test_no_etag_pin_when_the_responses_disagree(tmp_path, server):
    base, state = server
    state["etag"] = lambda path, n: '"fixed"' if n % 2 else None  # some responses have none
    _, o = virtualize(f"{base}/tiff/{TIFF.name}", policy=LOOPBACK)
    o.write(str(tmp_path / "out.vzip"))
    assert _table(tmp_path / "out.vzip")[0][0].etag is None


def test_fails_when_the_etag_changes_while_reading(server):
    base, state = server
    state["etag"] = lambda path, n: f'"v{n}"'
    with pytest.raises(OSError, match="changed while it was read"):
        virtualize(f"{base}/tiff/{TIFF.name}", policy=LOOPBACK)
