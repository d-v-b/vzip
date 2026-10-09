"""Every node of the given archives that declares a vzip convention, validated against
the convention's JSON Schema (conventions/<profile>/schema.json).
Usage: schemas.py <archive or directory of archives>..."""
import json, sys, zipfile
from pathlib import Path
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
UUIDS = {}
for d in (ROOT / "conventions").iterdir():
    f = d / "schema.json"
    if f.exists():
        s = json.loads(f.read_text())
        UUIDS[s["$defs"]["conventionMetadata"]["properties"]["uuid"]["const"]] = Draft202012Validator(s)
paths = [p for a in sys.argv[1:] for p in (sorted(Path(a).glob("*.vzip")) if Path(a).is_dir() else [Path(a)])]
bad = nodes = 0
for p in paths:
    z = zipfile.ZipFile(p)
    for n in z.namelist():
        if not n.endswith("zarr.json"):
            continue
        doc = json.loads(z.read(n))
        cmos = doc.get("attributes", {}).get("zarr_conventions", [])
        v = next((UUIDS[c["uuid"]] for c in cmos if isinstance(c, dict) and c.get("uuid") in UUIDS), None)
        if v is None:
            continue
        nodes += 1
        errors = [e.message[:200] for e in v.iter_errors(doc)]
        if errors:
            bad += 1
            print(p.name, n, errors[:2])
print(f"{len(paths)} archives, {nodes} declaring nodes, {bad} invalid")
sys.exit(1 if bad else 0)
