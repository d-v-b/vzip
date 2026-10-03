"""Writes corrupted copies of the synthetic fixtures, to compare how
implementations treat malformed input (VIRTUALIZE.md §1.2: rejected, never
crashed, and the same decision everywhere).

Each fixture file under 256 KiB gets `count` mutants: a byte set to a random
value near the start or end of the file (where headers, IFDs and chunk maps
are), a byte anywhere, or a truncation.

Each synthetic store (a directory under web/test/fixtures/n5 or zarr2) gets
`count` mutants, copied to <out dir>/<format>/<name>.m<k>/, each with one
change: a metadata document (attributes.json, .zarray, .zgroup, .zattrs)
mutated as bytes, or as JSON (a member's value replaced by a value of
another type or an edge value, or the member removed); a chunk object
truncated, emptied, deleted or changed; an object deleted; or a stray
metadata document added.

The choices are seeded, so a run is reproducible.

Usage: python conformance/virtualize/mutate.py <out dir> [count] [seed]
"""

from __future__ import annotations

import json
import random
import shutil
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "web" / "test" / "fixtures"
STORE_FORMATS = ("n5", "zarr2")
METADATA = ("attributes.json", ".zarray", ".zgroup", ".zattrs")
EDGE_VALUES = [None, -1, 0, 1, 2, 2**31, 2**53, 1.5, -0.0, 1e308, True, "", "x", "NaN", "raw", "gzip", "blosc",
               "uint8", "<u2", "|b1", "F", "/", [], [1], [0, 0], {}, {"type": "raw"}, {"id": "zlib"}]


def mutate(data: bytes, rng: random.Random) -> bytes:
    kind = rng.random()
    if kind < 0.2:
        return data[: rng.randrange(len(data))]
    out = bytearray(data)
    if kind < 0.8:
        edge = min(len(data), 4096)
        i = rng.randrange(edge) if rng.random() < 0.5 else len(data) - 1 - rng.randrange(edge)
    else:
        i = rng.randrange(len(data))
    out[i] = rng.choice([0, 1, 0xFF, 0x80, rng.randrange(256), out[i] ^ (1 << rng.randrange(8))])
    return bytes(out)


def _paths(v, prefix=()):
    """Every (path, value) inside a JSON value."""
    yield prefix, v
    if isinstance(v, dict):
        for k, x in v.items():
            yield from _paths(x, prefix + (k,))
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from _paths(x, prefix + (i,))


def mutate_json(data: bytes, rng: random.Random) -> bytes:
    try:
        doc = json.loads(data)
    except ValueError:
        return mutate(data, rng) if data else b"{"
    paths = [p for p, _ in _paths(doc) if p]
    if not paths:
        return json.dumps(rng.choice(EDGE_VALUES)).encode()
    path = rng.choice(paths)
    parent = doc
    for k in path[:-1]:
        parent = parent[k]
    if rng.random() < 0.25 and isinstance(parent, dict):
        del parent[path[-1]]
    elif rng.random() < 0.15 and isinstance(parent, list):
        parent.pop(path[-1])
    else:
        parent[path[-1]] = rng.choice(EDGE_VALUES)
    return json.dumps(doc).encode()


def mutate_store(src: Path, dst: Path, rng: random.Random) -> None:
    shutil.copytree(src, dst)
    files = sorted(p for p in dst.rglob("*") if p.is_file())
    meta = [p for p in files if p.name in METADATA]
    chunks = [p for p in files if p.name not in METADATA]
    kind = rng.random()
    if meta and kind < 0.3:
        f = rng.choice(meta)
        f.write_bytes(mutate_json(f.read_bytes(), rng))
    elif meta and kind < 0.5:
        f = rng.choice(meta)
        data = f.read_bytes()
        f.write_bytes(mutate(data, rng) if data else b"\xff")
    elif chunks and kind < 0.75:
        f = rng.choice(chunks)
        data = f.read_bytes()
        choice = rng.random()
        if choice < 0.3:
            f.write_bytes(data[: rng.randrange(len(data) + 1)])
        elif choice < 0.5:
            f.unlink()
        else:
            f.write_bytes(mutate(data, rng) if data else b"x")
    elif files and kind < 0.88:
        rng.choice(files).unlink()
    else:
        dirs = [dst] + sorted(p for p in dst.rglob("*") if p.is_dir())
        d = rng.choice(dirs)
        name = rng.choice(METADATA + ("0", "0.0", "0/0"))
        target = d / name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except (FileExistsError, NotADirectoryError):
            return
        if not target.exists():
            target.write_bytes(rng.choice([b"", b"{}", b'{"zarr_format": 2}', b'{"n5": "4.0.0"}', b"x"]))


def main(argv: list[str]) -> int:
    out = Path(argv[0])
    count = int(argv[1]) if len(argv) > 1 else 10
    rng = random.Random(int(argv[2]) if len(argv) > 2 else 0)
    out.mkdir(parents=True, exist_ok=True)
    stores = sorted(d for f in STORE_FORMATS if (FIXTURES / f).is_dir() for d in (FIXTURES / f).iterdir() if d.is_dir())
    for p in sorted([*FIXTURES.rglob("*.tif"), *FIXTURES.rglob("*.ndpi"), *FIXTURES.rglob("*.nd2"), *FIXTURES.rglob("*.dcm"), *FIXTURES.rglob("*.nii"),
                     *FIXTURES.rglob("*.ims")]):
        if any(p.is_relative_to(s) for s in stores):
            continue
        data = p.read_bytes()
        if len(data) > 256 * 1024 or len(data) < 2:
            continue
        for k in range(count):
            (out / f"{p.stem}.m{k}{p.suffix}").write_bytes(mutate(data, rng))
    for s in stores:
        for k in range(count):
            mutate_store(s, out / s.parent.name / f"{s.name}.m{k}", rng)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
