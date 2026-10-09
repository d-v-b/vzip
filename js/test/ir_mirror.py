"""For the verify scripts: TIFF, ND2 and CZI archives the IR wrote hold its mirror
under `vzip_source` (rust/vzip-ir/src/mirror.rs), not the source metadata of
conventions §5. Their source checks are then the mirror's: the source rebuilt from
the archive alone, byte for byte (`vzip.ir.cmirror.rebuild_from_archive`)."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path


def is_mirror(archive: Path) -> bool:
    """Whether the archive's vzip_source is the IR's mirror."""
    with zipfile.ZipFile(archive) as z:
        try:
            doc = json.loads(z.read("vzip_source/zarr.json"))
        except KeyError:
            return False
    own = doc.get("attributes", {}).get("vzip_virtualized", {})
    return any(isinstance(v, dict) and "ir" in v for v in own.values())


def mirror_problems(archive: Path, source: Path) -> list[str]:
    """[] when the mirror rebuilds `source` byte for byte, else what went wrong."""
    from vzip.ir.cmirror import rebuild_from_archive

    h = hashlib.sha256()
    with open(source, "rb") as f:
        def read(o: int, n: int) -> bytes:
            f.seek(o)
            return f.read(n)
        try:
            n = rebuild_from_archive(str(archive), read, h.update)
        except Exception as e:  # noqa: BLE001
            return [f"the mirror does not rebuild the source: {type(e).__name__}: {e}"]
    want = hashlib.sha256(source.read_bytes()).hexdigest()
    return [] if h.hexdigest() == want else [f"the mirror rebuilds {n} bytes that differ from the source"]
