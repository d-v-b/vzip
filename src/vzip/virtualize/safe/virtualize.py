"""The SAFE profile (profiles/safe.md, §12): the output of a Sentinel-2 product,
in the directory form (a store input) or the zip form (a file input)."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from vzip.virtualize.common import (
    CONVENTION_KEY, MAX_PAYLOAD, PROFILES, Reader, root_property, Rejected, SOURCE_NODE, blob_chunks, convention, declare,
    json_base64, payload_size, text_json,
)
from vzip.virtualize.safe import jp2, zipdir
from vzip.virtualize.safe.metadata import (
    band_metadata, choose_texts, group_attributes, root_geozarr, root_metadata,
)
from vzip.virtualize.safe.product import band_name, folders, is_xml, read_product, resolution
from vzip.virtualize.store import Store, object_key, object_url

PROFILE = "safe"
WORKERS = 16


# ---------------------------------------------------------------- the product's objects (§12.2, §12.4)


class Objects:
    """A product's objects, in either form: their sizes, and how to read them and
    reference them."""

    form: str
    sizes: dict[str, int]
    ignored: list[str]
    directories: list[str]  # keys ending in `/`: a zip file's directory entries, a listing's empty `d/`
    listing_requests: int = 0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reads = [0, 0]  # requests, bytes
        self._whole: dict[str, bytes] = {}

    def count(self, n: int) -> None:
        with self._lock:
            self.reads[0] += 1
            self.reads[1] += n

    def whole(self, key: str) -> bytes:
        if key not in self._whole:
            self._whole[key] = self._read_whole(key)
        return self._whole[key]

    def _read_whole(self, key: str) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def raw(self, key: str):  # pragma: no cover - interface
        """(read(offset, length) of a source, the object's offset in it), for range reads."""
        raise NotImplementedError

    def where(self, key: str) -> tuple[object, int]:  # pragma: no cover - interface
        """(source, offset) of the object's bytes: its key for its own url source, or 0."""
        raise NotImplementedError

    def copied(self, key: str) -> bytes | None:
        """The bytes of an object that the output copies (a deflated XML document), else None."""
        return None


class StoreObjects(Objects):
    form = "store"

    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store
        self.sizes = dict(store.objects)
        self.ignored = list(store.ignored)
        self.directories = list(getattr(store, "folders", []))
        self.listing_requests = store.requests

    def _read_whole(self, key: str) -> bytes:
        self.count(self.sizes[key])
        return self.store.read(key)

    def raw(self, key: str):
        def read(offset: int, length: int) -> bytes:
            self.count(length)
            return self.store.read_range(key, offset, length)

        return read, 0

    def where(self, key: str) -> tuple[object, int]:
        return key, 0


class ZipObjects(Objects):
    form = "zip"

    def __init__(self, read: Reader, size: int) -> None:
        super().__init__()
        self.read = read
        d = zipdir.central_directory(read, size)
        _, self.entries, self.ignored, self.directories = zipdir.product_entries(d)
        self.sizes = {k: e.us for k, e in self.entries.items()}
        self.deflated: set[str] = set()
        for k, e in self.entries.items():
            if not e.us:
                continue
            if e.method == 0:
                if e.cs != e.us:
                    raise Rejected(f"the stored zip entry {k[:200]} has a compressed size other than its size")
            elif e.method == 8:
                if not is_xml(k):
                    raise Rejected(f"the zip entry {k[:200]} is compressed: only XML documents may be")
                if e.us > zipdir.MAX_DEFLATED:
                    raise Rejected(f"the deflated XML document {k[:200]} is larger than 2^26 bytes")
                self.deflated.add(k)
            else:
                raise Rejected(f"the zip entry {k[:200]} has the compression method {e.method}")
        if sum(self.entries[k].us for k in self.deflated) > zipdir.MAX_INFLATED:
            raise Rejected("the deflated XML documents are larger than 2^27 bytes together")
        zipdir.locate(read, d, self.entries)

    def _inflate(self, key: str) -> bytes:
        """A deflated entry's bytes, inflated when they are needed, one entry at a time."""
        e = self.entries[key]
        return zipdir.inflate(self.read(e.ds, e.cs), e)

    def _read_whole(self, key: str) -> bytes:
        if key in self.deflated:
            return self._inflate(key)
        e = self.entries[key]
        return self.read(e.ds, e.us)

    def raw(self, key: str):
        uncached = getattr(self.read, "uncached", self.read)

        def read(offset: int, length: int) -> bytes:
            return uncached(offset, length)

        return read, self.entries[key].ds

    def where(self, key: str) -> tuple[object, int]:
        return 0, self.entries[key].ds

    def copied(self, key: str) -> bytes | None:
        if key not in self.deflated:
            return None
        return self._whole[key] if key in self._whole else self._inflate(key)


# ---------------------------------------------------------------- the output (§12.8)


@dataclass
class Shared:
    """A shared byte string: a data source (§1.2)."""

    value: bytes


@dataclass
class SafeOutput:
    """The SAFE profile's output: the JSON documents, copied bytes, and the
    references, whose ranges are literals, `(source, offset, length)` with the
    source an object's key (directory form) or 0 (zip form), or Shared."""

    url: str
    form: str
    docs: dict[str, dict] = field(default_factory=dict)
    bytes_entries: dict[str, bytes] = field(default_factory=dict)
    refs: dict[str, list] = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    # The url sources' size pins (VIRTUALIZE.md §1.2, §1.4): the zip file's size and
    # ETag (set by vzip.virtualize.virtualize), or each object's listed size; and the
    # CRC-32C of a url source's bytes, when ranges are to carry checksums (SPEC.md §5.2).
    size: int | None = None
    etag: str | None = None
    object_sizes: dict[str, int] = field(default_factory=dict)
    checksum: Callable[[str, int, int], int] | None = None

    def table(self) -> tuple[list[str], list[bytes], dict[str, list]]:
        """The url sources, the data sources, and the references with numbered sources (§12.8)."""
        keys = sorted(self.refs)
        if self.form == "zip":
            urls = [self.url]
            index = {0: 0}
        else:
            objs = sorted({r[0] for k in keys for r in self.refs[k] if isinstance(r, tuple)})
            urls = [object_url(self.url, o) for o in objs]
            index = {o: i for i, o in enumerate(objs)}
        data: dict[bytes, int] = {}
        out = {}
        for k in keys:
            ranges = []
            for r in self.refs[k]:
                if isinstance(r, Shared):
                    i = data.setdefault(r.value, len(urls) + len(data))
                    ranges.append((i, 0, len(r.value)))
                elif isinstance(r, tuple):
                    ranges.append((index[r[0]], r[1], r[2]))
                else:
                    ranges.append(r)
            out[k] = ranges
        return urls, list(data), out

    def write(self, out_path: str) -> None:
        from vzip.archive import VZipWriter
        from vzip.pb import Range, Source

        urls, data, refs = self.table()
        if self.form == "zip":
            pins = [{"size": self.size, "etag": self.etag}]
        else:
            by_url = {object_url(self.url, k): n for k, n in self.object_sizes.items()}
            pins = [{"size": by_url.get(u)} for u in urls]
        with open(out_path, "wb") as fh:
            w = VZipWriter(fh, page_size=1 << 16, checksum=self.checksum)
            for i, u in enumerate(urls):
                if w.source(Source(url=u, **pins[i])) != i:
                    raise AssertionError("url sources out of order")
            for i, value in enumerate(data):
                if w.blob(value) != len(urls) + i:
                    raise AssertionError("data sources out of order")
            for key, ranges in refs.items():
                w.add_ranges(key, [Range(data=r) if isinstance(r, bytes) else
                                   Range(source=r[0], offset=r[1], length=r[2]) for r in ranges])
            for key in sorted(self.bytes_entries):
                w.add_bytes(key, self.bytes_entries[key], compress=True)
            for key in sorted(self.docs):
                # The hierarchy's documents are read when the archive opens; vzip_source's only when asked for.
                late = not key.startswith(SOURCE_NODE + "/")
                body = json.dumps(self.docs[key], separators=(",", ":"), ensure_ascii=False).encode()
                w.add_bytes(key, body, compress=not late, late=late)
            w.close()


def _group(attributes: dict | None = None) -> dict:
    return {"zarr_format": 3, "node_type": "group", "attributes": attributes or {}}


def _bytes_array(n: int, size: int) -> dict:
    """The zarr.json of an array of `n` bytes in chunks of `size` (conventions §7, Bytes)."""
    return {"zarr_format": 3, "node_type": "array", "shape": [n], "data_type": "uint8",
            "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [size]}},
            "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
            "fill_value": 0, "codecs": [{"name": "bytes"}], "dimension_names": ["byte"], "attributes": {}}


def _referenced(out: SafeOutput, path: str, objects: Objects, key: str, start: int, n: int) -> None:
    """The array at `path` of the bytes [start, start + n) of the object `key`, referenced."""
    src, base = objects.where(key)
    size, chunks = blob_chunks(base + start, n)
    out.docs[f"{path}/zarr.json"] = _bytes_array(n, size)
    for (i,), parts in chunks.items():
        out.refs[f"{path}/c/{i}"] = [(src, *p) if isinstance(p, tuple) else p for p in parts]


def _copied(out: SafeOutput, path: str, data: bytes) -> None:
    size, chunks = blob_chunks(0, len(data))
    out.docs[f"{path}/zarr.json"] = _bytes_array(len(data), size)
    for (i,) in chunks:
        piece = data[i * size : (i + 1) * size]
        out.bytes_entries[f"{path}/c/{i}"] = piece + bytes(size - len(piece))


# ---------------------------------------------------------------- the profile


@dataclass
class Band:
    image: object
    cs: jp2.Codestream
    resolution: int
    name: str


def virtualize_product(url: str, objects: Objects) -> SafeOutput:
    markers, empty_dirs = folders(objects.sizes, objects.directories)
    sizes = {k: n for k, n in objects.sizes.items() if k not in markers}
    product = read_product(sizes, objects.whole)

    def read(image):
        raw, base = objects.raw(image.key)
        cs, _ = jp2.read_band(raw, base, sizes[image.key])
        return cs

    with ThreadPoolExecutor(WORKERS) as pool:
        streams = list(pool.map(read, product.images))
    bands: list[Band] = []
    names: set[tuple[int, str]] = set()
    for image, cs in zip(product.images, streams):
        r = resolution(product.geocoding, cs.width, cs.height, image.key)
        name = band_name(image.basename, r)
        if (r, name) in names:
            raise Rejected(f"two band files at {r} m have the band name {name}")
        names.add((r, name))
        bands.append(Band(image, cs, r, name))

    band_keys = {b.image.key for b in bands}
    empty = sorted(k for k, n in sizes.items() if n == 0)
    xml_keys = sorted(k for k, n in sizes.items() if n and k not in band_keys and is_xml(k))
    others = sorted(k for k, n in sizes.items() if n and k not in band_keys and not is_xml(k))

    ignored = sorted(objects.ignored)
    with ThreadPoolExecutor(WORKERS) as pool:
        texts = choose_texts({k: sizes[k] for k in xml_keys},
                             lambda keys: list(pool.map(lambda k: text_json(objects.whole(k)), keys)),
                             {"empty": empty, "empty_dirs": empty_dirs, "ignored": ignored})
    in_text = set(texts)
    arrays = [k for k in xml_keys if k not in in_text]

    out = SafeOutput(url, objects.form)
    if objects.form != "zip":
        out.object_sizes = dict(objects.sizes)
    resolutions = sorted({b.resolution for b in bands})
    cmos, geo_members = root_geozarr(product, resolutions)
    out.docs["zarr.json"] = _group({
        "zarr_conventions": [convention(PROFILE), *cmos],
        CONVENTION_KEY: {**root_property(PROFILE, url), PROFILE: root_metadata(product)},
        **geo_members})
    for r in resolutions:
        out.docs[f"r{r}m/zarr.json"] = _group(group_attributes(product.geocoding, r))

    chunks = edge = tile_parts = 0
    for b in bands:
        cs, path = b.cs, f"r{b.resolution}m/{b.name}"
        three = cs.components == 3
        shape = [cs.height, cs.width]
        chunk_shape = [cs.tile_h, cs.tile_w]
        codecs = [{"name": "imagecodecs_jpeg2k"}]
        if three:
            shape, chunk_shape = [3, *shape], [3, *chunk_shape]
            codecs = [{"name": "transpose", "configuration": {"order": [1, 2, 0]}}, *codecs]
        s = band_metadata(product, b.image.text, b.name, json_base64(cs.siz))
        out.docs[f"{path}/zarr.json"] = {
            "zarr_format": 3, "node_type": "array", "shape": shape,
            "data_type": "uint8" if cs.precision <= 8 else "uint16",
            "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": chunk_shape}},
            "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
            "fill_value": 0, "codecs": codecs, "dimension_names": ["c", "y", "x"] if three else ["y", "x"],
            "attributes": declare({}, PROFILE, None, s)}
        src, base = objects.where(b.image.key)
        rest = Shared(cs.rest)
        nx, _ = cs.grid
        # The empty tiles are measured before they are made: all of a band's, at most its file's size.
        total = sum(jp2.tail_length(cs, t) for t in range(len(cs.tiles)))
        if total > cs.n:
            raise Rejected(f"the empty tiles of {b.image.key[:200]}'s chunks would take {total} bytes, "
                           f"more than the band file's {cs.n}")
        for t in range(len(cs.tiles)):
            head, sot, (o, n), tail, padded = jp2.chunk_parts(cs, t)
            ranges = [head, rest, sot, (src, base + o, n), Shared(tail) if padded else tail]
            out.refs[f"{path}/c/{'0/' if three else ''}{t // nx}/{t % nx}"] = ranges
            chunks += 1
            edge += padded
        tile_parts += len(cs.tiles)

    # vzip_source (conventions/safe/README.md §6.3).
    s = {}
    if in_text:
        s["xml"] = {k: texts[k] for k in sorted(in_text)}
    if arrays:
        s["xml_arrays"] = arrays
    if empty:
        s["empty"] = empty
    if empty_dirs:
        s["empty_dirs"] = empty_dirs
    if ignored:
        s["ignored"] = ignored
    out.docs[f"{SOURCE_NODE}/zarr.json"] = _group(declare({}, PROFILE, None, s))
    if arrays:
        out.docs[f"{SOURCE_NODE}/xml/zarr.json"] = _group()
        for i, k in enumerate(arrays):
            data = objects.copied(k)
            if data is not None:
                _copied(out, f"{SOURCE_NODE}/xml/{i}", data)
            else:
                _referenced(out, f"{SOURCE_NODE}/xml/{i}", objects, k, 0, sizes[k])
    out.docs[f"{SOURCE_NODE}/jp2/zarr.json"] = _group()
    for r in resolutions:
        out.docs[f"{SOURCE_NODE}/jp2/r{r}m/zarr.json"] = _group()
    for b in bands:
        _referenced(out, f"{SOURCE_NODE}/jp2/r{b.resolution}m/{b.name}", objects, b.image.key, 0, b.cs.c0)
    for k in others:
        src, base = objects.where(k)
        out.refs[object_key(k)] = [(src, base, sizes[k])]

    for key in [*out.docs, *out.refs, *out.bytes_entries]:
        if len(key.encode()) > 65535:
            raise Rejected("an output key is longer than 65535 bytes")
        if key.startswith("__vz__/"):
            raise Rejected(f"output key {key[:200]!r} is in the reserved __vz__/ space")
    urls, data, refs = out.table()
    for key, ranges in refs.items():
        if payload_size(ranges) > MAX_PAYLOAD:
            raise Rejected(f"the reference payload of {key[:200]} is over {MAX_PAYLOAD} bytes")
    out.summary = {
        "level": product.level, "form": objects.form, "groups": len(resolutions), "bands": len(bands),
        "chunks": chunks, "edgeChunks": edge, "dataSources": len(data), "objects": len(objects.sizes),
        "folderMarkers": len(markers), "emptyDirs": len(empty_dirs), "xmlText": len(in_text), "xmlArrays": len(arrays),
        "otherObjects": len(others), "emptyObjects": len(empty), "tileParts": tile_parts,
        "listingRequests": objects.listing_requests, "readRequests": objects.reads[0], "readBytes": objects.reads[1],
    }
    return out


def virtualize_safe_store(store: Store) -> SafeOutput:
    """The directory form (§12): a listed `.SAFE` directory."""
    return virtualize_product(store.url, StoreObjects(store))


def virtualize_safe_zip(url: str, read: Reader, size: int) -> SafeOutput:
    """The zip form (§12.4): a `.SAFE.zip` file."""
    objects = ZipObjects(read, size)
    out = virtualize_product(url, objects)
    requests = getattr(read, "requests", None)
    if requests is not None:
        out.summary["readRequests"], out.summary["readBytes"] = requests
    else:
        del out.summary["readRequests"], out.summary["readBytes"]
    return out


def is_zip(head: bytes) -> bool:
    """A ZIP local file header's signature (§12.1)."""
    return head[:4] == zipdir.LOCAL
