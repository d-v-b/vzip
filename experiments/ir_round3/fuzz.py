"""Random mutations of the TIFF and CZI fixtures through the Rust parsers in memory:
each must be accepted (an IR the checker passes) or rejected, never panic.
Usage: fuzz.py <count per fixture> <seed>"""
import glob, random, sys, time
sys.path.insert(0, "conformance/virtualize")
from mutate import mutate
import vzip_ir
n, seed = int(sys.argv[1]), int(sys.argv[2])
rng = random.Random(seed)
stats = {"ok": 0, "rejected": 0, "panic": 0, "violation": 0}
t = time.time()
for path in sorted(glob.glob("web/test/fixtures/tiff/*") + glob.glob("web/test/fixtures/czi/*")):
    base = open(path, "rb").read()
    if len(base) > 256 * 1024:
        continue
    P = vzip_ir.CziParser if path.endswith(".czi") else vzip_ir.TiffParser
    for k in range(n):
        d = mutate(base, rng)
        for _ in range(rng.randrange(3)):
            if len(d) > 2:
                d = mutate(d, rng)
        p = P(len(d))
        try:
            while (b := p.step()) is not None:
                for o, m in b:
                    p.feed(o, d[o:o + m])
            ir, _ = p.finish()
            try:
                ir.check()
                stats["ok"] += 1
            except Exception as e:
                stats["violation"] += 1
                print("VIOLATION", path, k, e)
        except vzip_ir.Rejected:
            stats["rejected"] += 1
        except BaseException as e:
            stats["panic"] += 1
            open(f"panic_{stats['panic']}.bin", "wb").write(d)  # in the working directory
            print("PANIC", path, k, type(e).__name__, str(e)[:200])
print(stats, round(time.time() - t, 1), "s")
