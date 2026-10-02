"""Compares two virtualizers (VIRTUALIZE.md §1.1, §5).

Runs the JavaScript virtualizer (web/conformance/virtualize.ts, the browser
code under Node) and the Python one (`python -m vzip.virtualize`) on every
input of the corpus, and checks that both reject it, or that their outputs
are equivalent: the same source table, the same keys, and for each key the
same ranges, equal JSON documents, or identical bytes.

The corpus is the synthetic TIFFs in web/test/fixtures/ (served by a local
HTTP server), the 205 OME-TIFFs of IDR idr0096, and 17 public ND2 files
(corpus_nd2.txt).

Usage: uv run python conformance/virtualize/compare.py <out dir> [--quick] [--only <substring>]
"""

from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "conformance"))
from http_server import Server  # noqa: E402

from vzip.pb import Concat, Range, decode_source_table  # noqa: E402

HERE = Path(__file__).parent
IDR = "https://ftp.ebi.ac.uk/pub/databases/IDR/idr0096-tratwal-marrowquant/20210609-ftp-ome-tiffs/"
JS = ["node", str(ROOT / "web" / "conformance" / "virtualize.ts")]
PY = ["uv", "run", "python", "-m", "vzip.virtualize"]


def describe(path: Path) -> dict:
    """An archive's output (§1.1): sources and entries."""
    z = zipfile.ZipFile(path)
    sources = [s.url for s in decode_source_table(z.read("__vz__/sources"))]
    entries = {}
    for info in z.infolist():
        if info.filename.startswith("__vz__/"):
            continue
        ex, p, ref = info.extra, 0, None
        while p < len(ex):
            hid, n = struct.unpack_from("<HH", ex, p)
            if hid == 0x7A76:
                r = Range.decode(ex[p + 4 : p + 4 + n])
                ref = [(r.source, r.offset, r.length)]
            elif hid == 0x7A77:
                ref = [(r.source, r.offset, r.length) for r in Concat.decode(ex[p + 4 : p + 4 + n]).parts]
            p += 4 + n
        if ref is not None:
            entries[info.filename] = ("ranges", ref)
        elif info.filename.endswith("zarr.json"):
            entries[info.filename] = ("json", json.loads(z.read(info)))
        else:
            entries[info.filename] = ("bytes", hashlib.sha256(z.read(info)).hexdigest())
    return {"sources": sources, "entries": entries}


def differences(a: dict, b: dict) -> list[str]:
    out = []
    if a["sources"] != b["sources"]:
        out.append(f"sources {a['sources']} != {b['sources']}")
    ka, kb = set(a["entries"]), set(b["entries"])
    if ka != kb:
        out.append(f"keys only in js: {sorted(ka - kb)[:5]}, only in py: {sorted(kb - ka)[:5]}")
    for k in sorted(ka & kb):
        if a["entries"][k] != b["entries"][k]:
            ea, eb = a["entries"][k], b["entries"][k]
            out.append(f"{k}: js {json.dumps(ea, default=str)[:300]} != py {json.dumps(eb, default=str)[:300]}")
            if len(out) > 8:
                break
    return out


def run(cmd: list[str], url: str, out: Path) -> tuple[str, str]:
    p = subprocess.run(cmd + [url, str(out)], capture_output=True, text=True, timeout=900)
    if p.returncode == 3:
        return "rejected", p.stderr.strip().splitlines()[-1] if p.stderr.strip() else ""
    if p.returncode:
        return "crashed", p.stderr.strip()[-400:]
    return "ok", p.stdout.strip()


def check(item: tuple[str, str], out_dir: Path) -> dict:
    name, url = item
    js_out, py_out = out_dir / f"{name}.js.vzip", out_dir / f"{name}.py.vzip"
    js, py = run(JS, url, js_out), run(PY, url, py_out)
    result = {"name": name, "url": url, "js": js[0], "py": py[0]}
    if js[0] == py[0] == "rejected":
        result["verdict"] = "both reject"
    elif js[0] == py[0] == "ok":
        diffs = differences(describe(js_out), describe(py_out))
        result["verdict"] = "equivalent" if not diffs else "DIFFERENT"
        result["differences"] = diffs
        result["summary"] = js[1]
        js_out.unlink()
        py_out.unlink()
    else:
        result["verdict"] = "DISAGREE"
        result["messages"] = {"js": js[1], "py": py[1]}
    return result


def main(out_dir: str, quick: bool = False, only: str | None = None) -> int:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    fixtures = ROOT / "web" / "test" / "fixtures"
    server = Server(fixtures)
    corpus = [(f"fixture-{p.stem}", server.base + p.name)
              for p in sorted([*fixtures.glob("*.tif"), *fixtures.glob("*.nd2")])]
    listing = urllib.request.urlopen(IDR, timeout=60).read().decode()
    import re

    tiffs = sorted(set(re.findall(r'href="([^"?/][^"]*\.ome\.tiff)"', listing)))
    corpus += [(f"idr-{i:03d}", IDR + n) for i, n in enumerate(tiffs[:3] if quick else tiffs)]
    nd2 = [line.split("|") for line in (HERE / "corpus_nd2.txt").read_text().split("\n") if line and not line.startswith("#")]
    corpus += [(f"nd2-{name}", url) for url, name in (nd2[:3] if quick else nd2)]
    if only:
        corpus = [c for c in corpus if only in c[0]]
    results = []
    with ThreadPoolExecutor(3) as pool:
        for r in pool.map(lambda item: check(item, out), corpus):
            results.append(r)
            print(f"{r['verdict']:12s} {r['name']:40s} {r.get('summary', '')[:110]}", flush=True)
            for d in r.get("differences", []) + [f"{k}: {v}" for k, v in r.get("messages", {}).items()]:
                print(f"      {d}", flush=True)
    (out / "results.json").write_text(json.dumps(results, indent=1))
    bad = [r for r in results if r["verdict"] not in ("equivalent", "both reject")]
    print(f"\n{len(results)} inputs: {sum(r['verdict'] == 'equivalent' for r in results)} equivalent, "
          f"{sum(r['verdict'] == 'both reject' for r in results)} rejected by both, {len(bad)} not matching")
    return 1 if bad else 0


if __name__ == "__main__":
    args = sys.argv[2:]
    only = args[args.index("--only") + 1] if "--only" in args else None
    sys.exit(main(sys.argv[1], "--quick" in args, only))
