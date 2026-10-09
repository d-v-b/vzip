"""Adoption: today's profile and the IR on corpus inputs through the caching proxy, offline
(a block the cache lacks fails the request: no remote host is contacted), one process
each (/usr/bin/time -l), outputs compared without vzip_source. The IR runs at
VZIP_CONCURRENCY (6) connections. `--today <cmd>` names today's command (default
`python -m vzip.virtualize`); `--ir <cmd>` the IR's (default `python -m vzip.ir
--allow-private-hosts`).
Usage: corpus.py <prefix: czi-|nd2-|idr-|svs-> <out dir> [--keep] [--only name] [--today <cmd>] [--ir <cmd>]"""
import json, os, re, subprocess, sys, time, threading, hashlib, urllib.parse
from pathlib import Path
W = Path(__file__).resolve().parents[2]
HERE = Path(os.environ["VZIP_ADOPT_WORK"])  # the saved IDR listing (idr_listing.html)
sys.path.insert(0, str(W / "conformance/virtualize"))
import proxy as P
from compare import at_revision, from_vzip, differences

lock = threading.Lock()
misses = []
reads = {"n": 0, "bytes": 0}
orig_block, orig_read = P.Upstream.block, P.Upstream.read

def block(self, url, i):
    f = self.cache / f"{hashlib.sha256(url.encode()).hexdigest()}.{i}"
    if f.exists():
        return f.read_bytes()
    with lock:  # offline: never fetch upstream
        misses.append((url, i))
    raise OSError(f"offline: block {i} of {url} is not cached")

def read(self, url, start, end):
    reads["n"] += 1; reads["bytes"] += end - start
    return orig_read(self, url, start, end)

def offline(self, req):
    raise OSError(f"offline: {req.full_url} is not cached")

P.Upstream.block, P.Upstream.read, P.Upstream._open = block, read, offline

def strip(o):
    # the frozen reference's root records its own revision (HARNESS.md): read at the current one
    o = at_revision(o)
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
        corpus_file = {"czi-": "corpus_czi.txt", "nd2-": "corpus_nd2.txt"}.get(prefix, "corpus_tiff.txt")
        for line in (W / "conformance/virtualize" / corpus_file).read_text().split("\n"):
            if not line or line.startswith("#"):
                continue
            url, name, *_ = line.split("|")
            name = (prefix if prefix in ("czi-", "nd2-") else "") + name
            if name.startswith(prefix):
                out.append((name, proxy.remote(url)))
    return out

def opt(name, default):
    return sys.argv[sys.argv.index(name) + 1].split() if name in sys.argv else default


TODAY = opt("--today", ["uv", "run", "python", "-m", "vzip.virtualize"])
IR = opt("--ir", ["uv", "run", "python", "-m", "vzip.ir", "--allow-private-hosts"])


def main():
    prefix, outdir = sys.argv[1], Path(sys.argv[2])
    outdir.mkdir(parents=True, exist_ok=True)
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    res = []
    for name, url in items(prefix):
        if only and only not in name:
            continue
        a, b = outdir / f"{name}.today.vzip", outdir / f"{name}.ir.vzip"
        ra = timed(TODAY + [url], a)
        rb = timed(IR + [url], b)
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
