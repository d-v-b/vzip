"""Compares virtualizers (VIRTUALIZE.md §1.1, §14; HARNESS.md).

Every implementation runs on every input of the corpus, which the caching
proxy (proxy.py) serves over local HTTP: the synthetic files in
web/test/fixtures/ (one directory per format), the synthetic stores in
web/test/fixtures/n5/, zarr2/ and ome-zarr/ (one directory per store,
served with the S3 listing of VIRTUALIZE.md §1.5), the synthetic SAFE
products in web/test/fixtures/safe/ (directories, and .SAFE.zip files), the
synthetic CZI files in web/test/fixtures/czi/, the 205 OME-TIFFs of IDR
idr0096, and the public files and stores in corpus_*.txt. For each input, all
implementations must either reject it (exit status 3) or produce equivalent
outputs; outputs are compared with the reference implementation's (the first
one).

Corpus lines are `url|name`, or `url|name|py-only` for an input larger than
the browser implementation's limit (VIRTUALIZE.md §14): such inputs are
skipped unless `--large` is given, and then every implementation but `web` runs on them.
Store URLs end in `/`. A line may end with the input's fingerprint, which
corpus_hash.py records and checks.

Implementations are `name=command`, where the command is run as
`command <url> <out>`. The built-in ones write vzip archives: `ref` (the
reference, the frozen Python profiles of conformance/virtualize/reference/ for
TIFF, ND2 and CZI and the shipped ones for the others), `py` (`python -m
vzip.virtualize`) and `web` (the browser code under Node). Others write
HARNESS.md's JSON description; give them as `--impl name=command`.

`py` and `web` virtualize TIFF, ND2 and CZI through one Rust core (rust/vzip-ir,
natively and as wasm32), and write its mirror under `vzip_source`, which the
frozen reference does not: for those profiles an implementation is compared with
the reference outside `vzip_source`, and `py` and `web` with each other entirely,
entry for entry (they must give the same archive contents, `vzip_source` included).
The frozen reference records the revision it implements (20) in its root, where the
shipped code records the current one: compared with it, the reference's root is read
at the current revision. Each mirror is also checked on its own (`mirror_problem`):
it reads, it rebuilds a source of source 0's size, it is canonical, and its view is the
one its table and the source give.

Usage: uv run python conformance/virtualize/compare.py <out dir>
           [--impl name=command ...] [--no-builtin web] [--quick] [--only <substring>]
           [--fixtures <dir>]  (only the fixture files and stores under <dir>, e.g. from mutate.py)
           [--large]  (also the py-only corpus inputs, without web)
           [--shard k/n]  (only every n-th input, from the k-th: one of n parallel runs)
           [--idr-listing <file>]  (idr0096's listing, saved, instead of fetching it)

Every archive is also read by SPEC.md's strict reading (`strict`): an archive the
independent reader refuses, or reads differently, or that breaks a writer requirement,
counts as a crash of the implementation that wrote it.
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
sys.path.insert(0, str(ROOT / "conformance"))
sys.path.insert(0, str(ROOT / "impls" / "python"))
from corpus_hash import entries as corpus_entries  # noqa: E402
from proxy import Proxy  # noqa: E402
from validate import validate  # noqa: E402
from vzip_impl import reader as strict_reader  # noqa: E402
from vzip_impl.errors import VzError  # noqa: E402

from vzip.pb import Concat, Range, Source, decode_source_table  # noqa: E402
from vzip.virtualize.common import REVISION, same  # noqa: E402

IDR = "https://ftp.ebi.ac.uk/pub/databases/IDR/idr0096-tratwal-marrowquant/20210609-ftp-ome-tiffs/"
BUILTIN = {
    # the inputs are served from 127.0.0.1, which the reader policy refuses by default (SPEC.md §8.7)
    "ref": ("vzip", ["uv", "run", "python", str(HERE / "reference" / "cli.py"), "--allow-private-hosts"]),
    "py": ("vzip", ["uv", "run", "python", "-m", "vzip.virtualize", "--allow-private-hosts"]),
    "web": ("vzip", ["node", str(ROOT / "web" / "conformance" / "virtualize.ts"), "--allow-private-hosts"]),
}
# The profiles whose shipped implementations write the IR's mirror under vzip_source.
IR_PROFILES = ("tiff", "nd2", "czi")
SOURCE = "vzip_source/"
# The revision the frozen reference's TIFF, ND2 and CZI roots record (HARNESS.md).
REFERENCE_REVISION = 20


class Unreadable(Exception):
    """An archive that SPEC.md's strict reading refuses, or that breaks a writer requirement."""


def _no_duplicates(pairs: list) -> dict:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise Unreadable(f"a JSON document has a duplicate member: {sorted(k for k in keys if keys.count(k) > 1)[:3]}")
    return dict(pairs)


def from_vzip(path: Path) -> dict:
    """An archive's output (§1.1), once it has passed `strict`."""
    try:  # first, so that zipfile never reads what SPEC.md does not
        strict_reader.Archive(str(path)).close()
    except VzError as e:
        raise Unreadable(f"strict reader: {e.cls} error: {e}") from e
    view = _zip_view(path)
    problems = strict(path, view)
    if problems:
        raise Unreadable("; ".join(problems[:5]))
    return view


def _zip_view(path: Path) -> dict:
    """An archive's output as zipfile reads it."""
    z = zipfile.ZipFile(path)
    sources = [_source(s) for s in decode_source_table(z.read("__vz__/sources"))]
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
            entries[info.filename] = ("json", json.loads(z.read(info), object_pairs_hook=_no_duplicates))
        else:
            entries[info.filename] = ("bytes", hashlib.sha256(z.read(info)).hexdigest())
    return {"sources": sources, "entries": entries}


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def strict(path: Path, view: dict) -> list[str]:
    """The problems SPEC.md finds in an archive, by code that shares nothing with the
    writers under test: the independent reader (impls/python, written from SPEC.md
    alone) must open it and see exactly the output `view` holds, and the archive must
    meet the writer requirements (SPEC.md §3, §4, §7, §9) as conformance/validate.py,
    with its own ZIP parser, checks them: canonical payloads, reference bodies,
    duplicate names, hidden entries, CRC-32s, local headers and the page index."""
    try:
        archive = strict_reader.Archive(str(path))
    except VzError as e:
        return [f"strict reader: {e.cls} error: {e}"]
    problems: list[str] = []
    with archive:
        sources = [s.value if s.kind == "url" else {s.kind: _b64(s.value) if s.kind == "data" else s.value}
                   for s in archive.sources]
        if sources != view["sources"]:
            problems.append(f"strict reader: sources {sources[:3]} != {view['sources'][:3]}")
        keys = archive.list("")
        if set(keys) != set(view["entries"]):
            problems.append(f"strict reader: keys only it lists {sorted(set(keys) - set(view['entries']))[:5]}, "
                            f"keys only zipfile lists {sorted(set(view['entries']) - set(keys))[:5]}")
        desc_sources = [{s.kind: s.value.hex() if s.kind == "data" else s.value,
                         **{k: getattr(s, k) for k in ("size", "etag") if getattr(s, k) is not None},
                         **({"modified_not_after": s.mnf} if s.mnf is not None else {})} for s in archive.sources]
        entries, mirrored = [], set()
        for key in keys:
            try:
                e = archive._lookup(key.encode())
                if e.kind == "reference":
                    parts, _ = strict_reader.decode_payload(e.ref_id, e.payload, len(archive.sources))
                    ranges = [["data", _b64(p.data)] if p.data is not None else [p.source, p.offset, p.length]
                              for p in parts]
                    if view["entries"].get(key) != ("ranges", ranges):
                        problems.append(f"strict reader: {key}: ranges {ranges[:3]} differ from zipfile's")
                    mirrored.add(archive.raw(key) != b"")
                    entries.append({"key": key, "ranges": [
                        {"data": p.data.hex()} if p.data is not None else
                        {"source": p.source, "offset": p.offset, "length": p.length} for p in parts]})
                else:
                    value = archive.get(key)
                    kind, seen = view["entries"].get(key, (None, None))
                    if kind == "json" and json.loads(value) != seen or kind == "bytes" and (
                            hashlib.sha256(value).hexdigest() != seen) or kind not in ("json", "bytes"):
                        problems.append(f"strict reader: {key}: bytes differ from zipfile's")
                    entries.append({"key": key, "bytes": value.hex(), "compress": e.method == 8,
                                    "pinned": archive.paged and key.encode() in archive.pinned})
            except VzError as e:
                problems.append(f"strict reader: {key}: {e.cls} error: {e}")
        if len(mirrored) > 1:
            problems.append("some reference bodies are empty and others are not")
        desc = {"sources": desc_sources, "entries": entries, "mirror": mirrored != {False},
                "page_size": 1 if archive.paged else None}
    problems += [f"writer requirement: {p}" for p in validate(path, desc)]
    return problems


def _source(s: Source) -> str | dict:
    """A source as compared: its URL, or {"data": base64} for a data source."""
    if s.url is not None:
        return s.url
    if s.data is not None:
        return {"data": base64.b64encode(s.data).decode()}
    return {"key": s.key}


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
    if not same(a["sources"], b["sources"]):
        out.append(f"sources {a['sources']} != {b['sources']}")
    ka, kb = set(a["entries"]), set(b["entries"])
    if ka != kb:
        out.append(f"keys only in reference: {sorted(ka - kb)[:5]}, only in this one: {sorted(kb - ka)[:5]}")
    for k in sorted(ka & kb):
        if not same(a["entries"][k], b["entries"][k]):  # §1.1: integers exactly
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
        view = from_vzip(out) if kind == "vzip" else from_json(out)
        if kind == "vzip":
            problem = mirror_problem(out, view)
            if problem:
                return "crashed", f"invalid IR mirror: {problem}"
        return "ok", view
    except Exception as e:  # noqa: BLE001
        return "crashed", f"unreadable output: {type(e).__name__}: {e}"
    finally:
        out.unlink(missing_ok=True)


def mirror_problem(path: Path, view: dict, read=None) -> str | None:
    """For an archive whose vzip_source is an IR mirror (conventions §8): None when its
    table reads, its invariants hold, it describes a source of source 0's size, it is
    canonical (§8.8, rebuilt from what it loads, it is the same table) and its view is
    the one the table and the source give (§8.7); else what is wrong. The array
    columns' bytes and the view's values are read from source 0 (small ranges;
    `read(offset, length)`, by default its URL). These are checks in addition to
    comparing mirrors entry for entry (VIRTUALIZE.md §1.1)."""
    doc = view["entries"].get("vzip_source/zarr.json")
    own = (doc[1].get("attributes", {}).get("vzip_virtualized", {}) if doc and doc[0] == "json" else {})
    if not any(isinstance(v, dict) and "ir" in v for v in own.values()):
        return None
    from vzip.ir.cmirror import canonical_problem, load, view_problem
    from vzip.ir.mirror import Archive

    url = view["sources"][0]

    def fetch(o: int, n: int) -> bytes:
        req = urllib.request.Request(url, headers={"Range": f"bytes={o}-{o + n - 1}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()

    read = read or fetch

    try:
        ir = load(Archive(str(path), read))
        ir.check()
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    pinned = _source_size(path)
    if pinned is not None and ir.size != pinned:
        return f"the mirror describes {ir.size} bytes, source 0 pins {pinned}"
    problem = canonical_problem(Archive(str(path), read))
    if problem:
        return f"not canonical: {problem}"
    return view_problem(Archive(str(path), read))


def _source_size(path: Path) -> int | None:
    z = zipfile.ZipFile(path)
    sources = decode_source_table(z.read("__vz__/sources"))
    return sources[0].size if sources else None


def check(item: tuple, impls: dict, out_dir: Path) -> dict:
    name, url = item[:2]
    if len(item) > 2 and item[2] == "py-only":  # past the browser's limit: every implementation but web
        impls = {k: v for k, v in impls.items() if k != "web"}
    results = {n: run(impl, url, out_dir / f"{name}.{n}.out") for n, impl in impls.items()}
    ref_name = next(iter(impls))
    ref = results[ref_name]
    mirror = ref[0] == "ok" and profile(ref[1]) in IR_PROFILES
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
            diffs = differences(without_source(at_revision(ref[1])), without_source(value)) \
                if mirror and n != ref_name else differences(ref[1], value)
            verdicts[n] = ("DIVERGES", diffs) if diffs else ("equivalent", [])
    # one core, two hosts: py and web give the same archive contents, the mirror included
    if mirror and all(results.get(n, ("",))[0] == "ok" for n in ("py", "web")) and \
            verdicts["web"][0] == "equivalent":
        diffs = differences(results["py"][1], results["web"][1])
        if diffs:
            verdicts["web"] = ("DIVERGES", [f"from py: {d}" for d in diffs])
    return {"name": name, "url": url, "verdicts": verdicts}


def profile(output: dict) -> str | None:
    """The profile an output's root declares."""
    root = output["entries"].get("zarr.json")
    if not root or root[0] != "json":
        return None
    return (root[1].get("attributes", {}).get("vzip_virtualized") or {}).get("profile")


def at_revision(output: dict) -> dict:
    """The output with its root's `revision` the current one when it is the frozen
    reference's (REFERENCE_REVISION), for comparing the reference with shipped code."""
    root = output["entries"].get("zarr.json")
    prop = root[1].get("attributes", {}).get("vzip_virtualized") if root and root[0] == "json" else None
    if not isinstance(prop, dict) or prop.get("revision") != REFERENCE_REVISION:
        return output
    attrs = {**root[1]["attributes"], "vzip_virtualized": {**prop, "revision": REVISION}}
    return {**output, "entries": {**output["entries"], "zarr.json": (root[0], {**root[1], "attributes": attrs})}}


def without_source(output: dict) -> dict:
    """An output without the entries under vzip_source."""
    return {"sources": output["sources"],
            "entries": {k: v for k, v in output["entries"].items() if not k.startswith(SOURCE)}}


STORE_FORMATS = ("n5", "zarr2", "ome-zarr", "safe")


def store_fixtures(fixtures: Path) -> list[Path]:
    """The synthetic stores: each directory directly under <fixtures>/n5, zarr2 and ome-zarr."""
    return sorted(d for f in STORE_FORMATS if (fixtures / f).is_dir() for d in (fixtures / f).iterdir() if d.is_dir())


def corpus(proxy: Proxy, fixtures: Path, quick: bool, local_only: bool, large: bool = False,
           idr_listing: str | None = None) -> list[tuple]:
    stores = store_fixtures(fixtures)
    files = [p for p in sorted([*fixtures.rglob("*.tif"), *fixtures.rglob("*.ndpi"), *fixtures.rglob("*.nd2"),
                                *fixtures.rglob("*.dcm"), *fixtures.rglob("*.nii"), *fixtures.rglob("*.ims"),
                                *fixtures.rglob("*.zip"), *fixtures.rglob("*.czi")])
             if not any(p.is_relative_to(s) for s in stores)]
    items = [(f"fixture-{p.stem}", proxy.local(p.relative_to(fixtures).as_posix())) for p in files]
    items += [(f"fixture-{s.name}", proxy.local_store(s.relative_to(fixtures).as_posix())) for s in stores]
    if local_only:
        return items
    listing = (Path(idr_listing).read_text() if idr_listing else
               urllib.request.urlopen(IDR, timeout=60).read().decode())
    tiffs = sorted(set(re.findall(r'href="([^"?/][^"]*\.ome\.tiff)"', listing)))
    items += [(f"idr-{i:03d}", proxy.remote(IDR + n)) for i, n in enumerate(tiffs[:3] if quick else tiffs)]
    for corpus_file, prefix in (("corpus_nd2.txt", "nd2-"), ("corpus_tiff.txt", ""), ("corpus_dicom.txt", "dicom-"),
                                ("corpus_nifti.txt", "nifti-"), ("corpus_ims.txt", "ims-"), ("corpus_n5.txt", "n5-"),
                                ("corpus_zarr2.txt", "zarr2-"), ("corpus_ome_zarr.txt", "ome-zarr-"),
                                ("corpus_safe.txt", "safe-"), ("corpus_czi.txt", "czi-")):
        listed = [(url, name, *flags) for url, name, flags, _ in corpus_entries(HERE / corpus_file)]
        listed = [x for x in listed if large or x[2:] != ("py-only",)]
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
    listing = opts[opts.index("--idr-listing") + 1] if "--idr-listing" in opts else None
    items = corpus(proxy, fixtures, "--quick" in opts, "--fixtures" in opts, "--large" in opts, listing)
    if only:
        items = [c for c in items if only in c[0]]
    if "--shard" in opts:  # k/n: every n-th input from the k-th (1-based), for parallel CI jobs
        k, n = map(int, opts[opts.index("--shard") + 1].split("/"))
        if not 1 <= k <= n:
            raise SystemExit(f"--shard {k}/{n}: k must be from 1 to n")
        items = items[k - 1 :: n]
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
