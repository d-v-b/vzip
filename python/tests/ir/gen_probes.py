"""The round-3 probes (design/IR_NOTES.md §21): round 1's CZI review findings,
rebuilt, and round 1's TIFF amplification probes, plus IFD loops and huge counts.
Usage: uv run python python/tests/ir/gen_probes.py <out dir> [--small]
(--small writes the tests' sizes: no file over 1 MB.)"""
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, "fixtures/generators/czi")
from write_fixtures import Att, Czi, Sb, plane, segment, time_stamps  # noqa: E402

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
small = "--small" in sys.argv


def write(name: str, data: bytes) -> None:
    (out / name).write_bytes(data)


# ---- TIFF

def tiff(entries_per_ifd: list[list[tuple]], blobs: bytes = b"", big: bool = False, chain=None) -> bytes:
    """A little-endian TIFF: `blobs` after the header, then the IFDs (entries (tag, type, count, value)),
    chained in order unless `chain` gives each IFD's next (an index, or None)."""
    hs = 16 if big else 8
    es, cs, fs = (20, 8, 8) if big else (12, 2, 4)
    w = "Q" if big else "I"
    start = hs + len(blobs)
    sizes = [cs + es * len(e) + fs for e in entries_per_ifd]
    offs = [start + sum(sizes[:k]) for k in range(len(sizes))]
    body = bytearray()
    for k, e in enumerate(entries_per_ifd):
        nxt = chain[k] if chain is not None else (k + 1 if k + 1 < len(entries_per_ifd) else None)
        body += struct.pack("<" + ("Q" if big else "H"), len(e))
        for tag, typ, count, value in sorted(e):
            body += struct.pack(f"<HH{w}{w}", tag, typ, count, value)
        body += struct.pack("<" + w, offs[nxt] if nxt is not None else 0)
    head = (b"II+\0" + struct.pack("<HHQ", 8, 0, offs[0])) if big else (b"II*\0" + struct.pack("<I", offs[0]))
    return head + blobs + bytes(body)


def amp(planes: int, tiles: int) -> bytes:
    """An OME-TIFF whose planes all name IFD 0, whose tiles all name the same 256 bytes."""
    side = int(tiles ** 0.5)
    tiles = side * side
    td = "".join(f'<TiffData IFD="0" FirstZ="{k}" PlaneCount="1"/>' for k in range(planes))
    xml = (f'<OME><Image ID="Image:0"><Pixels DimensionOrder="XYZCT" Type="uint8" SizeX="{16 * side}" '
           f'SizeY="{16 * side}" SizeZ="{planes}" SizeC="1" SizeT="1">{td}</Pixels></Image></OME>').encode() + b"\0"
    blobs = bytearray()

    def put(b):
        o = 8 + len(blobs)
        blobs.extend(b + b"\0" * (len(b) % 2))
        return o

    tile, xo = put(bytes(256)), put(xml)
    offs, counts = put(struct.pack(f"<{tiles}I", *[tile] * tiles)), put(struct.pack(f"<{tiles}I", *[256] * tiles))
    e = [(256, 4, 1, 16 * side), (257, 4, 1, 16 * side), (258, 3, 1, 8), (259, 3, 1, 1), (262, 3, 1, 1),
         (270, 2, len(xml), xo), (277, 3, 1, 1), (322, 3, 1, 16), (323, 3, 1, 16), (324, 4, tiles, offs),
         (325, 4, tiles, counts)]
    return tiff([e], bytes(blobs))


def shared(n: int, tiles: int) -> bytes:
    """n main-chain IFDs of one size (only IFD 0 is an image) that all name one table of
    `tiles` tiles, which all name the same 256 bytes."""
    side = int(tiles ** 0.5)
    tiles = side * side
    blobs = bytearray()

    def put(b):
        o = 8 + len(blobs)
        blobs.extend(b + b"\0" * (len(b) % 2))
        return o

    tile = put(bytes(256))
    offs, counts = put(struct.pack(f"<{tiles}I", *[tile] * tiles)), put(struct.pack(f"<{tiles}I", *[256] * tiles))
    e = [(256, 4, 1, 16 * side), (257, 4, 1, 16 * side), (258, 3, 1, 8), (259, 3, 1, 1), (262, 3, 1, 1),
         (277, 3, 1, 1), (322, 3, 1, 16), (323, 3, 1, 16), (324, 4, tiles, offs), (325, 4, tiles, counts)]
    return tiff([e] * n, bytes(blobs))


def image(w=32, h=32, extra=()):
    """One tiled 8-bit image of w x h in 16 x 16 tiles, its tiles after the header."""
    n = ((w + 15) // 16) * ((h + 15) // 16)
    blobs = bytes(256 * n)
    offs = struct.pack(f"<{n}I", *[8 + 256 * k for k in range(n)])
    counts = struct.pack(f"<{n}I", *[256] * n)
    at = 8 + len(blobs)
    e = [(256, 4, 1, w), (257, 4, 1, h), (258, 3, 1, 8), (259, 3, 1, 1), (262, 3, 1, 1), (277, 3, 1, 1),
         (322, 3, 1, 16), (323, 3, 1, 16), (324, 4, n, at), (325, 4, n, at + 4 * n), *extra]
    return e, blobs + offs + counts


e, blobs = image()
write("tiff_loop_chain.tif", tiff([e, e], blobs, chain=[1, 0]))  # IFD 1's next is IFD 0: rejected
write("tiff_loop_self.tif", tiff([e], blobs, chain=[0]))  # an IFD whose next is itself: rejected
write("tiff_loop_sub.tif", tiff([[*e, (330, 4, 1, 8 + len(blobs))]], blobs))  # a SubIFD naming its own IFD: rejected
# EXIF pointers to IFD 0 and to the EXIF IFD itself: aliases, accepted
ex = 8 + len(blobs) + 2 + 12 * (len(e) + 1) + 4
write("tiff_loop_pointers.tif", tiff([[*e, (34665, 4, 1, ex)], [(34665, 4, 1, 8 + len(blobs)), (34853, 4, 1, ex)]],
                                     blobs, chain=[None, None]))
# huge counts
write("tiff_huge_entry_count.tif", b"II+\0" + struct.pack("<HHQ", 8, 0, 16) + struct.pack("<Q", 1 << 40) + bytes(64))
write("tiff_huge_table_count.tif", tiff([[*e[:8], (324, 4, 0xFFFFFFFF, 64), e[9]]], blobs))  # outside: rejected
write("tiff_huge_other_count.tif", tiff([[*e, (65000, 7, 0xFFFFFFFF, 64)]], blobs))  # not read: accepted
write("tiff_huge_bigtiff_value.tif", tiff([[*e[:8], (324, 16, 1, 1 << 60), e[9]]], blobs, big=True))  # > 2^53: rejected
write("tiff_many_ifds.tif", tiff([[(256, 4, 1, 1)]] * (1000 if small else 100001)))  # too many IFDs: rejected past 100000
write("tiff_amp_100_100.tif", amp(100, 100))
write("tiff_shared_1000_1024.tif", shared(1000, 1024))
if not small:
    write("tiff_amp_1000_1024.tif", amp(1000, 1024))
    write("tiff_amp_3000_4096.tif", amp(3000, 4096))
    write("tiff_shared_10000_4096.tif", shared(10000, 4096))

# ---- CZI

def sb_plane(t, w, h, data):
    return Sb(0, 0, [("X", 0, w, w), ("Y", 0, h, h), ("T", t, 1, 1)], data)


# 1. Row-band floor: a prime height of 1-byte rows (today's divisor bands: a chunk per row)
if not small:
    H = 16777259
    write("czi_bands1.czi", Czi(subblocks=[Sb(0, 0, [("X", 0, 1, 1), ("Y", 0, H, H)], bytes(H))]).build())
# 2. Quadratic XML: n opens, then n unmatched end tags
n = 2000 if small else 40000
write(f"czi_deep{n}.czi", Czi(subblocks=[plane(0, 0, 0, 0, 4, 4)],
                              xml=("<ImageDocument>" + "<a>" * n + "</b>" * n + "</ImageDocument>").encode()).build())
# 3. XML scan memory: 16 MB of empty tags
n = 100000 if small else 4000000
write("czi_flat.czi", Czi(subblocks=[plane(0, 0, 0, 0, 4, 4)],
                          xml=("<ImageDocument>" + "<a/>" * n + "</ImageDocument>").encode()).build())
# 4. Shared FilePosition: n directory entries naming the first subblock
c = Czi(subblocks=[plane(0, 0, 0, 0, 4, 4), plane(0, 0, 4, 0, 4, 4)])
b = bytearray(c.build())
d = c.offsets["directory"]
entry = bytes(b[d + 160: d + 160 + 32 + 20 * struct.unpack_from("<i", b, d + 160 + 28)[0]])
n = 1000 if small else 100000
body = entry * n
b = b[:d] + segment("ZISRAWDIRECTORY", struct.pack("<i", n).ljust(128, b"\0") + body)
write("czi_dup.czi", bytes(b))
# 5. Coalesced prefetch: n subblocks of 4 KiB, one line each (only their headers are needed)
n, w = (2000, 4096) if small else (100000, 4096)
data = os.urandom(w)
write("czi_xt.czi", Czi(subblocks=[sb_plane(t, w, 1, data) for t in range(n)]).build())
# 6. XML size past the scan: 64 MiB + 1 of XML is not read for the layout
# 7. Unchecked tile extent: a JPEG XR subblock whose coded size is 2^32 - 1 (a tile array of that shape)
sb = plane(0, 4, 0, 0, 16, 16)
bb = bytearray(sb.data)
ifd = struct.unpack_from("<I", bb, 4)[0]
for k in range(struct.unpack_from("<H", bb, ifd)[0]):
    at = ifd + 2 + 12 * k
    tag, typ, cnt, val = struct.unpack_from("<HHII", bb, at)
    if tag in (0xBC80, 0xBC81):
        struct.pack_into("<HHII", bb, at, tag, 4, 1, 0xFFFFFFFF)
sb.data = bytes(bb)
write("czi_jxrbig.czi", Czi(subblocks=[sb]).build())
# 8. Attachment names with bytes after their NUL
write("czi_attname.czi", Czi(subblocks=[plane(0, 0, 0, 0, 4, 4)],
                             attachments=[Att("Label\0hidden name", "CZTIMS\0x", time_stamps([0.5, 1.5])),
                                          Att("Thumb\0\0\0\0\0\0x", "JPG\0junk", b"\xff\xd8\xff\xd9")]).build())
# 9. Per-element cost: many empty segments after the directory
n = 10000 if small else 2000000
write("czi_walk0.czi", Czi(subblocks=[plane(0, 0, 0, 0, 4, 4)], late=[segment("DELETED", b"", allocated=0, used=0) * n]).build())
print(sorted(p.name for p in out.iterdir()))
