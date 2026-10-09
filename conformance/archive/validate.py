"""Structural validation of a written archive against spec/archive.md and its description.

Has its own ZIP parser and uses the official protobuf runtime (pbref), so it
shares no code with any implementation. Returns a list of problems (empty =
valid).
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pbref

U16, U32 = 0xFFFF, 0xFFFFFFFF
RANGE_ID, CONCAT_ID, ZIP64_ID = 0x7A76, 0x7A77, 0x0001
SOURCES, INDEX = "__vz__/sources", "__vz__/index"


def _extra_blocks(extra: bytes, problems: list, name: str) -> list[tuple[int, bytes]]:
    out, pos = [], 0
    while pos < len(extra):
        if pos + 4 > len(extra):
            problems.append(f"{name}: truncated extra field")
            break
        hid, n = struct.unpack_from("<HH", extra, pos)
        if pos + 4 + n > len(extra):
            problems.append(f"{name}: extra block overruns extra field")
            break
        out.append((hid, extra[pos + 4 : pos + 4 + n]))
        pos += 4 + n
    return out


def parse(buf: bytes, problems: list) -> dict:
    """Parse end records + central directory. Returns a dict describing the archive."""
    i = buf.rfind(b"PK\x05\x06")
    while i >= 0 and i + 22 + struct.unpack_from("<H", buf, i + 20)[0] != len(buf):
        i = buf.rfind(b"PK\x05\x06", 0, i)
    if i < 0:
        raise ValueError("no end of central directory record")
    (_, disk, cd_disk, n_disk, n, cd_size, cd_off, clen) = struct.unpack_from("<IHHHHIIH", buf, i)
    comment = buf[i + 22 : i + 22 + clen]
    zip64 = False
    if n == U16 or cd_size == U32 or cd_off == U32:
        zip64 = True
        loc = i - 20
        sig, _, e64, _ = struct.unpack_from("<IIQI", buf, loc)
        if sig != 0x07064B50:
            raise ValueError("bad zip64 locator")
        (sig, _, _, _, _, _, n, _, cd_size, cd_off) = struct.unpack_from("<IQHHIIQQQQ", buf, e64)
        if sig != 0x06064B50:
            raise ValueError("bad zip64 EOCD")
        cd_end_expected = e64
    else:
        cd_end_expected = i
        if buf.rfind(b"PK\x06\x07", 0, i) == i - 20:
            zip64 = True
    if cd_off + cd_size != cd_end_expected:
        problems.append("central directory does not end where the end records begin")
    recs, pos, any_zip64_extra = [], cd_off, False
    for _ in range(n):
        (sig, made, need, flags, method, _, _, crc, csize, size, nlen, xlen, cmlen, _, _, _, off) = (
            struct.unpack_from("<IHHHHHHIIIHHHHHII", buf, pos)
        )
        if sig != 0x02014B50:
            raise ValueError(f"bad CD record at {pos}")
        raw_name = buf[pos + 46 : pos + 46 + nlen]
        extra = buf[pos + 46 + nlen : pos + 46 + nlen + xlen]
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"non-UTF-8 name {raw_name!r}")
            name = raw_name.decode("utf-8", "replace")
        blocks = _extra_blocks(extra, problems, name)
        for hid, data in blocks:
            if hid == ZIP64_ID:
                any_zip64_extra = True
                vals = list(struct.unpack_from(f"<{len(data) // 8}Q", data))
                if size == U32:
                    size = vals.pop(0)
                if csize == U32:
                    csize = vals.pop(0)
                if off == U32:
                    off = vals.pop(0)
        recs.append({"name": name, "flags": flags, "method": method, "crc": crc, "csize": csize,
                     "size": size, "off": off, "blocks": blocks, "rec_off": pos - cd_off,
                     "rec_len": 46 + nlen + xlen + cmlen, "comment_len": cmlen})
        pos += 46 + nlen + xlen + cmlen
    if pos != cd_off + cd_size:
        problems.append("central directory size does not match its records")
    return {"comment": comment, "recs": recs, "cd_off": cd_off, "cd_size": cd_size, "n": n,
            "zip64": zip64, "any_zip64_extra": any_zip64_extra}


def validate(path: Path, desc: dict) -> list[str]:
    problems: list[str] = []
    buf = path.read_bytes()
    try:
        z = parse(buf, problems)
    except Exception as e:  # noqa: BLE001
        return [f"unparseable: {e}"]

    need64 = z["n"] >= U16 or z["cd_off"] >= U32 or z["cd_size"] >= U32 or any(
        r["off"] >= U32 or r["size"] >= U32 for r in z["recs"])
    if z["zip64"] != need64:
        problems.append(f"zip64 end records {'present' if z['zip64'] else 'absent'} but "
                        f"{'needed' if need64 else 'not needed'}")
    if z["any_zip64_extra"] and not any(r["off"] >= U32 or r["size"] >= U32 for r in z["recs"]):
        problems.append("zip64 extra field used where not needed")

    by_name: dict[str, dict] = {}
    for r in z["recs"]:
        if r["name"] in by_name:
            problems.append(f"duplicate record {r['name']!r}")
        by_name[r["name"]] = r

    bodies: dict[str, bytes] = {}
    for r in z["recs"]:
        nm = r["name"]
        if r["method"] not in (0, 8):
            problems.append(f"{nm}: method {r['method']}")
        if not r["flags"] & 0x0800:
            problems.append(f"{nm}: CD bit 11 (UTF-8) not set")
        if r["flags"] & 0x0009:
            problems.append(f"{nm}: encryption or data-descriptor bit set")
        o = r["off"]
        sig, _, lflags, lmethod, _, _, lcrc, lcsize, lsize, lnlen, lxlen = struct.unpack_from(
            "<IHHHHHIIIHH", buf, o)
        if sig != 0x04034B50:
            problems.append(f"{nm}: bad local header")
            continue
        if lxlen != 0:
            problems.append(f"{nm}: local extra field length {lxlen} (must be 0)")
        if buf[o + 30 : o + 30 + lnlen] != nm.encode():
            problems.append(f"{nm}: local header name differs")
        if not lflags & 0x0800:
            problems.append(f"{nm}: local bit 11 (UTF-8) not set")
        if lmethod != r["method"]:
            problems.append(f"{nm}: local method differs")
        start = o + 30 + lnlen + lxlen
        stored = buf[start : start + r["csize"]]
        try:
            body = zlib.decompress(stored, -15) if r["method"] == 8 else stored
        except zlib.error as e:
            problems.append(f"{nm}: bad DEFLATE data: {e}")
            continue
        if len(body) != r["size"]:
            problems.append(f"{nm}: size {len(body)} != declared {r['size']}")
        if zlib.crc32(body) != r["crc"] or lcrc != r["crc"]:
            problems.append(f"{nm}: CRC-32 mismatch")
        if lsize not in (r["size"], U32) or lcsize not in (r["csize"], U32):
            problems.append(f"{nm}: local sizes differ from central directory")
        bodies[nm] = body

    # ---- comment and source table
    c = z["comment"]
    paged = desc.get("page_size") is not None
    if not c.startswith(b"vzip/0") or len(c) not in (22, 38):
        problems.append(f"bad archive comment {c!r}")
        return problems
    if (len(c) == 38) != paged:
        problems.append(f"comment length {len(c)} but page_size={desc.get('page_size')}")
    soff, ssize = struct.unpack_from("<QQ", c, 6)
    sr = by_name.get(SOURCES)
    if sr is None:
        problems.append("no __vz__/sources entry")
    else:
        if sr["method"] != 8:
            problems.append("__vz__/sources is not DEFLATE")
        if soff != sr["off"] + 30 + len(SOURCES) or ssize != sr["csize"]:
            problems.append("comment does not locate the __vz__/sources body")
        # spec/archive.md §6: the revision is recorded by the writer and only reported by readers,
        # so the description may leave it to the table; the encoding must still be canonical
        revision = desc.get("revision")
        if revision is None and SOURCES in bodies:
            try:
                table = pbref.SourceTable.FromString(bodies[SOURCES])
                revision = table.revision if table.HasField("revision") else None
            except Exception:  # noqa: BLE001 - the comparison below reports the body
                pass
        want = pbref.source_table(desc.get("sources", []), revision)
        if bodies.get(SOURCES) != want:
            problems.append(f"source table body {bodies.get(SOURCES, b'').hex()} != {want.hex()}")

    # ---- entries vs description
    want_keys = {e["key"] for e in desc["entries"]} | {SOURCES} | ({INDEX} if paged else set())
    if set(by_name) != want_keys:
        problems.append(f"entry set differs: extra {sorted(set(by_name) - want_keys)[:5]}, "
                        f"missing {sorted(want_keys - set(by_name))[:5]}")
    for e in desc["entries"]:
        r = by_name.get(e["key"])
        if r is None:
            continue
        ids = [h for h, _ in r["blocks"] if h in (RANGE_ID, CONCAT_ID)]
        if "ranges" in e:
            hid, payload = pbref.payload(e["ranges"])
            got = [d for h, d in r["blocks"] if h in (RANGE_ID, CONCAT_ID)]
            if ids != [hid]:
                problems.append(f"{e['key']}: reference blocks {ids}, expected [{hex(hid)}]")
            elif got[0] != payload:
                problems.append(f"{e['key']}: payload {got[0].hex()} != canonical {payload.hex()}")
            if r["method"] != 0:
                problems.append(f"{e['key']}: reference entry not STORED")
            body = bodies.get(e["key"])
            want_body = payload if desc.get("mirror", True) else b""
            if body is not None and body != want_body:
                problems.append(f"{e['key']}: body {body.hex()} != {want_body.hex()} (mirror="
                                f"{desc.get('mirror', True)})")
        else:
            if ids:
                problems.append(f"{e['key']}: bytes entry carries a reference block")
            if bodies.get(e["key"]) != bytes.fromhex(e["bytes"]):
                problems.append(f"{e['key']}: wrong bytes")
            want_method = 8 if e.get("compress") else 0
            if r["method"] != want_method:
                problems.append(f"{e['key']}: method {r['method']}, compress={e.get('compress')}")

    # ---- page index
    if paged and len(c) == 38:
        problems += _check_index(z, by_name, bodies, c, desc)
    elif INDEX in by_name:
        problems.append("__vz__/index present without page_size")

    # ---- third-party tool
    # Debian's and Ubuntu's unzip convert entry names through a code page guessed from
    # the locale, and then report UTF-8 names as "mismatching local filename"; `-O UTF-8`
    # under a UTF-8 locale makes that conversion the identity (other builds lack -O)
    cmd, env = ["unzip", "-tq", str(path)], None
    if sys.platform.startswith("linux"):
        cmd, env = ["unzip", "-O", "UTF-8", "-tq", str(path)], {**os.environ, "LC_ALL": "C.UTF-8"}
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if res.returncode != 0:
        problems.append(f"unzip -t failed: {(res.stdout + res.stderr).strip()[:200]}")
    return problems


def _check_index(z, by_name, bodies, c, desc) -> list[str]:
    p: list[str] = []
    ioff, isize = struct.unpack_from("<QQ", c, 22)
    ir = by_name.get(INDEX)
    if ir is None:
        return ["paged archive without __vz__/index"]
    if ir["method"] != 8:
        p.append("__vz__/index is not DEFLATE")
    if ioff != ir["off"] + 30 + len(INDEX) or isize != ir["csize"]:
        p.append("comment does not locate the __vz__/index body")
    idx = pbref.CdIndex()
    try:
        idx.ParseFromString(bodies[INDEX])
    except Exception as e:  # noqa: BLE001
        return p + [f"index does not decode: {e}"]
    if idx.SerializeToString(deterministic=True) != bodies[INDEX]:
        p.append("index encoding is not canonical")
    recs = z["recs"]
    body = [r for r in recs if r["name"] not in (SOURCES, INDEX)]
    trailer = [r for r in recs if r["name"] in (SOURCES, INDEX)]
    if recs != body + trailer:
        p.append("trailer records are not last")
    names = [r["name"].encode() for r in body]
    if names != sorted(names):
        p.append("body records are not sorted by UTF-8 bytes")
    if any(r["comment_len"] for r in recs):
        p.append("record comments present (pages must hold whole records)")
    starts = {r["rec_off"]: k for k, r in enumerate(body)}
    end = body[-1]["rec_off"] + body[-1]["rec_len"] if body else 0
    pos, k = 0, 0
    if not body and len(idx.pages):
        p.append("pages present but no body records")
    for pg in idx.pages:
        if pg.offset != pos:
            p.append(f"page at {pg.offset} does not start where previous ended ({pos})")
            break
        if pg.offset not in starts:
            p.append(f"page offset {pg.offset} is not a record boundary")
            break
        first = body[starts[pg.offset]]
        if pg.first_key != first["name"]:
            p.append(f"page first_key {pg.first_key!r} != {first['name']!r}")
        if pg.length == 0:
            p.append("empty page")
        pos = pg.offset + pg.length
        k += 1
    if body and pos != end:
        p.append(f"pages end at {pos}, body records end at {end}")
    if pos not in starts and pos != end:
        p.append("a page does not end on a record boundary")
    want_pinned = {e["key"] for e in desc["entries"] if e.get("pinned")}
    got_pinned = {pn.key for pn in idx.pinned}
    if want_pinned != got_pinned:
        p.append(f"pinned {sorted(got_pinned)} != {sorted(want_pinned)}")
    for pn in idx.pinned:
        r = by_name.get(pn.key)
        if r is None:
            continue
        if (pn.data_offset, pn.size, pn.csize, pn.method) != (
                r["off"] + 30 + len(pn.key.encode()), r["size"], r["csize"], r["method"]):
            p.append(f"pinned {pn.key!r} does not match its record")
    return p
