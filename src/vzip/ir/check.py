"""The one invariant checker (ARCHITECTURE.md §3.3), and the generic rebuild."""

from __future__ import annotations

from typing import Callable

from vzip.ir.model import ALIAS, IR, KINDS, LEAVES


class Violation(Exception):
    """An IR that breaks an invariant; the message names the first violation."""


def _who(ir: IR, i: int) -> str:
    return f"element {i} ({KINDS[ir.kind[i]]} {ir.path(i)!r})"


def check(ir: IR) -> None:
    """Coverage and injectivity: the extents of the leaves partition [0, size).
    Also: parents come before their children, and aliases name an element."""
    n = len(ir)
    for i in range(n):
        p = ir.parent[i]
        if (i == 0) != (p < 0) or p >= i:
            raise Violation(f"{_who(ir, i)} has parent {p}")
        if ir.kind[i] == ALIAS:
            seen, j = {i}, ir.target[i]
            while 0 <= j < n and ir.kind[j] == ALIAS and j not in seen:
                seen.add(j)
                j = ir.target[j]
            if not 0 <= j < n or j in seen:
                raise Violation(f"{_who(ir, i)} names no element (target {ir.target[i]})")
    starts, ends, ids = ir.spans()
    pos, last = 0, None
    for o, e, i in zip(starts, ends, ids):
        if e <= o:
            raise Violation(f"{_who(ir, i)} has the empty extent [{o}, {e})")
        if o < 0 or e > ir.size:
            raise Violation(f"{_who(ir, i)} claims [{o}, {e}), outside [0, {ir.size})")
        if o < pos:
            raise Violation(f"bytes [{o}, {min(e, pos)}) are claimed by {_who(ir, last)} and {_who(ir, i)}")
        if o > pos:
            raise Violation(f"bytes [{pos}, {o}) are claimed by no element (next: {_who(ir, i)})")
        pos, last = e, i
    if pos != ir.size:
        raise Violation(f"bytes [{pos}, {ir.size}) are claimed by no element")


def leaves_in_order(ir: IR) -> list[tuple[int, int]]:
    """The leaves' extents in source order: (offset, length)."""
    starts, ends, _ = ir.spans()
    return [(o, e - o) for o, e in zip(starts, ends)]


def rebuild(ir: IR, read: Callable[[int, int], bytes], write: Callable[[bytes], object]) -> int:
    """The source, rebuilt from the IR's extents with no format knowledge: the
    leaves in extent order, concatenated. Returns the bytes written."""
    check(ir)
    total = 0
    for o, m in leaves_in_order(ir):
        write(read(o, m))
        total += m
    return total
