"""python -m vzip.ir <url or path> <out.vzip> [--url <source url>] [--no-mirror]
[--allow-private-hosts]: as python -m vzip.virtualize, through the IR (exit status 3
when rejected). Every format reads through the read planner, under the reader
policy (spec/archive.md §8.7): a source on a loopback or private host needs
--allow-private-hosts."""

import json
import sys

from vzip.ir import virtualize
from vzip.policy import Policy
from vzip.virtualize.common import Rejected


def main(argv: list[str]) -> int:
    args = list(argv)
    url, mirror = None, "--no-mirror" not in args
    policy = Policy(allow_private_hosts="--allow-private-hosts" in args)
    args = [a for a in args if a not in ("--no-mirror", "--planner", "--allow-private-hosts")]
    if "--url" in args:
        i = args.index("--url")
        url = args[i + 1]
        del args[i:i + 2]
    try:
        fmt, out, ir = virtualize(args[0], url, mirror, policy)
    except Rejected as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 3
    out.write(args[1])
    print(json.dumps({"format": fmt, "elements": len(ir), **out.summary}))
    return 0


sys.exit(main(sys.argv[1:]))
