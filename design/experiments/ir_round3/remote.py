"""Today and round 3 over the round-2 range server (design/experiments/ir_round2/rangeserver.py)
serving the sparse copies: no delay with multipart; EBI's profile (65 ms to the first
byte, 2.5 MB/s per connection) with multipart; and EBI's profile with single ranges.
The IR runs at 6 connections (a browser's). Servers are started here and stopped at the end.
Usage: remote.py <out.json> <profile: fast|ebi|ebi1|all> <name>..."""
import ast, json, os, re, subprocess, sys, time, urllib.request
from pathlib import Path
W = Path(__file__).resolve().parents[3]
HERE = Path(os.environ.get("VZIP_R3_WORK", W / "design/experiments/ir_round3/work"))  # sparse copies, outputs, the IDR listing, round 1's tree
PROFILES = {"fast": (18311, 0, 1, None), "ebi": (18312, 65, 1, 2.5), "ebi1": (18313, 65, 0, 2.5),
            "ebi1x16": (18313, 65, 0, 2.5),  # the single-range server, the IR allowed 16x amplification
            "ebi1c16": (18313, 65, 0, 2.5)}  # the single-range server, the IR at 16 connections (today uses 32)

def stats(port):
    return ast.literal_eval(urllib.request.urlopen(f"http://127.0.0.1:{port}/stats").read().decode())

def run(cmd, out, port, env=None):
    s0 = stats(port)
    t = time.time()
    p = subprocess.run(["/usr/bin/time", "-l"] + cmd + [str(out)], capture_output=True, text=True, timeout=7200, cwd=W,
                       env={**os.environ, "VZIP_CONCURRENCY": "6", **(env or {})})  # a browser's
    dt = time.time() - t
    s1 = stats(port)
    m = re.search(r"(\d+)\s+maximum resident set size", p.stderr)
    status = "ok" if p.returncode == 0 else "rejected" if p.returncode == 3 else "crashed"
    return {"status": status, "s": round(dt, 2), "rss": (int(m[1]) >> 20) if m else -1,
            "requests": s1["requests"] - s0["requests"], "ranges": s1["ranges"] - s0["ranges"],
            "bytes": s1["bytes"] - s0["bytes"], "size": out.stat().st_size if out.exists() else 0,
            "msg": (p.stdout.strip() if status == "ok" else p.stderr[-300:])[:300]}

def main():
    res_path = Path(sys.argv[1])
    profiles = list(PROFILES) if sys.argv[2] == "all" else sys.argv[2].split(",")
    names = [a for a in sys.argv[3:] if not a.startswith("--")]
    res = json.loads(res_path.read_text()) if res_path.exists() else {}
    servers = []
    try:
        started = set()
        for pr in profiles:
            port, lat, mp, rate = PROFILES[pr]
            if port in started:
                continue
            started.add(port)
            servers.append(subprocess.Popen([sys.executable, str(W / "design/experiments/ir_round2/rangeserver.py"), str(HERE / "sparse"),
                                             str(port), str(lat), str(mp)] + ([str(rate)] if rate else []),
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(1.5)
        out = HERE / "measure_out"
        out.mkdir(exist_ok=True)
        for name in names:
            for pr in profiles:
                port = PROFILES[pr][0]
                url = f"http://127.0.0.1:{port}/{name}"
                skip_today = "--no-today" in sys.argv
                r = {} if skip_today else {"today": run(["uv", "run", "python", "-m", "vzip.virtualize", url], out / "t.vzip", port)}
                r["r3"] = run(["uv", "run", "python", "-m", "vzip.ir", url], out / "i.vzip", port,
                              {"VZIP_AMPLIFICATION": "16"} if pr == "ebi1x16" else {"VZIP_CONCURRENCY": "16"} if pr == "ebi1c16" else None)
                res[f"{name}|{pr}"] = r
                res_path.write_text(json.dumps(res, indent=1))
                f = lambda k: f"{r[k]['status'][:3]} {r[k]['s']:7.1f}s {r[k]['rss']:5d}MB {r[k]['requests']:6d}rq {r[k]['ranges']:7d}rg {r[k]['bytes'] / 1e6:8.2f}MB"
                print(f"{name[:32]:32s} {pr:7s} today {f('today') if 'today' in r else '-'} | r3 {f('r3')}", flush=True)
    finally:
        for s in servers:
            s.terminate()
            s.wait()

main()
