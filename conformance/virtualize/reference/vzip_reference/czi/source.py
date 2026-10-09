"""The CZI source metadata node (conventions/czi/README.md §5)."""

from __future__ import annotations

import json
import struct

from vzip.virtualize.common import Plan, Reader, blob_chunks, family_plans, grid_chunks, json_base64, json_text
from vzip_reference.czi.segments import LETTERS, Attachment, Directory, MetadataSegment, Subblocks, guid
from vzip_reference.nd2.source import copied_bytes, json_size

MAX_COPIED = 1 << 16  # the chunk limit of copied columns (conventions/czi/README.md §5.3)
MAX_NODE = 1 << 16  # vzip_source's S, past which the attachment list is an array (§5.6)
MAX_EVENTS = 1 << 20  # an event list is decoded up to this many events
MAX_EVENT_BYTES = 1 << 26  # and this many bytes
INDEX_PATH = "attachments/index"


def copied(path: str, data_type: str, shape: list[int], item: int, data: bytes, dims: list[str]) -> Plan:
    """Values held in memory, copied, and cut as contiguous values with the chunk limit 2^16."""
    chunk_shape, chunks = grid_chunks(0, shape, item, lambda o, n: data[o : o + n], limit=MAX_COPIED)
    resolved = {k: v if isinstance(v, bytes) else b"".join(data[r[0] : r[0] + r[1]] if isinstance(r, tuple) else r
                                                            for r in v) for k, v in chunks.items()}
    return Plan(path, data_type, shape, chunk_shape, dims, resolved)


def bytes_plan(path: str, offset: int, length: int, attributes: dict | None = None) -> Plan:
    size, chunks = blob_chunks(offset, length)
    return Plan(path, "uint8", [length], [size], ["byte"], chunks, attributes=attributes)


def directory_plans(d: Directory) -> list[Plan]:
    """The directory's columns (conventions/czi/README.md §5.3)."""
    n = d.count
    if n == 0:
        return []
    plans = [copied("directory/pixel_type", "int32", [n], 4, d.pixel_type.tobytes(), ["index"]),
             copied("directory/compression", "int32", [n], 4, d.compression.tobytes(), ["index"]),
             copied("directory/pyramid_type", "uint8", [n], 1, d.pyramid_type.tobytes(), ["index"])]
    if any(d.spare):
        plans.append(copied("directory/spare", "uint8", [n, 5], 1, bytes(d.spare), ["index", "byte"]))
    for letter in LETTERS:
        c = d.columns.get(letter)
        if c is None:
            continue
        for name, data_type, values in (("start", "int32", c.start), ("size", "int32", c.size),
                                        ("stored_size", "int32", c.stored),
                                        ("start_coordinate", "float32", c.coordinate),
                                        ("position", "int8", c.position)):
            plans.append(copied(f"directory/{letter}/{name}", data_type, [n], values.itemsize, values.tobytes(),
                                ["index"]))
    return plans


def subblock_plans(read: Reader, d: Directory, s: Subblocks, unplaced: list[int],
                   trailing: list[tuple[int, int, int]]) -> list[Plan]:
    """The subblock families (conventions/czi/README.md §5.4); `trailing` lists (i, offset, length)."""
    n = d.count
    metadata, attachment, data, entry = [], [], [], []
    for i in range(n):
        o = d.file_position[i] + 32
        if s.metadata[i]:
            metadata.append((i, o + s.length[i], s.metadata[i]))
        if s.attachment[i]:
            attachment.append((i, o + s.length[i] + s.metadata[i] + s.data[i], s.attachment[i]))
        if not s.agrees[i]:
            entry.append((i, o + 16, s.copy_length[i]))
    for i in unplaced:
        if s.data[i]:
            data.append((i, d.file_position[i] + 32 + s.length[i] + s.metadata[i], s.data[i]))
    plans: list[Plan] = []
    for name, members in (("metadata", metadata), ("attachment", attachment), ("data", data),
                          ("trailing", trailing), ("entry", entry)):
        if members:
            plans += family_plans(f"subblocks/{name}", members, read, count=n)
    return plans


def metadata_plans(m: MetadataSegment | None) -> list[Plan]:
    if m is None:
        return []
    plans = []
    if m.xml:
        plans.append(bytes_plan("metadata/xml", m.offset + 32 + 256, m.xml))
    if m.attachment:
        plans.append(bytes_plan("metadata/attachment", m.offset + 32 + 256 + m.xml, m.attachment))
    return plans


def _events(data: bytes, count: int) -> list[tuple[int, int, int]] | None:
    """The events of an event list's data from byte 8: (offset in data, its time and
    type at offset + 4, description length), or None when they do not fill it exactly."""
    out, pos = [], 8
    for _ in range(count):
        if pos + 20 > len(data):
            return None
        e, _, _, u = struct.unpack_from("<idii", data, pos)
        if u < 0 or e != 20 + u or pos + 20 + u > len(data):
            return None
        out.append((pos, 0, u))
        pos += 20 + u
    return out if pos == len(data) else None


def attachment_plans(read: Reader, atts: list[Attachment]) -> tuple[list, list[Plan]]:
    """The attachment list and the attachments' arrays (conventions/czi/README.md §5.6)."""
    listed, plans = [], []
    for k, a in enumerate(atts):
        if not a.a1:
            listed.append({"entry": json_base64(a.entry)})
            continue
        kind = a.entry[40:48].split(b"\0", 1)[0]
        item = {"name": json_text(a.entry[48:128]), "content_file_type": json_text(a.entry[40:48]),
                "content_guid": guid(a.entry[24:40])}
        if a.segment_entry != a.entry:
            item["segment_entry"] = json_base64(a.segment_entry)
        s, at, path = a.data_size, a.data_offset, f"attachments/{k}"
        form = "bytes" if s else "empty"
        if kind in (b"CZTIMS", b"CZFOC") and s >= 8:
            size, c = struct.unpack("<ii", read(at, 8))
            if s == 8 + 8 * c:
                form = "time_stamps" if kind == b"CZTIMS" else "focus_positions"
                shape, chunks = grid_chunks(at + 8, [c], 8, read)
                plans.append(Plan(path, "float64", [c], shape, ["index"], chunks,
                                  attributes=_own({"size": size})))
        elif kind == b"CZEVL" and 8 <= s <= MAX_EVENT_BYTES:
            data = read(at, s)
            size, c = struct.unpack_from("<ii", data)
            events = _events(data, c) if 0 <= c <= MAX_EVENTS else None
            if events is not None:
                form = "event_list"
                times = b"".join(data[p + 4 : p + 12] for p, _, _ in events)
                types = b"".join(data[p + 12 : p + 16] for p, _, _ in events)
                plans.append(copied(f"{path}/time", "float64", [c], 8, times, ["index"]))
                plans.append(copied(f"{path}/type", "int32", [c], 4, types, ["index"]))
                described = [(e, at + p + 20, u) for e, (p, _, u) in enumerate(events) if u > 0]
                if described:
                    plans += family_plans(f"{path}/description", described, read, count=c)
                plans.append(_group(path, {"size": size}))
        if form == "bytes":
            plans.append(bytes_plan(path, at, s))
        item["form"] = form
        listed.append(item)
    return listed, plans


def _own(s: dict) -> dict:
    from vzip.virtualize.common import declare

    return declare({}, "czi", None, s)


def _group(path: str, s: dict) -> Plan:
    """A marker for a group with source metadata (emitted by the caller)."""
    return Plan(path, "group", [], [], [], {}, attributes=_own(s))


def node_metadata(listed: list) -> tuple[dict, list[Plan]]:
    """vzip_source's S, and the index array when the list is past the budget."""
    if not listed:
        return {}, []
    if json_size({"attachments": listed}) <= MAX_NODE:
        return {"attachments": listed}, []
    text = json.dumps(listed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {"attachments": INDEX_PATH}, [copied_bytes(INDEX_PATH, text)]


def segment_plans(read: Reader, segments: list[tuple[int, bytes, int, int]]) -> list[Plan]:
    """The segments nothing references (conventions/czi/README.md §5.7)."""
    if not segments:
        return []
    count = len(segments)
    ids = b"".join(ident for _, ident, _, _ in segments)
    plans = [copied("segments/id", "uint8", [count, 16], 1, ids, ["index", "byte"])]
    members = []
    for k, (o, _, allocated, used) in enumerate(segments):
        n = min(used or allocated, allocated)
        if n:
            members.append((k, o + 32, n))
    if members:
        plans += family_plans("segments/data", members, read, count=count)
    return plans


def tail_plan(start: int, size: int) -> list[Plan]:
    return [bytes_plan("tail", start, size - start)] if start < size else []
