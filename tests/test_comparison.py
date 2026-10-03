import json
import subprocess
import sys
from pathlib import Path

COMPARE = Path(__file__).parents[1] / "comparison" / "compare.py"


def test_every_format_reads_back_the_source_values(tmp_path):
    out = tmp_path / "results.json"
    p = subprocess.run(
        [sys.executable, str(COMPARE), "--files", "2", "--days", "3", "--grid", "8", "12",
         "--variables", "3", "--latency", "0", "--bandwidth", "0", "--repeats", "1",
         "--work", str(tmp_path / "work"), "--out", str(out)],
        capture_output=True, text=True, timeout=600, check=False,
    )
    assert p.returncode == 0, p.stdout[-2000:] + p.stderr[-2000:]
    rows = json.loads(out.read_text())["rows"]
    assert [(r["format"], r["config"]) for r in rows] == [
        (f, c) for f in ("kerchunk JSON", "kerchunk Parquet", "Icechunk", "vzip")
        for c in ("default", "tuned")]
    assert all(r["problems"] == [] and "everything" in r for r in rows)
