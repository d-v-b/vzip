"""Each invalid description is rejected: non-zero exit, no output file."""

import os
import unittest

from helpers import TmpDir, run_cli

BASE_ENTRY = {"key": "k", "bytes": "00"}


def d(**kw):
    out = {"sources": [], "entries": [BASE_ENTRY]}
    out.update(kw)
    return out


INVALID = {
    "empty_key": d(entries=[{"key": "", "bytes": ""}]),
    "lone_surrogate_key": '{"entries":[{"key":"\\ud800","bytes":""}]}',
    "duplicate_key": d(entries=[BASE_ENTRY, BASE_ENTRY]),
    "format_key_sources": d(entries=[{"key": "__vz__/sources", "bytes": ""}]),
    "format_key_index": d(entries=[{"key": "__vz__/index", "bytes": ""}]),
    "key_too_long": d(entries=[{"key": "x" * 65536, "bytes": ""}]),
    "source_out_of_range": d(entries=[{"key": "r", "ranges": [{"source": 0, "length": 1}]}]),
    "empty_range_no_sources": d(entries=[{"key": "r", "ranges": [{}]}]),
    "source_index_too_big": d(sources=[{"data": ""}],
                              entries=[{"key": "r", "ranges": [{"source": 1}]}]),
    "empty_url": d(sources=[{"url": ""}]),
    "bad_url_space": d(sources=[{"url": "a b"}]),
    "bad_url_non_ascii": d(sources=[{"url": "é.bin"}]),
    "bad_url_pct": d(sources=[{"url": "a%zz"}]),
    "key_source_absent": d(sources=[{"key": "nope"}]),
    "key_source_reference": d(sources=[{"key": "r"}, {"data": "00"}],
                              entries=[{"key": "r", "ranges": [{"source": 1, "length": 1}]}]),
    "key_source_format": d(sources=[{"key": "__vz__/sources"}]),
    "pin_on_key": d(sources=[{"key": "k", "size": 1}]),
    "pin_on_data": d(sources=[{"data": "", "modified_not_after": 0}]),
    "weak_etag": d(sources=[{"url": "a", "etag": "W/\"x\""}]),
    "unquoted_etag": d(sources=[{"url": "a", "etag": "x"}]),
    "etag_with_space": d(sources=[{"url": "a", "etag": "\"a b\""}]),
    "payload_too_big": d(sources=[{"data": "00"}],
                         entries=[{"key": "r", "ranges": [{"data": "00" * 65520}]}]),
    "pinned_without_page_index": d(entries=[{"key": "k", "bytes": "", "pinned": True}]),
    "pinned_reference": d(page_size=1, sources=[{"data": "00"}],
                          entries=[{"key": "r", "ranges": [], "pinned": True}]),
    "compress_on_reference": d(entries=[{"key": "r", "ranges": [], "compress": True}]),
    "both_bytes_and_ranges": d(entries=[{"key": "r", "ranges": [], "bytes": ""}]),
    "neither_bytes_nor_ranges": d(entries=[{"key": "r"}]),
    "mixed_range": d(sources=[{"data": "00"}],
                     entries=[{"key": "r", "ranges": [{"data": "00", "source": 0}]}]),
    "page_size_zero": d(page_size=0),
    "page_size_float": d(page_size=1.0),
    "page_size_bool": d(page_size=True),
    "mirror_null": d(mirror=None),
    "mirror_int": d(mirror=1),
    "compress_null": d(entries=[{"key": "k", "bytes": "", "compress": None}]),
    "uppercase_hex": d(entries=[{"key": "k", "bytes": "FF"}]),
    "odd_hex": d(entries=[{"key": "k", "bytes": "f"}]),
    "float_offset": d(sources=[{"data": "00"}],
                      entries=[{"key": "r", "ranges": [{"offset": 1.0}]}]),
    "negative_length": d(sources=[{"data": "00"}],
                         entries=[{"key": "r", "ranges": [{"length": -1}]}]),
    "source_two_kinds": d(sources=[{"url": "a", "data": "00"}]),
    "source_no_kind": d(sources=[{}]),
    "size_pin_null": d(sources=[{"url": "a", "size": None}]),
    "entries_not_list": d(entries={}),
    "not_json": "{",
    "nan": '{"page_size": NaN}',
}
INVALID = {k: v for k, v in INVALID.items() if v is not None}


class TestWriterRejects(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = TmpDir()

    @classmethod
    def tearDownClass(cls):
        cls.td.cleanup()

    def _check_invalid(self, name, desc):
        import json
        text = desc if isinstance(desc, str) else json.dumps(desc)
        dp = self.td.write(f"{name}.json", text)
        out = self.td.p(f"{name}.vzip")
        r = run_cli("write", dp, out)
        self.assertNotEqual(r.returncode, 0, f"{name}: accepted")
        self.assertTrue(r.stderr.strip(), name)
        self.assertFalse(os.path.exists(out), name)
        self.assertEqual([f for f in os.listdir(self.td.path) if f.startswith(".vzip-")], [])


def _make(name, desc):
    def t(self):
        self._check_invalid(name, desc)
    return t


for _name, _desc in INVALID.items():
    setattr(TestWriterRejects, f"test_rejects_{_name}", _make(_name, _desc))


class TestWriterLibraryRejects(unittest.TestCase):
    def test_rejects_offset_plus_length_overflow(self):
        import io
        from vzip_impl import Entry, Source, WriteError, write_archive
        with self.assertRaises(WriteError):
            write_archive(io.BytesIO(), [Entry("r", ranges=[
                {"source": 0, "offset": 2 ** 63, "length": 2 ** 63}])], [Source("data", b"")])

    def test_rejects_total_size_overflow(self):
        import io
        from vzip_impl import Entry, Source, WriteError, write_archive
        r = {"source": 0, "offset": 0, "length": 2 ** 63}
        with self.assertRaises(WriteError):
            write_archive(io.BytesIO(), [Entry("r", ranges=[r, r])], [Source("data", b"")])


class TestWriterAccepts(unittest.TestCase):
    def test_accepts_edge_cases(self):
        import json
        td = TmpDir()
        try:
            ok = [
                d(),
                d(page_size=None, mirror=False, unknown_member=None),
                d(entries=[{"key": "r", "ranges": [], "compress": False, "x": None}]),
                d(entries=[{"key": "__vz__/h", "bytes": "", "pinned": True}], page_size=1),
                d(sources=[{"url": "#x", "size": 0, "etag": "\"\"",
                            "modified_not_after": -5}]),
                d(sources=[{"data": "00"}],
                  entries=[{"key": "r", "ranges": [{"data": "00" * 65500}]}]),
                d(sources=[{"key": "k"}], entries=[BASE_ENTRY, {"key": "r", "ranges": [{}]}]),
            ]
            for i, desc in enumerate(ok):
                dp = td.write(f"{i}.json", json.dumps(desc))
                r = run_cli("write", dp, td.p(f"{i}.vzip"))
                self.assertEqual(r.returncode, 0, f"{i}: {r.stderr}")
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
