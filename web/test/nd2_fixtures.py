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


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    tz_uint16()
    padded_rgb()
    compressed_positions()
    float_uncalibrated()
    rejected()
    for p in sorted(OUT.glob("nd2_*.nd2")):
        print(p.name, p.stat().st_size)
