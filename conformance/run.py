"""Run the vzip conformance suite against one or more implementations.

    uv run python conformance/run.py --impl ref="uv run python -m refstore.cli" \\
        --impl rust=/path/to/vzip --out conformance/results/round1

For every implementation:
  1. read:  read every vector (archives written by the reference writer, and
            crafted archives) and compare each query result with the expectations;
  2. write: write every valid description, validate the archive structurally,
            and read it back with every implementation (a cross-read matrix);
  3. reject: every invalid description must be rejected.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import cases  # noqa: E402
from model import Model  # noqa: E402
from validate import validate  # noqa: E402

TIMEOUT = 900


def _matches(result: dict, accept: list[dict]) -> bool:
    for a in accept:
        if a.get("ok") is False:
            if result.get("ok") is False and ("class" not in a or result.get("class") == a["class"]):
                return True
        elif {k: result.get(k) for k in a} == a and result.get("ok") is True:
            return True
    return False


def run_read(cli: list[str], archive: Path, queries: list[dict], workdir: Path) -> dict:
    qpath = workdir / (archive.name + ".queries.json")
    qpath.write_text(json.dumps(queries))
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cli + ["read", str(archive), str(qpath)], capture_output=True,
                           text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"crash": "timeout"}
    dt = time.perf_counter() - t0
    if p.returncode != 0:
        return {"crash": f"exit {p.returncode}: {p.stderr.strip()[-800:]}"}
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"crash": f"stdout is not JSON: {p.stdout[:300]!r} stderr={p.stderr[-300:]!r}"}
    out["seconds"] = dt
    return out


def grade(out: dict, queries: list[dict], accepts: list[list[dict]], open_must: str) -> list[dict]:
    """List of failures: {query, got, accept}. `open_must` is "ok" or "fail"."""
    if "crash" in out:
        return [{"query": "<run>", "got": out["crash"], "accept": "no crash"}]
    opened = bool(out.get("open", {}).get("ok"))
    if open_must == "fail":
        if not opened and out.get("open", {}).get("class") != "archive":
            return [{"query": "<open>", "got": out.get("open"), "accept": "class archive"}]
        return [] if not opened else [
            {"query": "<open>", "got": out.get("open"), "accept": "open fails (archive error)"}]
    if not opened:
        return [{"query": "<open>", "got": out.get("open"), "accept": "open succeeds"}]
    res = out.get("results", [])
    if len(res) != len(queries):
        return [{"query": "<results>", "got": f"{len(res)} results", "accept": f"{len(queries)}"}]
    return [
        {"query": q, "got": r, "accept": a}
        for q, r, a in zip(queries, res, accepts)
        if not _matches(r, a)
    ]


# (query, max GET requests to data objects, pinned?) — spec §6.2
ACCOUNTING = [
    ({"op": "get", "key": "h/plain"}, 1, False),
    ({"op": "get", "key": "h/plain", "range": {"start": 2, "end": 5}}, 1, False),
    ({"op": "get", "key": "h/pinned"}, 1, True),
    ({"op": "get", "key": "h/two_ranges"}, 2, False),
    ({"op": "get", "key": "h/two_ranges", "range": {"start": 0, "end": 1}}, 1, False),
    ({"op": "classify", "key": "h/plain"}, 0, False),
]


def http_accounting(cli, arc: Path, http, out: Path) -> list[dict]:
    """Check the requests a reader sends to resolve http sources (spec §6.2)."""
    fails = []
    for q, max_gets, pinned in ACCOUNTING:
        http.log.clear()
        res = run_read(cli, arc, [q], out / "vectors")
        reqs = [e for e in http.log if "/data/" in e["path"]]
        gets = [e for e in reqs if e["method"] == "GET"]
        problems = []
        if "crash" in res or not res.get("open", {}).get("ok") or not res["results"][0].get("ok"):
            problems.append(f"read failed: {res}")
        if len(reqs) != len(gets):
            problems.append(f"non-GET requests: {[e['method'] for e in reqs]}")
        if len(gets) > max_gets or (max_gets and not gets):
            problems.append(f"{len(gets)} GET requests, expected 1..{max_gets}")
        for e in gets:
            h = e["headers"]
            if not h.get("range", "").startswith("bytes="):
                problems.append("GET without a Range header")
            if h.get("accept-encoding", "").strip().lower() != "identity":
                problems.append(f"Accept-Encoding is {h.get('accept-encoding')!r}, not identity")
            if pinned and not ("if-match" in h and "if-unmodified-since" in h):
                problems.append("pinned source read without If-Match / If-Unmodified-Since")
        if problems:
            fails.append({"query": q, "got": problems, "accept": "spec §6.2 request rules"})
    return fails


def _shape(r):
    """A result without its free-form error message."""
    if not isinstance(r, dict):
        return r
    return {k: v for k, v in r.items() if k != "error"}


def build_vectors(root: Path, ref_cli: list[str], http=None) -> dict[str, dict]:
    """name -> {archive, queries, accepts, open}"""
    vdir = root / "vectors"
    if vdir.exists():
        shutil.rmtree(vdir)
    vdir.mkdir(parents=True)
    cases.write_data_files(vdir)
    vectors = {}
    for name, desc in cases.descriptions(vdir, http_base=http and http.base).items():
        dpath = vdir / f"{name}.json"
        dpath.write_text(json.dumps(desc))
        arc = vdir / f"{name}.vzip"
        p = subprocess.run(ref_cli + ["write", str(dpath), str(arc)], capture_output=True, text=True)
        if p.returncode:
            raise RuntimeError(f"reference writer failed on {name}: {p.stderr}")
        qs = cases.queries_for(name, desc)
        m = Model(desc, arc, http=http and (http.base, root))
        vectors[name] = {"archive": arc, "queries": qs, "accepts": [m.expect(q) for q in qs],
                         "open": "ok"}
    for name, c in cases.crafted(vdir).items():
        arc = vdir / f"crafted_{name}.vzip"
        c["build"](arc)
        if c["read_path"]:
            arc = c["read_path"](arc)
        qs = [q for q, _ in c["expect"]]
        vectors[f"crafted/{name}"] = {"archive": arc, "queries": qs,
                                      "accepts": [a for _, a in c["expect"]], "open": c["open"]}
    return vectors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--impl", action="append", required=True, help="name=command")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ref", default="uv run python -m refstore.cli")
    ap.add_argument("--skip", action="append", default=[], help="skip descriptions by name")
    args = ap.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    impls = dict(i.split("=", 1) for i in args.impl)
    impls = {k: shlex.split(v) for k, v in impls.items()}
    ref_cli = shlex.split(args.ref)
    readers = {"ref": ref_cli, **impls}

    from http_server import Server

    http = Server(out)  # serves out/ (vectors/data/...) for the http profile
    vectors = build_vectors(out, ref_cli, http)
    report: dict = {"read": {}, "write": {}, "reject": {}, "cross": {}, "divergence": []}
    raw_results: dict[tuple[str, str], dict] = {}

    # 1. read vectors
    for impl, cli in impls.items():
        for vname, v in vectors.items():
            res = run_read(cli, v["archive"], v["queries"], out / "vectors")
            fails = grade(res, v["queries"], v["accepts"], v["open"])
            report["read"][f"{impl}:{vname}"] = {"queries": len(v["queries"]), "failures": fails,
                                                  "seconds": res.get("seconds")}
            raw_results[(impl, vname)] = res
            print(f"read   {impl:6s} {vname:40s} {len(v['queries']) - len(fails):4d}/"
                  f"{len(v['queries'])}", flush=True)

    # 1b. divergence: same query, different (each possibly acceptable) behaviour
    if "ref" not in impls:
        for vname, v in vectors.items():
            raw_results[("ref", vname)] = run_read(ref_cli, v["archive"], v["queries"], out / "vectors")
    names = ["ref", *impls]
    for vname, v in vectors.items():
        outs = {n: raw_results.get((n, vname), {}) for n in names}
        opens = {n: _shape(o.get("open")) if "crash" not in o else "crash" for n, o in outs.items()}
        if len(set(json.dumps(x, sort_keys=True) for x in opens.values())) > 1:
            report["divergence"].append({"vector": vname, "query": "<open>", "by_impl": opens})
            continue
        for i, q in enumerate(v["queries"]):
            got = {n: _shape((o.get("results") or [None] * (i + 1))[i]) for n, o in outs.items()}
            if len(set(json.dumps(x, sort_keys=True) for x in got.values())) > 1:
                report["divergence"].append({"vector": vname, "query": q, "by_impl": got})

    # 1c. malformed queries files must make the CLI exit non-zero (HARNESS)
    any_vec = next(iter(vectors.values()))["archive"]
    bad_queries = {
        "unknown_op": [{"op": "frobnicate", "key": "x"}],
        "missing_key": [{"op": "get"}],
        "range_with_two_forms": [{"op": "get", "key": "x", "range": {"offset": 1, "suffix": 2}}],
        "not_an_array": {"op": "get", "key": "x"},
        "range_on_get_raw": [{"op": "get_raw", "key": "x", "range": {"offset": 1}}],
        "list_without_prefix": [{"op": "list"}],
        "range_null": [{"op": "get", "key": "x", "range": None}],
        "partial_range": [{"op": "get", "key": "x", "range": {"start": 1}}],
        "negative_range": [{"op": "get", "key": "x", "range": {"offset": -1}}],
        "duplicate_members": '[{"op": "get", "key": "x", "key": "y"}]',
        "non_string_key": [{"op": "get", "key": 7}],
        "lone_surrogate_key": '[{"op": "get", "key": "a\\ud800"}]',
        "number_2_pow_53": [{"op": "get", "key": "x", "range": {"offset": 2**53}}],
    }
    for impl, cli in impls.items():
        for name, q in bad_queries.items():
            qp = out / "vectors" / f"bad_{name}.json"
            qp.write_text(q if isinstance(q, str) else json.dumps(q))
            p = subprocess.run(cli + ["read", str(any_vec), str(qp)], capture_output=True,
                               text=True, timeout=TIMEOUT)
            report["reject"][f"{impl}:queries/{name}"] = {
                "ok": p.returncode != 0, "exit": p.returncode, "file_left": False,
                "stderr": p.stderr.strip()[-200:]}

    # 1d. http profile: request accounting (spec §6.2), one query per CLI run
    if "http_basic" in vectors:
        arc = vectors["http_basic"]["archive"]
        for impl, cli in impls.items():
            report["read"][f"{impl}:http/request_accounting"] = {
                "queries": len(ACCOUNTING), "failures": http_accounting(cli, arc, http, out)}

    # 2. write + validate + cross-read
    for impl, cli in impls.items():
        wdir = out / "written" / impl
        if wdir.exists():
            shutil.rmtree(wdir)
        wdir.mkdir(parents=True)
        cases.write_data_files(wdir)
        for name, desc in cases.descriptions(wdir, http_base=http.base).items():
            if name in args.skip:
                continue
            dpath = wdir / f"{name}.json"
            dpath.write_text(json.dumps(desc))
            arc = wdir / f"{name}.vzip"
            p = subprocess.run(cli + ["write", str(dpath), str(arc)], capture_output=True,
                               text=True, timeout=TIMEOUT)
            if p.returncode or not arc.exists():
                report["write"][f"{impl}:{name}"] = {"problems": [
                    f"write failed (exit {p.returncode}): {p.stderr.strip()[-500:]}"]}
                print(f"write  {impl:6s} {name:40s} FAILED TO WRITE", flush=True)
                continue
            try:
                problems = validate(arc, desc)
            except Exception as e:  # noqa: BLE001 - a broken archive must not stop the run
                problems = [f"validator crashed: {type(e).__name__}: {e}"]
            report["write"][f"{impl}:{name}"] = {"problems": problems}
            print(f"write  {impl:6s} {name:40s} {'ok' if not problems else f'{len(problems)} problems'}",
                  flush=True)
            qs = cases.queries_for(name, desc)
            m = Model(desc, arc, http=(http.base, out))
            accepts = [m.expect(q) for q in qs]
            for reader, rcli in readers.items():
                res = run_read(rcli, arc, qs, wdir)
                fails = grade(res, qs, accepts, "ok")
                report["cross"][f"{impl}->{reader}:{name}"] = {"queries": len(qs), "failures": fails}
                print(f"cross  {impl:6s} read by {reader:6s} {name:24s} {len(qs) - len(fails):4d}/"
                      f"{len(qs)}", flush=True)

        # 3. invalid descriptions
        for name, desc in cases.invalid_descriptions(wdir).items():
            dpath = wdir / f"invalid_{name}.json"
            dpath.write_text(json.dumps(desc))
            arc = wdir / f"invalid_{name}.vzip"
            p = subprocess.run(cli + ["write", str(dpath), str(arc)], capture_output=True,
                               text=True, timeout=TIMEOUT)
            ok = p.returncode != 0 and not arc.exists()
            report["reject"][f"{impl}:{name}"] = {
                "ok": ok, "exit": p.returncode, "file_left": arc.exists(),
                "stderr": p.stderr.strip()[-300:]}
            print(f"reject {impl:6s} {name:40s} {'ok' if ok else 'NOT REJECTED'}", flush=True)

    (out / "report.json").write_text(json.dumps(report, indent=1, default=str))
    _summary(report, impls, out)
    return 0


def _summary(report: dict, impls: dict, out: Path) -> None:
    lines = ["# Conformance summary", ""]
    lines.append("| impl | read queries passed | write cases valid | rejects | cross-read queries passed "
                 "| http profile (optional) |")
    lines.append("|---|---|---|---|---|---|")
    is_http = lambda k: "http" in k.split(":", 1)[1]  # noqa: E731
    for impl in impls:
        hr = [v for k, v in list(report["read"].items()) + list(report["cross"].items())
              if (k.startswith(impl + ":") or k.startswith(impl + "->")) and is_http(k)]
        hq = sum(v["queries"] for v in hr)
        hf = sum(len(v["failures"]) for v in hr)
        rd = [v for k, v in report["read"].items() if k.startswith(impl + ":") and not is_http(k)]
        rq = sum(v["queries"] for v in rd)
        rf = sum(len(v["failures"]) for v in rd)
        wr = [v for k, v in report["write"].items() if k.startswith(impl + ":")]  # incl. http
        wok = sum(1 for v in wr if not v["problems"])
        rj = [v for k, v in report["reject"].items() if k.startswith(impl + ":")]
        rjok = sum(1 for v in rj if v["ok"])
        cr = [v for k, v in report["cross"].items() if k.startswith(impl + "->") and not is_http(k)]
        cq = sum(v["queries"] for v in cr)
        cf = sum(len(v["failures"]) for v in cr)
        lines.append(f"| {impl} | {rq - rf}/{rq} | {wok}/{len(wr)} | {rjok}/{len(rj)} | {cq - cf}/{cq} "
                     f"| {hq - hf}/{hq} |")
    lines.append("")
    lines.append("## Failures (first 3 per case)")
    for section in ("read", "cross"):
        for k, v in report[section].items():
            if v["failures"]:
                lines.append(f"\n### {section} {k} ({len(v['failures'])} failures)")
                for f in v["failures"][:3]:
                    lines.append(f"- query `{json.dumps(f['query'])}`\n  - got `{json.dumps(f['got'])[:300]}`"
                                 f"\n  - accept `{json.dumps(f['accept'])[:300]}`")
    for k, v in report["write"].items():
        if v["problems"]:
            lines.append(f"\n### write {k}")
            lines += [f"- {p}" for p in v["problems"][:8]]
    for k, v in report["reject"].items():
        if not v["ok"]:
            lines.append(f"\n### reject {k}: exit={v['exit']} file_left={v['file_left']}")
    if report["divergence"]:
        lines.append(f"\n## Divergence between implementations ({len(report['divergence'])} queries)")
        lines.append("Queries where implementations behaved differently, each within what the "
                     "suite accepts: candidates for tightening the spec.")
        for d in report["divergence"][:60]:
            lines.append(f"- {d['vector']} `{json.dumps(d['query'])}`: "
                         + "; ".join(f"{k}={json.dumps(v)[:80]}" for k, v in d["by_impl"].items()))
    (out / "SUMMARY.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:4 + len(impls)]))


if __name__ == "__main__":
    sys.exit(main())
