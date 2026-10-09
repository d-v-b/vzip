import json
import os
import sys

from vzip.errors import ResolutionError
from vzip.policy import Policy
from vzip.virtualize import Rejected, virtualize
from vzip.virtualize.store import WORKERS


def main(argv: list[str]) -> int:
    args = list(argv)
    url = None
    checksums = "--checksums" in args  # SPEC.md §5.2: reads every referenced byte
    if checksums:
        args.remove("--checksums")
    # SPEC.md §8.7 rule 3: an input on a loopback or private host needs this (UNSAFE)
    private = "--allow-private-hosts" in args
    if private:
        args.remove("--allow-private-hosts")
    connections = byte_cost = None
    if "--connections" in args:  # requests at a time to a remote TIFF, ND2 or CZI (default 8)
        i = args.index("--connections")
        connections = int(args[i + 1])
        del args[i:i + 2]
    if "--byte-cost" in args:  # seconds per MB the read planner charges for bytes fetched (default 0.1)
        i = args.index("--byte-cost")
        byte_cost = float(args[i + 1]) * 1e-6
        del args[i:i + 2]
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
        print("usage: python -m vzip.virtualize <url or path> <out.vzip> [--url <source url>] [--checksums]"
              " [--allow-private-hosts] [--connections N] [--byte-cost SECONDS_PER_MB] [--workers N]\n"
              "(a URL ending in / or a directory is an N5 or Zarr v2 store; a directory needs --url;\n"
              f"a store's documents are read by up to N requests at a time, default {WORKERS})",
              file=sys.stderr)
        return 2
    if url is None and os.path.isdir(args[0]):
        print("a local directory needs --url, the store URL (ending in /) it is served from", file=sys.stderr)
        return 2
    try:
        fmt, out = virtualize(args[0], url, checksums=checksums, policy=Policy(allow_private_hosts=private),
                             connections=connections, byte_cost=byte_cost, workers=workers)
    except Rejected as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 3
    except ResolutionError as e:  # the reader policy refused the source
        print(f"error: {e}", file=sys.stderr)
        return 1
    out.write(args[1])
    print(json.dumps({"format": fmt, **out.summary}))
    return 0


sys.exit(main(sys.argv[1:]))
