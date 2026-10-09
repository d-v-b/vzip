import json
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parents[3] / "fixtures"
ROOT = Path(__file__).parents[3]
# the frozen reference twins the IR is compared against (vzip_reference)
sys.path.insert(0, str(ROOT / "conformance" / "virtualize" / "reference"))


def same_hierarchy(a, b) -> None:
    """The frozen reference's output `a` and the IR's `b` hold the same hierarchy but
    vzip_source: documents, references and data sources. The reference's root records
    the revision it implements (vzip_reference.revision), read as the current one."""
    from vzip.virtualize.common import REVISION
    from vzip_reference.revision import REVISION as FROZEN

    def docs(o):
        d = {k: json.loads(v) for k, v in o.bytes_entries.items() if not k.startswith("vzip_source")}
        prop = d.get("zarr.json", {}).get("attributes", {}).get("vzip_virtualized", {})
        if o is a and prop.get("revision") == FROZEN:
            prop["revision"] = REVISION
        return d

    def refs(o):
        return {k: v for k, v in o.refs.items() if not k.startswith("vzip_source")}

    assert docs(a) == docs(b)
    assert refs(a) == refs(b)
    assert list(a.data) == list(b.data)


@pytest.fixture(scope="session")
def probes(tmp_path_factory) -> Path:
    """The round-3 probes at the tests' sizes (python/tests/ir/gen_probes.py --small)."""
    out = tmp_path_factory.mktemp("probes")
    subprocess.run([sys.executable, "python/tests/ir/gen_probes.py", str(out), "--small"], cwd=ROOT,
                   check=True, capture_output=True)
    return out
