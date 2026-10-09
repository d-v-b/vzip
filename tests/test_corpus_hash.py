"""conformance/virtualize/corpus_hash.py: the corpus lines' fingerprints."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "conformance" / "virtualize"))
import corpus_hash  # noqa: E402
from proxy import BLOCK, Upstream  # noqa: E402


def test_parse():
    cases = [
        ("https://a.test/x.nd2|x", ("https://a.test/x.nd2", "x", [], None)),
        ("https://a.test/s/|s|py-only", ("https://a.test/s/", "s", ["py-only"], None)),
        ("https://a.test/x|x|ends-sha256=ab", ("https://a.test/x", "x", [], "ends-sha256=ab")),
        ("https://a.test/s/|s|py-only|listing-sha256=cd", ("https://a.test/s/", "s", ["py-only"], "listing-sha256=cd")),
    ]
    for line, want in cases:
        assert corpus_hash.parse(line) == want, line


def test_parse_rejects_two_fingerprints():
    with pytest.raises(ValueError, match="more than one fingerprint"):
        corpus_hash.parse("https://a.test/x|x|ends-sha256=ab|ends-sha256=cd")


def cached(cache: Path, url: str, suffix: str, data: bytes | str) -> None:
    f = cache / f"{hashlib.sha256(url.encode()).hexdigest()}{suffix}"
    f.write_bytes(data) if isinstance(data, bytes) else f.write_text(data)


def test_fingerprint(tmp_path):
    """From the cache alone: a file's size, first and last block; a store's listing."""
    upstream = Upstream(tmp_path)
    body = bytes(range(256)) * (BLOCK // 128 + 3)  # three blocks, the last partial
    blocks = [body[i : i + BLOCK] for i in range(0, len(body), BLOCK)]
    url = "https://a.test/x.tif"
    cached(tmp_path, url, ".size", str(len(body)))
    cached(tmp_path, url, ".0", blocks[0])
    cached(tmp_path, url, ".2", blocks[2])
    want = hashlib.sha256(f"{len(body)}\n".encode() + blocks[0] + blocks[2]).hexdigest()
    assert corpus_hash.fingerprint(upstream, url, cache_only=True) == f"ends-sha256={want}"
    small = "https://a.test/small.dcm"  # one block, hashed once
    cached(tmp_path, small, ".size", "3")
    cached(tmp_path, small, ".0", b"abc")
    want = hashlib.sha256(b"3\nabc").hexdigest()
    assert corpus_hash.fingerprint(upstream, small, cache_only=True) == f"ends-sha256={want}"
    store = "https://a.test/s.zarr/"
    (tmp_path / (hashlib.sha256(f"list:{store}|".encode()).hexdigest() + ".json")).write_text(
        json.dumps([[".zgroup", 24], ["a/0", 7]]))
    want = hashlib.sha256(b".zgroup\t24\na/0\t7\n").hexdigest()
    assert corpus_hash.fingerprint(upstream, store, cache_only=True) == f"listing-sha256={want}"


def test_fingerprint_of_an_uncached_input_is_not_read(tmp_path):
    upstream = Upstream(tmp_path)
    for url in ("https://a.test/x.tif", "https://a.test/s.zarr/"):
        with pytest.raises(corpus_hash.NotCached):
            corpus_hash.fingerprint(upstream, url, cache_only=True)
    cached(tmp_path, "https://a.test/x.tif", ".size", str(3 * BLOCK))
    cached(tmp_path, "https://a.test/x.tif", ".0", b"\0" * BLOCK)  # the last block is not cached
    with pytest.raises(corpus_hash.NotCached):
        corpus_hash.fingerprint(upstream, "https://a.test/x.tif", cache_only=True)
