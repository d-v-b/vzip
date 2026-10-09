"""Checks the browser ND2 virtualizer's output on the synthetic ND2 files.

Each web/test/fixtures/nd2/*.nd2 is served over local HTTP and virtualized by
web/conformance/virtualize.ts (the browser code, run under Node). Every chunk
of the archive is read through the reference reader and zarr-python and must
equal the pixels write_fixtures.py wrote (<name>.npz); chunks absent from the
npz (missing frames) must read as the fill value. `nd2_reject_*` files must be
rejected.

Usage: uv run python web/test/nd2/verify.py
"""

from __future__ import annotations

import base64
import json
import math
import re
import struct
import subprocess
import sys
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import zarr
from zarr.abc.codec import BytesBytesCodec
from zarr.registry import register_codec

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "conformance"))
from http_server import Server  # noqa: E402

from vzip.policy import Policy  # noqa: E402
from vzip.store import VZipStore  # noqa: E402

# The sources are served on 127.0.0.1, which spec §8.7 rule 3 refuses by default.
LOOPBACK_SOURCES = Policy(allow_private_hosts=True)
sys.path.insert(0, str(HERE.parent))
from ir_mirror import is_mirror, mirror_problems  # noqa: E402


@dataclass(frozen=True)
class ZlibCodec(BytesBytesCodec):
    """`zlib` (as Neuroglancer names it), which zarr-python lacks."""

    is_fixed_size = False
    level: int = 1

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


def lv_norm(v):
    """The nd2 package's decoding of an LV value, as conventions/nd2/README.md §5 writes it."""
    if isinstance(v, dict):
        if v and all(re.fullmatch(r"i[0-9]{10}", k) for k in v):
            return [lv_norm(x) for x in v.values()]
        return {k: lv_norm(x) for k, x in v.items()}
    if isinstance(v, (bytes, bytearray)):
        return list(v)
    if isinstance(v, (list, tuple)):
        return [lv_norm(x) for x in v]
    if isinstance(v, float) and not math.isfinite(v):
        return "NaN" if math.isnan(v) else "Infinity" if v > 0 else "-Infinity"
    return v


TAGS = ("utf16", "int", "float")


def ours_norm(v):
    """Our JSON (conventions/nd2/README.md §5.1) as the nd2 package reads it: pairs
    as an object (first position, last value), or as a list when their names are empty;
    an int tag as its integer and a float tag as lv_norm writes the value (-0 as -0.0)."""
    if isinstance(v, dict) and len(v) == 1 and next(iter(v)) in ("int", "float"):
        (tag, x), = v.items()
        return int(x) if tag == "int" else -0.0 if x == "-0" else x
    if isinstance(v, list) and v and all(isinstance(x, list) and len(x) == 2 and isinstance(x[0], str) for x in v):
        if all(n == "" for n, _ in v):
            return [ours_norm(x) for _, x in v]
        out = {}
        for n, x in v:
            out[n] = ours_norm(x)
        return out
    if isinstance(v, list):
        return [ours_norm(x) for x in v]
    if isinstance(v, dict):
        return {k: ours_norm(x) for k, x in v.items()}
    return v


def text_bytes(t) -> bytes:
    """A text value's bytes (conventions/README.md §6)."""
    return t["latin1"].encode("latin-1") if isinstance(t, dict) else t.encode("utf-8")


def read_text(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1")


def node_path(name: str) -> str:
    return name[:-1].replace("|", "/") if name.endswith("!") else name.replace("|", "/")


def family_member(node, i: int) -> bytes | None:
    """Member `i` of a family of byte values (conventions/README.md §7)."""
    if hasattr(node, "dtype"):
        return bytes(node[i]) if i < node.shape[0] else None
    offsets = node["offsets"][...]
    if i + 1 >= len(offsets):
        return None
    return bytes(node["data"][offsets[i]:offsets[i + 1]]) if offsets[i + 1] > offsets[i] else b""


def frame_problems(root, loops: list[dict], frames: dict[int, bytes], attrs: dict) -> list[str]:
    """Each placed frame's pixels in the image, and its timestamp in ImageDataSeq,
    against its chunk's data (conventions/nd2/README.md §4.3, §5.3)."""
    h, w, comp = attrs["uiHeight"], attrs["uiWidth"], attrs["uiComp"]
    row, width_bytes = w * comp * attrs["uiBpcInMemory"] // 8, attrs["uiWidthBytes"]
    times = root["vzip_source/ImageDataSeq"] if frames else None
    problems = []
    for f, raw in sorted(frames.items()):
        coords, rest = {}, f
        for l in reversed(loops):
            c = rest % l["count"]
            coords[l["kind"]] = l["count"] - 1 - c if l.get("flip") else c
            rest //= l["count"]
        arr = root[f"{coords.get('p', 0)}/0"]
        axes = arr.metadata.dimension_names
        got = arr[tuple(coords.get(a, 0) if a in ("t", "z") else slice(None) for a in axes)]
        if attrs.get("eCompression", 2) == 0:
            data = zlib.decompress(raw[8:])
        else:
            data = b"".join(raw[8 + r * width_bytes:8 + r * width_bytes + row] for r in range(h))
        want = np.frombuffer(data, dtype=arr.dtype).reshape(h, w, comp)
        want = np.moveaxis(want, -1, 0) if "c" in axes else want[:, :, 0]
        if not np.array_equal(got, want):
            problems.append(f"frame {f}: pixels differ from its chunk")
        grid = tuple(coords[l["kind"]] for l in loops) or (0,)
        if struct.pack("<d", times[grid]) != raw[:8]:
            problems.append(f"frame {f}: timestamp differs from its chunk")
    return problems


def mirror_chunk_problems(path: Path, root, out: Path) -> list[str]:
    """An archive of the IR: the root's decoded chunks (conventions/nd2/README.md §5.1)
    against the nd2 package's decoder, and the source rebuilt from the mirror byte for
    byte (the frames' pixels are checked against the expected arrays)."""
    import nd2 as nd2lib

    problems = mirror_problems(out, path)
    decoded = root.attrs["vzip_virtualized"]["nd2"]["chunks"]
    with nd2lib.ND2File(path) as f:
        r = f._rdr
        for k in r.chunkmap:
            name = read_text(k)
            if name in decoded and (k.endswith(b"LV!") or b"LV|" in k):
                try:
                    theirs = lv_norm(r._decode_chunk(k, strip_prefix=False))
                except UnicodeDecodeError:
                    continue  # nd2 cannot decode unpaired surrogates, which ours keep
                if ours_norm(decoded[name]) != theirs:
                    problems.append(f"{name}: {str(decoded[name])[:60]} vs {str(theirs)[:60]}")
    return problems


def chunk_problems(path: Path, root) -> list[str]:
    """Reconstruction (conventions/nd2/README.md §5): every chunk of the map but
    the placed frames' pixels can be recovered from the hierarchy, against the nd2
    package's chunk map, loader and decoder."""
    import nd2 as nd2lib

    meta = root.attrs["vzip_virtualized"]["nd2"]
    src = root["vzip_source"] if "vzip_source" in root else None
    s = src.attrs["vzip_virtualized"]["nd2"] if src is not None and "vzip_virtualized" in src.attrs else {}
    decoded = {**meta["chunks"], **s.get("chunks", {})}

    def names(key: str) -> list:
        """A list of S, or, past the budget, the JSON text in the array it names."""
        v = s.get(key, [])
        return json.loads(bytes(src[v][...])) if isinstance(v, str) else v

    other = {text_bytes(t): i for i, t in enumerate(names("other"))}
    empty = {text_bytes(t) for t in names("empty")}
    attrs = ours_norm(decoded["ImageAttributesLV!"])["SLxImageAttributes"]
    pixels = 8 + attrs["uiHeight"] * attrs["uiWidthBytes"]
    compressed = attrs.get("eCompression", 2) == 0
    frames = int(np.prod(src["ImageDataSeq"].shape)) if src is not None and "ImageDataSeq" in src else 0
    problems = []
    placed_frames: dict[int, bytes] = {}
    with nd2lib.ND2File(path) as f:
        r = f._rdr
        for k in r.chunkmap:
            name = read_text(k)
            raw = r._load_chunk(k)
            frame = re.fullmatch(rb"ImageDataSeq\|(0|[1-9][0-9]*)!", k)
            placed = frame is not None and int(frame[1]) < frames
            if k in other:
                got = bytes(src[f"other/{other[k]}"][...])
                if got != (raw[pixels:] if placed else raw):
                    problems.append(f"{name}: other bytes differ")
                continue
            if k in empty:
                if raw:
                    problems.append(f"{name}: listed as empty")
                continue
            if placed:
                placed_frames[int(frame[1])] = raw
                if not compressed and len(raw) > pixels:
                    if family_member(src["ImageDataSeq.trailing"], int(frame[1])) != raw[pixels:]:
                        problems.append(f"{name}: trailing bytes differ")
                continue
            if frame is not None:
                if family_member(src["ImageDataSeq.beyond"], int(frame[1]) - frames) != raw:
                    problems.append(f"{name}: frame beyond the image differs")
                continue
            if name in decoded:
                if k.endswith(b"LV!") or b"LV|" in k:
                    try:
                        theirs = lv_norm(r._decode_chunk(k, strip_prefix=False))
                    except UnicodeDecodeError:
                        continue  # nd2 cannot decode unpaired surrogates, which ours keep
                    if ours_norm(decoded[name]) != theirs:
                        problems.append(f"{name}: {str(decoded[name])[:60]} vs {str(theirs)[:60]}")
                continue
            m = re.fullmatch(r"CustomDataSeq\|(.+)\|([0-9]+)!", name, re.S)
            p = f"CustomDataSeq/{node_path(m[1])}" if m else node_path(name)
            if src is None or p not in src:
                problems.append(f"{name}: not in the hierarchy")
                continue
            a = src[p]
            if m:
                got = family_member(a, int(m[2]))
            elif a.dtype == np.uint8:
                got = bytes(a[...])
            else:  # a stream: its values, in the file's frame order, then its rest
                values = a[...].reshape(-1)
                theirs = np.frombuffer(raw, dtype=a.dtype, count=values.size)
                if sorted(theirs.tolist()) != sorted(values.tolist()):
                    problems.append(f"{name}: stream values differ")
                rest = raw[values.size * values.itemsize:]
                if rest and (f"{p}.rest" not in src or bytes(src[f"{p}.rest"][...]) != rest):
                    problems.append(f"{name}: stream rest differs")
                continue
            if got != raw:
                problems.append(f"{name}: bytes differ")
        exp = r._load_chunk(b"ImageMetadataLV!") if b"ImageMetadataLV!" in r.chunkmap else None
    # the frozen reference twin of the ND2 profile (conformance/virtualize/reference)
    sys.path.insert(0, str(ROOT / "conformance" / "virtualize" / "reference"))
    from vzip_reference.nd2.lv import decode_lv
    from vzip_reference.nd2.virtualize import flatten_experiment

    loops = flatten_experiment(decode_lv(exp).get("SLxExperiment") if exp is not None else None)
    return problems + frame_problems(root, loops, placed_frames, attrs)


def main() -> int:
    fixtures = HERE.parent / "fixtures" / "nd2"
    server = Server(fixtures)
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for nd2 in sorted(fixtures.glob("nd2_*.nd2")):
            out = Path(tmp) / f"{nd2.stem}.vzip"
            p = subprocess.run(["node", str(ROOT / "web" / "conformance" / "virtualize.ts"), "--allow-private-hosts",
                                server.base + nd2.name, str(out)], capture_output=True, text=True)
            if nd2.stem.startswith("nd2_reject"):
                ok = p.returncode == 3 and not out.exists()
                failures += not ok
                print(f"{nd2.name:34s} {'rejected: ' + p.stderr.strip()[-90:] if ok else 'NOT REJECTED ' + p.stderr[-200:]}")
                continue
            if p.returncode:
                failures += 1
                print(f"{nd2.name:34s} FAILED: {p.stderr.strip()[-1500:]}")
                continue
            root = zarr.open_group(VZipStore(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
            meta = mirror_chunk_problems(nd2, root, out) if is_mirror(out) else chunk_problems(nd2, root)
            if meta:
                failures += 1
                print(f"{nd2.name:34s} CHUNKS: {meta[:3]}")
            expected = {k.replace("|", "/"): v for k, v in np.load(nd2.with_suffix(".npz")).items()}
            root = zarr.open_group(VZipStore(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
            problems, chunks = [], 0
            for series in root["OME"].attrs["ome"]["series"]:
                arr = root[f"{series}/0"]
                grid = [s // c for s, c in zip(arr.shape, arr.chunks)]
                # Every chunk, or for a large grid the chunks with data and the first ones.
                indices = list(np.ndindex(*grid)) if math.prod(grid) <= 100_000 else sorted(
                    {tuple(map(int, k.split("/")[3:])) for k in expected if k.startswith(f"{series}/")}
                    | {i for i, _ in zip(np.ndindex(*grid), range(10))})
                for index in indices:
                    key = f"{series}/0/c/" + "/".join(map(str, index))
                    sel = tuple(slice(i * c, (i + 1) * c) for i, c in zip(index, arr.chunks))
                    got = np.squeeze(arr[sel], axis=tuple(d for d, c in enumerate(arr.chunks) if c == 1 and d < arr.ndim - 2))
                    want = expected.get(key)
                    chunks += 1
                    if want is None:
                        if np.any(got != 0):
                            problems.append(f"{key}: expected the fill value")
                    elif got.dtype != want.dtype or not np.array_equal(got, want):
                        problems.append(f"{key}: differs ({got.dtype} {got.shape} vs {want.dtype} {want.shape})")
            failures += bool(problems)
            print(f"{nd2.name:34s} {'ok' if not problems else problems[:3]} ({chunks} chunks, "
                  f"{len(expected)} with data, axes {root[root['OME'].attrs['ome']['series'][0] + '/0'].metadata.dimension_names})")
    print(f"\n{failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
