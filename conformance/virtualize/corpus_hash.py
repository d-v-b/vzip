"""Fingerprints of the corpus inputs (corpus_*.txt), so that an input that changed
upstream is found as such, rather than read as a divergence or a new rejection.

A corpus line is `url|name[|py-only][|<fingerprint>]`, and the fingerprint is

- for a file, `ends-sha256=<hex>`: the SHA-256 of the file's size in decimal and a
  newline, then its first and its last 64 KiB block (aligned, as proxy.py caches them;
  the one block of a file of at most 64 KiB);
- for a store (its URL ends in `/`), `listing-sha256=<hex>`: the SHA-256 of a line
  `<key>\\t<size>\\n` per object, in key order, keys relative to the store, as proxy.py
  lists it.

A file's fingerprint costs two range requests, however large the file, and a store's
one listing. Both are read through proxy.py's cache (the one compare.py uses), so an
input compare.py has read costs no request at all. A host given with `--cache-only` is
never asked: its inputs are fingerprinted from the cache, or reported as not cached.

Usage: python conformance/virtualize/corpus_hash.py record|check [--cache-only <host>]...
  record: add each line's fingerprint, or replace it (prints the ones that changed);
  check:  exit 1 if a fingerprint differs from the input's, or is missing.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from proxy import BLOCK, RemoteListings, Upstream  # noqa: E402

CACHE = Path("/tmp/vzip-proxy-cache")
KINDS = ("ends-sha256", "listing-sha256")


def parse(line: str) -> tuple[str, str, list[str], str | None]:
    """(url, name, flags, fingerprint or None) of a corpus line."""
    url, name, *rest = line.split("|")
    prints = [f for f in rest if f.partition("=")[0] in KINDS]
    if len(prints) > 1:
        raise ValueError(f"{name}: more than one fingerprint")
    return url, name, [f for f in rest if f not in prints], prints[0] if prints else None


def entries(path: Path) -> list[tuple[str, str, list[str], str | None]]:
    """The parsed lines of a corpus file, without comments and blank lines."""
    return [parse(line) for line in path.read_text().split("\n") if line and not line.startswith("#")]


class NotCached(Exception):
    pass


def _cached(upstream: Upstream, url: str, suffix: str) -> bool:
    return (upstream.cache / f"{hashlib.sha256(url.encode()).hexdigest()}{suffix}").exists()


def fingerprint(upstream: Upstream, url: str, cache_only: bool = False) -> str:
    """The fingerprint of the input at `url` (see the module's docstring)."""
    if url.endswith("/"):
        listings = RemoteListings(upstream)
        key = hashlib.sha256(f"list:{url}|".encode()).hexdigest() + ".json"
        if cache_only and not (upstream.cache / key).exists():
            raise NotCached(url)
        h = hashlib.sha256()
        for k, n in listings.keys(url, ""):
            h.update(f"{k}\t{n}\n".encode())
        return f"listing-sha256={h.hexdigest()}"
    if cache_only and not _cached(upstream, url, ".size"):
        raise NotCached(url)
    size = upstream.size(url)
    blocks = sorted({0, max(size - 1, 0) // BLOCK}) if size else []
    if cache_only and not all(_cached(upstream, url, f".{i}") for i in blocks):
        raise NotCached(url)
    h = hashlib.sha256(f"{size}\n".encode())
    for i in blocks:
        h.update(upstream.block(url, i))
    return f"ends-sha256={h.hexdigest()}"


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("record", "check"):
        print(__doc__, file=sys.stderr)
        return 2
    mode = argv[0]
    cache_only = {argv[i + 1] for i, a in enumerate(argv) if a == "--cache-only"}
    upstream = Upstream(CACHE)
    bad = 0
    for path in sorted(HERE.glob("corpus_*.txt")):
        lines = path.read_text().split("\n")
        for j, line in enumerate(lines):
            if not line or line.startswith("#"):
                continue
            url, name, flags, old = parse(line)
            try:
                new = fingerprint(upstream, url, urllib.parse.urlsplit(url).hostname in cache_only)
            except NotCached:
                print(f"{path.name} {name}: not cached (host {urllib.parse.urlsplit(url).hostname} is --cache-only)")
                bad += mode == "check" and old is None
                continue
            except Exception as e:  # noqa: BLE001
                print(f"{path.name} {name}: unreadable: {type(e).__name__}: {e}")
                bad += 1
                continue
            if new != old:
                print(f"{path.name} {name}: {old or 'no fingerprint'} -> {new}")
                bad += mode == "check"
                lines[j] = "|".join([url, name, *flags, new])
        if mode == "record":
            path.write_text("\n".join(lines))
    print(f"{bad} problems" if mode == "check" else "recorded")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
