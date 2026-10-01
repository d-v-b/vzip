"""Reader tests for the error classes of spec §8.4, using hand-crafted archives."""

import os
import struct
import tempfile
import unittest
import zlib

from helpers import concat_extra, craft, deflate, range_extra, simple_index
from vzip_impl import proto
from vzip_impl.errors import (ArchiveError, BodyError, EntryError, PayloadError, RequestError,
                              ResolutionError)
from vzip_impl.reader import Archive, Request
from vzip_impl.writer import WEntry, build_archive

P = proto.Pinned
R = proto.Range
S = proto.Source


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, data: bytes, name=None) -> str:
        self.n += 1
        path = os.path.join(self.d, name or f"a{self.n}.vzip")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def open(self, data: bytes) -> Archive:
        ar = Archive(self.put(data))
        self.addCleanup(ar.close)
        return ar


# ---------------------------------------------------------------------- archive errors


class TestArchiveErrors(Base):
    def bad(self, data):
        with self.assertRaises(ArchiveError):
            Archive(self.put(data))

    def test_valid_minimal(self):
        ar = self.open(craft([]))
        self.assertEqual(ar.list(""), [])
        ar = self.open(craft([], index_fn=simple_index()))
        self.assertEqual(ar.list(""), [])

    def test_too_short(self):
        self.bad(b"PK\x05\x06")

    def test_plain_zip(self):
        self.bad(craft([], comment=b""))

    def test_wrong_magic(self):
        self.bad(craft([], comment=b"zzzz/0" + b"\x00" * 16))

    def test_unsupported_version(self):
        self.bad(craft([], comment=b"vzip/1" + b"\x00" * 16))

    def test_wrong_comment_length(self):
        self.bad(craft([], comment=b"vzip/0" + b"\x00" * 17))

    def test_no_fallback_from_step_1(self):
        # A real EOCD (disk number 38, which readers ignore) with a valid vzip comment sits at
        # file_size-44. 16 bytes before it, a second EOCD signature makes step 1 match a record
        # whose comment length field is the real record's disk number (38). Its "comment" does not
        # start with vzip/, and readers must not fall back to step 2.
        good = craft([{"name": "k", "body": b"v"}])
        cd_size, cd_off = struct.unpack_from("<II", good, len(good) - 44 + 12)
        pre, vz = good[:-44], good[-22:]
        inner = struct.pack("<IHHHHIIH", 0x06054B50, 38, 0, 2, 2, cd_size, cd_off, 22)
        outer16 = struct.pack("<IHHHHI", 0x06054B50, 0, 0, 0, 0, 0)
        data = pre + outer16 + inner + vz
        self.bad(data)
        # control: without the outer signature, step 2 finds the real record
        ar = self.open(pre + b"\x00" * 16 + inner + vz)
        self.assertEqual(ar.get("k"), b"v")

    def test_zip64_locator_missing(self):
        self.bad(craft([], eocd_override={6: 0xFFFFFFFF}))

    def test_zip64_record_outside_file(self):
        good = craft([])
        cd_size, cd_off = struct.unpack_from("<II", good, len(good) - 44 + 12)
        pre = good[:-44]
        loc = struct.pack("<IIQI", 0x07064B50, 0, 10 ** 9, 1)
        eocd = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 1, 1, cd_size, 0xFFFFFFFF, 22)
        self.bad(pre + loc + eocd + good[-22:])

    def zip64(self, sig=0x06064B50, size=44, entries=None):
        """An archive with a zip64 end record, optionally broken."""
        good = craft([{"name": "a", "body": b"x"}])
        cd_size, cd_off = struct.unpack_from("<II", good, len(good) - 44 + 12)
        pre = good[:-44]
        z64_off = len(pre)
        n = 2 if entries is None else entries
        z = struct.pack("<IQHHIIQQQQ", sig, size, 45, 45, 0, 0, n, n, cd_size, cd_off)
        loc = struct.pack("<IIQI", 0x07064B50, 0, z64_off, 1)
        eocd = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 0xFFFF, 0xFFFF, cd_size, cd_off, 22)
        return pre + z + loc + eocd + good[-22:]

    def test_zip64_valid(self):
        ar = self.open(self.zip64())
        self.assertEqual(ar.get("a"), b"x")
        # entry counts are ignored
        ar = self.open(self.zip64(entries=12345))
        self.assertEqual(ar.list(""), ["a"])

    def test_zip64_bad_signature(self):
        self.bad(self.zip64(sig=0x06064B51))

    def test_zip64_bad_size(self):
        self.bad(self.zip64(size=56))

    def test_cd_outside_file(self):
        self.bad(craft([], eocd_override={6: 10 ** 6}))

    def test_sources_outside_file(self):
        self.bad(craft([], comment=b"vzip/0" + struct.pack("<QQ", 10 ** 6, 2)))

    def test_sources_not_deflate(self):
        self.bad(craft([], sources_body=b"\xff\xff\xff"))

    def test_sources_trailing_bytes(self):
        self.bad(craft([], sources_body=deflate(b"") + b"\x00"))

    def test_sources_truncated(self):
        self.bad(craft([], sources_body=deflate(proto.encode_source_table([S("data", b"x" * 50)]))[:-1]))

    def test_source_table_malformed(self):
        self.bad(craft([], sources_raw=b"\x0b"))

    def test_source_without_kind(self):
        self.bad(craft([], sources_raw=b"\x0a\x00"))

    def test_source_empty_url(self):
        self.bad(craft([], sources=[S("url", "")]))

    def test_pin_on_key_source(self):
        self.bad(craft([], sources=[S("key", "a", size=3)]))

    def test_pin_on_data_source(self):
        self.bad(craft([], sources=[S("data", b"", etag='"x"')]))

    def test_weak_etag(self):
        self.bad(craft([], sources=[S("url", "x", etag='W/"x"')]))

    def test_etag_with_space(self):
        self.bad(craft([], sources=[S("url", "x", etag='"a b"')]))

    def test_cd_garbage(self):
        self.bad(craft([], cd_suffix=b"\x00" * 50))

    def test_cd_truncated_record(self):
        self.bad(craft([], cd_suffix=b"PK\x01\x02" + b"\x00" * 10))

    def test_index_record_in_unpaged_archive(self):
        self.bad(craft([{"name": "__vz__/index", "body": b""}]))

    def test_not_checked_at_open(self):
        # bad URL syntax, unreachable URL, entry errors: all fine at open
        ar = self.open(craft([{"name": "a", "body": b"", "method": 99}],
                             sources=[S("url", "a b"), S("url", "http://127.0.0.1:1/x"), S("key", "zz")]))
        self.assertEqual(ar.list(""), ["a"])

    # ---- page index (§7.2)

    def bad_index(self, index_fn=None, records=None, index_body=None):
        recs = records if records is not None else [{"name": "a", "body": b"1"}, {"name": "b", "body": b"2"}]
        self.bad(craft(recs, index_fn=index_fn, index_body=index_body))

    def idx(self, pages=None, pinned=()):
        def fn(infos):
            ps = pages(infos) if pages else [proto.Page(n.decode(), o, l) for n, o, l in infos]
            return proto.encode_cd_index(proto.CdIndex(ps, list(pinned)))
        return fn

    def test_index_valid(self):
        ar = self.open(craft([{"name": "a", "body": b"1"}, {"name": "b", "body": b"2"}], index_fn=self.idx()))
        self.assertEqual(ar.list(""), ["a", "b"])

    def test_index_does_not_inflate(self):
        self.bad_index(index_body=b"\xff")

    def test_index_does_not_decode(self):
        self.bad_index(index_body=deflate(b"\x0f"))

    def test_index_page_length_zero(self):
        self.bad_index(self.idx(lambda i: [proto.Page("a", 0, 0), proto.Page("b", 0, i[0][2] + i[1][2])]))

    def test_index_page_outside_cd(self):
        self.bad_index(self.idx(lambda i: [proto.Page("a", 0, 10 ** 6)]))

    def test_index_not_contiguous(self):
        self.bad_index(self.idx(lambda i: [proto.Page("a", 0, i[0][2]), proto.Page("b", i[0][2] + 1, 1)]))

    def test_index_not_from_zero(self):
        self.bad_index(self.idx(lambda i: [proto.Page("a", 1, i[0][2])]))

    def test_index_first_key_not_increasing(self):
        self.bad_index(self.idx(lambda i: [proto.Page("b", 0, i[0][2]), proto.Page("a", i[0][2], i[1][2])]))

    def test_index_first_key_equal(self):
        self.bad_index(self.idx(lambda i: [proto.Page("a", 0, i[0][2]), proto.Page("a", i[0][2], i[1][2])]))

    def test_index_first_key_empty(self):
        self.bad_index(self.idx(lambda i: [proto.Page("", 0, i[0][2] + i[1][2])]))

    def test_index_pinned_key_empty(self):
        self.bad_index(self.idx(pinned=[P("", 0, 0, 0, 0)]))

    def test_index_pinned_twice(self):
        self.bad_index(self.idx(pinned=[P("a", 0, 0, 0, 0), P("a", 0, 0, 0, 0)]))

    def test_index_pinned_format_entry(self):
        self.bad_index(self.idx(pinned=[P("__vz__/sources", 0, 0, 0, 0)]))

    def test_index_pinned_method(self):
        self.bad_index(self.idx(pinned=[P("a", 0, 0, 0, 2)]))

    def test_index_pinned_outside_file(self):
        self.bad_index(self.idx(pinned=[P("a", 10 ** 6, 1, 1, 0)]))

    def test_index_pinned_uint32_overflow(self):
        raw = b"\x12\x06\x28" + proto.enc_varint(1 << 32)
        self.bad_index(index_body=deflate(raw))


# ---------------------------------------------------------------------- entry errors


class TestEntryErrors(Base):
    def check(self, rec):
        ar = self.open(craft([rec, {"name": "ok", "body": b"fine"}]))
        name = rec["name"]
        for fn in (ar.classify, ar.get, ar.raw):
            with self.assertRaises(EntryError):
                fn(name)
        self.assertIn(name, ar.list(""))
        self.assertEqual(ar.get("ok"), b"fine")
        return ar

    def test_extra_does_not_parse(self):
        self.check({"name": "e", "extra": b"\x01\x02\x03"})

    def test_extra_block_overruns(self):
        self.check({"name": "e", "extra": b"\x99\x99\x05\x00ab"})

    def test_two_reference_blocks(self):
        self.check({"name": "e", "extra": range_extra(R(data=b"")) + concat_extra([])})

    def test_two_range_blocks(self):
        self.check({"name": "e", "extra": range_extra(R(data=b"")) * 2})

    def test_method_unsupported(self):
        self.check({"name": "e", "method": 12})

    def test_encrypted(self):
        self.check({"name": "e", "flags": 0x801})

    def test_reference_with_method_8(self):
        self.check({"name": "e", "method": 8, "body": deflate(b""), "raw_len": 0, "extra": range_extra(R(data=b""))})

    def test_zip64_offset_without_block(self):
        self.check({"name": "e", "loff": 0xFFFFFFFF})

    def test_zip64_block_too_short(self):
        self.check({"name": "e", "loff": 0xFFFFFFFF, "extra": b"\x01\x00\x04\x00\x00\x00\x00\x00"})

    def test_zip64_two_blocks(self):
        z = struct.pack("<HHQ", 1, 8, 0)
        self.check({"name": "e", "loff": 0xFFFFFFFF, "extra": z + z})

    def test_zip64_offset_valid(self):
        z = struct.pack("<HHQ", 1, 8, 0)
        ar = self.open(craft([{"name": "e", "body": b"hey", "loff": 0xFFFFFFFF, "extra": z}]))
        self.assertEqual(ar.get("e"), b"hey")

    def test_unknown_extra_blocks_ignored(self):
        ex = b"\x55\x54\x01\x00\x00" + range_extra(R(data=b"zz")) + b"\x99\x99\x00\x00"
        ar = self.open(craft([{"name": "e", "extra": ex}]))
        self.assertEqual((ar.classify("e"), ar.get("e")), ("reference", b"zz"))

    def test_page_cannot_be_parsed(self):
        def fn(infos):
            (na, oa, la), (nb, ob, lb), (nc, oc, lc) = infos
            # page 1 splits record b: covers a + 4 bytes of b; page 2 covers rest of b; page 3 c
            return proto.encode_cd_index(proto.CdIndex([
                proto.Page("a", 0, la), proto.Page("b", ob, 4), proto.Page("b2", ob + 4, lb - 4),
                proto.Page("c", oc, lc)]))
        ar = self.open(craft([{"name": "a", "body": b"1"}, {"name": "b", "body": b"2"},
                              {"name": "c", "body": b"3"}], index_fn=fn))
        self.assertEqual(ar.get("a"), b"1")
        self.assertEqual(ar.get("c"), b"3")
        for fn_ in (ar.classify, ar.get, ar.raw):
            with self.assertRaises(EntryError):
                fn_("b")
        with self.assertRaises(EntryError):
            ar.list("")
        with self.assertRaises(EntryError):
            ar.list("b")
        self.assertEqual(ar.list("a"), ["a"])
        self.assertEqual(ar.list("c"), ["c"])


# ---------------------------------------------------------------------- body errors


class TestBodyErrors(Base):
    def check(self, rec, raw_only=False):
        ar = self.open(craft([rec, {"name": "ok", "body": b"x"}], sources=[S("key", rec["name"])]
                             if not raw_only else []))
        if not raw_only:
            with self.assertRaises(BodyError):
                ar.get(rec["name"])
            with self.assertRaises(BodyError):
                ar.get(rec["name"], Request("range", 0, 1))
            self.assertEqual(ar.classify(rec["name"]), "bytes")
        with self.assertRaises(BodyError):
            ar.raw(rec["name"])
        return ar

    def test_body_outside_file(self):
        self.check({"name": "b", "body": b"abc", "csize": 10 ** 6, "usize": 10 ** 6})

    def test_deflate_garbage(self):
        self.check({"name": "b", "method": 8, "body": b"\xff\xfe", "raw_len": 2})

    def test_deflate_trailing(self):
        self.check({"name": "b", "method": 8, "body": deflate(b"abc") + b"\x00", "raw_len": 3})

    def test_deflate_truncated(self):
        self.check({"name": "b", "method": 8, "body": deflate(b"abc" * 100)[:-2], "raw_len": 300})

    def test_deflate_wrong_size(self):
        self.check({"name": "b", "method": 8, "body": deflate(b"abc"), "raw_len": 4})

    def test_stored_sizes_differ(self):
        self.check({"name": "b", "body": b"abc", "raw_len": 4})

    def test_reference_raw_outside_file(self):
        ar = self.check({"name": "r", "extra": range_extra(R(data=b"ok")), "body": b"zz",
                         "csize": 10 ** 6, "usize": 10 ** 6}, raw_only=True)
        self.assertEqual(ar.get("r"), b"ok")  # get never reads the body

    def test_key_source_with_body_error(self):
        ar = self.open(craft([{"name": "__vz__/h", "method": 8, "body": b"\xff", "raw_len": 1},
                              {"name": "r", "extra": range_extra(R(0, 0, 1))}], sources=[S("key", "__vz__/h")]))
        with self.assertRaises(ResolutionError):
            ar.get("r")

    def test_crc_not_checked(self):
        ar = self.open(craft([{"name": "b", "body": b"abc", "crc": 1}]))
        self.assertEqual(ar.get("b"), b"abc")

    def test_stored_window_only(self):
        # a STORED body is read only within the window but its bounds are judged as a whole
        ar = self.open(craft([{"name": "b", "body": b"abc"}]))
        self.assertEqual(ar.get("b", Request("range", 1, 2)), b"b")


# ---------------------------------------------------------------------- payload errors


class TestPayloadErrors(Base):
    def check(self, extra, sources=()):
        ar = self.open(craft([{"name": "r", "extra": extra}], sources=list(sources)))
        self.assertEqual(ar.classify("r"), "reference")
        for req in (Request(), Request("range", 0, 0), Request("suffix", 0)):
            with self.assertRaises(PayloadError):
                ar.get("r", req)
        self.assertEqual(ar.raw("r"), b"")

    def test_malformed_protobuf(self):
        self.check(b"\x76\x7a\x01\x00\x0b")

    def test_malformed_concat_part(self):
        self.check(b"\x77\x7a\x03\x00\x0a\x01\x0b")

    def test_literal_with_source(self):
        p = b"\x08\x01\x2a\x00"
        self.check(struct.pack("<HH", 0x7A76, len(p)) + p, [S("data", b"x"), S("data", b"y")])

    def test_literal_with_length(self):
        p = b"\x20\x01\x2a\x00"
        self.check(struct.pack("<HH", 0x7A76, len(p)) + p)

    def test_source_out_of_range(self):
        self.check(range_extra(R(1, 0, 5)), [S("data", b"abcde")])

    def test_source_out_of_range_zero_length_outside_window(self):
        self.check(concat_extra([R(data=b"abc"), R(5, 0, 0)]), [S("data", b"abcde")])

    def test_no_sources(self):
        self.check(range_extra(R(0, 0, 0)))

    def test_offset_plus_length_overflow(self):
        self.check(range_extra(R(0, 1 << 63, 1 << 63)), [S("data", b"")])

    def test_concat_size_overflow(self):
        self.check(concat_extra([R(0, 0, 1 << 63), R(0, 0, 1 << 63)]), [S("data", b"")])


# ---------------------------------------------------------------------- resolution errors


class TestResolution(Base):
    def setUp(self):
        super().setUp()
        self.ext = os.path.join(self.d, "sub", "obj.bin")
        os.makedirs(os.path.dirname(self.ext))
        with open(self.ext, "wb") as f:
            f.write(b"0123456789")
        os.utime(self.ext, (1_000_000_000, 1_000_000_000.5))

    def arch(self, sources, ranges, name=None):
        extra = range_extra(ranges[0]) if len(ranges) == 1 else concat_extra(ranges)
        data = craft([{"name": "__vz__/h", "body": b"HEAD"}, {"name": "r", "extra": extra}], sources=sources)
        ar = Archive(self.put(data, name))
        self.addCleanup(ar.close)
        return ar

    def test_resolution_successes(self):
        cases = [
            ([S("url", "sub/obj.bin")], [R(0, 2, 3)], b"234"),
            ([S("url", "./sub/../sub/obj.bin")], [R(0, 0, 10)], b"0123456789"),
            ([S("url", "sub/obj.bin", size=10, modified_not_after=1_000_000_000)], [R(0, 9, 1)], b"9"),
            ([S("url", "sub/obj.bin", modified_not_after=2_000_000_000)], [R(0, 0, 1)], b"0"),
            ([S("url", "file://" + self.ext.replace(" ", "%20"))], [R(0, 0, 2)], b"01"),
            ([S("key", "__vz__/h")], [R(0, 1, 2)], b"EA"),
            ([S("data", b"abc")], [R(0, 1, 2)], b"bc"),
            # zero-length / non-overlapping ranges are not resolved
            ([S("url", "missing.bin")], [R(0, 0, 0), R(data=b"q")], b"q"),
            ([S("url", "a b")], [R(0, 5, 0)], b""),
            ([S("key", "nope")], [R(0, 0, 0)], b""),
        ]
        for srcs, rngs, want in cases:
            with self.subTest(srcs=srcs, rngs=rngs):
                self.assertEqual(self.arch(srcs, rngs).get("r"), want)
        # window excludes a broken range
        ar = self.arch([S("url", "missing.bin")], [R(data=b"abc"), R(0, 0, 5)])
        self.assertEqual(ar.get("r", Request("range", 0, 3)), b"abc")
        with self.assertRaises(ResolutionError):
            ar.get("r", Request("range", 2, 4))
        # fragment-only URL resolves to the archive itself
        ar = self.arch([S("url", "#self")], [R(0, 0, 4)])
        self.assertEqual(ar.get("r"), b"PK\x03\x04")

    def test_symlinks_not_resolved(self):
        other = os.path.join(self.d, "other")
        os.makedirs(os.path.join(other, "sub"))
        with open(os.path.join(other, "sub", "obj.bin"), "wb") as f:
            f.write(b"OTHER")
        data = build_archive([WEntry("r", ranges=[R(0, 0, 5)])], [S("url", "sub/obj.bin")], None)
        real = self.put(data, "real.vzip")
        link = os.path.join(other, "link.vzip")
        os.symlink(real, link)
        with Archive(link) as ar:
            self.assertEqual(ar.get("r"), b"OTHER")
        with Archive(real) as ar:
            self.assertEqual(ar.get("r"), b"01234")

    def bad(self, sources, ranges):
        ar = self.arch(sources, ranges)
        with self.assertRaises(ResolutionError):
            ar.get("r")

    def test_missing_file(self):
        self.bad([S("url", "nope.bin")], [R(0, 0, 1)])

    def test_directory(self):
        self.bad([S("url", "sub")], [R(0, 0, 1)])

    def test_invalid_url_syntax(self):
        self.bad([S("url", "a b")], [R(0, 0, 1)])

    def test_unsupported_scheme(self):
        self.bad([S("url", "ftp://h/x")], [R(0, 0, 1)])

    def test_file_with_query(self):
        self.bad([S("url", "sub/obj.bin?")], [R(0, 0, 1)])

    def test_file_with_host(self):
        self.bad([S("url", "file://example.com/x")], [R(0, 0, 1)])

    def test_file_encoded_dotdot(self):
        self.bad([S("url", "sub/%2E%2E/sub/obj.bin")], [R(0, 0, 1)])

    def test_file_too_short(self):
        self.bad([S("url", "sub/obj.bin")], [R(0, 5, 6)])

    def test_size_pin(self):
        self.bad([S("url", "sub/obj.bin", size=11)], [R(0, 0, 1)])

    def test_etag_pin_on_file(self):
        self.bad([S("url", "sub/obj.bin", etag='"x"')], [R(0, 0, 1)])

    def test_mtime_pin(self):
        self.bad([S("url", "sub/obj.bin", modified_not_after=999_999_999)], [R(0, 0, 1)])

    def test_key_source_missing(self):
        # writer would reject; craft directly
        ar = self.open(craft([{"name": "r", "extra": range_extra(R(0, 0, 1))}], sources=[S("key", "zz")]))
        with self.assertRaises(ResolutionError):
            ar.get("r")

    def test_key_source_reference(self):
        ar = self.open(craft([{"name": "q", "extra": range_extra(R(data=b"a"))},
                              {"name": "r", "extra": range_extra(R(0, 0, 1))}], sources=[S("key", "q")]))
        with self.assertRaises(ResolutionError):
            ar.get("r")

    def test_key_source_format_entry(self):
        ar = self.open(craft([{"name": "r", "extra": range_extra(R(0, 0, 1))}],
                             sources=[S("key", "__vz__/sources")]))
        with self.assertRaises(ResolutionError):
            ar.get("r")

    def test_key_source_entry_error(self):
        ar = self.open(craft([{"name": "q", "method": 3}, {"name": "r", "extra": range_extra(R(0, 0, 1))}],
                             sources=[S("key", "q")]))
        with self.assertRaises(ResolutionError):
            ar.get("r")

    def test_key_source_too_short(self):
        self.bad([S("key", "__vz__/h")], [R(0, 2, 3)])

    def test_data_source_too_short(self):
        self.bad([S("data", b"abc")], [R(0, 2, 2)])


class TestRequestErrors(Base):
    def test_start_after_end(self):
        ar = self.open(craft([{"name": "a", "body": b"x"}, {"name": "e", "method": 9}]))
        for k in ("a", "missing", "__vz__/sources", "e"):
            with self.assertRaises(RequestError):
                ar.get(k, Request("range", 2, 1))


class TestPagedLookup(Base):
    def test_lookup_and_list(self):
        names = ["a", "b/1", "b/2", "b/3", "c", "__vz__/x", "d"]
        recs = [{"name": n, "body": n.encode()} for n in names]
        ar = self.open(craft(recs, index_fn=simple_index(2, [P("zz", 0, 0, 0, 0)])))
        self.assertEqual(ar.list(""), ["a", "b/1", "b/2", "b/3", "c", "d", "zz"])
        self.assertEqual(ar.list("b/"), ["b/1", "b/2", "b/3"])
        self.assertEqual(ar.get("b/3"), b"b/3")
        self.assertEqual(ar.raw("__vz__/x"), b"__vz__/x")
        self.assertEqual(ar.classify("zz"), "bytes")
        self.assertEqual(ar.get("zz"), b"")  # pinned: values from the index (offset 0, size 0)
        self.assertEqual(ar.classify("A"), "missing")  # before the first page

    def test_record_outside_its_page_range(self):
        # pages: [a..) holds "a","c"; second page first_key "b" holds "d". Lookup of "c" goes to page 2.
        def fn(infos):
            (na, oa, la), (nc, oc, lc), (nd, od, ld) = infos
            return proto.encode_cd_index(proto.CdIndex([proto.Page("a", 0, la + lc), proto.Page("b", od, ld)]))
        ar = self.open(craft([{"name": n, "body": n.encode()} for n in "acd"], index_fn=fn))
        self.assertEqual(ar.classify("c"), "missing")
        self.assertEqual(ar.list(""), ["a", "d"])


if __name__ == "__main__":
    unittest.main()
