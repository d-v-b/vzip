"""Checks the network model (model.simulate) against real HTTP fetches.

1. `local`: a local HTTP/1.1 server that answers single-range GETs of a
   virtual file with zero bytes, after a fixed delay (the RTT), pacing all
   responses through one shared bandwidth limit. The plans of some benchmark
   views are fetched over C kept-alive connections and the measured time is
   compared with the model's.
2. `remote`: the same plans fetched from openslide.cs.cmu.edu (the real
   host of the NDPI corpus), compared with the model under the RTT and
   bandwidth measured just before (a few MB per run).

Usage: uv run python experiments/ndpi_access/validate.py [local|remote|both]
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).parent))
import bench  # noqa: E402
import model as m  # noqa: E402

PIECE = 16384
REMOTE = "https://openslide.cs.cmu.edu/download/openslide-testdata/Hamamatsu/CMU-1.ndpi"
UA = "vzip-ndpi-access-validate (https://github.com/d-v-b/vzip)"


class Shaped:
    def __init__(self, rtt: float, bw: float):
        self.rtt, self.bw, self.next_free = rtt, bw, 0.0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                rng = next(line for line in head.split(b"\r\n") if line.lower().startswith(b"range:"))
                a, b = rng.split(b"=")[1].split(b"-")
                n = int(b) - int(a) + 1
                await asyncio.sleep(self.rtt)
                writer.write(b"HTTP/1.1 206 Partial Content\r\nContent-Length: %d\r\n"
                             b"Content-Range: bytes %s-%s/*\r\n\r\n" % (n, a, b))
                left = n
                while left:
                    k = min(PIECE, left)
                    now = loop.time()
                    start = max(now, self.next_free)
                    self.next_free = start + k / self.bw
                    if self.next_free > now:
                        await asyncio.sleep(self.next_free - now)
                    writer.write(bytes(k))
                    left -= k
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()


async def fetch_plan(url: str, plan: m.Plan, conns: int) -> float:
    conn = aiohttp.TCPConnector(limit=conns, force_close=False)
    async with aiohttp.ClientSession(connector=conn, headers={"User-Agent": UA}) as s:
        # warm the connections, as a viewer's would be
        await asyncio.gather(*[get(s, url, 0, 1) for _ in range(conns)])
        t = time.perf_counter()
        await asyncio.gather(*[get(s, url, sp[0][0], sp[0][1]) for sp in plan.requests])
        return time.perf_counter() - t


async def get(s: aiohttp.ClientSession, url: str, a: int, b: int) -> int:
    async with s.get(url, headers={"Range": f"bytes={a}-{b - 1}"}) as r:
        return len(await r.read())


def cases(slide: m.Slide) -> list[tuple[str, m.Plan]]:
    """A few views of the benchmark, each under a planner with few large
    requests and one with many small ones."""
    tr = m.traces(slide)
    out = []
    for tname, i in (("pan_L0", 1), ("pan_mid", 1), ("overview", 0)):
        v = tr[tname][i]
        lv = slide.levels[v.level]
        a, b = m.CHUNKINGS["spec"].ab(lv)
        ranges = [m.chunk_ranges(lv, a, b, c.u, c.v) for c in m.view_chunks(slide, m.CHUNKINGS["spec"], v)]
        for pname in ("pc64k", "b2"):
            p = bench.PLANNERS[pname]().plan(ranges, m.NETS["h1-20ms-50M"], lv)
            out.append((f"{tname}[{i}] {pname}", p))
    return out


async def local(slide: m.Slide) -> None:
    print("| net | case | requests | MB | model s | measured s |\n|---|---|---:|---:|---:|---:|")
    for nname in ("h1-20ms-50M", "h1-80ms-500M", "h2-20ms-50M"):
        net = m.NETS[nname]
        shaped = Shaped(net.rtt, net.bw)
        server = await asyncio.start_server(shaped.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        for label, plan in cases(slide):
            if net.conns > 6 and len(plan.requests) < 50:
                continue
            measured = await fetch_plan(f"http://127.0.0.1:{port}/f", plan, net.conns)
            print(f"| {nname} | {label} | {len(plan.requests)} | {plan.bytes() / 1e6:.2f} | "
                  f"{m.simulate(plan.sizes(), net):.3f} | {measured:.3f} |", flush=True)
        server.close()


async def remote(slide: m.Slide) -> None:
    conn = aiohttp.TCPConnector(limit=1)
    async with aiohttp.ClientSession(connector=conn, headers={"User-Agent": UA}) as s:
        await get(s, REMOTE, 0, 1)
        ts = []
        for i in range(8):
            t = time.perf_counter()
            await get(s, REMOTE, 1000 * i, 1000 * i + 100)
            ts.append(time.perf_counter() - t)
        rtt = statistics.median(ts)
        t = time.perf_counter()
        await get(s, REMOTE, 10 << 20, 14 << 20)
        bw1 = (4 << 20) / (time.perf_counter() - t - rtt)
    # aggregate bandwidth over 6 connections
    conn = aiohttp.TCPConnector(limit=6)
    async with aiohttp.ClientSession(connector=conn, headers={"User-Agent": UA}) as s:
        await asyncio.gather(*[get(s, REMOTE, 0, 1) for _ in range(6)])
        t = time.perf_counter()
        await asyncio.gather(*[get(s, REMOTE, (20 + i) << 20, (21 + i) << 20) for i in range(6)])
        bw6 = (6 << 20) / (time.perf_counter() - t - rtt)
    print(f"openslide.cs.cmu.edu: rtt {rtt * 1000:.0f} ms, 1 connection {bw1 * 8 / 1e6:.1f} Mbit/s, "
          f"6 connections {bw6 * 8 / 1e6:.1f} Mbit/s\n")
    print("| bw model | case | requests | MB | model s | measured s |\n|---|---|---:|---:|---:|---:|")
    for label, plan in cases(slide):
        if plan.bytes() > 12e6:
            continue
        measured = await fetch_plan(REMOTE, plan, 6)
        for bname, bw in (("per-connection", bw1), ("shared", bw6)):
            # per-connection: each connection gets bw1 on its own (6 × bw1 total)
            net = m.Net("openslide", rtt, bw1 * 6 if bname == "per-connection" else bw6, 6)
            print(f"| {bname} | {label} | {len(plan.requests)} | {plan.bytes() / 1e6:.2f} | "
                  f"{m.simulate(plan.sizes(), net):.2f} | {measured:.2f} |", flush=True)


def main() -> None:
    what = sys.argv[1] if len(sys.argv) > 1 else "both"
    slide = m.load("CMU-1")
    if what in ("local", "both"):
        asyncio.run(local(slide))
    if what in ("remote", "both"):
        asyncio.run(remote(slide))


if __name__ == "__main__":
    main()
