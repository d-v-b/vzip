"""Read planning for url sources (vzip.readplan) and VZipStore's flags for it."""

import asyncio
from pathlib import Path

import pytest
from zarr.core.buffer import default_buffer_prototype

from vzip.readplan import NetModel, SpanCache, cost_merge_runs, merge_runs
from vzip.store import Stats, VZipStore
from vzip.uri import file_uri
from vzip.virtualize import virtualize

NDPI = Path(__file__).parents[1] / "web/test/fixtures/ndpi/ndpi_levels.ndpi"


def test_plans():
    reads = [(0, 0, 10), (0, 12, 20), (0, 100, 110), (1, 5, 6), (0, 0, 10)]
    assert merge_runs(reads, 0) == [[0, 0, 10], [0, 12, 20], [0, 100, 110], [1, 5, 6]]
    assert merge_runs(reads, 2) == [[0, 0, 20], [0, 100, 110], [1, 5, 6]]
    assert merge_runs(reads, 80) == [[0, 0, 110], [1, 5, 6]]
    # a gap is worth fetching when it costs less than a request: with 1 connection,
    # 1 s per request and 50 bytes per second, gaps up to 50 bytes
    slow = NetModel(rtt=1.0, bandwidth=50, conns=1)
    assert cost_merge_runs(reads, slow) == [[0, 0, 20], [0, 100, 110], [1, 5, 6]]
    assert cost_merge_runs(reads, NetModel(rtt=1.0, bandwidth=1e6, conns=1)) == [[0, 0, 110], [1, 5, 6]]
    assert cost_merge_runs(reads, NetModel(rtt=1e-6, bandwidth=1, conns=100)) == merge_runs(reads, 2)
    assert NetModel(rtt=0.02, bandwidth=6.25e6, conns=6).gap() == pytest.approx(20833.3, rel=1e-4)
    cache = SpanCache(30)
    cache.put(0, 100, 120, bytes(range(20)))
    cache.put(0, 0, 10, bytes(10))
    assert cache.get(0, 105, 110) == bytes(range(5, 10))
    assert cache.get(0, 105, 121) is None and cache.get(1, 105, 110) is None
    cache.put(0, 200, 210, bytes(10))  # 40 bytes: the least recently used span goes
    assert cache.get(0, 0, 10) is None and cache.get(0, 100, 101) == b"\x00"


def test_store_read_planning(tmp_path):
    _, out = virtualize(str(NDPI), url=file_uri(str(NDPI)))
    path = tmp_path / "ndpi.vzip"
    out.write(str(path))
    keys = sorted(k for k in out.refs if k.startswith("0/c/"))

    async def read_all(**flags) -> tuple[list[bytes], int]:
        stats = Stats()
        s = VZipStore(str(path), stats=stats, **flags)
        proto = default_buffer_prototype()
        await s.get("zarr.json", proto)  # open the store before the concurrent reads
        values = await asyncio.gather(*[s.get(k, proto) for k in keys])
        return [v.to_bytes() for v in values], stats.external_requests

    base, n_base = asyncio.run(read_all())
    exact, n_exact = asyncio.run(read_all(merge_gap=0))
    batched, n_batched = asyncio.run(read_all(merge_gap=2, batch_window=0.005, span_cache=1 << 20))
    for flags in ({"merge_gap": "auto"}, {"merge_gap": "cost"},
                  {"merge_gap": "cost", "net": NetModel(0.08, 6e7, 6), "batch_window": 0.0}):
        assert asyncio.run(read_all(**flags))[0] == base, flags
    assert exact == base and batched == base
    # Level 0 is 10 × 150 intervals in chunks of 8 × 128 (the JPEG header is
    # in data sources, read without requests). Read alone with the 64 KiB
    # gap, each chunk is one request; with a gap of 0, every interval is one.
    # Batched, the four chunks cover whole rows, which are contiguous but for
    # the 2-byte restart markers: the whole level in one request.
    assert (n_base, n_exact, n_batched) == (4, 1500, 1)


def test_store_rejects_unknown_merge_gap():
    with pytest.raises(ValueError, match="merge_gap"):
        VZipStore("x.vzip", merge_gap="fast")
