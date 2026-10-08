"""The pieces of the NDPI access benchmark: slide layouts, chunkings, viewer
traces, read planners and a network model. See README.md.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
SCREEN = (1920, 1080)
MAX_PAYLOAD = 65519
RESPONSE_OVERHEAD = 400  # status line and headers of a response
PART_OVERHEAD = 90  # boundary and part headers of a multipart/byteranges part

# --------------------------------------------------------------------------- layouts


@dataclass
class Level:
    """One level of an NDPI slide: its restart intervals (profiles/ndpi.md §4)."""

    W: int
    H: int
    mw: int
    mh: int
    iw: int  # interval width, R × mw
    a: int  # §4 chunk: intervals across
    b: int  # §4 chunk: intervals down
    off: np.ndarray  # (r, q) file offset of each interval
    ln: np.ndarray  # (r, q) byte length of each interval (without its RST)
    d: float = 1.0  # downsample relative to level 0

    @property
    def q(self) -> int:
        return self.off.shape[1]

    @property
    def r(self) -> int:
        return self.off.shape[0]


@dataclass
class Slide:
    name: str
    levels: list[Level]


def load(name: str) -> Slide:
    z = np.load(CACHE / f"{name}.npz")
    levels = []
    L = 0
    while f"{L}_meta" in z or f"{L}_single" in z:
        if f"{L}_single" in z:
            o, n, W, H = (int(v) for v in z[f"{L}_single"])
            # A level without McuStarts: one chunk, one range. Modelled as a
            # single interval of the whole image.
            levels.append(Level(W, H, W, H, W, 1, 1, np.array([[o]]), np.array([[n]])))
        else:
            W, H, mw, mh, iw, a, b, q, r = (int(v) for v in z[f"{L}_meta"])
            levels.append(Level(W, H, mw, mh, iw, a, b, z[f"{L}_off"], z[f"{L}_len"]))
        L += 1
    for lv in levels:
        lv.d = levels[0].W / lv.W
    return Slide(name, levels)


# --------------------------------------------------------------------------- chunkings


def payload_estimate(n_intervals: int) -> int:
    """§1.2 payload of a §4 chunk reference of n intervals: per interval a
    range part (~12 B) and a literal RST part (~6 B), plus the header parts."""
    return 18 * n_intervals + 64


@dataclass(frozen=True)
class Chunking:
    """Chunk shape per level, in intervals across (a) and MCU rows down (b)."""

    name: str
    shape: object  # callable(Level) -> (a, b)

    def ab(self, lv: Level) -> tuple[int, int]:
        if lv.q == 1 and lv.r == 1:
            return 1, 1
        a, b = self.shape(lv)
        a = max(1, min(a, lv.q))
        b = max(1, min(b, lv.r))
        while b > 1 and payload_estimate(a * b) > MAX_PAYLOAD:
            b -= 1
        return a, b


def _spec(lv: Level) -> tuple[int, int]:
    return lv.a, lv.b


def _px(w: int, h: int):
    return lambda lv: (max(1, w // lv.iw), max(1, h // lv.mh))


def _band(px: int):
    """Full-width bands of about `px` pixels (rounded to whole MCU rows)."""
    return lambda lv: (lv.q, max(1, round(px / (lv.W * lv.mh))))


def _adaptive(lv: Level) -> tuple[int, int]:
    """Full-width bands while a level is at most two screens wide, else 1024²."""
    if lv.W <= 2 * SCREEN[0]:
        return _band(1 << 20)(lv)
    return lv.a, lv.b


def _adaptive_wide(lv: Level) -> tuple[int, int]:
    if lv.W <= 2 * SCREEN[0]:
        return _band(1 << 20)(lv)
    return _px(4096, 256)(lv)


CHUNKINGS = {
    "spec": Chunking("spec 1024² (§4)", _spec),
    "w4096x256": Chunking("4096×256", _px(4096, 256)),
    "w8192x128": Chunking("8192×128", _px(8192, 128)),
    "w2048x512": Chunking("2048×512", _px(2048, 512)),
    "band1M": Chunking("full-width band, ~1 Mpx", _band(1 << 20)),
    "adaptive": Chunking("bands if W ≤ 2 screens, else §4", _adaptive),
    "adaptive_wide": Chunking("bands if W ≤ 2 screens, else 4096×256", _adaptive_wide),
    # not a proposal: one interval per chunk, so that `interval:ideal` is the
    # bytes under the visible region in one request per view (the bound)
    "interval": Chunking("one interval", lambda lv: (1, 1)),
}


@dataclass(frozen=True)
class Chunk:
    level: int
    u: int
    v: int


def chunk_ranges(lv: Level, a: int, b: int, u: int, v: int) -> np.ndarray:
    """The interval byte ranges [start, end) of chunk (u, v), in reference
    order (clamped repeats at the edges dropped: they are the same bytes)."""
    y0, y1 = u * b, min(u * b + b, lv.r)
    x0, x1 = v * a, min(v * a + a, lv.q)
    o = lv.off[y0:y1, x0:x1].ravel()
    n = lv.ln[y0:y1, x0:x1].ravel()
    return np.stack([o, o + n], axis=1)


def chunk_pixels(lv: Level, a: int, b: int, u: int, v: int) -> int:
    """Pixels decoded for the chunk, clipped to the image (§4: the edge
    repeats lie outside the array and are not read)."""
    w = min((v + 1) * a * lv.iw, lv.W) - v * a * lv.iw
    h = min((u + 1) * b * lv.mh, lv.H) - u * b * lv.mh
    return w * h


# --------------------------------------------------------------------------- traces


@dataclass
class View:
    level: int
    x0: int  # visible region in level pixels, [x0, x1) × [y0, y1)
    x1: int
    y0: int
    y1: int
    label: str = ""


def pick_level(slide: Slide, z: float) -> int:
    """The coarsest level with at least one data pixel per screen pixel at
    zoom z (level-0 pixels per screen pixel), as Neuroglancer's default."""
    best = 0
    for i, lv in enumerate(slide.levels):
        if lv.d <= z * 1.0001:
            best = i
    return best


def view_at(slide: Slide, cx: float, cy: float, z: float, label: str = "") -> View:
    L = pick_level(slide, z)
    lv = slide.levels[L]
    s = z / lv.d  # level pixels per screen pixel
    hw, hh = SCREEN[0] * s / 2, SCREEN[1] * s / 2
    x0, x1 = max(0, int(cx / lv.d - hw)), min(lv.W, int(math.ceil(cx / lv.d + hw)))
    y0, y1 = max(0, int(cy / lv.d - hh)), min(lv.H, int(math.ceil(cy / lv.d + hh)))
    return View(L, x0, x1, y0, y1, label)


def point_of_interest(slide: Slide) -> tuple[float, float]:
    """Level-0 coordinates of the densest tissue: the interval with the most
    compressed bytes in a middle level (JPEG of blank glass is small)."""
    lv = slide.levels[len(slide.levels) // 2]
    if lv.q == 1:
        lv = slide.levels[0]
    c = np.cumsum(np.vstack([np.zeros((1, lv.q)), lv.ln.astype(float)]), axis=0)
    dens = c[16:] - c[:-16] if lv.r > 16 else c[1:]  # 16 MCU rows
    y, x = np.unravel_index(int(np.argmax(dens)), dens.shape)
    return (x + 0.5) * lv.iw * lv.d, (y + 0.5) * lv.mh * lv.d


def traces(slide: Slide) -> dict[str, list[View]]:
    W0, H0 = slide.levels[0].W, slide.levels[0].H
    zfit = max(W0 / SCREEN[0], H0 / SCREEN[1])
    px, py = point_of_interest(slide)
    out: dict[str, list[View]] = {"overview": [view_at(slide, W0 / 2, H0 / 2, zfit, "fit")]}
    zoom, z = [], zfit
    while z > 1:
        z = max(1.0, z / 2)
        zoom.append(view_at(slide, px, py, z, f"z={z:.3g}"))
    out["zoom"] = zoom
    mid = slide.levels[min(len(slide.levels) - 1, max(1, len(slide.levels) // 2 - 1))].d
    for name, z in (("pan_L0", 1.0), ("pan_mid", mid)):
        # 12 half-screen steps across, then 12 down, each toward the side of
        # the point of interest with more room, staying on the slide
        sx = 1 if px < W0 / 2 else -1
        sy = 1 if py < H0 / 2 else -1
        x = min(max(px, SCREEN[0] / 2 * z), W0 - SCREEN[0] / 2 * z)
        y = min(max(py, SCREEN[1] / 2 * z), H0 - SCREEN[1] / 2 * z)
        views = []
        for i in range(12):
            views.append(view_at(slide, x, y, z, f"x{i}"))
            x = min(max(x + sx * SCREEN[0] / 2 * z, SCREEN[0] / 2 * z), W0 - SCREEN[0] / 2 * z)
        for i in range(12):
            views.append(view_at(slide, x, y, z, f"y{i}"))
            y = min(max(y + sy * SCREEN[1] / 2 * z, SCREEN[1] / 2 * z), H0 - SCREEN[1] / 2 * z)
        out[name] = views
    return out


def view_chunks(slide: Slide, ch: Chunking, v: View) -> list[Chunk]:
    lv = slide.levels[v.level]
    a, b = ch.ab(lv)
    cw, chh = a * lv.iw, b * lv.mh
    return [Chunk(v.level, u, w)
            for u in range(v.y0 // chh, (v.y1 - 1) // chh + 1)
            for w in range(v.x0 // cw, (v.x1 - 1) // cw + 1)]


# --------------------------------------------------------------------------- network


@dataclass(frozen=True)
class Net:
    name: str
    rtt: float  # seconds: request out, server time, first byte back
    bw: float  # bytes per second, shared by all transfers
    conns: int  # requests in flight at once (6 for HTTP/1.1, ~100 streams for HTTP/2)
    multirange: bool = False


NETS = {
    "h1-20ms-50M": Net("HTTP/1.1×6, 20 ms, 50 Mbit/s", 0.020, 50e6 / 8, 6),
    "h1-80ms-50M": Net("HTTP/1.1×6, 80 ms, 50 Mbit/s", 0.080, 50e6 / 8, 6),
    "h1-20ms-500M": Net("HTTP/1.1×6, 20 ms, 500 Mbit/s", 0.020, 500e6 / 8, 6),
    "h1-80ms-500M": Net("HTTP/1.1×6, 80 ms, 500 Mbit/s", 0.080, 500e6 / 8, 6),
    "h2-20ms-50M": Net("HTTP/2×100, 20 ms, 50 Mbit/s", 0.020, 50e6 / 8, 100),
    "h2-80ms-500M": Net("HTTP/2×100, 80 ms, 500 Mbit/s", 0.080, 500e6 / 8, 100),
    # openslide.cs.cmu.edu as measured by probe_hosts.py (HTTP/1.1, no multi-range)
    "openslide": Net("openslide.cs.cmu.edu: HTTP/1.1×6, 500 ms, 5 Mbit/s", 0.500, 5.3e6 / 8, 6),
}


def simulate(sizes: list[int], net: Net) -> float:
    """Time until the last of `sizes` (response bytes, issued together at t=0,
    in order) arrives. Each request holds one of `net.conns` slots for one RTT
    then for its transfer; transfers share the bandwidth equally (processor
    sharing, so TCP slow start and per-connection windows are ignored)."""
    if not sizes:
        return 0.0
    pending = list(sizes)[::-1]
    t = 0.0
    waiting: list[tuple[float, int]] = []  # (time the first byte arrives, bytes)
    # transfers in progress, as finishing tags in virtual time: every transfer
    # receives V' = bw / n bytes per second
    vt, active = 0.0, []

    def launch(now: float) -> None:
        heapq.heappush(waiting, (now + net.rtt, pending.pop()))

    for _ in range(min(net.conns, len(pending))):
        launch(0.0)
    while waiting or active:
        n = len(active)
        t_fin = t + (active[0] - vt) * n / net.bw if n else math.inf
        t_wait = waiting[0][0] if waiting else math.inf
        if t_wait <= t_fin:
            if n:
                vt += (t_wait - t) * net.bw / n
            t = t_wait
            _, size = heapq.heappop(waiting)
            heapq.heappush(active, vt + size)
        else:
            vt = heapq.heappop(active)
            t = t_fin
            if pending:
                launch(t)
    return t


# --------------------------------------------------------------------------- planners


def merge(r: np.ndarray, gap: float) -> np.ndarray:
    """Sorted, de-duplicated runs covering the ranges r, joining runs whose
    gap is at most `gap` bytes."""
    if len(r) == 0:
        return r
    r = r[np.lexsort((r[:, 1], r[:, 0]))]
    ends = np.maximum.accumulate(r[:, 1])
    start_new = np.ones(len(r), bool)
    start_new[1:] = r[1:, 0] - ends[:-1] > gap
    idx = np.flatnonzero(start_new)
    starts = r[idx, 0]
    last = np.r_[idx[1:] - 1, len(r) - 1]
    return np.stack([starts, ends[last]], axis=1)


def optimal_merge(r: np.ndarray, net: Net, discount: float = 1.0) -> np.ndarray:
    """Merges the smallest gaps first, as many as minimise
    ceil(N / conns) · rtt + (needed + discount · gap bytes) / bw (N requests).
    A discount below 1 prices in that the gap bytes (the same MCU rows'
    other intervals) may be read later from a cache."""
    runs = merge(r, 2)  # the dropped RST markers are always worth merging
    if len(runs) <= 1:
        return runs
    gaps = runs[1:, 0] - runs[:-1, 1]
    order = np.sort(gaps)
    base = int((runs[:, 1] - runs[:, 0]).sum())
    k = np.arange(len(order) + 1)
    cost = np.ceil((len(runs) - k) / net.conns) * net.rtt + (base + discount * np.r_[0, np.cumsum(order)]) / net.bw
    best = int(np.argmin(cost))
    if best == 0:
        return runs
    return merge(runs, order[best - 1])


@dataclass
class Plan:
    requests: list[list[tuple[int, int]]] = field(default_factory=list)  # spans per request

    def sizes(self) -> list[int]:
        out = []
        for spans in self.requests:
            body = sum(e - s for s, e in spans)
            out.append(body + RESPONSE_OVERHEAD + (PART_OVERHEAD * len(spans) if len(spans) > 1 else 0))
        return out

    def bytes(self) -> int:
        return sum(e - s for spans in self.requests for s, e in spans)


class Planner:
    """Turns the chunk reads of one view into HTTP requests. Stateful planners
    (caches) keep state across the views of a trace."""

    name = "?"

    def plan(self, ranges: list[np.ndarray], net: Net, lv: Level) -> Plan:
        raise NotImplementedError


class PerChunk(Planner):
    """Each chunk's reference is read alone, merging its ranges whose gap is
    at most `gap` bytes: what store.py and archive.ts do (gap 64 KiB)."""

    def __init__(self, gap: float | str):
        self.gap = gap
        self.name = f"per-chunk gap={_fmt_gap(gap)}"

    def plan(self, ranges, net, lv):
        if self.gap == "opt":  # the cost model, as if the chunk were read alone
            return Plan([[tuple(run)] for r in ranges for run in optimal_merge(r, net)])
        g = _gap(self.gap, net)
        return Plan([[tuple(run)] for r in ranges for run in merge(r, g)])


class Batched(Planner):
    """All chunk reads of a view planned together (a batching window),
    merging across chunks."""

    def __init__(self, gap: float | str, discount: float = 1.0):
        self.gap, self.discount = gap, discount
        self.name = f"batched gap={_fmt_gap(gap)}" + (f" discount={discount}" if discount != 1 else "")

    def plan(self, ranges, net, lv):
        if not ranges:
            return Plan()
        r = np.concatenate(ranges)
        runs = (optimal_merge(r, net, self.discount) if self.gap == "opt"
                else merge(r, _gap(self.gap, net)))
        return Plan([[tuple(run)] for run in runs])


class MultiRange(Planner):
    """Batched, then the runs packed into multi-range requests (at most
    `max_ranges` each, and at least one request per connection)."""

    def __init__(self, gap: float | str = 2, max_ranges: int = 200, per_chunk: bool = False):
        self.gap, self.max_ranges, self.per_chunk = gap, max_ranges, per_chunk
        self.name = f"multi-range{' per-chunk' if per_chunk else ' batched'} gap={_fmt_gap(gap)}"

    def plan(self, ranges, net, lv):
        if not net.multirange:
            inner = PerChunk(self.gap) if self.per_chunk else Batched(self.gap)
            return inner.plan(ranges, net, lv)
        groups = ranges if self.per_chunk else ([np.concatenate(ranges)] if ranges else [])
        out = Plan()
        for r in groups:
            runs = merge(r, _gap(self.gap, net))
            n = max(-(-len(runs) // self.max_ranges), 1 if self.per_chunk else min(net.conns, len(runs)))
            for part in np.array_split(runs, n):
                if len(part):
                    out.requests.append([tuple(x) for x in part])
        return out


class BlockCache(Planner):
    """Reads in aligned blocks kept in a cache shared by all chunks and views
    (web/src/virtualize/common.ts blockReader: one request per block); with
    join=True, missing blocks next to each other share a request."""

    def __init__(self, block: int, join: bool = False):
        self.block, self.join = block, join
        self.name = f"block cache {block >> 10} KiB{' joined' if join else ''}"
        self.have: set[int] = set()

    def plan(self, ranges, net, lv):
        if not ranges:
            return Plan()
        r = merge(np.concatenate(ranges), 0)
        B = self.block
        need: set[int] = set()
        for s, e in r:
            need.update(range(int(s) // B, (int(e) - 1) // B + 1))
        new = sorted(need - self.have)
        self.have |= need
        out = Plan()
        for i in new:
            if self.join and out.requests and out.requests[-1][-1][1] == i * B:
                out.requests[-1][-1] = (out.requests[-1][-1][0], (i + 1) * B)
            else:
                out.requests.append([(i * B, (i + 1) * B)])
        return out


class RowCache(Planner):
    """Read-ahead of whole MCU rows: any interval read fetches its full-width
    row of the level into a cache shared across chunks and views. The rows a
    view needs are contiguous in the file, so they go in one batched,
    merged request set."""

    name = "whole-row read-ahead"

    def __init__(self):
        self.have: set[tuple[int, int]] = set()  # (row start, row end) spans

    def plan(self, ranges, net, lv):
        if not ranges:
            return Plan()
        r = np.concatenate(ranges)
        starts = lv.off[:, 0]
        ends = lv.off[:, -1] + lv.ln[:, -1]
        rows = set((np.searchsorted(starts, r[:, 0], side="right") - 1).tolist())
        need = {(int(starts[y]), int(ends[y])) for y in rows} - self.have
        self.have |= need
        if not need:
            return Plan()
        runs = merge(np.array(sorted(need)), 2)
        return Plan([[tuple(x)] for x in runs])


class Ideal(Planner):
    """A lower bound: each view's bytes, exactly, in one request."""

    name = "ideal (one exact request per view)"

    def plan(self, ranges, net, lv):
        if not ranges:
            return Plan()
        return Plan([[tuple(x) for x in merge(np.concatenate(ranges), 2)]])


class SpanCache(Planner):
    """Keeps every byte span fetched (not only the bytes a chunk asked for:
    the merged gaps too, which are other chunks' intervals of the same MCU
    rows) and plans only the ranges not yet covered, with `inner`. No
    eviction is modelled."""

    def __init__(self, inner: Planner):
        self.inner = inner
        self.name = f"{inner.name} + span cache"
        self.spans = np.zeros((0, 2), np.int64)

    def covered(self, r: np.ndarray) -> np.ndarray:
        if len(self.spans) == 0 or len(r) == 0:
            return np.zeros(len(r), bool)
        i = np.searchsorted(self.spans[:, 0], r[:, 0], side="right") - 1
        ok = i >= 0
        out = np.zeros(len(r), bool)
        out[ok] = self.spans[i[ok], 1] >= r[ok, 1]
        return out

    def plan(self, ranges, net, lv):
        todo = [r[~self.covered(r)] for r in ranges]
        todo = [r for r in todo if len(r)]
        p = self.inner.plan(todo, net, lv)
        new = [s for spans in p.requests for s in spans]
        if new:
            self.spans = merge(np.concatenate([self.spans, np.array(new, np.int64)]), 0)
        return p


def _gap(g, net: Net) -> float:
    if g == "bdp":  # merge when gap / bw < rtt
        return net.bw * net.rtt
    if g == "bdp/c":  # ... per connection: a request costs rtt / conns of the view
        return net.bw * net.rtt / net.conns
    return float(g)


def _fmt_gap(g) -> str:
    if isinstance(g, str):
        return g
    return f"{int(g) >> 10} KiB" if g >= 1024 else f"{int(g)} B"
