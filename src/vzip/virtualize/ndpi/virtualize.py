"""The NDPI profile (profiles/ndpi.md, §4), a variant of the TIFF profile.

Hamamatsu NDPI is a little-endian classic TIFF with 64-bit offsets (an 8-byte
first-IFD offset, 8-byte next-IFD offsets, and a high word per entry after
each IFD), whose pyramid levels are single JPEG strips with restart markers.
Each chunk is a JPEG stream rebuilt from the strip's header, a literal frame
header for the chunk's size, and `a × b` restart intervals of the strip.
"""

from __future__ import annotations

import math
import struct

from vzip.virtualize.common import (
    MAX_PAYLOAD, Output, Reader, Rejected, array_json, centred, group_json, image_ome, payload_size, transpose_codec,
)

MAX_IFDS = 100000
MAX_SAFE = 2**53 - 1
SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}
FORMATS = {1: "B", 3: "H", 4: "I", 8: "h", 9: "i", 13: "I", 16: "Q", 18: "Q", 11: "f", 12: "d"}
OFFSET_TYPES = {3, 4, 8, 9}  # X/YOffsetFromSlideCenter may be signed
INTEGER_TYPES = {1, 3, 4, 13, 16, 18}
# The tags of §4, with their allowed field types and whether they are scalars.
TAGS = {
    256: (INTEGER_TYPES, True), 257: (INTEGER_TYPES, True), 258: (INTEGER_TYPES, False),
    259: (INTEGER_TYPES, True), 262: (INTEGER_TYPES, True), 277: (INTEGER_TYPES, True),
    273: (INTEGER_TYPES, True), 279: (INTEGER_TYPES, True), 282: ({5}, True), 283: ({5}, True),
    296: (INTEGER_TYPES, True), 65420: (INTEGER_TYPES, True), 65421: ({11, 12}, True),
    65422: (OFFSET_TYPES, True), 65423: (OFFSET_TYPES, True),
    65426: (INTEGER_TYPES, False), 65432: (INTEGER_TYPES, False),
}
CHUNK = 1024  # target chunk size in pixels


def detect(read: Reader, size: int) -> int | None:
    """The first IFD's offset if the file is NDPI (§4), else None."""
    head = read(0, min(12, size))
    if len(head) < 12 or head[:4] != b"II*\x00":
        return None
    v = struct.unpack("<Q", head[4:12])[0]
    if v < 16 or v + 2 > size:
        return None
    n = struct.unpack("<H", read(v, 2))[0]
    if v + 2 + 12 * n > size:
        return None
    entries = read(v + 2, 12 * n)
    tags = {struct.unpack("<H", entries[12 * i : 12 * i + 2])[0] for i in range(n)}
    return v if 65420 in tags else None


def read_ifds(read: Reader, size: int, first: int) -> list[dict[int, list]]:
    """The main chain's IFDs, as {tag: values} (§4)."""
    seen: set[int] = set()
    ifds = []
    offset = first
    while offset:
        if offset < 16 or offset > MAX_SAFE:
            raise Rejected(f"IFD offset {offset} is not in the file")
        if offset in seen:
            raise Rejected(f"IFD offset {offset} read twice")
        if len(seen) >= MAX_IFDS:
            raise Rejected("too many IFDs")
        seen.add(offset)
        n = struct.unpack("<H", read(offset, 2))[0]
        body = read(offset + 2, 12 * n + 8 + 4 * n)
        nxt = struct.unpack("<Q", body[12 * n : 12 * n + 8])[0]
        tags: dict[int, list] = {}
        for i in range(n):
            tag, typ, count = struct.unpack("<HHI", body[12 * i : 12 * i + 8])
            if tag not in TAGS or tag in tags:
                continue  # unused, or a duplicate (the first is used)
            allowed, scalar = TAGS[tag]
            if typ not in allowed:
                raise Rejected(f"tag {tag} has field type {typ}")
            if scalar and count == 0:
                raise Rejected(f"tag {tag} has no value")
            field = body[12 * i + 8 : 12 * i + 12]
            low = struct.unpack("<I", field)[0]
            high = struct.unpack("<I", body[12 * n + 8 + 4 * i : 12 * n + 12 + 4 * i])[0]
            nbytes = count * SIZES[typ]
            if nbytes <= 4:
                if count == 1 and typ in (4, 13):
                    values = [low + (high << 32)]
                else:
                    values = _values(field[:nbytes], typ, count)
            else:
                values = _values(read(low + (high << 32), nbytes), typ, count)
            if typ in INTEGER_TYPES and any(v > MAX_SAFE for v in values):
                raise Rejected(f"tag {tag} has a value above 2^53 - 1")
            tags[tag] = values
        ifds.append(tags)
        offset = nxt
    if not ifds:
        raise Rejected("no images")
    return ifds


def _values(data: bytes, typ: int, count: int) -> list:
    if typ == 5:
        v = struct.unpack(f"<{2 * count}I", data)
        return [(v[2 * i], v[2 * i + 1]) for i in range(count)]
    return list(struct.unpack(f"<{count}{FORMATS[typ]}", data))


def _one(tags: dict, tag: int, what: str, default=None):
    v = tags.get(tag)
    if v is None:
        if default is None:
            raise Rejected(f"an NDPI image has no {what}")
        return default
    return v[0]


def jpeg_header(header: bytes):
    """(SOF0 start, SOF0 end, MCU width, MCU height, restart interval) of a
    strip's header, which runs from SOI to the end of SOS (§4)."""
    if header[:2] != b"\xff\xd8":
        raise Rejected("an NDPI strip does not start with a JPEG SOI marker")
    pos, sof, dri = 2, None, None
    while True:
        if pos + 4 > len(header) or header[pos] != 0xFF:
            raise Rejected("malformed JPEG header in an NDPI strip")
        marker = header[pos + 1]
        length = struct.unpack(">H", header[pos + 2 : pos + 4])[0]
        end = pos + 2 + length
        if length < 2 or end > len(header):
            raise Rejected("malformed JPEG header in an NDPI strip")
        if marker == 0xC0:
            if sof is not None:
                raise Rejected("an NDPI strip has two SOF0 segments")
            sof = (pos, end)
        elif 0xC1 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            raise Rejected(f"an NDPI strip is not baseline JPEG (marker FF{marker:02X})")
        elif marker == 0xDD:
            if length != 4:
                raise Rejected("malformed DRI segment in an NDPI strip")
            dri = struct.unpack(">H", header[pos + 4 : pos + 6])[0]
        elif marker == 0xDA:
            if end != len(header):
                raise Rejected("an NDPI strip's SOS does not end at McuStarts[0]")
            break
        pos = end
    if sof is None or not dri:
        raise Rejected("an NDPI strip has no SOF0 or no restart interval")
    seg = header[sof[0] : sof[1]]
    nf = seg[9] if len(seg) > 9 else 0
    if len(seg) != 10 + 3 * nf or nf == 0:
        raise Rejected("malformed SOF0 segment in an NDPI strip")
    h = max(seg[11 + 3 * k] >> 4 for k in range(nf))
    v = max(seg[11 + 3 * k] & 15 for k in range(nf))
    if h == 0 or v == 0:
        raise Rejected("an NDPI strip has a sampling factor of 0")
    return sof[0], sof[1], 8 * h, 8 * v, dri


def virtualize_ndpi(url: str, read: Reader, size: int, first: int) -> Output:
    ifds = read_ifds(read, size, first)
    levels = []
    for tags in ifds:
        mag = _one(tags, 65421, "Magnification")
        if not mag > 0:
            continue
        width, height = _one(tags, 256, "ImageWidth"), _one(tags, 257, "ImageLength")
        bits = tags.get(258)
        if (_one(tags, 259, "Compression", 1) != 7 or _one(tags, 262, "PhotometricInterpretation") != 6
                or _one(tags, 277, "SamplesPerPixel", 1) != 3 or not bits or any(b != 8 for b in bits)):
            raise Rejected("an NDPI level is not 8-bit YCbCr JPEG with 3 samples")
        if len(tags.get(273, [])) != 1 or len(tags.get(279, [])) != 1:
            raise Rejected("an NDPI level does not have exactly one strip")
        if levels and not (width < levels[-1]["w"] and height < levels[-1]["h"]):
            raise Rejected("NDPI levels do not decrease in size")
        if any(level["mag"] == mag for level in levels):
            raise Rejected("NDPI focal planes (two levels with one magnification) are not supported")
        if min(width, height) < 1:
            raise Rejected("an NDPI level is empty")
        levels.append({"mag": mag, "w": width, "h": height, "tags": tags})
    if not levels:
        raise Rejected("no NDPI levels")

    # Scale (§4).
    base = levels[0]
    unit_code = _one(base["tags"], 296, "ResolutionUnit", 2)
    per_unit = {3: 10000.0, 2: 25400.0}.get(unit_code)

    def physical(tag: int):
        r = base["tags"].get(tag)
        if per_unit is None or r is None or r[0][1] == 0 or r[0][0] == 0:
            return None
        return per_unit / (r[0][0] / r[0][1])

    px, py = physical(282), physical(283)
    units = {a: "micrometer" for a, p in (("x", px), ("y", py)) if p is not None}

    axes = ["c", "y", "x"]
    codecs = [transpose_codec(axes), {"name": "imagecodecs_jpeg"}]
    out = Output(url)
    scales = []
    for li, level in enumerate(levels):
        tags = level["tags"]
        w, h = level["w"], level["h"]
        s0, n = tags[273][0], tags[279][0]
        if s0 + n > size or n < 4:
            raise Rejected("an NDPI strip is outside the file")
        starts = tags.get(65426)
        if starts is None:
            chunk_shape = [3, h, w]
            out.refs[f"{li}/c/0/0/0"] = [(s0, n)]
        else:
            chunk_shape = _intervals(out, li, read, tags, starts, s0, n, w, h)
        out.json(f"{li}/zarr.json", array_json([3, h, w], "uint8", chunk_shape, codecs, axes))
        scales.append([1, (py or 1) * (base["h"] / h), (px or 1) * (base["w"] / w)])
    # Position (§4): the image's centre, from the slide's centre in nm.
    translation = None
    offset_x, offset_y = base["tags"].get(65422), base["tags"].get(65423)
    if px is not None and py is not None and offset_x and offset_y:
        corner = centred(offset_x[0] / 1000, offset_y[0] / 1000, base["w"], base["h"], px, py)
        translation = [0, corner["y"], corner["x"]]
    out.json("zarr.json", group_json(image_ome(axes, units, scales, None,
                                               [translation] * len(levels) if translation else None)))
    out.summary = {"axes": axes, "levels": [[3, lv["h"], lv["w"]] for lv in levels], "references": len(out.refs),
                   "codec": "imagecodecs_jpeg"}
    return out


def _intervals(out: Output, li: int, read: Reader, tags: dict, starts: list, s0: int, n: int, w: int, h: int):
    """Writes a McuStarts level's chunk references (§4); returns its chunk shape."""
    high = tags.get(65432)
    if high is not None:
        if len(high) != len(starts):
            raise Rejected("McuStartsHighBytes and McuStarts differ in length")
        starts = [s + (hi << 32) for s, hi in zip(starts, high)]
    if not starts or starts[0] < 2 or starts[0] > n:
        raise Rejected("McuStarts[0] is outside the strip")
    header = read(s0, starts[0])
    sof_start, sof_end, mw, mh, interval = jpeg_header(header)
    q, r = math.ceil(w / (interval * mw)), math.ceil(h / mh)
    if len(starts) != q * r or starts[-1] >= n or any(b <= a for a, b in zip(starts, starts[1:])):
        raise Rejected("McuStarts does not match the strip's intervals")
    ends = [b - 2 for b in starts[1:]] + [n - 2]
    if any(e <= s for s, e in zip(starts, ends)):
        raise Rejected("an NDPI restart interval is empty")
    a = min(q, max(1, CHUNK // (interval * mw)))
    sof = bytearray(header[sof_start:sof_end])

    def chunks(b: int):
        sof[5:7] = struct.pack(">H", b * mh)
        sof[7:9] = struct.pack(">H", a * interval * mw)
        head = [(s0, sof_start), bytes(sof), (s0 + sof_end, starts[0] - sof_end)]
        for u in range(math.ceil(r / b)):
            for v in range(math.ceil(q / a)):
                ranges = list(head)
                t = 0
                for y in range(b):
                    for x in range(a):
                        i = min(u * b + y, r - 1) * q + min(v * a + x, q - 1)
                        if t:
                            ranges.append(bytes([0xFF, 0xD0 + (t - 1) % 8]))
                        ranges.append((s0 + starts[i], ends[i] - starts[i]))
                        t += 1
                ranges.append(b"\xff\xd9")
                yield u, v, ranges

    for b in range(max(1, min(r, CHUNK // mh)), 0, -1):
        if b == 1 or all(payload_size(rs) <= MAX_PAYLOAD for _, _, rs in chunks(b)):
            break
    for u, v, ranges in chunks(b):
        if payload_size(ranges) > MAX_PAYLOAD:
            raise Rejected(f"an NDPI chunk's reference payload exceeds {MAX_PAYLOAD} bytes")
        out.refs[f"{li}/c/0/{u}/{v}"] = ranges
    return [3, b * mh, a * interval * mw]
