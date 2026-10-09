"""Round 3: today's profile and the IR on corpus inputs through the caching proxy (one
process each, /usr/bin/time -l), outputs compared without vzip_source. Upstream
fetches on a cache miss are serialized and logged (EBI must not be hammered).
Usage: corpus.py <prefix: czi-|idr-|svs-> <out dir> [--keep] [--only name]"""
import json, os, re, subprocess, sys, time, threading, hashlib, urllib.parse
from pathlib import Path
W = Path(__file__).resolve().parents[2]
HERE = Path(os.environ.get("VZIP_R3_WORK", W / "experiments/ir_round3/work"))  # sparse copies, outputs, the IDR listing, round 1's tree
sys.path.insert(0, str(W / "conformance/virtualize"))
import proxy as P
from compare import from_vzip, differences

lock = threading.Lock()
misses = []
reads = {"n": 0, "bytes": 0}
orig_block, orig_read = P.Upstream.block, P.Upstream.read

def block(self, url, i):
    f = self.cache / f"{hashlib.sha256(url.encode()).hexdigest()}.{i}"
    if f.exists():
        return f.read_bytes()
    with lock:  # one upstream fetch at a time
        misses.append((url, i))
        return orig_block(self, url, i)

def read(self, url, start, end):
    reads["n"] += 1; reads["bytes"] += end - start
    return orig_read(self, url, start, end)

P.Upstream.block, P.Upstream.read = block, read

def strip(o):
    return {"sources": o["sources"], "entries": {k: v for k, v in o["entries"].items() if not k.startswith("vzip_source")}}

def timed(cmd, out):
    r0, m0 = dict(reads), len(misses)
    t = time.time()
    p = subprocess.run(["/usr/bin/time", "-l"] + cmd + [str(out)], capture_output=True, text=True, timeout=7200, cwd=W,
                       env={**os.environ, "VZIP_CONCURRENCY": os.environ.get("VZIP_CONCURRENCY", "6")})
    dt = time.time() - t
    m = re.search(r"(\d+)\s+maximum resident set size", p.stderr)
    rss = int(m[1]) >> 20 if m else -1
    status = "ok" if p.returncode == 0 else "rejected" if p.returncode == 3 else "crashed"
    err = [l for l in p.stderr.splitlines() if l.startswith("rejected") or "Error" in l][-1:]
    msg = p.stdout.strip() if status == "ok" else (err[0] if err else p.stderr[-800:])
    return {"status": status, "s": round(dt, 2), "rss": rss, "msg": msg, "requests": reads["n"] - r0["n"],
            "bytes": reads["bytes"] - r0["bytes"], "misses": len(misses) - m0}

def items(prefix):
    proxy = P.Proxy(W / "web/test/fixtures", Path("/tmp/vzip-proxy-cache"))
    out = []
    if prefix == "idr-":
        base = "https://ftp.ebi.ac.uk/pub/databases/IDR/idr0096-tratwal-marrowquant/20210609-ftp-ome-tiffs/"
        names = sorted(set(re.findall(r'href="([^"?/][^"]*\.ome\.tiff)"', (HERE / "idr_listing.html").read_text())))
        out = [(f"idr-{i:03d}", proxy.remote(base + n)) for i, n in enumerate(names)]
    else:
        corpus_file = "corpus_czi.txt" if prefix == "czi-" else "corpus_tiff.txt"
        for line in (W / "conformance/virtualize" / corpus_file).read_text().split("\n"):
            if not line or line.startswith("#"):
                continue
            url, name, *_ = line.split("|")
            name = ("czi-" if prefix == "czi-" else "") + name
            if name.startswith(prefix):
                out.append((name, proxy.remote(url)))
    return out

def main():
    prefix, outdir = sys.argv[1], Path(sys.argv[2])
    outdir.mkdir(parents=True, exist_ok=True)
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    res = []
    for name, url in items(prefix):
        if only and only not in name:
            continue
        a, b = outdir / f"{name}.today.vzip", outdir / f"{name}.ir.vzip"
        ra = timed(["uv", "run", "python", "-m", "vzip.virtualize", url], a)
        rb = timed(["uv", "run", "python", "-m", "vzip.ir", url], b)
        if ra["status"] != rb["status"]:
            verdict, d = "STATUS", [f"today {ra['status']} {ra['msg'][:200]} / ir {rb['status']} {rb['msg'][-400:]}"]
        elif ra["status"] != "ok":
            verdict, d = ra["status"], [ra["msg"][:200], rb["msg"][:200]]
        else:
            d = differences(strip(from_vzip(a)), strip(from_vzip(b)))
            verdict = "DIFF" if d else "equivalent"
        sa = a.stat().st_size if a.exists() else 0
        sb = b.stat().st_size if b.exists() else 0
        summary = json.loads(rb["msg"]) if rb["status"] == "ok" else {}
        r = {"name": name, "verdict": verdict, "diffs": d[:5], "today": ra, "ir": rb, "size_today": sa, "size_ir": sb,
             "elements": summary.get("elements"), "planner": summary.get("planner")}
        r["ir"]["msg"] = r["ir"]["msg"][:300]
        res.append(r)
        print(f"{name[:38]:38s} {verdict:10s} today {ra['s']:6.1f}s {ra['rss']:5d}MB {ra['requests']:6d}rq {ra['bytes']/1e6:8.1f}MB {sa:9d}B | "
              f"ir {rb['s']:6.1f}s {rb['rss']:5d}MB {rb['requests']:6d}rq {rb['bytes']/1e6:8.1f}MB {sb:9d}B el {r['elements']} miss {ra['misses']}/{rb['misses']}", flush=True)
        for x in d[:3]:
            print("    ", x[:500], flush=True)
        for f in (a, b):
            if f.exists() and "--keep" not in sys.argv:
                f.unlink()
    (outdir / f"results-{prefix}.json").write_text(json.dumps(res, indent=1))
    print("misses", len(misses), sorted({urllib.parse.urlsplit(u).netloc for u, _ in misses}))

main()
