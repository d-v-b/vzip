"""Conformance-harness CLI (HARNESS.md): `read` and `write`."""

import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "vzip_impl"  # noqa: A001

from vzip_impl.errors import ArchiveError, VzipError, WriteError  # noqa: E402
from vzip_impl.reader import Archive  # noqa: E402
from vzip_impl.writer import Entry, Source, write_archive  # noqa: E402


class Invalid(Exception):
    pass


def _reject_constant(c):
    raise Invalid(f"non-standard JSON constant {c}")


def _reject_float(s):
    raise Invalid(f"non-integer number {s}")


def load_json(path):
    with open(path, "rb") as f:
        text = f.read().decode("utf-8")
    return json.loads(text, parse_constant=_reject_constant, parse_float=_reject_float)


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


# ------------------------------------------------------------------ read

def parse_query(q):
    if not isinstance(q, dict):
        raise Invalid("query is not an object")
    op = q.get("op")
    if op in ("classify", "get", "get_raw"):
        if not isinstance(q.get("key"), str):
            raise Invalid("query needs a string key")
        req = ("whole",)
        if op == "get" and "range" in q:
            r = q["range"]
            if not isinstance(r, dict):
                raise Invalid("range must be an object")
            forms = [f for f in (("start", "end"), ("offset",), ("suffix",))
                     if any(k in r for k in f)]
            if len(forms) != 1 or not all(k in r for k in forms[0]):
                raise Invalid("range must have exactly one form")
            vals = [r[k] for k in forms[0]]
            if not all(is_int(v) for v in vals):
                raise Invalid("range values must be integers")
            req = ("range", *vals) if forms[0][0] == "start" else (forms[0][0], vals[0])
        elif op != "get" and "range" in q:
            raise Invalid(f"{op} does not take a range")
        return (op, q["key"], req)
    if op == "list":
        p = q.get("prefix")
        if not isinstance(p, str):
            raise Invalid("list needs a string prefix")
        return (op, p, None)
    raise Invalid(f"unknown op {op!r}")


def run_query(ar, q):
    op, key, req = q
    try:
        if op == "classify":
            return {"ok": True, "kind": ar.classify(key)}
        if op == "get":
            v = ar.get(key, req)
            return {"ok": True, "value": None if v is None else v.hex()}
        if op == "get_raw":
            v = ar.raw(key)
            return {"ok": True, "value": None if v is None else v.hex()}
        keys = ar.list(key)
        return {"ok": True, "keys": [k.decode("utf-8") for k in keys]}
    except VzipError as e:
        return {"ok": False, "class": e.cls, "error": e.message}


def cmd_read(archive_path, queries_path):
    try:
        qs = load_json(queries_path)
        if not isinstance(qs, list):
            raise Invalid("queries file is not a JSON array")
        queries = [parse_query(q) for q in qs]
    except (OSError, ValueError, Invalid) as e:
        print(f"invalid queries file: {e}", file=sys.stderr)
        return 2
    try:
        ar = Archive(archive_path)
    except ArchiveError as e:
        out = {"open": {"ok": False, "class": "archive", "error": e.message}, "results": []}
        print(json.dumps(out))
        return 0
    results = [run_query(ar, q) for q in queries]
    print(json.dumps({"open": {"ok": True}, "results": results}))
    return 0


# ------------------------------------------------------------------ write

def _hex(v, what):
    if not isinstance(v, str) or len(v) % 2 or any(c not in "0123456789abcdef" for c in v):
        raise Invalid(f"{what} must be a lowercase even-length hex string")
    return bytes.fromhex(v)


def _get(obj, name, default, check, what):
    if name not in obj:
        return default
    v = obj[name]
    if v is None:
        raise Invalid(f"{what}.{name} must not be null")
    if not check(v):
        raise Invalid(f"{what}.{name} has the wrong type")
    return v


def _nonneg(v):
    return is_int(v) and v >= 0


def parse_description(d):
    if not isinstance(d, dict):
        raise Invalid("description is not an object")
    page_size = d.get("page_size")
    if page_size is not None and (not is_int(page_size) or page_size < 1):
        raise Invalid("page_size must be null or an integer >= 1")
    mirror = _get(d, "mirror", True, lambda v: isinstance(v, bool), "description")
    srcs = _get(d, "sources", [], lambda v: isinstance(v, list), "description")
    ents = _get(d, "entries", [], lambda v: isinstance(v, list), "description")
    sources = []
    for i, s in enumerate(srcs):
        w = f"sources[{i}]"
        if not isinstance(s, dict):
            raise Invalid(f"{w} is not an object")
        kinds = [k for k in ("url", "key", "data") if k in s]
        if len(kinds) != 1:
            raise Invalid(f"{w} must have exactly one of url, key, data")
        kind = kinds[0]
        if kind == "data":
            val = _hex(s["data"], w + ".data")
        else:
            val = s[kind]
            if not isinstance(val, str):
                raise Invalid(f"{w}.{kind} must be a string")
        size = _get(s, "size", None, _nonneg, w)
        etag = _get(s, "etag", None, lambda v: isinstance(v, str), w)
        mna = _get(s, "modified_not_after", None, is_int, w)
        sources.append(Source(kind, val, size, etag, mna))
    entries = []
    for i, e in enumerate(ents):
        w = f"entries[{i}]"
        if not isinstance(e, dict):
            raise Invalid(f"{w} is not an object")
        key = e.get("key")
        if not isinstance(key, str):
            raise Invalid(f"{w}.key must be a string")
        compress = _get(e, "compress", False, lambda v: isinstance(v, bool), w)
        pinned = _get(e, "pinned", False, lambda v: isinstance(v, bool), w)
        has_b, has_r = "bytes" in e, "ranges" in e
        if has_b == has_r:
            raise Invalid(f"{w} must have exactly one of bytes and ranges")
        if has_b:
            entries.append(Entry(key, data=_hex(e["bytes"], w + ".bytes"),
                                 compress=compress, pinned=pinned))
            continue
        rs = e["ranges"]
        if not isinstance(rs, list):
            raise Invalid(f"{w}.ranges must be a list")
        ranges = []
        for j, r in enumerate(rs):
            rw = f"{w}.ranges[{j}]"
            if not isinstance(r, dict):
                raise Invalid(f"{rw} is not an object")
            if "data" in r:
                if any(k in r for k in ("source", "offset", "length")):
                    raise Invalid(f"{rw} mixes a literal and a source range")
                ranges.append({"data": _hex(r["data"], rw + ".data")})
            else:
                ranges.append({k: _get(r, k, 0, _nonneg, rw)
                               for k in ("source", "offset", "length")})
        entries.append(Entry(key, ranges=ranges, compress=compress, pinned=pinned))
    return entries, sources, page_size, mirror


def cmd_write(desc_path, out_path):
    try:
        d = load_json(desc_path)
        entries, sources, page_size, mirror = parse_description(d)
        from vzip_impl.writer import validate
        validate(entries, sources, page_size)
    except (OSError, ValueError, Invalid, WriteError) as e:
        print(f"invalid description: {e}", file=sys.stderr)
        return 1
    import tempfile
    try:
        fd, tmp = tempfile.mkstemp(prefix=".vzip-", dir=os.path.dirname(os.path.abspath(out_path)))
    except OSError as e:
        print(f"write failed: {e}", file=sys.stderr)
        return 1
    try:
        with os.fdopen(fd, "wb") as f:
            write_archive(f, entries, sources, page_size, mirror)
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, out_path)
    except BaseException as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if not isinstance(e, (OSError, WriteError)):
            raise
        print(f"write failed: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv):
    if len(argv) == 3 and argv[0] == "read":
        return cmd_read(argv[1], argv[2])
    if len(argv) == 3 and argv[0] == "write":
        return cmd_write(argv[1], argv[2])
    print("usage: vzip read <archive> <queries.json> | vzip write <desc.json> <out.vzip>",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
