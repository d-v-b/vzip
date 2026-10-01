"""RFC 3986 URI-reference validation, strict reference resolution, and file: mapping (spec §6)."""

import os
import re

_UNRES = r"A-Za-z0-9\-._~"
_SUB = r"!$&'()*+,;="
_PCT = r"%[0-9A-Fa-f]{2}"
_PCHAR = rf"(?:[{_UNRES}{_SUB}:@]|{_PCT})"
_SEG = rf"{_PCHAR}*"
_SEG_NZ = rf"{_PCHAR}+"
_SEG_NZ_NC = rf"(?:[{_UNRES}{_SUB}@]|{_PCT})+"
_PATH_ABEMPTY = rf"(?:/{_SEG})*"
_PATH_ABSOLUTE = rf"/(?:{_SEG_NZ}(?:/{_SEG})*)?"
_PATH_NOSCHEME = rf"{_SEG_NZ_NC}(?:/{_SEG})*"
_PATH_ROOTLESS = rf"{_SEG_NZ}(?:/{_SEG})*"
_DEC = r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])"
_IPV4 = rf"{_DEC}\.{_DEC}\.{_DEC}\.{_DEC}"
_H16 = r"[0-9A-Fa-f]{1,4}"
_LS32 = rf"(?:{_H16}:{_H16}|{_IPV4})"


def _rep(n):
    return rf"(?:{_H16}:){{{n}}}"


def _upto(n):
    # [ *n( h16 ":" ) h16 ]
    return rf"(?:(?:{_H16}:){{0,{n}}}{_H16})?"


_IPV6 = "(?:" + "|".join([
    rf"{_rep(6)}{_LS32}",
    rf"::{_rep(5)}{_LS32}",
    rf"(?:{_H16})?::{_rep(4)}{_LS32}",
    rf"{_upto(1)}::{_rep(3)}{_LS32}",
    rf"{_upto(2)}::{_rep(2)}{_LS32}",
    rf"{_upto(3)}::{_H16}:{_LS32}",
    rf"{_upto(4)}::{_LS32}",
    rf"{_upto(5)}::{_H16}",
    rf"{_upto(6)}::",
]) + ")"
_IPVFUTURE = rf"[vV][0-9A-Fa-f]+\.[{_UNRES}{_SUB}:]+"
_IP_LITERAL = rf"\[(?:{_IPV6}|{_IPVFUTURE})\]"
_REG_NAME = rf"(?:[{_UNRES}{_SUB}]|{_PCT})*"
_HOST = rf"(?:{_IP_LITERAL}|{_IPV4}|{_REG_NAME})"
_USERINFO = rf"(?:[{_UNRES}{_SUB}:]|{_PCT})*"
_AUTHORITY = rf"(?:{_USERINFO}@)?{_HOST}(?::[0-9]*)?"
_QUERY = rf"(?:{_PCHAR}|[/?])*"
_SCHEME = r"[A-Za-z][A-Za-z0-9+\-.]*"
_HIER = rf"(?://{_AUTHORITY}{_PATH_ABEMPTY}|{_PATH_ABSOLUTE}|{_PATH_ROOTLESS}|)"
_URI = rf"{_SCHEME}:{_HIER}(?:\?{_QUERY})?(?:#{_QUERY})?"
_REL_PART = rf"(?://{_AUTHORITY}{_PATH_ABEMPTY}|{_PATH_ABSOLUTE}|{_PATH_NOSCHEME}|)"
_REL = rf"{_REL_PART}(?:\?{_QUERY})?(?:#{_QUERY})?"

URI_REFERENCE_RE = re.compile(rf"(?:{_URI}|{_REL})", re.ASCII)
_AUTHORITY_RE = re.compile(_AUTHORITY, re.ASCII)
_SPLIT_RE = re.compile(r"^(([^:/?#]+):)?(//([^/?#]*))?([^?#]*)(\?([^#]*))?(#(.*))?$", re.S)


def is_uri_reference(s):
    return isinstance(s, str) and URI_REFERENCE_RE.fullmatch(s) is not None


class URI:
    __slots__ = ("scheme", "authority", "path", "query", "fragment")

    def __init__(self, scheme, authority, path, query, fragment):
        self.scheme = scheme
        self.authority = authority
        self.path = path
        self.query = query
        self.fragment = fragment

    def __str__(self):
        s = ""
        if self.scheme is not None:
            s += self.scheme + ":"
        if self.authority is not None:
            s += "//" + self.authority
        s += self.path
        if self.query is not None:
            s += "?" + self.query
        if self.fragment is not None:
            s += "#" + self.fragment
        return s

    def __repr__(self):
        return "URI(%r)" % str(self)


def split(s):
    m = _SPLIT_RE.match(s)
    return URI(m.group(2), m.group(4), m.group(5), m.group(7), m.group(9))


def remove_dot_segments(path):
    inp = path
    out = []
    while inp:
        if inp.startswith("../"):
            inp = inp[3:]
        elif inp.startswith("./"):
            inp = inp[2:]
        elif inp.startswith("/./"):
            inp = inp[2:]
        elif inp == "/.":
            inp = "/"
        elif inp.startswith("/../"):
            inp = inp[3:]
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
            if i == -1:
                i = len(inp)
            out.append(inp[:i])
            inp = inp[i:]
    return "".join(out)


def _merge(base, rpath):
    if base.authority is not None and base.path == "":
        return "/" + rpath
    i = base.path.rfind("/")
    return base.path[:i + 1] + rpath


def resolve(base, ref):
    """RFC 3986 §5.2.2 strict resolution. base and ref are URI objects (or strings)."""
    if isinstance(base, str):
        base = split(base)
    if isinstance(ref, str):
        ref = split(ref)
    if ref.scheme is not None:
        t = URI(ref.scheme, ref.authority, remove_dot_segments(ref.path), ref.query, None)
    else:
        if ref.authority is not None:
            t = URI(None, ref.authority, remove_dot_segments(ref.path), ref.query, None)
        else:
            if ref.path == "":
                t = URI(None, None, base.path, ref.query if ref.query is not None else base.query, None)
            else:
                if ref.path.startswith("/"):
                    p = remove_dot_segments(ref.path)
                else:
                    p = remove_dot_segments(_merge(base, ref.path))
                t = URI(None, None, p, ref.query, None)
            t.authority = base.authority
        t.scheme = base.scheme
    t.fragment = ref.fragment
    return t


_PATH_SAFE = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@/")


def path_to_file_uri(path):
    """Base URI of an archive opened from a local path (spec §6)."""
    p = os.fsencode(path)
    if not p.startswith(b"/"):
        p = os.fsencode(os.getcwd()) + b"/" + p
    segs = []
    for s in p.split(b"/"):
        if s in (b"", b"."):
            continue
        if s == b"..":
            if segs:
                segs.pop()
            continue
        segs.append(s)
    raw = b"/" + b"/".join(segs)
    enc = "".join(chr(c) if c in _PATH_SAFE else "%%%02X" % c for c in raw)
    return "file://" + enc


class FileMappingError(Exception):
    pass


def _pct_decode(s):
    out = bytearray()
    i = 0
    b = s.encode("ascii")
    while i < len(b):
        c = b[i]
        if c == 0x25:
            out.append(int(b[i + 1:i + 3], 16))
            i += 3
        else:
            out.append(c)
            i += 1
    return bytes(out)


def file_uri_to_path(u):
    """Map a resolved file: URI to a POSIX path (bytes), spec §6."""
    a = u.authority
    if a is not None and a != "" and a.lower() != "localhost":
        raise FileMappingError("file: URI authority must be absent, empty or localhost: %r" % a)
    if not u.path.startswith("/"):
        raise FileMappingError("file: URI path must be absolute")
    if u.query is not None:
        raise FileMappingError("file: URI must not have a query")
    if re.search(r"%2[Ff]", u.path):
        raise FileMappingError("file: URI path contains encoded '/'")
    raw = _pct_decode(u.path)
    if b"\x00" in raw:
        raise FileMappingError("file: URI path contains NUL")
    for seg in raw.split(b"/"):
        if seg in (b".", b".."):
            raise FileMappingError("file: URI path contains a dot segment after decoding")
    return raw


class HttpUrlError(Exception):
    pass


def http_host_port(u):
    """Return (host, port) for an http(s) URI; raises HttpUrlError."""
    a = u.authority
    if a is None:
        raise HttpUrlError("http URL without authority")
    if "@" in a:
        raise HttpUrlError("http URL with userinfo")
    if a.startswith("["):
        i = a.index("]")
        host = a[:i + 1]
        rest = a[i + 1:]
    else:
        i = a.find(":")
        host, rest = (a, "") if i == -1 else (a[:i], a[i:])
    if rest.startswith(":"):
        port_s = rest[1:]
    else:
        port_s = ""
    if host == "":
        raise HttpUrlError("http URL with empty host")
    if not host.startswith("["):
        try:
            host = _pct_decode(host).decode("utf-8")
        except UnicodeDecodeError:
            raise HttpUrlError("http URL host is not valid UTF-8")
    if port_s == "":
        port = 443 if u.scheme.lower() == "https" else 80
    else:
        port = int(port_s)
        if port > 65535:
            raise HttpUrlError("http URL port out of range")
    return host, port
