"""What every projection shares: references to IR elements, checked against the
IR (ARCHITECTURE.md §3.3, "the end of the amplification class")."""

from __future__ import annotations

from vzip.ir.model import ALIAS, IR
from vzip.virtualize.common import MAX_PAYLOAD, Output, Rejected, payload_size


class Refs:
    """Writes chunk references to IR elements into an Output. Every reference is
    charged to the budget; a second reference to bytes already referenced (which
    the IR can only reach through an alias, since it claims no byte twice) is
    charged to the budget's `repeats`, whatever the format."""

    def __init__(self, ir: IR, out: Output) -> None:
        self.ir, self.out = ir, out
        self.seen: set = set()

    def parts(self, i: int) -> list:
        """An element's recipe, as Output parts."""
        out = []
        for p in self.ir.info[i]["recipe"]:
            if p[0] == "src":
                out.append((p[1], p[2]))
            elif p[0] == "lit":
                out.append(bytes(p[1]))
            else:
                out.append(self.out.shared(self.ir.shared[p[1]]))
        return out

    def chunk(self, key: str, i: int, parts: list | None = None, piece: int = 0) -> None:
        """The chunk `key` is element `i` (its recipe, or `parts` of it: `piece` k of it)."""
        ir = self.ir
        owner = ir.target[i] if ir.kind[i] == ALIAS else i
        ir.budget.charge("refs")
        if (owner, piece) in self.seen:
            ir.budget.charge("repeats")
        self.seen.add((owner, piece))
        parts = self.parts(i) if parts is None else parts
        if payload_size(parts) > MAX_PAYLOAD:
            raise Rejected(f"{key}: a reference payload of more than {MAX_PAYLOAD} bytes")
        self.out.refs[key] = parts
