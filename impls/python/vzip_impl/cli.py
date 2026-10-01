"""Command-line interface implementing HARNESS.md (`read` and `write`)."""

from __future__ import annotations

import json
import os
import sys

if __package__ in (None, ""):
    sys.path[0] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    __package__ = "vzip_impl"

from vzip_impl.errors import VzipError  # noqa: E402
from vzip_impl.reader import Archive, Request  # noqa: E402
from vzip_impl.writer import WriteInputError, parse_description, write_archive  # noqa: E402


class InvalidQueries(Exception):
    pass


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def parse_query(q) -> tuple[str, object, Request | None]:
    if not isinstance(q, dict):
        raise InvalidQueries(f"query {q!r} is not an object")
    op = q.get("op")
    if op not in ("classify", "get", "get_raw", "list"):
        raise InvalidQueries(f"unknown op {op!r}")
    if op == "list":
        p = q.get("prefix")
        if not isinstance(p, str):
            raise InvalidQueries("list query needs a string prefix")
        return op, p, None
    k = q.get("key")
    if not isinstance(k, str):
        raise InvalidQueries(f"{op} query needs a string key")
    req = None
    if op == "get":
        req = Request()
        if "range" in q:
            r = q["range"]
            if not isinstance(r, dict):
                raise InvalidQueries("range is not an object")
            forms = [f for f in (("start", "end"), ("offset",), ("suffix",)) if any(x in r for x in f)]
            if len(forms) != 1 or not all(x in r for x in forms[0]):
                raise InvalidQueries(f"range {r!r} must have exactly one form")
            vals = [r[x] for x in forms[0]]
            if not all(_int(v) for v in vals):
                raise InvalidQueries(f"range {r!r} has non-integer bounds")
            if forms[0] == ("start", "end"):
                req = Request("range", vals[0], vals[1])
            elif forms[0] == ("offset",):
                req = Request("offset", vals[0])
            else:
                req = Request("suffix", vals[0])
    elif "range" in q:
        raise InvalidQueries(f"{op} query does not take a range")
    return op, k, req


def run_query(ar: Archive, op: str, arg, req) -> dict:
    try:
        if op == "classify":
            return {"ok": True, "kind": ar.classify(arg)}
        if op == "get":
            v = ar.get(arg, req)
            return {"ok": True, "value": None if v is None else v.hex()}
        if op == "get_raw":
            v = ar.raw(arg)
            return {"ok": True, "value": None if v is None else v.hex()}
        if op == "list":
            return {"ok": True, "keys": ar.list(arg)}
    except VzipError as e:
        return {"ok": False, "class": e.cls, "error": str(e)}
    raise AssertionError(op)


def cmd_read(archive_path: str, queries_path: str) -> int:
    try:
        with open(queries_path, "rb") as f:
            queries = json.loads(f.read().decode("utf-8"))
        if not isinstance(queries, list):
            raise InvalidQueries("queries file is not a JSON array")
        parsed = [parse_query(q) for q in queries]
    except (OSError, ValueError, InvalidQueries) as e:
        print(f"vzip: invalid queries file: {e}", file=sys.stderr)
        return 2
    try:
        ar = Archive(archive_path)
    except VzipError as e:
        out = {"open": {"ok": False, "class": e.cls, "error": str(e)}, "results": []}
        sys.stdout.write(json.dumps(out) + "\n")
        return 0
    with ar:
        results = [run_query(ar, *p) for p in parsed]
    sys.stdout.write(json.dumps({"open": {"ok": True}, "results": results}) + "\n")
    return 0


def cmd_write(desc_path: str, out_path: str) -> int:
    try:
        with open(desc_path, "rb") as f:
            desc = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        print(f"vzip: cannot read description: {e}", file=sys.stderr)
        return 2
    try:
        entries, sources, page_size, mirror = parse_description(desc)
        write_archive(out_path, entries, sources, page_size, mirror)
    except WriteInputError as e:
        print(f"vzip: invalid description: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"vzip: cannot write {out_path!r}: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "read":
        return cmd_read(argv[1], argv[2])
    if len(argv) == 3 and argv[0] == "write":
        return cmd_write(argv[1], argv[2])
    print("usage: vzip read <archive> <queries.json> | vzip write <description.json> <out.vzip>",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
