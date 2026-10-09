"""Today and the IR over the round-2 range server (experiments/ir_round2/rangeserver.py)
serving the sparse copies, on EBI's profile (65 ms to the first byte, 2.5 MB/s per
connection), with multi-range requests ("ebi") and with single ranges ("ebi1"); the
IR at 6 connections (a browser's) and 16. Today's reader prefetches at 32. Servers
are started here and stopped at the end.
Usage: remote.py <out.json> <profiles: ebi,ebi1> <connections: 8,32> [--no-today] [--today <cmd>] <name>...
VZIP_ADOPT_TAG suffixes the IR's keys (`ir_c8<tag>`); VZIP_BYTE_COST and VZIP_WHOLE_BELOW
set the planner's byte cost (s per MB) and small-source size for experiments.
`--today` names today's command (default `python -m vzip.virtualize`; after the
switch-over, the frozen reference: `python conformance/virtualize/reference/cli.py`)."""
import ast, json, os, re, subprocess, sys, time, urllib.request
from pathlib import Path
W = Path(__file__).resolve().parents[2]
HERE = Path(os.environ["VZIP_ADOPT_WORK"])  # sparse/ (the copies), measure_out/
TAG = os.environ.get("VZIP_ADOPT_TAG", "")  # a suffix for the IR's results (e.g. a setting under test)
PROFILES = {"fast": (18321, 0, 1, None), "ebi": (18322, 65, 1, 2.5), "ebi1": (18323, 65, 0, 2.5)}


def stats(port):
    return ast.literal_eval(urllib.request.urlopen(f"http://127.0.0.1:{port}/stats").read().decode())


def run(cmd, out, port, env=None):
    s0 = stats(port)
    t = time.time()
    p = subprocess.run(["/usr/bin/time", "-l"] + cmd + [str(out)], capture_output=True, text=True, timeout=7200, cwd=W,
                       env={**os.environ, **(env or {})})
    dt = time.time() - t
    s1 = stats(port)
    m = re.search(r"(\d+)\s+maximum resident set size", p.stderr)
    status = "ok" if p.returncode == 0 else "rejected" if p.returncode == 3 else "crashed"
    return {"status": status, "s": round(dt, 2), "rss": (int(m[1]) >> 20) if m else -1,
            "requests": s1["requests"] - s0["requests"], "ranges": s1["ranges"] - s0["ranges"],
            "bytes": s1["bytes"] - s0["bytes"], "size": out.stat().st_size if out.exists() else 0,
            "msg": (p.stdout.strip() if status == "ok" else p.stderr[-300:])[:300],
            "planner": _planner(p.stdout) if status == "ok" else None}


def _planner(stdout: str):
    try:
        return json.loads(stdout.strip().splitlines()[-1]).get("planner")
    except (ValueError, IndexError):
        return None


def main():
    args = sys.argv[1:]
    today = ["uv", "run", "python", "-m", "vzip.virtualize"]
    if "--today" in args:
        i = args.index("--today")
        today = args[i + 1].split()
        del args[i:i + 2]
    no_today = "--no-today" in args
    args = [a for a in args if a != "--no-today"]
    res_path, profiles, concs, names = Path(args[0]), args[1].split(","), [int(c) for c in args[2].split(",")], args[3:]
    res = json.loads(res_path.read_text()) if res_path.exists() else {}
    servers = []
    try:
        for pr in profiles:
            port, lat, mp, rate = PROFILES[pr]
            servers.append(subprocess.Popen([sys.executable, str(W / "experiments/ir_round2/rangeserver.py"), str(HERE / "sparse"),
                                             str(port), str(lat), str(mp)] + ([str(rate)] if rate else []),
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(1.5)
        out = HERE / "measure_out"
        out.mkdir(exist_ok=True)
        for name in names:
            for pr in profiles:
                port = PROFILES[pr][0]
                url = f"http://127.0.0.1:{port}/{name}"
                r = res.get(f"{name}|{pr}", {})
                if not no_today:
                    r["today"] = run(today + [url], out / "t.vzip", port)
                for c in concs:
                    r[f"ir_c{c}{TAG}"] = run(["uv", "run", "python", "-m", "vzip.virtualize", "--allow-private-hosts",
                                              "--connections", str(c), url], out / "i.vzip", port)
                res[f"{name}|{pr}"] = r
                res_path.write_text(json.dumps(res, indent=1))
                f = lambda k: f"{r[k]['status'][:3]} {r[k]['s']:7.1f}s {r[k]['rss']:5d}MB {r[k]['requests']:6d}rq {r[k]['ranges']:7d}rg {r[k]['bytes'] / 1e6:8.2f}MB"
                line = " | ".join(f"{k} {f(k)}" for k in r if k == "today" or (k.startswith("ir_") and k.endswith(TAG)))
                print(f"{name[:32]:32s} {pr:5s} {line}", flush=True)
    finally:
        for s in servers:
            s.terminate()
            s.wait()


main()
