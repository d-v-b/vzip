"""Conformance harness CLI (HARNESS.md): `read` and `write`."""

import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "vzip_impl"

from vzip_impl import pb  # noqa: E402
from vzip_impl.errors import ArchiveError, VzipError, WriteError  # noqa: E402
from vzip_impl.reader import Archive  # noqa: E402
from vzip_impl.writer import InEntry, build_archive  # noqa: E402


class InvalidInput(Exception):
    pass


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _nonneg_int(v, what):
    if not _is_int(v) or v < 0:
        raise InvalidInput(f"{what} must be a non-negative integer")
    return v


def _str(v, what):
    if not isinstance(v, str):
        raise InvalidInput(f"{what} must be a string")
    return v


def _utf8(s):
    """Encode a JSON string to UTF-8; None if it holds lone surrogates."""
    try:
        return s.encode("utf-8")
    except UnicodeEncodeError:
        return None


# ------------------------------------------------------------------ read

def parse_query(q):
    if not isinstance(q, dict):
        raise InvalidInput("query is not an object")
    op = q.get("op")
    if op in ("classify", "get", "get_raw"):
        if "key" not in q:
            raise InvalidInput("query is missing key")
        key = _str(q["key"], "key")
        req = ("whole",)
        if op == "get" and "range" in q:
            r = q["range"]
            if not isinstance(r, dict):
                raise InvalidInput("range must be an object")
            ks = set(r)
            if ks == {"start", "end"}:
                req = ("range", _nonneg_int(r["start"], "start"), _nonneg_int(r["end"], "end"))
            elif ks == {"offset"}:
                req = ("offset", _nonneg_int(r["offset"], "offset"))
            elif ks == {"suffix"}:
                req = ("suffix", _nonneg_int(r["suffix"], "suffix"))
            else:
                raise InvalidInput(f"range has an invalid form: {sorted(ks)}")
        elif op != "get" and "range" in q:
            raise InvalidInput(f"{op} does not take a range")
        return op, key, req
    if op == "list":
        if "prefix" not in q:
            raise InvalidInput("list query is missing prefix")
        return op, _str(q["prefix"], "prefix"), None
    raise InvalidInput(f"unknown op {op!r}")


def run_query(ar, op, key, req):
    k = _utf8(key)
    try:
        if op == "list":
            if k is None:
                return {"ok": True, "keys": []}
            return {"ok": True, "keys": [x.decode("utf-8") for x in ar.list(k)]}
        if op == "classify":
            return {"ok": True, "kind": "missing" if k is None else ar.classify(k)}
        if op == "get":
            if k is None:
                if req[0] == "range" and req[1] > req[2]:
                    from vzip_impl.errors import RequestError
                    raise RequestError("range start is greater than end")
                return {"ok": True, "value": None}
            v = ar.get(k, req)
        else:
            v = None if k is None else ar.raw(k)
        return {"ok": True, "value": None if v is None else v.hex()}
    except VzipError as e:
        return {"ok": False, "class": e.cls, "error": str(e)}


def cmd_read(archive_path, queries_path):
    try:
        with open(queries_path, "rb") as fh:
            queries = json.loads(fh.read().decode("utf-8"))
        if not isinstance(queries, list):
            raise InvalidInput("queries file is not a JSON array")
        parsed = [parse_query(q) for q in queries]
    except (OSError, ValueError, InvalidInput) as e:
        print(f"invalid queries file: {e}", file=sys.stderr)
        return 2
    try:
        ar = Archive(archive_path)
    except ArchiveError as e:
        out = {"open": {"ok": False, "class": "archive", "error": str(e)}, "results": []}
    else:
        try:
            out = {"open": {"ok": True}, "results": [run_query(ar, *p) for p in parsed]}
        finally:
            ar.close()
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


# ------------------------------------------------------------------ write

_HEX = re.compile(r"(?:[0-9a-f]{2})*")


def _hex(v, what):
    if not isinstance(v, str) or not _HEX.fullmatch(v):
        raise InvalidInput(f"{what} must be a lowercase even-length hex string")
    return bytes.fromhex(v)


def _bool(v, what):
    if not isinstance(v, bool):
        raise InvalidInput(f"{what} must be a boolean")
    return v


def _no_null(obj, what):
    for k, v in obj.items():
        if v is None:
            raise InvalidInput(f"{what}.{k} is null")


def parse_description(d):
    if not isinstance(d, dict):
        raise InvalidInput("description is not an object")
    for k, v in d.items():
        if v is None and k != "page_size":
            raise InvalidInput(f"{k} is null")
    page_size = d.get("page_size")
    if page_size is not None:
        if not _is_int(page_size) or page_size < 1:
            raise InvalidInput("page_size must be null or an integer >= 1")
    mirror = _bool(d.get("mirror", True), "mirror")
    srcs = d.get("sources", [])
    ents = d.get("entries", [])
    if not isinstance(srcs, list) or not isinstance(ents, list):
        raise InvalidInput("sources and entries must be arrays")

    sources = []
    for i, s in enumerate(srcs):
        if not isinstance(s, dict):
            raise InvalidInput(f"source {i} is not an object")
        _no_null(s, f"sources[{i}]")
        kinds = [k for k in ("url", "key", "data") if k in s]
        if len(kinds) != 1:
            raise InvalidInput(f"source {i} must have exactly one of url, key, data")
        kind = kinds[0]
        if kind == "data":
            value = _hex(s["data"], f"sources[{i}].data")
        else:
            value = _utf8(_str(s[kind], f"sources[{i}].{kind}"))
            if value is None:
                raise InvalidInput(f"sources[{i}].{kind} is not valid Unicode")
        src = pb.Source(kind, value)
        if "size" in s:
            src.size = _nonneg_int(s["size"], "size")
        if "etag" in s:
            et = _utf8(_str(s["etag"], "etag"))
            if et is None:
                raise InvalidInput("etag is not valid Unicode")
            src.etag = et
        if "modified_not_after" in s:
            if not _is_int(s["modified_not_after"]):
                raise InvalidInput("modified_not_after must be an integer")
            src.modified_not_after = s["modified_not_after"]
        sources.append(src)

    entries = []
    for i, e in enumerate(ents):
        if not isinstance(e, dict):
            raise InvalidInput(f"entry {i} is not an object")
        _no_null(e, f"entries[{i}]")
        if "key" not in e:
            raise InvalidInput(f"entry {i} has no key")
        key = _utf8(_str(e["key"], "key"))
        if key is None:
            raise InvalidInput(f"entry {i} key is not valid UTF-8")
        has_b, has_r = "bytes" in e, "ranges" in e
        if has_b == has_r:
            raise InvalidInput(f"entry {i} must have exactly one of bytes, ranges")
        compress = _bool(e.get("compress", False), "compress")
        pinned = _bool(e.get("pinned", False), "pinned")
        if has_b:
            entries.append(InEntry(key, data=_hex(e["bytes"], "bytes"), compress=compress, pinned=pinned))
            continue
        if not isinstance(e["ranges"], list):
            raise InvalidInput("ranges must be an array")
        ranges = []
        for j, r in enumerate(e["ranges"]):
            if not isinstance(r, dict):
                raise InvalidInput("range is not an object")
            _no_null(r, f"entries[{i}].ranges[{j}]")
            if "data" in r:
                if any(k in r for k in ("source", "offset", "length")):
                    raise InvalidInput("range mixes data with source/offset/length")
                ranges.append(pb.Range(data=_hex(r["data"], "range data")))
            else:
                ranges.append(pb.Range(source=_nonneg_int(r.get("source", 0), "source"),
                                       offset=_nonneg_int(r.get("offset", 0), "offset"),
                                       length=_nonneg_int(r.get("length", 0), "length")))
        entries.append(InEntry(key, ranges=ranges, compress=compress, pinned=pinned))
    return entries, sources, page_size, mirror


def cmd_write(desc_path, out_path):
    try:
        with open(desc_path, "rb") as fh:
            desc = json.loads(fh.read().decode("utf-8"))
        entries, sources, page_size, mirror = parse_description(desc)
        data = build_archive(entries, sources, page_size, mirror)
    except (OSError, ValueError, InvalidInput, WriteError) as e:
        print(f"invalid description: {e}", file=sys.stderr)
        return 1
    tmp = out_path + ".tmp-vzip"
    try:
        with open(tmp, "xb") as fh:
            fh.write(data)
        os.replace(tmp, out_path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        print(f"cannot write {out_path}: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv):
    if len(argv) != 3 or argv[0] not in ("read", "write"):
        print("usage: vzip read <archive> <queries> | vzip write <description> <out>", file=sys.stderr)
        return 2
    if argv[0] == "read":
        return cmd_read(argv[1], argv[2])
    return cmd_write(argv[1], argv[2])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
