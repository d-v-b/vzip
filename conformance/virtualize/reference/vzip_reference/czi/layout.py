"""Series, layers, levels and tiles (conventions/czi/README.md §3.3–§4.4)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# libCZI's pyramid layer tables (conventions/czi/README.md §3.4): (v, delta, n).
LAYERS_2 = [(2, 0.1, 1), (4, 0.2, 2), (8, 0.4, 3), (16, 0.8, 4), (32, 1, 5), (64, 1, 6), (128, 1, 7), (256, 2, 8),
            (512, 4, 9), (1024, 10, 10)]
LAYERS_3 = [(3, 0.1, 1), (9, 0.2, 2), (27, 0.8, 3), (81, 1.5, 4), (243, 2, 5), (729, 5, 6), (2187, 15, 7)]
SERIES_LETTERS = "SBHIRV"
MAX_BAND = 1 << 24  # the most bytes of an uncompressed chunk, when a tile is larger (row bands)


def layer(wl: int, hl: int, w: int, h: int) -> tuple[int, int] | None:
    """A subblock's layer from its logical and stored sizes, or None."""
    if wl == w and hl == h:
        return (1, 0)
    f = wl / w if w > h else hl / h
    for table, base in ((LAYERS_2, 2), (LAYERS_3, 3)):
        for v, delta, n in table:
            if v - delta <= f <= v + delta:
                return (base, n)
    return None


@dataclass
class Placed:
    """A placed subblock (conventions/czi/README.md §3.2)."""
    index: int
    series: tuple
    plane: tuple[int, int, int]  # (t, c, z)
    x: int
    y: int
    wl: int
    hl: int
    w: int  # stored
    h: int
    cw: int  # coded
    ch: int
    form: tuple[int, int, bool]  # (PixelType, Compression, hi-lo)
    header: int  # the Zstd1 header's length
    layer: tuple[int, int] | None

    @property
    def conforming(self) -> bool:
        return (self.cw, self.ch) == (self.w, self.h)


@dataclass
class Level:
    layer: tuple[int, int]
    form: tuple[int, int, bool]
    factor: int
    tile: tuple[int, int]  # (W, H)
    edge: tuple[int, int]  # (W', H')
    origin: tuple[int, int]  # (x0, y0)
    grid: tuple[int, int]  # (m columns, r rows)
    cells: list[tuple[Placed, int, int]] = field(default_factory=list)  # (subblock, column, row)


def classify(b: list[Placed]) -> Level | None:
    """The level `b` is when it is regular (conventions/czi/README.md §3.5), else None."""
    forms = {s.form for s in b}
    if len(forms) != 1:
        return None
    wl, hl = max(s.wl for s in b), max(s.hl for s in b)
    w, h = max(s.w for s in b), max(s.h for s in b)
    if wl % w or hl % h or wl // w != hl // h:
        return None
    x0, y0 = min(s.x for s in b), min(s.y for s in b)
    cells, seen = [], set()
    for s in b:
        if (s.x - x0) % wl or (s.y - y0) % hl:
            return None
        i, j = (s.x - x0) // wl, (s.y - y0) // hl
        if (s.plane, i, j) in seen:
            return None
        seen.add((s.plane, i, j))
        cells.append((s, i, j))
    m, r = max(i for _, i, _ in cells) + 1, max(j for _, _, j in cells) + 1
    last_col, last_row = set(), set()
    for s, i, j in cells:
        if i < m - 1 and (s.wl, s.w) != (wl, w):
            return None
        if j < r - 1 and (s.hl, s.h) != (hl, h):
            return None
        if i == m - 1:
            last_col.add((s.wl, s.w))
        if j == r - 1:
            last_row.add((s.hl, s.h))
    if len(last_col) != 1 or len(last_row) != 1:
        return None
    edge = (next(iter(last_col))[1], next(iter(last_row))[1])
    return Level(b[0].layer, b[0].form, wl // w, (w, h), edge, (x0, y0), (m, r), cells)


def row_band(h: int, h2: int, w: int, q: int) -> int:
    """The rows of a band of an uncompressed tile of w x h (edge height h2) of q-byte pixels:
    h itself when the tile is at most 2^24 bytes, else the largest divisor of gcd(h, h2)
    whose band is at most 2^24 bytes, or 1."""
    if w * h * q <= MAX_BAND:
        return h
    g = math.gcd(h, h2)
    best = 1
    d = 1
    while d * d <= g:
        if g % d == 0:
            for v in (d, g // d):
                if v * w * q <= MAX_BAND and v > best:
                    best = v
        d += 1
    return best


def plane_axes(planes, p: int) -> tuple[list[str], dict[str, int], dict[str, int]]:
    """The axes of an image or tile array over its subblocks' planes, the least
    t, c, z, and the extent along each (conventions/czi/README.md §4.2)."""
    lo = {a: min(pl[k] for pl in planes) for k, a in enumerate("tcz")}
    hi = {a: max(pl[k] for pl in planes) for k, a in enumerate("tcz")}
    axes = (["t"] if hi["t"] > lo["t"] else []) + (["c"] if hi["c"] > lo["c"] or p > 1 else []) + (
        ["z"] if hi["z"] > lo["z"] else []) + ["y", "x"]
    extent = {a: hi[a] - lo[a] + 1 for a in "tcz"}
    return axes, lo, extent
