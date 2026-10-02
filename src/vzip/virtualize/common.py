"""Shared parts of the virtualizers (VIRTUALIZE.md §1, §2)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from vzip.archive import VZipWriter
from vzip.pb import Range, Source


class Rejected(Exception):
    """The input is not accepted by the profile (VIRTUALIZE.md §1.2)."""


@dataclass
class Output:
    """A virtualizer's output: one url source and the entries (§1.1)."""

    url: str
    bytes_entries: dict[str, bytes] = field(default_factory=dict)
    refs: dict[str, list[tuple[int, int]]] = field(default_factory=dict)  # (offset, length)
    summary: dict = field(default_factory=dict)

    def json(self, key: str, value) -> None:
        self.bytes_entries[key] = json.dumps(value, indent=2).encode()

    def write(self, out_path: str) -> None:
        with open(out_path, "wb") as fh:
            w = VZipWriter(fh, page_size=1 << 16)
            src = w.source(Source(url=self.url))
            for key in sorted(self.refs):
                w.add_ranges(key, [Range(source=src, offset=o, length=n) for o, n in self.refs[key]])
            for key in sorted(self.bytes_entries):
                w.add_bytes(key, self.bytes_entries[key], compress=not key.endswith("zarr.json"),
                            late=key.endswith("zarr.json"))
            w.close()


UNITS = {
    "µm": "micrometer", "μm": "micrometer", "um": "micrometer", "nm": "nanometer",
    "mm": "millimeter", "cm": "centimeter", "m": "meter", "\u00c5": "angstrom", "\u212b": "angstrom",
    "pm": "picometer",
    "in": "inch", "ft": "foot", "s": "second", "ms": "millisecond", "min": "minute", "h": "hour",
}

TYPES = {"t": "time", "c": "channel", "z": "space", "y": "space", "x": "space"}


def array_json(shape, data_type: str, chunk_shape, codecs: list, axes: list[str]) -> dict:
    """An array's zarr.json (§2.1)."""
    return {
        "zarr_format": 3,
        "node_type": "array",
        "shape": list(shape),
        "data_type": data_type,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": list(chunk_shape)}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": 0,
        "codecs": codecs,
        "dimension_names": list(axes),
        "attributes": {},
    }


def transpose_codec(axes: list[str]) -> dict:
    """The transpose codec for frames that hold the channel axis last (§2.1)."""
    stored = [a for a in axes if a != "c"] + ["c"]
    return {"name": "transpose", "configuration": {"order": [axes.index(a) for a in stored]}}


def group_json(ome: dict) -> dict:
    return {"zarr_format": 3, "node_type": "group", "attributes": {"ome": ome}}


def image_ome(axes: list[str], units: dict, scales: list[list[float]], name: str | None,
              translations: list[list[float]] | None = None) -> dict:
    """The OME-NGFF 0.5 object of an image (§2.2), with a translation per
    level after its scale when `translations` is given."""
    ms = {}
    if name is not None:
        ms["name"] = name
    ms["axes"] = [{"name": a, "type": TYPES[a], **({"unit": units[a]} if units.get(a) else {})} for a in axes]
    ms["datasets"] = [
        {"path": str(i), "coordinateTransformations": [{"type": "scale", "scale": s}] + (
            [{"type": "translation", "translation": translations[i]}] if translations else [])}
        for i, s in enumerate(scales)
    ]
    return {"version": "0.5", "multiscales": [ms]}


Reader = Callable[[int, int], bytes]


def _retry(f):
    """Calls f, retrying transient network errors with backoff."""
    import time
    import urllib.error

    for attempt in range(5):
        try:
            return f()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 4:
                raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == 4:
                raise
        time.sleep(2**attempt)


def http_reader(url: str, block: int = 1 << 16) -> tuple[Reader, int]:
    """A cached range reader for an http(s) URL, and the object's size."""

    def head():
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60) as r:
                return int(r.headers["Content-Length"])
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                raise
        # The server refuses HEAD: take the size from a one-byte range request.
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            total = (r.headers.get("Content-Range") or "").rpartition("/")[2]
            if r.status != 206 or not total.isdigit():
                raise Rejected(f"{url}: cannot determine the size")
            return int(total)

    size = _retry(head)
    cache: dict[int, bytes] = {}

    def fetch(i: int) -> bytes:
        if i not in cache:
            start = i * block
            end = min(size, start + block)
            req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end - 1}"})

            def get():
                with urllib.request.urlopen(req, timeout=120) as r:
                    return r.read()

            data = _retry(get)
            if len(data) != end - start:
                raise OSError(f"{url}: short read at {start}")
            cache[i] = data
        return cache[i]

    def read(offset: int, length: int) -> bytes:
        if offset < 0 or offset + length > size:
            raise Rejected(f"read of [{offset}, {offset + length}) outside the {size}-byte file")
        out = bytearray()
        pos = offset
        while pos < offset + length:
            data = fetch(pos // block)
            start = pos % block
            take = min(len(data) - start, offset + length - pos)
            out += data[start : start + take]
            pos += take
        return bytes(out)

    return read, size


def file_reader(path: str) -> tuple[Reader, int]:
    data = open(path, "rb").read()

    def read(offset: int, length: int) -> bytes:
        if offset < 0 or offset + length > len(data):
            raise Rejected(f"read of [{offset}, {offset + length}) outside the {len(data)}-byte file")
        return data[offset : offset + length]

    return read, len(data)
