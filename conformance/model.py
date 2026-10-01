"""Oracle: the expected results of harness queries against a valid description.

Computed straight from the description and the files on disk, following the
spec; it shares no code with any implementation. An expectation is a list of
acceptable results; {"ok": False} accepts any error.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

import pbref

RESERVED = "__vz__/"
ERROR = {"ok": False}


def err(cls: str) -> dict:
    return {"ok": False, "class": cls}


class ModelError(Exception):
    pass


def _utf8_key(k: str) -> bytes:
    return k.encode("utf-8")


class Model:
    def __init__(self, desc: dict, archive_path: Path, index_body: bytes | None = None,
                 http: tuple[str, Path] | None = None):
        self.http = http  # (server base URL, directory it serves)
        self.desc = desc
        # spec §6: absolute, lexically normalised, symlinks not resolved, and
        # every byte other than unreserved / sub-delims / ":" "@" "/" percent-encoded
        abs_path = os.path.normpath(os.path.join(os.getcwd(), str(archive_path)))
        self.base = "file://" + quote(abs_path.encode(), safe="/:@!$&'()*+,;=")
        self.entries = {e["key"]: e for e in desc["entries"]}
        self.sources = desc.get("sources", [])
        self.mirror = desc.get("mirror", True)

    # ------------------------------------------------------------ sources

    def _source_bytes(self, idx: int) -> bytes:
        """Whole source value, or raise ModelError (missing file, bad key source...)."""
        if idx >= len(self.sources):
            raise ModelError("source out of bounds")
        src = self.sources[idx]
        kind = next(k for k in ("url", "key", "data") if k in src)
        v = src[kind]
        if kind == "data":
            return bytes.fromhex(v)
        if kind == "key":
            e = self.entries.get(v)
            if e is None or "ranges" in e or v in (RESERVED + "sources", RESERVED + "index"):
                raise ModelError("bad key source")
            return bytes.fromhex(e["bytes"])
        if not _URI_REF.match(v):
            raise ModelError("not a URI reference")
        url = v if _SCHEME.match(v) else urljoin(self.base, v)
        p = urlparse(url)
        if p.scheme == "http" and self.http and url.startswith(self.http[0]):
            return self._http_bytes(url, src)
        if p.scheme != "file":
            raise ModelError("unsupported scheme")
        has_query = "?" in v.split("#")[0]
        if (p.netloc.lower() not in ("", "localhost") or has_query
                or not p.path.startswith("/") or re.search(r"%2[fF]|%00", p.path)):
            raise ModelError("bad file: URL")
        if any(seg in (".", "..") for seg in unquote(p.path).split("/")):
            raise ModelError("encoded dot segment")
        path = Path(unquote(p.path))
        if not path.is_file():
            raise ModelError("missing file")
        st = path.stat()
        if "etag" in src:
            raise ModelError("etag pin on file:")
        if "size" in src and st.st_size != src["size"]:
            raise ModelError("size pin")
        if "modified_not_after" in src and math.floor(st.st_mtime) > src["modified_not_after"]:
            raise ModelError("modified pin")
        return path.read_bytes()

    def _http_bytes(self, url: str, src: dict) -> bytes:
        """What a §6.2 reader gets from conformance/http_server.py for this source."""
        from http_server import QUIRKS, etag_for

        rel = unquote(url[len(self.http[0]):].split("?")[0])
        quirk, _, rest = rel.partition("/")
        if quirk not in QUIRKS:
            quirk, rest = None, rel
        if quirk == "redirect":  # readers follow up to 5 redirects (spec §6.2)
            n, _, rest = rest.partition("/")
            if int(n) > 5:
                raise ModelError("too many redirects")
            quirk = None
        path = self.http[1] / rest
        if not path.is_file():
            raise ModelError("404")
        data = path.read_bytes()
        if quirk == "gzip":
            raise ModelError("content-encoding")
        if quirk == "noetag" and ("etag" in src or "modified_not_after" in src):
            raise ModelError("pin uncheckable: no ETag / Last-Modified")
        if "etag" in src and src["etag"] != etag_for(data):
            raise ModelError("412")
        if "modified_not_after" in src and int(path.stat().st_mtime) > src["modified_not_after"]:
            raise ModelError("412")
        if "size" in src and (quirk == "nototal" or src["size"] != len(data)):
            raise ModelError("size pin")
        return data

    def _parts(self, ranges: list[dict]):
        """[(size, bytes or None, hard_error, source_len)] per range."""
        out = []
        for r in ranges:
            if "data" in r:
                out.append((len(bytes.fromhex(r["data"])), bytes.fromhex(r["data"]), None))
                continue
            off, n = r.get("offset", 0), r.get("length", 0)
            try:
                src = self._source_bytes(r.get("source", 0))
            except ModelError as e:
                out.append((n, None, str(e)))
                continue
            out.append((n, src[off : off + n], None if off + n <= len(src) else "oob"))
        return out

    # ------------------------------------------------------------ queries

    def kind(self, key: str) -> str:
        if key.startswith(RESERVED) or key not in self.entries:
            return "missing"
        return "reference" if "ranges" in self.entries[key] else "bytes"

    def get(self, key: str, rng: dict | None) -> list[dict]:
        if rng and "start" in rng and rng["start"] > rng["end"]:
            return [err("request")]
        k = self.kind(key)
        if k == "missing":
            return [{"ok": True, "value": None}]
        e = self.entries[key]
        if k == "bytes":
            v = bytes.fromhex(e["bytes"])
            a, b = _window(len(v), rng)
            return [{"ok": True, "value": v[a:b].hex()}]
        parts = self._parts(e["ranges"])
        size = sum(p[0] for p in parts)
        a, b = _window(size, rng)
        # spec §8.3: only ranges overlapping the window are resolved; a resolved
        # read fails iff the source is unusable or shorter than the bytes needed
        value, pos = b"", 0
        for n, data, problem in parts:
            lo, hi = max(a, pos), min(b, pos + n)
            if lo < hi:
                if problem not in (None, "oob") or hi - pos > len(data):
                    return [err("resolution")]
                value += data[lo - pos : hi - pos]
            pos += n
        return [{"ok": True, "value": value.hex()}]

    def get_raw(self, key: str) -> list[dict]:
        if key == RESERVED + "sources":
            return [{"ok": True, "value": pbref.source_table(self.sources).hex()}]
        e = self.entries.get(key)
        if e is None:
            return [{"ok": True, "value": None}]
        if "ranges" in e:
            body = pbref.payload(e["ranges"])[1] if self.mirror else b""
            return [{"ok": True, "value": body.hex()}]
        return [{"ok": True, "value": e["bytes"]}]

    def list(self, prefix: str) -> list[dict]:
        keys = sorted(
            (k for k in self.entries if not k.startswith(RESERVED) and k.startswith(prefix)),
            key=_utf8_key,
        )
        return [{"ok": True, "keys": keys}]

    def expect(self, q: dict) -> list[dict]:
        op = q["op"]
        if op == "classify":
            return [{"ok": True, "kind": self.kind(q["key"])}]
        if op == "get":
            return self.get(q["key"], q.get("range"))
        if op == "get_raw":
            return self.get_raw(q["key"])
        if op == "list":
            return self.list(q["prefix"])
        raise ValueError(op)


_URI_REF = re.compile(
    r"^(?![^:/?#]*:)(?:[A-Za-z0-9\-._~:/?#@!$&'()*+,;=]|%[0-9A-Fa-f]{2})*$"  # relative refs
    r"|^[A-Za-z][A-Za-z0-9+.\-]*:(?:[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=]|%[0-9A-Fa-f]{2})*$"
)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


def _window(n: int, rng: dict | None) -> tuple[int, int]:
    if rng is None:
        return 0, n
    if "suffix" in rng:
        return max(n - rng["suffix"], 0), n
    if "offset" in rng:
        return min(rng["offset"], n), n
    return min(rng["start"], n), min(rng["end"], n)


def standard_queries(desc: dict, extra_keys: list[str] = ()) -> list[dict]:
    """Queries exercising every key of a description plus some edge cases."""
    keys = [e["key"] for e in desc["entries"]] + list(extra_keys)
    keys += ["nope", "r/single/x", "__vz__/sources", ""]
    qs = []
    for k in keys:
        qs.append({"op": "classify", "key": k})
        qs.append({"op": "get", "key": k})
        qs.append({"op": "get_raw", "key": k})
        for r in ({"start": 1, "end": 3}, {"start": 0, "end": 10**9}, {"offset": 2},
                  {"suffix": 3}, {"suffix": 10**9}, {"start": 4, "end": 4}):
            qs.append({"op": "get", "key": k, "range": r})
    qs.append({"op": "get", "key": keys[0] if keys else "x", "range": {"start": 5, "end": 2}})
    qs.append({"op": "get", "key": "nope", "range": {"start": 5, "end": 2}})
    for p in ["", "r/", "unicode/", "__vz__/", "zz"]:
        qs.append({"op": "list", "prefix": p})
    return qs
