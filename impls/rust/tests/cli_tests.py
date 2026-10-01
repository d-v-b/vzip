#!/usr/bin/env python3
"""End-to-end tests of the vzip CLI (HARNESS.md), derived from SPEC.md.

Run: python3 tests/cli_tests.py   (after `cargo build --release`)
"""
import email.utils
import http.server
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "vzip")
TMP = tempfile.mkdtemp(prefix="vzip-tests-")
FAILS = []
PASSES = 0


def check(name, cond, info=""):
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(f"{name}: {info}")
        print(f"FAIL {name}: {info}")


def path(n):
    return os.path.join(TMP, n)


def write(desc, name):
    dp = path(name + ".json")
    with open(dp, "w") as f:
        f.write(desc if isinstance(desc, str) else json.dumps(desc))
    out = path(name)
    r = subprocess.run([CLI, "write", dp, out], capture_output=True, text=True, errors="replace")
    return r, out


def read(archive, queries, cwd=None):
    qp = path("q.json")
    with open(qp, "w") as f:
        f.write(queries if isinstance(queries, str) else json.dumps(queries))
    r = subprocess.run([CLI, "read", archive, qp], capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        return r.returncode, r.stderr
    return 0, json.loads(r.stdout)


def results(archive, queries, cwd=None):
    rc, out = read(archive, queries, cwd)
    assert rc == 0, out
    assert out["open"]["ok"], out
    return out["results"]


def H(b):
    return b.hex()


# ------------------------------------------------------------ protobuf helpers

def varint(v):
    v &= (1 << 64) - 1
    out = b""
    while True:
        c = v & 0x7F
        v >>= 7
        if v:
            out += bytes([c | 0x80])
        else:
            return out + bytes([c])


def fld(n, wt, payload):
    t = varint(n << 3 | wt)
    if wt == 0:
        return t + varint(payload)
    if wt == 2:
        return t + varint(len(payload)) + payload
    raise ValueError


def pb_range(source=0, offset=0, length=0, data=None):
    o = b""
    if source:
        o += fld(1, 0, source)
    if offset:
        o += fld(3, 0, offset)
    if length:
        o += fld(4, 0, length)
    if data is not None:
        o += fld(5, 2, data)
    return o


def pb_concat(parts):
    return b"".join(fld(1, 2, p) for p in parts)


def pb_source(url=None, key=None, data=None, size=None, etag=None, mna=None):
    o = b""
    if url is not None:
        o += fld(1, 2, url.encode())
    if key is not None:
        o += fld(2, 2, key.encode())
    if data is not None:
        o += fld(3, 2, data)
    if size is not None:
        o += fld(4, 0, size)
    if etag is not None:
        o += fld(5, 2, etag.encode())
    if mna is not None:
        o += fld(6, 0, mna)
    return o


def pb_table(sources):
    return b"".join(fld(1, 2, s) for s in sources)


def raw_deflate(b):
    c = zlib.compressobj(6, zlib.DEFLATED, -15)
    return c.compress(b) + c.flush()


# ------------------------------------------------------------ raw ZIP builder

def build_zip(entries, sources_pb, comment=None, extra_cd=b""):
    """entries: list of dicts name, body(bytes, stored as given), method, usize,
    extra(cd extra bytes), flags, csize override."""
    out = b""
    cd = b""
    for e in entries:
        name = e["name"].encode() if isinstance(e["name"], str) else e["name"]
        body = e.get("body", b"")
        method = e.get("method", 0)
        usize = e.get("usize", len(body))
        csize = e.get("csize", len(body))
        flags = e.get("flags", 0x800)
        crc = e.get("crc", zlib.crc32(body) if method == 0 else 0)
        lho = len(out)
        out += struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flags, method, 0, 0x21, crc, csize, usize, len(name), 0) + name + body
        extra = e.get("extra", b"")
        cd += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, flags, method, 0, 0x21, crc, csize, usize,
                          len(name), len(extra), 0, 0, 0, 0, e.get("lho", lho)) + name + extra
    sbody = raw_deflate(sources_pb)
    slho = len(out)
    sname = b"__vz__/sources"
    out += struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0x800, 8, 0, 0x21, zlib.crc32(sources_pb), len(sbody),
                       len(sources_pb), len(sname), 0) + sname + sbody
    cd += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0x800, 8, 0, 0x21, zlib.crc32(sources_pb), len(sbody),
                      len(sources_pb), len(sname), 0, 0, 0, 0, 0, slho) + sname
    cd += extra_cd
    cdo = len(out)
    out += cd
    if comment is None:
        comment = b"vzip/0" + struct.pack("<QQ", slho + 30 + len(sname), len(sbody))
    n = len(entries) + 1
    out += struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, n, n, len(cd), cdo, len(comment)) + comment
    return out


def ref_extra(payload, id=0x7A76):
    return struct.pack("<HH", id, len(payload)) + payload


def save(name, data):
    p = path(name)
    with open(p, "wb") as f:
        f.write(data)
    return p


# ------------------------------------------------------------ tests

def test_roundtrip():
    with open(path("data.bin"), "wb") as f:
        f.write(bytes(range(256)) * 4)
    os.makedirs(path("sub dir"), exist_ok=True)
    with open(path("sub dir/é.bin"), "wb") as f:
        f.write(b"hello world")
    st = os.stat(path("data.bin"))
    for page_size in [None, 1, 120, 100000]:
        for mirror in [True, False]:
            desc = {
                "page_size": page_size,
                "mirror": mirror,
                "sources": [
                    {"url": "data.bin", "size": 1024, "modified_not_after": int(st.st_mtime)},
                    {"key": "__vz__/hdr"},
                    {"data": "48445221"},
                    {"url": "sub%20dir/%C3%A9.bin"},
                    {"url": "file://" + path("data.bin").replace(" ", "%20")},
                ],
                "entries": [
                    {"key": "x/zarr.json", "bytes": H(b'{"zarr_format":3}'), "compress": True,
                     "pinned": page_size is not None},
                    {"key": "__vz__/hdr", "bytes": H(b"HEADER"), "pinned": page_size is not None},
                    {"key": "x/c/0", "ranges": [{"source": 0, "offset": 10, "length": 4}]},
                    {"key": "x/c/1", "ranges": [{"source": 2, "offset": 0, "length": 3}, {"data": "00ff"}]},
                    {"key": "x/c/2", "ranges": [{"source": 1, "offset": 1, "length": 3}, {"source": 3, "offset": 6, "length": 5}]},
                    {"key": "x/c/3", "ranges": []},
                    {"key": "x/c/4", "ranges": [{"data": ""}]},
                    {"key": "x/c/5", "ranges": [{"source": 4, "offset": 1020, "length": 4}], "compress": False},
                    {"key": "y", "bytes": ""},
                    {"key": "a/../b", "bytes": "01"},
                    {"key": "x/", "bytes": "02"},
                    {"key": "﻿bom", "bytes": "03"},
                    {"key": "\U0001F600", "bytes": "04"},
                    {"key": "～", "bytes": "05"},
                    {"key": "big", "bytes": H(os.urandom(5000)), "compress": True},
                ],
            }
            r, out = write(desc, f"rt-{page_size}-{mirror}.vzip")
            tag = f"roundtrip[{page_size},{mirror}]"
            check(tag + " write", r.returncode == 0, r.stderr)
            if r.returncode:
                continue
            u = subprocess.run(["unzip", "-t", out], capture_output=True, text=True, errors="replace")
            check(tag + " unzip -t", u.returncode == 0, u.stdout + u.stderr)
            big = bytes.fromhex(desc["entries"][-1]["bytes"])
            qs = [
                {"op": "get", "key": "x/zarr.json"},
                {"op": "get", "key": "x/c/0"},
                {"op": "get", "key": "x/c/1"},
                {"op": "get", "key": "x/c/2"},
                {"op": "get", "key": "x/c/3"},
                {"op": "get", "key": "x/c/4"},
                {"op": "get", "key": "x/c/5"},
                {"op": "get", "key": "x/c/2", "range": {"start": 2, "end": 5}},
                {"op": "get", "key": "x/c/2", "range": {"offset": 6}},
                {"op": "get", "key": "x/c/2", "range": {"suffix": 100}},
                {"op": "get", "key": "x/c/2", "range": {"start": 50, "end": 60}},
                {"op": "get", "key": "big", "range": {"suffix": 3}},
                {"op": "classify", "key": "x/c/0"},
                {"op": "classify", "key": "x/zarr.json"},
                {"op": "classify", "key": "__vz__/hdr"},
                {"op": "classify", "key": "nope"},
                {"op": "get", "key": "__vz__/hdr"},
                {"op": "get_raw", "key": "__vz__/hdr"},
                {"op": "get_raw", "key": "x/c/0"},
                {"op": "get_raw", "key": "__vz__/index"},
                {"op": "list", "prefix": ""},
                {"op": "list", "prefix": "x/c/"},
                {"op": "list", "prefix": "__vz__/"},
                {"op": "get", "key": "x/c/0", "range": {"start": 3, "end": 2}},
                {"op": "get", "key": "nope", "range": {"start": 3, "end": 2}},
                {"op": "get", "key": "nope"},
                {"op": "get", "key": "y"},
            ]
            res = results(out, qs)
            vals = [x.get("value") for x in res]
            exp_c2 = b"EAD" + b"world"
            payload0 = bytes.fromhex("0a")  # placeholder
            expect = [
                H(b'{"zarr_format":3}'), H(bytes([10, 11, 12, 13])), H(b"HDR\x00\xff"), H(exp_c2), "", "",
                H(bytes([252, 253, 254, 255])), H(exp_c2[2:5]), H(exp_c2[6:]), H(exp_c2), "", H(big[-3:]),
            ]
            check(tag + " values", vals[:12] == expect, f"{vals[:12]} != {expect}")
            kinds = [x.get("kind") for x in res[12:16]]
            check(tag + " classify", kinds == ["reference", "bytes", "missing", "missing"], kinds)
            check(tag + " hidden get", res[16] == {"ok": True, "value": None}, res[16])
            check(tag + " raw hidden", res[17].get("value") == H(b"HEADER"), res[17])
            want_raw = H(b"\x18\x0a\x20\x04") if mirror else ""
            check(tag + " raw ref", res[18].get("value") == want_raw, res[18])
            check(tag + " raw index", (res[19]["value"] is None) == (page_size is None), res[19])
            allk = sorted([e["key"] for e in desc["entries"] if not e["key"].startswith("__vz__/")],
                          key=lambda s: s.encode())
            check(tag + " list all", res[20].get("keys") == allk, res[20])
            check(tag + " list prefix", res[21].get("keys") == [f"x/c/{i}" for i in range(6)], res[21])
            check(tag + " list hidden", res[22].get("keys") == [], res[22])
            check(tag + " request error", res[23].get("class") == "request", res[23])
            check(tag + " request error missing key", res[24].get("class") == "request", res[24])
            check(tag + " missing", res[25] == {"ok": True, "value": None}, res[25])
            check(tag + " empty bytes", res[26].get("value") == "", res[26])
            # relative path open from another cwd gives same base
            res2 = results(os.path.basename(out), [{"op": "get", "key": "x/c/0"}], cwd=TMP)
            check(tag + " relative open", res2[0].get("value") == H(bytes([10, 11, 12, 13])), res2)


def test_writer_rejects():
    base_entries = [{"key": "k", "bytes": "00"}]
    bad = {
        "empty key": {"entries": [{"key": "", "bytes": "00"}]},
        "dup key": {"entries": [{"key": "a", "bytes": "00"}, {"key": "a", "bytes": "00"}]},
        "format key": {"entries": [{"key": "__vz__/sources", "bytes": "00"}]},
        "index key": {"entries": [{"key": "__vz__/index", "bytes": "00"}]},
        "source oob": {"entries": [{"key": "a", "ranges": [{"source": 0, "length": 1}]}]},
        "empty range no sources": {"entries": [{"key": "a", "ranges": [{}]}]},
        "empty url": {"sources": [{"url": ""}]},
        "bad url": {"sources": [{"url": "a b"}]},
        "non-ascii url": {"sources": [{"url": "é.bin"}]},
        "empty key source": {"sources": [{"key": ""}]},
        "absent key source": {"sources": [{"key": "zz"}], "entries": base_entries},
        "ref key source": {"sources": [{"key": "r"}], "entries": [{"key": "r", "ranges": []}]},
        "format key source": {"sources": [{"key": "__vz__/sources"}]},
        "key range past end": {"sources": [{"key": "k"}], "entries": base_entries + [{"key": "r", "ranges": [{"source": 0, "offset": 0, "length": 2}]}]},
        "data range past end": {"sources": [{"data": "0011"}], "entries": [{"key": "r", "ranges": [{"source": 0, "offset": 1, "length": 2}]}]},
        "pin on data": {"sources": [{"data": "00", "size": 1}]},
        "pin on key": {"sources": [{"key": "k", "etag": "\"a\""}], "entries": base_entries},
        "weak etag": {"sources": [{"url": "a", "etag": "W/\"a\""}]},
        "unquoted etag": {"sources": [{"url": "a", "etag": "abc"}]},
        "payload too big": {"entries": [{"key": "r", "ranges": [{"data": "00" * 65520}]}]},
        "offset+length overflow": {"sources": [{"url": "a"}], "entries": [{"key": "r", "ranges": [{"source": 0, "offset": 2**64 - 1, "length": 1}] }]},
        "pinned no index": {"entries": [{"key": "k", "bytes": "00", "pinned": True}]},
        "pinned reference": {"page_size": 10, "entries": [{"key": "k", "ranges": [], "pinned": True}]},
        "compress on ref": {"entries": [{"key": "k", "ranges": [], "compress": True}]},
        "page_size 0": {"page_size": 0},
        "page_size float": {"page_size": 1.0},
        "null mirror": {"mirror": None},
        "null sources": {"sources": None},
        "both bytes ranges": {"entries": [{"key": "k", "bytes": "", "ranges": []}]},
        "neither": {"entries": [{"key": "k"}]},
        "mixed range": {"sources": [{"data": "00"}], "entries": [{"key": "k", "ranges": [{"data": "00", "source": 0}]}]},
        "uppercase hex": {"entries": [{"key": "k", "bytes": "FF"}]},
        "odd hex": {"entries": [{"key": "k", "bytes": "f"}]},
        "string flag": {"entries": [{"key": "k", "bytes": "", "compress": "yes"}]},
        "two kinds": {"sources": [{"url": "a", "data": "00"}]},
        "no kind": {"sources": [{}]},
        "missing key": {"entries": [{"bytes": "00"}]},
        "long key": {"entries": [{"key": "a" * 65536, "bytes": "00"}]},
    }
    for name, d in bad.items():
        r, out = write(d, "bad.vzip")
        check(f"writer rejects {name}", r.returncode != 0 and not os.path.exists(out), r.stderr)
        if os.path.exists(out):
            os.remove(out)
    r, out = write('{"entries": [], "entries": []}', "dupmember.vzip")
    check("writer rejects duplicate member", r.returncode != 0 and not os.path.exists(out))
    # accepted edge cases
    ok = {
        "unknown members": {"foo": None, "entries": [{"key": "k", "bytes": "", "bar": 1}]},
        "compress false on ref": {"entries": [{"key": "k", "ranges": [], "compress": False}]},
        "hidden pinned": {"page_size": 5, "entries": [{"key": "__vz__/x", "bytes": "00", "pinned": True}]},
        "negative mna": {"sources": [{"url": "http://h/x", "modified_not_after": -5}]},
        "fragment url": {"sources": [{"url": "#x"}]},
        "empty": {},
        "max key": {"entries": [{"key": "a" * 65535, "bytes": "00"}]},
        "max payload": {"entries": [{"key": "r", "ranges": [{"data": "00" * 65514}]}]},
    }
    for name, d in ok.items():
        r, out = write(d, "ok.vzip")
        check(f"writer accepts {name}", r.returncode == 0, r.stderr)
        if r.returncode == 0:
            u = subprocess.run(["unzip", "-tq", out], capture_output=True, text=True, errors="replace")
            if name != "max key":  # Info-ZIP warns about (and truncates) names this long
                check(f"unzip -t {name}", u.returncode == 0, u.stdout)
            rc, o = read(out, [{"op": "list", "prefix": ""}])
            check(f"read back {name}", rc == 0 and o["open"]["ok"], o)
        if os.path.exists(out):
            os.remove(out)


def test_many_entries_zip64():
    entries = [{"key": f"k{i:05d}", "bytes": ""} for i in range(65535)]
    for ps in [None, 4096]:
        r, out = write({"page_size": ps, "entries": entries}, f"many-{ps}.vzip")
        check(f"many entries write {ps}", r.returncode == 0, r.stderr)
        data = open(out, "rb").read()
        check(f"zip64 eocd present {ps}", data.find(struct.pack("<I", 0x06064B50)) > 0)
        res = results(out, [{"op": "classify", "key": "k65534"}, {"op": "list", "prefix": "k6553"},
                            {"op": "classify", "key": "k65535"}])
        check(f"many entries read {ps}", res[0].get("kind") == "bytes" and len(res[1]["keys"]) == 5
              and res[2].get("kind") == "missing", res)
        u = subprocess.run(["unzip", "-tq", out], capture_output=True, text=True, errors="replace")
        check(f"many entries unzip -t {ps}", u.returncode == 0, u.stdout[-300:])


def test_reader_errors():
    ok_src = pb_table([pb_source(data=b"abcdef")])
    # baseline
    p = save("base.vzip", build_zip([{"name": "a", "body": b"xyz"}], ok_src))
    res = results(p, [{"op": "get", "key": "a"}])
    check("python-built baseline", res[0].get("value") == H(b"xyz"), res)

    def open_err(name, data):
        p = save(name + ".vzip", data)
        rc, out = read(p, [{"op": "list", "prefix": ""}])
        check(f"archive error: {name}", rc == 0 and out["open"] == {**out["open"], "ok": False, "class": "archive"}
              and out["results"] == [], out)

    good = build_zip([{"name": "a", "body": b"xyz"}], ok_src)
    open_err("not a zip", b"hello")
    open_err("empty", b"")
    open_err("bad version", good.replace(b"vzip/0", b"vzip/1"))
    open_err("bad magic", good.replace(b"vzip/0", b"vzop/0"))
    open_err("bad source table", build_zip([], b"\x0b"))
    open_err("source no kind", build_zip([], pb_table([pb_source(size=1)])))
    open_err("empty url", build_zip([], pb_table([pb_source(url="")])))
    open_err("empty key", build_zip([], pb_table([pb_source(key="")])))
    open_err("pin on data", build_zip([], pb_table([pb_source(data=b"x", size=1)])))
    open_err("weak etag", build_zip([], pb_table([pb_source(url="a", etag='W/"x"')])))
    open_err("sources trailing bytes", make_trailing())
    open_err("index record in unpaged", build_zip([{"name": "__vz__/index", "body": b""}], ok_src))
    open_err("cd outside file", patch_eocd(good, cd_offset=len(good) + 5))
    open_err("cd bad record", patch_eocd(good, cd_size_delta=-1))
    open_err("zip64 without locator", patch_eocd(good, cd_offset=0xFFFFFFFF))
    # bad url at open is fine (checked at resolution)
    p = save("badurl.vzip", build_zip([], pb_table([pb_source(url="a b")])))
    rc, out = read(p, [])
    check("bad url not checked at open", out["open"]["ok"], out)

    # entry/body/payload/resolution errors
    entries = [
        {"name": "zb", "body": b"QQ", "lho": 0xFFFFFFFF, "extra": struct.pack("<HHQ", 1, 8, 0)},
        {"name": "m9", "body": b"x", "method": 9},
        {"name": "enc", "body": b"x", "flags": 0x801},
        {"name": "two", "body": b"", "extra": ref_extra(pb_range(data=b"a")) + ref_extra(pb_range(data=b"b"))},
        {"name": "badextra", "body": b"", "extra": b"\x01\x02\x03"},
        {"name": "ref8", "body": raw_deflate(b""), "method": 8, "usize": 0, "extra": ref_extra(pb_range(data=b"a"))},
        {"name": "z64", "body": b"", "lho": 0xFFFFFFFF},
        {"name": "z64twice", "body": b"", "extra": struct.pack("<HHQ", 1, 8, 0) * 2},
        {"name": "sizeff", "body": b"", "usize": 0xFFFFFFFF},
        {"name": "trail", "body": raw_deflate(b"hi") + b"\x00", "method": 8, "usize": 2},
        {"name": "wrongsize", "body": raw_deflate(b"hi"), "method": 8, "usize": 3},
        {"name": "storeddiff", "body": b"hi", "usize": 3},
        {"name": "outside", "body": b"hi", "lho": 10**6},
        {"name": "badpayload", "body": b"", "extra": ref_extra(b"\x0b")},
        {"name": "oob", "body": b"", "extra": ref_extra(pb_concat([pb_range(data=b"a"), pb_range(source=7)]), 0x7A77)},
        {"name": "litfields", "body": b"", "extra": ref_extra(pb_range(offset=1, data=b"a"))},
        {"name": "bigpayload", "body": b"", "extra": ref_extra(pb_range(data=b"a" * 65520))},
        {"name": "ok", "body": b"", "extra": ref_extra(pb_concat([pb_range(source=0, offset=1, length=2), pb_range(data=b"!"), pb_range(source=1, length=3)]), 0x7A77)},
        {"name": "past", "body": b"", "extra": ref_extra(pb_range(source=0, offset=4, length=5))},
        {"name": "z64ok", "body": b"", "extra": ref_extra(pb_range(data=b"Z")) + struct.pack("<HHQ", 1, 8, 7)},
        {"name": "keysrc", "body": b"", "extra": ref_extra(pb_range(source=2, offset=0, length=1))},
        {"name": "keyref", "body": b"", "extra": ref_extra(pb_range(source=3, offset=0, length=1))},
        {"name": "keymissing", "body": b"", "extra": ref_extra(pb_range(source=4, offset=0, length=1))},
        {"name": "keyformat", "body": b"", "extra": ref_extra(pb_range(source=5, offset=0, length=1))},
        {"name": "keybody", "body": b"", "extra": ref_extra(pb_range(source=6, offset=0, length=1))},
        {"name": "zerolen", "body": b"", "extra": ref_extra(pb_concat([pb_range(source=1, offset=99, length=0), pb_range(data=b"q")]), 0x7A77)},
        {"name": "garbage-body", "body": b"not the payload", "extra": ref_extra(pb_range(data=b"G"))},
    ]
    src = pb_table([pb_source(data=b"abcdef"), pb_source(url="nonexistent.bin"), pb_source(key="__vz__/h"),
                    pb_source(key="ok"), pb_source(key="gone"), pb_source(key="__vz__/sources"),
                    pb_source(key="trail"), ])
    entries.append({"name": "__vz__/h", "body": b"HHH"})
    p = save("errs.vzip", build_zip(entries, src))
    q = []
    expect = []

    def add(op, key, cls, extra=None, **kw):
        d = {"op": op, "key": key}
        d.update(kw)
        q.append(d)
        expect.append((op + " " + key + (" " + json.dumps(kw) if kw else ""), cls, extra))

    for k in ["m9", "enc", "two", "badextra", "ref8", "z64", "z64twice", "sizeff"]:
        add("classify", k, "entry")
        add("get", k, "entry")
        add("get_raw", k, "entry")
    for k in ["trail", "wrongsize", "storeddiff", "outside"]:
        add("classify", k, None, "bytes")
        add("get", k, "body")
        add("get", k, "body", range={"start": 0, "end": 1})
        add("get_raw", k, "body")
    for k in ["badpayload", "oob", "litfields", "bigpayload"]:
        add("classify", k, None, "reference")
        add("get", k, "payload")
        add("get", k, "payload", range={"start": 0, "end": 0})
        add("get_raw", k, None, "")
    add("get", "ok", "resolution")  # source 1 missing file
    add("get", "ok", None, H(b"bc!"), range={"start": 0, "end": 3})
    add("get", "ok", None, H(b"!"), range={"start": 2, "end": 3})
    add("get", "ok", "resolution", range={"start": 2, "end": 4})
    add("get", "past", "resolution")
    add("get", "past", None, "", range={"offset": 100})
    add("get", "z64ok", None, H(b"Z"))
    add("get", "zb", None, H(b"QQ"))
    add("get", "keysrc", None, H(b"H"))
    add("get", "keyref", "resolution")
    add("get", "keymissing", "resolution")
    add("get", "keyformat", "resolution")
    add("get", "keybody", "resolution")
    add("get", "zerolen", None, H(b"q"))
    add("get", "garbage-body", None, H(b"G"))
    add("get_raw", "garbage-body", None, H(b"not the payload"))
    add("get_raw", "__vz__/sources", None, H(src))
    add("get_raw", "__vz__/index", None, None)
    q.append({"op": "list", "prefix": ""})
    rs = results(p, q)
    for (name, cls, val), r in zip(expect, rs):
        if cls:
            check(f"error {name}", r.get("ok") is False and r.get("class") == cls, r)
        else:
            got = r.get("kind", r.get("value"))
            check(f"value {name}", r.get("ok") and got == val, r)
    lst = rs[-1].get("keys")
    check("list includes entry-error keys", lst is not None and "m9" in lst and "__vz__/h" not in lst, lst)


def make_trailing():
    pb = pb_table([])
    out = build_zip([], pb)
    # rebuild with trailing byte in the sources body: patch the comment size +1 and insert a byte
    sname = b"__vz__/sources"
    body = raw_deflate(pb) + b"\x00"
    lh = struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0x800, 8, 0, 0x21, 0, len(body), 0, len(sname), 0) + sname + body
    cd = struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0x800, 8, 0, 0x21, 0, len(body), 0, len(sname), 0, 0, 0, 0, 0, 0) + sname
    comment = b"vzip/0" + struct.pack("<QQ", 30 + len(sname), len(body))
    return lh + cd + struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 1, 1, len(cd), len(lh), len(comment)) + comment


def patch_eocd(data, cd_offset=None, cd_size_delta=0):
    i = len(data) - 44
    assert data[i:i + 4] == b"PK\x05\x06"
    b = bytearray(data)
    size, off = struct.unpack_from("<II", b, i + 12)
    if cd_offset is not None:
        off = cd_offset
    struct.pack_into("<II", b, i + 12, size + cd_size_delta, off)
    return bytes(b)


def test_paged_lookup():
    entries = [{"key": f"a/{i}", "bytes": H(bytes([i]))} for i in range(10)] + [{"key": "b/0", "bytes": "ff"}]
    r, out = write({"page_size": 100, "entries": entries}, "paged.vzip")
    check("paged write", r.returncode == 0, r.stderr)
    res = results(out, [{"op": "get", "key": "a/7"}, {"op": "list", "prefix": "a/"}, {"op": "classify", "key": "0"},
                        {"op": "list", "prefix": "b"}])
    check("paged get", res[0].get("value") == "07", res)
    check("paged list", res[1].get("keys") == [f"a/{i}" for i in range(10)], res)
    check("paged before first", res[2].get("kind") == "missing", res)
    check("paged list b", res[3].get("keys") == ["b/0"], res)
    # Corrupt one page's record signature -> entry error for keys routed there, list fails
    data = bytearray(open(out, "rb").read())
    i = data.find(b"PK\x01\x02")
    data[i + 3] = 0
    p = save("paged-bad.vzip", bytes(data))
    res = results(p, [{"op": "classify", "key": "a/0"}, {"op": "classify", "key": "a/00"},
                      {"op": "list", "prefix": "a/"}, {"op": "classify", "key": "b/0"}, {"op": "list", "prefix": "b/"}])
    check("bad page classify", res[0].get("class") == "entry", res[0])
    check("bad page absent key is entry error", res[1].get("class") == "entry", res[1])
    check("bad page list", res[2].get("class") == "entry", res[2])
    check("other page fine", res[3].get("kind") == "bytes", res[3])
    check("list not touching bad page", res[4].get("keys") == ["b/0"], res[4])


def test_queries_file():
    r, out = write({"entries": [{"key": "k", "bytes": "00"}]}, "q.vzip")
    for name, q in {
        "unknown op": [{"op": "frob", "key": "k"}],
        "missing key": [{"op": "get"}],
        "missing prefix": [{"op": "list"}],
        "range on classify": [{"op": "classify", "key": "k", "range": {"offset": 1}}],
        "null range": [{"op": "get", "key": "k", "range": None}],
        "partial range": [{"op": "get", "key": "k", "range": {"start": 1}}],
        "two forms": [{"op": "get", "key": "k", "range": {"offset": 1, "suffix": 1}}],
        "negative": [{"op": "get", "key": "k", "range": {"offset": -1}}],
        "float": [{"op": "get", "key": "k", "range": {"offset": 1.5}}],
        "dup member": '[{"op": "get", "key": "k", "key": "j"}]',
        "not array": '{}',
    }.items():
        rc, o = read(out, q)
        check(f"invalid queries: {name}", rc != 0, o)
    rc, o = read(path("does-not-exist.vzip"), [])
    check("nonexistent archive is archive error", rc == 0 and o["open"]["class"] == "archive", o)


# ------------------------------------------------------------ HTTP

OBJ = bytes(range(256)) * 8  # 2048 bytes
LM = "Sun, 06 Nov 1994 08:49:37 GMT"
LM_TS = 784111777
LOG = []


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        LOG.append(("HEAD", self.path, dict(self.headers)))
        self.send_response(500)
        self.end_headers()

    def do_GET(self):
        LOG.append(("GET", self.path, dict(self.headers)))
        p = self.path
        rng = self.headers.get("Range")
        a, z = map(int, rng.split("=")[1].split("-"))
        if p.startswith("/redir/"):
            n = int(p.split("/")[2])
            self.send_response(302)
            self.send_header("Location", f"/redir/{n - 1}" if n > 1 else "/plain")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p == "/redir-noloc":
            self.send_response(301)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p == "/redir-file":
            self.send_response(307)
            self.send_header("Location", "file:///etc/passwd")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        im = self.headers.get("If-Match")
        ius = self.headers.get("If-Unmodified-Since")
        if p.startswith("/precond"):
            if im is not None and im != '"v1"':
                self.send_response(412)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if ius is not None:
                t = email.utils.parsedate_to_datetime(ius).timestamp()
                if LM_TS > t:
                    self.send_response(412)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
        if p == "/full":
            self.send_response(200)
            self.send_header("Content-Length", str(len(OBJ)))
            self.end_headers()
            self.wfile.write(OBJ)
            return
        if p == "/chunked":
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{z}/{len(OBJ)}")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            body = OBJ[a:z + 1]
            for i in range(0, len(body), 3):
                c = body[i:i + 3]
                self.wfile.write(b"%x\r\n" % len(c) + c + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        if p == "/short":
            self.send_response(416)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = OBJ[a:z + 1]
        self.send_response(206)
        cr = f"bytes {a}-{z}/{len(OBJ)}"
        if p == "/star":
            cr = f"bytes {a}-{z}/*"
        if p == "/wrongrange":
            cr = f"bytes {a + 1}-{z + 1}/{len(OBJ)}"
            body = OBJ[a + 1:z + 2]
        if p != "/nocr":
            self.send_header("Content-Range", cr)
        if p == "/twocr":
            self.send_header("Content-Range", cr)
        if p == "/gzip":
            self.send_header("Content-Encoding", "gzip")
        if p == "/identity":
            self.send_header("Content-Encoding", " Identity ")
        if p == "/identity2":
            self.send_header("Content-Encoding", "identity, identity")
        if p.startswith("/precond") or p == "/meta":
            self.send_header("ETag", '"v1"')
            self.send_header("Last-Modified", LM)
        if p == "/badlm":
            self.send_header("Last-Modified", "Sunday, 06-Nov-94 08:49:37 GMT")
        if p == "/wrongday":
            self.send_header("Last-Modified", "Mon, 06 Nov 1994 08:49:37 GMT")
        if p == "/weak":
            self.send_header("ETag", 'W/"v1"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_http():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    cases = [
        # (url, pins, expected class or None)
        ("/plain", {}, None),
        ("/full", {}, None),
        ("/full", {"size": 2048}, None),
        ("/full", {"size": 2047}, "resolution"),
        ("/chunked", {}, None),
        ("/plain", {"size": 2048}, None),
        ("/plain", {"size": 1}, "resolution"),
        ("/star", {}, None),
        ("/star", {"size": 2048}, "resolution"),
        ("/redir/5", {}, None),
        ("/redir/6", {}, "resolution"),
        ("/redir-noloc", {}, "resolution"),
        ("/redir-file", {}, "resolution"),
        ("/short", {}, "resolution"),
        ("/nocr", {}, "resolution"),
        ("/twocr", {}, "resolution"),
        ("/wrongrange", {}, "resolution"),
        ("/gzip", {}, "resolution"),
        ("/identity", {}, None),
        ("/identity2", {}, "resolution"),
        ("/precond", {"etag": '"v1"'}, None),
        ("/precond", {"etag": '"v2"'}, "resolution"),
        ("/precond", {"modified_not_after": LM_TS}, None),
        ("/precond", {"modified_not_after": LM_TS - 1}, "resolution"),
        ("/meta", {"etag": '"v2"'}, "resolution"),       # server ignores If-Match; reader checks ETag
        ("/meta", {"modified_not_after": LM_TS - 1}, "resolution"),
        ("/meta", {"etag": '"v1"', "modified_not_after": LM_TS, "size": 2048}, None),
        ("/plain", {"etag": '"v1"'}, "resolution"),        # no ETag header
        ("/badlm", {"modified_not_after": LM_TS}, "resolution"),
        ("/wrongday", {"modified_not_after": LM_TS}, "resolution"),
        ("/weak", {"etag": '"v1"'}, "resolution"),
        ("/plain", {"modified_not_after": -62135596801}, "resolution"),  # year 0 cannot be sent
    ]
    sources = [dict({"url": base + u}, **pins) for u, pins, _ in cases]
    sources.append({"url": "http://user@127.0.0.1:%d/plain" % port})
    sources.append({"url": "http:///plain"})
    entries = [{"key": f"e{i}", "ranges": [{"source": i, "offset": 100, "length": 20}]} for i in range(len(sources))]
    r, out = write({"sources": sources, "entries": entries}, "http.vzip")
    check("http write", r.returncode == 0, r.stderr)
    LOG.clear()
    res = results(out, [{"op": "get", "key": f"e{i}"} for i in range(len(sources))])
    exp_cls = [c for _, _, c in cases] + ["resolution", "resolution"]
    for i, (rr, c) in enumerate(zip(res, exp_cls)):
        name = f"http {sources[i]}"
        if c is None:
            check(name, rr.get("value") == H(OBJ[100:120]), rr)
        else:
            check(name, rr.get("class") == c, rr)
    check("http no HEAD", all(m == "GET" for m, _, _ in LOG))
    check("http range header", all(h.get("Range") == "bytes=100-119" for _, _, h in LOG), LOG[:2])
    check("http accept-encoding identity", all(h.get("Accept-Encoding") == "identity" for _, _, h in LOG))
    redirect_hops = [h for m, p, h in LOG if p.startswith("/redir/")]
    check("redirect resends headers", all(h.get("Range") for h in redirect_hops))
    ims = [h.get("If-Unmodified-Since") for _, p, h in LOG if p == "/precond" and h.get("If-Unmodified-Since")]
    check("If-Unmodified-Since format", LM in ims, ims)
    # window inside a reference: only overlapping ranges fetched
    LOG.clear()
    r, out = write({"sources": [{"url": base + "/plain"}, {"url": base + "/short"}],
                    "entries": [{"key": "w", "ranges": [{"source": 0, "offset": 0, "length": 10},
                                                        {"source": 1, "offset": 0, "length": 10}]}]}, "http2.vzip")
    res = results(out, [{"op": "get", "key": "w", "range": {"start": 2, "end": 5}},
                        {"op": "get", "key": "w"}])
    check("http window", res[0].get("value") == H(OBJ[2:5]), res[0])
    check("http window second range fails", res[1].get("class") == "resolution", res[1])
    check("http single request for window", [p for _, p, _ in LOG][:1] == ["/plain"] and
          LOG[0][2].get("Range") == "bytes=2-4", LOG)
    srv.shutdown()


def test_file_pins():
    p = path("pinned.bin")
    with open(p, "wb") as f:
        f.write(b"0123456789")
    os.utime(p, (1000000000, 1000000000))
    srcs = [{"url": "pinned.bin", "size": 10, "modified_not_after": 1000000000},
            {"url": "pinned.bin", "size": 11},
            {"url": "pinned.bin", "modified_not_after": 999999999},
            {"url": "pinned.bin", "etag": '"x"'},
            {"url": "pinned.bin?"},
            {"url": "%2E%2E/x"},
            {"url": "a%2Fb"},
            {"url": "file://otherhost/x"},
            {"url": "ftp://h/x"},
            {"url": "file://localhost" + p},
            {"url": "pinned.bin#frag"},
            ]
    entries = [{"key": f"f{i}", "ranges": [{"source": i, "offset": 2, "length": 3}]} for i in range(len(srcs))]
    r, out = write({"sources": srcs, "entries": entries}, "fpins.vzip")
    check("file pins write", r.returncode == 0, r.stderr)
    res = results(out, [{"op": "get", "key": f"f{i}"} for i in range(len(srcs))])
    exp = [H(b"234"), "resolution", "resolution", "resolution", "resolution", "resolution", "resolution",
           "resolution", "resolution", H(b"234"), H(b"234")]
    for i, (rr, e) in enumerate(zip(res, exp)):
        if e == "resolution":
            check(f"file source {srcs[i]}", rr.get("class") == "resolution", rr)
        else:
            check(f"file source {srcs[i]}", rr.get("value") == e, rr)
    # '#x' resolves to the archive itself
    r, out = write({"sources": [{"url": "#x"}], "entries": [{"key": "self", "ranges": [{"source": 0, "offset": 0, "length": 4}]}]}, "self.vzip")
    res = results(out, [{"op": "get", "key": "self"}])
    check("fragment-only url reads archive", res[0].get("value") == H(b"PK\x03\x04"), res)


def main():
    subprocess.run(["cargo", "build", "--release", "-q"], cwd=ROOT, check=True)
    test_roundtrip()
    test_writer_rejects()
    test_reader_errors()
    test_paged_lookup()
    test_queries_file()
    test_file_pins()
    test_http()
    test_many_entries_zip64()
    print(f"{PASSES} passed, {len(FAILS)} failed")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
