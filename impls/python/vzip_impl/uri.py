"""RFC 3986 URI-reference validation and strict resolution, plus the file: rules of spec §6."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

_HEX = "0-9A-Fa-f"
_UNRESERVED = "A-Za-z0-9\\-._~"
_SUBDELIMS = "!$&'()*+,;="
_PCT = f"%[{_HEX}]{{2}}"

_PCHAR = f"(?:[{_UNRESERVED}{re.escape(_SUBDELIMS)}:@]|{_PCT})"
_PCHAR_NC = f"(?:[{_UNRESERVED}{re.escape(_SUBDELIMS)}@]|{_PCT})"
_SEGMENT = f"{_PCHAR}*"
_SEGMENT_NZ = f"{_PCHAR}+"
_SEGMENT_NZ_NC = f"{_PCHAR_NC}+"
_QUERY = f"(?:{_PCHAR}|[/?])*"

_RE_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*")
_RE_QUERY = re.compile(_QUERY)
_RE_PATH_ABEMPTY = re.compile(f"(?:/{_SEGMENT})*")
_RE_PATH_ABSOLUTE = re.compile(f"/(?:{_SEGMENT_NZ}(?:/{_SEGMENT})*)?")
_RE_PATH_ROOTLESS = re.compile(f"{_SEGMENT_NZ}(?:/{_SEGMENT})*")
_RE_PATH_NOSCHEME = re.compile(f"{_SEGMENT_NZ_NC}(?:/{_SEGMENT})*")
_RE_USERINFO = re.compile(f"(?:[{_UNRESERVED}{re.escape(_SUBDELIMS)}:]|{_PCT})*")
_RE_REGNAME = re.compile(f"(?:[{_UNRESERVED}{re.escape(_SUBDELIMS)}]|{_PCT})*")
_RE_PORT = re.compile(r"[0-9]*")
_RE_IPVFUTURE = re.compile(f"[vV][{_HEX}]+\\.[{_UNRESERVED}{re.escape(_SUBDELIMS)}:]+")

_DEC_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])"
_IPV4 = f"{_DEC_OCTET}\\.{_DEC_OCTET}\\.{_DEC_OCTET}\\.{_DEC_OCTET}"
_H16 = f"[{_HEX}]{{1,4}}"
_LS32 = f"(?:{_H16}:{_H16}|{_IPV4})"
_IPV6 = "(?:" + "|".join([
    f"(?:{_H16}:){{6}}{_LS32}",
    f"::(?:{_H16}:){{5}}{_LS32}",
    f"(?:{_H16})?::(?:{_H16}:){{4}}{_LS32}",
    f"(?:(?:{_H16}:){{0,1}}{_H16})?::(?:{_H16}:){{3}}{_LS32}",
    f"(?:(?:{_H16}:){{0,2}}{_H16})?::(?:{_H16}:){{2}}{_LS32}",
    f"(?:(?:{_H16}:){{0,3}}{_H16})?::{_H16}:{_LS32}",
    f"(?:(?:{_H16}:){{0,4}}{_H16})?::{_LS32}",
    f"(?:(?:{_H16}:){{0,5}}{_H16})?::{_H16}",
    f"(?:(?:{_H16}:){{0,6}}{_H16})?::",
]) + ")"
_RE_IPV6 = re.compile(_IPV6)


def _valid_authority(auth: str) -> bool:
    if auth.count("@") > 1:
        return False
    if "@" in auth:
        userinfo, hostport = auth.split("@", 1)
        if not _RE_USERINFO.fullmatch(userinfo):
            return False
    else:
        hostport = auth
    if hostport.startswith("["):
        close = hostport.find("]")
        if close < 0:
            return False
        lit = hostport[1:close]
        if not (_RE_IPV6.fullmatch(lit) or _RE_IPVFUTURE.fullmatch(lit)):
            return False
        rest = hostport[close + 1:]
        if rest == "":
            return True
        return rest.startswith(":") and bool(_RE_PORT.fullmatch(rest[1:]))
    if ":" in hostport:
        host, port = hostport.split(":", 1)
        if not _RE_PORT.fullmatch(port):
            return False
    else:
        host = hostport
    return bool(_RE_REGNAME.fullmatch(host))


def is_uri_reference(s: str) -> bool:
    """True iff s matches the URI-reference rule of RFC 3986 §4.1 exactly."""
    if not s.isascii():
        return False
    frag = None
    if "#" in s:
        s, frag = s.split("#", 1)
        if not _RE_QUERY.fullmatch(frag):
            return False
    if "?" in s:
        s, q = s.split("?", 1)
        if not _RE_QUERY.fullmatch(q):
            return False
    # s is now scheme ":" hier-part, or relative-part
    m = re.match(r"([^:/?#]*):", s)
    if m:
        if not _RE_SCHEME.fullmatch(m.group(1)):
            return False
        rest = s[m.end():]
        noscheme = False
    else:
        rest = s
        noscheme = True
    if rest.startswith("//"):
        rest = rest[2:]
        i = rest.find("/")
        auth, path = (rest, "") if i < 0 else (rest[:i], rest[i:])
        return _valid_authority(auth) and bool(_RE_PATH_ABEMPTY.fullmatch(path))
    if rest == "":
        return True
    if rest.startswith("/"):
        return bool(_RE_PATH_ABSOLUTE.fullmatch(rest))
    if noscheme:
        return bool(_RE_PATH_NOSCHEME.fullmatch(rest))
    return bool(_RE_PATH_ROOTLESS.fullmatch(rest))


# ------------------------------------------------------------------ resolution

_RE_SPLIT = re.compile(r"^(([^:/?#]+):)?(//([^/?#]*))?([^?#]*)(\?([^#]*))?(#(.*))?$", re.S)


@dataclass
class URI:
    scheme: str | None
    authority: str | None
    path: str
    query: str | None
    fragment: str | None

    def __str__(self) -> str:
        out = ""
        if self.scheme is not None:
            out += self.scheme + ":"
        if self.authority is not None:
            out += "//" + self.authority
        out += self.path
        if self.query is not None:
            out += "?" + self.query
        if self.fragment is not None:
            out += "#" + self.fragment
        return out


def split_uri(s: str) -> URI:
    m = _RE_SPLIT.match(s)
    assert m is not None
    return URI(m.group(2), m.group(4), m.group(5), m.group(7), m.group(9))


def remove_dot_segments(path: str) -> str:
    inp = path
    out: list[str] = []
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
            j = inp.find("/", start)
            if j < 0:
                j = len(inp)
            out.append(inp[:j])
            inp = inp[j:]
    return "".join(out)


def _merge(base: URI, rpath: str) -> str:
    if base.authority is not None and base.path == "":
        return "/" + rpath
    i = base.path.rfind("/")
    return (base.path[: i + 1] if i >= 0 else "") + rpath


def resolve(base: str, ref: str) -> URI:
    """Strict RFC 3986 §5.2.2 resolution."""
    b = split_uri(base)
    r = split_uri(ref)
    if r.scheme is not None:
        return URI(r.scheme, r.authority, remove_dot_segments(r.path), r.query, r.fragment)
    if r.authority is not None:
        return URI(b.scheme, r.authority, remove_dot_segments(r.path), r.query, r.fragment)
    if r.path == "":
        q = r.query if r.query is not None else b.query
        return URI(b.scheme, b.authority, b.path, q, r.fragment)
    if r.path.startswith("/"):
        path = remove_dot_segments(r.path)
    else:
        path = remove_dot_segments(_merge(b, r.path))
    return URI(b.scheme, b.authority, path, r.query, r.fragment)


# ------------------------------------------------------------------ file: base URIs

_PATH_SAFE = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@/")


def normalise_abs_path(path: bytes) -> bytes:
    """Make a POSIX path absolute and normalise it lexically (spec §6)."""
    if not path.startswith(b"/"):
        path = os.getcwdb() + b"/" + path
    segs: list[bytes] = []
    for seg in path.split(b"/"):
        if seg in (b"", b"."):
            continue
        if seg == b"..":
            if segs:
                segs.pop()
            continue
        segs.append(seg)
    return b"/" + b"/".join(segs)


def file_base_uri(path) -> str:
    p = os.fsencode(path)
    p = normalise_abs_path(p)
    enc = "".join(chr(c) if c in _PATH_SAFE else f"%{c:02X}" for c in p)
    return "file://" + enc


class FileURIError(Exception):
    pass


def _pct_decode(s: str) -> bytes:
    out = bytearray()
    i = 0
    raw = s.encode("ascii")
    while i < len(raw):
        c = raw[i]
        if c == 0x25:
            out.append(int(raw[i + 1:i + 3], 16))
            i += 3
        else:
            out.append(c)
            i += 1
    return bytes(out)


def file_uri_to_path(u: URI) -> bytes:
    """Map a resolved file: URI to a local POSIX path, per spec §6. Raises FileURIError."""
    if u.authority is not None and u.authority not in ("",) and u.authority.lower() != "localhost":
        raise FileURIError(f"file: URI has a non-local authority {u.authority!r}")
    if not u.path.startswith("/"):
        raise FileURIError("file: URI path is not absolute")
    if u.query is not None:
        raise FileURIError("file: URI has a query component")
    segs = u.path.split("/")
    out = []
    for seg in segs:
        if re.search(r"%2[fF]", seg):
            raise FileURIError("file: URI path contains an encoded '/'")
        d = _pct_decode(seg)
        if b"\x00" in d:
            raise FileURIError("file: URI path contains a NUL byte")
        if d in (b".", b".."):
            raise FileURIError("file: URI path contains a dot segment after decoding")
        out.append(d)
    return b"/".join(out)
