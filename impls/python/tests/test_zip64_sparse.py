"""ZIP64 offsets above 4 GiB, using a sparse file (set VZIP_SKIP_SLOW=1 to skip)."""

import os
import struct
import subprocess
import tempfile
import unittest
import zlib

from helpers import deflate, range_extra
from vzip_impl import proto
from vzip_impl.reader import Archive


@unittest.skipIf(os.environ.get("VZIP_SKIP_SLOW"), "slow")
class TestSparseZip64(unittest.TestCase):
    def test_offsets_above_4gib(self):
        pad_len = 0xFFFFFFFE
        crc = 0
        chunk = bytes(1 << 24)
        left = pad_len
        while left:
            n = min(left, len(chunk))
            crc = zlib.crc32(chunk[:n], crc)
            left -= n
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "big.vzip")
            cd = bytearray()
            with open(path, "wb") as f:
                def local(name, method, crc_, csize, usize, body, at):
                    f.seek(at)
                    f.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 45, 0x800, method, 0, 0x21, crc_, csize,
                                        usize, len(name), 0) + name)
                    f.write(body)

                def cdrec(name, method, crc_, csize, usize, loff, extra=b""):
                    if loff >= 0xFFFFFFFF:
                        extra = struct.pack("<HHQ", 1, 8, loff) + extra
                        loff = 0xFFFFFFFF
                    cd.extend(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 45, 0x800, method, 0, 0x21, crc_,
                                          csize, usize, len(name), len(extra), 0, 0, 0, 0, loff) + name + extra)

                local(b"pad", 0, crc, pad_len, pad_len, b"", 0)
                pos = 30 + 3 + pad_len
                f.truncate(pos)
                big = b"beyond 4 GiB"
                local(b"big", 0, zlib.crc32(big), len(big), len(big), big, pos)
                big_off = pos
                pos += 30 + 3 + len(big)
                rx = range_extra(proto.Range(0, 7, 5))
                local(b"ref", 0, 0, 0, 0, b"", pos)
                ref_off = pos
                pos += 30 + 3
                src_raw = proto.encode_source_table([proto.Source("key", "big")])
                sb = deflate(src_raw)
                local(b"__vz__/sources", 8, zlib.crc32(src_raw), len(sb), len(src_raw), sb, pos)
                src_off = pos
                pos += 30 + 14 + len(sb)
                cdrec(b"big", 0, zlib.crc32(big), len(big), len(big), big_off)
                cdrec(b"pad", 0, crc, pad_len, pad_len, 0)
                cdrec(b"ref", 0, 0, 0, 0, ref_off, rx)
                cdrec(b"__vz__/sources", 8, zlib.crc32(src_raw), len(sb), len(src_raw), src_off)
                cd_off = pos
                f.seek(pos)
                f.write(cd)
                z64 = cd_off + len(cd)
                f.write(struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0, 4, 4, len(cd), cd_off))
                f.write(struct.pack("<IIQI", 0x07064B50, 0, z64, 1))
                comment = b"vzip/0" + struct.pack("<QQ", src_off + 30 + 14, len(sb))
                f.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 4, 4, len(cd), 0xFFFFFFFF, len(comment)))
                f.write(comment)
            with Archive(path) as ar:
                self.assertEqual(ar.list(""), ["big", "pad", "ref"])
                self.assertEqual(ar.get("big"), big)
                self.assertEqual(ar.get("ref"), b"4 GiB")
                self.assertEqual(ar.get("pad", __import__("vzip_impl.reader").reader.Request("suffix", 3)),
                                 b"\0\0\0")
            r = subprocess.run(["unzip", "-t", path], capture_output=True, text=True, errors="replace")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
