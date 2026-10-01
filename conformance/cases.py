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

import struct
import zipfile
from pathlib import Path

H = lambda b: b.hex() if isinstance(b, bytes) else b.encode().hex()  # noqa: E731
ERROR = {"ok": False}


def ok_value(b: bytes | None) -> dict:
    return {"ok": True, "value": None if b is None else b.hex()}


def write_data_files(root: Path) -> None:
    (root / "data" / "sub").mkdir(parents=True, exist_ok=True)
    (root / "data" / "blob.bin").write_bytes(bytes(range(256)) * 4)
    (root / "data" / "sub" / "my file.bin").write_bytes(b"spaced-out bytes")
    (root / "data" / "short.bin").write_bytes(b"0123456789")
    (root / "data" / "é.bin").write_bytes(b"accented")


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
            {"url": "data/sub/my file.bin"},
            {"url": "file://elsewhere/data/blob.bin"},
            {"url": "file:data/blob.bin"},
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
            {"key": "r/raw_space", "ranges": [{"source": 12, "offset": 0, "length": 2}]},
            {"key": "r/authority", "ranges": [{"source": 13, "offset": 0, "length": 2}]},
            {"key": "r/scheme_relative", "ranges": [{"source": 14, "offset": 0, "length": 2}]},
            {"key": "r/oob_tail", "ranges": [
                {"source": 3, "offset": 4, "length": 5}, {"data": H(b"!")}]},
            {"key": "r/zero_len_bad", "ranges": [
                {"data": H(b"z")}, {"source": 5, "offset": 0, "length": 0}]},
        ],
    }


def descriptions(root: Path) -> dict[str, dict]:
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
    empty = {"page_size": None, "mirror": True, "sources": [], "entries": []}
    empty_paged = {**empty, "page_size": 100}
    return {
        "basic": basic,
        "nomirror": nomirror,
        "paged": paged,
        "zip64_paged": many,
        "empty": empty,
        "empty_paged": empty_paged,
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
        "pinned_hidden": {**base, "page_size": 100,
                          "entries": [{"key": "__vz__/h", "bytes": "", "pinned": True}]},
        "source_two_kinds": {**base, "sources": [{"url": "x", "data": "00"}], "entries": []},
        "empty_url": {**base, "sources": [{"url": ""}], "entries": []},
        "key_source_names_format_entry": {**base, "sources": [{"key": "__vz__/sources"}],
                                          "entries": []},
        "compress_on_reference": {**base, "entries": [{**ref, "compress": True}]},
        "page_size_zero": {**base, "page_size": 0, "entries": []},
        "entry_with_bytes_and_ranges": {**base, "entries": [{**ref, "bytes": "00"}]},
        "range_mixes_data_and_source": {**base, "entries": [
            {"key": "r", "ranges": [{"data": "00", "source": 0}]}]},
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

    def entry_error(k):
        return [(classify(k), [ERROR]), (get(k), [ERROR]), (raw(k), [ERROR])]

    def payload_error(k):
        return [(classify(k), [REF]), (get(k), [ERROR]), (get(k, {"start": 0, "end": 0}), [ERROR])]

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
    src_oob.expect = payload_error("x") + fine + [(get("x", {"start": 0, "end": 1}), [ERROR])]
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
    key_to_ref.expect = [(get("x"), [ERROR]), (get("target"), [ok_value(BLOB[:4])])]
    case("key_source_names_reference")(key_to_ref)

    def key_missing(path):
        f, w = _writer(path, sources=[Source(url="data/blob.bin"), Source(key="ghost")])
        w._skip_checks = True
        _raw_ref(w, "x", 0x7A76, Range(source=1, length=2).encode())
        _raw_ref(w, "z", 0x7A77, Concat((Range(data=b"ab"), Range(source=1, length=0))).encode())
        w.add_bytes("ghost-ish", b"no")
        w.close(); f.close()
    key_missing.expect = [(get("x"), [ERROR]), (get("ghost-ish"), [ok_value(b"no")]),
                          (get("z"), [ok_value(b"ab")]),
                          (get("z", {"start": 0, "end": 1}), [ok_value(b"a")])]
    case("key_source_missing")(key_missing)

    def key_format(path):
        f, w = _writer(path, sources=[Source(url="data/blob.bin"), Source(key="__vz__/sources")])
        w._skip_checks = True
        _raw_ref(w, "x", 0x7A76, Range(source=1, length=1).encode())
        w.close(); f.close()
    key_format.expect = [(get("x"), [ERROR])]
    case("key_source_names_format_entry")(key_format)

    def key_errored(path):
        f, w = _writer(path, sources=[Source(url="data/blob.bin"), Source(key="broken")])
        w._entry("broken", b"abc", b"\x01")  # entry error: bad extra field
        _raw_ref(w, "x", 0x7A76, Range(source=1, length=1).encode())
        w.close(); f.close()
    key_errored.expect = [(get("x"), [ERROR])] + entry_error("broken")
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

    # -- archive errors (§8.4): open fails ------------------------------------
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
