import json
import os
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CLI = os.path.join(ROOT, "vzip")
TMP_ROOT = os.path.join(ROOT, "tests", "_tmp")
os.makedirs(TMP_ROOT, exist_ok=True)

from vzip_impl import pb  # noqa: E402
from vzip_impl.cli import parse_description, run_query  # noqa: E402
from vzip_impl.reader import Archive  # noqa: E402
from vzip_impl.writer import build_archive  # noqa: E402


def tmpdir():
    return tempfile.TemporaryDirectory(dir=TMP_ROOT)


def write_desc(desc, path):
    entries, sources, page_size, mirror = parse_description(desc)
    data = build_archive(entries, sources, page_size, mirror)
    with open(path, "wb") as fh:
        fh.write(data)
    return data


def cli(*args, cwd=None):
    return subprocess.run([CLI, *args], capture_output=True, cwd=cwd)


def query(path, queries):
    """In-process equivalent of `vzip read`."""
    from vzip_impl.cli import parse_query
    from vzip_impl.errors import ArchiveError
    try:
        ar = Archive(path)
    except ArchiveError as e:
        return {"open": {"ok": False, "class": "archive", "error": str(e)}, "results": []}
    try:
        return {"open": {"ok": True},
                "results": [run_query(ar, *parse_query(q)) for q in queries]}
    finally:
        ar.close()


def q1(path, q):
    r = query(path, [q])
    assert r["open"]["ok"], r
    return r["results"][0]


def open_error(path):
    r = query(path, [])
    return None if r["open"]["ok"] else r["open"]


# ---------------------------------------------------------------- ZIP surgery

def _eocd(data):
    for clen in (38, 22):
        off = len(data) - 22 - clen
        if off >= 0 and struct.unpack_from("<I", data, off)[0] == 0x06054B50 and \
                struct.unpack_from("<H", data, off + 20)[0] == clen:
            return off
    raise ValueError("no eocd")


def split_cd(data):
    """Return (prefix, records(list of bytearray), eocd_off) for a non-zip64 archive."""
    e = _eocd(data)
    cd_size, cd_off = struct.unpack_from("<II", data, e + 12)
    recs = []
    pos = cd_off
    while pos < cd_off + cd_size:
        nlen, xlen, clen = struct.unpack_from("<HHH", data, pos + 28)
        end = pos + 46 + nlen + xlen + clen
        recs.append(bytearray(data[pos:end]))
        pos = end
    return data[:cd_off], recs, e


def rec_name(rec):
    nlen = struct.unpack_from("<H", rec, 28)[0]
    return bytes(rec[46:46 + nlen])


def rebuild(data, mutate):
    """Apply mutate(name, rec)->rec|None (None drops) to every CD record."""
    prefix, recs, e = split_cd(data)
    out = []
    for r in recs:
        nr = mutate(rec_name(r), r)
        if nr is not None:
            out.append(bytes(nr))
    cd = b"".join(out)
    eocd = bytearray(data[e:])
    struct.pack_into("<HHII", eocd, 8, len(out), len(out), len(cd), len(prefix))
    return prefix + cd + bytes(eocd)


def set_extra(rec, extra):
    nlen, xlen = struct.unpack_from("<HH", rec, 28)
    new = bytearray(rec[:46 + nlen]) + extra + rec[46 + nlen + xlen:]
    struct.pack_into("<H", new, 30, len(extra))
    return new


def get_extra(rec):
    nlen, xlen = struct.unpack_from("<HH", rec, 28)
    return bytes(rec[46 + nlen:46 + nlen + xlen])


def save(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


def dumps(o):
    return json.dumps(o)
