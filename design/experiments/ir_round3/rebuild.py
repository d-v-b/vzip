"""Rebuilds each file from its IR archive alone (the mirror's table, checked by the Rust
checker, and vzip_source/bytes) and compares sha-256 with the file's.
Usage: rebuild.py <file>..."""
import hashlib, mmap, sys, tempfile, time
from vzip.ir import virtualize
from vzip.ir.cmirror import rebuild_from_archive
ok = bad = 0
for path in sys.argv[1:]:
    t = time.time()
    _, out, ir = virtualize(path, "https://data.test/" + path.rsplit("/", 1)[-1])
    with tempfile.TemporaryDirectory() as d, open(path, "rb") as fh:
        out.write(d + "/a.vzip")
        m = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        h = hashlib.sha256()
        n = rebuild_from_archive(d + "/a.vzip", lambda o, k: m[o:o + k], h.update)
        same = h.hexdigest() == hashlib.sha256(m).hexdigest() and n == len(m)
    ok += same; bad += not same
    print(f"{path.rsplit('/', 1)[-1][:40]:40s} {'rebuilt' if same else 'DIFFERENT'} {n} bytes {time.time() - t:.1f}s", flush=True)
print(f"rebuilt {ok} different {bad}")
