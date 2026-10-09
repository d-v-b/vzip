"""vzip's intermediate representation (ARCHITECTURE.md §3), the path `vzip.virtualize`
takes for TIFF, ND2 and CZI: the Rust core (rust/vzip-ir, the `vzip_ir` module)
parses, checking each format's source model schema, plans the reads, projects the
convention's hierarchy and mirrors the IR under `vzip_source`; this package is the
host: the I/O (`planner.py`), the archive writer (`output.py` into `Output`), and
the rebuild from an archive (`cmirror.py`). The round-1 Python IR (`model.py`,
`check.py`, `mirror.py`) remains as the core its tests cover."""

from __future__ import annotations

import json

from vzip.virtualize.common import Rejected

TIFF_MAGIC = (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+")
CZI_MAGIC = b"ZISRAWFILE" + bytes(6)
ND2_MAGIC = bytes.fromhex("DACEBE0A")


def sniff(head: bytes) -> str | None:
    """The IR format a file's first 16 bytes give, if any."""
    if head[:4] == ND2_MAGIC:
        return "nd2"
    if head[:4] in TIFF_MAGIC:
        return "tiff"
    if head[:16] == CZI_MAGIC:
        return "czi"
    return None


def parse(fmt: str, transport, conc: int | None = None):
    """(IR, facts, planner counts) of the source the transport reads, or Rejected."""
    import vzip_ir

    from vzip.ir.planner import drive, new_run

    run = new_run(fmt, transport, conc)
    drive(run, transport, conc)
    try:
        ir, facts, stats = run.finish()
    except vzip_ir.Rejected as e:
        raise Rejected(str(e)) from None
    return ir, json.loads(facts), json.loads(stats)


def virtualize(location: str, url: str | None = None, mirror: bool = True, policy=None, transport=None,
               connections: int | None = None, byte_cost: float | None = None):
    """(format, output, IR): the source read through the read planner, parsed by the
    Rust parser of its format, projected and mirrored under vzip_source by the Rust
    core. The output pins source 0's size, and its ETag when every response gave the
    same strong one. `connections`: requests at a time to a remote source (default 8);
    `byte_cost`: seconds of wall time the planner charges a byte fetched (default 0.1 s
    per MB)."""
    import vzip_ir

    from vzip.ir.output import from_rust
    from vzip.ir.planner import drive, new_run, open_transport

    transport = transport or open_transport(location, policy)
    head = transport.head[1][:16] if transport.head[1] else transport.get(0, min(16, transport.size))
    fmt = sniff(head)
    if fmt is None:
        raise Rejected("not a TIFF, CZI or ND2 file")
    run = new_run(fmt, transport, connections, byte_cost)
    drive(run, transport, connections)
    try:
        t, ir = run.output(url or location, mirror)
    except vzip_ir.Rejected as e:
        raise Rejected(str(e)) from None
    out = from_rust(t)
    out.size = transport.size
    out.etag = transport.etag() if url in (None, location) else None
    return fmt, out, ir
