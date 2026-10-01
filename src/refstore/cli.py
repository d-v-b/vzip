"""Conformance-harness CLI (conformance/HARNESS.md) for the reference implementation.

    python -m refstore.cli read <archive> <queries.json>
    python -m refstore.cli write <description.json> <out>
"""

from __future__ import annotations

import json
import os
import sys

from zarr.abc.store import OffsetByteRequest, RangeByteRequest, SuffixByteRequest
from zarr.core.buffer import default_buffer_prototype
from zarr.core.sync import sync

from refstore.archive import RESERVED_PREFIX, VZipWriter
from refstore.pb import Range, Source
from refstore.store import VZipStore


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
            sync(store._lookup(q["key"]))
            kind = store.classify(q["key"])
            return {"ok": True, "kind": {"ref": "reference", None: "missing"}.get(kind, kind)}
        if q["op"] in ("get", "get_raw"):
            s = store if q["op"] == "get" else raw
            buf = sync(s.get(q["key"], proto, _byte_request(q.get("range"))))
            return {"ok": True, "value": None if buf is None else buf.to_bytes().hex()}
        if q["op"] == "list":
            return {"ok": True, "keys": sync(_list(store, q["prefix"]))}
        raise ValueError(f"unknown op {q['op']!r}")
    except Exception as e:  # noqa: BLE001 - every failure is reported per query
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def read(archive: str, queries_path: str) -> dict:
    with open(queries_path) as f:
        queries = json.load(f)
    store, raw = VZipStore(archive), VZipStore(archive, resolve=False)
    try:
        sync(store._open())
        sync(raw._open())
    except Exception as e:  # noqa: BLE001
        return {"open": {"ok": False, "error": f"{type(e).__name__}: {e}"}, "results": []}
    return {"open": {"ok": True}, "results": [_run_query(store, raw, q) for q in queries]}


def _source(d: dict) -> Source:
    if len(d) != 1:
        raise ValueError(f"a source has exactly one of url, key, data: {d}")
    (k, v), = d.items()
    if k == "url":
        return Source(url=v)
    if k == "key":
        return Source(key=v)
    if k == "data":
        return Source(data=bytes.fromhex(v))
    raise ValueError(f"unknown source kind {k!r}")


def _range(r: dict) -> Range:
    if "data" in r:
        if set(r) & {"source", "offset", "length"}:
            raise ValueError(f"range mixes data with source fields: {r}")
        return Range(data=bytes.fromhex(r["data"]))
    return Range(source=r.get("source", 0), offset=r.get("offset", 0), length=r.get("length", 0))


def write(desc_path: str, out: str) -> None:
    with open(desc_path) as f:
        desc = json.load(f)
    page_size = desc.get("page_size")
    if page_size is not None and (not isinstance(page_size, int) or page_size < 1):
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
                data = bytes.fromhex(e["bytes"])
                compress = e.get("compress", False)
                if e.get("pinned") and page_size is None:
                    raise ValueError(f"{key!r}: pinned requires page_size")
                if key.startswith(RESERVED_PREFIX):
                    if e.get("pinned"):
                        raise ValueError(f"{key!r}: hidden entries are not pinned")
                    w.add_hidden(key[len(RESERVED_PREFIX):], data, compress=compress)
                else:
                    w.add_bytes(key, data, compress=compress, late=e.get("pinned", False))
            w.close()
    except Exception:
        if os.path.exists(out):
            os.remove(out)
        raise


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "read":
        print(json.dumps(read(argv[1], argv[2])))
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
