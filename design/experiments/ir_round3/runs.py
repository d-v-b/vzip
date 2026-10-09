"""How runs fold: per input, data elements (tiles, strips, subblock data) and how many
are members of runs, the runs, and the IR's rows and memory."""
import sys, glob, json
from vzip.ir import parse, sniff
from vzip.ir.planner import open_transport
for path in sorted({f for g in sys.argv[1:] for f in glob.glob(g)}):
    t = open_transport(path)
    try:
        ir, facts, _ = parse(sniff(t.get(0, min(16, t.size))), t)
    except Exception as e:
        print(path.rsplit("/", 1)[-1], "rejected", str(e)[:60]); continue
    n = len(ir)
    runs = ir.runs()
    kinds = bytes(ir.columns()["kind"])
    data_rows = kinds.count(2)
    in_runs = sum(c for r, c, s in runs if kinds[r] == 2)
    data_run_roots = sum(1 for r, c, s in runs if kinds[r] == 2)
    struct_runs = sum(c for r, c, s in runs if kinds[r] == 0)
    expanded = data_rows - data_run_roots + in_runs
    print(f"{path.rsplit('/', 1)[-1][:36]:36s} rows {n:8d} runs {len(runs):6d} data {expanded:8d} in data runs {in_runs:8d} ({100 * in_runs / max(1, expanded):5.1f}%) structs folded {struct_runs:8d} mem {ir.memory() >> 10:7d} KiB {ir.memory() / max(1, n):6.1f} B/row")
