"""Checks the browser TIFF virtualizer against tifffile.

Each supported fixture in web/test/fixtures/tiff/ (or, given as an argument,
another directory) is virtualized by web/conformance/virtualize_file.ts (the browser code, run under Node). The archive is
then read by the reference reader (src/vzip) and zarr-python, and every
pyramid level must equal what tifffile reads from the TIFF directly. The
unsupported fixtures must be refused.

Usage: uv run python web/test/tiff/verify.py [<fixture directory>]
"""

from __future__ import annotations

import base64
import json
import logging
import math
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import zlib
from dataclasses import dataclass

import numpy as np
import tifffile
import zarr
from zarr.abc.codec import BytesBytesCodec
from zarr.registry import register_codec

import vzip.codecs  # noqa: F401  (registers imagecodecs_jpeg2k)
from vzip.policy import Policy  # noqa: E402
from vzip.store import VZipStore

sys.path.insert(0, str(Path(__file__).parents[1]))
from ir_mirror import is_mirror, mirror_problems  # noqa: E402

# The sources are served on 127.0.0.1, which spec §8.7 rule 3 refuses by default.
LOOPBACK_SOURCES = Policy(allow_private_hosts=True)


@dataclass(frozen=True)
class ZlibCodec(BytesBytesCodec):
    """`zlib` (as Neuroglancer names it), which zarr-python lacks."""

    is_fixed_size = False
    level: int = 6

    @classmethod
    def from_dict(cls, data):
        return cls(**data.get("configuration", {}))

    def to_dict(self):
        return {"name": "zlib", "configuration": {"level": self.level}}

    async def _decode_single(self, chunk_bytes, chunk_spec):
        return chunk_spec.prototype.buffer.from_bytes(zlib.decompress(chunk_bytes.to_bytes()))

    async def _encode_single(self, chunk_bytes, chunk_spec):
        return chunk_spec.prototype.buffer.from_bytes(zlib.compress(chunk_bytes.to_bytes()))

    def compute_encoded_size(self, input_byte_length, chunk_spec):
        raise NotImplementedError


register_codec("zlib", ZlibCodec)
logging.getLogger("tifffile").setLevel(logging.CRITICAL)  # its warnings on the odd fixtures

HERE = Path(__file__).parent
CLI = ["node", str(HERE.parents[1] / "conformance" / "virtualize_file.ts")]
AXIS = {"T": "t", "C": "c", "S": "c", "Z": "z", "Y": "y", "X": "x"}


def expected_levels(path: Path) -> list[tuple[np.ndarray, str]]:
    with tifffile.TiffFile(path) as tf:
        series = tf.series[0]
        if path.name.startswith("svs_like"):
            return [(tf.pages[i].asarray(), "YX") for i in (0, 2)]
        if path.suffix == ".ndpi":  # the levels: pages with a positive magnification (65421)
            return [(p.asarray(), "YXS") for p in tf.pages if p.tags[65421].value > 0]
        return [(level.asarray(), series.axes) for level in series.levels]


TYPES = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 16, 17, 18}
STRUCTURE = {273, 279, 288, 289, 324, 325, 330, 513, 514, 519, 520, 521, 34665, 34853, 40965, 65426, 65432}


def as_json(tag):
    """tifffile's value of a tag, as conventions/tiff/README.md §5 writes it."""
    v = tag.value
    if tag.dtype in (1, 7):
        b = v if isinstance(v, bytes) else bytes(v if isinstance(v, tuple) else [v])
        return base64.b64encode(b).decode()
    if tag.dtype == 2:
        return v
    flat = list(v) if isinstance(v, (tuple, list)) else [v]
    if tag.dtype in (5, 10):
        return [[int(flat[2 * i]), int(flat[2 * i + 1])] for i in range(len(flat) // 2)]

    def number(x):
        if isinstance(x, float) and not math.isfinite(x):
            return "NaN" if math.isnan(x) else "Infinity" if x > 0 else "-Infinity"
        return float(x) if tag.dtype in (11, 12) else int(x)
    return [number(x) for x in flat]


def image_offsets(path: Path, tf) -> set[int]:
    """The offsets of the IFDs that are images of the hierarchy (pyramid levels), as
    tifffile finds them: their strips or tiles are the arrays, not source metadata."""
    if path.name.startswith("svs_like"):
        return {tf.pages[i].offset for i in (0, 2)}
    if path.suffix == ".ndpi":
        return {p.offset for p in tf.pages if p.tags[65421].value > 0}
    try:
        return {p.offset for level in tf.series[0].levels for p in level.pages if p is not None}
    except Exception:  # noqa: BLE001  (OME-XML that tifffile does not read: any main-chain IFD or SubIFD)
        return {o for p in tf.pages for o in (p.offset, *(s.offset for s in p.pages or []))}


def raw_entries(f, offset: int, big: bool, order: str) -> dict[int, tuple[int, int, bytes]]:
    """The first entry of each tag of the IFD at `offset`, as (type, count, value field),
    read directly: only to find the offsets that pointer tags hold (NDPI's high words,
    0 in the fixtures, are not read)."""
    count_size, entry_size, field_size = (8, 20, 8) if big else (2, 12, 4)
    f.seek(offset)
    n = struct.unpack(order + ("Q" if big else "H"), f.read(count_size))[0]
    body = f.read(n * entry_size)
    out = {}
    for i in range(n):
        e = body[i * entry_size:(i + 1) * entry_size]
        tag, typ = struct.unpack(order + "HH", e[:4])
        count = struct.unpack(order + ("Q" if big else "I"), e[4:12] if big else e[4:8])[0]
        out.setdefault(tag, (typ, count, e[entry_size - field_size:]))
    return out


def pointer_offsets(f, entry: tuple[int, int, bytes], big: bool, order: str) -> list[int]:
    typ, count, field = entry
    size = {1: 1, 3: 2, 4: 4, 13: 4, 16: 8, 18: 8}[typ]
    fmt = {1: "B", 3: "H", 4: "I", 13: "I", 16: "Q", 18: "Q"}[typ]
    if count * size <= len(field):
        data = field[:count * size]
    else:
        f.seek(struct.unpack(order + ("Q" if big else "I"), field)[0])
        data = f.read(count * size)
    return list(struct.unpack(order + fmt * count, data))


def tag_problems(path: Path, root, key: str) -> list[str]:
    """Reconstruction (conventions/tiff/README.md §5): every tag of every IFD recorded
    (the main chain, and the IFDs that SubIFDs and the other pointer tags lead to) but
    the layout and pointer tags, in JSON or in an array, on the IFD's group, against
    tifffile's reading of that IFD; each pointer value leads to the IFD at its offset;
    and the strips or tiles of every IFD that is not an image are present."""
    problems = []
    node = root["vzip_source"]
    source = node.attrs["vzip_virtualized"][key]

    def meta(p):
        return node[p].attrs["vzip_virtualized"][key]

    def compare(where, ifd, page, path):
        theirs = {}
        for t in page.tags:
            theirs.setdefault(t.code, t)
        unknown = {int(k) for k, v in ifd["tags"].items() if v["type"] not in TYPES}
        problems.extend(f"{where} tag {k}: unknown type has a value" for k in unknown if "value" in ifd["tags"][str(k)])
        pointers = ifd.get("pointers", {})
        expected = {c for c in theirs if c not in STRUCTURE and str(c) not in pointers}
        if set(map(int, ifd["tags"])) - unknown != expected:
            problems.append(f"{where}: tags {sorted(map(int, ifd['tags']))} vs {sorted(expected)}")
            return
        for code in expected:
            t = theirs[code]
            ours = ifd["tags"][str(code)]
            if (ours["type"], ours["count"]) != (int(t.dtype), t.count):
                problems.append(f"{where} tag {code}: type/count {ours['type']}/{ours['count']}")
                continue
            if "value" not in ours:  # large: in an array
                a = node[f"{path}/{code}"][...]
                got = a.tobytes().decode("latin-1") if t.dtype == 2 else None
                want = as_json(t)
                if t.dtype == 2:
                    if t.value not in (got.rstrip("\0"), got.split("\0")[0]):
                        problems.append(f"{where} tag {code}: array text differs")
                elif t.dtype in (1, 7):
                    if base64.b64encode(a.tobytes()).decode() != want:
                        problems.append(f"{where} tag {code}: array bytes differ")
                elif a.tolist() != want:
                    problems.append(f"{where} tag {code}: array values differ")
            elif t.dtype == 2:
                v = ours["value"]
                v = v["latin1"] if isinstance(v, dict) else v  # text that is not UTF-8, tagged
                if t.value not in (v, v.split("\0")[0]):
                    problems.append(f"{where} tag {code}: {ours.get('value')!r:.60} vs {t.value!r:.60}")
            elif t.dtype in (1, 7):
                if base64.b64encode(bytes(ours["value"])).decode() != as_json(t):
                    problems.append(f"{where} tag {code}: bytes differ")
            elif ours["value"] != as_json(t):
                problems.append(f"{where} tag {code}: {str(ours['value']):.60} vs {str(as_json(t)):.60}")

    def data(where, page, path, f, size, ifd, name="data", offsets=None, counts=None):
        """The IFD's strips or tiles, which MUST be the family <path>/<name> if any is present,
        or the earlier one that its same_as names."""
        offsets = page.dataoffsets if offsets is None else offsets
        counts = page.databytecounts if counts is None else counts
        members = [(i, o, n) for i, (o, n) in enumerate(zip(offsets, counts)) if n > 0 and o + n <= size]
        if not members:
            return
        family = ifd.get("same_as", {}).get(name, f"{path}/{name}")
        if family not in node:
            problems.append(f"{where}: no {name} for its {len(members)} strips or tiles")
            return
        a = node[family]
        if isinstance(a, zarr.Group):  # members of several lengths: offsets and data (conventions §7)
            starts, joined = a["offsets"][...], a["data"][...].tobytes()
        for i, o, n in members:
            f.seek(o)
            raw = f.read(n)
            got = a[i].tobytes() if not isinstance(a, zarr.Group) else joined[starts[i]:starts[i + 1]]
            if got != raw:
                problems.append(f"{where}: strip or tile {i} differs")
                return

    records = {}  # record number -> path, for the pointer tags whose IFDs are an array
    for name, member in node.members(max_depth=None):
        if isinstance(member, zarr.Group):
            r = member.attrs.get("vzip_virtualized", {}).get(key, {}).get("record")
            if r is not None:
                records[r] = name

    with tifffile.TiffFile(path) as tf, open(path, "rb") as f:
        if source["ifd_count"] != len(tf.pages):
            return [f"{source['ifd_count']} IFDs, tifffile has {len(tf.pages)}"]
        images = image_offsets(path, tf)
        tf.pages.useframes = False  # every IFD as a TiffPage, with its tags
        tf.pages._clear(fully=True)
        size = Path(path).stat().st_size
        big, order = tf.is_bigtiff, tf.byteorder
        at = {f"ifds/{i}": page.offset for i, page in enumerate(tf.pages)}  # path -> offset
        links = []  # (where, path, offset): a pointer value that leads to an IFD recorded elsewhere

        def visit(where, path, page):
            ifd = meta(path)
            compare(where, ifd, page, path)
            if page.offset not in images:
                data(where, page, path, f, size, ifd)
            codes = {t.code: t for t in page.tags}
            if {273, 279, 324, 325} <= set(codes):  # a tiled IFD's strips
                data(where, page, path, f, size, ifd, "strips", codes[273].value, codes[279].value)
            raw = raw_entries(f, page.offset, big, order)
            for code, p in ifd.get("pointers", {}).items():
                array = ifd.get("same_as", {}).get(code, f"{path}/{code}")
                offsets = pointer_offsets(f, raw[int(code)], big, order) if "ifds" in p or array in node else []
                if "ifds" in p:
                    paths = p["ifds"]
                elif array in node:
                    paths = [None if r < 0 else records.get(r, f"<record {r}>") for r in node[array][...]]
                else:
                    continue
                if len(paths) != len(offsets):
                    problems.append(f"{where} pointer {code}: {len(paths)} IFDs for {len(offsets)} values")
                    continue
                name = {"330": "subifds", "34665": "exif", "34853": "gps", "40965": "interoperability"}.get(
                    code, f"ifd_{code}")
                for j, (child, o) in enumerate(zip(paths, offsets)):
                    new = f"{path}/{name}/{j}" if code == "330" or len(offsets) > 1 else f"{path}/{name}"
                    if child is None:
                        continue
                    if child != new:
                        links.append((f"{where} pointer {code}[{j}]", child, o))
                        continue
                    at[child] = o
                    tf.filehandle.seek(o)
                    try:
                        sub = tifffile.TiffPage(tf, index=0)
                    except Exception as e:  # noqa: BLE001
                        problems.append(f"{where} pointer {code}[{j}]: tifffile cannot read it: {e}")
                        continue
                    visit(f"{where} {name} {j}", child, sub)


        for i, page in enumerate(tf.pages):
            visit(f"IFD {i}", f"ifds/{i}", page)
        for where, child, o in links:
            if at.get(child) != o:
                problems.append(f"{where}: leads to {child}, which is not the IFD at {o}")
    return problems


def main(fixtures: Path = HERE.parent / "fixtures" / "tiff") -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for tiff in sorted([*fixtures.glob("*.tif"), *fixtures.glob("*.ndpi")]):
            out = Path(tmp) / (tiff.stem + ".vzip")
            p = subprocess.run(CLI + [str(tiff), str(out), tiff.resolve().as_uri()],
                               capture_output=True, text=True)
            if p.returncode == 0:
                if is_mirror(out):  # the IR's mirror (TIFF): the source rebuilt from it
                    tags = mirror_problems(out, tiff)
                else:
                    root = zarr.open_group(VZipStore(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
                    tags = tag_problems(tiff, root, "ndpi" if tiff.suffix == ".ndpi" else "tiff")
                failures += bool(tags)
                if tags:
                    print(f"{tiff.name:42s} TAGS: {tags[:5]}")
            if tiff.name.startswith("edge_") and not tiff.name.startswith("edge_reject"):
                continue  # compared between implementations by conformance/virtualize/compare.py
            if tiff.name.startswith(("unsupported", "edge_reject")):
                ok = p.returncode == 1 and not out.exists()
                failures += not ok
                print(f"{tiff.name:42s} {'refused: ' + p.stderr.strip() if ok else 'NOT REFUSED'}")
                continue
            if p.returncode:
                print(f"{tiff.name:42s} FAILED: {p.stderr.strip()[-300:]}")
                failures += 1
                continue
            summary = json.loads(p.stdout)
            group = zarr.open_group(VZipStore(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
            ms = group.attrs["ome"]["multiscales"][0]
            names = [a["name"] for a in ms["axes"]]
            problems = []
            want = expected_levels(tiff)
            if len(want) != len(ms["datasets"]):
                problems.append(f"{len(ms['datasets'])} levels, tifffile has {len(want)}")
            for (data, axes), d in zip(want, ms["datasets"]):
                got = group[d["path"]][...]
                # tifffile's axes, renamed and reordered to ours.
                letters = [AXIS[a] for a in axes]
                data = np.transpose(data, [letters.index(n) for n in names])
                if got.dtype != data.dtype or not np.array_equal(got, data):
                    problems.append(f"level {d['path']} differs ({got.dtype} {got.shape} vs "
                                    f"{data.dtype} {data.shape})")
            failures += bool(problems)
            print(f"{tiff.name:42s} {'ok' if not problems else problems} "
                  f"axes={''.join(names)} levels={summary['levels']} codec={summary.get('codec', '-')}")
    print(f"\n{failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(*map(Path, sys.argv[1:2])))
