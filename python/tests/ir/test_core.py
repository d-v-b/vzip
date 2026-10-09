"""The IR core: types, the invariants, the generic rebuild, references and the mirror."""

import io
import math

import pytest

from conftest import FIXTURES

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


def table(**edit):
    """A 30-byte source as a stored table (the Rust IR): the root, `a` (a value of 10
    bytes) and `gaps/10` (a gap of 20); `edit` replaces columns."""
    import vzip_ir

    cols = dict(size=30, kind=[0, 1, 5], parent=[-1, 0, 0], start=[0, 0, 10], len=[30, 10, 20],
                space=[0, 0, 0], runs=[], targets=[], name=["", "a", "gaps/"], nidx=[None, None, 10])
    cols.update(edit)
    return vzip_ir.Ir.from_table(**cols)


def test_the_root_spans_the_source_and_every_path_is_its_own():
    """The root (element 0) is a struct named "" with no index spanning the source,
    whose path is ""; full names differ among siblings, and none is another's followed
    by "/" (conventions §8.1): siblings sharing a prefix, names holding "/", an empty
    name with an index, one name under two parents, a run's members."""
    ir = table(
        size=12,
        kind=[0, 1, 1, 0, 1, 1, 0, 1, 1, 1],
        parent=[-1, 0, 0, 0, 3, 3, 0, 6, 0, 0],
        start=[0, 0, 1, 2, 2, 3, 0, 4, 5, 9],
        len=[12, 1, 1, 2, 1, 1, 0, 1, 1, 3],
        space=[0] * 10,
        runs=[(8, 4, 1)],
        name=["", "a", "ab", "tags/", "entry", "a", "frames", "", "t", "t"],
        nidx=[None, None, None, 256, None, None, None, 0, 0, 4],
    )
    ir.check()
    assert ir.path(0) == "" and ir.name(0) == "" and ir.nidx(0) is None and ir.extent(0) == (0, 12)
    assert [ir.path(i) for i in (4, 5, 7, 8, 9)] == ["tags/256/entry", "tags/256/a", "frames/0", "t0", "t4"]


def test_a_named_root_is_a_violation():
    import vzip_ir

    with pytest.raises(vzip_ir.Violation, match=r'^the root \(element 0\) is named "r", not ""$'):
        table(name=["r", "a", "gaps/"]).check()


def test_a_root_with_an_index_is_a_violation():
    import vzip_ir

    with pytest.raises(vzip_ir.Violation, match=r"^the root \(element 0\) has the name index 0$"):
        table(nidx=[0, None, 10]).check()


def test_a_root_that_does_not_span_the_source_is_a_violation():
    import vzip_ir

    with pytest.raises(vzip_ir.Violation, match=r"^the root's extent is \(0, 0\), not \(0, 30\)"):
        table(len=[0, 10, 20]).check()


def test_an_empty_full_name_is_a_violation():
    import vzip_ir

    with pytest.raises(vzip_ir.Violation, match=r'^element 1 \(value ""\) has an empty full name$'):
        table(name=["", "", "gaps/"]).check()


def test_siblings_of_one_path_are_a_violation():
    import vzip_ir

    with pytest.raises(vzip_ir.Violation, match=r'have one path, "a"$'):
        table(name=["", "a", "a"], nidx=[None, None, None]).check()


def test_a_full_name_that_continues_a_siblings_with_a_slash_is_a_violation():
    import vzip_ir

    with pytest.raises(vzip_ir.Violation, match=r'^the full name "a/0" of element 2'):
        table(name=["", "a", "a/"], nidx=[None, None, 0]).check()


def test_the_mirror_of_an_ir_whose_names_break_the_rules_is_rejected():
    """The producer writes no invalid table: Rejected, as any rejection."""
    import vzip_ir

    with pytest.raises(vzip_ir.Rejected, match=r'have one path, "a"$'):
        vzip_ir.mirror(table(name=["", "a", "a"], nidx=[None, None, None]), "tiff")


def test_an_archive_whose_root_is_named_is_a_violation():
    """A reader of an archive whose mirror names its root rejects it (Violation), and
    the validator calls the table invalid."""
    import json

    import vzip_ir

    from vzip.ir import virtualize as via_ir

    path = FIXTURES / "tiff" / "edge_duplicate_tags.tif"
    data = path.read_bytes()
    _, out, _ = via_ir(str(path), "https://data.test/a.tif")
    doc = json.loads(out.bytes_entries["vzip_source/zarr.json"])
    ir = doc["attributes"]["vzip_virtualized"]["tiff"]["ir"]
    assert ir["names"][0] == ""
    ir["names"][0] = "!"  # still sorted first
    entries = dict(out.bytes_entries, **{"vzip_source/zarr.json": json.dumps(doc).encode()})
    loaded = vzip_ir.Ir.from_archive(entries.get, lambda o, n: data[o:o + n])
    with pytest.raises(vzip_ir.Violation, match=r'^the root \(element 0\) is named "!", not ""$'):
        loaded.check()
    problem = vzip_ir.canonical_problem(entries.get, lambda o, n: data[o:o + n])
    assert problem == 'invalid: the root (element 0) is named "!", not ""'


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
