"""The hand-written codec must agree with the official protobuf runtime."""
import pytest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from vzip.pb import Concat, Range, Source, decode_source_table, encode_source_table


def _official():
    F = descriptor_pb2.FieldDescriptorProto
    fdp = descriptor_pb2.FileDescriptorProto(name="vzip.proto", package="vzip.v0", syntax="proto3")
    rng = fdp.message_type.add(name="Range")
    for n, i, t in [("source", 1, F.TYPE_UINT32), ("offset", 3, F.TYPE_UINT64),
                    ("length", 4, F.TYPE_UINT64), ("data", 5, F.TYPE_BYTES)]:
        rng.field.add(name=n, number=i, type=t, label=F.LABEL_OPTIONAL)
    cat = fdp.message_type.add(name="Concat")
    cat.field.add(name="parts", number=1, type=F.TYPE_MESSAGE, type_name=".vzip.v0.Range",
                  label=F.LABEL_REPEATED)
    src = fdp.message_type.add(name="Source")
    src.oneof_decl.add(name="kind")
    for n, i, t in [("url", 1, F.TYPE_STRING), ("key", 2, F.TYPE_STRING), ("data", 3, F.TYPE_BYTES)]:
        src.field.add(name=n, number=i, type=t, label=F.LABEL_OPTIONAL, oneof_index=0)
    tbl = fdp.message_type.add(name="SourceTable")
    tbl.field.add(name="sources", number=1, type=F.TYPE_MESSAGE, type_name=".vzip.v0.Source",
                  label=F.LABEL_REPEATED)
    pool = descriptor_pool.DescriptorPool()
    pool.Add(fdp)
    get = lambda n: message_factory.GetMessageClass(pool.FindMessageTypeByName(f"vzip.v0.{n}"))
    return get("Range"), get("Concat"), get("Source"), get("SourceTable")


def _fill_range(msg, r: Range):
    msg.source, msg.offset, msg.length = r.source, r.offset, r.length
    if r.data is not None:
        msg.data = r.data
    return msg


def _fill_source(msg, s: Source):
    for name in ("url", "key", "data"):
        if getattr(s, name) is not None:
            setattr(msg, name, getattr(s, name))
    return msg


def test_wire_compat():
    PRange, PConcat, PSource, PTable = _official()
    ranges = [
        Range(offset=10575, length=1157),
        Range(source=3, offset=2**40, length=2**33),
        Range(data=b"\x00\xff"),
    ]
    for ours in ranges:
        theirs = _fill_range(PRange(), ours).SerializeToString()
        assert ours.encode() == theirs
        assert Range.decode(theirs) == ours
    for ours in [Concat(tuple(ranges)), Concat(tuple(ranges[1:])), Concat(())]:
        theirs = PConcat()
        for r in ours.parts:
            _fill_range(theirs.parts.add(), r)
        assert ours.encode() == theirs.SerializeToString()
        assert Concat.decode(theirs.SerializeToString()) == ours
    # empty url / key / data are still a set oneof member, and must round-trip as such
    sources = [Source(url="s3://b/k.nc"), Source(key="__vz__/idx/a"), Source(data=b"HDR"),
               Source(url=""), Source(data=b""), Source(url="ü")]
    theirs = PTable()
    for src in sources:
        _fill_source(theirs.sources.add(), src)
    assert encode_source_table(sources) == theirs.SerializeToString()
    assert decode_source_table(theirs.SerializeToString()) == sources


def test_single_range_size():
    # source 0, 2-byte offset, 2-byte length: 3 + 3 bytes, no list wrapper
    assert Range(offset=10575, length=1157).encode().hex() == "18cf52208509"


def test_source_needs_exactly_one_kind():
    with pytest.raises(ValueError, match="exactly one"):
        Source(url="a", key="b")
