"""Reading byte ranges of external objects named by URL (spec §6, §6.1, §6.2)."""

import datetime
import http.client
import os
import re
import stat

from . import uri
from .errors import ResolutionError

MAX_REDIRECTS = 5
HTTP_TIMEOUT = 30

_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_LONG_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def imf_fixdate(seconds):
    """Format integer epoch seconds as an IMF-fixdate; None if not representable."""
    try:
        dt = _EPOCH + datetime.timedelta(seconds=seconds)
    except OverflowError:
        return None
    return "%s, %02d %s %04d %02d:%02d:%02d GMT" % (
        _DAYS[dt.weekday()], dt.day, _MONTHS[dt.month - 1], dt.year,
        dt.hour, dt.minute, dt.second)


_MON = "(" + "|".join(_MONTHS) + ")"
_IMF_RE = re.compile(r"(?:%s), (\d\d) %s (\d{4}) (\d\d):(\d\d):(\d\d) GMT\Z"
                     % ("|".join(_DAYS), _MON))
_RFC850_RE = re.compile(r"(?:%s), (\d\d)-%s-(\d\d) (\d\d):(\d\d):(\d\d) GMT\Z"
                        % ("|".join(_LONG_DAYS), _MON))
_ASCTIME_RE = re.compile(r"(?:%s) %s ( \d|\d\d) (\d\d):(\d\d):(\d\d) (\d{4})\Z"
                         % ("|".join(_DAYS), _MON))


def parse_http_date(s, now_year=None):
    """Parse an HTTP-date (RFC 9110 §5.6.7) to epoch seconds, or None."""
    s = s.strip()
    m = _IMF_RE.match(s)
    if m:
        day, mon, year, hh, mm, ss = m.groups()
    else:
        m = _RFC850_RE.match(s)
        if m:
            day, mon, yy, hh, mm, ss = m.groups()
            if now_year is None:
                now_year = datetime.datetime.now(datetime.timezone.utc).year
            year = (now_year // 100) * 100 + int(yy)
            if year > now_year + 50:
                year -= 100
        else:
            m = _ASCTIME_RE.match(s)
            if not m:
                return None
            mon, day, hh, mm, ss, year = m.groups()
    try:
        dt = datetime.datetime(int(year), _MONTHS.index(mon) + 1, int(day), int(hh),
                               int(mm), int(ss), tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return int((dt - _EPOCH).total_seconds())


class Pins:
    def __init__(self, size=None, etag=None, modified_not_after=None):
        self.size = size
        self.etag = etag
        self.modified_not_after = modified_not_after


# ------------------------------------------------------------------ file:

def read_file(path_bytes, start, end, pins):
    try:
        fd = os.open(path_bytes, os.O_RDONLY)
    except OSError as e:
        raise ResolutionError(f"cannot open {os.fsdecode(path_bytes)}: {e.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ResolutionError(f"{os.fsdecode(path_bytes)} is not a regular file")
        if pins.etag is not None:
            raise ResolutionError("an etag pin cannot be checked for a file: URL")
        if pins.size is not None and st.st_size != pins.size:
            raise ResolutionError(f"size pin failed: file is {st.st_size} bytes, pin {pins.size}")
        if pins.modified_not_after is not None:
            mtime = st.st_mtime_ns // 1_000_000_000
            if mtime > pins.modified_not_after:
                raise ResolutionError(
                    f"modified_not_after pin failed: mtime {mtime} > {pins.modified_not_after}")
        if st.st_size < end:
            raise ResolutionError(f"source is {st.st_size} bytes, need {end}")
        data = os.pread(fd, end - start, start)
        if len(data) != end - start:
            raise ResolutionError("short read")
        return data
    except OSError as e:
        raise ResolutionError(f"read error: {e}") from None
    finally:
        os.close(fd)


# ------------------------------------------------------------------ http:

_CR_RE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)\Z", re.IGNORECASE)


def _http_once(u, headers):
    scheme = u.scheme.lower()
    _userinfo, host, port = uri.split_authority(u.authority or "")
    if host.startswith("["):
        host = host[1:-1]
    else:
        host = bytes(uri._pct_decode(host)).decode("utf-8", "replace")
    if not host:
        raise ResolutionError("HTTP URL has no host")
    port = int(port) if port else (443 if scheme == "https" else 80)
    target = u.path or "/"
    if u.query is not None:
        target += "?" + u.query
    if scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=HTTP_TIMEOUT)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=HTTP_TIMEOUT)
    try:
        conn.request("GET", target, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        hdrs = resp.headers
        body = resp.read() if status in (200, 206) else b""
        resp.close()
        return status, hdrs, body
    finally:
        conn.close()


def read_http(u, start, end, pins):
    """GET bytes [start, end) of the object at URIRef u (http/https)."""
    headers = {"Range": f"bytes={start}-{end - 1}", "Accept-Encoding": "identity"}
    if pins.etag is not None:
        headers["If-Match"] = pins.etag
    if pins.modified_not_after is not None:
        d = imf_fixdate(pins.modified_not_after)
        if d is None:
            raise ResolutionError("modified_not_after pin is outside years 1-9999")
        headers["If-Unmodified-Since"] = d
    redirects = 0
    while True:
        try:
            status, hdrs, body = _http_once(u, headers)
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise ResolutionError(f"HTTP request to {u} failed: {e}") from None
        if status in (301, 302, 303, 307, 308):
            redirects += 1
            if redirects > MAX_REDIRECTS:
                raise ResolutionError("too many redirects")
            loc = hdrs.get("Location")
            if loc is None:
                raise ResolutionError("redirect without Location")
            try:
                target = uri.resolve(u, uri.parse(loc.strip()))
            except uri.URIError as e:
                raise ResolutionError(f"invalid redirect Location: {e}") from None
            if target.scheme.lower() not in ("http", "https"):
                raise ResolutionError(f"redirect to unsupported scheme {target.scheme}")
            u = target
            continue
        break
    if status == 412:
        raise ResolutionError("HTTP 412: a pin failed")
    if status == 416:
        raise ResolutionError("HTTP 416: object shorter than the requested range")
    if status not in (200, 206):
        raise ResolutionError(f"HTTP status {status}")
    ce = hdrs.get_all("Content-Encoding") or []
    for v in ce:
        for tok in v.split(","):
            if tok.strip().lower() not in ("identity", ""):
                raise ResolutionError(f"Content-Encoding {v!r}")
    if status == 206:
        cr = hdrs.get("Content-Range")
        m = _CR_RE.match(cr.strip()) if cr else None
        if not m:
            raise ResolutionError(f"206 without a usable Content-Range: {cr!r}")
        first, last, total = int(m.group(1)), int(m.group(2)), m.group(3)
        if first != start or last != end - 1:
            raise ResolutionError(f"server returned range {first}-{last}, wanted {start}-{end - 1}")
        if len(body) != end - start:
            raise ResolutionError("206 body length does not match Content-Range")
        size = None if total == "*" else int(total)
        if size is not None and size <= last:
            raise ResolutionError("Content-Range total smaller than the returned range")
        data = body
    else:
        size = len(body)
        data = None
    # Pin checks against the final response
    if pins.size is not None:
        if size is None:
            raise ResolutionError("size pin cannot be checked: object size unknown")
        if size != pins.size:
            raise ResolutionError(f"size pin failed: object is {size} bytes, pin {pins.size}")
    if pins.etag is not None:
        et = hdrs.get("ETag")
        if et is None:
            raise ResolutionError("etag pin cannot be checked: no ETag header")
        if et.strip() != pins.etag:
            raise ResolutionError(f"etag pin failed: {et.strip()} != {pins.etag}")
    if pins.modified_not_after is not None:
        lm = hdrs.get("Last-Modified")
        t = parse_http_date(lm) if lm is not None else None
        if t is None:
            raise ResolutionError("modified_not_after pin cannot be checked: bad Last-Modified")
        if t > pins.modified_not_after:
            raise ResolutionError("modified_not_after pin failed")
    if data is None:
        if len(body) < end:
            raise ResolutionError(f"object is {len(body)} bytes, need {end}")
        data = body[start:end]
    return data


def read_url(resolved, start, end, pins):
    """Read [start, end) of the object named by a resolved URIRef."""
    scheme = (resolved.scheme or "").lower()
    if scheme == "file":
        try:
            path = uri.file_uri_to_path(resolved)
        except uri.URIError as e:
            raise ResolutionError(str(e)) from None
        return read_file(path, start, end, pins)
    if scheme in ("http", "https"):
        return read_http(resolved, start, end, pins)
    raise ResolutionError(f"unsupported URL scheme {resolved.scheme!r}")
