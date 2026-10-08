"""Concurrent document reads of the store profiles (store.py, `Store.prefetch`):
the same outcome as reading one document at a time, each document read once,
and a bounded number of reads in flight. The browser implementation has the
same tests in web/test/store.test.ts.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from vzip.virtualize import Rejected, virtualize_store
from vzip.virtualize.store import _Prefetch, object_url

FIXTURES = Path(__file__).parent.parent / "web" / "test" / "fixtures"
STORES = sorted(p for f in ("n5", "zarr2", "ome-zarr") for p in (FIXTURES / f).iterdir() if p.is_dir())
URL = "http://h/b/s/"
LISTING = "http://h/b/?list-type=2&prefix=s%2F"


class FakeS3:
    """An urlopen serving `objects` (key → bytes, or an HTTP status to answer
    with) at URL: counting the reads of each key and the reads in flight."""

    def __init__(self, objects: dict, delay: float = 0.0, short: frozenset = frozenset()):
        self.objects, self.delay, self.short = objects, delay, short
        self.by_url = {object_url(URL, k): k for k in objects}
        self.reads: dict[str, int] = {}
        self.inflight = self.most = 0
        self.lock = threading.Lock()

    def listing(self) -> bytes:
        def size(v):
            return len(v) if isinstance(v, bytes) else 1
        contents = "".join(f"<Contents><Key>s/{escape(k)}</Key><Size>{size(v)}</Size></Contents>"
                           for k, v in sorted(self.objects.items()))
        return (f'<?xml version="1.0"?><ListBucketResult><IsTruncated>false</IsTruncated>{contents}'
                "</ListBucketResult>").encode()

    def __call__(self, req, timeout=None):
        import io

        class R(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        if req.full_url == LISTING:
            return R(self.listing())
        key = self.by_url[req.full_url]
        with self.lock:
            self.reads[key] = self.reads.get(key, 0) + 1
            self.inflight += 1
            self.most = max(self.most, self.inflight)
        try:
            time.sleep(self.delay)
            v = self.objects[key]
            if isinstance(v, int):
                raise urllib.error.HTTPError(req.full_url, v, "refused", {}, None)
            return R(v[:-1] if key in self.short else v)
        finally:
            with self.lock:
                self.inflight -= 1


def directory(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def outcome(monkeypatch, s3: FakeS3, workers: int):
    """("ok", format, docs, chunks), ("rejected", message) or ("failed", type, message)."""
    monkeypatch.setattr(urllib.request, "urlopen", s3)
    try:
        fmt, out = virtualize_store(URL, workers=workers)
    except Rejected as e:
        return ("rejected", str(e))
    except OSError as e:
        return ("failed", type(e).__name__, str(e))
    return ("ok", fmt, json.dumps(out.docs, sort_keys=True), out.chunks)


@pytest.mark.parametrize("root", STORES, ids=lambda p: p.name)
def test_concurrent_reads_give_the_same_outcome_reading_each_document_once(root, monkeypatch):
    objects = directory(root)
    sequential, concurrent = FakeS3(objects), FakeS3(objects)
    expected = outcome(monkeypatch, sequential, 1)
    assert outcome(monkeypatch, concurrent, 8) == expected
    assert set(concurrent.reads.values()) <= {1}
    if expected[0] == "ok":  # the documents read are those the profile reads
        assert concurrent.reads.keys() == sequential.reads.keys()


def test_reads_in_flight_are_bounded(monkeypatch):
    objects = {".zgroup": b'{"zarr_format": 2}'}
    for i in range(40):
        objects[f"a{i}/.zarray"] = json.dumps({
            "zarr_format": 2, "shape": [4], "chunks": [2], "dtype": "<u2", "order": "C",
            "compressor": None, "fill_value": 0, "filters": None}).encode()
        objects[f"a{i}/.zattrs"] = b"{}"
    s3 = FakeS3(objects, delay=0.01)
    assert outcome(monkeypatch, s3, 4)[0] == "ok"
    assert s3.most == 4
    assert set(s3.reads.values()) == {1} and len(s3.reads) == 81


def test_bytes_held_ahead_are_bounded():
    sizes = {f"k{i}": 10 for i in range(30)}
    held, most, lock = [0], [0], threading.Lock()

    def read(key):
        with lock:
            held[0] += sizes[key]
            most[0] = max(most[0], held[0])
        time.sleep(0.002)
        return b"x" * sizes[key]

    p = _Prefetch(read, list(sizes.items()), workers=8, budget=35, wanted=None)
    for key in sizes:
        time.sleep(0.005)
        assert p.take(key) == (True, b"x" * 10)
        with lock:
            held[0] -= sizes[key]
    assert most[0] <= 35


ZARRAY = json.dumps({"zarr_format": 2, "shape": [4], "chunks": [2], "dtype": "|u1", "order": "C",
                     "compressor": None, "fill_value": 0, "filters": None}).encode()


def test_a_failed_read_is_the_same_failure(monkeypatch):
    objects = {".zgroup": b'{"zarr_format": 2}', "a/.zarray": ZARRAY, "b/.zarray": 404, "c/.zarray": ZARRAY}
    failed = outcome(monkeypatch, FakeS3(objects), 8)
    assert failed == outcome(monkeypatch, FakeS3(objects), 1)
    assert failed == ("failed", "HTTPError", "HTTP Error 404: refused")


def test_a_short_read_is_the_same_failure(monkeypatch):
    objects = {".zgroup": b'{"zarr_format": 2}', "a/.zarray": ZARRAY, "b/.zarray": ZARRAY}
    failed = outcome(monkeypatch, FakeS3(objects, short=frozenset({"b/.zarray"})), 8)
    assert failed == outcome(monkeypatch, FakeS3(objects, short=frozenset({"b/.zarray"})), 1)
    assert failed[:2] == ("failed", "OSError") and "the listing says" in failed[2]


def test_a_failed_read_the_profile_never_makes_is_not_a_failure(monkeypatch):
    # a/.zarray rejects the input before the profile reads b/.zarray, which the
    # concurrent reads have read (and failed to).
    objects = {".zgroup": b'{"zarr_format": 2}', "a/.zarray": b'{"zarr_format": 3}', "b/.zarray": 404}
    rejected = outcome(monkeypatch, FakeS3(objects), 8)
    assert rejected == outcome(monkeypatch, FakeS3(objects), 1)
    assert rejected == ("rejected", "a/.zarray: zarr_format is not 2")
