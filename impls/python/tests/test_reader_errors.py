import os
import struct
import unittest
import zlib
from unittest import mock

from helpers import (get_extra, open_error, q1, query, rebuild, save, set_extra,
                     tmpdir, write_desc)
from vzip_impl import pb, writer


def raw_deflate(b):
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(b) + c.flush()


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tmpdir()
        self.d = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def p(self, name):
        return os.path.join(self.d, name)

    def make(self, desc, name="a.vzip"):
        path = self.p(name)
        data = write_desc(desc, path)
        return path, data

    def cls(self, path, q):
        r = q1(path, q)
        self.assertFalse(r["ok"], r)
        return r["class"]


class ArchiveErrors(Base):
    def assert_archive_error(self, data):
        path = self.p("bad.vzip")
        save(path, data)
        err = open_error(path)
        self.assertIsNotNone(err)
        self.assertEqual(err["class"], "archive")

    def test_missing_file(self):
        self.assertEqual(open_error(self.p("nope.vzip"))["class"], "archive")

    def test_not_a_zip(self):
        self.assert_archive_error(b"hello")
        self.assert_archive_error(b"\0" * 100)

    def test_plain_zip_without_vzip_comment(self):
        _, data = self.make({})
        # strip the comment: comment length 0
        e = len(data) - 44
        bad = data[:e + 20] + b"\0\0"
        self.assert_archive_error(bad)

    def test_bad_magic(self):
        _, data = self.make({})
        self.assert_archive_error(data[:-22] + b"vzip/2" + data[-16:])

    def test_comment_length_mismatch(self):
        _, data = self.make({})
        bad = bytearray(data)
        struct.pack_into("<H", bad, len(data) - 44 + 20, 38)
        self.assert_archive_error(bytes(bad))

    def test_truncated(self):
        _, data = self.make({"entries": [{"key": "a", "bytes": "00" * 100}]})
        self.assert_archive_error(data[:-1])
        self.assert_archive_error(data[50:])  # prepended data removed => offsets wrong

    def test_cd_outside_file(self):
        _, data = self.make({"entries": [{"key": "a", "bytes": "00"}]})
        bad = bytearray(data)
        struct.pack_into("<I", bad, len(data) - 44 + 12, 10 ** 6)
        self.assert_archive_error(bytes(bad))

    def test_cd_does_not_parse(self):
        _, data = self.make({"entries": [{"key": "a", "bytes": "00"}]})
        bad = bytearray(data)
        cd_size, cd_off = struct.unpack_from("<II", bad, len(data) - 44 + 12)
        bad[cd_off] ^= 0xFF  # signature
        self.assert_archive_error(bytes(bad))
        bad = bytearray(data)
        struct.pack_into("<I", bad, len(data) - 44 + 12, cd_size - 1)
        self.assert_archive_error(bytes(bad))

    def test_sources_body_outside_or_not_clean(self):
        _, data = self.make({"sources": [{"data": "00"}]})
        bad = bytearray(data)
        s_off, s_size = struct.unpack_from("<QQ", bad, len(data) - 16)
        struct.pack_into("<Q", bad, len(data) - 8, len(data))
        self.assert_archive_error(bytes(bad))
        bad = bytearray(data)
        struct.pack_into("<Q", bad, len(data) - 8, s_size + 1)  # trailing byte after stream
        self.assert_archive_error(bytes(bad))
        bad = bytearray(data)
        struct.pack_into("<Q", bad, len(data) - 8, s_size - 1)  # truncated stream
        self.assert_archive_error(bytes(bad))

    def _with_sources_raw(self, raw):
        with mock.patch.object(writer.pb, "encode_source_table", lambda s: raw):
            _, data = self.make({}, name="s.vzip")
        return data

    def test_source_table_invalid(self):
        bad_tables = [
            b"\x0b",                                   # wire type 3
            b"\x0a\x05\x0a\x01a",                      # truncated LEN
            b"\x0a\x00",                               # Source with no kind
            b"\x0a\x02\x0a\x00",                       # empty url
            b"\x0a\x05\x12\x01k\x20\x01",              # pin on key source
            b"\x0a\x05\x1a\x01\x00\x20\x01",           # pin on data source
            b"\x0a\x08\x0a\x01a\x2a\x03abc",           # weak/unquoted etag
            b"\x0a\x09\x0a\x01a\x2a\x04W/\"\"",        # weak etag
            b"\x0a\x03\x0a\x01\xff",                   # invalid UTF-8 url
            b"\x0a\x03\x08\x01\x00"[:0] + b"\x0a\x02\x08\x01",  # url field with VARINT wire type
            b"\x0a\x03\x20" + b"\xff" * 10,           # varint too long (LEN mismatch too)
            b"\x02\x00",                               # field number 0
        ]
        for raw in bad_tables:
            with self.subTest(raw=raw):
                self.assert_archive_error(self._with_sources_raw(raw))

    def test_source_table_valid_variants(self):
        ok_tables = [
            b"",
            b"\x0a\x03\x0a\x01a" + b"\x10\x05" + b"\x19" + b"\0" * 8,   # unknown fields skipped
            b"\x0a\x06\x0a\x01a\x12\x01b",            # oneof: last wins (key b) -> no pins, ok
            b"\x0a\x04\x1a\x00\x30\x00"[:0] + b"\x0a\x02\x1a\x00",  # empty data source
            b"\x0a\x0e\x0a\x01a\x30" + b"\xff" * 9 + b"\x01",  # negative int64 pin
            b"\x0a\x09\x0a\x01a\xf8\xff\xff\xff\x0f\x00",  # unknown field 2^29-1 (max) skipped
        ]
        for raw in ok_tables:
            with self.subTest(raw=raw):
                path = self.p("ok.vzip")
                save(path, self._with_sources_raw(raw))
                self.assertIsNone(open_error(path))

    def test_index_record_in_unpaged_archive(self):
        _, data = self.make({"entries": [{"key": "__vz__/indey", "bytes": "00"}]})
        bad = data.replace(b"__vz__/indey", b"__vz__/index")
        self.assert_archive_error(bad)

    def _with_index(self, mutate, entries=None):
        orig = pb.encode_cdindex

        def enc(idx):
            mutate(idx)
            return orig(idx)
        entries = entries or [{"key": k, "bytes": "00"} for k in "abcdef"] + \
            [{"key": "p", "bytes": "0102", "pinned": True}]
        with mock.patch.object(writer.pb, "encode_cdindex", enc):
            _, data = self.make({"page_size": 1, "entries": entries}, name="i.vzip")
        return data

    def test_malformed_page_index(self):
        def setp(i, **kw):
            def m(idx):
                for k, v in kw.items():
                    setattr(idx.pages[i], k, v)
            return m

        def addpin(**kw):
            def m(idx):
                idx.pinned.append(pb.Pinned(**kw))
            return m
        cases = {
            "zero length": setp(1, length=0),
            "outside cd": setp(-1, length=10 ** 6),
            "gap": setp(2, offset=1000),
            "first not 0": lambda idx: setattr(idx.pages[0], "offset", 1),
            "keys not increasing": setp(2, first_key=b"a"),
            "pinned twice": addpin(key=b"p", data_offset=0, size=0, csize=0, method=0),
            "pinned format": addpin(key=b"__vz__/sources", data_offset=0),
            "pinned method": addpin(key=b"q", method=12),
            "pinned outside": addpin(key=b"q", data_offset=10 ** 6, csize=1),
        }
        for name, m in cases.items():
            with self.subTest(case=name):
                self.assert_archive_error(self._with_index(m))
        with mock.patch.object(writer.pb, "encode_cdindex", lambda idx: b"\x0f"):
            _, data = self.make({"page_size": 1}, name="j.vzip")
        self.assert_archive_error(data)

    def test_zip64_rules(self):
        entries = [{"key": "k%05d" % i, "bytes": ""} for i in range(0xFFFF)]
        _, data = self.make({"entries": entries}, name="z.vzip")
        e = len(data) - 44
        loc = e - 20
        # locator missing
        bad = bytearray(data)
        bad[loc] ^= 1
        self.assert_archive_error(bytes(bad))
        # zip64 record size field wrong
        z_off = struct.unpack_from("<Q", data, loc + 8)[0]
        bad = bytearray(data)
        struct.pack_into("<Q", bad, z_off + 4, 45)
        self.assert_archive_error(bytes(bad))
        # zip64 record outside file
        bad = bytearray(data)
        struct.pack_into("<Q", bad, loc + 8, len(data))
        self.assert_archive_error(bytes(bad))
        # a locator-looking signature before an EOCD that does not ask for zip64 is ignored
        _, small = self.make({"entries": [{"key": "PK\x06\x07" + "x" * 16, "bytes": ""}]}, name="y.vzip")
        path = self.p("y.vzip")
        self.assertIsNone(open_error(path))


class EntryAndBodyErrors(Base):
    def setUp(self):
        super().setUp()
        self.path, self.data = self.make({
            "sources": [{"data": "0011223344"}, {"key": "s"}, {"key": "d"}],
            "entries": [
                {"key": "s", "bytes": "aabbccdd"},
                {"key": "d", "bytes": "aabbccdd" * 10, "compress": True},
                {"key": "r", "ranges": [{"offset": 1, "length": 2}]},
                {"key": "ok", "bytes": "01"},
                {"key": "ks", "ranges": [{"source": 1, "offset": 1, "length": 2}]},
                {"key": "kd", "ranges": [{"source": 2, "offset": 1, "length": 2}]},
            ]})

    def mutated(self, key, fn):
        data = rebuild(self.data, lambda n, r: fn(r) if n == key.encode() else r)
        path = self.p("m.vzip")
        save(path, data)
        return path

    def assert_entry_error(self, path, key):
        for q in ({"op": "classify", "key": key}, {"op": "get", "key": key},
                  {"op": "get_raw", "key": key}):
            self.assertEqual(self.cls(path, q), "entry", q)
        self.assertEqual(q1(path, {"op": "get", "key": "ok"}), {"ok": True, "value": "01"})
        self.assertIn(key, q1(path, {"op": "list", "prefix": ""})["keys"])

    def test_two_reference_blocks(self):
        def f(r):
            x = get_extra(r)
            return set_extra(r, x + x)
        self.assert_entry_error(self.mutated("r", f), "r")

    def test_mixed_reference_blocks(self):
        def f(r):
            x = get_extra(r)
            return set_extra(r, x + struct.pack("<HH", 0x7A77, 0))
        self.assert_entry_error(self.mutated("r", f), "r")

    def test_unparseable_extra(self):
        self.assert_entry_error(self.mutated("s", lambda r: set_extra(r, b"\x01\x00\x05")), "s")
        self.assert_entry_error(self.mutated("s", lambda r: set_extra(r, b"\x01\x00\x05\x00ab")), "s")

    def test_bad_method(self):
        def f(r):
            struct.pack_into("<H", r, 10, 12)
            return r
        self.assert_entry_error(self.mutated("s", f), "s")

    def test_encrypted(self):
        def f(r):
            struct.pack_into("<H", r, 8, 0x0801)
            return r
        self.assert_entry_error(self.mutated("s", f), "s")

    def test_reference_with_method_8(self):
        def f(r):
            struct.pack_into("<H", r, 10, 8)
            return r
        self.assert_entry_error(self.mutated("r", f), "r")

    def test_ffffffff_offset_without_zip64(self):
        def f(r):
            struct.pack_into("<I", r, 42, 0xFFFFFFFF)
            return r
        self.assert_entry_error(self.mutated("s", f), "s")

    def test_other_extra_blocks_ignored(self):
        path = self.mutated("s", lambda r: set_extra(r, struct.pack("<HH", 0x5455, 1) + b"\x00"))
        self.assertEqual(q1(path, {"op": "classify", "key": "s"}), {"ok": True, "kind": "bytes"})
        path = self.mutated("r", lambda r: set_extra(r, struct.pack("<HH", 0x5455, 1) + b"\x00" + get_extra(r)))
        self.assertEqual(q1(path, {"op": "get", "key": "r"}), {"ok": True, "value": "1122"})

    def test_bytes_entry_with_zip64_offset_block(self):
        def f(r):
            off = struct.unpack_from("<I", r, 42)[0]
            struct.pack_into("<I", r, 42, 0xFFFFFFFF)
            return set_extra(r, struct.pack("<HHQ", 1, 8, off))
        path = self.mutated("s", f)
        self.assertEqual(q1(path, {"op": "get", "key": "s"}), {"ok": True, "value": "aabbccdd"})

    def test_stored_size_mismatch(self):
        def f(r):
            struct.pack_into("<I", r, 24, 5)  # uncompressed size
            return r
        path = self.mutated("s", f)
        for q in ({"op": "get", "key": "s"}, {"op": "get_raw", "key": "s"}):
            self.assertEqual(self.cls(path, q), "body")
        self.assertEqual(q1(path, {"op": "classify", "key": "s"}), {"ok": True, "kind": "bytes"})
        self.assertEqual(self.cls(path, {"op": "get", "key": "ks"}), "resolution")

    def test_body_outside_file(self):
        def f(r):
            struct.pack_into("<II", r, 20, 10 ** 6, 10 ** 6)
            return r
        path = self.mutated("s", f)
        self.assertEqual(self.cls(path, {"op": "get", "key": "s", "range": {"start": 0, "end": 1}}), "body")
        self.assertEqual(self.cls(path, {"op": "get", "key": "ks"}), "resolution")

    def test_deflate_wrong_size_or_trailing(self):
        def bigger(r):
            struct.pack_into("<I", r, 24, 81)
            return r
        path = self.mutated("d", bigger)
        self.assertEqual(self.cls(path, {"op": "get", "key": "d", "range": {"start": 0, "end": 1}}), "body")
        self.assertEqual(self.cls(path, {"op": "get_raw", "key": "d"}), "body")
        self.assertEqual(self.cls(path, {"op": "get", "key": "kd"}), "resolution")

        def trailing(r):
            csize = struct.unpack_from("<I", r, 20)[0]
            struct.pack_into("<I", r, 20, csize + 1)
            return r
        path = self.mutated("d", trailing)
        self.assertEqual(self.cls(path, {"op": "get", "key": "d"}), "body")

        def short(r):
            csize = struct.unpack_from("<I", r, 20)[0]
            struct.pack_into("<I", r, 20, csize - 1)
            return r
        path = self.mutated("d", short)
        self.assertEqual(self.cls(path, {"op": "get", "key": "d"}), "body")

    def test_reference_body_ignored(self):
        # corrupt the mirrored body: get still works, raw shows the corrupted body
        i = self.data.find(b"r" + pb.encode_range(pb.Range(offset=1, length=2)))
        bad = bytearray(self.data)
        bad[i + 1] ^= 0xFF
        path = self.p("b.vzip")
        save(path, bytes(bad))
        self.assertEqual(q1(path, {"op": "get", "key": "r"}), {"ok": True, "value": "1122"})
        self.assertNotEqual(q1(path, {"op": "get_raw", "key": "r"})["value"],
                            pb.encode_range(pb.Range(offset=1, length=2)).hex())

    def test_request_error_precedes_everything(self):
        def f(r):
            struct.pack_into("<H", r, 10, 12)
            return r
        path = self.mutated("s", f)
        for k in ("s", "missing", "__vz__/x"):
            self.assertEqual(self.cls(path, {"op": "get", "key": k, "range": {"start": 2, "end": 1}}), "request")

    def test_invalid_names_ignored(self):
        _, data = self.make({"entries": [{"key": "ok", "bytes": "01"}, {"key": "zz", "bytes": "02"}]}, name="n.vzip")
        bad = data.replace(b"zz", b"\xff\xfe")
        path = self.p("n2.vzip")
        save(path, bad)
        self.assertEqual(q1(path, {"op": "list", "prefix": ""}), {"ok": True, "keys": ["ok"]})


class PayloadErrors(Base):
    def make_payload(self, pid, payload, nsrc=1):
        orig_range = pb.encode_range
        orig_concat = pb.encode_concat
        target = {"done": False}

        def er(r):
            return payload if r.data == b"MARK" else orig_range(r)

        def ec(parts):
            return payload if parts and parts[0].data == b"MARK" else orig_concat(parts)
        ranges = [{"data": "4d41524b"}] if pid == 0x7A76 else [{"data": "4d41524b"}, {"data": ""}]
        with mock.patch.object(writer.pb, "encode_range", er), \
                mock.patch.object(writer.pb, "encode_concat", ec):
            path, _ = self.make({"sources": [{"data": "00112233"}] * nsrc,
                                 "entries": [{"key": "r", "ranges": ranges}]}, name="p.vzip")
        del target
        return path

    def test_malformed_payloads(self):
        R, C = 0x7A76, 0x7A77
        cases = [
            (R, b"\x0b"), (R, b"\x0c"), (R, b"\x0e"), (R, b"\x0f"),     # wire types 3,4,6,7
            (R, b"\x00\x00"),                                           # field 0
            (R, b"\x80\x80\x80\x80\x10\x00"),                           # field 2^29 (> max)
            (R, b"\x2a\x05ab"),                                         # truncated LEN
            (R, b"\x18" + b"\x80" * 10 + b"\x00"),                      # 11-byte varint
            (R, b"\x18" + b"\xff" * 9 + b"\x02"),                       # > 2^64-1
            (R, b"\x18"),                                               # truncated varint
            (R, b"\x08\x80\x80\x80\x80\x10"),                           # uint32 overflow (2^32)
            (R, b"\x0a\x00"),                                           # source as LEN
            (R, b"\x28\x01"),                                           # data as VARINT
            (R, b"\x2a\x00\x08\x01"),                                   # literal with source
            (R, b"\x2a\x00\x18\x01"),                                   # literal with offset
            (R, b"\x08\x01"),                                           # source out of range
            (R, b"\x18" + b"\xff" * 9 + b"\x01" + b"\x20\x01"),         # offset+length overflow
            (C, b"\x0a\x02\x08\x05"),                                   # part source oob
            (C, b"\x0a\x01"),                                           # part truncated
            (C, b"\x08\x01"),                                           # parts as VARINT
            (C, (b"\x0a\x0b\x20" + b"\xff" * 9 + b"\x01") * 2),         # concat size overflow
            (C, b"\x0a\x03\x2a\x01"),                                   # nested LEN past parent end
        ]
        for pid, payload in cases:
            with self.subTest(pid=hex(pid), payload=payload):
                path = self.make_payload(pid, payload)
                self.assertEqual(q1(path, {"op": "classify", "key": "r"}), {"ok": True, "kind": "reference"})
                self.assertEqual(self.cls(path, {"op": "get", "key": "r", "range": {"suffix": 0}}), "payload")
                self.assertTrue(q1(path, {"op": "get_raw", "key": "r"})["ok"])

    def test_tolerated_payloads(self):
        R, C = 0x7A76, 0x7A77
        cases = [
            (R, b"\x12\x03abc\x18\x01\x20\x02", "1122"),                # reserved field 2 skipped
            (R, b"\x10\x05\x18\x01\x20\x02", "1122"),                   # reserved field 2 as VARINT
            (R, b"\x18\x81\x00\x20\x02", "1122"),                       # non-minimal varint
            (R, b"\x20\x02\x18\x01", "1122"),                           # any order
            (R, b"\x18\x03\x18\x01\x20\x02", "1122"),                   # last wins
            (R, b"\x2a\x01X\x2a\x02ab", "6162"),                        # last data wins
            (R, b"\x31" + b"\0" * 8 + b"\x3d\0\0\0\0\x18\x01\x20\x01", "11"),  # I64/I32 unknown
            (R, b"", ""),                                               # empty source range
            (C, b"", ""),
            (C, b"\x0a\x00", ""),                                       # one empty source range
            (C, b"\x0a\x02\x2a\x00\x0a\x04\x18\x02\x20\x01", "22"),
            (R, b"\x08\x07\x18\x01", None),                             # oob even with length 0
        ]
        for pid, payload, expect in cases:
            with self.subTest(payload=payload):
                path = self.make_payload(pid, payload)
                r = q1(path, {"op": "get", "key": "r"})
                if expect is None:
                    self.assertEqual(r["class"], "payload")
                else:
                    self.assertEqual(r, {"ok": True, "value": expect})


class ResolutionErrors(Base):
    def setUp(self):
        super().setUp()
        with open(self.p("f.bin"), "wb") as fh:
            fh.write(b"0123456789")
        os.utime(self.p("f.bin"), (1_000_000_000.7, 1_000_000_000.7))
        os.makedirs(self.p("sub"))
        with open(self.p("sub/g h.bin"), "wb") as fh:
            fh.write(b"abcdef")

    def get(self, src, offset=0, length=1, extra_entries=(), ranges=None):
        path, _ = self.make({"sources": [src] if isinstance(src, dict) else src,
                             "entries": [{"key": "r", "ranges": ranges or [{"offset": offset, "length": length}]},
                                         *extra_entries]}, name="res.vzip")
        return q1(path, {"op": "get", "key": "r"})

    def assert_ok(self, src, expect, **kw):
        self.assertEqual(self.get(src, **kw), {"ok": True, "value": expect.hex()}, src)

    def assert_res(self, src, **kw):
        r = self.get(src, **kw)
        self.assertEqual(r.get("class"), "resolution", (src, r))

    def test_url_resolution_ok(self):
        abs_uri = "file://" + self.d.replace(" ", "%20") + "/f.bin"
        for u, exp in [("f.bin", b"0"), ("./f.bin", b"0"), ("sub/../f.bin", b"0"),
                       ("sub/g%20h.bin", b"a"), (abs_uri, b"0"), ("FILE:" + abs_uri[5:], b"0"),
                       ("file://localhost" + abs_uri[7:], b"0"),
                       ("file://LocalHost" + abs_uri[7:], b"0"),
                       ("file:" + abs_uri[7:], b"0"), (abs_uri + "#frag", b"0"),
                       ("f.bin#x", b"0"), ("../" * 30 + self.d[1:] + "/f.bin", b"0"),
                       ("%66.bin", b"0")]:
            self.assert_ok({"url": u}, exp)
        # fragment-only reference resolves to the archive itself
        r = self.get({"url": "#x"}, offset=0, length=4)
        self.assertEqual(r, {"ok": True, "value": b"PK\x03\x04".hex()})

    def test_url_resolution_errors(self):
        for u in ["missing.bin", "http://example.invalid/x", "s3://b/k", "file://host/x",
                  "f.bin?", "f.bin?q=1", "file:f.bin", "sub%2Fg%20h.bin", "%2E%2E/f.bin",
                  "sub/%2e%2e/f.bin", "f%00.bin", "sub", "f.bin/"]:
            self.assert_res({"url": u})

    def test_short_source(self):
        self.assert_res({"url": "f.bin"}, offset=5, length=6)
        self.assert_ok({"url": "f.bin"}, b"56789", offset=5, length=5)
        self.assert_res({"data": "00"}, offset=0, length=2)
        self.assert_res({"key": "k"}, offset=3, length=1, extra_entries=[{"key": "k", "bytes": "000000"}])
        self.assert_res({"key": "__vz__/k"}, offset=3, length=1,
                        extra_entries=[{"key": "__vz__/k", "bytes": "000000", "compress": True}])

    def test_pins(self):
        self.assert_ok({"url": "f.bin", "size": 10}, b"0")
        self.assert_res({"url": "f.bin", "size": 11})
        self.assert_res({"url": "f.bin", "etag": "\"x\""})
        self.assert_ok({"url": "f.bin", "modified_not_after": 1_000_000_000}, b"0")
        self.assert_ok({"url": "f.bin", "modified_not_after": 2_000_000_000}, b"0")
        self.assert_res({"url": "f.bin", "modified_not_after": 999_999_999})
        self.assert_res({"url": "f.bin", "size": 10, "modified_not_after": 999_999_999})

    def test_key_sources(self):
        self.assert_ok({"key": "__vz__/hdr"}, b"\x02", offset=1, length=1,
                       extra_entries=[{"key": "__vz__/hdr", "bytes": "0102"}])
        self.assert_ok({"key": "z"}, b"\x02", offset=1, length=1,
                       extra_entries=[{"key": "z", "bytes": "0102", "compress": True}])

    def test_key_source_reader_side_errors(self):
        # a reference or format entry named by a key source (crafted past the writer)
        for name, entries in [(b"__vz__/sources", []), (b"q", [{"key": "q", "ranges": []}]),
                              (b"zz", [])]:
            with self.subTest(name=name):
                src = pb.Source("key", name)
                path = self.p("ks.vzip")
                from vzip_impl.writer import InEntry
                es = [InEntry(b"r", ranges=[pb.Range(source=0, length=1)])]
                es += [InEntry(e["key"].encode(), ranges=[]) for e in entries]
                orig = pb.encode_source_table
                with mock.patch.object(writer.pb, "encode_source_table", lambda s: orig([src])):
                    data = writer.build_archive(es, [pb.Source("data", b"x")])
                save(path, data)
                self.assertEqual(self.cls(path, {"op": "get", "key": "r"}), "resolution")

    def test_unresolved_ranges_cause_no_error(self):
        srcs = [{"url": "missing.bin"}, {"data": "aabb"}]
        rs = [{"source": 0, "offset": 0, "length": 5}, {"source": 1, "offset": 0, "length": 2},
              {"source": 0, "offset": 100, "length": 0}]
        path, _ = self.make({"sources": srcs, "entries": [{"key": "r", "ranges": rs}]}, name="u.vzip")
        self.assertEqual(q1(path, {"op": "get", "key": "r", "range": {"suffix": 2}}), {"ok": True, "value": "aabb"})
        self.assertEqual(q1(path, {"op": "get", "key": "r", "range": {"start": 5, "end": 6}}), {"ok": True, "value": "aa"})
        self.assertEqual(q1(path, {"op": "get", "key": "r", "range": {"start": 4, "end": 6}})["class"], "resolution")
        self.assertEqual(q1(path, {"op": "get", "key": "r", "range": {"start": 9, "end": 9}}), {"ok": True, "value": ""})


class PagedLookup(Base):
    def test_unparseable_page(self):
        path, data = self.make({"page_size": 1, "entries": [{"key": k, "bytes": "00"} for k in ["a", "b", "c"]]
                                + [{"key": "p", "bytes": "01", "pinned": True}]})
        # corrupt signature of record "b" (second record)
        e = len(data) - 60
        cd_off = struct.unpack_from("<I", data, e + 16)[0]
        rec_len = 46 + 1
        bad = bytearray(data)
        bad[cd_off + rec_len] ^= 0xFF
        save(path, bytes(bad))
        self.assertIsNone(open_error(path))
        for q in ({"op": "classify", "key": "b"}, {"op": "get", "key": "bz"}, {"op": "get_raw", "key": "b"}):
            self.assertEqual(self.cls(path, q), "entry")
        self.assertEqual(q1(path, {"op": "get", "key": "a"}), {"ok": True, "value": "00"})
        self.assertEqual(q1(path, {"op": "get", "key": "p"}), {"ok": True, "value": "01"})
        self.assertEqual(self.cls(path, {"op": "list", "prefix": ""}), "entry")
        self.assertEqual(self.cls(path, {"op": "list", "prefix": "b"}), "entry")
        self.assertEqual(q1(path, {"op": "list", "prefix": "a"}), {"ok": True, "keys": ["a"]})
        self.assertEqual(q1(path, {"op": "list", "prefix": "c"}), {"ok": True, "keys": ["c"]})
        self.assertEqual(q1(path, {"op": "list", "prefix": "p"}), {"ok": True, "keys": ["p"]})

    def test_lookup_follows_index_not_directory(self):
        # Pages say first_key "b" for the page holding "a": "a" is not found.
        orig = pb.encode_cdindex

        def enc(idx):
            idx.pages[0].first_key = b"a0"
            return orig(idx)
        with mock.patch.object(writer.pb, "encode_cdindex", enc):
            path, _ = self.make({"page_size": 1, "entries": [{"key": k, "bytes": "00"} for k in ["a", "b"]]})
        self.assertEqual(q1(path, {"op": "classify", "key": "a"}), {"ok": True, "kind": "missing"})
        self.assertEqual(q1(path, {"op": "list", "prefix": ""}), {"ok": True, "keys": ["b"]})


if __name__ == "__main__":
    unittest.main()
