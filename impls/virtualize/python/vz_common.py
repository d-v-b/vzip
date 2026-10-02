"""Shared pieces: rejection, HTTP range reader, Zarr/OME helpers (VIRTUALIZE.md §1-2)."""

from __future__ import annotations

import http.client
import urllib.parse


class Reject(Exception):
    """The specification rejects the input (exit status 3)."""


# ---------------------------------------------------------------- HTTP reader

BLOCK = 1 << 16


class Source:
    """Random access to an http(s) URL with HEAD + single-range GETs, cached in blocks."""

    def __init__(self, url: str) -> None:
        self.url = url
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ("http", "https"):
            raise ValueError(f"unsupported URL scheme: {p.scheme!r}")
        self.scheme = p.scheme
        self.netloc = p.netloc
        self.path = (p.path or "/") + (("?" + p.query) if p.query else "")
        self.conn = None
        self.blocks: dict[int, bytes] = {}
        self.requests = 0
        self.size = self._head()

    def _connect(self):
        if self.conn is None:
            cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
            self.conn = cls(self.netloc, timeout=600)
        return self.conn

    def _request(self, method: str, headers: dict):
        last = None
        for _ in range(3):
            conn = self._connect()
            try:
                conn.request(method, self.path, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
                self.requests += 1
                return resp, body
            except (http.client.HTTPException, OSError) as e:
                last = e
                try:
                    conn.close()
                finally:
                    self.conn = None
        raise last  # type: ignore[misc]

    def _head(self) -> int:
        resp, _ = self._request("HEAD", {})
        if resp.status != 200:
            raise RuntimeError(f"HEAD {self.url}: HTTP {resp.status}")
        n = resp.getheader("Content-Length")
        if n is None:
            raise RuntimeError(f"HEAD {self.url}: no Content-Length")
        return int(n)

    def _fetch(self, first: int, last: int) -> None:
        """Fetch blocks first..last (inclusive) with one request."""
        a = first * BLOCK
        b = min(self.size, (last + 1) * BLOCK) - 1
        resp, body = self._request("GET", {"Range": f"bytes={a}-{b}"})
        if resp.status != 206:
            raise RuntimeError(f"GET {self.url} bytes={a}-{b}: HTTP {resp.status}")
        if len(body) != b - a + 1:
            raise RuntimeError(f"GET {self.url} bytes={a}-{b}: short body {len(body)}")
        for i in range(first, last + 1):
            self.blocks[i] = body[(i - first) * BLOCK:(i - first + 1) * BLOCK]

    def read(self, off: int, n: int) -> bytes:
        """Read exactly n bytes at off; reading outside the file rejects the input."""
        if n == 0:
            return b""
        if off < 0 or n < 0 or off + n > self.size:
            raise Reject(f"read of {n} bytes at offset {off} is outside the file (size {self.size})")
        first, last = off // BLOCK, (off + n - 1) // BLOCK
        i = first
        while i <= last:
            if i in self.blocks:
                i += 1
                continue
            j = i
            while j + 1 <= last and (j + 1) not in self.blocks:
                j += 1
            self._fetch(i, j)
            i = j + 1
        if first == last:
            s = off - first * BLOCK
            return self.blocks[first][s:s + n]
        buf = b"".join(self.blocks[i] for i in range(first, last + 1))
        s = off - first * BLOCK
        return buf[s:s + n]


# ---------------------------------------------------------------- units (§2.3)

UNITS = {
    "µm": "micrometer",
    "μm": "micrometer",
    "um": "micrometer",
    "nm": "nanometer",
    "mm": "millimeter",
    "cm": "centimeter",
    "m": "meter",
    "Å": "angstrom",
    "pm": "picometer",
    "in": "inch",
    "ft": "foot",
    "s": "second",
    "ms": "millisecond",
    "min": "minute",
    "h": "hour",
}

AXIS_TYPE = {"t": "time", "c": "channel", "z": "space", "y": "space", "x": "space"}


# ---------------------------------------------------------------- zarr (§2.1)


def array_json(shape, data_type, chunk_shape, codecs, dimension_names) -> dict:
    return {
        "zarr_format": 3,
        "node_type": "array",
        "shape": list(shape),
        "data_type": data_type,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": list(chunk_shape)}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": 0,
        "codecs": codecs,
        "dimension_names": list(dimension_names),
        "attributes": {},
    }


def build_codecs(axes, itemsize: int, endian: str | None, interleaved: bool,
                 jpeg2k: bool = False, compressor: str | None = None) -> list:
    codecs = []
    if interleaved:
        order = [i for i, a in enumerate(axes) if a != "c"] + [axes.index("c")]
        codecs.append({"name": "transpose", "configuration": {"order": order}})
    if jpeg2k:
        codecs.append({"name": "imagecodecs_jpeg2k"})
    elif itemsize == 1:
        codecs.append({"name": "bytes"})
    else:
        codecs.append({"name": "bytes", "configuration": {"endian": endian}})
    if compressor == "zlib":
        codecs.append({"name": "zlib", "configuration": {"level": 1}})
    elif compressor == "zstd":
        codecs.append({"name": "zstd", "configuration": {"level": 0, "checksum": False}})
    return codecs


def group_json(attributes: dict) -> dict:
    return {"zarr_format": 3, "node_type": "group", "attributes": attributes}


def image_ome(name, axes, units: dict, scales: list[list[float]], omero=None) -> dict:
    """The OME-NGFF 0.5 object M (§2.2). scales: one list per level."""
    ax = []
    for a in axes:
        d = {"name": a, "type": AXIS_TYPE[a]}
        if units.get(a) is not None:
            d["unit"] = units[a]
        ax.append(d)
    ms = {}
    if name is not None:
        ms["name"] = name
    ms["axes"] = ax
    for s in scales:
        for x in s:
            if x != x or x in (float("inf"), float("-inf")):
                raise Reject(f"non-finite scale {x}")
    ms["datasets"] = [
        {"path": str(i), "coordinateTransformations": [{"type": "scale", "scale": list(s)}]}
        for i, s in enumerate(scales)
    ]
    m = {"version": "0.5", "multiscales": [ms]}
    if omero is not None:
        m["omero"] = omero
    return m
