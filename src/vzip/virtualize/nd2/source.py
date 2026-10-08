"""Every metadata chunk of an ND2 file as JSON: the source metadata of the ND2
convention (conventions/nd2/README.md §5)."""

from __future__ import annotations

import re
import struct

from vzip.virtualize.common import Reader, Rejected, decode_text, json_base64, json_number
from vzip.virtualize.nd2.lv import Scalar, decode_lv

CHUNK_MAGIC = 0x0ABECEDA
MAX_DATA_BYTES = 1 << 26  # the most chunk data recorded per file
FRAME = re.compile(rb"ImageDataSeq\|")


def lv_json(v):
    """An LV value as JSON: objects, lists, and scalars by their type."""
    if isinstance(v, Scalar):
        return v.value if v.type in (1, 8) else json_number(v.value)
    if isinstance(v, list):
        return [lv_json(x) for x in v]
    return {k: lv_json(x) for k, x in v.items()}


def is_lv(name: bytes) -> bool:
    return name.endswith(b"LV!") or b"LV|" in name


def chunks_json(read: Reader, size: int, chunks: dict[bytes, int]) -> dict:
    """The chunk map's chunks other than frames, in map order, sharing one budget."""
    out, used = {}, 0
    for name, offset in chunks.items():
        if FRAME.match(name):
            continue
        entry: dict = {}
        out[decode_text(name)] = entry
        if offset + 16 > size:
            continue
        magic, n, d = struct.unpack("<IIQ", read(offset, 16))
        if magic != CHUNK_MAGIC:
            continue
        start = offset + 16 + n
        if start + d > size or used + d > MAX_DATA_BYTES:
            entry["size"] = json_number(d)
            continue
        used += d
        data = read(start, d)
        if is_lv(name):
            try:
                entry["lv"] = lv_json(decode_lv(data, MAX_DATA_BYTES))
                continue
            except Rejected:
                pass
        entry["data"] = json_base64(data)
    return out
