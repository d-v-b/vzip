"""RFC 3986 URI references, strict resolution, and the file: rules of spec §6."""

from __future__ import annotations

import os
import re
from urllib.parse import quote, unquote

_PCT = r"%[0-9A-Fa-f]{2}"
_UNRESERVED = r"A-Za-z0-9\-._~"
_SUB = r"!$&'()*+,;="
_PCHAR = rf"(?:[{_UNRESERVED}{_SUB}:@]|{_PCT})"
_SPLIT = re.compile(r"^(?:([^:/?#]+):)?(?://([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#(.*))?\Z")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*\Z")
_USERINFO_HOST_PORT = re.compile(
    rf"^(?:(?:[{_UNRESERVED}{_SUB}:]|{_PCT})*@)?"
    rf"(?:\[[0-9A-Fa-f:.vV{_UNRESERVED}{_SUB}]+\]|(?:[{_UNRESERVED}{_SUB}]|{_PCT})*)"
    r"(?::[0-9]*)?$"
)
_PATH = re.compile(rf"^(?:{_PCHAR}|/)*\Z")
_QUERY = re.compile(rf"^(?:{_PCHAR}|[/?])*\Z")


def is_uri_reference(ref: str) -> bool:
    """Does `ref` match RFC 3986 URI-reference (§4.1)?"""
    m = _SPLIT.match(ref)
    if not m or not ref.isascii():
        return False
    scheme, authority, path, query, fragment = m.groups()
    if scheme is not None and not _SCHEME.match(scheme):
        return False
    if authority is not None and not _USERINFO_HOST_PORT.match(authority):
        return False
    if not _PATH.match(path):
        return False
    if authority is not None and path and not path.startswith("/"):
        return False
    if scheme is None and authority is None:
        first = path.split("/", 1)[0]
        if ":" in first:  # relative-path reference: first segment has no colon
            return False
    return all(x is None or _QUERY.match(x) for x in (query, fragment))


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 §5.2.4."""
    out: list[str] = []
    while path:
        if path.startswith("../"):
            path = path[3:]
        elif path.startswith("./"):
            path = path[2:]
        elif path.startswith("/./"):
            path = path[2:]
        elif path == "/.":
            path = "/"
        elif path.startswith("/../"):
            path = path[3:]
            if out:
                out.pop()
        elif path == "/..":
            path = "/"
            if out:
                out.pop()
        elif path in (".", ".."):
            path = ""
        else:
            i = path.find("/", 1)
            seg, path = (path, "") if i < 0 else (path[:i], path[i:])
            out.append(seg)
    return "".join(out)


def _compose(scheme, authority, path, query, fragment) -> str:
    """RFC 3986 §5.3; a defined-but-empty component keeps its delimiter."""
    s = f"{scheme}:" if scheme is not None else ""
    if authority is not None:
        s += "//" + authority
    s += path
    if query is not None:
        s += "?" + query
    if fragment is not None:
        s += "#" + fragment
    return s


def resolve(base: str, ref: str) -> str:
    """Strict RFC 3986 §5.2.2 resolution (urllib's urljoin drops empty queries)."""
    if not is_uri_reference(ref):
        raise ValueError(f"not a valid URI reference: {ref!r}")
    bs, ba, bp, bq, _ = _SPLIT.match(base).groups()
    rs, ra, rp, rq, rf = _SPLIT.match(ref).groups()
    if rs is not None:
        return _compose(rs, ra, _remove_dot_segments(rp), rq, rf)
    if ra is not None:
        return _compose(bs, ra, _remove_dot_segments(rp), rq, rf)
    if rp == "":
        return _compose(bs, ba, bp, rq if rq is not None else bq, rf)
    if rp.startswith("/"):
        path = _remove_dot_segments(rp)
    elif ba is not None and bp == "":
        path = _remove_dot_segments("/" + rp)
    else:
        path = _remove_dot_segments(bp[: bp.rfind("/") + 1] + rp)
    return _compose(bs, ba, path, rq, rf)


def file_uri(path: str) -> str:
    """Base URI of a local path (spec §6): absolute, lexical, percent-encoded."""
    abs_path = os.path.normpath(os.path.join(os.getcwd(), path))
    if abs_path.startswith("//"):
        abs_path = "/" + abs_path.lstrip("/")
    return "file://" + quote(abs_path.encode("utf-8"), safe="/:@!$&'()*+,;=")


def file_path(url: str) -> str:
    """Local path of a file: URI, or ValueError per the rules of spec §6."""
    m = _SPLIT.match(url)
    scheme, authority, path, query, _ = m.groups()
    if (scheme or "").lower() != "file":
        raise ValueError(f"not a file: URI: {url!r}")
    if (authority or "").lower() not in ("", "localhost"):
        raise ValueError(f"file: URI with authority {authority!r}")
    if query is not None:
        raise ValueError("file: URI with a query component")
    if not path.startswith("/"):
        raise ValueError("file: URI with a relative path")
    if re.search(r"%2[fF]|%00", path):
        raise ValueError("file: URI path encodes '/' or NUL")
    decoded = unquote(path)
    if any(seg in (".", "..") for seg in decoded.split("/")):
        raise ValueError("file: URI path has an encoded '.' or '..' segment")
    return decoded
