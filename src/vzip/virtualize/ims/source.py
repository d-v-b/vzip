"""The HDF5 attributes of an Imaris file as JSON: the source metadata of the
IMS convention (conventions/ims/README.md §5)."""

from __future__ import annotations

import struct

from vzip.virtualize.common import Rejected, decode_text, json_base64, json_number
from vzip.virtualize.ims.hdf5 import Hdf5, attribute_value

MAX_VALUE_BYTES = 1 << 26  # the most attribute data recorded per file
INTEGERS = {1: "b", 2: "h", 4: "i", 8: "q"}
FLOATS = {4: "f", 8: "d"}


def value_json(cls: int, size: int, bits: int, count: int, data: bytes):
    """An attribute's value by its datatype class: strings, numbers, or opaque."""
    if size == 0:
        return {"class": cls, "size": size, "data": json_base64(data)}
    if cls == 3:
        if size == 1:
            return decode_text(data.split(b"\0", 1)[0])
        return [decode_text(data[i:i + size].split(b"\0", 1)[0]) for i in range(0, count * size, size)]
    order = ">" if bits & 1 else "<"
    if cls == 0 and size in INTEGERS:
        f = INTEGERS[size] if bits & 8 else INTEGERS[size].upper()
        return [json_number(v) for v in struct.unpack(order + f * count, data)]
    if cls == 1 and size in FLOATS:
        return [json_number(v) for v in struct.unpack(order + FLOATS[size] * count, data)]
    return {"class": cls, "size": size, "data": json_base64(data)}


class Source:
    """Reads attributes for the source metadata, sharing one budget; a failure
    to read an object or an attribute records null, and never rejects."""

    def __init__(self, f: Hdf5) -> None:
        self.f = f
        self.used = 0

    def attributes(self, at: int | None) -> dict | None:
        try:
            raw = self.f.attributes(at)
        except Rejected:
            return None
        out = {}
        for name in sorted(raw):
            try:
                datatype, count, data = attribute_value(raw[name])
            except Rejected:
                out[decode_text(name)] = None
                continue
            if self.used + len(data) > MAX_VALUE_BYTES:
                out[decode_text(name)] = None
                continue
            self.used += len(data)
            out[decode_text(name)] = value_json(datatype.cls, datatype.size, datatype.bits, count, data)
        return out

    def group(self, links: dict, name: bytes) -> dict | None:
        """The attributes of the group that a link leads to, or None."""
        try:
            at = self.f.follow(links, name)
            self.f.links(at)  # it must be a group
        except Rejected:
            return None
        return self.attributes(at)


def source_json(f: Hdf5, root_links: dict, info_links: dict, channels: list[tuple[str, int]]) -> dict:
    """{"root", "DataSetInfo", "DataSet"}: the root group's attributes, those of
    each group that DataSetInfo links to, and those of each channel group."""
    s = Source(f)
    root = s.attributes(f.root)
    info = {decode_text(name): s.group(info_links, name) for name in sorted(info_links)}
    data = {path: s.attributes(at) for path, at in channels}
    return {"root": root, "DataSetInfo": info, "DataSet": data}
