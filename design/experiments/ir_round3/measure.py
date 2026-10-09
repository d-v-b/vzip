"""Today, round 1 and round 3 on local files (or URLs), one process each under
/usr/bin/time -l: status, time, peak RSS, archive size, and the IR's elements and
reads. Outputs compared without vzip_source. Usage: measure.py <out.json> <file or url>... [--no-r1]"""
import json, os, re, subprocess, sys, time
from pathlib import Path
W = Path(__file__).resolve().parents[3]
HERE = Path(os.environ.get("VZIP_R3_WORK", W / "design/experiments/ir_round3/work"))  # sparse copies, outputs, the IDR listing, round 1's tree
sys.path.insert(0, str(W / "conformance/virtualize"))
from compare import from_vzip, differences

def strip(o):
    return {"sources": o["sources"], "entries": {k: v for k, v in o["entries"].items() if not k.startswith("vzip_source")}}

def run(cmd, out, env=None):
    t = time.time()
    p = subprocess.run(["/usr/bin/time", "-l"] + cmd + [str(out)], capture_output=True, text=True, timeout=7200, cwd=W,
                       env={**os.environ, **(env or {})})
    dt = time.time() - t
    m = re.search(r"(\d+)\s+maximum resident set size", p.stderr)
    status = "ok" if p.returncode == 0 else "rejected" if p.returncode == 3 else "crashed"
    msg = p.stdout.strip() if status == "ok" else ([l for l in p.stderr.splitlines() if "rejected" in l or "Error" in l] or [p.stderr[-300:]])[-1]
    size = out.stat().st_size if out.exists() else 0
    return {"status": status, "s": round(dt, 2), "rss": (int(m[1]) >> 20) if m else -1, "size": size, "msg": msg[:400]}

def main():
    res_path = Path(sys.argv[1])
    items = [a for a in sys.argv[2:] if not a.startswith("--")]
    out_dir = HERE / "measure_out"
    out_dir.mkdir(exist_ok=True)
    res = json.loads(res_path.read_text()) if res_path.exists() else {}
    for src in items:
        name = src.rsplit("/", 1)[-1]
        a, b, c = out_dir / "today.vzip", out_dir / "r3.vzip", out_dir / "r1.vzip"
        for f in (a, b, c):
            f.unlink(missing_ok=True)
        only = "--only-r3" in sys.argv and name in res
        r = dict(res[name]) if only else {"today": run(["uv", "run", "python", "-m", "vzip.virtualize", src], a)}
        r["r3"] = run(["uv", "run", "python", "-m", "vzip.ir", src], b)
        if only:
            pass  # today's and round 1's runs (unchanged code) and the verdict are kept
        elif "--no-r1" not in sys.argv:
            r["r1"] = run(["uv", "run", "python", "-m", "vzip.ir", src], c, {"PYTHONPATH": str(HERE / "round1/src")})
        if only:
            pass
        elif r["today"]["status"] == r["r3"]["status"] == "ok":
            d = differences(strip(from_vzip(a)), strip(from_vzip(b)))
            r["verdict"] = "equivalent" if not d else "DIFF"
            r["diffs"] = d[:3]
        else:
            r["verdict"] = "both " + r["today"]["status"] if r["today"]["status"] == r["r3"]["status"] else "STATUS"
        try:
            s = json.loads(r["r3"]["msg"])
            r["elements"], r["planner"] = s.get("elements"), s.get("planner")
        except ValueError:
            pass
        res[name] = r
        res_path.write_text(json.dumps(res, indent=1))
        f = lambda k: (f"{r[k]['status'][:3]} {r[k]['s']:6.1f}s {r[k]['rss']:5d}MB {r[k]['size']:9d}B" if k in r else "")
        pl = r.get("planner") or {}
        print(f"{name[:30]:30s} {r['verdict']:12s} today {f('today')} | r3 {f('r3')} el {r.get('elements')} rq {pl.get('requests')} rd {pl.get('bytes')} | r1 {f('r1')}", flush=True)

main()
