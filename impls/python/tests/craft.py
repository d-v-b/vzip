"""Low-level archive crafting helpers for tests (allows building invalid archives)."""

import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vzip_impl import pb  # noqa: E402


def deflate(b):
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(b) + c.flush()


class E:
    def __init__(self, name, body=b"", method=0, usize=None, csize=None, extra=b"", flags=0x0800,
                 offset=None, cd_name=None):
        self.name = name.encode() if isinstance(name, str) else name
        self.body = body
        self.method = method
        self.usize = len(body) if usize is None else usize
        self.csize = len(body) if csize is None else csize
        self.extra = extra
        self.flags = flags
        self.offset = offset
        self.cd_name = cd_name


def ref_extra(payload, rid=0x7A76):
    return struct.pack("<HH", rid, len(payload)) + payload


def range_payload(source=0, offset=0, length=0, data=None):
    return pb.encode_range(source, offset, length, data)


def cd_rec(e, off):
    name = e.cd_name if e.cd_name is not None else e.name
    o = off if e.offset is None else e.offset
    return struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, e.flags, e.method, 0, 0x21,
                       zlib.crc32(e.body), e.csize, e.usize, len(name), len(e.extra), 0, 0, 0, 0,
                       o) + name + e.extra


def build(entries, sources_pb=b"", sources_body=None, index_pb=None, index_body=None,
          magic=b"vzip/0", comment=None, cd_override=None, cd_extra=b"", zip64=True,
          eocd_fields=None, prefix_cd=None, cd_sort=False):
    """Assemble an archive. Entries are written in order, then sources, then index, then CD.

    `zip64=False` leaves out the zip64 end records (invalid since revision 9). `eocd_fields`
    replaces the end of central directory record's (count, count, cd size, cd offset), which
    are all ones in a valid archive."""
    out = bytearray()
    recs = []
    for e in entries:
        off = len(out)
        out += struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, e.flags, e.method, 0, 0x21,
                           zlib.crc32(e.body), e.csize, e.usize, len(e.name), 0) + e.name + e.body
        recs.append((e, off))
    sb = deflate(sources_pb) if sources_body is None else sources_body
    se = E("__vz__/sources", sb, 8, usize=len(sources_pb))
    soff = len(out)
    out += struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0x800, 8, 0, 0x21, 0, len(sb), len(sources_pb),
                       len(se.name), 0) + se.name + sb
    s_body_off = soff + 30 + len(se.name)
    fmt = [(se, soff)]
    idx_info = None
    if index_pb is not None or index_body is not None:
        ib = deflate(index_pb) if index_body is None else index_body
        ie = E("__vz__/index", ib, 8, usize=len(index_pb or b""))
        ioff = len(out)
        out += struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0x800, 8, 0, 0x21, 0, len(ib), ie.usize,
                           len(ie.name), 0) + ie.name + ib
        idx_info = (ioff + 30 + len(ie.name), len(ib))
        fmt.append((ie, ioff))
    cd_off = len(out)
    body = recs
    if cd_sort:
        body = sorted(recs, key=lambda r: r[0].cd_name or r[0].name)
    if cd_override is not None:
        cd = cd_override
    else:
        cd = b"".join(cd_rec(e, o) for e, o in body) + b"".join(cd_rec(e, o) for e, o in fmt) + cd_extra
    out += cd
    if comment is None:
        comment = magic + struct.pack("<QQ", s_body_off, len(sb))
        if idx_info:
            comment += struct.pack("<QQ", *idx_info)
    n = len(recs) + len(fmt)
    if zip64:
        z64off = len(out)
        out += struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0, n, n, len(cd), cd_off)
        out += struct.pack("<IIQI", 0x07064B50, 0, z64off, 1)
    if eocd_fields is None:
        eocd_fields = (0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF)
    out += struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, *eocd_fields, len(comment)) + comment
    return bytes(out)

