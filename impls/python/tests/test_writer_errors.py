import json
import os
import unittest

from helpers import cli, tmpdir
from vzip_impl.cli import InvalidInput, parse_description
from vzip_impl.errors import WriteError
from vzip_impl.writer import build_archive


def rejects(desc):
    try:
        e, s, p, m = parse_description(desc)
        build_archive(e, s, p, m)
    except (InvalidInput, WriteError):
        return True
    return False


B = {"key": "k", "bytes": "00"}


class WriterRejects(unittest.TestCase):
    def check(self, desc):
        self.assertTrue(rejects(desc), desc)

    def test_cli_rejection_creates_no_file(self):
        with tmpdir() as d:
            with open(os.path.join(d, "d.json"), "w") as fh:
                json.dump({"entries": [B, B]}, fh)
            p = cli("write", os.path.join(d, "d.json"), os.path.join(d, "o.vzip"))
            self.assertNotEqual(p.returncode, 0)
            self.assertTrue(p.stderr)
            self.assertEqual(os.listdir(d), ["d.json"])

    def test_empty_key(self):
        self.check({"entries": [{"key": "", "bytes": ""}]})

    def test_lone_surrogate_key(self):
        self.check({"entries": [{"key": "\ud800", "bytes": ""}]})

    def test_duplicate_key(self):
        self.check({"entries": [B, B]})

    def test_format_entry_key(self):
        self.check({"entries": [{"key": "__vz__/sources", "bytes": ""}]})
        self.check({"entries": [{"key": "__vz__/index", "bytes": ""}]})

    def test_source_index_out_of_range(self):
        self.check({"entries": [{"key": "r", "ranges": [{}]}]})
        self.check({"sources": [{"data": "00"}], "entries": [{"key": "r", "ranges": [{"source": 1}]}]})

    def test_bad_url(self):
        for u in ["", "a b", "é", "http://[::1", "%zz", "a:b c", "1a:b" + "\x00"]:
            self.check({"sources": [{"url": u}]})

    def test_key_source_absent_reference_or_format(self):
        self.check({"sources": [{"key": "nope"}]})
        self.check({"sources": [{"key": "__vz__/sources"}]})
        self.check({"sources": [{"key": "r"}], "entries": [{"key": "r", "ranges": []}]})

    def test_pin_on_key_or_data_source(self):
        self.check({"sources": [{"data": "00", "size": 1}]})
        self.check({"sources": [{"key": "k", "etag": "\"a\""}], "entries": [B]})

    def test_bad_etag(self):
        for et in ["abc", "W/\"abc\"", "\"a b\"", "\"", "\"a\"b\"", "\"é\""]:
            self.check({"sources": [{"url": "a", "etag": et}]})

    def test_payload_too_large(self):
        self.check({"entries": [{"key": "r", "ranges": [{"data": "00" * 65520}]}]})
        # boundary: a single literal of 65515 bytes encodes to exactly 65519 bytes
        self.assertFalse(rejects({"entries": [{"key": "r", "ranges": [{"data": "00" * 65515}]}]}))
        self.check({"entries": [{"key": "r", "ranges": [{"data": "00" * 65516}]}]})

    def test_pinned_invalid(self):
        self.check({"entries": [dict(B, pinned=True)]})  # no page index
        self.check({"page_size": 1, "entries": [{"key": "r", "ranges": [], "pinned": True}]})

    def test_compress_on_reference(self):
        self.check({"entries": [{"key": "r", "ranges": [], "compress": True}]})

    def test_harness_type_rules(self):
        for d in [
            {"page_size": 0}, {"page_size": 1.0}, {"page_size": True}, {"mirror": None},
            {"mirror": 1}, {"sources": None}, {"entries": [{"key": "k", "bytes": "0"}]},
            {"entries": [{"key": "k", "bytes": "AB"}]}, {"entries": [{"key": "k"}]},
            {"entries": [{"key": "k", "bytes": "", "ranges": []}]},
            {"entries": [{"key": "k", "bytes": "", "compress": None}]},
            {"entries": [{"key": "r", "ranges": [{"data": "00", "offset": 0}]}]},
            {"entries": [{"key": "r", "ranges": [{"source": -1}]}]},
            {"entries": [{"key": "r", "ranges": [{"length": 1.0}]}]},
            {"sources": [{"url": "a", "key": "b"}]}, {"sources": [{}]},
            {"sources": [{"url": "a", "size": None}]},
        ]:
            self.check(d)


if __name__ == "__main__":
    unittest.main()
