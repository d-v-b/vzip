import asyncio
import itertools

import pytest
from zarr.core.sync import sync

from vzip.archive import VZipWriter
from vzip.errors import EntryError
from vzip.store import VZipStore


def _write(path, page_size):
    """An archive whose keys exercise the listing's seeks: siblings that sort
    between a child and its subtree ("a-b", "a.x" before "a/"), a leaf and a
    subtree with the same name, nested groups, hidden keys, non-ASCII keys."""
    with open(path, "wb") as f, VZipWriter(f, page_size=page_size) as w:
        w.add_bytes("zarr.json", b"{}", late=True)
        for g in ("a", "b", "g/h"):
            w.add_bytes(f"{g}/zarr.json", b"{}", late=True)
        for i in range(200):
            w.add_ref(f"a/c/{i}", "file:///nowhere", i, 1)
        for i in range(50):
            w.add_ref(f"b/c/{i}", "file:///nowhere", i, 1)
            w.add_hidden(f"idx/{i}", b"x")
        for k in ("a-b", "a.x", "a/c0", "c", "c/d", "g/h/c/0", "é/x", "é/é"):
            w.add_ref(k, "file:///nowhere", 0, 1)


def _children(keys, prefix):
    out = []
    for k in sorted(keys):
        if k.startswith(prefix):
            child = k[len(prefix):].split("/", 1)[0]
            if child not in out:
                out.append(child)
    return out


async def _list_dir(s, prefix):
    return [k async for k in s.list_dir(prefix)]


async def _list(s):
    return [k async for k in s.list()]


def test_list_dir_finds_every_child(tmp_path):
    for page_size, resolve in itertools.product([None, 64, 300, 1 << 16], [True, False]):
        arc = tmp_path / f"{page_size}.vzip"
        _write(arc, page_size)
        keys = sync(_list(VZipStore(str(arc), resolve=resolve)))
        for prefix in ["", "a", "a/", "a/c", "b", "c", "g", "g/h/", "é", "nope", "__vz__"]:
            s = VZipStore(str(arc), resolve=resolve)
            norm = prefix.rstrip("/") + "/" if prefix else ""
            assert sync(_list_dir(s, prefix)) == _children(keys, norm), (page_size, resolve, prefix)
        if page_size == 64:
            # the root's children start on a few pages; the chunk keys fill the rest
            s = VZipStore(str(arc), resolve=resolve)
            sync(_list_dir(s, ""))
            assert len(s._loaded) < len(s._pages) // 4


def test_list_dir_fails_on_an_unparseable_page(tmp_path):
    arc = tmp_path / "x.vzip"
    _write(arc, 64)
    s = VZipStore(str(arc))
    sync(s._open())
    raw = bytearray(arc.read_bytes())
    at = s._cd_offset + s._pages[0].offset
    raw[at : at + 4] = b"XXXX"  # the signature of the first record of page 0
    arc.write_bytes(bytes(raw))
    with pytest.raises(EntryError, match="page 0 cannot be parsed"):
        sync(_list_dir(VZipStore(str(arc)), ""))


def test_concurrent_lookups_share_one_page_read(tmp_path):
    arc = tmp_path / "x.vzip"
    _write(arc, 1 << 16)
    s = VZipStore(str(arc))
    sync(s._open())
    opened = s.stats.archive_requests

    async def lookups():
        return await asyncio.gather(*(s.exists(f"a/c/{i}") for i in range(200)))

    assert all(sync(lookups()))
    assert s.stats.archive_requests - opened == len(s._loaded) == 1
