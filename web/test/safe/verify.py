"""Checks the SAFE virtualizers' outputs against an independent reader (GDAL).

Each product in web/test/fixtures/safe/ (a directory, served by the harness
proxy with its S3 listing, or a `.SAFE.zip` file) is virtualized by the
Python reference (`python -m vzip.virtualize`) and by the browser code
(web/conformance/virtualize.ts, under Node). Each archive is opened through
VZipStore and zarr-python (with vzip.codecs' imagecodecs_jpeg2k), and:

- every band array, read whole, must equal its band file as rasterio's
  JP2OpenJPEG driver (GDAL, OpenJPEG) decodes it;
- each resolution group's `spatial:transform`, `spatial:shape` and
  `proj:code` must be GDAL's geotransform, size and CRS of each of its band
  files (from their GML-in-JP2), and `proj:wkt2` must be the same CRS as
  pyproj's EPSG definition;
- the product must be rebuilt from the hierarchy (conventions/safe/README.md
  §6.4): every XML document from its text or its array, every other object
  from `vzip_source/objects/`, the empty objects from their keys, and every
  band file from its JP2 header array, its `siz` and its chunks, byte for
  byte, with nothing left over (folder markers excepted);
- `safe_reject_*` products must be rejected by both.

With `--remote <product URL> [<band path> ...]`, a public product (a `.SAFE/`
directory, or a zip file, read through the harness proxy) is virtualized by
both implementations,
and the listed band arrays (default: the 60 m group) are compared with GDAL
reading the band files over HTTP (/vsicurl/), and, for a directory, with
GDAL's SENTINEL2 driver reading the product metadata; the rebuild check
covers the XML documents, the other objects and the listed bands' files.

Usage: uv run python web/test/safe/verify.py [<fixture> ...] [--py-only]
       uv run python web/test/safe/verify.py --remote <url> [<band path> ...] [--py-only]
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import rasterio
import zarr
from pyproj import CRS

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
FIXTURES = HERE.parent / "fixtures"
sys.path.insert(0, str(ROOT / "conformance" / "virtualize"))
from proxy import Proxy  # noqa: E402

import vzip.codecs  # noqa: E402,F401  (registers imagecodecs_jpeg2k)
from vzip.policy import Policy  # noqa: E402
from vzip.store import VZipStore  # noqa: E402

# The sources are served on 127.0.0.1, which spec §8.7 rule 3 refuses by default.
LOOPBACK_SOURCES = Policy(allow_private_hosts=True)

ESCAPED = re.compile(r"(?:zarr\.json|\.zarray|\.zgroup)~+\Z")


def run(impl: str, url: str, out: Path) -> subprocess.CompletedProcess:
    cmd = (["uv", "run", "python", "-m", "vzip.virtualize", "--allow-private-hosts"] if impl == "py"
           else ["node", str(ROOT / "web" / "conformance" / "virtualize.ts"), "--allow-private-hosts"])
    return subprocess.run(cmd + [url, str(out)], capture_output=True, text=True, cwd=ROOT)


def product_files(path: Path) -> dict[str, bytes]:
    """The product's objects: a directory's files, or a zip file's entries under its root."""
    if path.is_dir():
        return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    z = zipfile.ZipFile(path)
    return {i.filename.partition("/")[2]: z.read(i) for i in z.infolist() if not i.filename.endswith("/")}


def product_dirs(path: Path) -> set[str]:
    """The directories the product names: by its folder markers (`d_$folder$`), or by its zip
    file's directory entries."""
    if path.is_dir():
        return {p.relative_to(path).as_posix()[: -len("_$folder$")] for p in path.rglob("*_$folder$")
                if p.is_file() and p.name != "_$folder$"}
    return {n.partition("/")[2][:-1] for n in zipfile.ZipFile(path).namelist() if n.endswith("/") and n.partition("/")[2]}


def parents(keys) -> set[str]:
    return {k[:i] for k in keys for i in range(len(k)) if k[i] == "/"}


def get_all(vz: VZipStore, keys: list[str]) -> list[bytes]:
    from zarr.core.buffer import default_buffer_prototype

    async def go():
        return [(await vz.get(k, default_buffer_prototype())).to_bytes() for k in keys]

    return asyncio.run(go())


def documents(out: Path) -> dict[str, dict]:
    z = zipfile.ZipFile(out)
    return {n: json.loads(z.read(n)) for n in z.namelist() if n.endswith("zarr.json")}


def gdal_read(data: bytes | str):
    """(pixels [c, h, w], transform, crs, width, height) of a band file, by GDAL's JP2OpenJPEG driver."""
    with rasterio.Env(GDAL_SKIP="JP2KAK JP2ECW JP2MrSID JP2Lura"):
        if isinstance(data, str):
            with rasterio.open(data, driver="JP2OpenJPEG") as ds:
                return ds.read(), ds.transform, ds.crs, ds.width, ds.height
        with rasterio.MemoryFile(data) as m, m.open(driver="JP2OpenJPEG") as ds:
            return ds.read(), ds.transform, ds.crs, ds.width, ds.height


def check_bands(out: Path, docs: dict, read_band, only: list[str] | None = None) -> tuple[list[str], int]:
    """Every band array (or those of `only`) against GDAL, and each group's georeferencing."""
    problems = []
    vz = VZipStore(str(out), policy=LOOPBACK_SOURCES)
    arrays = sorted(k[: -len("/zarr.json")] for k, d in docs.items()
                    if d.get("node_type") == "array" and re.match(r"r[0-9]+m/", k))
    if only is not None:
        arrays = [a for a in arrays if a in only or a.split("/")[0] in only]
    for path in arrays:
        group = docs[path.split("/")[0] + "/zarr.json"]["attributes"]
        s = docs[f"{path}/zarr.json"]["attributes"]["vzip_virtualized"]["safe"]
        want, transform, crs, w, h = read_band(s["IMAGE_FILE"] + ".jp2")
        got = zarr.open_array(vz, path=path, mode="r", zarr_format=3)[...]
        if got.ndim == 2:
            got = got[None]
        if got.shape != want.shape or got.dtype != want.dtype or not np.array_equal(got, want):
            problems.append(f"{path}: {got.dtype}{got.shape} differs from GDAL's {want.dtype}{want.shape}")
        if group["spatial:transform"] != list(transform)[:6]:
            problems.append(f"{path}: spatial:transform {group['spatial:transform']} is not GDAL's {list(transform)[:6]}")
        if group["spatial:shape"] != [h, w]:
            problems.append(f"{path}: spatial:shape {group['spatial:shape']} is not GDAL's {[h, w]}")
        if crs is None or group["proj:code"] != f"EPSG:{crs.to_epsg()}":
            problems.append(f"{path}: proj:code {group['proj:code']} is not GDAL's {crs}")
    for key, d in docs.items():
        a = d.get("attributes", {})
        if "proj:wkt2" in a:
            c = CRS.from_wkt(a["proj:wkt2"])
            if not c.equals(CRS.from_user_input(a["proj:code"])):
                problems.append(f"{key}: proj:wkt2 is not {a['proj:code']}")
    return problems, len(arrays)


def rebuild(out: Path, docs: dict, bands_only: list[str] | None = None) -> dict[str, bytes]:
    """The product's objects, rebuilt from the hierarchy (conventions/safe/README.md §6.4)."""
    vz = VZipStore(str(out), policy=LOOPBACK_SOURCES)
    z = zipfile.ZipFile(out)
    names = z.namelist()
    s = docs["vzip_source/zarr.json"]["attributes"]["vzip_virtualized"]["safe"]
    files: dict[str, bytes] = {}
    for k, t in s.get("xml", {}).items():
        files[k] = t.encode() if isinstance(t, str) else t["latin1"].encode("latin-1")
    for i, k in enumerate(s.get("xml_arrays", [])):
        files[k] = zarr.open_array(vz, path=f"vzip_source/xml/{i}", mode="r", zarr_format=3)[...].tobytes()
    for k in s.get("empty", []):
        files[k] = b""
    kept = [n for n in names if n.startswith("vzip_source/objects/")]
    for n, data in zip(kept, get_all(vz, kept)):
        key = n.removeprefix("vzip_source/objects/")
        last = key.rpartition("/")[2]
        files[key[:-1] if ESCAPED.match(last) else key] = data
    for k, d in docs.items():
        if d.get("node_type") != "array" or not re.match(r"r[0-9]+m/", k):
            continue
        path = k[: -len("/zarr.json")]
        if bands_only is not None and path not in bands_only and path.split("/")[0] not in bands_only:
            continue
        band = d["attributes"]["vzip_virtualized"]["safe"]
        head = zarr.open_array(vz, path=f"vzip_source/jp2/{path}", mode="r", zarr_format=3)[...].tobytes()
        siz = base64.b64decode(band["siz"])
        chunk_keys = sorted((n for n in names if n.startswith(path + "/c/")),
                            key=lambda n: tuple(int(x) for x in n.split("/c/")[1].split("/")))
        cols = -(-d["shape"][-1] // d["chunk_grid"]["configuration"]["chunk_shape"][-1])
        chunks = get_all(vz, chunk_keys)
        parts = [head, b"\xff\x4f", siz]
        rest = None
        for key, c in zip(chunk_keys, chunks):
            *_, v, u = (int(x) for x in key.split("/c/")[1].split("/"))
            lsiz = struct.unpack_from(">H", c, 4)[0]
            sot = c.index(b"\xff\x90\x00\x0a", 4 + lsiz)
            if rest is None:
                rest = c[4 + lsiz : sot]
                parts.append(rest)
            elif c[4 + lsiz : sot] != rest:
                raise AssertionError(f"{key}: the main header differs from the band's other chunks")
            psot = struct.unpack_from(">I", c, sot + 6)[0]
            parts.append(c[sot : sot + 4] + struct.pack(">H", v * cols + u) + c[sot + 6 : sot + psot])
        parts.append(b"\xff\xd9")
        files[band["IMAGE_FILE"] + ".jp2"] = b"".join(parts)
    return files


def check_rebuild(product: dict[str, bytes], out: Path, docs: dict, dirs: set[str] = frozenset()) -> list[str]:
    """The rebuilt objects against the product's, and its directories (`dirs`, those its folder
    markers or directory entries name): each must be a directory of a rebuilt object, or one
    of the empty directories rebuilt from `empty_dirs`. The folder markers are layout of the
    mirror, as directory entries are of a zip file, and are not rebuilt as objects."""
    files = rebuild(out, docs)
    want = {k: v for k, v in product.items() if not (k.endswith("_$folder$") and k[: -len("_$folder$")] in dirs)}
    problems = [f"{k}: not rebuilt" for k in sorted(set(want) - set(files))]
    problems += [f"{k}: rebuilt, but not in the product" for k in sorted(set(files) - set(want))]
    problems += [f"{k}: rebuilt with other bytes" for k in sorted(set(want) & set(files)) if want[k] != files[k]]
    empty = docs["vzip_source/zarr.json"]["attributes"]["vzip_virtualized"]["safe"].get("empty_dirs", [])
    kept = parents(files) | set(empty) | parents(empty)
    problems += [f"{d}/: a directory not rebuilt" for d in sorted(dirs - kept)]
    problems += [f"{d}/: rebuilt as an empty directory, but not one of the product" for d in empty
                 if d not in dirs or d in parents(files)]
    return problems


def fixtures(argv: list[str], impls: list[str]) -> int:
    items = [Path(a) for a in argv] or sorted(p for p in (FIXTURES / "safe").iterdir())
    proxy = Proxy(FIXTURES, Path(tempfile.mkdtemp()))
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for item in items:
            rel = item.resolve().relative_to(FIXTURES.resolve()).as_posix()
            url = proxy.local_store(rel) if item.is_dir() else proxy.local(rel)
            for impl in impls:
                out = Path(tmp) / f"{item.name}.{impl}.vzip"
                p = run(impl, url, out)
                label = f"{item.name[:44]:44s} {impl:3s}"
                if item.name.startswith("safe_reject"):
                    ok = p.returncode == 3 and not out.exists()
                    failures += not ok
                    print(f"{label} {'rejected: ' + p.stderr.strip()[-80:] if ok else 'NOT REJECTED ' + p.stderr[-200:]}")
                    continue
                if p.returncode:
                    failures += 1
                    print(f"{label} FAILED: {p.stderr.strip()[-300:]}")
                    continue
                product = product_files(item)
                docs = documents(out)
                problems, n = check_bands(out, docs, lambda key: gdal_read(product[key]))
                problems += check_rebuild(product, out, docs, product_dirs(item))
                failures += bool(problems)
                print(f"{label} {problems[:4] if problems else 'ok'} ({n} bands, {len(product)} objects rebuilt)")
    print(f"\n{failures} failures")
    return 1 if failures else 0


def sentinel2(url: str, docs: dict, out: Path, groups: list[str]) -> list[str]:
    """The bands of GDAL's SENTINEL2 driver at the groups' resolutions against the arrays, and
    its band metadata against the band arrays' source metadata."""
    problems = []
    s = docs["zarr.json"]["attributes"]["vzip_virtualized"]["safe"]
    level = "L2A" if s.get("PROCESSING_LEVEL", "").startswith("Level-2A") else "L1C"  # Level-2Ap: PSD 14.2
    code = docs["zarr.json"]["attributes"]["proj:code"].replace(":", "_")
    vz = VZipStore(str(out), policy=LOOPBACK_SOURCES)
    for g in groups:
        sub = f"SENTINEL2_{level}:/vsicurl/{url}MTD_MSI{level}.xml:{g[1:]}:{code}"
        try:
            ds = rasterio.open(sub)
        except Exception as e:  # noqa: BLE001
            problems.append(f"SENTINEL2 {g}: {e}")
            continue
        with ds:
            for i in range(1, ds.count + 1):
                tags = ds.tags(i)
                name = tags.get("BANDNAME", "")
                m = re.fullmatch(r"B([0-9]+|8A)", name)
                array = f"{g}/B{int(m.group(1)):02d}" if m and m.group(1) != "8A" else f"{g}/{name}"
                if f"{array}/zarr.json" not in docs:
                    continue
                want = ds.read(i)
                got = zarr.open_array(vz, path=array, mode="r", zarr_format=3)[...]
                if got.shape != want.shape or not np.array_equal(got, want):
                    problems.append(f"SENTINEL2 {array}: differs ({got.shape} vs {want.shape})")
                band = docs[f"{array}/zarr.json"]["attributes"]["vzip_virtualized"]["safe"]
                for tag, member in (("SOLAR_IRRADIANCE", "SOLAR_IRRADIANCE"), ("BOA_ADD_OFFSET", "BOA_ADD_OFFSET"),
                                    ("RADIO_ADD_OFFSET", "RADIO_ADD_OFFSET")):
                    if tag in tags and member in band:
                        v = band[member]["value"] if isinstance(band[member], dict) else band[member]
                        if float(tags[tag]) != float(v):
                            problems.append(f"SENTINEL2 {array}: {tag} {tags[tag]} != {v}")
                print(f"    SENTINEL2 {array}: compared, metadata {sorted(set(tags) & {'SOLAR_IRRADIANCE', 'BOA_ADD_OFFSET', 'RADIO_ADD_OFFSET', 'WAVELENGTH'})}")
    return problems


def remote(url: str, only: list[str], impls: list[str]) -> int:
    failures = 0
    if not url.endswith("/"):
        # A zip file is read through the harness proxy, which retries and caches its blocks: the
        # many range requests of the reads below otherwise meet dropped connections at some hosts.
        url = Proxy(FIXTURES, Path("/tmp/vzip-proxy-cache")).remote(url)
    with tempfile.TemporaryDirectory() as tmp:
        for impl in impls:
            out = Path(tmp) / f"remote.{impl}.vzip"
            t0 = time.time()
            p = run(impl, url, out)
            if p.returncode:
                print(f"{impl}: FAILED {p.stderr[-400:]}")
                failures += 1
                continue
            print(f"{impl}: {p.stdout.strip()[:600]} ({time.time() - t0:.0f} s, archive {out.stat().st_size} bytes)")
            docs = documents(out)
            groups = only or [min((k.split("/")[0] for k in docs if re.match(r"r[0-9]+m/zarr.json", k)),
                                  key=lambda g: -int(g[1:-1]))]
            if url.endswith("/"):
                problems, n = check_bands(out, docs, lambda key: gdal_read(f"/vsicurl/{url}{key}"), groups)
                problems += sentinel2(url, docs, out, [g for g in groups if "/" not in g])
            else:
                # A zip file: GDAL reads its band files through /vsizip/ over /vsicurl/.
                root = zip_entries(url)[0]
                problems, n = check_bands(out, docs, lambda key: gdal_read(f"/vsizip/{{/vsicurl/{url}}}/{root}/{key}"),
                                          groups)
            files = rebuild(out, docs, groups)
            problems += check_dirs(url, out, docs)
            bad = [k for k, data in files.items() if (k.endswith(".jp2") or len(data) < (1 << 22)) and fetch(url, k) != data]
            problems += [f"{k}: rebuilt with other bytes" for k in bad]
            failures += bool(problems)
            print(f"{impl}: {problems[:6] if problems else 'ok'} ({n} bands compared with GDAL, "
                  f"{len(files)} objects rebuilt and compared)")
    return 1 if failures else 0


def described_keys(out: Path, docs: dict) -> set[str]:
    """The keys of every object the hierarchy rebuilds, read from its documents and entry names."""
    s = docs["vzip_source/zarr.json"]["attributes"]["vzip_virtualized"]["safe"]
    keys = {*s.get("xml", {}), *s.get("xml_arrays", []), *s.get("empty", [])}
    for n in zipfile.ZipFile(out).namelist():
        if n.startswith("vzip_source/objects/"):
            key = n.removeprefix("vzip_source/objects/")
            keys.add(key[:-1] if ESCAPED.match(key.rpartition("/")[2]) else key)
    for k, d in docs.items():
        if d.get("node_type") == "array" and re.match(r"r[0-9]+m/", k):
            keys.add(d["attributes"]["vzip_virtualized"]["safe"]["IMAGE_FILE"] + ".jp2")
    return keys


def check_dirs(url: str, out: Path, docs: dict) -> list[str]:
    """Each directory a remote product names (by its folder markers, or its zip directory entries)
    is a directory of a rebuilt object or a rebuilt empty directory."""
    if url.endswith("/"):
        from vzip.virtualize.store import open_store

        dirs = {k[: -len("_$folder$")] for k in open_store(url).objects if k.endswith("_$folder$") and len(k) > 9}
    else:
        from vzip.virtualize.common import http_reader
        from vzip.virtualize.safe import zipdir

        read, size = http_reader(url)
        dirs = {k[:-1] for k in zipdir.product_entries(zipdir.central_directory(read, size))[3]}
    empty = docs["vzip_source/zarr.json"]["attributes"]["vzip_virtualized"]["safe"].get("empty_dirs", [])
    kept = parents(described_keys(out, docs)) | set(empty) | parents(empty)
    return [f"{d}/: a directory not rebuilt" for d in sorted(dirs - kept)]


_ZIPS: dict[str, tuple] = {}


def zip_entries(url: str) -> tuple:
    """(root, entries by key, reader) of a remote zip file, read once."""
    if url not in _ZIPS:
        from vzip.virtualize.common import http_reader
        from vzip.virtualize.safe import zipdir

        read, size = http_reader(url)
        d = zipdir.central_directory(read, size)
        root, entries, _, _ = zipdir.product_entries(d)
        zipdir.locate(read, d, entries)
        _ZIPS[url] = (root, entries, read)
    return _ZIPS[url]


def fetch(url: str, key: str) -> bytes:
    """An object of a remote product: by its URL (directory form), or by its entry's range."""
    from vzip.virtualize.store import object_url

    if url.endswith("/"):
        with urllib.request.urlopen(object_url(url, key), timeout=300) as r:
            return r.read()
    from vzip.virtualize.safe import zipdir

    _, entries, read = zip_entries(url)
    e = entries[key]
    data = read(e.ds, e.cs)
    return zipdir.inflate(data, e) if e.method == 8 else data


def main(argv: list[str]) -> int:
    impls = ["py"] if "--py-only" in argv else ["py", "web"]
    argv = [a for a in argv if a != "--py-only"]
    if argv[:1] == ["--remote"]:
        return remote(argv[1], argv[2:], impls)
    return fixtures(argv, impls)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
