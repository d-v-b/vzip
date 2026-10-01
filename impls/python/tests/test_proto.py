import unittest

from helpers import ROOT  # noqa: F401
from vzip_impl import proto
from vzip_impl.proto import Malformed, decode_range, decode_source, decode_cd_index, enc_varint


def tag(f, wt):
    return enc_varint((f << 3) | wt)


class TestRoundTrip(unittest.TestCase):
    def test_encode_decode(self):
        cases = [
            proto.Range(),
            proto.Range(source=3, offset=10, length=4),
            proto.Range(data=b""),
            proto.Range(data=b"\x00\xff"),
            proto.Range(offset=(1 << 64) - 2, length=1),
        ]
        for r in cases:
            self.assertEqual(decode_range(proto.encode_range(r)), r)
        self.assertEqual(proto.encode_range(proto.Range()), b"")
        self.assertEqual(proto.encode_range(proto.Range(data=b"")), b"\x2a\x00")
        self.assertEqual(proto.decode_concat(proto.encode_concat(cases)), cases)
        # empty Range part in a Concat is still emitted
        self.assertEqual(proto.encode_concat([proto.Range()]), b"\x0a\x00")

        srcs = [
            proto.Source("url", "a.bin", size=0, etag='"x"', modified_not_after=-5),
            proto.Source("key", "__vz__/h"),
            proto.Source("data", b""),
            proto.Source("url", "x", modified_not_after=0),
        ]
        self.assertEqual(proto.decode_source_table(proto.encode_source_table(srcs)), srcs)
        # negative int64 is a 10-byte varint
        enc = proto.encode_source(proto.Source("url", "u", modified_not_after=-1))
        self.assertEqual(enc[-11:], b"\x30" + b"\xff" * 9 + b"\x01")

        idx = proto.CdIndex([proto.Page("a", 0, 10), proto.Page("b", 10, 5)],
                            [proto.Pinned("z", 100, 3, 3, 0)])
        self.assertEqual(decode_cd_index(proto.encode_cd_index(idx)), idx)

    def test_decoding_rules(self):
        # unknown fields of all four wire types are skipped; reserved field 2 is skipped
        unk = (tag(9, 0) + b"\x05" + tag(10, 1) + b"\x00" * 8 + tag(11, 2) + b"\x01x" + tag(12, 5)
               + b"\x00" * 4 + tag(2, 2) + b"\x00")
        self.assertEqual(decode_range(unk + tag(3, 0) + b"\x07"), proto.Range(offset=7))
        # last occurrence wins; non-minimal varint accepted
        self.assertEqual(decode_range(tag(3, 0) + b"\x01" + tag(3, 0) + b"\x82\x80\x00"), proto.Range(offset=2))
        # last oneof member wins
        s = decode_source(tag(1, 2) + b"\x01u" + tag(3, 2) + b"\x01d")
        self.assertEqual((s.kind, s.value), ("data", b"d"))
        # maximum field number accepted
        decode_range(enc_varint(((1 << 29) - 1) << 3) + b"\x00")
        # 10-byte varint with max value
        self.assertEqual(decode_range(tag(3, 0) + b"\xff" * 9 + b"\x01").offset, (1 << 64) - 1)


class TestMalformed(unittest.TestCase):
    def bad(self, b, fn=decode_range):
        with self.assertRaises(Malformed):
            fn(b)

    def test_wire_type_3(self):
        self.bad(tag(9, 3))

    def test_wire_type_4(self):
        self.bad(tag(9, 4))

    def test_wire_type_6(self):
        self.bad(tag(9, 6))

    def test_wire_type_7(self):
        self.bad(tag(9, 7))

    def test_field_zero(self):
        self.bad(b"\x00\x00")

    def test_field_too_large(self):
        self.bad(enc_varint((1 << 29) << 3) + b"\x00")

    def test_truncated_varint(self):
        self.bad(tag(3, 0) + b"\x80")

    def test_truncated_i64(self):
        self.bad(tag(9, 1) + b"\x00" * 7)

    def test_len_past_end(self):
        self.bad(tag(5, 2) + b"\x05abc")

    def test_nested_len_past_enclosing(self):
        # Concat part whose inner LEN overruns the part
        inner = tag(5, 2) + b"\x05ab"
        self.bad(tag(1, 2) + enc_varint(len(inner)) + inner + b"xyz", proto.decode_concat)

    def test_varint_too_long(self):
        self.bad(tag(3, 0) + b"\x80" * 10 + b"\x00")

    def test_varint_overflow(self):
        self.bad(tag(3, 0) + b"\xff" * 9 + b"\x02")

    def test_wrong_wire_type(self):
        self.bad(tag(3, 2) + b"\x00")

    def test_uint32_overflow(self):
        self.bad(tag(1, 0) + enc_varint(1 << 32))

    def test_invalid_utf8(self):
        self.bad(tag(1, 2) + b"\x01\xff", decode_source)


if __name__ == "__main__":
    unittest.main()
