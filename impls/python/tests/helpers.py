"""Test helpers: run the CLI, craft and tamper with archives."""

import json
import os
import struct
import subprocess
import sys
import tempfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vzip_impl import proto  # noqa: E402

CLI = os.path.join(ROOT, "vzip")


def run_cli(*args, cwd=None):
    return subprocess.run([CLI, *args], capture_output=True, text=True, cwd=cwd)


class TmpDir:
    def __init__(self):
        base = os.path.join(ROOT, "tests", "_tmp")
        os.makedirs(base, exist_ok=True)
        self.td = tempfile.TemporaryDirectory(prefix="vzt-", dir=base)
        self.path = self.td.name

    def p(self, *parts):
        return os.path.join(self.path, *parts)

    def write(self, name, data):
        path = self.p(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "w" if isinstance(data, str) else "wb"
        with open(path, mode) as f:
            f.write(data)
        return path

    def cleanup(self):
        self.td.cleanup()


def deflate(data):
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


def cd_record(name, method=0, crc=0, csize=0, usize=0, lho=0, extra=b"", flags=0x800,
              comment=b""):
    return struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, flags, method, 0, 0x21,
                       crc, csize, usize, len(name), len(extra), len(comment), 0, 0, 0,
                       lho) + name + extra + comment


def local_header(name, method, crc, csize, usize, flags=0x800):
    return struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flags, method, 0, 0x21, crc, csize,
                       usize, len(name), 0) + name


def build_raw(entries, sources_raw=b"", sources_body=None, cd_extra_records=b"",
              magic=b"vzip/0", eocd_override=None, index_builder=None, cd_order=None):
    """Build an archive with full control over records.

    entries: list of dicts with name (bytes), body (bytes), method, usize, crc,
    flags, extra (CD extra), csize (override recorded csize).
    index_builder: f(records_info, cd_bytes_of_body_records) -> index raw bytes;
    when given, the archive is paged.
    """
    out = bytearray()
    recs = []
    for e in entries:
        name = e["name"]
        body = e.get("body", b"")
        method = e.get("method", 0)
        usize = e.get("usize", len(body))
        crc = e.get("crc", zlib.crc32(body) if method == 0 else 0)
        csize = e.get("csize", len(body))
        lho = len(out)
        out += local_header(name, method, crc, csize, usize, e.get("flags", 0x800))
        body_off = len(out)
        out += body
        rec = cd_record(name, method, crc, csize, usize, e.get("lho", lho), e.get("extra", b""),
                        e.get("flags", 0x800))
        recs.append((name, rec, body_off, usize, csize, method))
    if sources_body is None:
        sources_body = deflate(sources_raw)
    s_lho = len(out)
    sname = b"__vz__/sources"
    out += local_header(sname, 8, zlib.crc32(sources_raw), len(sources_body), len(sources_raw))
    s_off = len(out)
    out += sources_body
    s_rec = cd_record(sname, 8, zlib.crc32(sources_raw), len(sources_body), len(sources_raw),
                      s_lho)
    comment = magic + struct.pack("<QQ", s_off, len(sources_body))
    if cd_order is not None:
        recs = cd_order(recs)
    body_cd = b"".join(r[1] for r in recs) + cd_extra_records
    fmt = [s_rec]
    if index_builder is not None:
        iraw = index_builder(recs, body_cd)
        ibody = deflate(iraw)
        i_lho = len(out)
        iname = b"__vz__/index"
        out += local_header(iname, 8, zlib.crc32(iraw), len(ibody), len(iraw))
        i_off = len(out)
        out += ibody
        fmt.append(cd_record(iname, 8, zlib.crc32(iraw), len(ibody), len(iraw), i_lho))
        comment += struct.pack("<QQ", i_off, len(ibody))
    cd_off = len(out)
    out += body_cd + b"".join(fmt)
    cd_size = len(out) - cd_off
    n = len(recs) + len(fmt)
    fields = dict(n=n, cd_size=cd_size, cd_off=cd_off)
    if eocd_override:
        fields.update(eocd_override)
    out += struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, fields["n"], fields["n"],
                       fields["cd_size"], fields["cd_off"], len(comment)) + comment
    return bytes(out)


def simple_pages(recs, body_cd, per_page=1):
    """index builder: per_page records per page, no pinned entries."""
    pages = []
    off = 0
    for i in range(0, len(recs), per_page):
        chunk = recs[i:i + per_page]
        ln = sum(len(r[1]) for r in chunk)
        pages.append((chunk[0][0], off, ln))
        off += ln
    return proto.encode_cd_index(pages, [])


def ref_extra(payload, single=True):
    hid = 0x7A76 if single else 0x7A77
    return struct.pack("<HH", hid, len(payload)) + payload


def write_desc(td, desc, name="out.vzip"):
    dp = td.write("desc.json", json.dumps(desc))
    out = td.p(name)
    r = run_cli("write", dp, out)
    return r, out


def read_queries(td, archive, queries):
    qp = td.write("q.json", json.dumps(queries))
    r = run_cli("read", archive, qp)
    if r.returncode != 0:
        raise AssertionError(f"read failed: {r.stderr}")
    return json.loads(r.stdout)
