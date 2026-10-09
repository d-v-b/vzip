"""The IR core: types, the invariants, the generic rebuild, references and the mirror."""

import io
import math

import pytest

from vzip.ir.check import Violation, check, rebuild
from vzip.ir.emit import Refs
from vzip.ir.mirror import Archive, load, mirror, rebuild_from_archive, tag_json
from vzip.ir.model import ALIAS, GAP, IR, Budget
from vzip.ir.types import decode, size
from vzip.virtualize.common import Output, Rejected


def test_decodes_every_kind_of_type():
    cases = [
        ("<u2", b"\x01\x02", 0x0201),
        (">u2[2]", b"\x00\x01\x00\x02", [1, 2]),
        ("<u4[1,2]", bytes([1, 0, 0, 0, 2, 0, 0, 0]), [[1, 2]]),
        ("i1[2]", b"\xff\x01", [-1, 1]),
        ("<f8", b"\x00" * 6 + b"\xf0\x7f", math.inf),
        ("ascii[3]", b"ab\0", "ab\0"),
        ("cstr[4]", b"ab\0c", "ab"),
        ("ascii[1]", b"\xe9", b"\xe9"),
        ("bytes[2]", b"\x00\x01", b"\x00\x01"),
        ("guid", bytes(range(16)), "03020100-0504-0706-0809-0a0b0c0d0e0f"),
        ("{a:<u2,b:cstr[2]}", b"\x05\x00x\0", {"a": 5, "b": "x"}),
        ("{a:u1}[2]", b"\x01\x02", [{"a": 1}, {"a": 2}]),
    ]
    for t, raw, expected in cases:
        assert size(t) == len(raw), t
        assert decode(t, raw) == expected, t


def test_rejects_an_unknown_type():
    with pytest.raises(ValueError):
        decode("u3", b"\0")


def test_rejects_bytes_of_the_wrong_size():
    with pytest.raises(ValueError):
        decode("<u4", b"\0")


def source() -> bytes:
    return bytes(range(64))


def toy() -> IR:
    """A struct of two values, a data element, a second data element over the same
    bytes, one overlapping in part, and bytes nobody claims."""
    data = source()
    ir = IR(len(data))
    s = ir.struct(ir.root, "head", [(0, 8)])
    ir.value(s, "magic", "ascii[4]", [(0, 4)], data[0:4])
    ir.value(s, "n", "<u4", [(4, 4)], data[4:8])
    ir.data(ir.root, "tiles/0", [(16, 16)], {"shape": [4, 4], "dtype": "uint8"}, [{"name": "bytes"}],
            [("src", 16, 16)])
    ir.data(ir.root, "tiles/1", [(16, 16)], {"shape": [4, 4], "dtype": "uint8"}, [{"name": "bytes"}],
            [("src", 16, 16)])
    ir.data(ir.root, "tiles/2", [(24, 16)], None, None, [("src", 24, 16), ("lit", b"\0")])
    return ir.finish()


def test_finish_makes_claims_of_claimed_bytes_aliases_and_fills_gaps():
    ir = toy()
    check(ir)
    kinds = {ir.path(i): ir.kind[i] for i in range(len(ir))}
    assert kinds["tiles/1"] == ALIAS and ir.target[ir.child(ir.root, "tiles/1")] == ir.child(ir.root, "tiles/0")
    assert kinds["tiles/2"] == ALIAS
    assert {p: ir.extents(ir.child(ir.root, p)) for p, k in kinds.items() if k == GAP} == {
        "gaps/8": [(8, 8)], "gaps/32": [(32, 32)]}
    out = io.BytesIO()
    assert rebuild(ir, lambda o, n: source()[o:o + n], out.write) == 64
    assert out.getvalue() == source()


def broken(edit) -> IR:
    ir = IR(16)
    edit(ir)
    ir.finished = True
    return ir


def test_check_reports_bytes_claimed_twice():
    def edit(ir):
        ir.value(ir.root, "a", "bytes[10]", [(0, 10)])
        ir.value(ir.root, "b", "bytes[10]", [(6, 10)])
    with pytest.raises(Violation, match=r"bytes \[6, 10\) are claimed by element 1 .* and element 2"):
        check(broken(edit))


def test_check_reports_bytes_nobody_claims():
    with pytest.raises(Violation, match=r"bytes \[8, 16\) are claimed by no element"):
        check(broken(lambda ir: ir.value(ir.root, "a", "bytes[8]", [(0, 8)])))


def test_check_reports_a_claim_outside_the_source():
    with pytest.raises(Violation, match="outside"):
        check(broken(lambda ir: ir.value(ir.root, "a", "bytes[20]", [(0, 20)])))


def test_check_reports_an_alias_of_nothing():
    def edit(ir):
        ir.gap(ir.root, "g", [(0, 16)])
        ir.alias(ir.root, "a", 99)
    with pytest.raises(Violation, match="names no element"):
        check(broken(edit))


def test_check_reports_a_child_before_its_parent():
    def edit(ir):
        ir.gap(ir.root, "g", [(0, 16)])
        ir.parent[1] = 5
    with pytest.raises(Violation, match="has parent 5"):
        check(broken(edit))


def test_budget_rejects_too_many_records():
    ir = IR(0, Budget(0))
    ir.budget.limits["records"] = 3
    ir.struct(ir.root, "a")
    ir.struct(ir.root, "b")
    with pytest.raises(Rejected, match="records"):
        ir.struct(ir.root, "c")


def test_references_charge_repeats_through_aliases():
    ir = toy()
    out = Output("u")
    refs = Refs(ir, out)
    refs.chunk("a", ir.child(ir.root, "tiles/0"))
    refs.chunk("b", ir.child(ir.root, "tiles/1"))  # the same bytes, by alias
    assert ir.budget.spent["repeats"] == 1
    assert out.refs == {"a": [(16, 16)], "b": [(16, 16)]}
    ir.budget.limits["repeats"] = 1
    with pytest.raises(Rejected, match="repeats"):
        refs.chunk("c", ir.child(ir.root, "tiles/0"))


def test_tags_values_json_cannot_hold():
    assert tag_json({"$x": [2**60, float("nan"), b"\x01"], "a": "b"}) == {
        "$$x": [{"$vz": "int", "v": str(2**60)}, {"$vz": "float", "v": "NaN"}, {"$vz": "bytes", "b64": "AQ=="}],
        "a": "b"}


def test_rebuilds_the_source_from_the_mirror_alone(tmp_path):
    ir = toy()
    out = Output("file:///toy")
    mirror(ir, out, "tiff")
    out.write(str(tmp_path / "a.vzip"))
    archive = Archive(str(tmp_path / "a.vzip"), lambda o, n: source()[o:o + n])
    stored = load(archive)
    assert list(stored.kind) == list(ir.kind) and stored.name == ir.name
    buf = io.BytesIO()
    rebuild_from_archive(str(tmp_path / "a.vzip"), lambda o, n: source()[o:o + n], buf.write)
    assert buf.getvalue() == source()
    tree = archive.json("vzip_source/tree/zarr.json")["attributes"]["vzip_virtualized"]["tiff"]
    assert tree["head"] == {"magic": "\0\x01\x02\x03", "n": 0x07060504}


def test_a_mirror_past_the_row_budget_is_rejected():
    """A 4-byte source whose IR stands for 4,200,001 elements (a run of empty structs):
    the mirror is rejected (conventions §8.3), as Rejected, before the run is expanded."""
    import vzip_ir

    ir = vzip_ir.Ir.from_table(4, [0, 0, 5], [-1, 0, 0], [0, 0, 0], [0, 0, 4], [0, 0, 0], [(1, 4_200_000, 0)], [])
    with pytest.raises(vzip_ir.Rejected, match=r"^budget: the mirror would have more than 4194305 rows$"):
        vzip_ir.mirror(ir, "tiff")


def test_the_fill_batch_is_empty_on_every_probe(probes):
    """Every parser keeps every value the view shows (conventions §8.7): on every probe
    the run accepts, the batch that reads what a parser did not keep is empty."""
    from vzip.ir import virtualize as via_ir

    seen = 0
    for path in sorted(probes.iterdir()):
        try:
            _, out, _ = via_ir(str(path), "u")
        except Rejected:
            continue
        seen += 1
        assert out.summary["planner"]["fill"] == {"ranges": 0, "requests": 0, "bytes": 0}, path.name
    assert seen > 0
