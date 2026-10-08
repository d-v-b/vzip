"""Read planning for url sources: merging nearby byte ranges into requests,
batching the reads of concurrent values, and caching fetched spans.

None of this changes what a value is (spec §6.2 allows a reader to fetch
more than a range and to combine ranges). See experiments/ndpi_access/ for
the measurements behind the defaults.
"""

from __future__ import annotations

import asyncio
import bisect
import math
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

Read = tuple[int, int, int]  # (source, start, end)


def merge_runs(reads: Iterable[Read], gap: float) -> list[list[int]]:
    """Sorted runs [source, start, end] covering `reads`, joining reads of a
    source at most `gap` bytes apart."""
    runs: list[list[int]] = []
    for s, a, b in sorted(set(reads)):
        if runs and runs[-1][0] == s and a - runs[-1][2] <= gap:
            runs[-1][2] = max(runs[-1][2], b)
        else:
            runs.append([s, a, b])
    return runs


@dataclass
class NetModel:
    """What a request costs: `rtt` seconds, then `bandwidth` bytes per second
    shared by all transfers, with `conns` requests in flight at once."""

    rtt: float = 0.05
    bandwidth: float = 12.5e6
    conns: int = 6

    def gap(self) -> float:
        """The gap worth fetching rather than paying for another request:
        a request costs about rtt / conns of a batch's time, a gap byte 1 / bw."""
        return self.bandwidth * self.rtt / self.conns


def cost_merge_runs(reads: Iterable[Read], net: NetModel) -> list[list[int]]:
    """Runs covering `reads`, merging the smallest gaps first, as many as
    minimise ceil(N / conns) · rtt + bytes / bandwidth for N requests."""
    runs = merge_runs(reads, 2)  # adjacent ranges, or a dropped 2-byte marker apart
    gaps = sorted(runs[i + 1][1] - runs[i][2] for i in range(len(runs) - 1)
                  if runs[i + 1][0] == runs[i][0])
    if not gaps:
        return runs
    total = sum(r[2] - r[1] for r in runs)
    best, best_cost, extra = 0, math.inf, 0
    for k in range(len(gaps) + 1):
        if k:
            extra += gaps[k - 1]
        cost = math.ceil((len(runs) - k) / net.conns) * net.rtt + (total + extra) / net.bandwidth
        if cost < best_cost:
            best, best_cost = k, cost
    return merge_runs([tuple(r) for r in runs], gaps[best - 1]) if best else runs


class NetEstimate:
    """A running estimate of a host's NetModel from the reader's own requests:
    the time of small requests gives the RTT, large ones the bandwidth."""

    def __init__(self, prior: NetModel | None = None, conns: int = 6) -> None:
        p = prior or NetModel(conns=conns)
        self.rtt, self.bandwidth, self.conns = p.rtt, p.bandwidth, p.conns
        self.samples = 0

    def observe(self, n: int, seconds: float) -> None:
        self.samples += 1
        if n <= 16384:
            self.rtt = 0.8 * self.rtt + 0.2 * seconds
        elif seconds > self.rtt:
            bw = n / (seconds - self.rtt)
            self.bandwidth = 0.8 * self.bandwidth + 0.2 * bw

    def model(self) -> NetModel:
        return NetModel(self.rtt, self.bandwidth, self.conns)


class SpanCache:
    """Fetched byte spans of url sources, least recently used evicted past
    `capacity` bytes. Spans may overlap; a read is served by any one span
    that contains it."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.size = 0
        self._lru: OrderedDict[Read, bytes] = OrderedDict()
        self._starts: dict[int, list[tuple[int, int]]] = {}  # source -> sorted (start, end)

    def get(self, s: int, a: int, b: int) -> bytes | None:
        spans = self._starts.get(s)
        if not spans:
            return None
        i = bisect.bisect_right(spans, (a, math.inf)) - 1
        # spans starting at or before a; the longest reach is not always the
        # nearest, so look back over a few
        for j in range(i, max(-1, i - 8), -1):
            sa, sb = spans[j]
            if sb >= b:
                key = (s, sa, sb)
                self._lru.move_to_end(key)
                data = self._lru[key]
                return data[a - sa : b - sa]
        return None

    def put(self, s: int, a: int, b: int, data: bytes) -> None:
        if b - a > self.capacity or (s, a, b) in self._lru:
            return
        self._lru[(s, a, b)] = data
        bisect.insort(self._starts.setdefault(s, []), (a, b))
        self.size += b - a
        while self.size > self.capacity:
            (os_, oa, ob), _ = self._lru.popitem(last=False)
            self._starts[os_].remove((oa, ob))
            self.size -= ob - oa


class Batcher:
    """Collects the url reads of values requested together (within `window`
    seconds of the first) and plans them jointly, so that the rows of
    neighbouring chunks become one request per row (or fewer).

    `fetch(source, start, end)` performs one request; `plan(reads)` turns the
    batch's reads into runs."""

    def __init__(self, window: float, fetch: Callable[[int, int, int], Awaitable[bytes]],
                 plan: Callable[[list[Read]], list[list[int]]],
                 cache: SpanCache | None = None) -> None:
        self.window, self.fetch, self.plan, self.cache = window, fetch, plan, cache
        self._pending: list[tuple[Read, asyncio.Future]] = []
        self._timer: asyncio.Task | None = None
        self._inflight: list[tuple[Read, asyncio.Future]] = []

    async def read(self, s: int, a: int, b: int) -> bytes:
        if self.cache is not None:
            hit = self.cache.get(s, a, b)
            if hit is not None:
                return hit
        for (rs, ra, rb), fut in self._inflight:  # a request already on its way
            if rs == s and ra <= a and b <= rb:
                return (await asyncio.shield(fut))[a - ra : b - ra]
        fut = asyncio.get_running_loop().create_future()
        self._pending.append(((s, a, b), fut))
        if self._timer is None:
            self._timer = asyncio.ensure_future(self._flush_later())
        return await fut

    async def _flush_later(self) -> None:
        await asyncio.sleep(self.window)
        batch, self._pending, self._timer = self._pending, [], None
        runs = self.plan([r for r, _ in batch])
        loop = asyncio.get_running_loop()
        futs = []
        for s, a, b in runs:
            f = loop.create_future()
            self._inflight.append(((s, a, b), f))
            futs.append(((s, a, b), f))

        async def one(run: Read, f: asyncio.Future) -> None:
            try:
                data = await self.fetch(*run)
                if self.cache is not None:
                    self.cache.put(*run, data)
                f.set_result(data)
            except BaseException as e:  # noqa: BLE001 - handed to the readers of this run
                f.set_exception(e)
            finally:
                self._inflight.remove((run, f))

        tasks = [asyncio.ensure_future(one(run, f)) for run, f in futs]
        for (s, a, b), fut in batch:
            run, f = next(((r, f) for r, f in futs if r[0] == s and r[1] <= a and b <= r[2]))
            _deliver(fut, f, a - run[1], b - run[1])
        await asyncio.gather(*tasks, return_exceptions=True)
        for _, f in futs:  # retrieve exceptions nobody awaited
            if f.done() and not f.cancelled():
                f.exception()


def _deliver(dst: asyncio.Future, src: asyncio.Future, lo: int, hi: int) -> None:
    def done(f: asyncio.Future) -> None:
        if dst.done():
            return
        if f.exception() is not None:
            dst.set_exception(f.exception())
        else:
            dst.set_result(f.result()[lo:hi])

    src.add_done_callback(done)
