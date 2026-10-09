"""wasm32/native parity: each input through the native parser (PyO3, fed from memory) and
the wasm32 build (Node, wasm/run.mjs): the same IR (digest, elements, runs, check) and
facts, or the same rejection. Usage: parity.py <glob>..."""
import glob, json, subprocess, sys
import vzip_ir
W = str(__import__("pathlib").Path(__file__).resolve().parents[2])
files = sorted({f for g in sys.argv[1:] for f in glob.glob(g)})

def native(path):
    d = open(path, "rb").read()
    head = d[:16]
    P = vzip_ir.Nd2Parser if head[:4] == bytes.fromhex("DACEBE0A") else vzip_ir.TiffParser if head[:2] in (b"II", b"MM") and head[:4] in (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+") else vzip_ir.CziParser
    p = P(len(d))
    try:
        while (b := p.step()) is not None:
            for o, n in b:
                p.feed(o, d[o:o + n])
        ir, facts = p.finish()
    except vzip_ir.Rejected as e:
        return {"rejected": str(e)}
    try:
        ir.check(); ok = True
    except Exception:
        ok = False
    return {"elements": len(ir), "runs": ir.runs_count(), "check": ok, "digest": f"{ir.digest():016x}", "facts": json.loads(facts)}

same = diff = 0
for k in range(0, len(files), 200):
    chunk = files[k:k + 200]
    p = subprocess.run(["node", f"{W}/rust/vzip-ir/wasm/run.mjs", *chunk], capture_output=True, text=True)
    lines = [json.loads(l) for l in p.stdout.splitlines()]
    assert len(lines) == len(chunk), p.stderr[-2000:]
    for f, w in zip(chunk, lines):
        n = native(f)
        w = {k: v for k, v in w.items() if k not in ("file", "rounds", "ranges", "bytes", "ms")}
        if n == w:
            same += 1
        else:
            diff += 1
            print("DIFF", f, json.dumps(n)[:300], "|", json.dumps(w)[:300])
print(f"same {same} different {diff} of {len(files)}")
