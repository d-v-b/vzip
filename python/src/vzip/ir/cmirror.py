"""The source mirror of a compact IR (ARCHITECTURE.md §3.4): written by the Rust core
(`rust/vzip-ir/src/mirror.rs`, which documents the table), read back here.

`Archive` reads an archive's entries with its references resolved.
`rebuild_from_archive(path, read_source, write)` rebuilds the source from an archive
alone: the table (its column runs expanded and its invariants checked by the Rust
checker), whose leaves index the archive's source 0, with no format code."""

from __future__ import annotations

import struct
import zipfile


class Archive:
    """A vzip archive's entries, references resolved: `read_source(offset, length)`
    reads the archive's source 0 (the file the archive describes)."""

    def __init__(self, path: str, read_source) -> None:
        from vzip.pb import Concat, Range, decode_source_table

        self.z = zipfile.ZipFile(path)
        self.sources = decode_source_table(self.z.read("__vz__/sources"))
        self.read_source = read_source
        self.refs = {}
        for info in self.z.infolist():
            ex, p = info.extra, 0
            while p < len(ex):
                hid, k = struct.unpack_from("<HH", ex, p)
                if hid == 0x7A76:
                    self.refs[info.filename] = [Range.decode(ex[p + 4:p + 4 + k])]
                elif hid == 0x7A77:
                    self.refs[info.filename] = list(Concat.decode(ex[p + 4:p + 4 + k]).parts)
                p += 4 + k

    def get(self, key: str) -> bytes | None:
        if key in self.refs:
            out = bytearray()
            for r in self.refs[key]:
                if r.data is not None:
                    out += r.data
                elif r.source == 0:
                    out += self.read_source(r.offset, r.length)
                else:
                    out += self.sources[r.source].data[r.offset:r.offset + r.length]
            return bytes(out)
        try:
            return self.z.read(key)
        except KeyError:
            return None


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
