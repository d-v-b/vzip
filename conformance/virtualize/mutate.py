"""Writes corrupted copies of the synthetic fixtures, to compare how
implementations treat malformed input (VIRTUALIZE.md §1.2: rejected, never
crashed, and the same decision everywhere).

Each fixture under 256 KiB gets `count` mutants: a byte set to a random
value near the start or end of the file (where headers, IFDs and chunk maps
are), a byte anywhere, or a truncation. The choice is seeded, so a run is
reproducible.

Usage: python conformance/virtualize/mutate.py <out dir> [count] [seed]
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "web" / "test" / "fixtures"


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


def main(argv: list[str]) -> int:
    out = Path(argv[0])
    count = int(argv[1]) if len(argv) > 1 else 10
    rng = random.Random(int(argv[2]) if len(argv) > 2 else 0)
    out.mkdir(parents=True, exist_ok=True)
    for p in sorted([*FIXTURES.glob("*.tif"), *FIXTURES.glob("*.nd2")]):
        data = p.read_bytes()
        if len(data) > 256 * 1024 or len(data) < 2:
            continue
        for k in range(count):
            (out / f"{p.stem}.m{k}{p.suffix}").write_bytes(mutate(data, rng))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
