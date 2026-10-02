"""virtualize <url> <out.json>: VIRTUALIZE.md (profiles version 0, revision 1).

Exit status 0 on success, 3 when the specification rejects the input.
"""

from __future__ import annotations

import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vz_common import Reject, Source  # noqa: E402
from vz_nd2 import JP2_SIG, virtualize_nd2  # noqa: E402
from vz_tiff import virtualize_tiff  # noqa: E402


def virtualize(url: str) -> dict:
    src = Source(url)
    head = src.read(0, min(16, src.size))
    if head[:4] in (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+"):
        entries = virtualize_tiff(src, head)
    elif head[:4] == b"\xda\xce\xbe\x0a":
        entries = virtualize_nd2(src)
    elif head[:12] == JP2_SIG:
        raise Reject("legacy (JPEG 2000) ND2 file")
    else:
        raise Reject("neither a TIFF nor an ND2 file")
    out = {}
    for k in sorted(entries):
        v = entries[k]
        if "bytes" in v:
            out[k] = {"base64": base64.b64encode(v["bytes"]).decode("ascii")}
        else:
            out[k] = v
    return {"sources": [url], "entries": out}, src


def main(argv) -> int:
    if len(argv) != 3:
        print("usage: virtualize <url> <out.json>", file=sys.stderr)
        return 2
    url, path = argv[1], argv[2]
    try:
        doc, src = virtualize(url)
    except Reject as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 3
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, allow_nan=False)
    n = len(doc["entries"])
    refs = sum(1 for v in doc["entries"].values() if "ranges" in v)
    print(f"{url}: {n} entries ({refs} references), {src.requests} requests")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
