"""The virtualization conventions (VIRTUALIZE.md conventions §2) on every synthetic file and store.

Every fixture is virtualized by both implementations (the browser one under
Node, through web/conformance/documents.ts). In each accepted output, every
node that declares the profile's convention validates against its JSON Schema
(conventions/<profile>/schema.json): the root always declares it, with the
provenance members, and another node only when it has source metadata, with
only that member. No node has any other attribute besides `ome`.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from vzip.virtualize import Rejected, virtualize
from vzip.virtualize.common import CONVENTION_KEY, PROFILES, UUIDS, convention, declare

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "web" / "test" / "fixtures"
FILES = {"tiff": "*.tif", "ndpi": "*.ndpi", "nd2": "*.nd2", "dicom": "*.dcm", "nifti": "*.nii", "ims": "*.ims"}
STORES = ("n5", "zarr2", "ome-zarr")


def schema(profile: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((ROOT / "conventions" / profile / "schema.json").read_text()))


def inputs() -> list[tuple[Path, str]]:
    out = []
    for d, pattern in FILES.items():
        out += [(p, f"https://data.test/{d}/{p.name}") for p in sorted((FIXTURES / d).glob(pattern))]
    for d in STORES:
        out += [(p, f"https://data.test/{d}/{p.name}/") for p in sorted((FIXTURES / d).iterdir()) if p.is_dir()]
    return out


def python_docs(path: Path, url: str):
    """(format, {key: zarr.json document}), or None if the input is rejected."""
    try:
        fmt, out = virtualize(str(path), url=url)
    except Rejected:
        return None
    if hasattr(out, "docs"):
        return fmt, dict(out.docs)
    return fmt, {k: json.loads(v) for k, v in out.bytes_entries.items() if k.endswith("zarr.json")}


def declares(doc: dict) -> bool:
    attrs = doc.get("attributes", {})
    cmos = attrs.get("zarr_conventions", [])
    return any(isinstance(c, dict) and c.get("uuid") in UUIDS for c in cmos)


def test_schemas_describe_the_conventions():
    for profile in PROFILES:
        validator = schema(profile)
        Draft202012Validator.check_schema(validator.schema)
        cmo = validator.schema["$defs"]["conventionMetadata"]["properties"]
        assert {k: v["const"] for k, v in cmo.items()} == convention(profile), profile
        assert validator.schema["$id"] == convention(profile)["schema_url"], profile


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node for the browser implementation")
def test_every_output_declares_its_profile_where_the_convention_says():
    cases = inputs()
    p = subprocess.run(["node", str(ROOT / "web" / "conformance" / "documents.ts")], cwd=ROOT,
                       input=json.dumps([[str(path), url] for path, url in cases]), capture_output=True,
                       text=True, check=True)
    web = json.loads(p.stdout)
    accepted: dict[str, int] = {}
    for (path, url), w in zip(cases, web, strict=True):
        py = python_docs(path, url)
        assert (py is None) == ("rejected" in w), (path.name, w.get("rejected"))
        if py is None:
            continue
        fmt, docs = py
        assert fmt == w["format"], path.name
        validator = schema(fmt)
        for impl, d in (("py", docs), ("web", w["docs"])):
            assert d["zarr.json"]["attributes"][CONVENTION_KEY]["source"] == {"url": url}, (impl, path.name)
            for key, doc in d.items():
                attrs = doc.get("attributes", {})
                assert set(attrs) <= {"ome", "zarr_conventions", CONVENTION_KEY}, (impl, path.name, key)
                assert declares(doc) == (CONVENTION_KEY in attrs), (impl, path.name, key)
                if key == "zarr.json":
                    assert declares(doc), (impl, path.name)
                if declares(doc):
                    errors = [e.message for e in validator.iter_errors(doc)]
                    assert not errors, (impl, path.name, key, errors)
                    assert ("profile" in attrs[CONVENTION_KEY]) == (key == "zarr.json"), (impl, path.name, key)
        assert docs == w["docs"], path.name
        accepted[fmt] = accepted.get(fmt, 0) + 1
    assert set(accepted) == set(PROFILES), accepted
    print(f"schema-validated roots: {sum(accepted.values())} of {len(cases)} inputs, {accepted}")


def test_declare():
    url = "https://data.test/x/"
    root = {"profile": "zarr2", "version": 1, "source": {"url": url}}
    copied = {"zarr_conventions": [{"name": "proj:"}], "proj:code": "EPSG:4326"}
    for attrs, url_, own, expected in [
        # the root, with and without source metadata
        ({"ome": {}}, url, None, {"ome": {}, "zarr_conventions": [convention("zarr2")], CONVENTION_KEY: root}),
        ({}, url, copied, {"zarr_conventions": [convention("zarr2")], CONVENTION_KEY: {**root, "zarr2": copied}}),
        # another node: declared only when it has source metadata
        ({"ome": {}}, None, copied, {"ome": {}, "zarr_conventions": [convention("zarr2")],
                                     CONVENTION_KEY: {"zarr2": copied}}),
        ({"ome": {}}, None, {}, {"ome": {}}),
        ({}, None, None, {}),
    ]:
        assert declare(attrs, "zarr2", url_, own) == expected, (attrs, url_, own)
