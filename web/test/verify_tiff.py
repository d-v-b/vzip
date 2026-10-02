"""Checks the browser TIFF virtualizer against tifffile.

Each supported fixture in web/test/fixtures/ is virtualized by
web/conformance/tiff.ts (the browser code, run under Node). The archive is
then read by the reference reader (src/vzip) and zarr-python, and every
pyramid level must equal what tifffile reads from the TIFF directly. The
unsupported fixtures must be refused.

Usage: uv run python web/test/verify_tiff.py
"""

from __future__ import annotations

import json
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
CLI = ["node", str(HERE.parent / "conformance" / "tiff.ts")]
AXIS = {"T": "t", "C": "c", "S": "c", "Z": "z", "Y": "y", "X": "x"}


def expected_levels(path: Path) -> list[tuple[np.ndarray, str]]:
    with tifffile.TiffFile(path) as tf:
        series = tf.series[0]
        if path.name.startswith("svs_like"):
            return [(tf.pages[i].asarray(), "YX") for i in (0, 2)]
        return [(level.asarray(), series.axes) for level in series.levels]


def main() -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for tiff in sorted((HERE / "fixtures").glob("*.tif")):
            out = Path(tmp) / (tiff.stem + ".vzip")
            p = subprocess.run(CLI + [str(tiff), str(out), tiff.resolve().as_uri()],
                               capture_output=True, text=True)
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
    sys.exit(main())
