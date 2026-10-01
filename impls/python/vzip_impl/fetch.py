"""Reading byte ranges of url sources (spec §6, §6.1, §6.2)."""

from __future__ import annotations

import http.client
import os
import re
import ssl
import stat

from .errors import ResolutionError
from .proto import Source
from .uri import URI, FileURIError, file_uri_to_path, is_uri_reference, resolve, split_uri

MAX_REDIRECTS = 5
HTTP_TIMEOUT = 30

_ETAG_RE = re.compile(r'"[\x21\x23-\x7e]*"')


def is_strong_etag(s: str) -> bool:
    return bool(_ETAG_RE.fullmatch(s))


_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def imf_fixdate(t: int) -> str:
    """Format seconds since the epoch as an IMF-fixdate; ResolutionError outside years 1-9999."""
    import datetime
    try:
        dt = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=t)
    except OverflowError:
        raise ResolutionError(f"modified_not_after {t} is outside years 1-9999") from None
    return (f"{_DAYS[dt.weekday()]}, {dt.day:02d} {_MONTHS[dt.month - 1]} {dt.year:04d} "
            f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} GMT")


def read_url_source(base_uri: str, src: Source, start: int, end: int) -> bytes:
    """Return bytes [start, end) of the object named by a url source, checking all pins."""
    ref = src.value
    if not is_uri_reference(ref):
        raise ResolutionError(f"source url {ref!r} is not a valid URI reference")
    target = resolve(base_uri, ref)
    scheme = (target.scheme or "").lower()
    if scheme == "file":
        return _read_file(target, src, start, end)
    if scheme in ("http", "https"):
        return _read_http(target, src, start, end)
    raise ResolutionError(f"unsupported URL scheme {target.scheme!r}")


# ------------------------------------------------------------------------- file:


def _read_file(target: URI, src: Source, start: int, end: int) -> bytes:
    try:
        path = file_uri_to_path(target)
    except FileURIError as e:
        raise ResolutionError(str(e)) from None
    if src.etag is not None:
        raise ResolutionError("an etag pin cannot be checked for a file: URL")
    try:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise ResolutionError(f"{os.fsdecode(path)!r} is not a regular file")
            if src.size is not None and st.st_size != src.size:
                raise ResolutionError(f"size pin failed: file has {st.st_size} bytes, pin says {src.size}")
            if src.modified_not_after is not None:
                mtime = st.st_mtime_ns // 1_000_000_000  # floor, also for negative times
                if mtime > src.modified_not_after:
                    raise ResolutionError(
                        f"modified_not_after pin failed: mtime {mtime} > {src.modified_not_after}")
            if end > st.st_size:
                raise ResolutionError(f"file has {st.st_size} bytes, range needs {end}")
            f.seek(start)
            data = f.read(end - start)
    except OSError as e:
        raise ResolutionError(f"cannot read {os.fsdecode(path)!r}: {e}") from None
    if len(data) != end - start:
        raise ResolutionError("short read from file")
    return data


# ------------------------------------------------------------------------- http(s):

_CR_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)\s*$")


def _host_port(authority: str, scheme: str) -> tuple[str, int]:
    hostport = authority.rsplit("@", 1)[-1]
    default = 443 if scheme == "https" else 80
    if hostport.startswith("["):
        close = hostport.index("]")
        host = hostport[1:close]
        rest = hostport[close + 1:]
        port = int(rest[1:]) if rest.startswith(":") and rest[1:] else default
    elif ":" in hostport:
        host, p = hostport.split(":", 1)
        port = int(p) if p else default
    else:
        host, port = hostport, default
    return host, port


def _read_http(target: URI, src: Source, start: int, end: int) -> bytes:
    if src.modified_not_after is not None:
        ius = imf_fixdate(src.modified_not_after)
    else:
        ius = None
    url = target
    for _hop in range(MAX_REDIRECTS + 1):
        scheme = (url.scheme or "").lower()
        if scheme not in ("http", "https"):
            raise ResolutionError(f"redirect to unsupported scheme {url.scheme!r}")
        if not url.authority:
            raise ResolutionError("HTTP URL has no host")
        host, port = _host_port(url.authority, scheme)
        path = url.path or "/"
        if url.query is not None:
            path += "?" + url.query
        headers = {
            "Range": f"bytes={start}-{end - 1}",
            "Accept-Encoding": "identity",
        }
        if src.etag is not None:
            headers["If-Match"] = src.etag
        if ius is not None:
            headers["If-Unmodified-Since"] = ius
        try:
            if scheme == "https":
                conn = http.client.HTTPSConnection(host, port, timeout=HTTP_TIMEOUT,
                                                   context=ssl.create_default_context())
            else:
                conn = http.client.HTTPConnection(host, port, timeout=HTTP_TIMEOUT)
            try:
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                status = resp.status
                if status in (301, 302, 303, 307, 308):
                    loc = resp.getheader("Location")
                    resp.read()
                    if not loc:
                        raise ResolutionError(f"HTTP {status} without Location")
                    url = resolve(str(URI(url.scheme, url.authority, url.path, url.query, None)), loc)
                    continue
                return _handle_response(resp, src, start, end)
            finally:
                conn.close()
        except ResolutionError:
            raise
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise ResolutionError(f"HTTP request failed: {e}") from None
    raise ResolutionError("too many redirects")


def _handle_response(resp, src: Source, start: int, end: int) -> bytes:
    status = resp.status
    ce = resp.getheader("Content-Encoding")
    if ce is not None and ce.strip().lower() not in ("identity", ""):
        raise ResolutionError(f"unexpected Content-Encoding {ce!r}")
    if status == 412:
        raise ResolutionError("HTTP 412: a pin failed")
    if status == 416:
        raise ResolutionError("HTTP 416: object shorter than requested range")
    if status == 206:
        cr = resp.getheader("Content-Range")
        m = _CR_RE.fullmatch(cr.strip()) if cr else None
        if m is None:
            raise ResolutionError(f"206 response with unusable Content-Range {cr!r}")
        a, z, total = int(m.group(1)), int(m.group(2)), m.group(3)
        if a != start or z != end - 1:
            raise ResolutionError(f"server returned bytes {a}-{z}, requested {start}-{end - 1}")
        if src.size is not None:
            if total == "*":
                raise ResolutionError("size pin cannot be checked: server did not report the size")
            if int(total) != src.size:
                raise ResolutionError(f"size pin failed: object has {total} bytes, pin says {src.size}")
        body = resp.read()
        if len(body) != end - start:
            raise ResolutionError("206 body length does not match Content-Range")
        return body
    if status == 200:
        body = resp.read()
        if src.size is not None and len(body) != src.size:
            raise ResolutionError(f"size pin failed: object has {len(body)} bytes, pin says {src.size}")
        if len(body) < end:
            raise ResolutionError(f"object has {len(body)} bytes, range needs {end}")
        return body[start:end]
    raise ResolutionError(f"unexpected HTTP status {status}")
