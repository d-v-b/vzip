"""Checks the browser NIfTI virtualizer's output against nibabel.

Each fixtures/nifti/*.nii is served over local HTTP and virtualized
by js/conformance/virtualize.ts (the browser code, run under Node). The
array is read through the reference reader and zarr-python and must equal
nibabel's unscaled data (`img.dataobj.get_unscaled()`), with nibabel's
(x, y, z, t, dimension 5) axes put in the output's order (t, c, z, y, x), and
a color type's samples as the channel axis. A nontrivial scaling must be
recorded as nibabel reads it, and every header field as nibabel reads the
header. The bytes from the header to the voxels are parsed here, independently
of nibabel (which fails on some extension chains) and of the virtualizers: the
extender, each extension, and the bytes the chain does not hold must be
recorded (spec/virtualize/nifti.md §5), and the extensions also match
nibabel's where nibabel reads them. `nifti_reject_*` files must be rejected.

Usage: uv run python js/test/nifti/verify.py [<dir of .nii files>]
"""

from __future__ import annotations

import base64
import io
import math
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import nibabel as nib
import numpy as np
import zarr

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "conformance" / "archive"))
from http_server import Server  # noqa: E402

from vzip.policy import Policy  # noqa: E402
from vzip.store import VZipStore  # noqa: E402

# The sources are served on 127.0.0.1, which spec §8.7 rule 3 refuses by default.
LOOPBACK_SOURCES = Policy(allow_private_hosts=True)


def expected(img) -> tuple[np.ndarray, list[str]]:
    """nibabel's raw voxels as a (t, c, z, y, x) array, and the axes present."""
    n = int(img.header["dim"][0])
    data = np.asarray(img.dataobj.get_unscaled())
    color = data.dtype.names is not None
    if color:
        data = np.stack([data[name] for name in data.dtype.names], axis=-1)
    else:
        data = data[..., np.newaxis]  # the samples
    sizes = list(data.shape[:-1]) + [1] * (7 - data.ndim + 1)
    data = data.reshape(sizes[:5] + [data.shape[-1]])  # x, y, z, t, k, samples
    # Color samples are the channel axis; dimension 5 is otherwise.
    data = data[..., 0, :] if color else data[..., 0]
    axes = (["t"] if n >= 4 else []) + (["c"] if n >= 5 or color else []) + (["z"] if n >= 3 else []) + ["y", "x"]
    full = data.transpose(3, 4, 2, 1, 0)  # t, c, z, y, x
    drop = tuple(i for i, a in enumerate("tczyx") if a not in axes)
    return full.squeeze(axis=drop), axes


def raw_header(data: bytes):
    """nibabel's reading of the header alone, without the extensions."""
    klass = nib.Nifti2Header if data[4:7] == b"n+2" else nib.Nifti1Header
    length = 540 if klass is nib.Nifti2Header else 348
    return klass.from_fileobj(io.BytesIO(data[:length] + bytes(4)), check=False)


def load(nii: Path, tmp: str):
    """nibabel's image. nibabel refuses a valid scl_slope with a non-finite
    scl_inter, which the profile (like nifti1_io) takes as 0, and fails on some
    extension chains (an esize below 8) that the profile reads up to where they
    stop; such a file is loaded from a copy with scl_inter set to 0 and the
    extender's first byte to 0."""
    try:
        return nib.load(nii)
    except nib.spatialimages.HeaderDataError as e:
        if "invalid intercept" not in str(e) and "extension" not in str(e):
            raise
        return load_copy(nii, tmp)
    except ValueError as e:  # nibabel's error for an esize below 8
        if "read length" not in str(e):
            raise
    return load_copy(nii, tmp)


def load_copy(nii: Path, tmp: str):
    data = nii.read_bytes()
    hdr = raw_header(data)
    if not (math.isfinite(float(hdr["scl_inter"]))):
        hdr["scl_inter"] = 0
    copy = Path(tmp) / nii.name
    block = hdr.binaryblock
    copy.write_bytes(block + b"\0" + data[len(block) + 1:])
    return nib.load(copy)


def region(data: bytes, raw) -> tuple[bytes, list[tuple[int, bytes]], int, int]:
    """The extender, the extension chain (ecode, data), where the chain ends, and
    the voxel offset, read from the file (spec/virtualize/nifti.md §5)."""
    length = raw.structarr.itemsize
    order = raw.endianness
    v = int(raw.structarr["vox_offset"])
    extender, exts, q = data[length:length + 4], [], length + 4
    if extender[0]:
        while q + 8 <= v:
            esize, ecode = struct.unpack_from(order + "ii", data, q)
            if esize < 8 or q + esize > v:
                break
            exts.append((ecode, data[q + 8:q + esize]))
            q += esize
    return extender, exts, q, v


CANONICAL_NAN = {4: 0x7FC00000, 8: 0x7FF8000000000000}


def as_json(v):
    """A header value as spec/conventions.md §6 and spec/virtualize/nifti.md §5 write
    it, from nibabel's numpy value: a negative zero, or a NaN other than the canonical
    one, as {"bits": hex}."""
    if isinstance(v, np.ndarray) and v.dtype.kind != "S":
        return [as_json(x) for x in v]
    if isinstance(v, np.floating):
        size = v.dtype.itemsize
        bits = int(np.asarray(v).astype(v.dtype.newbyteorder("=")).view(f"=u{size}"))
        if bits == 1 << (8 * size - 1) or (v != v and bits != CANONICAL_NAN[size]):
            return {"bits": f"{bits:0{2 * size}x}"}
    if isinstance(v, (bytes, np.bytes_)):
        b = bytes(v).split(b"\0", 1)[0]
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return {"latin1": b.decode("latin-1")}
    v = v.item() if hasattr(v, "item") else v
    if isinstance(v, float):
        return "NaN" if v != v else "Infinity" if v == float("inf") else "-Infinity" if v == float("-inf") else v
    return v if abs(v) <= 2**53 - 1 else str(v)


def header_problems(nii: Path, meta: dict, root) -> list[str]:
    """Every recorded header field against nibabel's reading of the raw header, and
    everything between the header and the voxels, and after the voxels, against the file."""
    data = nii.read_bytes()
    two = data[4:7] == b"n+2"
    raw = raw_header(data)
    want = {}
    for name in raw.structarr.dtype.names:
        if name == "eol_check":  # nibabel splits NIfTI-2's char magic[8]; the text stops at its NUL
            continue
        want[name] = as_json(raw.structarr[name][()] if raw.structarr[name].shape == () else raw.structarr[name])
    problems = []
    if list(meta["header"]) != list(want):
        problems.append(f"header fields {list(meta['header'])} != nibabel's {list(want)}")
    for name, value in want.items():
        if meta["header"].get(name) != value:
            problems.append(f"header {name} {meta['header'].get(name)!r} != nibabel's {value!r}")
    # The bytes after the first NUL of each character field (numpy keeps the inner NULs).
    rest = {}
    for name in raw.structarr.dtype.names:
        if raw.structarr.dtype[name].kind == "S" and name not in ("magic", "eol_check"):
            tail = bytes(raw.structarr[name][()]).partition(b"\0")[2]
            if tail.strip(b"\0"):
                rest[name] = tail
    got_rest = {k: base64.b64decode(r).rstrip(b"\0") for k, r in meta.get("header_rest", {}).items()}
    if got_rest != rest:
        problems.append(f"header_rest {got_rest} != {rest}")
    if meta["nifti_version"] != (2 if two else 1) or meta["byte_order"] != ("little" if raw.endianness == "<" else "big"):
        problems.append(f"version or byte order {meta['nifti_version']}, {meta['byte_order']}")

    extender, chain, end, v = region(data, raw)
    if extender in (bytes(4), b"\1\0\0\0"):
        if "extender" in meta:
            problems.append("extender recorded")
    elif base64.b64decode(meta.get("extender", "")) != extender:
        problems.append(f"extender {meta.get('extender')} != {extender!r}")
    if extender[0]:
        def edata(e) -> bytes:  # an extension's data, from JSON or from its array
            if "edata" in e:
                return base64.b64decode(e["edata"])
            if "text" in e:  # padded with NULs to its esize, or to a multiple of 16 with the fewest
                t = e["text"]
                d = t["latin1"].encode("latin-1") if isinstance(t, dict) else t.encode("utf-8")
                return d.ljust(e.get("esize", -(-(len(d) + 8) // 16) * 16) - 8, b"\0")
            return root[e["data"]][...].tobytes()

        def same(got, wanted) -> bool:  # every extension's bytes, the padding included
            return got == wanted

        recorded = meta.get("extensions")
        if isinstance(recorded, dict):  # over the root's budget: the ecodes and esizes, the inline texts, a family
            codes = root[recorded["ecode"]][...].tolist()
            sizes = root[recorded["esize"]][...].tolist()
            if sizes != [len(r) + 8 for _, r in chain]:
                problems.append("the esizes of vzip_source/extensions/esize are not the chain's")
            offsets = root[recorded["data"] + "/offsets"][...].tolist()
            blob = root[recorded["data"] + "/data"][...].tobytes() if offsets[-1] else b""
            texts = recorded.get("text", {})
            recorded = [{"ecode": c, "text": texts[str(i)], "esize": sizes[i]} if str(i) in texts else
                        {"ecode": c, "edata": base64.b64encode(blob[offsets[i]:offsets[i + 1]]).decode()}
                        for i, c in enumerate(codes)]
        got = [(e["ecode"], edata(e)) for e in recorded or []]
        if not same(got, chain):
            problems.append(f"extensions {[(c, len(d)) for c, d in got]} != {[(c, len(r)) for c, r in chain]}")
        try:  # nibabel's reading, where it reads the chain
            full = type(raw).from_fileobj(io.BytesIO(data), check=False)
            nib_exts = [(e.get_code(), e._raw) for e in full.extensions]
            # nibabel drops an extension's trailing NULs.
            if [(c, d.rstrip(b"\0")) for c, d in got] != [(c, r.rstrip(b"\0")) for c, r in nib_exts]:
                problems.append(f"extensions {[(c, len(d)) for c, d in got]} != nibabel's "
                                f"{[(c, len(r)) for c, r in nib_exts]}")
        except (ValueError, nib.spatialimages.HeaderDataError):
            pass  # nibabel cannot read the chain
    elif "extensions" in meta:
        problems.append("extensions recorded, the extender says none")
    rest_bytes = data[end:v]
    if rest_bytes.strip(b"\0"):
        if "unparsed" not in meta or root[meta["unparsed"]][...].tobytes() != rest_bytes:
            problems.append(f"the {len(rest_bytes)} bytes from {end} to the voxels are not kept")
        if meta.get("extensions_truncated", False) != bool(extender[0]):
            problems.append("extensions_truncated")
    elif "unparsed" in meta or "extensions_truncated" in meta:
        problems.append("padding recorded")
    dim = [int(x) for x in raw.structarr["dim"]]
    total = math.prod(dim[1:dim[0] + 1]) * int(raw.structarr["bitpix"]) // 8
    after = data[v + total:]
    if after and ("trailing" not in meta or root[meta["trailing"]][...].tobytes() != after):
        problems.append(f"the {len(after)} bytes after the voxels are not kept")
    return problems


def main(fixtures: Path) -> int:
    server = Server(fixtures)
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for nii in sorted(fixtures.glob("*.nii")):
            out = Path(tmp) / f"{nii.stem}.vzip"
            p = subprocess.run(["node", str(ROOT / "js" / "conformance" / "virtualize.ts"), "--allow-private-hosts",
                                server.base + nii.name, str(out)], capture_output=True, text=True)
            if nii.stem.startswith("nifti_reject"):
                ok = p.returncode == 3 and not out.exists()
                failures += not ok
                print(f"{nii.name:44s} {'rejected: ' + p.stderr.strip()[-80:] if ok else 'NOT REJECTED ' + p.stderr[-200:]}")
                continue
            if p.returncode:
                failures += 1
                print(f"{nii.name:44s} FAILED: {p.stderr.strip()[-300:]}")
                continue
            img = load(nii, tmp)
            want, axes = expected(img)
            root = zarr.open_group(VZipStore(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
            arr = root["0"]
            got = arr[...]
            problems = []
            if list(arr.metadata.dimension_names) != axes:
                problems.append(f"axes {arr.metadata.dimension_names} != {axes}")
            if got.dtype != want.dtype.newbyteorder("="):
                problems.append(f"dtype {got.dtype} != {want.dtype}")
            if got.shape != want.shape or not np.array_equal(got, want, equal_nan=got.dtype.kind == "f"):
                problems.append(f"voxels differ ({got.shape} vs {want.shape})")
            meta = root.attrs["vzip_virtualized"]["nifti"]  # spec/virtualize/nifti.md §5
            slope, inter = img.dataobj.slope, img.dataobj.inter  # the header's are reset on load
            recorded = meta.get("scaling")
            # nibabel keeps a color type's scl_slope but cannot apply it; NIfTI does not scale color.
            if img.get_data_dtype().names is None and (slope, inter) != (1, 0):
                if recorded != {"slope": slope, "inter": inter}:
                    problems.append(f"scaling {recorded} != nibabel's {(slope, inter)}")
            elif recorded is not None:
                problems.append(f"scaling {recorded} recorded, nibabel has none")
            problems += header_problems(nii, meta, root)
            failures += bool(problems)
            print(f"{nii.name:44s} {'ok' if not problems else problems} ({arr.nchunks} chunks, axes {axes})")
    print(f"\n{failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "fixtures" / "nifti"))
