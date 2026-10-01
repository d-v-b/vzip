import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import craft  # noqa: E402
from craft import E, ref_extra, range_payload  # noqa: E402
from vzip_impl import pb, reader, writer  # noqa: E402
from vzip_impl.errors import VzError  # noqa: E402

VZIP = os.path.join(ROOT, "vzip")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vzt")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.dir, name)

    def put(self, name, data):
        p = self.path(name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def cli(self, *args):
        return subprocess.run([VZIP, *args], capture_output=True, cwd=self.dir)

    def write_desc(self, desc, out="out.vzip"):
        dp = self.put("desc.json", json.dumps(desc).encode())
        r = self.cli("write", dp, self.path(out))
        return r, self.path(out)

    def read_q(self, archive, queries):
        qp = self.put("q.json", json.dumps(queries).encode())
        r = self.cli("read", archive, qp)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def open(self, data):
        return reader.Archive(self.put("t.vzip", data))

    def assertErr(self, cls, fn, *a):
        with self.assertRaises(VzError) as cm:
            fn(*a)
        self.assertEqual(cm.exception.cls, cls, str(cm.exception))


class TestRoundTrip(Base):
    def desc(self, page_size=None, mirror=True):
        return {
            "page_size": page_size, "mirror": mirror,
            "sources": [{"url": "data/a%20b.bin", "size": 26}, {"key": "__vz__/hdr"}, {"data": "48445221"},
                        {"url": "missing.bin"}],
            "entries": [
                {"key": "x/zarr.json", "bytes": "7b7d", "pinned": page_size is not None},
                {"key": "__vz__/hdr", "bytes": "000102030405060708090a", "compress": True,
                 "pinned": page_size is not None},
                {"key": "x/c/0", "ranges": [{"source": 0, "offset": 10, "length": 4}]},
                {"key": "x/c/1", "ranges": [{"source": 2, "offset": 0, "length": 3}, {"data": "00ff"},
                                            {"source": 1, "offset": 2, "length": 3}]},
                {"key": "x/c/2", "ranges": []},
                {"key": "x/c/3", "ranges": [{"source": 3, "offset": 0, "length": 5}, {"data": "aa"}]},
                {"key": "y/big", "bytes": ("00010203" * 3000), "compress": True},
                {"key": "\U0001F600", "bytes": "01"},
                {"key": "～", "bytes": "02"},
                {"key": "﻿bom", "bytes": "03"},
                {"key": "a/../b", "bytes": ""},
                {"key": "x/", "ranges": [{}]} if False else {"key": "x/", "bytes": "04"},
                {"key": "z/empty", "ranges": [{"data": ""}]},
            ],
        }

    def test_round_trip(self):
        os.makedirs(self.path("data"))
        self.put("data/a b.bin", b"abcdefghijklmnopqrstuvwxyz")
        for page_size in (None, 1, 100, 300, 100000):
            for mirror in (True, False):
                with self.subTest(page_size=page_size, mirror=mirror):
                    out = "o_%s_%s.vzip" % (page_size, mirror)
                    r, p = self.write_desc(self.desc(page_size, mirror), out)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    t = subprocess.run(["unzip", "-t", p], capture_output=True)
                    self.assertEqual(t.returncode, 0, t.stdout + t.stderr)
                    with zipfile.ZipFile(p) as zf:
                        self.assertEqual(zf.read("x/zarr.json"), b"{}")
                        self.assertEqual(zf.read("y/big"), bytes.fromhex("00010203" * 3000))
                        if mirror:
                            self.assertEqual(zf.read("x/c/0"), pb.encode_range(0, 10, 4))
                        else:
                            self.assertEqual(zf.read("x/c/0"), b"")
                    res = self.read_q(p, [
                        {"op": "list", "prefix": ""},
                        {"op": "list", "prefix": "x/"},
                        {"op": "list", "prefix": "x/c/"},
                        {"op": "classify", "key": "x/c/0"},
                        {"op": "classify", "key": "x/zarr.json"},
                        {"op": "classify", "key": "__vz__/hdr"},
                        {"op": "classify", "key": "nope"},
                        {"op": "get", "key": "x/c/0"},
                        {"op": "get", "key": "x/c/1"},
                        {"op": "get", "key": "x/c/1", "range": {"start": 2, "end": 6}},
                        {"op": "get", "key": "x/c/1", "range": {"offset": 5}},
                        {"op": "get", "key": "x/c/1", "range": {"suffix": 100}},
                        {"op": "get", "key": "x/c/1", "range": {"start": 50, "end": 60}},
                        {"op": "get", "key": "x/c/2"},
                        {"op": "get", "key": "x/c/3"},
                        {"op": "get", "key": "x/c/3", "range": {"offset": 5}},
                        {"op": "get", "key": "__vz__/hdr"},
                        {"op": "get_raw", "key": "__vz__/hdr"},
                        {"op": "get", "key": "y/big", "range": {"start": 4, "end": 8}},
                        {"op": "get", "key": "\U0001F600"},
                        {"op": "get_raw", "key": "__vz__/index"},
                        {"op": "get", "key": "x/c/0", "range": {"start": 3, "end": 1}},
                        {"op": "get", "key": "nope", "range": {"start": 3, "end": 1}},
                        {"op": "get", "key": "z/empty"},
                        {"op": "get", "key": "a/../b"},
                    ])
                    self.assertEqual(res["open"], {"ok": True})
                    R = res["results"]
                    self.assertEqual(R[0]["keys"], ["a/../b", "x/", "x/c/0", "x/c/1", "x/c/2", "x/c/3",
                                                    "x/zarr.json", "y/big", "z/empty", "\ufeffbom",
                                                    "\uFF5E", "\U0001F600"])
                    self.assertEqual(R[1]["keys"], ["x/", "x/c/0", "x/c/1", "x/c/2", "x/c/3", "x/zarr.json"])
                    self.assertEqual(R[2]["keys"], ["x/c/0", "x/c/1", "x/c/2", "x/c/3"])
                    self.assertEqual([r["kind"] for r in R[3:7]], ["reference", "bytes", "missing", "missing"])
                    self.assertEqual(R[7]["value"], b"klmn".hex())
                    self.assertEqual(R[8]["value"], "48445200ff020304")
                    self.assertEqual(R[9]["value"], "5200ff02")
                    self.assertEqual(R[10]["value"], "020304")
                    self.assertEqual(R[11]["value"], "48445200ff020304")
                    self.assertEqual(R[12]["value"], "")
                    self.assertEqual(R[13]["value"], "")
                    self.assertEqual(R[14]["class"], "resolution")
                    self.assertEqual(R[15]["value"], "aa")
                    self.assertIsNone(R[16]["value"])
                    self.assertEqual(R[17]["value"], "000102030405060708090a")
                    self.assertEqual(R[18]["value"], "00010203")
                    self.assertEqual(R[19]["value"], "01")
                    if page_size is None:
                        self.assertIsNone(R[20]["value"])
                    else:
                        self.assertIsNotNone(R[20]["value"])
                    self.assertEqual(R[21]["class"], "request")
                    self.assertEqual(R[22]["class"], "request")
                    self.assertEqual(R[23]["value"], "")
                    self.assertEqual(R[24]["value"], "")
                    # paged structure: pinned entries and index after sources
                    if page_size is not None:
                        ar = reader.Archive(p)
                        self.assertIn(b"x/zarr.json", ar.pinned)
                        self.assertTrue(len(ar.pages) >= 1)
                        if page_size == 1:
                            self.assertEqual(len(ar.pages), 13)
                        ar.close()

    def test_canonical_payloads(self):
        r, p = self.write_desc({"sources": [{"data": "00"}], "entries": [
            {"key": "a", "ranges": [{}]},
            {"key": "b", "ranges": [{}, {"data": ""}]},
            {"key": "c", "ranges": [{"source": 0, "offset": 1, "length": 0}]},
        ]})
        self.assertEqual(r.returncode, 0, r.stderr)
        with zipfile.ZipFile(p) as zf:
            infos = {i.filename: i for i in zf.infolist()}
            self.assertEqual(infos["a"].extra, bytes.fromhex("767a0000"))
            self.assertEqual(infos["b"].extra, bytes.fromhex("777a06000a000a022a00"))
            self.assertEqual(infos["c"].extra, bytes.fromhex("767a02001801"))
            self.assertEqual(infos["a"].flag_bits & 0x800, 0x800)
        data = open(p, "rb").read()
        self.assertEqual(data[-22:-16], b"vzip/0")

    def test_file_pins(self):
        p_data = self.put("d.bin", b"0123456789")
        os.utime(p_data, (1000, 1000))
        r, p = self.write_desc({"sources": [
            {"url": "d.bin", "size": 10, "modified_not_after": 1000},
            {"url": "d.bin", "size": 11},
            {"url": "d.bin", "modified_not_after": 999},
            {"url": "d.bin", "etag": "\"x\""},
            {"url": "./sub/../d.bin"},
            {"url": "file://" + self.dir + "/d.bin"},
            {"url": "file://otherhost" + self.dir + "/d.bin"},
            {"url": "d.bin?x"},
            {"url": "%2E%2E/d.bin"},
            {"url": "ftp://x/y"},
            {"url": "#frag"},
        ], "entries": [{"key": "k%d" % i, "ranges": [{"source": i, "offset": 1, "length": 2}]} for i in range(11)]})
        self.assertEqual(r.returncode, 0, r.stderr)
        res = self.read_q(p, [{"op": "get", "key": "k%d" % i} for i in range(11)])
        R = res["results"]
        self.assertEqual(R[0]["value"], "3132")
        for i in (1, 2, 3, 6, 7, 9):
            self.assertEqual(R[i]["class"], "resolution", (i, R[i]))
        self.assertEqual(R[4]["value"], "3132")
        self.assertEqual(R[5]["value"], "3132")
        self.assertEqual(R[8]["class"], "resolution")  # path_dir/%2E%2E/d.bin -> encoded dot segment
        self.assertEqual(R[10]["value"], open(p, "rb").read()[1:3].hex())  # the archive itself

    def assert_end_records(self, data, n):
        """§3.2: zip64 record, locator, then an end record whose four fields are all ones."""
        eocd = len(data) - (60 if data[-38:-32] == b"vzip/0" else 44)
        self.assertEqual(struct.unpack_from("<IHHHHII", data, eocd),
                         (0x06054B50, 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF))
        sig, disk, z64, disks = struct.unpack_from("<IIQI", data, eocd - 20)
        self.assertEqual((sig, disk, z64, disks), (0x07064B50, 0, eocd - 76, 1))
        sig, size, _made, need, _d, _cd, n1, n2, cd_size, cd_off = struct.unpack_from("<IQHHIIQQQQ", data, z64)
        self.assertEqual((sig, size, need, n1, n2), (0x06064B50, 44, 45, n, n))
        self.assertEqual(cd_off + cd_size, z64)

    def test_small_archive_has_zip64_end_records(self):
        for paged in (False, True):
            data = writer.build([writer.WEntry("a", data=b"x")], [], page_size=64 if paged else None)
            self.assert_end_records(data, 3 if paged else 2)
            p = self.put("small.vzip", data)
            self.assertEqual(reader.Archive(p).get("a"), b"x")
            self.assertEqual(subprocess.run(["unzip", "-tq", p], capture_output=True).returncode, 0)

    def test_many_entries_zip64(self):
        n = 70000
        entries = [writer.WEntry("k%06d" % i, data=b"") for i in range(n)]
        data = writer.build(entries, [])
        self.assert_end_records(data, n + 1)
        p = self.put("big.vzip", data)
        ar = reader.Archive(p)
        self.assertEqual(len(ar.list("")), n)
        self.assertEqual(ar.get("k069999"), b"")
        ar.close()
        t = subprocess.run(["unzip", "-tq", p], capture_output=True)
        self.assertEqual(t.returncode, 0, t.stdout[-500:])
        data = writer.build(entries[:10], [], page_size=50)
        ar = self.open(data)
        self.assertEqual(ar.list("k00000"), ["k%06d" % i for i in range(10)])

    def test_list_page_selection(self):
        entries = [writer.WEntry(k, data=b"x") for k in ["a/0", "a/1", "a/5", "a/6", "b/0", "b/1"]]
        data = bytearray(writer.build(entries, [], page_size=2 * (46 + 3)))
        ar = self.open(bytes(data))
        self.assertEqual([p[0] for p in ar.pages], [b"a/0", b"a/5", b"b/0"])
        # corrupt the third page's first record signature -> only lists that read it fail
        cd_off = ar.cd_off
        third = ar.pages[2][1]
        ar.close()
        data[cd_off + third] = 0
        ar = self.open(bytes(data))
        self.assertEqual(ar.list("a/"), ["a/0", "a/1", "a/5", "a/6"])
        self.assertEqual(ar.list("a"), ["a/0", "a/1", "a/5", "a/6"])
        self.assertErr("entry", ar.list, "")
        self.assertErr("entry", ar.list, "b")
        self.assertErr("entry", ar.classify, "zzz")  # not in page, still entry error
        self.assertEqual(ar.classify("a/1"), "bytes")
        self.assertEqual(ar.classify("0"), "missing")  # before first page


class TestWriterRejects(Base):
    def reject(self, desc, raw=None):
        if raw is not None:
            dp = self.put("desc.json", raw.encode())
            r = self.cli("write", dp, self.path("out.vzip"))
        else:
            r, _ = self.write_desc(desc)
        self.assertNotEqual(r.returncode, 0, desc)
        self.assertFalse(os.path.exists(self.path("out.vzip")))
        self.assertTrue(r.stderr)

    def test_accepts(self):
        r, _ = self.write_desc({"page_size": 1, "entries": [{"key": "a", "bytes": "", "pinned": True},
                                                            {"key": "r", "ranges": [], "compress": False}],
                                "unknown": None, "sources": [{"url": "x", "foo": None}]})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_empty_key(self):
        self.reject({"entries": [{"key": "", "bytes": ""}]})

    def test_surrogate_key(self):
        self.reject(None, raw='{"entries": [{"key": "\\ud800", "bytes": ""}]}')

    def test_duplicate_key(self):
        self.reject({"entries": [{"key": "a", "bytes": ""}, {"key": "a", "bytes": ""}]})

    def test_format_key(self):
        self.reject({"entries": [{"key": "__vz__/sources", "bytes": ""}]})
        self.reject({"entries": [{"key": "__vz__/index", "bytes": ""}]})

    def test_source_out_of_bounds(self):
        self.reject({"entries": [{"key": "a", "ranges": [{}]}]})
        self.reject({"sources": [{"data": ""}], "entries": [{"key": "a", "ranges": [{"source": 1}]}]})

    def test_bad_url(self):
        self.reject({"sources": [{"url": ""}]})
        self.reject({"sources": [{"url": "a b"}]})
        self.reject({"sources": [{"url": "é"}]})

    def test_bad_key_source(self):
        self.reject({"sources": [{"key": ""}]})
        self.reject({"sources": [{"key": "nope"}]})
        self.reject({"sources": [{"key": "r"}], "entries": [{"key": "r", "ranges": []}]})
        self.reject({"sources": [{"key": "__vz__/sources"}]})

    def test_range_past_end(self):
        self.reject({"sources": [{"data": "0102"}], "entries": [{"key": "a", "ranges": [{"offset": 1, "length": 2}]}]})
        self.reject({"sources": [{"key": "b"}], "entries": [{"key": "b", "bytes": "01", "compress": True},
                                                           {"key": "a", "ranges": [{"length": 2}]}]})

    def test_pins(self):
        self.reject({"sources": [{"data": "", "size": 1}]})
        self.reject({"sources": [{"key": "a", "etag": "\"x\""}], "entries": [{"key": "a", "bytes": ""}]})
        self.reject({"sources": [{"url": "x", "etag": "W/\"x\""}]})
        self.reject({"sources": [{"url": "x", "etag": "x"}]})
        self.reject({"sources": [{"url": "x", "etag": "\"a\"b\""}]})
        self.reject({"sources": [{"url": "x", "size": -1}]})

    def test_payload_too_large(self):
        self.reject({"entries": [{"key": "a", "ranges": [{"data": "00" * 65520}]}]})

    def test_pinned(self):
        self.reject({"entries": [{"key": "a", "bytes": "", "pinned": True}]})
        self.reject({"page_size": 10, "entries": [{"key": "a", "ranges": [], "pinned": True}]})

    def test_harness_types(self):
        bad = [
            {"page_size": 0}, {"page_size": 1.0}, {"page_size": True}, {"mirror": None}, {"mirror": 1},
            {"sources": None}, {"entries": None},
            {"entries": [{"key": "a", "bytes": "0"}]}, {"entries": [{"key": "a", "bytes": "AB"}]},
            {"entries": [{"key": "a"}]}, {"entries": [{"key": "a", "bytes": "", "ranges": []}]},
            {"entries": [{"key": "a", "ranges": [], "compress": True}]},
            {"entries": [{"key": "a", "bytes": "", "compress": None}]},
            {"entries": [{"key": None, "bytes": ""}]}, {"entries": [{"bytes": ""}]},
            {"sources": [{"data": "00"}], "entries": [{"key": "a", "ranges": [{"data": "", "source": 0}]}]},
            {"sources": [{"data": "00"}], "entries": [{"key": "a", "ranges": [{"length": 1.0}]}]},
            {"sources": [{"url": "x", "key": "y"}]}, {"sources": [{}]}, {"sources": [{"url": "x", "key": None}]},
            {"sources": [{"url": "x", "size": None}]}, {"sources": [{"url": "x", "modified_not_after": 1.5}]},
        ]
        for d in bad:
            with self.subTest(d=d):
                self.reject(d)

    def test_duplicate_json_member(self):
        self.reject(None, raw='{"entries": [], "entries": []}')


class TestReadCLI(Base):
    def test_malformed_queries(self):
        r, p = self.write_desc({"entries": [{"key": "a", "bytes": "01"}]})
        bad = [
            [{"op": "nope", "key": "a"}], [{"op": "get"}], [{"op": "list"}],
            [{"op": "classify", "key": "a", "range": {"offset": 1}}],
            [{"op": "get", "key": "a", "range": None}], [{"op": "get", "key": "a", "range": {"start": 1}}],
            [{"op": "get", "key": "a", "range": {"offset": 1, "suffix": 1}}],
            [{"op": "get", "key": "a", "range": {"offset": -1}}],
            [{"op": "get", "key": "a", "range": {"offset": 1.0}}],
            {"op": "get", "key": "a"},
        ]
        for q in bad:
            qp = self.put("q.json", json.dumps(q).encode())
            r = self.cli("read", p, qp)
            self.assertNotEqual(r.returncode, 0, q)
        qp = self.put("q.json", b'[{"op":"get","key":"a","key":"b"}]')
        self.assertNotEqual(self.cli("read", p, qp).returncode, 0)
        res = self.read_q(p, [{"op": "get", "key": "a", "extra": 1, "range": {"offset": 0, "foo": 1}}])
        self.assertEqual(res["results"][0]["value"], "01")

    def test_open_failure(self):
        res = self.read_q(self.path("does-not-exist"), [{"op": "list", "prefix": ""}])
        self.assertEqual(res["open"]["ok"], False)
        self.assertEqual(res["open"]["class"], "archive")
        self.assertEqual(res["results"], [])


class TestReaderErrors(Base):
    """Crafted invalid archives: one test per error condition."""

    SRC = pb.encode_source_table([pb.encode_source("data", b"0123456789")])

    def good(self, entries=(), **kw):
        kw.setdefault("sources_pb", self.SRC)
        return craft.build(list(entries), **kw)

    def test_crafted_baseline(self):
        ar = self.open(self.good([E("a", b"hello"), E("r", b"", extra=ref_extra(range_payload(0, 2, 3)))]))
        self.assertEqual(ar.get("a"), b"hello")
        self.assertEqual(ar.get("r"), b"234")

    # ---- archive errors
    def test_not_vzip(self):
        self.assertErr("archive", self.open, b"PK\x05\x06" + b"\0" * 18)
        self.assertErr("archive", self.open, b"")

    def test_bad_magic(self):
        self.assertErr("archive", self.open, self.good(magic=b"vzip/1"))
        self.assertErr("archive", self.open, self.good(magic=b"vzap/0"))

    def test_no_fallback_to_22(self):
        # a 38-byte comment whose magic is wrong must not fall back to step 2
        data = self.good()
        ar = self.open(data)
        ar.close()
        bad = self.good(comment=b"xxxx/0" + b"\0" * 32)
        self.assertErr("archive", self.open, bad)

    def test_cd_outside_file(self):
        data = bytearray(self.good([E("a", b"x")]))
        z64 = len(data) - 44 - 20 - 56
        struct.pack_into("<Q", data, z64 + 48, len(data))  # cd offset beyond
        self.assertErr("archive", self.open, bytes(data))

    def test_cd_does_not_parse(self):
        data = self.good([E("a", b"x")], cd_extra=b"junk")
        self.assertErr("archive", self.open, data)

    def test_index_record_in_unpaged(self):
        self.assertErr("archive", self.open, self.good([E("__vz__/index", b"")]))

    def test_sources_not_inflating(self):
        self.assertErr("archive", self.open, self.good(sources_body=b"\x00\x01"))
        self.assertErr("archive", self.open, self.good(sources_body=craft.deflate(self.SRC) + b"\0"))
        self.assertErr("archive", self.open, self.good(sources_body=craft.deflate(self.SRC)[:-1]))

    def test_sources_outside_file(self):
        self.assertErr("archive", self.open, self.good(comment=b"vzip/0" + struct.pack("<QQ", 10 ** 9, 5)))

    def test_source_table_invalid(self):
        bad = [
            b"\x0a\x01\xff",  # malformed
            pb.encode_source_table([b""]),  # no kind
            pb.encode_source_table([pb.encode_source("url", "")]),
            pb.encode_source_table([pb.encode_source("key", "")]),
            pb.encode_source_table([pb.encode_source("data", b"", size=1)]),
            pb.encode_source_table([pb.encode_source("key", "a", mnf=0)]),
            pb.encode_source_table([pb.encode_source("url", "a", etag="W/\"x\"")]),
            pb.encode_source_table([pb.encode_source("url", "a", etag="x")]),
        ]
        for s in bad:
            self.assertErr("archive", self.open, self.good(sources_pb=s))

    def test_zip64_locator_missing(self):
        self.assertErr("archive", self.open, self.good(zip64=False))
        # a revision-8 archive: no zip64 records, actual values in the end record
        new = self.good([E("a", b"x")])
        fields = struct.unpack_from("<QQQQ", new, len(new) - 44 - 20 - 32)
        self.assertErr("archive", self.open, self.good([E("a", b"x")], zip64=False, eocd_fields=fields))

    def test_zip64_record_invalid(self):
        good = self.good([E("a", b"x")])
        z64 = len(good) - 44 - 20 - 56
        self.assertEqual(good[z64:z64 + 4], b"PK\x06\x06")
        for off, fmt, value in ((z64, "<I", 0x06064B51),          # record signature
                                (z64 + 4, "<Q", 45),              # size field is not 44
                                (z64 + 56 + 8, "<Q", len(good))):  # locator points outside the file
            data = bytearray(good)
            struct.pack_into(fmt, data, off, value)
            self.assertErr("archive", self.open, bytes(data))

    def test_eocd_fields_ignored(self):
        # readers take the directory's size and offset from the zip64 record (§3.2)
        for fields in ((0, 0, 0, 0), (7, 7, 12345, 99999)):
            ar = self.open(self.good([E("a", b"hello")], eocd_fields=fields))
            self.assertEqual(ar.get("a"), b"hello")
            self.assertEqual(ar.list(""), ["a"])

    def test_page_index_malformed(self):
        recs = [E("a", b"1"), E("b", b"2")]
        reclen = 46 + 1
        P = lambda k, o, l: pb.encode_page(k.encode(), o, l)  # noqa: E731
        good_idx = pb.encode_cd_index([P("a", 0, reclen), P("b", reclen, reclen)], [])
        ar = self.open(self.good(recs, index_pb=good_idx, cd_sort=True))
        self.assertEqual(ar.list(""), ["a", "b"])
        ar.close()
        bad = [
            b"\x0a\x05",  # does not decode
            pb.encode_cd_index([P("a", 0, 0)], []),
            pb.encode_cd_index([P("a", 0, 10 ** 6)], []),
            pb.encode_cd_index([P("a", 1, reclen)], []),
            pb.encode_cd_index([P("b", 0, reclen), P("a", reclen, reclen)], []),
            pb.encode_cd_index([P("a", 0, reclen), P("a", reclen, reclen)], []),
            pb.encode_cd_index([pb.encode_page(b"", 0, reclen)], []),
            pb.encode_cd_index([], [pb.encode_pinned(b"", 0, 0, 0, 0)]),
            pb.encode_cd_index([], [pb.encode_pinned(b"a", 0, 0, 0, 0)] * 2),
            pb.encode_cd_index([], [pb.encode_pinned(b"__vz__/sources", 0, 0, 0, 0)]),
            pb.encode_cd_index([], [pb.encode_pinned(b"a", 0, 0, 0, 1)]),
            pb.encode_cd_index([], [pb.encode_pinned(b"a", 10 ** 9, 0, 1, 0)]),
        ]
        for idx in bad:
            with self.subTest(idx=idx):
                self.assertErr("archive", self.open, self.good(recs, index_pb=idx, cd_sort=True))

    # ---- entry errors
    def test_entry_bad_extra(self):
        ar = self.open(self.good([E("a", b"x", extra=b"\x01\x00\x05\x00ab")]))
        self.assertErr("entry", ar.classify, "a")
        self.assertErr("entry", ar.get, "a")
        self.assertErr("entry", ar.raw, "a")
        self.assertEqual(ar.list(""), ["a"])

    def test_entry_two_ref_blocks(self):
        ex = ref_extra(b"") + ref_extra(b"", 0x7A77)
        self.assertErr("entry", self.open(self.good([E("a", b"", extra=ex)])).classify, "a")

    def test_entry_method(self):
        self.assertErr("entry", self.open(self.good([E("a", b"x", method=12)])).get, "a")

    def test_entry_encrypted(self):
        self.assertErr("entry", self.open(self.good([E("a", b"x", flags=0x801)])).get, "a")

    def test_entry_reference_deflate(self):
        e = E("a", craft.deflate(b""), method=8, usize=0, extra=ref_extra(b""))
        self.assertErr("entry", self.open(self.good([e])).classify, "a")

    def test_entry_zip64_rules(self):
        self.assertErr("entry", self.open(self.good([E("a", b"x", csize=0xFFFFFFFF)])).get, "a")
        self.assertErr("entry", self.open(self.good([E("a", b"x", offset=0xFFFFFFFF)])).get, "a")
        short = struct.pack("<HHI", 1, 4, 0)
        self.assertErr("entry", self.open(self.good([E("a", b"x", offset=0xFFFFFFFF, extra=short)])).get, "a")
        two = struct.pack("<HHQ", 1, 8, 0) * 2
        self.assertErr("entry", self.open(self.good([E("a", b"x", extra=two)])).get, "a")
        # a single 0x0001 block on a normal offset is ignored; a correct one is used
        ok = struct.pack("<HHQ", 1, 8, 12345)
        self.assertEqual(self.open(self.good([E("a", b"x", extra=ok)])).get("a"), b"x")
        ok = struct.pack("<HHQ", 1, 8, 0)
        self.assertEqual(self.open(self.good([E("a", b"x", offset=0xFFFFFFFF, extra=ok)])).get("a"), b"x")

    # ---- body errors
    def test_body_outside_file(self):
        ar = self.open(self.good([E("a", b"x", csize=10 ** 6, usize=10 ** 6)]))
        self.assertErr("body", ar.get, "a", ("range", 0, 0))
        self.assertErr("body", ar.raw, "a")

    def test_body_bad_deflate(self):
        ar = self.open(self.good([E("a", craft.deflate(b"hello") + b"\0", method=8, usize=5),
                                  E("b", craft.deflate(b"hello"), method=8, usize=4),
                                  E("c", b"\xff\xff", method=8, usize=4)]))
        for k in "abc":
            self.assertErr("body", ar.get, k, ("range", 0, 1))

    def test_body_stored_sizes(self):
        ar = self.open(self.good([E("a", b"xyz", usize=2)]))
        self.assertErr("body", ar.get, "a", ("range", 0, 1))

    def test_body_key_source_is_resolution(self):
        src = pb.encode_source_table([pb.encode_source("key", "__vz__/k")])
        ar = self.open(self.good([E("__vz__/k", b"xyz", usize=2),
                                  E("r", b"", extra=ref_extra(range_payload(0, 0, 1)))], sources_pb=src))
        self.assertErr("resolution", ar.get, "r")
        self.assertErr("body", ar.raw, "__vz__/k")

    def test_raw_reference_body(self):
        pl = range_payload(0, 2, 3)
        ar = self.open(self.good([E("r", pl, extra=ref_extra(pl)), E("q", b"junk", extra=ref_extra(pl))]))
        self.assertEqual(ar.raw("r"), pl)
        self.assertEqual(ar.get("q"), b"234")  # body ignored by get
        self.assertEqual(ar.raw("q"), b"junk")

    # ---- payload errors
    def test_payload_errors(self):
        bad = [
            (0x7A76, b"\x0a\x01"),  # truncated / malformed
            (0x7A76, range_payload(1, 0, 1)),  # source out of bounds
            (0x7A76, pb.encode_range(source=0, offset=1) + bytes.fromhex("2a00")),  # literal + offset
            (0x7A76, bytes.fromhex("18ffffffffffffffffff01 2001".replace(" ", ""))),  # offset+length overflow
            (0x7A77, pb.encode_concat([pb.encode_range(data=b""), pb.encode_range(5, 0, 0)])),
            (0x7A77, pb.encode_concat([pb.encode_range(0, 0, 2 ** 63), pb.encode_range(0, 0, 2 ** 63)])),
            (0x7A76, b"\x2a" + pb.enc_varint(65520) + b"\0" * 65520),  # too long (crafted, >65519)
        ]
        for rid, pl in bad:
            with self.subTest(pl=pl[:20]):
                if len(pl) > 65000:
                    # does not fit together with other blocks; craft extra directly
                    ex = struct.pack("<HH", rid, len(pl) - 4)[:4] + pl[:65531]
                    pl2 = pl[:65520]
                    ex = struct.pack("<HH", rid, len(pl2)) + pl2
                else:
                    ex = ref_extra(pl, rid)
                ar = self.open(self.good([E("r", b"", extra=ex)]))
                self.assertEqual(ar.classify("r"), "reference")
                self.assertErr("payload", ar.get, "r", ("range", 0, 0))
                self.assertErr("request", ar.get, "r", ("range", 1, 0))

    def test_literal_zero_explicit(self):
        pl = bytes.fromhex("0800 1800 2000 2a0141".replace(" ", ""))
        ar = self.open(self.good([E("r", b"", extra=ref_extra(pl))]))
        self.assertEqual(ar.get("r"), b"A")

    # ---- resolution errors and laziness
    def test_resolution(self):
        src = pb.encode_source_table([
            pb.encode_source("url", "nonexistent.bin"),
            pb.encode_source("key", "r2"),
            pb.encode_source("key", "nope"),
            pb.encode_source("key", "__vz__/sources"),
            pb.encode_source("data", b"abc"),
            pb.encode_source("url", "a b"),
            pb.encode_source("url", "gopher://x/y"),
        ])
        refs = [E("r%d" % i, b"", extra=ref_extra(range_payload(i, 0, 2))) for i in range(7)]
        lazy = pb.encode_concat([pb.encode_range(data=b"ok"), pb.encode_range(0, 0, 5), pb.encode_range(2, 0, 0)])
        refs.append(E("lazy", b"", extra=ref_extra(lazy, 0x7A77)))
        short = range_payload(4, 2, 5)
        refs.append(E("short", b"", extra=ref_extra(short)))
        ar = self.open(self.good(refs, sources_pb=src))
        for i in (0, 1, 2, 3, 5, 6):
            self.assertErr("resolution", ar.get, "r%d" % i)
        self.assertEqual(ar.get("r4"), b"ab")
        self.assertEqual(ar.get("lazy", ("range", 0, 2)), b"ok")
        self.assertErr("resolution", ar.get, "lazy")
        self.assertEqual(ar.get("short", ("range", 0, 1)), b"c")
        self.assertErr("resolution", ar.get, "short", ("range", 0, 2))

    def test_hidden(self):
        ar = self.open(self.good([E("__vz__/x", b"1")]))
        self.assertEqual(ar.classify("__vz__/x"), "missing")
        self.assertIsNone(ar.get("__vz__/x"))
        self.assertEqual(ar.raw("__vz__/x"), b"1")
        self.assertEqual(ar.list(""), [])
        self.assertEqual(ar.raw("__vz__/sources"), self.SRC)
        self.assertIsNone(ar.raw("__vz__/index"))


if __name__ == "__main__":
    unittest.main()
