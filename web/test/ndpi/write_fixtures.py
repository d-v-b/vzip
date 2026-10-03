"""Writes NDPI files to web/test/fixtures/ndpi/ for profiles/ndpi.md §4, cut from
the restart intervals of level 2 of OpenSlide's CMU-1.ndpi (CC0 1.0), which
is downloaded once into a cache.

- `ndpi_levels.ndpi`: three levels. Level 0 is 10 × 150 intervals (1280 ×
  1200 px), so its chunks are 8 × 128 intervals and the chunks at its right
  and bottom edges repeat intervals; level 1 is a small McuStarts level;
  level 2 is a single JPEG strip without McuStarts. A macro image (negative
  magnification) is skipped.
- `edge_reject_ndpi_*.ndpi`: inputs §4 rejects.

Pixels are checked against tifffile by verify.py.

Usage: uv run python web/test/ndpi/write_fixtures.py
"""

from __future__ import annotations

import struct
import urllib.request
from pathlib import Path

import imagecodecs
import numpy as np

OUT = Path(__file__).parents[1] / "fixtures" / "ndpi"
SOURCE = "https://openslide.cs.cmu.edu/download/openslide-testdata/Hamamatsu/CMU-1.ndpi"
CACHE = Path("/tmp/vzip-fixture-cache/CMU-1-level2.bin")


def fetch(offset: int, length: int) -> bytes:
    req = urllib.request.Request(SOURCE, headers={"Range": f"bytes={offset}-{offset + length - 1}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def level2() -> tuple[bytes, list[int], int]:
    """CMU-1.ndpi's level 2 strip, its McuStarts and intervals per row."""
    if not CACHE.exists():
        # IFD 2 of CMU-1.ndpi (3200 × 2384, 25 intervals of 128 px per row);
        # the offsets were read with tifffile.
        strip_offset, strip_count, mcu_offset, mcu_count = 197055725, 833122, None, 7450
        head = fetch(0, 12)
        first = struct.unpack("<Q", head[4:12])[0]
        offset = first
        for _ in range(3):
            n = struct.unpack("<H", fetch(offset, 2))[0]
            body = fetch(offset + 2, 12 * n + 8)
            entries = {struct.unpack("<H", body[12 * i : 12 * i + 2])[0]: body[12 * i : 12 * i + 12] for i in range(n)}
            if struct.unpack("<I", entries[273][8:12])[0] == strip_offset:
                mcu_offset = struct.unpack("<I", entries[65426][8:12])[0]
                break
            offset = struct.unpack("<Q", body[12 * n : 12 * n + 8])[0]
        starts = fetch(mcu_offset, 4 * mcu_count)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_bytes(struct.pack("<I", len(starts)) + starts + fetch(strip_offset, strip_count))
    data = CACHE.read_bytes()
    k = struct.unpack("<I", data[:4])[0]
    starts = list(struct.unpack(f"<{k // 4}I", data[4 : 4 + k]))
    return data[4 + k :], starts, 25


def sof_and_rest(header: bytes) -> tuple[int, int]:
    pos = 2
    while True:
        marker, length = header[pos + 1], struct.unpack(">H", header[pos + 2 : pos + 4])[0]
        if marker == 0xC0:
            return pos, pos + 2 + length
        pos += 2 + length


def crop(strip: bytes, starts: list[int], per_row: int, x0: int, y0: int, across: int, down: int,
         header_override: bytes | None = None) -> tuple[bytes, list[int], int, int]:
    """A strip of `across × down` intervals from (x0, y0), with its McuStarts,
    width and height (8 × 8 MCUs, 16 MCUs per interval)."""
    header = header_override if header_override is not None else strip[: starts[0]]
    a, b = sof_and_rest(header)
    sof = bytearray(header[a:b])
    width, height = across * 128, down * 8
    sof[5:7] = struct.pack(">H", height)
    sof[7:9] = struct.pack(">H", width)
    out = bytearray(header[:a] + bytes(sof) + header[b:])
    new_starts = []
    t = 0
    for y in range(down):
        for x in range(across):
            i = (y0 + y) * per_row + x0 + x
            end = (starts[i + 1] if i + 1 < len(starts) else len(strip)) - 2
            if t:
                out += bytes([0xFF, 0xD0 + (t - 1) % 8])
            new_starts.append(len(out))
            out += strip[starts[i] : end]
            t += 1
    out += b"\xff\xd9"
    return bytes(out), new_starts, width, height


class Ndpi:
    """An NDPI file: little-endian, an 8-byte first-IFD offset, and IFDs
    followed by an 8-byte next offset and a high word per entry."""

    def __init__(self) -> None:
        self.buf = bytearray(b"II*\x00" + b"\x00" * 8)
        self.ifds: list[int] = []

    def blob(self, data: bytes) -> int:
        offset = len(self.buf)
        self.buf += data
        if len(self.buf) % 2:
            self.buf += b"\x00"
        return offset

    def image(self, strip: bytes, width: int, height: int, mag: float, starts: list[int] | None = None,
              extra: dict | None = None) -> None:
        so = self.blob(strip)
        tags = {
            256: (4, [width]), 257: (4, [height]), 258: (3, [8, 8, 8]), 259: (3, [7]), 262: (3, [6]),
            273: (4, [so]), 277: (3, [3]), 279: (4, [len(strip)]), 282: (5, [(21910 // 4, 1)]),
            283: (5, [(21910 // 4, 1)]), 296: (3, [3]), 65420: (4, [1]), 65421: (11, [mag]),
            65422: (9, [4876667]), 65423: (9, [-2340000]),  # the image's centre from the slide's, in nm
        }
        if starts is not None:
            tags[65426] = (4, starts)
        tags.update(extra or {})
        entries = []
        for tag in sorted(tags):
            typ, values = tags[tag]
            if typ == 5:
                raw = b"".join(struct.pack("<II", *v) for v in values)
            else:
                raw = struct.pack(f"<{len(values)}{ {3: 'H', 4: 'I', 9: 'i', 11: 'f'}[typ] }", *values)
            field = raw.ljust(4, b"\x00") if len(raw) <= 4 else struct.pack("<I", self.blob(raw))
            entries.append(struct.pack("<HHI", tag, typ, len(values)) + field)
        n = len(entries)
        self.ifds.append(self.blob(struct.pack("<H", n) + b"".join(entries) + b"\x00" * 8 + b"\x00" * 4 * n))

    def write(self, name: str) -> None:
        struct.pack_into("<Q", self.buf, 4, self.ifds[0])
        for a, b in zip(self.ifds, self.ifds[1:]):
            n = struct.unpack_from("<H", self.buf, a)[0]
            struct.pack_into("<Q", self.buf, a + 2 + 12 * n, b)
        (OUT / name).write_bytes(bytes(self.buf))


def main() -> None:
    OUT.mkdir(exist_ok=True)
    strip, starts, per_row = level2()
    level0 = crop(strip, starts, per_row, 8, 60, 10, 150)
    level1 = crop(strip, starts, per_row, 10, 100, 3, 20)
    rng = np.random.default_rng(3)
    small = imagecodecs.jpeg8_encode(rng.integers(0, 256, (48, 64, 3), dtype=np.uint8), level=85)
    macro = imagecodecs.jpeg8_encode(rng.integers(0, 256, (16, 48, 3), dtype=np.uint8), level=85)

    f = Ndpi()
    f.image(level0[0], level0[2], level0[3], 20.0, level0[1])
    f.image(level1[0], level1[2], level1[3], 5.0, level1[1])
    f.image(small, 64, 48, 1.25)
    f.image(macro, 48, 16, -1.0)
    f.write("ndpi_levels.ndpi")

    # Rejected.
    f = Ndpi()  # two levels with one magnification (focal planes)
    f.image(level0[0], level0[2], level0[3], 20.0, level0[1])
    f.image(level1[0], level1[2], level1[3], 20.0, level1[1])
    f.write("edge_reject_ndpi_focal_planes.ndpi")
    f = Ndpi()  # a later level that is not smaller
    f.image(level1[0], level1[2], level1[3], 20.0, level1[1])
    f.image(level0[0], level0[2], level0[3], 5.0, level0[1])
    f.write("edge_reject_ndpi_growing.ndpi")
    f = Ndpi()  # McuStarts with one value too few
    f.image(level1[0], level1[2], level1[3], 20.0, level1[1][:-1])
    f.write("edge_reject_ndpi_mcustarts.ndpi")
    header = strip[: starts[0]]
    pos = header.index(b"\xff\xdd")  # remove the DRI segment
    no_dri = crop(strip, starts, per_row, 10, 100, 3, 20, header[:pos] + header[pos + 6 :])
    f = Ndpi()
    f.image(no_dri[0], no_dri[2], no_dri[3], 20.0, no_dri[1])
    f.write("edge_reject_ndpi_no_restart_interval.ndpi")
    for p in sorted(OUT.glob("*ndpi*")):
        print(p.name, p.stat().st_size)


if __name__ == "__main__":
    main()
