import json
import os
import random
import struct
import subprocess
import unittest
import zipfile

from helpers import cli, query, tmpdir, write_desc

KEY_POOL = ["a", "a/b", "a/b/c", "b", "x/", "a/../b", "﻿bom", "\U0001F600", "～",
            "zarr.json", "x/zarr.json", "x/c/0", "x/c/1", "x/c/1/2", "é", "z" * 300,
            "__vz__/hdr", "__vz__/other", "~", "\x7f", "a b"]


def random_desc(rng, url_files):
    page_size = rng.choice([None, 1, 50, 300, 100000])
    keys = rng.sample(KEY_POOL, rng.randint(0, len(KEY_POOL)))
    entries = []
    bytes_keys = []
    values = {}
    for k in keys:
        if rng.random() < 0.5 or not keys:
            v = bytes(rng.randrange(256) for _ in range(rng.choice([0, 1, 10, 200, 3000])))
            if rng.random() < 0.5:
                v = b"abc" * rng.randint(0, 500)
            e = {"key": k, "bytes": v.hex(), "compress": rng.random() < 0.5}
            if page_size is not None and rng.random() < 0.3:
                e["pinned"] = True
            entries.append(e)
            bytes_keys.append(k)
            values[k] = v
        else:
            entries.append({"key": k, "ranges": None})
    sources = []
    src_vals = []
    for name, content in url_files.items():
        s = {"url": name.replace(" ", "%20")}
        if rng.random() < 0.5:
            s["size"] = len(content)
        sources.append(s)
        src_vals.append(content)
    for k in bytes_keys:
        if rng.random() < 0.5:
            sources.append({"key": k})
            src_vals.append(values[k])
    d = bytes(rng.randrange(256) for _ in range(rng.randint(0, 40)))
    sources.append({"data": d.hex()})
    src_vals.append(d)
    for e in entries:
        if e.get("ranges", 1) is None:
            rs = []
            val = b""
            for _ in range(rng.choice([0, 1, 1, 2, 5])):
                if rng.random() < 0.3:
                    lit = bytes(rng.randrange(256) for _ in range(rng.randint(0, 8)))
                    rs.append({"data": lit.hex()})
                    val += lit
                else:
                    si = rng.randrange(len(sources))
                    n = len(src_vals[si])
                    off = rng.randint(0, n)
                    ln = rng.randint(0, n - off)
                    r = {}
                    if si:
                        r["source"] = si
                    if off:
                        r["offset"] = off
                    if ln:
                        r["length"] = ln
                    rs.append(r)
                    val += src_vals[si][off:off + ln]
            e["ranges"] = rs
            values[e["key"]] = val
    desc = {"page_size": page_size, "mirror": rng.random() < 0.7, "sources": sources, "entries": entries}
    return desc, values


def check_archive(tc, path, desc, values):
    kinds = {e["key"]: ("bytes" if "bytes" in e else "reference") for e in desc["entries"]}
    public = sorted((k for k in values if not k.startswith("__vz__/")), key=lambda s: s.encode())
    queries = []
    expect = []
    for k, v in values.items():
        hidden = k.startswith("__vz__/")
        queries.append({"op": "classify", "key": k})
        expect.append({"ok": True, "kind": "missing" if hidden else kinds[k]})
        n = len(v)
        for req, sl in [(None, v), ({"start": 0, "end": n}, v), ({"start": 1, "end": 3}, v[1:3]),
                        ({"start": n + 5, "end": n + 9}, b""), ({"offset": 2}, v[2:]),
                        ({"offset": n + 1}, b""), ({"suffix": 3}, v[-3:] if n >= 3 else v),
                        ({"suffix": 0}, b""), ({"suffix": n + 10}, v),
                        ({"start": n // 3, "end": (2 * n) // 3 + 1}, v[n // 3:(2 * n) // 3 + 1])]:
            q = {"op": "get", "key": k}
            if req is not None:
                q["range"] = req
            queries.append(q)
            expect.append({"ok": True, "value": None if hidden else sl.hex()})
        queries.append({"op": "get_raw", "key": k})
        if kinds[k] == "bytes":
            expect.append({"ok": True, "value": v.hex()})
        else:
            expect.append(None)  # checked below
    queries.append({"op": "classify", "key": "__vz__/sources"})
    expect.append({"ok": True, "kind": "missing"})
    queries.append({"op": "get", "key": "not-there"})
    expect.append({"ok": True, "value": None})
    prefixes = {"", "x/", "a", "a/b", "__vz__/", "\U0001F600", "～", "zz", "\xff"}
    for k in values:
        prefixes.add(k[:2])
    for p in sorted(prefixes):
        queries.append({"op": "list", "prefix": p})
        pb_ = p.encode()
        expect.append({"ok": True, "keys": [k for k in public if k.encode().startswith(pb_)]})
    res = query(path, queries)
    tc.assertTrue(res["open"]["ok"], res["open"])
    for q, e, r in zip(queries, expect, res["results"]):
        if e is None:
            tc.assertTrue(r["ok"], (q, r))
            continue
        tc.assertEqual(r, e, q)
    # raw of format entries is visible
    r = query(path, [{"op": "get_raw", "key": "__vz__/sources"},
                     {"op": "get_raw", "key": "__vz__/index"}])["results"]
    tc.assertTrue(r[0]["ok"] and r[0]["value"] is not None)
    tc.assertEqual(r[1]["value"] is None, desc["page_size"] is None)

    # unzip -t accepts it
    p = subprocess.run(["unzip", "-t", path], capture_output=True)
    tc.assertEqual(p.returncode, 0, p.stdout + p.stderr)
    # zipfile interoperability: names and bodies
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        tc.assertIn("__vz__/sources", names)
        for k, v in values.items():
            if kinds[k] == "bytes":
                tc.assertEqual(zf.read(k), v)
            else:
                body = zf.read(k)
                tc.assertTrue(body == b"" if not desc["mirror"] else len(body) >= 0)


class RoundTrip(unittest.TestCase):
    def test_random_round_trips(self):
        rng = random.Random(1234)
        with tmpdir() as d:
            files = {"a.bin": bytes(range(256)) * 4, "sub/b b.bin": b"hello world" * 10}
            os.makedirs(os.path.join(d, "sub"))
            for n, c in files.items():
                with open(os.path.join(d, n), "wb") as fh:
                    fh.write(c)
            for i in range(60):
                with self.subTest(i=i):
                    desc, values = random_desc(rng, files)
                    path = os.path.join(d, f"r{i}.vzip")
                    write_desc(desc, path)
                    check_archive(self, path, desc, values)

    def test_cli_round_trip_from_other_cwd(self):
        with tmpdir() as d:
            with open(os.path.join(d, "data.bin"), "wb") as fh:
                fh.write(b"0123456789")
            desc = {"sources": [{"url": "data.bin", "size": 10}],
                    "entries": [{"key": "k", "ranges": [{"offset": 2, "length": 3}]},
                                {"key": "z", "bytes": "00", "pinned": True}],
                    "page_size": 1}
            with open(os.path.join(d, "d.json"), "w") as fh:
                json.dump(desc, fh)
            with open(os.path.join(d, "q.json"), "w") as fh:
                json.dump([{"op": "get", "key": "k"}, {"op": "list", "prefix": ""}], fh)
            p = cli("write", "d.json", "out dir.vzip", cwd=d)
            self.assertEqual(p.returncode, 0, p.stderr)
            # read with a relative path from the archive's directory
            p = cli("read", "out dir.vzip", "q.json", cwd=d)
            self.assertEqual(p.returncode, 0, p.stderr)
            out = json.loads(p.stdout)
            self.assertEqual(out["results"], [{"ok": True, "value": b"234".hex()},
                                              {"ok": True, "keys": ["k", "z"]}])
            # and with a path containing '..' from a subdirectory
            os.makedirs(os.path.join(d, "s"))
            p = cli("read", "../out dir.vzip", "../q.json", cwd=os.path.join(d, "s"))
            self.assertEqual(json.loads(p.stdout)["results"][0], {"ok": True, "value": b"234".hex()})

    def test_canonical_layout(self):
        """Paged archive: sorted body records, then format records, pages partition."""
        with tmpdir() as d:
            path = os.path.join(d, "a.vzip")
            keys = ["b", "\U0001F600", "～", "a", "__vz__/x", "c"]
            data = write_desc({"page_size": 1, "entries": [{"key": k, "bytes": "00"} for k in keys]}, path)
            with zipfile.ZipFile(path) as zf:
                names = [i.filename for i in zf.infolist()]
            body = sorted(keys, key=lambda s: s.encode())
            self.assertEqual(names[:len(body)], body)
            self.assertEqual(set(names[len(body):]), {"__vz__/sources", "__vz__/index"})
            self.assertEqual(len(data) - struct.unpack_from("<H", data, len(data) - 60 + 20)[0], len(data) - 38)
            self.assertEqual(data[-38:-32], b"vzip/1")

    def test_empty_archive(self):
        with tmpdir() as d:
            for ps in (None, 5):
                path = os.path.join(d, f"e{ps}.vzip")
                write_desc({"page_size": ps}, path)
                r = query(path, [{"op": "list", "prefix": ""}, {"op": "get", "key": "a"}])
                self.assertEqual(r["results"], [{"ok": True, "keys": []}, {"ok": True, "value": None}])
                self.assertEqual(subprocess.run(["unzip", "-t", path], capture_output=True).returncode, 0)

    def test_zip64_many_entries(self):
        with tmpdir() as d:
            path = os.path.join(d, "big.vzip")
            n = 70000
            entries = [{"key": f"k{i:06d}", "bytes": "%02x" % (i % 256)} for i in range(n)]
            entries.append({"key": "r", "ranges": [{"data": "ab"}, {"data": "cd"}]})
            for ps in (None, 4096):
                with self.subTest(page_size=ps):
                    if os.path.exists(path):
                        os.unlink(path)
                    data = write_desc({"page_size": ps, "entries": entries}, path)
                    clen = 22 if ps is None else 38
                    eocd = len(data) - 22 - clen
                    self.assertEqual(struct.unpack_from("<HH", data, eocd + 8), (0xFFFF, 0xFFFF))
                    self.assertEqual(struct.unpack_from("<I", data, eocd - 20)[0], 0x07064B50)
                    r = query(path, [{"op": "get", "key": "k069999"}, {"op": "get", "key": "r"},
                                     {"op": "classify", "key": "k000000"},
                                     {"op": "list", "prefix": "k06999"}])
                    self.assertEqual(r["results"][0], {"ok": True, "value": "%02x" % (69999 % 256)})
                    self.assertEqual(r["results"][1], {"ok": True, "value": "abcd"})
                    self.assertEqual(r["results"][2], {"ok": True, "kind": "bytes"})
                    self.assertEqual(r["results"][3]["keys"], [f"k0699{i}{j}" for i in "9" for j in "0123456789"])
                    p = subprocess.run(["unzip", "-tq", path], capture_output=True)
                    self.assertEqual(p.returncode, 0, p.stdout[-500:])
                    with zipfile.ZipFile(path) as zf:
                        self.assertEqual(len(zf.namelist()), n + 2 + (ps is not None))


if __name__ == "__main__":
    unittest.main()
