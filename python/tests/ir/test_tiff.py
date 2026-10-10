"""The TIFF IR (round 3): the Rust parser and its schema, the image projection against
today's profile, the compact mirror, and the amplification probes."""

import io

import pytest

from conftest import FIXTURES, same_hierarchy
from vzip.ir import virtualize as via_ir
from vzip.ir.cmirror import rebuild_from_archive
from vzip.virtualize import Rejected
from vzip_reference import virtualize  # the frozen reference (conftest)

TIFFS = sorted((FIXTURES / "tiff").iterdir())


@pytest.mark.parametrize("path", TIFFS, ids=lambda p: p.name)
def test_projects_as_today_and_rebuilds(path, tmp_path):
    data = path.read_bytes()
    url = f"https://data.test/{path.name}"
    try:
        _, today = virtualize(str(path), url)
    except Rejected:
        with pytest.raises(Rejected):
            via_ir(str(path), url)
        return
    fmt, out, ir = via_ir(str(path), url)
    assert fmt == "tiff"
    ir.check()
    same_hierarchy(today, out)
    out.write(str(tmp_path / "a.vzip"))
    buf = io.BytesIO()
    rebuild_from_archive(str(tmp_path / "a.vzip"), lambda o, n: data[o:o + n], buf.write)
    assert buf.getvalue() == data


def test_probes_decide_as_today(probes):
    """Loops, huge counts and too many IFDs are rejected where today rejects them;
    pointer loops are aliases; every IR the parser returns is checked."""
    for path in sorted(probes.glob("tiff_*")):
        url = f"https://data.test/{path.name}"
        try:
            _, today = virtualize(str(path), url)
        except Rejected:
            with pytest.raises(Rejected):
                via_ir(str(path), url, mirror=False)
            continue
        _, out, ir = via_ir(str(path), url, mirror=False)
        ir.check()
        same_hierarchy(today, out)


def kinds(ir, prefix):
    return [ir.kind(i) for i in range(len(ir)) if ir.path(i).startswith(prefix)]


def test_planes_and_tiles_that_share_bytes_are_aliases(probes):
    _, out, ir = via_ir(str(probes / "tiff_amp_100_100.tif"), "u", mirror=False)
    planes = [i for i in range(len(ir)) if ir.path(i).startswith("ome/planes")]
    assert len(planes) == 100 and all(ir.kind(i) == 4 for i in planes)
    tiles = ir.child(ir.child(0, "ifds/0"), "tiles")
    ks, starts, lengths, _ = ir.members(tiles)
    assert len(ks) == 100 and set(starts) == {starts[0]}
    assert len(out.refs) == 100 * 100


def test_ifds_that_share_a_tile_table_name_it_by_alias(probes):
    _, out, ir = via_ir(str(probes / "tiff_shared_1000_1024.tif"), "u", mirror=False)
    tiles = [ir.child(ir.child(0, f"ifds/{k}"), "tiles") for k in range(1000)]
    assert ir.kind(tiles[0]) == 0 and all(ir.kind(t) == 4 and ir.target(t) == tiles[0] for t in tiles[1:])
    assert len(ir) < 50000  # about 40 per IFD (entries and values), none per tile: not 1000 x 1024
