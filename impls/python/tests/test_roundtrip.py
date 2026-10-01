import io
import itertools
import json
import os
import random
import subprocess
import unittest
import zipfile

from helpers import TmpDir, read_queries, run_cli, write_desc

from vzip_impl import Archive, Entry, Source, WriteError, write_archive


def _desc(page_size, mirror, compress, pinned):
    return {
        "page_size": page_size,
        "mirror": mirror,
        "sources": [{"url": "data/a%20b.bin"}, {"key": "__vz__/hdr"}, {"data": "48445221"},
                    {"url": "data/a%20b.bin", "size": 26}],
        "entries": [
            {"key": "x/zarr.json", "bytes": "7b7d", "compress": compress, "pinned": pinned},
            {"key": "__vz__/hdr", "bytes": "68656c6c6f", "compress": compress,
             "pinned": pinned},
            {"key": "x/c/0", "ranges": [{"source": 0, "offset": 10, "length": 4}]},
            {"key": "x/c/1", "ranges": [{"source": 2, "offset": 0, "length": 3},
                                        {"data": "00ff"},
                                        {"source": 1, "offset": 1, "length": 3}]},
            {"key": "x/c/2", "ranges": []},
            {"key": "x/c/3", "ranges": [{"data": ""}]},
            {"key": "x/c/4", "ranges": [{"source": 3, "offset": 0, "length": 26},
                                        {}]},
            {"key": "x/empty", "bytes": ""},
            {"key": "é/\U0001F600", "bytes": "01", "compress": compress},
            {"key": "é/～", "bytes": "02"},
            {"key": "a/../b", "bytes": "03"},
            {"key": "x/", "bytes": "04"},
            {"key": "﻿bom", "bytes": "05"},
        ],
    }


ALPHA = b"abcdefghijklmnopqrstuvwxyz"
EXPECT = {
    "x/zarr.json": b"{}",
    "x/c/0": ALPHA[10:14],
    "x/c/1": b"HDR\x00\xffell",
    "x/c/2": b"",
    "x/c/3": b"",
    "x/c/4": ALPHA,
    "x/empty": b"",
    "é/\U0001F600": b"\x01",
    "é/～": b"\x02",
    "a/../b": b"\x03",
    "x/": b"\x04",
    "﻿bom": b"\x05",
}


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self.td = TmpDir()
        self.td.write("data/a b.bin", ALPHA)

    def tearDown(self):
        self.td.cleanup()

    def test_roundtrip_combinations(self):
        """One test over the cross product of writer options (via the CLI)."""
        combos = list(itertools.product([None, 1, 64, 100000], [True, False], [True, False],
                                        [True, False]))
        for page_size, mirror, compress, pinned in combos:
            if pinned and page_size is None:
                continue
            with self.subTest(page_size=page_size, mirror=mirror, compress=compress,
                              pinned=pinned):
                name = f"o-{page_size}-{mirror}-{compress}-{pinned}.vzip"
                r, out = write_desc(self.td, _desc(page_size, mirror, compress, pinned), name)
                self.assertEqual(r.returncode, 0, r.stderr)
                ut = subprocess.run(["unzip", "-t", out], capture_output=True, text=True, errors="replace")
                self.assertEqual(ut.returncode, 0, ut.stdout + ut.stderr)
                with zipfile.ZipFile(out) as zf:
                    self.assertIsNone(zf.testzip())
                    names = zf.namelist()
                    self.assertIn("__vz__/sources", names)
                    self.assertEqual("__vz__/index" in names, page_size is not None)
                    if mirror:
                        self.assertNotEqual(zf.read("x/c/0"), b"")
                    else:
                        self.assertEqual(zf.read("x/c/0"), b"")
                keys = sorted(EXPECT, key=lambda k: k.encode())
                queries = [{"op": "list", "prefix": ""}, {"op": "list", "prefix": "x/c"},
                           {"op": "list", "prefix": "é"}, {"op": "list", "prefix": "zz"}]
                for k in keys:
                    queries += [{"op": "get", "key": k}, {"op": "classify", "key": k},
                                {"op": "get", "key": k, "range": {"start": 1, "end": 3}},
                                {"op": "get", "key": k, "range": {"offset": 2}},
                                {"op": "get", "key": k, "range": {"suffix": 3}},
                                {"op": "get", "key": k, "range": {"start": 100, "end": 200}}]
                queries += [{"op": "classify", "key": "__vz__/hdr"},
                            {"op": "get", "key": "__vz__/hdr"},
                            {"op": "get_raw", "key": "__vz__/hdr"},
                            {"op": "get", "key": "missing"},
                            {"op": "get_raw", "key": "x/c/0"},
                            {"op": "get_raw", "key": "__vz__/sources"},
                            {"op": "get", "key": "missing", "range": {"start": 3, "end": 1}}]
                res = read_queries(self.td, out, queries)
                self.assertEqual(res["open"], {"ok": True})
                rs = iter(res["results"])
                self.assertEqual(next(rs)["keys"], keys)
                self.assertEqual(next(rs)["keys"], [k for k in keys if k.startswith("x/c")])
                self.assertEqual(next(rs)["keys"], ["\u00e9/\uff5e", "\u00e9/\U0001F600"])
                self.assertEqual(next(rs)["keys"], [])
                for k in keys:
                    v = EXPECT[k]
                    self.assertEqual(next(rs), {"ok": True, "value": v.hex()}, k)
                    kind = "bytes" if k in ("x/zarr.json", "x/empty") or not k.startswith("x/c") \
                        else "reference"
                    self.assertEqual(next(rs), {"ok": True, "kind": kind}, k)
                    self.assertEqual(next(rs)["value"], v[1:3].hex(), k)
                    self.assertEqual(next(rs)["value"], v[2:].hex(), k)
                    self.assertEqual(next(rs)["value"], v[-3:].hex(), k)
                    self.assertEqual(next(rs)["value"], "", k)
                self.assertEqual(next(rs), {"ok": True, "kind": "missing"})
                self.assertEqual(next(rs), {"ok": True, "value": None})
                self.assertEqual(next(rs), {"ok": True, "value": b"hello".hex()})
                self.assertEqual(next(rs), {"ok": True, "value": None})
                raw = next(rs)["value"]
                self.assertEqual(raw != "", mirror)
                self.assertTrue(next(rs)["ok"])
                self.assertEqual(next(rs)["class"], "request")

    def test_cli_from_other_directory_relative_paths(self):
        r, out = write_desc(self.td, _desc(None, True, False, False))
        self.assertEqual(r.returncode, 0, r.stderr)
        qp = self.td.write("q.json", json.dumps([{"op": "get", "key": "x/c/0"}]))
        res = run_cli("read", "out.vzip", "q.json", cwd=self.td.path)
        self.assertEqual(json.loads(res.stdout)["results"][0]["value"], ALPHA[10:14].hex())
        del qp

    def test_paged_many_entries_matches_unpaged(self):
        rnd = random.Random(1)
        keys = set()
        while len(keys) < 300:
            keys.add("".join(rnd.choice("ab/é\U0001F600～c") for _ in
                             range(rnd.randint(1, 6))))
        keys = sorted(keys)
        entries = [Entry(k, data=k.encode() * 2, compress=bool(i % 2), pinned=(i % 37 == 0))
                   for i, k in enumerate(keys)]
        prefixes = ["", "a", "ab", "b/", "é", "\U0001F600", "～", "c", "zz", "/",
                    "\U0001F600\U0001F600"]
        results = {}
        for ps in (None, 1, 50, 200, 5000):
            for e in entries:
                e.pinned = e.pinned and ps is not None
            path = self.td.p(f"m{ps}.vzip")
            with open(path, "wb") as f:
                write_archive(f, entries, [], page_size=ps)
            ar = Archive(path)
            got = ([ar.list(p) for p in prefixes],
                   [ar.get(k) for k in keys] + [ar.get(k + "zz") for k in keys[:20]],
                   [ar.classify(k) for k in keys])
            results[ps] = got
            self.assertEqual(got[0][0], sorted(k.encode() for k in keys))
            for e in entries:
                e.pinned = (keys.index(e.key) % 37 == 0)
        for ps, got in results.items():
            self.assertEqual(got, results[None], ps)

    def test_library_bytes_mapping_is_reproducible(self):
        def build():
            buf = io.BytesIO()
            write_archive(buf, [Entry("a", data=b"x"), Entry("b", ranges=[{"data": b"y"}])],
                          [Source("url", "a.bin", size=3)], page_size=10)
            return buf.getvalue()
        self.assertEqual(build(), build())

    def test_writer_error_raises_before_output(self):
        buf = io.BytesIO()
        with self.assertRaises(WriteError):
            write_archive(buf, [Entry("", data=b"")], [])
        self.assertEqual(buf.getvalue(), b"")


class TestZip64(unittest.TestCase):
    def test_many_entries_zip64_end_records(self):
        td = TmpDir()
        try:
            n = 0xFFFF
            entries = [Entry(f"k{i:05d}", data=b"") for i in range(n)]
            for ps in (None, 4096):
                path = td.p(f"z{ps}.vzip")
                with open(path, "wb") as f:
                    write_archive(f, entries, [], page_size=ps)
                with open(path, "rb") as f:
                    data = f.read()
                self.assertIn(b"PK\x06\x06", data[-200:])
                ar = Archive(path)
                self.assertEqual(ar.get("k65534"), b"")
                self.assertEqual(ar.classify("k00000"), "bytes")
                self.assertEqual(len(ar.list("k6553")), 5)
                ut = subprocess.run(["unzip", "-tq", path], capture_output=True, text=True, errors="replace")
                self.assertEqual(ut.returncode, 0, ut.stdout[-500:] + ut.stderr[-500:])
            # one fewer entry: no zip64
            path = td.p("noz.vzip")
            with open(path, "wb") as f:
                write_archive(f, entries[:n - 2], [])
            with open(path, "rb") as f:
                self.assertNotIn(b"PK\x06\x06", f.read()[-200:])
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
