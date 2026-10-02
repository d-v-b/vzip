"""Writes JPEG-in-TIFF files to web/test/fixtures/ for the JPEG rule of
VIRTUALIZE.md §3.5-§3.6.

- `jpeg_aperio_rgb.tif`: like Aperio SVS. The IFD's JPEGTables holds the
  quantization and Huffman tables, and each tile is an abbreviated stream
  (no tables, no colour marker) of RGB samples with component IDs 0, 1, 2,
  which a JPEG decoder would take for YCbCr without the Adobe marker the
  virtualizer adds. PhotometricInterpretation is RGB.
- `jpeg_ycbcr.tif`, `jpeg_gray.tif`: written by tifffile.
- `edge_reject_jpeg_*.tif`: inputs the rule rejects.

Pixels are checked against tifffile by verify_tiff.py.

Usage: uv run python web/test/tiff_jpeg_fixtures.py
"""

from __future__ import annotations

import struct
from pathlib import Path

import imagecodecs
import numpy as np
import tifffile

from tiff_edge_fixtures import LONG, SHORT, UNDEFINED, Tiff

OUT = Path(__file__).parent / "fixtures"
rng = np.random.default_rng(7)


def segments(stream: bytes) -> list[tuple[int, bytes]]:
    """The marker segments of a JPEG stream, as (marker, bytes including the
    marker), with the entropy-coded data kept in the SOS segment."""
    out, p = [], 2
    while p < len(stream):
        assert stream[p] == 0xFF
        marker = stream[p + 1]
        if marker == 0xD9:
            out.append((marker, stream[p : p + 2]))
            break
        n = struct.unpack(">H", stream[p + 2 : p + 4])[0]
        end = p + 2 + n
        if marker == 0xDA:  # the scan runs to the EOI marker
            end = stream.index(b"\xff\xd9", end)
        out.append((marker, stream[p:end]))
        p = end
    return out


def renumber(seg: bytes, marker: int) -> bytes:
    """A SOF or SOS segment with component IDs 0, 1, 2."""
    b = bytearray(seg)
    if marker == 0xC0:  # FF C0 len(2) P(1) Y(2) X(2) Nf(1), then 3 bytes per component
        for i in range(b[9]):
            b[10 + 3 * i] = i
    else:  # FF DA len(2) Ns(1), then 2 bytes per component
        for i in range(b[4]):
            b[5 + 2 * i] = i
    return bytes(b)


def aperio(name: str, rgb: np.ndarray, tile: int) -> None:
    """Abbreviated RGB tiles with shared tables, as in Aperio SVS."""
    h, w, _ = rgb.shape
    t = Tiff()
    tables, offsets, counts = None, [], []
    for y in range(0, h, tile):
        for x in range(0, w, tile):
            block = np.zeros((tile, tile, 3), np.uint8)
            part = rgb[y : y + tile, x : x + tile]
            block[: part.shape[0], : part.shape[1]] = part
            full = imagecodecs.jpeg8_encode(block, level=90, colorspace="RGB", outcolorspace="RGB")
            segs = segments(full)
            t_segs = b"".join(s for m, s in segs if m in (0xDB, 0xC4))
            tables = b"\xff\xd8" + t_segs + b"\xff\xd9"
            body = b"".join(renumber(s, m) if m in (0xC0, 0xDA) else s
                            for m, s in segs if m in (0xC0, 0xDA, 0xD9))
            offsets.append(t.blob(b"\xff\xd8" + body))
            counts.append(len(body) + 2)
    t.chain([t.ifd([
        (256, SHORT, [w]), (257, SHORT, [h]), (258, SHORT, [8, 8, 8]), (259, SHORT, [7]), (262, SHORT, [2]),
        (277, SHORT, [3]), (284, SHORT, [1]), (322, SHORT, [tile]), (323, SHORT, [tile]),
        (324, LONG, offsets), (325, LONG, counts), (347, UNDEFINED, tables),
    ])])
    t.write(name)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    y, x = np.mgrid[0:96, 0:128]
    rgb = np.stack([(x * 2) % 256, (y * 3) % 256, ((x + y) * 5) % 256], -1).astype(np.uint8)
    aperio("jpeg_aperio_rgb.tif", rgb, 32)
    tifffile.imwrite(OUT / "jpeg_ycbcr.tif", rgb, tile=(32, 32), compression="jpeg", photometric="rgb")
    tifffile.imwrite(OUT / "jpeg_gray.tif", rgb[..., 0], tile=(32, 32), compression="jpeg", photometric="minisblack")

    # Rejected: planar JPEG; no PhotometricInterpretation; malformed tables;
    # a tile of 2 bytes; 16-bit samples.
    small = rgb[:32, :32]
    planar = tifffile.TiffWriter(OUT / "edge_reject_jpeg_planar.tif")
    planar.write(np.moveaxis(small, -1, 0), tile=(32, 32), compression="jpeg", photometric="rgb", planarconfig="separate")
    planar.close()

    def one_tile(name: str, entries_change, tile_bytes: bytes | None = None) -> None:
        t = Tiff()
        stream = imagecodecs.jpeg8_encode(small, level=90)
        off = t.blob(tile_bytes if tile_bytes is not None else stream)
        entries = {256: (SHORT, [32]), 257: (SHORT, [32]), 258: (SHORT, [8, 8, 8]), 259: (SHORT, [7]),
                   262: (SHORT, [6]), 277: (SHORT, [3]), 284: (SHORT, [1]), 322: (SHORT, [32]),
                   323: (SHORT, [32]), 324: (LONG, [off]), 325: (LONG, [len(tile_bytes or stream)])}
        entries_change(entries)
        t.chain([t.ifd(sorted((k, *v) for k, v in entries.items()))])
        t.write(name)

    one_tile("edge_reject_jpeg_no_photometric.tif", lambda e: e.pop(262))
    one_tile("edge_reject_jpeg_tables.tif", lambda e: e.update({347: (UNDEFINED, b"\x00\x01\x02\x03\x04\x05")}))
    one_tile("edge_reject_jpeg_short_tile.tif", lambda e: None, tile_bytes=b"\xff\xd8")
    one_tile("edge_reject_jpeg_16bit.tif", lambda e: e.update({258: (SHORT, [16, 16, 16])}))
    for p in sorted(OUT.glob("*jpeg*.tif")):
        print(p.name, p.stat().st_size)


if __name__ == "__main__":
    main()
