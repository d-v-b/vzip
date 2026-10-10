"""How the mirror's table folds, per format: the IR's rows, the rows the table stores
(the rest are members of column runs), the column runs and their columns by
encoding, over local files. Usage: folds.py <file>..."""
import json, sys
from vzip.ir import virtualize

tot = {}
for f in sys.argv[1:]:
    try:
        fmt, out, _ = virtualize(f, "https://data.test/x")
    except Exception as e:  # noqa: BLE001
        print(f, "rejected", str(e)[:60])
        continue
    d = out.summary["folded"]
    t = tot.setdefault(fmt, {k: 0 for k in d} | {"files": 0})
    t["files"] += 1
    for k, v in d.items():
        t[k] += v
    print(json.dumps({"file": f.rsplit("/", 1)[-1], "format": fmt, **d}))
for fmt, t in tot.items():
    print(fmt, json.dumps(t), f"stored {t['stored'] / t['rows']:.3%} of rows")
