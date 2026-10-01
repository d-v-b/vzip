"""Writer tests: round trips through our reader, unzip -t and zipfile; writer rejections; CLI."""

import itertools
import json
import os
import random
import struct
import subprocess
import tempfile
import unittest
import zipfile

from helpers import ROOT
from vzip_impl import proto
from vzip_impl.reader import Archive, Request
from vzip_impl.writer import WEntry, WriteInputError, build_archive, parse_description

CLI = os.path.join(ROOT, "vzip")


def unzip_ok(path):
    r = subprocess.run(["unzip", "-t", path], capture_output=True, text=True, errors="replace")
    return r.returncode == 0, r.stdout + r.stderr


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.ext = os.path.join(self.d, "ext dir", "é.bin")
        os.makedirs(os.path.dirname(self.ext))
        self.ext_data = bytes(range(256)) * 4
        with open(self.ext, "wb") as f:
            f.write(self.ext_data)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, page_size, mirror, compress):
        rnd = random.Random(42)
        hdr = b"HDR!" * 3
        sources = [
            proto.Source("url", "ext%20dir/%C3%A9.bin", size=len(self.ext_data)),
            proto.Source("key", "__vz__/hdr"),
            proto.Source("data", b"shared-data"),
            proto.Source("key", "plain/a"),
        ]
        entries = [WEntry("__vz__/hdr", data=hdr, compress=compress, pinned=page_size is not None)]
        expect = {}
        for i in range(40):
            k = f"plain/{chr(97 + i % 26)}{i // 26 or ''}"
            if i == 0:
                k = "plain/a"
            data = bytes(rnd.randrange(256) for _ in range(rnd.randrange(0, 300)))
            entries.append(WEntry(k, data=data, compress=compress and i % 2 == 0,
                                  pinned=page_size is not None and i % 7 == 3))
            expect[k] = data
        a_data = expect["plain/a"]
        refs = {
            "ref/one": [proto.Range(0, 5, 10)],
            "ref/empty": [],
            "ref/lit": [proto.Range(data=b"literal")],
            "ref/emptylit": [proto.Range(data=b"")],
            "ref/mix": [proto.Range(1, 2, 4), proto.Range(data=b"--"), proto.Range(2, 0, 6),
                        proto.Range(0, 1000, 24), proto.Range(3, 0, min(3, len(a_data)))],
            "ref/zero": [proto.Range(0, 0, 0)],
            "😀": [proto.Range(data=b"emoji")],
            "～": [proto.Range(data=b"tilde")],
            "﻿bom": [proto.Range(data=b"bom")],
            "a/../b": [proto.Range(data=b"dots")],
            "x/": [proto.Range(data=b"slash")],
        }
        expect["ref/one"] = self.ext_data[5:15]
        expect["ref/empty"] = b""
        expect["ref/lit"] = b"literal"
        expect["ref/emptylit"] = b""
        expect["ref/mix"] = hdr[2:6] + b"--" + b"shared" + self.ext_data[1000:1024] + a_data[:3]
        expect["ref/zero"] = b""
        expect["😀"] = b"emoji"
        expect["～"] = b"tilde"
        expect["﻿bom"] = b"bom"
        expect["a/../b"] = b"dots"
        expect["x/"] = b"slash"
        for k, r in refs.items():
            entries.append(WEntry(k, ranges=r))
        path = os.path.join(self.d, f"rt-{page_size}-{mirror}-{compress}.vzip")
        with open(path, "wb") as f:
            f.write(build_archive(entries, sources, page_size, mirror))
        return path, expect, set(refs)

    def test_round_trip(self):
        for page_size, mirror, compress in itertools.product([None, 1, 100, 1000, 10 ** 9],
                                                             [True, False], [True, False]):
            with self.subTest(page_size=page_size, mirror=mirror, compress=compress):
                path, expect, refs = self.build(page_size, mirror, compress)
                ok, out = unzip_ok(path)
                self.assertTrue(ok, out)
                with zipfile.ZipFile(path) as zf:
                    names = set(zf.namelist())
                    self.assertIn("__vz__/sources", names)
                    self.assertEqual("__vz__/index" in names, page_size is not None)
                    for k in expect:
                        body = zf.read(k)
                        if k not in refs:
                            self.assertEqual(body, expect[k])
                        elif not mirror:
                            self.assertEqual(body, b"")
                with open(path, "rb") as f:
                    tail = f.read()[-60:]
                self.assertEqual(len(tail) - tail.rfind(b"vzip/0"), 38 if page_size else 22)
                with Archive(path) as ar:
                    keys = sorted(expect, key=lambda k: k.encode())
                    self.assertEqual(ar.list(""), keys)
                    self.assertEqual(ar.list("plain/"), [k for k in keys if k.startswith("plain/")])
                    self.assertEqual(ar.list("ref/m"), ["ref/mix"])
                    self.assertEqual(ar.list("zz"), [])
                    # UTF-8 order: 😀 (F0..) after ～ (EF..)
                    self.assertLess(keys.index("～"), keys.index("😀"))
                    for k, v in expect.items():
                        self.assertEqual(ar.classify(k), "reference" if k in refs else "bytes")
                        self.assertEqual(ar.get(k), v, k)
                        n = len(v)
                        for req, want in [
                            (Request("range", 1, 5), v[1:5]),
                            (Request("range", 3, 3), b""),
                            (Request("range", n + 5, n + 9), b""),
                            (Request("offset", 2), v[2:]),
                            (Request("offset", n + 10), b""),
                            (Request("suffix", 3), v[max(n - 3, 0):]),
                            (Request("suffix", n + 10), v),
                            (Request("suffix", 0), b""),
                        ]:
                            self.assertEqual(ar.get(k, req), want, (k, req))
                        raw = ar.raw(k)
                        if k in refs:
                            self.assertTrue(raw == b"" if not mirror else len(raw) >= 0)
                        else:
                            self.assertEqual(raw, v)
                    self.assertEqual(ar.classify("__vz__/hdr"), "missing")
                    self.assertIsNone(ar.get("__vz__/hdr"))
                    self.assertEqual(ar.raw("__vz__/hdr"), b"HDR!" * 3)
                    self.assertIsNotNone(ar.raw("__vz__/sources"))
                    self.assertEqual(ar.raw("__vz__/index") is not None, page_size is not None)
                    self.assertEqual(ar.classify("nope"), "missing")
                    self.assertIsNone(ar.get("nope"))
                    self.assertIsNone(ar.get("\u0000"))
                    self.assertIsNone(ar.raw("nope"))

    def test_many_entries_zip64_eocd(self):
        n = 0xFFFF
        entries = [WEntry(f"k{i:06d}", data=b"") for i in range(n)]
        path = os.path.join(self.d, "many.vzip")
        data = build_archive(entries, [], 4096)
        with open(path, "wb") as f:
            f.write(data)
        # zip64 end record + locator precede the EOCD
        eocd = len(data) - 60
        self.assertEqual(data[eocd - 20:eocd - 16], b"PK\x06\x07")
        self.assertEqual(struct.unpack_from("<HH", data, eocd + 8), (0xFFFF, 0xFFFF))
        ok, out = unzip_ok(path)
        self.assertTrue(ok, out[-500:])
        with Archive(path) as ar:
            self.assertEqual(len(ar.list("")), n)
            self.assertEqual(ar.get("k065534"), b"")
            self.assertEqual(ar.classify("k000000"), "bytes")
        # just below the threshold: no zip64
        data = build_archive(entries[:0xFFFD], [], None)
        self.assertNotIn(b"PK\x06\x07", data[-100:])


class TestWriterRejects(unittest.TestCase):
    def rej(self, entries, sources=(), page_size=None):
        with self.assertRaises(WriteInputError):
            build_archive(list(entries), list(sources), page_size)

    def test_empty_key(self):
        self.rej([WEntry("", data=b"")])

    def test_invalid_utf8_key(self):
        self.rej([WEntry("\ud800", data=b"")])

    def test_duplicate_key(self):
        self.rej([WEntry("a", data=b""), WEntry("a", data=b"")])

    def test_format_key_sources(self):
        self.rej([WEntry("__vz__/sources", data=b"")])

    def test_format_key_index(self):
        self.rej([WEntry("__vz__/index", data=b"")])

    def test_source_out_of_range(self):
        self.rej([WEntry("a", ranges=[proto.Range(0, 0, 1)])])

    def test_source_out_of_range_zero_length(self):
        self.rej([WEntry("a", ranges=[proto.Range(1, 0, 0)])], [proto.Source("data", b"x")])

    def test_empty_url(self):
        self.rej([], [proto.Source("url", "")])

    def test_bad_url(self):
        self.rej([], [proto.Source("url", "a b")])

    def test_key_source_absent(self):
        self.rej([], [proto.Source("key", "nope")])

    def test_key_source_reference(self):
        self.rej([WEntry("r", ranges=[])], [proto.Source("key", "r")])

    def test_key_source_format(self):
        self.rej([], [proto.Source("key", "__vz__/sources")])

    def test_pin_on_key(self):
        self.rej([WEntry("a", data=b"")], [proto.Source("key", "a", size=1)])

    def test_pin_on_data(self):
        self.rej([], [proto.Source("data", b"", modified_not_after=1)])

    def test_weak_etag(self):
        self.rej([], [proto.Source("url", "x", etag='W/"a"')])

    def test_unquoted_etag(self):
        self.rej([], [proto.Source("url", "x", etag="abc")])

    def test_payload_too_large(self):
        self.rej([WEntry("a", ranges=[proto.Range(data=b"x" * 65520)])])

    def test_offset_plus_length_overflow(self):
        self.rej([WEntry("a", ranges=[proto.Range(0, 1 << 63, 1 << 63)])], [proto.Source("data", b"")])

    def test_total_size_overflow(self):
        r = proto.Range(0, 0, 1 << 63)
        self.rej([WEntry("a", ranges=[r, r])], [proto.Source("data", b"")])

    def test_key_too_long(self):
        self.rej([WEntry("k" * 65536, data=b"")])

    def test_pinned_reference(self):
        self.rej([WEntry("a", ranges=[], pinned=True)], page_size=10)

    def test_pinned_without_index(self):
        self.rej([WEntry("a", data=b"", pinned=True)])

    def test_compressed_reference(self):
        self.rej([WEntry("a", ranges=[], compress=True)])

    def test_payload_limit_boundary_ok(self):
        # 65519 bytes is allowed: 2-byte tag+len prefix for field 5 with 65516 data -> 1+3+65515
        r = proto.Range(data=b"x" * (65519 - 4))
        self.assertEqual(len(proto.encode_range(r)), 65519)
        build_archive([WEntry("a", ranges=[r])], [], None)


class TestDescription(unittest.TestCase):
    def test_valid(self):
        e, s, ps, m = parse_description({"x": None, "entries": [{"key": "a", "bytes": "00ff", "compress": True},
                                                                 {"key": "b", "ranges": [{}], "compress": False}],
                                         "sources": [{"url": "u", "size": 1, "etag": '"e"',
                                                      "modified_not_after": -3}]})
        self.assertEqual((ps, m), (None, True))
        self.assertEqual(e[1].ranges, [proto.Range()])
        self.assertEqual(s[0].modified_not_after, -3)

    def bad(self, d):
        with self.assertRaises(WriteInputError):
            e, s, ps, m = parse_description(d)
            build_archive(e, s, ps, m)

    def test_float(self):
        self.bad({"page_size": 1.0})

    def test_page_size_zero(self):
        self.bad({"page_size": 0})

    def test_null_mirror(self):
        self.bad({"mirror": None})

    def test_uppercase_hex(self):
        self.bad({"entries": [{"key": "a", "bytes": "FF"}]})

    def test_odd_hex(self):
        self.bad({"entries": [{"key": "a", "bytes": "f"}]})

    def test_both_bytes_and_ranges(self):
        self.bad({"entries": [{"key": "a", "bytes": "", "ranges": []}]})

    def test_mixed_range(self):
        self.bad({"sources": [{"data": ""}], "entries": [{"key": "a", "ranges": [{"data": "", "source": 0}]}]})

    def test_empty_range_no_sources(self):
        self.bad({"entries": [{"key": "a", "ranges": [{}]}]})

    def test_two_source_kinds(self):
        self.bad({"sources": [{"url": "a", "data": ""}]})

    def test_bool_as_int(self):
        self.bad({"sources": [{"url": "a", "size": True}]})

    def test_compress_on_reference(self):
        self.bad({"entries": [{"key": "a", "ranges": [], "compress": True}]})

    def test_pinned_without_page_size(self):
        self.bad({"entries": [{"key": "a", "bytes": "", "pinned": True}]})


class TestCLI(unittest.TestCase):
    def test_cli_write_read(self):
        with tempfile.TemporaryDirectory() as d:
            desc = {"page_size": 50, "sources": [{"data": "41424344"}],
                    "entries": [{"key": "z.json", "bytes": "7b7d", "pinned": True},
                                {"key": "c/0", "ranges": [{"source": 0, "offset": 1, "length": 2}, {"data": "00"}]}]}
            dp, out, qp = (os.path.join(d, x) for x in ("d.json", "o.vzip", "q.json"))
            json.dump(desc, open(dp, "w"))
            r = subprocess.run([CLI, "write", dp, out], capture_output=True, cwd="/")
            self.assertEqual(r.returncode, 0, r.stderr)
            json.dump([{"op": "get", "key": "c/0"}, {"op": "list", "prefix": ""},
                       {"op": "get", "key": "c/0", "range": {"start": 2, "end": 1}},
                       {"op": "classify", "key": "z.json"}], open(qp, "w"))
            r = subprocess.run([CLI, "read", out, qp], capture_output=True, cwd="/")
            self.assertEqual(r.returncode, 0, r.stderr)
            res = json.loads(r.stdout)
            self.assertEqual(res["results"], [
                {"ok": True, "value": "424300"}, {"ok": True, "keys": ["c/0", "z.json"]},
                {"ok": False, "class": "request", "error": res["results"][2]["error"]},
                {"ok": True, "kind": "bytes"}])
            # invalid description: non-zero exit and no file
            bad = os.path.join(d, "bad.vzip")
            json.dump({"entries": [{"key": ""}]}, open(dp, "w"))
            r = subprocess.run([CLI, "write", dp, bad], capture_output=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertFalse(os.path.exists(bad))
            # open failure is reported as JSON with exit 0
            r = subprocess.run([CLI, "read", dp, qp], capture_output=True)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(json.loads(r.stdout)["open"]["class"], "archive")
            # invalid queries: non-zero
            json.dump([{"op": "get", "key": "a", "range": {"offset": 1, "suffix": 2}}], open(qp, "w"))
            r = subprocess.run([CLI, "read", out, qp], capture_output=True)
            self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
