"""Writes synthetic ND2 files (format version 3) to web/test/fixtures/, with
the pixels each one should decode to, covering the rules of the ND2 profile
(VIRTUALIZE.md §4) that the public corpus does not: compressed frames, padded
rows, multi-period time loops, disabled positions, sibling and spectral
experiment nodes, missing frames, float data, and the inputs it rejects.

Each `<name>.nd2` has `<name>.npz`: one array per expected chunk key.

Usage: uv run python web/test/nd2_fixtures.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

OUT = Path(__file__).parent / "fixtures"
MAGIC = 0x0ABECEDA
NAME_PAD = 4072  # NIS-Elements pads chunk names so that headers are 4 KiB


# ---- lite variant (VIRTUALIZE.md §4.2)

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


# ---- file layout (§4.1)

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
        expected[f"0/0/c/{i // z}/0/{i % z}/0/0"] = np.moveaxis(px, -1, 0)
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
    """The edges of §4 that the files above do not reach."""
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
    """The typed-member and loop-kind rules of revision 3 (§4.2, §4.3)."""
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
    """Whole-tree checks, validity lists everywhere, and nesting (§4.2, §4.3)."""
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
    # pItemValid holds flags on every node, not only position loops.
    write_case("nd2_reject_itemvalid_on_time",
               experiment({"eType": 1, "uLoopPars": {"uiCount": 1}, "pItemValid": ["x"]}))


def revision6() -> None:
    """Stage positions become translations (§4.6)."""
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
    for p in sorted(OUT.glob("nd2_*.nd2")):
        print(p.name, p.stat().st_size)
