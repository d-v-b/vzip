"""Sparse local copies of the remote corpus files from the caching proxy's blocks
(fetched once from the remote hosts): each block written at its offset into a file
of the remote's size. The CZI corpus, the Aperio SVS files, and three IDR inputs.
Usage: sparse.py <out dir>"""
import hashlib, os, re, sys
from pathlib import Path
W = Path(__file__).resolve().parents[2]
HERE = Path(os.environ.get("VZIP_R3_WORK", W / "experiments/ir_round3/work"))  # sparse copies, outputs, the IDR listing, round 1's tree
CACHE = Path("/tmp/vzip-proxy-cache")
out = Path(sys.argv[1])
items = []
for corpus, ext in (("corpus_czi.txt", ".czi"), ("corpus_tiff.txt", ".svs")):
    for line in (W / "conformance/virtualize" / corpus).read_text().splitlines():
        if line and not line.startswith("#") and (ext == ".czi" or "Aperio" in line):
            url, name = line.split("|")[:2]
            items.append((name + ext, url))
base = "https://ftp.ebi.ac.uk/pub/databases/IDR/idr0096-tratwal-marrowquant/20210609-ftp-ome-tiffs/"
names = sorted(set(re.findall(r'href="([^"?/][^"]*\.ome\.tiff)"', (HERE / "idr_listing.html").read_text())))
items += [(f"idr-{i:03d}.ome.tiff", base + names[i]) for i in (0, 100, 204)]
for name, url in items:
    h = hashlib.sha256(url.encode()).hexdigest()
    if not (CACHE / f"{h}.size").exists():
        print("NOT CACHED", name, url)
        continue
    size = int((CACHE / f"{h}.size").read_text())
    blocks = sorted(int(p.name.split(".")[1]) for p in CACHE.glob(h + ".*") if not p.name.endswith(".size"))
    with open(out / name, "wb") as f:
        f.truncate(size)
        for i in blocks:
            f.seek(i << 16)
            f.write((CACHE / f"{h}.{i}").read_bytes())
    print(name, size, len(blocks), "blocks")
