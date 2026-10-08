import json
import os
import sys

from vzip.virtualize import Rejected, virtualize
from vzip.virtualize.store import WORKERS


def main(argv: list[str]) -> int:
    args = list(argv)
    url = None
    if "--url" in args:
        i = args.index("--url")
        url = args[i + 1]
        del args[i : i + 2]
    workers = WORKERS
    if "--workers" in args:
        i = args.index("--workers")
        workers = int(args[i + 1])
        del args[i : i + 2]
    if len(args) != 2:
        print("usage: python -m vzip.virtualize <url or path> <out.vzip> [--url <source url>] [--workers n]\n"
              "(a URL ending in / or a directory is an N5 or Zarr v2 store; a directory needs --url;\n"
              f"a store's documents are read by up to n requests at a time, default {WORKERS})",
              file=sys.stderr)
        return 2
    if url is None and os.path.isdir(args[0]):
        print("a local directory needs --url, the store URL (ending in /) it is served from", file=sys.stderr)
        return 2
    try:
        fmt, out = virtualize(args[0], url, workers=workers)
    except Rejected as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 3
    out.write(args[1])
    print(json.dumps({"format": fmt, **out.summary}))
    return 0


sys.exit(main(sys.argv[1:]))
