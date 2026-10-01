"""Test helpers: a low-level archive crafter for invalid archives, and a local HTTP server."""

from __future__ import annotations

import email.utils
import os
import struct
import sys
import threading
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vzip_impl import proto  # noqa: E402


def deflate(b: bytes) -> bytes:
    c = zlib.compressobj(6, zlib.DEFLATED, -15)
    return c.compress(b) + c.flush()


def range_extra(r: proto.Range) -> bytes:
    p = proto.encode_range(r)
    return struct.pack("<HH", 0x7A76, len(p)) + p


def concat_extra(parts) -> bytes:
    p = proto.encode_concat(parts)
    return struct.pack("<HH", 0x7A77, len(p)) + p


def craft(records, sources=None, sources_raw=None, sources_body=None, index_fn=None,
          comment=None, cd_suffix=b"", eocd_override=None, index_body=None):
    """Build a (possibly invalid) archive.

    records: list of dicts with keys name(bytes|str), body(bytes), method(0), flags(0x800),
             extra(b""), usize/csize/loff (override), crc.
    sources: list[proto.Source] (or sources_raw: encoded table, sources_body: raw deflated body).
    index_fn: callable(list of (name, cd_off, length)) -> CdIndex bytes; makes a paged archive.
    """
    buf = bytearray()
    cd_infos = []

    def local(name, method, flags, crc, csize, usize, body):
        off = len(buf)
        buf.extend(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flags, method, 0, 0x21, crc, csize,
                               usize, len(name), 0))
        buf.extend(name)
        buf.extend(body)
        return off

    def cdrec(name, method, flags, crc, csize, usize, loff, extra):
        return (struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, flags, method, 0, 0x21, crc, csize,
                            usize, len(name), len(extra), 0, 0, 0, 0, loff) + name + extra)

    recs = []
    for r in records:
        name = r["name"].encode() if isinstance(r["name"], str) else r["name"]
        body = r.get("body", b"")
        method = r.get("method", 0)
        flags = r.get("flags", 0x800)
        raw_len = r.get("raw_len", len(body))
        crc = r.get("crc", zlib.crc32(body))
        off = local(name, method, flags, crc, len(body), raw_len, body)
        recs.append(cdrec(name, method, flags, crc, r.get("csize", len(body)), r.get("usize", raw_len),
                          r.get("loff", off), r.get("extra", b"")))
    if sources_body is None:
        if sources_raw is None:
            sources_raw = proto.encode_source_table(sources or [])
        sources_body = deflate(sources_raw)
    s_off = local(b"__vz__/sources", 8, 0x800, 0, len(sources_body), 0, sources_body)
    s_body_off = s_off + 30 + len(b"__vz__/sources")
    fmt_recs = [cdrec(b"__vz__/sources", 8, 0x800, 0, len(sources_body), 0, s_off, b"")]

    # sort body records for paged archives
    if index_fn is not None:
        recs.sort(key=lambda rec: rec[46:46 + struct.unpack_from("<H", rec, 28)[0]])
    pos = 0
    for rec in recs:
        nlen = struct.unpack_from("<H", rec, 28)[0]
        cd_infos.append((rec[46:46 + nlen], pos, len(rec)))
        pos += len(rec)
    i_body_off = None
    if index_fn is not None or index_body is not None:
        if index_body is None:
            index_body = deflate(index_fn(cd_infos))
        i_off = local(b"__vz__/index", 8, 0x800, 0, len(index_body), 0, index_body)
        i_body_off = i_off + 30 + len(b"__vz__/index")
        fmt_recs.append(cdrec(b"__vz__/index", 8, 0x800, 0, len(index_body), 0, i_off, b""))
    cd_off = len(buf)
    for rec in recs + fmt_recs:
        buf.extend(rec)
    buf.extend(cd_suffix)
    cd_size = len(buf) - cd_off
    if comment is None:
        comment = b"vzip/0" + struct.pack("<QQ", s_body_off, len(sources_body))
        if i_body_off is not None:
            comment += struct.pack("<QQ", i_body_off, len(index_body))
    n = len(recs) + len(fmt_recs)
    fields = [0x06054B50, 0, 0, n, n, cd_size, cd_off, len(comment)]
    if eocd_override:
        for k, v in eocd_override.items():
            fields[k] = v
    buf.extend(struct.pack("<IHHHHIIH", *fields))
    buf.extend(comment)
    return bytes(buf)


def simple_index(page_size=1, pinned=()):
    """index_fn: one page per `page_size` records, plus given Pinned entries."""
    def fn(infos):
        idx = proto.CdIndex()
        for i in range(0, len(infos), page_size):
            grp = infos[i:i + page_size]
            idx.pages.append(proto.Page(grp[0][0].decode(), grp[0][1], sum(g[2] for g in grp)))
        idx.pinned.extend(pinned)
        return proto.encode_cd_index(idx)
    return fn


# ------------------------------------------------------------------------ HTTP server


class ObjectServer:
    """A small HTTP/1.1 server serving byte objects with Range, ETag and conditional support.

    Path prefixes select behaviour:
      /plain/<name>       normal 206 responses
      /full/<name>        ignores Range, answers 200 with the whole object
      /star/<name>        206 with Content-Range total '*'
      /gzip/<name>        206 with Content-Encoding: gzip
      /clamp/<name>       clamps the range end to the object (like real servers)
      /redir<N>/<rest>    redirects N times, then serves /<rest>
      /status<NNN>/<name> always answers with that status
    """

    def __init__(self):
        self.objects: dict[str, tuple[bytes, str | None, int | None]] = {}
        self.log: list[tuple[str, str, dict]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_HEAD(self):
                outer.log.append(("HEAD", self.path, dict(self.headers)))
                self.send_response(405)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                outer.log.append(("GET", self.path, dict(self.headers)))
                parts = self.path.lstrip("/").split("/", 1)
                mode, name = parts[0], parts[1] if len(parts) > 1 else ""
                if mode.startswith("redir"):
                    n = int(mode[5:])
                    loc = f"/redir{n - 1}/{name}" if n > 1 else f"/{name}"
                    self._send(302, b"", {"Location": loc})
                    return
                if mode.startswith("status"):
                    self._send(int(mode[6:]), b"")
                    return
                obj = outer.objects.get(urllib.parse.unquote(name))
                if obj is None:
                    self._send(404, b"not found")
                    return
                data, etag, mtime = obj
                hdrs = {}
                if etag:
                    hdrs["ETag"] = etag
                if mtime is not None:
                    hdrs["Last-Modified"] = email.utils.formatdate(mtime, usegmt=True)
                im = self.headers.get("If-Match")
                if im is not None and (etag is None or etag.startswith("W/") or
                                       im.strip() != "*" and etag not in [t.strip() for t in im.split(",")]):
                    self._send(412, b"")
                    return
                ius = self.headers.get("If-Unmodified-Since")
                if ius is not None and mtime is not None:
                    t = email.utils.parsedate_to_datetime(ius).timestamp()
                    if mtime > t:
                        self._send(412, b"")
                        return
                rng = self.headers.get("Range")
                if mode == "full" or rng is None:
                    self._send(200, data, hdrs)
                    return
                spec = rng.split("=", 1)[1]
                a, b = spec.split("-")
                a, b = int(a), int(b)
                if a >= len(data):
                    hdrs["Content-Range"] = f"bytes */{len(data)}"
                    self._send(416, b"", hdrs)
                    return
                if mode == "clamp":
                    b = min(b, len(data) - 1)
                elif b >= len(data):
                    b = len(data) - 1
                total = "*" if mode == "star" else str(len(data))
                hdrs["Content-Range"] = f"bytes {a}-{b}/{total}"
                if mode == "gzip":
                    hdrs["Content-Encoding"] = "gzip"
                self._send(206, data[a:b + 1], hdrs)

            def _send(self, status, body, hdrs=None):
                self.send_response(status)
                for k, v in (hdrs or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}/{path}"
