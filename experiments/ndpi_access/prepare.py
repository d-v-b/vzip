"""Virtualizes the corpus NDPI slides and extracts their interval layout.

For each slide, runs the browser virtualizer (web/conformance/virtualize.ts)
on the slide's URL behind the caching proxy (conformance/virtualize/proxy.py),
then reads every chunk reference back with vzip's Python reader and rebuilds,
per level, the table of restart intervals of profiles/ndpi.md §4: the file
offset and byte length of interval (row, column), plus the header ranges.

Outputs, under experiments/ndpi_access/cache/ (git-ignored):

- `<slide>.vzip`: the virtualized slide;
- `<slide>.npz`: per level `L`, arrays `L_off` and `L_len` (r × q, int64),
  and `L_meta` = [W, H, mw, mh, interval width R·mw, a, b, q, r].

Usage:
    uv run python experiments/ndpi_access/prepare.py [--proxy http://127.0.0.1:8791] [slide ...]

The proxy is started on the given port if nothing answers there.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CACHE = HERE / "cache"
SLIDES = {
    "CMU-1": "https://openslide.cs.cmu.edu/download/openslide-testdata/Hamamatsu/CMU-1.ndpi",
    "Hamamatsu-1": "https://openslide.cs.cmu.edu/download/openslide-testdata/Hamamatsu/Hamamatsu-1.ndpi",
}


def proxied(proxy: str, url: str) -> str:
    i = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"{proxy}/u/{i}/{url.rsplit('/', 1)[1]}"


def ensure_proxy(proxy: str) -> None:
    p = urlparse(proxy)
    with socket.socket() as s:
        if s.connect_ex((p.hostname, p.port)) == 0:
            return
    subprocess.Popen(
        [sys.executable, str(ROOT / "conformance/virtualize/proxy.py"), str(ROOT / "web/test/fixtures"),
         "/tmp/vzip-proxy-cache", str(p.port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(1.0)


async def extract(vzip: Path) -> dict[str, np.ndarray]:
    from vzip.pb import parts
    from vzip.store import VZipStore

    s = VZipStore(str(vzip))
    await s._ensure_open()
    keys = [k async for k in s.list()]
    out: dict[str, np.ndarray] = {}
    levels = sorted({int(k.split("/")[0]) for k in keys if k.split("/")[0].isdigit()})
    for L in levels:
        await s._lookup(f"{L}/zarr.json")
        e = s._entries[f"{L}/zarr.json"]
        z = json.loads(await s._body(e, 0, e.size))
        _, H, W = z["shape"]
        _, ch, cw = z["chunk_grid"]["configuration"]["chunk_shape"]
        nu, nv = -(-H // ch), -(-W // cw)
        first = None
        cells: dict[tuple[int, int], tuple[int, int]] = {}
        header: list[tuple[int, int]] = []
        geom = None
        for u in range(nu):
            for v in range(nv):
                key = f"{L}/c/0/{u}/{v}"
                await s._lookup(key)
                ps = parts(s._entries[key].ref)
                if len(ps) == 1 and ps[0].data is None:  # a level without McuStarts: one chunk, one range
                    geom = None
                    first = ps[0]
                    continue
                # url-source ranges are the intervals; the header's pieces are
                # literals or (VIRTUALIZE.md revision 14) data sources
                def is_url(p):
                    return p.data is None and s._sources[p.source].url is not None

                if geom is None:
                    head = b"".join(p.data if p.data is not None else
                                    s._sources[p.source].data[p.offset:p.offset + p.size]
                                    for p in ps[:3] if not is_url(p))
                    at = head.index(b"\xff\xc0")
                    sof = head[at:]  # the SOF0 segment of §4 step 2
                    hmax = max(sof[11 + 3 * i] >> 4 for i in range(sof[9]))
                    vmax = max(sof[11 + 3 * i] & 15 for i in range(sof[9]))
                    mw, mh = 8 * hmax, 8 * vmax
                    b, a_px = ch // mh, cw
                    header = [(p.offset, p.size) for p in ps[:3] if is_url(p)]
                    geom = (mw, mh, b)
                mw, mh, b = geom
                ivs = [p for p in ps if is_url(p) and (p.offset, p.size) not in header]
                a = len(ivs) // b
                iw = cw // a
                q, r = -(-W // iw), -(-H // mh)
                for k, p in enumerate(ivs):
                    y, x = divmod(k, a)
                    cells[(min(u * b + y, r - 1), min(v * a + x, q - 1))] = (p.offset, p.size)
        if first is not None and not cells:
            out[f"{L}_single"] = np.array([first.offset, first.size, W, H], dtype=np.int64)
            continue
        off = np.zeros((r, q), np.int64)
        ln = np.zeros((r, q), np.int64)
        for (y, x), (o, n) in cells.items():
            off[y, x], ln[y, x] = o, n
        assert len(cells) == r * q, (L, len(cells), r * q)
        out[f"{L}_off"], out[f"{L}_len"] = off, ln
        out[f"{L}_meta"] = np.array([W, H, mw, mh, iw, a, b, q, r], np.int64)
        out[f"{L}_header"] = np.array(header, np.int64)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="http://127.0.0.1:8791")
    ap.add_argument("slides", nargs="*", default=list(SLIDES))
    args = ap.parse_args()
    CACHE.mkdir(exist_ok=True)
    ensure_proxy(args.proxy)
    for name in args.slides:
        vzip = CACHE / f"{name}.vzip"
        if not vzip.exists():
            subprocess.run(["node", str(ROOT / "web/conformance/virtualize.ts"),
                            proxied(args.proxy, SLIDES[name]), str(vzip)], check=True)
        arrays = asyncio.run(extract(vzip))
        np.savez_compressed(CACHE / f"{name}.npz", **arrays)
        for k in sorted(arrays):
            if k.endswith("_meta"):
                W, H, mw, mh, iw, a, b, q, r = arrays[k]
                L = k.split("_")[0]
                ln = arrays[f"{L}_len"]
                print(f"{name} level {L}: {W}x{H}, MCU {mw}x{mh}, interval {iw} px, chunk {a}x{b} "
                      f"intervals ({a * iw}x{b * mh} px), grid {q}x{r} intervals, "
                      f"mean interval {ln.mean():.0f} B, row {ln.sum(1).mean() / 1e3:.0f} kB")
            elif k.endswith("_single"):
                print(f"{name} level {k.split('_')[0]}: single strip, {arrays[k][1]} B")


if __name__ == "__main__":
    main()
