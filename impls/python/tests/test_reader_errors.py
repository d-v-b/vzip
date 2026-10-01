"""Reader error classification (spec §8.1, §8.4) on hand-crafted archives."""

import io
import os
import struct
import unittest
import zipfile

from helpers import (TmpDir, build_raw, deflate, read_queries, ref_extra, simple_pages)

from vzip_impl import (Archive, ArchiveError, BodyError, EntryError, PayloadError,
                       RequestError, ResolutionError)
from vzip_impl import proto
from vzip_impl.proto import encode_concat, encode_range, encode_source_table


def src_table(*sources):
    return encode_source_table(sources)


class Base(unittest.TestCase):
    def setUp(self):
        self.td = TmpDir()
        self.n = 0
        self.opened = []

    def tearDown(self):
        for a in self.opened:
            a.close()
        self.td.cleanup()

    def save(self, data):
        self.n += 1
        return self.td.write(f"a{self.n}.vzip", data)

    def open(self, data):
        a = Archive(self.save(data))
        self.opened.append(a)
        return a

    def assertArchiveError(self, data):
        with self.assertRaises(ArchiveError):
            self.open(data)


# --------------------------------------------------------------- archive errors

class TestArchiveErrors(Base):
    def test_valid_baseline_opens(self):
        a = self.open(build_raw([{"name": b"k", "body": b"v"}]))
        self.assertEqual(a.get("k"), b"v")
        a = self.open(build_raw([{"name": b"k", "body": b"v"}], index_builder=simple_pages))
        self.assertEqual(a.get("k"), b"v")

    def test_missing_file(self):
        with self.assertRaises(ArchiveError):
            Archive(self.td.p("nope.vzip"))

    def test_empty_file(self):
        self.assertArchiveError(b"")

    def test_plain_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("a", b"b")
        self.assertArchiveError(buf.getvalue())

    def test_unsupported_version(self):
        self.assertArchiveError(build_raw([], magic=b"vzip/1"))

    def test_bad_magic(self):
        self.assertArchiveError(build_raw([], magic=b"zzip/0"))

    def test_sources_outside_file(self):
        data = bytearray(build_raw([]))
        struct.pack_into("<Q", data, len(data) - 16, 1 << 40)
        self.assertArchiveError(bytes(data))

    def test_sources_bad_deflate(self):
        self.assertArchiveError(build_raw([], sources_body=b"\xff\xff\xff"))

    def test_sources_trailing_bytes(self):
        self.assertArchiveError(build_raw([], sources_body=deflate(b"") + b"\x00"))

    def test_sources_truncated(self):
        self.assertArchiveError(build_raw([], sources_body=deflate(b"x" * 100)[:-2]))

    def test_sources_malformed(self):
        self.assertArchiveError(build_raw([], sources_raw=b"\x0b"))

    def test_source_without_kind(self):
        self.assertArchiveError(build_raw([], sources_raw=src_table({"data": b""})[:-2]
                                          + b"\x0a\x00"))

    def test_source_empty_url(self):
        self.assertArchiveError(build_raw([], sources_raw=src_table({"url": ""})))

    def test_pin_on_key_source(self):
        self.assertArchiveError(build_raw([], sources_raw=src_table({"key": "k", "size": 1})))

    def test_pin_on_data_source(self):
        self.assertArchiveError(build_raw([], sources_raw=src_table(
            {"data": b"", "modified_not_after": 0})))

    def test_weak_etag(self):
        self.assertArchiveError(build_raw([], sources_raw=src_table(
            {"url": "a", "etag": 'W/"x"'})))

    def test_cd_outside_file(self):
        self.assertArchiveError(build_raw([], eocd_override={"cd_off": 1 << 30}))

    def test_cd_bad_signature(self):
        self.assertArchiveError(build_raw([], cd_extra_records=b"PK\x01\x03" + b"\0" * 42))

    def test_cd_size_mismatch(self):
        good = build_raw([{"name": b"k"}])
        cd_size = struct.unpack_from("<I", good, len(good) - 44 + 12)[0]
        self.assertArchiveError(build_raw([{"name": b"k"}],
                                          eocd_override={"cd_size": cd_size - 1}))

    def test_index_record_in_unpaged_archive(self):
        from helpers import cd_record
        self.assertArchiveError(build_raw([], cd_extra_records=cd_record(b"__vz__/index")))

    def test_zip64_without_locator(self):
        self.assertArchiveError(build_raw([], eocd_override={"n": 0xFFFF}))


class TestPageIndexMalformed(Base):
    def paged(self, pages=None, pinned=(), raw=None, entries=None):
        entries = entries or [{"name": b"a", "body": b"1"}, {"name": b"b", "body": b"2"}]

        def ib(recs, cd):
            if raw is not None:
                return raw
            pg = pages
            if pg is None:
                pg = [(recs[0][0], 0, len(recs[0][1])),
                      (recs[1][0], len(recs[0][1]), len(recs[1][1]))]
            return proto.encode_cd_index(pg, list(pinned))
        return build_raw(entries, index_builder=ib)

    def test_valid_index_opens(self):
        a = self.open(self.paged(pinned=[(b"a", 31, 1, 1, 0)]))
        self.assertEqual(a.list(""), [b"a", b"b"])

    def test_does_not_decode(self):
        self.assertArchiveError(self.paged(raw=b"\x0f"))

    def test_page_length_zero(self):
        self.assertArchiveError(self.paged(pages=[(b"a", 0, 0)]))

    def test_page_outside_cd(self):
        self.assertArchiveError(self.paged(pages=[(b"a", 0, 10 ** 6)]))

    def test_pages_not_from_zero(self):
        self.assertArchiveError(self.paged(pages=[(b"a", 1, 10)]))

    def test_pages_not_contiguous(self):
        self.assertArchiveError(self.paged(pages=[(b"a", 0, 10), (b"b", 11, 10)]))

    def test_first_key_not_increasing(self):
        self.assertArchiveError(self.paged(pages=[(b"b", 0, 47), (b"a", 47, 47)]))

    def test_first_key_empty(self):
        self.assertArchiveError(self.paged(pages=[(b"", 0, 47)]))

    def test_pinned_key_empty(self):
        self.assertArchiveError(self.paged(pinned=[(b"", 31, 1, 1, 0)]))

    def test_pinned_twice(self):
        self.assertArchiveError(self.paged(pinned=[(b"a", 31, 1, 1, 0), (b"a", 31, 1, 1, 0)]))

    def test_pinned_format_entry(self):
        self.assertArchiveError(self.paged(pinned=[(b"__vz__/sources", 31, 1, 1, 0)]))

    def test_pinned_bad_method(self):
        self.assertArchiveError(self.paged(pinned=[(b"a", 31, 1, 1, 1)]))

    def test_pinned_body_outside_file(self):
        self.assertArchiveError(self.paged(pinned=[(b"a", 31, 1, 10 ** 6, 0)]))


# --------------------------------------------------------------- entry errors

def _bad_records():
    pl = encode_range({"data": b"x"})
    return {
        "method99": {"method": 99},
        "encrypted": {"flags": 0x801},
        "two_ref_blocks": {"extra": ref_extra(pl) + ref_extra(pl, False)},
        "extra_unparseable": {"extra": b"\x01\x00\x08"},
        "extra_overrun": {"extra": b"\x99\x99\x05\x00ab"},
        "deflated_reference": {"method": 8, "body": deflate(b""), "extra": ref_extra(pl)},
        "lho_ff_no_zip64": {"lho": 0xFFFFFFFF},
        "lho_ff_two_zip64": {"lho": 0xFFFFFFFF,
                             "extra": (struct.pack("<HHQ", 1, 8, 0)) * 2},
        "lho_ff_short_zip64": {"lho": 0xFFFFFFFF, "extra": struct.pack("<HHI", 1, 4, 0)},
    }


class TestEntryErrors(Base):
    def test_each_entry_error(self):
        for name, over in _bad_records().items():
            for paged in (False, True):
                with self.subTest(name, paged=paged):
                    e = {"name": b"bad", "body": b""}
                    e.update(over)
                    data = build_raw([{"name": b"aa", "body": b"ok"}, e,
                                      {"name": b"zz", "body": b"ok"}],
                                     index_builder=simple_pages if paged else None)
                    a = self.open(data)
                    for op in (a.classify, a.get, a.raw):
                        with self.assertRaises(EntryError):
                            op("bad")
                    self.assertEqual(a.get("aa"), b"ok")
                    self.assertEqual(a.get("zz"), b"ok")
                    self.assertEqual(a.list(""), [b"aa", b"bad", b"zz"])
                    # a key source naming it is a resolution error
                    self.assertEqual(a.classify("aa"), "bytes")

    def test_valid_zip64_offset_block(self):
        data = bytearray(build_raw([{"name": b"k", "body": b"v", "lho": 0xFFFFFFFF,
                                     "extra": struct.pack("<HHQ", 1, 8, 0)}]))
        self.assertEqual(self.open(bytes(data)).get("k"), b"v")

    def test_unparseable_page(self):
        def ib(recs, cd):
            return proto.encode_cd_index([(b"a", 0, len(recs[0][1])),
                                          (b"m", len(recs[0][1]), len(cd) - len(recs[0][1]))],
                                         [])
        data = bytearray(build_raw([{"name": b"a", "body": b"1"}, {"name": b"m", "body": b"2"},
                                    {"name": b"n", "body": b"3"}], index_builder=ib))
        # corrupt the signature of record "n" (third record in CD)
        cd_off = struct.unpack_from("<I", data, len(data) - 60 + 16)[0]
        rl = 46 + 1
        struct.pack_into("<I", data, cd_off + 2 * rl, 0x12345678)
        a = self.open(bytes(data))
        self.assertEqual(a.get("a"), b"1")
        for k in ("m", "n", "x", "mm"):
            with self.assertRaises(EntryError):
                a.classify(k)
        self.assertEqual(a.list("a"), [b"a"])
        with self.assertRaises(EntryError):
            a.list("")
        with self.assertRaises(EntryError):
            a.list("m")
        self.assertEqual(a.classify("0"), "missing")  # before first page


# --------------------------------------------------------------- body errors

class TestBodyErrors(Base):
    def cases(self):
        return {
            "outside_file": {"body": b"abc", "csize": 1 << 20},
            "bad_deflate": {"method": 8, "body": b"\xff\xfe", "usize": 2},
            "deflate_trailing": {"method": 8, "body": deflate(b"ab") + b"!", "usize": 2},
            "deflate_wrong_size": {"method": 8, "body": deflate(b"abc"), "usize": 2},
            "deflate_short": {"method": 8, "body": deflate(b"abc"), "usize": 4},
            "stored_size_mismatch": {"body": b"abc", "usize": 2},
        }

    def test_each_body_error(self):
        for name, over in self.cases().items():
            with self.subTest(name):
                e = {"name": b"bad"}
                e.update(over)
                data = build_raw([e, {"name": b"r", "body": b"",
                                      "extra": ref_extra(encode_range({"source": 0,
                                                                       "length": 1}))}],
                                 sources_raw=src_table({"key": "bad"}))
                a = self.open(data)
                self.assertEqual(a.classify("bad"), "bytes")
                with self.assertRaises(BodyError):
                    a.get("bad")
                with self.assertRaises(BodyError):
                    a.get("bad", ("range", 0, 1))
                with self.assertRaises(BodyError):
                    a.raw("bad")
                with self.assertRaises(ResolutionError):
                    a.get("r")

    def test_raw_of_reference_body_error(self):
        pl = encode_range({"data": b"x"})
        data = build_raw([{"name": b"r", "body": pl, "usize": 99, "extra": ref_extra(pl)}])
        a = self.open(data)
        self.assertEqual(a.get("r"), b"x")
        with self.assertRaises(BodyError):
            a.raw("r")


# --------------------------------------------------------------- payload errors

class TestPayloadErrors(Base):
    def test_each_payload_error(self):
        cases = {
            "malformed": (b"\x0b", True),
            "literal_with_source": (encode_range({"data": b"x"}) + b"\x08\x01", True),
            "literal_with_offset": (b"\x18\x01" + encode_range({"data": b"x"}), True),
            "source_out_of_bounds": (encode_range({"source": 1, "length": 1}), True),
            "oob_outside_window": (encode_concat([{"data": b"abc"},
                                                  {"source": 5, "offset": 0, "length": 0}]),
                                   False),
            "offset_length_overflow": (encode_range({"source": 0, "offset": 2 ** 63,
                                                     "length": 2 ** 63}), True),
            "total_overflow": (encode_concat([{"source": 0, "offset": 0, "length": 2 ** 63}] * 2),
                               False),
            "bad_utf8_unknown_ok?": None,
        }
        for name, c in cases.items():
            if c is None:
                continue
            payload, single = c
            with self.subTest(name):
                data = build_raw([{"name": b"r", "body": payload,
                                   "extra": ref_extra(payload, single)}],
                                 sources_raw=src_table({"data": b"0123"}))
                a = self.open(data)
                self.assertEqual(a.classify("r"), "reference")
                with self.assertRaises(PayloadError):
                    a.get("r", ("range", 0, 1))
                self.assertEqual(a.raw("r"), payload)


# --------------------------------------------------------------- resolution

class TestResolution(Base):
    def setUp(self):
        super().setUp()
        self.td.write("data/x.bin", b"0123456789")
        self.mtime = int(os.stat(self.td.p("data/x.bin")).st_mtime)

    def arch(self, sources, ranges_by_key, extra_entries=()):
        entries = list(extra_entries)
        for k, ranges in ranges_by_key.items():
            pl = encode_range(ranges[0]) if len(ranges) == 1 else encode_concat(ranges)
            entries.append({"name": k.encode(), "body": pl,
                            "extra": ref_extra(pl, len(ranges) == 1)})
        return self.open(build_raw(entries, sources_raw=src_table(*sources)))

    def test_successful_resolutions(self):
        a = self.arch(
            [{"url": "data/x.bin"}, {"url": "./data/../data/x.bin", "size": 10,
                                     "modified_not_after": self.mtime},
             {"url": "file://" + self.td.p("data/x.bin")}, {"key": "__vz__/h"},
             {"key": "s"}, {"data": b"ABC"}, {"url": "#frag"},
             {"url": "data/missing.bin"}, {"url": "data/x.bin", "etag": '"e"'}],
            {"a": [{"source": 0, "offset": 2, "length": 3}],
             "b": [{"source": 1, "offset": 0, "length": 10}],
             "c": [{"source": 2, "offset": 9, "length": 1}, {"source": 3, "offset": 1,
                                                             "length": 2},
                   {"source": 4, "offset": 0, "length": 2}, {"source": 5, "offset": 1,
                                                             "length": 2}],
             "self": [{"source": 6, "offset": 0, "length": 4}],
             # zero-length and out-of-window ranges are never resolved
             "lazy": [{"data": b"xy"}, {"source": 7, "offset": 0, "length": 0},
                      {"source": 8, "offset": 0, "length": 5}]},
            extra_entries=[{"name": b"__vz__/h", "body": b"hello"},
                           {"name": b"s", "method": 8, "body": deflate(b"stu"), "usize": 3}])
        self.assertEqual(a.get("a"), b"234")
        self.assertEqual(a.get("b"), b"0123456789")
        self.assertEqual(a.get("c"), b"9elstBC")
        self.assertEqual(a.get("self"), b"PK\x03\x04")
        self.assertEqual(a.get("lazy", ("range", 0, 2)), b"xy")
        self.assertEqual(a.get("lazy", ("suffix", 0)), b"")
        with self.assertRaises(ResolutionError):
            a.get("lazy")

    def test_each_resolution_error(self):
        cases = {
            "file_missing": {"url": "data/nope.bin"},
            "directory": {"url": "data/"},
            "file_too_short": ({"url": "data/x.bin"}, 8, 5),
            "unsupported_scheme": {"url": "s3://bucket/x"},
            "invalid_url_syntax": {"url": "a b"},
            "file_with_host": {"url": "file://example.com/x"},
            "file_with_query": {"url": "data/x.bin?"},
            "file_encoded_slash": {"url": "data%2Fx.bin"},
            "file_encoded_dotdot": {"url": "data/%2E%2E/data/x.bin"},
            "etag_on_file": {"url": "data/x.bin", "etag": '"x"'},
            "size_pin_fails": {"url": "data/x.bin", "size": 11},
            "mtime_pin_fails": {"url": "data/x.bin", "modified_not_after": 0},
            "key_missing": {"key": "nope"},
            "key_reference": {"key": "other"},
            "key_format_entry": {"key": "__vz__/sources"},
            "key_entry_error": {"key": "enc"},
            "key_too_short": ({"key": "__vz__/h"}, 3, 3),
            "data_too_short": ({"data": b"ab"}, 1, 2),
        }
        for name, c in cases.items():
            src, off, ln = (c, 0, 1) if isinstance(c, dict) else c
            with self.subTest(name):
                a = self.arch([src, {"data": b"zz"}],
                              {"r": [{"data": b"ok"}, {"source": 0, "offset": off, "length": ln}],
                               "other": [{"source": 1, "offset": 0, "length": 1}]},
                              extra_entries=[{"name": b"__vz__/h", "body": b"hello"},
                                             {"name": b"enc", "flags": 0x801}])
                with self.assertRaises(ResolutionError):
                    a.get("r")
                self.assertEqual(a.get("r", ("range", 0, 2)), b"ok")


class TestRequestErrors(Base):
    def test_request_error_precedes_everything(self):
        a = self.open(build_raw([{"name": b"k", "body": b"v"}, {"name": b"bad", "method": 99}]))
        for k in ("k", "missing", "__vz__/sources", "bad"):
            with self.assertRaises(RequestError):
                a.get(k, ("range", 2, 1))

    def test_cli_reports_classes(self):
        data = build_raw([{"name": b"bad", "method": 99}])
        path = self.save(data)
        res = read_queries(self.td, path, [{"op": "get", "key": "bad"},
                                           {"op": "get", "key": "x", "range": {"start": 1,
                                                                               "end": 0}},
                                           {"op": "list", "prefix": ""}])
        self.assertEqual([r.get("class") for r in res["results"]], ["entry", "request", None])
        self.assertEqual(res["results"][2]["keys"], ["bad"])
        res = read_queries(self.td, self.save(b"junk"), [{"op": "list", "prefix": ""}])
        self.assertEqual(res["open"]["ok"], False)
        self.assertEqual(res["open"]["class"], "archive")
        self.assertEqual(res["results"], [])

    def test_cli_invalid_queries(self):
        from helpers import run_cli
        path = self.save(build_raw([]))
        for q in ['[{"op":"nope"}]', '[{"op":"get"}]', '{}', '[{"op":"get","key":"k",'
                  '"range":{"start":1,"end":2,"suffix":1}}]', 'nope']:
            qp = self.td.write("bad.json", q)
            self.assertNotEqual(run_cli("read", path, qp).returncode, 0, q)


if __name__ == "__main__":
    unittest.main()
