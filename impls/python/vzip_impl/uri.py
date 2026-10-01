"""RFC 3986 URI-reference validation and strict resolution (spec §6)."""

import ipaddress
import os
import re

ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
DIGIT = "0123456789"
HEX = "0123456789ABCDEFabcdef"
UNRESERVED = set(ALPHA + DIGIT + "-._~")
SUB_DELIMS = set("!$&'()*+,;=")
PCHAR = UNRESERVED | SUB_DELIMS | {":", "@"}


class URIError(Exception):
    pass


def _check_chars(s, allowed, what):
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "%":
            if i + 3 > n or s[i + 1] not in HEX or s[i + 2] not in HEX:
                raise URIError(f"bad percent-encoding in {what}")
            i += 3
            continue
        if c not in allowed:
            raise URIError(f"invalid character {c!r} in {what}")
        i += 1


_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+\-.]*\Z")
_IPVFUTURE_RE = re.compile(r"[vV][0-9A-Fa-f]+\.[A-Za-z0-9\-._~!$&'()*+,;=:]+\Z")


def _check_ipv6(s):
    if "%" in s:
        raise URIError("zone identifiers are not allowed")
    # RFC 3986 IPv6address; ipaddress is close enough (it accepts embedded IPv4)
    try:
        ipaddress.IPv6Address(s)
    except ValueError:
        raise URIError("invalid IPv6 literal") from None


class URIRef:
    """A parsed URI reference; undefined components are None."""

    __slots__ = ("scheme", "authority", "path", "query", "fragment")

    def __init__(self, scheme, authority, path, query, fragment):
        self.scheme = scheme
        self.authority = authority
        self.path = path
        self.query = query
        self.fragment = fragment

    def __str__(self):  # RFC 3986 §5.3
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


def split_authority(auth):
    """Return (userinfo|None, host, port|None)."""
    userinfo = None
    if "@" in auth:
        userinfo, auth = auth.split("@", 1)
    if auth.startswith("["):
        close = auth.find("]")
        if close < 0:
            raise URIError("unterminated IP literal")
        host = auth[:close + 1]
        rest = auth[close + 1:]
        if rest == "":
            port = None
        elif rest.startswith(":"):
            port = rest[1:]
        else:
            raise URIError("garbage after IP literal")
    else:
        if ":" in auth:
            host, port = auth.split(":", 1)
        else:
            host, port = auth, None
    return userinfo, host, port


def _check_authority(auth):
    userinfo, host, port = split_authority(auth)
    if userinfo is not None:
        _check_chars(userinfo, UNRESERVED | SUB_DELIMS | {":"}, "userinfo")
    if host.startswith("["):
        inner = host[1:-1]
        if _IPVFUTURE_RE.match(inner):
            pass
        else:
            _check_ipv6(inner)
    else:
        _check_chars(host, UNRESERVED | SUB_DELIMS, "host")
    if port is not None and not all(c in DIGIT for c in port):
        raise URIError("invalid port")


def parse(s):
    """Parse and validate s against RFC 3986 URI-reference. Raise URIError."""
    if not isinstance(s, str):
        raise URIError("not a string")
    if any(ord(c) > 0x7E or ord(c) < 0x21 for c in s):
        raise URIError("URI reference must be printable ASCII without spaces")
    rest = s
    fragment = query = None
    if "#" in rest:
        rest, fragment = rest.split("#", 1)
        _check_chars(fragment, PCHAR | {"/", "?"}, "fragment")
    if "?" in rest:
        rest, query = rest.split("?", 1)
        _check_chars(query, PCHAR | {"/", "?"}, "query")
    scheme = None
    m = re.match(r"([^:/?#]+):", rest)
    if m:
        if not _SCHEME_RE.match(m.group(1)):
            # A ':' in the first segment of a relative reference is not allowed
            raise URIError("invalid scheme / colon in first path segment")
        scheme = m.group(1)
        rest = rest[m.end():]
    elif rest.startswith(":"):
        raise URIError("empty scheme")
    authority = None
    if rest.startswith("//"):
        end = rest.find("/", 2)
        if end < 0:
            end = len(rest)
        authority = rest[2:end]
        rest = rest[end:]
        _check_authority(authority)
    path = rest
    for seg in path.split("/"):
        _check_chars(seg, PCHAR, "path")
    if scheme is None and authority is None and not path.startswith("/"):
        first = path.split("/", 1)[0]
        if ":" in first:
            raise URIError("colon in first segment of relative path")
    return URIRef(scheme, authority, path, query, fragment)


def is_uri_reference(s):
    try:
        parse(s)
        return True
    except URIError:
        return False


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
            nxt = inp.find("/", start)
            if nxt < 0:
                nxt = len(inp)
            out.append(inp[:nxt])
            inp = inp[nxt:]
    return "".join(out)


def _merge(base, rpath):
    if base.authority is not None and base.path == "":
        return "/" + rpath
    i = base.path.rfind("/")
    return base.path[:i + 1] + rpath


def resolve(base, ref):
    """RFC 3986 §5.2.2, strict.  base and ref are URIRef."""
    if ref.scheme is not None:
        return URIRef(ref.scheme, ref.authority, remove_dot_segments(ref.path),
                      ref.query, ref.fragment)
    if ref.authority is not None:
        return URIRef(base.scheme, ref.authority, remove_dot_segments(ref.path),
                      ref.query, ref.fragment)
    if ref.path == "":
        path = base.path
        query = ref.query if ref.query is not None else base.query
    else:
        if ref.path.startswith("/"):
            path = remove_dot_segments(ref.path)
        else:
            path = remove_dot_segments(_merge(base, ref.path))
        query = ref.query
    return URIRef(base.scheme, base.authority, path, query, ref.fragment)


# ------------------------------------------------------------ local paths

_PATH_SAFE = set((ALPHA + DIGIT + "-._~" + "!$&'()*+,;=" + ":@/").encode())


def normalize_path(path):
    """Absolute, lexically normalised path (spec §6, base URI step 1).

    Works on bytes.  Symlinks are not resolved.
    """
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
    return b"/" + b"/".join(out)


def base_uri_for_path(path):
    p = normalize_path(path)
    enc = "".join(chr(b) if b in _PATH_SAFE else "%%%02X" % b for b in p)
    return "file://" + enc


def _pct_decode(s):
    out = bytearray()
    i = 0
    raw = s.encode("ascii")
    while i < len(raw):
        if raw[i] == 0x25:
            out.append(int(raw[i + 1:i + 3], 16))
            i += 3
        else:
            out.append(raw[i])
            i += 1
    return bytes(out)


def file_uri_to_path(u):
    """Map a resolved file: URIRef to a local path (bytes), per spec §6."""
    if u.authority is not None and u.authority not in ("",) and u.authority.lower() != "localhost":
        raise URIError("file: URI with a non-local authority")
    if not u.path.startswith("/"):
        raise URIError("file: URI path is not absolute")
    if u.query is not None:
        raise URIError("file: URI has a query component")
    segs = u.path.split("/")
    out = []
    for seg in segs:
        d = _pct_decode(seg)
        if b"/" in d:
            raise URIError("file: URI path contains an encoded '/'")
        if b"\x00" in d:
            raise URIError("file: URI path contains NUL")
        if d in (b".", b".."):
            raise URIError("file: URI path contains a dot segment")
        out.append(d)
    return b"/".join(out)
