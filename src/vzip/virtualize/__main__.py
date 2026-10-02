import json
import sys

from vzip.virtualize import Rejected, virtualize


def main(argv: list[str]) -> int:
    args = list(argv)
    url = None
    if "--url" in args:
        i = args.index("--url")
        url = args[i + 1]
        del args[i : i + 2]
    if len(args) != 2:
        print("usage: python -m vzip.virtualize <url or path> <out.vzip> [--url <source url>]", file=sys.stderr)
        return 2
    try:
        fmt, out = virtualize(args[0], url)
    except Rejected as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 3
    out.write(args[1])
    print(json.dumps({"format": fmt, **out.summary}))
    return 0


sys.exit(main(sys.argv[1:]))
