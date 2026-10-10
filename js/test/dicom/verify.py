"""Checks the browser DICOM virtualizer's output against pydicom.

Each fixtures/dicom/*.dcm (or, given as arguments, other DICOM files
or http(s) URLs) is served over local HTTP and virtualized by
js/conformance/virtualize.ts (the browser code, run under Node). The whole
array is then read through the reference reader (python/src/vzip) and zarr-python,
and must equal what pydicom reads from the file: its pixel_array for native
pixel data, and for encapsulated pixel data its frames (pydicom's
generate_frames) decoded by imagecodecs, with the color space that the
Photometric Interpretation names. Whole-slide tiles are assembled into the
total pixel matrix. `dicom_reject_*` files must be rejected.

Usage: uv run python js/test/dicom/verify.py [<file or URL> ...]
"""

from __future__ import annotations

import base64
import io
import struct
import subprocess
import sys
import tempfile
import urllib.request
from collections import Counter
from pathlib import Path

import imagecodecs
import numpy as np
import pydicom
import zarr
from pydicom.charset import convert_encodings
from pydicom.dataelem import RawDataElement, convert_raw_data_element
from pydicom.encaps import generate_frames
from pydicom.filereader import _read_file_meta_info, data_element_generator, read_preamble
from pydicom.tag import BaseTag
from zarr.core.buffer import default_buffer_prototype
from zarr.core.sync import sync

import vzip.codecs  # noqa: F401  (registers imagecodecs_jpeg and imagecodecs_jpeg2k)
from vzip.policy import Policy  # noqa: E402
from vzip.store import VZipStore

# The sources are served on 127.0.0.1, which spec §8.7 rule 3 refuses by default.
LOOPBACK_SOURCES = Policy(allow_private_hosts=True)
from vzip.virtualize.dicom.source import dictionary_vr

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "conformance" / "archive"))
from http_server import Server  # noqa: E402

WHOLE_SLIDE = "1.2.840.10008.5.1.4.1.1.77.1.6"
NATIVE = {"1.2.840.10008.1.2", "1.2.840.10008.1.2.1", "1.2.840.10008.1.2.2"}


def expected(ds: pydicom.Dataset) -> np.ndarray:
    """The image as the profile lays it out: [c,] [z,] y, x."""
    n = int(ds.get("NumberOfFrames", 1) or 1)
    rows, columns, spp = ds.Rows, ds.Columns, ds.SamplesPerPixel
    dtype = np.dtype(f"{'u' if ds.PixelRepresentation == 0 else ''}int{ds.BitsAllocated}")
    if ds.file_meta.TransferSyntaxUID in NATIVE:
        frames = np.asarray(ds.pixel_array).ravel()[: n * rows * columns * spp]  # without excess pixel data
        frames = frames.reshape(n, rows, columns, *([spp] if spp > 1 else []))
    else:
        offsets = None
        if "ExtendedOffsetTable" in ds:
            offsets = (ds.ExtendedOffsetTable, ds.ExtendedOffsetTableLengths)
        decoded = []
        for frame in generate_frames(ds.PixelData, number_of_frames=n, extended_offsets=offsets):
            if ds.file_meta.TransferSyntaxUID == "1.2.840.10008.1.2.4.50":
                colorspace = None
                if spp == 3:
                    colorspace = "RGB" if ds.PhotometricInterpretation == "RGB" else "YCBCR"
                decoded.append(imagecodecs.jpeg8_decode(frame, colorspace=colorspace, outcolorspace="RGB" if spp == 3 else None))
            else:
                decoded.append(imagecodecs.jpeg2k_decode(frame))
        frames = np.stack(decoded).astype(dtype)
    frames = frames.astype(dtype)
    if ds.get("SOPClassUID") == WHOLE_SLIDE:
        width, height = ds.TotalPixelMatrixColumns, ds.TotalPixelMatrixRows
        across = -(-width // columns)
        down = -(-height // rows)
        full = np.zeros((down * rows, across * columns, *frames.shape[3:]), dtype)
        for f in range(n):
            r, c = divmod(f, across)
            full[r * rows : (r + 1) * rows, c * columns : (c + 1) * columns] = frames[f]
        image = full[:height, :width]
    else:
        image = frames if n > 1 else frames[0]
    return np.moveaxis(image, -1, 0) if spp == 3 else image


LAYOUT = {"7FE00001", "7FE00002", "FFFCFFFC"}  # offset tables and padding: recorded only where they are not layout
ADOBE = 18  # the bytes of the SOI and Adobe marker that replace an RGB JPEG frame's SOI (spec/virtualize.md §6.5)
BINARY = {"OB", "OD", "OF", "OL", "OV", "OW", "UN"}
SIZES = {"AT": 4, "FL": 4, "FD": 8, "SL": 4, "SS": 2, "UL": 4, "US": 2, "SV": 8, "UV": 8}
pydicom.config.convert_wrong_length_to_UN = True  # numeric values whose length is not a multiple of the size


def pydicom_json(ds: pydicom.Dataset, little: bool = True) -> dict:
    """pydicom's DICOM JSON Model of a dataset, with the bytes of an element it
    cannot convert. An explicit UN value is in little endian in every transfer
    syntax (PS3.5 §6.2.2), where pydicom reads it in the dataset's byte order
    (at every depth)."""
    out = {}
    for tag in list(ds.keys()):
        raw = ds.get_item(tag)  # before conversion
        try:
            if not little and isinstance(raw, RawDataElement) and raw.VR == "UN":
                out[f"{tag:08X}"] = convert_raw_data_element(raw._replace(is_little_endian=True)).to_json_dict(
                    None, 1 << 30)
                continue
            if not little and ds[tag].VR == "SQ":  # its items, whose explicit UN values too
                out[f"{tag:08X}"] = {"vr": "SQ", "Value": [pydicom_json(x, little) for x in ds[tag].value]}
                continue
            out[f"{tag:08X}"] = ds[tag].to_json_dict(None, 1 << 30)
        except Exception:
            out[f"{tag:08X}"] = {"vr": raw.VR, "InlineBinary": base64.b64encode(raw.value).decode()}
    return out


def array_bytes(root, uri: str) -> bytes:
    """The value an array of vzip_source holds, as the file stores it: `uri` is
    its path, then #i for member i of a family or row i of a 2-D array."""
    path, _, index = uri.partition("#")
    node = root[path]
    if isinstance(node, zarr.Group):  # a family: offsets and data
        offsets = node["offsets"][...]
        data = node["data"][...].tobytes() if "data" in node else b""
        i = int(index)
        return data[offsets[i]:offsets[i + 1]]
    endian = getattr(node.metadata.codecs[0], "endian", None)
    order = ">" if endian is not None and getattr(endian, "value", endian) == "big" else "<"
    arr = node[int(index)] if index else node[...]
    return arr.astype(arr.dtype.newbyteorder(order)).tobytes()


def collapse(d: dict, path: str) -> dict:
    """A dataset at `path` with its duplicates applied in order: the last
    element of a tag wins, as pydicom reads them. Each attribute with its path."""
    out = {k: (v, f"{path}/{k}") for k, v in d.items() if k != "duplicates"}
    for j, dup in enumerate(d.get("duplicates", [])):
        out.update({k: (v, f"{path}/duplicates/{j}/{k}") for k, v in dup.items()})
    return out


def gathered_uri(path: str, sequence: str) -> str:
    """Where a value in an item of the gathered sequence at `sequence` is
    gathered (spec/virtualize/dicom.md §5): `path` is <sequence>/<i>/<p>."""
    i, rest = path[len(sequence) + 1:].split("/", 1)
    return f"vzip_source/{sequence}/items/{rest}/value#{i}"


def chunk_bytes(root, key: str) -> bytes:
    """A chunk's bytes, as the archive stores them."""
    return sync(root.store.get(key, default_buffer_prototype())).to_bytes()


def fragment_problems(root, source: dict, ds: pydicom.Dataset) -> list[str]:
    """Encapsulated pixel data without an Extended Offset Table, rebuilt from
    the frames' chunks, `pixel_fragments` and `pixel_offset_table`
    (spec/virtualize/dicom.md §5): the Basic Offset Table, then each
    fragment's item."""
    n = int(ds.get("NumberOfFrames", 1) or 1)
    axes = list(root["0"].metadata.dimension_names)
    across = -(-ds.TotalPixelMatrixColumns // ds.Columns) if ds.get("SOPClassUID") == WHOLE_SLIDE else None
    jpeg_rgb = ds.file_meta.TransferSyntaxUID == "1.2.840.10008.1.2.4.50" and ds.SamplesPerPixel == 3
    lengths: dict[int, list[int]] = {}
    if "pixel_fragments" in source:
        for f, length in array_values(root, source["pixel_fragments"]).reshape(-1, 2).tolist():
            lengths.setdefault(f, []).append(length)
    fragments, starts, at = [], [], 0
    for f in range(n):
        r, c = divmod(f, across) if across else (0, 0)
        coords = {"c": 0, "z": f, "t": f, "y": r, "x": c}
        stream = chunk_bytes(root, "0/c/" + "/".join(str(coords[a]) for a in axes))
        if jpeg_rgb:
            stream = b"\xff\xd8" + stream[ADOBE:]
        starts.append(at)
        for length in lengths.get(f, [len(stream)]):
            fragments.append(stream[:length])
            stream = stream[length:]
            at += 8 + length
        if stream:
            return [f"pixel_fragments: frame {f} has {len(stream)} bytes past its fragments"]
    bot = struct.pack(f"<{n}I", *starts) if source.get("pixel_offset_table") else b""
    rebuilt = b"".join(struct.pack("<HHI", 0xFFFE, 0xE000, len(x)) + x for x in [bot, *fragments])
    return [] if rebuilt == ds.PixelData else ["pixel_fragments: the rebuilt fragments differ"]


def array_values(root, uri: str) -> np.ndarray:
    """The values of an array of vzip_source, or row i of it."""
    path, _, index = uri.partition("#")
    return root[path][int(index)] if index else root[path][...]


def header_problems(root, data: bytes) -> list[str]:
    """Reconstruction (spec/virtualize/dicom.md §5): the source metadata, its
    node's meta and dataset and the arrays its BulkDataURIs name, against pydicom's DICOM
    JSON Model, without group lengths, layout and Pixel Data; then the preamble,
    the trailing bytes, native pixel data past the frames, what an Extended
    Offset Table skips, and every top-level element, duplicates included. An
    element that the dictionary leaves ambiguous (US or SS, OB or OW) is UN in
    ours, where pydicom resolves it."""
    source = root.attrs["vzip_virtualized"]["dicom"]
    problems = []
    if "trailing" in source:  # not elements: pydicom reads the file without them
        tail = array_bytes(root, source["trailing"])
        if not data.endswith(tail):
            problems.append("trailing: not the end of the file")
        data = data[: len(data) - len(tail)]
    ds = pydicom.dcmread(io.BytesIO(data))
    little = ds.file_meta.TransferSyntaxUID != "1.2.840.10008.1.2.2"
    preamble = base64.b64decode(source["preamble"]) if "preamble" in source else bytes(128)
    if preamble != data[:128]:
        problems.append("preamble")
    node = root["vzip_source"] if "vzip_source" in root else None
    meta, dataset = dict(source["meta"]), dict(source["dataset"])
    if node is not None and "vzip_virtualized" in node.attrs:
        meta.update(node.attrs["vzip_virtualized"]["dicom"].get("meta", {}))
        dataset.update(node.attrs["vzip_virtualized"]["dicom"].get("dataset", {}))

    def strip(d: dict) -> dict:
        return {k: v for k, v in d.items() if k not in ("7FE00010", "FFFCFFFC")}

    def layout(k: str) -> bool:
        """Group lengths and the Extended Offset Table, which ours omits where
        they are layout (spec/virtualize/dicom.md §5)."""
        return k.endswith("0000") or k in LAYOUT

    def resolve(k: str, a: dict, charsets, path: str, sequence: str | None) -> dict:
        """Our attribute as pydicom gives it from the bytes we keep: those of an
        array, or of a value we keep unsplit (a multibyte character set, or not
        whole values); a gathered DS or IS value's typed values as its numbers."""
        uri = gathered_uri(path, sequence) if a.get("Gathered") else a.get("BulkDataURI")
        node = root[uri.partition("#")[0]] if uri is not None else None
        if isinstance(node, zarr.Array) and a["vr"] in ("DS", "IS") and node.dtype != np.uint8:
            return {"vr": a["vr"], "Value": array_values(root, uri).tolist()}
        # The byte order of the bytes we keep: an array's own, else the
        # dataset's, unless they are an explicit UN value's (LittleEndian).
        order = little or bool(a.get("LittleEndian"))
        if uri is not None:
            raw = array_bytes(root, uri)
            endian = getattr(node.metadata.codecs[0], "endian", None) if isinstance(node, zarr.Array) else None
            order = order if endian is None else getattr(endian, "value", endian) == "little"
        elif "InlineBinary" in a and a["vr"] not in BINARY:
            raw = base64.b64decode(a["InlineBinary"])
        else:
            return a
        vr = a["vr"]
        if vr in BINARY or vr == "SQ" or len(raw) % SIZES.get(vr, 1):
            return {"vr": vr, "InlineBinary": base64.b64encode(raw).decode()}
        el = convert_raw_data_element(RawDataElement(BaseTag(int(k, 16)), vr, len(raw), raw, 0, False, order),
                                      encoding=charsets)
        return el.to_json_dict(None, 1 << 30)

    def same(a, b) -> bool:
        """Equal, with a DS value we keep as text equal to pydicom's number, and
        an empty value (null, PS3.18 §F.2.5) equal to pydicom's ""."""
        if a is None and b == "":
            return True
        if isinstance(a, dict) and isinstance(b, dict):
            return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
        if isinstance(a, list) and isinstance(b, list):
            return len(a) == len(b) and all(map(same, a, b))
        if isinstance(a, str) and isinstance(b, (int, float)) and not isinstance(b, bool):
            try:
                return float(a) == b
            except ValueError:
                return False
        return a == b

    def same_text(a: dict, b: dict) -> bool:
        """Our IS or DS values against the bytes of one that pydicom cannot
        convert (an integer of over 4300 digits): each the text, a number
        equal to it, or an integer string without its sign and leading zeros."""
        parts = [p.strip(" \0") for p in base64.b64decode(b["InlineBinary"]).decode("latin-1").split("\\")]
        values = a.get("Value", [])
        if len(values) != len(parts):
            return False
        for x, p in zip(values, parts):
            if isinstance(x, str):
                digits = p.lstrip("+-").lstrip("0")
                if x not in (p, ("-" if p.startswith("-") else "") + digits):
                    return False
            elif float(p) != x:
                return False
        return True

    def compare(ours, theirs, where: str, charsets, path: str, sequence: str | None = None) -> list[str]:
        """`sequence`: the path of the gathered sequence that `ours` is in, if any."""
        out = []
        ours = collapse(ours, path)
        if "00080005" in ours:  # Specific Character Set: this dataset's, and its items'
            charsets = convert_encodings([v or "" for v in ours["00080005"][0].get("Value", [])])
        for k in sorted(set(ours) | set(theirs)):
            (a, at), b = ours.get(k, (None, None)), theirs.get(k)
            if a is None and layout(k):
                continue
            if a is None or b is None:
                out.append(f"{where}{k}: only in {'pydicom' if a is None else 'ours'}")
                continue
            if "Structures" in a:  # gathered: each item's structure, whose values are arrays
                structure = root[f"vzip_source/{at}/items/structure"][...].tolist()
                a = {"vr": a["vr"], "Value": [a["Structures"][j] for j in structure]}
            a = resolve(k, a, charsets, at, sequence)
            if a.get("vr") == "UN" and dictionary_vr(int(k, 16)) is None and b.get("vr") != "UN":
                continue  # ambiguous in the dictionary
            if b.get("vr") == "UN" and a.get("InlineBinary") == b.get("InlineBinary"):
                continue  # not whole values, which pydicom reads as UN
            if a.get("vr") == "SQ" and "InlineBinary" in a and b.get("vr") == "SQ":
                continue  # a sequence that breaks a rule: its bytes, which pydicom reads leniently
            if a.get("vr") in ("IS", "DS") and "Value" in a and "InlineBinary" in b and same_text(a, b):
                continue
            if a.get("vr") == "SQ" and b.get("vr") == "SQ" and len(a.get("Value", [])) == len(b.get("Value", [])):
                inner = at if "Structures" in ours[k][0] else sequence
                for i, (x, y) in enumerate(zip(a.get("Value", []), b.get("Value", []))):
                    out += compare(strip(x), strip(y), f"{where}{k}[{i}]/", charsets, f"{at}/{i}", inner)
            elif not same(a, b):
                out.append(f"{where}{k}: {str(a)[:60]} vs {str(b)[:60]}")
        return out

    problems += compare(strip(meta), strip(ds.file_meta.to_json_dict(bulk_data_threshold=1 << 30)),
                        "meta/", None, "meta")
    problems += compare(strip(dataset), strip(pydicom_json(ds, little)), "", None, "dataset")

    # Every top-level element, duplicates included, as pydicom walks them.
    fp = io.BytesIO(data)
    read_preamble(fp, False)
    _read_file_meta_info(fp)
    walked = Counter(f"{e.tag:08X}" for e in data_element_generator(fp, ds.file_meta.TransferSyntaxUID == "1.2.840.10008.1.2", little))
    ours = Counter(k for k in dataset if k != "duplicates")
    ours.update(k for d in dataset.get("duplicates", []) for k in d)
    walked = Counter({k: n for k, n in walked.items() if k in strip({k: 0})})
    for k in [k for k in walked if layout(k) and walked[k] - ours[k] == 1]:
        walked[k] -= 1  # the first, which is layout
    if +walked != +Counter({k: n for k, n in ours.items() if k in strip({k: 0})}):
        problems.append(f"top-level elements: {dict(walked)} vs {dict(ours)}")

    # Native pixel data past the frames, unless one 00 byte.
    n = int(ds.get("NumberOfFrames", 1) or 1)
    if ds.file_meta.TransferSyntaxUID in NATIVE:
        extra = ds.PixelData[n * ds.Rows * ds.Columns * ds.SamplesPerPixel * (ds.BitsAllocated // 8):]
        kept = array_bytes(root, source["pixel_extra"]) if "pixel_extra" in source else b""
        if kept != (extra if extra not in (b"", b"\0") else b""):
            problems.append(f"pixel_extra: {kept[:8]!r} vs {extra[:8]!r}")
    # The bytes between the frames' items of an Extended Offset Table: with
    # their item headers when one frame's is not an item of its length.
    elif "ExtendedOffsetTable" in ds:
        offsets = struct.unpack(f"<{n}Q", ds.ExtendedOffsetTable)
        lengths = struct.unpack(f"<{n}Q", ds.ExtendedOffsetTableLengths)
        fragments = ds.PixelData[8:]  # after the empty Basic Offset Table
        headers = all(fragments[o:o + 8] == struct.pack("<HHI", 0xFFFE, 0xE000, m) for o, m in zip(offsets, lengths))
        if source.get("pixel_unreferenced_headers", False) == headers:
            problems.append(f"pixel_unreferenced_headers: {source.get('pixel_unreferenced_headers')}")
        items = [(o, 8 + m) if headers else (o + 8, m) for o, m in zip(offsets, lengths)]
        cursor, gaps = 0, []
        for o, length in sorted(items):
            gaps.append(fragments[cursor:o])
            cursor = max(cursor, o + length)
        kept = [b""] * n
        if "pixel_unreferenced" in source:
            kept = [array_bytes(root, f"{source['pixel_unreferenced']}#{k}") for k in range(n)]
        if kept != gaps:
            problems.append(f"pixel_unreferenced: {kept} vs {gaps}")
    else:
        problems += fragment_problems(root, source, ds)
    return problems


def check(name: str, url: str, data: bytes, tmp: Path) -> tuple[bool, str]:
    out = tmp / "out.vzip"
    out.unlink(missing_ok=True)
    p = subprocess.run(["node", str(ROOT / "js" / "conformance" / "virtualize.ts"), "--allow-private-hosts", url, str(out)],
                       capture_output=True, text=True)
    if name.startswith("dicom_reject"):
        ok = p.returncode == 3 and not out.exists()
        return ok, ("rejected: " + p.stderr.strip()[-90:]) if ok else "NOT REJECTED " + p.stderr[-200:]
    if p.returncode:
        return False, f"FAILED: {p.stderr.strip()[-300:]}"
    root = zarr.open_group(VZipStore(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
    header = header_problems(root, data)
    if header:
        return False, f"HEADER: {header[:3]}"
    arr = root["0"]
    got = arr[...]
    want = expected(pydicom.dcmread(io.BytesIO(data)))
    if got.dtype != want.dtype or got.shape != want.shape:
        return False, f"{got.dtype} {got.shape} != {want.dtype} {want.shape}"
    if not np.array_equal(got, want):
        return False, f"pixels differ in {np.count_nonzero(got != want)} of {got.size} values"
    return True, f"ok {list(arr.metadata.dimension_names)} {got.shape} {got.dtype}"


def main(argv: list[str]) -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        if argv:
            for target in argv:
                if target.startswith(("http://", "https://")):
                    req = urllib.request.Request(target, headers={"User-Agent": "vzip-verify"})
                    data = urllib.request.urlopen(req, timeout=600).read()
                    url = target
                else:
                    data = Path(target).read_bytes()
                    server = Server(Path(target).resolve().parent)
                    url = server.base + Path(target).name
                ok, message = check(Path(target).stem, url, data, Path(tmp))
                failures += not ok
                print(f"{Path(target).name[:60]:60s} {message}")
        else:
            fixtures = ROOT / "fixtures" / "dicom"
            server = Server(fixtures)
            for path in sorted(fixtures.glob("*.dcm")):
                ok, message = check(path.stem, server.base + path.name, path.read_bytes(), Path(tmp))
                failures += not ok
                print(f"{path.name:44s} {message}")
    print(f"\n{failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
