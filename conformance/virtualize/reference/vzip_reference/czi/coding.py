"""CZI pixel types, compressions and codec headers (spec/virtualize/czi.md
§3.1–§3.2, spec/virtualize.md §13.4)."""

from __future__ import annotations

import struct
from typing import Callable

# PixelType -> (name, data type, samples p, bytes per pixel q)
PIXEL_TYPES = {
    0: ("Gray8", "uint8", 1, 1),
    1: ("Gray16", "uint16", 1, 2),
    2: ("Gray32Float", "float32", 1, 4),
    3: ("Bgr24", "uint8", 3, 3),
    4: ("Bgr48", "uint16", 3, 6),
    8: ("Bgr96Float", "float32", 3, 12),
    9: ("Bgra32", "uint8", 4, 4),
    10: ("Gray64ComplexFloat", "complex64", 1, 8),
    11: ("Bgr192ComplexFloat", "complex64", 3, 24),
    12: ("Gray32", "int32", 1, 4),
    13: ("Gray64Float", "float64", 1, 8),
}
ALL = frozenset(PIXEL_TYPES)
# Compression -> (name, the pixel types it admits)
COMPRESSIONS = {
    0: ("uncompressed", ALL),
    1: ("JpgFile", frozenset({0, 3})),
    4: ("JpgXr", frozenset({0, 1, 2, 3, 4, 8, 9})),
    5: ("Zstd0", ALL),
    6: ("Zstd1", ALL),
}
UNCOMPRESSED, JPEG, JPEGXR, ZSTD0, ZSTD1 = 0, 1, 4, 5, 6
HILO_TYPES = frozenset({1, 4})  # Gray16, Bgr48: the only types hi-lo packing is on
MAX_HEADER = 1 << 16  # codec headers are scanned within this many bytes of the data
ZSTD = {"name": "zstd", "configuration": {"level": 0, "checksum": False}}
SHUFFLE = {"name": "numcodecs.shuffle", "configuration": {"elementsize": 2}}
# JPEG XR pixel format GUIDs (as stored) each pixel type admits (spec/virtualize.md §13.4).
WIC = bytes.fromhex("24C3DD6F034EFE4BB1853D77768DC9")
JXR_FORMATS = {
    0: {WIC + b"\x08"},
    1: {WIC + b"\x0b"},
    2: {WIC + b"\x11"},
    3: {WIC + b"\x0c", WIC + b"\x0d"},
    4: {WIC + b"\x15"},
    9: {WIC + b"\x0f"},
    8: {bytes.fromhex("8FD7FEE3DBE8CF4A84C1E97F6136B327")},
}
SAMPLES = {1: [""], 3: ["B", "G", "R"], 4: ["B", "G", "R", "A"]}
RGB_SAMPLES = {1: [""], 3: ["R", "G", "B"], 4: ["R", "G", "B", "A"]}

Head = Callable[[int, int], "bytes | None"]  # (offset, length) within the data, or None past the bound


def bytes_codec(data_type: str) -> dict:
    return {"name": "bytes"} if data_type in ("uint8", "int8") else {"name": "bytes", "configuration": {"endian": "little"}}


def codec_chain(pixel_type: int, compression: int, hilo: bool) -> list[dict]:
    """The codecs after `transpose` (spec/virtualize/czi.md §3.1)."""
    data_type = PIXEL_TYPES[pixel_type][1]
    if compression == JPEG:
        return [{"name": "imagecodecs_jpeg"}]
    if compression == JPEGXR:
        return [{"name": "imagecodecs_jpegxr"}]
    if compression in (ZSTD0, ZSTD1):
        return [bytes_codec(data_type), *([SHUFFLE] if hilo else []), ZSTD]
    return [bytes_codec(data_type)]


def sample_letters(pixel_type: int, compression: int) -> list[str]:
    p = PIXEL_TYPES[pixel_type][2]
    return (RGB_SAMPLES if compression in (JPEG, JPEGXR) else SAMPLES)[p]


def zstd1_header(head: Head) -> tuple[int, bool] | None:
    """(header length, hi-lo flag) of a Zstd1 subblock's data, or None."""
    b = head(0, 1)
    if b is None:
        return None
    if b[0] == 1:
        return 1, False
    b = head(0, 3)
    if b is None or b[0] != 3 or b[1] != 1:
        return None
    return 3, bool(b[2] & 1)


def zstd_content_size(head: Head, at: int) -> int | None:
    """The content size a zstd frame header at `at` declares, or None."""
    b = head(at, 5)
    if b is None or b[:4] != b"\x28\xb5\x2f\xfd":
        return None
    h = b[4]
    if h & 0x08 or h & 0x03:
        return None
    single = bool(h & 0x20)
    n = {0: 1 if single else 0, 1: 2, 2: 4, 3: 8}[h >> 6]
    if n == 0:
        return None
    pos = at + 5 + (0 if single else 1)
    f = head(pos, n)
    if f is None:
        return None
    v = int.from_bytes(f, "little")
    return v + 256 if n == 2 else v


def jpeg_frame(head: Head, p: int) -> tuple[int, int] | None:
    """(width, height) of a JPEG stream's frame header, or None."""
    b = head(0, 2)
    if b is None or b != b"\xff\xd8":
        return None
    pos = 2
    while True:
        b = head(pos, 1)
        if b is None or b[0] != 0xFF:
            return None
        while b is not None and b[0] == 0xFF:
            pos += 1
            b = head(pos, 1)
        if b is None:
            return None
        m = b[0]
        pos += 1
        if 0xD0 <= m <= 0xD7 or m == 0x01:
            continue
        if m in (0xDA, 0xD9):
            return None
        lb = head(pos, 2)
        if lb is None:
            return None
        length = int.from_bytes(lb, "big")
        if length < 2:
            return None
        if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
            f = head(pos + 2, 6)
            if f is None:
                return None
            precision, height, width, count = f[0], int.from_bytes(f[1:3], "big"), int.from_bytes(f[3:5], "big"), f[5]
            if m in (0xC0, 0xC1, 0xC2) and precision == 8 and height >= 1 and width >= 1 and count == p:
                return width, height
            return None
        pos += length


def jpegxr_size(head: Head, pixel_type: int) -> tuple[int, int] | None:
    """(ImageWidth, ImageHeight) of a JPEG XR file whose pixel format the pixel type admits, or None."""
    b = head(0, 8)
    if b is None or b[:4] != b"\x49\x49\xbc\x01":
        return None
    ifd = struct.unpack_from("<I", b, 4)[0]
    c = head(ifd, 2)
    if c is None:
        return None
    count = struct.unpack("<H", c)[0]
    found: dict[int, bytes] = {}
    for k in range(count):
        e = head(ifd + 2 + 12 * k, 12)
        if e is None:
            return None
        tag = struct.unpack_from("<H", e)[0]
        if tag in (0xBC01, 0xBC80, 0xBC81) and tag not in found:
            found[tag] = e
            if len(found) == 3:
                break
    if len(found) < 3:
        return None
    _, typ, n = struct.unpack_from("<HHI", found[0xBC01])
    if typ != 1 or n != 16:
        return None
    guid = head(struct.unpack_from("<I", found[0xBC01], 8)[0], 16)
    if guid is None or guid not in JXR_FORMATS.get(pixel_type, ()):
        return None
    size = []
    for t in (0xBC80, 0xBC81):
        _, typ, n = struct.unpack_from("<HHI", found[t])
        if n != 1 or typ not in (3, 4):
            return None
        v = struct.unpack_from("<H" if typ == 3 else "<I", found[t], 8)[0]
        if v < 1:
            return None
        size.append(v)
    return size[0], size[1]


def coded_size(pixel_type: int, compression: int, width: int, height: int, n: int, head: Head
               ) -> tuple[int, int, bool, int] | None:
    """(coded width, coded height, hi-lo flag, header length) of a subblock whose
    stored size is width x height and whose data is n bytes, or None when it has
    no coded size (spec/virtualize/czi.md §3.2)."""
    q = PIXEL_TYPES[pixel_type][3]
    pixels = width * height * q
    if compression == UNCOMPRESSED:
        return (width, height, False, 0) if n >= pixels else None
    if compression == ZSTD0:
        return (width, height, False, 0) if zstd_content_size(head, 0) == pixels else None
    if compression == ZSTD1:
        h = zstd1_header(head)
        if h is None or n <= h[0] or (h[1] and pixel_type not in HILO_TYPES):
            return None
        return (width, height, h[1], h[0]) if zstd_content_size(head, h[0]) == pixels else None
    if compression == JPEG:
        f = jpeg_frame(head, PIXEL_TYPES[pixel_type][2])
        return None if f is None else (f[0], f[1], False, 0)
    f = jpegxr_size(head, pixel_type)
    return None if f is None else (f[0], f[1], False, 0)


def bounded_head(read: Callable[[int, int], bytes], start: int, n: int, first: bytes = b"") -> Head:
    """Reads within the first min(n, 2^16) bytes of the data at `start` (the
    first of them may be given), in growing windows so that a header costs few reads."""
    bound = min(n, MAX_HEADER)
    cache = [first[:bound]]

    def head(offset: int, length: int) -> bytes | None:
        if offset < 0 or offset + length > bound:
            return None
        if offset + length > len(cache[0]):
            want = min(bound, max(offset + length, 2 * len(cache[0]), 64))
            cache[0] = read(start, want)
        return cache[0][offset : offset + length]

    return head
