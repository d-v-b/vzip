"""The frozen reference's command line (`python -m vzip.virtualize` as it was before
the IR switch-over): python conformance/virtualize/reference/cli.py <url or path>
<out.vzip> [--url <source url>] [--checksums]. Exit status 3 when rejected."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from vzip.virtualize.common import Rejected  # noqa: E402
from vzip_reference import virtualize  # noqa: E402


def main(argv: list[str]) -> int:
    args = list(argv)
    url = None
    checksums = "--checksums" in args
    if checksums:
        args.remove("--checksums")
    if "--url" in args:
        i = args.index("--url")
        url = args[i + 1]
        del args[i:i + 2]
    if len(args) != 2:
        print("usage: cli.py <url or path> <out.vzip> [--url <source url>] [--checksums]", file=sys.stderr)
        return 2
    if url is None and os.path.isdir(args[0]):
        print("a local directory needs --url", file=sys.stderr)
        return 2
    try:
        fmt, out = virtualize(args[0], url, checksums=checksums)
    except Rejected as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 3
    out.write(args[1])
    print(json.dumps({"format": fmt, **out.summary}))
    return 0


sys.exit(main(sys.argv[1:]))
