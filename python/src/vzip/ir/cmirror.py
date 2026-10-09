"""The source mirror of a compact IR (design/ARCHITECTURE.md §3.4): written by the Rust core
(`rust/vzip-ir/src/mirror.rs`, which documents the table), read back here.

`mirror_compact(ir, out, profile)` adds the mirror's entries under `vzip_source` to
an Output. `rebuild_from_archive(path, read_source, write)` rebuilds the source
from an archive alone: the table (its column runs expanded and its invariants
checked by the Rust checker), whose leaves index the archive's source 0, with no
format code."""

from __future__ import annotations

import json

from vzip.ir.mirror import Archive
from vzip.virtualize.common import Output


def mirror_compact(ir, out: Output, profile: str) -> dict:
    """Adds the mirror of `ir` to `out`; returns what the table folded."""
    import vzip_ir

    t, folded = vzip_ir.mirror(ir, profile)
    _, entries, refs, _, _, _ = t
    for k, v in entries:
        out.bytes_entries[k] = v
    for k, v in refs:
        out.refs[k] = list(v)
    return json.loads(folded)


def load(archive: Archive):
    """The IR the mirror's table describes, as a Rust IR (for check and leaves)."""
    import vzip_ir

    return vzip_ir.Ir.from_archive(archive.get, archive.read_source)


def canonical_problem(archive: Archive) -> str | None:
    """What makes the archive's mirror non-canonical (conventions §8.8), or None: the
    validator's check, which rebuilds the table from what it loads and the source."""
    import vzip_ir

    return vzip_ir.canonical_problem(archive.get, archive.read_source)


def view_problem(archive: Archive) -> str | None:
    """Where the archive's view differs from the one its table and source give
    (conventions §8.7), or None: the validator's check of the view."""
    import vzip_ir

    return vzip_ir.view_problem(archive.z.namelist(), archive.get, archive.read_source)


def rebuild_from_archive(path: str, read_source, write) -> int:
    """The source, from the archive alone: the table's leaves (checked by the Rust
    checker), read from the archive's source 0. No format code is involved."""
    ir = load(Archive(path, read_source))
    ir.check()
    total = 0
    for o, n in ir.leaves():
        while n:
            k = min(n, 1 << 24)
            write(read_source(o, k))
            o, n, total = o + k, n - k, total + k
    return total
