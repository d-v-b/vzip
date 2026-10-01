"""vzip messages built with the official protobuf runtime (independent of vzip.pb).

Used by the conformance kit to compute canonical encodings and to decode
payloads found in archives under test.
"""

from __future__ import annotations

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

_F = descriptor_pb2.FieldDescriptorProto


def _build():
    fdp = descriptor_pb2.FileDescriptorProto(name="vzip.proto", package="vzip.v0", syntax="proto3")

    rng = fdp.message_type.add(name="Range")
    for n, i, t in [("source", 1, _F.TYPE_UINT32), ("offset", 3, _F.TYPE_UINT64),
                    ("length", 4, _F.TYPE_UINT64)]:
        rng.field.add(name=n, number=i, type=t, label=_F.LABEL_OPTIONAL)
    rng.oneof_decl.add(name="_data")  # proto3 `optional` = synthetic oneof
    rng.field.add(name="data", number=5, type=_F.TYPE_BYTES, label=_F.LABEL_OPTIONAL,
                  oneof_index=0, proto3_optional=True)

    cat = fdp.message_type.add(name="Concat")
    cat.field.add(name="parts", number=1, type=_F.TYPE_MESSAGE, type_name=".vzip.v0.Range",
                  label=_F.LABEL_REPEATED)

    src = fdp.message_type.add(name="Source")
    src.oneof_decl.add(name="kind")
    for n, i, t in [("url", 1, _F.TYPE_STRING), ("key", 2, _F.TYPE_STRING),
                    ("data", 3, _F.TYPE_BYTES)]:
        src.field.add(name=n, number=i, type=t, label=_F.LABEL_OPTIONAL, oneof_index=0)
    for j, (n, i, t) in enumerate([("size", 4, _F.TYPE_UINT64), ("etag", 5, _F.TYPE_STRING),
                                   ("modified_not_after", 6, _F.TYPE_INT64)]):
        src.oneof_decl.add(name=f"_{n}")
        src.field.add(name=n, number=i, type=t, label=_F.LABEL_OPTIONAL, oneof_index=1 + j,
                      proto3_optional=True)

    tbl = fdp.message_type.add(name="SourceTable")
    tbl.field.add(name="sources", number=1, type=_F.TYPE_MESSAGE, type_name=".vzip.v0.Source",
                  label=_F.LABEL_REPEATED)

    idx = fdp.message_type.add(name="CdIndex")
    page = idx.nested_type.add(name="Page")
    for n, i, t in [("first_key", 1, _F.TYPE_STRING), ("offset", 2, _F.TYPE_UINT64),
                    ("length", 3, _F.TYPE_UINT64)]:
        page.field.add(name=n, number=i, type=t, label=_F.LABEL_OPTIONAL)
    pin = idx.nested_type.add(name="Pinned")
    for n, i, t in [("key", 1, _F.TYPE_STRING), ("data_offset", 2, _F.TYPE_UINT64),
                    ("size", 3, _F.TYPE_UINT64), ("csize", 4, _F.TYPE_UINT64),
                    ("method", 5, _F.TYPE_UINT32)]:
        pin.field.add(name=n, number=i, type=t, label=_F.LABEL_OPTIONAL)
    idx.field.add(name="pages", number=1, type=_F.TYPE_MESSAGE,
                  type_name=".vzip.v0.CdIndex.Page", label=_F.LABEL_REPEATED)
    idx.field.add(name="pinned", number=2, type=_F.TYPE_MESSAGE,
                  type_name=".vzip.v0.CdIndex.Pinned", label=_F.LABEL_REPEATED)

    pool = descriptor_pool.DescriptorPool()
    pool.Add(fdp)
    get = lambda n: message_factory.GetMessageClass(pool.FindMessageTypeByName(f"vzip.v0.{n}"))
    return get("Range"), get("Concat"), get("Source"), get("SourceTable"), get("CdIndex")


Range, Concat, Source, SourceTable, CdIndex = _build()


def range_msg(r: dict):
    """Description range dict -> Range message."""
    m = Range()
    if "data" in r:
        m.data = bytes.fromhex(r["data"])
    else:
        m.source, m.offset, m.length = r.get("source", 0), r.get("offset", 0), r.get("length", 0)
    return m


def payload(ranges: list[dict]) -> tuple[int, bytes]:
    """(extra field header ID, canonical payload) a writer must produce."""
    if len(ranges) == 1:
        return 0x7A76, range_msg(ranges[0]).SerializeToString(deterministic=True)
    c = Concat()
    for r in ranges:
        c.parts.append(range_msg(r))
    return 0x7A77, c.SerializeToString(deterministic=True)


def source_table(sources: list[dict]) -> bytes:
    t = SourceTable()
    for s in sources:
        m = t.sources.add()
        for k, v in s.items():
            setattr(m, k, bytes.fromhex(v) if k == "data" else v)
    return t.SerializeToString(deterministic=True)
