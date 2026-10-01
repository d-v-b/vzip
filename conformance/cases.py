"""Conformance cases.

* DESCRIPTIONS: valid write descriptions. Each one is (a) written by the
  reference writer and read by every implementation, and (b) written by every
  implementation and checked by the validator, the reference reader and the
  implementation itself. Expected results come from model.Model.
* INVALID_DESCRIPTIONS: descriptions a writer must reject.
* CRAFTED: hand-built archives (malformed or unusual), with hand-written
  expectations. Built with the reference writer's internals.
"""

from __future__ import annotations

import os
import struct
import zipfile
import zlib
from pathlib import Path

H = lambda b: b.hex() if isinstance(b, bytes) else b.encode().hex()  # noqa: E731
ERROR = {"ok": False}


def err(cls: str) -> dict:
    return {"ok": False, "class": cls}


def ok_value(b: bytes | None) -> dict:
    return {"ok": True, "value": None if b is None else b.hex()}


MTIME = 1_600_000_000  # every data file's modification time (2020-09-13 12:26:40 UTC)


def write_data_files(root: Path) -> None:
    (root / "data" / "sub").mkdir(parents=True, exist_ok=True)
    (root / "data" / "blob.bin").write_bytes(bytes(range(256)) * 4)
    (root / "data" / "sub" / "my file.bin").write_bytes(b"spaced-out bytes")
    (root / "data" / "short.bin").write_bytes(b"0123456789")
    (root / "data" / "é.bin").write_bytes(b"accented")
    for f in (root / "data").rglob("*"):
        if f.is_file():
            os.utime(f, (MTIME, MTIME))


BLOB = bytes(range(256)) * 4


def _basic(root: Path) -> dict:
    return {
        "page_size": None,
        "mirror": True,
        "sources": [
            {"url": "data/blob.bin"},
            {"url": "data/sub/my%20file.bin"},
            {"key": "__vz__/hdr"},
            {"data": H(b"SHARED")},
            {"key": "meta/zarr.json"},
            {"url": "data/missing.bin"},
            {"url": "data/short.bin"},
            {"url": (root / "data" / "blob.bin").resolve().as_uri()},
            {"url": "./data/../data/blob.bin"},
            {"url": "data/%C3%A9.bin"},
            {"url": "file://localhost" + (root / "data" / "blob.bin").resolve().as_uri()[7:]},
            {"url": "data/blob.bin?x=1"},
            {"url": "data/blob.bin?"},
            {"url": "file://elsewhere/data/blob.bin"},
            {"url": "file:data/blob.bin"},
            {"url": "data/sub%2Fmy%20file.bin"},
            {"url": "FILE://LocalHost" + (root / "data" / "blob.bin").resolve().as_uri()[7:]},
            # 17-23: pins (spec §6.1); every data file's mtime is MTIME
            {"url": "data/blob.bin", "size": 1024},
            {"url": "data/blob.bin", "size": 1023},
            {"url": "data/blob.bin", "modified_not_after": MTIME},
            {"url": "data/blob.bin", "modified_not_after": MTIME - 1},
            {"url": "data/blob.bin", "etag": '"abc"'},
            {"url": "data/blob.bin", "size": 1024, "modified_not_after": MTIME + 3600},
            {"url": "data/short.bin", "size": 10, "modified_not_after": MTIME},
            {"url": "file:" + (root / "data" / "blob.bin").resolve().as_uri()[7:]},  # 24: no authority
            {"url": "data/%2E%2E/data/blob.bin"},  # 25: encoded dot segment
            {"url": "data/%2e/blob.bin"},          # 26
        ],
        "entries": [
            {"key": "meta/zarr.json", "bytes": H(b'{"zarr_format":3}'), "compress": False},
            {"key": "meta/big", "bytes": H(bytes(range(200)) * 3), "compress": True},
            {"key": "empty", "bytes": ""},
            {"key": "unicode/ключ/é", "bytes": H("é")},
            {"key": "__vz__/hdr", "bytes": H(b"HDR!"), "compress": True},
            {"key": "__vz__/raw", "bytes": H(b"raw")},
            {"key": "r/single", "ranges": [{"source": 0, "offset": 10, "length": 20}]},
            {"key": "r/space", "ranges": [{"source": 1, "offset": 0, "length": 6}]},
            {"key": "r/concat", "ranges": [
                {"data": "aa"}, {"source": 0, "offset": 100, "length": 3},
                {"source": 3, "offset": 1, "length": 2}]},
            {"key": "r/hdr", "ranges": [
                {"source": 2, "offset": 0, "length": 4}, {"source": 0, "offset": 0, "length": 2}]},
            {"key": "r/meta", "ranges": [{"source": 4, "offset": 2, "length": 5}]},
            {"key": "r/empty_concat", "ranges": []},
            {"key": "r/empty_literal", "ranges": [{"data": ""}]},
            {"key": "r/zero_len", "ranges": [{"source": 0, "offset": 5, "length": 0}]},
            {"key": "r/missing_file", "ranges": [
                {"data": H(b"ok")}, {"source": 5, "offset": 0, "length": 4}]},
            {"key": "r/short", "ranges": [{"source": 6, "offset": 5, "length": 10}]},
            {"key": "r/abs", "ranges": [{"source": 7, "offset": 1000, "length": 24}]},
            {"key": "r/dotdot", "ranges": [{"source": 8, "offset": 3, "length": 3}]},
            {"key": "r/big_offset", "ranges": [{"source": 0, "offset": 2**40, "length": 1}]},
            {"key": "r/accent", "ranges": [{"source": 9, "offset": 0, "length": 8}]},
            {"key": "r/localhost", "ranges": [{"source": 10, "offset": 7, "length": 2}]},
            {"key": "r/query", "ranges": [{"source": 11, "offset": 0, "length": 2}]},
            {"key": "r/empty_query", "ranges": [{"source": 12, "offset": 0, "length": 2}]},
            {"key": "r/authority", "ranges": [{"source": 13, "offset": 0, "length": 2}]},
            {"key": "r/scheme_relative", "ranges": [{"source": 14, "offset": 0, "length": 2}]},
            {"key": "r/oob_tail", "ranges": [
                {"source": 3, "offset": 4, "length": 5}, {"data": H(b"!")}]},
            {"key": "r/zero_len_bad", "ranges": [
                {"data": H(b"z")}, {"source": 5, "offset": 0, "length": 0}]},
            {"key": "r/encoded_slash", "ranges": [{"source": 15, "offset": 0, "length": 2}]},
            {"key": "r/upper_localhost", "ranges": [{"source": 16, "offset": 5, "length": 2}]},
            {"key": "r/pin_size_ok", "ranges": [{"source": 17, "offset": 1, "length": 2}]},
            {"key": "r/pin_size_bad", "ranges": [{"source": 18, "offset": 1, "length": 2}]},
            {"key": "r/pin_mtime_ok", "ranges": [{"source": 19, "offset": 1, "length": 2}]},
            {"key": "r/pin_mtime_bad", "ranges": [{"source": 20, "offset": 1, "length": 2}]},
            {"key": "r/pin_etag_file", "ranges": [{"source": 21, "offset": 1, "length": 2}]},
            {"key": "r/pin_both_ok", "ranges": [{"source": 22, "offset": 1, "length": 2}]},
            {"key": "r/pin_untouched", "ranges": [
                {"data": H(b"y")}, {"source": 18, "offset": 1, "length": 2}]},
            {"key": "r/pin_short", "ranges": [{"source": 23, "offset": 8, "length": 4}]},
            {"key": "r/no_authority", "ranges": [{"source": 24, "offset": 9, "length": 2}]},
            {"key": "r/encoded_dotdot", "ranges": [{"source": 25, "offset": 0, "length": 2}]},
            {"key": "r/encoded_dot", "ranges": [{"source": 26, "offset": 0, "length": 2}]},
            # keys whose UTF-8 and UTF-16 orders differ, and a leading byte order mark
            {"key": "o/\ufeffbom", "bytes": H(b"bom")},
            {"key": "o/\U0001F600", "bytes": H(b"grin")},
            {"key": "o/\uff5e", "bytes": H(b"tilde")},
            {"key": "o/\uffff", "bytes": H(b"max-bmp")},
        ],
    }


def _http(server_base: str) -> dict:
    """A description whose sources are served by conformance/http_server.py (spec §6.2)."""
    from http_server import etag_for

    B = server_base + "vectors/"
    blob = B + "data/blob.bin"
    return {
        "page_size": None,
        "mirror": True,
        "sources": [
            {"url": blob},                                                       # 0
            {"url": B + "data/sub/my%20file.bin"},                               # 1
            {"url": B + "data/%C3%A9.bin"},                                      # 2
            {"url": B + "data/missing.bin"},                                     # 3
            {"url": blob, "size": 1024, "etag": etag_for(BLOB),
             "modified_not_after": MTIME},                                       # 4
            {"url": blob, "etag": '"not-the-etag"'},                             # 5
            {"url": blob, "size": 1023},                                         # 6
            {"url": blob, "modified_not_after": MTIME - 1},                      # 7
            {"url": server_base + "norange/vectors/data/blob.bin", "size": 1024},  # 8
            {"url": server_base + "nototal/vectors/data/blob.bin"},              # 9
            {"url": server_base + "nototal/vectors/data/blob.bin", "size": 1024},  # 10
            {"url": server_base + "gzip/vectors/data/blob.bin"},                 # 11
            {"url": B + "data/short.bin"},                                       # 12
            {"url": server_base + "nocond/vectors/data/blob.bin", "etag": '"not-the-etag"'},  # 13
            {"url": server_base + "nocond/vectors/data/blob.bin",
             "modified_not_after": MTIME - 1},                                   # 14
            {"url": server_base + "nocond/vectors/data/blob.bin", "etag": etag_for(BLOB),
             "modified_not_after": MTIME},                                       # 15
            {"url": server_base + "noetag/vectors/data/blob.bin", "etag": etag_for(BLOB)},  # 16
            {"url": server_base + "noetag/vectors/data/blob.bin"},               # 17
            {"url": server_base + "redirect/2/vectors/data/blob.bin", "etag": etag_for(BLOB)},  # 18
            {"url": server_base + "redirect/6/vectors/data/blob.bin"},           # 19
        ],
        "entries": [
            {"key": "meta", "bytes": H(b"{}")},
            {"key": "h/plain", "ranges": [{"source": 0, "offset": 10, "length": 20}]},
            {"key": "h/space", "ranges": [{"source": 1, "offset": 0, "length": 6}]},
            {"key": "h/accent", "ranges": [{"source": 2, "offset": 0, "length": 8}]},
            {"key": "h/missing", "ranges": [{"data": H(b"ok")}, {"source": 3, "length": 4}]},
            {"key": "h/pinned", "ranges": [{"source": 4, "offset": 100, "length": 4}]},
            {"key": "h/bad_etag", "ranges": [{"source": 5, "offset": 1, "length": 2}]},
            {"key": "h/bad_size", "ranges": [{"source": 6, "offset": 1, "length": 2}]},
            {"key": "h/bad_mtime", "ranges": [{"source": 7, "offset": 1, "length": 2}]},
            {"key": "h/norange", "ranges": [{"source": 8, "offset": 500, "length": 3}]},
            {"key": "h/nototal", "ranges": [{"source": 9, "offset": 3, "length": 3}]},
            {"key": "h/nototal_size_pin", "ranges": [{"source": 10, "offset": 3, "length": 3}]},
            {"key": "h/gzip", "ranges": [{"source": 11, "offset": 0, "length": 2}]},
            {"key": "h/clipped", "ranges": [{"source": 12, "offset": 5, "length": 10}]},
            {"key": "h/past_end", "ranges": [{"source": 12, "offset": 20, "length": 2}]},
            {"key": "h/two_ranges", "ranges": [{"source": 0, "offset": 0, "length": 2},
                                               {"source": 0, "offset": 700, "length": 2}]},
            {"key": "h/nocond_bad_etag", "ranges": [{"source": 13, "offset": 1, "length": 2}]},
            {"key": "h/nocond_bad_mtime", "ranges": [{"source": 14, "offset": 1, "length": 2}]},
            {"key": "h/nocond_good", "ranges": [{"source": 15, "offset": 1, "length": 2}]},
            {"key": "h/noetag_pinned", "ranges": [{"source": 16, "offset": 1, "length": 2}]},
            {"key": "h/noetag_unpinned", "ranges": [{"source": 17, "offset": 1, "length": 2}]},
            {"key": "h/redirect_2", "ranges": [{"source": 18, "offset": 40, "length": 3}]},
            {"key": "h/redirect_6", "ranges": [{"source": 19, "offset": 40, "length": 3}]},
        ],
    }


def descriptions(root: Path, http_base: str | None = None) -> dict[str, dict]:
    basic = _basic(root)
    nomirror = {**basic, "mirror": False}
    paged = {
        **basic,
        "page_size": 256,
        "entries": [
            {**e, "pinned": e["key"] == "meta/zarr.json"} for e in basic["entries"]
        ] + [
            {"key": f"p/c/{i}", "ranges": [{"source": 0, "offset": i, "length": 3}]}
            for i in range(300)
        ] + [{"key": "p/zarr.json", "bytes": H(b"{}"), "pinned": True}],
    }
    many = {
        "page_size": 65536,
        "mirror": True,
        "sources": [{"url": "data/blob.bin"}],
        "entries": [{"key": "m/zarr.json", "bytes": H(b"{}"), "pinned": True}] + [
            {"key": f"m/c/{i}", "ranges": [{"source": 0, "offset": i % 1000, "length": 7}]}
            for i in range(70_000)
        ],
    }
    # pages small enough that the o/ keys (UTF-8 vs UTF-16 order) straddle pages
    paged_tiny = {**paged, "page_size": 1,
                  "entries": paged["entries"][:60] + [
                      {"key": "__vz__/pinned_hidden", "bytes": H(b"ph"), "pinned": True}]}
    empty = {"page_size": None, "mirror": True, "sources": [], "entries": []}
    empty_paged = {**empty, "page_size": 100}
    # a single literal range of 65515 bytes encodes to exactly 65519 bytes (spec §4.3)
    max_payload = {"page_size": None, "mirror": True, "sources": [], "entries": [
        {"key": "big", "ranges": [{"data": "ab" * 65515}]}]}
    return {
        "basic": basic,
        "nomirror": nomirror,
        "paged": paged,
        "paged_tiny_pages": paged_tiny,
        "zip64_paged": many,
        "empty": empty,
        "empty_paged": empty_paged,
        "max_payload": max_payload,
        "path needs encoding é": {**basic, "entries": basic["entries"][:12]},
        **({"http_basic": _http(http_base),
            "http_paged": {**_http(http_base), "page_size": 128}} if http_base else {}),
    }


def queries_for(name: str, desc: dict) -> list[dict]:
    from model import standard_queries

    if name == "zip64_paged":
        sub = {**desc, "entries": [desc["entries"][i] for i in (0, 1, 2, 35_000, 69_999, 70_000)]}
        qs = standard_queries(sub)
        return [q for q in qs if q["op"] != "list"] + [{"op": "list", "prefix": "m/zarr"}]
    return standard_queries(desc)


def invalid_descriptions(root: Path) -> dict[str, dict]:
    base = {"page_size": None, "mirror": True, "sources": [{"url": "data/blob.bin"}]}
    ref = {"key": "r", "ranges": [{"source": 0, "offset": 0, "length": 1}]}
    return {
        "duplicate_key": {**base, "entries": [{"key": "a", "bytes": ""}, {"key": "a", "bytes": "00"}]},
        "source_out_of_bounds": {**base, "entries": [
            {"key": "r", "ranges": [{"source": 1, "offset": 0, "length": 1}]}]},
        "key_source_missing": {**base, "sources": [{"key": "nope"}], "entries": [
            {"key": "r", "ranges": [{"source": 0, "length": 1}]}]},
        "key_source_names_reference": {**base, "sources": [{"url": "data/blob.bin"}, {"key": "r"}],
                                       "entries": [ref]},
        "reserved_sources_key": {**base, "entries": [{"key": "__vz__/sources", "bytes": ""}]},
        "reserved_index_key": {**base, "entries": [{"key": "__vz__/index", "bytes": ""}]},
        "empty_key": {**base, "entries": [{"key": "", "bytes": ""}]},
        "payload_too_large": {**base, "entries": [
            {"key": "r", "ranges": [{"data": "00" * 8} for _ in range(7000)]}]},
        "pinned_reference": {**base, "page_size": 100, "entries": [{**ref, "pinned": True}]},
        "pinned_without_index": {**base, "entries": [{"key": "a", "bytes": "", "pinned": True}]},
        "source_two_kinds": {**base, "sources": [{"url": "x", "data": "00"}], "entries": []},
        "empty_url": {**base, "sources": [{"url": ""}], "entries": []},
        "key_source_names_format_entry": {**base, "sources": [{"key": "__vz__/sources"}],
                                          "entries": []},
        "compress_on_reference": {**base, "entries": [{**ref, "compress": True}]},
        "page_size_zero": {**base, "page_size": 0, "entries": []},
        "entry_with_bytes_and_ranges": {**base, "entries": [{**ref, "bytes": "00"}]},
        "range_mixes_data_and_source": {**base, "entries": [
            {"key": "r", "ranges": [{"data": "00", "source": 0}]}]},
        "payload_over_limit": {**base, "entries": [
            {"key": "r", "ranges": [{"data": "ab" * 65516}]}]},
        "url_not_a_uri_reference": {**base, "sources": [{"url": "data/my file.bin"}],
                                    "entries": []},
        "url_non_ascii": {**base, "sources": [{"url": "data/é.bin"}], "entries": []},
        "pin_on_key_source": {**base, "sources": [{"key": "a", "size": 1}],
                              "entries": [{"key": "a", "bytes": "00"}]},
        "pin_on_data_source": {**base, "sources": [{"data": "00", "size": 1}], "entries": []},
        "weak_etag": {**base, "sources": [{"url": "x", "etag": 'W/"abc"'}], "entries": []},
        "unquoted_etag": {**base, "sources": [{"url": "x", "etag": "abc"}], "entries": []},
        "empty_range_without_sources": {**base, "sources": [], "entries": [
            {"key": "r", "ranges": [{}]}]},
        "uppercase_hex": {**base, "entries": [{"key": "a", "bytes": "AB"}]},
        "non_boolean_flag": {**base, "entries": [{"key": "a", "bytes": "", "compress": 1}]},
        "non_integer_page_size": {**base, "page_size": 1.0, "entries": []},
    }


# ---------------------------------------------------------------- crafted


def _writer(path: Path, sources=None, **kw):
    from refstore.archive import VZipWriter
    from refstore.pb import Source

    f = open(path, "wb")
    w = VZipWriter(f, **kw)
    for s in sources or [Source(url="data/blob.bin")]:
        w.source(s)
    return f, w


def _raw_ref(w, key: str, hid: int, payload: bytes, *, extra_blocks: bytes = b"") -> None:
    """Add a reference entry with an arbitrary payload, bypassing validation."""
    extra = struct.pack("<HH", hid, len(payload)) + payload + extra_blocks
    w._entry(key, payload, extra)
    w._ref_names.add(key)


def _varint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def crafted(root: Path) -> dict[str, dict]:
    """name -> {"build": fn(path), "open": "ok" | "fail", "expect": [(query, accept)],
    "read_path": optional fn(path) -> path to hand to the reader}"""
    from refstore.pb import Concat, Range, Source

    cases: dict[str, dict] = {}
    REF = {"ok": True, "kind": "reference"}

    def case(name, open="ok"):
        def deco(fn):
            cases[name] = {"build": fn, "open": open, "expect": getattr(fn, "expect", []),
                           "read_path": getattr(fn, "read_path", None)}
            return fn
        return deco

    def get(k, rng=None):
        return {"op": "get", "key": k, **({"range": rng} if rng else {})}

    def classify(k):
        return {"op": "classify", "key": k}

    def raw(k):
        return {"op": "get_raw", "key": k}

    E, P, R, B = (err(c) for c in ("entry", "payload", "resolution", "body"))

    def entry_error(k):
        return [(classify(k), [E]), (get(k), [E]), (raw(k), [E])]

    def payload_error(k):
        return [(classify(k), [REF]), (get(k), [P]), (get(k, {"start": 0, "end": 0}), [P])]

    fine = [(get("fine"), [ok_value(b"ok")])]

    # -- entry errors (§4.1, §4.3): only that key fails ---------------------
    def both_ids(path):
        f, w = _writer(path)
        p = Range(offset=1, length=2).encode()
        _raw_ref(w, "x", 0x7A76, p, extra_blocks=struct.pack("<HH", 0x7A77, 0))
        w.add_bytes("fine", b"ok")
        w.close(); f.close()
    both_ids.expect = entry_error("x") + fine + [
        ({"op": "list", "prefix": ""}, [{"ok": True, "keys": ["fine", "x"]}])]
    case("both_reference_ids")(both_ids)

    def dup_block(path):
        f, w = _writer(path)
        p = Range(offset=1, length=2).encode()
        _raw_ref(w, "x", 0x7A76, p, extra_blocks=struct.pack("<HH", 0x7A76, len(p)) + p)
        w.add_bytes("fine", b"ok")
        w.close(); f.close()
    dup_block.expect = entry_error("x") + fine
    case("duplicate_reference_block")(dup_block)

    def bad_extra(path):
        f, w = _writer(path)
        w._entry("x", b"abc", b"\x01\x02\x03")  # 3 bytes: not a whole block header
        w.add_bytes("fine", b"ok")
        w.close(); f.close()
    bad_extra.expect = entry_error("x") + fine
    case("unparseable_extra_field")(bad_extra)

    def deflated_ref(path):
        f, w = _writer(path)
        p = Range(offset=1, length=2).encode()
        w._entry("x", p, struct.pack("<HH", 0x7A76, len(p)) + p, compress=True)
        w.add_bytes("fine", b"ok")
        w.close(); f.close()
    deflated_ref.expect = entry_error("x") + fine
    case("reference_entry_deflated")(deflated_ref)

    def bad_method(path, paged=False):
        f, w = _writer(path, page_size=64 if paged else None)
        w.add_bytes("fine", b"ok")
        w.add_bytes("x", b"abc")
        for i in range(20):
            w.add_bytes(f"y{i:02d}", b"y")
        w.close(); f.close()
        _set_method(path, "x", 9)
    bad_method.expect = entry_error("x") + fine + [(get("y19"), [ok_value(b"y")])]
    case("unsupported_method")(bad_method)

    def bad_method_paged(path):
        bad_method(path, paged=True)
    bad_method_paged.expect = bad_method.expect
    case("unsupported_method_paged")(bad_method_paged)

    # -- payload errors (§5): classify says reference, every get fails ------
    def truncated(path):
        f, w = _writer(path)
        _raw_ref(w, "x", 0x7A76, b"\x18\xcf")  # varint cut short
        w.add_bytes("fine", b"ok")
        w.close(); f.close()
    truncated.expect = payload_error("x") + fine
    case("truncated_payload")(truncated)

    def wiretype(path):
        f, w = _writer(path)
        _raw_ref(w, "x", 0x7A76, b"\x1a\x01\x05")  # field 3 (offset) sent as LEN
        w.close(); f.close()
    wiretype.expect = payload_error("x")
    case("wire_type_mismatch")(wiretype)

    def long_varint(path):
        f, w = _writer(path)
        _raw_ref(w, "x", 0x7A76, b"\x18" + b"\x80" * 10 + b"\x01")  # 11-byte varint
        _raw_ref(w, "y", 0x7A76, b"\x18" + b"\xff" * 9 + b"\x02")   # 10 bytes, > 2^64-1
        w.close(); f.close()
    long_varint.expect = payload_error("x") + payload_error("y")
    case("varint_too_long_or_too_large")(long_varint)

    def field_zero(path):
        f, w = _writer(path)
        _raw_ref(w, "x", 0x7A76, b"\x00\x05\x20\x02")  # field number 0
        _raw_ref(w, "y", 0x7A76, _varint((2**29) << 3) + b"\x01\x20\x02")  # field 2^29
        w.close(); f.close()
    field_zero.expect = payload_error("x") + payload_error("y")
    case("invalid_field_numbers")(field_zero)

    def literal_nonzero(path):
        f, w = _writer(path)
        _raw_ref(w, "x", 0x7A76, b"\x20\x02\x2a\x02ab")  # length=2 and data
        w.add_bytes("fine", b"ok")
        w.close(); f.close()
    literal_nonzero.expect = payload_error("x") + fine
    case("literal_range_with_length")(literal_nonzero)

    def src_oob(path):
        f, w = _writer(path)
        _raw_ref(w, "x", 0x7A77, Concat((Range(data=b"ab"), Range(source=9, length=1))).encode())
        w.add_bytes("fine", b"ok")
        w.close(); f.close()
    src_oob.expect = payload_error("x") + fine + [(get("x", {"start": 0, "end": 1}), [P])]
    case("source_index_out_of_bounds")(src_oob)

    def uint32_overflow(path):
        f, w = _writer(path)
        _raw_ref(w, "x", 0x7A76, b"\x08" + _varint(2**32) + b"\x20\x02")  # source = 2^32
        w.close(); f.close()
    uint32_overflow.expect = payload_error("x")
    case("source_uint32_overflow")(uint32_overflow)

    def length_overflow(path):
        f, w = _writer(path)
        _raw_ref(w, "x", 0x7A76, b"\x18" + _varint(2**64 - 1) + b"\x20\x01")
        w.close(); f.close()
    length_overflow.expect = payload_error("x")
    case("offset_plus_length_overflow")(length_overflow)

    # -- accepted encodings (§5.1) -------------------------------------------
    def unknown_fields(path):
        f, w = _writer(path)
        # Range{offset=10,length=3} plus unknown varint (9), LEN (10), I64 (11), I32 (12)
        p = (b"\x18\x0a\x20\x03" + b"\x48" + _varint(300) + b"\x52\x02hi"
             + b"\x59" + b"\x00" * 8 + b"\x65" + b"\x00" * 4)
        _raw_ref(w, "x", 0x7A76, p)
        w.close(); f.close()
    unknown_fields.expect = [(get("x"), [ok_value(BLOB[10:13])])]
    case("unknown_fields_ignored")(unknown_fields)

    def last_wins(path):
        # offset given twice (last wins); a Source whose oneof is set twice (data wins)
        f, w = _writer(path, sources=[Source(url="data/blob.bin"), Source(data=b"placeholder")])
        w._source_table_override = (b"\x0a\x0f\x0a\x0ddata/blob.bin"
                                    + b"\x0a\x07" + b"\x0a\x01u" + b"\x1a\x02zz")
        _raw_ref(w, "x", 0x7A76, b"\x18\x05\x18\x0a\x20\x02")
        _raw_ref(w, "y", 0x7A76, b"\x08\x01\x20\x02")
        w.close(); f.close()
    last_wins.expect = [(get("x"), [ok_value(BLOB[10:12])]), (get("y"), [ok_value(b"zz")])]
    case("last_occurrence_wins")(last_wins)

    def out_of_order(path):
        f, w = _writer(path)
        _raw_ref(w, "x", 0x7A76, b"\x20\x02\x18\x04")  # length before offset
        _raw_ref(w, "y", 0x7A76, b"\x18\x84\x00\x20\x02")  # non-minimal varint offset = 4
        w.close(); f.close()
    out_of_order.expect = [(get("x"), [ok_value(BLOB[4:6])]), (get("y"), [ok_value(BLOB[4:6])])]
    case("non_canonical_encodings_accepted")(out_of_order)

    def concat_one(path):
        f, w = _writer(path)
        _raw_ref(w, "x", 0x7A77, Concat((Range(offset=3, length=2),)).encode())
        _raw_ref(w, "y", 0x7A77, b"")  # zero parts
        w.close(); f.close()
    concat_one.expect = [(get("x"), [ok_value(BLOB[3:5])]), (get("y"), [ok_value(b"")]),
                         (classify("y"), [REF])]
    case("concat_with_one_or_zero_parts")(concat_one)

    # -- reference bodies (§4.3): never checked, raw returns them -------------
    def empty_body(path):
        f, w = _writer(path)
        p = Range(offset=7, length=1).encode()
        w._entry("x", b"", struct.pack("<HH", 0x7A76, len(p)) + p)
        w.close(); f.close()
    empty_body.expect = [(get("x"), [ok_value(BLOB[7:8])]), (raw("x"), [ok_value(b"")])]
    case("reference_with_empty_body")(empty_body)

    # -- resolution errors (§6, §8.3) ----------------------------------------
    def key_to_ref(path):
        f, w = _writer(path, sources=[Source(url="data/blob.bin"), Source(key="target")])
        w._skip_checks = True
        w.add_ref("target", "data/blob.bin", 0, 4)
        _raw_ref(w, "x", 0x7A76, Range(source=1, length=2).encode())
        w.close(); f.close()
    key_to_ref.expect = [(get("x"), [R]), (get("target"), [ok_value(BLOB[:4])])]
    case("key_source_names_reference")(key_to_ref)

    def key_missing(path):
        f, w = _writer(path, sources=[Source(url="data/blob.bin"), Source(key="ghost")])
        w._skip_checks = True
        _raw_ref(w, "x", 0x7A76, Range(source=1, length=2).encode())
        _raw_ref(w, "z", 0x7A77, Concat((Range(data=b"ab"), Range(source=1, length=0))).encode())
        w.add_bytes("ghost-ish", b"no")
        w.close(); f.close()
    key_missing.expect = [(get("x"), [R]), (get("ghost-ish"), [ok_value(b"no")]),
                          (get("z"), [ok_value(b"ab")]),
                          (get("z", {"start": 0, "end": 1}), [ok_value(b"a")])]
    case("key_source_missing")(key_missing)

    def key_format(path):
        f, w = _writer(path, sources=[Source(url="data/blob.bin"), Source(key="__vz__/sources")])
        w._skip_checks = True
        _raw_ref(w, "x", 0x7A76, Range(source=1, length=1).encode())
        w.close(); f.close()
    key_format.expect = [(get("x"), [R])]
    case("key_source_names_format_entry")(key_format)

    def key_errored(path):
        f, w = _writer(path, sources=[Source(url="data/blob.bin"), Source(key="broken")])
        w._entry("broken", b"abc", b"\x01")  # entry error: bad extra field
        _raw_ref(w, "x", 0x7A76, Range(source=1, length=1).encode())
        w.close(); f.close()
    key_errored.expect = [(get("x"), [R])] + entry_error("broken")
    case("key_source_names_entry_with_error")(key_errored)

    def symlinked(path):
        # archive in vectors/sym_real/deep/, opened through vectors/sym_link -> sym_real/deep
        real = path.parent / "sym_real" / "deep"
        real.mkdir(parents=True, exist_ok=True)
        link = path.parent / "sym_link"
        if not link.exists():
            link.symlink_to(real, target_is_directory=True)
        f, w = _writer(real / path.name)
        w.add_ref("x", "../data/blob.bin", 3, 2)  # lexically: vectors/data/blob.bin
        w.close(); f.close()
    symlinked.read_path = lambda path: path.parent / "sym_link" / path.name
    symlinked.expect = [(get("x"), [ok_value(BLOB[3:5])])]
    case("base_uri_not_symlink_resolved")(symlinked)

    # -- revision 3: check order, body errors, ignored records, pins over time ---
    def hidden_entry_error(path):
        f, w = _writer(path)
        w._entry("__vz__/broken", b"abc", b"\x01")  # hidden + entry error
        w.add_bytes("fine", b"ok")
        w.close(); f.close()
    hidden_entry_error.expect = [
        (classify("__vz__/broken"), [{"ok": True, "kind": "missing"}]),
        (get("__vz__/broken"), [ok_value(None)]),
        (raw("__vz__/broken"), [E]),
        (get("nope", {"start": 3, "end": 1}), [err("request")]),
        (get("__vz__/broken", {"start": 3, "end": 1}), [err("request")])]
    case("check_order_hidden_and_request")(hidden_entry_error)

    def corrupt_deflate(path):
        f, w = _writer(path, sources=[Source(url="data/blob.bin"), Source(key="z")])
        w.add_bytes("z", b"hello hello hello", compress=True)
        w.add_ref("r", "data/blob.bin", 0, 1)
        _raw_ref(w, "k", 0x7A76, Range(source=1, length=2).encode())
        w.close(); f.close()
        b = bytearray(path.read_bytes())
        i = b.find(b"PK\x03\x04z") if False else _body_offset(b, "z")
        b[i] = 0xFF  # an invalid DEFLATE block type
        b[i + 1] = 0xFF
        path.write_bytes(bytes(b))
    corrupt_deflate.expect = [
        (classify("z"), [{"ok": True, "kind": "bytes"}]), (get("z"), [B]), (raw("z"), [B]),
        (get("z", {"start": 0, "end": 1}), [B]), (get("k"), [R]), (get("r"), [ok_value(BLOB[:1])])]
    case("body_does_not_inflate")(corrupt_deflate)

    def wrong_size(path):
        f, w = _writer(path)
        w.add_bytes("z", b"hello hello hello", compress=True)
        w.close(); f.close()
        _set_size(path, "z", 99)
    wrong_size.expect = [(get("z"), [B]), (raw("z"), [B]), (classify("z"), [{"ok": True, "kind": "bytes"}])]
    case("body_inflates_to_wrong_size")(wrong_size)

    def ignored_names(path):
        f, w = _writer(path)
        w.add_bytes("fine", b"ok")
        w._entry("\udcff" if False else "zz-invalid", b"x")
        w._entry("zz-empty", b"y")
        w.close(); f.close()
        _rename(path, "zz-invalid", b"\xff\xfe\xfd\xfc\xfb\xfa\xf9\xf8\xf7\xf6")
        _rename(path, "zz-empty", b"")
    ignored_names.expect = fine + [({"op": "list", "prefix": ""}, [{"ok": True, "keys": ["fine"]}])]
    case("records_with_empty_or_invalid_names_ignored")(ignored_names)

    def stale(path):
        d = path.parent / "data"
        (d / "regen.bin").write_bytes(bytes(range(100)))
        os.utime(d / "regen.bin", (MTIME, MTIME))
        f, w = _writer(path, sources=[
            Source(url="data/regen.bin"),
            Source(url="data/regen.bin", size=100, modified_not_after=MTIME),
            Source(url="data/regen.bin", size=100)])
        _raw_ref(w, "unpinned", 0x7A76, Range(source=0, offset=10, length=4).encode())
        _raw_ref(w, "pinned", 0x7A76, Range(source=1, offset=10, length=4).encode())
        _raw_ref(w, "size_only", 0x7A76, Range(source=2, offset=10, length=4).encode())
        w.close(); f.close()
        # the producer regenerates the file in place: same size, new values, newer mtime
        (d / "regen.bin").write_bytes(bytes(range(100, 200)))
        os.utime(d / "regen.bin", (MTIME + 86400, MTIME + 86400))
    stale.expect = [(get("unpinned"), [ok_value(bytes(range(110, 114)))]),
                    (get("pinned"), [R]),
                    (get("size_only"), [ok_value(bytes(range(110, 114)))])]
    case("source_regenerated_in_place")(stale)

    def invalid_uri(path):
        f, w = _writer(path, sources=[Source(url="data/blob.bin")])
        w._skip_checks = True
        for i, u in enumerate(["data/my file.bin", "%zz", "data/é.bin", "a:b/../x", "1a:b"]):
            w._sources[Source(url=u)] = i + 1
            _raw_ref(w, f"u{i}", 0x7A76, Range(source=i + 1, length=1).encode())
        w.close(); f.close()
    invalid_uri.expect = [(get(f"u{i}"), [R]) for i in range(5)]
    case("url_not_a_uri_reference")(invalid_uri)

    def bad_page(path):
        f, w = _writer(path, page_size=64)
        for i in range(30):
            w.add_bytes(f"k{i:02d}", b"v")
        w.add_bytes("zz", b"last")
        w.close(); f.close()
        _corrupt_cd_record(path, "k10")
    bad_page.expect = [(get("k10"), [E]), (classify("k10"), [E]), (get("zz"), [ok_value(b"last")]),
                       ({"op": "list", "prefix": "k1"}, [E]),
                       ({"op": "list", "prefix": "zz"}, [{"ok": True, "keys": ["zz"]}])]
    case("page_that_cannot_be_parsed")(bad_page)

    # -- revision 4 ----------------------------------------------------------
    def false_locator(path):
        # unpaged, trailer records first, so the last record is "zz..." whose name ends
        # with a zip64 locator signature exactly 20 bytes before the EOCD
        f, w = _writer(path)
        w._trailer_first = True
        name = "zz" + "PK\x06\x07" + "\x01" * 16
        w.add_bytes(name, b"still fine")
        w.add_bytes("fine", b"ok")
        w.close(); f.close()
        b = path.read_bytes()
        assert b[len(b) - 44 - 20 : len(b) - 44 - 16] == b"PK\x06\x07"
    false_locator.expect = fine + [(get("zz" + "PK\x06\x07" + "\x01" * 16), [ok_value(b"still fine")])]
    case("false_zip64_locator_signature")(false_locator)

    def trailing_bytes(path):
        f, w = _writer(path)
        body = b"hello hello hello"
        c = zlib.compressobj(9, zlib.DEFLATED, -15)
        stored = c.compress(body) + c.flush() + b"\x00junk"
        w._entry_raw("z", body, stored)
        w.add_bytes("fine", b"ok")
        w.close(); f.close()
    trailing_bytes.expect = fine + [(get("z"), [B]), (raw("z"), [B]),
                                    (get("z", {"start": 0, "end": 1}), [B])]
    case("deflate_trailing_bytes")(trailing_bytes)

    def truncated_deflate(path):
        f, w = _writer(path)
        body = bytes(range(256)) * 8
        c = zlib.compressobj(9, zlib.DEFLATED, -15)
        stored = (c.compress(body) + c.flush())[:-3]
        w._entry_raw("z", body, stored)
        w.close(); f.close()
    truncated_deflate.expect = [(get("z", {"start": 0, "end": 1}), [B]), (raw("z"), [B])]
    case("deflate_truncated_window_at_start")(truncated_deflate)

    def sources_trailing(path):
        f, w = _writer(path)
        w._sources_trailing_junk = True
        w.close(); f.close()
    case("source_table_trailing_bytes", open="fail")(sources_trailing)

    def stored_size_mismatch(path):
        f, w = _writer(path)
        w.add_bytes("z", b"hello")
        w.add_bytes("fine", b"ok")
        w.close(); f.close()
        _set_size(path, "z", 9)
    stored_size_mismatch.expect = fine + [(get("z"), [B]), (raw("z"), [B])]
    case("stored_sizes_differ")(stored_size_mismatch)

    def offset_sentinel(path):
        f, w = _writer(path)
        w.add_bytes("z", b"hello")
        w.add_bytes("fine", b"ok")
        w.close(); f.close()
        b = bytearray(path.read_bytes())
        i = next(_records(b, b"PK\x01\x02", 28, 46, b"z"))
        struct.pack_into("<I", b, i + 42, 0xFFFFFFFF)
        path.write_bytes(bytes(b))
    offset_sentinel.expect = fine + entry_error("z")
    case("offset_sentinel_without_zip64_block")(offset_sentinel)

    def pre_epoch(path):
        d = path.parent / "data"
        (d / "old.bin").write_bytes(b"ancient")
        os.utime(d / "old.bin", (-1.5, -1.5))  # floor(-1.5) = -2
        f, w = _writer(path, sources=[Source(url="data/old.bin", modified_not_after=-2),
                                      Source(url="data/old.bin", modified_not_after=-3)])
        _raw_ref(w, "ok", 0x7A76, Range(source=0, length=3).encode())
        _raw_ref(w, "bad", 0x7A76, Range(source=1, length=3).encode())
        w.close(); f.close()
    pre_epoch.expect = [(get("ok"), [ok_value(b"anc")]), (get("bad"), [R])]
    case("pre_epoch_mtime_rounds_down")(pre_epoch)

    # -- archive errors (§8.4): open fails ------------------------------------
    def pin_on_key(path):
        f, w = _writer(path)
        w._source_table_override = b"\x0a\x0f\x0a\x0ddata/blob.bin" + b"\x0a\x05\x12\x01a\x20\x01"
        w.add_bytes("a", b"x")
        w.close(); f.close()
    case("pin_on_key_source", open="fail")(pin_on_key)

    def weak_etag(path):
        f, w = _writer(path)
        w._source_table_override = b"\x0a\x16\x0a\x0ddata/blob.bin\x2a\x05W/\"a\""
        w.close(); f.close()
    case("weak_etag_pin", open="fail")(weak_etag)

    def page_outside(path, mutate=None):
        f, w = _writer(path, page_size=64)
        for i in range(20):
            w.add_bytes(f"k{i:02d}", b"v")
        w.close(); f.close()
        _rewrite_index(path, mutate)
    case("page_outside_central_directory", open="fail")(
        lambda p: page_outside(p, lambda idx: setattr(idx.pages[-1], "length", 10**6)))
    case("page_keys_not_increasing", open="fail")(
        lambda p: page_outside(p, lambda idx: setattr(idx.pages[1], "first_key", "a")))
    case("pinned_method_invalid", open="fail")(
        lambda p: page_outside(p, lambda idx: idx.pinned.add(key="k00", data_offset=0, size=1,
                                                             csize=1, method=9)))
    case("pinned_duplicate", open="fail")(
        lambda p: page_outside(p, lambda idx: [idx.pinned.add(key="k00", data_offset=0, size=1,
                                                              csize=1, method=0) for _ in "ab"]))

    def sentinel_without_locator(path):
        f, w = _writer(path)
        w.add_bytes("a", b"x")
        w.close(); f.close()
        b = bytearray(path.read_bytes())
        struct.pack_into("<H", b, len(b) - 44 + 10, 0xFFFF)  # total entries = 0xFFFF
        path.write_bytes(bytes(b))
    case("zip64_sentinel_without_locator", open="fail")(sentinel_without_locator)

    def empty_url(path):
        f, w = _writer(path, sources=[Source(url="data/blob.bin"), Source(url="")])
        w._skip_checks = True
        _raw_ref(w, "x", 0x7A76, Range(source=1, length=2).encode())
        w.close(); f.close()
    case("empty_url_source", open="fail")(empty_url)

    def source_no_kind(path):
        f, w = _writer(path)
        w._source_table_override = b"\x0a\x0f\x0a\x0ddata/blob.bin" + b"\x0a\x00"
        w.add_bytes("fine", b"ok")
        w.close(); f.close()
    case("source_without_kind", open="fail")(source_no_kind)

    def bad_comment_offset(path):
        f, w = _writer(path)
        w.add_ref("x", "data/blob.bin", 0, 2)
        w.close(); f.close()
        b = bytearray(path.read_bytes())
        struct.pack_into("<Q", b, len(b) - 16, 3)  # sources_offset -> 3
        path.write_bytes(bytes(b))
    case("comment_offset_wrong", open="fail")(bad_comment_offset)

    def other_version(path):
        f, w = _writer(path)
        w.add_bytes("fine", b"ok")
        w.close(); f.close()
        b = bytearray(path.read_bytes())
        b[len(b) - 22 : len(b) - 16] = b"vzip/2"
        path.write_bytes(bytes(b))
    case("other_version", open="fail")(other_version)

    def index_unpaged(path):
        f, w = _writer(path)
        w._entry("__vz__/index", b"", compress=True)
        w.add_bytes("fine", b"ok")
        w.close(); f.close()
    case("index_entry_without_page_index", open="fail")(index_unpaged)

    def foreign(path):
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("a/zarr.json", b'{"zarr_format":3}')
            zf.writestr("a/c/0", bytes(100))
    case("foreign_zip", open="fail")(foreign)

    return cases


def _records(b: bytes, sig: bytes, name_len_at: int, name_at: int, key: bytes):
    i = 0
    while (i := b.find(sig, i)) >= 0:
        n = struct.unpack_from("<H", b, i + name_len_at)[0]
        if n == len(key) and b[i + name_at : i + name_at + n] == key:
            yield i
        i += 4


def _body_offset(b: bytes, key: str) -> int:
    i = next(_records(b, b"PK\x03\x04", 26, 30, key.encode()))
    return i + 30 + len(key.encode())


def _set_size(path: Path, key: str, size: int) -> None:
    """Change an entry's uncompressed size in both headers."""
    b = bytearray(path.read_bytes())
    for i in _records(b, b"PK\x03\x04", 26, 30, key.encode()):
        struct.pack_into("<I", b, i + 22, size)
    for i in _records(b, b"PK\x01\x02", 28, 46, key.encode()):
        struct.pack_into("<I", b, i + 24, size)
    path.write_bytes(bytes(b))


def _rename(path: Path, key: str, new: bytes) -> None:
    """Replace an entry's name (in both headers) with `new`, keeping offsets valid
    by padding: only equal-length or shorter names; shorter names are padded into
    the extra field of the central directory record."""
    b = bytearray(path.read_bytes())
    old = key.encode()
    assert len(new) <= len(old)
    for i in _records(b, b"PK\x01\x02", 28, 46, old):
        pad = len(old) - len(new)
        xlen = struct.unpack_from("<H", b, i + 30)[0]
        b[i + 46 : i + 46 + len(old)] = new + struct.pack("<HH", 0x5A5A, pad - 4) + b"\0" * (pad - 4) \
            if pad >= 4 else new + b"\0" * pad
        struct.pack_into("<H", b, i + 28, len(new))
        struct.pack_into("<H", b, i + 30, xlen + pad)
    path.write_bytes(bytes(b))


def _corrupt_cd_record(path: Path, key: str) -> None:
    """Break the signature of a central directory record (its page no longer parses)."""
    b = bytearray(path.read_bytes())
    i = next(_records(b, b"PK\x01\x02", 28, 46, key.encode()))
    b[i : i + 4] = b"XXXX"
    path.write_bytes(bytes(b))


def _rewrite_index(path: Path, mutate) -> None:
    """Re-encode __vz__/index after `mutate(CdIndex)`; the new body replaces the old one in
    place (padding the comment-declared size is not possible, so the index is re-deflated and
    must not grow: the archive is rebuilt with the body appended before the central directory)."""
    import zlib as _z

    import pbref

    b = bytearray(path.read_bytes())
    c = b[-38:]
    ioff, isize = struct.unpack_from("<QQ", c, 22)
    idx = pbref.CdIndex()
    idx.ParseFromString(_z.decompress(bytes(b[ioff : ioff + isize]), -15))
    mutate(idx)
    comp = _z.compressobj(9, _z.DEFLATED, -15)
    new = comp.compress(idx.SerializeToString()) + comp.flush()
    # append the new body at the end of the file and point the comment at it; readers
    # use the comment's offsets (spec §3.4), and a ZIP tool still sees the old entry
    eocd = bytes(b[-60:])
    body_off = len(b) - 60
    out = b[:-60] + new + eocd
    struct.pack_into("<I", out, len(out) - 60 + 16,
                     struct.unpack_from("<I", eocd, 16)[0])  # cd offset unchanged
    struct.pack_into("<QQ", out, len(out) - 16, body_off, len(new))
    path.write_bytes(bytes(out))


def _set_method(path: Path, key: str, method: int) -> None:
    """Change an entry's compression method in both headers."""
    b = bytearray(path.read_bytes())
    name = key.encode()
    i = 0
    while (i := b.find(b"PK\x03\x04", i)) >= 0:
        if b[i + 30 : i + 30 + len(name)] == name and struct.unpack_from("<H", b, i + 26)[0] == len(name):
            struct.pack_into("<H", b, i + 8, method)
        i += 4
    i = 0
    while (i := b.find(b"PK\x01\x02", i)) >= 0:
        if b[i + 46 : i + 46 + len(name)] == name and struct.unpack_from("<H", b, i + 28)[0] == len(name):
            struct.pack_into("<H", b, i + 10, method)
        i += 4
    path.write_bytes(bytes(b))
