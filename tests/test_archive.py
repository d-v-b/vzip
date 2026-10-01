import io
import zipfile

import pytest
import zarr
from zarr.abc.store import OffsetByteRequest, RangeByteRequest, SuffixByteRequest
from zarr.core.buffer import default_buffer_prototype
from zarr.core.sync import sync

from vzip.archive import VZipWriter
from vzip.pb import Range
from vzip.store import VZipStore


def _get(store, key, br=None):
    b = sync(store.get(key, default_buffer_prototype(), br))
    return None if b is None else b.to_bytes()


def test_overlay_semantics(tmp_path):
    target = tmp_path / "blob.bin"
    blob = bytes(range(256)) * 4
    target.write_bytes(blob)
    arc = tmp_path / "x.vzip"
    with open(arc, "wb") as f, VZipWriter(f) as w:
        w.add_bytes("a/zarr.json", b"{}")
        w.add_ref("a/c/0", f"file://{target}", 10, 100)
        w.add_ref("a/c/1", "blob.bin", 500, 20)  # relative to the archive
        ext = w.url(f"file://{target}")
        w.add_ranges("a/c/2", [Range(data=b"HEAD"), Range(source=ext, offset=0, length=4)])
        # internal references: a deflated hidden entry, and a forward reference to a
        # zarr key written later (checked when the archive is closed)
        hidden = w.internal(w.add_hidden("tail", b"0123456789"))
        w.add_ranges("a/c/3", [Range(source=hidden, offset=2, length=5),
                               Range(source=w.internal("a/late.bin"), length=2)])
        # a decoding header stored once in the source table, shared by two chunks
        hdr = w.blob(b"HDR:")
        w.add_ranges("a/c/4", [Range(source=hdr, length=4), Range(source=ext, offset=4, length=2)])
        w.add_ranges("a/c/5", [Range(source=hdr, offset=1, length=3), Range(source=ext, offset=8, length=1)])
        w.add_bytes("a/late.bin", b"{}", late=True)

    s = VZipStore(str(arc))
    cases = {
        ("a/zarr.json", None): b"{}",
        ("a/c/0", None): blob[10:110],
        ("a/c/0", RangeByteRequest(5, 15)): blob[15:25],
        ("a/c/0", OffsetByteRequest(90)): blob[100:110],
        ("a/c/0", SuffixByteRequest(3)): blob[107:110],
        ("a/c/1", None): blob[500:520],
        ("a/c/2", None): b"HEAD" + blob[:4],
        ("a/c/2", RangeByteRequest(2, 6)): b"AD" + blob[:2],
        ("a/c/3", None): b"23456{}",
        ("a/c/3", SuffixByteRequest(4)): b"56{}",
        ("a/c/4", None): b"HDR:" + blob[4:6],
        ("a/c/5", None): b"DR:" + blob[8:9],
        ("a/c/9", None): None,
        ("__vz__/sources", None): None,  # reserved, hidden from the resolved view
    }
    for (k, br), want in cases.items():
        assert _get(s, k, br) == want, (k, br)
    assert {k: s.classify(k) for k in ["a/zarr.json", "a/c/0", "a/c/2", "nope"]} == {
        "a/zarr.json": "bytes", "a/c/0": "ref", "a/c/2": "ref", "nope": None}
    # one range -> bare Range (0x7a76); several -> Concat (0x7a77)
    assert {k: type(s.entry(k).ref).__name__ for k in ["a/c/0", "a/c/1", "a/c/2", "a/c/3"]} == {
        "a/c/0": "Range", "a/c/1": "Range", "a/c/2": "Concat", "a/c/3": "Concat"}
    with zipfile.ZipFile(arc) as zf:
        assert zf.getinfo("a/c/0").extra[:2] == b"\x76\x7a"
        assert zf.getinfo("a/c/2").extra[:2] == b"\x77\x7a"

    naive = VZipStore(str(arc), resolve=False)
    with zipfile.ZipFile(arc) as zf:
        for name in zf.namelist():
            assert _get(naive, name) == zf.read(name)


def test_zip64_many_entries_paged(tmp_path):
    arc = tmp_path / "many.vzip"
    n = 70_000
    with open(arc, "wb") as f, VZipWriter(f, page_size=1 << 16) as w:
        w.add_bytes("zarr.json", b"{}", late=True)
        for i in range(n):
            w.add_ref(f"c/{i}", "file:///nowhere", i, 1)
    with zipfile.ZipFile(arc) as zf:
        assert len(zf.namelist()) == n + 3  # + urls, index
    s = VZipStore(str(arc))
    sync(s._open())
    opened = s.stats.archive_requests
    assert _get(s, "zarr.json") == b"{}"  # pinned: no page load
    assert sync(s.exists("c/69999")) and not sync(s.exists("c/69999x"))
    assert s.entry("c/69999").ref.offset == 69999
    assert s.stats.archive_requests - opened == 1  # one page, shared by both lookups
    assert len(s._loaded) == 1 < len(s._pages)
    keys = sync(_list(s))
    assert len(keys) == n + 1


async def _list(s):
    return [k async for k in s.list()]


def test_unregistered_source_rejected():
    with pytest.raises(ValueError, match="unregistered source"):
        VZipWriter(io.BytesIO()).add_ranges("k", [Range(source=0, length=1)])


def test_dangling_internal_reference_rejected():
    w = VZipWriter(io.BytesIO())
    w.add_ranges("k", [Range(source=w.internal("nope"), length=1)])
    with pytest.raises(ValueError, match="missing entries: \\['nope'\\]"):
        w.close()


def test_duplicate_key_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        w = VZipWriter(io.BytesIO())
        w.add_bytes("k", b"1")
        w.add_ref("k", "u", 0, 1)


def test_reserved_key_rejected():
    with pytest.raises(ValueError, match="reserved"):
        VZipWriter(io.BytesIO()).add_bytes("__vz__/x", b"")


def test_read_only(tmp_path):
    arc = tmp_path / "e.vzip"
    with open(arc, "wb") as f, VZipWriter(f):
        pass
    s = VZipStore(str(arc))
    with pytest.raises(PermissionError):
        sync(s.set("k", default_buffer_prototype().buffer.from_bytes(b"")))
