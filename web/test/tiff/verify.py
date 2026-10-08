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
import math
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
from vzip.store import VZipStore


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
STRUCTURE = {273, 279, 288, 289, 324, 325, 330, 513, 514, 34665, 34853, 40965, 65426, 65432}


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


def tag_problems(path: Path, source: dict) -> list[str]:
    """Every tag of every IFD and SubIFD against tifffile's."""
    problems = []

    def compare(where, ifd, page):
        theirs = {}
        for t in page.tags:
            theirs.setdefault(t.code, t)
        # A field type TIFF does not define is recorded by type and count only;
        # tifffile drops such tags.
        unknown = {int(k) for k, v in ifd["tags"].items() if v["type"] not in TYPES}
        problems.extend(f"{where} tag {k}: unknown type has a value" for k in unknown if "value" in ifd["tags"][str(k)])
        if set(map(int, ifd["tags"])) - unknown != set(theirs):
            problems.append(f"{where}: tags {sorted(map(int, ifd['tags']))} vs {sorted(theirs)}")
            return
        for code, t in theirs.items():
            ours = ifd["tags"][str(code)]
            if (ours["type"], ours["count"]) != (int(t.dtype), t.count):
                problems.append(f"{where} tag {code}: type/count {ours['type']}/{ours['count']}")
            elif code in STRUCTURE:
                if "value" in ours:
                    problems.append(f"{where} tag {code}: structure tag has a value")
            elif t.dtype == 2:
                if t.value not in (ours.get("value"), ours.get("value", "").split("\0")[0]):
                    problems.append(f"{where} tag {code}: {ours.get('value')!r:.60} vs {t.value!r:.60}")
            elif ours.get("value") != as_json(t):
                problems.append(f"{where} tag {code}: {str(ours.get('value')):.60} vs {str(as_json(t)):.60}")

    with tifffile.TiffFile(path) as tf:
        if len(source["ifds"]) != len(tf.pages):
            return [f"{len(source['ifds'])} IFDs, tifffile has {len(tf.pages)}"]
        for i, (ifd, page) in enumerate(zip(source["ifds"], tf.pages)):
            compare(f"IFD {i}", ifd, page)
            subs = page.pages if 330 in page.tags else []
            for j, sub in enumerate(ifd.get("subifds", [])):
                compare(f"IFD {i} SubIFD {j}", sub, subs[j])
    return problems


def main(fixtures: Path = HERE.parent / "fixtures" / "tiff") -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for tiff in sorted([*fixtures.glob("*.tif"), *fixtures.glob("*.ndpi")]):
            out = Path(tmp) / (tiff.stem + ".vzip")
            p = subprocess.run(CLI + [str(tiff), str(out), tiff.resolve().as_uri()],
                               capture_output=True, text=True)
            if p.returncode == 0:
                prop = zarr.open_group(VZipStore(str(out)), mode="r", zarr_format=3).attrs["vzip_virtualized"]
                source = prop["ndpi" if tiff.suffix == ".ndpi" else "tiff"]
                tags = tag_problems(tiff, source)
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
            group = zarr.open_group(VZipStore(str(out)), mode="r", zarr_format=3)
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
                  f"axes={''.join(names)} levels={summary['levels']} codec={summary['codec']}")
    print(f"\n{failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(*map(Path, sys.argv[1:2])))
