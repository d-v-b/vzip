"""Checks the browser OME-Zarr virtualizer's output (spec/virtualize/ome-zarr/profile.md)
against ome-zarr-models and zarr-python.

Each store in fixtures/ome-zarr/ is served by the harness proxy (with
its S3 listing) and virtualized by js/conformance/virtualize.ts (the browser
code, run under Node). Then:

* every group of the output whose attributes have `ome` is validated with the
  ome-zarr-models 0.5 class its members call for (`plate`: HCS, `well`: Well,
  `image-label`: ImageLabel, other `multiscales`: Image, `labels`: Labels,
  `bioformats2raw.layout`: BioFormats2Raw);
* every OME group of the input is validated with the 0.4 class: an accepted
  store must validate; for a rejected one (`ome_zarr_reject_*`, which must be
  rejected) the script reports whether the 0.4 models reject it too, or do not
  cover the rule;
* every array of the archive is read through VZipStore and zarr-python as
  Zarr v3 and compared with the same array read from the fixture directory by
  zarr-python as Zarr v2 (js/test/zarr2/verify.py's check).

With `--remote <store URL> [<array path> ...]`, a public OME-Zarr 0.4 store
is virtualized directly, every output group is validated with the 0.5
models, and each named array is compared with zarr-python reading the Zarr v2
array over HTTP (the checks of conformance/virtualize/corpus_ome_zarr.txt).

Usage: uv run python js/test/ome-zarr/verify.py [<store dir> ...]
       uv run python js/test/ome-zarr/verify.py --remote <store URL> [<array path> ...]
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import warnings
import zipfile
from pathlib import Path

import numpy as np
import zarr
from ome_zarr_models import v04, v05
from zarr.storage import FsspecStore, LocalStore

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
FIXTURES = ROOT / "fixtures"
sys.path.insert(0, str(ROOT / "conformance" / "virtualize"))
from proxy import Proxy  # noqa: E402

import vzip.codecs  # noqa: E402,F401  (registers zlib)
from vzip.policy import Policy  # noqa: E402
from vzip.store import VZipStore  # noqa: E402

# The sources are served on 127.0.0.1, which spec §8.7 rule 3 refuses by default.
LOOPBACK_SOURCES = Policy(allow_private_hosts=True)

_spec = importlib.util.spec_from_file_location("zarr2_verify", HERE.parent / "zarr2" / "verify.py")
zarr2_verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(zarr2_verify)


def models(members: dict, version) -> list:
    """The ome-zarr-models classes for a group whose OME members are `members`."""
    out = []
    if "plate" in members:
        out.append(version.HCS)
    if "well" in members:
        out.append(version.Well)
    if "image-label" in members:
        out.append(version.ImageLabel)
    elif "multiscales" in members:
        out.append(version.Image)
    if "labels" in members:
        out.append(version.Labels)
    if "bioformats2raw.layout" in members:
        out.append(version.BioFormats2Raw)
    return out


def validate(open_group, groups: dict[str, dict], version) -> tuple[list[str], list[str], int]:
    """Validates each group {path: OME members} with its classes: (problems, warnings, groups validated)."""
    problems, notes, n = [], [], 0
    for path, members in sorted(groups.items()):
        for cls in models(members, version):
            n += 1
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                try:
                    cls.from_zarr(open_group(path))
                except Exception as e:  # noqa: BLE001
                    problems.append(f"{path or '/'} {cls.__module__.rsplit('.', 2)[-2]}.{cls.__name__}: "
                                    f"{str(e).splitlines()[0][:160]}")
            notes += sorted({f"{path or '/'}: {str(w.message)[:100]}" for w in caught
                             if "ValidationWarning" in type(w.message).__name__})
    return problems, notes, n


def output_groups(out: Path) -> dict[str, dict]:
    z = zipfile.ZipFile(out)
    groups = {}
    for name in z.namelist():
        if name.endswith("zarr.json"):
            doc = json.loads(z.read(name))
            if doc.get("node_type") == "group" and "ome" in doc.get("attributes", {}):
                groups[name[: -len("zarr.json")].rstrip("/")] = doc["attributes"]["ome"]
    return groups


def input_groups(store_dir: Path) -> dict[str, dict]:
    keys = ("multiscales", "omero", "labels", "image-label", "plate", "well", "bioformats2raw.layout")
    groups = {}
    for f in store_dir.rglob(".zattrs"):
        if (f.parent / ".zgroup").exists():
            try:
                a = json.loads(f.read_text())
            except ValueError:
                continue
            if isinstance(a, dict) and any(k in a for k in keys):
                groups[f.parent.relative_to(store_dir).as_posix().removeprefix(".")] = a
    return groups


def v05_check(out: Path) -> tuple[list[str], list[str], int]:
    vz = VZipStore(str(out), policy=LOOPBACK_SOURCES)
    return validate(lambda p: zarr.open_group(vz, path=p, mode="r", zarr_format=3), output_groups(out), v05)


def v04_check(store_dir: Path) -> tuple[list[str], list[str], int]:
    local = LocalStore(store_dir, read_only=True)
    return validate(lambda p: zarr.open_group(local, path=p, mode="r", zarr_format=2), input_groups(store_dir), v04)


def remote(store_url: str, paths: list[str]) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "remote.vzip"
        p = zarr2_verify.virtualize(store_url, out)
        if p.returncode:
            print(f"{'REJECTED' if p.returncode == 3 else 'FAILED'}: {p.stderr.strip()[-300:]}")
            return 1
        print(p.stdout.strip()[:400])
        problems, notes, n = v05_check(out)
        print(f"ome-zarr-models 0.5: {n} group validations, {len(problems)} failures")
        for x in problems[:5] + notes[:3]:
            print("   ", x)
        failures = len(problems)
        for path in paths:
            got = zarr.open_array(VZipStore(str(out), policy=LOOPBACK_SOURCES), path=path, mode="r", zarr_format=3)[...]
            want = zarr.open_array(FsspecStore.from_url(store_url, read_only=True), path=path, mode="r",
                                   zarr_format=2)[...]
            ok = zarr2_verify.same(got, want)
            failures += not ok
            print(f"{path}: {got.dtype}{got.shape}, {'equal to' if ok else 'DIFFERS from'} zarr-python's Zarr v2 "
                  f"reading, {int(np.count_nonzero(want))} nonzero values")
        return 1 if failures else 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--remote"]:
        return remote(argv[1], argv[2:])
    stores = [Path(a) for a in argv] or sorted(d for d in (FIXTURES / "ome-zarr").iterdir() if d.is_dir())
    proxy = Proxy(FIXTURES, Path(tempfile.mkdtemp()))
    failures = 0
    covered = uncovered = 0
    with tempfile.TemporaryDirectory() as tmp:
        for store_dir in stores:
            out = Path(tmp) / f"{store_dir.name}.vzip"
            p = zarr2_verify.virtualize(
                proxy.local_store(store_dir.resolve().relative_to(FIXTURES.resolve()).as_posix()), out)
            in_problems, _, _ = v04_check(store_dir)
            if "_reject_" in store_dir.name:
                ok = p.returncode == 3 and not out.exists()
                failures += not ok
                model = "0.4 models reject it too" if in_problems else "0.4 models do not cover the rule"
                covered += bool(in_problems)
                uncovered += not in_problems
                print(f"{store_dir.name:46s} {'rejected' if ok else 'NOT REJECTED ' + p.stderr[-200:]}; {model}")
                continue
            if p.returncode:
                failures += 1
                print(f"{store_dir.name:46s} FAILED: {p.stderr.strip()[-300:]}")
                continue
            fmt = json.loads(p.stdout)["format"]
            problems = [f"format {fmt}"] if fmt != "ome-zarr" else []
            problems += [f"input not valid 0.4: {x}" for x in in_problems]
            out_problems, notes, n = v05_check(out)
            problems += [f"output not valid 0.5: {x}" for x in out_problems]
            problems += zarr2_verify.check_store(store_dir, out, tmp)
            failures += bool(problems)
            arrays = len(zarr2_verify.arrays_of(out))
            print(f"{store_dir.name:46s} {problems[:3] if problems else 'ok'} ({arrays} arrays read, {n} groups "
                  f"valid 0.5{'; ' + '; '.join(notes[:2]) if notes else ''})")
    print(f"\n{failures} failures; rejected stores: 0.4 models reject {covered}, do not cover {uncovered}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
