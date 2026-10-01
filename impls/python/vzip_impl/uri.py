"""RFC 3986 URI-reference validation and resolution, and file: URI mapping."""

import os
import re
from urllib.parse import unquote_to_bytes

_UNRES = r"A-Za-z0-9\-._~"
_SUB = r"!$&'()*+,;="
_PCT = r"%[0-9A-Fa-f]{2}"
_PCHAR = rf"(?:[{_UNRES}{_SUB}:@]|{_PCT})"
_SEGMENT = rf"{_PCHAR}*"
_SEGMENT_NZ = rf"{_PCHAR}+"
_SEGMENT_NZ_NC = rf"(?:[{_UNRES}{_SUB}@]|{_PCT})+"
_QUERY = rf"(?:{_PCHAR}|[/?])*"
_SCHEME = r"[A-Za-z][A-Za-z0-9+\-.]*"
_USERINFO = rf"(?:[{_UNRES}{_SUB}:]|{_PCT})*"
_REGNAME = rf"(?:[{_UNRES}{_SUB}]|{_PCT})*"
_H16 = r"[0-9A-Fa-f]{1,4}"
_DEC = r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])"
_IPV4 = rf"{_DEC}\.{_DEC}\.{_DEC}\.{_DEC}"
_LS32 = rf"(?:{_H16}:{_H16}|{_IPV4})"
_IPV6 = "(?:" + "|".join([
    rf"(?:{_H16}:){{6}}{_LS32}",
    rf"::(?:{_H16}:){{5}}{_LS32}",
    rf"(?:{_H16})?::(?:{_H16}:){{4}}{_LS32}",
    rf"(?:(?:{_H16}:){{0,1}}{_H16})?::(?:{_H16}:){{3}}{_LS32}",
    rf"(?:(?:{_H16}:){{0,2}}{_H16})?::(?:{_H16}:){{2}}{_LS32}",
    rf"(?:(?:{_H16}:){{0,3}}{_H16})?::{_H16}:{_LS32}",
    rf"(?:(?:{_H16}:){{0,4}}{_H16})?::{_LS32}",
    rf"(?:(?:{_H16}:){{0,5}}{_H16})?::{_H16}",
    rf"(?:(?:{_H16}:){{0,6}}{_H16})?::",
]) + ")"
_IPVFUTURE = rf"v[0-9A-Fa-f]+\.[{_UNRES}{_SUB}:]+"
_HOST = rf"(?:\[(?:{_IPV6}|{_IPVFUTURE})\]|{_REGNAME})"
_AUTHORITY = rf"(?:{_USERINFO}@)?{_HOST}(?::[0-9]*)?"
_PATH_ABEMPTY = rf"(?:/{_SEGMENT})*"
_PATH_ABSOLUTE = rf"/(?:{_SEGMENT_NZ}(?:/{_SEGMENT})*)?"
_PATH_NOSCHEME = rf"{_SEGMENT_NZ_NC}(?:/{_SEGMENT})*"
_PATH_ROOTLESS = rf"{_SEGMENT_NZ}(?:/{_SEGMENT})*"
_TAIL = rf"(?:\?{_QUERY})?(?:#{_QUERY})?"
_URI = rf"{_SCHEME}:(?://{_AUTHORITY}{_PATH_ABEMPTY}|{_PATH_ABSOLUTE}|{_PATH_ROOTLESS}|){_TAIL}"
_RELREF = rf"(?://{_AUTHORITY}{_PATH_ABEMPTY}|{_PATH_ABSOLUTE}|{_PATH_NOSCHEME}|){_TAIL}"
_URIREF_RE = re.compile(rf"(?:{_URI}|{_RELREF})", re.ASCII)

_SPLIT_RE = re.compile(r"^(?:([^:/?#]+):)?(?://([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#(.*))?$", re.DOTALL)


def is_uri_reference(s):
    if not isinstance(s, str):
        return False
    return _URIREF_RE.fullmatch(s) is not None


def split(s):
    """RFC 3986 Appendix B. Undefined components are None."""
    m = _SPLIT_RE.match(s)
    return m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)


def remove_dot_segments(path):
    inp = path
    out = []
    while inp:
        if inp.startswith("../"):
            inp = inp[3:]
        elif inp.startswith("./"):
            inp = inp[2:]
        elif inp.startswith("/./"):
            inp = "/" + inp[3:]
        elif inp == "/.":
            inp = "/"
        elif inp.startswith("/../"):
            inp = "/" + inp[4:]
            if out:
                out.pop()
        elif inp == "/..":
            inp = "/"
            if out:
                out.pop()
        elif inp in (".", ".."):
            inp = ""
        else:
            start = 1 if inp.startswith("/") else 0
            i = inp.find("/", start)
            if i < 0:
                i = len(inp)
            out.append(inp[:i])
            inp = inp[i:]
    return "".join(out)


def recompose(scheme, auth, path, query, frag):
    r = ""
    if scheme is not None:
        r += scheme + ":"
    if auth is not None:
        r += "//" + auth
    r += path
    if query is not None:
        r += "?" + query
    if frag is not None:
        r += "#" + frag
    return r


def resolve(base, ref):
    """Strict RFC 3986 section 5.2.2 resolution. Returns a component tuple."""
    rs, ra, rp, rq, rf = split(ref)
    bs, ba, bp, bq, _ = split(base)
    if rs is not None:
        return rs, ra, remove_dot_segments(rp), rq, rf
    if ra is not None:
        return bs, ra, remove_dot_segments(rp), rq, rf
    if rp == "":
        return bs, ba, bp, (rq if rq is not None else bq), rf
    if rp.startswith("/"):
        tp = remove_dot_segments(rp)
    else:
        if ba is not None and bp == "":
            merged = "/" + rp
        else:
            i = bp.rfind("/")
            merged = bp[:i + 1] + rp if i >= 0 else rp
        tp = remove_dot_segments(merged)
    return bs, ba, tp, rq, rf


class FileUriError(ValueError):
    pass


def file_uri_to_path(components):
    """Map a resolved file: URI (component tuple) to a POSIX path (bytes)."""
    scheme, auth, path, query, _frag = components
    if scheme is None or scheme.lower() != "file":
        raise FileUriError("not a file: URI")
    if auth is not None and auth != "" and auth.lower() != "localhost":
        raise FileUriError(f"file: URI has a non-local authority {auth!r}")
    if not path.startswith("/"):
        raise FileUriError("file: URI path is not absolute")
    if query is not None:
        raise FileUriError("file: URI has a query component")
    segs = []
    for seg in path.split("/"):
        d = unquote_to_bytes(seg)
        if b"/" in d:
            raise FileUriError("file: URI path contains an encoded '/'")
        if b"\x00" in d:
            raise FileUriError("file: URI path contains a NUL byte")
        if d in (b".", b".."):
            raise FileUriError("file: URI path contains an encoded dot segment")
        segs.append(d)
    return b"/".join(segs)


_SAFE = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@/")


def path_to_base_uri(path):
    """Spec section 6: base URI of an archive opened from a local path."""
    p = os.fsencode(path)
    if not p.startswith(b"/"):
        p = os.fsencode(os.getcwd()) + b"/" + p
    out = []
    for seg in p.split(b"/"):
        if seg in (b"", b"."):
            continue
        if seg == b"..":
            if out:
                out.pop()
            continue
        out.append(seg)
    norm = b"/" + b"/".join(out)
    enc = "".join(chr(b) if b in _SAFE else "%%%02X" % b for b in norm)
    return "file://" + enc
