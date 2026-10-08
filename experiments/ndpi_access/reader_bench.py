"""Times vzip's Python reader (src/vzip/store.py), with its read-planning
flags, on benchmark views served by the shaped local server of validate.py,
next to the model's prediction for the matching planner.

The slides are virtualized again by the Python virtualizer (through the
caching proxy, structure only) with their source URL on the shaped server,
which answers every range with zero bytes after the RTT, through one shared
bandwidth limit. The reader's thread pool is capped at the connection count
(6, like a browser's HTTP/1.1 limit per host). The JPEG header is in data
sources (VIRTUALIZE.md revision 14), so it costs no request, as in the model.

Usage: uv run python experiments/ndpi_access/reader_bench.py [views per trace]
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bench  # noqa: E402
import model as m  # noqa: E402
import prepare  # noqa: E402
import validate  # noqa: E402

from vzip.readplan import NetModel  # noqa: E402
from vzip.store import Stats, VZipStore  # noqa: E402
from zarr.core.buffer import default_buffer_prototype  # noqa: E402

PORT = 8792
CONFIGS = [
    # (label, VZipStore flags, model planner)
    ("today: per-chunk gap 64 KiB", {}, "pc64k"),
    ("batched gap 2 B + span cache", {"merge_gap": 2, "batch_window": 0.005, "span_cache": 1 << 30}, "b2+c"),
    ("batched cost model + span cache", {"merge_gap": "cost", "batch_window": 0.005, "span_cache": 1 << 30},
     "bopt+c"),
]


def archive(name: str) -> Path:
    path = m.CACHE / f"{name}.shaped.vzip"
    if not path.exists():
        from vzip.virtualize import virtualize

        prepare.ensure_proxy("http://127.0.0.1:8791")
        _, out = virtualize(prepare.proxied("http://127.0.0.1:8791", prepare.SLIDES[name]),
                            url=f"http://127.0.0.1:{PORT}/{name}.ndpi")
        out.write(str(path))
    return path


async def run(name: str, nname: str, n_views: int) -> None:
    net = m.NETS[nname]
    shaped = validate.Shaped(net.rtt, net.bw)
    server = await asyncio.start_server(shaped.handle, "127.0.0.1", PORT)
    asyncio.get_running_loop().set_default_executor(
        concurrent.futures.ThreadPoolExecutor(max_workers=net.conns))
    slide = m.load(name)
    tr = m.traces(slide)
    path = archive(name)
    proto = default_buffer_prototype()
    print(f"\n### {name}, {net.name}\n")
    print("| reader | trace | views | requests | MB | measured s | model s |")
    print("|---|---|---:|---:|---:|---:|---:|")
    for label, flags, pname in CONFIGS:
        if "net" not in flags and flags.get("merge_gap") == "cost":
            flags = {**flags, "net": NetModel(net.rtt, net.bw, net.conns)}
        for tname in ("pan_L0", "pan_mid"):
            views = tr[tname][:n_views]
            stats = Stats()
            store = VZipStore(str(path), stats=stats, **flags)
            await store.get("zarr.json", proto)
            loaded: set = set()
            total = 0.0
            for v in views:
                keys = [f"{c.level}/c/0/{c.u}/{c.v}" for c in m.view_chunks(slide, m.CHUNKINGS["spec"], v)
                        if c not in loaded]
                loaded.update(m.view_chunks(slide, m.CHUNKINGS["spec"], v))
                t = time.perf_counter()
                await asyncio.gather(*[store.get(k, proto) for k in keys])
                total += time.perf_counter() - t
            rows = bench.run_trace(slide, m.CHUNKINGS["spec"], views, bench.PLANNERS[pname](), net)
            modelled = sum(r["time"] for r in rows)
            print(f"| {label} | {tname} | {len(views)} | {stats.external_requests} | "
                  f"{stats.external_bytes / 1e6:.1f} | {total:.2f} | {modelled:.2f} |", flush=True)
    server.close()
    # the reader's kept-alive connections go to this server, which is gone
    import vzip.store

    for conns in vzip.store._POOL._idle.values():
        for c in conns:
            c.close()
    vzip.store._POOL._idle.clear()


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    for name in ("CMU-1", "Hamamatsu-1"):
        for nname in ("h1-20ms-50M", "h1-80ms-500M"):
            asyncio.run(run(name, nname, n))


if __name__ == "__main__":
    main()
