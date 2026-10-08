"""Shared parts of the virtualizers (VIRTUALIZE.md §1, §2)."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
import math
from dataclasses import dataclass, field
from typing import Callable

from vzip.archive import VZipWriter
from vzip.pb import Range, Source


class Rejected(Exception):
    """The input is not accepted by the profile (VIRTUALIZE.md §1.2)."""


# A range of an output: (offset, length) of source 0, (source, offset, length)
# of any source, or literal bytes.
Part = tuple[int, int] | tuple[int, int, int] | bytes


@dataclass
class Output:
    """A virtualizer's output: the url source 0, the data sources after it,
    and the entries (§1.1, §1.2)."""

    url: str
    bytes_entries: dict[str, bytes] = field(default_factory=dict)
    refs: dict[str, list[Part]] = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    # The data sources 1, 2, ... in order of first use (§1.2), and their indexes.
    data: dict[bytes, int] = field(default_factory=dict)

    def json(self, key: str, value) -> None:
        self.bytes_entries[key] = json.dumps(value, indent=2).encode()

    def shared(self, value: bytes) -> tuple[int, int, int]:
        """A range of all of `value`, as a data source (§1.2): the source is
        added to the table the first time `value` is used."""
        value = bytes(value)
        i = self.data.setdefault(value, len(self.data) + 1)
        return (i, 0, len(value))

    def write(self, out_path: str) -> None:
        with open(out_path, "wb") as fh:
            w = VZipWriter(fh, page_size=1 << 16)
            src = w.source(Source(url=self.url))
            for value, i in self.data.items():
                if w.blob(value) != i:
                    raise AssertionError("data sources out of order")

            def rng(r: Part) -> Range:
                if isinstance(r, bytes):
                    return Range(data=r)
                if len(r) == 3:
                    return Range(source=r[0], offset=r[1], length=r[2])
                return Range(source=src, offset=r[0], length=r[1])

            for key in sorted(self.refs):
                w.add_ranges(key, [rng(r) for r in self.refs[key]])
            for key in sorted(self.bytes_entries):
                w.add_bytes(key, self.bytes_entries[key], compress=not key.endswith("zarr.json"),
                            late=key.endswith("zarr.json"))
            w.close()


MAX_PAYLOAD = 65519


MAX_SAFE = 2**53 - 1


def json_number(v):
    """A number as source metadata (conventions/README.md §6)."""
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if math.isinf(v):
            return "Infinity" if v > 0 else "-Infinity"
        return v
    return v if abs(v) <= MAX_SAFE else str(v)


def decode_text(b: bytes) -> str:
    """Bytes as text: UTF-8 if valid, else ISO 8859-1 (conventions/README.md §6)."""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1")


def json_text(b: bytes) -> str:
    """A fixed-size character field: its bytes up to the first NUL, as text."""
    return decode_text(b.split(b"\0", 1)[0])


def json_base64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _varint_size(v: int) -> int:
    return max(1, (v.bit_length() + 6) // 7)


def _range_size(r: Part) -> int:
    if isinstance(r, bytes):
        return 1 + _varint_size(len(r)) + len(r)
    source, offset, length = r if len(r) == 3 else (0, *r)
    return sum(1 + _varint_size(v) for v in (source, offset, length) if v)


def payload_size(ranges: list[Part]) -> int:
    """The encoded size of a reference to `ranges` (§1.2)."""
    if len(ranges) == 1:
        return _range_size(ranges[0])
    return sum(1 + _varint_size(n) + n for n in map(_range_size, ranges))


UNITS = {
    "µm": "micrometer", "μm": "micrometer", "um": "micrometer", "nm": "nanometer",
    "mm": "millimeter", "cm": "centimeter", "m": "meter", "\u00c5": "angstrom", "\u212b": "angstrom",
    "pm": "picometer",
    "in": "inch", "ft": "foot", "s": "second", "ms": "millisecond", "min": "minute", "h": "hour",
}

TYPES = {"t": "time", "c": "channel", "z": "space", "y": "space", "x": "space"}

# Sizes of the length units in metres (conventions §5).
LENGTHS = {
    "micrometer": 1e-6, "nanometer": 1e-9, "millimeter": 1e-3, "centimeter": 1e-2, "meter": 1.0,
    "angstrom": 1e-10, "picometer": 1e-12, "inch": 0.0254, "foot": 0.3048,
}


def centred(cx: float, cy: float, w0: int, h0: int, sx: float, sy: float) -> dict:
    """The translation of an image whose centre is at (cx, cy) (conventions §5)."""
    return {"x": cx - w0 * sx / 2, "y": cy - h0 * sy / 2}


def array_json(shape, data_type: str, chunk_shape, codecs: list, axes: list[str]) -> dict:
    """An array's zarr.json (conventions §3)."""
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
    """The transpose codec for frames that hold the channel axis last (conventions §3)."""
    stored = [a for a in axes if a != "c"] + ["c"]
    return {"name": "transpose", "configuration": {"order": [axes.index(a) for a in stored]}}


def group_json(ome: dict) -> dict:
    return {"zarr_format": 3, "node_type": "group", "attributes": {"ome": ome}}


# The virtualization conventions (conventions §2): one per profile, each with its fixed
# UUID, its current version and its name in the description.
CONVENTION_KEY = "vzip_virtualized"
PROFILES = {
    "tiff": ("48e9ac4e-1156-4a62-955e-20467d9c2700", 1, "TIFF"),
    "ndpi": ("6cac71ef-dbb2-4acd-b60c-00389aa4238a", 1, "NDPI"),
    "nd2": ("59612f14-e314-4207-ba00-8f422ba71490", 1, "ND2"),
    "dicom": ("acf17198-e5a5-48d3-8187-22ec4bb40ea5", 1, "DICOM"),
    "nifti": ("06e5809d-4d54-4b72-afd0-6bf61a7b4c85", 1, "NIfTI"),
    "ims": ("5067a535-8261-4b25-a93c-1985ed333bde", 1, "IMS"),
    "n5": ("ad5d4c39-c69e-48f7-a3ef-4cc8c607d416", 1, "N5"),
    "zarr2": ("8e792619-d671-4687-ab51-752885dd3ee6", 1, "Zarr v2"),
    "ome-zarr": ("b74ea302-65bb-49ae-b81f-f9bb52cd4eed", 1, "OME-Zarr"),
}
UUIDS = frozenset(uuid for uuid, _, _ in PROFILES.values())


def convention(profile: str) -> dict:
    """The Convention Metadata Object of a profile's convention (conventions §2)."""
    uuid, version, title = PROFILES[profile]
    tag = f"virtualize-{profile}-v{version}"
    return {
        "uuid": uuid,
        "schema_url": f"https://raw.githubusercontent.com/d-v-b/vzip/refs/tags/{tag}/conventions/{profile}/schema.json",
        "spec_url": f"https://github.com/d-v-b/vzip/blob/{tag}/conventions/{profile}/README.md",
        "name": CONVENTION_KEY,
        "description": f"The Zarr layout of a {title} source virtualized by vzip, and the source's metadata",
    }


def declare(attributes: dict, profile: str, url: str | None, own: dict | None = None) -> dict:
    """A node's attributes (conventions §2): `attributes`, the members the target formats
    define (such as `ome`), and the profile's convention when the node is the
    root (`url`, the source URL, is given) or has source-specific metadata
    (`own` is a nonempty object): its metadata object in `zarr_conventions`,
    and the property `vzip_virtualized`, which holds `own` as its member
    named after the profile."""
    value: dict = {}
    if url is not None:
        value = {"profile": profile, "version": PROFILES[profile][1], "source": {"url": url}}
    if own:
        value[profile] = own
    if not value:
        return dict(attributes)
    return {**attributes, "zarr_conventions": [convention(profile)], CONVENTION_KEY: value}


def root_json(ome: dict, profile: str, url: str, own: dict | None = None) -> dict:
    """The root image group of a file profile (conventions §4, conventions §2), with the
    source-specific metadata `own`."""
    return {"zarr_format": 3, "node_type": "group", "attributes": declare({"ome": ome}, profile, url, own)}


def image_ome(axes: list[str], units: dict, scales: list[list[float]], name: str | None,
              translations: list[list[float]] | None = None) -> dict:
    for t in translations or []:
        if not all(math.isfinite(v) for v in t):
            raise Rejected("a translation is not finite")
    """The OME-NGFF 0.5 object of an image (conventions §4), with a translation per
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
