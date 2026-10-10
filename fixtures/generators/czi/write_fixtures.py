"""Writes the synthetic CZI fixtures in fixtures/czi/ (spec/virtualize/czi.md).

A small struct-based CZI writer (`Czi`): no library writes pyramids, JPEG XR,
attachments, deleted segments or broken files. Each accepted fixture is
cross-checked once here: czifile must parse it and decode every subblock the
convention places, and pylibCZIrw (libCZI), where installed, must open it and
read its bounding box. Rejected fixtures (`czi_reject_*`) each break one rule
the profile rejects.

Usage: uv run python fixtures/generators/czi/write_fixtures.py [--big <dir>]
  --big writes the row-band fixture (over 16 MiB per tile) and the 2^16 + 1
  series rejection into <dir> instead (tests generate them at test time).
"""

from __future__ import annotations

import gzip
import struct
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import imagecodecs
import numpy as np


def pinned_gzip(data: bytes, level: int = 9) -> bytes:
    """gzip.compress with modification time 0 and the header's OS byte pinned to 255
    ("unknown"): zlib writes its build's OS code there (3 on Linux, 19 on macOS), so
    unpinned fixtures differ between platforms."""
    out = bytearray(gzip.compress(data, compresslevel=level, mtime=0))
    out[9] = 255
    return bytes(out)

HERE = Path(__file__).parent
OUT = HERE.parents[1] / "czi"
TYPES = {0: ("u1", 1), 1: ("<u2", 1), 2: ("<f4", 1), 3: ("u1", 3), 4: ("<u2", 3), 8: ("<f4", 3), 9: ("u1", 4),
         10: ("<c8", 1), 11: ("<c8", 3), 12: ("<i4", 1), 13: ("<f8", 1)}
WIC = bytes.fromhex("24C3DD6F034EFE4BB1853D77768DC9")
RNG = np.random.default_rng(20261008)


# ---- pixels and codecs

def pixels(pixel_type: int, w: int, h: int) -> np.ndarray:
    """Random pixels of a pixel type, [h, w] or [h, w, samples], as stored (B, G, R, A)."""
    dt, p = TYPES[pixel_type]
    shape = (h, w) if p == 1 else (h, w, p)
    kind = np.dtype(dt)
    if kind.kind == "c":
        return (RNG.standard_normal(shape) + 1j * RNG.standard_normal(shape)).astype(dt)
    if kind.kind == "f":
        return RNG.standard_normal(shape).astype(dt)
    info = np.iinfo(kind)
    return RNG.integers(info.min, info.max, shape, endpoint=True).astype(dt)


def raw(a: np.ndarray) -> bytes:
    return np.ascontiguousarray(a).tobytes()


def zstd0(a: np.ndarray) -> bytes:
    return imagecodecs.zstd_encode(raw(a))


def zstd1(a: np.ndarray, hilo: bool = False) -> bytes:
    b = raw(a)
    if hilo:
        b = np.frombuffer(b, "u1").reshape(-1, 2).T.tobytes()  # low bytes, then high bytes
        return b"\x03\x01\x01" + imagecodecs.zstd_encode(b)
    return b"\x01" + imagecodecs.zstd_encode(b)


def jpeg(a: np.ndarray) -> bytes:
    return imagecodecs.jpeg8_encode(a, level=95)


def set_jxr_format(b: bytes, guid: bytes) -> bytes:
    b = bytearray(b)
    ifd = struct.unpack_from("<I", b, 4)[0]
    for k in range(struct.unpack_from("<H", b, ifd)[0]):
        tag, _, _, value = struct.unpack_from("<HHII", b, ifd + 2 + 12 * k)
        if tag == 0xBC01:
            b[value : value + 16] = guid
    return bytes(b)


def jxr(a: np.ndarray, guid: bytes | None = None) -> bytes:
    b = imagecodecs.jpegxr_encode(a, level=1.0)
    return set_jxr_format(b, guid) if guid else b


# ---- the writer

def dv_entry(pixel_type: int, compression: int, position: int, dims: list, pyramid: int = 0,
             spare: bytes = bytes(5), file_part: int = 0, schema: bytes = b"DV") -> bytes:
    """A directory entry of schema DV; `dims` are (letter, start, size, stored, coordinate)."""
    b = schema + struct.pack("<iqii", pixel_type, position, file_part, compression) + bytes([pyramid]) + spare
    b += struct.pack("<i", len(dims))
    for letter, start, size, stored, *coordinate in dims:
        ident = letter.encode().ljust(4, b"\0") if isinstance(letter, str) else letter
        b += ident + struct.pack("<iifi", start, size, coordinate[0] if coordinate else 0.0, stored)
    return b


def segment(ident: str | bytes, data: bytes, allocated: int | None = None, used: int | None = None) -> bytes:
    ident = ident.encode() if isinstance(ident, str) else ident
    allocated = (len(data) + 31) // 32 * 32 if allocated is None else allocated
    used = len(data) if used is None else used
    return ident.ljust(16, b"\0") + struct.pack("<qq", allocated, used) + data.ljust(allocated, b"\0")[:max(allocated, 0)]


@dataclass
class Sb:
    pixel_type: int
    compression: int
    dims: list  # (letter, start, size, stored[, coordinate])
    data: bytes
    metadata: bytes = b""
    attachment: bytes = b""
    pyramid: int = 0
    spare: bytes = bytes(5)
    copy: object = None  # bytes -> bytes: changes the subblock's own copy of its entry
    entry: object = None  # bytes -> bytes: changes the directory's entry
    segment_id: str = "ZISRAWSUBBLOCK"
    sizes: tuple | None = None  # (MetadataSize, AttachmentSize, DataSize) as written


@dataclass
class Att:
    name: str
    content_type: str
    data: bytes
    schema: bytes = b"A1"
    file_part: int = 0
    segment_entry: object = None  # bytes -> bytes: changes the segment's copy of the entry
    segment_id: str = "ZISRAWATTACH"
    guid: bytes = b""


@dataclass
class Czi:
    subblocks: list[Sb] = field(default_factory=list)
    xml: bytes | None = None
    metadata_attachment: bytes = b""
    attachments: list[Att] | None = None
    early: list[bytes] = field(default_factory=list)  # segments after the file header
    late: list[bytes] = field(default_factory=list)  # segments after the directory
    tail: bytes = b""
    major: int = 1
    minor: int = 0
    file_part: int = 0
    update_pending: int = 0
    directory_id: str = "ZISRAWDIRECTORY"
    directory_count: int | None = None
    directory_used: int | None = None
    metadata_id: str = "ZISRAWMETADATA"
    metadata_sizes: tuple | None = None
    attdir_id: str = "ZISRAWATTDIR"
    attdir_count: int | None = None
    no_directory: bool = False
    offsets: dict = field(default_factory=dict)

    def build(self) -> bytes:
        out = bytearray(segment("ZISRAWFILE", bytes(512)))
        for s in self.early:
            out += s
        entries = []
        for i, sb in enumerate(self.subblocks):
            at = len(out)
            self.offsets[f"subblock{i}"] = at
            entry = dv_entry(sb.pixel_type, sb.compression, at, sb.dims, sb.pyramid, sb.spare)
            copy = sb.copy(entry) if sb.copy else entry
            entries.append(sb.entry(entry) if sb.entry else entry)
            dims = struct.unpack_from("<i", copy, 28)[0] if len(copy) >= 32 else 0
            length = max(256, 48 + 20 * max(dims, 0))
            m, a, n = sb.sizes or (len(sb.metadata), len(sb.attachment), len(sb.data))
            head = (struct.pack("<iiq", m, a, n) + copy).ljust(length, b"\0")
            out += segment(sb.segment_id, head + sb.metadata + sb.data + sb.attachment)
        metadata = 0
        if self.xml is not None:
            metadata = len(out)
            x, b = self.metadata_sizes or (len(self.xml), len(self.metadata_attachment))
            out += segment(self.metadata_id, struct.pack("<ii", x, b).ljust(256, b"\0") + self.xml
                           + self.metadata_attachment)
        attdir = 0
        if self.attachments is not None:
            att_entries = []
            for k, att in enumerate(self.attachments):
                at = len(out)
                self.offsets[f"attachment{k}"] = at
                guid = att.guid or uuid.UUID(int=k + 1).bytes_le
                entry = (att.schema + bytes(10) + struct.pack("<qi", at, att.file_part) + guid
                         + att.content_type.encode().ljust(8, b"\0") + att.name.encode().ljust(80, b"\0"))
                att_entries.append(entry)
                if att.schema != b"A1":
                    continue
                copy = att.segment_entry(entry) if att.segment_entry else entry
                out += segment(att.segment_id, (struct.pack("<q", len(att.data)) + bytes(8) + copy).ljust(256, b"\0")
                               + att.data)
            attdir = len(out)
            count = len(att_entries) if self.attdir_count is None else self.attdir_count
            out += segment(self.attdir_id, struct.pack("<i", count).ljust(256, b"\0") + b"".join(att_entries))
        directory = len(out)
        body = b"".join(entries)
        count = len(entries) if self.directory_count is None else self.directory_count
        data = struct.pack("<i", count).ljust(128, b"\0") + body
        out += segment(self.directory_id, data, used=self.directory_used)
        for s in self.late:
            out += s
        out += self.tail
        guid = uuid.UUID("9314a6f8-3e05-4ec9-bae0-bc7e96d18997").bytes_le
        header = struct.pack("<ii", self.major, self.minor) + bytes(8) + guid + guid + struct.pack(
            "<iqqiq", self.file_part, 0 if self.no_directory else directory, metadata, self.update_pending, attdir)
        out[32 : 32 + len(header)] = header
        self.offsets.update(directory=directory, metadata=metadata, attdir=attdir)
        return bytes(out)


def plane(pixel_type: int, compression: int, x: int, y: int, w: int, h: int, *, wl: int | None = None,
          hl: int | None = None, stored: np.ndarray | None = None, coded: bytes | None = None, hilo: bool = False,
          **dims) -> Sb:
    """A subblock of stored size w x h at (x, y), logical size wl x hl (default w x h), with
    the other dimensions as keyword arguments (C=1, ...), its pixels random unless given."""
    a = pixels(pixel_type, w, h) if stored is None else stored
    if coded is None:
        coded = {0: raw, 5: zstd0, 1: jpeg, 4: jxr}[compression](a) if compression != 6 else zstd1(a, hilo)
    letters = [("X", x, wl or w, w), ("Y", y, hl or h, h)]
    for letter, v in dims.items():
        letters.append((letter, v, 1, 1) if letter != "M" else (letter, v, 1, 1))
    return Sb(pixel_type, compression, letters, coded)


def xml(*, px=None, py=None, pz=None, increment=None, bits=None, channels=(), display=(), scenes=(), bom=False,
        raw_text: str | None = None) -> bytes:
    """A metadata XML with the values spec/virtualize/czi.md §2.7 reads."""
    if raw_text is not None:
        return raw_text.encode("utf-8")
    items = "".join(f'<Distance Id="{a}"><Value>{v}</Value><DefaultUnitFormat>µm</DefaultUnitFormat></Distance>'
                    for a, v in (("X", px), ("Y", py), ("Z", pz)) if v is not None)
    chans = "".join(f'<Channel Id="Channel:{k}"{f" Name=\"{n}\"" if n is not None else ""}>'
                    + (f"<Color>{c}</Color>" if c else "") + (f"<ComponentBitCount>{b}</ComponentBitCount>" if b else "")
                    + "</Channel>" for k, (n, c, b) in enumerate(channels))
    disp = "".join(f'<Channel Id="Channel:{k}"{f" Name=\"{n}\"" if n is not None else ""}>'
                   + (f"<Low>{lo}</Low>" if lo is not None else "") + (f"<High>{hi}</High>" if hi is not None else "")
                   + (f"<Color>{c}</Color>" if c else "") + "</Channel>" for k, (n, c, lo, hi) in enumerate(display))
    sc = "".join(f'<Scene Index="{i}" Name="{n}"/>' for i, n in scenes)
    t = (f"<T><Positions><Interval><Increment>{increment}</Increment></Interval></Positions></T>"
         if increment is not None else "")
    text = ("<?xml version=\"1.0\"?>\n<ImageDocument><Metadata><Information><Image>"
            + (f"<ComponentBitCount>{bits}</ComponentBitCount>" if bits is not None else "")
            + f"<Dimensions>{t}<Channels>{chans}</Channels><S><Scenes>{sc}</Scenes></S></Dimensions></Image>"
            + f"</Information><Scaling><Items>{items}</Items></Scaling>"
            + f"<DisplaySetting><Channels>{disp}</Channels></DisplaySetting></Metadata></ImageDocument>")
    return (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")


def time_stamps(values, size: int | None = None) -> bytes:
    return struct.pack("<ii", 8 + 8 * len(values) if size is None else size, len(values)) + struct.pack(
        f"<{len(values)}d", *values)


def event_list(events) -> bytes:
    body = b"".join(struct.pack("<idii", 20 + len(d), t, k, len(d)) + d for t, k, d in events)
    return struct.pack("<ii", 8 + len(body), len(events)) + body


def submeta(k: int) -> bytes:
    return (f"<METADATA><Tags><AcquisitionTime>2026-10-08T00:00:{k:02d}</AcquisitionTime>"
            f"<StageXPosition>{100 + k}.5</StageXPosition></Tags></METADATA>").encode()


# ---- fixtures

def grid(pixel_type, compression, cols, rows, w, h, *, x0=0, y0=0, edge=None, factor=1, skip=(), **dims):
    """A lattice of subblocks: (cols x rows) tiles of stored w x h, the last column and row
    of stored size `edge` (w', h'), each of logical size factor x stored."""
    out = []
    for j in range(rows):
        for i in range(cols):
            if (i, j) in skip:
                continue
            sw = edge[0] if edge and i == cols - 1 else w
            sh = edge[1] if edge and j == rows - 1 else h
            out.append(plane(pixel_type, compression, x0 + i * w * factor, y0 + j * h * factor, sw, sh,
                             wl=sw * factor, hl=sh * factor, **dims))
    return out


def accepted() -> dict[str, Czi]:
    f: dict[str, Czi] = {}
    f["czi_gray8_single"] = Czi([plane(0, 0, 0, 0, 13, 7, C=0)], xml(px=2.5e-7, py=2.5e-7, channels=[("DAPI", "#FF0000FF", 8)]))
    sbs = []
    k = 0
    for t in range(2):
        for c in range(3):
            for z in range(4):
                sb = plane(1, 0, 0, 0, 11, 9, T=t, C=c, Z=z)
                sb.metadata = submeta(k)
                k += 1
                sbs.append(sb)
    f["czi_gray16_tczyx"] = Czi(sbs, xml(px=1e-7, py=1.2e-7, pz=5e-7, increment=0.5, bits=12,
                                         channels=[("DAPI", "#FF0000FF", 14), ("", "#00FF00", None), (None, None, 99)],
                                         display=[(None, "#FFFF0000", 0.01, 0.25), ("disp1", "#00FF00", None, 0.5),
                                                  ("disp2", "not a color", "x", None)]),
                                attachments=[Att("TimeStamps", "CZTIMS", time_stamps([0.0, 0.5])),
                                             Att("EventList", "CZEVL", event_list([(0.25, 1, b"start"), (0.75, 2, b"")])),
                                             Att("Thumbnail", "JPG", jpeg(pixels(3, 8, 8)))])
    f["czi_bgr24_raw"] = Czi([plane(3, 0, 0, 0, 10, 6, C=0)], xml(channels=[("Brightfield", None, None)]))
    f["czi_bgra32_raw"] = Czi([plane(9, 0, 0, 0, 10, 6)])
    f["czi_bgr48_raw"] = Czi([plane(4, 0, 0, 0, 10, 6, C=0), plane(4, 0, 0, 0, 10, 6, C=1)])
    f["czi_types"] = Czi([plane(t, 0, 0, 0, 6, 5, S=k) for k, t in enumerate((2, 12, 13, 10, 8, 11))],
                         xml(scenes=[(0, "float"), (1, "int32"), (2, "double"), (3, ""), (4, "x" * 300)]))
    f["czi_zstd"] = Czi([plane(0, 5, 0, 0, 12, 8, S=0), plane(1, 6, 0, 0, 12, 8, S=1),
                         plane(1, 6, 0, 0, 12, 8, S=2, hilo=True), plane(4, 6, 0, 0, 12, 8, S=3, hilo=True),
                         plane(12, 5, 0, 0, 12, 8, S=4), plane(2, 6, 0, 0, 12, 8, S=5)])
    f["czi_jpeg_grid"] = Czi(grid(3, 1, 2, 2, 16, 8, C=0) + grid(0, 1, 2, 1, 16, 8, S=1))
    f["czi_jxr"] = Czi([plane(0, 4, 0, 0, 10, 8, S=0), plane(1, 4, 0, 0, 10, 8, S=1), plane(2, 4, 0, 0, 10, 8, S=2),
                        plane(3, 4, 0, 0, 10, 8, S=3),
                        plane(3, 4, 0, 0, 10, 8, S=4, coded=jxr(pixels(3, 10, 8), WIC + b"\x0c")),
                        plane(4, 4, 0, 0, 10, 8, S=5),
                        plane(9, 4, 0, 0, 10, 8, S=6, coded=jxr(pixels(9, 10, 8), WIC + b"\x0f")),
                        plane(8, 4, 0, 0, 10, 8, S=7)])
    # A slide scan: overlapping layer-0 tiles, then layers 1 and 2 on a lattice, clipped.
    layer0 = [plane(1, 4, x, y, 20, 16, C=0, M=k) for k, (x, y) in enumerate([(-100, 50), (-82, 50), (-100, 64), (-82, 66)])]
    layer1 = grid(1, 4, 2, 2, 10, 8, x0=-100, y0=50, edge=(9, 5), factor=2, C=0)
    layer2 = grid(1, 4, 1, 1, 10, 8, x0=-100, y0=50, edge=(10, 8), factor=4, C=0)
    for sb in layer1 + layer2:
        sb.pyramid = 2
    f["czi_jxr_pyramid"] = Czi(layer0 + layer1 + layer2, xml(px=3.444225755520869e-07, py=3.444225755520869e-07,
                                                              display=[("EGFP", "#FF00FF5B", None, 0.25)],
                                                              channels=[("EGFP", None, 14)], scenes=[(0, "ScanRegion0")]))
    for sb in f["czi_jxr_pyramid"].subblocks:
        sb.dims.append(("S", 0, 1, 1))
    f["czi_regular_holes"] = Czi(grid(1, 0, 3, 2, 8, 6, skip={(1, 0), (2, 1)}, C=0)
                                 + grid(1, 0, 3, 2, 8, 6, skip={(0, 1)}, C=1))
    f["czi_clipped_raw"] = Czi(grid(0, 0, 3, 2, 8, 6, edge=(5, 4), Z=0) + grid(0, 0, 3, 2, 8, 6, edge=(5, 4), Z=1))
    over = []
    for c in range(2):
        for z in range(2):
            for k, (x, y) in enumerate([(0, 0), (12, 1), (0, 9)]):
                over.append(plane(1, 0, x, y, 14, 10, C=c, Z=z, M=k))
    f["czi_mosaic_overlap"] = Czi(over)
    f["czi_irregular"] = Czi([plane(0, 0, 0, 0, 10, 10, C=0, M=0), plane(0, 0, 3, 3, 10, 10, C=0, M=1),
                              plane(0, 0, 3, 3, 10, 10, C=0, M=2),  # the same position and plane: a copy
                              plane(0, 0, 3, 3, 8, 10, C=0, M=3),  # the same position, another size
                              plane(0, 0, 3, 3, 10, 10, C=1, M=1),
                              plane(0, 5, 3, 3, 10, 10, C=2, M=1)])  # the same position, another codec form
    multi = []
    for s, name in enumerate(("A1", "B2", "C3")):
        multi.append(plane(0, 0, 100 * s, 0, 6, 4, S=s, C=0))
    multi += [plane(0, 0, 0, 0, 6, 4, H=h, B=1, I=2, R=0, V=3, C=0) for h in range(2)]
    f["czi_multiscene"] = Czi(multi, xml(scenes=[(0, "A1"), (1, "B2"), (2, "C3"), (1, "dup")]))
    f["czi_mixed_types"] = Czi([plane(0, 0, 0, 0, 6, 4, C=0), plane(1, 0, 0, 0, 6, 4, C=1)])
    f["czi_subsampled"] = Czi([plane(1, 0, 0, 0, 8, 6, wl=16, hl=12, C=0, Z=z) for z in range(3)])
    f["czi_line_scan"] = Czi([plane(1, 0, 0, 0, 512, 1, T=t) for t in range(5)], xml(increment=0.001))
    orphan = segment("ZISRAWSUBBLOCK", bytes(300))
    f["czi_deleted"] = Czi([plane(0, 0, 0, 0, 4, 4)], xml(),
                           early=[segment("DELETED", b"old directory bytes"), orphan],
                           late=[segment("DELETED", b"x" * 40, used=0), segment("UNKNOWN_ID", b"?")],
                           tail=b"\x01garbage that is not a segment")
    f["czi_entry_mismatch"] = Czi([plane(0, 0, 0, 0, 4, 4, C=0),
                                   _patched(plane(0, 0, 4, 0, 4, 4, C=0), lambda e: e[:6] + struct.pack("<q", 1) + e[14:])])
    short = plane(0, 1, 0, 0, 16, 9, wl=32, hl=18, C=0)
    short.data = jpeg(pixels(0, 16, 8))  # coded one row short of its stored height
    full = plane(0, 1, 0, 0, 32, 18, C=0)
    f["czi_pyramid_edge_bug"] = Czi([full, short])
    bad_xr = plane(1, 4, 0, 0, 6, 4, S=2, coded=jxr(pixels(0, 6, 4)))  # an 8-bit pixel format for Gray16
    f["czi_unplaced"] = Czi([
        plane(1, 5, 0, 0, 6, 4, S=0, coded=zstd0(pixels(1, 6, 3))),  # content size of 3 rows
        plane(1, 0, 0, 0, 6, 4, S=1, coded=raw(pixels(1, 6, 3))),  # short uncompressed data
        bad_xr,
        plane(1, 6, 0, 0, 6, 4, S=3, coded=b"\x02" + zstd0(pixels(1, 6, 4))),  # a bad Zstd1 header
        plane(0, 6, 0, 0, 6, 4, S=4, coded=b"\x03\x01\x01" + zstd0(pixels(0, 6, 4))),  # hi-lo on Gray8
        plane(0, 1, 0, 0, 6, 4, S=5, coded=b"\xff\xd8\xff\xda\x00\x02"),  # a scan before the frame header
        plane(0, 0, 0, 0, 6, 4, S=6),  # placed
    ])
    tr = plane(1, 0, 0, 0, 6, 4)
    tr.data += b"trailing bytes"
    f["czi_trailing"] = Czi([tr])
    sa = [plane(0, 0, 0, 0, 6, 4, C=c) for c in range(3)]
    sa[0].attachment, sa[1].attachment, sa[2].attachment = b"M" * 40, b"N" * 40, b"O" * 41
    sa[0].metadata = b"<METADATA><AttachmentSchema>CHUNKCONTAINER</AttachmentSchema></METADATA>"
    f["czi_subblock_attachments"] = Czi(sa)
    eq = [plane(0, 0, 0, 0, 6, 4, C=c) for c in range(3)]
    for sb in eq:
        sb.attachment = b"mask" * 10
    f["czi_subblock_masks"] = Czi(eq)
    inner = Czi([plane(3, 0, 0, 0, 8, 4)]).build()
    f["czi_attachments"] = Czi([plane(0, 0, 0, 0, 4, 4)], xml(), attachments=[
        Att("FocusPositions", "CZFOC", time_stamps([1.5, 2.5, 3.5], size=12)),
        Att("LookupTables", "CZLUT", b"<LUT/>"),
        Att("Label", "CZI", inner),
        Att("Profile", "Zip-Comp", pinned_gzip(b"<Profile/>")),
        Att("Odd", "BINARY", b"", schema=b"A2"),
        Att("Changed", "BINARY", b"payload", segment_entry=lambda e: e[:48] + b"Renamed".ljust(80, b"\0")),
        Att("Empty", "BINARY", b""),
        Att("TimeStamps", "CZTIMS", time_stamps([1.0, 2.0]) + b"extra"),
        Att("EventList", "CZEVL", event_list([(1.0, 3, b"abc")]) + b"!"),
        Att("Caf\xe9", "PNG", b"\x89PNG", guid=bytes(range(16))),
        Att("Events0", "CZEVL", event_list([])),
        Att("Times0", "CZTIMS", time_stamps([])),
    ])
    f["czi_attachments"].attachments[9].name = "Caf\xe9"
    f["czi_metadata_attachment"] = Czi([plane(0, 0, 0, 0, 4, 4, C=0)],
                                       xml(px=1e-6, channels=[("BOM channel", None, None)], bom=True),
                                       metadata_attachment=b"\x00\x01binary part")
    f["czi_xml_latin1"] = Czi([plane(0, 0, 0, 0, 4, 4, C=0)], b"<ImageDocument>\xe9</ImageDocument>")
    f["czi_xml_odd"] = Czi([plane(1, 0, 0, 0, 4, 4, C=0), plane(1, 0, 0, 0, 4, 4, C=1)], xml(raw_text=(
        '<!-- <ImageDocument> --><ImageDocument><Metadata><Scaling><Items>'
        '<Distance Id="Y"><Value>-1</Value></Distance><Distance Id="X"><Value>2<!-- a comment -->e-6</Value></Distance>'
        '<Distance Id="X"><Value>9</Value></Distance></Items></Scaling><Information><Image><Dimensions><Channels>'
        '<Channel Name="a &amp; b"><Color>#ABCDEF</Color></Channel><Channel Name="second"/></Channels></Dimensions>'
        '</Image></Information></Metadata></Other></ImageDocument>')))
    f["czi_many_attachments"] = Czi([plane(0, 0, 0, 0, 4, 4)], attachments=[
        Att(f"Attachment number {k} with a long name", "BINARY", bytes([k % 256]) * 3) for k in range(700)])
    f["czi_empty_directory"] = Czi([], xml())
    f["czi_spare_bytes"] = Czi([_spare(plane(0, 0, 0, 0, 4, 4, C=0), b"\x00\x00\x07\x00\x00"),
                                _pyramid(plane(0, 0, 0, 0, 2, 2, wl=4, hl=4, C=0), 1)])
    return f


def _patched(sb: Sb, copy) -> Sb:
    sb.copy = copy
    return sb


def _spare(sb: Sb, spare: bytes) -> Sb:
    sb.spare = spare
    return sb


def _pyramid(sb: Sb, kind: int) -> Sb:
    sb.pyramid = kind
    return sb


def rejected() -> dict[str, tuple[Czi, object]]:
    """name -> (fixture, a patch of its bytes or None)."""
    def base(**kw) -> Czi:
        return Czi([plane(0, 0, 0, 0, 4, 4, C=0)], xml(), attachments=[Att("Thumbnail", "JPG", b"jpeg")], **kw)

    def entry(fn):
        c = base()
        c.subblocks[0].entry = fn
        return c

    def dims(fn):
        c = base()
        c.subblocks[0].dims = fn(c.subblocks[0].dims)
        return c

    def sub(**kw):
        c = base()
        for k, v in kw.items():
            setattr(c.subblocks[0], k, v)
        return c

    def att(**kw):
        c = base()
        for k, v in kw.items():
            setattr(c.attachments[0], k, v)
        return c

    r: dict[str, tuple[Czi, object]] = {
        "major_2": (base(major=2), None),
        "file_part": (base(file_part=1), None),
        "entry_file_part": (entry(lambda e: e[:14] + struct.pack("<i", 1) + e[18:]), None),
        "attachment_file_part": (att(file_part=1), None),
        "no_directory": (base(no_directory=True), None),
        "directory_id": (base(directory_id="ZISRAWDIRECTORX"), None),
        "entry_count_negative": (base(directory_count=-1), None),
        "entry_count_over_limit": (base(directory_count=(1 << 21) + 1), None),
        "directory_overrun": (base(directory_count=2), None),
        "directory_used": (base(directory_used=128 + 40), None),
        "de_schema": (entry(lambda e: b"DE" + e[2:]), None),
        "unknown_schema": (entry(lambda e: b"XX" + e[2:]), None),
        "dimension_letter": (dims(lambda d: d + [("Q", 0, 1, 1)]), None),
        "dimension_lowercase": (dims(lambda d: d + [("z", 0, 1, 1)]), None),
        "dimension_padding": (dims(lambda d: d + [(b"Z\0\0\x01", 0, 1, 1)]), None),
        "duplicate_dimension": (dims(lambda d: d + [("C", 1, 1, 1)]), None),
        "missing_x": (dims(lambda d: [x for x in d if x[0] != "X"] + [("Z", 0, 1, 1)]), None),
        "zero_size": (dims(lambda d: [("X", 0, 4, 0)] + d[1:]), None),
        "plane_size_not_1": (dims(lambda d: d[:2] + [("C", 0, 2, 2)]), None),
        "dimension_count_13": (entry(lambda e: e[:28] + struct.pack("<i", 13) + e[32:] + bytes(20 * 11)), None),
        "subblock_id": (sub(segment_id="ZISRAWSUBBLOCX"), None),
        "subblock_outside_file": (sub(sizes=(0, 0, 1 << 30)), None),
        "subblock_copy_schema": (sub(copy=lambda e: b"DE" + e[2:]), None),
        "subblock_copy_count": (sub(copy=lambda e: e[:28] + struct.pack("<i", 41) + e[32:]), None),
        "negative_sizes": (sub(sizes=(-1, 0, 16)), None),
        "pixel_type_unknown": (entry(lambda e: e[:2] + struct.pack("<i", 5) + e[6:]), None),
        "compression_lzw": (entry(lambda e: e[:18] + struct.pack("<i", 2) + e[22:]), None),
        "compression_chunked": (entry(lambda e: e[:18] + struct.pack("<i", 7) + e[22:]), None),
        "compression_raw_camera": (entry(lambda e: e[:18] + struct.pack("<i", 100) + e[22:]), None),
        "jpeg_gray16": (entry(lambda e: e[:2] + struct.pack("<ii", 1, 0) + e[10:18] + struct.pack("<i", 1) + e[22:]), None),
        "metadata_id": (base(metadata_id="ZISRAWMETADATX"), None),
        "metadata_outside_file": (base(metadata_sizes=(1 << 30, 0)), None),
        "metadata_negative": (base(metadata_sizes=(-5, 0)), None),
        "attdir_id": (base(attdir_id="ZISRAWATTDIX"), None),
        "attdir_count_over_limit": (base(attdir_count=(1 << 16) + 1), None),
        "attachment_id": (att(segment_id="ZISRAWATTACX"), None),
        "attachment_outside_file": (base(), ("attachment0", lambda b, at: b[: at + 32] + struct.pack("<q", 1 << 30)
                                             + b[at + 40 :])),
        "extent": (Czi([plane(0, 0, -(1 << 31), 0, 1, 1), plane(0, 0, (1 << 31) - 1, 0, 1, 1)]), None),
        "segment_negative": (base(), ("directory", lambda b, at: b[: at + 16] + struct.pack("<q", -1) + b[at + 24 :])),
    }
    return r


def too_many_series() -> Czi:
    return Czi([plane(0, 0, 0, 0, 1, 1, S=s) for s in range((1 << 16) + 1)])


def big() -> Czi:
    """Row bands (spec/virtualize/czi.md §4.3): a level of 4100 x 2048 Gray16 tiles
    (over 16 MiB each), its last row 1024 high, and a tile of 4100 x 2050."""
    rows = [plane(1, 0, 0, 0, 4100, 2048, S=0), plane(1, 0, 0, 2048, 4100, 1024, S=0)]
    tile = plane(1, 0, 7, 3, 4100, 2050, wl=20500, hl=10250, S=1)  # minified 5x: no layer, a tile
    return Czi(rows + [tile])


# ---- cross-checks

def czifile_check(path: Path) -> list[str]:
    """czifile parses the file and decodes every subblock it can; returns what failed."""
    import czifile

    failed = []
    with czifile.CziFile(path) as c:
        for i, sb in enumerate(c.subblocks()):
            try:
                sb.data()
            except Exception as e:  # noqa: BLE001
                failed.append(f"subblock {i}: {type(e).__name__}: {e}"[:120])
        try:
            _ = c.metadata() if c.metadata_segment is not None else None
        except Exception as e:  # noqa: BLE001
            failed.append(f"metadata: {type(e).__name__}: {e}"[:120])
        try:
            for a in c.attachments():
                a.data()
        except Exception as e:  # noqa: BLE001
            failed.append(f"attachments: {type(e).__name__}: {e}"[:120])
    return failed


def libczi_check(path: Path) -> str:
    try:
        from pylibCZIrw import czi as pyczi
    except ImportError:
        return "pylibCZIrw not installed"
    try:
        with pyczi.open_czi(str(path)) as doc:
            box = doc.total_bounding_box
            if box.get("X", (0, 0))[1] - box.get("X", (0, 0))[0] > 0:
                try:
                    doc.read(roi=(box["X"][0], box["Y"][0], min(64, box["X"][1] - box["X"][0]),
                                  min(64, box["Y"][1] - box["Y"][0])))
                except Exception as e:  # noqa: BLE001
                    return f"opened; its composite read fails: {e}"[:160]
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"[:160]


def main(argv: list[str]) -> int:
    if "--big" in argv:
        out = Path(argv[argv.index("--big") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "czi_big_bands.czi").write_bytes(big().build())
        (out / "czi_reject_too_many_series.czi").write_bytes(too_many_series().build())
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.czi"):
        old.unlink()
    for name, czi in accepted().items():
        path = OUT / f"{name}.czi"
        path.write_bytes(czi.build())
        failed = czifile_check(path)
        print(f"{name}: czifile {'ok' if not failed else failed[:2]}, libCZI {libczi_check(path)}")
    for name, (czi, patch) in rejected().items():
        data = czi.build()
        if patch is not None:
            key, fn = patch
            data = fn(data, czi.offsets[key])
        (OUT / f"czi_reject_{name}.czi").write_bytes(data)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
