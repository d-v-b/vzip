"""The IR core through its Python bindings: the mirror's budget, the root and the
names' invariants, and the fill batch."""

import pytest

from conftest import FIXTURES

from vzip.virtualize.common import Rejected


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
