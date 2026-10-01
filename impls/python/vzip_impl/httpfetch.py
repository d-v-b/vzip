"""Byte-range reads over HTTP(S), spec §6.1 and §6.2."""

import calendar
import datetime
import http.client
import re
import socket
import ssl

from . import uri as urimod
from .errors import VzError

TIMEOUT = 30
MAX_REDIRECTS = 5

_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_IMF_RE = re.compile(
    r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun), ([0-9]{2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"([0-9]{4}) ([0-9]{2}):([0-9]{2}):([0-9]{2}) GMT", re.ASCII)
_CR_RE = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+|\*)", re.ASCII | re.IGNORECASE)


def res_err(msg):
    return VzError("resolution", msg)


def format_imf_fixdate(seconds):
    try:
        dt = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=seconds)
    except OverflowError:
        raise res_err("modified_not_after pin %d cannot be expressed as an HTTP-date" % seconds)
    return "%s, %02d %s %04d %02d:%02d:%02d GMT" % (
        _DAYS[dt.weekday()], dt.day, _MONTHS[dt.month - 1], dt.year, dt.hour, dt.minute, dt.second)


def parse_imf_fixdate(s):
    """Return whole seconds since the epoch, or None if s is not a valid IMF-fixdate."""
    m = _IMF_RE.fullmatch(s)
    if not m:
        return None
    dname, day, mon, year, hh, mm, ss = m.groups()
    day, year, hh, mm, ss = int(day), int(year), int(hh), int(mm), int(ss)
    if hh > 23 or mm > 59 or ss > 60:
        return None
    leap = 0
    if ss == 60:  # leap second (RFC 5322 style); treated as :59 + 1
        ss, leap = 59, 1
    try:
        dt = datetime.datetime(year, _MONTHS.index(mon) + 1, day, hh, mm, ss)
    except ValueError:
        return None
    if _DAYS[dt.weekday()] != dname:
        return None
    return calendar.timegm(dt.timetuple()) + leap


def _single_header(resp, name):
    vals = resp.msg.get_all(name)
    if not vals or len(vals) != 1:
        return None
    return vals[0].strip(" \t")


def _request(u, headers):
    scheme = u.scheme.lower()
    try:
        host, port = urimod.http_host_port(u)
    except urimod.HttpUrlError as e:
        raise res_err(str(e))
    target = u.path or "/"
    if u.query is not None:
        target += "?" + u.query
    conn = None
    try:
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=TIMEOUT,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
        conn.request("GET", target, headers=headers)
        resp = conn.getresponse()
    except (OSError, http.client.HTTPException, ValueError, UnicodeError) as e:
        if conn is not None:
            conn.close()
        raise res_err("HTTP request to %s failed: %s" % (u, e))
    return conn, resp


def _read_body(conn, resp, limit):
    try:
        cl = resp.getheader("Content-Length")
        if cl is not None and cl.strip().isdigit() and int(cl) > limit:
            raise VzError("request", "response body of %s bytes exceeds the resource limit" % cl)
        body = resp.read(limit + 1)
        if len(body) > limit:
            raise VzError("request", "response body exceeds the resource limit")
        return body
    except (OSError, http.client.HTTPException) as e:
        raise res_err("reading HTTP body failed: %s" % e)
    finally:
        conn.close()


def http_read(target, start, end, size_pin, etag_pin, mnf_pin, limit):
    """Read bytes [start, end) of the object at target (a resolved URI), checking pins."""
    conns = []
    try:
        return _http_read(target, start, end, size_pin, etag_pin, mnf_pin, limit, conns)
    finally:
        for c in conns:
            c.close()


def _http_read(target, start, end, size_pin, etag_pin, mnf_pin, limit, conns):
    headers = {"Range": "bytes=%d-%d" % (start, end - 1), "Accept-Encoding": "identity"}
    if etag_pin is not None:
        headers["If-Match"] = etag_pin
    if mnf_pin is not None:
        headers["If-Unmodified-Since"] = format_imf_fixdate(mnf_pin)
    cur = target
    for hop in range(MAX_REDIRECTS + 1):
        conn, resp = _request(cur, headers)
        conns.append(conn)
        status = resp.status
        if status in (301, 302, 303, 307, 308):
            loc = _single_header(resp, "Location")
            conn.close()
            if hop == MAX_REDIRECTS:
                raise res_err("too many redirects")
            if loc is None:
                raise res_err("redirect without a (single) Location header")
            if not urimod.is_uri_reference(loc):
                raise res_err("redirect Location is not a valid URI reference: %r" % loc)
            nxt = urimod.resolve(cur, loc)
            if nxt.scheme.lower() not in ("http", "https"):
                raise res_err("redirect to non-HTTP scheme %r" % nxt.scheme)
            cur = nxt
            continue
        break
    if status not in (200, 206):
        conn.close()
        if status == 412:
            raise res_err("precondition failed (pin mismatch): HTTP 412")
        if status == 416:
            raise res_err("object is shorter than the requested range: HTTP 416")
        raise res_err("unexpected HTTP status %d" % status)

    ces = resp.msg.get_all("Content-Encoding")
    if ces:
        joined = ", ".join(ces)
        if joined.strip(" \t").lower() != "identity":
            conn.close()
            raise res_err("response has Content-Encoding %r" % joined)

    if status == 200:
        body = _read_body(conn, resp, limit)
        total = len(body)
        if total < end:
            raise res_err("object (%d bytes) is shorter than the requested range end %d" % (total, end))
        data = body[start:end]
    else:
        ctype = resp.getheader("Content-Type") or ""
        if ctype.strip().lower().startswith("multipart/byteranges"):
            conn.close()
            raise res_err("multipart/byteranges response")
        cr = _single_header(resp, "Content-Range")
        m = _CR_RE.fullmatch(cr) if cr is not None else None
        if m is None:
            conn.close()
            raise res_err("206 response without exactly one valid Content-Range")
        a, z = int(m.group(1)), int(m.group(2))
        total = None if m.group(3) == "*" else int(m.group(3))
        if a != start or z != end - 1:
            conn.close()
            raise res_err("206 returned range %d-%d, requested %d-%d" % (a, z, start, end - 1))
        if total is not None and z >= total:
            conn.close()
            raise res_err("Content-Range last byte is not less than total")
        data = _read_body(conn, resp, limit)
        if len(data) != z - a + 1:
            raise res_err("206 body length %d does not match Content-Range" % len(data))

    if size_pin is not None:
        if total is None:
            raise res_err("size pin cannot be checked: object size unknown")
        if total != size_pin:
            raise res_err("size pin failed: object has %d bytes, pin says %d" % (total, size_pin))
    if etag_pin is not None:
        et = _single_header(resp, "ETag")
        if et is None:
            raise res_err("etag pin cannot be checked: no ETag header")
        if et.startswith("W/") or et != etag_pin:
            raise res_err("etag pin failed: %r != %r" % (et, etag_pin))
    if mnf_pin is not None:
        lm = _single_header(resp, "Last-Modified")
        t = parse_imf_fixdate(lm) if lm is not None else None
        if t is None:
            raise res_err("modified_not_after pin cannot be checked: missing or invalid Last-Modified")
        if t > mnf_pin:
            raise res_err("modified_not_after pin failed: Last-Modified %s is after the pin" % lm)
    return data
