"""Conformance harness CLI (HARNESS.md): `read` and `write` commands."""

import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "vzip_impl"

from vzip_impl.errors import InvalidInput, VzError  # noqa: E402
from vzip_impl import reader, writer  # noqa: E402


class BadJSON(Exception):
    pass


def _no_dupes(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise BadJSON("duplicate member %r" % k)
        d[k] = v
    return d


def _bad_constant(name):
    raise BadJSON("invalid JSON constant %s" % name)


def load_json(path):
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BadJSON("not UTF-8")
    try:
        return json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_bad_constant)
    except json.JSONDecodeError as e:
        raise BadJSON(str(e))


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


# ------------------------------------------------------------------ read

def parse_query(q):
    if not isinstance(q, dict):
        raise BadJSON("query is not an object")
    op = q.get("op")
    if op not in ("classify", "get", "get_raw", "list"):
        raise BadJSON("unknown op %r" % (op,))
    if op == "list":
        if "prefix" not in q or not isinstance(q["prefix"], str):
            raise BadJSON("list without a string prefix")
    else:
        if "key" not in q or not isinstance(q["key"], str):
            raise BadJSON("%s without a string key" % op)
    if "range" in q and op != "get":
        raise BadJSON("range on %s" % op)
    req = ("whole",)
    if op == "get" and "range" in q:
        r = q["range"]
        if not isinstance(r, dict):
            raise BadJSON("range is not an object")
        forms = [f for f in (("start", "end"), ("offset",), ("suffix",)) if any(m in r for m in f)]
        if len(forms) != 1 or not all(m in r for m in forms[0]):
            raise BadJSON("range must have exactly one complete form")
        vals = [r[m] for m in forms[0]]
        for v in vals:
            if not _is_int(v) or v < 0:
                raise BadJSON("range numbers must be non-negative integers")
        name = {"start": "range", "offset": "offset", "suffix": "suffix"}[forms[0][0]]
        req = (name, *vals)
    return op, q.get("key", q.get("prefix")), req


def run_query(ar, op, key, req):
    try:
        if op == "classify":
            return {"ok": True, "kind": ar.classify(key)}
        if op == "get":
            v = ar.get(key, req)
            return {"ok": True, "value": None if v is None else v.hex()}
        if op == "get_raw":
            v = ar.raw(key)
            return {"ok": True, "value": None if v is None else bytes(v).hex()}
        return {"ok": True, "keys": ar.list(key)}
    except VzError as e:
        return {"ok": False, "class": e.cls, "error": str(e)}
    except MemoryError:
        return {"ok": False, "class": "request", "error": "out of memory"}
    except Exception as e:  # bug: report rather than lose the other results
        return {"ok": False, "class": "internal", "error": "%s: %s" % (type(e).__name__, e)}


def cmd_read(archive_path, queries_path):
    try:
        qs = load_json(queries_path)
        if not isinstance(qs, list):
            raise BadJSON("queries file is not an array")
        parsed = [parse_query(q) for q in qs]
    except (BadJSON, OSError) as e:
        print("invalid queries file: %s" % e, file=sys.stderr)
        return 2
    try:
        ar = reader.Archive(archive_path)
    except VzError as e:
        out = {"open": {"ok": False, "class": "archive", "error": str(e)}, "results": []}
        sys.stdout.write(json.dumps(out) + "\n")
        return 0
    with ar:
        results = [run_query(ar, *p) for p in parsed]
    sys.stdout.write(json.dumps({"open": {"ok": True}, "results": results}) + "\n")
    return 0


# ------------------------------------------------------------------ write

_HEX_RE = re.compile(r"(?:[0-9a-f]{2})*")


def _hex(v, what):
    if not isinstance(v, str) or not _HEX_RE.fullmatch(v):
        raise InvalidInput("%s must be a lowercase even-length hex string" % what)
    return bytes.fromhex(v)


def _get(obj, name, default, check, what):
    if name not in obj:
        return default
    v = obj[name]
    if v is None:
        raise InvalidInput("%s must not be null" % what)
    return check(v, what)


def _bool(v, what):
    if not isinstance(v, bool):
        raise InvalidInput("%s must be a boolean" % what)
    return v


def _uint(v, what):
    if not _is_int(v) or v < 0:
        raise InvalidInput("%s must be a non-negative integer" % what)
    return v


def _int(v, what):
    if not _is_int(v):
        raise InvalidInput("%s must be an integer" % what)
    return v


def _str(v, what):
    if not isinstance(v, str):
        raise InvalidInput("%s must be a string" % what)
    return v


def _list(v, what):
    if not isinstance(v, list):
        raise InvalidInput("%s must be an array" % what)
    return v


def parse_description(d):
    if not isinstance(d, dict):
        raise InvalidInput("description must be an object")
    page_size = d.get("page_size")
    if page_size is not None and (not _is_int(page_size) or page_size < 1):
        raise InvalidInput("page_size must be null or an integer >= 1")
    mirror = _get(d, "mirror", True, _bool, "mirror")
    srcs = _get(d, "sources", [], _list, "sources")
    ents = _get(d, "entries", [], _list, "entries")

    sources = []
    for i, s in enumerate(srcs):
        w = "sources[%d]" % i
        if not isinstance(s, dict):
            raise InvalidInput("%s must be an object" % w)
        kinds = [k for k in ("url", "key", "data") if k in s]
        if len(kinds) != 1:
            raise InvalidInput("%s must have exactly one of url, key, data" % w)
        kind = kinds[0]
        if s[kind] is None:
            raise InvalidInput("%s.%s must not be null" % (w, kind))
        value = _hex(s[kind], w + ".data") if kind == "data" else _str(s[kind], "%s.%s" % (w, kind))
        size = _get(s, "size", None, _uint, w + ".size")
        etag = _get(s, "etag", None, _str, w + ".etag")
        mnf = _get(s, "modified_not_after", None, _int, w + ".modified_not_after")
        if kind != "url" and (size is not None or etag is not None or mnf is not None):
            raise InvalidInput("%s: pins are only allowed on url sources" % w)
        sources.append(writer.WSource(kind, value, size, etag, mnf))

    entries = []
    for i, e in enumerate(ents):
        w = "entries[%d]" % i
        if not isinstance(e, dict):
            raise InvalidInput("%s must be an object" % w)
        if "key" not in e:
            raise InvalidInput("%s has no key" % w)
        key = _get(e, "key", None, _str, w + ".key")
        has_b, has_r = "bytes" in e, "ranges" in e
        if has_b == has_r:
            raise InvalidInput("%s must have exactly one of bytes and ranges" % w)
        compress = _get(e, "compress", False, _bool, w + ".compress")
        pinned = _get(e, "pinned", False, _bool, w + ".pinned")
        if has_b:
            data = _get(e, "bytes", None, _hex, w + ".bytes")
            entries.append(writer.WEntry(key, data=data, compress=compress, pinned=pinned))
        else:
            rs = _get(e, "ranges", None, _list, w + ".ranges")
            ranges = []
            for j, r in enumerate(rs):
                wr = "%s.ranges[%d]" % (w, j)
                if not isinstance(r, dict):
                    raise InvalidInput("%s must be an object" % wr)
                if "data" in r:
                    if any(m in r for m in ("source", "offset", "length")):
                        raise InvalidInput("%s mixes a literal and a source range" % wr)
                    ranges.append(writer.WRange(data=_get(r, "data", None, _hex, wr + ".data")))
                else:
                    ranges.append(writer.WRange(
                        source=_get(r, "source", 0, _uint, wr + ".source"),
                        offset=_get(r, "offset", 0, _uint, wr + ".offset"),
                        length=_get(r, "length", 0, _uint, wr + ".length")))
            entries.append(writer.WEntry(key, ranges=ranges, compress=compress, pinned=pinned))
    return entries, sources, page_size, mirror


def cmd_write(desc_path, out_path):
    try:
        d = load_json(desc_path)
        entries, sources, page_size, mirror = parse_description(d)
        data = writer.build(entries, sources, page_size, mirror)
    except (BadJSON, InvalidInput, OSError) as e:
        print("invalid description: %s" % e, file=sys.stderr)
        return 1
    tmp = out_path + ".tmp-vzip-%d" % os.getpid()
    try:
        with open(tmp, "xb") as f:
            f.write(data)
        os.replace(tmp, out_path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        print("cannot write %s: %s" % (out_path, e), file=sys.stderr)
        return 1
    return 0


def main(argv):
    if len(argv) == 3 and argv[0] == "read":
        return cmd_read(argv[1], argv[2])
    if len(argv) == 3 and argv[0] == "write":
        return cmd_write(argv[1], argv[2])
    print("usage: vzip read <archive> <queries.json> | vzip write <description.json> <out.vzip>",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
