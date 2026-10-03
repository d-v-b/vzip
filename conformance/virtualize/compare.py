"""Compares virtualizers (VIRTUALIZE.md §1.1, §9; HARNESS.md).

Every implementation runs on every input of the corpus, which the caching
proxy (proxy.py) serves over local HTTP: the synthetic files in
web/test/fixtures/ (one directory per format), the synthetic stores in
web/test/fixtures/n5/ and web/test/fixtures/zarr2/ (one directory per store,
served with the S3 listing of VIRTUALIZE.md §1.5), the 205 OME-TIFFs of IDR
idr0096, and the public files and stores in corpus_*.txt. For each input, all
implementations must either reject it (exit status 3) or produce equivalent
outputs; outputs are compared with the reference implementation's (the first
one).

Corpus lines are `url|name`, or `url|name|py-only` for an input larger than
the browser implementation's limit (VIRTUALIZE.md §11): such inputs are
skipped unless `--large` is given, and then only the reference runs on them.
Store URLs end in `/`.

Implementations are `name=command`, where the command is run as
`command <url> <out>`. The built-in ones write vzip archives: `py` (the
reference, `python -m vzip.virtualize`) and `web` (the browser code under
Node). Others write HARNESS.md's JSON description; give them as
`--impl name=command`.

Usage: uv run python conformance/virtualize/compare.py <out dir>
           [--impl name=command ...] [--no-builtin web] [--quick] [--only <substring>]
           [--fixtures <dir>]  (only the fixture files and stores under <dir>, e.g. from mutate.py)
           [--large]  (also the py-only corpus inputs, with the reference alone)
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shlex
import struct
import subprocess
import sys
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from proxy import Proxy  # noqa: E402

from vzip.pb import Concat, Range, decode_source_table  # noqa: E402

IDR = "https://ftp.ebi.ac.uk/pub/databases/IDR/idr0096-tratwal-marrowquant/20210609-ftp-ome-tiffs/"
BUILTIN = {
    "py": ("vzip", ["uv", "run", "python", "-m", "vzip.virtualize"]),
    "web": ("vzip", ["node", str(ROOT / "web" / "conformance" / "virtualize.ts")]),
}


def from_vzip(path: Path) -> dict:
    """An archive's output (§1.1)."""
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
                ref = [_range(Range.decode(ex[p + 4 : p + 4 + n]))]
            elif hid == 0x7A77:
                ref = [_range(r) for r in Concat.decode(ex[p + 4 : p + 4 + n]).parts]
            p += 4 + n
        if ref is not None:
            entries[info.filename] = ("ranges", ref)
        elif info.filename.endswith("zarr.json"):
            entries[info.filename] = ("json", json.loads(z.read(info)))
        else:
            entries[info.filename] = ("bytes", hashlib.sha256(z.read(info)).hexdigest())
    return {"sources": sources, "entries": entries}


def _range(r: Range) -> list:
    """A range as compared: [source, offset, length], or ["data", base64] for a literal."""
    if r.data is not None:
        return ["data", base64.b64encode(r.data).decode()]
    return [r.source, r.offset, r.length]


def from_json(path: Path) -> dict:
    """A HARNESS.md JSON description's output."""
    d = json.loads(path.read_text())
    entries = {}
    for key, v in d["entries"].items():
        if "ranges" in v:
            entries[key] = ("ranges", [["data", r["data"]] if isinstance(r, dict) else list(r) for r in v["ranges"]])
        elif "json" in v:
            entries[key] = ("json", v["json"])
        else:
            entries[key] = ("bytes", hashlib.sha256(base64.b64decode(v["base64"])).hexdigest())
    return {"sources": d["sources"], "entries": entries}


def differences(a: dict, b: dict) -> list[str]:
    out = []
    if a["sources"] != b["sources"]:
        out.append(f"sources {a['sources']} != {b['sources']}")
    ka, kb = set(a["entries"]), set(b["entries"])
    if ka != kb:
        out.append(f"keys only in reference: {sorted(ka - kb)[:5]}, only in this one: {sorted(kb - ka)[:5]}")
    for k in sorted(ka & kb):
        if a["entries"][k] != b["entries"][k]:
            out.append(f"{k}: reference {json.dumps(a['entries'][k], default=str)[:400]} != "
                       f"{json.dumps(b['entries'][k], default=str)[:400]}")
            if len(out) > 6:
                break
    return out


def run(impl: tuple[str, list[str]], url: str, out: Path) -> tuple[str, object]:
    kind, cmd = impl
    try:
        p = subprocess.run(cmd + [url, str(out)], capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return "crashed", "timeout"
    try:
        if p.returncode == 3:
            return "rejected", (p.stderr.strip().splitlines() or [""])[-1][:300]
        if p.returncode or not out.exists():
            return "crashed", (p.stderr or p.stdout).strip()[-600:]
        return "ok", (from_vzip(out) if kind == "vzip" else from_json(out))
    except Exception as e:  # noqa: BLE001
        return "crashed", f"unreadable output: {type(e).__name__}: {e}"
    finally:
        out.unlink(missing_ok=True)


def check(item: tuple, impls: dict, out_dir: Path) -> dict:
    name, url = item[:2]
    if len(item) > 2 and item[2] == "py-only":
        impls = dict(list(impls.items())[:1])
    results = {n: run(impl, url, out_dir / f"{name}.{n}.out") for n, impl in impls.items()}
    ref_name = next(iter(impls))
    ref = results[ref_name]
    verdicts = {}
    for n, (status, value) in results.items():
        if status == "crashed":
            verdicts[n] = ("CRASHED", [str(value)])
        elif status != ref[0]:
            why = value if status == "rejected" else ref[1] if ref[0] == "rejected" else ""
            verdicts[n] = ("DIVERGES", [f"{ref_name} {ref[0]}, this one {status}: {why}"])
        elif status == "rejected":
            verdicts[n] = ("rejects", [])
        else:
            diffs = differences(ref[1], value)
            verdicts[n] = ("DIVERGES", diffs) if diffs else ("equivalent", [])
    return {"name": name, "url": url, "verdicts": verdicts}


STORE_FORMATS = ("n5", "zarr2")


def store_fixtures(fixtures: Path) -> list[Path]:
    """The synthetic stores: each directory directly under <fixtures>/n5 and <fixtures>/zarr2."""
    return sorted(d for f in STORE_FORMATS if (fixtures / f).is_dir() for d in (fixtures / f).iterdir() if d.is_dir())


def corpus(proxy: Proxy, fixtures: Path, quick: bool, local_only: bool, large: bool = False) -> list[tuple]:
    stores = store_fixtures(fixtures)
    files = [p for p in sorted([*fixtures.rglob("*.tif"), *fixtures.rglob("*.ndpi"), *fixtures.rglob("*.nd2"),
                                *fixtures.rglob("*.dcm"), *fixtures.rglob("*.nii"), *fixtures.rglob("*.ims")])
             if not any(p.is_relative_to(s) for s in stores)]
    items = [(f"fixture-{p.stem}", proxy.local(p.relative_to(fixtures).as_posix())) for p in files]
    items += [(f"fixture-{s.name}", proxy.local_store(s.relative_to(fixtures).as_posix())) for s in stores]
    if local_only:
        return items
    listing = urllib.request.urlopen(IDR, timeout=60).read().decode()
    tiffs = sorted(set(re.findall(r'href="([^"?/][^"]*\.ome\.tiff)"', listing)))
    items += [(f"idr-{i:03d}", proxy.remote(IDR + n)) for i, n in enumerate(tiffs[:3] if quick else tiffs)]
    for corpus_file, prefix in (("corpus_nd2.txt", "nd2-"), ("corpus_tiff.txt", ""), ("corpus_dicom.txt", "dicom-"),
                                ("corpus_nifti.txt", "nifti-"), ("corpus_ims.txt", "ims-"), ("corpus_n5.txt", "n5-"),
                                ("corpus_zarr2.txt", "zarr2-")):
        listed = [line.split("|") for line in (HERE / corpus_file).read_text().split("\n")
                  if line and not line.startswith("#")]
        listed = [x for x in listed if large or x[2:] != ["py-only"]]
        for url, name, *flags in (listed[:3] if quick else listed):
            target = proxy.remote_store(url) if url.endswith("/") else proxy.remote(url)
            items.append((f"{prefix}{name}", target, *flags))
    return items


def main(argv: list[str]) -> int:
    out = Path(argv[0])
    out.mkdir(parents=True, exist_ok=True)
    opts = argv[1:]
    impls = dict(BUILTIN)
    for i, a in enumerate(opts):
        if a == "--impl":
            name, _, cmd = opts[i + 1].partition("=")
            impls[name] = ("json", shlex.split(cmd))
        if a == "--no-builtin":
            impls.pop(opts[i + 1], None)
    only = opts[opts.index("--only") + 1] if "--only" in opts else None
    fixtures = Path(opts[opts.index("--fixtures") + 1]) if "--fixtures" in opts else ROOT / "web" / "test" / "fixtures"
    proxy = Proxy(fixtures, Path("/tmp/vzip-proxy-cache"))
    items = corpus(proxy, fixtures, "--quick" in opts, "--fixtures" in opts, "--large" in opts)
    if only:
        items = [c for c in items if only in c[0]]
    print(f"{len(items)} inputs, implementations: {', '.join(impls)} (reference: {next(iter(impls))})", flush=True)
    results = []
    with ThreadPoolExecutor(3) as pool:
        for r in pool.map(lambda item: check(item, impls, out), items):
            results.append(r)
            line = "  ".join(f"{n}:{v[0]}" for n, v in r["verdicts"].items())
            print(f"{r['name'][:44]:44s} {line}", flush=True)
            for n, (verdict, details) in r["verdicts"].items():
                for d in details[:4]:
                    print(f"      {n}: {d}", flush=True)
    (out / "results.json").write_text(json.dumps(results, indent=1, default=str))
    print()
    for n in impls:
        counts: dict[str, int] = {}
        for r in results:
            counts[r["verdicts"][n][0]] = counts.get(r["verdicts"][n][0], 0) + 1
        print(f"{n:10s} " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    bad = [r for r in results if any(v[0] not in ("equivalent", "rejects") for v in r["verdicts"].values())]
    print(f"\n{len(results)} inputs, {len(bad)} with a divergence or crash")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
