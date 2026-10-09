"""The frozen reference and the IR path (`python -m vzip.virtualize`) on local files: the
hostile probes of rounds 2 and 3 and the CZI review, one process each under
/usr/bin/time -l: status, time, peak RSS, archive size; outputs compared without
vzip_source, and the IR's archive rebuilt byte for byte from its mirror.
Usage: probes.py <out.json> <file>..."""
import hashlib, json, os, re, subprocess, sys, time
from pathlib import Path
W = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(W / "conformance/virtualize"))
from compare import at_revision, differences, from_vzip, without_source  # noqa: E402
OUT = Path(os.environ.get("VZIP_ADOPT_WORK", "/tmp")) / "probes_out"


def run(cmd, out):
    t = time.time()
    p = subprocess.run(["/usr/bin/time", "-l"] + cmd + [str(out)], capture_output=True, text=True, timeout=7200, cwd=W)
    m = re.search(r"(\d+)\s+maximum resident set size", p.stderr)
    status = "ok" if p.returncode == 0 else "rejected" if p.returncode == 3 else "crashed"
    msg = "" if status == "ok" else ([l for l in p.stderr.splitlines() if "rejected" in l or "Error" in l] or [p.stderr[-300:]])[-1]
    return {"status": status, "s": round(time.time() - t, 2), "rss": (int(m[1]) >> 20) if m else -1,
            "size": out.stat().st_size if out.exists() else 0, "msg": msg[:300]}


def rebuilt(archive: Path, source: Path) -> bool:
    from vzip.ir.cmirror import rebuild_from_archive
    with open(source, "rb") as f:
        def read(o, n):
            f.seek(o)
            return f.read(n)
        h = hashlib.sha256()
        rebuild_from_archive(str(archive), read, h.update)
    with open(source, "rb") as f:
        return h.hexdigest() == hashlib.file_digest(f, "sha256").hexdigest()


def main():
    res_path = Path(sys.argv[1])
    res = json.loads(res_path.read_text()) if res_path.exists() else {}
    OUT.mkdir(parents=True, exist_ok=True)
    for f in sys.argv[2:]:
        p = Path(f)
        a, b = OUT / "ref.vzip", OUT / "ir.vzip"
        for x in (a, b):
            x.unlink(missing_ok=True)
        url = f"https://data.test/{p.name}"
        ra = run(["uv", "run", "python", str(W / "conformance/virtualize/reference/cli.py"), str(p), "--url", url], a)
        rb = run(["uv", "run", "python", "-m", "vzip.virtualize", str(p), "--url", url], b)
        if ra["status"] != rb["status"]:
            verdict = "DIVERGES"
        elif ra["status"] != "ok":
            verdict = ra["status"]
        else:
            d = differences(without_source(at_revision(from_vzip(a))), without_source(from_vzip(b)))
            verdict = "DIFF" if d else "equivalent"
        rb["rebuilt"] = rebuilt(b, p) if rb["status"] == "ok" else None
        res[p.name] = {"verdict": verdict, "ref": ra, "ir": rb}
        res_path.write_text(json.dumps(res, indent=1))
        print(f"{p.name[:34]:34s} {verdict:10s} ref {ra['status'][:3]} {ra['s']:6.1f}s {ra['rss']:6d}MB {ra['size']:10d}B | "
              f"ir {rb['status'][:3]} {rb['s']:6.1f}s {rb['rss']:6d}MB {rb['size']:10d}B rebuilt {rb['rebuilt']}"
              + (f" | {ra['msg'][:60]} / {rb['msg'][:60]}" if ra["status"] != "ok" or rb["status"] != "ok" else ""), flush=True)


main()
