"""Conformance-harness CLI (conformance/HARNESS.md) for the reference implementation.

    python -m vzip.cli [--allow-private-hosts] read <archive> <queries.json>
    python -m vzip.cli write <description.json> <out>

`read` uses the reader's default policy (spec §8.7). --allow-private-hosts is
UNSAFE: it lets the archive's sources reach loopback, private, link-local and
other special addresses. The harness needs it, since the runner serves its
HTTP sources on 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import sys

from zarr.abc.store import OffsetByteRequest, RangeByteRequest, SuffixByteRequest
from zarr.core.buffer import default_buffer_prototype
from zarr.core.sync import sync

from vzip.archive import RESERVED_PREFIX, VZipWriter
from vzip.pb import Range, Source
from vzip.policy import Policy
from vzip.store import VZipStore


def _byte_request(r: dict | None):
    if r is None:
        return None
    if "suffix" in r:
        return SuffixByteRequest(r["suffix"])
    if "offset" in r:
        return OffsetByteRequest(r["offset"])
    return RangeByteRequest(r["start"], r["end"])


async def _list(store: VZipStore, prefix: str) -> list[str]:
    return [k async for k in store.list_prefix(prefix)]


def _run_query(store: VZipStore, raw: VZipStore, q: dict) -> dict:
    proto = default_buffer_prototype()
    try:
        if q["op"] == "classify":
            kind = sync(store.kind(q["key"]))
            return {"ok": True, "kind": {"ref": "reference", None: "missing"}.get(kind, kind)}
        if q["op"] in ("get", "get_raw"):
            s = store if q["op"] == "get" else raw
            buf = sync(s.get(q["key"], proto, _byte_request(q.get("range"))))
            return {"ok": True, "value": None if buf is None else buf.to_bytes().hex()}
        if q["op"] == "list":
            return {"ok": True, "keys": sync(_list(store, q["prefix"]))}
        raise ValueError(f"unknown op {q['op']!r}")
    except Exception as e:  # noqa: BLE001 - every failure is reported per query
        return {"ok": False, "class": getattr(e, "cls", "error"),
                "error": f"{type(e).__name__}: {e}"}


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate JSON member names: {keys}")
    return dict(pairs)


def _well_formed(text: str) -> bool:
    try:
        text.encode("utf-8")
        return True
    except UnicodeEncodeError:  # a lone surrogate from a JSON "\udXXX" escape
        return False


def _check_query(q) -> None:
    """HARNESS: a malformed query makes the whole queries file invalid."""
    if not isinstance(q, dict) or q.get("op") not in ("classify", "get", "get_raw", "list"):
        raise ValueError(f"malformed query {q!r}")
    if q["op"] == "list":
        if not isinstance(q.get("prefix"), str) or not _well_formed(q["prefix"]):
            raise ValueError(f"malformed query {q!r}")
        return
    if not isinstance(q.get("key"), str) or not _well_formed(q["key"]):
        raise ValueError(f"malformed query {q!r}")
    if "range" in q:
        r = q["range"]
        if q["op"] != "get":
            raise ValueError(f"range on a {q['op']} query: {q!r}")
        forms = set(r) & {"start", "end", "offset", "suffix"} if isinstance(r, dict) else None
        if forms not in ({"start", "end"}, {"offset"}, {"suffix"}):
            raise ValueError(f"malformed range in {q!r}")
        r = {k: v for k, v in r.items() if k in ("start", "end", "offset", "suffix")}
        if any(not isinstance(v, int) or isinstance(v, bool) or not 0 <= v < 2**53
               for v in r.values()):
            raise ValueError(f"range values must be non-negative integers: {q!r}")


def read(archive: str, queries_path: str, policy: Policy | None = None) -> dict:
    with open(queries_path) as f:
        queries = json.load(f, object_pairs_hook=_no_duplicates)
    if not isinstance(queries, list):
        raise ValueError("the queries file must hold a JSON array")
    for q in queries:
        _check_query(q)
    store = VZipStore(archive, policy=policy)
    raw = VZipStore(archive, resolve=False, policy=policy)
    try:
        sync(store._open())
        sync(raw._open())
    except Exception as e:  # noqa: BLE001
        return {"open": {"ok": False, "class": "archive", "error": f"{type(e).__name__}: {e}"},
                "results": []}
    return {"open": {"ok": True}, "results": [_run_query(store, raw, q) for q in queries]}


_PINS = ("size", "etag", "modified_not_after")


def _hex(v) -> bytes:
    if not isinstance(v, str) or v != v.lower() or len(v) % 2:
        raise ValueError(f"not lowercase even-length hex: {v!r}")
    return bytes.fromhex(v)


def _int(v, what: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise ValueError(f"{what} must be a non-negative JSON integer, not {v!r}")
    return v


def _source(d: dict) -> Source:
    kinds = [k for k in ("url", "key", "data") if k in d]
    if len(kinds) != 1:
        raise ValueError(f"a source has exactly one of url, key, data: {d}")
    pins = {k: d[k] for k in _PINS if k in d}
    for k in ("size", "modified_not_after"):
        if k in pins:
            _int(pins[k], k)
    k = kinds[0]
    if k == "url":
        return Source(url=d["url"], **pins)
    if pins:
        raise ValueError("pins are only allowed on url sources")
    if k == "key":
        return Source(key=d["key"])
    return Source(data=_hex(d["data"]))


def _range(r: dict) -> Range:
    if "data" in r:
        if set(r) & {"source", "offset", "length"}:
            raise ValueError(f"range mixes data with source fields: {r}")
        return Range(data=_hex(r["data"]))
    return Range(source=_int(r.get("source", 0), "source"), offset=_int(r.get("offset", 0), "offset"),
                 length=_int(r.get("length", 0), "length"))


def _reject_nulls(desc: dict) -> None:
    """HARNESS: null is allowed only for page_size."""
    def walk(v, path):
        if v is None and path != "page_size":
            raise ValueError(f"null not allowed at {path}")
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, k if path == "" else f"{path}.{k}")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]")
    walk(desc, "")


def write(desc_path: str, out: str) -> None:
    with open(desc_path) as f:
        desc = json.load(f, object_pairs_hook=_no_duplicates)
    _reject_nulls(desc)
    page_size = desc.get("page_size")
    if not isinstance(desc.get("mirror", True), bool):
        raise ValueError("mirror must be a JSON boolean")
    if page_size is not None and (not isinstance(page_size, int) or isinstance(page_size, bool)
                                  or page_size < 1):
        raise ValueError(f"page_size must be null or an integer >= 1, not {page_size!r}")
    try:
        with open(out, "wb") as f:
            w = VZipWriter(f, mirror_refs=desc.get("mirror", True), page_size=page_size)
            for src in desc.get("sources", []):
                w.source(_source(src))
            for e in desc["entries"]:
                key = e["key"]
                if ("bytes" in e) == ("ranges" in e):
                    raise ValueError(f"{key!r}: an entry has exactly one of bytes, ranges")
                if "ranges" in e:
                    if e.get("pinned"):
                        raise ValueError(f"{key!r}: only bytes entries can be pinned")
                    if e.get("compress"):
                        raise ValueError(f"{key!r}: reference entries are never compressed")
                    w.add_ranges(key, [_range(r) for r in e["ranges"]])
                    continue
                data = _hex(e["bytes"])
                compress = e.get("compress", False)
                for flag in ("compress", "pinned"):
                    if not isinstance(e.get(flag, False), bool):
                        raise ValueError(f"{flag} must be a JSON boolean")
                if e.get("pinned") and page_size is None:
                    raise ValueError(f"{key!r}: pinned requires page_size")
                if key.startswith(RESERVED_PREFIX):
                    w.add_hidden(key[len(RESERVED_PREFIX):], data, compress=compress,
                                 late=e.get("pinned", False))
                else:
                    w.add_bytes(key, data, compress=compress, late=e.get("pinned", False))
            w.close()
    except Exception:
        if os.path.exists(out):
            os.remove(out)
        raise


def main(argv: list[str]) -> int:
    policy = None
    if argv[:1] == ["--allow-private-hosts"]:
        policy, argv = Policy(allow_private_hosts=True), argv[1:]  # unsafe: asked for by name
    if len(argv) == 3 and argv[0] == "read":
        try:
            result = read(argv[1], argv[2], policy)
        except (OSError, ValueError) as e:
            print(f"invalid queries file: {e}", file=sys.stderr)
            return 2
        print(json.dumps(result))
        return 0
    if len(argv) == 3 and argv[0] == "write":
        try:
            write(argv[1], argv[2])
        except Exception as e:  # noqa: BLE001
            print(f"{type(e).__name__}: {e}", file=sys.stderr)
            return 1
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
