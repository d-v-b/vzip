"""Writes synthetic ND2 files (format version 3) to web/test/fixtures/nd2/, with
the pixels each one should decode to, covering the rules of the ND2 profile
(profiles/nd2.md §5) that the public corpus does not: compressed frames, padded
rows, multi-period time loops, disabled positions, sibling and spectral
experiment nodes, missing frames, float data, and the inputs it rejects.

Each `<name>.nd2` has `<name>.npz`: one array per expected chunk key.

Usage: uv run python web/test/nd2/write_fixtures.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

OUT = Path(__file__).parents[1] / "fixtures" / "nd2"
MAGIC = 0x0ABECEDA
NAME_PAD = 4072  # NIS-Elements pads chunk names so that headers are 4 KiB


# ---- lite variant (conventions/nd2/README.md §2.2)

def _name(name: str) -> bytes:
    return bytes([len(name) + 1]) + (name + "\0").encode("utf-16le")


def lv(name: str, value) -> bytes:
    if isinstance(value, bool):
        return bytes([1]) + _name(name) + bytes([int(value)])
    if isinstance(value, int):
        return bytes([3]) + _name(name) + struct.pack("<I", value) if value >= 0 else bytes([2]) + _name(name) + struct.pack("<i", value)
    if isinstance(value, float):
        return bytes([6]) + _name(name) + struct.pack("<d", value)
    if isinstance(value, str):
        return bytes([8]) + _name(name) + (value + "\0").encode("utf-16le")
    if isinstance(value, (bytes, bytearray)):
        return bytes([9]) + _name(name) + struct.pack("<Q", len(value)) + bytes(value)
    if isinstance(value, list):  # a level of unnamed records
        return level(name, [lv("", v) for v in value])
    if isinstance(value, dict):
        return level(name, [lv(k, v) for k, v in value.items()])
    raise TypeError(type(value))


def level(name: str, members: list[bytes]) -> bytes:
    head = bytes([11]) + _name(name)
    body = b"".join(members)
    length = len(head) + 12 + len(body)
    return head + struct.pack("<IQ", len(members), length) + body + b"\0" * (8 * len(members))


def compressed(structure: bytes) -> bytes:
    return bytes([76, 0]) + b"\0" * 10 + zlib.compress(structure)


# ---- file layout (§5.1)

class Nd2:
    def __init__(self, version: bytes = b"Ver3.0") -> None:
        sig = b"ND2 FILE SIGNATURE CHUNK NAME01!"
        self.buf = bytearray(struct.pack("<IIQ", MAGIC, 32, 64) + sig + version.ljust(64, b"\0"))
        self.map: list[tuple[bytes, int, int]] = []

    def chunk(self, name: str, data: bytes, name_pad: int = NAME_PAD) -> None:
        offset = len(self.buf)
        raw = name.encode().ljust(name_pad, b"\0")
        self.buf += struct.pack("<IIQ", MAGIC, len(raw), len(data)) + raw + data
        self.map.append((name.encode(), offset, len(data)))

    def raw_chunk(self, name: bytes, data: bytes, name_pad: int = NAME_PAD) -> None:
        """A chunk whose name is any bytes (up to its `!`)."""
        offset = len(self.buf)
        self.buf += struct.pack("<IIQ", MAGIC, name_pad, len(data)) + name.ljust(name_pad, b"\0") + data
        self.map.append((name, offset, len(data)))

    def finish(self, path: Path) -> None:
        sig = b"ND2 CHUNK MAP SIGNATURE 0000001!"
        body = b"".join(n + struct.pack("<QQ", o, s) for n, o, s in self.map) + sig + b"\0" * 16
        offset = len(self.buf)
        name = b"ND2 FILEMAP SIGNATURE NAME 0001!"
        self.buf += struct.pack("<IIQ", MAGIC, len(name), len(body)) + name + body
        self.buf += sig + struct.pack("<Q", offset)
        path.write_bytes(bytes(self.buf))


def attributes(width, height, comp, bpc, *, sequence, width_bytes=None, compression=2, significant=None,
               tile_width=None, tile_height=None) -> bytes:
    a = {
        "uiWidth": width, "uiWidthBytes": width_bytes or width * comp * bpc // 8, "uiHeight": height,
        "uiComp": comp, "uiBpcInMemory": bpc, "uiBpcSignificant": significant or bpc,
        "uiSequenceCount": sequence, "uiTileWidth": tile_width or width, "uiTileHeight": tile_height or height,
        "eCompression": compression, "dCompressionParam": -1.0, "ePixelType": 1, "uiVirtualComponents": comp,
    }
    return lv("SLxImageAttributes", a)


def picture(planes: list[tuple[str, int, int]], calibration: float | None) -> bytes:
    p = {"dCalibration": calibration or 1.0, "dAspect": 1.0, "bCalibrated": calibration is not None,
         "sPicturePlanes": {"uiCount": len(planes), "uiCompCount": sum(k for _, _, k in planes),
                            "sPlaneNew": {f"a{i}": {"sDescription": n, "uiColor": c, "uiCompCount": k}
                                          for i, (n, c, k) in enumerate(planes)}}}
    return lv("SLxPictureMetadata", p)


def node(e_type: int, pars: dict, children: list[dict] | None = None, item_valid: bytes | None = None) -> dict:
    n = {"eType": e_type, "uLoopPars": pars}
    if item_valid is not None:
        n["pItemValid"] = item_valid
    if children:
        n["ppNextLevelEx"] = children
    return n


def experiment(root: dict) -> bytes:
    return lv("SLxExperiment", root)


def frame_bytes(pixels: np.ndarray, width_bytes: int, compress: bool) -> bytes:
    """8-byte timestamp + rows of `width_bytes` (padding is 0xAB)."""
    rows = pixels.reshape(pixels.shape[0], -1).view(np.uint8)
    pad = width_bytes - rows.shape[1]
    data = b"".join(r.tobytes() + b"\xab" * pad for r in rows)
    return struct.pack("<d", 1.5) + (zlib.compress(data) if compress else data)


def write(name: str, f: Nd2, expected: dict[str, np.ndarray]) -> None:
    f.finish(OUT / f"{name}.nd2")
    if expected:
        np.savez(OUT / f"{name}.npz", **{k.replace("/", "|"): v for k, v in expected.items()})


rng = np.random.default_rng(0)


def tz_uint16() -> None:
    """Time 3 x z 4, 2 channels, uint16, calibrated."""
    t, z, c, h, w = 3, 4, 2, 6, 5
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, c, 16, sequence=t * z, significant=12))
    f.chunk("ImageMetadataLV!", experiment(node(1, {"uiCount": t, "dPeriod": 2500.0, "dStart": 0.0, "dDuration": 0.0},
                                                [node(4, {"uiCount": z, "dZStep": -0.5, "dZLow": 0.0, "dZHigh": 1.5})])))
    f.chunk("ImageMetadataSeqLV|0!", picture([("DAPI", 0xFF0000, 1), ("GFP", 0x00FF00, 1)], 0.25))
    expected = {}
    for i in range(t * z):
        px = rng.integers(0, 4096, (h, w, c), dtype=np.uint16)
        f.chunk(f"ImageDataSeq|{i}!", frame_bytes(px, w * c * 2, False))
        # dZStep is negative, so the z index is flipped (conventions/nd2/README.md §4.3).
        expected[f"0/0/c/{i // z}/0/{z - 1 - i % z}/0/0"] = np.moveaxis(px, -1, 0)
    write("nd2_tz_uint16", f, expected)


def padded_rgb() -> None:
    """RGB uint8, rows padded to 4 bytes, z 2 with a step from dZHigh/dZLow."""
    z, h, w = 2, 4, 13  # rows of 39 bytes, padded to 40
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 3, 8, sequence=z, width_bytes=40))
    f.chunk("ImageMetadataLV!", experiment(node(4, {"uiCount": z, "dZStep": 0.0, "dZLow": 1.0, "dZHigh": 3.0})))
    f.chunk("ImageMetadataSeqLV|0!", picture([("Brightfield", 0xFFFFFF, 3)], 0.5))
    expected = {}
    for i in range(z):
        px = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
        f.chunk(f"ImageDataSeq|{i}!", frame_bytes(px, 40, False))
        expected[f"0/0/c/0/{i}/0/0"] = np.moveaxis(px, -1, 0)
    write("nd2_padded_rgb", f, expected)


def compressed_positions() -> None:
    """Compressed frames; a two-period time loop (3 points); positions with one
    disabled point (3 of 4); a smaller sibling position loop (dropped); a
    spectral child; a missing frame; compressed metadata."""
    h, w = 5, 7
    positions = node(2, {"Points": [{"dPosX": float(i)} for i in range(4)]},
                     [node(6, {"uiCount": 1})], item_valid=bytes([1, 0, 1, 1]))
    sibling = node(2, {"Points": [{"dPosX": 0.0}, {"dPosX": 1.0}]})
    root = node(8, {"pPeriod": [{"uiCount": 2, "dPeriod": 1000.0, "dStart": 0.0, "dDuration": 2000.0},
                                {"uiCount": 1, "dPeriod": 4000.0, "dStart": 2000.0, "dDuration": 4000.0}],
                    "pPeriodValid": bytes([1, 1])}, [positions, sibling])
    f = Nd2()
    f.chunk("ImageAttributesLV!", compressed(attributes(w, h, 1, 16, sequence=9, compression=0, significant=16)))
    f.chunk("ImageMetadataLV!", compressed(experiment(root)))
    f.chunk("ImageMetadataSeqLV|0!", picture([("mCherry", 0x0000FF, 1)], 0.1))
    expected = {}
    for i in range(9):
        if i == 4:
            continue  # missing frame (t 1, position 1)
        px = rng.integers(0, 65536, (h, w, 1), dtype=np.uint16)
        f.chunk(f"ImageDataSeq|{i}!", frame_bytes(px, w * 2, True))
        expected[f"{i % 3}/0/c/{i // 3}/0/0"] = px[:, :, 0]
    write("nd2_compressed_positions", f, expected)


def float_uncalibrated() -> None:
    """float32, one frame, no loops, no picture metadata."""
    h, w = 3, 4
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 32, sequence=1))
    px = rng.random((h, w, 1), dtype=np.float32)
    f.chunk("ImageDataSeq|0!", frame_bytes(px, w * 4, False))
    write("nd2_float_uncalibrated", f, {"0/0/c/0/0": px[:, :, 0]})


def rejected() -> None:
    base = dict(width=4, height=3, comp=1, bpc=16, sequence=1)
    cases = {
        "nd2_reject_lossy": dict(attrs=attributes(**base, compression=1)),
        "nd2_reject_tiled": dict(attrs=attributes(**base, tile_width=2)),
        "nd2_reject_loop_type": dict(attrs=attributes(**base), exp=experiment(node(7, {"uiCount": 2}))),
        "nd2_reject_version2": dict(attrs=attributes(**base), version=b"Ver2.0"),
        "nd2_reject_header_lengths": dict(attrs=attributes(4, 3, 1, 16, sequence=2),
                                          exp=experiment(node(1, {"uiCount": 2, "dPeriod": 1.0})), pads=[NAME_PAD, 64]),
    }
    for name, c in cases.items():
        f = Nd2(c.get("version", b"Ver3.0"))
        f.chunk("ImageAttributesLV!", c["attrs"])
        if "exp" in c:
            f.chunk("ImageMetadataLV!", c["exp"])
        for i, pad in enumerate(c.get("pads", [NAME_PAD])):
            f.chunk(f"ImageDataSeq|{i}!", frame_bytes(np.zeros((3, 4, 1), np.uint16), 8, False), name_pad=pad)
        write(name, f, {})
    # A legacy (JPEG 2000) ND2 starts with the JP2 signature box.
    (OUT / "nd2_reject_legacy.nd2").write_bytes(b"\x00\x00\x00\x0cjP  \r\n\x87\n" + b"\0" * 64)


def record(typ: int, name: str, payload: bytes) -> bytes:
    """An LV record of any type, with a raw payload."""
    return bytes([typ]) + _name(name) + payload


def edges() -> None:
    """The edges of §5 that the files above do not reach."""
    h, w = 3, 4

    def frames(f: Nd2, n: int, comp: int = 1, dtype=np.uint16, width_bytes=None, skip=()) -> dict:
        out = {}
        for i in range(n):
            px = rng.integers(0, 200, (h, w, comp)).astype(dtype)
            if i not in skip:
                f.chunk(f"ImageDataSeq|{i}!", frame_bytes(px, width_bytes or w * comp * px.itemsize, False))
            out[i] = px
        return out

    # Spectral nodes: a counted one is not a loop and its children stay at
    # its depth; one with count 0 is skipped with its children. Frames past
    # N are ignored. Time 2 x z 2.
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=4))
    spectral = node(6, {"pPlanes": {"uiCount": 2}}, [node(4, {"uiCount": 2, "dZStep": 0.5})])
    empty = node(6, {"uiCount": 0}, [node(2, {"Points": [{"dPosX": 0.0}] * 3})])
    f.chunk("ImageMetadataLV!", experiment(node(1, {"uiCount": 2, "dPeriod": 0.0}, [spectral, empty])))
    px = frames(f, 5)
    write("nd2_edge_spectral", f, {f"0/0/c/{i // 2}/{i % 2}/0/0": px[i][:, :, 0] for i in range(4)})

    # Validity: no pPeriodValid (all valid), and a pItemValid shorter than
    # Points (the missing entries are invalid): time 3 x position 1.
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=3))
    positions = node(2, {"Points": [{"dPosX": 0.0}] * 3}, item_valid=bytes([1]))
    f.chunk("ImageMetadataLV!", experiment(node(8, {"pPeriod": [{"uiCount": 1, "dPeriod": 0.0}, {"uiCount": 2}]},
                                                [positions])))
    px = frames(f, 3)
    write("nd2_edge_validity", f, {f"0/0/c/{i}/0/0": px[i][:, :, 0] for i in range(3)})

    # Picture defaults: no uiColor, no uiCompCount, bCalibrated as a u32,
    # dAspect 0, uiBpcSignificant 0, an unpaired surrogate in a name, and a
    # string member where a number is not needed. Last of duplicate map names
    # wins (frame 0 is written twice).
    f = Nd2()
    f.chunk("ImageAttributesLV!", lv("SLxImageAttributes", {"uiWidth": w, "uiWidthBytes": w * 4, "uiHeight": h, "uiComp": 2,
                                      "uiBpcInMemory": 16, "uiBpcSignificant": 0, "ePixelType": "x"}))
    planes = level("sPlaneNew", [
        level("a0", [record(8, "sDescription", b"A\x00\x42\xd8B\x00\0\0")]),
        level("a1", [lv("sDescription", "B"), lv("uiColor", 0x0000FF)])])
    f.chunk("ImageMetadataSeqLV|0!", level("SLxPictureMetadata", [
        lv("dCalibration", 0.5), lv("dAspect", 0.0), record(3, "bCalibrated", struct.pack("<I", 7)),
        level("sPicturePlanes", [lv("uiCount", 2), planes])]))
    frames(f, 1, comp=2)
    px = frames(f, 1, comp=2)
    write("nd2_edge_picture", f, {"0/0/c/0/0/0": np.moveaxis(px[0], -1, 0)})

    # A plane with 2 components: generic channel labels; 8-bit, no endian.
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 2, 8, sequence=1))
    f.chunk("ImageMetadataSeqLV|0!", picture([("AB", 0x00FF00, 2)], None))
    px = frames(f, 1, comp=2, dtype=np.uint8)
    write("nd2_edge_generic_channels", f, {"0/0/c/0/0/0": np.moveaxis(px[0], -1, 0)})

    # Objects: a repeated name keeps its first position and last value;
    # integer-like names keep their order.
    f = Nd2()
    f.chunk("ImageAttributesLV!", level("SLxImageAttributes", [
        lv("uiWidth", 99), lv("2", 0), lv("uiHeight", h), lv("1", 0), lv("uiWidthBytes", w * 2), lv("uiComp", 1),
        lv("uiBpcInMemory", 16), lv("uiBpcSignificant", 16), lv("uiWidth", w)]))
    px = frames(f, 1)
    write("nd2_edge_duplicate_member", f, {"0/0/c/0/0": px[0][:, :, 0]})

    # Rejected.
    base = attributes(w, h, 1, 16, sequence=1)
    good_level = lv("SLxImageAttributes", {"uiWidth": w})
    cases = {
        "nd2_reject_etype_without_pars": dict(exp=experiment({"eType": 7})),
        "nd2_reject_missing_etype": dict(exp=experiment({"uLoopPars": {"uiCount": 2}})),
        "nd2_reject_trailing_byte": dict(attrs=base + b"\0"),
        "nd2_reject_level_short": dict(attrs=good_level[:1 + 1 + 2 * 19 + 4] + struct.pack("<Q", 5) + good_level[1 + 1 + 2 * 19 + 12:]),
        "nd2_reject_nested_compression": dict(attrs=compressed(compressed(base))),
        "nd2_reject_zlib_trailing": dict(attrs=compressed(base) + b"\0"),
        "nd2_reject_lv_type": dict(attrs=base + record(10, "x", b"")),
        "nd2_reject_number_type": dict(attrs=lv("SLxImageAttributes", {"uiWidth": "4", "uiHeight": h, "uiWidthBytes": 8,
                                                                       "uiComp": 1, "uiBpcInMemory": 16, "uiBpcSignificant": 16})),
        "nd2_reject_missing_width_bytes": dict(attrs=lv("SLxImageAttributes", {"uiWidth": w, "uiHeight": h, "uiComp": 1,
                                                                               "uiBpcInMemory": 16, "uiBpcSignificant": 16})),
        "nd2_reject_width_bytes": dict(attrs=attributes(w, h, 1, 16, sequence=1, width_bytes=6)),
        "nd2_reject_compression": dict(attrs=attributes(w, h, 1, 16, sequence=1, compression=3)),
        "nd2_reject_width_zero": dict(attrs=attributes(0, h, 1, 16, sequence=1, width_bytes=8)),
        "nd2_reject_two_time_loops": dict(exp=experiment(node(1, {"uiCount": 2}, [node(8, {"pPeriod": [{"uiCount": 2}]})]))),
    }
    for name, c in cases.items():
        f = Nd2()
        f.chunk("ImageAttributesLV!", c.get("attrs", base))
        if "exp" in c:
            f.chunk("ImageMetadataLV!", c["exp"])
        frames(f, 4)
        write(name, f, {})
    # Frames: the last is too short; a compressed frame with no data.
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=3))
    f.chunk("ImageMetadataLV!", experiment(node(1, {"uiCount": 3})))
    frames(f, 2)
    f.chunk("ImageDataSeq|2!", struct.pack("<d", 0) + b"\0" * (h * w * 2 - 1))
    write("nd2_reject_short_frame", f, {})
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=1, compression=0))
    f.chunk("ImageDataSeq|0!", struct.pack("<d", 0))
    write("nd2_reject_empty_compressed_frame", f, {})
    # Padded rows too many for one reference (65519 bytes): the frame is
    # split into row blocks of 6000 rows, the largest divisor of 12000 whose
    # blocks fit (each row costs 8 bytes of payload here, so at most 8189).
    tall, block = 12000, 6000
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(1, tall, 1, 8, sequence=1, width_bytes=4))
    px = rng.integers(0, 256, (tall, 1, 1), dtype=np.uint8)
    f.chunk("ImageDataSeq|0!", frame_bytes(px, 4, False))
    write("nd2_edge_tall_padded", f, {f"0/0/c/{j}/0": px[j * block : (j + 1) * block, :, 0] for j in range(tall // block)})
    # A chunk map entry pointing at bytes without the magic.
    f = Nd2()
    f.chunk("ImageAttributesLV!", base)
    frames(f, 1)
    f.map[-1] = (f.map[-1][0], f.map[-1][1] + 1, f.map[-1][2])
    write("nd2_reject_magic", f, {})
    # Too short for a chunk map.
    (OUT / "nd2_reject_tiny.nd2").write_bytes(Nd2().buf[:32] + b"\0" * 4)


def revision3() -> None:
    """The typed-member and loop-kind rules of revision 3 (conventions/nd2/README.md §2.2, §3)."""
    h, w = 3, 4

    def frames(f: Nd2, n: int) -> dict:
        out = {}
        for i in range(n):
            px = rng.integers(0, 200, (h, w, 1)).astype(np.uint16)
            f.chunk(f"ImageDataSeq|{i}!", frame_bytes(px, w * 2, False))
            out[i] = px[:, :, 0]
        return out

    # Sibling time loops of different eTypes merge by kind: a position loop
    # (2 points) with time children of 2 (eType 1) and 3 (eType 8) gives
    # [position 2, time 3], with the second loop's period. eType is a
    # binary64; uiColor a negative i32; an unused pointer member is not read.
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=6))
    first = {"eType": 1, "uLoopPars": {"uiCount": 2, "dPeriod": 100.0}}
    second = {"eType": 8, "uLoopPars": {"pPeriod": [{"uiCount": 3, "dPeriod": 500.0}]}}
    root = level("SLxExperiment", [record(6, "eType", struct.pack("<d", 2.0)),
                                    lv("uLoopPars", {"Points": [{"dPosX": 0.0}, {"dPosX": 1.0}]}),
                                    lv("ppNextLevelEx", {"i0": first, "i1": second})])
    f.chunk("ImageMetadataLV!", root)
    f.chunk("ImageMetadataSeqLV|0!", level("SLxPictureMetadata", [
        record(7, "pPointer", struct.pack("<Q", 12345)),
        level("sPicturePlanes", [lv("uiCount", 1), level("sPlaneNew", [
            level("a0", [lv("sDescription", "red"), record(2, "uiColor", struct.pack("<i", -16776961))])])])]))
    px = frames(f, 6)
    write("nd2_edge_kind_merge", f, {f"{i // 3}/0/c/{i % 3}/0/0": px[i] for i in range(6)})

    def attrs(**changes) -> bytes:
        a = {"uiWidth": lv("uiWidth", w), "uiHeight": lv("uiHeight", h), "uiWidthBytes": lv("uiWidthBytes", w * 2),
             "uiComp": lv("uiComp", 1), "uiBpcInMemory": lv("uiBpcInMemory", 16),
             "uiBpcSignificant": lv("uiBpcSignificant", 16)}
        a.update(changes)
        return level("SLxImageAttributes", list(a.values()))

    time_loop = lambda pars: experiment(node(1, pars))  # noqa: E731
    cases = {
        "nd2_reject_pointer_number": dict(attrs=attrs(uiWidth=record(7, "uiWidth", struct.pack("<Q", w)))),
        "nd2_reject_fraction": dict(attrs=attrs(uiHeight=lv("uiHeight", 3.5))),
        "nd2_reject_negative_count": dict(exp=time_loop({"uiCount": -1})),
        "nd2_reject_nan_period": dict(exp=time_loop({"uiCount": 1, "dPeriod": float("nan")})),
        "nd2_reject_path_through_scalar": dict(exp=experiment({"eType": 1, "uLoopPars": 5})),
        "nd2_reject_validity_object": dict(exp=experiment(node(2, {"Points": [{"x": 1.0}]}, item_valid=None) | {
            "pItemValid": {"a": 1}})),
        "nd2_reject_unused_calibration": dict(pic=level("SLxPictureMetadata", [
            lv("bCalibrated", False), lv("dCalibration", "0.5")])),
        "nd2_reject_unused_plane_color": dict(pic=level("SLxPictureMetadata", [level("sPicturePlanes", [
            lv("uiCount", 2), level("sPlaneNew", [level("a0", [lv("uiColor", "red")])])])])),
    }
    for name, c in cases.items():
        f = Nd2()
        f.chunk("ImageAttributesLV!", c.get("attrs", attrs()))
        if "exp" in c:
            f.chunk("ImageMetadataLV!", c["exp"])
        if "pic" in c:
            f.chunk("ImageMetadataSeqLV|0!", c["pic"])
        frames(f, 1)
        write(name, f, {})


def revision4() -> None:
    """Whole-tree checks, validity lists everywhere, and nesting (conventions/nd2/README.md §2.2, §3)."""
    h, w = 3, 4

    def write_case(name: str, exp: bytes | None = None, attrs: bytes | None = None, accept: bool = False) -> None:
        f = Nd2()
        f.chunk("ImageAttributesLV!", attrs or attributes(w, h, 1, 16, sequence=1))
        if exp is not None:
            f.chunk("ImageMetadataLV!", exp)
        px = rng.integers(0, 200, (h, w, 1)).astype(np.uint16)
        f.chunk("ImageDataSeq|0!", frame_bytes(px, w * 2, False))
        write(name, f, {"0/0/c/0/0": px[:, :, 0]} if accept else {})

    def nested(depth: int) -> bytes:
        """SLxImageAttributes with an unused member nested `depth` levels below it."""
        inner = lv("x", 1)
        for _ in range(depth):
            inner = level("d", [inner])
        return level("SLxImageAttributes", [lv("uiWidth", w), lv("uiHeight", h), lv("uiWidthBytes", w * 2),
                                            lv("uiComp", 1), lv("uiBpcInMemory", 16), lv("uiBpcSignificant", 16), inner])

    # pPlanes is not read when the spectral node has uiCount.
    write_case("nd2_edge_spectral_unread_pplanes",
               experiment(node(6, {"uiCount": 2, "pPlanes": {"uiCount": "x"}})), accept=True)
    # Records at depth 100 and 101 (SLxImageAttributes is at 0, its members at 1).
    write_case("nd2_edge_nesting_100", attrs=nested(99), accept=True)
    write_case("nd2_reject_nesting_101", attrs=nested(100))
    # A node under a count-0 node is not visited but is still checked.
    write_case("nd2_reject_skipped_bad_etype", experiment(node(1, {"uiCount": 0}, [node(7, {"uiCount": 2})])))
    # At most 1024 components.
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(1, h, 1025, 8, sequence=1))
    f.chunk("ImageDataSeq|0!", frame_bytes(np.zeros((h, 1, 1025), np.uint8), 1025, False))
    write("nd2_reject_too_many_components", f, {})
    # A z step that overflows rejects, even on a node the flattening skips.
    write_case("nd2_reject_z_step_overflow", experiment(node(1, {"uiCount": 0}, [
        node(4, {"uiCount": 2, "dZStep": 0.0, "dZLow": -1e308, "dZHigh": 1e308})])))
    # A single image's stage position, from the picture metadata (conventions/nd2/README.md §4.3).
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=1))
    f.chunk("ImageMetadataSeqLV|0!", level("SLxPictureMetadata", [
        lv("dCalibration", 0.5), lv("bCalibrated", True), lv("dXPos", 100.0), lv("dYPos", -20.0),
        lv("dStgLgCT11", -1.0), lv("dStgLgCT22", -1.0)]))
    px = rng.integers(0, 200, (h, w, 1)).astype(np.uint16)
    f.chunk("ImageDataSeq|0!", frame_bytes(px, w * 2, False))
    write("nd2_edge_single_position", f, {"0/0/c/0/0": px[:, :, 0]})
    # pItemValid holds flags on every node, not only position loops.
    write_case("nd2_reject_itemvalid_on_time",
               experiment({"eType": 1, "uLoopPars": {"uiCount": 1}, "pItemValid": ["x"]}))


def revision6() -> None:
    """Stage positions become translations (conventions/nd2/README.md §4.3)."""
    h, w = 3, 4

    def case(name: str, points: list[dict], camera: tuple | None, valid: bytes | None = None) -> None:
        f = Nd2()
        f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=len(points)))
        f.chunk("ImageMetadataLV!", experiment(node(2, {"Points": points}, item_valid=valid)))
        pic = [lv("dCalibration", 0.5), lv("bCalibrated", True)]
        if camera is not None:
            pic += [lv(f"dStgLgCT{k}", float(v)) for k, v in zip(("11", "12", "21", "22"), camera)]
        f.chunk("ImageMetadataSeqLV|0!", level("SLxPictureMetadata", pic))
        expected = {}
        positions = len(points) if valid is None else sum(valid)
        for i in range(len(points)):
            px = rng.integers(0, 200, (h, w, 1)).astype(np.uint16)
            f.chunk(f"ImageDataSeq|{i}!", frame_bytes(px, w * 2, False))
            if i < positions:  # frame i is position i; later frames are ignored
                expected[f"{i}/0/c/0/0"] = px[:, :, 0]
        write(name, f, expected)

    # A camera rotated by 90 degrees: C = [[0, -1], [1, 0]], C^-1 = [[0, 1], [-1, 0]].
    # The second point is invalid, so positions 0 and 1 are points 0 and 2.
    case("nd2_edge_stage_positions",
         [{"dPosX": 100.0, "dPosY": 20.0}, {"dPosX": 1e300}, {"dPosX": 130.5, "dPosY": -7.25}],
         (0, -1, 1, 0), bytes([1, 0, 1]))
    # A singular camera matrix, or a point without dPosY: no translations.
    case("nd2_edge_stage_singular", [{"dPosX": 1.0, "dPosY": 2.0}, {"dPosX": 3.0, "dPosY": 4.0}], (1, 2, 2, 4))
    case("nd2_edge_stage_incomplete", [{"dPosX": 1.0, "dPosY": 2.0}, {"dPosX": 3.0}], None)


def variant(members: str) -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8"?><variant version="1.0">' + members + "</variant>").encode()


def streams() -> None:
    """Numeric streams, XML variants, events and a negative z step (conventions/nd2/README.md §5)."""
    t, z, h, w = 2, 3, 4, 5
    n = t * z
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 8, sequence=n))
    f.chunk("ImageMetadataLV!", experiment(node(1, {"uiCount": t, "dPeriod": 1000.0, "dStart": 0.0, "dDuration": 0.0},
                                                [node(4, {"uiCount": z, "dZStep": -1.0, "dZLow": 0.0, "dZHigh": 2.0})])))
    f.chunk("ImageMetadataSeqLV|0!", picture([("BF", 0xFFFFFF, 1)], 0.5))
    f.chunk("ImageMetadataSeqLV|1!", picture([("BF", 0xFFFFFF, 1)], 0.5))  # per frame: on vzip_source
    f.chunk("ImageEventsLV!", lv("RLxExperimentRecord", {"uiCount": 1}))  # events: on vzip_source
    f.chunk("CustomDataVar|CustomDataV2_0!", variant(
        '<CustomTagDescription_v1.0 runtype="CLxListVariant"><Tag0 runtype="CLxListVariant">'
        '<ID runtype="CLxStringW" value="PFS_OFFSET"/><Type runtype="lx_int32" value="2"/>'
        f'<Group runtype="lx_int32" value="0"/><Size runtype="lx_int32" value="{n}"/>'
        '<Desc runtype="CLxStringW" value="PFS offset"/><Unit runtype="CLxStringW" value=""/></Tag0>'
        "</CustomTagDescription_v1.0>"))
    f.chunk("CustomDataVar|AppInfo_V1_0!", variant(
        '<no_name runtype="CLxListVariant"><Version runtype="CLxStringW" value="5.42 &amp; more"/>'
        '<Gain runtype="double" value="1.5"/><Live runtype="bool" value="true"/>'
        '<Count runtype="lx_uint32" value="7"/><Odd runtype="lx_int32" value="x7"/></no_name>'))
    f.chunk("CustomData|AcqTimesCache!", struct.pack(f"<{n}d", *[100.0 * i for i in range(n)]))
    f.chunk("CustomData|X!", struct.pack(f"<{n}d", *[10.0 + i for i in range(n)]))
    f.chunk("CustomData|PFS_OFFSET!", struct.pack(f"<{n}i", *[-i for i in range(n)]))
    f.chunk("CustomData|Y!", struct.pack("<d", 1.0))  # too short for every frame: not an array
    f.chunk("CustomData|Blob!", bytes(range(37)))  # opaque: not recorded
    f.chunk("CustomDataSeq|STREAM_0|0!", bytes(16))  # opaque, per frame: not recorded
    expected = {}
    for i in range(n):
        px = rng.integers(0, 256, (h, w, 1), dtype=np.uint8)
        f.chunk(f"ImageDataSeq|{i}!", frame_bytes(px, w, False))
        expected[f"0/0/c/{i // z}/{z - 1 - i % z}/0/0"] = px[..., 0]
    write("nd2_streams", f, expected)


def revision7() -> None:
    """Source metadata that keeps every chunk (conventions/nd2/README.md §5)."""
    h, w = 3, 4

    def placed(f: Nd2, n: int, trailing: dict[int, bytes]) -> dict:
        expected = {}
        for i in range(n):
            px = rng.integers(0, 200, (h, w, 1)).astype(np.uint16)
            f.chunk(f"ImageDataSeq|{i}!", frame_bytes(px, w * 2, False) + trailing.get(i, b""))
            expected[f"0/0/c/{i}/0/0"] = px[:, :, 0]
        return expected

    # Values: repeated names (LV and XML), a list whose members read as pairs,
    # unpaired surrogates in a string and a name, the declarations read before
    # the stream they declare (whose bytes would decode as LV), a Type that is
    # an element, character references and integers too long to parse, and a
    # chunk whose JSON is over 16 KiB (on vzip_source). Frames 1 and 2 have
    # trailing bytes of different lengths.
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=3))
    f.chunk("ImageMetadataLV!", experiment(node(1, {"uiCount": 3, "dPeriod": 10.0})))
    f.chunk("ImageTextInfoLV!", level("SLxImageTextInfo", [
        lv("a", 1), lv("a", 2), lv("b", "x"),
        record(8, "s", struct.pack("<3H", 0x41, 0xDC00, 0)),
        bytes([3, 2]) + struct.pack("<HH", 0xD800, 0) + struct.pack("<I", 5),
        level("pairs", [level("", [lv("", "k"), lv("", 1)]), level("", [lv("", "l"), lv("", 2)])])]))
    f.chunk("ImageCalibrationLV|0!", level("SLxCalibration", [lv(f"v{i:05d}", i) for i in range(1500)]))
    f.chunk("CustomData|S!", bytes([1, 0, 1] * 4))
    f.chunk("CustomDataVar|CustomDataV2_0!", variant(
        '<CustomTagDescription_v1.0 runtype="CLxListVariant">'
        '<Tag0 runtype="CLxListVariant"><ID runtype="CLxStringW" value="S"/><Type runtype="lx_int32" value="2"/></Tag0>'
        '<Tag1 runtype="CLxListVariant"><ID runtype="CLxStringW" value="T"/>'
        '<Type runtype="CLxListVariant"><x runtype="lx_int32" value="3"/></Type></Tag1>'
        "</CustomTagDescription_v1.0>"))
    f.chunk("CustomDataVar|NDControlV1_0!", variant(
        '<NDControl runtype="CLxListVariant"><LoopSize runtype="CLxListVariant">'
        '<no_name runtype="lx_uint32" value="57"/><no_name runtype="lx_uint32" value="0"/></LoopSize>'
        f'<Ref runtype="CLxStringW" value="&#{"0" * 5000}65;&#{"9" * 5000};"/>'
        f'<Long runtype="lx_int64" value="-{"0" * 5000}{"7" * 30}"/></NDControl>'))
    deep = '<x runtype="lx_int32" value="1"/>'
    for depth in range(100, 0, -1):
        deep = f'<d{depth} runtype="CLxListVariant">{deep}</d{depth}>'
    f.chunk("CustomDataVar|DeepV1_0!", variant(deep))  # an element at depth 101: not decoded
    f.chunk("CustomDataVar|DeepEnoughV1_0!", variant(deep.replace("<d1 ", "<e1 ").replace("</d1>", "</e1>")
                                                    .replace('<d100 runtype="CLxListVariant">', "")
                                                    .replace("</d100>", "")))  # depth 100: decoded
    expected = placed(f, 3, {1: b"TRAIL", 2: b"END"})
    write("nd2_source_values", f, expected)

    # Chunks: frames the image does not place (f >= N, an unparsable name, an
    # empty one), a stream longer than N, paths that collide, are invalid or
    # reserved, names that read alike, an empty chunk, and families with a
    # ragged member, a member index of 2^24, a blob at their path, and a newline.
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=2))
    f.chunk("ImageMetadataLV!", experiment(node(1, {"uiCount": 2, "dPeriod": 10.0})))
    expected = placed(f, 2, {})
    f.chunk("ImageDataSeq|2!", frame_bytes(np.zeros((h, w, 1), np.uint16), w * 2, False))
    f.chunk("ImageDataSeq|01!", b"\0frame")
    f.chunk("ImageDataSeq|x!", b"")
    f.chunk("ImageDataSeq!", b"\0data")
    f.chunk("CustomData|AcqTimesCache!", struct.pack("<3d", 1.0, 2.0, 3.0))
    f.chunk("CustomData|X!", struct.pack("<2d", 5.0, 6.0))
    f.chunk("CustomData|X|1!", b"\0q")
    f.chunk("CustomData|Foo!", b"\0abc")
    f.chunk("CustomData|Foo|Bar!", b"\0xyz")
    f.chunk("CustomData|a/b!", b"\0slash")
    f.chunk("CustomData|...!", b"\0dots")
    f.chunk("CustomData|__x!", b"\0reserved")
    f.chunk("other!", b"\0other")
    f.raw_chunk(b"CustomData|\xe9!", b"\0latin1")
    f.raw_chunk(b"CustomData|\xc3\xa9!", b"\0utf8")
    f.chunk("CustomData|Empty!", b"")
    f.chunk("CustomDataSeq|F|0!", b"\0abc")
    f.chunk("CustomDataSeq|F|1!", b"\0abcde")
    f.chunk("CustomDataSeq|F|16777216!", b"\0far")
    f.chunk("CustomDataSeq|F!", b"\0blob")
    f.raw_chunk(b"CustomDataSeq|a\nb|0!", b"\0nl")
    write("nd2_source_chunks", f, expected)

    # A chunk whose JSON is 16384 bytes long (at the root) and one of 16385 (on
    # vzip_source), with numbers and strings that JSON writers format differently.
    sized = sized_level
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=1))
    f.chunk("ImageMetadataLV!", experiment(node(1, {"uiCount": 1})))
    f.chunk("AtLimitLV!", sized(16384))
    f.chunk("OverLimitLV!", sized(16385))
    write("nd2_source_json_size", f, placed(f, 1, {}))


def sized_level(target: int, legacy: bool = True) -> bytes:
    """A level with `target` bytes of JSON, of numbers and strings that JSON
    writers format differently."""
    import sys

    # the frozen reference twin of the ND2 profile (conformance/virtualize/reference)
    sys.path.insert(0, str(Path(__file__).parents[3] / "conformance" / "virtualize" / "reference"))
    from vzip_reference.nd2.lv import decode_lv
    from vzip_reference.nd2.source import json_size, lv_json

    members = [lv(f"f{i}", x) for i, x in enumerate(
        [0.1, 1e-7, 1e21, 123456789.125, 5e-324, -2.5e-300, 2.0, 1e16, 1.5e-6, 1e-6, 123e20, -0.0])]
    members += [lv("s", 'é\n\t"\\\x01\x7f\u2028😀'), lv("i", 2**31)]
    base = json_size(lv_json(decode_lv(level("SLxT", members + [lv("pad", "")]))))
    if legacy:  # nd2_source_json_size was written when -0 was JSON's 0 (1 byte), not the tag {"float":"-0"} (14)
        base -= json_size({"float": "-0"}) - 1
    return level("SLxT", members + [lv("pad", "x" * (target - base))])


def revision8() -> None:
    """Round-2 review: every placed frame's header, tags, lossless CustomData,
    sparse families and frame grids, and the root's budget (profiles/nd2.md §5.3,
    conventions/nd2/README.md §5)."""
    h, w = 3, 4

    def frame(i: int, trailing: bytes = b"") -> tuple[bytes, np.ndarray]:
        px = rng.integers(0, 200, (h, w, 1)).astype(np.uint16)
        return frame_bytes(px, w * 2, False) + trailing, px[:, :, 0]

    # Rejected: a frame between the first and the last whose header is not the
    # one the image assumes (its name length, its magic), or whose data is short.
    for name in ("nd2_reject_frame_name_length", "nd2_reject_frame_magic", "nd2_reject_frame_short"):
        f = Nd2()
        f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=3))
        f.chunk("ImageMetadataLV!", experiment(node(1, {"uiCount": 3})))
        for i in range(3):
            data, _ = frame(i)
            if i == 1 and name.endswith("short"):
                data = data[:-1]
            f.chunk(f"ImageDataSeq|{i}!", data, name_pad=64 if i == 1 and name.endswith("length") else NAME_PAD)
            if i == 1 and name.endswith("magic"):
                f.map[-1] = (f.map[-1][0], f.map[-1][1] + 1, f.map[-1][2])
        write(name, f, {})

    # Tags: integers JSON cannot hold exactly, non-finite binary64s, levels and
    # elements that would read as a tag, a top level of one record named int,
    # and integer-like member names (the first declaration of S is int32).
    # CustomData chunks decode only when their JSON keeps every byte.
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=1))
    f.chunk("ImageTextInfoLV!", level("SLxTags", [
        record(5, "big", struct.pack("<Q", 2**64 - 1)), record(4, "neg", struct.pack("<q", -(2**53))),
        record(4, "safe", struct.pack("<q", 2**53 - 1)), lv("nan", float("nan")), lv("inf", float("inf")),
        lv("ninf", float("-inf")), lv("text", "NaN"), level("one", [lv("utf16", "x")]),
        level("i", [lv("int", "5")]), level("fl", [lv("float", 1.0)]), level("two", [lv("int", 1), lv("float", 2.0)]),
        level("list", [lv("", 1.0), record(6, "", struct.pack("<d", float("inf")))])]))
    f.chunk("ImageTagLV!", lv("int", 5))
    f.chunk("CustomDataVar|CustomDataV2_0!", variant(
        '<CustomTagDescription_v1.0 runtype="CLxListVariant">'
        '<Tag_b runtype="CLxListVariant"><ID runtype="CLxStringW" value="S"/><Type runtype="lx_int32" value="2"/></Tag_b>'
        '<5 runtype="CLxListVariant"><ID runtype="CLxStringW" value="S"/><Type runtype="lx_int32" value="3"/></5>'
        "</CustomTagDescription_v1.0>"))
    f.chunk("CustomDataVar|TagsV1_0!", variant(
        '<no_name runtype="CLxListVariant"><Big runtype="lx_uint64" value="18446744073709551615"/>'
        '<Inf runtype="double" value="1e999"/><one runtype="CLxListVariant"><float runtype="double" value="1"/></one>'
        "</no_name>"))
    f.chunk("CustomData|S!", struct.pack("<i", -7))

    def tabled(name: str, members: list[tuple[str, bytes]], table: list[int] | None = None,
               shift: int = 0) -> bytes:
        """A level whose skipped bytes are the offset table: the records' offsets from
        the level's start in name order (or in the order `table` gives, an index
        possibly repeated), the last one plus `shift`."""
        head = bytes([11]) + _name(name)
        at, offsets = len(head) + 12, []
        for n, r in members:
            offsets.append((n.encode("utf-16le"), at))
            at += len(r)
        order = [o for _, o in sorted(offsets, key=lambda x: [int.from_bytes(x[0][i:i + 2], "little")
                                                               for i in range(0, len(x[0]), 2)])]
        order = [offsets[i][1] for i in table] if table is not None else order
        order[-1] += shift
        body = b"".join(r for _, r in members)
        return head + struct.pack("<IQ", len(members), at) + body + b"".join(struct.pack("<Q", o) for o in order)

    unnamed = lambda typ, payload: bytes([typ, 0]) + payload  # noqa: E731  (k = 0: the canonical empty name)
    good = (lv("a", True) + lv("b", 2.5) + lv("n", float("nan"))
            + tabled("l", [("", unnamed(3, struct.pack("<I", 1))), ("", unnamed(3, struct.pack("<I", 2)))])
            + tabled("t", [("z", lv("z", 1)), ("B", lv("B", 2)), ("a", lv("a", 3))]))
    f.chunk("CustomData|Good!", good)
    f.chunk("CustomData|Zipped!", compressed(good))
    f.chunk("CustomData|Unnamed!", unnamed(3, struct.pack("<I", 1)))
    f.chunk("CustomData|BoolByte!", record(1, "a", b"\x05"))
    f.chunk("CustomData|EmptyNul!", level("l", [lv("", 1)]))  # an empty name of k = 1
    f.chunk("CustomData|AfterNul!", bytes([3, 3]) + "a\0b".encode("utf-16le") + struct.pack("<I", 1))
    f.chunk("CustomData|Skipped!", level("l", [lv("a", 1)])[:-1] + b"\x01")
    f.chunk("CustomData|Unsorted!", tabled("t", [("z", lv("z", 1)), ("B", lv("B", 2)), ("a", lv("a", 3))], [0, 1, 2]))
    f.chunk("CustomData|Ties!", tabled("l", [("", unnamed(3, struct.pack("<I", 1))),
                                             ("", unnamed(3, struct.pack("<I", 2)))], [1, 0]))
    f.chunk("CustomData|BadTable!", tabled("t", [("a", lv("a", 1)), ("b", lv("b", 2))], shift=1))
    f.chunk("CustomData|DupTable!", tabled("t", [("a", lv("a", 1)), ("b", lv("b", 2))], [0, 0]))
    f.chunk("CustomData|NaNBits!", record(6, "x", struct.pack("<Q", 0x7FF8000000000001)))
    f.chunk("CustomData|ZippedLossy!", compressed(record(1, "a", b"\x05")))
    f.chunk("ImageLossyLV!", record(1, "a", b"\x05"))  # named LV: decoded, as true
    f.chunk("ImageMetadataSeqLV|00!", picture([("A", 255, 1)], None))  # n = 0: at the root
    f.chunk("ImageMetadataSeqLV|01!", picture([("A", 255, 1)], None))  # n = 1: on vzip_source
    data, px = frame(0)
    f.chunk("ImageDataSeq|0!", data)
    write("nd2_source_tags", f, {"0/0/c/0/0": px})

    # Sparse: a time loop of 3,000,000 with frames 0, 1 and 2000 (the frame
    # times' chunks hold 8043 frames; only the first is written), trailing
    # bytes on frames 0 and 2000, frames beyond N at 1 and 3000 past it, and a
    # family with members 0 and 5000: each family keeps the indices below
    # 16 x 2 + 1024 = 1056, and the others go to other.
    n = 3_000_000
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=3))
    f.chunk("ImageMetadataLV!", experiment(node(1, {"uiCount": n, "dPeriod": 1.0})))
    expected = {}
    for i, trailing in ((0, b"T0"), (1, b""), (2000, b"T2000")):
        data, px = frame(i, trailing)
        f.chunk(f"ImageDataSeq|{i}!", data)
        expected[f"0/0/c/{i}/0/0"] = px
    for i in (n + 1, n + 3000):
        f.chunk(f"ImageDataSeq|{i}!", frame(i)[0])
    f.chunk("CustomDataSeq|x|0!", b"a")
    f.chunk("CustomDataSeq|x|5000!", b"bb")
    write("nd2_source_sparse", f, expected)

    # The root's budget: five chunks of 12000 to 15000 bytes of JSON (over
    # 65536 in all); the largest moves to vzip_source, and of the two largest
    # (equal), the first in map order.
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=1))
    for name, size in (("ALV!", 12000), ("BLV!", 15000), ("CLV!", 15000), ("DLV!", 14000), ("ELV!", 13000)):
        f.chunk(name, lv("s", "x" * (size - 8)))
    data, px = frame(0)
    f.chunk("ImageDataSeq|0!", data)
    write("nd2_source_root_budget", f, {"0/0/c/0/0": px})


def revision9() -> None:
    """Round-3 review: byte arrays as one value, vzip_source's budget, decodes
    charged whether or not they succeed, array-index names, -0, and the frame
    times in chunks of at most 64 KiB (conventions/nd2/README.md §5.1-§5.3)."""
    h, w = 3, 4

    def frame0(f: Nd2) -> dict:
        px = rng.integers(0, 200, (h, w, 1)).astype(np.uint16)
        f.chunk("ImageDataSeq|0!", frame_bytes(px, w * 2, False))
        return {"0/0/c/0/0": px[:, :, 0]}

    # The JSON-size boundary (16384 at the root, 16385 on vzip_source), with -0 a tag.
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=1))
    f.chunk("AtLimitLV!", sized_level(16384, legacy=False))
    f.chunk("OverLimitLV!", sized_level(16385, legacy=False))
    write("nd2_source_json_size_tagged", f, frame0(f))

    # vzip_source's budget: the picture metadata of frames 1-4, of 30008, 30008,
    # 10008 and 3008 bytes of JSON: 1, 2 and 4 fit in 64 KiB, 3 is bytes. Byte
    # arrays: one of 100 bytes (a JSON array), one of 70000 and one of 8 MiB
    # (compressed), whose JSON would not fit (bytes). Names that are array
    # indices (pairs) or not ("01"), and -0 (a tag), guessed and named.
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=1))
    for i, size in ((1, 30000), (2, 30000), (3, 10000), (4, 3000)):
        f.chunk(f"ImageMetadataSeqLV|{i}!", lv("s", "x" * size))
    f.chunk("BytesLV!", lv("b", bytes(range(100))))
    f.chunk("BigBytesLV!", lv("b", bytes(70000)))
    f.chunk("HugeBytesLV!", compressed(lv("b", bytes(8 << 20))))
    f.chunk("CustomData|Index!", lv("b", 1.0) + lv("1", 2.0) + lv("a", {"z": 1, "0": 2}))
    f.chunk("IndexLV!", lv("L", {"x": 1, "01": 2, "10": 3}))
    f.chunk("CustomData|NegZero!", lv("a", -0.0) + lv("b", 1.0))
    f.chunk("NegZeroLV!", lv("a", -0.0))
    f.chunk("CustomDataVar|NegZeroV1_0!", variant('<z runtype="double" value="-0.0"/><one runtype="double" value="1"/>'))
    write("nd2_source_node_budget", f, frame0(f))

    # Charged decodes: three CustomData chunks that inflate to 30 MiB and are not
    # lossless (the third inflates past what is left of the 64 MiB), then one
    # that would decode, but the budget is spent.
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=1))
    failing = compressed(lv("b", bytes(30 << 20)) + record(1, "x", b"\x02"))
    for i in range(3):
        f.chunk(f"CustomData|F{i}!", failing)
    f.chunk("CustomData|Ok!", lv("a", 1))
    write("nd2_source_charged", f, frame0(f))

    # An invalid (truncated) zlib stream of 70000 bytes is charged the most it
    # could inflate to, 1032 x 70000 bytes, capped at what is left: the budget.
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=1))
    noise = np.random.default_rng(9).integers(0, 256, 100_000, dtype=np.uint8).tobytes()
    f.chunk("CustomData|Truncated!", (bytes([76, 0]) + bytes(10) + zlib.compress(noise))[:70000])
    f.chunk("CustomData|Ok!", lv("a", 1))
    write("nd2_source_charged_invalid", f, frame0(f))

    # The frame times of a time loop of 40 and a z loop of 2^21, with frame 0
    # of each time point: each in its own chunk of 8192 values (64 KiB).
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(1, 1, 1, 8, sequence=40))
    f.chunk("ImageMetadataLV!", experiment(node(1, {"uiCount": 40, "dPeriod": 1.0}, [node(4, {"uiCount": 2**21, "dZStep": 1.0})])))
    expected = {}
    for t in range(40):
        f.chunk(f"ImageDataSeq|{t * 2**21}!", struct.pack("<d", t) + bytes([t]))
        expected[f"0/0/c/{t}/0/0/0"] = np.array([[t]], dtype=np.uint8)
    write("nd2_source_frame_times", f, expected)


def revision10() -> None:
    """Round-3 follow-up: the profile's chunks inflate to at most 64 MiB
    (profiles/nd2.md §5.1), and other and empty past vzip_source's budget
    (conventions/nd2/README.md §5.4)."""
    h, w = 3, 4

    # ImageAttributesLV! compressed, inflating to more than 2^26 bytes.
    f = Nd2()
    f.chunk("ImageAttributesLV!", compressed(attributes(w, h, 1, 16, sequence=1) + lv("pad", bytes((1 << 26) + 1))))
    write("nd2_reject_inflate_limit", f, {})

    # 4000 chunks without a valid path (other) and 6000 empty ones: both lists
    # are over vzip_source's 64 KiB, so each is an array of its JSON text.
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=1))
    for i in range(4000):
        f.chunk(f"CustomData|a/{i}!", b"\0", name_pad=32)
    for i in range(6000):
        f.chunk(f"Empty|{i}!", b"", name_pad=32)
    px = rng.integers(0, 200, (h, w, 1)).astype(np.uint16)
    f.chunk("ImageDataSeq|0!", frame_bytes(px, w * 2, False))
    write("nd2_source_names_spill", f, {"0/0/c/0/0": px[:, :, 0]})


def revision11() -> None:
    """Round-4 review: positions and the profile's LV records are bounded
    (profiles/nd2.md §5.1, §5.3), sparse frame times take smaller chunks, a
    large declared tag is its index, and path segments that a store cannot
    hold go to other (conventions/nd2/README.md §5.2-§5.4)."""
    h, w = 3, 4

    def frame0(f: Nd2) -> dict:
        px = rng.integers(0, 200, (h, w, 1)).astype(np.uint16)
        f.chunk("ImageDataSeq|0!", frame_bytes(px, w * 2, False), name_pad=32)
        return {"0/0/c/0/0": px[:, :, 0]}

    # 2^16 + 1 positions (empty points), compressed: more than 2^16 positions.
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=1))
    f.chunk("ImageMetadataLV!", compressed(experiment({"eType": 2, "uLoopPars": {"Points": [{}] * (2**16 + 1)}})))
    write("nd2_reject_positions", f, {})

    # 1025 positions of 1024 components: more than 2^20 positions x components.
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(1, 1, 1024, 8, sequence=1))
    f.chunk("ImageMetadataLV!", compressed(experiment({"eType": 2, "uLoopPars": {"Points": [{}] * 1025}})))
    write("nd2_reject_position_channels", f, {})

    # The experiment and the picture metadata, each of 600000 records (under
    # 2^20 each, over it together): the profile's chunks share the budget.
    many = level("Many", [record(1, "", b"\x01")] * 600_000)
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=1))
    f.chunk("ImageMetadataLV!", compressed(experiment(node(1, {"uiCount": 1})) + many))
    f.chunk("ImageMetadataSeqLV|0!", compressed(picture([("BF", 0xFFFFFF, 1)], None) + many))
    write("nd2_reject_profile_records", f, {})

    # 2000 frames 8192 apart in a time loop of 2^31 - 1: at 64 KiB a chunk
    # each, more than max(1 MiB, 512 x 2000) bytes; at 512 bytes (64 values), 1 MiB.
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(1, 1, 1, 8, sequence=2000), name_pad=32)
    f.chunk("ImageMetadataLV!", experiment(node(1, {"uiCount": 2**31 - 1, "dPeriod": 1.0})), name_pad=32)
    expected = {}
    for k in range(2000):
        f.chunk(f"ImageDataSeq|{k * 8192}!", struct.pack("<d", k) + bytes([k % 251]), name_pad=24)
        if k < 20:
            expected[f"0/0/c/{k * 8192}/0/0"] = np.array([[k % 251]], dtype=np.uint8)
    write("nd2_source_frame_times_sparse", f, expected)

    # Two declared streams: one's member is 16 KiB of JSON at most (kept), the
    # other's more (its index, 2, among the members; member 1 is not an object).
    small = '<Desc runtype="CLxStringW" value="small"/>'
    big = f'<Desc runtype="CLxStringW" value="{"x" * 16400}"/>'
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=1))
    f.chunk("CustomDataVar|CustomDataV2_0!", variant(
        '<CustomTagDescription_v1.0 runtype="CLxListVariant">'
        f'<T0 runtype="CLxListVariant"><ID runtype="CLxStringW" value="Small"/><Type runtype="lx_int32" value="3"/>{small}</T0>'
        '<T1 runtype="CLxStringW" value="not a member"/>'
        f'<T2 runtype="CLxListVariant"><ID runtype="CLxStringW" value="Big"/><Type runtype="lx_int32" value="2"/>{big}</T2>'
        "</CustomTagDescription_v1.0>"))
    f.chunk("CustomData|Small!", struct.pack("<d", 1.5))
    f.chunk("CustomData|Big!", struct.pack("<i", -3))
    write("nd2_source_tag_index", f, frame0(f))

    # Names whose paths would meet a store's documents, or hold U+0000: other.
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(w, h, 1, 16, sequence=1))
    for name in ("zarr.json!", "Foo|zarr.json!", "Foo|.zarray!", ".zgroup|x!", "zarr.json~!", "Foo|Bar!"):
        f.chunk(name, name.encode(), name_pad=32)
    f.raw_chunk(b"Foo\x00Baz!", b"\x01\x02\x03", name_pad=32)
    write("nd2_source_document_names", f, frame0(f))


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    tz_uint16()
    padded_rgb()
    compressed_positions()
    float_uncalibrated()
    rejected()
    edges()
    revision3()
    revision4()
    revision6()
    streams()
    revision7()
    revision8()
    revision9()
    revision10()
    revision11()
    for p in sorted(OUT.glob("nd2_*.nd2")):
        print(p.name, p.stat().st_size)
