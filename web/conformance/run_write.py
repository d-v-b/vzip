"""Runs the conformance kit's write cases against the browser writer.

For each valid description (conformance/cases.py): write it with
`web/conformance/cli.ts`, check the archive with conformance/validate.py, and
read it back with the reference reader. Descriptions with a page index are
skipped, since this writer does not write one. Each invalid description must
be rejected with no file left behind.

Usage: uv run python web/conformance/run_write.py <out dir>
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "conformance"))

import cases  # noqa: E402
from model import Model  # noqa: E402
from run import grade, run_read  # noqa: E402
from validate import validate  # noqa: E402

CLI = ["node", str(ROOT / "web" / "conformance" / "cli.ts")]
REF = ["uv", "run", "python", "-m", "vzip.cli"]


def main(out: Path) -> int:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    cases.write_data_files(out)
    failures = 0
    for name, desc in cases.descriptions(out).items():
        if desc.get("page_size") is not None:
            print(f"skip   {name:40s} (page index)")
            continue
        dpath = out / f"{name}.json"
        dpath.write_text(json.dumps(desc))
        arc = out / f"{name}.vzip"
        p = subprocess.run(CLI + ["write", str(dpath), str(arc)], capture_output=True, text=True)
        if p.returncode or not arc.exists():
            print(f"write  {name:40s} FAILED: {p.stderr.strip()[-300:]}")
            failures += 1
            continue
        problems = validate(arc, desc)
        qs = cases.queries_for(name, desc)
        m = Model(desc, arc)
        fails = grade(run_read(REF, arc, qs, out), qs, [m.expect(q) for q in qs], "ok")
        failures += bool(problems) + bool(fails)
        print(f"write  {name:40s} {'valid' if not problems else problems[:3]}; "
              f"ref read {len(qs) - len(fails)}/{len(qs)}")
    # The kit's only zip64 case is paged; this one needs zip64 end records
    # because of its entry count alone.
    many = {"sources": [{"url": "blob.bin"}],
            "entries": [{"key": f"c/{i}", "ranges": [{"source": 0, "offset": i % 7, "length": 1}]}
                        for i in range(70000)]}
    dpath, arc = out / "zip64_entries.json", out / "zip64_entries.vzip"
    dpath.write_text(json.dumps(many))
    p = subprocess.run(CLI + ["write", str(dpath), str(arc)], capture_output=True, text=True)
    problems = validate(arc, many) if p.returncode == 0 else [p.stderr.strip()[-300:]]
    failures += bool(problems)
    print(f"write  {'zip64_entries (70000)':40s} {'valid' if not problems else problems[:3]}")

    for name, desc in cases.invalid_descriptions(out).items():
        dpath = out / f"invalid_{name}.json"
        dpath.write_text(json.dumps(desc))
        arc = out / f"invalid_{name}.vzip"
        p = subprocess.run(CLI + ["write", str(dpath), str(arc)], capture_output=True, text=True)
        ok = p.returncode != 0 and not arc.exists()
        failures += not ok
        print(f"reject {name:40s} {'ok' if ok else 'NOT REJECTED ' + p.stderr.strip()[-200:]}")
    print(f"\n{failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
