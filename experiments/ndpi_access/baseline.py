"""The baseline measurements of the header fix (VIRTUALIZE.md revision 14),
reproduced by the model and, with --real, by vzip's Python reader through the
caching proxy:

- CMU-1.ndpi level 1, all 130 chunks: 130 requests, 166 MB read for 13.4 MB
  of chunks;
- CMU-1.ndpi level 0, the central 4 × 4 chunks (rows 17–20, columns 10–13):
  16 requests, 89 MB read for 1.4 MB.

Then the same chunk sets under the reader-side strategies (requests and
bytes as planned for the first network; multi-range assumed available
except on openslide.cs.cmu.edu, which does not answer it).

Usage: uv run python experiments/ndpi_access/baseline.py [--real]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bench  # noqa: E402
import model as m  # noqa: E402

CASES = {
    "CMU-1 level 1, all 130 chunks": (1, None),
    "CMU-1 level 0, central 4×4 chunks": (0, (range(17, 21), range(10, 14))),
}
STRATEGIES = ["pc64k", "pc2", "pcopt", "b2", "bopt", "mr", "ideal"]
NETS = ["h1-20ms-50M", "h1-80ms-500M", "h2-20ms-50M", "openslide"]


def chunk_set(slide: m.Slide, L: int, region) -> list[m.Chunk]:
    lv = slide.levels[L]
    nu, nv = -(-lv.r // lv.b), -(-lv.q // lv.a)
    us, vs = region or (range(nu), range(nv))
    return [m.Chunk(L, u, v) for u in us for v in vs]


def modelled(slide: m.Slide) -> None:
    import dataclasses

    print("| case | strategy | requests | MB read | MB of intervals | " + " | ".join(NETS) + " |")
    print("|---|---|---:|---:|---:|" + "---:|" * len(NETS))
    for case, (L, region) in CASES.items():
        lv = slide.levels[L]
        chunks = chunk_set(slide, L, region)
        ranges = [m.chunk_ranges(lv, lv.a, lv.b, c.u, c.v) for c in chunks]
        need = sum(int((r[:, 1] - r[:, 0]).sum()) for r in ranges)
        for pname in STRATEGIES:
            times = []
            for nname in NETS:
                net = dataclasses.replace(m.NETS[nname], multirange=nname != "openslide")
                p = bench.PLANNERS[pname]().plan(ranges, net, lv)
                times.append(f"{m.simulate(p.sizes(), net):.2f} s")
            # requests and bytes as planned for the first network
            p = bench.PLANNERS[pname]().plan(ranges, dataclasses.replace(m.NETS[NETS[0]], multirange=True), lv)
            print(f"| {case} | {bench.PLANNERS[pname]().name} | {len(p.requests)} | {p.bytes() / 1e6:.1f} | "
                  f"{need / 1e6:.2f} | " + " | ".join(times) + " |")


async def real(slide: m.Slide) -> None:
    from zarr.core.buffer import default_buffer_prototype

    from vzip.store import Stats, VZipStore

    proto = default_buffer_prototype()
    print("\n| case | reader | requests | MB read | MB of chunk values |\n|---|---|---:|---:|---:|")
    for case, (L, region) in CASES.items():
        keys = [f"{c.level}/c/0/{c.u}/{c.v}" for c in chunk_set(slide, L, region)]
        for label, flags in (("today (gap 64 KiB)", {}),
                             ("batched gap 2 B", {"merge_gap": 2, "batch_window": 0.005})):
            stats = Stats()
            s = VZipStore(str(m.CACHE / "CMU-1.vzip"), stats=stats, **flags)
            await s.get("zarr.json", proto)
            vals = await asyncio.gather(*[s.get(k, proto) for k in keys])
            print(f"| {case} | {label} | {stats.external_requests} | {stats.external_bytes / 1e6:.1f} | "
                  f"{sum(len(v) for v in vals) / 1e6:.1f} |", flush=True)


if __name__ == "__main__":
    slide = m.load("CMU-1")
    modelled(slide)
    if "--real" in sys.argv:
        asyncio.run(real(slide))
