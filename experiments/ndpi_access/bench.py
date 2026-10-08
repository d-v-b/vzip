"""Runs viewer traces over NDPI slides through read planners and a network
model, and prints a results table. See README.md.

Usage:
    uv run python experiments/ndpi_access/bench.py [--slides CMU-1,Hamamatsu-1]
        [--nets h1-20ms-50M,...] [--planners spec:pc64k,...] [--multirange]
        [--json out.json] [--per-view]

A planner spec is `<chunking>:<planner>`; chunkings are in model.CHUNKINGS,
planners in PLANNERS below.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import model as m  # noqa: E402

PLANNERS = {
    "pc2": lambda: m.PerChunk(2),  # exact: only the dropped RST markers are bridged
    "pc64k": lambda: m.PerChunk(1 << 16),  # store.py / archive.ts today
    "pc256k": lambda: m.PerChunk(1 << 18),
    "pc1m": lambda: m.PerChunk(1 << 20),
    "pcbdp": lambda: m.PerChunk("bdp"),
    "pcbdpc": lambda: m.PerChunk("bdp/c"),
    "b2": lambda: m.Batched(2),
    "b64k": lambda: m.Batched(1 << 16),
    "bbdp": lambda: m.Batched("bdp"),
    "bbdpc": lambda: m.Batched("bdp/c"),
    "bopt": lambda: m.Batched("opt"),
    "mr": lambda: m.MultiRange(2),
    "mrpc": lambda: m.MultiRange(2, per_chunk=True),
    "blk64k": lambda: m.BlockCache(1 << 16),
    "blk64kj": lambda: m.BlockCache(1 << 16, join=True),
    "blk1m": lambda: m.BlockCache(1 << 20),
    "row": lambda: m.RowCache(),
    # the same planners, keeping every fetched span (gaps included) for later views
    "pc64k+c": lambda: m.SpanCache(m.PerChunk(1 << 16)),
    "pcbdpc+c": lambda: m.SpanCache(m.PerChunk("bdp/c")),
    "b2+c": lambda: m.SpanCache(m.Batched(2)),
    "bbdpc+c": lambda: m.SpanCache(m.Batched("bdp/c")),
    "bopt+c": lambda: m.SpanCache(m.Batched("opt")),
    "mr+c": lambda: m.SpanCache(m.MultiRange(2)),
    "mrbdpc+c": lambda: m.SpanCache(m.MultiRange("bdp/c")),
    "ideal": lambda: m.Ideal(),
    "pcopt": lambda: m.PerChunk("opt"),
    "pcopt+c": lambda: m.SpanCache(m.PerChunk("opt")),
    "bopt.5+c": lambda: m.SpanCache(m.Batched("opt", 0.5)),
    "bopt.25+c": lambda: m.SpanCache(m.Batched("opt", 0.25)),
}

DEFAULT_PLANNERS = ["spec:pc64k", "spec:pc2", "spec:pcbdpc", "spec:b2", "spec:bbdpc", "spec:bopt",
                    "spec:blk64k", "spec:blk64kj", "spec:row"]


def run_trace(slide: m.Slide, chunking: m.Chunking, views: list[m.View], planner: m.Planner,
              net: m.Net) -> list[dict]:
    loaded: set[m.Chunk] = set()  # the viewer's chunk cache
    seen: set[tuple[int, int, int]] = set()  # intervals visible so far
    out = []
    for v in views:
        lv = slide.levels[v.level]
        # the bytes any reader must fetch: intervals under the visible region
        vis = 0
        for y in range(v.y0 // lv.mh, (v.y1 - 1) // lv.mh + 1):
            for x in range(v.x0 // lv.iw, (v.x1 - 1) // lv.iw + 1):
                if (v.level, y, x) not in seen:
                    seen.add((v.level, y, x))
                    vis += int(lv.ln[y, x])
        a, b = chunking.ab(lv)
        chunks = [c for c in m.view_chunks(slide, chunking, v) if c not in loaded]
        loaded.update(chunks)
        ranges = [m.chunk_ranges(lv, a, b, c.u, c.v) for c in chunks]
        needed = int(sum((r[:, 1] - r[:, 0]).sum() for r in ranges))
        plan = planner.plan(ranges, net, lv)
        sizes = plan.sizes()
        out.append({
            "label": v.label, "level": v.level, "chunks": len(chunks),
            "requests": len(sizes), "fetched": plan.bytes(), "needed": needed, "visible": vis,
            "time": m.simulate(sizes, net),
            "decoded_px": sum(m.chunk_pixels(lv, a, b, c.u, c.v) for c in chunks),
            "visible_px": (v.x1 - v.x0) * (v.y1 - v.y0),
        })
    return out


def summarize(rows: list[dict]) -> dict:
    times = [r["time"] for r in rows if r["chunks"]]
    fetched = sum(r["fetched"] for r in rows)
    needed = sum(r["needed"] for r in rows)
    return {
        "views": len(rows),
        "requests": sum(r["requests"] for r in rows),
        "fetched_MB": fetched / 1e6,
        "needed_MB": needed / 1e6,
        "overfetch": fetched / needed if needed else float("nan"),
        "visible_MB": sum(r["visible"] for r in rows) / 1e6,
        "overfetch_visible": fetched / max(1, sum(r["visible"] for r in rows)),
        "total_s": sum(times),
        "median_view_s": statistics.median(times) if times else 0.0,
        "max_view_s": max(times) if times else 0.0,
        "px_overdecode": sum(r["decoded_px"] for r in rows) / max(1, sum(r["visible_px"] for r in rows)),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slides", default="CMU-1,Hamamatsu-1")
    ap.add_argument("--nets", default="h1-20ms-50M,h1-80ms-500M,h2-20ms-50M,h2-80ms-500M,openslide")
    ap.add_argument("--planners", default=",".join(DEFAULT_PLANNERS))
    ap.add_argument("--traces", default="overview,zoom,pan_L0,pan_mid")
    ap.add_argument("--multirange", action="store_true", help="the server answers multi-range requests")
    ap.add_argument("--json")
    ap.add_argument("--per-view", action="store_true")
    a = ap.parse_args()
    results = []
    for sname in a.slides.split(","):
        slide = m.load(sname)
        tr = m.traces(slide)
        for nname in a.nets.split(","):
            net = m.NETS[nname]
            if a.multirange:
                net = dataclasses.replace(net, multirange=True)
            print(f"\n## {sname} — {net.name}{' + multi-range' if a.multirange else ''}\n")
            print("| strategy | requests | fetched MB | chunk MB | visible MB | overfetch (chunk) | "
                  "overfetch (visible) | px overdecode | total s | median view s | max view s |")
            print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
            for spec in a.planners.split(","):
                cname, pname = spec.split(":")
                rows = []
                for tname in a.traces.split(","):
                    rows += run_trace(slide, m.CHUNKINGS[cname], tr[tname], PLANNERS[pname](), net)
                    if a.per_view:
                        for r in rows[-len(tr[tname]):]:
                            print(f"    {tname} {r}")
                s = summarize(rows)
                label = f"{m.CHUNKINGS[cname].name} + {PLANNERS[pname]().name}"
                print(f"| {label} | {s['requests']} | {s['fetched_MB']:.1f} | {s['needed_MB']:.1f} | "
                      f"{s['visible_MB']:.1f} | {s['overfetch']:.2f} | {s['overfetch_visible']:.2f} | "
                      f"{s['px_overdecode']:.2f} | {s['total_s']:.2f} | "
                      f"{s['median_view_s']:.3f} | {s['max_view_s']:.2f} |", flush=True)
                results.append({"slide": sname, "net": nname, "multirange": a.multirange,
                                "chunking": cname, "planner": pname, **s})
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    np.seterr(all="raise")
    main()
