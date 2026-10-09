"""The frozen reference's command line (`python -m vzip.virtualize` as it was before
the IR switch-over): python conformance/virtualize/reference/cli.py <url or path>
<out.vzip> [--url <source url>] [--checksums] [--allow-private-hosts]. Exit status 3
when rejected. A store is read under the reader policy (SPEC.md §8.7), which refuses
private and special hosts unless --allow-private-hosts (UNSAFE)."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from vzip.errors import ResolutionError  # noqa: E402
from vzip.policy import Policy  # noqa: E402
from vzip.virtualize.common import Rejected  # noqa: E402
from vzip_reference import virtualize  # noqa: E402


def main(argv: list[str]) -> int:
    args = list(argv)
    url = None
    checksums = "--checksums" in args
    if checksums:
        args.remove("--checksums")
    private = "--allow-private-hosts" in args
    if private:
        args.remove("--allow-private-hosts")
    if "--url" in args:
        i = args.index("--url")
        url = args[i + 1]
        del args[i:i + 2]
    if len(args) != 2:
        print("usage: cli.py <url or path> <out.vzip> [--url <source url>] [--checksums] [--allow-private-hosts]",
              file=sys.stderr)
        return 2
    if url is None and os.path.isdir(args[0]):
        print("a local directory needs --url", file=sys.stderr)
        return 2
    try:
        fmt, out = virtualize(args[0], url, checksums=checksums, policy=Policy(allow_private_hosts=private))
    except Rejected as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 3
    except ResolutionError as e:  # the reader policy refused the store
        print(f"error: {e}", file=sys.stderr)
        return 1
    out.write(args[1])
    print(json.dumps({"format": fmt, **out.summary}))
    return 0


sys.exit(main(sys.argv[1:]))
