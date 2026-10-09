"""The cutting of contiguous values into chunks (spec/conventions.md §7, "Contiguous
values"); js/test/chunks.test.ts has the same cases."""

import pytest

from vzip.virtualize.common import MAX_CHUNK, grid_chunks, row_chunks


def read(o: int, n: int) -> bytes:
    return bytes([7]) * n


def test_grid_chunks():
    # Small values: one chunk; scalars; empty arrays.
    assert grid_chunks(100, [3, 4], 2) == ([3, 4], {(0, 0): [(100, 24)]})
    assert grid_chunks(100, [], 8) == ([], {(): [(100, 8)]})
    assert grid_chunks(100, [0, 5], 1) == ([1, 5], {})
    # Rows of 2^23 + 1 bytes: one per chunk, never two.
    shape, chunks = grid_chunks(0, [3, (1 << 23) + 1], 1)
    assert shape == [1, (1 << 23) + 1] and list(chunks) == [(0, 0), (1, 0), (2, 0)]
    # A row over 2^24 bytes cuts the next axis, balanced: [1, 40e6] in three chunks, the last padded by 2.
    shape, chunks = grid_chunks(10, [1, 40_000_000], 1)
    assert shape == [1, 13_333_334] and chunks[(0, 2)] == [(10 + 2 * 13_333_334, 13_333_332), bytes(2)]
    # Rows of 2 KiB: 74 chunks would pad by 73 rows; 96 chunks pad by one (the least), not a deeper cut.
    assert grid_chunks(0, [599_326, 2048], 1)[0] == [8099, 2048]
    shape, chunks = grid_chunks(0, [599_327, 2048], 1)
    assert shape == [6243, 2048] and len(chunks) == 96
    # Five rows of 8 MiB: two per chunk would pad 8 MiB; five chunks of one row pad nothing.
    shape, chunks = grid_chunks(0, [5, 1 << 23], 1)
    assert shape == [1, 1 << 23] and len(chunks) == 5
    # A prime count of 100 kB rows: no count of chunks avoids padding, so the next axis is cut.
    shape, chunks = grid_chunks(0, [1009, 100_000], 1)
    assert shape == [1, 100_000] and len(chunks) == 1009
    # ... and without deepening, the padded edge chunk is copied.
    shape, chunks = grid_chunks(0, [1009, 100_000], 1, read, deepen=False)
    assert shape == [145, 100_000] and chunks[(6, 0)] == bytes([7]) * 139 * 100_000 + bytes(6 * 100_000)
    # A smaller chunk limit (NIfTI's 2^17).
    shape, chunks = grid_chunks(0, [100, 64, 64], 2, limit=1 << 17)
    assert shape == [10, 64, 64] and len(chunks) == 10
    for parts in chunks.values():
        assert sum(p[1] if isinstance(p, tuple) else len(p) for p in parts) <= 1 << 17
    # Row chunks: rows along the first axis only.
    assert row_chunks(0, 10, 4, (0,)) == (10, {(0, 0): [(0, 40)]})


def test_grid_chunks_rejects_an_element_over_the_limit():
    with pytest.raises(ValueError, match=f"element of more than {MAX_CHUNK} bytes"):
        grid_chunks(0, [2], MAX_CHUNK + 1)


def test_grid_chunks_rejects_a_padding_to_copy_without_a_reader():
    with pytest.raises(ValueError, match="padding does not fit"):
        grid_chunks(0, [1009, 100_000], 1, deepen=False)


def test_row_chunks_rejects_a_row_over_2_24_bytes():
    with pytest.raises(ValueError, match="row of more than 2"):
        row_chunks(0, 2, MAX_CHUNK + 1)
