"""Checks the CZI virtualizers' output (spec/virtualize/czi/profile.md) against czifile and libCZI.

Each fixtures/czi/*.czi is served over local HTTP and virtualized by
the browser code under Node (js/conformance/virtualize.ts; `--impl py` uses
the Python reference). Then, reading only the archive, and the file through
czifile (Christoph Gohlke's independent reader) and pylibCZIrw (ZEISS libCZI)
where installed:

1. **Pixels, per subblock.** Every subblock czifile lists is found in the
   archive exactly once: as a chunk (or row bands) of an image's level or of
   a tile array, or as `vzip_source/subblocks/data`. The chunk decodes, both
   through zarr-python (rectilinear chunk grids enabled) and by decoding its
   bytes directly, to czifile's pixels for the subblock (sample order
   reversed for stored B, G, R(, A)); its place agrees with the subblock's
   directory entry: plane, series, and for a level the X and Y Start given
   by the level's OME-NGFF scale and translation, for a tile array its
   recorded position.
2. **Stitching.** Each full-resolution level of a pixel type libCZI reads
   equals libCZI's composite of its scene and plane over its extent.
3. **Reconstruction.** Every directory entry is rebuilt from the directory
   columns; every subblock's metadata, data (chunks + trailing bytes, or
   `subblocks/data`) and attachment; the metadata XML and its attachment;
   every attachment's data, from its form; every segment nothing
   references, and the tail. Each is compared with the file's bytes.
4. Every `czi_reject_*` file is rejected.

Usage: uv run python js/test/czi/verify.py [--impl py|web] [--only <substring>] [--big]
  --big also checks the row-band file written by write_fixtures.py --big.
"""

from __future__ import annotations

import asyncio
import json
import re
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import czifile
import imagecodecs
import numpy as np
import zarr
from zarr.core.buffer import default_buffer_prototype

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
FIXTURES = ROOT / "fixtures" / "czi"
sys.path.insert(0, str(ROOT / "conformance" / "archive"))
sys.path.insert(0, str(ROOT / "conformance" / "virtualize"))
from http_server import Server  # noqa: E402

import vzip.codecs  # noqa: E402,F401  (registers imagecodecs_jpeg, imagecodecs_jpegxr)
from vzip.pb import Concat, Range  # noqa: E402
from vzip.policy import Policy  # noqa: E402
from vzip.store import VZipStore  # noqa: E402

# The sources are served on 127.0.0.1, which spec §8.7 rule 3 refuses by default.
LOOPBACK_SOURCES = Policy(allow_private_hosts=True)
sys.path.insert(0, str(HERE.parent))
from ir_mirror import is_mirror, mirror_problems  # noqa: E402

zarr.config.set({"array.rectilinear_chunks": True})
LETTERS = "XYZCTRSIHVBM"
SERIES = "SBHIRV"
TYPES = {0: ("u1", 1), 1: ("<u2", 1), 2: ("<f4", 1), 3: ("u1", 3), 4: ("<u2", 3), 8: ("<f4", 3), 9: ("u1", 4),
         10: ("<c8", 1), 11: ("<c8", 3), 12: ("<i4", 1), 13: ("<f8", 1)}
LIBCZI_TYPES = {0, 1, 2, 3, 4}  # pixel types libCZI composites


def run(impl: str, url: str, out: Path) -> subprocess.CompletedProcess:
    """`impl`: py, web, or ref (the frozen TypeScript twin, conformance/virtualize/reference/ts)."""
    cmd = {"py": ["uv", "run", "python", "-m", "vzip.virtualize", "--allow-private-hosts"],
           "web": ["node", str(ROOT / "js" / "conformance" / "virtualize.ts"), "--allow-private-hosts"],
           "ref": ["node", str(ROOT / "conformance" / "virtualize" / "reference" / "ts" / "virtualize.ts")]}[impl]
    return subprocess.run(cmd + [url, str(out)], capture_output=True, text=True, cwd=ROOT)


def mirror_check(path: Path, url: str, out: Path) -> list[str]:
    """An archive of the IR, whose vzip_source is its mirror: the checks below run on the
    frozen reference's archive of the same file, the IR's hierarchy must equal that one
    outside vzip_source (the reference's root read at the current revision, compare.py's
    `at_revision`), and the mirror must rebuild the source byte for byte."""
    sys.path.insert(0, str(ROOT / "conformance" / "virtualize"))
    from compare import at_revision, differences, from_vzip, without_source

    ref = out.with_suffix(".ref.vzip")
    p = run("ref", url, ref)
    if p.returncode:
        return [f"the reference failed: {(p.stderr or p.stdout)[-200:]}"]
    problems = [f"differs from the reference: {d}" for d in
                differences(without_source(at_revision(from_vzip(ref))), without_source(from_vzip(out)))]
    problems += mirror_problems(out, path)
    ar = Archive(ref)
    problems += check_file(path, ar)
    return problems


class Archive:
    """An archive's documents, its chunk references (key -> ranges of source 0), and its entries' bytes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        z = zipfile.ZipFile(path)
        self.docs = {n: json.loads(z.read(n)) for n in z.namelist() if n.endswith("zarr.json")}
        self.refs: dict[str, list] = {}
        for info in z.infolist():
            ex, p = info.extra, 0
            while p < len(ex):
                hid, n = struct.unpack_from("<HH", ex, p)
                if hid == 0x7A76:
                    self.refs[info.filename] = [Range.decode(ex[p + 4 : p + 4 + n])]
                elif hid == 0x7A77:
                    self.refs[info.filename] = list(Concat.decode(ex[p + 4 : p + 4 + n]).parts)
                p += 4 + n
        self.store = VZipStore(str(path), policy=LOOPBACK_SOURCES)
        self.root = zarr.open_group(self.store, mode="r", zarr_format=3)
        self.lo: dict[str, dict] = {}  # image -> the T, C and Z Start of its index 0

    def covered(self, array: str, doc: dict, at: dict) -> np.ndarray:
        """Where the plane `at` of an image level has chunks: [y, x] booleans."""
        axes = doc["dimension_names"]
        mask = np.zeros(doc["shape"][-2:], bool)
        for key in self.refs:
            if not key.startswith(f"{array}/c/"):
                continue
            idx = list(map(int, key[len(array) + 3 :].split("/")))
            start, shape = chunk_index(doc, idx)
            if all(start[axes.index(a)] == v for a, v in at.items() if a in axes):
                mask[start[-2] : start[-2] + shape[-2], start[-1] : start[-1] + shape[-1]] = True
        return mask

    def get(self, key: str) -> bytes | None:
        async def go():
            b = await self.store.get(key, default_buffer_prototype())
            return None if b is None else b.to_bytes()
        return asyncio.run(go())

    def array_bytes(self, path: str) -> bytes:
        """A 1-D uint8 array's bytes (conventions §7)."""
        a = zarr.open_array(self.store, path=path, mode="r", zarr_format=3)
        return bytes(a[...])

    def family(self, path: str, i: int) -> bytes | None:
        """Member i of a family of byte values, or None (conventions §7)."""
        full = f"vzip_source/{path}"
        if self.docs.get(f"{full}/zarr.json", {}).get("node_type") == "array":
            a = zarr.open_array(self.store, path=full, mode="r", zarr_format=3)
            if i >= a.shape[0] or self.get(f"{full}/c/{i}/0") is None and a.chunks[0] == 1:
                return None
            return bytes(a[i])
        if f"{full}/offsets/zarr.json" not in self.docs:
            return None
        offsets = zarr.open_array(self.store, path=f"{full}/offsets", mode="r", zarr_format=3)[...]
        if i + 1 >= len(offsets) or offsets[i + 1] == offsets[i]:
            return None
        data = zarr.open_array(self.store, path=f"{full}/data", mode="r", zarr_format=3)
        return bytes(data[int(offsets[i]) : int(offsets[i + 1])])


def chunk_index(arr_doc: dict, index: list[int]) -> tuple[list[int], list[int]]:
    """The start and shape of the chunk at `index` of an array's regular or rectilinear grid."""
    g = arr_doc["chunk_grid"]
    shape = arr_doc["shape"]
    if g["name"] == "regular":
        cs = g["configuration"]["chunk_shape"]
        return [i * c for i, c in zip(index, cs)], [min(c, s - i * c) for i, c, s in zip(index, cs, shape)]
    starts, sizes = [], []
    for i, lengths, s in zip(index, g["configuration"]["chunk_shapes"], shape):
        if isinstance(lengths, int):
            starts.append(i * lengths)
            sizes.append(min(lengths, s - i * lengths))
            continue
        flat = []
        for x in lengths:
            flat += [x[0]] * x[1] if isinstance(x, list) else [x]
        assert sum(flat) == s, (lengths, s)
        starts.append(sum(flat[:i]))
        sizes.append(flat[i])
    return starts, sizes


def decode_direct(raw: bytes, codecs: list[dict], data_type: str, shape: list[int], order: list[int] | None):
    """A chunk decoded by hand, without zarr-python: the check of a clipped level that
    does not depend on rectilinear support."""
    names = [c["name"] for c in codecs]
    stored = [shape[i] for i in order] if order else shape
    if "imagecodecs_jpeg" in names:
        a = imagecodecs.jpeg8_decode(raw)
    elif "imagecodecs_jpegxr" in names:
        a = imagecodecs.jpegxr_decode(raw)
    else:
        b = raw
        if "zstd" in names:
            b = imagecodecs.zstd_decode(b)
        if "numcodecs.shuffle" in names:
            b = np.frombuffer(b, "u1").reshape(2, -1).T.tobytes()
        a = np.frombuffer(b, np.dtype(data_type).newbyteorder("<"))
    a = np.asarray(a).reshape(stored)
    if order:
        a = np.transpose(a, np.argsort(order))
    return a


def xml_scale(xml: bytes) -> tuple[float, float] | None:
    """(PX, PY) in micrometers, or 1 where absent, by ElementTree (not the profile's tag scan);
    None when ElementTree cannot parse the XML (positions are then not checked)."""
    if not xml:
        return 1.0, 1.0
    try:
        root = ET.fromstring(xml[3:] if xml.startswith(b"\xef\xbb\xbf") else xml)
    except ET.ParseError:
        return None
    out = []
    for axis in "XY":
        v = None
        for d in root.findall("./Metadata/Scaling/Items/Distance"):
            if d.get("Id") == axis:
                t = d.findtext("Value")
                try:
                    v = float(t) if t is not None and float(t) > 0 else None
                except ValueError:
                    v = None
                break
        out.append(v * (1 / 1e-6) if v else 1.0)
    return out[0], out[1]


def czi_pixels(sb_data: np.ndarray, pixel_type: int) -> np.ndarray:
    """czifile's pixels of a subblock as [h, w] or [h, w, samples]."""
    p = TYPES[pixel_type][1]
    a = np.asarray(sb_data)
    h_w = a.reshape(-1, *a.shape[-3:])[0] if a.ndim > 3 else a
    return h_w[..., 0] if p == 1 and h_w.ndim == 3 else h_w


def walk(data: bytes) -> tuple[list[tuple[int, bytes, int, int]], int]:
    """The segments in file order (spec/virtualize/czi/profile.md §13.2 step 6), as the verifier reads them."""
    out, o = [], 0
    while o < len(data) and len(out) < 1 << 23:
        if o + 32 > len(data):
            break
        ident = data[o : o + 16]
        name = ident.rstrip(b"\0")
        if not (1 <= len(name) <= 16 and re.fullmatch(rb"[A-Z0-9_]+", name) and ident[len(name):] == bytes(16 - len(name))):
            break
        a, u = struct.unpack_from("<qq", data, o + 16)
        if a < 0 or u < 0 or o + 32 + a > len(data):
            break
        out.append((o, ident, a, u))
        o += 32 + a
    return out, o


def check_file(path: Path, ar: Archive) -> list[str]:
    problems: list[str] = []
    file = path.read_bytes()
    c = czifile.CziFile(path)
    entries = c.subblock_directory
    xml_bytes = ar.array_bytes("vzip_source/metadata/xml")[: c.metadata_segment.xml_size] if c.metadata_segment and c.metadata_segment.xml_size else b""
    scale = xml_scale(xml_bytes)
    # Where each chunk's first range starts in the file: -> (array path, chunk index, band).
    by_offset: dict[int, list[tuple[str, list[int]]]] = {}
    for key, ranges in ar.refs.items():
        m = re.fullmatch(r"((?:[0-9]+/[0-9]+)|(?:tiles/[0-9]+))/c/([0-9/]+)", key)
        if m and ranges and ranges[0].data is None:
            by_offset.setdefault(ranges[0].offset, []).append((m[1], list(map(int, m[2].split("/")))))
    used_keys = set()
    n = len(entries)
    # 3. The directory columns.
    cols = {}
    for name in ("pixel_type", "compression", "pyramid_type"):
        if n:
            cols[name] = zarr.open_array(ar.store, path=f"vzip_source/directory/{name}", mode="r")[...]
    spare = (zarr.open_array(ar.store, path="vzip_source/directory/spare", mode="r")[...]
             if "vzip_source/directory/spare/zarr.json" in ar.docs else np.zeros((n, 5), "u1"))
    dims = {}
    for letter in LETTERS:
        if f"vzip_source/directory/{letter}/start/zarr.json" in ar.docs:
            dims[letter] = {f: zarr.open_array(ar.store, path=f"vzip_source/directory/{letter}/{f}", mode="r")[...]
                            for f in ("start", "size", "stored_size", "start_coordinate", "position")}
    images: dict[str, dict] = {}  # image path -> lo planes seen, for consistency
    for i, e in enumerate(entries):
        # The entry, rebuilt from the columns (FilePosition from czifile: layout).
        placed = sorted((int(d["position"][i]), letter) for letter, d in dims.items() if d["position"][i] >= 0)
        rebuilt = b"DV" + struct.pack("<iqii", int(cols["pixel_type"][i]), e.file_position, 0, int(cols["compression"][i]))
        rebuilt += bytes([int(cols["pyramid_type"][i])]) + bytes(spare[i]) + struct.pack("<i", len(placed))
        for _, letter in placed:
            d = dims[letter]
            rebuilt += letter.encode().ljust(4, b"\0") + struct.pack("<ii", int(d["start"][i]), int(d["size"][i]))
            rebuilt += np.float32(d["start_coordinate"][i]).tobytes() + struct.pack("<i", int(d["stored_size"][i]))
        if rebuilt != file[e.offset : e.offset + len(rebuilt)]:
            problems.append(f"subblock {i}: directory entry differs when rebuilt from the columns")
        sb = e.read_segment_data(c)
        # 3. Metadata and attachment.
        meta = file[sb.metadata_offset : sb.metadata_offset + sb.metadata_size]
        if (ar.family("subblocks/metadata", i) or b"") != meta:
            problems.append(f"subblock {i}: metadata differs")
        att_at = sb.data_offset + sb.data_size
        if (ar.family("subblocks/attachment", i) or b"") != file[att_at : att_at + sb.attachment_size]:
            problems.append(f"subblock {i}: attachment differs")
        own = file[e.file_position + 48 : e.file_position + 48 + len(rebuilt)]
        if own != rebuilt and ar.family("subblocks/entry", i) != own:
            problems.append(f"subblock {i}: its own copy of its entry is not kept")
        data = file[sb.data_offset : sb.data_offset + sb.data_size]
        unplaced = ar.family("subblocks/data", i)
        hits = [h for at in range(sb.data_offset, sb.data_offset + 4) for h in by_offset.get(at, [])]
        if unplaced is not None or not hits:
            if hits:
                problems.append(f"subblock {i}: both unplaced and in {hits[0][0]}")
            if (unplaced or b"") != data:
                problems.append(f"subblock {i}: not in any array, and subblocks/data differs from its data")
            continue
        array = hits[0][0]
        doc = ar.docs[f"{array}/zarr.json"]
        axes = doc["dimension_names"]
        keys = sorted((h for h in by_offset_all(by_offset, sb.data_offset, sb.data_size) if h[0] == array),
                      key=lambda h: h[1])
        rebuilt_data = b""
        for _, idx in keys:
            key = f"{array}/c/" + "/".join(map(str, idx))
            used_keys.add(key)
            rebuilt_data += ar.get(key)
        trailing = ar.family("subblocks/trailing", i) or b""
        comp = int(cols["compression"][i])
        if comp == 6:
            header = 3 if data[:1] == b"\x03" else 1
            if rebuilt_data != data[header:]:
                problems.append(f"subblock {i}: chunks differ from its zstd frame")
        elif rebuilt_data + trailing != data:
            problems.append(f"subblock {i}: chunks and trailing bytes differ from its data")
        # 1. Pixels: through zarr, and decoded by hand.
        idx0 = keys[0][1]
        start, _ = chunk_index(doc, idx0)
        _, last_size = chunk_index(doc, keys[-1][1])
        h = sum(chunk_index(doc, k[1])[1][axes.index("y")] for k in keys)
        w = chunk_index(doc, idx0)[1][axes.index("x")]
        sel = []
        for a, s in zip(axes, start):
            sel.append(slice(s, s + (h if a == "y" else w if a == "x" else chunk_index(doc, idx0)[1][axes.index(a)])))
        arr = zarr.open_array(ar.store, path=array, mode="r", zarr_format=3)
        got = np.asarray(arr[tuple(sel)])
        order = next((c["configuration"]["order"] for c in doc["codecs"] if c["name"] == "transpose"), None)
        direct = np.concatenate([decode_direct(ar.get(f"{array}/c/" + "/".join(map(str, k[1]))), doc["codecs"],
                                               doc["data_type"], chunk_index(doc, k[1])[1], order) for k in keys],
                                axis=axes.index("y"))
        if not np.array_equal(got, direct, equal_nan=True):
            problems.append(f"subblock {i}: zarr-python and the direct decoding of {array} differ")
        if "c" in axes:  # samples last
            got = np.moveaxis(got, axes.index("c"), -1)
        pt = int(cols["pixel_type"][i])
        got = got.reshape(got.shape[-3:] if "c" in axes else got.shape[-2:])
        if "c" in axes and TYPES[pt][1] == 1:
            got = got[..., 0]
        try:
            want = czi_pixels(sb.data(), pt)
        except ValueError:  # czifile cannot reshape data with trailing bytes: its first pixels' bytes, as czifile reads them
            dt, p = TYPES[pt]
            hw = got.shape[:2]
            want = np.frombuffer(data, dt, count=hw[0] * hw[1] * p).reshape(*hw, p)
            want = want[..., 0] if p == 1 else want[..., [2, 1, 0, 3][:p]]
        if comp in (0, 5, 6) and TYPES[pt][1] > 1:
            got = got[..., [2, 1, 0, 3][: TYPES[pt][1]]]  # czifile returns R, G, B; the archive holds B, G, R
            if pt == 9:  # czifile sets alpha to 255: compare the stored alpha with the raw bytes
                raw_alpha = np.frombuffer(data, "u1")[3::4][: got[..., 3].size].reshape(got.shape[:2]) if comp == 0 else None
                if raw_alpha is not None and not np.array_equal(got[..., 3], raw_alpha):
                    problems.append(f"subblock {i}: alpha differs from the stored bytes")
                got, want = got[..., :3], want[..., :3]
        if got.shape != want.shape or not np.array_equal(got, want, equal_nan=True):
            problems.append(f"subblock {i}: pixels in {array} {got.shape} differ from czifile's {want.shape}")
        # 1. Place: plane, series, position.
        letter_start = {letter: int(d["start"][i]) for letter, d in dims.items() if d["position"][i] >= 0}
        plane = [letter_start.get(x, 0) for x in "TCZ"]
        p = TYPES[pt][1]
        pos = {a: s for a, s in zip(axes, start)}
        if array.startswith("tiles/"):
            s = doc["attributes"]["vzip_virtualized"]["czi"]
            if [s["x"], s["y"]] != [letter_start["X"], letter_start["Y"]] or s["size"] != [int(e.shape[e.dims.index("X")]), int(e.shape[e.dims.index("Y")])]:
                problems.append(f"subblock {i}: tile array {array} records another position")
            got_plane = [s["planes"]["t"] + pos.get("t", 0), s["planes"]["c"] + pos.get("c", 0) // p, s["planes"]["z"] + pos.get("z", 0)]
            if got_plane != plane:
                problems.append(f"subblock {i}: in {array} at plane {got_plane}, not {plane}")
            if s.get("dimensions", {}) != {x: letter_start[x] for x in SERIES if x in letter_start}:
                problems.append(f"subblock {i}: tile array {array}'s dimensions differ")
            continue
        k, d = array.split("/")
        group = ar.docs[f"{k}/zarr.json"]["attributes"]
        want_dims = {x: letter_start[x] for x in SERIES if x in letter_start}
        if group.get("vzip_virtualized", {}).get("czi", {}).get("dimensions", {}) != want_dims:
            problems.append(f"subblock {i}: image {k}'s dimensions differ")
        ds = group["ome"]["multiscales"][0]["datasets"][int(d)]["coordinateTransformations"]
        if scale is None:
            continue
        px, py = scale
        sx, sy = ds[0]["scale"][axes.index("x")], ds[0]["scale"][axes.index("y")]
        tx, ty = ds[1]["translation"][axes.index("x")], ds[1]["translation"][axes.index("y")]
        f = sx / px
        x_start = tx / px - (f - 1) / 2 + pos["x"] * f
        y_start = ty / py - (f - 1) / 2 + pos["y"] * f
        if abs(x_start - letter_start["X"]) > 1e-3 * max(1, abs(x_start)) or abs(y_start - letter_start["Y"]) > 1e-3 * max(1, abs(y_start)):
            problems.append(f"subblock {i}: in {array} at ({x_start}, {y_start}), not ({letter_start['X']}, {letter_start['Y']})")
        if abs(sy / py - f) > 1e-9 * f:
            problems.append(f"subblock {i}: {array} has unequal x and y factors")
        lo = images.setdefault(k, {})
        ar.lo[k] = lo
        for a, v, j in (("t", plane[0], pos.get("t", 0)), ("c", plane[1], pos.get("c", 0) // p), ("z", plane[2], pos.get("z", 0))):
            if lo.setdefault(a, v - j) != v - j:
                problems.append(f"subblock {i}: image {k}'s {a} index is not its plane less a constant")
    for key in ar.refs:
        if re.fullmatch(r"(?:[0-9]+/[0-9]+|tiles/[0-9]+)/c/[0-9/]+", key) and key not in used_keys:
            problems.append(f"{key}: a chunk that is no subblock's")
    problems += check_source(file, c, ar)
    return problems


def by_offset_all(by_offset: dict, start: int, length: int):
    """The chunks whose first range starts within [start, start + length)."""
    return [h for at, hs in by_offset.items() if start <= at < start + length for h in hs]


def check_source(file: bytes, c, ar: Archive) -> list[str]:
    """3. The metadata XML, the attachments, the unreferenced segments and the tail."""
    problems = []
    h = c.header
    referenced = {0, h.directory_position}
    m = c.metadata_segment
    if h.metadata_position:
        referenced.add(h.metadata_position)
        at = h.metadata_position + 32 + 256
        xml = file[at : at + m.xml_size]
        got = ar.array_bytes("vzip_source/metadata/xml")[: m.xml_size] if m.xml_size else b""
        if got != xml:
            problems.append("the metadata XML differs")
        att = file[at + m.xml_size : at + m.xml_size + m.attachment_size]
        got = ar.array_bytes("vzip_source/metadata/attachment")[: len(att)] if att else b""
        if got != att:
            problems.append("the metadata attachment differs")
    for e in c.subblock_directory:
        referenced.add(e.file_position)
    if h.attachment_directory_position:
        referenced.add(h.attachment_directory_position)
        o = h.attachment_directory_position
        count = struct.unpack_from("<i", file, o + 32)[0]
        s = ar.root["vzip_source"].attrs["vzip_virtualized"]["czi"]["attachments"]
        listed = json.loads(ar.array_bytes("vzip_source/attachments/index").rstrip(b"\0")) if isinstance(s, str) else s
        if len(listed) != count:
            problems.append(f"{len(listed)} attachments listed, the directory has {count}")
        for k in range(count):
            entry = file[o + 288 + 128 * k : o + 288 + 128 * (k + 1)]
            item = listed[k]
            if entry[:2] != b"A1":
                if item != {"entry": __import__("base64").b64encode(entry).decode()}:
                    problems.append(f"attachment {k}: its entry is not kept")
                continue
            position = struct.unpack_from("<q", entry, 12)[0]
            referenced.add(position)
            size = struct.unpack_from("<q", file, position + 32)[0]
            data = file[position + 288 : position + 288 + size]
            name = entry[48:128].split(b"\0")[0]
            if item["name"] != (name.decode() if is_utf8(name) else {"latin1": name.decode("latin-1")}):
                problems.append(f"attachment {k}: name differs")
            base = f"vzip_source/attachments/{k}"
            form = item["form"]
            if form in ("time_stamps", "focus_positions"):
                a = zarr.open_array(ar.store, path=base, mode="r")
                got = struct.pack("<ii", a.attrs["vzip_virtualized"]["czi"]["size"], a.shape[0]) + a[...].astype("<f8").tobytes()
            elif form == "event_list":
                g = ar.root[base]
                times = zarr.open_array(ar.store, path=f"{base}/time", mode="r")[...]
                types = zarr.open_array(ar.store, path=f"{base}/type", mode="r")[...]
                body = b""
                for j in range(len(times)):
                    d = ar.family(f"attachments/{k}/description", j) or b""
                    body += struct.pack("<idii", 20 + len(d), times[j], types[j], len(d)) + d
                got = struct.pack("<ii", g.attrs["vzip_virtualized"]["czi"]["size"], len(times)) + body
            elif form == "bytes":
                got = ar.array_bytes(base)[:size]
            else:
                got = b""
            if got != data:
                problems.append(f"attachment {k} ({form}): data differs when rebuilt")
            seg_entry = file[position + 48 : position + 176]
            if seg_entry != entry and item.get("segment_entry") != __import__("base64").b64encode(seg_entry).decode():
                problems.append(f"attachment {k}: its segment's copy of its entry is not kept")
    segments, tail = walk(file)
    others = [s for s in segments if s[0] not in referenced]
    if others:
        ids = zarr.open_array(ar.store, path="vzip_source/segments/id", mode="r")[...]
        for j, (o, ident, a, u) in enumerate(others):
            if bytes(ids[j]) != ident:
                problems.append(f"segment {j} at {o}: id differs")
            n = min(u or a, a)
            if (ar.family("segments/data", j) or b"") != file[o + 32 : o + 32 + n]:
                problems.append(f"segment {j} at {o}: data differs")
        if len(ids) != len(others):
            problems.append(f"{len(ids)} segments kept, {len(others)} found")
    elif "vzip_source/segments/id/zarr.json" in ar.docs:
        problems.append("segments kept, none found")
    got_tail = ar.array_bytes("vzip_source/tail")[: len(file) - tail] if tail < len(file) else b""
    if got_tail != file[tail:]:
        problems.append("the tail differs")
    return problems


def is_utf8(b: bytes) -> bool:
    try:
        b.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


LIBCZI = {("uint8", 1): "Gray8", ("uint16", 1): "Gray16", ("float32", 1): "Gray32Float", ("uint8", 3): "Bgr24",
          ("uint16", 3): "Bgr48"}


def check_libczi(path: Path, ar: Archive) -> tuple[list[str], int]:
    """2. Each full-resolution level of a type libCZI composites against libCZI's composite
    of its scene and plane (where it has a subblock: libCZI fills the rest with 0)."""
    try:
        from pylibCZIrw import czi as pyczi
    except ImportError:
        return [], 0
    problems, checked = [], 0
    xml = ar.array_bytes("vzip_source/metadata/xml") if "vzip_source/metadata/xml/zarr.json" in ar.docs else b""
    scale = xml_scale(xml.rstrip(b"\0"))
    # libCZI composites a plane over every series but the scene: compare only files of scenes alone.
    if scale is None or any(f"vzip_source/directory/{x}/start/zarr.json" in ar.docs for x in "BHIRV"):
        return [], 0
    px, py = scale
    planes_of = {}  # image -> {(t, c, z) index: the plane's T, C, Z}, from the chunks the verifier has placed
    with pyczi.open_czi(str(path)) as doc:
        for key, d in sorted(ar.docs.items()):
            m = re.fullmatch(r"([0-9]+)/([0-9]+)/zarr\.json", key)
            if not m:
                continue
            k = m[1]
            group = ar.docs[f"{k}/zarr.json"]["attributes"]
            ds = group["ome"]["multiscales"][0]["datasets"][int(m[2])]["coordinateTransformations"]
            axes = d["dimension_names"]
            f = ds[0]["scale"][axes.index("x")] / px
            dims = group.get("vzip_virtualized", {}).get("czi", {}).get("dimensions", {})
            g = d["chunk_grid"]["configuration"]
            lengths = g.get("chunk_shape") or g["chunk_shapes"]
            p = lengths[axes.index("c")] if "c" in axes else 1
            pixel_type = LIBCZI.get((d["data_type"], p))
            if abs(f - 1) > 1e-9 or set(dims) - {"S"} or pixel_type is None:
                continue
            if p > 1 and any(c["name"] == "numcodecs.shuffle" for c in d["codecs"]):
                # pylibCZIrw 6.1.0 (on arm64) unpacks only the first 16 bytes of each row of a Bgr48
                # hi-lo subblock and leaves the rest 0; its C code and czifile agree with the archive.
                continue
            arr = zarr.open_array(ar.store, path=f"{k}/{m[2]}", mode="r")
            x0 = round(ds[1]["translation"][axes.index("x")] / px)
            y0 = round(ds[1]["translation"][axes.index("y")] / py)
            h, w = d["shape"][-2:]
            lo = ar.lo.get(k, {"t": 0, "c": 0, "z": 0})
            extent = {a: (d["shape"][axes.index(a)] // (p if a == "c" else 1)) if a in axes else 1 for a in "tcz"}
            for t, c, z in list(np.ndindex(extent["t"], extent["c"], extent["z"]))[:12]:
                sel = tuple({"t": t, "c": slice(c * p, c * p + p) if p > 1 else c, "z": z}.get(a, slice(None)) for a in axes)
                ours = np.asarray(arr[sel])
                plane = {"T": t + lo["t"], "C": c + lo["c"], "Z": z + lo["z"]}
                kw = {"scene": dims["S"]} if "S" in dims else {}
                try:
                    theirs = np.asarray(doc.read(roi=(x0, y0, w, h), plane=plane, pixel_type=pixel_type, **kw))
                except Exception as e:  # noqa: BLE001
                    if "not implemented" not in str(e) and "Unsupported pixel format" not in str(e):
                        # (libCZI builds without a JPEG decoder; its jxrlib reads neither 24bppRGB nor 32bppBGRA)
                        problems.append(f"{k}: libCZI cannot read plane {plane}: {e}"[:160])
                    break
                if p > 1:
                    ours = np.moveaxis(ours, 0, -1)  # samples last, as libCZI gives them, in B, G, R order:
                    if any(c["name"] in ("imagecodecs_jpeg", "imagecodecs_jpegxr") for c in d["codecs"]):
                        ours = ours[..., ::-1]  # JPEG and JPEG XR decode to R, G, B
                elif theirs.ndim == 3:
                    theirs = theirs[..., 0]
                covered = ar.covered(f"{k}/{m[2]}", d, {"t": t, "c": c * p, "z": z})
                if ours.shape != theirs.shape or not np.array_equal(ours[covered], theirs[covered], equal_nan=True):
                    problems.append(f"{k}/{m[2]} plane {plane}: differs from libCZI's composite")
                checked += 1
    return problems, checked


def main(argv: list[str]) -> int:
    impl = argv[argv.index("--impl") + 1] if "--impl" in argv else "web"
    only = argv[argv.index("--only") + 1] if "--only" in argv else ""
    paths = sorted(p for p in FIXTURES.glob("czi_*.czi") if only in p.name)
    tmp = Path(tempfile.mkdtemp())
    if "--big" in argv:
        subprocess.run([sys.executable, str(ROOT / "fixtures" / "generators" / "czi" / "write_fixtures.py"), "--big", str(tmp / "big")], check=True)
        paths.append(tmp / "big" / "czi_big_bands.czi")
    failures = 0
    for path in paths:
        server = Server(path.parent)
        out = tmp / f"{path.stem}.vzip"
        p = run(impl, server.base + path.name, out)
        if path.stem.startswith("czi_reject"):
            ok = p.returncode == 3
            failures += not ok
            print(f"{path.name:44s} {'rejected' if ok else 'NOT REJECTED ' + (p.stderr or p.stdout)[-200:]}")
            continue
        if p.returncode:
            failures += 1
            print(f"{path.name:44s} FAILED: {(p.stderr or p.stdout).strip()[-300:]}")
            continue
        if is_mirror(out):
            problems = mirror_check(path, server.base + path.name, out)
            ar = Archive(out.with_suffix(".ref.vzip"))
        else:
            ar = Archive(out)
            problems = check_file(path, ar)
        libczi, checked = check_libczi(path, ar)
        problems += libczi
        failures += bool(problems)
        print(f"{path.name:44s} {'ok' if not problems else 'PROBLEMS'} ({len(czifile.CziFile(path).subblock_directory)} "
              f"subblocks, {checked} libCZI planes)")
        for x in problems[:6]:
            print(f"      {x}")
    print(f"\n{len(paths)} files, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
